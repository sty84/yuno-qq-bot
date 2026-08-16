"""环境感知（v31）：日程 → 地点 → 周围人物 → 天气/光照/风 → 快照。

- 快照按「日期 + 槽位」持久化（同一段时间内稳定，不会上一句排练室下一句超市）；
- 人物分两类：可具名（队友/朋友/同事，配 cast 名单）与泛称（路人/观众——不重要）；
- 提及规则：用户之前提过某人（记忆里有名字）→ 可详细说；不认识 → 一笔带过；
- 天气来自 weather.py（国内 API 优先，模拟兜底）。
"""

import gzip
import hashlib
import json
import os
import random
import urllib.parse
import urllib.request
from datetime import datetime

from plugins import _db, _shared

# 日程活动 → (地点, 场景事件文本)
LOCATION_MAP = {
    "rehearsal": ("排练室", "正在排练新曲子"),
    "performance": ("演出现场", "正在台上演出"),
    "work": ("公司", "在上班"),
    "exercise": ("户外", "在运动"),
    "shopping": ("便利店/街道", "在外面买东西"),
    "out_entertain": ("户外", "在外面逛"),
    "friend": ("外面", "和朋友在一起"),
    "compose": ("家", "刚写完一段旋律"),
    "dj_practice": ("家", "在打碟练习"),
    "gaming": ("家", "在打游戏/看漫画"),
    "home_entertain": ("家", "在家闲着"),
    "home_rest": ("家", "在家休息"),
    "sleep": ("家", "在睡觉"),
    "study": ("家", "在学习"),
    "work_related": ("家", "在家忙"),
}

SCENE_EVENT_KINDS = {
    "performance": "performance",
    "rehearsal": "rehearsal",
    "compose": "compose",
    "shopping": "shopping",
    "friend": "friend",
    "exercise": "exercise",
    "dj_practice": "dj_practice",
}


def _cfg(key, default):
    return _shared.core_cfg("environment", key, default)
def _cast() -> list:
    cast = [str(x).strip() for x in (_cfg("cast", []) or []) if str(x).strip()]
    if not cast:
        try:
            from memory import pack
            cs = pack.world().get("cast_schedule")
            if isinstance(cs, dict):
                cast = [str(x) for x in cs.keys()]
        except Exception:
            pass
    return cast


def _people_for(activity, rng) -> list:
    """按活动生成周围人物：{name, kind, known}。known 由记忆决定，这里先置 False。"""
    cast = _cast()
    if activity == "performance":
        out = [{"name": c, "kind": "队友", "known": False, "position_in_room": "台上"}
               for c in cast[:2]]
        out += [{"name": "工作人员", "kind": "工作人员", "known": False, "position_in_room": "后台"},
                {"name": "观众", "kind": "观众", "known": False, "position_in_room": "台下"}]
        return out or [{"name": "队友", "kind": "队友", "known": False, "position_in_room": "台上"},
                       {"name": "工作人员", "kind": "工作人员", "known": False, "position_in_room": "后台"},
                       {"name": "观众", "kind": "观众", "known": False, "position_in_room": "台下"}]
    if activity == "rehearsal":
        spots = ["打碟台旁", "窗边", "门口"]
        return ([{"name": c, "kind": "队友", "known": False, "position_in_room": rng.choice(spots)}
                 for c in cast[:2]]
                or [{"name": "队友", "kind": "队友", "known": False, "position_in_room": "打碟台旁"}])
    if activity == "work":
        return [{"name": "同事", "kind": "同事", "known": False, "position_in_room": "工位"}]
    if activity == "friend":
        return [{"name": "朋友", "kind": "朋友", "known": False, "position_in_room": "旁边"}]
    if activity in ("exercise", "shopping", "out_entertain"):
        people = [{"name": "路人", "kind": "路人", "known": False, "position_in_room": ""}]
        if cast and rng.random() < 0.15:
            people.append({"name": rng.choice(cast), "kind": "朋友", "known": False, "position_in_room": "旁边"})
        if activity == "shopping":
            people.append({"name": "便利店店员", "kind": "店员", "known": False, "position_in_room": "收银台"})
        return people
    # 在家：默认独自一人，偶尔队友串门
    if cast and rng.random() < 0.12:
        return [{"name": rng.choice(cast), "kind": "队友", "known": False, "position_in_room": "沙发"}]
    return [{"name": "独自一人", "kind": "独自", "known": False, "position_in_room": ""}]


def _known(scope, name) -> bool:
    """用户是否认识某人：在用户记忆里找过这个名字。"""
    if not scope or not name or name in ("路人", "观众", "工作人员", "独自一人", "队友", "同事", "朋友"):
        return False
    try:
        for r in _db.memory_rows(scope):  # type: ignore[attr-defined]
            if name in str(r.get("fact", "")):
                return True
    except Exception as e:
        _stats_err(e)
        pass
    return False


def _snapshot_key(now) -> str:
    from memory import schedule as schedule_mod
    return f"{now.date().isoformat()}_{schedule_mod.slot_index(now.hour)}_{schedule_mod.profile_id()}"


def snapshot(scope="", now=None, force=False) -> dict:
    """当前环境快照（按日期+槽位缓存；force=True 强制重建，路上状态不缓存）。"""
    if not _cfg("enabled", True):
        return {}
    now = now or datetime.now()
    key = _snapshot_key(now)
    ttl_s = float(_cfg("ttl_min", 60)) * 60
    data = _db.kv_get("memory", "env_snapshot") or {}  # type: ignore[attr-defined]
    old_snap = data.get("snap") or {}
    if not force and data.get("key") == key and data.get("ts") and not old_snap.get("transit"):
        try:
            age = (datetime.now() - datetime.fromisoformat(data["ts"])).total_seconds()
            if age < ttl_s:
                return old_snap
        except Exception as e:
            _stats_err(e)
            pass
    transit = False
    try:
        from memory import space as space_mod
        pos = space_mod.position(now)
    except Exception as e:
        _stats_err(e)
        pos = {}
    if pos.get("state") == "在途中":
        transit = True
        activity = "transit"
        location = "路上"
        scene_text = f"在去{pos.get('to', '某处')}的路上（{pos.get('mode', '')}）"
        people = []
    else:
        try:
            from memory import schedule as schedule_mod
            cur = schedule_mod.current_activity(now)
            activity = cur.get("activity") if cur else "home_rest"  # type: ignore[assignment]
        except Exception as e:
            _stats_err(e)
            activity = "home_rest"
        location, scene_text = LOCATION_MAP.get(activity, ("家", "在家"))
        if pos.get("state") == "在场" and pos.get("location") not in ("", "家"):
            location = pos["location"]
            scene_text = f"在{location}"
    # 人物流动（v31.2）：种子加入 15 分钟时间桶——同一时间段内稳定，跨桶人物会变化
    bucket = int(now.timestamp()) // (15 * 60)
    rng = random.Random(
        int(hashlib.sha256(f"env:{key}:{location}:{bucket}".encode()).hexdigest()[:12], 16)
    )
    people = _people_for(activity, rng)
    for p in people:
        if p["kind"] in ("队友", "朋友", "同事"):
            p["known"] = _known(scope, p["name"])
    try:
        from memory import weather as weather_mod
        wtext = weather_mod.describe(now)
    except Exception as e:
        _stats_err(e)
        wtext = ""
    snap = {
        "key": key,
        "activity": activity,
        "location": location,
        "scene_text": scene_text,
        "people": people,
        "weather": wtext,
        "transit": transit,
        "to": pos.get("to", "") if transit else "",
    }
    # 人物流动事件：和旧快照比，谁来了/谁走了
    try:
        old_names = {p["name"] for p in old_snap.get("people") or [] if p.get("known") is not None}
        new_names = {p["name"] for p in people if p["kind"] in ("队友", "朋友", "同事", "店员")}
        from memory import space as space_mod
        for n in new_names - old_names:
            space_mod.emit("person_in", f"{n}来了")
        for n in old_names - new_names:
            space_mod.emit("person_out", f"{n}走了")
    except Exception as e:
        _stats_err(e)
        pass
    if not transit:
        _db.kv_set(  # type: ignore[attr-defined]
            "memory", "env_snapshot",
            {"key": key, "ts": datetime.now().isoformat(timespec="seconds"), "snap": snap},
        )
    return snap


def notable_event(now=None) -> tuple:
    """当前日程阶段的可分享事件：(kind, text)；无则 ("", "")。"""
    snap = snapshot("", now)
    kind = SCENE_EVENT_KINDS.get(snap.get("activity", ""), "")
    return (kind, snap.get("scene_text", "")) if kind else ("", "")


def _people_text(people) -> str:
    if not people:
        return ""
    if all(p["kind"] == "独自" for p in people):
        return "周围：独自一人"
    names = []
    for p in people:
        if p["kind"] == "路人":
            continue
        names.append(p["name"] if p["kind"] in ("队友", "朋友", "同事") else p["kind"])
    return "周围：" + "、".join(names) if names else "周围：只有路人"


def block(scope="", text="", now=None, force=None) -> str:
    """环境注入块（内部参考）。"""
    try:
        import memory.stats as _st
        _st.bump("tick:environment")
    except Exception as e:
        _stats_err(e)
    if force is None:
        force = any(w in (text or "") for w in ("现在", "刚刚", "刚才", "谁在", "在哪", "在哪呢", "这会"))
    snap = snapshot(scope, now, force=force)
    if not snap:
        return ""
    parts = [f"【周围环境】{snap['location']}（{snap['scene_text']}）"]
    if snap.get("transit"):
        parts.append("（在路上，信号不太好，回消息慢）")
    pt = _people_text(snap["people"])
    if pt:
        parts.append(pt)
    if snap.get("weather"):
        parts.append(f"天气：{snap['weather']}")
    rules = []
    for p in snap["people"]:
        if p["kind"] in ("路人", "观众", "工作人员"):
            continue
        if p["kind"] == "店员":
            continue
        if p.get("known"):
            rules.append(f"用户认识{p['name']}，可以详细说")
        else:
            rules.append(f"用户不认识{p['name']}，提一句带过就好")
    if rules:
        parts.append("【提及规则】" + "；".join(rules))
    parts.append("内部参考：别主动报环境，被问起或相关时自然带一句，不要生硬播报")
    return "；".join(parts)


# ===== 天气提供方（v31.3 合并自 memory/weather.py）：国内 API 直连，失败回退种子模拟 =====
QWEATHER_URL = "https://devapi.qweather.com/v7/weather/now"
AMAP_URL = "https://restapi.amap.com/v3/weather/weatherInfo"


def _weather_cfg(key, default):
    w = (_shared.CONFIG.get("memory", {}).get("core", {}) or {}).get("weather", {}) or {}
    return w.get(key, default)


def _qweather_url() -> str:
    host = str(_weather_cfg("base_url", "")).strip().rstrip("/")
    if not host:
        host = "https://devapi.qweather.com"
    if not host.startswith(("http://", "https://")):
        host = "https://" + host
    return f"{host}/v7/weather/now"


def weather_enabled() -> bool:
    return bool(_weather_cfg("enabled", True))


def _api_key() -> str:
    env = str(_weather_cfg("api_key_env", "WEATHER_API_KEY"))
    return os.getenv(env, "") or ""


def _weather_get(url) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "yuno-bot/2.0"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        raw = resp.read()
        if raw[:2] == b"\x1f\x8b":  # gzip-compressed response
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8"))


def _fetch_api() -> dict | None:
    """调真实天气 API；任何失败返回 None（调用方回退模拟）。"""
    provider = str(_weather_cfg("provider", "qweather"))
    key = _api_key()
    if not key:
        return None
    try:
        if provider == "amap":
            city = str(_weather_cfg("city", "北京"))
            data = _weather_get(f"{AMAP_URL}?city={urllib.parse.quote(city)}&key={key}&extensions=base")
            lives = (data.get("lives") or [{}])[0]
            return {
                "source": "amap",
                "temperature": lives.get("temperature"),
                "text": lives.get("weather"),
                "wind": f"{lives.get('winddirection', '')}{lives.get('windpower', '')}级",
            }
        loc = str(_weather_cfg("location_id", "101010100"))
        data = _weather_get(f"{_qweather_url()}?location={loc}&key={key}")
        now = data.get("now") or {}
        return {
            "source": "qweather",
            "temperature": now.get("temp"),
            "text": now.get("text"),
            "wind": f"{now.get('windDir', '')}{now.get('windScale', '')}级",
        }
    except Exception as e:
        _stats_err(e)
        return None


def _simulate(now=None) -> dict:
    """按季节 + 当日种子的模拟天气（离线兜底）。"""
    now = now or datetime.now()
    rng = random.Random(
        int(hashlib.sha256(f"weather:{now.date()}:{_weather_cfg('city', '')}".encode()).hexdigest()[:12], 16)
    )
    month = now.month
    base = {12: 5, 1: 3, 2: 5, 3: 13, 4: 19, 5: 24, 6: 28, 7: 31, 8: 30, 9: 25, 10: 18, 11: 10}.get(month, 15)
    temp = base + rng.randint(-4, 4)
    text = rng.choices(["晴", "多云", "阴", "小雨"], weights=[4, 3, 2, 1])[0]
    wind = rng.choices(["无风", "微风", "3级风"], weights=[2, 3, 1])[0]
    return {"source": "sim", "temperature": temp, "text": text, "wind": wind}


def _light(now, weather_text) -> str:
    """光照描述：由时段 + 天气推断（天气 API 不直接给 lux）。"""
    hour = now.hour
    if hour < 6 or hour >= 19:
        return "夜晚，光线暗"
    if weather_text in ("晴", "晴间多云"):
        return "阳光正亮"
    if weather_text == "多云":
        return "光线柔和"
    return "光线偏暗"


def fetch(now=None, force=False) -> dict:
    """获取当前天气（API 优先，模拟兜底），带 kv 缓存。"""
    if not weather_enabled():
        return {}
    now = now or datetime.now()
    ttl = float(_weather_cfg("ttl_s", 1800))
    data = _db.kv_get("memory", "weather_cache") or {}  # type: ignore[attr-defined]
    if not force and data.get("ts") and (datetime.now() - datetime.fromisoformat(data["ts"])).total_seconds() < ttl:
        return data.get("w") or {}
    prev_src = (data.get("w") or {}).get("source")
    w = _fetch_api()
    if not w:
        w = _simulate(now)
    if prev_src and prev_src != w.get("source"):
        # 真实/模拟天气切换 → 写空间事件，避免"昨天32°C今天25°C"无解释穿帮
        try:
            from memory import space as space_mod
            space_mod.emit("weather_change", f"天气变了：{w.get('text', '')}")
        except Exception as e:
            _stats_err(e)
            pass
    w["light"] = _light(now, str(w.get("text", "")))
    w["ts"] = datetime.now().isoformat(timespec="seconds")
    _db.kv_set("memory", "weather_cache", {"ts": w["ts"], "w": w})  # type: ignore[attr-defined]
    try:  # keep env snapshot weather in sync after refresh
        snap_data = _db.kv_get("memory", "env_snapshot") or {}  # type: ignore[attr-defined]
        if snap_data.get("snap"):
            snap_data["snap"]["weather"] = describe(now)
            _db.kv_set("memory", "env_snapshot", snap_data)  # type: ignore[attr-defined]
    except Exception as e:
        _stats_err(e)
        pass
    return w


def describe(now=None) -> str:
    """一句话天气（供环境感知 prompt 注入）。"""
    w = fetch(now)
    if not w:
        return ""
    t = w.get("temperature")
    temp = f"{t}°C" if t is not None else "温度未知"
    return f"{temp}，{w.get('text', '')}，{w.get('wind', '')}，{w.get('light', '')}"



def _stats_err(e):
    """裸 except 审计（v2.2）：错误计数 + 日志，供消融/排查。"""
    try:
        import memory.stats as _st
        _st.bump_err("environment", e)
    except Exception:
        pass
