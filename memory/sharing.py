"""分享欲 + 自动发消息（v31）：事件驱动 + 情绪联动 + 环境素材 + 反骚扰。

分享欲 S ∈ [0,1]：
- 事件增量（演出/作曲/梦/被夸…）→ S 涨；
- 指数衰减（半衰期默认 8h）——"想说的冲动"会消退；
- 情绪加成（复用 emotion VAD：高兴高唤醒 / 低落高唤醒 / 高支配）；
- 关系门槛（陌生×0.2 / 初识×0.5 / 熟悉×1.0 / 深度伙伴×1.2）；
- 触发（S≥阈值）→ 组装上下文（日程/环境/心情）→ LLM 生成人设消息
  → 走现有 notif 队列发给用户 → S 降残值 + 冷却 + 日/周上限。
"""

import json
import random
from datetime import date, datetime, timedelta

from plugins import _db, _shared

EVENT_DELTAS = {
    "performance": 0.5,
    "compose": 0.35,
    "stood_up": 0.35,
    "dream": 0.3,
    "friend": 0.3,
    "praised": 0.25,
    "rehearsal": 0.2,
    "shopping": 0.2,
    "exercise": 0.15,
    "dj_practice": 0.15,
    "user_down": 0.1,
    "playful": 0.05,
}

STAGE_MULT = {"陌生": 0.2, "初识": 0.5, "熟悉": 1.0, "深度伙伴": 1.2}

_ANNOY_PHRASES = (
    "别老发", "别发了", "别再发", "少发点", "少发", "别刷屏", "消息太多了",
    "烦不烦", "别老找", "不要老找", "安静点", "别烦",
)
_PLAYFUL_MARKERS = ("哈哈", "😄", "😂", "🤣", "开玩笑", "狗头", "玩梗", "闹着玩", "皮一下")

_NEG_USER_LABELS = ("忧郁", "悲伤", "低落", "不安", "恐惧", "焦虑", "恼怒", "愤怒", "憎恶", "厌恶")
_POSITIVE_REASONS = ("performance", "compose", "praised", "friend", "shopping", "dj_practice")

FALLBACK_MESSAGES = {
    "performance": "刚下台。……今天观众挺给面子的，新曲子没翻车。",
    "dream": "昨晚做了个梦，醒了就忘得差不多了。……没事，就是突然想跟你说一声。",
    "compose": "新曲子写完了……算半个吧，懒得改了。",
    "praised": "……谢了。这话我记下了，别想收回去。",
    "stood_up": "……行吧，又被放鸽子了。忙你的，我不催。",
    "friend": "刚跟朋友回来。……外面人真多，还是家里好。",
    "generic": "……刚忙完，突然想起你。没事，就随便发一句。",
}


def _cfg(key, default):
    s = (_shared.CONFIG.get("memory", {}).get("core", {}) or {}).get("sharing", {}) or {}
    return s.get(key, default)


def _state() -> dict:
    st = _db.kv_get("memory", "sharing_state") or {}
    if not st:
        st = {"S": 0.0, "ts": datetime.now().isoformat(timespec="seconds"),
              "last_trigger_ts": "", "day": "", "daily": 0, "week": "", "weekly": 0, "reasons": []}
    return st


def _save(st):
    _db.kv_set("memory", "sharing_state", st)


def _week_key(d=None) -> str:
    iso = (d or date.today()).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _decayed(st) -> float:
    """指数衰减：想说的冲动随时间消退。"""
    residual = float(_cfg("residual", 0.2))
    half = float(_cfg("half_life_hours", 8))
    try:
        hours = (datetime.now() - datetime.fromisoformat(st.get("ts") or "")).total_seconds() / 3600.0
    except Exception as e:
        _stats_err(e)
        hours = 0.0
    if hours <= 0 or half <= 0:
        return max(0.0, min(1.0, float(st.get("S", 0.0))))
    f = 0.5 ** (hours / half)
    return max(0.0, min(1.0, residual + (float(st.get("S", 0.0)) - residual) * f))


def add_delta(delta, reason=""):
    """事件增量（可被 schedule/sleep/对话等调用）。"""
    if not _cfg("enabled", True) or float(delta) <= 0:
        return
    st = _state()
    st["S"] = round(min(1.0, _decayed(st) + float(delta)), 3)
    st["ts"] = datetime.now().isoformat(timespec="seconds")
    reasons = list(st.get("reasons") or [])
    if reason and reason not in reasons:
        reasons.append(reason)
    st["reasons"] = reasons[-5:]
    _save(st)


def emotion_bonus() -> float:
    """情绪加成：从 emotion 状态机的 VAD 推导。"""
    try:
        from memory import emotion as emotion_mod
        s = emotion_mod.ai_state()
    except Exception as e:
        _stats_err(e)
        return 0.02
    v, a, d = s.get("v", 0.0), s.get("a", 0.0), s.get("d", 0.0)
    if v > 0.4 and a > 0.4:
        return 0.25
    if v < -0.3 and a > 0.3:
        return 0.2
    if d > 0.5:
        return 0.1
    return 0.02


def _stage_mult(scope) -> float:
    """关系门槛：熟悉度 → 分享欲折算系数。"""
    try:
        from memory import relationship as rel_mod
        row = _db.relationship_get(scope)
        if row:
            fam = float(row.get("familiarity", 0.0))
            stage = "深度伙伴" if fam >= 0.65 else ("熟悉" if fam >= 0.4 else ("初识" if fam >= 0.2 else "陌生"))
            return STAGE_MULT.get(stage, 0.5)
    except Exception as e:
        _stats_err(e)
        pass
    return 0.5  # 无记录按"初识"


def _penalty_mult(scope) -> float:
    """嫌烦惩罚（分级 + 连续恢复）：mult = 1 - step×次数 × 0.5^(年龄/窗口)。"""
    if not scope:
        return 1.0
    data = _db.kv_get("memory", f"sharing_penalty:{scope}") or {}
    count = max(0, int(data.get("count", 0)))
    if count <= 0:
        return 1.0
    try:
        age_h = (datetime.now() - datetime.fromisoformat(data["ts"])).total_seconds() / 3600.0
    except Exception as e:
        _stats_err(e)
        age_h = 0.0
    window = float(data.get("window_hours", _cfg("penalty_hours", 48)))
    p = min(0.6, float(_cfg("penalty_step", 0.25)) * count)
    decay = 0.5 ** (age_h / window) if window > 0 else 0.0
    return round(max(0.4, 1.0 - p * decay), 3)


def _reduce_penalty(scope):
    """原谅路径：用户之后给正向反馈 → 惩罚降一级，早恢复。"""
    if not scope:
        return
    data = _db.kv_get("memory", f"sharing_penalty:{scope}") or {}
    count = int(data.get("count", 0))
    if count <= 0:
        return
    if count <= 1:
        _db.kv_set("memory", f"sharing_penalty:{scope}", None)
        return
    data["count"] = count - 1
    data["ts"] = datetime.now().isoformat(timespec="seconds")
    _db.kv_set("memory", f"sharing_penalty:{scope}", data)


def on_annoyed(scope="", text=""):
    """用户嫌烦反馈（v31）：分级惩罚 + 玩笑豁免。返回惩罚等级（0=未触发）。"""
    if not scope or not any(p in (text or "") for p in _ANNOY_PHRASES):
        return 0
    if any(w in (text or "") for w in _PLAYFUL_MARKERS):
        return 0  # 玩笑式抱怨不算真嫌烦
    data = _db.kv_get("memory", f"sharing_penalty:{scope}") or {}
    count = min(3, int(data.get("count", 0)) + 1)
    window = float(_cfg("penalty_hours", 48))
    _db.kv_set("memory", f"sharing_penalty:{scope}",
               {"count": count, "ts": datetime.now().isoformat(timespec="seconds"),
                "window_hours": window})
    st = _state()
    st["S"] = max(0.0, _decayed(st) - 0.2 * count)
    st["ts"] = datetime.now().isoformat(timespec="seconds")
    _save(st)
    try:
        from memory import relationship as rel_mod
        rel_mod.update(scope, event="negative", detail="用户嫌消息多")
    except Exception as e:
        _stats_err(e)
        pass
    return count


def desire(scope="") -> dict:
    """当前有效分享欲：衰减后 S + 情绪加成，再乘关系门槛。"""
    st = _state()
    raw = min(1.0, _decayed(st) + emotion_bonus())
    mult = _stage_mult(scope)
    pen = _penalty_mult(scope)
    eff = round(min(1.0, raw * mult * pen), 3)
    return {"raw": round(raw, 3), "mult": mult, "penalty": pen,
            "effective": eff, "reasons": st.get("reasons") or []}


def _sent_reasons(now) -> list:
    data = _db.kv_get("memory", "sharing_sent") or {}
    if data.get("date") != now.date().isoformat():
        return []
    return data.get("reasons") or []


def _mark_sent_reason(now, reason):
    data = _db.kv_get("memory", "sharing_sent") or {}
    if data.get("date") != now.date().isoformat():
        data = {"date": now.date().isoformat(), "reasons": []}
    reasons = list(data.get("reasons") or [])
    if reason not in reasons:
        reasons.append(reason)
    _db.kv_set("memory", "sharing_sent", {"date": now.date().isoformat(), "reasons": reasons})


def _schedule_events(now=None) -> list:
    """当日日程里的可分享事件（每天每类只计一次）。"""
    now = now or datetime.now()
    # 深夜（22:00–06:00）不新计分享事件，避免"昨天的排练"在凌晨被当成今天的新事件
    if now.hour >= 22 or now.hour < 6:
        return []
    key = f"sharing_events:{now.date()}"
    data = _db.kv_get("memory", key) or {"kinds": []}
    kinds = set(data.get("kinds") or [])
    new = []
    try:
        from memory import schedule as schedule_mod
        plan = schedule_mod.get_week_plan()
        wd = now.weekday()
        if now.hour < 6:
            wd = (wd - 1) % 7
        acts = plan.get(wd, []) if plan else []
    except Exception as e:
        _stats_err(e)
        acts = []
    for slot, act in enumerate(acts):
        if act in EVENT_DELTAS and act not in ("praised", "stood_up", "user_down", "playful", "dream"):
            # 只统计"已经开始"的槽位（避免凌晨就把未来一整天的活动都预加进来）
            if slot != 3:
                start = {0: 6, 1: 12, 2: 18}.get(slot)
                if start is None or now.hour < start:
                    continue
            if act not in kinds:
                new.append(act)
                kinds.add(act)
    # 昨晚的梦：有模糊梦记忆或当日梦日志 → 计一次
    try:
        dream_rows = _db.memory_rows("ai", "dream")
        if dream_rows and "dream" not in kinds:
            new.append("dream")
            kinds.add("dream")
    except Exception as e:
        _stats_err(e)
        pass
    # 空间事件：到达演出/排练/迟到 → 当日分享素材（每天每类一次）
    try:
        from memory import space as space_mod
        for ev in space_mod.today_events(now):
            kind = ev.get("kind", "")
            detail = str(ev.get("detail", ""))
            mapped = None
            if kind == "arrive" and "演出" in detail:
                mapped = "performance"
            elif kind == "arrive" and ("排练" in detail or "录音" in detail):
                mapped = "rehearsal"
            elif "迟到" in detail or "放鸽子" in detail:
                mapped = "stood_up"
            if mapped and mapped not in kinds:
                new.append(mapped)
                kinds.add(mapped)
    except Exception as e:
        _stats_err(e)
        pass
    if new:
        _db.kv_set("memory", key, {"kinds": sorted(kinds)})
    return new


def on_conversation(an, text="", scope=""):
    """对话事件（agent 每轮调用）：被夸 / 用户低落 / 玩梗 → 微增量；被夸可提前解惩罚。"""
    if not _cfg("enabled", True):
        return
    an = an or {}
    t = str(text or "")
    if any(w in t for w in ("谢谢", "厉害", "好棒", "靠谱", "爱你", "喜欢", "辛苦了", "真棒", "夸")):
        add_delta(EVENT_DELTAS["praised"], "被用户夸了")
        _reduce_penalty(scope)  # 原谅路径：正向反馈降一级惩罚
    elif str(an.get("emotion") or "") in ("低落", "焦虑", "恐惧"):
        add_delta(EVENT_DELTAS["user_down"], "用户心情不好，想陪陪TA")
    elif bool(an.get("playful")) or float(an.get("joke_probability", 0.0)) >= 0.5:
        add_delta(EVENT_DELTAS["playful"], "和用户玩了梗")


def _target_of(scope):
    if scope.startswith("c2c:"):
        return "c2c", scope.split(":", 1)[1]
    if scope.startswith("group:") or scope.startswith("group_all:"):
        return "group", scope.split(":", 1)[1]
    return None, None


def _eligible(scope, now) -> tuple:
    """返回 (是否可发, 原因)。"""
    if not _cfg("enabled", True):
        return False, "disabled"
    d = desire(scope)
    try:
        from memory import interaction as interaction_mod
        mod = interaction_mod.modulate(
            scope, "share", 1.0, now,
            scene="group" if str(scope).startswith("group") else "c2c", axis="disturb",
        )
        th = float(_cfg("threshold", 0.6)) / max(0.3, mod)
    except Exception as e:
        _stats_err(e)
        th = float(_cfg("threshold", 0.6))
    if d["effective"] < th:
        return False, f"分享欲不足 {d['effective']}<{round(th, 2)}"
    st = _state()
    cd = float(_cfg("cooldown_hours", 3))
    if st.get("last_trigger_ts"):
        try:
            if (now - datetime.fromisoformat(st["last_trigger_ts"])).total_seconds() < cd * 3600:
                return False, "冷却中"
        except Exception as e:
            _stats_err(e)
            pass
    try:
        from memory import interaction as interaction_mod
        cap = max(1, round(int(_cfg("max_per_day", 2)) * min(1.5, interaction_mod.relation_mult(scope))))
    except Exception as e:
        _stats_err(e)
        cap = int(_cfg("max_per_day", 2))
    if st.get("day") == now.date().isoformat() and int(st.get("daily", 0)) >= cap:
        return False, "日上限"
    if st.get("week") == _week_key(now.date()) and int(st.get("weekly", 0)) >= int(_cfg("max_per_week", 8)):
        return False, "周上限"
    try:
        from memory import schedule as schedule_mod
        cur = schedule_mod.current_activity(now)
        if cur.get("activity") in ("sleep", "performance", "rehearsal"):
            return False, f"安静时段（{cur.get('label')}）"
    except Exception as e:
        _stats_err(e)
        pass
    return True, ""


_REASON_TEXT = {
    "performance": "刚演出完，有点兴奋也有点累",
    "compose": "刚写完或改完一段曲子，想跟人说一声",
    "rehearsal": "刚排练完",
    "stood_up": "被放鸽子了，有点无语",
    "dream": "昨晚做了个梦，有点印象又记不清",
    "friend": "和朋友刚聚完",
    "shopping": "刚才出去买了点东西",
    "exercise": "刚运动完",
    "dj_practice": "刚练了会打碟",
    "praised": "被用户夸了，心里记着",
    "user_down": "感觉用户心情不好，想陪陪TA",
    "playful": "和用户玩了梗，心情不错",
    "generic": "突然想找用户说句话",
}


def _reason_text(reason):
    return _REASON_TEXT.get(reason, reason)


def _compose(scope, ctx: str, reason: str) -> str:
    """人设口吻生成消息；失败回退模板。"""
    try:
        from agent import persona
        system = persona.compose(include_ai=False)
    except Exception as e:
        _stats_err(e)
        system = _shared.BASE_SYSTEM_PROMPT
    try:
        from memory import tz as tz_mod
        now_txt = tz_mod.now_text(scope)
    except Exception as e:
        _stats_err(e)
        now_txt = datetime.now().strftime("%m月%d日 %H:%M")
    prompt = (
        "你是千石由乃。你在主动找用户说话（用户此刻没发消息），下面是你此刻的状态素材（内部参考，不要全部复述）：\n"
        f"{ctx}\n"
        f"想发这条消息的缘由：{_reason_text(reason)}\n"
        f"现在是：{now_txt}。\n"
        "要求：\n"
        "1. 消息必须自洽——用户看不到你的生活，所以消息里要带上'前因'：要么提你此刻正在做的事"
        "（写歌/排练/天气/家里），要么提你们共同知道的事；不能让用户觉得莫名其妙。\n"
        "2. 素材里没有的事不要编——没有用户夸你、没有人听过你的歌，就不要写'有人……''谢了''谢谢'"
        "这类回应或外部评价，也不要写成对用户上一句话的回应。\n"
        "3. 短（40字以内）、口语化、像随手发的一条；不要报日程、不要括号舞台提示、"
        "不要'作为AI''希望对你有帮助'这类 AI 味表达、不要总结升华。\n"
        "4. 时间一致：只有素材显示你'此刻正在做 / 1 小时内刚做完'的事才能用'刚…'；"
        "白天发生的事就说'今天/下午/傍晚排练过'；现在是深夜（22 点后）就绝不要说'刚回来''刚排练完'这类话。\n"
        "5. 天气：素材里有【天气/环境】内容才可提天气；素材里没有天气，就完全不要提天气，"
        "也不要用'闷热/凉快/热'这类词。\n"
        "直接输出消息内容。"
    )
    try:
        msg = _shared.ask_deepseek(prompt, system=system, max_tokens=120)
        from memory import sleep as sleep_mod
        msg = sleep_mod._sanitize_dream(msg)
        if msg:
            return msg[:80]
    except Exception as e:
        _stats_err(e)
        pass
    return FALLBACK_MESSAGES.get(reason, FALLBACK_MESSAGES["generic"])


def drive(scope="", now=None) -> dict:
    """自动发消息检查：日程事件 → 算欲 → 可发则生成并入通知队列。"""
    now = now or datetime.now()
    for ev in _schedule_events(now):
        add_delta(EVENT_DELTAS.get(ev, 0.1), ev)
    ok, why = _eligible(scope, now)
    d = desire(scope)
    if not ok:
        return {"sent": False, "reason": why, "desire": d}
    reason = (d["reasons"] or ["generic"])[-1]
    # 用户情绪不佳时克制正向炫耀类分享
    try:
        from memory import emotion as emotion_mod
        est = emotion_mod.user_estimate(scope)
        if est and est.get("label") in _NEG_USER_LABELS and reason in _POSITIVE_REASONS:
            return {"sent": False, "reason": "用户情绪不佳，克制正向分享", "desire": d}
    except Exception as e:
        _stats_err(e)
        pass
    # 同一天同一类事件不重复发（内容去重）
    if reason in _sent_reasons(now):
        return {"sent": False, "reason": f"今天发过同类分享（{reason}）", "desire": d}
    ctx_parts = []
    try:
        from memory import schedule as schedule_mod
        if s := schedule_mod.block(scope, now=now):
            ctx_parts.append(s)
    except Exception as e:
        _stats_err(e)
        pass
    try:
        from memory import environment as env_mod
        if s := env_mod.block(scope, now=now, force=True):  # 发消息前强制刷新环境，防旧快照穿帮
            ctx_parts.append(s)
    except Exception as e:
        _stats_err(e)
        pass
    try:
        from memory import emotion as emotion_mod
        ctx_parts.append(emotion_mod.ai_block())
    except Exception as e:
        _stats_err(e)
        pass
    msg = _compose(scope, "\n".join(ctx_parts), reason)
    target_type, target = _target_of(scope)
    if not target_type or not target or not msg:
        return {"sent": False, "reason": "no target/msg", "desire": d}
    _db.notif_add(target_type, target, msg)
    _mark_sent_reason(now, reason)
    try:
        from memory import interaction as interaction_mod
        interaction_mod.mark_event("share", now)  # 分享频率计数（刺激适应）
    except Exception as e:
        _stats_err(e)
        pass
    st = _state()
    st["S"] = float(_cfg("residual", 0.2))
    st["ts"] = now.isoformat(timespec="seconds")
    st["last_trigger_ts"] = now.isoformat(timespec="seconds")
    st["day"] = now.date().isoformat()
    st["daily"] = int(st.get("daily", 0)) + 1
    st["week"] = _week_key(now.date())
    st["weekly"] = int(st.get("weekly", 0)) + 1
    st["reasons"] = []
    _save(st)
    try:
        _db.memory_add(
            "ai", "experience",
            f"给用户发了条消息：「{msg}」", now.isoformat(timespec="seconds"), None,
            confidence=0.6, source="sharing", mclass="short", audience="public", speaker="ai",
        )
        from memory import policy as policy_mod
        policy_mod.touch("ai", "experience", f"给用户发了条消息：「{msg}」", importance=0.5)
        from memory import relationship as rel_mod
        rel_mod.update(scope, event="share", detail=msg[:60])
    except Exception as e:
        _stats_err(e)
        pass
    return {"sent": True, "msg": msg, "reason": reason, "desire": d}


def drive_all(now=None) -> list:
    """后台循环入口：遍历有关系的用户场景，各自检查是否触发。"""
    try:
        import memory.stats as _st
        _st.bump("tick:sharing")
    except Exception as e:
        _stats_err(e)
    if not _cfg("enabled", True):
        return []
    now = now or datetime.now()
    sent = []
    try:
        rows = _db.relationship_rows()
    except Exception as e:
        _stats_err(e)
        return []
    scopes = [r.get("scope") for r in rows if str(r.get("scope") or "").startswith("c2c:")]
    for scope in scopes:
        try:
            r = drive(scope, now)
            if r.get("sent"):
                sent.append({"scope": scope, "msg": r["msg"], "reason": r["reason"]})
        except Exception as e:
            print(f"分享驱动失败 {scope}: {e}")
    return sent



def _stats_err(e):
    """裸 except 审计（v2.2）：错误计数 + 日志，供消融/排查。"""
    try:
        import memory.stats as _st
        _st.bump_err("sharing", e)
    except Exception:
        pass
