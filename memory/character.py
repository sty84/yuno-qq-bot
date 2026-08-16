"""人物设定检索入库：输入人物名称，用 LLM 知识生成设定/经历档案，存入统一记忆。

存储格式与用户/AI 记忆完全一致（memories 表，scope='char:<名>'）：向量化 + 事件图 +
议题化 + 可信度（AI 知识默认 0.7，用户可随时纠正）。查询提到该人物时自动注入检索。

同时支持 md 档案双写（v23）：/设定 人物 后自动生成 docs/characters/<名>.md（新角色卡
模板结构，可人工审阅/编辑），编辑后运行 tools.py character-sync 一键同步回记忆库
（md 为权威来源：清空旧档案后重建，不堆叠）。
"""

from memory._llmutil import parse_json_object
import pathlib
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

# md 档案：段落标题 → 档案键（与 persona.md 模板风格一致，方便人工阅读/编辑）
CHAR_MD_SECTIONS = [
    ("basic", "身份"),
    ("personality", "性格"),
    ("style", "说话风格"),
    ("experience", "经历"),
    ("relations", "人物关系"),
    ("quotes", "口头禅"),
]
_MD_TO_KEY = {title: key for key, title in CHAR_MD_SECTIONS}
_MKEY_TO_DOSSIER = {
    "identity": "basic",
    "personality": "personality",
    "style": "style",
    "experience": "experience",
    "relations": "relations",
    "catchphrase": "quotes",
}


def _char_dir() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent / "docs" / "characters"


def md_path(name, out_dir=None) -> pathlib.Path:
    """人物档案 md 路径（默认 docs/characters/<名>.md；out_dir 供测试/自定义）。"""
    base = pathlib.Path(out_dir) if out_dir else _char_dir()
    base.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|\r\n]', "_", (name or "").strip()) or "未命名"
    return base / f"{safe}.md"


def _dossier_from_memory(name, limit=200) -> dict:
    """从记忆库读回档案（按 key 映射回 md 段落）。"""
    out = {k: [] for k, _t in CHAR_MD_SECTIONS}  # type: ignore[var-annotated]
    rows = _db.memory_rows(f"char:{name}")[:limit]  # type: ignore[attr-defined]
    for r in rows:
        dk = _MKEY_TO_DOSSIER.get(r.get("key") or "")
        if dk:
            out[dk].append(str(r["fact"]))
    return out


def render_markdown(name, dossier=None) -> str:
    """把角色档案渲染成可读 md（新角色卡模板结构；每行一条记忆，可编辑后同步回）。"""
    dossier = dossier if dossier is not None else _dossier_from_memory(name)
    lines = [
        f"# {name}",
        "",
        f"> 角色档案 · 来源 LLM 知识 · 可信度 70% · 生成于 {datetime.now():%Y-%m-%d %H:%M}",
        f"> 编辑本文件后运行：tools.py character-sync {name}",
        "> 每行一条记忆：删除该行 = 移除对应记忆；新增一行 = 收录一条新记忆。",
        "",
    ]
    for key, title in CHAR_MD_SECTIONS:
        items = dossier.get(key) or []
        lines.append(f"## {title}")
        if items:
            lines.extend(f"- {it}" for it in items)
        else:
            lines.append("- （暂无）")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_markdown(name, dossier=None, out_dir=None) -> pathlib.Path:
    """写入/更新人物档案 md，返回文件路径。"""
    p = md_path(name, out_dir=out_dir)
    p.write_text(render_markdown(name, dossier), encoding="utf-8")
    return p


def parse_markdown(text) -> dict:
    """解析 md 档案回 dossier：按段落标题分组，忽略引用块与（暂无）。"""
    out = {k: [] for k, _t in CHAR_MD_SECTIONS}  # type: ignore[var-annotated]
    cur = None
    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith("#"):
            m = re.match(r"^#{1,6}\s*(.+)$", s)
            cur = _MD_TO_KEY.get(m.group(1).strip()) if m else None
            continue
        if not cur or s.startswith(">"):
            continue
        item = s[1:].strip() if s.startswith("-") else s
        if item and item != "（暂无）":
            out[cur].append(item[:60])
    return out


def _clear_scope(scope):
    """清空某人物 scope 的全部派生数据（记忆/元数据/属性/事件/议题），供重建。"""
    _db.memory_clear(scope)
    for m in _db.meta_rows(scope):
        _db.meta_delete(scope, m["key"], m["fact"])
    _db.attr_delete(scope)
    for ev in _db.event_rows(scope, limit=10000):
        _db.event_delete(ev["id"])
    _db.topic_clear(scope)


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
            resp = _shared.deepseek_chat(
                messages=[
                    {"role": "system", "content": CHAR_PROMPT.format(name=name)},
                    {"role": "user", "content": f"角色：{name}"},
                ],
                max_tokens=900,
                temperature=0.3,
                module="character",
            )
            raw = resp.choices[0].message.content or ""
        raw = re.sub(
            r"^```(?:json)?\s*|\s*```$", "", str(raw).strip(), flags=re.S
        )
        data = parse_json_object(raw)
        if data is None:
            return {}
        out = {}  # type: ignore[var-annotated]
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
    except Exception as e:
        _stats_err(e)
        return {}


def build(name, llm=None) -> dict:
    """生成并存入人物档案（重新生成 = 刷新：先清旧档案再重建，不堆叠）。
    返回统计；失败时返回 error 字段，不抛异常。"""
    name = (name or "").strip()
    if not name:
        return {"error": "缺少人物名称"}
    scope = f"char:{name}"
    dossier = _llm_dossier(name, llm=llm)
    if not dossier:
        return {"name": name, "scope": scope, "added": 0, "error": "档案生成失败（LLM 无返回或格式错误）"}
    _clear_scope(scope)
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


def sync_from_markdown(name=None, path=None) -> dict:
    """把编辑后的 md 档案同步回记忆库（md 为权威来源：清空旧档案后重建）。
    name 与 path 二选一：path 优先；只给 name 时读取 docs/characters/<名>.md。"""
    p = pathlib.Path(path) if path else md_path(name or "")
    if not p.exists():
        return {"error": f"档案文件不存在：{p}"}
    dossier = parse_markdown(p.read_text(encoding="utf-8"))
    nm = name or p.stem
    scope = f"char:{nm}"
    _clear_scope(scope)
    added = 0
    for kind, (mkey, category) in KIND_META.items():
        for content in dossier.get(kind, []):
            memory_controller.add_fact(
                scope, mkey, content, importance=0.6, confidence=0.7, source="character:md"
            )
            topic.link_fact(scope, mkey, content, category, 0.7)
            added += 1
    return {
        "name": nm,
        "scope": scope,
        "added": added,
        "path": str(p),
        "kinds": {k: len(v) for k, v in dossier.items()},
    }


def search(name, limit=30) -> list:
    """按人物名读取已存档案。"""
    scope = f"char:{(name or '').strip()}"
    rows = _db.memory_rows(scope)  # type: ignore[attr-defined]
    rows.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    return rows[:limit]


def list_names() -> list[str]:
    """已收录的人物名列表。"""
    names = []
    for r in _db.memory_rows():  # type: ignore[attr-defined]
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



def _stats_err(e):
    """裸 except 审计（v2.2）：错误计数 + 日志，供消融/排查。"""
    try:
        import memory.stats as _st
        _st.bump_err("character", e)
    except Exception:
        pass
