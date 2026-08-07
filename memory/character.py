"""人物设定检索入库：输入人物名称，用 LLM 知识生成设定/经历档案，存入统一记忆。

存储格式与用户/AI 记忆完全一致（memories 表，scope='char:<名>'）：向量化 + 事件图 +
议题化 + 可信度（AI 知识默认 0.7，用户可随时纠正）。查询提到该人物时自动注入检索。
"""

import json
import re
from datetime import datetime

from plugins import _db, _shared
from memory import controller as memory_controller
from memory import topic

CHAR_PROMPT = (
    "你是角色资料库。请为「{name}」整理一份角色档案（基于你的知识，虚构或现实人物均可）。"
    "只输出一个 JSON 对象，键为："
    "basic(基本信息列表), personality(性格列表), style(说话风格列表), "
    "experience(经历时间线列表，按时间排序), relations(人物关系列表), quotes(口头禅/名言列表)。"
    "每个列表项是一句简短陈述（不超过30字），experience 每项尽量以时间开头，例如“2023年加入乐队”。"
    "信息不确定的项末尾加“（存疑）”。不要输出任何其他内容。"
)

# 档案键 → (记忆 key, 议题大类)
KIND_META = {
    "basic": ("identity", "身份"),
    "personality": ("personality", "性格"),
    "style": ("style", "说话风格"),
    "experience": ("experience", "经历"),
    "relations": ("relations", "人物关系"),
    "quotes": ("catchphrase", "口头禅"),
}


def _norm_item(it):
    if isinstance(it, dict):
        for k in ("info", "fact", "content", "text", "name", "time"):
            if it.get(k):
                return str(it[k]).strip()
        values = [str(v).strip() for v in it.values() if str(v).strip()]
        return values[0] if values else ""
    return str(it).strip()


def _llm_dossier(name, llm=None) -> dict:
    """生成角色档案 JSON；任何失败都返回 {}，不抛异常。"""
    try:
        if llm:
            raw = llm(name)
        else:
            resp = _shared.deepseek.chat.completions.create(
                model=_shared.DEEPSEEK_MODEL,
                messages=[
                    {"role": "system", "content": CHAR_PROMPT.format(name=name)},
                    {"role": "user", "content": f"角色：{name}"},
                ],
                max_tokens=900,
                temperature=0.3,
            )
            raw = resp.choices[0].message.content or ""
        raw = re.sub(
            r"^```(?:json)?\s*|\s*```$", "", str(raw).strip(), flags=re.S
        )
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end < 0:
            return {}
        data = json.loads(raw[start:end + 1])
        out = {}
        for key in KIND_META:
            items = data.get(key) or []
            if isinstance(items, dict):
                items = list(items.values())
            seen = set()
            for it in items:
                text = _norm_item(it)
                if text and text not in seen:
                    seen.add(text)
                    out.setdefault(key, []).append(text[:60])
        return out
    except Exception:
        return {}


def build(name, llm=None) -> dict:
    """生成并存入人物档案。返回统计；失败时返回 error 字段，不抛异常。"""
    name = (name or "").strip()
    if not name:
        return {"error": "缺少人物名称"}
    scope = f"char:{name}"
    dossier = _llm_dossier(name, llm=llm)
    if not dossier:
        return {"name": name, "scope": scope, "added": 0, "error": "档案生成失败（LLM 无返回或格式错误）"}
    ts = datetime.now().isoformat(timespec="seconds")
    added = 0
    for kind, (mkey, category) in KIND_META.items():
        for content in dossier.get(kind, []):
            memory_controller.add_fact(
                scope, mkey, content, importance=0.6, confidence=0.7, source="character:llm"
            )
            topic.link_fact(scope, mkey, content, category, 0.7)
            added += 1
    return {
        "name": name,
        "scope": scope,
        "added": added,
        "kinds": {k: len(v) for k, v in dossier.items()},
    }


def search(name, limit=30) -> list:
    """按人物名读取已存档案。"""
    scope = f"char:{(name or '').strip()}"
    rows = _db.memory_rows(scope)
    rows.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    return rows[:limit]


def list_names() -> list[str]:
    """已收录的人物名列表。"""
    names = []
    for r in _db.memory_rows():
        sc = r.get("scope") or ""
        if sc.startswith("char:"):
            n = sc.split(":", 1)[1].strip()
            if n and n not in names:
                names.append(n)
    return sorted(names)


def match_scopes(text) -> list[str]:
    """查询里提到已知人物时，返回对应 char scope（供检索自动注入）。"""
    t = (text or "").lower()
    out = []
    for n in list_names():
        if n and n.lower() in t:
            out.append(f"char:{n}")
    return out
