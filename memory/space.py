"""空间层（v31）：统一位置事实源 + 移动中间态 + 场所拓扑 + 空间事件流 + 视觉一致性。

- 位置状态机：location / state（在场/在途中）/ from→to / depart_ts / arrive_ts，kv 持久化；
- 懒演化：position(now) 被调用时按日程自动出发/到达（出发窗口 = 槽位开始 - 路程），
  同一时刻同一问法答案一致；
- 场所拓扑：travel_between 支持任意两点（家→X 走 living 路程，非家成对走 pair_times 或回退）；
- 空间事件流：emit(kind, detail) 统一落 kv，environment/sharing/sleep 消费；
- 视觉一致性：房间静态描述 + can_see（同房间 + 光线）。
"""

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


def _cfg(key, default):
    sp = (_shared.CONFIG.get("memory", {}).get("core", {}) or {}).get("space", {}) or {}
    return sp.get(key, default)


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


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
    except Exception:
        return 6


def _planned_place(now) -> str:
    try:
        from memory import schedule as schedule_mod
        cur = schedule_mod.current_activity(now)
        act = cur.get("activity") if cur else ""
    except Exception:
        act = ""
    return _ACTIVITY_PLACE.get(act, "家")


def _next_slot_start(now) -> datetime:
    try:
        from memory import schedule as schedule_mod
        slot = schedule_mod.slot_index(now.hour)
        start = now.replace(hour=_slot_start_hour(now), minute=0, second=0, microsecond=0)
        if start > now:
            start -= timedelta(days=1)
        return start + timedelta(hours={0: 6, 1: 6, 2: 4, 3: 8}[slot])
    except Exception:
        return now + timedelta(hours=1)


def _travel_minutes(place, now) -> int:
    try:
        from memory import living as living_mod
        r = living_mod.travel_time(place, now=now)
        return max(1, int(r.get("minutes", 30)))
    except Exception:
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
    except Exception:
        return None


# ===== 空间事件流 =====
def _events() -> dict:
    return _db.kv_get("memory", "space_events") or {"rows": []}


def emit(kind, detail, memorable=False):
    """写一条空间事件；memorable=True 的（到达演出/排练等）顺带写 ai:experience。"""
    data = _events()
    rows = data.get("rows") or []
    rows.append({"ts": _now_iso(), "kind": kind, "detail": str(detail)[:80], "memorable": bool(memorable)})
    _db.kv_set("memory", "space_events", {"rows": rows[-200:]})
    if memorable:
        try:
            _db.memory_add(
                "ai", "experience", str(detail)[:60],
                _now_iso(), None, confidence=0.6, source="space",
                mclass="short", audience="public", speaker="ai",
            )
            from memory import policy as policy_mod
            policy_mod.touch("ai", "experience", str(detail)[:60], importance=0.5)
        except Exception:
            pass


def today_events(now=None) -> list:
    d = (now or datetime.now()).date().isoformat()
    return [r for r in (_events().get("rows") or []) if str(r.get("ts", "")).startswith(d)]


def prune_events(days=7):
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    data = _events()
    rows = [r for r in (data.get("rows") or []) if str(r.get("ts", "")) >= cutoff]
    _db.kv_set("memory", "space_events", {"rows": rows})
    return len(rows)


# ===== 位置状态机 =====
def position(now=None) -> dict:
    """当前位置（懒演化）：被调用时按日程自动出发/到达。返回 {location, state, detail, ...}。"""
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
            emit("arrive", f"到了{loc}", memorable=loc in _MEMORABLE_PLACES)
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
        except Exception:
            mode = ""
        npos = _home_pos(now)
        npos.update({"location": "家", "state": "在途中", "to": plan,
                     "mode": mode, "depart_ts": _now_iso(),
                     "arrive_ts": start.isoformat(timespec="seconds")})
        _set_pos(npos)
        emit("depart", f"出发去{plan}", memorable=plan in _MEMORABLE_PLACES)
        remember("", f"出发去{plan}")
        return npos
    # 已经在目的地的槽位时段内：按槽位开始到达
    npos = _home_pos(now)
    npos.update({"location": plan, "state": "在场",
                 "arrive_ts": start.isoformat(timespec="seconds")})
    _set_pos(npos)
    emit("arrive", f"到了{plan}", memorable=plan in _MEMORABLE_PLACES)
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
    except Exception:
        minutes, m = 30, mode or ""
    npos = _home_pos(now)
    npos.update({"location": "家", "state": "在途中", "to": to, "mode": m,
                 "depart_ts": _now_iso(),
                 "arrive_ts": (now + timedelta(minutes=minutes)).isoformat(timespec="seconds")})
    _set_pos(npos)
    emit("depart", f"出发去{to}", memorable=to in _MEMORABLE_PLACES)
    remember("", f"出发去{to}")
    return npos


# ===== AI 行为记忆流（v32，斯坦福式：只追加，供注入/回忆）=====
def remember(scope, action, detail=""):
    """记录 AI 自己的一次行动（去过哪/做了什么）。"""
    now = _now_iso()
    d = _db.kv_get("memory", "ai_actions") or {"items": []}
    items = list(d.get("items") or [])
    items.append({"ts": now, "scope": str(scope or ""), "action": str(action)[:40], "detail": str(detail)[:80]})
    d["items"] = items[-60:]
    _db.kv_set("memory", "ai_actions", d)


def recent_actions(scope="", hours=24) -> list:
    d = _db.kv_get("memory", "ai_actions") or {"items": []}
    out = []
    now = datetime.now()
    for it in (d.get("items") or []):
        if scope and it.get("scope") != scope:
            continue
        try:
            ts = datetime.fromisoformat(it["ts"])
        except Exception:
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
    pair = _cfg("pair_times", {}) or {}
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
    except Exception:
        return {"ok": False, "reason": "无路程数据"}


# ===== 视觉一致性 =====
def room_visual(room) -> dict:
    return ROOM_VISUALS.get(room, {"size": "", "tone": "", "desc": ""})


def can_see(room, container, now=None) -> dict:
    """从某房间能否看见某容器内容：必须同房间 + 光线足够。"""
    now = now or datetime.now()
    try:
        from memory import living as living_mod
        layout = living_mod.home_layout()
        furniture = (layout.get(room) or {}).get("furniture", [])
    except Exception:
        furniture = []
    if container not in furniture:
        return {"visible": False, "dim": False, "reason": f"{container}不在{room}里"}
    dim = False
    try:
        from memory import weather as weather_mod
        w = weather_mod.fetch(now)
        if "暗" in str(w.get("light", "")):
            dim = True
    except Exception:
        pass
    if dim and container in ("储物箱", "床头柜"):
        return {"visible": False, "dim": True, "reason": "光线太暗，看不太清"}
    return {"visible": True, "dim": dim, "reason": ""}
