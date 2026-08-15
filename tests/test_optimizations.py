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
