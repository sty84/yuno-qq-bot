# -*- coding: utf-8 -*-
"""针对近期优化点的回归测试：
1) daily_reflect 死代码修复后确实会把反思写入 AI 记忆
2) assemble_context 能通过 evidence_out 显式返回本次检索证据，不再只靠全局变量
3) record_negative_feedback 能按检索通道降低命中计数
4) active_edit 不会绕过 promote_core 直接把非稳定事实写成 core
"""

import json
import os
import sys
import tempfile
import types


def _setup(prefix="yuno_opt_"):
    stub = types.ModuleType("openai")

    class _OpenAI:
        def __init__(self, *a, **k):
            self.chat = types.SimpleNamespace(completions=None)

    stub.OpenAI = _OpenAI
    sys.modules["openai"] = stub
    tmp = tempfile.mkdtemp(prefix=prefix)
    cfg = {"memory": {"embedder": {"provider": "none"}, "core": {"enabled": True}}}
    os.environ["CONFIG_PATH"] = os.path.join(tmp, "config.json")
    with open(os.environ["CONFIG_PATH"], "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    from plugins import _db, _shared
    _shared.CONFIG_PATH = os.environ["CONFIG_PATH"]
    _shared.reload_config()
    _db.init(tmp, force=True)
    return _db, _shared


def test_daily_reflect_persists_insights(monkeypatch):
    _db, _shared = _setup("yuno_refl_")
    from memory import advisor

    _db.event_add(
        "c2c:refl", "", "event", "用户聊了项目进展",
        ts="2026-08-01T10:00:00", ts_source="explicit",
    )

    class _FakeResp:
        def __init__(self, content):
            self.choices = [types.SimpleNamespace(message=types.SimpleNamespace(content=content))]

    monkeypatch.setattr(_shared, "deepseek_chat", lambda **kwargs: _FakeResp("用户偏好稳定\n第二条用户洞察也很重要\n"))
    import memory.stats as st
    before = int(st.counters().get("reflect_insight", 0))
    n = advisor.daily_reflect(limit=10)
    assert n == 2, n
    after = int(st.counters().get("reflect_insight", 0))
    assert after - before == n, (before, after)
    rows = _db.memory_rows("ai", "reflection")
    texts = [r["fact"] for r in rows]
    assert "用户偏好稳定" in texts
    assert "第二条用户洞察也很重要" in texts


def test_assemble_context_evidence_out():
    _db, _shared = _setup("yuno_evout_")
    from memory import context

    scope = "c2c:evout"
    fact = "用户养了一只橘猫"
    _db.memory_add(scope, "", fact, "2026-08-01T10:00:00", None, 0.7, "user")
    evidence = []
    ctx = context.assemble_context("用户养了什么猫", [scope], top_k=5, min_score=0.0, evidence_out=evidence)
    assert ctx, "应能组装出记忆上下文"
    assert fact in evidence, evidence


def test_record_negative_feedback():
    _db, _shared = _setup("yuno_fb_")
    from memory import reasoning

    reasoning._route_cache = None
    reasoning._last_details = {"用户养了橘猫": {"channels": ["lexical", "vector"]}}
    # 先给一个已有统计
    reasoning._route_cache = {
        "lexical": {"trials": 5, "hits": 3},
        "vector": {"trials": 5, "hits": 4},
    }
    ok = reasoning.record_negative_feedback("用户养了橘猫")
    assert ok is True
    stats = reasoning._route_cache
    assert stats["lexical"]["hits"] == 2
    assert stats["vector"]["hits"] == 3
    assert stats["lexical"]["misses"] == 1


def test_record_negative_feedback_scope_alignment():
    _db, _shared = _setup("yuno_fb_align_")
    from memory import reasoning
    import time

    reasoning._route_cache = {
        "lexical": {"trials": 5, "hits": 3},
    }
    reasoning._last_retrieval["c2c:fb"] = {
        "ts": time.time(),
        "facts": {"用户养了橘猫"},
        "details": {"用户养了橘猫": {"channels": ["lexical"]}},
    }
    # 不在最近一次检索结果里 -> 不惩罚
    ok = reasoning.record_negative_feedback("无关事实", scope="c2c:fb")
    assert ok is False
    assert reasoning._route_cache["lexical"]["hits"] == 3

    # 在最近一次检索结果里 -> 惩罚
    ok = reasoning.record_negative_feedback("用户养了橘猫", scope="c2c:fb")
    assert ok is True
    assert reasoning._route_cache["lexical"]["hits"] == 2


def test_retrieval_contextvar_isolation_between_threads():
    _db, _shared = _setup("yuno_ctx_")
    from memory import reasoning
    import threading

    scope_a = "c2c:ctx_a"
    scope_b = "c2c:ctx_b"
    _db.memory_add(scope_a, "", "用户喜欢猫", "2026-01-01T00:00:00", None, 0.7, "user")
    _db.memory_add(scope_b, "", "用户喜欢狗", "2026-01-01T00:00:00", None, 0.7, "user")

    reasoning.retrieve("用户喜欢什么", [scope_a], top_k=3, min_score=0.0)
    main_before = set(reasoning.current_details().keys())
    assert "用户喜欢猫" in main_before

    def worker():
        reasoning.retrieve("用户喜欢什么", [scope_b], top_k=3, min_score=0.0)
        worker_keys = set(reasoning.current_details().keys())
        assert "用户喜欢狗" in worker_keys
        assert "用户喜欢猫" not in worker_keys, "worker 不应看到 main 的检索明细"

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    main_after = set(reasoning.current_details().keys())
    assert main_after == main_before, (main_before, main_after)


def test_cognitive_architecture_interfaces():
    _db, _shared = _setup("yuno_cog_")
    _db.memory_add("c2c:cog", "", "用户养了一只橘猫", "2026-01-01T00:00:00", None, 0.7, "user")
    from memory.interfaces import default_architecture
    arch = default_architecture()
    result = arch.run("用户养了什么猫", scope="c2c:cog")
    assert isinstance(result.activated_memories, list)
    assert any("橘猫" in h["fact"] for h in result.activated_memories)
    assert result.action is not None


def test_memory_consolidator():
    _db, _shared = _setup("yuno_cons_")
    from memory import consolidator
    _db.memory_add("c2c:cons", "", "用户喜欢猫", "2026-01-01T00:00:00", None, 0.7, "user")
    _db.memory_add("c2c:cons", "", "用户讨厌猫", "2026-01-01T00:00:00", None, 0.7, "user")
    report = consolidator.run(scope="c2c:cons", apply=False)
    assert "fragments_merged" in report
    assert "conflicts" in report
    assert "promoted" in report
    assert "forgotten" in report


def test_skill_library():
    _db, _shared = _setup("yuno_skill_")
    from memory import skills
    skills.record("用户催约", "先查约定表再否认", result="通过门控", source="test")
    skills.mark_failure("用户问边界", "直接答应", reason="越界", source="reflection")
    rows = skills.search("催约", limit=5)
    assert rows and any("查约定表" in r["action"] for r in rows)
    fail = skills.search("边界", limit=5)
    assert fail and any(r["failure_reason"] == "越界" for r in fail)
    skills.update("用户催约", "先查约定表再否认", success=0.95)
    updated = skills.search("催约", limit=5)
    assert updated and updated[0]["success"] == 0.95


def test_mbti_plugin_flow():
    _db, _shared = _setup("yuno_mbti_")
    from plugins import mbti
    class Ctx:
        chat_key = "c2c:mbtitest"
    _db.kv_set("mbti", "c2c:mbtitest", None)
    assert "已重置" in mbti.handle("/mbti 重置", Ctx())
    first = mbti.handle("/mbti", Ctx())
    assert "第 1/8 题" in first
    for ans in ["A", "B", "A", "B", "A", "B", "A", "B"]:
        r = mbti.handle(f"/mbti {ans}", Ctx())
        assert r
    final = mbti.handle("/mbti 结果", Ctx())
    assert "MBTI" in final
    _db.kv_set("mbti", "c2c:mbtitest", None)


def test_schema_migration_and_scope_meta():
    _db, _shared = _setup("yuno_schema_")
    assert _db._schema_version() == _db.SCHEMA_VERSION
    conn = _db._connect()
    if hasattr(conn, "cursor"):
        cur = conn.cursor()
        try:
            cur.execute("SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='scope_meta'")
        except Exception:
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='scope_meta'")
        row = cur.fetchone()
        cur.close()
    else:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='scope_meta'"
        ).fetchone()
    assert row is not None


def test_conflict_scan_detects_like_dislike():
    _db, _shared = _setup("yuno_cf_")
    from memory import controller as ctl
    _db.memory_add("c2c:cf", "", "用户喜欢猫", "2026-01-01T00:00:00", None, 0.7, "user")
    _db.memory_add("c2c:cf", "", "用户讨厌猫", "2026-01-01T00:00:00", None, 0.7, "user")
    _report, conflicts = ctl.conflict_scan("c2c:cf")
    assert len(conflicts) == 1, conflicts


def test_auto_adjust_true_applies_params():
    _db, _shared = _setup("yuno_conv_auto_")
    from memory import convreview, trace

    _db.conv_add("c2c:auto", "c2c:auto", "2026-08-15T00:00:00", "你好", "你好呀")
    rows = _db.conv_rows(limit=1)
    assert rows
    _db.conv_review_add(
        rows[0]["id"], 2,
        {"remember": 2, "natural": 2, "emotional": 4, "proactive": 4, "boundary": 2},
    )
    # 打开自动调参并强制刷新报告
    _shared.CONFIG.setdefault("memory", {}).setdefault("core", {})["convreview"] = {"auto_adjust": True}
    convreview.report(force=True)
    rec = convreview.apply_adjustments()
    assert rec["auto_adjust"] is True
    assert rec["applied"] is True
    assert "privacy_threshold" in rec["params"]
    assert "confidence_factor" in rec["params"]
    audit_rows = _db.audit_query(limit=5, action="conv_auto_adjust")
    assert audit_rows, "自动调参应写入审计"

    adj = trace.adjustments(force=True)
    assert adj.get("convreview_applied") is True
    assert adj.get("privacy_threshold") == 0.6
    assert adj.get("confidence_factor") == 0.9

    # 回滚后恢复
    convreview.rollback_adjustments()
    adj2 = trace.adjustments(force=True)
    assert adj2.get("convreview_applied") is None


def test_conv_adjustments_visible_in_trace():
    _db, _shared = _setup("yuno_conv_trace_")
    from memory import convreview, trace
    convreview.apply_adjustments()
    adj = trace.adjustments(force=True)
    assert "convreview" in adj
    assert adj["convreview"]["auto_adjust"] is False
    assert "suggestions" in adj["convreview"]


def test_conv_adjustments_dry_run():
    _db, _shared = _setup("yuno_convadj_")
    from memory import convreview
    rec = convreview.apply_adjustments()
    assert rec["auto_adjust"] is False
    assert rec["applied"] is False
    assert "suggestions" in rec
    saved = _db.kv_get("memory", "conv_adjustments")
    assert saved is not None and saved["auto_adjust"] is False


def test_reflection_quality_rejects_generic():
    _db, _shared = _setup("yuno_reflq_")
    from memory import advisor
    evs = [{"title": "用户聊了项目进展"}]
    assert advisor._reflection_quality("继续加油", evs, []) is False
    assert advisor._reflection_quality("用户偏好稳定", evs, []) is True


def test_hesitation_safe_rule_skips_llm():
    _db, _shared = _setup("yuno_hes_")
    from memory import hesitation
    out = hesitation.gate("晚安", scope="c2c:x", kind="share")
    assert out["action"] == "send"
    assert out["reason"] == "safe_rule"


def test_active_edit_not_direct_core():
    _db, _shared = _setup("yuno_ae_")
    from memory import controller

    scope = "c2c:ae"
    result = controller._apply_ops(scope, "", [{"op": "remember", "fact": "用户今天心情不错", "mclass": "core"}])
    assert result["remember"] == 1
    rows = _db.memory_rows(scope, "")
    assert len(rows) == 1
    # 非稳定事实不应直接进 core，应落为 long，等待 promote_core 安全升级
    assert rows[0]["mclass"] == "long", rows[0]


def test_vecindex_cosine_and_kmeans():
    _db, _shared = _setup("yuno_vec_")
    from memory import vecindex
    assert vecindex._cosine([1, 0], [0, 1]) == 0.0
    assert abs(vecindex._cosine([1, 1], [1, 1]) - 1.0) < 1e-6
    centroids = vecindex._kmeans([[1, 0], [0, 1], [1, 1]], nlist=2)
    assert len(centroids) == 2


def test_world_snapshot_and_subject_gate():
    _db, _shared = _setup("yuno_world_")
    from memory import world
    _db.memory_add("c2c:w", "", "用户喜欢猫", "2026-01-01T00:00:00", None, 0.7, "user")
    snap = world.snapshot("c2c:w")
    assert isinstance(snap, str)
    assert "用户喜欢猫" in snap
    assert world.subject_gate("c2c:w", "", "用户喜欢猫") is False  # c2c 私聊不传播
    assert world.subject_confidence("experienced") == 0.9
    stats = world.stats()
    assert stats["active"] >= 1


def test_sensors_flow():
    _db, _shared = _setup("yuno_sensors_")
    from memory import sensors
    ok = sensors.set_device("客厅灯", "开")
    assert ok["ok"] is True
    assert sensors.device_state("客厅灯")["state"] == "开"
    ev = sensors.sensor_event("门铃", "ring", "门铃响了")
    assert ev["kind"] == "ring"
    assert len(sensors.recent_events(seconds=3600)) >= 1
    block = sensors.block("客厅")
    assert isinstance(block, str)


def test_tools_eval_commands():
    _db, _shared = _setup("yuno_evalcmd_")
    from tools.eval import cmd_evidence_gate_eval, cmd_memory_eval
    res = cmd_evidence_gate_eval()
    assert '"total": 50' in res

    probes = [{"query": "煤球是什么猫", "expected": ["用户养了一只叫煤球的橘猫"], "scope": "c2c:evalcmd", "category": "偏好"}]
    _db.memory_add("c2c:evalcmd", "", "用户养了一只叫煤球的橘猫", "2026-01-01T00:00:00", None, 0.8, "user")
    import pathlib as _p
    p = _p.Path(tempfile.mkdtemp()) / "probes.json"
    p.write_text(json.dumps(probes, ensure_ascii=False), encoding="utf-8")
    res2 = cmd_memory_eval(str(p), k=5, save=False)
    assert '"recall_at_k": 1.0' in res2


def test_trace_review_and_render():
    _db, _shared = _setup("yuno_trace_")
    from memory import trace
    trace._dedup_cache.clear()
    trace.record("c2c:trace", raw_content="用户说月底有演出", action="create", reasoning="test")
    rows = _db.trace_rows(scope="c2c:trace", limit=10)
    assert rows
    tid = rows[0]["id"]
    res = trace.score(tid, {"extraction": 5, "decision": 5, "confidence": 5, "provenance": 5, "privacy": 5}, reviewer="test")
    assert "已记录评分" in res
    md = trace.render_markdown(rows, {tid: {"score": 5}})
    assert isinstance(md, str)
    assert isinstance(trace.detect_modules("ai", "belief", "目标：写歌"), list)
    assert isinstance(trace.prune(days=30), int)
    adj = trace.adjustments(force=True)
    assert isinstance(adj, dict)


def test_character_markdown_flow():
    _db, _shared = _setup("yuno_char_")
    from memory import character
    md = "# 测试角色\n\n## 经历\n- 曾经是鼓手\n"
    parsed = character.parse_markdown(md)
    assert isinstance(parsed, dict)
    out = character.render_markdown("测试角色", parsed)
    assert "测试角色" in out
    p = character.write_markdown("测试角色", parsed, out_dir=tempfile.mkdtemp())
    assert p.exists()
    synced = character.sync_from_markdown(name="测试角色", path=str(p))
    assert synced.get("name") == "测试角色"
    assert isinstance(character.search("测试"), list)
    assert isinstance(character.match_scopes("测试角色"), list)


def test_mistake_flow():
    _db, _shared = _setup("yuno_mist_")
    from datetime import datetime
    from memory import mistake
    rec = mistake.record("c2c:mist", "我记错了，是明天", now=datetime.now())
    assert rec.get("recorded") == 1
    assert isinstance(mistake.anger_of(rec, datetime.now()), dict)
    assert isinstance(mistake.forgive_probability(rec, "c2c:mist", now=datetime.now()), float)
    ctx = mistake.context_block("c2c:mist", "对不起")
    assert isinstance(ctx, str)


def test_expression_flow():
    _db, _shared = _setup("yuno_expr_")
    from memory import expression
    assert isinstance(expression.detect_expressions("笑死我了"), list)
    an = expression.analyze("笑死我了")
    assert isinstance(an, dict)
    upd = expression.profile_update("c2c:expr", "笑死我了")
    assert isinstance(upd, dict)
    assert isinstance(expression.profile_get("c2c:expr"), dict)
    assert isinstance(expression.describe("c2c:expr"), str)


def test_scenario_replay_offline():
    _db, _shared = _setup("yuno_scen_")
    import pathlib as _p
    scenarios = [{"id": "s1", "scope": "c2c:scen", "messages": [{"user": "你好"}], "expected": []}]
    p = _p.Path(tempfile.mkdtemp()) / "scenarios.json"
    p.write_text(json.dumps(scenarios, ensure_ascii=False), encoding="utf-8")
    _shared.ask_deepseek = lambda *a, **k: "……嗯。"
    from tools.core import scenario_replay
    res = scenario_replay(path=str(p))
    assert res["replayed"] == 1
    assert res["scenarios"][0]["replies"][0]["ai"] == "……嗯。"


def test_tools_admin_config_validate():
    _db, _shared = _setup("yuno_adm_")
    from tools.admin import cmd_config_validate
    code, text = cmd_config_validate()
    assert code in (0, 1)
    assert isinstance(text, str)


def test_embedder_none_mode():
    _db, _shared = _setup("yuno_emb_")
    from memory import embedder
    assert embedder.enabled() is False
    assert embedder.embed(["你好"]) is None
    assert embedder.cosine([1, 0], [0, 1]) == 0.0


def test_revive_flow():
    _db, _shared = _setup("yuno_rev_")
    import time as _time
    from memory import revive
    assert isinstance(revive.poisson_p(None), float)
    assert revive.poisson_p(_time.time()) < 1.0
    assert isinstance(revive.state_posterior("c2c:rev"), dict)
    assert isinstance(revive.peek("c2c:rev"), dict)
    assert isinstance(revive.decide("c2c:rev", force=True), dict)


def test_interaction_modulate():
    _db, _shared = _setup("yuno_inter_")
    from memory import interaction
    assert isinstance(interaction.scene_mult("chat", "c2c"), float)
    assert isinstance(interaction.relation_mult("c2c:inter"), float)
    assert isinstance(interaction.user_mult("c2c:inter"), dict)
    assert isinstance(interaction.fatigue_mult("chat"), float)
    interaction.mark_event("chat")
    assert isinstance(interaction.modulate("c2c:inter", "chat", base=1.0), float)


def test_tools_admin_dump_json():
    _db, _shared = _setup("yuno_dump_")
    from tools.admin import cmd_data_dump_json
    out = os.path.join(tempfile.mkdtemp(), "dump.json")
    res = cmd_data_dump_json(out)
    assert os.path.exists(out)
    assert "已导出" in res


def test_relationship_update_describe():
    _db, _shared = _setup("yuno_rel_")
    from memory import relationship
    row = relationship.update("c2c:rel", subject="用户", event="chat")
    assert row and row["scope"] == "c2c:rel"
    assert isinstance(relationship.describe("c2c:rel"), str)
    assert isinstance(relationship.rows(), list)
    assert relationship.note_return("c2c:rel") in (True, False)


def test_tools_memory_probes():
    _db, _shared = _setup("yuno_probes_")
    from tools.memory import cmd_memory_probes
    _db.query_log_add("白巧克力放在哪", ["c2c:probe"], 5, ["白巧克力"])
    out = os.path.join(tempfile.mkdtemp(), "probes.json")
    res = cmd_memory_probes(limit=10, out=out)
    assert os.path.exists(out)
    assert "已导出" in res


def test_time_extract():
    _db, _shared = _setup("yuno_time_")
    from datetime import datetime
    from memory import time_extract
    res = time_extract.extract("明天下午三点", scope="c2c:time")
    assert isinstance(res, dict)
    assert isinstance(time_extract.label_for("2026-08-16T15:00:00", now=datetime(2026, 8, 16, 12, 0)), str)


def test_tools_memory_views():
    _db, _shared = _setup("yuno_memviews_")
    from tools.memory import (
        cmd_memory_history,
        cmd_memory_sessions,
        cmd_memory_topics,
        cmd_persona_probes,
        cmd_relationship,
    )
    _db.memory_add("c2c:mv", "", "用户喜欢猫", "2026-01-01T00:00:00", None, 0.7, "user")
    _db.topic_add("c2c:mv", "", "偏好", "猫", importance=0.5, confidence=0.7)
    _db.session_create("c2c:mv", "", topic="猫", summary="用户喜欢猫")
    assert isinstance(cmd_memory_topics("c2c:mv", 10), str)
    assert isinstance(cmd_memory_sessions("c2c:mv", 10), str)
    assert isinstance(cmd_memory_history("c2c:mv", 10), str)
    assert isinstance(cmd_relationship("c2c:mv"), str)
    out = os.path.join(tempfile.mkdtemp(), "persona.json")
    assert isinstance(cmd_persona_probes(out=out), str)
