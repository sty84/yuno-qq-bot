"""错误与原谅（v23）：跟踪用户重复犯的错 → 生气度 → 随时间衰减 → 道歉/关系决定是否松口。

- 记录：从对话识别「又迟到/放鸽子/忘了/骗你」等自述，或约定迟到（appointment 联动）；
- 分类：普通失误（迟到/放鸽子/忘记）vs 底线（撒谎/欺骗/越界/伤害）；
- 生气度：同类别重复犯错 +1（封顶 3 → 习以为常），随时间衰减（0.5^(天/半衰期)）；
- 原谅：底线类不道歉绝不松口；道歉后按 关系分/信任/冷静程度 计算松口概率（随机判定）；
- 注入：当前消息命中同类错误时，告诉 AI 该有的态度（提一嘴/生气/习以为常/冷淡）。
"""

import hashlib
import re
from datetime import datetime, timedelta

from plugins import _db, _shared
from memory import relationship

KV_NS = "memory"

LATE_CAT = "迟到/放鸽子"
FORGET_CAT = "忘记约定"
LIE_CAT = "撒谎/欺骗"
BOUNDARY_CAT = "越界/伤害"
OTHER_CAT = "其他失误"
BOTTOM_LINE_CATS = {LIE_CAT, BOUNDARY_CAT}

CATEGORY_HINTS = {
    LATE_CAT: ("迟到", "失约", "爽约", "鸽了", "没来", "又没到"),
    FORGET_CAT: ("忘了", "忘记", "记错", "错过"),
    LIE_CAT: ("骗", "撒谎", "欺骗", "隐瞒", "说谎"),
    BOUNDARY_CAT: ("越界", "偷", "泄密", "背叛", "出卖", "利用", "侮辱", "伤害", "人身攻击", "隐私"),
}
APOLOGY_HINTS = ("对不起", "抱歉", "我错了", "我的错", "不好意思", "原谅我", "下不为例", "下次不会")
_LATE_RE = re.compile(r"放.{0,3}鸽子")

ANGER_CAP = 3.0
HALF_LIFE_DAYS = 7.0


def _cfg(key, default):
    core = (_shared.CONFIG.get("memory", {}).get("core", {}) or {}).get("mistake", {}) or {}
    return core.get(key, default)


def _records(scope):
    return _db.kv_get(KV_NS, f"mistakes:{scope}", []) or []


def _save(scope, recs):
    _db.kv_set(KV_NS, f"mistakes:{scope}", recs)


def _category_of(text) -> str:
    t = str(text or "")
    if _LATE_RE.search(t):  # 放鸽子 / 放你鸽子 / 放我鸽子
        return LATE_CAT
    for cat, hints in CATEGORY_HINTS.items():
        if any(h in t for h in hints):
            return cat
    return ""


_OTHER_SUBJECT_RE = re.compile(
    r"(你|他|她|他们|她们|人家)\s*(又\s*)?(?:没来|迟到|放鸽子|爽约|失约|忘了|忘记|骗|撒谎|欺骗|隐瞒|"
    r"越界|背叛|出卖|伤害|侮辱|利用)"
)


def _is_self_mistake(text) -> bool:
    """只记用户自己的错：主语是对方/第三方，或被动句式（被骗/被放鸽子）→ 不算。"""
    t = str(text or "")
    if re.search(r"别(?:忘了|忘记|忘掉|忘)|不要(?:忘|忘记)", t):
        return False  # “别忘了/不要忘”是祈使句，不是认错
    if re.search(r"(你|他|她|他们|她们|人家)\s*(又\s*)?放.{0,3}鸽子", t):
        return False  # “你又放我鸽子”是对方放用户鸽子
    if _OTHER_SUBJECT_RE.search(t):
        return False
    if "被" in t or "让人" in t:
        return False
    return True


def _days_since(iso, now=None):
    try:
        d = datetime.fromisoformat(iso)
        return max(0.0, (now - d).total_seconds() / 86400)
    except Exception as e:
        _stats_err(e)
        return 0.0


def anger_of(rec, now=None) -> dict:
    """当前生气度：基数 = 次数（封顶 3），随时间衰减。返回 {level, label}。"""
    now = now or datetime.now()
    base = min(float(rec.get("count", 1)), ANGER_CAP)
    days = _days_since(rec.get("last_at") or rec.get("first_at") or "", now)
    level = round(base * (0.5 ** (days / HALF_LIFE_DAYS)), 2)
    count = int(rec.get("count", 1))
    if count >= int(ANGER_CAP):
        label = "习以为常"
    elif level >= 2:
        label = "生气"
    elif level >= 1:
        label = "不满"
    else:
        label = "快释然"
    return {"level": level, "label": label}


def forgive_probability(rec, scope, now=None) -> float:
    """松口概率 = f(关系分/信任/熟悉度, 冷静程度, 犯错次数)。
    底线类未道歉 → 0（绝不松口）。"""
    now = now or datetime.now()
    row = _db.relationship_get(scope) or {}
    trust = float(row.get("trust", 0.3))
    fam = float(row.get("familiarity", 0.0))
    try:
        score = relationship.score_of(row)
    except Exception as e:
        _stats_err(e)
        score = 0.0
    rel_norm = min(1.0, 0.3 * trust + 0.3 * fam + 0.4 * min(1.0, max(0.0, score) / 5.0))
    calm = min(1.0, _days_since(rec.get("last_at") or rec.get("first_at") or "", now) / 7.0)
    count_penalty = 0.05 * max(0, int(rec.get("count", 1)) - 1)
    if rec.get("category") in BOTTOM_LINE_CATS:
        if not int(rec.get("apology_count", 0)):
            return 0.0
        p = 0.25 + 0.45 * rel_norm + 0.30 * calm - count_penalty
    else:
        p = 0.50 + 0.30 * rel_norm + 0.20 * calm - 0.08 * max(0, int(rec.get("count", 1)) - 1)
    return round(min(0.95, max(0.05, p)), 2)


def record(scope, text, category="", now=None) -> dict:
    """记录一次错误（同类计数 +1）。返回 {recorded, category, count, label}。"""
    if not scope or scope == "ai" or scope.startswith("ai:"):
        return {"recorded": 0}
    cat = category or _category_of(text)
    if not cat:
        return {"recorded": 0}
    if not _is_self_mistake(text):
        return {"recorded": 0, "category": cat, "skip": "主语非用户或被动句式"}
    now = now or datetime.now()
    ts = now.isoformat(timespec="seconds")
    recs = _records(scope)
    rec = next((r for r in recs if r.get("category") == cat and r.get("status") == "active"), None)
    if rec:
        rec["count"] = int(rec.get("count", 1)) + 1
        rec["last_at"] = ts
        rec["text"] = str(text or rec.get("text", ""))[:80]
    else:
        rec = {
            "id": len(recs) + 1,
            "scope": scope,
            "category": cat,
            "text": str(text or "")[:80],
            "first_at": ts,
            "last_at": ts,
            "count": 1,
            "apology_count": 0,
            "status": "active",
        }
        recs.append(rec)
    _save(scope, recs)
    return {"recorded": 1, "category": cat, "count": int(rec["count"]), **anger_of(rec, now)}


def record_no_show(scope, appt_text="") -> dict:
    """约定迟到联动（appointment 模块调用）：用户没出现 = 一次放鸽子。"""
    return record(scope, f"放鸽子（约定「{appt_text}」没出现）", category=LATE_CAT)


def _apologize(scope, text, now=None, rng=None) -> dict:
    """处理道歉：底线类之前绝不松口；道歉后按稳定骰子判定（同一天同一错误结果一致，不翻脸）。"""
    now = now or datetime.now()
    ts = now.isoformat(timespec="seconds")
    recs = _records(scope)
    forgiven, still = [], []
    for rec in recs:
        if rec.get("status") != "active":
            continue
        rec["apology_count"] = int(rec.get("apology_count", 0)) + 1
        rec["last_apology_at"] = ts
        p = forgive_probability(rec, scope, now)
        if _stable_draw(scope, rec["category"], now.date().isoformat(), p):
            rec["status"] = "forgiven"
            rec["forgiven_at"] = ts
            forgiven.append({"category": rec["category"], "p": p})
        else:
            still.append({"category": rec["category"], "p": p})
    _save(scope, recs)
    return {"apology": True, "forgiven": forgiven, "still_angry": still}


def _stable_draw(scope, category, date_str, p) -> bool:
    """确定性骰子：同一天同一用户同一错误 → 结果一致（不翻脸）；次日重新掷。"""
    seed = f"{scope}:{category}:{date_str}"
    h = int(hashlib.md5(seed.encode()).hexdigest()[:8], 16)
    return (h % 1000) / 1000.0 < float(p)


def process(scope, text, now=None, rng=None) -> dict:
    """对话入口：先记录错误（如有），再处理道歉（如有）。"""
    t = str(text or "").strip()
    if not t:
        return {}
    out = {}
    if _category_of(t):
        out.update(record(scope, t, now=now))
    if any(a in t for a in APOLOGY_HINTS):
        out.update(_apologize(scope, t, now=now, rng=rng))
    return out


def context_block(scope, text="") -> str:
    """错误上下文（只读，注入 prompt）：命中同类错误 → 提示 AI 该有的态度。"""
    if not scope:
        return ""
    t = str(text or "")
    recs = [r for r in _records(scope) if r.get("status") == "active"]
    if not recs:
        return ""
    now = datetime.now()
    if any(a in t for a in APOLOGY_HINTS):
        # 用户刚道歉：注入松口概率，让 AI 自己决定给不给台阶
        lines = ["【用户刚为之前的错误道歉】"]
        for rec in recs[:2]:
            p = forgive_probability(rec, scope, now)
            st = anger_of(rec, now)
            if rec.get("category") in BOTTOM_LINE_CATS:
                guide = "这是涉及底线的事：可以冷淡地给个台阶，也可以继续不松口"
            else:
                guide = "可以顺势原谅，也可以傲娇地再端一会儿"
            lines.append(
                f"· {rec['category']}（第{rec.get('count')}次，你目前{st['label']}）："
                f"原谅概率约 {p:.0%}，{guide}。"
            )
        return "\n".join(lines)
    cat = _category_of(t)
    if not cat:
        return ""
    hits = [r for r in recs if r.get("category") == cat]
    if not hits:
        return ""
    lines = ["【用户近期错误】"]
    for rec in hits[:2]:
        st = anger_of(rec, now)
        days = int(_days_since(rec.get("last_at") or rec.get("first_at") or "", now))
        if rec.get("category") in BOTTOM_LINE_CATS and not int(rec.get("apology_count", 0)):
            guide = "涉及底线：除非对方认真道歉，否则别轻易给台阶"
        elif st["label"] == "习以为常":
            guide = "你已习惯，懒得生气，最多吐槽一句"
        elif st["label"] == "生气":
            guide = "你有点真的生气了，语气可以重，但别翻旧账没完"
        else:
            guide = "可以提一嘴，但别揪着不放"
        lines.append(
            f"· 用户上次「{rec.get('text', '')}」（{rec['category']}第{rec.get('count')}次，{days}天前）："
            f"你现在的态度是{st['label']}（生气度{st['level']}，随时间在降）。{guide}。"
        )
    return "\n".join(lines)



def _stats_err(e):
    """裸 except 审计（v2.2）：错误计数 + 日志，供消融/排查。"""
    try:
        import memory.stats as _st
        _st.bump_err("mistake", e)
    except Exception:
        pass
