"""Memory Policy 学习层：调用计数 / 重要度 / 时效衰减 / 渐进遗忘 / 短期→长期巩固 / 修剪 /
Bayesian 置信度更新（可信度视为后验概率，用似然比随证据更新）与置信度标定。
遗忘分三档：清晰 → 模糊 → 遗忘。"""

from datetime import datetime

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


def update(confidence, kind, clamp=(0.05, 0.99)):
    """贝叶斯更新：confidence 为当前后验，kind ∈ confirm/dispute/conflict。"""
    p = min(max(float(confidence), 0.001), 0.999)
    odds = p / (1.0 - p) * _lr(kind)
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


def recency_factor(last_access, half_life=None) -> float:
    """时效衰减：越久没被调用分数越低；half_life 可被情绪强度拉长（情绪锚定）。"""
    if not last_access:
        return 0.5
    try:
        last = datetime.fromisoformat(last_access)
        days = (datetime.now() - last).total_seconds() / 86400
    except Exception:
        return 0.5
    half = float(half_life) if half_life else decay_days()
    return 0.5 ** (days / half) if half > 0 else 0.5


def retrieval_strength(access_count) -> float:
    """提取强度：每次成功召回都强化（对数增长，间隔重复效应）。"""
    import math
    return math.log1p(max(0, int(access_count)) + 1) / math.log1p(10)


def stats_for(scope, key, facts) -> dict:
    """批量取统计，返回 {fact: {access_count, importance, recency, arousal}}；recency 已含情绪锚定。"""
    rows = _db.meta_rows(scope, key)
    by_fact = {r["fact"]: r for r in rows}
    arousal_map = {r["fact"]: float(r.get("arousal", 0.0)) for r in _db.memory_rows(scope, key)}
    out = {}
    for f in facts:
        row = by_fact.get(f)
        if row:
            half = decay_days() * (1.0 + arousal_map.get(f, 0.0))
            out[f] = {
                "access_count": row["access_count"],
                "importance": float(row["importance"]),
                "recency": recency_factor(row["last_access"], half_life=half),
                "arousal": arousal_map.get(f, 0.0),
            }
        else:
            out[f] = {"access_count": 0, "importance": 0.5, "recency": 0.5, "arousal": 0.0}
    return out


def memory_strength(row, meta_row) -> float:
    """记忆强度 = 重要度 × 提取强度 × 时效衰减（遗忘曲线的核心）。"""
    return (
        float(meta_row["importance"])
        * retrieval_strength(meta_row["access_count"])
        * recency_factor(
            meta_row["last_access"],
            half_life=decay_days() * (1.0 + float(row.get("arousal", 0.0))),
        )
    )


def forget(scope=None) -> dict:
    """渐进遗忘：低强度 + 久未提取 → 模糊档（可信度压到 0.25）；极低 → 删除。返回 {fuzzy, forgotten}。"""
    fuzzy = forgotten = 0
    th = fuzzy_strength()
    cutoff = datetime.now().isoformat(timespec="seconds")
    rows = {(r["scope"], r["key"], r["fact"]): r for r in _db.memory_rows(scope)}
    for m in _db.meta_rows(scope):
        row = rows.get((m["scope"], m["key"], m["fact"]))
        if not row or row.get("mclass") == "core":
            continue
        strength = memory_strength(row, m)
        if strength < th * 0.4 and (m["last_access"] or "") < cutoff:
            _db.memory_delete(row["scope"], row["key"], row["fact"])
            _db.meta_delete(row["scope"], row["key"], row["fact"])
            forgotten += 1
        elif strength < th:
            cur = float(row.get("confidence", 0.7))
            if cur > 0.25:
                _db.memory_set_confidence(row["scope"], row["key"], row["fact"], 0.25)
                fuzzy += 1
    _db.policy_log_add("forget", f"fuzzy={fuzzy} forgotten={forgotten}", detail="渐进遗忘")
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
