"""生活环境层（v31）：家的布局 + 容器物品（懒展开）+ 动态距离感。

- 布局：房间 → 家具（部分家具是容器），静态配置，kv 持久化；
- 物品：存在 kv（SQLite 落盘），"装在箱子里"——上下文只注入房间和目光所及，
  箱子里有什么要查询才展开（lookup）；
- 操作：拿/用（数量-1）、用户送（新增或累加）、移动（换容器）、查（find/lookup）；
- 距离感：不是固定时间——基准分钟 × 交通方式系数 × 天气修正 × 由乃的懒系数 ×
  情绪唤醒修正 × 当日种子抖动（同一天答案稳定，不同天/天气会变）；
- 联动：物品事件 → ai:experience 记忆 + 分享欲；梦境素材混入物品名。
"""

import hashlib
import json
import random
import re
from datetime import date, datetime, timedelta

from plugins import _db, _shared

HOME_LAYOUT_DEFAULT = {
    "客厅": {"furniture": ["沙发", "茶几", "电视柜"]},
    "工作室": {"furniture": ["打碟台", "耳机架", "储物箱"]},
    "卧室": {"furniture": ["床", "抱枕", "床头柜"]},
    "厨房": {"furniture": ["冰箱", "零食柜", "料理台"]},
}

# 场所 → 各交通方式基准分钟 + 默认方式（基准是"健康成人"时间，最后会乘由乃系数）
PLACES_DEFAULT = {
    "便利店": {"walk_min": 10, "bike_min": 5, "transit_min": 15, "drive_min": 6, "default_mode": "walk"},
    "公园": {"walk_min": 20, "bike_min": 9, "transit_min": 18, "drive_min": 12, "default_mode": "walk"},
    "外面": {"walk_min": 15, "bike_min": 8, "transit_min": 20, "drive_min": 10, "default_mode": "walk"},
    "排练室": {"walk_min": 75, "bike_min": 30, "transit_min": 30, "drive_min": 22, "default_mode": "transit"},
    "录音室": {"walk_min": 55, "bike_min": 22, "transit_min": 26, "drive_min": 16, "default_mode": "transit"},
    "演出场地": {"walk_min": 100, "bike_min": 42, "transit_min": 40, "drive_min": 26, "default_mode": "transit"},
    "队友家": {"walk_min": 60, "bike_min": 25, "transit_min": 35, "drive_min": 18, "default_mode": "bike"},
    "学校": {"walk_min": 60, "bike_min": 24, "transit_min": 45, "drive_min": 20, "default_mode": "transit"},
}

# 天气文本 → (步行/骑车, 公交, 开车) 时间修正
_WEATHER_MULT = {
    "雨": (1.45, 1.15, 1.2),
    "雪": (1.5, 1.2, 1.25),
    "雷": (1.5, 1.2, 1.25),
    "风": (1.2, 1.0, 1.05),
}

_ACTIVITY_ROOM = {
    "sleep": "卧室",
    "compose": "工作室",
    "dj_practice": "工作室",
    "study": "工作室",
    "gaming": "客厅",
    "home_entertain": "客厅",
    "home_rest": "客厅",
}

DEFAULT_ITEMS = [
    {"name": "白巧克力", "category": "零食", "qty": 3, "room": "厨房", "container": "冰箱", "position": "冷藏层", "difficulty": "浅", "status": "有"},
    {"name": "能量饮料", "category": "饮品", "qty": 2, "room": "厨房", "container": "冰箱", "position": "冷藏层", "difficulty": "浅", "status": "有"},
    {"name": "牛奶", "category": "饮品", "qty": 1, "room": "厨房", "container": "冰箱", "position": "冷藏层", "difficulty": "浅", "status": "有"},
    {"name": "TCG卡包", "category": "娱乐", "qty": 3, "room": "厨房", "container": "零食柜", "position": "中层", "difficulty": "浅", "status": "有"},
    {"name": "薯片", "category": "零食", "qty": 1, "room": "厨房", "container": "零食柜", "position": "顶层", "difficulty": "浅", "status": "有"},
    {"name": "旧CD", "category": "音乐", "qty": 5, "room": "工作室", "container": "储物箱", "position": "箱子深处", "difficulty": "深", "status": "有"},
    {"name": "备用耳机", "category": "设备", "qty": 1, "room": "工作室", "container": "储物箱", "position": "表面", "difficulty": "浅", "status": "有"},
    {"name": "眼药水", "category": "日用品", "qty": 1, "room": "卧室", "container": "床头柜", "position": "抽屉里", "difficulty": "浅", "status": "有"},
]

# 物品显示单位（仅展示用；库存里没有 unit 字段时显示为空）
_ITEM_UNIT = {
    "白巧克力": "块",
    "能量饮料": "罐",
    "牛奶": "盒",
    "TCG卡包": "包",
    "薯片": "袋",
    "旧CD": "张",
    "备用耳机": "副",
    "眼药水": "瓶",
}

# 跨房间查看（v31.4）：先回"我去看看"，后台延迟一条自然汇报
INSPECT_GO = "【待办】{container}在{room}（你现在在{room_now}）。先回一句“我去看看”就行，不要现在回答里面的内容；系统会在你走过去后自动汇报，这句是内部参考，别对用户提“系统”。"
INSPECT_GOING = "【待办】你已经在去{room}看{container}的路上了，回复里带一句“马上到/这就看”就行，别重复答应。"
INSPECT_PROMPT = "你是千石由乃（节能系宅女、乐队DJ）。你刚才答应用户“我去看看”，现在你走到了{room}，打开{container}，看到：{items}。用她的口吻给用户发一条简短消息（15~35字），像真人聊天自然地汇报里面有什么；名称和数量照实说，不要加“盒/罐/袋/碎”等原文没有的单位，不要提“系统/数据/汇报”。"

_BUY_RE = re.compile(r"(?:顺路买了|刚买了|买了点|买了)([\u4e00-\u9fffA-Za-z0-9]{1,8}?)(?:，|。|！|！|$)")
_EMPTY_RE = re.compile(r"([\u4e00-\u9fffA-Za-z0-9]{1,8}?)(?:吃完了|喝完了|用完了|吃光了|喝光了|用光了)")
_ADOPT_RE = re.compile(r"(?:养了|领养了)(?:一只|一只小|个)?(猫|狗|仓鼠|兔子|金鱼|鹦鹉)")


def _cfg(key, default):
    lv = (_shared.CONFIG.get("memory", {}).get("core", {}) or {}).get("living", {}) or {}
    return lv.get(key, default)


def home_layout() -> dict:
    return _cfg("layout", HOME_LAYOUT_DEFAULT) or HOME_LAYOUT_DEFAULT


def home_location() -> str:
    return str(_cfg("home_location", "") or "").strip()


def places() -> dict:
    return _cfg("places", PLACES_DEFAULT) or PLACES_DEFAULT


# ===== 物品存储（kv，SQLite 落盘）=====
def _load() -> dict:
    data = _db.kv_get("memory", "living_items") or {}
    if data and data.get("items") is not None:
        return data
    data = {"items": [dict(i) for i in DEFAULT_ITEMS], "moves": []}
    _db.kv_set("memory", "living_items", data)
    return data


def _save(data):
    _db.kv_set("memory", "living_items", data)


def _move_log(data, action, name, n, detail=""):
    moves = list(data.get("moves") or [])
    moves.append(
        {"ts": datetime.now().isoformat(timespec="seconds"),
         "action": action, "name": name, "n": n, "detail": detail[:80]}
    )
    data["moves"] = moves[-100:]


def find(name) -> list:
    """按名字找物品（模糊）。"""
    t = str(name or "").strip()
    data = _load()
    if not t:
        return []
    return [i for i in data["items"] if t in str(i.get("name", ""))]


def lookup(container) -> list:
    """展开容器：返回该容器里的所有物品。"""
    t = str(container or "").strip()
    data = _load()
    if not t:
        return []
    return [i for i in data["items"] if str(i.get("container", "")) == t]


def all_items() -> list:
    return _load()["items"]


def take(name, n=1, scope=""):
    """AI 拿/吃/用：数量-1；到 0 标记'没有了'；拿最后一件写记忆 + 分享欲。"""
    data = _load()
    items = data["items"]
    hit = next((i for i in items if str(i.get("name", "")) == name), None)
    if not hit:
        return {"ok": False, "reason": f"家里没有{name}"}
    n = max(1, int(n))
    qty = int(hit.get("qty", 0))
    if qty <= 0:
        return {"ok": False, "reason": f"{name}已经没有了"}
    qty = max(0, qty - n)
    hit["qty"] = qty
    hit["status"] = "没有了" if qty <= 0 else "有"
    was_last = qty <= 0
    _move_log(data, "take", name, n, "AI 拿走了" + ("最后一个" if was_last else f"{n}个"))
    _save(data)
    try:
        if was_last:
            _db.memory_add(
                "ai", "experience", f"我把最后一个{name}用掉了",
                datetime.now().isoformat(timespec="seconds"), None,
                confidence=0.6, source="living", mclass="short", audience="public", speaker="ai",
            )
            from memory import policy as policy_mod
            policy_mod.touch("ai", "experience", f"我把最后一个{name}用掉了", importance=0.5)
            from memory import sharing as sharing_mod
            sharing_mod.add_delta(0.15, f"{name}吃完了，想补货")
    except Exception:
        pass
    return {"ok": True, "name": name, "qty": qty, "was_last": was_last}


def consume(name, n=1, scope=""):
    """消耗（用户喝掉/吃掉了）：数量-1，不移动位置。"""
    data = _load()
    hit = next((i for i in data["items"] if str(i.get("name", "")) == name), None)
    if not hit:
        return {"ok": False, "reason": f"没有{name}"}
    qty = max(0, int(hit.get("qty", 1)) - int(n))
    hit["qty"] = qty
    hit["status"] = "没有了" if qty <= 0 else "有"
    _move_log(data, "consume", name, int(n), "被消耗（用掉了）")
    _save(data)
    return {"ok": True, "name": name, "qty": qty}


def give(name, n=1, scope="", source="user_gift"):
    """收物品：用户送（默认）/ AI 自己买（self_buy）/ 领养（self_adopt）。写记忆 + 分享欲。"""
    name = str(name or "").strip()
    if not name:
        return {"ok": False, "reason": "没收到东西名"}
    n = max(1, int(n))
    data = _load()
    hit = next((i for i in data["items"] if str(i.get("name", "")) == name), None)
    if hit:
        cap = int(_cfg("container_capacity", 10))
        if int(hit.get("qty", 0)) + n > cap:
            return {"ok": False, "reason": f"{hit.get('container', '容器')}满了，放不下"}
        hit["qty"] = int(hit.get("qty", 0)) + n
        hit["status"] = "有"
        hit["source"] = source
    else:
        hit = {"name": name, "category": "礼物", "qty": n, "room": "客厅",
               "container": "茶几", "position": "桌面上", "difficulty": "浅",
               "status": "有", "source": source}
        room_count = sum(1 for i in data["items"] if i.get("container") == "茶几")
        if room_count + 1 > int(_cfg("container_capacity", 10)):
            hit["container"] = "储物箱"
        data["items"].append(hit)
    _move_log(data, "give", name, n, f"用户送来了{n}个{name}")
    _save(data)
    try:
        audience = "private" if source == "user_gift" else "public"
        _db.memory_add(
            "ai", "experience", f"用户送了我{name}",
            datetime.now().isoformat(timespec="seconds"), None,
            confidence=0.7, source="living", mclass="short", audience=audience, speaker="ai",
        )
        from memory import policy as policy_mod
        policy_mod.touch("ai", "experience", f"用户送了我{name}", importance=0.6)
        from memory import sharing as sharing_mod
        sharing_mod.add_delta(0.3, "收到用户的礼物")
    except Exception:
        pass
    return {"ok": True, "name": name, "qty": hit["qty"]}


_WORLD_PROMPT = (
    "你是千石由乃家里的世界状态解析器。根据用户这句话，判断世界发生了什么变化，"
    "只输出 JSON：{\"ops\":[{\"type\":\"take|give|consume|move|device\",\"item\":\"物品名\",\"qty\":1,"
    "\"room\":\"房间\",\"container\":\"容器\",\"device\":\"设备名\",\"state\":\"状态\"}]}。"
    "type 含义：take=由乃拿取；give=用户送东西给由乃；consume=物品被消耗（用户喝/吃了）；"
    "move=物品移动；device=设备状态变化。没有变化就输出 {\"ops\":[]}。不要输出任何其他文字。"
)
_WORLD_KEYWORDS = ("拿", "喝", "吃", "买", "放", "扔", "开", "关", "冰箱", "床头柜", "零食柜", "储物箱",
                   "灯", "门铃", "快递", "外卖", "送", "薯片", "巧克力", "饮料", "牛奶")
_WORLD_LAST = {}


def _world_hint(text) -> bool:
    t = str(text or "")
    return any(k in t for k in _WORLD_KEYWORDS)


def propose_world_delta(scope, text, now=None):
    """LLM 提议世界变化 → 引擎校验并应用（SandboxHome 式 world_delta）。"""
    if not _cfg("world_delta", True) or not _world_hint(text):
        return {"changed": 0, "reason": "no_hint"}
    now = now or datetime.now()
    last = _WORLD_LAST.get(scope or "")
    if last and (now - last).total_seconds() < 60:
        return {"changed": 0, "reason": "throttled"}
    _WORLD_LAST[scope or ""] = now
    item_hint = "；".join(f"{i['name']}×{i['qty']}在{i.get('container', '')}" for i in all_items()) or "无"
    prompt = _WORLD_PROMPT + "\n已知物品：" + item_hint + "\n用户说：" + str(text)[:200]
    try:
        from plugins import _shared
        reply = _shared.ask_deepseek(prompt, temperature=0)
    except Exception:
        return {"changed": 0, "reason": "llm_fail"}
    try:
        m = re.search(r"\{.*\}", reply, re.S)
        delta = json.loads(m.group(0))
    except Exception:
        return {"changed": 0, "reason": "parse_fail"}
    return _apply_delta(scope, delta)


def _apply_delta(scope, delta):
    changed = 0
    for op in (delta.get("ops") or []):
        t = str(op.get("type", ""))
        try:
            if t == "take":
                r = take(str(op.get("item", "")), int(op.get("qty", 1) or 1), scope=scope)
            elif t == "give":
                r = give(str(op.get("item", "")), int(op.get("qty", 1) or 1), scope=scope, source="user_gift")
            elif t == "consume":
                r = consume(str(op.get("item", "")), int(op.get("qty", 1) or 1))
            elif t == "move":
                r = move_item(str(op.get("item", "")), str(op.get("room", "")), str(op.get("container", "")))
            elif t == "device":
                from memory import sensors as sensors_mod
                r = sensors_mod.set_device(str(op.get("device", "")), str(op.get("state", "")))
            else:
                continue
            if r.get("ok"):
                changed += 1
        except Exception:
            continue
    return {"changed": changed}


def sync_from_text(text) -> dict:
    """把 AI 回复里随口说的生活事实回流状态层（买了/用完了/养了宠物）。
    让"她嘴里说的世界"和"系统里的世界"保持一致（v31.2）。"""
    if not str(text or "").strip():
        return {"changed": 0}
    t = str(text)
    changed = 0
    for m in _BUY_RE.finditer(t):
        name = str(m.group(1)).strip()
        if name and len(name) <= 8 and not any(w in name for w in ("东西", "零食", "一堆", "点东西")):
            r = give(name, 1, source="self_buy")
            if r.get("ok"):
                changed += 1
    for m in _EMPTY_RE.finditer(t):
        name = str(m.group(1)).strip().lstrip("我把将")
        if name and find(name):
            r = take(name, 999)
            if r.get("ok"):
                changed += 1
    for m in _ADOPT_RE.finditer(t):
        pet = m.group(1)
        if not find(f"宠物：{pet}"):
            give(f"宠物：{pet}", 1, source="self_adopt")
            changed += 1
    return {"changed": changed}


def move_item(name, room, container):
    """把物品移到别的容器。"""
    data = _load()
    hit = next((i for i in data["items"] if str(i.get("name", "")) == name), None)
    if not hit:
        return {"ok": False, "reason": f"没有{name}"}
    cap = int(_cfg("container_capacity", 10))
    occupants = [i for i in data["items"] if i.get("container") == container and i.get("name") != name]
    if len(occupants) + 1 > cap:
        return {"ok": False, "reason": f"{container}满了，放不下"}
    hit["room"], hit["container"] = room, container
    hit["position"] = "不知道塞哪了"
    _move_log(data, "move", name, 0, f"移到了{room}的{container}")
    _save(data)
    try:
        from memory import space as space_mod
        space_mod.emit("item_move", f"把{name}放到了{room}的{container}")
    except Exception:
        pass
    return {"ok": True, "name": name, "room": room, "container": container}


def today_events() -> list:
    """今天的生活事件（供分享/回忆使用）。"""
    d = date.today().isoformat()
    data = _load()
    return [m for m in data.get("moves") or [] if str(m.get("ts", "")).startswith(d)]


def daily_tick() -> dict:
    """每日演化：小概率物品被消耗/过期，让世界会自己变化。"""
    if not _cfg("daily_tick", True):
        return {"changed": False}
    data = _load()
    consumables = [i for i in data["items"] if int(i.get("qty", 0)) > 0 and i.get("category") in ("零食", "饮品")]
    if consumables and random.random() < 0.35:
        it = random.choice(consumables)
        qty = max(0, int(it.get("qty", 1)) - 1)
        it["qty"] = qty
        it["status"] = "没有了" if qty <= 0 else "有"
        _move_log(data, "expire", it["name"], 1, "日常消耗（过期/用掉了）")
        _save(data)
        try:
            from memory import space as space_mod
            space_mod.emit("item_expire", f"{it['name']}被用掉了")
        except Exception:
            pass
        return {"changed": True, "kind": "expire", "name": it["name"], "qty": qty}
    # 小概率错放/丢失：物品被挪到别的容器，且"找不到"
    if random.random() < 0.12:
        loose = [i for i in data["items"] if i.get("status") == "有"]
        if loose:
            it = random.choice(loose)
            rooms = list(home_layout().keys())
            it["room"] = random.choice(rooms)
            it["container"] = random.choice(["储物箱", "抽屉", "柜子"])
            it["position"] = "不知道塞哪了"
            it["status"] = "找不到"
            _move_log(data, "lost", it["name"], 0, "不知道被塞到哪了（错放/丢失）")
            _save(data)
            try:
                from memory import space as space_mod
                space_mod.emit("item_lost", f"{it['name']}找不到了")
            except Exception:
                pass
            return {"changed": True, "kind": "lost", "name": it["name"]}
    return {"changed": False}


def random_flavor() -> str:
    """梦境素材：随机一个家里物件名。"""
    try:
        items = all_items()
        if items:
            return random.choice(items).get("name", "")
    except Exception:
        pass
    return ""


# ===== 动态距离感 =====
def travel_time(place, mode=None, now=None) -> dict:
    """到某处的耗时：基准 × 交通方式 × 天气 × 由乃懒系数 × 情绪 × 当日种子抖动。"""
    now = now or datetime.now()
    pts = places()
    if place not in pts:
        return {"ok": False, "reason": f"不知道{place}在哪"}
    p = pts[place]
    mode = mode or str(p.get("default_mode", "walk"))
    key = f"{mode}_min"
    if key not in p:
        return {"ok": False, "reason": f"没有{mode}方式"}
    base = float(p[key])

    mult = 1.0
    factors = []
    try:
        from memory import weather as weather_mod
        w = weather_mod.fetch(now)
        wtext = str(w.get("text", ""))
        for k, (mw, mt, md) in _WEATHER_MULT.items():
            if k in wtext:
                mult *= {"walk": mw, "bike": mw, "transit": mt, "drive": md}.get(mode, mt)
                factors.append(f"天气({wtext})")
                break
    except Exception:
        pass

    if mode in ("walk", "bike"):
        lazy = float(_cfg("lazy_factor", 1.15))
        mult *= lazy
        factors.append("由乃懒得动")
        try:
            from memory import emotion as emotion_mod
            st = emotion_mod.ai_state()
            if float(st.get("a", 0.0)) < 0.1:
                mult *= 1.2
                factors.append("今天没什么力气")
        except Exception:
            pass

    rng = random.Random(
        int(hashlib.sha256(f"dist:{place}:{mode}:{now.date()}".encode()).hexdigest()[:12], 16)
    )
    jitter = 1.0 + rng.uniform(-0.1, 0.1)
    minutes = max(1, round(base * mult * jitter))
    return {"ok": True, "place": place, "mode": mode, "minutes": minutes,
            "base": base, "factors": factors,
            "arrive_at": (now + timedelta(minutes=minutes)).isoformat(timespec="seconds")}


def depart_at(target, mode=None, now=None) -> dict:
    """倒推出发时间：想在 target 时刻到某处，几点出发。"""
    now = now or datetime.now()
    if isinstance(target, str):
        try:
            target = datetime.fromisoformat(target)
        except Exception:
            return {"ok": False, "reason": "目标时间格式不对"}
    try:
        r = travel_time(target_place_of(target), mode, now)
        minutes = int(r.get("minutes", 30))
    except Exception:
        minutes = 30
    return {"ok": True, "depart_at": (target - timedelta(minutes=minutes)).isoformat(timespec="seconds"),
            "minutes": minutes}


def target_place_of(dt) -> str:
    """目标时刻对应的场所（按日程）。"""
    try:
        from memory import schedule as schedule_mod
        cur = schedule_mod.current_activity(dt)
        act = cur.get("activity") if cur else ""
    except Exception:
        act = ""
    return {"performance": "演出场地", "rehearsal": "排练室", "work": "公司",
            "shopping": "便利店", "exercise": "公园", "friend": "公园",
            "out_entertain": "外面"}.get(act, "家")


# ===== 注入块 =====
def room_now(now=None) -> str:
    """按空间层判断此刻在家哪个房间；不在家/在路上返回 ''（单一事实源）。"""
    try:
        from memory import space as space_mod
        pos = space_mod.position(now)
        if pos.get("state") == "在途中" or pos.get("location") != "家":
            return ""
    except Exception:
        pass
    try:
        from memory import schedule as schedule_mod
        cur = schedule_mod.current_activity(now)
        act = cur.get("activity") if cur else ""
    except Exception:
        act = ""
    return _ACTIVITY_ROOM.get(act, "")


def visible(room) -> list:
    """目光所及：房间家具（不展开容器内容）。"""
    layout = home_layout()
    room_data = layout.get(room) or {}
    return room_data.get("furniture", [])


def _container_room(container) -> str:
    for r, lv in home_layout().items():
        if container in (lv.get("furniture") or []):
            return r
    return ""


def _inspect_pending(scope, container) -> bool:
    data = _db.kv_get("memory", "inspect_pending") or {}
    p = data.get(str(scope or ""), {})
    return bool(p and p.get("container") == container)


def schedule_inspection(scope, container, now=None):
    if not scope:
        return
    now = now or datetime.now()
    delay = max(10, int(_cfg("inspect_delay_s", 30)) + random.randint(-8, 8))
    data = _db.kv_get("memory", "inspect_pending") or {}
    data[str(scope)] = {
        "container": str(container),
        "room": _container_room(container),
        "deliver_at": (now + timedelta(seconds=delay)).isoformat(timespec="seconds"),
        "ts": now.isoformat(timespec="seconds"),
    }
    _db.kv_set("memory", "inspect_pending", data)


def due_inspections(now=None) -> list:
    now = now or datetime.now()
    data = _db.kv_get("memory", "inspect_pending") or {}
    out = []
    for scope, p in data.items():
        try:
            if now < datetime.fromisoformat(str(p.get("deliver_at", ""))):
                continue
        except Exception:
            continue
        s = str(scope)
        if s.startswith("group:"):
            tt, tg = "group", s.split(":", 1)[1]
        else:
            tt, tg = "c2c", s.split(":", 1)[1] if ":" in s else s
        out.append({
            "scope": scope,
            "container": str(p.get("container", "")),
            "room": str(p.get("room", "")),
            "target_type": tt,
            "target": tg,
        })
    return out


def take_inspection(scope):
    data = _db.kv_get("memory", "inspect_pending") or {}
    if str(scope) in data:
        data.pop(str(scope))
        _db.kv_set("memory", "inspect_pending", data)


def inspection_prompt(item) -> str:
    items = lookup(str(item.get("container", "")))
    item_text = "；".join(
        f"{i['name']}×{i['qty']}{_ITEM_UNIT.get(i['name'], '')}（{i.get('position', '')}）"
        for i in items
    ) or "空的"
    return INSPECT_PROMPT.format(room=item.get("room", ""), container=item.get("container", ""), items=item_text)


def home_block(scope="", text="", now=None) -> str:
    """生活注入块（内部参考）：此刻房间 + 目光所及 + 按需展开容器/距离。"""
    if not _cfg("enabled", True):
        return ""
    now = now or datetime.now()
    try:
        from memory import space as space_mod
    except Exception:
        space_mod = None
    pos = space_mod.position(now) if space_mod else {}
    parts = []
    if pos.get("state") == "在途中":
        parts.append(f"【此刻】在去{pos.get('to', '某处')}的路上（{pos.get('mode', '')}）")
    room = room_now(now)
    if room and pos.get("location") == "家":
        vis = visible(room)
        v = space_mod.room_visual(room) if space_mod else {}
        vis_text = "、".join(vis)
        if v.get("desc"):
            vis_text += f"（{v['desc']}）"
        parts.append(f"【此刻】在家·{room}（目光所及：{vis_text}）")
    t = str(text or "")
    if room:
        containers = ("储物箱", "冰箱", "零食柜", "床头柜", "茶几", "电视柜")
        any_named = any(c in t for c in containers)
        generic = any(w in t for w in ("有什么", "还有", "找找", "看看", "箱", "抽屉"))
        vis = set(visible(room) or [])
        for container in containers:
            named = container in t
            if not named and (not generic or any_named or container not in vis):
                continue
            items = lookup(container)
            if items:
                see = space_mod.can_see(room, container, now) if space_mod else {"visible": True}
                if see.get("visible"):
                    parts.append(
                        f"【{container}里】" + "；".join(
                            f"{i['name']}×{i['qty']}{_ITEM_UNIT.get(i['name'], '')}（{i.get('position','')}，{i.get('status','有')}）"
                            for i in items
                        )
                    )
                elif see.get("dim"):
                    parts.append(f"【{container}】光线太暗，看不太清")
                else:
                    croom = _container_room(container)
                    if _inspect_pending(scope, container):
                        parts.append(INSPECT_GOING.format(room=croom, container=container))
                    else:
                        schedule_inspection(scope, container, now)
                        parts.append(INSPECT_GO.format(room=croom, room_now=room, container=container))
    for place in places():
        if place in t or any(w in t for w in ("多久", "分钟", "远不远", "远吗", "多远", "去")):
            if place in t:
                r = travel_time(place, now=now)
                if r.get("ok"):
                    home = home_location()
                    head = f"家（{home}）→" if home else "家→"
                    parts.append(
                        f"【距离参考】{head}{place} 用{r['mode']}约{r['minutes']}分钟"
                        + (f"（{('、'.join(r['factors']))}）" if r["factors"] else "")
                        + f"，现在出发约{r.get('arrive_at', '')[-5:]}到"
                    )
                break
    if not parts:
        return ""
    try:
        from memory import sensors as sensors_mod
        if s := sensors_mod.block(room):
            parts.append(s)
        if s := sensors_mod.named_block(room, t):
            parts.append(s)
    except Exception:
        pass
    try:
        if space_mod and (s := space_mod.actions_block(scope)):
            parts.append(s)
    except Exception:
        pass
    parts.append("内部参考：别主动报生活细节，被问起或相关时自然带一句，不要生硬播报")
    return "；".join(parts)

# ===== AI 生日与年龄（v31.3）=====
def ai_birthday():
    """AI 生日 (月, 日)，config → memory.core.living.birthday，格式 MM-DD。"""
    b = str(_cfg("birthday", "") or "").strip()
    if not b:
        return None
    try:
        m, d = b.split("-")
        return int(m), int(d)
    except Exception:
        return None


def ai_age(now=None):
    """AI 年龄 = 当前年 - birth_year（config）。"""
    now = now or datetime.now()
    by = _cfg("birth_year", None)
    if not by:
        return None
    try:
        return max(0, now.year - int(by))
    except Exception:
        return None


def days_to_birthday(now=None) -> int:
    """距生日天数：今天生日返回 0，否则返回下一次生日的天数。"""
    now = now or datetime.now()
    bd = ai_birthday()
    if not bd:
        return 9999
    m, d = bd
    this = now.date().replace(month=m, day=d)
    if this == now.date():
        return 0
    nxt = this if this > now.date() else this.replace(year=this.year + 1)
    return (nxt - now.date()).days


def birthday_hint_block(scope="", text="", now=None) -> str:
    """生日临近暗示（内部参考）：关系达到门槛才注入——她不好意思直说，但渴望你在意。"""
    now = now or datetime.now()
    d = days_to_birthday(now)
    if d <= 0 or d > int(_cfg("birthday_hint_days", 7)):
        return ""
    try:
        from memory import interaction as interaction_mod
        if interaction_mod.familiarity_effective(scope) < float(_cfg("birthday_threshold", 0.4)):
            return ""
    except Exception:
        return ""
    return (
        f"【生日临近·内部参考】再过 {d} 天是她的生日。她不好意思直说，但希望你在意。"
        "可以在合适的时机自然带一句'快到某个日子了'这类暗示，别直说'我生日快到了'，更别要礼物——"
        "嘴硬心软，暗示就好。如果用户记起来并祝贺，她会别扭地开心。"
    )


def birthday_reaction_block(scope="", text="", now=None) -> str:
    """生日当天收到祝贺：别扭的开心（内部参考）。"""
    now = now or datetime.now()
    if days_to_birthday(now) != 0:
        return ""
    t = str(text or "")
    if not any(w in t for w in ("生日快乐", "祝贺", "生辰", "happy birthday", "hbd")):
        return ""
    return (
        "【今天是她的生日】用户来祝贺生日。她心里非常高兴但嘴硬（别扭的开心）："
        "可以'……你怎么知道的''谢了，记性不错'这种反应，别长篇大论煽情，也别否认今天是生日。"
    )


def birthday_celebrate(now=None) -> dict:
    """生日当天（每日 grow 调用）：写一条记忆 + 空间事件，一年一次。"""
    now = now or datetime.now()
    if days_to_birthday(now) != 0:
        return {"celebrated": False}
    year = now.year
    flag = _db.kv_get("memory", f"birthday_celebrated:{year}") or {}
    if flag.get("done"):
        return {"celebrated": False, "already": True}
    age = ai_age(now)
    detail = f"今天是我的生日，{age}岁了" if age is not None else "今天是我的生日"
    try:
        from memory import space as space_mod
        space_mod.emit("birthday", detail, memorable=True)
    except Exception:
        pass
    _db.kv_set("memory", f"birthday_celebrated:{year}", {"done": True, "detail": detail})
    return {"celebrated": True, "age": age, "detail": detail}
