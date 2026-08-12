"""Memory Policy 学习层：调用计数 / 重要度 / 时效衰减 / 渐进遗忘 / 短期→长期巩固 / 修剪 /
Bayesian 置信度更新（可信度视为后验概率，用似然比随证据更新）与置信度标定。
遗忘分三档：清晰 → 模糊 → 遗忘。

v22：① 贝叶斯更新按事实类型加阻力（稳定事实 3x / 主观偏好 2x，玩笑与单次异常难撼动）；
② 遗忘曲线按用户活跃密度缩放半衰期（密集交流=近期重要，稀疏交流=琐碎记忆快淡出）。"""

import re
import time
from datetime import datetime, timedelta

from plugins import _db, _shared


def _cfg(key, default):
    policy = (_shared.CONFIG.get("memory", {}).get("core", {}) or {}).get("policy", {}) or {}
    return policy.get(key, default)


# ===== Bayesian 置信度更新 =====
def _lr(kind: str) -> float:
    """证据似然比：确认 2.0、反驳 0.3、轻微冲突 0.5（可在 config 调整）。"""
    policy_cfg = (_shared.CONFIG.get("memory", {}).get("core", {}) or {}).get("policy", {}) or {}
    table = {
        "confirm": float(policy_cfg.get("confirm_lr", 2.0)),
        "dispute": float(policy_cfg.get("dispute_lr", 0.3)),
        "conflict": float(policy_cfg.get("conflict_lr", 0.5)),
    }
    return table.get(kind, 1.0)


# 强稳定锚点：出现即 stable（生日/血型这类几乎不可能被过程语境误伤）
STABLE_ANCHOR = (
    "生日", "血型", "星座", "身份证", "手机号", "邮箱", "真名", "本名",
    "已婚", "未婚", "离婚", "国籍", "学历", "老家", "入职", "毕业", "学校",
    "父母", "爸妈", "兄弟", "姐妹",
)
# 需要语境确认的稳定词：词出现 + 语境模式（正则）命中才判 stable。
# 解决"今天工作很累（过程）"/"记住这个地址（指令）"这类子串误伤。
STABLE_CTX = [
    ("地址", (r"地址是", r"地址为", r"地址在", r"住址", r"我的地址", r"家在", r"家住", r"住在", r"地址[:：]")),
    ("工作", (r"在[^，。]{0,8}工作", r"工作单位", r"工作地点", r"在哪工作", r"上班", r"公司工作")),
    ("公司", (r"在[^，。]{0,8}公司", r"公司工作", r"入职公司", r"公司的")),
]
# 过程标记：命中则降级为 process（除非已命中强锚点）
PROCESS_MARK = (
    "很累", "好累", "太累", "累了", "累死", "累得", "加班", "很忙", "太忙",
    "忙死", "累到", "上班很累",
)
PREF_HINT = (
    "喜欢", "讨厌", "最爱", "爱吃", "爱喝", "忌口", "不吃", "偏好", "口味",
    "习惯", "不喜欢",
)


def fact_class(scope, key, fact) -> str:
    """事实类型：stable 客观稳定事实 / preference 主观偏好 / process 过程状态。
    贝叶斯更新按类型加抗噪阻力：稳定事实不怕玩笑，偏好不因单次异常翻转。
    v2.2+：子串匹配改为"强锚点 + 语境确认 + 过程标记降级"，避免
    "今天工作很累"（含'工作'）/ "记住这个地址"（含'住'）被误判 stable。"""
    t = str(fact or "")
    if key in ("identity", "birthday", "blood_type", "profile"):
        return "stable"
    if any(w in t for w in STABLE_ANCHOR):
        return "stable"
    if any(m in t for m in PROCESS_MARK):
        return "process"
    if any(re.search(p, t) for _w, patterns in STABLE_CTX for p in patterns):
        return "stable"
    if key in ("preference", "偏好", "喜好") or any(w in t for w in PREF_HINT):
        return "preference"
    return "process"


# ===== 分类评测探针（含关键词但其实是过程/指令的句子）=====
CLASSIFY_PROBES = [
    ("今天工作很累", "process"),          # 含"工作"但过程
    ("记住这个地址", "process"),          # 含"住"但是指令
    ("我的地址是上海市徐汇区", "stable"),
    ("我在腾讯工作", "stable"),
    ("上周三买了只猫", "process"),
    ("她家住在广州", "stable"),
    ("生日是八月十二号", "stable"),
    ("今天加班到十点", "process"),
    ("公司组织团建", "process"),
    ("喜欢喝冰美式", "preference"),
]


def classify_report() -> dict:
    """跑一遍分类探针，返回误判清单与准确率（policy-classify 的数据源）。"""
    errors = []
    for text, expect in CLASSIFY_PROBES:
        got = fact_class("c2c:probe", "", text)
        if got != expect:
            errors.append({"text": text, "expected": expect, "got": got})
    return {
        "total": len(CLASSIFY_PROBES),
        "errors": errors,
        "accuracy": round(1 - len(errors) / len(CLASSIFY_PROBES), 3),
    }


def resistance_for(cls) -> float:
    """证据阻力：单次证据的冲击被削弱到 1/resistance。
    stable=3 / preference=2 / process=1（保持原行为）。"""
    return {"stable": 3.0, "preference": 2.0}.get(cls, 1.0)


# ===== 类别化保留（v31）：不同类别记忆用不同衰减机制 =====
# 半衰期单位：天。None → 回退 decay_days；0 → 永不忘（不随时间衰减）。
CLASS_HALF_LIFE_DEFAULT = {
    "core": 0.0,        # 人格/核心：永不忘，只随证据更新
    "stable": 720.0,    # 客观稳定事实（生日/血型/身份）：≈ 2 年半衰期
    "preference": 360.0,  # 主观偏好：≈ 1 年
    "long": 240.0,      # 巩固后的长期记忆：≈ 8 个月
    "process": None,    # 过程/情景事实：回退 decay_days（默认曲线）
    "short": 60.0,      # 未巩固的短期记忆：≈ 2 个月
    "dream": 1.5,       # 梦的模糊记忆：1.5 天半衰期，很快忘光
}

# AI 自身的人格类字段（scope=ai 下按 key 判定，避免反向依赖 agent/persona）
_AI_CORE_KEYS = {
    "identity", "personality", "preference", "style", "avoid", "defaults",
    "value", "catchphrase", "mood_profile", "experience_persona", "examples",
    "motivation", "relationship", "conflict", "behavior_policy",
}


def class_of(scope, key, fact, mclass="") -> str:
    """记忆类别：core（人格/核心）→ stable → preference → long → short → process。
    决定用哪套衰减机制（半衰期 + 是否可删除）。"""
    if mclass == "core":
        return "core"
    if (key or "") == "dream":
        return "dream"
    if scope == "ai" or str(scope or "").startswith("ai:"):
        return "core" if (key or "") in _AI_CORE_KEYS else "long"
    cls = fact_class(scope, key, fact)
    if cls == "stable":
        return "stable"
    if cls == "preference":
        return "preference"
    if mclass == "long":
        return "long"
    if mclass == "short":
        return "short"
    return "process"


def class_half_life(cls) -> float:
    """类别半衰期（天）；返回 0 表示永不忘。config → policy.class_half_life 可覆盖。"""
    table = _cfg("class_half_life", CLASS_HALF_LIFE_DEFAULT) or CLASS_HALF_LIFE_DEFAULT
    try:
        h = table.get(cls, CLASS_HALF_LIFE_DEFAULT.get(cls))
        if h is None:
            return decay_days()
        return float(h)
    except (TypeError, ValueError):
        return decay_days()


def half_life_for(scope, key, fact, mclass="") -> float | None:
    """按类别的半衰期（天）；None 表示该类别不随时间衰减（永不忘）。"""
    h = class_half_life(class_of(scope, key, fact, mclass))
    return None if h <= 0 else h


def update(confidence, kind, resistance=1.0, clamp=(0.05, 0.99)):
    """贝叶斯更新：confidence 为当前后验，kind ∈ confirm/dispute/conflict。
    resistance≥1 时有效 LR = 1 + (LR−1)/resistance：单次玩笑/轻纠错冲击被削弱，
    但多次明确的强证据（LR 仍偏离 1）最终仍能翻转。
    v31：确认（confirm）证据不受阻力稀释——稳定的东西被反复确认应该更可信。"""
    p = min(max(float(confidence), 0.001), 0.999)
    lr = _lr(kind)
    if kind == "confirm":
        eff_lr = lr
    else:
        eff_lr = 1.0 + (lr - 1.0) / max(1.0, float(resistance))
    odds = p / (1.0 - p) * eff_lr
    posterior = odds / (1.0 + odds)
    return round(min(clamp[1], max(clamp[0], posterior)), 4)


# ===== 置信度标定（弱监督分桶）=====
BUCKETS = [(0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01)]


def _calibration_key():
    return "memory", "calibration"


def calibrate_train(probes, k=5) -> dict:
    """用评测集（命中=正例）分桶统计实际正确率，产出校准映射并用于检索打分。"""
    from memory import reasoning
    buckets = {b: {"n": 0, "pos": 0} for b in BUCKETS}
    samples = []
    for p in probes:
        scopes = [p["scope"]] if p.get("scope") else list(
            dict.fromkeys(r["scope"] for r in _db.memory_rows())
        )
        hits = reasoning.retrieve(p["query"], scopes, top_k=k, min_score=0.0)
        expected = p["expected"]
        for fact, _s, _sc in hits:
            conf = 0.7
            for r in _db.memory_rows(_sc):
                if r["fact"] == fact:
                    conf = float(r.get("confidence", 0.7))
                    break
            label = 1 if any(e in fact or fact in e for e in expected) else 0
            samples.append((conf, label))
            for lo, hi in BUCKETS:
                if lo <= conf < hi:
                    b = buckets[(lo, hi)]
                    b["n"] += 1
                    b["pos"] += label
                    break
    report = {}
    mapping = {}
    for (lo, hi), v in buckets.items():
        acc = v["pos"] / v["n"] if v["n"] else None
        report[f"{lo:.1f}-{hi:.1f}"] = {"n": v["n"], "accuracy": round(acc, 3) if acc is not None else None}
        if v["n"] >= 3 and acc is not None:
            mapping[(lo, hi)] = round(min(0.99, max(0.05, acc)), 3)
    if samples:
        naive = sum(l for _c, l in samples) / len(samples)
        report["naive_accuracy"] = round(naive, 3)
        report["samples"] = len(samples)
    _db.kv_set(*_calibration_key(), {"mapping": [[lo, hi, v] for (lo, hi), v in mapping.items()]})
    return report


def calibrate_adjust(confidence) -> float:
    """用已训练映射校准置信度；未训练时原样返回。"""
    data = _db.kv_get(*_calibration_key())
    if not data or not data.get("mapping"):
        return float(confidence)
    for lo, hi, v in data["mapping"]:
        if lo <= float(confidence) < hi:
            return float(v)
    return float(confidence)


def calibrate_report() -> str:
    data = _db.kv_get(*_calibration_key())
    if not data:
        return "尚未训练标定（用评测集跑 memory-calibrate）"
    return "置信度标定已启用：" + str(data.get("mapping", []))


def decay_days() -> float:
    return float(_cfg("decay_days", 90))


def arousal_half_factor(arousal) -> float:
    """情绪锚定系数（Twig）：高唤醒记忆半衰期更长（忘得慢），低唤醒更快淡出。
    可配 policy.arousal_boost（默认 1.0）。已接入 stats_for / memory_strength / forget。"""
    try:
        a = float(arousal or 0.0)
    except (TypeError, ValueError):
        a = 0.0
    return max(0.25, 1.0 + a * _cfg("arousal_boost", 1.0))


def prune_importance() -> float:
    return float(_cfg("prune_importance", 0.15))


def fuzzy_strength() -> float:
    return float(_cfg("fuzzy_strength", 0.15))


def forget_days() -> float:
    return float(_cfg("forget_days", 120))


def promote_min_access() -> int:
    return int(_cfg("promote_min_access", 3))


def promote_min_importance() -> float:
    return float(_cfg("promote_min_importance", 0.6))


def touch(scope, key, fact, importance=0.5):
    """被提取/注入时更新访问计数与最后访问时间。"""
    _db.meta_touch(scope, key, fact, importance=float(importance))


def _current_confidence(scope, key, fact) -> float:
    for r in _db.memory_rows(scope, key):
        if r["fact"] == fact:
            return float(r.get("confidence", 0.7))
    return 0.7


def confirm(scope, key, fact, delta=None):
    """确认反馈：贝叶斯上调可信度（LR=confirm_lr，默认 2.0）。返回新可信度。"""
    conf = update(_current_confidence(scope, key, fact), "confirm")
    _db.memory_set_confidence(scope, key, fact, conf)
    _db.feedback_add(scope, key, "confirm", fact=fact, weight=0.5)
    return conf


def dispute(scope, key, fact, delta=None):
    """反驳反馈：贝叶斯下调可信度（LR=dispute_lr，默认 0.3）。返回新可信度。"""
    conf = update(_current_confidence(scope, key, fact), "dispute")
    _db.memory_set_confidence(scope, key, fact, conf)
    _db.feedback_add(scope, key, "dispute", fact=fact)
    return conf


BASE_DENSITY = 4.0  # 条/天：常规活跃度基准
_density_cache = {}
_DENSITY_TTL = 60.0


def _activity_density(scope, window_days=7.0) -> float:
    """scope 近期活跃密度：近 window_days 天新增/更新的记忆条数 / 天数。"""
    if not scope:
        return 0.0
    now = time.time()
    hit = _density_cache.get(scope)
    if hit and now - hit["ts"] < _DENSITY_TTL:
        return hit["density"]
    cutoff = (datetime.now() - timedelta(days=window_days)).isoformat(timespec="seconds")
    n = sum(1 for r in _db.memory_rows(scope) if (r.get("updated_at") or "") >= cutoff)
    density = n / window_days
    _density_cache[scope] = {"ts": now, "density": density}
    return density


def density_factor(scope) -> float:
    """活跃度缩放遗忘半衰期：密集交流 ×3（记得牢），稀疏交流 ×0.3（琐碎记忆快淡出）。
    AI 自身记忆不受用户活跃度影响（恒 1.0）。"""
    if not scope or scope == "ai" or scope.startswith("ai:"):
        return 1.0
    d = _activity_density(scope)
    if d <= 0:
        return 0.3
    return min(3.0, max(0.3, d / BASE_DENSITY))


def recency_factor(last_access, half_life=None, scope=None) -> float:
    """时效衰减：越久没被调用分数越低；half_life 可被情绪强度拉长（情绪锚定），
    再按用户活跃密度缩放（密集交流=近期重要，稀疏交流=更快淡出）。"""
    if not last_access:
        return 0.5
    try:
        last = datetime.fromisoformat(last_access)
        days = (datetime.now() - last).total_seconds() / 86400
    except Exception as e:
        _stats_err(e)
        return 0.5
    half = float(half_life) if half_life else decay_days()
    if scope:
        half *= density_factor(scope)
    return 0.5 ** (days / half) if half > 0 else 0.5


def retrieval_strength(access_count) -> float:
    """提取强度：每次成功召回都强化（对数增长，间隔重复效应）。"""
    import math
    return math.log1p(max(0, int(access_count)) + 1) / math.log1p(10)


def stats_for(scope, key, facts) -> dict:
    """批量取统计，返回 {fact: {access_count, importance, recency, arousal}}；recency 已含情绪锚定。"""
    rows = _db.meta_rows(scope, key)
    by_fact = {r["fact"]: r for r in rows}
    mem_map = {r["fact"]: r for r in _db.memory_rows(scope, key)}
    out = {}
    for f in facts:
        row = by_fact.get(f)
        if row:
            mrow = mem_map.get(f) or {}
            half = half_life_for(scope, key, f, mrow.get("mclass", ""))
            if half is None:
                rec = 1.0  # 人格/核心记忆不随时间衰减（v31）
            else:
                half = half * arousal_half_factor(mrow.get("arousal", 0.0))  # 情绪锚定
                rec = recency_factor(row["last_access"], half_life=half, scope=scope)
            out[f] = {
                "access_count": row["access_count"],
                "importance": float(row["importance"]),
                "recency": rec,
                "arousal": float(mrow.get("arousal", 0.0)),
            }
        else:
            out[f] = {"access_count": 0, "importance": 0.5, "recency": 0.5, "arousal": 0.0}
    return out


def memory_strength(row, meta_row) -> float:
    """记忆强度 = 重要度 × 提取强度 × 时效衰减（按类别半衰期，v31）。"""
    half = half_life_for(
        row.get("scope") or "", row.get("key") or "", row["fact"], row.get("mclass") or ""
    )
    rec = 1.0 if half is None else recency_factor(
        meta_row["last_access"],
        half_life=half * arousal_half_factor(row.get("arousal", 0.0)),
        scope=row.get("scope") or "",
    )
    return (
        float(meta_row["importance"])
        * retrieval_strength(meta_row["access_count"])
        * rec
    )


def forget(scope=None) -> dict:
    """渐进遗忘：低强度 + 久未提取 → 模糊档（可信度压到 0.25）；极低 → 删除。返回 {fuzzy, forgotten}。"""
    fuzzy = forgotten = 0
    th = fuzzy_strength()
    cutoff = datetime.now().isoformat(timespec="seconds")
    rows = {(r["scope"], r["key"], r["fact"]): r for r in _db.memory_rows(scope)}
    for m in _db.meta_rows(scope):
        row = rows.get((m["scope"], m["key"], m["fact"]))
        if not row:
            continue
        cls = class_of(
            row.get("scope") or "", row.get("key") or "", row["fact"], row.get("mclass") or ""
        )
        if cls == "core":
            continue  # 人格/核心记忆不参与遗忘（只随证据更新）
        strength = memory_strength(row, m)
        if strength < th * 0.4 and (m["last_access"] or "") < cutoff:
            if cls in ("stable", "preference"):
                # 稳定事实/偏好：只降模糊，不硬删除（宁可用"待核实"也不丢）
                cur = float(row.get("confidence", 0.7))
                if cur > 0.25:
                    _db.memory_set_confidence(row["scope"], row["key"], row["fact"], 0.25)
                    fuzzy += 1
            else:
                _db.memory_delete(row["scope"], row["key"], row["fact"])
                _db.meta_delete(row["scope"], row["key"], row["fact"])
                forgotten += 1
        elif strength < th:
            cur = float(row.get("confidence", 0.7))
            if cur > 0.25:
                _db.memory_set_confidence(row["scope"], row["key"], row["fact"], 0.25)
                fuzzy += 1
    _db.policy_log_add("forget", f"fuzzy={fuzzy} forgotten={forgotten}", detail="类别化遗忘")
    return {"fuzzy": fuzzy, "forgotten": forgotten}


def promote(scope=None) -> int:
    """巩固：高重要度 + 多次提取的短期记忆升为长期（mclass=long）。返回升迁数。"""
    n = 0
    min_access = promote_min_access()
    min_imp = promote_min_importance()
    rows = {(r["scope"], r["key"], r["fact"]): r for r in _db.memory_rows(scope)}
    for m in _db.meta_rows(scope):
        row = rows.get((m["scope"], m["key"], m["fact"]))
        if not row or row.get("mclass") != "short":
            continue
        if m["access_count"] >= min_access and float(m["importance"]) >= min_imp:
            _db.memory_add(
                row["scope"],
                row["key"],
                row["fact"],
                updated_at=row.get("updated_at") or "",
                confidence=float(row.get("confidence", 0.7)),
                source=row.get("source", ""),
                audience=row.get("audience", ""),
                speaker=row.get("speaker", ""),
                mclass="long",
                arousal=float(row.get("arousal", 0.0)),
                valence=float(row.get("valence", 0.0)),
            )
            n += 1
    _db.policy_log_add("promote", f"promoted={n}", detail="短期→长期巩固")
    return n


def prune(scope=None, key=None) -> int:
    """回收低价值记忆：重要度低于阈值则删除事实与元数据。返回删除条数。"""
    removed = 0
    threshold = prune_importance()
    for r in _db.meta_rows(scope, key):
        if float(r["importance"]) >= threshold:
            continue
        fact, sc, k = r["fact"], r["scope"], r["key"]
        if fact in _db.memory_get(sc, k):
            _db.memory_delete(sc, k, fact)
            _db.meta_delete(sc, k, fact)
            removed += 1
    _db.policy_log_add("prune", f"removed={removed}", detail="低价值记忆回收")
    return removed


def governance(scope=None) -> dict:
    """Memory Governance 报告（v3.1 §9）：遗忘/巩固/冲突/隐私 现状。"""
    rows = _db.memory_rows(scope)
    return {
        "total": len(rows),
        "core": sum(1 for r in rows if r.get("mclass") == "core"),
        "long": sum(1 for r in rows if r.get("mclass") == "long"),
        "short": sum(1 for r in rows if r.get("mclass") == "short"),
        "low_confidence": sum(1 for r in rows if float(r.get("confidence", 0.7)) < 0.3),
        "private": sum(1 for r in rows if float(r.get("privacy", 0.0)) >= 0.6),
        "recent_history_entries": len(_db.history_rows(scope, limit=100)),
        "policy_log_entries": len(_db.policy_log_rows(100)),
    }



def _stats_err(e):
    """裸 except 审计（v2.2）：错误计数 + 日志，供消融/排查。"""
    try:
        import memory.stats as _st
        _st.bump_err("policy", e)
    except Exception:
        pass
