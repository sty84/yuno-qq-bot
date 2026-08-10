# -*- coding: utf-8 -*-
"""YUNO 全量回归（pytest 兼容）：内存/心智/空间/程序记忆/评测 一键跑。

运行：python -m pytest tests/ -q   或   python tests/test_features.py
说明：openai 用 stub 替代（不联网、不依赖 LLM）；数据全部走临时目录。
"""

import json
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta


def _stub_openai():
    stub = types.ModuleType("openai")

    class _OpenAI:
        def __init__(self, *a, **k):
            self.chat = types.SimpleNamespace(completions=None)

    stub.OpenAI = _OpenAI
    sys.modules["openai"] = stub


def _make_cfg(tmp):
    cfg = {
        "memory": {
            "embedder": {"provider": "none"},
            "core": {
                "enabled": True,
                "living": {
                    "enabled": True, "bootstrap": True, "bootstrap_max_items": 8,
                    "activation_half_life_days": 30, "search_activation_low": 0.35,
                    "search_activation_mid": 0.6, "search_max_steps": 5,
                    "see_throttle_s": 300, "search_ttl_min": 30,
                    "search_fail_mark_prob": 1.0,
                },
                "space": {
                    "enabled": True,
                    "home_edges": [["客厅", "厨房"], ["客厅", "卧室"], ["卧室", "工作室"], ["厨房", "工作室"]],
                    "edge_min": 1,
                    "cast_schedule": {
                        "仲町阿拉蕾": {
                            "default_place": "排练室",
                            "week": [{"days": [0], "start_hour": 10, "end_hour": 18, "place": "排练室"}],
                        }
                    },
                },
                "environment": {"cast": ["仲町阿拉蕾"]},
                "mind": {"enabled": True, "system1": True, "cognitive_turn": True},
                "sharing": {
                    "enabled": True, "threshold": 0.15, "half_life_hours": 8,
                    "cooldown_hours": 0.01, "max_per_day": 10, "max_per_week": 20,
                },
                "schedule": {"enabled": False},
                "weather": {"enabled": False},
            },
        }
    }
    cfg_path = os.path.join(tmp, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg_path


def test_all_features():
    _stub_openai()
    tmp = tempfile.mkdtemp(prefix="yuno_test_")
    os.environ["CONFIG_PATH"] = _make_cfg(tmp)
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, repo)

    import agent
    import memory
    import memory.stats as stats_mod
    from memory import living, mind, procedures, space, space_eval
    from plugins import _db, _shared

    checks = []

    def check(name, cond, extra=""):
        checks.append(bool(cond))
        print(("PASS" if cond else "FAIL"), name, extra if not cond else "")

    # ---- 物品位置历史（P0-1）----
    r = living.move_item("白巧克力", "卧室", "床头柜")
    check("move-ok", r.get("ok"), r)
    pos = living.position_at("白巧克力")
    check("position_at", pos.get("room") == "卧室" and pos.get("container") == "床头柜", pos)
    check("history", living.item_history("白巧克力")[0]["event"] == "move")

    # ---- 激活 / 找东西（P0-3）----
    living.touch_item("白巧克力")
    check("activation", living.activation("白巧克力") >= 0.5)
    check("where-direct", "【找东西·直接】" in living.where_is_block("c2c:t", "白巧克力在哪"))
    d = _db.item_activation_rows()
    d["白巧克力"] = {"seen_ts": (datetime.now() - timedelta(days=365)).isoformat(timespec="seconds"), "count": 0}
    _db.item_activation_set(d)
    check("where-search", "【找东西·搜索】" in living.where_is_block("c2c:t", "白巧克力在哪"))
    check("search-found", living.search_progress("c2c:t").get("found") is True)

    # ---- 搜索取消 / 过期（P2 优化）----
    d = _db.item_activation_rows()
    d["白巧克力"] = {"seen_ts": (datetime.now() - timedelta(days=365)).isoformat(timespec="seconds"), "count": 0}
    _db.item_activation_set(d)
    living.where_is_block("c2c:t", "白巧克力在哪")
    check("search-cancel", living.cancel_search("c2c:t") is True)
    check("search-cancel-again", living.cancel_search("c2c:t") is False)
    d = _db.item_activation_rows()
    d["白巧克力"] = {"seen_ts": (datetime.now() - timedelta(days=365)).isoformat(timespec="seconds"), "count": 0}
    _db.item_activation_set(d)
    living.where_is_block("c2c:t", "白巧克力在哪")
    st = _db.item_search_rows().get("c2c:t") or {}
    st["started_at"] = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
    _db.item_search_set("c2c:t", st)
    check("search-expire", living.search_progress("c2c:t").get("done") is True)

    # ---- 搜索推进不丢失下一次查看（broadcast 顺序修复）----
    _db.item_search_set(
        "c2c:loop",
        {"name": "牛奶", "queue": ["沙发", "茶几", "电视柜"], "step": 0,
         "started_at": datetime.now().isoformat(timespec="seconds")},
    )
    living.schedule_inspection("c2c:loop", "沙发", now=datetime.now(), kind="search")
    living.take_inspection("c2c:loop")  # 修复后的顺序：先清当前 pending
    ploop = living.search_progress("c2c:loop")  # 再推进 → 写入下一次查看
    pending = _db.kv_get("memory", "inspect_pending") or {}
    check(
        "search-next-kept",
        ploop.get("done") is False
        and "c2c:loop" in pending
        and pending["c2c:loop"].get("container") == "茶几",
        pending,
    )
    living.take_inspection("c2c:loop")
    living.cancel_search("c2c:loop")

    # ---- 静默搜索：中间步骤不播报，只报结果 ----
    _db.item_search_set(
        "c2c:q",
        {"name": "牛奶", "queue": ["沙发", "茶几", "电视柜"], "step": 0,
         "started_at": datetime.now().isoformat(timespec="seconds")},
    )
    pq = living.search_progress("c2c:q")
    check("search-quiet", pq.get("quiet") is True and not pq.get("prompt"), pq)
    living.cancel_search("c2c:q")
    living.take_inspection("c2c:q")

    # ---- 话题转移暂停 + 重问恢复（不突兀）----
    nowp = datetime.now()
    _db.item_search_set(
        "c2c:p",
        {"name": "牛奶", "queue": ["沙发", "茶几"], "step": 0,
         "started_at": nowp.isoformat(timespec="seconds")},
    )
    _db.kv_set(
        "memory", "last_user_msg:c2c:p",
        {"ts": (nowp + timedelta(seconds=10)).isoformat(timespec="seconds"),
         "text": "今天天气怎么样"},
    )
    pp = living.search_progress("c2c:p")
    check("search-paused", pp.get("paused") is True and pp.get("done") is False and not pp.get("prompt"), pp)
    blk = living.where_is_block("c2c:p", "牛奶在哪")
    check("search-resume-block", "进行中" in blk, blk)
    pending = _db.kv_get("memory", "inspect_pending") or {}
    check("search-resume-scheduled", "c2c:p" in pending, pending)
    living.cancel_search("c2c:p")
    living.take_inspection("c2c:p")
    _db.kv_set("memory", "last_user_msg:c2c:p", None)

    # ---- 房间图 / 真实移动 / can_see（P1-1）----
    check("adjacent", space.rooms_adjacent("客厅", "厨房") and not space.rooms_adjacent("客厅", "工作室"))
    space.move_room("厨房")
    check("room-moving", space.room_position().get("state") == "在途中")
    check("can_see", not space.can_see("客厅", "床头柜").get("visible"))
    check("cast-loc", space.cast_location("仲町阿拉蕾", datetime(2026, 8, 10, 12, 0)).get("place") == "排练室")
    check("cast-block", "【队友位置】" in space.cast_block("阿拉蕾在哪", datetime(2026, 8, 10, 12, 0)))

    # ---- 空间事件进记忆 + location 过滤（P0-2）----
    space.emit("arrive", "到了演出场地", location="演出场地")
    rows = [x for x in _db.memory_rows("ai") if x.get("key") == "episodic"]
    check("episodic", any("[地点：演出场地]" in x["fact"] for x in rows))
    check("episodic-indexed", memory.retrieve_detailed("演出场地", ["ai"], location="演出场地") != [])

    # ---- 心智状态 / 意图 / 程序记忆 / System1 / 认知（mind/procedures）----
    mind.intention_set("c2c:t", "准备新曲", source="goal", strength=0.8)
    check("mind-snapshot", (mind.snapshot("c2c:t", "你好") or {}).get("intention") is not None)
    check("mind-block", "【心智状态" in mind.block("c2c:t", "你好"))
    for _ in range(3):
        procedures.learn("c2c:t", "今天心情不错", "……今天也还行，懒得动。", 1.0)
    check("procedure-hit", procedures.match("今天心情不错") is not None)
    reply, meta = agent.ask("今天心情不错", scopes=["c2c:t"])
    check("system1", meta.get("system1") is not None and "今天也还行" in reply)
    _shared.ask_deepseek = lambda *a, **k: (
        '{"appraisal":"机会","activated_goals":["推进新曲"],"intention":"把新曲demo做完",'
        '"chosen_action":"推进目标","reply":"……新曲在写，别催。"}'
    )
    reply2, meta2 = agent.ask("新曲写完了吗", scopes=["c2c:t"])
    check("cognitive", meta2.get("cognitive") is not None and "别催" in reply2)
    check("intention-cognitive", "demo" in (mind.intention_current("c2c:t") or {}).get("title", ""))

    # ---- 程序记忆 EMA（旧样本衰减，学得动）----
    for _ in range(5):
        procedures.learn("c2c:t", "今天心情不错", "……今天也还行，懒得动。", 1.0)
    procedures.learn("c2c:t", "今天心情不错", "……今天也还行，懒得动。", 0.0)
    ema_row = next(
        (r for r in _db.procedure_rows(min_tries=1) if "今天也还行" in r["action"]), None
    )
    check("procedure-ema", ema_row is not None and float(ema_row["success"]) < 0.9, ema_row)

    # ---- 人设→场景生成（P1-2）----
    boot_capture = {}

    def fake_boot(*a, **k):
        boot_capture["prompt"] = a[0] if a else ""
        return (
            '{"items":[{"name":"降噪耳机","category":"设备","qty":1,"room":"工作室",'
            '"container":"打碟台","position":"台面上","difficulty":"浅","origin":"常熬夜作曲"}]}'
        )

    _shared.ask_deepseek = fake_boot
    check("bootstrap", living.bootstrap_from_persona().get("changed") == 1)
    check("bootstrap-no-dup", living.bootstrap_from_persona().get("changed") == 0)
    check("bootstrap-uses-memory", "经历/偏好" in boot_capture.get("prompt", ""))

    # ---- repair 扩展（P1-3）----
    rp = living.repair_spatial()
    check("repair", isinstance(rp.get("fixed"), int), rp)

    # ---- 空间评测 + 基线（P2）----
    ev = space_eval.run(save=True, compare=True)
    check("space-eval", ev.get("baseline_saved") and ev.get("delta") is not None, list(ev.keys()))

    # ---- 搜索彻底失败 → 物品标记为找不到（产品缺口修复）----
    _db.item_search_set(
        "c2c:f",
        {"name": "牛奶", "queue": ["沙发"], "step": 0,
         "started_at": datetime.now().isoformat(timespec="seconds")},
    )
    pf = living.search_progress("c2c:f")
    check("search-fail-done", pf.get("done") and not pf.get("found"), pf)
    milk = next((i for i in living.all_items() if i.get("name") == "牛奶"), None)
    check("search-fail-mark", milk is not None and milk.get("status") == "找不到", milk)

    # ---- recall 口径：同秒两条不同位置事件 → 不应自证恒 1.0 ----
    now0 = datetime.now().isoformat(timespec="seconds")
    _db.item_event_add("能量饮料", now0, "give", "", "厨房/冰箱", "test", "user")
    _db.item_event_add("能量饮料", now0, "see", "", "卧室/床头柜", "test", "ai")
    rv = space_eval.run()["where_recall"]
    check("recall-not-selfproof", rv.get("recall") is not None and rv.get("recall", 1.0) < 1.0, rv)

    # ---- 夜晚槽只能在家活动（v2.2 修复：正常人不会凌晨还在外面）----
    from memory import schedule as schedule_mod
    bad_plan = {wd: ["sleep", "home_rest", "home_entertain", "friend"] for wd in range(7)}
    check("plan-night-invalid", schedule_mod._plan_night_ok(bad_plan) is False)
    good_plan = {wd: ["sleep", "home_rest", "home_entertain", "gaming"] for wd in range(7)}
    check("plan-night-valid", schedule_mod._plan_night_ok(good_plan) is True)
    p = schedule_mod.generate_week(schedule_mod.profile(), "2026-W33")
    night_out = [
        (wd, p[wd][3])
        for wd in range(7)
        if not schedule_mod.ACTIVITIES.get(p[wd][3], {}).get("home", False)
    ]
    check("night-home-only", not night_out, night_out)
    # 深夜拆分：22–02 在家夜生活，02–06 强制睡觉（人设"凌晨2点后才睡"）
    plan4 = {wd: ["sleep", "home_rest", "home_entertain", "dj_practice"] for wd in range(7)}
    _wd, _slot, act3 = schedule_mod._slot_act(plan4, datetime(2026, 8, 11, 3, 0))
    check("night-2am-sleep", act3 == "sleep", act3)
    _wd, _slot, act23 = schedule_mod._slot_act(plan4, datetime(2026, 8, 11, 23, 0))
    check(
        "night-before-2am-home",
        act23 == "dj_practice" and schedule_mod.ACTIVITIES.get(act23, {}).get("home"),
        act23,
    )

    # ---- 组装提示词的时间/天气约束（v2.2 修复）----
    from memory import sharing
    cap = {}

    def fake_compose(*a, **k):
        cap["prompt"] = a[0] if a else ""
        return "……嗯。"

    _shared.ask_deepseek = fake_compose
    sharing._compose("c2c:t", "【此刻状态】在家休息\n【天气】晴 32℃", "rehearsal")
    check(
        "compose-time-weather-rules",
        "现在是" in cap.get("prompt", "") and "天气" in cap.get("prompt", ""),
        cap.get("prompt", "")[:160],
    )

    # 白天可正常发（回归）
    _db.kv_set(
        "memory", "sharing_state",
        {"S": 0.9, "ts": datetime.now().isoformat(timespec="seconds"),
         "last_trigger_ts": "", "day": "", "daily": 0, "week": "", "weekly": 0,
         "reasons": ["rehearsal"]},
    )
    _shared.ask_deepseek = lambda *a, **k: "……傍晚排练完回来，懒得动。"
    dr = sharing.drive("c2c:t", datetime(2026, 8, 11, 15, 0))
    check("share-day-send", dr.get("sent") is True, dr)

    # ---- 计数器（P0 可观测性）----
    c = stats_mod.counters()
    check("counters", int(c.get("system1_hit", 0)) >= 1 and bool(c), c)
    ticks = [k for k in c if str(k).startswith("tick:")]
    check("tick-counters", len(ticks) >= 5, sorted(ticks))

    # ---- 数据模型迁移后的状态表可用 ----
    _db.space_state_set({"room": "客厅", "state": "在场", "path": []})
    check("space_state-table", (_db.space_state_get() or {}).get("room") == "客厅")
    check("mind_intention-table", "c2c:t" in _db.mind_intention_rows())
    check("item_search-table", isinstance(_db.item_search_rows(), dict))
    check("ai_actions-table", isinstance(_db.ai_action_rows(limit=5), list))
    check("space_events-table", isinstance(_db.space_event_rows(limit=5), list))

    failed = [i for i, c in enumerate(checks) if not c]
    print("\nRESULT:", "ALL PASS" if not failed else f"FAILED #{failed}", f"({len(checks)} checks)")
    assert not failed, f"failed checks: {failed}"


if __name__ == "__main__":
    test_all_features()
    print("tests OK")
