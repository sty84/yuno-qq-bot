"""Persona 层：人格记忆与用户记忆共用同一套记忆系统（memories 表，scope='ai'）。

persona.md 按 `# 段落` 拆成结构化字段（身份/性格/喜好/说话风格/口头禅/价值观/经历/情绪倾向等），
每一条字段都走统一记忆流程：向量化 + 事件图 + 议题化 + 可信度，和用户记忆同一套处理；
对话中还会沉淀 experience（经历）与 belief（观点），同样带可信度。
低可信度字段不注入 system prompt。
"""

import re
import os
from datetime import datetime

from plugins import _db, _shared
from memory import ai_memory_rows, controller as memory_controller
from memory import topic

MIN_CONFIDENCE = 0.35


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
    "avoid": ("avoid", 0.85, 0.7),
    "禁忌": ("avoid", 0.85, 0.7),
    "defaults": ("defaults", 0.85, 0.7),
    "默认": ("defaults", 0.85, 0.7),
    "preference": ("preference", 0.85, 0.75),
    "偏好": ("preference", 0.85, 0.75),
    "喜好": ("preference", 0.85, 0.75),
    "value": ("value", 0.85, 0.7),
    "价值观": ("value", 0.85, 0.7),
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
}

# 输出规范：禁止括号舞台提示（如“（歪头）（打哈欠）”），动作/情绪直接用文字表达
OUTPUT_RULE = (
    "\n\n【输出规范】禁止用括号标注动作或情绪（例如“（歪了歪头）（打了个哈欠）”）。"
    "动作和情绪必须直接用文字本身表达（语气词、句式、用词、标点），"
    "像真人打字聊天一样，不要说任何舞台提示。"
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


def _render_fields(fields) -> str:
    lines = []
    for key in PERSONA_KEYS:
        items = fields.get(key)
        if not items:
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


def compose(base=None, mood=None, include_ai=True) -> str:
    """合成最终 system prompt：结构化人设字段（记忆库）+ 当前心情 + 动态人格记忆。"""
    fields = _persona_fields()
    if fields:
        parts = [_render_fields(fields)]
    else:
        parts = [(base or _identity()).strip()]
    parts.append(OUTPUT_RULE)
    mood = mood if mood is not None else _shared.state.get("mood", "")
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
