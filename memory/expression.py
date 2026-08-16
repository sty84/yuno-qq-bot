"""语言语义解释层（v7）：网络词/梗/反讽/夸张/谐音/圈层语言 → 真实意图。

- Expression Analyzer：命中网络用语词典 → possible_intents + confidence + need_context。
- Humor Detector：joke_probability + 情绪（区分"玩笑表达"与"事实陈述"）。
- Expression Profile：用户表达画像（slang/irony/emoji/formal），供关系与人格适配。

作用：避免网络用语被错误结构化、玩笑污染人格；为 Memory Controller 提供"用户真正表达了什么"。
"""

import re

from plugins import _db

# 网络用语词典：词 → 可能意图 + 是否需要上下文
SLANG: dict[str, dict] = {
    "笑死": {"meanings": [("觉得有趣", 0.8), ("嘲讽", 0.15)], "need_context": True},
    "绷不住了": {"meanings": [("搞笑到忍不住笑", 0.8), ("压力大到撑不住", 0.2)], "need_context": True},
    "破防": {"meanings": [("情绪被戳中/崩溃", 0.85)], "need_context": True},
    "破大防": {"meanings": [("情绪崩溃", 0.85)], "need_context": True},
    "emo": {"meanings": [("情绪低落", 0.9)]},
    "yyds": {"meanings": [("非常赞赏/封神", 0.9)]},
    "绝绝子": {"meanings": [("非常棒", 0.75), ("无语", 0.2)], "need_context": True},
    "离谱": {"meanings": [("难以置信/夸张", 0.7), ("吐槽", 0.3)], "need_context": True},
    "麻了": {"meanings": [("无奈麻木", 0.9)]},
    "我裂开": {"meanings": [("崩溃/震惊", 0.85)]},
    "裂开": {"meanings": [("崩溃/震惊", 0.8)], "need_context": True},
    "无语": {"meanings": [("无奈", 0.9)]},
    "无语子": {"meanings": [("无奈", 0.9)]},
    "就这": {"meanings": [("失望/嘲讽", 0.75)], "need_context": True},
    "我不理解": {"meanings": [("困惑/无语", 0.75)], "need_context": True},
    "逆天": {"meanings": [("震惊", 0.7), ("无语", 0.25)], "need_context": True},
    "抽象": {"meanings": [("难以理解/圈层梗", 0.7)], "need_context": True},
    "格局打开": {"meanings": [("视野开阔", 0.75)], "need_context": True},
    "格局小了": {"meanings": [("视野狭窄", 0.8)]},
    "躺平": {"meanings": [("放弃努力", 0.8), ("无奈自嘲", 0.2)], "need_context": True},
    "摆烂": {"meanings": [("放弃努力随缘", 0.85)]},
    "内卷": {"meanings": [("竞争激烈", 0.85)]},
    "摸鱼": {"meanings": [("偷懒", 0.9)]},
    "划水": {"meanings": [("偷懒不干活", 0.9)]},
    "社死": {"meanings": [("极度尴尬", 0.9)]},
    "上头": {"meanings": [("沉迷/上头", 0.7)], "need_context": True},
    "下头": {"meanings": [("扫兴", 0.85)]},
    "拿捏": {"meanings": [("完全掌握", 0.75)]},
    "栓Q": {"meanings": [("感谢/无语", 0.75)], "need_context": True},
    "芭比Q了": {"meanings": [("完蛋了", 0.85)]},
    "寄了": {"meanings": [("完蛋了/没救了", 0.85)]},
    "贴贴": {"meanings": [("想要亲近", 0.9)]},
    "退退退": {"meanings": [("反感驱赶", 0.85)], "need_context": True},
    "我劝你善良": {"meanings": [("开玩笑式威胁", 0.7)], "need_context": True},
    "偷家": {"meanings": [("趁虚而入", 0.7)]},
    "芜湖": {"meanings": [("兴奋", 0.85)]},
    "冲了": {"meanings": [("决定行动", 0.65)], "need_context": True},
    "白嫖": {"meanings": [("免费获取", 0.85)]},
    "氪金": {"meanings": [("付费充值", 0.85)]},
    "欧皇": {"meanings": [("运气极好", 0.9)]},
    "非酋": {"meanings": [("运气很差", 0.9)]},
    "老六": {"meanings": [("狡猾/阴险的人", 0.75)], "need_context": True},
    "玩明白了": {"meanings": [("很熟练", 0.8)]},
    "哈基米": {"meanings": [("猫咪拟声梗/可爱", 0.8)]},
    "真的会谢": {"meanings": [("无奈", 0.7), ("感谢", 0.3)], "need_context": True},
    "狠狠共鸣": {"meanings": [("强烈认同", 0.9)]},
    "属于是": {"meanings": [("确实是（调侃）", 0.7)], "need_context": True},
    "难绷": {"meanings": [("尴尬/忍不住", 0.8)], "need_context": True},
    "典": {"meanings": [("典型/讽刺", 0.75)], "need_context": True},
    "炸了": {"meanings": [("情绪爆发（夸张表达）", 0.8)], "need_context": True},
    "6": {"meanings": [("厉害/赞赏", 0.75)], "need_context": True},
}

IRONY_MARKERS = {"离谱", "就这", "难绷", "典", "笑死", "我不理解", "属于是", "格局小了"}
JOKE_MARKERS = {"哈哈", "笑死", "玩笑", "狗头", "玩梗", "整活", "绷不住了", "我裂开", "裂开", "难绷", "炸了", "😂", "🤣", "😝"}
PRESSURE_MARKERS = {"麻了", "无语", "躺平", "摆烂", "emo", "破防", "破大防", "我裂开", "裂开", "炸了", "寄了"}
EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]")
SERIOUS_WORDS = ("工作", "合同", "面试", "重要", "正式", "麻烦", "请问", "谢谢", "开会", "项目")


def detect_expressions(text) -> list:
    """命中网络用语词典 → [{expression, possible_intents, confidence, need_context}]。"""
    t = str(text or "").lower()
    out = []
    for word, spec in SLANG.items():
        if word.lower() in t:
            best = max(m[1] for m in spec["meanings"])  # type: ignore[index]
            out.append(
                {
                    "expression": word,
                    "possible_intents": [
                        {"meaning": m[0], "confidence": m[1]} for m in spec["meanings"]  # type: ignore[index]
                    ],
                    "confidence": best,
                    "need_context": spec.get("need_context", False),
                }
            )
    return out


def analyze(text) -> dict:
    """表达分析：{joke_probability, emotion, expressions, need_context, irony}。"""
    t = str(text or "")
    exprs = detect_expressions(t)
    joke = 0.0
    if any(w in t for w in JOKE_MARKERS):
        joke = max(joke, 0.75)
    if any(e["expression"] in IRONY_MARKERS for e in exprs):
        joke = max(joke, 0.6)
    if EMOJI_RE.search(t):
        joke = max(joke, 0.35)
    if any(w in t for w in ("救命", "我不理解", "麻了", "无语")):
        joke = max(joke, 0.3)

    if any(w in t for w in ("笑死", "绷不住了", "哈哈", "芜湖", "yyds", "绝绝子")):
        emotion = "有趣"
    elif any(w in t for w in PRESSURE_MARKERS):
        emotion = "轻度压力表达"
    elif any(w in t for w in IRONY_MARKERS):
        emotion = "吐槽"
    else:
        emotion = "平静"

    return {
        "joke_probability": round(min(1.0, joke), 2),
        "emotion": emotion,
        "expressions": exprs,
        "need_context": bool(exprs and any(e["need_context"] for e in exprs)),
        "irony": bool(exprs and any(e["expression"] in IRONY_MARKERS for e in exprs)),
    }


# ===== 用户表达画像 =====
def profile_get(scope):
    row = _db.expr_profile_get(scope)
    if row:
        return row
    return {
        "scope": scope,
        "slang_frequency": 0.0,
        "irony_usage": 0.0,
        "emoji_usage": 0.0,
        "serious_mode_switch": 0,
        "humor_style": "unknown",
        "communication_style": "unknown",
        "formality_level": 0.5,
    }


def profile_update(scope, text) -> dict:
    """按消息更新表达画像（滑动平均，学习率 0.1）。"""
    an = analyze(text)
    # 网络语候选池（v6 建议 §3）：高玩笑概率但未收录 → 记 language_context 待人工收录
    if (
        an["joke_probability"] >= 0.4
        and not an["expressions"]
        and 2 <= len(str(text or "").strip()) <= 8
    ):
        raw = str(text).strip()
        recent = {r["raw_expression"] for r in _db.expr_log_rows(limit=50)}
        if raw not in recent:
            _db.expr_log_add(raw, "", [], 0.3, "候选：疑似网络词待收录")
    cur = profile_get(scope)
    n = 0.1
    slang = float(cur["slang_frequency"]) * (1 - n) + (1.0 if an["expressions"] else 0.0) * n
    irony = float(cur["irony_usage"]) * (1 - n) + (1.0 if an["irony"] else 0.0) * n
    emoji = float(cur["emoji_usage"]) * (1 - n) + (1.0 if EMOJI_RE.search(text or "") else 0.0) * n
    formal = float(cur["formality_level"]) * (1 - n) + (
        0.7 if any(w in (text or "") for w in SERIOUS_WORDS) else 0.3
    ) * n
    serious = 1 if int(cur.get("serious_mode_switch", 0)) or any(
        w in (text or "") for w in ("工作", "合同", "面试", "正式", "重要")
    ) else 0
    if slang >= 0.4:
        humor = "幽默爱玩梗"
    elif formal >= 0.6:
        humor = "正经"
    else:
        humor = "mixed"
    comm = "轻松" if (emoji >= 0.3 or slang >= 0.4) else ("正式" if formal >= 0.6 else "日常")
    row = {
        "slang_frequency": round(slang, 3),
        "irony_usage": round(irony, 3),
        "emoji_usage": round(emoji, 3),
        "serious_mode_switch": serious,
        "humor_style": humor,
        "communication_style": comm,
        "formality_level": round(formal, 3),
    }
    _db.expr_profile_upsert(scope, **row)
    return row


def describe(scope) -> str:
    """表达适配注入块（Persona）：让 AI 按用户风格调整正式程度与语气。"""
    p = profile_get(scope)
    if not p or (float(p.get("slang_frequency", 0)) < 0.1 and float(p.get("formality_level", 0.5)) >= 0.5):
        return ""
    parts = []
    if float(p.get("slang_frequency", 0)) >= 0.4:
        parts.append("用户爱用网络用语/玩梗")
    if float(p.get("irony_usage", 0)) >= 0.4:
        parts.append("用户常反讽/吐槽，多为朋友式表达，不必当真")
    if float(p.get("emoji_usage", 0)) >= 0.4:
        parts.append("用户爱用表情，回复可以轻松些")
    if float(p.get("formality_level", 0.5)) >= 0.6:
        parts.append("用户偏正式交流")
    if parts:
        return "【用户表达风格】" + "；".join(parts) + "——回复时相应调整正式程度与语气。"
    return ""
