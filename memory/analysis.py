"""当前状态分析 + 意图分析（轻量规则版，避免每条消息都调 LLM）。
规则判不出情绪/玩笑时，可节流调用 LLM 补充（config.json → memory.core.analysis）。
返回结构：{intent, emotion, importance, event_type, confidence, correction,
correction_strong, playful, valence, arousal, dominance}。"""

import json
import re
import threading
import time

from memory.extract import classify_event_type

INTENT_RULES = [
    ("求助", ["怎么", "如何", "帮我", "能不能", "可以吗", "求助", "?", "？"]),
    ("偏好", ["喜欢", "讨厌", "最爱", "爱吃", "爱喝"]),
    ("事件", ["项目", "部署", "上线", "完成", "参加", "去过", "开始", "发布", "做了", "学到"]),
    ("回忆", ["记得", "还记得", "想起", "回忆", "记不记得", "想起来"]),
    ("情绪", ["生气", "难过", "开心", "高兴", "伤心", "烦", "累"]),
]

EMOTION_WORDS = {
    "开心": ["开心", "高兴", "太好了", "哈哈", "耶", "爽", "好棒", "满意", "幸福", "笑死", "好玩", "美滋滋"],
    "低落": ["难过", "伤心", "烦", "累", "生气", "崩溃", "郁闷", "沮丧", "失望", "委屈", "心累", "emo", "破防", "想哭", "哭", "难受", "痛苦", "摆烂"],
    "焦虑": ["焦虑", "烦躁", "担心", "害怕", "紧张", "压力", "失眠", "慌", "不安", "坐立不安"],
    "兴奋": ["兴奋", "激动", "期待", "惊喜", "迫不及待", "超爽", "太棒了", "冲鸭", "终于"],
    "愤怒": ["气死", "火大", "忍不了", "太过分", "凭什么", "岂有此理", "烦死了", "滚", "神经病", "有病吧"],
    "惊讶": ["震惊", "离谱", "什么鬼", "不会吧", "真的假的", "天哪", "我去", "居然", "没想到"],
}

EMOTION_METRICS = {
    "开心": {"valence": 0.8, "arousal": 0.5, "dominance": 0.5},
    "低落": {"valence": -0.7, "arousal": 0.6, "dominance": -0.5},
    "焦虑": {"valence": -0.5, "arousal": 0.85, "dominance": -0.4},
    "兴奋": {"valence": 0.8, "arousal": 0.85, "dominance": 0.4},
    "愤怒": {"valence": -0.7, "arousal": 0.7, "dominance": 0.5},
    "恐惧": {"valence": -0.8, "arousal": 0.7, "dominance": -0.7},
    "惊讶": {"valence": 0.0, "arousal": 0.8, "dominance": 0.0},
    "厌恶": {"valence": -0.7, "arousal": 0.3, "dominance": 0.1},
    "平静": {"valence": 0.0, "arousal": 0.0, "dominance": 0.0},
}

PROJECT_WORDS = ["项目", "部署", "服务器", "MCP", "代码", "开发", "上线", "仓库", "学习", "规划"]

CORRECTION_STRONG = [
    "不对", "不是", "错了", "说错", "记错", "记错了", "根本没", "根本不是",
    "并没有", "没有这回事", "撤销", "忘掉", "别记", "反了", "反过来",
    "纠正", "更正", "记反了", "才不是", "乱说",
]
CORRECTION_WORDS = CORRECTION_STRONG + [
    "其实", "改一下", "重新说", "好像不对", "不太对", "等下",
]

PLAYFUL_WORDS = [
    "哈哈", "开玩笑", "玩笑", "嘻嘻", "闹着玩", "😄", "😂", "XD", "狗头", "认真脸",
    "玩梗", "整活", "皮一下", "反串", "阴阳", "内涵", "逗你玩", "🤣", "😝", "😜",
    "🙃", "玩笑话", "说笑", "调侃", "沙雕", "离谱", "哈哈哈",
]

INTENT_CONFIDENCE = {
    "偏好": 0.8,
    "事件": 0.8,
    "回忆": 0.7,
    "求助": 0.55,
    "情绪": 0.6,
    "闲聊": 0.5,
}


def detect_correction(text: str) -> bool:
    """检测纠错信号（用户否定/更正之前的记忆）。"""
    return any(w in (text or "") for w in CORRECTION_WORDS)


def detect_correction_strong(text: str) -> bool:
    """检测强纠错信号（明确说“记错了/不对/不是”），用于更重地降可信度。"""
    return any(w in (text or "") for w in CORRECTION_STRONG)


def detect_playful(text: str) -> bool:
    """检测玩笑/非严肃语境（玩笑记忆不参与 belief 巩固）。"""
    return any(w in (text or "") for w in PLAYFUL_WORDS)


def _intent_of(text: str) -> str:
    for intent, words in INTENT_RULES:
        if any(w in text for w in words):
            return intent
    return "闲聊"


def _emotion_of(text: str) -> str:
    for emotion, words in EMOTION_WORDS.items():
        if any(w in text for w in words):
            return emotion
    return "平静"


def analyze(text: str, reply: str = "") -> dict:
    """分析一条消息：意图、情绪、重要度、事件类型。"""
    text = text or ""
    intent = _intent_of(text)
    emotion = _emotion_of(text + " " + (reply or ""))
    correction = detect_correction(text)
    correction_strong = detect_correction_strong(text)
    playful = detect_playful(text)
    confidence = INTENT_CONFIDENCE.get(intent, 0.5)
    if correction:
        confidence = 0.35
    importance = 0.5
    if intent == "求助":
        importance += 0.15
    if intent == "事件":
        importance += 0.2
    if intent == "偏好":
        importance += 0.15
    if emotion != "平静":
        importance += 0.1
    if len(text) >= 60:
        importance += 0.1
    if any(w in text for w in PROJECT_WORDS):
        importance += 0.1
    metrics = EMOTION_METRICS.get(emotion, {"valence": 0.0, "arousal": 0.0, "dominance": 0.0})
    try:
        from memory import expression as expr_mod
        ex = expr_mod.analyze(text + " " + (reply or ""))
    except Exception as e:
        _stats_err(e)
        ex = {}
    return {
        "intent": intent,
        "emotion": emotion,
        "emotion_source": "rule",
        "importance": round(min(0.95, importance), 2),
        "event_type": classify_event_type(text),
        "confidence": round(confidence, 2),
        "correction": correction,
        "correction_strong": correction_strong,
        "playful": playful,
        "valence": float(metrics["valence"]),
        "arousal": float(metrics["arousal"]),
        "dominance": float(metrics["dominance"]),
        "joke_probability": float(ex.get("joke_probability", 0.0)),
        "expressions": ex.get("expressions", []),
    }


# ===== LLM 轻量情绪/玩笑补充（节流，规则判不出时兜底）=====
LLM_EMOTION_PROMPT = (
    "你是轻量情绪识别器。分析下面对话中“用户”的情绪和语气，只输出 JSON："
    '{"emotion":"开心|低落|焦虑|兴奋|愤怒|恐惧|惊讶|厌恶|平静","playful":true|false}。'
    "playful=true 表示用户在开玩笑/玩梗/反讽。不要输出任何其他内容。"
)

_llm_state = {"ts": 0.0}
_llm_lock = threading.Lock()


def llm_enrich(text, reply="", force=False):
    """LLM 轻量情绪/玩笑分析（带节流与配置开关）；失败返回 None，绝不抛异常。"""
    try:
        from plugins import _shared

        core_cfg = (_shared.CONFIG.get("memory", {}) or {}).get("core", {}) or {}
        cfg = core_cfg.get("analysis", {}) or {}
        if not cfg.get("llm", True):
            return None
        min_interval = float(cfg.get("min_interval_s", 300))
        now = time.time()
        with _llm_lock:
            if not force and now - _llm_state["ts"] < min_interval:
                return None
        resp = _shared.deepseek.chat.completions.create(
            model=_shared.DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": LLM_EMOTION_PROMPT},
                {"role": "user", "content": f"用户：{(text or '')[:300]}\n机器人：{(reply or '')[:300]}"},
            ],
            max_tokens=40,
            temperature=0,
        )
        raw = re.sub(
            r"^```(?:json)?\s*|\s*```$",
            "",
            (resp.choices[0].message.content or "").strip(),
            flags=re.S,
        )
        data = json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
        with _llm_lock:
            _llm_state["ts"] = time.time()
        return data
    except Exception as e:
        _stats_err(e)
        return None


def enrich(an, text, reply=""):
    """规则分析偏弱（情绪=平静且非玩笑）时，用 LLM 补充；失败保持原样。"""
    if not an or an.get("emotion") != "平静" or an.get("playful"):
        return an
    llm = llm_enrich(text, reply)
    if not llm:
        return an
    emotion = str(llm.get("emotion") or "平静").strip()
    if emotion not in EMOTION_WORDS:
        emotion = "平静"
    playful = bool(llm.get("playful"))
    if emotion == "平静" and not playful:
        return an
    out = dict(an)
    out["emotion"] = emotion
    out["emotion_source"] = "llm"
    out["playful"] = playful
    metrics = EMOTION_METRICS.get(emotion, {"valence": 0.0, "arousal": 0.0, "dominance": 0.0})
    out["valence"] = float(metrics["valence"])
    out["arousal"] = float(metrics["arousal"])
    out["dominance"] = float(metrics["dominance"])
    if emotion != "平静":
        out["importance"] = round(min(0.95, float(out.get("importance", 0.5)) + 0.05), 2)
    return out


# ===== 分类路由：把对话/事实分类，存到最适合的存储（词法/向量/图谱/结构化属性）=====
ATTR_BY_ETYPE = {
    "偏好": "preference",
    "家庭": "family",
    "健康": "health",
    "工作": "work",
    "娱乐": "hobby",
}


def route_fact(fact: str, an=None) -> dict:
    """返回该事实应写入的存储：{lexical, vector, graph, structured}。"""
    an = an or analyze(fact)
    etype = an["event_type"]
    return {
        "lexical": True,  # 所有事实进词法索引（专名/短词命中）
        "vector": True,   # 语义相似经历
        "graph": an["intent"] in ("事件", "偏好", "情绪"),
        "structured": an["intent"] in ("偏好",) or etype in ATTR_BY_ETYPE,
    }


def attr_of(fact: str, an=None):
    """结构化属性名（偏好→preference 等）；不适合结构化的返回 None。"""
    an = an or analyze(fact)
    return ATTR_BY_ETYPE.get(an["event_type"])



def _stats_err(e):
    """裸 except 审计（v2.2）：错误计数 + 日志，供消融/排查。"""
    try:
        import memory.stats as _st
        _st.bump_err("analysis", e)
    except Exception:
        pass
