"""互动调节层（v31）：把散在各系统的"拍脑袋常数"统一为上下文加权公式。

有效值 = 基准 × 场景系数 × 关系系数 × 用户状态系数 × 频率系数
- scene：私聊/群聊（群里少打扰）、正式事件（更重要）
- relation：陌生0.5 / 初识0.8 / 熟悉1.0 / 深度1.2（熟悉度含时间衰减，30 天半衰期）
- user：情绪低落（正向×0.4 / 关心×1.3）、用户深夜（打扰×0.6）、潜水（打扰×0.7）
- fatigue：同类事件当日第 k 次 ×0.8^(k-1)（刺激适应，重复会疲劳）

所有乘数 config → memory.core.interaction 可调，可被真实数据校准。
"""

from datetime import datetime

from plugins import _db, _shared

STAGE_MULT = {"陌生": 0.5, "初识": 0.8, "熟悉": 1.0, "深度伙伴": 1.2}
_NEG_LABELS = ("忧郁", "悲伤", "低落", "不安", "恐惧", "焦虑", "恼怒", "愤怒", "憎恶", "厌恶")


def _cfg(key, default):
    it = (_shared.CONFIG.get("memory", {}).get("core", {}) or {}).get("interaction", {}) or {}
    return it.get(key, default)


def _scene_of(scope) -> str:
    return "group" if str(scope or "").startswith("group") else "c2c"


# ===== 关系系数（含熟悉度时间衰减）=====
def familiarity_effective(scope) -> float:
    """熟悉度（计算值）：存储值 × 0.5^(距上次互动天数/半衰期)，1 天内不衰减。"""
    try:
        row = _db.relationship_get(scope)
    except Exception as e:
        _stats_err(e)
        row = None
    if not row:
        return 0.0
    fam = float(row.get("familiarity", 0.0))
    try:
        import json
        history = json.loads(row.get("history") or "[]")
        last_ts = max((h.get("ts") or "" for h in history), default="")
        if last_ts:
            days = max(0.0, (datetime.now() - datetime.fromisoformat(last_ts)).total_seconds() / 86400.0)
            half = float(_cfg("familiarity_half_life_days", 30))
            if days > 1.0 and half > 0:
                fam *= 0.5 ** ((days - 1.0) / half)
    except Exception as e:
        _stats_err(e)
        pass
    return round(max(0.0, min(1.0, fam)), 4)


def relation_mult(scope) -> float:
    """关系阶段 → 乘数（陌生0.5/初识0.8/熟悉1.0/深度1.2）。"""
    fam = familiarity_effective(scope)
    stage = "深度伙伴" if fam >= 0.65 else ("熟悉" if fam >= 0.4 else ("初识" if fam >= 0.2 else "陌生"))
    table = _cfg("relation_mult", STAGE_MULT) or STAGE_MULT
    return float(table.get(stage, 1.0))


# ===== 场景系数 =====
def scene_mult(kind, scene="c2c") -> float:
    """场景：群里少打扰；正式事件更重要。"""
    if kind in ("formal", "poke_formal", "consult_formal"):
        return 1.2
    if scene == "group" and kind in ("share", "poke", "proactive"):
        return 0.7
    return 1.0


# ===== 用户状态系数 =====
def user_mult(scope, now=None) -> dict:
    """用户状态 → {disturb, care, neutral} 乘数。"""
    now = now or datetime.now()
    m = {"disturb": 1.0, "care": 1.0, "neutral": 1.0}
    if not scope:
        return m
    # 情绪：低落 → 少打扰、多关心
    try:
        from memory import emotion as emotion_mod
        est = emotion_mod.user_estimate(scope)
        if est and est.get("label") in _NEG_LABELS:
            m["disturb"] *= 0.4
            m["care"] *= 1.3
    except Exception as e:
        _stats_err(e)
        pass
    # 用户时区深夜 → 少打扰
    try:
        from memory import tz as tz_mod
        from zoneinfo import ZoneInfo
        hour = now.astimezone(ZoneInfo(tz_mod.user_tz(scope))).hour
        lo, hi = _cfg("user_night_hours", [23, 7])
        if hour >= int(lo) or hour < int(hi):
            m["disturb"] *= 0.6
    except Exception as e:
        _stats_err(e)
        pass
    # 潜水：最近没消息 → 少打扰
    try:
        last = _db.kv_get("memory", f"lastmsg:{scope}", "") or ""
        if last:
            days = (now - datetime.fromisoformat(last)).total_seconds() / 86400.0
            if days > float(_cfg("dormant_days", 3)):
                m["disturb"] *= 0.7
    except Exception as e:
        _stats_err(e)
        pass
    return {k: round(v, 3) for k, v in m.items()}


# ===== 频率系数（刺激适应）=====
_fatigue_cache = {}


def fatigue_mult(kind, now=None) -> float:
    """同类事件当日第 k 次 → 0.8^(k-1)。"""
    now = now or datetime.now()
    cache_key = f"{now.date().isoformat()}:{kind}"
    hit = _fatigue_cache.get(cache_key)
    if hit is not None:
        return float(_cfg("fatigue_decay", 0.8)) ** hit
    counts = _db.kv_get("memory", f"interaction_counts:{now.date().isoformat()}") or {}
    n = int(counts.get(kind, 0))
    _fatigue_cache[cache_key] = n
    return float(_cfg("fatigue_decay", 0.8)) ** n


def mark_event(kind, now=None):
    """事件计数 +1（供 fatigue_mult 使用）。"""
    now = now or datetime.now()
    key = f"interaction_counts:{now.date().isoformat()}"
    counts = _db.kv_get("memory", key) or {}
    counts[kind] = int(counts.get(kind, 0)) + 1
    _db.kv_set("memory", key, counts)
    _fatigue_cache[f"{now.date().isoformat()}:{kind}"] = int(counts[kind])


# ===== 统一入口 =====
def modulate(scope, kind, base=1.0, now=None, scene="", axis="neutral",
             with_relation=True, with_fatigue=True) -> float:
    """有效值 = 基准 × 场景 × 关系 × 用户状态 × 频率。
    with_relation=False：关系系统自身更新时不乘关系系数（避免富者愈富）。
    with_fatigue=False：累积证据类（关系）不适用刺激适应——重复可靠应更涨信任。"""
    try:
        import memory.stats as _st
        _st.bump("tick:interaction")
    except Exception as e:
        _stats_err(e)
    now = now or datetime.now()
    scene = scene or _scene_of(scope)
    m = float(base)
    m *= scene_mult(kind, scene)
    if with_relation:
        m *= relation_mult(scope)
    um = user_mult(scope, now)
    m *= float(um.get(axis, 1.0))
    if with_fatigue:
        m *= fatigue_mult(kind, now)
    return round(max(0.05, m), 3)



def _stats_err(e):
    """裸 except 审计（v2.2）：错误计数 + 日志，供消融/排查。"""
    try:
        import memory.stats as _st
        _st.bump_err("interaction", e)
    except Exception:
        pass
