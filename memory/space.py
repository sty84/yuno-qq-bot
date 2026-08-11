"""空间层（v31）：统一位置事实源 + 移动中间态 + 场所拓扑 + 空间事件流 + 视觉一致性。

- 位置状态机：location / state（在场/在途中）/ from→to / depart_ts / arrive_ts，kv 持久化；
- 懒演化：position(now) 被调用时按日程自动出发/到达（出发窗口 = 槽位开始 - 路程），
  同一时刻同一问法答案一致；
- 场所拓扑：travel_between 支持任意两点（家→X 走 living 路程，非家成对走 pair_times 或回退）；
- 空间事件流：emit(kind, detail) 统一落 kv，environment/sharing/sleep 消费；
- 视觉一致性：房间静态描述 + can_see（同房间 + 光线）。
"""

import re
from datetime import datetime, timedelta

from plugins import _db, _shared

ROOM_VISUALS = {
    "客厅": {"size": "不大", "tone": "暖色",
             "desc": "沙发靠着窗，茶几上乱七八糟堆着东西，电视柜上放着游戏机"},
    "工作室": {"size": "小", "tone": "偏暗",
              "desc": "打碟台占了大半面墙，耳机架挂满耳机，角落里堆着储物箱"},
    "卧室": {"size": "单人房", "tone": "粉色调",
             "desc": "床上有两个抱枕，床头柜放着眼药水，窗帘常年拉着"},
    "厨房": {"size": "窄", "tone": "白",
             "desc": "冰箱贴着便利贴，零食柜塞得满满当当"},
}

_ACTIVITY_PLACE = {
    "performance": "演出场地", "rehearsal": "排练室", "work": "公司",
    "shopping": "便利店", "exercise": "公园", "friend": "公园", "out_entertain": "外面",
    "study": "家", "compose": "家", "dj_practice": "家", "gaming": "家",
    "home_entertain": "家", "home_rest": "家", "sleep": "家",
}

_MEMORABLE_PLACES = ("演出场地", "排练室", "录音室", "公司")

_HOME_EDGES_DEFAULT = [("客厅", "厨房"), ("客厅", "卧室"), ("卧室", "工作室"), ("厨房", "工作室")]
_DOORS_DEFAULT = {"客厅门": "开", "厨房门": "开", "卧室门": "关", "工作室门": "开"}
_MEMORY_EMIT_KINDS = ("depart", "arrive", "enter_room", "depart_room", "item_move", "item_find", "item_lost", "birthday")


def _cfg(key, default):
    sp = (_shared.CONFIG.get("memory", {}).get("core", {}) or {}).get("space", {}) or {}
    return sp.get(key, default)


def _pack_world() -> dict:
    try:
        from memory import pack
        return pack.world()
    except Exception:
        return {}


def _memorable_places() -> tuple:
    m = _pack_world().get("memorable_places")
    return tuple(m) if isinstance(m, list) and m else _MEMORABLE_PLACES


def _activity_place_map() -> dict:
    ap = dict(_ACTIVITY_PLACE)
    ap.update(_pack_world().get("activity_place") or {})
    return ap


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


# ===== 家内房间图（P1-1：邻接 / 最短路径 / 门灯）=====
def home_edges() -> list:
    w = _pack_world().get("edges")
    if isinstance(w, list) and w:
        return w
    e = _cfg("home_edges", None)
    return e if isinstance(e, list) and e else _HOME_EDGES_DEFAULT


def rooms_adjacent(a, b) -> bool:
    if not a or not b or a == b:
        return False
    for x, y in home_edges():
        if (a == x and b == y) or (a == y and b == x):
            return True
    return False


def shortest_route(a, b) -> list:
    """房间间最短路径（BFS，按 home_edges）。"""
    if a == b:
        return [a]
    adj = {}
    for x, y in home_edges():
        adj.setdefault(x, []).append(y)
        adj.setdefault(y, []).append(x)
    seen, queue = {a}, [[a]]
    while queue:
        path = queue.pop(0)
        for nxt in adj.get(path[-1], []):
            if nxt in seen:
                continue
            p = path + [nxt]
            if nxt == b:
                return p
            seen.add(nxt)
            queue.append(p)
    return []


def route_minutes(a, b) -> int:
    """房间间移动分钟数（每边默认 1 分钟，可配 space.edge_min）。"""
    route = shortest_route(a, b)
    if not route:
        return 0
    return max(0, len(route) - 1) * int(_cfg("edge_min", 1))


def _doors_data() -> dict:
    d = _db.kv_get("memory", "space_doors") or {}
    if not d.get("doors"):
        doors = _pack_world().get("doors") or _DOORS_DEFAULT
        d = {"doors": dict(doors)}
        _db.kv_set("memory", "space_doors", d)
    return d


def door_open(room) -> bool:
    """该房间门是否开着：优先 sensors 设备状态，回退 space_doors 配置。"""
    name = f"{room}门"
    try:
        from memory import sensors as sensors_mod
        s = sensors_mod.device_state(name)
        if s.get("state") in ("开", "关"):
            return s["state"] == "开"
    except Exception as e:
        _stats_err(e)
        pass
    d = _doors_data().get("doors") or {}
    return str(d.get(name, "开")) == "开"


def set_door(room, state):
    d = _doors_data()
    d.setdefault("doors", {})[f"{room}门"] = str(state)
    _db.kv_set("memory", "space_doors", d)
    return {"ok": True, "room": room, "state": state}


def light_on(room) -> bool:
    """该房间灯是否亮着（读 sensors 设备状态；异常时视为亮）。"""
    try:
        from memory import sensors as sensors_mod
        return sensors_mod.device_state(f"{room}灯").get("state") == "开"
    except Exception as e:
        _stats_err(e)
        return True


# ===== 队友确定性位置（P2：周表，替代随机人物桶）=====
def cast_schedule() -> dict:
    w = _pack_world().get("cast_schedule")
    if isinstance(w, dict) and w:
        return w
    return _cfg("cast_schedule", {}) or {}


def cast_location(name, now=None) -> dict:
    """队友此刻位置：命中周表时段 → 地点；否则回退 default_place。"""
    now = now or datetime.now()
    cs = cast_schedule()
    entry = cs.get(str(name or ""))
    if not entry:
        return {"name": name, "place": "", "known": False}
    default_place = str(entry.get("default_place", ""))
    for slot in (entry.get("week") or []):
        days = slot.get("days") or []
        if now.weekday() not in days:
            continue
        try:
            start, end = int(slot.get("start_hour", 0)), int(slot.get("end_hour", 24))
        except (TypeError, ValueError):
            start, end = 0, 24
        if start <= now.hour < end:
            return {
                "name": name, "place": str(slot.get("place", default_place)),
                "known": True, "since": start, "until": end,
            }
    return {"name": name, "place": default_place, "known": bool(default_place)}


def cast_block(text, now=None) -> str:
    """用户问队友在哪 → 注入【队友位置】确定性答案。"""
    t = str(text or "")
    if not any(w in t for w in ("在哪", "哪里", "在不在", "谁在")):
        return ""
    try:
        env_cfg = (_shared.CONFIG.get("memory", {}).get("core", {}) or {}).get("environment", {}) or {}
        cast = env_cfg.get("cast") or []
    except Exception as e:
        _stats_err(e)
        cast = []
    out = []
    for name in cast:
        if name in t or any(name[i:i + 2] in t for i in range(max(0, len(name) - 1))):
            loc = cast_location(name, now)
            if loc.get("known"):
                out.append(f"{name}：{loc['place']}")
    if not out:
        return ""
    return "【队友位置】" + "；".join(out) + "（内部参考：被问起时按此回答）"


# ===== 家内房间移动状态机（P1-1：真实移动）=====
def _room_state() -> dict:
    st = _db.space_state_get()
    if not st:
        return {
        "room": "客厅", "state": "在场", "from": "", "to": "",
        "path": [], "depart_ts": "", "arrive_ts": "",
    }
    st["from"] = st.pop("from_room", "")
    st["to"] = st.pop("to_room", "")
    return st


def _set_room_state(st):
    st = dict(st)
    st["updated_ts"] = _now_iso()
    _db.space_state_set(st)


def move_room(to, now=None):
    """家内真实移动：沿房间图走过去，到点自动到达（懒演化）。
    已在目标房间 → 短延迟'到达'（供打开容器/查看用）。"""
    now = now or datetime.now()
    if not to:
        return {}
    st = _room_state()
    cur = str(st.get("room", "客厅"))
    if cur == to:
        return {"ok": True, "room": to,
                "arrive_ts": (now + timedelta(seconds=6)).isoformat(timespec="seconds"),
                "moved": False}
    route = shortest_route(cur, to)
    if not route:
        return {"ok": False, "reason": f"{cur}到{to}没有通路"}
    minutes = max(1, route_minutes(cur, to))
    st.update({
        "room": cur, "state": "在途中", "from": cur, "to": to,
        "path": route[1:], "depart_ts": _now_iso(),
        "arrive_ts": (now + timedelta(minutes=minutes)).isoformat(timespec="seconds"),
    })
    _set_room_state(st)
    emit("depart_room", f"从{cur}走向{to}", location=cur)
    remember("", f"从{cur}走向{to}")
    return {"ok": True, "room": cur, "to": to, "minutes": minutes,
            "arrive_ts": st["arrive_ts"], "moved": True}


def room_position(now=None) -> dict:
    """当前家内房间（懒演化）：在途中到点自动到达。"""
    now = now or datetime.now()
    st = _room_state()
    if st.get("state") == "在途中":
        arrive_ts = _parse(st.get("arrive_ts"))
        if arrive_ts and now >= arrive_ts:
            to = str(st.get("to", "客厅"))
            st.update({"room": to, "state": "在场", "from": "", "to": "", "path": []})
            _set_room_state(st)
            emit("enter_room", f"走进了{to}", location=to)
            remember("", f"走进了{to}")
            return st
    return st


def room_now(now=None) -> str:
    """当前所在房间名（在场时）；在途中返回 ''。"""
    try:
        st = room_position(now)
        if st.get("state") != "在途中":
            return str(st.get("room", ""))
    except Exception as e:
        _stats_err(e)
        pass
    return ""


def _get_pos() -> dict:
    return _db.kv_get("memory", "space_position") or {}


def _set_pos(pos):
    pos = dict(pos)
    pos["updated_ts"] = _now_iso()
    _db.kv_set("memory", "space_position", pos)


def _home_pos(now):
    return {"location": "家", "state": "在场", "from": "", "to": "", "mode": "",
            "depart_ts": "", "arrive_ts": "", "updated_ts": _now_iso()}


def _slot_start_hour(now) -> int:
    try:
        from memory import schedule as schedule_mod
        return {0: 6, 1: 12, 2: 18, 3: 22}[schedule_mod.slot_index(now.hour)]
    except Exception as e:
        _stats_err(e)
        return 6


def _planned_place(now) -> str:
    try:
        from memory import schedule as schedule_mod
        cur = schedule_mod.current_activity(now)
        act = cur.get("activity") if cur else ""
    except Exception as e:
        _stats_err(e)
        act = ""
    return _activity_place_map().get(act, "家")


def _next_slot_start(now) -> datetime:
    try:
        from memory import schedule as schedule_mod
        slot = schedule_mod.slot_index(now.hour)
        start = now.replace(hour=_slot_start_hour(now), minute=0, second=0, microsecond=0)
        if start > now:
            start -= timedelta(days=1)
        return start + timedelta(hours={0: 6, 1: 6, 2: 4, 3: 8}[slot])
    except Exception as e:
        _stats_err(e)
        return now + timedelta(hours=1)


def _travel_minutes(place, now) -> int:
    try:
        from memory import living as living_mod
        r = living_mod.travel_time(place, now=now)
        return max(1, int(r.get("minutes", 30)))
    except Exception as e:
        _stats_err(e)
        return 30


def _plan_and_start(now):
    """当前/下一个要去的地方 + 对应槽位开始时间（出发窗口 = 下一槽开始 - 路程）。"""
    start = now.replace(hour=_slot_start_hour(now), minute=0, second=0, microsecond=0)
    if start > now:
        start -= timedelta(days=1)
    plan = _planned_place(now)
    next_start = _next_slot_start(now)
    next_plan = _planned_place(next_start)
    if next_plan != "家":
        travel = _travel_minutes(next_plan, now)
        if now >= next_start - timedelta(minutes=travel) and now < next_start:
            return next_plan, next_start
    return plan, start


def _parse(ts):
    try:
        return datetime.fromisoformat(str(ts))
    except Exception as e:
        _stats_err(e)
        return None


# ===== 空间事件流 =====
def _events() -> dict:
    return {"rows": _db.space_event_rows(limit=500)}


def _loc_from_detail(kind, detail):
    d = str(detail or "")
    for pre in ("到了", "出发去", "回到家", "走进"):
        if d.startswith(pre):
            return d[len(pre):].strip()
    m = re.search(r"放到了(.+?)的", d)
    return m.group(1).strip() if m else ""


def emit(kind, detail, memorable=False, location=""):
    """写一条空间事件；记忆类 kind（出发/到达/进房间/物品移动/丢失/生日）顺带写
    ai:episodic 记忆（带地点标签，P0-2），可被 7 路检索召回。"""
    _db.space_event_add(_now_iso(), kind, detail, bool(memorable))
    if memorable:
        # 保留原 ai:experience 写入（兼容依赖 experience 的模块）
        try:
            _db.memory_add(
                "ai", "experience", str(detail)[:60],
                _now_iso(), None, confidence=0.6, source="space",
                mclass="short", audience="public", speaker="ai",
            )
            from memory import policy as policy_mod
            policy_mod.touch("ai", "experience", str(detail)[:60], importance=0.5)
        except Exception as e:
            _stats_err(e)
            pass
    if kind in _MEMORY_EMIT_KINDS:
        try:
            loc = location or _loc_from_detail(kind, detail)
            fact = f"[地点：{loc}] {detail}" if loc else str(detail)[:60]
            _db.memory_add(
                "ai", "episodic", fact[:120],
                _now_iso(), None, confidence=0.5, source="space",
                mclass="episodic", audience="public", speaker="ai",
            )
            from memory import policy as policy_mod
            policy_mod.touch("ai", "episodic", fact[:120], importance=0.4)
            # 写完即建索引（P0 优化：空间记忆可被检索，不必等 grow）
            try:
                from memory import lexical as lexical_mod
                lexical_mod.bm25_upsert("ai", "episodic", [fact[:120]])
                _db.lexicon_sync("ai", "episodic")
            except Exception as e:
                _stats_err(e)
                pass
            # 向量索引增量写入（最近质心归属；无质心时等 grow build 全量重建）
            try:
                from memory import embedder, vecindex
                if embedder.enabled():
                    vecs = embedder.embed([fact[:120]])
                    if vecs:
                        vecindex.upsert("ai", "episodic", fact[:120], vecs[0])
            except Exception as e:
                _stats_err(e)
                pass
        except Exception as e:
            _stats_err(e)
            pass


def today_events(now=None) -> list:
    d = (now or datetime.now()).date().isoformat()
    return [r for r in (_events().get("rows") or []) if str(r.get("ts", "")).startswith(d)]


def prune_events(days=7):
    return _db.space_event_prune(days)


# ===== 位置状态机 =====
def position(now=None) -> dict:
    """当前位置（懒演化）：被调用时按日程自动出发/到达。返回 {location, state, detail, ...}。"""
    try:
        import memory.stats as _st
        _st.bump("tick:space")
    except Exception as e:
        _stats_err(e)
    if not _cfg("enabled", True):
        return {"location": "家", "state": "在场", "detail": ""}
    now = now or datetime.now()
    pos = _get_pos()
    plan, start = _plan_and_start(now)

    # 在途中：到点自动到达
    if pos.get("state") == "在途中":
        depart_ts = _parse(pos.get("depart_ts"))
        if depart_ts and now < depart_ts:
            # 出发时间在未来（时钟回拨/测试倒拨）→ 还没出发
            npos = _home_pos(now)
            _set_pos(npos)
            return npos
        arrive_ts = _parse(pos.get("arrive_ts"))
        if arrive_ts and now >= arrive_ts:
            loc = str(pos.get("to") or plan)
            npos = _home_pos(now)
            npos.update({"location": loc, "state": "在场",
                         "arrive_ts": pos.get("arrive_ts") or _now_iso()})
            _set_pos(npos)
            emit("arrive", f"到了{loc}", memorable=loc in _memorable_places())
            remember("", f"到了{loc}")
            return npos
        return pos

    loc = str(pos.get("location") or "家")
    if plan == "家":
        if loc != "家":
            _set_pos(_home_pos(now))
            emit("arrive", "回到家")
            remember("", "回到家")
        return _home_pos(now)
    if loc == plan and pos.get("state") == "在场":
        arrive_ts = _parse(pos.get("arrive_ts"))
        if not arrive_ts or arrive_ts <= now:
            return pos
        # 到达时间在未来 → 状态过期（时钟回拨/测试倒拨），重新按出发窗口推算

    # 日程换了地方：按"槽位开始 - 路程"推算出发/到达
    travel = _travel_minutes(plan, now)
    depart_by = start - timedelta(minutes=travel)
    if now < depart_by:
        # 还没到出发窗口：还在家
        return _home_pos(now) if loc != "家" or not pos else pos
    if now < start:
        try:
            from memory import living as living_mod
            mode = living_mod.travel_time(plan, now=now).get("mode", "")
        except Exception as e:
            _stats_err(e)
            mode = ""
        npos = _home_pos(now)
        npos.update({"location": "家", "state": "在途中", "to": plan,
                     "mode": mode, "depart_ts": _now_iso(),
                     "arrive_ts": start.isoformat(timespec="seconds")})
        _set_pos(npos)
        emit("depart", f"出发去{plan}", memorable=plan in _memorable_places())
        remember("", f"出发去{plan}")
        return npos
    # 已经在目的地的槽位时段内：按槽位开始到达
    npos = _home_pos(now)
    npos.update({"location": plan, "state": "在场",
                 "arrive_ts": start.isoformat(timespec="seconds")})
    _set_pos(npos)
    emit("arrive", f"到了{plan}", memorable=plan in _memorable_places())
    remember("", f"到了{plan}")
    return npos


def depart(to, mode=None, now=None):
    """显式出发（供未来指令/剧情用）。"""
    now = now or datetime.now()
    try:
        from memory import living as living_mod
        r = living_mod.travel_time(to, mode, now)
        minutes = int(r.get("minutes", 30))
        m = r.get("mode", mode or "")
    except Exception as e:
        _stats_err(e)
        minutes, m = 30, mode or ""
    npos = _home_pos(now)
    npos.update({"location": "家", "state": "在途中", "to": to, "mode": m,
                 "depart_ts": _now_iso(),
                 "arrive_ts": (now + timedelta(minutes=minutes)).isoformat(timespec="seconds")})
    _set_pos(npos)
    emit("depart", f"出发去{to}", memorable=to in _memorable_places())
    remember("", f"出发去{to}")
    return npos


# ===== AI 行为记忆流（v32，斯坦福式：只追加，供注入/回忆）=====
def remember(scope, action, detail=""):
    """记录 AI 自己的一次行动（去过哪/做了什么）。"""
    try:
        _db.ai_action_add(_now_iso(), str(scope or ""), str(action)[:40], str(detail)[:80])
    except Exception as e:
        _stats_err(e)
        pass


def recent_actions(scope="", hours=24) -> list:
    out = []
    now = datetime.now()
    for it in _db.ai_action_rows(scope or "", limit=60):
        if scope and it.get("scope") != scope:
            continue
        try:
            ts = datetime.fromisoformat(it["ts"])
        except Exception as e:
            _stats_err(e)
            continue
        if now - ts <= timedelta(hours=hours):
            out.append(it)
    return out


def actions_block(scope, hours=24, limit=3) -> str:
    """注入：AI 近期的空间/物品行动，防'你刚才不是去过吗'穿帮。"""
    acts = recent_actions(scope, hours)
    if not acts:
        return ""
    lines = [f"{a.get('action', '')}（{a.get('detail', '')}）" for a in acts[-limit:]]
    return "【近期行动】" + "；".join(lines) + "（内部参考：被问起时自然带一句，别主动播报）"


# ===== 场所拓扑 =====
def travel_between(frm, to, mode=None, now=None) -> dict:
    """任意两点耗时：家→X 用 living 路程；非家成对走 pair_times 或回退（以目的地方向为准）。"""
    now = now or datetime.now()
    if frm in ("", "家", "home"):
        try:
            from memory import living as living_mod
            return living_mod.travel_time(to, mode, now)
        except Exception as e:
            return {"ok": False, "reason": f"living 不可用：{e}"}
    pair = dict(_pack_world().get("pair_times") or {})
    pair.update(_cfg("pair_times", {}) or {})
    key, rkey = f"{frm}:{to}", f"{to}:{frm}"
    if key in pair:
        return {"ok": True, "place": to, "mode": mode or "默认", "minutes": int(pair[key]),
                "base": int(pair[key]), "factors": ["成对路程表"]}
    if rkey in pair:
        return {"ok": True, "place": to, "mode": mode or "默认", "minutes": int(pair[rkey]),
                "base": int(pair[rkey]), "factors": ["成对路程表（反向）"]}
    try:
        from memory import living as living_mod
        r = living_mod.travel_time(to, mode, now)
        r["factors"] = list(r.get("factors", [])) + ["无成对数据，按目的地方向估算"]
        return r
    except Exception as e:
        _stats_err(e)
        return {"ok": False, "reason": "无路程数据"}


# ===== 视觉一致性 =====
def room_visual(room) -> dict:
    w = _pack_world().get("visuals") or {}
    return w.get(room) or ROOM_VISUALS.get(room, {"size": "", "tone": "", "desc": ""})


def can_see(room, container, now=None) -> dict:
    """从某房间能否看见某容器：同房间 + 光线；或相邻房间 + 门开 + 灯亮（P1-1）。"""
    now = now or datetime.now()
    try:
        from memory import living as living_mod
        croom = living_mod.container_room(container)
    except Exception as e:
        _stats_err(e)
        croom = ""
    if not croom:
        return {"visible": False, "dim": False, "reason": f"{container}不在家里"}
    if croom != room:
        if not rooms_adjacent(room, croom):
            return {"visible": False, "dim": False, "reason": f"{container}在{croom}，不在{room}"}
        if not door_open(croom):
            return {"visible": False, "dim": False, "reason": f"{croom}的门关着"}
        if not light_on(croom):
            return {"visible": False, "dim": True, "reason": f"{croom}没开灯，看不太清"}
    dim = False
    try:
        from memory import weather as weather_mod
        w = weather_mod.fetch(now)
        if "暗" in str(w.get("light", "")):
            dim = True
    except Exception as e:
        _stats_err(e)
        pass
    if dim and container in ("储物箱", "床头柜"):
        return {"visible": False, "dim": True, "reason": "光线太暗，看不太清"}
    return {"visible": True, "dim": dim, "reason": "", "room": croom, "adjacent": croom != room}



def _stats_err(e):
    """裸 except 审计（v2.2）：错误计数 + 日志，供消融/排查。"""
    try:
        import memory.stats as _st
        _st.bump_err("space", e)
    except Exception:
        pass
