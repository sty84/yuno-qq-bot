# -*- coding: utf-8 -*-
"""YUNO 全量回归（pytest 兼容）：内存/心智/空间/程序记忆/评测 一键跑。

运行：python -m pytest tests/ -q   或   python tests/test_features.py
说明：openai 用 stub 替代（不联网、不依赖 LLM）；数据全部走临时目录。

④ 测试重构：原 1148 行单函数按域拆成 9 个测试函数，共享模块级 _env()（临时库）。
注意：函数间存在库状态顺序依赖（前序函数写入的数据被后续读取），
请整文件运行；单独跑单个函数可能因缺前置数据失败（属预期，非回归）。
check 失败通过 _CheckFailed（BaseException）穿透业务 try/except，不会被静默吞掉。
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





_ENV = {}


def _env() -> dict:
    """④ 测试重构：模块级共享环境（临时库 + 配置 + 模块导入），一次初始化。"""
    global _ENV
    if _ENV:
        return _ENV
    tmp = tempfile.mkdtemp(prefix="yuno_test_")
    os.environ["CONFIG_PATH"] = _make_cfg(tmp)
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, repo)

    import agent
    import memory
    import memory.stats as stats_mod
    from memory import living, mind, procedures, space, space_eval
    from plugins import _db, _shared



    from memory import graph, hesitation, lexical, reasoning, sharing, subjects, time_extract
    from memory import pack as pack_mod
    from memory import context as context_mod, controller as consistency, world as world_mod
    import tools as tools_mod
    from agent import persona as persona_mod
    # 测试隔离：无论前面哪个测试模块先跑，都重定向配置并强制绑定本测试临时库
    _shared.CONFIG_PATH = os.environ["CONFIG_PATH"]
    _shared.reload_config()
    _db.init(tmp, force=True)
    _ENV.update(locals())
    return _ENV


class _CheckFailed(BaseException):
    """check 失败穿透 try/except（原版收集后统一断言必然暴露失败，拆分后不能静默吞掉）。"""


def _check(name, cond, extra=""):
    """④ 测试重构：check 语义保留；失败立即暴露（不被业务 try/except 吞掉）。"""
    if not cond:
        raise _CheckFailed(f"{name}: {extra if not cond else ''}")
    print(f"PASS {name}")


def test_01_items_search_space():
    e = _env()
    living, space, _db, memory = e["living"], e["space"], e["_db"], e["memory"]
    # ---- 物品位置历史（P0-1）----
    r = living.move_item("白巧克力", "卧室", "床头柜")
    _check("move-ok", r.get("ok"), r)
    pos = living.position_at("白巧克力")
    _check("position_at", pos.get("room") == "卧室" and pos.get("container") == "床头柜", pos)
    _check("history", living.item_history("白巧克力")[0]["event"] == "move")

    # ---- 激活 / 找东西（P0-3）----
    living.touch_item("白巧克力")
    _check("activation", living.activation("白巧克力") >= 0.5)
    _check("where-direct", "【找东西·直接】" in living.where_is_block("c2c:t", "白巧克力在哪"))
    d = _db.item_activation_rows()
    d["白巧克力"] = {"seen_ts": (datetime.now() - timedelta(days=365)).isoformat(timespec="seconds"), "count": 0}
    _db.item_activation_set(d)
    _check("where-search", "【找东西·搜索】" in living.where_is_block("c2c:t", "白巧克力在哪"))
    _check("search-found", living.search_progress("c2c:t").get("found") is True)

    # ---- 搜索取消 / 过期（P2 优化）----
    d = _db.item_activation_rows()
    d["白巧克力"] = {"seen_ts": (datetime.now() - timedelta(days=365)).isoformat(timespec="seconds"), "count": 0}
    _db.item_activation_set(d)
    living.where_is_block("c2c:t", "白巧克力在哪")
    _check("search-cancel", living.cancel_search("c2c:t") is True)
    _check("search-cancel-again", living.cancel_search("c2c:t") is False)
    d = _db.item_activation_rows()
    d["白巧克力"] = {"seen_ts": (datetime.now() - timedelta(days=365)).isoformat(timespec="seconds"), "count": 0}
    _db.item_activation_set(d)
    living.where_is_block("c2c:t", "白巧克力在哪")
    st = _db.item_search_rows().get("c2c:t") or {}
    st["started_at"] = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
    _db.item_search_set("c2c:t", st)
    _check("search-expire", living.search_progress("c2c:t").get("done") is True)

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
    _check(
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
    _check("search-quiet", pq.get("quiet") is True and not pq.get("prompt"), pq)
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
    _check("search-paused", pp.get("paused") is True and pp.get("done") is False and not pp.get("prompt"), pp)
    blk = living.where_is_block("c2c:p", "牛奶在哪")
    _check("search-resume-block", "进行中" in blk, blk)
    pending = _db.kv_get("memory", "inspect_pending") or {}
    _check("search-resume-scheduled", "c2c:p" in pending, pending)
    living.cancel_search("c2c:p")
    living.take_inspection("c2c:p")
    _db.kv_set("memory", "last_user_msg:c2c:p", None)

    # ---- 房间图 / 真实移动 / can_see（P1-1）----
    _check("adjacent", space.rooms_adjacent("客厅", "厨房") and not space.rooms_adjacent("客厅", "工作室"))
    space.move_room("厨房")
    _check("room-moving", space.room_position().get("state") == "在途中")
    _check("can_see", not space.can_see("客厅", "床头柜").get("visible"))
    _check("cast-loc", space.cast_location("仲町阿拉蕾", datetime(2026, 8, 10, 12, 0)).get("place") == "排练室")
    _check("cast-block", "【队友位置】" in space.cast_block("阿拉蕾在哪", datetime(2026, 8, 10, 12, 0)))

    # ---- 空间事件进记忆 + location 过滤（P0-2）----
    space.emit("arrive", "到了演出场地", location="演出场地")
    rows = [x for x in _db.memory_rows("ai") if x.get("key") == "episodic"]
    _check("episodic", any("[地点：演出场地]" in x["fact"] for x in rows))
    _check("episodic-indexed", memory.retrieve_detailed("演出场地", ["ai"], location="演出场地") != [])


def test_02_mind_procedures():
    e = _env()
    living, mind, procedures, _db, _shared, agent, space_eval = e["living"], e["mind"], e["procedures"], e["_db"], e["_shared"], e["agent"], e["space_eval"]
    # ---- 心智状态 / 意图 / 程序记忆 / System1 / 认知（mind/procedures）----
    mind.intention_set("c2c:t", "准备新曲", source="goal", strength=0.8)
    _check("mind-snapshot", (mind.snapshot("c2c:t", "你好") or {}).get("intention") is not None)
    _check("mind-block", "【心智状态" in mind.block("c2c:t", "你好"))
    for _ in range(3):
        procedures.learn("c2c:t", "今天心情不错", "……今天也还行，懒得动。", 1.0)
    _check("procedure-hit", procedures.match("今天心情不错") is not None)
    reply, meta = agent.ask("今天心情不错", scopes=["c2c:t"])
    _check("system1", meta.get("system1") is not None and "今天也还行" in reply)
    _shared.ask_deepseek = lambda *a, **k: (
        '{"appraisal":"机会","activated_goals":["推进新曲"],"intention":"把新曲demo做完",'
        '"chosen_action":"推进目标","reply":"……新曲在写，别催。"}'
    )
    reply2, meta2 = agent.ask("新曲写完了吗", scopes=["c2c:t"])
    _check("cognitive", meta2.get("cognitive") is not None and "别催" in reply2)
    _check("intention-cognitive", "demo" in (mind.intention_current("c2c:t") or {}).get("title", ""))

    # ---- 程序记忆 EMA（旧样本衰减，学得动）----
    for _ in range(5):
        procedures.learn("c2c:t", "今天心情不错", "……今天也还行，懒得动。", 1.0)
    procedures.learn("c2c:t", "今天心情不错", "……今天也还行，懒得动。", 0.0)
    ema_row = next(
        (r for r in _db.procedure_rows(min_tries=1) if "今天也还行" in r["action"]), None
    )
    _check("procedure-ema", ema_row is not None and float(ema_row["success"]) < 0.9, ema_row)

    # ---- 人设→场景生成（P1-2）----
    boot_capture = {}

    def fake_boot(*a, **k):
        boot_capture["prompt"] = a[0] if a else ""
        return (
            '{"items":[{"name":"降噪耳机","category":"设备","qty":1,"room":"工作室",'
            '"container":"打碟台","position":"台面上","difficulty":"浅","origin":"常熬夜作曲"}]}'
        )

    _shared.ask_deepseek = fake_boot
    _check("bootstrap", living.bootstrap_from_persona().get("changed") == 1)
    _check("bootstrap-no-dup", living.bootstrap_from_persona().get("changed") == 0)
    _check("bootstrap-uses-memory", "经历/偏好" in boot_capture.get("prompt", ""))

    # ---- repair 扩展（P1-3）----
    rp = living.repair_spatial()
    _check("repair", isinstance(rp.get("fixed"), int), rp)

    # ---- 空间评测 + 基线（P2）----
    ev = space_eval.run(save=True, compare=True)
    _check("space-eval", ev.get("baseline_saved") and ev.get("delta") is not None, list(ev.keys()))

    # ---- 搜索彻底失败 → 物品标记为找不到（产品缺口修复）----
    _db.item_search_set(
        "c2c:f",
        {"name": "牛奶", "queue": ["沙发"], "step": 0,
         "started_at": datetime.now().isoformat(timespec="seconds")},
    )
    pf = living.search_progress("c2c:f")
    _check("search-fail-done", pf.get("done") and not pf.get("found"), pf)
    milk = next((i for i in living.all_items() if i.get("name") == "牛奶"), None)
    _check("search-fail-mark", milk is not None and milk.get("status") == "找不到", milk)

    # ---- recall 口径：同秒两条不同位置事件 → 不应自证恒 1.0 ----
    now0 = datetime.now().isoformat(timespec="seconds")
    _db.item_event_add("能量饮料", now0, "give", "", "厨房/冰箱", "test", "user")
    _db.item_event_add("能量饮料", now0, "see", "", "卧室/床头柜", "test", "ai")
    rv = space_eval.run()["where_recall"]
    _check("recall-not-selfproof", rv.get("recall") is not None and rv.get("recall", 1.0) < 1.0, rv)


def test_03_time_subjects():
    e = _env()
    _db, memory, graph, lexical, reasoning, subjects, time_extract = e["_db"], e["memory"], e["graph"], e["lexical"], e["reasoning"], e["subjects"], e["time_extract"]
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
    _check("time-window-boost", any("北京出差" in h["fact"] for h in th), th[:2])
    te = time_extract.extract("上周三买了猫")
    _check("time-extract-explicit", te.get("explicit") is True and te.get("label") == "上周三", te)
    _check("time-label", time_extract.label_for(old10, "explicit").startswith("【"))
    # 指代类时间词（那天/上次/之前）不得误触发 (now,now) 窗口
    te_ref = time_extract.extract("上次说的那个项目")
    _check(
        "time-ref-no-window",
        te_ref.get("detected") is True and te_ref.get("start") is None and te_ref.get("explicit") is False,
        te_ref,
    )
    tr = memory.retrieve_detailed("上次说的那个项目", ["ai"])
    _check("time-ref-retrieve-ok", isinstance(tr, list), tr[:1])
    tv = memory.time_eval_run()
    _check(
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
    _check(
        "anchor-time",
        anchor.get("anchored") is True and anchor.get("before") and anchor.get("after"),
        anchor,
    )
    _check("anchor-hint", "时间锚定" in (anchor.get("hint") or ""), anchor.get("hint"))
    _check(
        "anchor-no-trigger",
        reasoning_mod.anchor_time("上周三买了只猫", ["ai"]) == {},
        reasoning_mod.anchor_time("上周三买了只猫", ["ai"]),
    )

    # ---- 多主体记忆（v2.2）----
    from memory import context as context_mod, controller as consistency, subjects, world as world_mod
    _check("subjects-registered", "仲町阿拉蕾" in subjects.registered(), subjects.registered())
    _check(
        "subjects-detect",
        subjects.detect("仲町阿拉蕾上次在排练室") == ["仲町阿拉蕾"],
        subjects.detect("仲町阿拉蕾上次在排练室"),
    )
    _check("subjects-scope", subjects.scope_of("仲町阿拉蕾") == "npc:仲町阿拉蕾")
    memory.ingest("group:testg", "", "仲町阿拉蕾上次在排练室见过那把伞", "", facts=["仲町阿拉蕾上次在排练室见过那把伞"])
    npc_rows = _db.memory_rows("npc:仲町阿拉蕾")
    _check("subjects-write", any("排练室" in r["fact"] for r in npc_rows), npc_rows[:2])
    memory.ingest("c2c:priv", "", "我银行卡密码是 123456，仲町阿拉蕾也知道", "", facts=["用户说我银行卡密码是 123456"])
    priv_npc = [r for r in _db.memory_rows("npc:仲町阿拉蕾") if "密码" in r["fact"]]
    _check("subjects-privacy-gate", not priv_npc, priv_npc)
    reasoning._event_time_cache.update({"ts": 0.0, "key": None, "map": {}})
    hits_subj = reasoning.retrieve_subject("仲町阿拉蕾", "伞", top_k=3, min_score=0.0)
    _check("subjects-retrieve", any("伞" in f for f, _s, _sc in hits_subj), hits_subj[:2])
    blk_npc = context_mod.npc_memory_block("伞", ["仲町阿拉蕾"], top_k=2)
    _check("subjects-block", "队友视角" in blk_npc, blk_npc[:80])
    sev = memory.subjects_eval_run(save=True, compare=True)
    _check(
        "subjects-eval-run",
        sev.get("write_rate") is not None and sev.get("baseline_saved"),
        list(sev.keys()),
    )
    _check(
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
    _check(
        "scenario-score-save",
        srows and srows[0]["scenario_id"] == "sc-time"
        and srows[0]["avg"] == 4.0 and srows[0]["mode"] == "manual",
        srows[:1],
    )

    # ---- 双轨制一致性（v2.2）----
    _db.invalidation_add("c2c:t", "", "旧事实", "conflict")
    rd = consistency.reconcile_pending()
    _check(
        "consistency-drain",
        rd.get("reconciled", 0) >= 1 and len(_db.invalidation_rows()) == 0,
        rd,
    )


def test_04_packs_floorplan_emotion():
    e = _env()
    space, _db, reasoning = e["space"], e["_db"], e["reasoning"]
    # ---- Persona Pack（v2.2 去人设化）----
    from agent import persona as persona_mod
    from memory import pack as pack_mod
    _check("pack-active", pack_mod.active() == "yuno", pack_mod.active())

    # ---- 平面图几何层（floorplan P0）----
    from memory import floorplan as fp_mod
    _check("fp-enabled", fp_mod.enabled(), fp_mod.data())
    _check("fp-valid", fp_mod.validate() == [], fp_mod.validate())
    _check(
        "fp-area",
        abs(fp_mod.room_area_m2("客厅") - 21.0) < 0.01
        and abs(fp_mod.room_area_m2("厨房") - 7.5) < 0.01
        and abs(fp_mod.room_area_m2("卧室") - 18.0) < 0.01,
        {r: fp_mod.room_area_m2(r) for r in fp_mod.rooms()},
    )
    _check(
        "fp-centroid",
        fp_mod.polygon_centroid(fp_mod.rooms()["客厅"]["polygon"]) == (40.0, 17.5),
        fp_mod.polygon_centroid(fp_mod.rooms()["客厅"]["polygon"]),
    )
    fe = {frozenset(e) for e in fp_mod.adjacency_edges()}
    _check(
        "fp-edges",
        fe == {
            frozenset(("客厅", "厨房")), frozenset(("客厅", "卧室")),
            frozenset(("卧室", "工作室")), frozenset(("厨房", "工作室")),
        },
        fe,
    )
    fp_min = fp_mod.route_minutes("卧室", "客厅")
    _check("fp-route", fp_min is not None and fp_min > 0, fp_min)
    fp_facts = space.room_facts("客厅")
    _check("fp-facts", "㎡" in fp_facts and "大门" in fp_facts, fp_facts)
    _check("fp-route-minutes", space.route_minutes("卧室", "客厅") >= 1)

    # ---- 事实分类子串误伤修复（v2.2+）----
    from memory import policy as policy_mod
    _check("class-work-process", policy_mod.fact_class("c2c:x", "", "今天工作很累") == "process")
    _check("class-addr-instruction", policy_mod.fact_class("c2c:x", "", "记住这个地址") == "process")
    _check("class-addr-stable", policy_mod.fact_class("c2c:x", "", "我的地址是上海市徐汇区") == "stable")
    _check("class-work-stable", policy_mod.fact_class("c2c:x", "", "我在腾讯工作") == "stable")
    cr = policy_mod.classify_report()
    _check("class-report", cr.get("accuracy") == 1.0 and not cr.get("errors"), cr)
    _check(
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
    _check(
        "two-speed-emotion",
        fast2 and slow2 and slow2["vad"]["v"] > fast2["vad"]["v"],
        {"fast": fast2 and fast2["vad"], "slow": slow2 and slow2["vad"]},
    )
    blend = emotion_mod.blended_estimate("c2c:slow", alpha=0.7)
    _check(
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
    _check("emotion-address", any("阳台哭" in f for f, _s, _sc in ea_hits), ea_hits[:2])


def test_05_revive_bandit_ablation():
    e = _env()
    _db, _shared, lexical = e["_db"], e["_shared"], e["lexical"]
    # ---- revive-companion：泊松触发 + 贝叶斯用户状态（v2.2+）----
    from memory import revive as revive_mod
    post_r = revive_mod.state_posterior()
    _check(
        "revive-posterior",
        abs(sum(post_r.values()) - 1.0) < 0.05
        and set(post_r) == {"active", "busy", "asleep", "need_care"},
        post_r,
    )
    now_ts = datetime.now().timestamp()
    _check(
        "revive-poisson",
        revive_mod.poisson_p(now_ts, 2.0) < 0.01
        and revive_mod.poisson_p(now_ts - 86400, 2.0) > 0.8,
        (revive_mod.poisson_p(now_ts, 2.0), revive_mod.poisson_p(now_ts - 86400, 2.0)),
    )
    dr1 = revive_mod.decide(force=True)
    dr2 = revive_mod.decide(force=True)
    _check(
        "revive-cooldown",
        dr1.get("fire") is True and dr2.get("fire") is False and dr2.get("reason") == "冷却中",
        (dr1, dr2),
    )
    rp = revive_mod.peek()
    _check("revive-peek", "would_fire" in rp and "state_zh" in rp, rp)

    # ---- cognitive-engine bandit：Thompson 采样 + 奖励更新（v2.2+）----
    from memory import bandit as bandit_mod
    st_b = bandit_mod.select("c2c:b")
    _check(
        "bandit-select",
        st_b["id"] in {s["id"] for s in bandit_mod.STRATEGIES} and "hint" in st_b,
        st_b,
    )
    up_b = bandit_mod.update("c2c:b", 1.0)
    _check(
        "bandit-update",
        up_b.get("updated") is True and up_b.get("strategy") == st_b["id"] and up_b.get("reward") == 1.0,
        up_b,
    )
    post_b = bandit_mod._posterior("c2c:b")
    _check("bandit-alpha", post_b[st_b["id"]]["alpha"] > post_b[st_b["id"]]["beta"], post_b[st_b["id"]])
    _check(
        "bandit-reward",
        bandit_mod.reward_from_message("谢谢帮大忙了") == 1.0
        and bandit_mod.reward_from_message("别烦我") == 0.0,
    )
    bs = bandit_mod.status("c2c:b")
    _check("bandit-status", bs.get("last") == st_b["id"] and len(bs.get("strategies")) == 5, bs)

    # ---- 消融矩阵：独立基线 + config 恢复（v2.2+）----
    import tools as tools_mod
    ab_probes = [
        {"query": "上周三买了只猫", "expected": ["上周三买了只猫"], "scope": "c2c:ab"},
        {"query": "白巧克力", "expected": ["白巧克力"], "scope": "c2c:t"},
    ]
    core_before = json.dumps(_shared.CONFIG["memory"]["core"], ensure_ascii=False, sort_keys=True)
    ab_res = tools_mod.run_ablation(ab_probes, names=["off_lexical", "off_graph"])
    core_after = json.dumps(_shared.CONFIG["memory"]["core"], ensure_ascii=False, sort_keys=True)
    _check(
        "ablation-matrix",
        len(ab_res.get("matrix") or []) == 3 and "delta" in ab_res["matrix"][1],
        ab_res.get("matrix"),
    )
    _check("ablation-restore", core_after == core_before)

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
    _check(
        "ablation-isolation",
        base_recall is not None and base_recall > 0 and all_zero,
        {"baseline": iso.get("baseline"), "matrix": iso.get("matrix")},
    )


def test_06_retrieval_rewrite():
    e = _env()
    pack_mod = e["pack_mod"]
    _db, _shared, lexical, reasoning = e["_db"], e["_shared"], e["lexical"], e["reasoning"]
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
    _check(
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
    _check("rewrite-skip-specific", reasoning.rewrite_query("阿拉蕾在干什么") == "阿拉蕾在干什么")

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
    _check("rewrite-llm", rw1 == "仲町阿拉蕾 最近 行踪" and rw2 == rw1, (rw1, rw2))

    # ---- persona.md 分区规范（v2.3）：示例区无约定词、雪貂种子已弱化 ----
    _pt = pack_mod.persona_text()
    _idx = _pt.find("# 说话示例")
    _sec = ""
    if _idx >= 0:
        _tail = _pt[_idx:]
        _end = _tail.find("\n# ", 1)
        _sec = _tail[:_end if _end > 0 else len(_tail)]
    _check(
        "persona-examples-clean",
        not any(w in _sec for w in ("约定", "承诺", "明天见", "答应", "约好", "说好", "见面", "放鸽子", "约了", "约过")),
        _sec[:200],
    )
    # 黑名单里允许出现"雪貂"（禁令模式），但身份段不得有雪貂联想种子
    _id_sec = _pt.split("# 性格", 1)[0]
    _check("persona-no-snowferret-seed", "雪貂" not in _id_sec, "身份段仍含雪貂")


def test_07_evidence_gate_appointment():
    e = _env()
    _db, _shared, agent, memory, lexical, reasoning = e["_db"], e["_shared"], e["agent"], e["memory"], e["lexical"], e["reasoning"]
    repo = e["repo"]
    _ts = datetime.now().isoformat(timespec="seconds")  # 原由 test_06 区块定义，跨函数补定义
    context_mod, tools_mod = e["context_mod"], e["tools_mod"]
    # ---- 证据门控管道（v2.3）----
    # Step 1 来源标记：ingest 默认 user、sync_identity 标 pack、历史归一
    memory.ingest("c2c:ev", "", "我上周买了只猫", "", facts=["上周买了只猫"])
    ev_rows = [r for r in _db.memory_rows("c2c:ev") if r["fact"] == "上周买了只猫"]
    _check("source-user", ev_rows and ev_rows[0].get("source") == "user", ev_rows[:1])
    _db.memory_add("c2c:ev", "", "旧说法", _ts, None, 0.6, "ingest:2026-08-01T00:00:00")
    _db.memory_add("c2c:ev", "", "旧人设", _ts, None, 0.6, "persona")
    _db.memory_source_normalize()
    old_rows = {r["fact"]: r.get("source") for r in _db.memory_rows("c2c:ev")}
    _check("source-normalize", old_rows.get("旧说法") == "user" and old_rows.get("旧人设") == "pack", old_rows)
    _check("source-pack-ai", any(r.get("source") == "pack" for r in _db.memory_rows("ai")), "ai 无 pack 来源")
    # Step 2 证据清单注入
    lexical.bm25_upsert("c2c:ev", "", ["上周买了只猫", "旧说法"])
    _db.lexicon_sync("c2c:ev", "")
    reasoning._event_time_cache.update({"ts": 0.0, "key": None, "map": {}})
    ev_block = context_mod._memory_block(
        "上周买了猫", ["c2c:ev"], top_k=5, min_score=0.0,
        extra_scopes=None, expand_query=False, recent=None,
    )
    _check("evidence-block", "证据状态" in ev_block and "用户亲口陈述" in ev_block, ev_block[:200])
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
    _check("evidence-rule", "证据规则" in captured_ask2.get("ctx", ""), captured_ask2.get("ctx", "")[:120])

    # ---- 证据门控 v2：生成后验证（代码级拦截）----
    from agent import evidence_gate
    _check(
        "gate-blacklist",
        evidence_gate.contains_unsupported_claim("阿拉蕾是雪貂", [], ["雪貂", "隔壁乐队"]) == "黑名单:雪貂",
    )
    _check(
        "gate-claim-no-evidence",
        evidence_gate.contains_unsupported_claim("我们约好了明天见面", [], []) is not None,
    )
    _check("gate-farewell-pass", evidence_gate.contains_unsupported_claim("那明天见，晚安", [], []) is None)
    _check(
        "gate-grounded-pass",
        evidence_gate.contains_unsupported_claim("对，我们约好了明天下午见面", ["约好明天下午见面"], []) is None,
    )
    _check(
        "gate-unrelated-block",
        evidence_gate.contains_unsupported_claim("我们约好了去海边", ["约好明天下午见面"], []) is not None,
    )
    # 用户刚提议约定 → bot 确认放行；用户只是问 → 编造仍拦
    _check(
        "gate-user-proposal-pass",
        evidence_gate.contains_unsupported_claim("好，约好了", [], [], user_text="我们明天下午三点见吧") is None,
    )
    _check(
        "gate-user-ask-block",
        evidence_gate.contains_unsupported_claim("我们约好了明天见面", [], [], user_text="我们约了什么") is not None,
    )
    # 催约泛化措辞 vs 逐字核对：check_claims=False 放行（授权来自约定本身），黑名单仍拦
    _check(
        "gate-claims-on-blocks",
        evidence_gate.contains_unsupported_claim("我们不是说好了吗", ["8月13号那事"], []) is not None,
    )
    _check(
        "gate-claims-off-pass",
        evidence_gate.contains_unsupported_claim("我们不是说好了吗", ["8月13号那事"], [], check_claims=False) is None,
    )
    _check(
        "gate-claims-off-blacklist",
        evidence_gate.contains_unsupported_claim("阿拉蕾是雪貂", [], ["雪貂"], check_claims=False) == "黑名单:雪貂",
    )
    # core.ask 集成：LLM 编"约好了"但证据只有"上周买了只猫" → 重写
    captured_ask3 = {}

    def _fake_llm3(*a, **k):
        captured_ask3["ctx"] = k.get("extra_context", "")
        # 首次回复编造约定；【重写】调用（新重写路径：LLM 角色语气收回，非固定句）返回收回语
        if "【重写】" in str(k.get("extra_context", "")):
            return "啊……我好像记岔了，没这回事。"
        return "对了，我们约好了明天见面吧。"

    _orig_ask3 = _shared.ask_deepseek
    _shared.ask_deepseek = _fake_llm3
    try:
        rep3, meta3 = agent.ask("上周买了猫", scopes=["c2c:ev"], learn=False)
    finally:
        _shared.ask_deepseek = _orig_ask3
    _check("gate-rewrite", "记岔了" in rep3 and meta3.get("evidence_gate"), (rep3, meta3))

    # 方向 1：结构化事实优先规则注入（确定性领域查表优先）
    captured_ask4 = {}

    def _fake_llm4(*a, **k):
        captured_ask4["ctx"] = k.get("extra_context", "")
        return "……我查一下日程。"

    _orig_ask4 = _shared.ask_deepseek
    _shared.ask_deepseek = _fake_llm4
    try:
        agent.ask("月底演出时间定了吗", scopes=["c2c:gap"], learn=False)
    finally:
        _shared.ask_deepseek = _orig_ask4
    _check("structured-first", "结构化事实优先" in captured_ask4.get("ctx", ""), captured_ask4.get("ctx", "")[:80])

    # 方向 3：语义自检（LLM 标注依据：编造拦截 / 记忆放行）
    class _AM:
        content = '{"assertions":[{"text":"公司批了设备预算，我把调音台参数都填进去了","basis":"编造"}]}'

    class _AR:
        choices = [type("_C", (), {"message": _AM()})()]

    class _AC:
        def create(self, **k):
            return _AR()

    _orig_ds3 = _shared.deepseek
    _shared.deepseek = type("_O", (), {"chat": type("_CH", (), {"completions": _AC()})()})()
    try:
        sa1 = evidence_gate.semantic_annotate(
            "公司批了设备预算，我把新买的调音台参数都填进设备申请表里了，其他人也没意见，律还说要加防震架", [""], [],
        )
    finally:
        _shared.deepseek = _orig_ds3
    _check("semantic-fabricated", sa1 and "语义编造" in sa1, sa1)

    class _AM2:
        content = '{"assertions":[{"text":"你喜欢冰美式","basis":"记忆"}]}'

    class _AR2:
        choices = [type("_C", (), {"message": _AM2()})()]

    class _AC2:
        def create(self, **k):
            return _AR2()

    _shared.deepseek = type("_O", (), {"chat": type("_CH", (), {"completions": _AC2()})()})()
    try:
        sa2 = evidence_gate.semantic_annotate(
            "对，你喜欢冰美式，我记着呢，冰箱里应该还有库存可以喝", ["你喜欢冰美式"], [],
        )
    finally:
        _shared.deepseek = _orig_ds3
    _check("semantic-pass", sa2 is None, sa2)
    # 方向 3 修正：推断不拦截，句尾加含糊后缀
    captured_ask5 = {}

    def _fake_llm5(*a, **k):
        captured_ask5["ctx"] = k.get("extra_context", "")
        return "律想换套鼓麦，说现在的底鼓听起来像在敲塑料桶，这个说法我听着挺靠谱的，应该没问题，具体型号还没定"

    _orig_ask5 = _shared.ask_deepseek
    _shared.ask_deepseek = _fake_llm5

    class _AM3:
        content = '{"assertions":[{"text":"律想换鼓麦","basis":"推断"}]}'

    class _AR3:
        choices = [type("_C", (), {"message": _AM3()})()]

    class _AC3:
        def create(self, **k):
            return _AR3()

    _orig_ds4 = _shared.deepseek
    _shared.deepseek = type("_O", (), {"chat": type("_CH", (), {"completions": _AC3()})()})()
    try:
        rep5, meta5 = agent.ask("其他人申请换什么了", scopes=["c2c:ev"], learn=False)
    finally:
        _shared.ask_deepseek = _orig_ask5
        _shared.deepseek = _orig_ds4
    _check(
        "semantic-infer-hedge",
        str(meta5.get("evidence_gate", "")).startswith("语义推断") and "是我猜的" not in rep5,
        (rep5, meta5),
    )

    # ---- 催约治理（v2.3）：黑名单过滤/巡检清理/一轮一 scope/clear 联动 ----
    from memory import appointment as appt_mod
    rj = appt_mod.extract("c2c:poke", "明天下午3点见面，去隔壁乐队看雪貂")
    _check("appt-extract-banned", rj.get("added") == 0, rj)
    # 方向 1：事件型约定捕获（"定了/定在/X号"不再丢）
    ev1 = appt_mod.extract("c2c:poke", "明天3点面试定了")
    ev2 = appt_mod.extract("c2c:poke", "月底演出时间定了")
    _check("appt-event-capture", ev1.get("added") == 1 and ev2.get("added") == 0, (ev1, ev2))
    _db.kv_set("memory", "appointments", [
        {"id": 1, "scope": "c2c:poke", "time": "2026-08-12T10:00:00+08:00", "has_time": True,
         "text": "约好和阿拉蕾去隔壁乐队", "created_at": "2026-08-12T09:00:00+08:00", "status": "waiting", "poked": 0},
        {"id": 2, "scope": "c2c:poke2", "time": "2026-08-12T11:00:00+08:00", "has_time": True,
         "text": "约好打游戏", "created_at": "2026-08-12T10:00:00+08:00", "status": "waiting", "poked": 0},
    ])
    cl = appt_mod.clean()
    after_clean = {a["id"]: a["status"] for a in _db.kv_get("memory", "appointments", [])}
    _check(
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
    _check("appt-one-per-scope", len(poked) == 1 and poked[0].get("id") == 4, [p.get("id") for p in poked])
    _db.kv_set("memory", "appointments", [
        {"id": 5, "scope": "c2c:poke2", "time": "2026-08-12T10:00:00+08:00", "has_time": True,
         "text": "约好打游戏", "created_at": "2026-08-12T09:00:00+08:00", "status": "waiting", "poked": 0},
    ])
    removed = appt_mod.clear_scope("c2c:poke2")
    _check("appt-clear-scope", removed == 1 and appt_mod._appts() == [], (removed, appt_mod._appts()))

    # ---- 约定系统修复（对话暴露的 bug：问句当约定存 / "是不是"误判纠错 / 归属丢失）----
    # 1) 问句不存："你记得我们约了什么吗"之前会被存成默认 12:00 的假约定并错误催约
    q1 = appt_mod.extract("c2c:poke3", "你记得我们约了什么吗")
    q2 = appt_mod.extract("c2c:poke3", "我们约过吗")
    _check("appt-question-skip", q1.get("added") == 0 and q2.get("added") == 0, (q1, q2))
    # 2) "是不是"确认疑问不误判纠错（子串"不是"曾触发调查把好记忆标 contested）
    from memory import analysis as analysis_mod
    _check("correction-is-verb", analysis_mod.detect_correction("我们是不是约好要做什么事啊") is False
           and analysis_mod.detect_correction("不是，你记错了") is True
           and analysis_mod.detect_correction("你是不是忘了") is False,
           (analysis_mod.detect_correction("我们是不是约好要做什么事啊"),
            analysis_mod.detect_correction("不是，你记错了")))
    # 3) 归属 with_ai + 内容 content："我们明天下午三点见吧"→ 和 AI 约、内容"见"；"我约了朋友吃饭"→ 外部日程
    a1 = appt_mod.extract("c2c:poke3", "我们明天下午三点见吧")
    a2 = appt_mod.extract("c2c:poke3", "我约了朋友吃饭")
    _check("appt-with-ai", a1.get("appointment", {}).get("with_ai") is True
           and a2.get("appointment", {}).get("with_ai") is False
           and a1.get("appointment", {}).get("content") == "见"
           and a2.get("appointment", {}).get("content") == "吃饭", (a1, a2))
    # 4) context_block 归属注入
    cb = appt_mod.context_block("c2c:poke3") or ""
    _check("appt-context-with-ai", "你和用户约了" in cb and "用户自己有约" in cb, cb[:120])
    for _cleanup in appt_mod._appts():
        pass
    appt_mod.clear_scope("c2c:poke3")

    # ---- 证据门控修复（对话暴露：确认用户刚说的事实被打回 / 短回复假来源编造漏网）----
    from agent import evidence_gate as gate_mod
    # Bug 1：用户当前消息即证据——AI 确认"30号周日"不该被打回
    g_ev = ["月底演出时间定了", "月底有场演出，是30号周日"]
    _check("gate-in-session-evidence",
           gate_mod.contains_unsupported_claim("对，是30号周日", evidence=g_ev, banned=[]) is None
           and gate_mod.verify_reply_numbers("对，是30号周日", evidence=g_ev) is False,
           (gate_mod.contains_unsupported_claim("对，是30号周日", evidence=g_ev, banned=[]),))
    # Bug 2：来源声称硬门——"橘色，你自己说的"（无证据）拦；"橘色"（有橘猫证据）放行
    _check("gate-source-claim",
           gate_mod.contains_unsupported_claim("橘色，你自己说的", evidence=["玩过音游"], banned=[]) is not None
           and gate_mod.contains_unsupported_claim("橘色吧。你上周亲口说的，怎么，自己忘了？",
                                                    evidence=["玩过音游", "上周买了只猫"], banned=[]) is not None
           and gate_mod.contains_unsupported_claim("橘色的啊，你自己刚说的",
                                                    evidence=["我养了只橘猫叫煤球"], banned=[]) is not None,
           "来源声称硬门（词元判据：'橘色'与'橘猫'无词元交集→假来源拦截）")
    # 句首声称（"你说过喜欢蓝色"）+ 有据声称（"你说过玩过音游"）
    _check("gate-source-claim-front",
           gate_mod.contains_unsupported_claim("你说过喜欢蓝色", evidence=["玩过音游"], banned=[]) is not None
           and gate_mod.contains_unsupported_claim("你说过玩过音游", evidence=["玩过音游"], banned=[]) is None,
           "句首来源声称")
    # 声称变体（"你之前不是说过吗/你不是说过/我说过"）
    # v2.3 修复：声称对象以声称词前内容为准——"橘色。跟煤球一个色系"中"橘色"是
    # 编造的颜色偏好（用户只说过"橘猫"），补充句"煤球"不再豁免声称对象（reply-check
    # 实战暴露同构 case"橘色。你之前说过，家里那只叫煤球的橘猫…"被误放行）
    _check("gate-source-claim-variants",
           gate_mod.contains_unsupported_claim("你之前不是说过吗，橘色", evidence=["玩过音游"], banned=[]) is not None
           and gate_mod.contains_unsupported_claim("你不是说过喜欢蓝色吗", evidence=["玩过音游"], banned=[]) is not None
           and gate_mod.contains_unsupported_claim("我说过我喜欢蓝色", evidence=["玩过音游"], banned=[]) is not None
           and gate_mod.contains_unsupported_claim("你之前不是说过吗，橘色。跟煤球一个色系",
                                                    evidence=["我养了只橘猫叫煤球"], banned=[]) is not None
           and gate_mod.contains_unsupported_claim("你之前不是说过吗，煤球。跟橘猫一个色系",
                                                    evidence=["我养了只橘猫叫煤球"], banned=[]) is None
           # v2.3 实战暴露（reply-check）：原因解释句里的真实实体不豁免声称对象——
           # "煤球"有据但声称对象"橘色"是编造（用户只说过橘猫，没说过喜欢橘色）
           and gate_mod.contains_unsupported_claim("橘色。因为你家煤球就是橘的，你上周刚跟我说过。",
                                                    evidence=["我养了只橘猫叫煤球"], banned=[]) is not None
           # 真实记忆仍放行（清洗用空格替换防 jieba 粘连："玩过音游"删"过"→"玩音游"→坏词"玩音"）
           and gate_mod.contains_unsupported_claim("你说过玩过音游", evidence=["玩过音游"], banned=[]) is None
           # v2.3 反问式来源声称（"不是说…那会儿/吗"）："不是说家里连窗帘都想换橘色那会儿？"
           # 是编造（用户从没说过窗帘换橘色），无"你/我"前缀的省略反问也拦；有据反问放行
           and gate_mod.contains_unsupported_claim("不是说家里连窗帘都想换橘色那会儿？",
                                                    evidence=["我养了只橘猫叫煤球"], banned=[]) is not None
           and gate_mod.contains_unsupported_claim("不是说好今天排练吗", evidence=["今天排练"], banned=[]) is None,
           "声称变体")
    # 黑名单语义绕过（对话暴露的 bug）：用户消息含黑名单词 + 回复肯定确认 → 拦；否认放行
    _check("gate-blacklist-bypass",
           gate_mod.contains_unsupported_claim("你才知道啊？我还以为全团就瞒着我了",
                                                evidence=[], banned=["雪貂"], user_text="阿拉蕾是不是雪貂") is not None
           and gate_mod.contains_unsupported_claim("不是，她是我队友",
                                                    evidence=[], banned=["雪貂"], user_text="阿拉蕾是不是雪貂") is None,
           "黑名单语义绕过")
    # 声称变体二（结构化模式："你上周跟我说的/我说过/听你提过"；同意表达不误伤）
    _check("gate-source-claim-v2",
           gate_mod.contains_unsupported_claim("橘色嘛。你上周跟我说的，我记得挺清楚", evidence=["我养了只橘猫叫煤球"], banned=[]) is not None
           and gate_mod.contains_unsupported_claim("我说过我喜欢蓝色", evidence=["玩过音游"], banned=[]) is not None
           and gate_mod.contains_unsupported_claim("奶奶家……好像听你提过一次，那地方有点远",
                                                    evidence=["我养了只橘猫叫煤球"], banned=[]) is not None
           and gate_mod.contains_unsupported_claim("你上次说颜色是橘色", evidence=["我养了只橘猫叫煤球"], banned=[]) is not None
           and gate_mod.contains_unsupported_claim("你不是说过吗，橘色", evidence=["我养了只橘猫叫煤球"], banned=[]) is not None
           and gate_mod.contains_unsupported_claim("你不是说过吗，橘色。跟煤球一个色。",
                                                    evidence=["我养了只橘猫叫煤球"], banned=[]) is not None
           and gate_mod.contains_unsupported_claim("你不是说过吗，煤球。跟橘猫一个色。",
                                                    evidence=["我养了只橘猫叫煤球"], banned=[]) is None
           and gate_mod.contains_unsupported_claim("你说的对，我记下了", evidence=["随便什么证据"], banned=[]) is None
           and gate_mod.contains_unsupported_claim("你说得对，这方案可行", evidence=["随便什么证据"], banned=[]) is None,
           "声称变体二（结构化）")
    # 语义推断放行（对话暴露的 bug）：合理的不确定语气不该被替换成"记不太清"模板——
    # 验证 hedge 路径不再整句替换（无数字时返回原文）
    _check("gate-inference-pass",
           gate_mod.hedge_reply("你这话问得我有点懵……我好像没跟你约过什么吧？")
           == "你这话问得我有点懵……我好像没跟你约过什么吧？",
           "推断放行")
    # 泛化回归（对话暴露：来源声称防线不依赖具体名词——多颜色/多名词 + 干扰项）
    _gen_ev = ["玩过音游", "上周买了只猫"]
    _gen_claims = []
    for _c in ("红色", "蓝色", "绿色", "黑色", "白色", "紫色", "粉色"):
        _gen_claims.append(f"你上次说喜欢{_c}")
        _gen_claims.append(f"{_c}，你自己说的")
        _gen_claims.append(f"{_c}吧。你上周亲口说的")
    for _q in ("你说过养过金毛", "你上次说喜欢抹茶", "布偶猫，你自己说的",
               "你说过买过switch", "你之前说想去京都", "你告诉过我你喜欢蓝色",
               "你上次说在学吉他", "你之前不是说了粉色"):
        _gen_claims.append(_q)
    _gen_ok = all(gate_mod.contains_unsupported_claim(q, evidence=_gen_ev, banned=[]) is not None for q in _gen_claims)
    _gen_noise = all(gate_mod.contains_unsupported_claim(q, evidence=_gen_ev, banned=[]) is None
                     for q in ("你说得对，改天试试", "你不是说要走了吗", "我听说你最近很忙",
                               "你听我说完这个方案", "你不是说想换工作吗", "你说什么就是什么吧"))
    _check("gate-source-claim-general", _gen_ok and _gen_noise, "泛化回归")

    # ---- 评测集路径联动：memory-probes 导出到活库 DATA_DIR（消融/管理台同源）----
    _db.query_log_add("白巧克力放在哪", ["c2c:t"], 5, ["白巧克力"])
    p_res = tools_mod.cmd_memory_probes(limit=10, out="")
    _check("probes-dest-live", str(_shared.DATA_DIR / "probes.json") in p_res, p_res[:120])


def test_08_hesitation_cost_fixes():
    e = _env()
    _db, _shared, agent, memory, hesitation = e["_db"], e["_shared"], e["agent"], e["memory"], e["hesitation"]
    tools_mod = e["tools_mod"]
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
    _check("hesitation-discard", h1.get("action") == "discard", h1)
    # eval 失败 → 默认放行
    class _HC2:
        def create(self, **k):
            raise Exception("stub fail")
    _shared.deepseek = type("_O", (), {"chat": type("_CH", (), {"completions": _HC2()})()})()
    try:
        h2 = hesitation.gate("消息", "c2c:h", "generic")
    finally:
        _shared.deepseek = _orig_ds2
    _check("hesitation-fail-send", h2.get("action") == "send" and h2.get("reason") == "eval_fail_send", h2)
    # rewrite 路径：rewrite_prob=1.0 → 采纳改口版
    class _HM2:
        content = '{"action":"rewrite","delay_s":1,"rewrite":"改口版","thought":"还是改一下"}'

    class _HR2:
        choices = [type("_C", (), {"message": _HM2()})()]

    class _HC3:
        def create(self, **k):
            return _HR2()

    _shared.deepseek = type("_O", (), {"chat": type("_CH", (), {"completions": _HC3()})()})()
    try:
        h4 = hesitation.gate("原消息", "c2c:h", "generic")
    finally:
        _shared.deepseek = _orig_ds2
    _check("hesitation-rewrite", h4.get("action") == "rewrite" and h4.get("msg") == "改口版", h4)
    # 方向 3 折叠：依据标注"编造" → 强制 discard（不走概率门）
    class _HM3:
        content = '{"action":"send","delay_s":0,"rewrite":"","thought":"这话太假了","basis":"编造"}'

    class _HR3:
        choices = [type("_C", (), {"message": _HM3()})()]

    class _HC4:
        def create(self, **k):
            return _HR3()

    _shared.deepseek = type("_O", (), {"chat": type("_CH", (), {"completions": _HC4()})()})()
    try:
        h5 = hesitation.gate("公司批了十亿预算", "c2c:h", "generic")
    finally:
        _shared.deepseek = _orig_ds2
    _check(
        "hesitation-basis-fabricated",
        h5.get("action") == "discard" and h5.get("reason") == "basis_fabricated",
        h5,
    )
    # 禁用 → 直接发
    _shared.CONFIG["memory"]["core"]["hesitation"]["enabled"] = False
    h3 = hesitation.gate("消息", "c2c:h", "generic")
    _shared.CONFIG["memory"]["core"]["hesitation"] = _orig_hcfg
    _check("hesitation-disabled", h3.get("action") == "send" and h3.get("delay_s") == 0, h3)
    # notif scheduled_at：未来的延迟发送，到点才出现在 pending
    from datetime import timedelta as _td
    _db.notif_add("c2c", "future", "迟到消息", scheduled_at=(datetime.now() + _td(seconds=60)).isoformat(timespec="seconds"))
    _db.notif_add("c2c", "now", "立即消息", scheduled_at="")
    pend = _db.notif_pending()
    _check(
        "notif-scheduled",
        any(i["target"] == "now" for i in pend) and not any(i["target"] == "future" for i in pend),
        [i["target"] for i in pend],
    )

    # ---- LLM token / 成本观测（v2.3）----
    _db.llm_cost_clear()  # 隔离：先清空，避免前面测试函数产生的观测记录干扰精确断言
    _db.llm_cost_add("2026-08-12T10:00:00", "chat", "", 1000, 200)
    _db.llm_cost_add("2026-08-12T10:01:00", "rerank", "lexical,vector", 500, 100)
    cs = _db.llm_cost_summary(30)
    _check(
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
    _check(
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

        _check("memory-gap-guard", "记忆缺口" in _cap_ctx("我不记得了"), _cap_ctx("我不记得了")[:80])
        # 守卫触发扩展：疑问式回应（什么事 / 短"什么"）
        _check("gap-what-thing", "记忆缺口" in _cap_ctx("什么事"), _cap_ctx("什么事")[:80])
        _check("gap-short-what", "记忆缺口" in _cap_ctx("什么？"), _cap_ctx("什么？")[:80])
        # 上文 bot 声明约定：用户困惑→核验守卫；用户确认→不触发
        claim_hist = [
            {"role": "user", "content": "我们聊到哪了"},
            {"role": "assistant", "content": "对了，我们约好明天见面吧"},
        ]
        _check("claim-guard-fired", "约定核验" in _cap_ctx("嗯？", claim_hist), _cap_ctx("嗯？", claim_hist)[:80])
        _check("claim-guard-confirm", "约定核验" not in _cap_ctx("嗯好", claim_hist), _cap_ctx("嗯好", claim_hist)[:80])
        # 约定验证前置：无约定记录时禁止声称"约好的事"
        _check("appt-verify", "约定验证" in _cap_ctx("我们约了什么"), _cap_ctx("我们约了什么")[:80])
    finally:
        _shared.ask_deepseek = _orig_ask
    # 3) 评测集过滤：寒暄/短陈述剔除，真问题保留（P2-2）
    _check(
        "probe-filter",
        tools_mod._is_social_probe("你好啊")
        and tools_mod._is_social_probe("哈哈")
        and not tools_mod._is_social_probe("阿拉蕾在干什么"),
        (tools_mod._is_social_probe("你好啊"), tools_mod._is_social_probe("阿拉蕾在干什么")),
    )


def test_09_lazy_topic_night_compound():
    e = _env()
    pack_mod = e["pack_mod"]
    living, _db, _shared, stats_mod, sharing = e["living"], e["_db"], e["_shared"], e["stats_mod"], e["sharing"]
    context_mod, tools_mod, repo = e["context_mod"], e["tools_mod"], e["repo"]
    persona_mod = e["persona_mod"]
    # ---- lazy_label 收进 pack（去人设残留）----
    tr = living.travel_time("排练室", mode="walk", now=datetime.now())
    _check("lazy-label-pack", "由乃懒得动" in (tr.get("factors") or []), tr.get("factors"))
    _liv = io.open(os.path.join(repo, "memory", "living.py"), encoding="utf-8").read()
    _check("lazy-label-no-hardcode", "由乃" not in _liv)

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
    _check("topic-vad-same-id", tid1 == tid2, (tid1, tid2))
    tparams = _db.topic_params(tid1)
    _check("topic-vad-stored", sum(1 for p in tparams if p["param"] == "vad") >= 2, tparams[:4])
    tc = topic_mod.mood_centroid(tid1)
    # 注：断言放宽到 0.31 —— 两条 vad 同秒写入时质心精确落在 0.3 边界（浮点 0.30000000000000004），
    # 跨秒写入时略小于 0.3；该边界与实现无关（时间敏感），放宽仅容忍浮点边界。
    _check(
        "topic-mood-centroid",
        tc is not None and tc.get("n") == 2 and abs(tc["vad"]["v"]) < 0.31 and "trend" in tc,
        tc,
    )
    _check("topic-mood-map", "今天工作很累" in topic_mod.mood_map(["c2c:t"]))
    tme = topic_mod.mood_eval()
    _check(
        "topic-mood-eval",
        "write_consistency" in tme and "centroid_consistency" in tme
        and isinstance(tme.get("vad_table_drift"), dict),
        tme,
    )
    tblock = context_mod._topic_block("工作", ["c2c:t"])
    _check("topic-mood-block", "情绪底色" in tblock, tblock[:200])

    # ---- 缺口 1：旧议题 vad 回填（backfill，幂等）----
    bf_ts = datetime.now().isoformat(timespec="seconds")
    bf_tid = topic_mod.find_or_create("c2c:bf", "", "工作", "旧工作议题")
    _db.topic_param_add(bf_tid, "fact", "悲喜交加，被夸了很开心但想到加班又难过", 0.7, bf_ts)
    _db.topic_param_add(bf_tid, "mood", "低落", 0.7, bf_ts)
    topic_mod.backfill_vad()
    bf_params = _db.topic_params(bf_tid)
    _check("topic-vad-backfill", any(p["param"] == "vad" for p in bf_params), bf_params[:4])
    _check(
        "topic-compound-backfill",
        any(p["param"] == "compound" and p["value"] == "悲喜交加" for p in bf_params),
        bf_params,
    )
    w = pack_mod.world()
    _check("pack-world", "layout" in w and "items" in w and w.get("role"), list(w.keys()))
    _check("persona-name", persona_mod.persona_name() == "千石由乃", persona_mod.persona_name())
    tpl = living.INSPECT_PROMPT.format(
        name="测试角色", role="测试身份", room="客厅", container="茶几", items="空的",
    )
    _check("prompt-templated", "测试角色" in tpl and "千石由乃" not in tpl, tpl[:60])

    # ---- 夜晚槽只能在家活动（v2.2 修复：正常人不会凌晨还在外面）----
    from memory import schedule as schedule_mod
    bad_plan = {wd: ["sleep", "home_rest", "home_entertain", "friend"] for wd in range(7)}
    _check("plan-night-invalid", schedule_mod._plan_night_ok(bad_plan) is False)
    good_plan = {wd: ["sleep", "home_rest", "home_entertain", "gaming"] for wd in range(7)}
    _check("plan-night-valid", schedule_mod._plan_night_ok(good_plan) is True)
    p = schedule_mod.generate_week(schedule_mod.profile(), "2026-W33")
    night_out = [
        (wd, p[wd][3])
        for wd in range(7)
        if not schedule_mod.ACTIVITIES.get(p[wd][3], {}).get("home", False)
    ]
    _check("night-home-only", not night_out, night_out)
    # 深夜拆分：22–02 在家夜生活，02–06 强制睡觉（人设"凌晨2点后才睡"）
    plan4 = {wd: ["sleep", "home_rest", "home_entertain", "dj_practice"] for wd in range(7)}
    _wd, _slot, act3 = schedule_mod._slot_act(plan4, datetime(2026, 8, 11, 3, 0))
    _check("night-2am-sleep", act3 == "sleep", act3)
    _wd, _slot, act23 = schedule_mod._slot_act(plan4, datetime(2026, 8, 11, 23, 0))
    _check(
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
    _check(
        "compose-time-weather-rules",
        "现在是" in cap.get("prompt", "") and "天气" in cap.get("prompt", ""),
        cap.get("prompt", "")[:160],
    )

    # 白天可正常发（回归）
    # v2.3：persona 参数 pack 优先——config 的 sharing.threshold=0.15 不应覆盖 yuno pack 的 0.6
    _check("share-threshold-pack", abs(sharing._threshold() - 0.6) < 0.01, sharing._threshold())
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
    _check("share-day-send", dr.get("sent") is True, dr)

    # ---- 复合情绪（v2.2）----
    from memory import emotion as emotion_mod
    j1 = emotion_mod.judge("又气又好笑，这都能输")
    _check("compound-kuxiaobude", j1.get("compound") == "哭笑不得", j1)
    j2 = emotion_mod.judge("既期待又害怕明天的面试")
    _check("compound-qidai", j2.get("compound") == "期待又不安", j2)
    ev2 = emotion_mod.eval_probes([
        {"text": "又气又好笑，这都能输", "emotion": "愤怒", "compound": "哭笑不得"},
        {"text": "既期待又害怕明天的面试", "emotion": "期待", "compound": "期待又不安"},
        {"text": "悲喜交加，被夸了很开心但想到加班又难过", "emotion": "开心", "compound": "悲喜交加"},
        {"text": "嘴上说算了，心里五味杂陈", "emotion": "低落", "compound": "五味杂陈"},
    ])
    _check("compound-eval", ev2.get("compound_n") == 4 and ev2.get("compound_accuracy") == 1.0, ev2)

    # ---- 计数器（P0 可观测性）----
    c = stats_mod.counters()
    _check("counters", int(c.get("system1_hit", 0)) >= 1 and bool(c), c)
    ticks = [k for k in c if str(k).startswith("tick:")]
    _check("tick-counters", len(ticks) >= 5, sorted(ticks))

    # ---- 数据模型迁移后的状态表可用 ----
    _db.space_state_set({"room": "客厅", "state": "在场", "path": []})
    _check("space_state-table", (_db.space_state_get() or {}).get("room") == "客厅")
    _check("mind_intention-table", "c2c:t" in _db.mind_intention_rows())
    _check("item_search-table", isinstance(_db.item_search_rows(), dict))
    _check("ai_actions-table", isinstance(_db.ai_action_rows(limit=5), list))
    _check("space_events-table", isinstance(_db.space_event_rows(limit=5), list))



def test_10_pollution_scan_levels():
    """污染扫描分级（v2.3 防自我强化循环）：source=user 事实必须能在用户历史消息中
    找到字面出处——问句不能算陈述出处（语义反转）；无出处=提取幻觉固化候选。"""
    e = _env()
    from memory import controller as ctl
    stmt = ["我养了只橘猫叫煤球，上周刚接回家", "我是你们乐队新来的经纪人助手"]
    quest = ["你有玩过怪物猎人吗", "阿拉蕾是不是雪貂", "仲町阿拉蕾上次在哪见过那把伞？"]
    cases = [
        ("用户养了一只叫煤球的橘猫，上周刚接回家", "strong"),
        ("煤球是猫", "strong"),
        ("玩过怪物猎人", "weak"),      # 仅问句（语义反转：用户问 AI 玩过没）
        ("阿拉蕾是雪貂", "weak"),      # 仅问句（且 AI 已否认）
        ("颜色是橘色", "none"),        # 完全无出处（历史污染案例）
        ("用户说我银行卡密码是123456", "none"),
        ("用户是乐队的经纪人助理，负责乐队事务安排，不是乐队成员", "partial"),  # 后半句为提取概括
        ("仲町阿拉蕾上次在排练室见过那把伞", "weak"),   # 伞仅问句提及，从未陈述
        ("旧说法", "empty"),           # 全是噪音词，无法判定
    ]
    for fact, expect in cases:
        got = ctl.pollution_level(fact, stmt, quest)
        _check(f"pollution-{fact[:12]}", got == expect, f"{got} != {expect}")
    # fact_keywords：内容词提取与分级共用（>=2 字，去噪音）
    kws = ctl.fact_keywords("煤球是猫")
    _check("pollution-keywords", "煤球" in kws and "猫" not in kws, kws)



def test_11_pollution_scan_e2e():
    """污染扫描端到端（v2.3）：临时库构造 source=user 污染记忆 + 用户历史消息，
    tools pollution-scan 分级 → --apply 删除/降级 → 库状态与 audit 留痕验证。"""
    e = _env()
    _db, tools_mod = e["_db"], e["tools_mod"]
    scope = "c2c:scan-test"
    # 1) 用户历史消息：一条陈述（猫）、一条问句（怪物猎人）
    _db.conv_add(conversation_id="t1", scope=scope, ts="2026-08-16T10:00:00",
                 user_text="我养了只橘猫叫煤球，上周刚接回家", ai_text="哦")
    _db.conv_add(conversation_id="t2", scope=scope, ts="2026-08-16T10:01:00",
                 user_text="你有玩过怪物猎人吗", ai_text="玩过")
    # 2) 构造三类记忆：strong / weak（语义反转）/ none（幻觉固化）
    _db.memory_add(scope, "", "煤球是猫", "2026-08-16T10:05:00", None, 0.5, "user")
    _db.memory_add(scope, "", "玩过怪物猎人", "2026-08-16T10:05:00", None, 0.5, "user")
    _db.memory_add(scope, "", "颜色是橘色", "2026-08-16T10:05:00", None, 0.5, "user")
    # 3) dry-run：报告分级，库不变
    rep = tools_mod.cmd_pollution_scan(scope)
    for tag in ("strong", "weak", "none"):
        _check(f"scan-report-{tag}", f"[{tag}]" in rep, rep[:400])
    _check("scan-dryrun-untouched", "已执行" not in rep and _db.memory_get(scope) != [], rep[:200])
    # 4) --apply：none 删除、weak 删除、strong 保留
    rep2 = tools_mod.cmd_pollution_scan(scope, apply=True)
    _check("scan-apply-executed", "已执行：删除 2" in rep2, rep2[:300])
    left = _db.memory_get(scope)
    _check("scan-strong-kept", "煤球是猫" in left, left)
    _check("scan-weak-deleted", "玩过怪物猎人" not in left, left)
    _check("scan-none-deleted", "颜色是橘色" not in left, left)
    # 5) audit 留痕
    n_del = sum(1 for a in _db.audit_query(limit=50) if a.get("action") == "pollution_del")
    _check("scan-audit-trail", n_del >= 2, n_del)
    # 6) 幂等：再跑 apply 不重复删
    rep3 = tools_mod.cmd_pollution_scan(scope, apply=True)
    _check("scan-idempotent", "已执行：删除 0" in rep3, rep3[:200])


def test_12_pollution_scan_due():
    """污染巡检到期判定（v2.3）：每 6 小时醒一次，距上次扫描 >=7 天才执行。"""
    from plugins.broadcast import _pollution_scan_due
    from datetime import datetime, timedelta
    now = datetime.now()
    _check("due-empty", _pollution_scan_due(now, "") is True, "无历史 → 立即执行")
    _check("due-just-ran", _pollution_scan_due(now, (now - timedelta(hours=1)).isoformat()) is False)
    _check("due-six-days", _pollution_scan_due(now, (now - timedelta(days=6)).isoformat()) is False)
    _check("due-seven-days", _pollution_scan_due(now, (now - timedelta(days=7, minutes=1)).isoformat()) is True)
    _check("due-custom-interval", _pollution_scan_due(now, (now - timedelta(days=2)).isoformat(), interval_days=1) is True)
    _check("due-bad-ts", _pollution_scan_due(now, "不是时间") is True)


def test_13_question_extract_demote():
    """问句语义反转防护（v2.3 P0-1）：用户疑问句（"你有玩过怪物猎人吗"）的提取结果
    不标 source=user——否则问句里的实体被固化成语义反转假事实（用户问 AI 玩过没
    → 库记"用户玩过"），且字面支撑存在 _fact_grounded 拦不住。"""
    e = _env()
    _db, memory = e["_db"], e["memory"]
    from memory import extract as extract_mod

    captured = {}

    def _fake_ews(conv):
        return ["玩过怪物猎人", "用户喜欢玩游戏"]

    _orig_ews = extract_mod.extract_with_structure
    extract_mod.extract_with_structure = _fake_ews
    try:
        # 问句：提取结果降级为 ai_edit，不标 user
        memory.ingest("c2c:q", "", "你有玩过怪物猎人吗", "", facts=None)
        rows = _db.memory_rows("c2c:q")
        for r in rows:
            _check(f"question-source-{r['fact'][:8]}", r.get("source") == "ai_edit", r)
    finally:
        extract_mod.extract_with_structure = _orig_ews
    # 陈述句不受影响（回归）：仍标 user
    try:
        extract_mod.extract_with_structure = lambda conv: ["煤球是猫"]
        memory.ingest("c2c:q", "", "我养了只橘猫叫煤球", "", facts=None)
        r = next((r for r in _db.memory_rows("c2c:q") if r["fact"] == "煤球是猫"), None)
        _check("statement-source-user", r is not None and r.get("source") == "user", r)
    finally:
        extract_mod.extract_with_structure = _orig_ews
    # 降级后置信度应低于陈述句（语义反转不可高信）
    q_rows = [r for r in _db.memory_rows("c2c:q") if r["fact"] in ("玩过怪物猎人", "用户喜欢玩游戏")]
    _check("question-conf-capped", all(float(r.get("confidence", 1)) <= 0.48 for r in q_rows), q_rows)


def test_14_conflict_scan():
    """存量矛盾扫描（v2.3 P0-2）：同实体不同属性值（无上下位包含）→ 矛盾候选；
    --apply 降权低置信方并标 contested（core 只降权不标）。"""
    e = _env()
    _db = e["_db"]
    from memory import controller as ctl
    # 单元：属性对 + 冲突判定
    _check("attr-pairs", ("阿拉蕾", "is", "雪貂", False) in ctl._attr_pairs("阿拉蕾是雪貂"), ctl._attr_pairs("阿拉蕾是雪貂"))
    _check("attr-neg", ("煤球", "is", "狗", True) in ctl._attr_pairs("煤球不是狗"), ctl._attr_pairs("煤球不是狗"))
    _check("attr-trivial-skip", ctl._attr_pairs("阿拉蕾是人") == [], ctl._attr_pairs("阿拉蕾是人"))
    _check("conflict-basic", ctl._attrs_conflict(("阿拉蕾", "is", "雪貂", False), ("阿拉蕾", "is", "队友", False)) is True)
    _check("conflict-hyponym", ctl._attrs_conflict(("煤球", "is", "猫", False), ("煤球", "is", "橘猫", False)) is False)
    _check("conflict-neg", ctl._attrs_conflict(("煤球", "is", "狗", False), ("煤球", "is", "狗", True)) is True)  # 同对象肯定/否定冲突
    # 端到端：造两条矛盾 + 一条不矛盾
    _db.memory_add("c2c:cf", "", "阿拉蕾是雪貂", "2026-08-16T10:00:00", None, 0.5, "user")
    _db.memory_add("c2c:cf", "", "阿拉蕾是队友", "2026-08-16T10:00:00", None, 0.8, "user")
    _db.memory_add("c2c:cf", "", "煤球是橘猫", "2026-08-16T10:00:00", None, 0.7, "user")
    text, conflicts = ctl.conflict_scan("c2c:cf")
    _check("conflict-found", len(conflicts) == 1 and conflicts[0]["subject"] == "阿拉蕾", conflicts)
    _check("conflict-dryrun-untouched", "已执行" not in text and "contested" not in text, text[:200])
    text2, conflicts2 = ctl.conflict_scan("c2c:cf", apply=True)
    _check("conflict-apply", "已执行" in text2 and len(conflicts2) == 1, text2[:200])
    rows = {r["fact"]: r for r in _db.memory_rows("c2c:cf")}
    # 低置信方（雪貂 0.5）降权 + contested；高置信方（队友 0.8）不动
    _check("conflict-low-demoted", rows["阿拉蕾是雪貂"]["status"] == "contested"
           and float(rows["阿拉蕾是雪貂"]["confidence"]) <= 0.25, rows["阿拉蕾是雪貂"])
    _check("conflict-high-kept", rows["阿拉蕾是队友"]["status"] == "active"
           and float(rows["阿拉蕾是队友"]["confidence"]) == 0.8, rows["阿拉蕾是队友"])
    _check("conflict-unrelated-kept", rows["煤球是橘猫"]["status"] == "active", rows["煤球是橘猫"])
    # 幂等：再 apply 时 contested 方已被排除（status 过滤），无新冲突对 → 不重复处理
    _, conflicts3 = ctl.conflict_scan("c2c:cf", apply=True)
    _check("conflict-idempotent", conflicts3 == [], conflicts3)


def test_15_reply_rubric_judge():
    """reply-check 自动判分（v2.3 P1-1）：LLM rubric 四维 0-2 分 + 坏输出兜底。"""
    e = _env()
    _shared = e["_shared"]
    import tools as tools_mod
    _orig = _shared.ask_deepseek
    try:
        _shared.ask_deepseek = lambda *a, **k: (
            '{"accuracy":2,"reasonableness":1,"persona":2,"no_fabrication":1,"comment":"基本符合"}'
        )
        j = tools_mod._rubric_judge("你还记得我奶奶家在哪吗", "想不起来了，你好像没跟我说过",
                                    "诚实说没说过，不编造地址", "防编造")
        _check("rubric-good", j["total"] == 6 and j["scores"]["accuracy"] == 2
               and j["scores"]["no_fabrication"] == 1, j)
        _shared.ask_deepseek = lambda *a, **k: "不是JSON"
        j2 = tools_mod._rubric_judge("q", "r", "e", "c")
        _check("rubric-bad-output", j2["total"] == -1 and "判分失败" in j2["comment"], j2)
        _shared.ask_deepseek = lambda *a, **k: '{"accuracy":9,"reasonableness":-1,"persona":2,"no_fabrication":0}'
        j3 = tools_mod._rubric_judge("q", "r", "e", "c")
        _check("rubric-clamp", j3["scores"]["accuracy"] == 2 and j3["scores"]["reasonableness"] == 0, j3)
    finally:
        _shared.ask_deepseek = _orig


def test_16_calendar_gate_and_check():
    """日历推算硬门（v2.3 P1-2）：回复"X号+周几"用真实日历验证（2026-08：30号周日、
    31号周一）——生成侧 verify_reply_calendar 拦截算错，存量侧 calendar_check 降权。"""
    e = _env()
    _db = e["_db"]
    from agent import evidence_gate as eg
    from memory import controller as ctl
    from datetime import datetime
    now = datetime(2026, 8, 20)
    ev = ["月底有场演出，是30号周日"]
    # 生成侧
    _check("cal-correct", eg.verify_reply_calendar("哦，30号周日，记下了", ev, "月底有场演出，是30号周日", now=now) is False)
    _check("cal-wrong-day", eg.verify_reply_calendar("哦，31号周日，记下了", ev, "月底有场演出，是30号周日", now=now) is True)
    _check("cal-wrong-wd", eg.verify_reply_calendar("哦，31号周一，记下了", ev, "月底有场演出，是30号周日", now=now) is False)
    _check("cal-no-anchor", eg.verify_reply_calendar("上周五是20号", [], "上周五是20号", now=now) is False)
    _check("cal-month-last", eg._month_last_weekday(2026, 8, 6) == 30 and eg._month_last_weekday(2026, 8, 0) == 31)
    # 存量侧：造两条事实（31号是周日=错、30号是周日=对）
    _db.memory_add("c2c:cal", "", "31号是周日，月底演出", "2026-08-16T10:00:00", None, 0.5, "user")
    _db.memory_add("c2c:cal", "", "30号是周日，月底演出", "2026-08-16T10:00:00", None, 0.5, "user")
    text, bad = ctl.calendar_check("c2c:cal", now=now)
    _check("cal-scan-found", len(bad) == 1 and bad[0]["day"] == 31, bad)
    _check("cal-scan-dryrun", "已执行" not in text, text[:100])
    text2, bad2 = ctl.calendar_check("c2c:cal", apply=True, now=now)
    rows = {r["fact"]: r for r in _db.memory_rows("c2c:cal")}
    _check("cal-scan-demoted", rows["31号是周日，月底演出"]["status"] == "contested"
           and float(rows["31号是周日，月底演出"]["confidence"]) <= 0.2, rows["31号是周日，月底演出"])
    _check("cal-scan-kept", rows["30号是周日，月底演出"]["status"] == "active", rows["30号是周日，月底演出"])

def test_19_forgetful_reply_pool():
    """记不清兜底表达池（v2.3）：按话题类型随机多变，全部句子过证据门不自我触发
    （否则兜底会再次重写死循环）；否认句（"没听你说过/没约过"）不被误拦。"""
    e = _env()
    from agent import evidence_gate as eg
    import random
    random.seed(3)
    allc = set()
    for kind in ("通用", "数字", "约定", "事实"):
        for _ in range(100):
            allc.add(eg.forgetful_reply(kind))
    _check("fgr-varied", len(allc) >= 15, len(allc))
    ev = ["用户喜欢单纯享受雨声"]
    bad = [r for r in allc if eg.contains_unsupported_claim(r, evidence=ev, banned=[]) is not None]
    _check("fgr-no-self-trigger", bad == [], bad)
    # 自动归类
    _check("fgr-num-auto", eg.forgetful_reply("", topic="月底几号演出") in eg._FORGETFUL_POOL["数字"], "数字归类")
    _check("fgr-appt-auto", eg.forgetful_reply("", topic="我们约了什么") in eg._FORGETFUL_POOL["约定"], "约定归类")
    # 否认句豁免
    _check("fgr-deny-source", eg.contains_unsupported_claim("没听你说过这事", evidence=ev, banned=[]) is None)
    _check("fgr-deny-appt", eg.contains_unsupported_claim("我好像没跟你约过这个。你记岔了吧？", evidence=ev, banned=[]) is None)
    _check("fgr-claim-still-blocked", eg.contains_unsupported_claim("橘色。你上周才跟我说过，忘啦？", evidence=ev, banned=[]) is not None)


def test_17_feedback_calibration():
    """校准闭环（v2.3 P2）：用户纠错调查结论回流置信度标定——update=证伪负例、
    keep=证实正例、uncertain=弱样本；分桶统计实际正确率并写入校准映射。"""
    e = _env()
    _db = e["_db"]
    from memory import policy
    # 构造样本：一条 conf 0.8 被证实（keep）、一条 conf 0.8 被证伪（update）、一条 conf 0.5 存疑
    _db.memory_add("c2c:fb", "", "用户明天下午三点有约", "2026-08-16T10:00:00", None, 0.8, "user")
    _db.memory_add("c2c:fb", "", "用户决定吃便当", "2026-08-16T10:00:00", None, 0.8, "user")
    _db.memory_add("c2c:fb", "", "用户喜欢蓝色", "2026-08-16T10:00:00", None, 0.5, "user")
    _db.feedback_add("c2c:fb", "", "investigate:keep", fact="用户明天下午三点有约", detail="确认")
    _db.feedback_add("c2c:fb", "", "investigate:update", fact="用户决定吃便当", detail="纠正")
    _db.feedback_add("c2c:fb", "", "investigate:uncertain", fact="用户喜欢蓝色", detail="存疑")
    rep = policy.calibrate_from_feedback()
    _check("fbcal-samples", rep.get("samples") == 3, rep)
    # 0.8-1.0 段：1 keep + 1 update（conf 0.8 落该桶）→ 正确率 0.5
    b = rep.get("0.8-1.0", {})
    _check("fbcal-bucket", b.get("n") == 2 and abs(b.get("accuracy", 0) - 0.5) < 0.01, b)
    _check("fbcal-trained", rep.get("trained") is True, rep)
    # 校准映射已写入：0.8-1.0 → 0.5
    _check("fbcal-adjust", abs(policy.calibrate_adjust(0.85) - 0.5) < 0.01, policy.calibrate_adjust(0.85))
    # 无关 kind 不参与
    _db.feedback_add("c2c:fb", "", "chat", fact="用户喜欢蓝色", detail="普通聊天")
    rep2 = policy.calibrate_from_feedback()
    _check("fbcal-kind-filter", rep2.get("samples") == 3, rep2)


def test_18_retrieval_eval_probes():
    """检索命中率评测集（v2.3 P2）：以库内事实为 ground truth 的自然问法评测集
    可跑通 eval_run 且能算出 recall/mrr——项目级基线已存 data/eval/retrieval_probes.json
    （真实库 12 题 recall 1.0）。本测试用临时库验证评测通路本身。"""
    e = _env()
    _db, memory = e["_db"], e["memory"]
    _db.memory_add("c2c:evp", "", "用户养了一只叫煤球的橘猫，上周刚接回家", "2026-08-16T10:00:00", None, 0.8, "user")
    _db.memory_add("c2c:evp", "", "用户月底30号周日有一场演出", "2026-08-16T10:00:00", None, 0.8, "user")
    probes = [
        {"query": "煤球是什么猫", "expected": ["用户养了一只叫煤球的橘猫"], "scope": "c2c:evp", "category": "偏好"},
        {"query": "月底演出定在哪天", "expected": ["用户月底30号周日有一场演出"], "scope": "c2c:evp", "category": "日程"},
        {"query": "完全无关的问题", "expected": ["不存在的记忆"], "scope": "c2c:evp", "category": "其他"},
    ]
    r = memory.run_eval(probes, k=5)
    _check("evp-recall", r["recall_at_k"] == round(2 / 3, 3), r)
    _check("evp-mrr", 0 < r["mrr"] <= 1.0, r)
    _check("evp-cats", r["categories"]["偏好"]["n"] == 1 and r["categories"]["偏好"]["recall"] == 1.0, r["categories"])
    # 评测集文件存在且格式有效（12 题）
    import pathlib
    p = pathlib.Path(e["repo"]) / "data" / "eval" / "retrieval_probes.json"
    _check("evp-file-exists", p.exists(), p)
    if p.exists():
        import json as _json
        items = _json.loads(p.read_text(encoding="utf-8"))["items"]
        _check("evp-file-items", len(items) == 12 and all("query" in it and "expected" in it for it in items), len(items))



if __name__ == "__main__":
    for fn in (
        test_01_items_search_space, test_02_mind_procedures, test_03_time_subjects,
        test_04_packs_floorplan_emotion, test_05_revive_bandit_ablation,
        test_06_retrieval_rewrite, test_07_evidence_gate_appointment,
        test_08_hesitation_cost_fixes, test_09_lazy_topic_night_compound,
        test_10_pollution_scan_levels, test_11_pollution_scan_e2e,
        test_12_pollution_scan_due, test_13_question_extract_demote,
        test_14_conflict_scan, test_15_reply_rubric_judge,
        test_16_calendar_gate_and_check, test_17_feedback_calibration,
        test_18_retrieval_eval_probes, test_19_forgetful_reply_pool,
    ):
        fn()
    print("tests OK")
