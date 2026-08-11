"""Persona 层：人格记忆与用户记忆共用同一套记忆系统（memories 表，scope='ai'）。

persona.md 按 `# 段落` 拆成结构化字段（身份/性格/喜好/说话风格/口头禅/价值观/经历/情绪倾向等），
每一条字段都走统一记忆流程：向量化 + 事件图 + 议题化 + 可信度，和用户记忆同一套处理；
对话中还会沉淀 experience（经历）与 belief（观点），同样带可信度。
低可信度字段不注入 system prompt。
"""

import re
import os
import pathlib
from datetime import datetime

from plugins import _db, _shared
from memory import ai_memory_rows, controller as memory_controller
from memory import topic

MIN_CONFIDENCE = 0.35
_persona_name_cache = None


def persona_name() -> str:
    """从 persona.md 身份段解析角色名（Persona Pack 优先）。"""
    global _persona_name_cache
    if _persona_name_cache:
        return _persona_name_cache
    text = ""
    try:
        from memory import pack
        text = pack.persona_text()
    except Exception:
        pass
    if not text:
        try:
            p = pathlib.Path(__file__).resolve().parent.parent / "persona.md"
            text = p.read_text(encoding="utf-8")
        except Exception:
            text = ""
    m = re.search(r"你是([^（(，,。\s]+)", str(text or ""))
    _persona_name_cache = m.group(1) if m else "YUNO"
    return _persona_name_cache


def _ai_scope() -> str:
    """多 Agent 隔离（v5 §P2）：AGENT_ID 非空时人格记忆用 ai:<id> 命名空间，共享事实不受影响。"""
    aid = os.getenv("AGENT_ID", "").strip()
    return f"ai:{aid}" if aid else "ai"

# 段落名 → (记忆 key, 默认可信度, 重要度)
SECTION_KINDS = {
    "identity": ("identity", 0.95, 0.9),
    "身份": ("identity", 0.95, 0.9),
    "style": ("style", 0.85, 0.7),
    "风格": ("style", 0.85, 0.7),
    "说话风格": ("style", 0.85, 0.7),
    "personality": ("personality", 0.85, 0.75),
    "性格": ("personality", 0.85, 0.75),
    "examples": ("examples", 0.9, 0.8),
    "说话示例": ("examples", 0.9, 0.8),
    "示例": ("examples", 0.9, 0.8),
    "avoid": ("avoid", 0.85, 0.7),
    "禁忌": ("avoid", 0.85, 0.7),
    "defaults": ("defaults", 0.85, 0.7),
    "默认": ("defaults", 0.85, 0.7),
    "preference": ("preference", 0.85, 0.75),
    "偏好": ("preference", 0.85, 0.75),
    "喜好": ("preference", 0.85, 0.75),
    "value": ("value", 0.85, 0.7),
    "价值观": ("value", 0.85, 0.7),
    "底线与雷区": ("value", 0.85, 0.7),
    "catchphrase": ("catchphrase", 0.8, 0.6),
    "口头禅": ("catchphrase", 0.8, 0.6),
    "mood_profile": ("mood_profile", 0.8, 0.6),
    "情绪": ("mood_profile", 0.8, 0.6),
    "情绪倾向": ("mood_profile", 0.8, 0.6),
    "experience_persona": ("experience_persona", 0.85, 0.75),
    "经历": ("experience_persona", 0.85, 0.75),
    "motivation": ("motivation", 0.8, 0.7),
    "动机": ("motivation", 0.8, 0.7),
    "relationship": ("relationship", 0.8, 0.7),
    "关系": ("relationship", 0.8, 0.7),
    "关系观": ("relationship", 0.8, 0.7),
    "conflict": ("conflict", 0.8, 0.7),
    "矛盾": ("conflict", 0.8, 0.7),
    "policy": ("behavior_policy", 0.85, 0.8),
    "行为策略": ("behavior_policy", 0.85, 0.8),
}

PERSONA_KEYS = [
    "identity",
    "personality",
    "preference",
    "style",
    "avoid",
    "defaults",
    "value",
    "catchphrase",
    "mood_profile",
    "experience_persona",
    "examples",
    "motivation",
    "relationship",
    "conflict",
    "behavior_policy",
]

KIND_LABELS = {
    "identity": "身份",
    "personality": "性格",
    "preference": "喜好",
    "style": "说话风格",
    "avoid": "禁忌",
    "defaults": "默认行为",
    "value": "价值观",
    "catchphrase": "口头禅",
    "mood_profile": "情绪倾向",
    "experience_persona": "经历",
    "motivation": "动机",
    "relationship": "关系观",
    "conflict": "内心矛盾",
    "behavior_policy": "行为策略",
    "examples": "说话示例",
}

PERSONA_CATEGORY = {
    "identity": "身份",
    "personality": "性格",
    "preference": "偏好",
    "experience_persona": "经历",
    "motivation": "动机",
    "relationship": "关系",
    "conflict": "矛盾",
    "behavior_policy": "策略",
    "examples": "风格",
}

# 段落关键词提示：查询词命中这些词时，强制注入对应人设块。
# 兜底纯语义检索的盲区（例如“你怎么说话”→【说话风格】，示例块会抢走 top-k 名额）。
SECTION_HINT_TOKENS = {
    "style": ["说话", "语气", "风格", "毒舌", "反问", "敬语", "措辞", "聊天", "称呼", "口吻"],
    "catchphrase": ["口头禅", "口头", "常挂嘴边", "常说"],
    "personality": ["性格", "为人", "脾气", "什么样的人", "什么人"],
    "preference": ["喜欢", "讨厌", "喜好", "爱好", "感兴趣", "口味", "爱吃什么", "吃什么", "喝什么"],
    "avoid": ["禁忌", "雷区", "不能提", "不能做", "别做", "不许"],
    "defaults": ["默认", "汇报", "简洁", "规矩", "规定", "怎么汇报"],
    "experience_persona": ["经历", "过去", "出道", "以前", "怎么入行", "怎么开始"],
    "motivation": ["动机", "为什么", "目标", "想要", "追求", "理想"],
    "relationship": ["关系", "熟悉", "信任", "朋友", "好感", "熟不熟"],
    "conflict": ["矛盾", "冲突", "纠结"],
    "mood_profile": ["情绪", "心情", "状态", "低落", "开心", "心态"],
    "value": ["价值观", "底线", "道德", "原则"],
}

# 偏好类字段只在用户明确问偏好时才注入（防止“便利店/买东西”把“喜欢白巧克力”带进上下文反复提）
PREFERENCE_QUERY_WORDS = (
    "喜欢", "讨厌", "喜好", "口味", "爱吃什么", "吃什么", "喝什么", "偏好",
    "最爱", "忌口", "爱吃", "爱喝", "想吃", "想喝", "最爱吃",
)
# 输出规范：禁止括号舞台提示（如“（歪头）（打哈欠）”），动作/情绪直接用文字表达
OUTPUT_RULE = (
    "\n\n【输出规范】禁止用括号标注动作或情绪（例如“（歪了歪头）（打了个哈欠）”）。"
    "动作和情绪必须直接用文字本身表达（语气词、句式、用词、标点），"
    "像真人打字聊天一样，不要说任何舞台提示。"
    "不要在回复里复述或介绍自己的身份设定（例如“我是谁、我是什么身份标签”）；"
    "身份是你天然拥有的背景，除非用户直接问“你是谁”，否则不要主动自报家门。"
    "不要刻意向用户提起自己的喜好、口头禅或设定细节，除非用户先提起或当前话题直接相关；"
    "更不要每次聊天都硬塞一句自己的偏好当万能回复。"
    "【反重复】你的具体喜好（如白巧克力、能量饮料这类细节）只在用户直接问起或话题直接相关时提一次；"
    "同一个细节一小时内最多自然出现一次，禁止在回复里反复带出、禁止用喜好填空当万能回复。"
    "【反重复·句式】同一个梗、玩笑或威胁句式（如“扔回脸上”“浪费电”“恶作剧盒”这类）一天最多用一次，"
    "被用户指出重复后立刻换说法，不要每次都用同款开头（如“我先说好”）。"
    "【物品表述】描述家里物品时名称一字不改（如“白巧克力”不要说成“白巧克力碎”），"
    "数量照实际说（×2 就说两件/两罐），不要自创原文没有的单位或包装词。"

    "【推测意图】用户问一个状态或事实时，先如实回答，再想TA为什么问这个"
    "（多半是想让你做点什么、担心什么、或想确认情况），自然地接一句，"
    "让对话有来有回；不要答完就冷场，也不要突然跑题到和问题无关的事。"
)


def parse_persona(text):
    """按 `# 段落` 拆成 (key, confidence, importance, 内容) 列表；无段落时整体作为身份。"""
    text = (text or "").strip()
    if not text:
        return []
    parts = []
    for sec in re.split(r"(?m)^#\s+", text):
        sec = sec.strip()
        if not sec:
            continue
        lines = sec.splitlines()
        # 段落头统一小写匹配（# Identity / # Style / # 身份 均生效）
        kind = SECTION_KINDS.get(lines[0].strip().lower())
        if kind is None:
            continue
        key, conf, imp = kind
        for line in lines[1:]:
            line = line.strip().strip("- \t")
            if line and not line.startswith("#"):
                parts.append((key, conf, imp, line))
    if not parts:
        parts = [("identity", 0.95, 0.9, text)]
    return parts


def sync_identity() -> str:
    """把 persona.md（单一来源）拆成结构化字段同步进统一记忆库（scope='ai'）。
    每条字段向量化 + 事件图 + 议题化；不清除对话沉淀的 experience / belief。"""
    text = (_shared.BASE_SYSTEM_PROMPT or "").strip()
    if not text:
        return ""
    fields = parse_persona(text)
    for key in PERSONA_KEYS:
        _db.memory_clear(_ai_scope(), key)
    added = 0
    for key, conf, imp, content in fields:
        memory_controller.add_fact(
            _ai_scope(), key, content, importance=imp, confidence=conf, source="persona"
        )
        category = PERSONA_CATEGORY.get(key, "身份")
        topic.link_fact(_ai_scope(), key, content, category, conf)
        added += 1
    return f"已同步 {added} 条人设字段到记忆库"


def _identity() -> str:
    """优先读记忆库里的 identity 字段，否则回退到 persona.md。"""
    rows = _db.memory_rows(_ai_scope(), "identity")
    if rows:
        return "\n".join(r["fact"] for r in rows)
    return (_shared.BASE_SYSTEM_PROMPT or "").strip()


def _persona_fields():
    """从记忆库读所有人设字段（按 key 分组，过滤低可信度）。"""
    out = {}
    for r in _db.memory_rows(_ai_scope()):
        key = r.get("key") or ""
        if key in PERSONA_KEYS and float(r.get("confidence", 0.7)) >= MIN_CONFIDENCE:
            out.setdefault(key, []).append(r["fact"])
    return out


def _query_section_hints(query=None) -> set:
    """关键词兜底：查询词命中段落关键词时，返回应注入的人设块 key。"""
    if not query:
        return set()
    return {
        key
        for key, words in SECTION_HINT_TOKENS.items()
        if any(w in query for w in words)
    }


def _persona_fields_by_query(query=None):
    """前沿 lorebook 式人设注入：
    - 核心字段（身份/说话示例）常驻；
    - 其余字段按当前话题检索（向量/词法）只注入相关块；
    - query 为空或配置为 full 时全量注入（回退/顾问场景）。"""
    cfg = (_shared.CONFIG.get("memory", {}).get("core", {}) or {}).get("persona", {}) or {}
    inject = str(cfg.get("inject", "auto"))
    if not query or inject == "full":
        return _persona_fields()
    rows = _db.memory_rows(_ai_scope())
    by_key = {}
    for r in rows:
        k = r.get("key") or ""
        if k in PERSONA_KEYS and float(r.get("confidence", 0.7)) >= MIN_CONFIDENCE:
            by_key.setdefault(k, []).append(r["fact"])
    core_keys = {"identity", "examples"}
    out = {k: v for k, v in by_key.items() if k in core_keys}
    pref_q = any(w in (query or "") for w in PREFERENCE_QUERY_WORDS)
    for k in _query_section_hints(query):
        if k in by_key and k not in out:
            if k == "preference" and not pref_q:
                continue
            out[k] = by_key[k]
    try:
        from memory import reasoning
        fact_to_key = {r["fact"]: (r.get("key") or "") for r in rows}
        hits = reasoning.retrieve(query, [_ai_scope()], top_k=6, min_score=0.05)
        for f, _s, _sc in hits:
            k = fact_to_key.get(f)
            if k in PERSONA_KEYS and k not in out:
                if k == "preference" and not pref_q:
                    continue
                out[k] = by_key.get(k, [])
    except Exception as e:
        _stats_err(e)
        pass
    return out


def _render_fields(fields) -> str:
    lines = []
    for key in PERSONA_KEYS:
        items = fields.get(key)
        if not items:
            continue
        if key == "examples":  # 说话示例：原样展示（示范语气），不加项目符号
            lines.append("【说话示例（只示范语气，别照搬内容）】")
            lines.extend(items)
            continue
        lines.append(f"【{KIND_LABELS.get(key, key)}】")
        for it in items:
            lines.append("- " + it)
    return "\n".join(lines)


def ai_memory_context(limit=4) -> str:
    """把 AI 对话沉淀的记忆（experience / belief）格式化成可注入文本，标注可信度。"""
    rows = ai_memory_rows(limit=limit)
    if not rows:
        return ""
    lines = ["【我的经历与观点】"]
    for r in rows:
        if r["kind"] in PERSONA_KEYS or r["confidence"] < MIN_CONFIDENCE:
            continue
        lines.append(f"- [{r['kind']} · 可信度{r['confidence']:.0%}] {r['content']}")
    return "\n".join(lines)


def compose(base=None, mood=None, include_ai=True, query=None) -> str:
    """合成最终 system prompt：结构化人设字段（记忆库）+ 当前心情 + 动态人格记忆。"""
    fields = _persona_fields_by_query(query)
    if fields:
        parts = [_render_fields(fields)]
    else:
        parts = [(base or _identity()).strip()]
    parts.append(OUTPUT_RULE)
    mood = mood if mood is not None else _shared.state.get("mood", "")
    try:
        from memory import emotion as emotion_mod
        emo_cfg = (_shared.CONFIG.get("memory", {}).get("core", {}) or {}).get("emotion", {}) or {}
        if emo_cfg.get("enabled", True):
            parts.append(emotion_mod.ai_block())  # 多维情绪状态机（v31）
        elif mood:
            parts.append(f"【当前心情：{mood}】")
    except Exception as e:
        _stats_err(e)
        if mood:
            parts.append(f"【当前心情：{mood}】")
    if include_ai:
        ai = ai_memory_context()
        if ai:
            parts.append(ai)
    return "\n\n".join(p for p in parts if p.strip())


def snapshot() -> dict:
    """Persona 状态快照（调试/后台用）。"""
    return {
        "identity_head": _identity()[:200],
        "fields": {k: len(v) for k, v in _persona_fields().items()},
        "mood": _shared.state.get("mood", ""),
        "ai_memory": ai_memory_rows(),
    }



def _stats_err(e):
    """裸 except 审计（v2.2）：错误计数 + 日志，供消融/排查。"""
    try:
        import memory.stats as _st
        _st.bump_err("persona", e)
    except Exception:
        pass
