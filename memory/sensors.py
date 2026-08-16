# -*- coding: utf-8 -*-
"""家庭传感器/设备层（v32，SandboxHome 式）：设备即状态，事件驱动。

- 设备：门铃/大门/各房间灯/冰箱门/窗户/空调/电视，落 kv；
- 事件：sensor_event() 追加事件 + 更新设备状态 + 写空间事件（进记忆）；
- 可听性：门铃全屋可闻，其余按房间传播（active_events）；
- 注入：block(room) 生成"此刻能感知到的设备/事件"；
- 演化：tick() 小概率让家自己动（门铃响/灯忘关）。
"""
import random
from datetime import datetime, timedelta

from plugins import _db, _shared

DEFAULT_DEVICES = {
    "门铃": {"room": "玄关", "state": "安静", "kind": "sensor"},
    "大门": {"room": "玄关", "state": "关", "kind": "door"},
    "客厅灯": {"room": "客厅", "state": "关", "kind": "light"},
    "卧室灯": {"room": "卧室", "state": "关", "kind": "light"},
    "工作室灯": {"room": "工作室", "state": "关", "kind": "light"},
    "厨房灯": {"room": "厨房", "state": "关", "kind": "light"},
    "冰箱门": {"room": "厨房", "state": "关", "kind": "door"},
    "客厅窗": {"room": "客厅", "state": "关", "kind": "window"},
    "空调": {"room": "客厅", "state": "关", "kind": "appliance"},
    "电视": {"room": "客厅", "state": "关", "kind": "appliance"},
}

# 声音传播：设备能被哪些房间感知（门铃全屋，其余同房/相邻）
HEAR_MAP = {
    "门铃": ("客厅", "玄关", "工作室", "厨房", "卧室"),
    "大门": ("玄关", "客厅"),
    "冰箱门": ("厨房",),
    "电视": ("客厅", "工作室"),
}

SILENT_STATES = ("关", "安静")


def _cfg(key, default):
    return _shared.core_cfg("sensors", key, default)
def _data() -> dict:
    d = _db.kv_get("memory", "sensors") or {}
    if not d.get("devices"):
        d = {"devices": dict(DEFAULT_DEVICES), "events": []}
        _db.kv_set("memory", "sensors", d)
    return d


def _save(d):
    _db.kv_set("memory", "sensors", d)


def devices() -> dict:
    return dict(_data().get("devices") or {})


def device_state(name) -> dict:
    return dict((_data().get("devices") or {}).get(name, {}))


def set_device(name, state, source="manual"):
    """改设备状态（引擎裁决后调用）。"""
    d = _data()
    dev = (d.get("devices") or {}).get(name)
    if not dev:
        return {"ok": False, "reason": f"没有设备{name}"}
    old = dev.get("state")
    dev["state"] = str(state)
    dev["changed_at"] = datetime.now().isoformat(timespec="seconds")
    _save(d)
    if old != state:
        sensor_event(name, "change", f"{name}：{old}→{state}", source)
    return {"ok": True, "name": name, "state": state}


def sensor_event(name, kind, detail, source="sensor"):
    """传感器事件：门铃响了/门开了/灯灭了。"""
    d = _data()
    now = datetime.now().isoformat(timespec="seconds")
    ev = {"ts": now, "device": name, "kind": str(kind), "detail": str(detail)[:80], "source": source}
    d.setdefault("events", []).append(ev)
    d["events"] = d["events"][-50:]
    dev = (d.get("devices") or {}).get(name)
    if dev:
        dev["last_event"] = str(kind)
        dev["last_event_ts"] = now
    _save(d)
    try:
        from memory import space as space_mod
        space_mod.emit("sensor", str(detail)[:80])
    except Exception as e:
        _stats_err(e)
        pass
    return ev


def recent_events(seconds=3600) -> list:
    now = datetime.now()
    out = []
    for e in (_data().get("events") or []):
        try:
            ts = datetime.fromisoformat(e["ts"])
        except Exception as e:
            _stats_err(e)
            continue
        if now - ts <= timedelta(seconds=seconds):
            out.append(e)  # type: ignore[misc]
    return out


def active_events(room, seconds=1800) -> list:
    """当前房间能感知到的最近事件（门铃全屋、同房/相邻）。"""
    out = []
    for e in recent_events(seconds):
        if room in HEAR_MAP.get(e.get("device"), []):
            out.append(e)
    return out


def block(room, seconds=1800) -> str:
    """注入：此刻家里能感知到的设备状态 + 最近事件。"""
    if not _cfg("enabled", True):
        return ""
    d = _data()
    parts = []
    for name, dev in (d.get("devices") or {}).items():
        if str(dev.get("room", "")) != room:
            continue
        st = dev.get("state")
        if st and st not in SILENT_STATES:
            parts.append(f"{name}：{st}")
    for e in active_events(room, seconds):
        parts.append(f"最近：{e.get('detail', '')}")
    if not parts:
        return ""
    return "【家里设备】" + "；".join(parts[:6])


def named_block(room, text, seconds=1800) -> str:
    """用户点名某设备时，返回该设备当前状态（跨房间可查，像智能家居查询）。"""
    if not _cfg("enabled", True):
        return ""
    t = str(text or "")
    devs = _data().get("devices") or {}
    out = []
    for name, dev in devs.items():
        if name in t:
            st = str(dev.get("state", ""))
            out.append(f"{name}：{st}")
    if not out:
        return ""
    return "【设备状态】" + "；".join(out[:4]) + "（内部参考：先如实回答；再推测用户为什么问——多半是想让你关/开灯或确认家里状态，自然接一句'要我关掉吗？'之类；回答要有用，别只报状态就冷场，也别突然跑题到躺平/省电）"


def tick(now=None):
    """日常演化：小概率门铃响/灯忘关，让家会自己动。"""
    try:
        import memory.stats as _st
        _st.bump("tick:sensors")
    except Exception as e:
        _stats_err(e)
    if not _cfg("tick", True):
        return []
    now = now or datetime.now()
    d = _data()
    out = []
    rng = random.Random()
    if 8 <= now.hour < 22 and rng.random() < 0.015:
        out.append(sensor_event("门铃", "ring", "门铃响了", "sim"))
    lights = [n for n, v in (d.get("devices") or {}).items() if v.get("kind") == "light"]
    if lights and rng.random() < 0.04:
        out.append(set_device(rng.choice(lights), "开", source="sim"))
    return out



def _stats_err(e):
    """裸 except 审计（v2.2）：错误计数 + 日志，供消融/排查。"""
    try:
        import memory.stats as _st
        _st.bump_err("sensors", e)
    except Exception:
        pass
