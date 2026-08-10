"""心智状态中枢（mind state）：情境解读 / 情绪 / 目标与意图 / 检索命中 / 候选动作。

- 统一快照：把散在各模块的状态收敛成一个结构化 dict，供注入/诊断/评测；
- 目标强度：强度 = 人设价值权重 × 优先级 × 记忆激活（人设从"描述"变"决策参数"）；
- 意图（BDI 式承诺）：最高强度激活目标成为当前 intention，持续到完成或放弃；
- 候选动作：按规则产出 (动作, 效用分)，供决策参考（不含 LLM 额外调用）。
"""

from datetime import datetime, timedelta

from plugins import _db, _shared

PERSONA_WEIGHTS_DEFAULT = {
    "节能": 0.8, "省电": 0.8, "懒": 0.7, "躺": 0.6, "休息": 0.6,
    "音乐": 0.9, "演出": 0.9, "排练": 0.85, "作曲": 0.85, "打碟": 0.85,
    "游戏": 0.7, "漫画": 0.6, "动画": 0.6,
    "朋友": 0.6, "队友": 0.6, "团": 0.6,
    "毒舌": 0.6, "吐槽": 0.6, "省事": 0.7,
    "白巧克力": 0.5, "能量饮料": 0.5, "TCG": 0.6,
}


def _cfg(key, default):
    m = (_shared.CONFIG.get("memory", {}).get("core", {}) or {}).get("mind", {}) or {}
    return m.get(key, default)


def persona_weights() -> dict:
    w = _cfg("persona_weights", None)
    if isinstance(w, dict) and w:
        return w
    return PERSONA_WEIGHTS_DEFAULT


def persona_weight(text) -> float:
    """人设价值权重：文本命中关键词的权重归一化（0~1）。"""
    t = str(text or "")
    if not t:
        return 0.0
    total = 0.0
    for kw, w in persona_weights().items():
        if kw in t:
            total += float(w)
    return round(min(1.0, total / 2.0), 3)


# ===== 目标强度与意图 =====
def goal_strength(goal) -> float:
    """强度 = 人设权重 × 优先级归一化 × 记忆激活（无记忆时 0.5 基线）。"""
    title = str(goal.get("title") or "")
    motivation = str(goal.get("motivation") or "")
    try:
        priority = int(goal.get("priority", 3))
    except (TypeError, ValueError):
        priority = 3
    p_norm = max(0.3, 1.0 - (priority - 1) * 0.2)
    w = persona_weight(title + motivation)
    base = 0.5 if w <= 0 else max(0.5, w)
    return round(min(1.0, base * p_norm), 3)


def _intention_data() -> dict:
    return _db.mind_intention_rows()


def _save_intention(data):
    existing = _db.mind_intention_rows()
    for scope in existing:
        if scope not in data:
            _db.mind_intention_delete(scope)
    for scope, d in data.items():
        _db.mind_intention_set(scope, d)


def recompute_intention(scope):
    """从激活目标里选最高强度作为当前意图（BDI 式承诺）。"""
    scope = str(scope or "")
    if not scope:
        return None
    try:
        from memory import advisor
        goals = advisor.goal_active(scope)
    except Exception as e:
        _stats_err(e)
        goals = []
    if not goals:
        return None
    ranked = sorted(
        ((goal_strength(g), g) for g in goals), key=lambda x: -x[0]
    )
    strength, goal = ranked[0]
    data = _intention_data()
    cur = data.get(scope) or {}
    now = datetime.now().isoformat(timespec="seconds")
    # 已有更强意图时不动；否则（重新）承诺
    if cur.get("state") == "committed" and float(cur.get("strength", 0)) >= strength:
        return cur
    intention = {
        "scope": scope,
        "title": goal.get("title", ""),
        "source": goal.get("source", "goal"),
        "strength": strength,
        "state": "committed",
        "due": goal.get("deadline", ""),
        "condition": str(goal.get("note", ""))[:120],
        "started_at": cur.get("started_at", now),
        "updated_at": now,
    }
    data[scope] = intention
    _save_intention(data)
    return intention


def intention_set(scope, title, source="manual", strength=0.6, due="", condition=""):
    scope = str(scope or "")
    if not scope or not title:
        return None
    data = _intention_data()
    now = datetime.now().isoformat(timespec="seconds")
    data[scope] = {
        "scope": scope, "title": str(title)[:80], "source": str(source)[:20],
        "strength": round(float(strength), 3), "state": "committed",
        "due": str(due)[:40], "condition": str(condition)[:120],
        "started_at": now, "updated_at": now,
    }
    _save_intention(data)
    return data[scope]


def intention_current(scope):
    data = _intention_data()
    it = data.get(str(scope or ""))
    if not it:
        return None
    # 过期放弃（超过 TTL 且不是目标驱动的长期意图）
    ttl = float(_cfg("intention_ttl_hours", 72))
    try:
        updated = datetime.fromisoformat(str(it.get("updated_at", "")))
        if datetime.now() - updated > timedelta(hours=ttl) and it.get("source") != "goal":
            intention_abandon(scope)
            return None
    except Exception as e:
        _stats_err(e)
        pass
    return it


def intention_complete(scope):
    data = _intention_data()
    it = data.pop(str(scope or ""), None)
    _save_intention(data)
    if it:
        try:
            import memory.stats as stats_mod
            stats_mod.bump("intention_complete")
        except Exception as e:
            _stats_err(e)
            pass
    return it


def intention_abandon(scope):
    data = _intention_data()
    it = data.pop(str(scope or ""), None)
    _save_intention(data)
    if it:
        try:
            import memory.stats as stats_mod
            stats_mod.bump("intention_abandon")
        except Exception as e:
            _stats_err(e)
            pass
    return it


def prune_expired(now=None):
    """清理过期意图（每日 grow 调用）：非目标驱动的意图超过 TTL 即放弃。"""
    now = now or datetime.now()
    ttl = float(_cfg("intention_ttl_hours", 72))
    data = _intention_data()
    dropped = []
    for scope, it in data.items():
        if it.get("source") == "goal" or it.get("state") != "committed":
            continue
        try:
            updated = datetime.fromisoformat(str(it.get("updated_at", "")))
            if now - updated > timedelta(hours=ttl):
                data.pop(scope, None)
                dropped.append(scope)
        except Exception as e:
            _stats_err(e)
            continue
    if dropped:
        _save_intention(data)
    return dropped


# ===== 情境解读（appraisal，规则版；LLM 版由 agent 单次结构化输出提供）=====
def appraise(text, scope="") -> dict:
    """AI 侧情境解读：威胁/机会/无关 + 触发信念 + 想做的事（规则快路径）。"""
    t = str(text or "")
    out = {"stance": "无关", "beliefs": [], "wants": []}
    if not t:
        return out
    threat = ("烦", "讨厌", "滚", "闭嘴", "别吵", "浪费", "无聊", "嫌弃", "又", "错", "骗", "撒谎")
    oppo = ("喜欢", "棒", "好耶", "太", "谢谢", "厉害", "夸", "记住", "礼物", "送", "一起", "去", "帮我", "约")
    if any(w in t for w in threat):
        out["stance"] = "威胁"
        out["beliefs"].append("用户对我有负面情绪，先接住情绪再讲事")
        out["wants"].append("保持边界但不激化")
    elif any(w in t for w in oppo):
        out["stance"] = "机会"
        out["beliefs"].append("用户带着好感或需求来，是拉近关系/推进目标的窗口")
        out["wants"].append("回应需求并顺势推进当前意图")
    else:
        out["stance"] = "无关"
        out["beliefs"].append("日常对话，按人设自然回应即可")
        out["wants"].append("不硬找意义，别生硬推销")
    return out


def options_for(text, scope="", now=None) -> list:
    """候选动作 + 效用分（规则；不额外调 LLM）。"""
    opts = []
    t = str(text or "")
    try:
        from memory import appointment
        if appointment.context_block(scope):
            opts.append({"action": "履约提醒/确认约定", "utility": 0.85})
    except Exception as e:
        _stats_err(e)
        pass
    try:
        from memory import advisor
        g = advisor.goal_active(scope)
        if g:
            top = max(g, key=goal_strength)
            opts.append({"action": f"推进目标：{top['title']}", "utility": round(0.7 * goal_strength(top), 2)})
    except Exception as e:
        _stats_err(e)
        pass
    try:
        from memory import emotion
        st = emotion.user_estimate(scope) if hasattr(emotion, "user_estimate") else {}
        if float(st.get("v", 0.0)) < -0.2:
            opts.append({"action": "先安抚情绪再解决问题", "utility": 0.75})
    except Exception as e:
        _stats_err(e)
        pass
    try:
        from memory import living
        if living.where_is_block(scope, t):
            opts.append({"action": "回答物品位置（分级）", "utility": 0.7})
    except Exception as e:
        _stats_err(e)
        pass
    opts.append({"action": "自然闲聊（人设回应）", "utility": 0.4})
    return opts


def snapshot(scope, text, top_k=3) -> dict:
    """统一心智状态快照：situation / emotion / goals / intention / activated_memories / options。"""
    try:
        import memory.stats as _st
        _st.bump("tick:mind")
    except Exception as e:
        _stats_err(e)
    t = str(text or "")
    app = appraise(t, scope)
    emotion = {}
    try:
        from memory import emotion as emotion_mod
        emotion = {
            "ai": emotion_mod.ai_state(),
            "user_estimate": emotion_mod.user_estimate(scope) if hasattr(emotion_mod, "user_estimate") else {},
        }
    except Exception as e:
        _stats_err(e)
        pass
    goals = []
    try:
        from memory import advisor
        goals = [
            {"title": g.get("title", ""), "strength": goal_strength(g), "status": g.get("status", "active")}
            for g in advisor.goal_active(scope)
        ]
        goals.sort(key=lambda x: -x["strength"])
    except Exception as e:
        _stats_err(e)
        pass
    intention = intention_current(scope)
    memories = []
    try:
        from memory import reasoning
        if scope:
            memories = [f for f, _s, _sc in reasoning.retrieve(t, [scope], top_k=top_k, min_score=0.1)]
    except Exception as e:
        _stats_err(e)
        pass
    return {
        "situation": app,
        "emotion": emotion,
        "goals": goals[:5],
        "intention": intention,
        "activated_memories": memories,
        "options": options_for(t, scope),
        "ts": datetime.now().isoformat(timespec="seconds"),
    }


def block(scope, text) -> str:
    """心智状态注入块（内部参考，不主动播报）。"""
    if not _cfg("enabled", True):
        return ""
    s = snapshot(scope, text)
    parts = [f"【心智状态·内部参考】情境解读：{s['situation'].get('stance', '无关')}"]
    it = s.get("intention")
    if it:
        parts.append(f"当前意图：{it.get('title', '')}（强度{it.get('strength', 0)}，{it.get('state', '')}）")
    if s.get("goals"):
        parts.append("激活目标：" + "；".join(f"{g['title']}（{g['strength']}）" for g in s["goals"][:3]))
    if s.get("activated_memories"):
        parts.append("命中的记忆：" + "；".join(s["activated_memories"][:2]))
    if s.get("options"):
        top = max(s["options"], key=lambda o: o.get("utility", 0))
        parts.append(f"倾向动作：{top.get('action', '')}（效用{top.get('utility', 0)}）")
    parts.append("别主动提'心智状态'这个词，按内部参考自然行事")
    return "；".join(parts)


def apply_cognitive(scope, text, parsed):
    """消费单次结构化输出：意图落地 + 目标强度记录（P0 认知循环）。"""
    scope = str(scope or "")
    try:
        intention = parsed.get("intention") if isinstance(parsed, dict) else None
        if intention and scope:
            intention_set(
                scope,
                str(intention)[:80],
                source="cognitive",
                strength=0.7,
            )
    except Exception as e:
        _stats_err(e)
        pass
    return True


def stats() -> dict:
    data = _intention_data()
    committed = sum(1 for v in data.values() if v.get("state") == "committed")
    return {"intentions": len(data), "committed": committed}



def _stats_err(e):
    """裸 except 审计（v2.2）：错误计数 + 日志，供消融/排查。"""
    try:
        import memory.stats as _st
        _st.bump_err("mind", e)
    except Exception:
        pass
