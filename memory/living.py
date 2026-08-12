"""生活环境层（v31）：家的布局 + 容器物品（懒展开）+ 动态距离感。

- 布局：房间 → 家具（部分家具是容器），静态配置，kv 持久化；
- 物品：存在 kv（SQLite 落盘），"装在箱子里"——上下文只注入房间和目光所及，
  箱子里有什么要查询才展开（lookup）；
- 操作：拿/用（数量-1）、用户送（新增或累加）、移动（换容器）、查（find/lookup）；
    - 距离感：不是固定时间——基准分钟 × 交通方式系数 × 天气修正 × 角色的懒系数 ×
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

# 场所 → 各交通方式基准分钟 + 默认方式（基准是"健康成人"时间，最后会乘角色懒系数）
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
INSPECT_PROMPT = "你是{name}（{role}）。你刚才答应用户“我去看看”，现在你走到了{room}，打开{container}，看到：{items}。用她的口吻给用户发一条简短消息（15~35字），像真人聊天自然地汇报里面有什么；名称和数量照实说，不要加“盒/罐/袋/碎”等原文没有的单位，不要提“系统/数据/汇报”。"

_BUY_RE = re.compile(r"(?:顺路买了|刚买了|买了点|买了)([\u4e00-\u9fffA-Za-z0-9]{1,8}?)(?:，|。|！|！|$)")
_EMPTY_RE = re.compile(r"([\u4e00-\u9fffA-Za-z0-9]{1,8}?)(?:吃完了|喝完了|用完了|吃光了|喝光了|用光了)")
_ADOPT_RE = re.compile(r"(?:养了|领养了)(?:一只|一只小|个)?(猫|狗|仓鼠|兔子|金鱼|鹦鹉)")


def _cfg(key, default):
    lv = (_shared.CONFIG.get("memory", {}).get("core", {}) or {}).get("living", {}) or {}
    return lv.get(key, default)


def _pack_world() -> dict:
    try:
        from memory import pack
        return pack.world()
    except Exception:
        return {}


def _pack_behavior() -> dict:
    try:
        from memory import pack
        return pack.behavior()
    except Exception:
        return {}


def _container_capacity() -> int:
    return int(_cfg("container_capacity", _pack_world().get("container_capacity", 10)))


def home_layout() -> dict:
    w = _pack_world().get("layout")
    if isinstance(w, dict) and w:
        return w
    return _cfg("layout", HOME_LAYOUT_DEFAULT) or HOME_LAYOUT_DEFAULT


def home_location() -> str:
    w = _pack_world().get("home_location")
    if w:
        return str(w)
    return str(_cfg("home_location", "") or "").strip()


def places() -> dict:
    w = _pack_world().get("places")
    if isinstance(w, dict) and w:
        return w
    return _cfg("places", PLACES_DEFAULT) or PLACES_DEFAULT


# ===== 物品存储（kv，SQLite 落盘）=====
def _load() -> dict:
    data = _db.kv_get("memory", "living_items") or {}
    if data and data.get("items") is not None:
        return data
    w = _pack_world().get("items")
    items = [dict(i) for i in w] if isinstance(w, list) and w else [dict(i) for i in DEFAULT_ITEMS]
    data = {"items": items, "moves": []}
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


# ===== 物品位置历史 / 激活 / 找东西（P0-1 / P0-3）=====
_SEE_LAST = {}  # 内存节流：see 事件同一物品 5 分钟内只记一次（重启丢失无影响）


def container_room(container) -> str:
    """容器所在房间（public，供 space 层复用）。"""
    return _container_room(container)


def _record_item_event(item_name, event, from_place="", to_place="", cause="", seen_by="", ts=None):
    """追加物品事件流水（move/give/see/take/consume/lost/find…）；see 类节流。"""
    item_name = str(item_name or "").strip()
    if not item_name:
        return
    now = ts or datetime.now()
    if event == "see":
        last = _SEE_LAST.get(item_name)
        if last is not None and (now - last).total_seconds() < float(_cfg("see_throttle_s", 300)):
            return
        _SEE_LAST[item_name] = now
    try:
        _db.item_event_add(
            item_name, now.isoformat(timespec="seconds"),
            event, from_place, to_place, cause, seen_by,
        )
    except Exception as e:
        _stats_err(e)
        pass


def _see_items(names, cause="lookup", seen_by="ai", ts=None):
    """批量记录'看到'：一次 kv 更新刷新激活 + 一次批量落事件（节流在内存判断）。"""
    names = [str(n or "").strip() for n in names]
    names = [n for n in names if n]
    if not names:
        return
    now = ts or datetime.now()
    throttle = float(_cfg("see_throttle_s", 300))
    fresh = [n for n in names if _SEE_LAST.get(n) is None or (now - _SEE_LAST.get(n)).total_seconds() >= throttle]
    for n in fresh:
        _SEE_LAST[n] = now
    if not fresh:
        return
    try:
        d = _db.item_activation_rows()
        changed = False
        for n in fresh:
            a = d.get(n) or {"seen_ts": "", "count": 0}
            if a.get("seen_ts"):
                try:
                    if (now - datetime.fromisoformat(str(a["seen_ts"]))).total_seconds() < throttle:
                        continue
                except Exception as e:
                    _stats_err(e)
                    pass
            a["seen_ts"] = now.isoformat(timespec="seconds")
            a["count"] = int(a.get("count", 0)) + 1
            d[n] = a
            changed = True
        if changed:
            _db.item_activation_set(d)
    except Exception as e:
        _stats_err(e)
        pass
    try:
        data = _load()
        rows = []
        for n in fresh:
            it = next((i for i in data["items"] if str(i.get("name", "")) == n), {})
            tp = _item_where(it)
            if not tp:
                continue
            rows.append({
                "item": n, "ts": now.isoformat(timespec="seconds"),
                "event": "see", "from_place": "", "to_place": tp,
                "cause": cause, "seen_by": seen_by,
            })
        if rows:
            _db.item_event_add_many(rows)
    except Exception as e:
        _stats_err(e)
        pass


def _item_where(item) -> str:
    return f"{item.get('room', '')}/{item.get('container', '')}"


def touch_item(name, ts=None) -> None:
    """刷新物品激活（最近看到/移动）：节流，避免每次注入都写。"""
    name = str(name or "").strip()
    if not name:
        return
    now = ts or datetime.now()
    d = _db.item_activation_rows()
    a = d.get(name) or {"seen_ts": "", "count": 0}
    try:
        if a.get("seen_ts") and (now - datetime.fromisoformat(str(a["seen_ts"]))).total_seconds() < float(_cfg("see_throttle_s", 300)):
            return
    except Exception as e:
        _stats_err(e)
        pass
    a["seen_ts"] = now.isoformat(timespec="seconds")
    a["count"] = int(a.get("count", 0)) + 1
    d[name] = a
    _db.item_activation_set(d)


def activation(name, now=None) -> float:
    """物品激活值 0~1：频率(0.35) + 最近可见指数衰减(0.4) − 难度/丢失惩罚(≤0.4)。
    高→直接答；中→模糊答；低→触发搜索。"""
    name = str(name or "").strip()
    now = now or datetime.now()
    it = next((i for i in all_items() if str(i.get("name", "")) == name), None)
    if not it:
        return 0.0
    d = _db.item_activation_rows()
    a = d.get(name) or {}
    count = int(a.get("count", 0))
    days = 9999.0
    try:
        days = (now - datetime.fromisoformat(str(a.get("seen_ts", "")))).total_seconds() / 86400.0
    except Exception as e:
        _stats_err(e)
        pass
    half = float(_cfg("activation_half_life_days", 30))
    freq = min(1.0, count / 5.0) * 0.35
    recency = 0.4 * (0.5 ** (max(0.0, days) / max(1.0, half)))
    penalty = 0.25 if str(it.get("difficulty", "")) == "深" else 0.0
    if str(it.get("status", "")) == "找不到":
        penalty += 0.15
    return round(max(0.0, min(1.0, 0.2 + freq + recency - penalty)), 3)


def item_history(name, limit=100) -> list:
    """物品事件流水（新→旧）。"""
    return _db.item_event_rows(str(name or "").strip(), limit=int(limit))


def position_at(name, ts=None) -> dict:
    """某时刻物品在哪（事件溯源投影）；无历史时回退当前库存状态。"""
    name = str(name or "").strip()
    if not name:
        return {"known": False, "reason": "没有物品名"}
    now = ts or datetime.now()
    if isinstance(now, datetime):
        now = now.isoformat(timespec="seconds")
    r = _db.item_position_at(name, now)
    if r:
        return r
    it = next((i for i in all_items() if str(i.get("name", "")) == name), None)
    if not it:
        return {"known": False, "reason": "没有这个物品"}
    return {
        "item": name, "room": it.get("room", ""), "container": it.get("container", ""),
        "known": it.get("status") != "找不到",
    }


_SEARCH_WORDS = ("在哪", "哪里", "哪儿", "放哪", "找找", "找不到", "找不到了", "找一下", "帮我找", "帮我找找")


def _mentioned_item(text) -> str:
    t = str(text or "")
    for it in all_items():
        n = str(it.get("name", ""))
        if n and n in t:
            return n
    return ""


def _start_item_search(scope, name, now=None) -> str:
    """开始搜索：候选容器 = 最后已知位置优先 + 其余容器（最多 search_max_steps 个）。"""
    now = now or datetime.now()
    try:
        from memory import space as space_mod
        p = space_mod.position(now)
        if p.get("location") != "家" or p.get("state") == "在途中":
            return f"【找东西】{name}现在不在家，等回去再找。"
    except Exception as e:
        _stats_err(e)
        pass
    it = next((i for i in all_items() if str(i.get("name", "")) == name), None)
    if not it:
        return ""
    last = _db.item_position_at(name, now.isoformat(timespec="seconds"))
    containers = []
    for r, lv in home_layout().items():
        for c in (lv.get("furniture") or []):
            containers.append(c)
    seen, ordered = set(), []
    for c in ([last.get("container", "")] if last else []) + containers:
        if c and c not in seen:
            seen.add(c)
            ordered.append(c)
    if not ordered:
        return f"【找东西·搜索】{name}不知道塞哪了，家里好像也没有能放它的地方。"
    queue = ordered[: int(_cfg("search_max_steps", 5))]
    _db.item_search_set(str(scope), {
        "name": name, "queue": queue, "step": 0,
        "started_at": now.isoformat(timespec="seconds"),
    })
    schedule_inspection(scope, queue[0], now, kind="search")
    return (
        f"【找东西·搜索】{name}不知道塞哪了（印象很模糊）。"
        f"先回一句'我去找找'，系统会从{queue[0]}开始逐处查看，别现在就下结论说在哪。"
    )


def where_is_block(scope, text, now=None) -> str:
    """'X 在哪'分级：激活高直接答 / 中模糊答 / 低或丢失触发搜索（P0-3）。"""
    t = str(text or "")
    if not any(w in t for w in _SEARCH_WORDS):
        return ""
    name = _mentioned_item(t)
    if not name:
        return ""
    # 搜索进行中：不重启，给进度并继续（重问不从头来）
    try:
        d = _db.item_search_rows()
        st = d.get(str(scope))
        if st and st.get("name") == name:
            step = int(st.get("step", 0))
            queue = st.get("queue") or []
            if step < len(queue):
                schedule_inspection(scope, queue[step], now, kind="search")
            return (
                f"【找东西·进行中】{name}还在找（已翻到第{step}处）。"
                f"用户问进度就自然说'还在找/刚翻到哪'；新一轮提问就说'我去看看'，别重启搜索。"
            )
    except Exception as e:
        _stats_err(e)
        pass
    it = next((i for i in all_items() if str(i.get("name", "")) == name), None)
    if not it:
        return ""
    now = now or datetime.now()
    a = activation(name, now)
    if str(it.get("status", "")) == "找不到" or a < float(_cfg("search_activation_low", 0.35)):
        return _start_item_search(scope, name, now)
    if a < float(_cfg("search_activation_mid", 0.6)):
        room = str(it.get("room", ""))
        return (
            f"【找东西·模糊】{name}好像在{room}附近（记不太清具体位置）。"
            f"被问起时可以说'好像在那块/记不清了'，也可以说'我帮你找找'，别把话说死。"
        )
    touch_item(name, now)
    return (
        f"【找东西·直接】{name}在{it.get('room', '')}的{it.get('container', '')}"
        f"（{it.get('position', '')}）。被问起时直接回答即可。"
    )


_CANCEL_WORDS = ("别找了", "不找了", "算了", "不用找", "别找", "放弃吧")


def _persona_name() -> str:
    try:
        from agent import persona
        return persona.persona_name()
    except Exception:
        return "YUNO"


def _persona_role() -> str:
    try:
        from memory import pack
        return str(pack.world().get("role") or pack.behavior().get("role") or "")
    except Exception:
        return ""


def cancel_search(scope) -> bool:
    """用户说别找了 → 取消进行中的搜索。"""
    d = _db.item_search_rows()
    if str(scope) in d:
        _db.item_search_delete(str(scope))
        return True
    return False


def ask_npc(name, text, top_k=2) -> str:
    """问队友找东西/回忆：检索该 NPC 视角的记忆（含物品位置事件）。"""
    try:
        from memory import reasoning, subjects
        q = str(text or "")
        hits = reasoning.retrieve_subject(name, q, top_k=int(top_k), min_score=0.25)
        if not hits:
            return ""
        lines = [f"- {f}" for f, _s, _sc in hits]
        return f"【问队友·{name}】她记得：{'；'.join(lines)}（内部参考：被问起时按她说的转述，注明'她说的'）"
    except Exception as e:
        _stats_err(e)
        return ""


def search_progress(scope) -> dict:
    """搜索推进（broadcast 汇报循环调用）：当前容器找到→成功；否则下一处或失败。
    返回 {done, found, name, container, prompt}。"""
    d = _db.item_search_rows()
    st = d.get(str(scope))
    if not st:
        return {"done": True, "found": False, "name": "", "container": "", "prompt": ""}
    # 过期放弃（P2 优化）：搜索超过 TTL 未推进即作废
    try:
        started = datetime.fromisoformat(str(st.get("started_at", "")))
        if datetime.now() - started > timedelta(minutes=float(_cfg("search_ttl_min", 30))):
            _db.item_search_delete(str(scope))
            return {"done": True, "found": False, "name": st.get("name", ""), "container": "",
                    "prompt": ""}
    except Exception as e:
        _stats_err(e)
        pass
    # 话题转移暂停（不突兀）：搜索开始后用户发了不相关消息 → 停住，等用户再提起
    try:
        last = _db.kv_get("memory", f"last_user_msg:{str(scope)}")
        if last and last.get("text"):
            last_ts = None
            try:
                last_ts = datetime.fromisoformat(str(last.get("ts", "")))
            except Exception as e:
                _stats_err(e)
                pass
            if last_ts:
                started = datetime.fromisoformat(str(st.get("started_at", "")))
                item_name = str(st.get("name", ""))
                lt = str(last.get("text", ""))
                if last_ts > started and item_name not in lt and not any(w in lt for w in _SEARCH_WORDS):
                    return {"done": False, "paused": True, "found": False,
                            "name": item_name, "container": "", "prompt": ""}
    except Exception as e:
        _stats_err(e)
        pass
    name = st.get("name", "")
    queue = st.get("queue") or []
    step = int(st.get("step", 0))

    def _finish():
        _db.item_search_delete(str(scope))

    if step >= len(queue):
        _finish()
        return {
            "done": True, "found": False, "name": name, "container": "",
            "prompt": f"你是{_persona_name()}。你翻遍了家里几处都没找到{name}。用她的口吻回一句（15~35字），像'……没找到，不知道塞哪了'，别说'系统'。",
        }
    container = queue[step]
    hit = next((i for i in lookup(container) if str(i.get("name", "")) == name), None)
    if hit and hit.get("status") != "找不到":
        _finish()
        try:
            import memory.stats as stats_mod
            stats_mod.bump("search_found")
        except Exception as e:
            _stats_err(e)
            pass
        touch_item(name)
        _record_item_event(name, "find", to_place=_item_where(hit), cause="search", seen_by="ai")
        data = _load()
        for i in data["items"]:
            if str(i.get("name", "")) == name:
                i["status"] = "有"
                i["room"] = hit.get("room", i.get("room", ""))
                i["container"] = hit.get("container", i.get("container", ""))
        _save(data)
        try:
            from memory import space as space_mod
            space_mod.emit("item_find", f"在{container}里找到了{name}", location=hit.get("room", ""))
        except Exception as e:
            _stats_err(e)
            pass
        return {
            "done": True, "found": True, "name": name, "container": container,
            "prompt": f"你是{_persona_name()}。你在{container}里找到了{name}（{hit.get('position','')}）。用她的口吻给用户发一条简短消息（15~35字）汇报找到了，别加原文没有的单位，别提系统。",
        }
    nxt_step = step + 1
    st["step"] = nxt_step
    _db.item_search_set(str(scope), st)
    _record_item_event(
        name, "search_miss", to_place=f"{container_room(container)}/{container}",
        cause="search", seen_by="ai",
    )
    if nxt_step >= len(queue):
        _finish()
        try:
            import memory.stats as stats_mod
            stats_mod.bump("search_fail")
        except Exception as e:
            _stats_err(e)
            pass
        # 彻底失败：按概率把物品标记为"找不到"（下次是"真的丢了"，而不是同一场搜索重来）
        try:
            if random.random() < float(_cfg("search_fail_mark_prob", 0.6)):
                data = _load()
                for i in data["items"]:
                    if str(i.get("name", "")) == name:
                        i["status"] = "找不到"
                        i["position"] = "不知道塞哪了"
                _save(data)
                _record_item_event(name, "lost", to_place="", cause="search_fail", seen_by="ai")
                from memory import space as space_mod2
                space_mod2.emit("item_lost", f"{name}找不到了")
        except Exception as e:
            _stats_err(e)
            pass
        return {
            "done": True, "found": False, "name": name, "container": container,
            "prompt": f"你是{_persona_name()}。你翻遍家里几处都没找到{name}，这次是真的找不到了。用她的口吻回一句（15~35字），像'……找不到了，不知道塞哪去了'，别硬编一个位置，别提系统。",
        }
    nxt = queue[nxt_step]
    schedule_inspection(scope, nxt, kind="search")
    try:
        import memory.stats as stats_mod
        stats_mod.bump("search_step")
    except Exception as e:
        _stats_err(e)
        pass
    if _cfg("search_quiet", True):
        # 静默推进：中间步骤不播报，只报结果（不突兀）
        return {"done": False, "quiet": True, "found": False,
                "name": name, "container": container, "prompt": ""}
    return {
        "done": False, "found": False, "name": name, "container": container,
        "prompt": f"你是{_persona_name()}。你在{container}里没找到{name}，准备再去{nxt}看看。用她的口吻回一句（15~35字），像'这没有，去{nxt}看看'，别提系统。",
    }


def find(name) -> list:
    """按名字找物品（模糊）。"""
    t = str(name or "").strip()
    data = _load()
    if not t:
        return []
    hits = [i for i in data["items"] if t in str(i.get("name", ""))]
    if hits:
        _see_items([str(it.get("name", "")) for it in hits], cause="lookup")
    return hits


def lookup(container, data=None) -> list:
    """展开容器：返回该容器里的所有物品（data 可复用一次 _load，避免重复 kv 读）。"""
    t = str(container or "").strip()
    data = data if data is not None else _load()
    if not t:
        return []
    hits = [i for i in data["items"] if str(i.get("container", "")) == t]
    if hits:
        _see_items([str(it.get("name", "")) for it in hits], cause="lookup")
    return hits


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
        _record_item_event(name, "take", to_place=_item_where(hit), cause="take", seen_by="ai")
        touch_item(name)
    except Exception as e:
        _stats_err(e)
        pass
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
    except Exception as e:
        _stats_err(e)
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
    try:
        _record_item_event(name, "consume", to_place=_item_where(hit), cause="consume", seen_by="user")
        touch_item(name)
    except Exception as e:
        _stats_err(e)
        pass
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
        cap = _container_capacity()
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
        if room_count + 1 > _container_capacity():
            hit["container"] = "储物箱"
        data["items"].append(hit)
    _move_log(data, "give", name, n, f"用户送来了{n}个{name}")
    _save(data)
    try:
        _record_item_event(name, "give", to_place=_item_where(hit), cause=source, seen_by="user")
        touch_item(name)
    except Exception as e:
        _stats_err(e)
        pass
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
    except Exception as e:
        _stats_err(e)
        pass
    return {"ok": True, "name": name, "qty": hit["qty"]}


_WORLD_PROMPT = (
    "你是{name}家里的世界状态解析器。根据用户这句话，判断世界发生了什么变化，"
    "只输出 JSON：{\"ops\":[{\"type\":\"take|give|consume|move|device\",\"item\":\"物品名\",\"qty\":1,"
    "\"room\":\"房间\",\"container\":\"容器\",\"device\":\"设备名\",\"state\":\"状态\"}]}。"
    "type 含义：take=角色拿取；give=用户送东西给角色；consume=物品被消耗（用户喝/吃了）；"
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
    prompt = _WORLD_PROMPT.format(name=_persona_name()) + "\n已知物品：" + item_hint + "\n用户说：" + str(text)[:200]
    try:
        from plugins import _shared
        reply = _shared.ask_deepseek(prompt, temperature=0, module="living")
    except Exception as e:
        _stats_err(e)
        return {"changed": 0, "reason": "llm_fail"}
    try:
        m = re.search(r"\{.*\}", reply, re.S)
        delta = json.loads(m.group(0))
    except Exception as e:
        _stats_err(e)
        return {"changed": 0, "reason": "parse_fail"}
    return _apply_delta(scope, delta)


def _apply_delta(scope, delta):
    changed = 0
    rejected = 0
    reasons = []
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
            elif r.get("reason"):
                rejected += 1
                reasons.append(f"{t}({op.get('item', '')}): {r['reason']}")
        except Exception as e:
            _stats_err(e)
            continue
    return {"changed": changed, "rejected": rejected, "reasons": reasons[:5]}


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


def _valid_target(room, container) -> bool:
    """目标位置必须真实存在：room ∈ 布局，container ∈ room 的家具。"""
    layout = home_layout()
    if room not in layout:
        return False
    return str(container) in (layout.get(room, {}).get("furniture") or [])


def repair_spatial() -> dict:
    """空间一致性修复（P1-3）：物品 room 必须匹配容器所在房间；容器必须存在。"""
    data = _load()
    fixed = []
    seen = {}
    for it in data["items"]:
        name = str(it.get("name", ""))
        # 数量与状态一致性
        try:
            qty = int(it.get("qty", 0) or 0)
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0 and it.get("status") != "没有了":
            it["status"] = "没有了"
            fixed.append({"name": name, "issue": "数量为 0 但状态为有，改为没有了"})
        elif qty > 0 and it.get("status") == "没有了":
            it["status"] = "有"
            fixed.append({"name": name, "issue": "数量 >0 但状态为没有了，改回有"})
        # 同名多容器：保留第一个，其余标记找不到
        seen[name] = seen.get(name, 0) + 1
        if seen[name] > 1:
            it["status"] = "找不到"
            fixed.append({"name": name, "issue": "同名物品出现在多个容器，保留第一处，其余标记找不到"})
            continue
        croom = _container_room(str(it.get("container", "")))
        if not croom:
            if it.get("status") != "找不到":
                it["status"] = "找不到"
                it["position"] = "不知道塞哪了"
                fixed.append({"name": name, "issue": "容器不存在，标记为找不到"})
            continue
        if str(it.get("room", "")) != croom:
            it["room"] = croom
            fixed.append({"name": name, "issue": f"房间不符，修正为{croom}"})
    # 容器超容量：只记录不搬动（等人工整理）
    cap = _container_capacity()
    per_container = {}
    for it in data["items"]:
        if it.get("status") == "没有了":
            continue
        c = str(it.get("container", ""))
        per_container[c] = per_container.get(c, 0) + 1
    for c, n in per_container.items():
        if n > cap:
            fixed.append({"name": "", "issue": f"容器{c}超出容量（{n} > {cap}），待人工整理"})
    _save(data)
    return {"fixed": len(fixed), "details": fixed[:10]}


_BOOTSTRAP_PROMPT = (
    "你是{name}家里的场景设计师。根据她的身份/性格/动机/偏好（见人设），"
    "补全她的家应该有什么物品。只输出 JSON：{\"items\":[{\"name\":\"物品名\",\"category\":\"类别\","
    "\"qty\":1,\"room\":\"房间\",\"container\":\"容器\",\"position\":\"位置\",\"difficulty\":\"浅|深\","
    "\"origin\":\"为什么有（引用人设依据）\"}]}。要求：房间必须是 客厅/工作室/卧室/厨房 之一，"
    "容器必须是该房间家具之一；最多输出 {max_items} 件；不要输出已有的物品。"
)


def bootstrap_from_persona(scope="", now=None) -> dict:
    """人设→场景生成（P1-2）：LLM 提案 → 合理性校验 → 与现有 diff → 只新增 + 写 origin。"""
    if not _cfg("bootstrap", True):
        return {"changed": 0, "reason": "disabled"}
    try:
        from plugins import _shared
        import pathlib
        persona = ""
        p = pathlib.Path(__file__).resolve().parent.parent / "persona.md"
        if p.exists():
            persona = str(p.read_text(encoding="utf-8"))[:800]
        extra = []
        try:
            rows = _db.memory_rows("ai")
            rows.sort(key=lambda r: float(r.get("confidence", 0.0)), reverse=True)
            for r in rows[:8]:
                if r.get("status") == "superseded":
                    continue
                extra.append(str(r.get("fact", ""))[:60])
        except Exception as e:
            _stats_err(e)
            pass
        try:
            for r in _db.attr_rows("ai"):
                v = str(r.get("value", ""))[:60]
                if v and v not in extra:
                    extra.append(v)
        except Exception as e:
            _stats_err(e)
            pass
        max_items = int(_cfg("bootstrap_max_items", 8))
        prompt = _BOOTSTRAP_PROMPT.replace("{name}", _persona_name()).replace("{max_items}", str(max_items)) + "\n人设摘要：\n" + persona
        if extra:
            prompt += "\n她的经历/偏好（记忆库）：\n" + "\n".join(extra[:6])
        reply = _shared.ask_deepseek(prompt, temperature=0.7, module="living")
        m = re.search(r"\{.*\}", reply, re.S)
        if not m:
            return {"changed": 0, "reason": "parse_fail"}
        delta = json.loads(m.group(0))
    except Exception as e:
        return {"changed": 0, "reason": f"llm_fail: {e}"}
    data = _load()
    existing = {str(i.get("name", "")) for i in data["items"]}
    layout = home_layout()
    cap = _container_capacity()
    added = []
    for it in (delta.get("items") or [])[:max_items]:
        name = str(it.get("name", "")).strip()
        room = str(it.get("room", "")).strip()
        container = str(it.get("container", "")).strip()
        if not name or name in existing or name in [a["name"] for a in added]:
            continue
        if room not in layout or container not in (layout.get(room, {}).get("furniture") or []):
            continue
        occupants = sum(1 for i in data["items"] if i.get("container") == container)
        if occupants + 1 > cap:
            continue
        item = {
            "name": name[:30],
            "category": str(it.get("category", "杂物"))[:20] or "杂物",
            "qty": max(1, int(it.get("qty", 1) or 1)),
            "room": room,
            "container": container,
            "position": str(it.get("position", "放着"))[:30] or "放着",
            "difficulty": "深" if str(it.get("difficulty", "浅")) == "深" else "浅",
            "status": "有",
            "source": "bootstrap",
            "origin": str(it.get("origin", ""))[:80],
        }
        data["items"].append(item)
        added.append(item)
        existing.add(name)
    _save(data)
    return {
        "changed": len(added),
        "items": [a["name"] for a in added],
        "origins": {a["name"]: a["origin"] for a in added},
    }


def move_item(name, room, container):
    """把物品移到别的容器（目标必须真实存在，P1-3）。"""
    data = _load()
    hit = next((i for i in data["items"] if str(i.get("name", "")) == name), None)
    if not hit:
        return {"ok": False, "reason": f"没有{name}"}
    if not _valid_target(room, container):
        return {"ok": False, "reason": f"家里没有{room}的{container}"}
    cap = _container_capacity()
    occupants = [i for i in data["items"] if i.get("container") == container and i.get("name") != name]
    if len(occupants) + 1 > cap:
        return {"ok": False, "reason": f"{container}满了，放不下"}
    from_place = _item_where(hit)
    hit["room"], hit["container"] = room, container
    hit["position"] = "不知道塞哪了"
    _move_log(data, "move", name, 0, f"移到了{room}的{container}")
    _save(data)
    try:
        _record_item_event(name, "move", from_place, _item_where(hit), cause="move", seen_by="ai")
        touch_item(name)
        from memory import space as space_mod
        space_mod.emit("item_move", f"把{name}放到了{room}的{container}", location=room)
    except Exception as e:
        _stats_err(e)
        pass
    return {"ok": True, "name": name, "room": room, "container": container}


def today_events() -> list:
    """今天的生活事件（供分享/回忆使用）。"""
    d = date.today().isoformat()
    data = _load()
    return [m for m in data.get("moves") or [] if str(m.get("ts", "")).startswith(d)]


def daily_tick() -> dict:
    """每日演化：小概率物品被消耗/过期，让世界会自己变化。"""
    try:
        import memory.stats as _st
        _st.bump("tick:living_daily")
    except Exception as e:
        _stats_err(e)
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
            _record_item_event(it["name"], "expire", to_place=_item_where(it), cause="daily_tick", seen_by="sim")
            from memory import space as space_mod
            space_mod.emit("item_expire", f"{it['name']}被用掉了")
        except Exception as e:
            _stats_err(e)
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
                _record_item_event(it["name"], "lost", from_place=_item_where(it), cause="daily_tick", seen_by="sim")
                from memory import space as space_mod
                space_mod.emit("item_lost", f"{it['name']}找不到了")
            except Exception as e:
                _stats_err(e)
                pass
            return {"changed": True, "kind": "lost", "name": it["name"]}
    return {"changed": False}


def random_flavor() -> str:
    """梦境素材：随机一个家里物件名。"""
    try:
        items = all_items()
        if items:
            return random.choice(items).get("name", "")
    except Exception as e:
        _stats_err(e)
        pass
    return ""


# ===== 动态距离感 =====
def travel_time(place, mode=None, now=None) -> dict:
    """到某处的耗时：基准 × 交通方式 × 天气 × 角色懒系数 × 情绪 × 当日种子抖动。"""
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
    except Exception as e:
        _stats_err(e)
        pass

    if mode in ("walk", "bike"):
        lazy = float(_pack_behavior().get("lazy_factor", _cfg("lazy_factor", 1.15)))
        mult *= lazy
        # 因素标签收进 pack（lazy_label），换人设不显示错误名字
        factors.append(str(_pack_behavior().get("lazy_label", _cfg("lazy_label", "懒得动"))))
        try:
            from memory import emotion as emotion_mod
            st = emotion_mod.ai_state()
            if float(st.get("a", 0.0)) < 0.1:
                mult *= 1.2
                factors.append("今天没什么力气")
        except Exception as e:
            _stats_err(e)
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
        except Exception as e:
            _stats_err(e)
            return {"ok": False, "reason": "目标时间格式不对"}
    try:
        r = travel_time(target_place_of(target), mode, now)
        minutes = int(r.get("minutes", 30))
    except Exception as e:
        _stats_err(e)
        minutes = 30
    return {"ok": True, "depart_at": (target - timedelta(minutes=minutes)).isoformat(timespec="seconds"),
            "minutes": minutes}


def target_place_of(dt) -> str:
    """目标时刻对应的场所（按日程）。"""
    try:
        from memory import schedule as schedule_mod
        cur = schedule_mod.current_activity(dt)
        act = cur.get("activity") if cur else ""
    except Exception as e:
        _stats_err(e)
        act = ""
    return {"performance": "演出场地", "rehearsal": "排练室", "work": "公司",
            "shopping": "便利店", "exercise": "公园", "friend": "公园",
            "out_entertain": "外面"}.get(act, "家")


# ===== 注入块 =====
def room_now(now=None) -> str:
    """按空间层判断此刻在家哪个房间；不在家/在路上返回 ''（单一事实源）。"""
    try:
        from memory import space as space_mod
        sp_cfg = (_shared.CONFIG.get("memory", {}).get("core", {}) or {}).get("space", {}) or {}
        if sp_cfg.get("enabled", True):
            r = space_mod.room_position(now)
            if r.get("state") == "在途中":
                return ""
            if r.get("room"):
                return r["room"]
    except Exception as e:
        _stats_err(e)
        pass
    try:
        from memory import space as space_mod
        pos = space_mod.position(now)
        if pos.get("state") == "在途中" or pos.get("location") != "家":
            return ""
    except Exception as e:
        _stats_err(e)
        pass
    try:
        from memory import schedule as schedule_mod
        cur = schedule_mod.current_activity(now)
        act = cur.get("activity") if cur else ""
    except Exception as e:
        _stats_err(e)
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


def schedule_inspection(scope, container, now=None, kind="container"):
    """安排一次查看：先去目标容器所在房间（真实移动，P1-1），到点后汇报。"""
    if not scope:
        return
    now = now or datetime.now()
    room = _container_room(container)
    delay = None
    try:
        from memory import space as space_mod
        if room:
            r = space_mod.move_room(room, now=now)
            ats = r.get("arrive_ts") if r else ""
            if ats:
                try:
                    delay = (datetime.fromisoformat(ats) - now).total_seconds()
                except Exception as e:
                    _stats_err(e)
                    delay = None
    except Exception as e:
        _stats_err(e)
        pass
    if delay is None:
        delay = max(
            10,
            int(_pack_behavior().get("inspect_delay_s", _cfg("inspect_delay_s", 30))) + random.randint(-8, 8),
        )
    delay = max(3.0, float(delay))
    data = _db.kv_get("memory", "inspect_pending") or {}
    data[str(scope)] = {
        "container": str(container),
        "room": room,
        "kind": str(kind),
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
        except Exception as e:
            _stats_err(e)
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
            "kind": str(p.get("kind", "container")),
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
    return INSPECT_PROMPT.format(
        name=_persona_name(), role=_persona_role(),
        room=item.get("room", ""), container=item.get("container", ""), items=item_text,
    )


def home_block(scope="", text="", now=None) -> str:
    """生活注入块（内部参考）：此刻房间 + 目光所及 + 按需展开容器/距离。"""
    try:
        import memory.stats as _st
        _st.bump("tick:living")
    except Exception as e:
        _stats_err(e)
    if not _cfg("enabled", True):
        return ""
    now = now or datetime.now()
    ldata = _load()  # 单次加载，容器循环复用（P0 性能优化）
    try:
        from memory import space as space_mod
    except Exception as e:
        _stats_err(e)
        space_mod = None
    pos = space_mod.position(now) if space_mod else {}
    parts = []
    if pos.get("state") == "在途中":
        parts.append(f"【此刻】在去{pos.get('to', '某处')}的路上（{pos.get('mode', '')}）")
    try:
        rpos = space_mod.room_position(now) if space_mod else {}
        if rpos.get("state") == "在途中":
            path = "、".join(rpos.get("path") or [])
            parts.append(f"【此刻】正从{rpos.get('from', '')}走向{rpos.get('to', '')}" + (f"（路过{path}）" if path else ""))
    except Exception as e:
        _stats_err(e)
        pass
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
            items = lookup(container, data=ldata)
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
    try:
        if s := where_is_block(scope, t, now):
            parts.append(s)
    except Exception as e:
        _stats_err(e)
        pass
    try:
        if any(w in t for w in _CANCEL_WORDS) and cancel_search(scope):
            parts.append("【找东西·取消】用户说别找了，别再继续翻找了，自然带一句'行，不找了'即可，别提系统。")
    except Exception as e:
        _stats_err(e)
        pass
    try:
        from memory import subjects
        if any(w in t for w in ("在哪", "找", "见过", "有没有", "记得")):
            for nm in subjects.detect(t)[:1]:
                if s := ask_npc(nm, t):
                    parts.append(s)
    except Exception as e:
        _stats_err(e)
        pass
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
    except Exception as e:
        _stats_err(e)
        pass
    try:
        if space_mod and (s := space_mod.cast_block(t, now)):
            parts.append(s)
    except Exception as e:
        _stats_err(e)
        pass
    try:
        if space_mod and (s := space_mod.actions_block(scope)):
            parts.append(s)
    except Exception as e:
        _stats_err(e)
        pass
    parts.append("内部参考：别主动报生活细节，被问起或相关时自然带一句，不要生硬播报")
    return "；".join(parts)

# ===== AI 生日与年龄（v31.3）=====
def ai_birthday():
    """AI 生日 (月, 日)，config → memory.core.living.birthday，格式 MM-DD。"""
    b = str(_pack_behavior().get("birthday", _cfg("birthday", "")) or "").strip()
    if not b:
        return None
    try:
        m, d = b.split("-")
        return int(m), int(d)
    except Exception as e:
        _stats_err(e)
        return None


def ai_age(now=None):
    """AI 年龄 = 当前年 - birth_year（config）。"""
    now = now or datetime.now()
    by = _pack_behavior().get("birth_year", _cfg("birth_year", None))
    if not by:
        return None
    try:
        return max(0, now.year - int(by))
    except Exception as e:
        _stats_err(e)
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
    if d <= 0 or d > int(_pack_behavior().get("birthday_hint_days", _cfg("birthday_hint_days", 7))):
        return ""
    try:
        from memory import interaction as interaction_mod
        if interaction_mod.familiarity_effective(scope) < float(
            _pack_behavior().get("birthday_threshold", _cfg("birthday_threshold", 0.4))
        ):
            return ""
    except Exception as e:
        _stats_err(e)
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
    except Exception as e:
        _stats_err(e)
        pass
    _db.kv_set("memory", f"birthday_celebrated:{year}", {"done": True, "detail": detail})
    return {"celebrated": True, "age": age, "detail": detail}



def _stats_err(e):
    """裸 except 审计（v2.2）：错误计数 + 日志，供消融/排查。"""
    try:
        import memory.stats as _st
        _st.bump_err("living", e)
    except Exception:
        pass
