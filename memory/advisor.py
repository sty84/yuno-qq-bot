"""决策顾问 / 目标规划 / 自我反思（v6）。

- 决策辅助：像真人顾问一样，结合对用户的了解（偏好/目标/约束/经历）**一次只问一个问题**，
  问满几轮后给出结构化建议；每次咨询沉淀为推理记忆。
- 目标规划：目标的增删改查；活跃目标参与注意力加权（检索提升）。
- 自我反思：定期把近期事件/关系/目标整理成洞察，写入 AI 记忆。
"""

from memory._llmutil import parse_json_object
import json
import re
from datetime import datetime

from plugins import _db, _shared
from memory import extract, policy, reasoning

MAX_CONSULT_ROUNDS = 4

CONSULT_SYSTEM = (
    "你是用户的私人决策顾问。目标：帮用户把模糊的决策变成清晰的行动方案。\n"
    "规则：\n"
    "1. 一次只问一个问题，绝不一次性列出一堆问题。\n"
    "2. 每个问题都要结合你对用户的了解（偏好、目标、约束、过去经历）来问，"
    "让用户觉得你真的懂他。\n"
    "3. 必须考虑现实约束：钱、时间、精力、家庭、风险、可行性。\n"
    "4. 问满几轮后给建议：可以直接给推荐和理由，或简短比较两三个选项，"
    "但必须用自然连贯的话表达——禁止输出'方案A/B/C'式编号列表、禁止标题模板、"
    "禁止分点堆砌；像真人顾问聊天一样说人话，别像在填表。\n"
    "5. 语气像真人顾问：先共情，再追问，最后给方案，别端着；保持简短，别长篇大论。\n"
    "6. 表达符合常识，禁止夸张或离谱比喻（比如把三千块说成'一顿饭钱'）；"
    "金额、时间、比例都要贴近现实，不确定就具体问用户。\n"
    "7. 结尾给一个具体可行的第一步行动即可，不用再总结一遍。\n"
    "8. 禁止用括号标注动作或情绪（如'（点头）（叹口气）'），动作情绪直接用文字表达。\n"
    "9. 提问要口语化、先共情：先接住用户当前的话和情绪（简短一句），再自然地问最关键的未知信息；"
    "不要一上来就蹦术语或直接问钱。\n"
    "10. 用户说出与你已知信息明显不符或很意外的话时（比如突然说人在另一个国家/城市），"
    "先按你的性格自然表达惊讶或怀疑（可以调侃、可以确认），不要面无表情地直接继续追问。"
)


def _consult_system() -> str:
    """顾问 system prompt：规则 + 人格语气（保持角色声音，不脱戏）。"""
    s = CONSULT_SYSTEM
    try:
        from agent import persona
        core = persona.compose(include_ai=False)
        s = "保持以下人格，像这个人一样说话（语气、口头禅、性格都要贴合）：\n" + core + "\n\n" + s
    except Exception as e:
        _stats_err(e)
        pass
    return s

DECISION_TRIGGER = re.compile(
    r"要不要|该不该|怎么选|给个建议|帮我决定|纠结|犹豫|选哪个|值不值得|帮我想想|拿不定主意"
)


# ===== 目标规划 =====
def goal_add(scope, title, priority=3, deadline="", note="", motivation="", confidence=0.7, current_state=None):
    title = (title or "").strip()
    if not title:
        return "目标内容不能为空"
    _db.goal_add(
        scope, title, priority=priority, deadline=deadline, note=note,
        motivation=motivation, confidence=confidence, current_state=current_state,
    )
    return f"已记录目标：{title}" + (f"（动机：{motivation}）" if motivation else "")


def goal_list(scope=None, status=None):
    return _db.goal_rows(scope, status)


def goal_update(scope, title, progress=None, status=None, note=None, motivation=None, confidence=None, current_state=None):
    rows = _db.goal_rows(scope)
    match = next((r for r in rows if title in r["title"]), None)
    if not match:
        return f"没有找到目标：{title}"
    _db.goal_update(
        scope, match["title"], progress=progress, status=status, note=note,
        motivation=motivation, confidence=confidence, current_state=current_state,
    )
    return f"已更新目标：{match['title']}"


def goal_active(scope=None):
    return _db.goal_rows(scope, status="active")


# ===== 决策辅助（一次一问，像真人顾问）=====
def consult_active(scope):
    return bool(_db.consult_get(scope))


def consult_related(scope, text) -> bool:
    """新消息是否还在进行中的咨询话题上（防顾问劫持无关话题）。
    极短回复（≤4字）视为回答顾问问题；否则按词元重叠判断。"""
    sess = _db.consult_get(scope)
    if not sess:
        return False
    t = str(text or "").strip()
    if not t:
        return False
    if len(t) <= 4:
        return True
    try:
        answers = json.loads(sess.get("answers") or "[]")
    except Exception as e:
        _stats_err(e)
        answers = []
    base = f"{sess.get('topic', '')} " + " ".join(answers[-2:])
    tk = set(extract.fact_keywords(t))
    bk = set(extract.fact_keywords(base))
    if not tk:
        return True
    return len(tk & bk) / len(tk) >= 0.15


def consult_abort(scope):
    """话题切走时结束咨询会话（交回正常聊天）。"""
    sess = _db.consult_get(scope)
    if not sess:
        return
    try:
        answers = json.loads(sess.get("answers") or "[]")
    except Exception as e:
        _stats_err(e)
        answers = []
    _db.consult_save(scope, sess.get("topic", ""), "done", int(sess.get("stage", 0)), answers)


def consult_status(scope):
    return _db.consult_get(scope)


def _consult_memory_context(scope, topic):
    """把对用户的了解组装给顾问：目标 + 相关记忆。"""
    parts = []
    goals = goal_active(scope)
    if goals:
        parts.append(
            "【用户目标】" + "；".join(
                f"{g['title']}（优先级{g['priority']}）" for g in goals[:5]
            )
        )
    try:
        from memory import reasoning
        hits = reasoning.retrieve(topic, [scope], top_k=6, min_score=0.05)
        if hits:
            parts.append(
                "【我了解的背景】" + "；".join(
                    extract.nice_fact(f) for f, _s, _sc in hits[:6]
                )
            )
    except Exception as e:
        _stats_err(e)
        pass
    return "\n".join(parts) or "（暂无背景）"


def _consult_llm(system, prompt) -> str:
    try:
        resp = _shared.deepseek_chat(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            max_tokens=600,
            temperature=0.6,
            module="advisor",
            detail="advice",
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        return f"（咨询暂时不可用，稍后再试：{e}）"


def consult_turn(scope, text) -> str:
    """决策咨询一轮：开始/追问/收尾。返回顾问回复。"""
    sess = _db.consult_get(scope)
    if not sess:
        sess = {
            "scope": scope,
            "topic": (text or "")[:60],
            "status": "active",
            "stage": 0,
            "answers": [],
        }
    stage = int(sess.get("stage", 0))
    answers = json.loads(sess.get("answers") or "[]")
    if text and stage > 0:
        answers.append((text or "")[:200])
    topic = sess.get("topic", "")
    mem = _consult_memory_context(scope, topic)
    history = "\n".join(f"{i + 1}. {a}" for i, a in enumerate(answers)) if answers else "（还没有）"

    urgent = any(w in (text or "") for w in ("急", "快", "马上", "赶紧", "尽快", "别问了", "直接说", "直接给", "来不及"))
    max_rounds = 2 if urgent else MAX_CONSULT_ROUNDS
    if stage >= max_rounds:
        prompt = (
            f"你正在帮用户做决策：{topic}\n{mem}\n"
            f"用户已回答：\n{history}\n"
            f"现在给出最终建议：用自然连贯的话（像朋友聊天），直接给推荐和理由，"
            f"结尾给一个具体的第一步行动；不要用方案编号列表、不要标题模板、不要分点堆砌。"
        )
        reply = _consult_llm(_consult_system(), prompt)
        _db.consult_save(scope, topic, "done", stage + 1, answers + [reply[:200]])
        _db.memory_add(
            "ai", "reasoning",
            f"决策咨询《{topic}》：{reply[:100]}",
            datetime.now().isoformat(timespec="seconds"),
            None, 0.7, "consult",
        )
        return reply

    prompt = (
        f"你正在帮用户做决策：{topic}\n{mem}\n"
        f"用户已回答：\n{history}\n"
        f"用户最新回答：{text}\n"
        f"请针对最关键的未知信息，只问【一个问题】；如果信息已足够，可以直接给建议。"
    )
    reply = _consult_llm(_consult_system(), prompt)
    _db.consult_save(scope, topic, "active", stage + 1, answers)
    return reply


# ===== 自我反思 =====
_GENERIC_REFLECTION = (
    "继续加油", "继续保持", "关系不错", "聊得很好", "今天不错", "一切顺利",
    "越来越好", "要努力", "保持现状", "没什么特别", "平平淡淡", "心情不错", "挺好的",
)


def _reflection_quality(line, evs, rels) -> bool:
    """反思质量过滤：拒绝空泛套话，保留有具体锚点或明确主体/内容的洞察。"""
    t = str(line or "").strip()
    if len(t) < 6 or len(t) > 80:
        return False
    if any(g in t for g in _GENERIC_REFLECTION):
        return False
    if re.search(r"\d", t):
        return True
    if any(w in t for w in ("用户", "我", "她", "他", "我们", "你")) and len(t) >= 6:
        return True
    anchors = set()
    for ev in evs:
        anchors.update(extract.fact_keywords(ev.get("title") or ""))
    for r in rels:
        anchors.update(extract.fact_keywords(str(r.get("scope") or "")))
    if extract.fact_keywords(t) & anchors:
        return True
    return False


def daily_reflect(limit=20) -> int:
    """把近期事件/关系/目标整理成洞察，沉淀为 AI 记忆（reflection）。"""
    evs = _db.event_rows(limit=limit)
    if not evs:
        return 0
    titles = "\n".join(f"- {e['title']}" for e in evs[:limit])
    rels = _db.relationship_rows()
    rel_text = "；".join(f"{r['scope']}:{r['stage']}" for r in rels[:5])
    prompt = (
        "你是记忆反思器。基于近期事件与关系状态，写出 1-3 条值得记住的自我洞察"
        "（关于用户偏好、我的失误、关系变化或目标进展），每行一条，不要解释，不要编号。\n"
        f"近期事件：\n{titles}\n关系：{rel_text or '无'}"
    )
    try:
        resp = _shared.deepseek_chat(
            messages=[
                {"role": "system", "content": "你是记忆反思器，输出简洁中文洞察。"},
                {"role": "user", "content": prompt},
            ],
            max_tokens=240,
            temperature=0.4,
            module="advisor",
            detail="reflect",
        )
        lines = [
            l.strip().lstrip("-• ")
            for l in (resp.choices[0].message.content or "").splitlines()
            if l.strip()
        ]
        lines = [l for l in lines if _reflection_quality(l, evs, rels)][:3]
    except Exception as e:
        _stats_err(e)
        return 0
    n = 0
    ts = datetime.now().isoformat(timespec="seconds")
    for l in lines[:3]:
        if 6 <= len(l) <= 80:
            _db.memory_add("ai", "reflection", l, ts, None, 0.6, "reflection")
            # 反思闭环（v6 建议 §8）：洞察落策略日志，供复盘与后续 HITL 行为调整
            _db.policy_log_add("reflection", "insight", 0.6, detail=l[:100])
            n += 1
    return n


# ===== 成长反思闭环（v31.3 合并自 memory/reflect.py）=====
def _refl_cfg(key, default):
    core = (_shared.CONFIG.get("memory", {}).get("core", {}) or {}).get("reflection", {}) or {}
    return core.get(key, default)


def _refl_enabled() -> bool:
    return bool(_refl_cfg("enabled", True))


def _refl_llm() -> bool:
    return bool(_refl_cfg("llm", True))


def _evidence_for(belief):
    """找相关证据：用户记忆里与 belief 相关的事实（含可信度/玩笑标记）。"""
    scopes = list(dict.fromkeys(r["scope"] for r in _db.memory_rows() if r["scope"] != "ai"))
    if not scopes:
        return []
    hits = reasoning.retrieve(belief, scopes, top_k=8, min_score=0.0)
    bt = set(extract.tokenize(belief))
    hits = [h for h in hits if len(set(extract.tokenize(h[0])) & bt) >= 2]
    playful_topic_ids = {
        t["id"]
        for t in _db.topic_rows()
        if any(p["param"] == "playful" and p["value"] == "true" for p in _db.topic_params(t["id"]))
    }
    playful_facts = {
        ev["title"]
        for ev in _db.event_rows()
        if ev.get("topic_id") in playful_topic_ids
    }
    conf_map = {}
    for scope in scopes:
        for r in _db.memory_rows(scope):
            conf_map.setdefault(r["fact"], float(r.get("confidence", 0.7)))
    evidence = []
    for f, _s, _sc in hits:
        evidence.append(
            {
                "fact": f,
                "confidence": conf_map.get(f, 0.7),
                "playful": f in playful_facts,
            }
        )
    return evidence


def _rule_review(belief, evidence):
    """规则审查：证据含低可信度/玩笑 → 驳回；完全无证据 → 降权存疑；否则接受。"""
    if any(e["confidence"] < 0.3 or e["playful"] for e in evidence):
        return {"action": "reject", "note": "证据可信度低或来自玩笑语境"}
    if not evidence:
        return {"action": "revise", "note": "无证据支撑，降低可信度"}
    return {"action": "accept", "note": "有证据支撑"}


def _llm_review(belief, evidence):
    prompt = (
        "你是记忆审查器。判断以下 AI 观点是否与证据矛盾。"
        "只输出 JSON：{\"action\": \"accept|revise|reject\", \"revised\": \"改写后的观点（可空）\", \"confidence\": 0.0-1.0}\n"
        f"观点：{belief}\n证据：\n"
        + "\n".join(
            f"- {e['fact']}（可信度{e['confidence']:.2f}{'，玩笑' if e['playful'] else ''}）"
            for e in evidence
        )
        or "（无证据）"
    )
    try:
        resp = _shared.deepseek_chat(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            temperature=0.1,
            module="advisor",
            detail="decision",
        )
        raw = (resp.choices[0].message.content or "").strip()
        data = parse_json_object(raw) or {}
        action = data.get("action", "accept")
        if action not in ("accept", "revise", "reject"):
            action = "accept"
        return {
            "action": action,
            "revised": str(data.get("revised") or "").strip(),
            "confidence": float(data.get("confidence", 0.5)),
            "note": "LLM 审查",
        }
    except Exception as e:
        print(f"LLM 审查失败，回退规则审查：{e}")
        return _rule_review(belief, evidence)


def reflect_beliefs(limit=10) -> dict:
    """对 belief 逐条审查并应用动作。返回统计。"""
    result = {"checked": 0, "accepted": 0, "revised": 0, "rejected": 0}
    if not _refl_enabled():
        return result
    beliefs = _db.memory_rows("ai", "belief")
    for b in beliefs[:limit]:
        fact, conf = b["fact"], float(b.get("confidence", 0.7))
        evidence = _evidence_for(fact)
        verdict = _llm_review(fact, evidence) if _refl_llm() else _rule_review(fact, evidence)
        result["checked"] += 1
        action = verdict["action"]
        note = verdict.get("note", "")
        if action == "accept":
            new_conf = policy.update(conf, "confirm")
            _db.memory_set_confidence("ai", "belief", fact, new_conf)
            _db.belief_log_add("belief", fact, "accept", new_conf, note, old_content=fact)
            result["accepted"] += 1
        elif action == "revise":
            revised = (verdict.get("revised") or "").strip()
            if revised and revised != fact:
                _db.memory_add(
                    "ai", "belief", revised,
                    confidence=float(verdict.get("confidence", conf * 0.7)),
                    source="reflection",
                )
                _db.memory_set_confidence("ai", "belief", fact, max(0.2, conf * 0.5))
                _db.belief_log_add("belief", revised, "revise", verdict.get("confidence"), note, old_content=fact)
            else:
                new_conf = max(0.2, conf * 0.7)
                _db.memory_set_confidence("ai", "belief", fact, new_conf)
                _db.belief_log_add("belief", fact, "revise", new_conf, note, old_content=fact)
            result["revised"] += 1
        else:
            _db.memory_set_confidence("ai", "belief", fact, 0.05)
            _db.belief_log_add("belief", fact, "reject", 0.05, note, old_content=fact)
            result["rejected"] += 1
    return result


def rollback_belief(log_id) -> str:
    """回滚 belief 到日志记录的旧版本（可审计）。"""
    entry = _db.belief_log_get(log_id)
    if not entry or entry["kind"] != "belief":
        return "日志不存在或不是 belief"
    old = entry.get("old_content") or entry["content"]
    conf = float(entry.get("confidence") or 0.5)
    _db.memory_add("ai", "belief", old, confidence=max(conf, 0.5), source="rollback")
    if entry["action"] in ("revise", "reject"):
        _db.memory_set_confidence("ai", "belief", entry["content"], 0.05)
    _db.belief_log_add(
        "belief", old, "rollback", conf,
        note=f"回滚自 #{log_id}", old_content=entry["content"],
    )
    return f"已回滚 belief 至：{old[:40]}"



def _stats_err(e):
    """裸 except 审计（v2.2）：错误计数 + 日志，供消融/排查。"""
    try:
        import memory.stats as _st
        _st.bump_err("advisor", e)
    except Exception:
        pass
