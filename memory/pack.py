"""Persona Pack（v2.2）：角色的名字/世界/活动/基线参数全部可插拔。

目录 personas/<pack>/：world.json（房间/边/门/地点/路程/容器/物品/队友周表）、
schedule.json（活动/周模板/状态链/作息）、behavior.json（情绪基线/懒系数/角色标签）、
persona.md（可选，缺省用仓库根 persona.md）、voice.md（可选）。
优先级：pack → config 覆盖 → 代码默认值。
"""

import json
import pathlib

from plugins import _shared

ROOT = pathlib.Path(__file__).resolve().parent.parent
_cache: dict = {}


def active() -> str:
    try:
        cfg = (_shared.CONFIG.get("memory", {}).get("core", {}) or {}).get("persona_pack", {}) or {}
        return str(cfg.get("pack", "yuno")).strip() or "yuno"
    except Exception:
        return "yuno"


def pack_dir(name=None) -> pathlib.Path:
    return ROOT / "personas" / (name or active())


def load(file_name, default=None):
    key = (active(), file_name)
    if key in _cache:
        return _cache[key]
    data = default
    p = pack_dir() / file_name
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            data = default
    _cache[key] = data
    return data


def world() -> dict:
    return load("world.json", {}) or {}


def schedule() -> dict:
    return load("schedule.json", {}) or {}


def behavior() -> dict:
    return load("behavior.json", {}) or {}


def persona_text() -> str:
    p = pack_dir() / "persona.md"
    if p.exists():
        return p.read_text(encoding="utf-8")
    return ""


def get(name, default=None):
    """pack → config(memory.core.pack_cfg.<name>) → default。"""
    b = behavior()
    if name in b:
        return b[name]
    try:
        cfg = (_shared.CONFIG.get("memory", {}).get("core", {}) or {}).get("persona_pack", {}) or {}
        if name in cfg:
            return cfg[name]
    except Exception:
        pass
    return default


def invalidate():
    """配置/切换 pack 后清缓存。"""
    _cache.clear()
