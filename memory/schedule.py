"""AI 日程表（v31）：人设驱动的"生活层"。

- 活动注册表：工作/学习/宅家休息/宅家娱乐/外出娱乐/运动/和朋友玩/排练/演出/作曲/打碟…
- 人设档案：不同人格有不同周模板（上班族 vs 夜行演出型等，随 Persona Pack 变化）。
- 种子化周生成：seed = hash(人设ID + 自然周) → 同一周内结果稳定（不会一天一个样），
  下周种子变化才有新变化；演出前合练、演出后恢复这类状态链保证连续性。
- 运行时：按当前时间查"此刻状态"，注入 prompt（内部参考，不主动播报）。
- 联动：用户约定（appointment）优先于默认日程；情绪/能耗提示；深睡"今日回忆"混入日程。
"""

import hashlib
import random
from datetime import date, datetime, timedelta

from plugins import _db, _shared

# 一天 4 个槽：早晨 / 下午 / 傍晚 / 夜晚（22:00-次日06:00 归当天夜晚）
SLOTS = [
    ("morning", 6, 12),
    ("afternoon", 12, 18),
    ("evening", 18, 22),
    ("night", 22, 30),
]

ACTIVITIES = {
    "work": {"label": "上班", "energy": 0.6, "home": False, "social": 0.2},
    "study": {"label": "学习", "energy": 0.5, "home": True, "social": 0.0},
    "rehearsal": {"label": "乐队排练", "energy": 0.8, "home": False, "social": 0.8},
    "performance": {"label": "演出", "energy": 1.0, "home": False, "social": 1.0},
    "compose": {"label": "作曲/写歌", "energy": 0.4, "home": True, "social": 0.0},
    "dj_practice": {"label": "打碟练习", "energy": 0.4, "home": True, "social": 0.0},
    "home_entertain": {"label": "宅家娱乐", "energy": -0.2, "home": True, "social": 0.0},
    "gaming": {"label": "打游戏/看漫画", "energy": -0.2, "home": True, "social": 0.0},
    "home_rest": {"label": "宅家休息", "energy": -0.4, "home": True, "social": 0.0},
    "sleep": {"label": "睡觉", "energy": -1.0, "home": True, "social": 0.0},
    "out_entertain": {"label": "外出娱乐", "energy": 0.5, "home": False, "social": 0.6},
    "exercise": {"label": "运动", "energy": 0.7, "home": False, "social": 0.3},
    "friend": {"label": "和朋友出去玩", "energy": 0.6, "home": False, "social": 1.0},
    "shopping": {"label": "外出采购", "energy": 0.3, "home": False, "social": 0.2},
    "idle": {"label": "空闲", "energy": -0.1, "home": True, "social": 0.0},
}

# ===== 人设档案 =====
PROFILES = {
    # 示例人设：夜行型、节能、乐队成员——排练/演出/宅家为主，出门少（由 Persona Pack 提供）
    "yuno": {
        "seed_id": "yuno",
        "nocturnal": True,
        "fixed": {
            0: {0: "sleep", 1: "home_rest", 2: "compose", 3: "dj_practice"},       # 周一
            1: {0: "sleep", 1: "home_rest", 2: "rehearsal", 3: "home_rest"},       # 周二晚排练
            2: {0: "sleep", 1: None, 2: "home_entertain", 3: None},                # 周三
            3: {0: "sleep", 1: "home_rest", 2: "gaming", 3: None},                 # 周四
            4: {0: "sleep", 1: "home_rest", 2: "rehearsal", 3: "home_rest"},       # 周五合练
            5: {0: "sleep", 1: None, 2: None, 3: None},                            # 周六（演出概率）
            6: {0: "home_rest", 1: "home_rest", 2: "home_entertain", 3: "compose"},  # 周日恢复
        },
        "pool": {
            "compose": 3.0, "dj_practice": 3.0, "gaming": 2.0, "home_entertain": 2.0,
            "home_rest": 1.5, "friend": 0.8, "shopping": 0.6, "out_entertain": 0.5,
            "study": 0.4, "exercise": 0.2,
        },
        "slot_pool": {
            2: {1: {"compose": 2.0, "study": 1.0, "home_entertain": 1.5},
                3: {"dj_practice": 2.0, "gaming": 1.5}},
            3: {3: {"home_entertain": 2.0, "dj_practice": 1.5}},
            5: {1: {"rehearsal": 1.0, "home_rest": 1.0, "shopping": 0.8},
                2: {"rehearsal": 1.2, "dj_practice": 1.0, "performance": 1.0, "home_rest": 0.5},
                3: {"dj_practice": 1.0, "home_rest": 1.0}},
        },
    },
    # 普通上班族：周一~五上班，周末自由
    "office": {
        "seed_id": "office",
        "nocturnal": False,
        "fixed": {
            0: {0: "work", 1: "work", 2: None, 3: "sleep"},
            1: {0: "work", 1: "work", 2: None, 3: "sleep"},
            2: {0: "work", 1: "work", 2: None, 3: "sleep"},
            3: {0: "work", 1: "work", 2: None, 3: "sleep"},
            4: {0: "work", 1: "work", 2: None, 3: "sleep"},
            5: {0: "sleep", 1: None, 2: None, 3: None},
            6: {0: "sleep", 1: None, 2: None, 3: None},
        },
        "pool": {
            "home_entertain": 2.0, "gaming": 1.5, "exercise": 1.2, "friend": 1.0,
            "out_entertain": 1.0, "home_rest": 1.0, "study": 0.8, "shopping": 0.6,
        },
        "slot_pool": {},
    },
}


def _cfg(key, default):
    return _shared.core_cfg("schedule", key, default)
def profile_id() -> str:
    return str(_cfg("profile", "yuno")).strip() or "yuno"


def profile() -> dict:
    prof = PROFILES.get(profile_id(), PROFILES["yuno"])
    try:
        from memory import pack
        ps = pack.schedule()
        if ps:
            base_key = str(ps.get("profile") or profile_id())
            prof = PROFILES.get(base_key, PROFILES["yuno"])
            merged = dict(prof)
            for k in ("fixed", "pool", "slot_pool", "state_chains", "nocturnal", "activities"):
                if k in ps:
                    merged[k] = ps[k]
            if ps.get("profile"):
                merged["seed_id"] = str(ps["profile"])
            if "fixed" in merged:
                merged["fixed"] = {
                    int(k): {int(s): v for s, v in (v or {}).items()}
                    for k, v in merged["fixed"].items()  # type: ignore[attr-defined]
                }
            if "slot_pool" in merged:
                merged["slot_pool"] = {
                    int(k): {int(s): v for s, v in v.items()}
                    for k, v in merged["slot_pool"].items()  # type: ignore[attr-defined]
                }
            return merged
    except Exception:
        pass
    return prof


def slot_index(hour: int) -> int:
    """小时 → 槽位：早晨0 / 下午1 / 傍晚2 / 夜晚3（22:00-06:00）。"""
    if 6 <= hour < 12:
        return 0
    if 12 <= hour < 18:
        return 1
    if 18 <= hour < 22:
        return 2
    return 3


def _week_key(d=None) -> str:
    d = d or date.today()
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _week_seed(seed_id, week_key) -> int:
    return int(hashlib.sha256(f"{seed_id}:{week_key}".encode()).hexdigest()[:16], 16)


def generate_week(profile, week_key, rng=None) -> dict:
    """种子化生成一周计划：固定槽 + 权重填充 + 状态链（演出前合练、演出后恢复）。"""
    rng = rng or random.Random(_week_seed(profile.get("seed_id", "?"), week_key))
    pool = profile.get("pool") or {"home_rest": 1.0}
    slot_pool = profile.get("slot_pool") or {}
    plan, fixed_marks = {}, {}
    for wd in range(7):
        day = [None] * 4
        marks = [False] * 4
        for slot, act in (profile.get("fixed") or {}).get(wd, {}).items():
            day[slot] = act
            marks[slot] = True
        sp = slot_pool.get(wd, {})
        for slot in range(4):
            if day[slot] is not None:
                continue
            local = sp.get(slot)
            if local:
                acts = list(local.keys())
                wts = [float(local[a]) for a in acts]
            else:
                acts = list(pool.keys())
                wts = [float(pool[a]) for a in acts]
            # 夜晚槽（22:00–06:00）只能在家活动：正常人不会在外面呆到凌晨
            if slot == 3:
                home_acts = [a for a in acts if ACTIVITIES.get(a, {}).get("home", False)]
                if home_acts:
                    acts = home_acts
                    wts = [float(local[a]) if local else float(pool[a]) for a in acts]
                else:
                    acts, wts = ["home_rest"], [1.0]
            day[slot] = rng.choices(acts, weights=wts, k=1)[0]
        plan[wd] = day
        fixed_marks[wd] = marks
    # 能量预算：一天高能耗 → 把第一个非固定高能耗槽换成休息
    for wd in range(7):
        day_energy = sum(float(ACTIVITIES.get(a, {"energy": 0})["energy"]) for a in plan[wd])  # type: ignore[call-overload, misc]
        if day_energy > 1.6:
            for slot in range(4):
                if fixed_marks[wd][slot]:
                    continue
                act = plan[wd][slot]
                if act in ("rehearsal", "performance", "work", "exercise", "out_entertain", "friend"):
                    plan[wd][slot] = "home_rest"  # type: ignore[call-overload]
                    break
    # 状态链（Persona Pack 配置）：{"activity": {"before": "x", "after": "y"}}
    _CHAIN_ALIAS = {"rest": "home_rest", "workday": "work", "class": "study"}
    chains = profile.get("state_chains") or {}
    for wd in range(7):
        for slot in (2, 3):
            act = plan[wd][slot]
            chain = chains.get(act) or {}
            after = chain.get("after")
            if after:
                plan[(wd + 1) % 7][0] = _CHAIN_ALIAS.get(after, after)  # 活动后次日早晨
            before = chain.get("before")
            if before and slot == 2:
                prev = (wd - 1) % 7
                if plan[prev][2] in ("idle", "home_entertain", "gaming", "compose"):
                    plan[prev][2] = _CHAIN_ALIAS.get(before, before)
    return plan


def _plan_night_ok(plan) -> bool:
    """夜晚槽合法性：22:00–06:00 的活动必须 home=True（防止旧计划里有凌晨演出/外出）。"""
    if not plan:
        return True
    try:
        for wd in range(7):
            day = plan.get(wd) or []
            if len(day) < 4:
                return False
            act = day[3]
            if act is None:
                continue
            if not ACTIVITIES.get(act, {}).get("home", False):
                return False
    except Exception as e:
        _stats_err(e)
        return False
    return True


_plan_cache = {}  # type: ignore[var-annotated]


def get_week_plan() -> dict:
    """读取/生成当周计划（kv 持久化 + 进程内缓存，同一周稳定）。"""
    if not _cfg("enabled", True):
        return {}
    wk = _week_key()
    pid = profile_id()
    hit = _plan_cache.get(pid)
    if hit and hit[0] == wk:
        return hit[1]
    data = _db.kv_get("memory", "schedule_week") or {}  # type: ignore[attr-defined]
    if data.get("week") == wk and data.get("profile") == pid and data.get("plan"):
        # JSON 持久化后顶层键变成字符串，还原为 int（槽位索引仍为数组）
        plan = {int(k): v for k, v in data["plan"].items()}
        if not _plan_night_ok(plan):
            # 旧计划夜晚槽不合理（凌晨演出/外出）→ 重新生成
            plan = generate_week(profile(), wk)
            _db.kv_set("memory", "schedule_week", {"week": wk, "profile": pid, "plan": plan})  # type: ignore[attr-defined]
    else:
        plan = generate_week(profile(), wk)
        _db.kv_set("memory", "schedule_week", {"week": wk, "profile": pid, "plan": plan})  # type: ignore[attr-defined]
    _plan_cache[pid] = (wk, plan)
    return plan


def _slot_act(plan, now):
    """当前槽活动（含深夜拆分）：22:00–02:00 用夜晚槽活动（在家夜生活），
    02:00–06:00 强制睡觉——人设"凌晨 2 点后才睡"，2 点起就是睡眠延续。"""
    now = now or datetime.now()
    wd = now.weekday()
    if now.hour < 6:
        wd = (wd - 1) % 7  # 凌晨 0~6 点属于前一晚的夜晚槽（22:00 开始的延续）
    slot = slot_index(now.hour)
    act = plan[wd][slot]
    if 2 <= now.hour < 6:
        act = "sleep"
    return wd, slot, act


def current_activity(now=None) -> dict:
    """当前槽活动：{activity, label, weekday, slot, energy}。"""
    try:
        import memory.stats as _st
        _st.bump("tick:schedule")
    except Exception as e:
        _stats_err(e)
    plan = get_week_plan()
    if not plan:
        return {}
    now = now or datetime.now()
    wd, slot, act = _slot_act(plan, now)
    meta = ACTIVITIES.get(act, ACTIVITIES["idle"])
    return {
        "activity": act,
        "label": meta["label"],
        "weekday": wd,
        "slot": slot,
        "energy": float(meta["energy"]),  # type: ignore[arg-type]
        "home": bool(meta["home"]),
    }


def _next_activity(plan, wd, slot) -> str:
    for s in range(slot + 1, 4):
        a = plan[wd][s]
        if a not in ("sleep", "home_rest", "idle"):
            return ACTIVITIES.get(a, ACTIVITIES["idle"])["label"]  # type: ignore[return-value]
    for s in range(4):
        a = plan[(wd + 1) % 7][s]
        if a not in ("sleep", "home_rest", "idle"):
            return "明天" + ACTIVITIES.get(a, ACTIVITIES["idle"])["label"]  # type: ignore[operator]
    return ""


def _appointment_line(scope, now) -> str:
    """用户约定优先：今天/明天有待履约约定时覆盖默认日程提示。"""
    if not scope:
        return ""
    try:
        from memory import appointment as appt_mod
        appts = [a for a in appt_mod._appts() if a.get("scope") == scope and a.get("status") == "waiting"]
    except Exception as e:
        _stats_err(e)
        return ""
    for a in appts[:2]:
        try:
            due = datetime.fromisoformat(a["time"])
            if due.date() == now.date():
                return f"今天有约（{appt_mod._when_text(a)}），用户约定优先，别安排别的事"
            if due.date() == now.date() + timedelta(days=1):
                return f"明天有约（{appt_mod._when_text(a)}），到时候记得"
        except Exception as e:
            _stats_err(e)
            continue
    return ""


def _energy_hint(plan, wd) -> str:
    prof = profile()
    hints = []
    prev = plan[(wd - 1) % 7]
    if "performance" in prev:
        hints.append("昨天刚演出完，还没缓过来，很累")
    day_energy = sum(float(ACTIVITIES.get(a, {"energy": 0})["energy"]) for a in plan[wd])  # type: ignore[arg-type, misc]
    if day_energy > 1.4:
        hints.append("今天安排比较满，消耗大")
    if prof.get("nocturnal") and plan[wd][0] == "sleep" and slot_index(datetime.now().hour) == 0:
        hints.append("这个点她一般在补觉")
    return "；".join(hints)


def block(scope="", now=None) -> str:
    """此刻状态注入块（内部参考）。"""
    if not _cfg("enabled", True):
        return ""
    plan = get_week_plan()
    if not plan:
        return ""
    now = now or datetime.now()
    wd, slot, act = _slot_act(plan, now)
    meta = ACTIVITIES.get(act, ACTIVITIES["idle"])
    parts = [f"【此刻状态】{meta['label']}"]
    _today = today_summary(now.date())
    if _today:
        parts.append(f"【今日安排】{_today}")
    nxt = _next_activity(plan, wd, slot)
    if nxt:
        parts.append(f"接下来：{nxt}")
    appt = _appointment_line(scope, now)
    if appt:
        parts.append(appt)
    eng = _energy_hint(plan, wd)
    if eng:
        parts.append(f"（{eng}）")
    parts.append("内部参考：别主动报日程，被问起或相关时自然带一句，不要生硬播报")
    return "；".join(parts)


def today_summary(d=None) -> str:
    """今天的日程摘要（深睡"今日回忆"用）：跳过睡觉/休息的琐碎槽。"""
    plan = get_week_plan()
    if not plan:
        return ""
    wd = (d or date.today()).weekday()
    labels = [
        ACTIVITIES.get(a, ACTIVITIES["idle"])["label"]
        for a in plan[wd]
        if a not in ("sleep", "home_rest", "idle")
    ]
    return "、".join(labels) if labels else ""  # type: ignore[arg-type]



def _stats_err(e):
    """裸 except 审计（v2.2）：错误计数 + 日志，供消融/排查。"""
    try:
        import memory.stats as _st
        _st.bump_err("schedule", e)
    except Exception:
        pass
