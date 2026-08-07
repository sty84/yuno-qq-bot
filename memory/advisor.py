"""决策顾问 / 目标规划 / 自我反思（v6）。

- 决策辅助：像真人顾问一样，结合对用户的了解（偏好/目标/约束/经历）**一次只问一个问题**，
  问满几轮后给出结构化建议；每次咨询沉淀为推理记忆。
- 目标规划：目标的增删改查；活跃目标参与注意力加权（检索提升）。
- 自我反思：定期把近期事件/关系/目标整理成洞察，写入 AI 记忆。
"""

import json
import re
from datetime import datetime

from plugins import _db, _shared
from memory import extract

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
    "8. 禁止用括号标注动作或情绪（如'（点头）（叹口气）'），动作情绪直接用文字表达。"
)

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
    except Exception:
        pass
    return "\n".join(parts) or "（暂无背景）"


def _consult_llm(system, prompt) -> str:
    try:
        resp = _shared.deepseek.chat.completions.create(
            model=_shared.DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            max_tokens=600,
            temperature=0.6,
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

    if stage >= MAX_CONSULT_ROUNDS:
        prompt = (
            f"你正在帮用户做决策：{topic}\n{mem}\n"
            f"用户已回答：\n{history}\n"
            f"现在给出最终建议：用自然连贯的话（像朋友聊天），直接给推荐和理由，"
            f"结尾给一个具体的第一步行动；不要用方案编号列表、不要标题模板、不要分点堆砌。"
        )
        reply = _consult_llm(CONSULT_SYSTEM, prompt)
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
    reply = _consult_llm(CONSULT_SYSTEM, prompt)
    _db.consult_save(scope, topic, "active", stage + 1, answers)
    return reply


# ===== 自我反思 =====
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
        resp = _shared.deepseek.chat.completions.create(
            model=_shared.DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": "你是记忆反思器，输出简洁中文洞察。"},
                {"role": "user", "content": prompt},
            ],
            max_tokens=240,
            temperature=0.4,
        )
        lines = [
            l.strip().lstrip("-• ")
            for l in (resp.choices[0].message.content or "").splitlines()
            if l.strip()
        ]
    except Exception:
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
