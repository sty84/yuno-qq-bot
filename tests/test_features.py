# -*- coding: utf-8 -*-
"""YUNO 全量回归（pytest 兼容）：内存/心智/空间/程序记忆/评测 一键跑。

运行：python -m pytest tests/ -q   或   python tests/test_features.py
说明：openai 用 stub 替代（不联网、不依赖 LLM）；数据全部走临时目录。
"""

import io
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

    # ---- 时间感知回忆（v2.2）----
    from memory import graph, lexical, reasoning, time_extract
    _now = datetime.now()
    old10 = (_now - timedelta(days=10)).isoformat(timespec="seconds")
    _db.memory_add("ai", "test", "上个月去了北京出差", _now.isoformat(timespec="seconds"), None, 0.7, "test")
    graph.build_for_fact("ai", "test", "上个月去了北京出差", ts=old10, ts_source="explicit")
    lexical.bm25_upsert("ai", "test", ["上个月去了北京出差"])
    _db.lexicon_sync("ai", "test")
    reasoning._event_time_cache.update({"ts": 0.0, "key": None, "map": {}})  # 清缓存让新事件生效
    w = (_now - timedelta(days=11), _now - timedelta(days=9))
    th = memory.retrieve_detailed("出差", ["ai"], window=w)
    check("time-window-boost", any("北京出差" in h["fact"] for h in th), th[:2])
    te = time_extract.extract("上周三买了猫")
    check("time-extract-explicit", te.get("explicit") is True and te.get("label") == "上周三", te)
    check("time-label", time_extract.label_for(old10, "explicit").startswith("【"))
    # 指代类时间词（那天/上次/之前）不得误触发 (now,now) 窗口
    te_ref = time_extract.extract("上次说的那个项目")
    check(
        "time-ref-no-window",
        te_ref.get("detected") is True and te_ref.get("start") is None and te_ref.get("explicit") is False,
        te_ref,
    )
    tr = memory.retrieve_detailed("上次说的那个项目", ["ai"])
    check("time-ref-retrieve-ok", isinstance(tr, list), tr[:1])
    tv = memory.time_eval_run()
    check(
        "time-eval",
        isinstance(tv.get("window_recall"), dict) and isinstance(tv.get("timeline_order"), dict),
        list(tv.keys()),
    )

    # ---- 时间锚定（P2-1）：approx 事件沿 follows 链找 explicit 锚点 ----
    from memory import graph as graph_mod
    from memory import reasoning as reasoning_mod
    old30 = (_now - timedelta(days=30)).isoformat(timespec="seconds")
    old20 = (_now - timedelta(days=20)).isoformat(timespec="seconds")
    _db.memory_add("ai", "anchor", "月初去了趟深圳出差", _now.isoformat(timespec="seconds"), None, 0.7, "test")
    graph_mod.build_for_fact("ai", "anchor", "月初去了趟深圳出差", ts=old30, ts_source="explicit")
    _db.memory_add("ai", "anchor", "上周三买了只猫", _now.isoformat(timespec="seconds"), None, 0.6, "test")
    graph_mod.build_for_fact("ai", "anchor", "上周三买了只猫", ts=old20, ts_source="approx")
    _db.memory_add("ai", "anchor", "周末去海边露营", _now.isoformat(timespec="seconds"), None, 0.7, "test")
    graph_mod.build_for_fact("ai", "anchor", "周末去海边露营", ts=old10, ts_source="explicit")
    lexical.bm25_upsert("ai", "anchor", ["月初去了趟深圳出差", "上周三买了只猫", "周末去海边露营"])
    _db.lexicon_sync("ai", "anchor")
    reasoning_mod._event_time_cache.update({"ts": 0.0, "key": None, "map": {}})
    anchor = reasoning_mod.anchor_time("上周三买了只猫到底是哪天", ["ai"])
    check(
        "anchor-time",
        anchor.get("anchored") is True and anchor.get("before") and anchor.get("after"),
        anchor,
    )
    check("anchor-hint", "时间锚定" in (anchor.get("hint") or ""), anchor.get("hint"))
    check(
        "anchor-no-trigger",
        reasoning_mod.anchor_time("上周三买了只猫", ["ai"]) == {},
        reasoning_mod.anchor_time("上周三买了只猫", ["ai"]),
    )

    # ---- 多主体记忆（v2.2）----
    from memory import context as context_mod, controller as consistency, subjects, world as world_mod
    check("subjects-registered", "仲町阿拉蕾" in subjects.registered(), subjects.registered())
    check(
        "subjects-detect",
        subjects.detect("仲町阿拉蕾上次在排练室") == ["仲町阿拉蕾"],
        subjects.detect("仲町阿拉蕾上次在排练室"),
    )
    check("subjects-scope", subjects.scope_of("仲町阿拉蕾") == "npc:仲町阿拉蕾")
    memory.ingest("group:testg", "", "仲町阿拉蕾上次在排练室见过那把伞", "", facts=["仲町阿拉蕾上次在排练室见过那把伞"])
    npc_rows = _db.memory_rows("npc:仲町阿拉蕾")
    check("subjects-write", any("排练室" in r["fact"] for r in npc_rows), npc_rows[:2])
    memory.ingest("c2c:priv", "", "我银行卡密码是 123456，仲町阿拉蕾也知道", "", facts=["用户说我银行卡密码是 123456"])
    priv_npc = [r for r in _db.memory_rows("npc:仲町阿拉蕾") if "密码" in r["fact"]]
    check("subjects-privacy-gate", not priv_npc, priv_npc)
    reasoning._event_time_cache.update({"ts": 0.0, "key": None, "map": {}})
    hits_subj = reasoning.retrieve_subject("仲町阿拉蕾", "伞", top_k=3, min_score=0.0)
    check("subjects-retrieve", any("伞" in f for f, _s, _sc in hits_subj), hits_subj[:2])
    blk_npc = context_mod.npc_memory_block("伞", ["仲町阿拉蕾"], top_k=2)
    check("subjects-block", "队友视角" in blk_npc, blk_npc[:80])
    sev = memory.subjects_eval_run(save=True, compare=True)
    check(
        "subjects-eval-run",
        sev.get("write_rate") is not None and sev.get("baseline_saved"),
        list(sev.keys()),
    )
    check(
        "subjects-eval-decay",
        isinstance(sev.get("decay"), dict)
        and sev["decay"].get("cap_rate") == 1.0
        and sev["decay"].get("source_ceiling_rate") == 1.0,
        sev.get("decay"),
    )

    # ---- 对话回放五维评分（webapp 场景页）----
    _db.scenario_score_add(
        "sc-time", "c2c:t",
        {"recall": 4, "precision": 3, "coherence": 5, "consistency": 4, "naturalness": 4},
        "人工备注", "manual",
    )
    srows = _db.scenario_score_rows()
    check(
        "scenario-score-save",
        srows and srows[0]["scenario_id"] == "sc-time"
        and srows[0]["avg"] == 4.0 and srows[0]["mode"] == "manual",
        srows[:1],
    )

    # ---- 双轨制一致性（v2.2）----
    _db.invalidation_add("c2c:t", "", "旧事实", "conflict")
    rd = consistency.reconcile_pending()
    check(
        "consistency-drain",
        rd.get("reconciled", 0) >= 1 and len(_db.invalidation_rows()) == 0,
        rd,
    )

    # ---- Persona Pack（v2.2 去人设化）----
    from agent import persona as persona_mod
    from memory import pack as pack_mod
    check("pack-active", pack_mod.active() == "yuno", pack_mod.active())

    # ---- 平面图几何层（floorplan P0）----
    from memory import floorplan as fp_mod
    check("fp-enabled", fp_mod.enabled(), fp_mod.data())
    check("fp-valid", fp_mod.validate() == [], fp_mod.validate())
    check(
        "fp-area",
        abs(fp_mod.room_area_m2("客厅") - 21.0) < 0.01
        and abs(fp_mod.room_area_m2("厨房") - 7.5) < 0.01
        and abs(fp_mod.room_area_m2("卧室") - 18.0) < 0.01,
        {r: fp_mod.room_area_m2(r) for r in fp_mod.rooms()},
    )
    check(
        "fp-centroid",
        fp_mod.polygon_centroid(fp_mod.rooms()["客厅"]["polygon"]) == (40.0, 17.5),
        fp_mod.polygon_centroid(fp_mod.rooms()["客厅"]["polygon"]),
    )
    fe = {frozenset(e) for e in fp_mod.adjacency_edges()}
    check(
        "fp-edges",
        fe == {
            frozenset(("客厅", "厨房")), frozenset(("客厅", "卧室")),
            frozenset(("卧室", "工作室")), frozenset(("厨房", "工作室")),
        },
        fe,
    )
    fp_min = fp_mod.route_minutes("卧室", "客厅")
    check("fp-route", fp_min is not None and fp_min > 0, fp_min)
    fp_facts = space.room_facts("客厅")
    check("fp-facts", "㎡" in fp_facts and "大门" in fp_facts, fp_facts)
    check("fp-route-minutes", space.route_minutes("卧室", "客厅") >= 1)

    # ---- 事实分类子串误伤修复（v2.2+）----
    from memory import policy as policy_mod
    check("class-work-process", policy_mod.fact_class("c2c:x", "", "今天工作很累") == "process")
    check("class-addr-instruction", policy_mod.fact_class("c2c:x", "", "记住这个地址") == "process")
    check("class-addr-stable", policy_mod.fact_class("c2c:x", "", "我的地址是上海市徐汇区") == "stable")
    check("class-work-stable", policy_mod.fact_class("c2c:x", "", "我在腾讯工作") == "stable")
    cr = policy_mod.classify_report()
    check("class-report", cr.get("accuracy") == 1.0 and not cr.get("errors"), cr)
    check(
        "arousal-factor",
        policy_mod.arousal_half_factor(0.8) > 1.0
        and policy_mod.arousal_half_factor(-0.5) < 1.0
        and policy_mod.arousal_half_factor(0) == 1.0,
        (policy_mod.arousal_half_factor(0.8), policy_mod.arousal_half_factor(-0.5)),
    )

    # ---- 双速情绪（Sentipolis）：快窗口 + 持久化慢 EMA ----
    from memory import emotion as emotion_mod
    emotion_mod.user_observe("c2c:slow", {"emotion": "开心", "valence": 0.8, "arousal": 0.5, "dominance": 0.5, "playful": False})
    emotion_mod.user_observe("c2c:slow", {"emotion": "低落", "valence": -0.7, "arousal": 0.6, "dominance": -0.5, "playful": False})
    emotion_mod.user_observe("c2c:slow", {"emotion": "低落", "valence": -0.7, "arousal": 0.6, "dominance": -0.5, "playful": False})
    fast2 = emotion_mod.user_estimate("c2c:slow")
    slow2 = emotion_mod.user_mood_slow("c2c:slow")
    check(
        "two-speed-emotion",
        fast2 and slow2 and slow2["vad"]["v"] > fast2["vad"]["v"],
        {"fast": fast2 and fast2["vad"], "slow": slow2 and slow2["vad"]},
    )
    blend = emotion_mod.blended_estimate("c2c:slow", alpha=0.7)
    check(
        "mood-blend-slow",
        blend and fast2["vad"]["v"] < blend["v"] < slow2["vad"]["v"],
        blend,
    )

    # ---- 情绪寻址复核（affective-episodic）：语义弱时按情绪 VAD 二级检索 ----
    emotion_mod.user_observe("c2c:ea", {"emotion": "低落", "valence": -0.7, "arousal": 0.6, "dominance": -0.5, "playful": False})
    _db.memory_add(
        "c2c:ea", "", "那天在阳台哭得很凶",
        datetime.now().isoformat(timespec="seconds"), None, 0.7, "test",
        valence=-0.8, arousal=0.8,
    )
    ea_hits = reasoning.retrieve("zzzzqwerty不存在的词", ["c2c:ea"], min_score=0.0)
    check("emotion-address", any("阳台哭" in f for f, _s, _sc in ea_hits), ea_hits[:2])

    # ---- revive-companion：泊松触发 + 贝叶斯用户状态（v2.2+）----
    from memory import revive as revive_mod
    post_r = revive_mod.state_posterior()
    check(
        "revive-posterior",
        abs(sum(post_r.values()) - 1.0) < 0.05
        and set(post_r) == {"active", "busy", "asleep", "need_care"},
        post_r,
    )
    now_ts = datetime.now().timestamp()
    check(
        "revive-poisson",
        revive_mod.poisson_p(now_ts, 2.0) < 0.01
        and revive_mod.poisson_p(now_ts - 86400, 2.0) > 0.8,
        (revive_mod.poisson_p(now_ts, 2.0), revive_mod.poisson_p(now_ts - 86400, 2.0)),
    )
    dr1 = revive_mod.decide(force=True)
    dr2 = revive_mod.decide(force=True)
    check(
        "revive-cooldown",
        dr1.get("fire") is True and dr2.get("fire") is False and dr2.get("reason") == "冷却中",
        (dr1, dr2),
    )
    rp = revive_mod.peek()
    check("revive-peek", "would_fire" in rp and "state_zh" in rp, rp)

    # ---- cognitive-engine bandit：Thompson 采样 + 奖励更新（v2.2+）----
    from memory import bandit as bandit_mod
    st_b = bandit_mod.select("c2c:b")
    check(
        "bandit-select",
        st_b["id"] in {s["id"] for s in bandit_mod.STRATEGIES} and "hint" in st_b,
        st_b,
    )
    up_b = bandit_mod.update("c2c:b", 1.0)
    check(
        "bandit-update",
        up_b.get("updated") is True and up_b.get("strategy") == st_b["id"] and up_b.get("reward") == 1.0,
        up_b,
    )
    post_b = bandit_mod._posterior("c2c:b")
    check("bandit-alpha", post_b[st_b["id"]]["alpha"] > post_b[st_b["id"]]["beta"], post_b[st_b["id"]])
    check(
        "bandit-reward",
        bandit_mod.reward_from_message("谢谢帮大忙了") == 1.0
        and bandit_mod.reward_from_message("别烦我") == 0.0,
    )
    bs = bandit_mod.status("c2c:b")
    check("bandit-status", bs.get("last") == st_b["id"] and len(bs.get("strategies")) == 5, bs)

    # ---- 消融矩阵：独立基线 + config 恢复（v2.2+）----
    import tools as tools_mod
    ab_probes = [
        {"query": "上周三买了只猫", "expected": ["上周三买了只猫"], "scope": "c2c:ab"},
        {"query": "白巧克力", "expected": ["白巧克力"], "scope": "c2c:t"},
    ]
    core_before = json.dumps(_shared.CONFIG["memory"]["core"], ensure_ascii=False, sort_keys=True)
    ab_res = tools_mod.run_ablation(ab_probes, names=["off_lexical", "off_graph"])
    core_after = json.dumps(_shared.CONFIG["memory"]["core"], ensure_ascii=False, sort_keys=True)
    check(
        "ablation-matrix",
        len(ab_res.get("matrix") or []) == 3 and "delta" in ab_res["matrix"][1],
        ab_res.get("matrix"),
    )
    check("ablation-restore", core_after == core_before)

    # 消融隔离回归（v2.3）：与检索无关的开关 delta 必须全为 0，不得串出相同假值
    _db.memory_add(
        "c2c:abl2", "", "上个月去了趟北京出差",
        datetime.now().isoformat(timespec="seconds"), None, 0.7, "test",
    )
    lexical.bm25_upsert("c2c:abl2", "", ["上个月去了趟北京出差"])
    _db.lexicon_sync("c2c:abl2", "")
    iso_probes = [{"query": "北京出差", "expected": ["上个月去了趟北京出差"], "scope": "c2c:abl2"}]
    iso = tools_mod.run_ablation(
        iso_probes,
        names=["off_system1", "on_cognitive", "off_mood_boost",
               "off_emotion_address", "off_bandit", "off_revive"],
    )
    base_recall = (iso.get("baseline") or {}).get("recall")
    all_zero = all(
        (r.get("delta") or {}).get("recall", 0) == 0
        and (r.get("delta") or {}).get("mrr", 0) == 0
        and (r.get("delta") or {}).get("ndcg", 0) == 0
        for r in (iso.get("matrix") or [])[1:]
    )
    check(
        "ablation-isolation",
        base_recall is not None and base_recall > 0 and all_zero,
        {"baseline": iso.get("baseline"), "matrix": iso.get("matrix")},
    )

    # ---- 改动 1：ai 人设元字段（examples/规则）排除出普通检索 ----
    _ts = datetime.now().isoformat(timespec="seconds")
    _db.memory_add("ai", "examples", "你是做什么的，DJ兼音控师", _ts, None, 0.9, "persona")
    _db.memory_add("ai", "identity", "千石由乃是乐队DJ兼音控师", _ts, None, 0.9, "persona")
    _db.memory_add("ai", "personality", "性格节能主义、家里蹲，看似冷酷", _ts, None, 0.9, "persona")
    _db.memory_add("ai", "preference", "喜欢冰美式和DJ打碟", _ts, None, 0.9, "persona")
    _db.memory_add("ai", "experience_persona", "因选秀出道加入MewType", _ts, None, 0.9, "persona")
    lexical.bm25_upsert("ai", "examples", ["你是做什么的，DJ兼音控师"])
    lexical.bm25_upsert("ai", "identity", ["千石由乃是乐队DJ兼音控师"])
    lexical.bm25_upsert("ai", "personality", ["性格节能主义、家里蹲，看似冷酷"])
    lexical.bm25_upsert("ai", "preference", ["喜欢冰美式和DJ打碟"])
    lexical.bm25_upsert("ai", "experience_persona", ["因选秀出道加入MewType"])
    _db.lexicon_sync("ai", "")
    reasoning._event_time_cache.update({"ts": 0.0, "key": None, "map": {}})
    meta_hits = reasoning.retrieve("做什么的", ["ai"], top_k=5, min_score=0.0)
    id_hits = reasoning.retrieve("千石由乃", ["ai"], top_k=5, min_score=0.0)
    pers_hits = reasoning.retrieve("性格", ["ai"], top_k=5, min_score=0.0)
    pref_hits = reasoning.retrieve("喜欢什么", ["ai"], top_k=5, min_score=0.0)
    exp_hits = reasoning.retrieve("怎么出道", ["ai"], top_k=5, min_score=0.0)
    check(
        "ai-meta-excluded",
        not any("你是做什么的" in f for f, _s, _sc in meta_hits)
        and any("乐队DJ" in f for f, _s, _sc in id_hits)
        and not any("节能主义" in f for f, _s, _sc in pers_hits)
        and any("冰美式" in f for f, _s, _sc in pref_hits)
        and any("出道" in f for f, _s, _sc in exp_hits),
        {"meta": meta_hits[:3], "id": id_hits[:3],
         "personality": pers_hits[:3], "pref": pref_hits[:3], "exp": exp_hits[:3]},
    )

    # ---- 改动 2：LLM 查询改写（宽泛→具体；已具体不改；失败降级；缓存）----
    reasoning._rewrite_cache.clear()
    check("rewrite-skip-specific", reasoning.rewrite_query("阿拉蕾在干什么") == "阿拉蕾在干什么")

    class _FakeMsg:
        content = "仲町阿拉蕾 最近 行踪"

    class _FakeResp:
        choices = [type("_C", (), {"message": _FakeMsg()})()]

    class _FakeCompletions:
        def create(self, **k):
            return _FakeResp()

    class _FakeChat:
        completions = _FakeCompletions()

    _orig_deepseek = _shared.deepseek
    _shared.deepseek = type("_O", (), {"chat": _FakeChat()})()
    try:
        rw1 = reasoning.rewrite_query("我最近说过什么")
        rw2 = reasoning.rewrite_query("我最近说过什么")
    finally:
        _shared.deepseek = _orig_deepseek
    check("rewrite-llm", rw1 == "仲町阿拉蕾 最近 行踪" and rw2 == rw1, (rw1, rw2))

    # ---- persona.md 分区规范（v2.3）：示例区无约定词、雪貂种子已弱化 ----
    _pt = pack_mod.persona_text()
    _idx = _pt.find("# 说话示例")
    _sec = ""
    if _idx >= 0:
        _tail = _pt[_idx:]
        _end = _tail.find("\n# ", 1)
        _sec = _tail[:_end if _end > 0 else len(_tail)]
    check(
        "persona-examples-clean",
        not any(w in _sec for w in ("约定", "承诺", "明天见", "答应", "约好", "说好", "见面", "放鸽子", "约了", "约过")),
        _sec[:200],
    )
    # 黑名单里允许出现"雪貂"（禁令模式），但身份段不得有雪貂联想种子
    _id_sec = _pt.split("# 性格", 1)[0]
    check("persona-no-snowferret-seed", "雪貂" not in _id_sec, "身份段仍含雪貂")

    # ---- 证据门控管道（v2.3）----
    # Step 1 来源标记：ingest 默认 user、sync_identity 标 pack、历史归一
    memory.ingest("c2c:ev", "", "我上周买了只猫", "", facts=["上周买了只猫"])
    ev_rows = [r for r in _db.memory_rows("c2c:ev") if r["fact"] == "上周买了只猫"]
    check("source-user", ev_rows and ev_rows[0].get("source") == "user", ev_rows[:1])
    _db.memory_add("c2c:ev", "", "旧说法", _ts, None, 0.6, "ingest:2026-08-01T00:00:00")
    _db.memory_add("c2c:ev", "", "旧人设", _ts, None, 0.6, "persona")
    _db.memory_source_normalize()
    old_rows = {r["fact"]: r.get("source") for r in _db.memory_rows("c2c:ev")}
    check("source-normalize", old_rows.get("旧说法") == "user" and old_rows.get("旧人设") == "pack", old_rows)
    check("source-pack-ai", any(r.get("source") == "pack" for r in _db.memory_rows("ai")), "ai 无 pack 来源")
    # Step 2 证据清单注入
    lexical.bm25_upsert("c2c:ev", "", ["上周买了只猫", "旧说法"])
    _db.lexicon_sync("c2c:ev", "")
    reasoning._event_time_cache.update({"ts": 0.0, "key": None, "map": {}})
    ev_block = context_mod._memory_block(
        "上周买了猫", ["c2c:ev"], top_k=5, min_score=0.0,
        extra_scopes=None, expand_query=False, recent=None,
    )
    check("evidence-block", "证据状态" in ev_block and "用户亲口陈述" in ev_block, ev_block[:200])
    # Step 3 生成约束注入（有记忆注入时）
    captured_ask2 = {}

    def _fake_llm2(*a, **k):
        captured_ask2["ctx"] = k.get("extra_context", "")
        return "……嗯。"

    _orig_ask2 = _shared.ask_deepseek
    _shared.ask_deepseek = _fake_llm2
    try:
        agent.ask("上周买了猫", scopes=["c2c:ev"], learn=False)
    finally:
        _shared.ask_deepseek = _orig_ask2
    check("evidence-rule", "证据规则" in captured_ask2.get("ctx", ""), captured_ask2.get("ctx", "")[:120])

    # ---- 证据门控 v2：生成后验证（代码级拦截）----
    from agent import evidence_gate
    check(
        "gate-blacklist",
        evidence_gate.contains_unsupported_claim("阿拉蕾是雪貂", [], ["雪貂", "隔壁乐队"]) == "黑名单:雪貂",
    )
    check(
        "gate-claim-no-evidence",
        evidence_gate.contains_unsupported_claim("我们约好了明天见面", [], []) is not None,
    )
    check("gate-farewell-pass", evidence_gate.contains_unsupported_claim("那明天见，晚安", [], []) is None)
    check(
        "gate-grounded-pass",
        evidence_gate.contains_unsupported_claim("对，我们约好了明天下午见面", ["约好明天下午见面"], []) is None,
    )
    check(
        "gate-unrelated-block",
        evidence_gate.contains_unsupported_claim("我们约好了去海边", ["约好明天下午见面"], []) is not None,
    )
    # 用户刚提议约定 → bot 确认放行；用户只是问 → 编造仍拦
    check(
        "gate-user-proposal-pass",
        evidence_gate.contains_unsupported_claim("好，约好了", [], [], user_text="我们明天下午三点见吧") is None,
    )
    check(
        "gate-user-ask-block",
        evidence_gate.contains_unsupported_claim("我们约好了明天见面", [], [], user_text="我们约了什么") is not None,
    )
    # core.ask 集成：LLM 编"约好了"但证据只有"上周买了只猫" → 重写
    captured_ask3 = {}

    def _fake_llm3(*a, **k):
        captured_ask3["ctx"] = k.get("extra_context", "")
        return "对了，我们约好了明天见面吧。"

    _orig_ask3 = _shared.ask_deepseek
    _shared.ask_deepseek = _fake_llm3
    try:
        rep3, meta3 = agent.ask("上周买了猫", scopes=["c2c:ev"], learn=False)
    finally:
        _shared.ask_deepseek = _orig_ask3
    check("gate-rewrite", "记不太清" in rep3 and meta3.get("evidence_gate"), (rep3, meta3))

    # ---- 催约治理（v2.3）：黑名单过滤/巡检清理/一轮一 scope/clear 联动 ----
    from memory import appointment as appt_mod
    rj = appt_mod.extract("c2c:poke", "明天下午3点见面，去隔壁乐队看雪貂")
    check("appt-extract-banned", rj.get("added") == 0, rj)
    _db.kv_set("memory", "appointments", [
        {"id": 1, "scope": "c2c:poke", "time": "2026-08-12T10:00:00+08:00", "has_time": True,
         "text": "约好和阿拉蕾去隔壁乐队", "created_at": "2026-08-12T09:00:00+08:00", "status": "waiting", "poked": 0},
        {"id": 2, "scope": "c2c:poke2", "time": "2026-08-12T11:00:00+08:00", "has_time": True,
         "text": "约好打游戏", "created_at": "2026-08-12T10:00:00+08:00", "status": "waiting", "poked": 0},
    ])
    cl = appt_mod.clean()
    after_clean = {a["id"]: a["status"] for a in _db.kv_get("memory", "appointments", [])}
    check(
        "appt-clean",
        cl.get("cleaned") == 1 and after_clean.get(1) == "done" and after_clean.get(2) == "waiting",
        after_clean,
    )
    _db.kv_set("memory", "appointments", [
        {"id": 3, "scope": "c2c:poke", "time": "2026-08-12T10:00:00+08:00", "has_time": True,
         "text": "约好打游戏", "created_at": "2026-08-12T09:00:00+08:00", "status": "waiting", "poked": 0},
        {"id": 4, "scope": "c2c:poke", "time": "2026-08-12T09:00:00+08:00", "has_time": True,
         "text": "约好见面", "created_at": "2026-08-12T08:00:00+08:00", "status": "waiting", "poked": 0},
    ])
    _db.kv_set("memory", "lastmsg:c2c:poke", "")
    _db.kv_set("memory", "lastmsg:c2c:poke2", "")
    poked = appt_mod.check_and_poke(now=datetime.now().astimezone())
    check("appt-one-per-scope", len(poked) == 1 and poked[0].get("id") == 4, [p.get("id") for p in poked])
    _db.kv_set("memory", "appointments", [
        {"id": 5, "scope": "c2c:poke2", "time": "2026-08-12T10:00:00+08:00", "has_time": True,
         "text": "约好打游戏", "created_at": "2026-08-12T09:00:00+08:00", "status": "waiting", "poked": 0},
    ])
    removed = appt_mod.clear_scope("c2c:poke2")
    check("appt-clear-scope", removed == 1 and appt_mod._appts() == [], (removed, appt_mod._appts()))

    # ---- 犹豫层（v2.3）：软硬分离 + 概率化 ----
    from memory import hesitation
    _orig_hcfg = _shared.CONFIG.setdefault("memory", {}).setdefault("core", {}).setdefault("hesitation", {})
    _shared.CONFIG["memory"]["core"]["hesitation"].update(
        {"enabled": True, "sample_rate": 1.0, "discard_cap": 1.0, "rewrite_prob": 1.0, "delay_max_s": 5}
    )

    class _HM:
        content = '{"action":"discard","delay_s":2,"rewrite":"","thought":"算了不发了"}'

    class _HR:
        choices = [type("_C", (), {"message": _HM()})()]

    class _HC:
        def create(self, **k):
            return _HR()

    _orig_ds2 = _shared.deepseek
    _shared.deepseek = type("_O", (), {"chat": type("_CH", (), {"completions": _HC()})()})()
    try:
        h1 = hesitation.gate("我有点烦", "c2c:h", "generic")
    finally:
        _shared.deepseek = _orig_ds2
    check("hesitation-discard", h1.get("action") == "discard", h1)
    # eval 失败 → 默认放行
    class _HC2:
        def create(self, **k):
            raise Exception("stub fail")
    _shared.deepseek = type("_O", (), {"chat": type("_CH", (), {"completions": _HC2()})()})()
    try:
        h2 = hesitation.gate("消息", "c2c:h", "generic")
    finally:
        _shared.deepseek = _orig_ds2
    check("hesitation-fail-send", h2.get("action") == "send" and h2.get("reason") == "eval_fail_send", h2)
    # 禁用 → 直接发
    _shared.CONFIG["memory"]["core"]["hesitation"]["enabled"] = False
    h3 = hesitation.gate("消息", "c2c:h", "generic")
    _shared.CONFIG["memory"]["core"]["hesitation"] = _orig_hcfg
    check("hesitation-disabled", h3.get("action") == "send" and h3.get("delay_s") == 0, h3)
    # notif scheduled_at：未来的延迟发送，到点才出现在 pending
    from datetime import timedelta as _td
    _db.notif_add("c2c", "future", "迟到消息", scheduled_at=(datetime.now() + _td(seconds=60)).isoformat(timespec="seconds"))
    _db.notif_add("c2c", "now", "立即消息", scheduled_at="")
    pend = _db.notif_pending()
    check(
        "notif-scheduled",
        any(i["target"] == "now" for i in pend) and not any(i["target"] == "future" for i in pend),
        [i["target"] for i in pend],
    )

    # ---- LLM token / 成本观测（v2.3）----
    _db.llm_cost_add("2026-08-12T10:00:00", "chat", "", 1000, 200)
    _db.llm_cost_add("2026-08-12T10:01:00", "rerank", "lexical,vector", 500, 100)
    cs = _db.llm_cost_summary(30)
    check(
        "llm-cost-summary",
        cs["total"]["prompt"] == 1500 and cs["total"]["completion"] == 300
        and any(p["path"] == "lexical" for p in cs["by_path"])
        and any(m["module"] == "rerank" for m in cs["by_module"]),
        cs,
    )

    # ---- 第一批修复验证（v2.3 决策清单）----
    # 1) 提取器只喂用户的话：bot 回复不进提取输入（P1-2）
    from memory import extract as extract_mod
    captured = {}

    def _fake_ews(conv):
        captured["conv"] = str(conv)
        return []

    _orig_ews = extract_mod.extract_with_structure
    extract_mod.extract_with_structure = _fake_ews
    try:
        memory.ingest("c2c:guard", "", "阿拉蕾在干什么", "阿拉蕾是雪貂，借给隔壁乐队了")
    finally:
        extract_mod.extract_with_structure = _orig_ews
    check(
        "extract-user-only",
        "机器人" not in captured.get("conv", "") and "雪貂" not in captured.get("conv", ""),
        captured.get("conv", "")[:80],
    )
    # 2) 生成层记忆缺口守卫：说不记得时注入硬性约束（P0-1 生成层）
    captured_ask = {}

    def _fake_llm(*a, **k):
        captured_ask["ctx"] = k.get("extra_context", "")
        return "……我这边也没有相关记录。"

    _orig_ask = _shared.ask_deepseek
    _shared.ask_deepseek = _fake_llm
    try:
        def _cap_ctx(text, history=None):
            captured_ask["ctx"] = ""
            agent.ask(text, history=history, scopes=["c2c:gap"], learn=False)
            return captured_ask.get("ctx", "")

        check("memory-gap-guard", "记忆缺口" in _cap_ctx("我不记得了"), _cap_ctx("我不记得了")[:80])
        # 守卫触发扩展：疑问式回应（什么事 / 短"什么"）
        check("gap-what-thing", "记忆缺口" in _cap_ctx("什么事"), _cap_ctx("什么事")[:80])
        check("gap-short-what", "记忆缺口" in _cap_ctx("什么？"), _cap_ctx("什么？")[:80])
        # 上文 bot 声明约定：用户困惑→核验守卫；用户确认→不触发
        claim_hist = [
            {"role": "user", "content": "我们聊到哪了"},
            {"role": "assistant", "content": "对了，我们约好明天见面吧"},
        ]
        check("claim-guard-fired", "约定核验" in _cap_ctx("嗯？", claim_hist), _cap_ctx("嗯？", claim_hist)[:80])
        check("claim-guard-confirm", "约定核验" not in _cap_ctx("嗯好", claim_hist), _cap_ctx("嗯好", claim_hist)[:80])
        # 约定验证前置：无约定记录时禁止声称"约好的事"
        check("appt-verify", "约定验证" in _cap_ctx("我们约了什么"), _cap_ctx("我们约了什么")[:80])
    finally:
        _shared.ask_deepseek = _orig_ask
    # 3) 评测集过滤：寒暄/短陈述剔除，真问题保留（P2-2）
    check(
        "probe-filter",
        tools_mod._is_social_probe("你好啊")
        and tools_mod._is_social_probe("哈哈")
        and not tools_mod._is_social_probe("阿拉蕾在干什么"),
        (tools_mod._is_social_probe("你好啊"), tools_mod._is_social_probe("阿拉蕾在干什么")),
    )

    # ---- lazy_label 收进 pack（去人设残留）----
    tr = living.travel_time("排练室", mode="walk", now=datetime.now())
    check("lazy-label-pack", "由乃懒得动" in (tr.get("factors") or []), tr.get("factors"))
    _liv = io.open(os.path.join(repo, "memory", "living.py"), encoding="utf-8").read()
    check("lazy-label-no-hardcode", "由乃" not in _liv)

    # ---- 议题情绪打通（topic mood ↔ VAD，v2.2+）----
    from memory import topic as topic_mod
    tid1 = topic_mod.link_fact(
        "c2c:t", "", "今天工作很累", "工作", confidence=0.7,
        an={"emotion": "低落", "valence": -0.7, "arousal": 0.6, "dominance": -0.5,
            "importance": 0.5, "playful": False},
    )
    tid2 = topic_mod.link_fact(
        "c2c:t", "", "今天工作很累", "工作", confidence=0.7,
        an={"emotion": "开心", "valence": 0.8, "arousal": 0.5, "dominance": 0.5,
            "importance": 0.5, "playful": False},
    )
    check("topic-vad-same-id", tid1 == tid2, (tid1, tid2))
    tparams = _db.topic_params(tid1)
    check("topic-vad-stored", sum(1 for p in tparams if p["param"] == "vad") >= 2, tparams[:4])
    tc = topic_mod.mood_centroid(tid1)
    check(
        "topic-mood-centroid",
        tc is not None and tc.get("n") == 2 and abs(tc["vad"]["v"]) < 0.3 and "trend" in tc,
        tc,
    )
    check("topic-mood-map", "今天工作很累" in topic_mod.mood_map(["c2c:t"]))
    tme = topic_mod.mood_eval()
    check(
        "topic-mood-eval",
        "write_consistency" in tme and "centroid_consistency" in tme
        and isinstance(tme.get("vad_table_drift"), dict),
        tme,
    )
    tblock = context_mod._topic_block("工作", ["c2c:t"])
    check("topic-mood-block", "情绪底色" in tblock, tblock[:200])

    # ---- 缺口 1：旧议题 vad 回填（backfill，幂等）----
    bf_ts = datetime.now().isoformat(timespec="seconds")
    bf_tid = topic_mod.find_or_create("c2c:bf", "", "工作", "旧工作议题")
    _db.topic_param_add(bf_tid, "fact", "悲喜交加，被夸了很开心但想到加班又难过", 0.7, bf_ts)
    _db.topic_param_add(bf_tid, "mood", "低落", 0.7, bf_ts)
    topic_mod.backfill_vad()
    bf_params = _db.topic_params(bf_tid)
    check("topic-vad-backfill", any(p["param"] == "vad" for p in bf_params), bf_params[:4])
    check(
        "topic-compound-backfill",
        any(p["param"] == "compound" and p["value"] == "悲喜交加" for p in bf_params),
        bf_params,
    )
    w = pack_mod.world()
    check("pack-world", "layout" in w and "items" in w and w.get("role"), list(w.keys()))
    check("persona-name", persona_mod.persona_name() == "千石由乃", persona_mod.persona_name())
    tpl = living.INSPECT_PROMPT.format(
        name="测试角色", role="测试身份", room="客厅", container="茶几", items="空的",
    )
    check("prompt-templated", "测试角色" in tpl and "千石由乃" not in tpl, tpl[:60])

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
    sharing._compose("c2c:share", "【此刻状态】在家休息\n【天气】晴 32℃", "rehearsal")
    check(
        "compose-time-weather-rules",
        "现在是" in cap.get("prompt", "") and "天气" in cap.get("prompt", ""),
        cap.get("prompt", "")[:160],
    )

    # 白天可正常发（回归）
    # v2.3：persona 参数 pack 优先——config 的 sharing.threshold=0.15 不应覆盖 yuno pack 的 0.6
    check("share-threshold-pack", abs(sharing._threshold() - 0.6) < 0.01, sharing._threshold())
    _db.kv_set(
        "memory", "sharing_state",
        {"S": 0.9, "ts": datetime.now().isoformat(timespec="seconds"),
         "last_trigger_ts": "", "day": "", "daily": 0, "week": "", "weekly": 0,
         "reasons": ["rehearsal"]},
    )
    _shared.ask_deepseek = lambda *a, **k: "……傍晚排练完回来，懒得动。"
    _orig_th = sharing._threshold
    sharing._threshold = lambda: 0.15  # 机制测试：压低阈值，验证白天分享路径可发
    try:
        dr = sharing.drive("c2c:share", datetime(2026, 8, 11, 15, 0))
    finally:
        sharing._threshold = _orig_th
    check("share-day-send", dr.get("sent") is True, dr)

    # ---- 复合情绪（v2.2）----
    from memory import emotion as emotion_mod
    j1 = emotion_mod.judge("又气又好笑，这都能输")
    check("compound-kuxiaobude", j1.get("compound") == "哭笑不得", j1)
    j2 = emotion_mod.judge("既期待又害怕明天的面试")
    check("compound-qidai", j2.get("compound") == "期待又不安", j2)
    ev2 = emotion_mod.eval_probes([
        {"text": "又气又好笑，这都能输", "emotion": "愤怒", "compound": "哭笑不得"},
        {"text": "既期待又害怕明天的面试", "emotion": "期待", "compound": "期待又不安"},
        {"text": "悲喜交加，被夸了很开心但想到加班又难过", "emotion": "开心", "compound": "悲喜交加"},
        {"text": "嘴上说算了，心里五味杂陈", "emotion": "低落", "compound": "五味杂陈"},
    ])
    check("compound-eval", ev2.get("compound_n") == 4 and ev2.get("compound_accuracy") == 1.0, ev2)

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
