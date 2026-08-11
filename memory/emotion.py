"""多维情绪模型（v31）：Russell 环形（效价-唤醒 2D）+ VAD（3D：加支配度）+ Plutchik 情绪锥（8 基本情绪 × 强度档）。

设计：
- 连续状态：VAD 三维向量（各 ∈ [-1,1]），情绪标签 = 最近 Plutchik 锚点 + 强度。
- AI 情绪状态机：基线（人设默认） + 消息事件增量 + 指数衰减回基线，kv 持久化（重启不丢）。
- 用户情绪：滚动窗口观测 + 效价趋势 + 保守标签，注入 prompt 供 AI 调整语气。
"""

import math
import re
import time
from datetime import datetime, date, timedelta

from plugins import _db, _shared

# ---- Plutchik 八基本情绪锚点（VAD 坐标，主观标定） ----
PLUTCHIK = {
    "喜悦": {"v": 0.85, "a": 0.5, "d": 0.5},
    "信任": {"v": 0.6, "a": 0.1, "d": 0.4},
    "期待": {"v": 0.4, "a": 0.65, "d": 0.3},
    "惊讶": {"v": 0.0, "a": 0.8, "d": 0.0},
    "悲伤": {"v": -0.8, "a": -0.4, "d": -0.5},
    "厌恶": {"v": -0.7, "a": 0.3, "d": 0.1},
    "愤怒": {"v": -0.7, "a": 0.7, "d": 0.6},
    "恐惧": {"v": -0.8, "a": 0.7, "d": -0.7},
}

# Plutchik 强度档位（低 / 中 / 高）
INTENSITY_TIERS = {
    "喜悦": ("安心", "快乐", "狂喜"),
    "信任": ("接受", "信任", "信赖"),
    "期待": ("好奇", "期待", "热望"),
    "惊讶": ("微惊", "惊讶", "震惊"),
    "悲伤": ("忧郁", "悲伤", "悲痛"),
    "厌恶": ("不耐烦", "厌恶", "憎恶"),
    "愤怒": ("恼怒", "愤怒", "暴怒"),
    "恐惧": ("不安", "恐惧", "惊恐"),
}

# ---- 复合情绪（Plutchik 情绪对 + 对立并存）----
PLUTCHIK_COMPOUND = {
    ("喜悦", "信任"): "爱",
    ("信任", "恐惧"): "服从",
    ("恐惧", "惊讶"): "敬畏",
    ("惊讶", "悲伤"): "失望",
    ("悲伤", "厌恶"): "悔恨",
    ("厌恶", "愤怒"): "轻蔑",
    ("愤怒", "期待"): "攻击性",
    ("期待", "喜悦"): "乐观",
}

OPPOSITE_COMPOUND = {
    frozenset(["喜悦", "悲伤"]): "悲喜交加",
    frozenset(["喜悦", "愤怒"]): "哭笑不得",
    frozenset(["期待", "恐惧"]): "期待又不安",
    frozenset(["喜悦", "恐惧"]): "又惊又喜",
    frozenset(["信任", "厌恶"]): "又爱又恨",
}

COMPOUND_WORDS = {
    "悲喜交加": "悲喜交加", "喜忧参半": "悲喜交加",
    "哭笑不得": "哭笑不得", "又气又笑": "哭笑不得", "又气又好笑": "哭笑不得",
    "又惊又喜": "又惊又喜", "又爱又恨": "又爱又恨", "五味杂陈": "五味杂陈",
    "既期待又害怕": "期待又不安", "既期待又怕": "期待又不安", "又期待又紧张": "期待又不安",
    "百感交集": "五味杂陈",
}

_BASIC_EMO_WORDS = {
    "开心": "喜悦", "高兴": "喜悦", "快乐": "喜悦", "喜": "喜悦", "笑": "喜悦",
    "气": "愤怒", "生气": "愤怒", "愤怒": "愤怒", "恼": "愤怒", "火": "愤怒",
    "难过": "悲伤", "伤心": "悲伤", "悲": "悲伤", "哭": "悲伤",
    "怕": "恐惧", "害怕": "恐惧", "慌": "恐惧", "紧张": "恐惧",
    "期待": "期待", "盼": "期待",
    "惊": "惊讶", "吓": "恐惧",
    "厌": "厌恶", "烦": "厌恶",
    "信": "信任", "放心": "信任",
}

# 现有 analysis 标签 → VAD（用户侧观测用，独立于 analysis 的 metrics 以免循环依赖）
LABEL_VAD = {
    "开心": {"v": 0.8, "a": 0.5, "d": 0.5},
    "兴奋": {"v": 0.8, "a": 0.85, "d": 0.4},
    "期待": {"v": 0.4, "a": 0.65, "d": 0.3},
    "焦虑": {"v": -0.5, "a": 0.85, "d": -0.4},
    "低落": {"v": -0.55, "a": -0.35, "d": -0.4},
    "愤怒": {"v": -0.7, "a": 0.7, "d": 0.5},
    "恐惧": {"v": -0.8, "a": 0.7, "d": -0.7},
    "惊讶": {"v": 0.0, "a": 0.8, "d": 0.0},
    "厌恶": {"v": -0.7, "a": 0.3, "d": 0.1},
    "平静": {"v": 0.0, "a": 0.0, "d": 0.0},
}

INTENSIFIERS = (
    (("有点", "稍微", "一点点", "略", "些许"), 0.6),
    (("很", "好", "真", "特别", "超级", "非常", "太"), 1.2),
    (("死了", "爆了", "炸了", "崩溃", "疯", "要命", "到极点"), 1.4),
)

_STRONG_WORDS = (
    "死了", "爆了", "炸了", "崩溃", "疯", "哭", "疼", "救命", "气死", "烦死",
    "好难过", "好开心", "太棒了", "气炸", "绝望", "受不了",
)

_SELF_REPORT_RE = re.compile(
    r"我(?:真的|确实|好|很|特别|有点|感觉|觉得|心情)?(?:好|很)?(生气|难过|开心|高兴|烦|焦虑|紧张|低落|委屈|兴奋|期待)"
)

_CORRECTION_WORDS = ("我没生气", "不是生气", "才没生气", "没难过", "没低落", "开玩笑的", "闹着玩", "反话")

CENTER = {"v": 0.0, "a": 0.0, "d": 0.0}


def _norm(x):
    return max(-1.0, min(1.0, float(x)))


def _v(s):
    return {
        "v": _norm(s.get("v", 0.0)),
        "a": _norm(s.get("a", 0.0)),
        "d": _norm(s.get("d", 0.0)),
    }


def vadd(a, b):
    """VAD 向量相加（夹到 [-1,1]）。"""
    a, b = _v(a), _v(b)
    return {"v": _norm(a["v"] + b["v"]), "a": _norm(a["a"] + b["a"]), "d": _norm(a["d"] + b["d"])}


def dist(a, b):
    a, b = _v(a), _v(b)
    return math.sqrt((a["v"] - b["v"]) ** 2 + (a["a"] - b["a"]) ** 2 + (a["d"] - b["d"]) ** 2)


def label_from_vad(s):
    """最近 Plutchik 锚点 + 强度（0~1，越靠近锚点越高）。返回 (标签, 强度, 主情绪)。"""
    s = _v(s)
    best, best_d = "平静", dist(s, CENTER)
    for name, anchor in PLUTCHIK.items():
        d = dist(s, anchor)
        if d < best_d:
            best, best_d = name, d
    if best == "平静":
        return "平静", 0.0, "平静"
    intensity = 1.0 - min(1.0, best_d / 0.9)
    return best, round(max(0.0, min(1.0, intensity)), 2), best


def label_zh(s):
    """完整强度档（AI 侧可用）：低/中/高三档标签。"""
    name, intensity, _ = label_from_vad(s)
    if name == "平静":
        return "平静"
    tiers = INTENSITY_TIERS[name]
    if intensity < 0.4:
        return tiers[0]
    if intensity < 0.75:
        return tiers[1]
    return tiers[2]


def user_label(s):
    """用户侧保守标签：只取低/中两档，避免把用户情绪说得过重。"""
    name, intensity, _ = label_from_vad(s)
    if name == "平静":
        return "平静"
    tiers = INTENSITY_TIERS[name]
    return tiers[0] if intensity < 0.5 else tiers[1]


# ===== 配置 =====
def _cfg(key, default):
    emo = (_shared.CONFIG.get("memory", {}).get("core", {}) or {}).get("emotion", {}) or {}
    return emo.get(key, default)


# ===== AI 情绪状态机 =====
def ai_baseline():
    """人设基线（千石由乃：平静慵懒——低唤醒、中等支配、略正效价）。可在 config 覆盖。"""
    b = _cfg("baseline", None) or {"v": 0.15, "a": -0.55, "d": 0.5}
    return _v(b)


def ai_state():
    """当前 AI 情绪：先按流逝时间指数衰减回基线。"""
    try:
        import memory.stats as _st
        _st.bump("tick:emotion")
    except Exception as e:
        _stats_err(e)
    data = _db.kv_get("memory", "ai_emotion") or {}
    if not data:
        return ai_baseline()
    s = _v(data)
    raw_ts = data.get("ts", 0.0) or 0.0
    try:
        ts = float(raw_ts)
    except (TypeError, ValueError):
        try:
            ts = datetime.fromisoformat(str(raw_ts)).timestamp()
        except Exception as e:
            _stats_err(e)
            ts = 0.0
    minutes = (time.time() - ts) / 60.0
    half = float(_cfg("decay_minutes", 90))
    if minutes > 0 and half > 0:
        f = 0.5 ** (minutes / half)
        b = ai_baseline()
        s = {
            "v": _norm(b["v"] + (s["v"] - b["v"]) * f),
            "a": _norm(b["a"] + (s["a"] - b["a"]) * f),
            "d": _norm(b["d"] + (s["d"] - b["d"]) * f),
        }
    return s


def _ai_save(s):
    s = dict(_v(s))
    s["ts"] = time.time()
    _db.kv_set("memory", "ai_emotion", s)


def _event_deltas(an, text=""):
    """消息事件 → AI 情绪增量（AI 的“回应式情绪”：对用户情绪/行为做反应）。"""
    an = an or {}
    d = {"v": 0.0, "a": 0.0, "d": 0.0}
    user_emotion = str(an.get("emotion") or "平静")
    intent = str(an.get("intent") or "闲聊")
    playful = bool(an.get("playful")) or float(an.get("joke_probability", 0.0)) >= 0.5
    correction = bool(an.get("correction_strong")) or bool(an.get("correction"))
    t = str(text or "")

    if playful:
        d = vadd(d, {"v": 0.12, "a": 0.18, "d": 0.0})          # 玩梗：来点兴致
    if user_emotion in ("低落", "焦虑", "恐惧", "悲伤"):
        d = vadd(d, {"v": 0.08, "a": -0.12, "d": 0.12})        # 用户不好 → 收着点、认真关心
    elif user_emotion in ("兴奋", "开心", "喜悦", "期待"):
        d = vadd(d, {"v": 0.18, "a": 0.08, "d": 0.04})         # 用户开心 → 心情跟着好
    elif user_emotion in ("愤怒", "厌恶"):
        d = vadd(d, {"v": -0.12, "a": 0.02, "d": 0.08})        # 用户不爽 → 少惹、稳住
    if intent == "求助":
        d = vadd(d, {"v": 0.04, "a": 0.28, "d": 0.18})         # 被认真求助 → 投入
    if correction:
        d = vadd(d, {"v": -0.12, "a": 0.08, "d": -0.08})       # 被纠正 → 轻微吃瘪
    if any(w in t for w in ("谢谢", "厉害", "好棒", "靠谱", "爱你", "喜欢", "辛苦了", "真棒", "夸")):
        try:
            from memory import interaction as interaction_mod
            f = interaction_mod.fatigue_mult("praise")  # 刺激适应：连夸效果递减
        except Exception as e:
            _stats_err(e)
            f = 1.0
        d = vadd(d, {"v": 0.22 * f, "a": 0.0, "d": 0.08 * f})   # 被夸 → 心里高兴
    if any(w in t for w in ("滚", "别烦", "废物", "蠢", "闭嘴", "差劲", "讨厌你")):
        d = vadd(d, {"v": -0.3, "a": 0.15, "d": -0.15})        # 被冒犯 → 冷淡反击
    return d


def ai_apply(an, text="", scope=""):
    """用一条消息更新 AI 情绪（先衰减再增量），并记录情绪归因（谁触发的）。返回新状态。"""
    if not _cfg("enabled", True):
        return None
    cur = ai_state()
    nxt = vadd(cur, _event_deltas(an, text))
    try:
        from memory import interaction as interaction_mod
        if any(w in str(text or "") for w in ("谢谢", "厉害", "好棒", "靠谱", "爱你", "喜欢", "辛苦了", "真棒", "夸")):
            interaction_mod.mark_event("praise")
    except Exception as e:
        _stats_err(e)
        pass
    _ai_save(nxt)
    data = _db.kv_get("memory", "ai_emotion") or {}
    data["last_trigger_scope"] = scope
    data["last_trigger_ts"] = datetime.now().isoformat(timespec="seconds")
    _db.kv_set("memory", "ai_emotion", data)
    return nxt


def attribution_block(scope="") -> str:
    """情绪归因（v31.2）：情绪是别人惹的，对当前用户收着点。"""
    if not scope:
        return ""
    data = _db.kv_get("memory", "ai_emotion") or {}
    trigger = str(data.get("last_trigger_scope") or "")
    if not trigger or trigger == scope:
        return ""
    try:
        age_h = (datetime.now() - datetime.fromisoformat(str(data.get("last_trigger_ts") or ""))).total_seconds() / 3600.0
    except Exception as e:
        _stats_err(e)
        age_h = 999.0
    if age_h > 3:
        return ""
    return "【情绪归因】她现在的情绪不是因为你，是刚才和其他人互动留下的。别让这份情绪全撒在用户身上，适当收着点；被问起也别甩锅给具体的人。"


def ai_display(s=None):
    """AI 侧展示文案：VAD 分区 → 人设化表达（慵懒、嘴硬、冷淡但靠谱）。"""
    s = _v(s or ai_state())
    v, a, d = s["v"], s["a"], s["d"]
    if v <= -0.45 and a >= 0.45:
        return "冷淡，带点火气"
    if v <= -0.45 and d < 0.0:
        return "心里有点委屈，但不会说"
    if v <= -0.35:
        return "情绪不佳，爱答不理"
    if v >= 0.5 and a >= 0.5:
        return "难得来劲"
    if v >= 0.35 and a < 0.25:
        return "心里偷着高兴，但面上淡淡的"
    if a <= -0.3:
        return "平静慵懒"
    return "平静"


def ai_block():
    """AI 当前情绪注入块（短小，供 persona.compose 使用）。"""
    return f"【当前情绪：{ai_display()}】"


# ===== 用户情绪观测 =====
def _user_vad(an):
    an = an or {}
    label = str(an.get("emotion") or "平静")
    if label in LABEL_VAD:
        return dict(LABEL_VAD[label])
    # 兼容已有 VAD 字段
    return {
        "v": _norm(an.get("valence", 0.0)),
        "a": _norm(an.get("arousal", 0.0)),
        "d": _norm(an.get("dominance", 0.0)),
    }


def _intensity_mult(text) -> float:
    """情绪强度：有点×0.6 / 很·非常×1.2 / 死了·崩溃×1.4。"""
    t = str(text or "")
    m = 1.0
    for words, k in INTENSIFIERS:
        if any(w in t for w in words):
            m = max(m, k)
    return m


def _confidence_of(text, an, source="rule") -> float:
    """判断置信度：强词 0.85 / 普通规则命中 0.7 / 玩笑 0.5 / LLM 兜底 0.5 / 纯规则兜底 0.3。"""
    t = str(text or "")
    if source == "llm":
        return 0.5
    if any(w in t for w in _STRONG_WORDS):
        return 0.85
    if an and an.get("emotion") != "平静":
        return 0.7
    if an and (an.get("playful") or float(an.get("joke_probability", 0.0)) >= 0.5):
        return 0.5
    return 0.3


def _compound_of(text, label, vad) -> str:
    """复合情绪检测：词表直配 → '又X又Y' 情绪对 → 正负词并存兜底。"""
    t = str(text or "")
    for w, c in COMPOUND_WORDS.items():
        if w in t:
            return c
    m = re.search(r"(?:又|既)([\u4e00-\u9fff]{1,3})(?:又|还)([\u4e00-\u9fff]{1,3})", t)
    if m:
        a, b = _BASIC_EMO_WORDS.get(m.group(1)), _BASIC_EMO_WORDS.get(m.group(2))
        if a and b:
            if a == b:
                return ""
            pair = frozenset([a, b])
            if pair in OPPOSITE_COMPOUND:
                return OPPOSITE_COMPOUND[pair]
            if (a, b) in PLUTCHIK_COMPOUND:
                return PLUTCHIK_COMPOUND[(a, b)]
            if (b, a) in PLUTCHIK_COMPOUND:
                return PLUTCHIK_COMPOUND[(b, a)]
    pos = any(w in t for w in ("开心", "高兴", "快乐", "喜", "爱", "期待", "好笑", "好耶", "笑死"))
    neg = any(w in t for w in ("难过", "生气", "气", "伤心", "烦", "怕", "哭", "倒霉", "崩溃"))
    if pos and neg and len(t) >= 6:
        return "五味杂陈"
    return ""


def judge(text="", an=None, scope="") -> dict:
    """单条消息的情绪判断：规则 → 强度修正 → 上下文修正 → 置信度。"""
    if an is None:
        try:
            from memory import analysis as analysis_mod
            an = analysis_mod.analyze(text or "")
        except Exception as e:
            _stats_err(e)
            an = {}
    an = an or {}
    label = str(an.get("emotion") or "平静")
    base = _user_vad(an)
    mult = _intensity_mult(text)
    vad = {
        "v": _norm(base["v"] * mult),
        "a": _norm(base["a"] * mult),
        "d": _norm(base["d"] * mult),
    }
    src = str(an.get("emotion_source") or "rule")
    conf = _confidence_of(text, an, src)
    # 上下文：近几条明显低落时，"哈哈/还行"更像苦笑/强颜欢笑
    if scope:
        rows = (_db.kv_get("memory", f"user_emotion:{scope}") or {}).get("rows") or []
        if len(rows) >= 2:
            recent = rows[-3:]
            avg_v = sum(float(r.get("v", 0.0)) for r in recent) / len(recent)
            if avg_v < -0.4 and vad["v"] > 0.15:
                vad["v"] = _norm(-0.2)
                conf = min(conf, 0.6)
                if label == "平静":
                    label = "低落"
    out = {"emotion": label, "vad": vad, "confidence": round(conf, 2), "source": src}
    compound = _compound_of(text, label, vad)
    if compound:
        out["compound"] = compound
    return out


def log_judgment(scope, text, j):
    """数据管道：把每次判断写进日志（训练集原料，按日期分桶）。"""
    if not scope:
        return
    d = date.today().isoformat()
    key = f"emotion_log:{d}"
    data = _db.kv_get("memory", key) or {"rows": []}
    rows = list(data.get("rows") or [])
    rows.append(
        {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "scope": scope,
            "text": str(text or "")[:60],
            "emotion": j["emotion"],
            "v": j["vad"]["v"],
            "a": j["vad"]["a"],
            "d": j["vad"]["d"],
            "confidence": j["confidence"],
            "source": j["source"],
        }
    )
    data["rows"] = rows[-500:]
    _db.kv_set("memory", key, data)


def record_feedback(scope, text):
    """用户显式情绪自述/纠正 → 强/弱标签（训练数据）。"""
    if not scope:
        return None
    t = str(text or "")
    m = _SELF_REPORT_RE.search(t)
    kind, label = None, ""
    if m:
        kind, label = "self_report", m.group(1)
    elif any(w in t for w in _CORRECTION_WORDS):
        kind, label = "correction", "纠正"
    if not kind:
        return None
    d = date.today().isoformat()
    key = f"emotion_log:{d}"
    data = _db.kv_get("memory", key) or {"rows": []}
    rows = list(data.get("rows") or [])
    rows.append(
        {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "scope": scope,
            "text": t[:60],
            "kind": kind,
            "label": label,
        }
    )
    data["rows"] = rows[-500:]
    _db.kv_set("memory", key, data)
    return {"kind": kind, "label": label}


def user_observe(scope, an, text=""):
    """记录一次用户情绪观测（滚动窗口，按 scope 隔离）。"""
    if not scope or not an:
        return
    j = judge(text, an, scope)
    s = j["vad"]
    key = f"user_emotion:{scope}"
    data = _db.kv_get("memory", key) or {}
    rows = data.get("rows") or []
    rows.append(
        {
            "ts": time.time(),
            "v": s["v"],
            "a": s["a"],
            "d": s["d"],
            "emotion": str(an.get("emotion") or "平静"),
            "playful": bool(an.get("playful")) or float(an.get("joke_probability", 0.0)) >= 0.5,
            "confidence": j["confidence"],
            "source": j["source"],
        }
    )
    max_n = max(1, int(_cfg("user_window", 5)))
    _db.kv_set("memory", key, {"rows": rows[-max_n:]})
    log_judgment(scope, text, j)


def user_estimate(scope):
    """窗口内 VAD 加权平均（时间衰减 + 位置加权）+ 效价趋势。无观测返回 None。"""
    rows = (_db.kv_get("memory", f"user_emotion:{scope}") or {}).get("rows") or []
    if not rows:
        return None
    n = len(rows)
    now = time.time()
    weights = []
    for i, r in enumerate(rows):
        try:
            age_h = max(0.0, (now - float(r.get("ts", now))) / 3600.0)
        except Exception as e:
            _stats_err(e)
            age_h = 0.0
        rec = 0.5 ** (age_h / 12.0)          # 时间衰减：12 小时半衰期
        pos = 0.5 + 0.5 * i / max(1, n - 1)  # 越近权重越高
        weights.append(rec * pos)
    total = sum(weights)
    s = {"v": 0.0, "a": 0.0, "d": 0.0}
    conf = 0.0
    for r, w in zip(rows, weights):
        s["v"] += float(r.get("v", 0.0)) * w / total
        s["a"] += float(r.get("a", 0.0)) * w / total
        s["d"] += float(r.get("d", 0.0)) * w / total
        conf += float(r.get("confidence", 0.5)) * w / total
    half = max(1, n // 2)
    v_first = sum(float(r["v"]) for r in rows[:half]) / half
    rest = rows[half:]
    v_last = sum(float(r["v"]) for r in rest) / len(rest) if rest else v_first
    trend = "变好" if v_last - v_first > 0.2 else ("变差" if v_first - v_last > 0.2 else "平稳")
    return {
        "vad": s,
        "label": user_label(s),
        "trend": trend,
        "n": n,
        "confidence": round(conf, 2),
        "decay_n": sum(1 for r in rows if _age_hours(r) <= 24),
        "playful_recent": any(r.get("playful") for r in rows[-2:]),
    }


def _age_hours(r) -> float:
    try:
        return max(0.0, (time.time() - float(r.get("ts", time.time()))) / 3600.0)
    except Exception as e:
        _stats_err(e)
        return 0.0


def user_block(scope):
    """用户情绪注入块（内部参考）：供 AI 调整语气。无信号时返回空串。"""
    est = user_estimate(scope)
    if not est or (est["label"] == "平静" and est["trend"] == "平稳"):
        return ""
    head = "【用户情绪（可能）" if float(est.get("confidence", 1.0)) < 0.5 else "【用户情绪："
    parts = [f"{head}{est['label']}】"]
    if est["trend"] != "平稳":
        parts[0] += f"（趋势：{est['trend']}）"
    if est["label"] in ("忧郁", "悲伤", "低落", "不安", "恐惧", "焦虑"):
        parts.append("内部提示：用户心情不好，回复放轻、少开玩笑、多耐心。")
    elif est["label"] in ("恼怒", "愤怒", "憎恶", "厌恶"):
        parts.append("内部提示：用户带着情绪，别硬顶，先顺一下再讲道理。")
    elif est.get("playful_recent"):
        parts.append("内部提示：用户最近在玩梗/开玩笑，别当真。")
    return "；".join(parts)


# ===== 评测与数据管道导出 =====
def eval_probes(probes, scope="") -> dict:
    """情绪判断评测：分类准确率 + VAD MAE + 分桶。
    probes: [{"text": ..., "emotion": "开心", "v": 0.8, "a": 0.5, "d": 0.5}]。"""
    results, cats = [], {}
    for p in probes or []:
        text = str(p.get("text", ""))
        j = judge(text, scope=scope)
        exp = p.get("emotion")
        exp_compound = str(p.get("compound") or "")
        if exp_compound:
            # 复合探针以复合标签为准（主标签由普通探针单独测）
            ok = str(j.get("compound") or "") == exp_compound
        else:
            ok = (not exp) or (j["emotion"] == exp)
        mae = 0.0
        if "v" in p:
            mae = (
                abs(j["vad"]["v"] - float(p["v"]))
                + abs(j["vad"]["a"] - float(p.get("a", 0)))
                + abs(j["vad"]["d"] - float(p.get("d", 0)))
            ) / 3.0
        cat = exp_compound or exp or "vad_only"
        c = cats.setdefault(cat, {"n": 0, "hit": 0, "mae": 0.0})
        c["n"] += 1
        if ok:
            c["hit"] += 1
        c["mae"] += mae
        results.append(
            {"text": text[:30], "pred": j["emotion"], "expected": exp,
             "compound": j.get("compound", ""), "compound_expected": exp_compound,
             "hit": ok, "mae": round(mae, 3), "confidence": j["confidence"]}
        )
    n = len(probes or [])
    acc = sum(1 for r in results if r["hit"]) / n if n else 0.0
    mae_all = sum(r["mae"] for r in results) / n if n else 0.0
    comp_n = sum(1 for p in probes or [] if p.get("compound"))
    comp_hit = sum(1 for r in results if r.get("compound_expected") and r["hit"])
    return {
        "n": n,
        "accuracy": round(acc, 3),
        "vad_mae": round(mae_all, 3),
        "compound_accuracy": round(comp_hit / comp_n, 3) if comp_n else None,
        "compound_n": comp_n,
        "by_category": {
            k: {"n": v["n"], "accuracy": round(v["hit"] / v["n"], 3),
                "mae": round(v["mae"] / v["n"], 3)}
            for k, v in cats.items()
        },
        "samples": results[:10],
    }


def emotion_log_rows(days=14) -> list:
    """导出情绪判断日志（训练数据原料），按日期倒序。"""
    out = []
    for i in range(max(1, int(days))):
        d = (date.today() - timedelta(days=i)).isoformat()
        data = _db.kv_get("memory", f"emotion_log:{d}") or {}
        for r in (data.get("rows") or []):
            r = dict(r)
            r.setdefault("date", d)
            out.append(r)
    return out



def _stats_err(e):
    """裸 except 审计（v2.2）：错误计数 + 日志，供消融/排查。"""
    try:
        import memory.stats as _st
        _st.bump_err("emotion", e)
    except Exception:
        pass
