"""多主体记忆（v2.2）：注册主体（队友/NPC）→ 独立 scope 的记忆写入 / 检索 / 注入。

- registered(): 从 config memory.core.agents.cast 读主体名单（空则回退 environment.cast）；
- detect(text): 文本里出现的主体名（支持短名/二元组匹配）；
- scope_of(name): npc:<name> 独立命名空间。
"""

from plugins import _shared


def _cfg(key, default):
    a = (_shared.CONFIG.get("memory", {}).get("core", {}) or {}).get("agents", {}) or {}
    return a.get(key, default)


def enabled() -> bool:
    return bool(_cfg("enabled", False))


def registered() -> list:
    cast = [str(x).strip() for x in (_cfg("cast", []) or []) if str(x).strip()]
    if not cast:
        try:
            env_cfg = (_shared.CONFIG.get("memory", {}).get("core", {}) or {}).get("environment", {}) or {}
            cast = [str(x).strip() for x in (env_cfg.get("cast") or []) if str(x).strip()]
        except Exception:
            cast = []
    return cast


def detect(text) -> list:
    """文本里出现的已注册主体名（全名或名字片段）。"""
    t = str(text or "")
    out = []
    for name in registered():
        if name and (name in t or any(name[i:i + 2] in t for i in range(max(0, len(name) - 1)))):
            out.append(name)
    return out


def scope_of(name) -> str:
    return f"npc:{str(name).strip()}"


def top_k() -> int:
    try:
        return max(1, int(_cfg("npc_top_k", 2)))
    except (TypeError, ValueError):
        return 2


def confidence_cap() -> float:
    try:
        return min(1.0, max(0.0, float(_cfg("npc_confidence_cap", 0.8))))
    except (TypeError, ValueError):
        return 0.8
