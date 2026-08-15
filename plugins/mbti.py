# -*- coding: utf-8 -*-
"""MBTI 轻量测试插件：几轮问答后给出类型，并写入 AI 记忆。

用法：
  /mbti          开始或继续
  /mbti A        回答当前题为 A
  /mbti B        回答当前题为 B
  /mbti 重置     清除进度重新开始
  /mbti 结果     查看当前结果
"""

import json

from plugins import _db, _shared

NAME = "MBTI"
HELP = "/mbti 开始/继续 MBTI 测试｜/mbti A|B 回答｜/mbti 重置｜/mbti 结果"

QUESTIONS = [
    ("忙碌一天后，你更倾向于？", "A. 约朋友出去玩，回血靠社交", "B. 一个人待着，安静回血", "E"),
    ("你更容易注意到？", "A. 具体事实、细节和实际经验", "B. 可能性、联想和未来图景", "S"),
    ("做决定时，你更看重？", "A. 逻辑、原则和一致性", "B. 人情、感受和关系和谐", "T"),
    ("你的生活节奏更像？", "A. 提前计划，按安排走", "B. 随性灵活，临场发挥", "J"),
    ("新环境里你会？", "A. 主动找人搭话，快速活跃", "B. 先观察，熟了再放开", "E"),
    ("别人描述一件事时，你更在意？", "A. 它实际是什么、怎么发生的", "B. 它可能意味着什么、还能怎样", "S"),
    ("朋友找你吐槽时，你通常？", "A. 先帮 TA 分析问题、给方案", "B. 先接住情绪，陪着再说", "T"),
    ("面对临时变动，你？", "A. 不太舒服，最好按原计划", "B. 无所谓，随机应变更有意思", "J"),
]

DIMENSIONS = [
    ("E", "I"),
    ("S", "N"),
    ("T", "F"),
    ("J", "P"),
]


def _session_key(ctx):
    return f"mbti:{ctx.chat_key}"


def _load(ctx):
    return _db.kv_get("mbti", _session_key(ctx)) or {}


def _save(ctx, data):
    _db.kv_set("mbti", _session_key(ctx), data)


def _compute(session):
    scores = {"E": 0, "I": 0, "S": 0, "N": 0, "T": 0, "F": 0, "J": 0, "P": 0}
    for i, ans in enumerate(session.get("answers", [])):
        q = QUESTIONS[i]
        letter = q[3]
        if ans == "A":
            scores[letter] += 1
        elif ans == "B":
            # B 对应相反维度
            for a, b in DIMENSIONS:
                if letter == a:
                    scores[b] += 1
                elif letter == b:
                    scores[a] += 1
    result = ""
    for a, b in DIMENSIONS:
        result += a if scores[a] >= scores[b] else b
    return result, scores


def _question_text(idx):
    q = QUESTIONS[idx]
    return f"第 {idx + 1}/{len(QUESTIONS)} 题\n{q[0]}\n{q[1]}\n{q[2]}\n\n回复：/mbti A 或 /mbti B"


def handle(text, ctx):
    cmd = text.strip()
    low = cmd.lower()
    if low.startswith("/mbti重置") or low.startswith("/mbti 重置"):
        _save(ctx, {})
        return "已重置 MBTI 测试，随时 /mbti 重新开始。"
    if low.startswith("/mbti结果") or low.startswith("/mbti 结果"):
        session = _load(ctx)
        if not session.get("answers"):
            return "还没有 MBTI 进度，先 /mbti 开始。"
        result, scores = _compute(session)
        return f"当前 MBTI：{result}\n维度倾向：{scores}"
    if low.startswith("/mbti"):
        rest = cmd[len("/mbti"):].strip().upper()
        session = _load(ctx)
        idx = int(session.get("idx", 0))
        answers = session.get("answers", [])
        if rest in ("A", "B"):
            if idx >= len(QUESTIONS):
                return "测试已经完成，/mbti 重置 可重新开始。"
            answers = answers[:idx] + [rest]
            idx += 1
            session["answers"] = answers
            session["idx"] = idx
            _save(ctx, session)
            if idx >= len(QUESTIONS):
                result, scores = _compute(session)
                try:
                    _db.memory_add(
                        "ai", "identity", f"我的 MBTI 是 {result}",
                        source="mbti", mclass="core", confidence=0.8,
                    )
                except Exception:
                    pass
                _save(ctx, {})
                return f"测试完成！你的 MBTI 是：{result}\n维度倾向：{scores}"
            return _question_text(idx)
        # 没有答案参数：开始/继续
        if not session.get("answers"):
            session = {"idx": 0, "answers": []}
            _save(ctx, session)
            return _question_text(0)
        if idx >= len(QUESTIONS):
            result, scores = _compute(session)
            return f"你已经完成了，结果是 {result}\n/save 重置？哦不，/mbti 重置 可重新开始。"
        return _question_text(idx)
    return None


COMMANDS = {
    "/mbti": handle,
}
