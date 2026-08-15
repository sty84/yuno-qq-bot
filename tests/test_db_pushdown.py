# -*- coding: utf-8 -*-
"""② 检索下推回归（P1-4）：memory_rows_by_facts == 全表+Python 过滤；
meta_touch_many == 逐条 meta_touch（一次事务）。scope 用独特前缀避免与其他测试共享库冲突。
"""

import json
import os
import sys
import tempfile
import types

_SCOPE = "c2c:pdtest"  # 独特 scope，避免污染 test_features 的共享库


def _setup():
    stub = types.ModuleType("openai")

    class _OpenAI:
        def __init__(self, *a, **k):
            self.chat = types.SimpleNamespace(completions=None)

    stub.OpenAI = _OpenAI
    sys.modules["openai"] = stub
    tmp = tempfile.mkdtemp(prefix="yuno_pushdown_")
    cfg = {"memory": {"embedder": {"provider": "none"}, "core": {"enabled": True}}}
    os.environ["CONFIG_PATH"] = os.path.join(tmp, "config.json")
    with open(os.environ["CONFIG_PATH"], "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    from plugins import _db
    _db.init(tmp, force=True)  # 强制绑定本测试临时库（防真实库污染）
    return _db


def test_memory_rows_by_facts_equals_filtered():
    """下推查询结果 == 全表拉取 + Python 过滤；superseded 排除；空集合安全。"""
    _db = _setup()
    for i in range(20):
        _db.memory_add(_SCOPE, "", f"事实{i}号", "2026-08-01T10:00:00", None, 0.7, "test")
    _db.memory_add(_SCOPE, "", "已废弃事实", "2026-08-01T10:00:00", None, 0.7, "test")
    _db.memory_set_status(_SCOPE, "", "已废弃事实", "superseded")

    cands = [f"事实{i}号" for i in range(5)] + ["已废弃事实", "不存在的事实"]
    got = _db.memory_rows_by_facts(_SCOPE, cands, exclude_status=("superseded",))
    expect = [
        r for r in _db.memory_rows(_SCOPE)
        if r["fact"] in cands and r["status"] != "superseded"
    ]
    gs = {(r["scope"], r["key"], r["fact"]) for r in got}
    es = {(r["scope"], r["key"], r["fact"]) for r in expect}
    assert gs == es
    assert all(r["fact"] != "已废弃事实" for r in got)
    assert len(got) <= len(cands)
    assert _db.memory_rows_by_facts(_SCOPE, []) == []
    assert _db.memory_rows_by_facts(_SCOPE, None) == []


def test_meta_touch_many_equals_individual():
    """批量隐式反馈与逐条等价：access_count +1、importance 只增不减、空批量安全。"""
    _db = _setup()
    _db.memory_add(_SCOPE, "", "事实1号", "2026-08-01T10:00:00", None, 0.7, "test")
    _db.memory_add(_SCOPE, "", "事实2号", "2026-08-01T10:00:00", None, 0.7, "test")

    _db.meta_touch(_SCOPE, "", "事实1号")  # 逐条（对照）
    _db.meta_touch_many([(_SCOPE, "", "事实2号", 0.5, "")])  # 批量
    rows = {r["fact"]: r for r in _db.meta_rows(_SCOPE)}
    assert rows["事实1号"]["access_count"] == 1
    assert rows["事实2号"]["access_count"] == 1
    assert rows["事实2号"]["importance"] == 0.5

    # importance 只增不减（0.9 覆盖 0.5；0.2 不覆盖）
    _db.meta_touch_many([(_SCOPE, "", "事实2号", 0.9, "")])
    rows = {r["fact"]: r for r in _db.meta_rows(_SCOPE)}
    assert rows["事实2号"]["importance"] == 0.9
    _db.meta_touch_many([(_SCOPE, "", "事实2号", 0.2, "")])
    rows = {r["fact"]: r for r in _db.meta_rows(_SCOPE)}
    assert rows["事实2号"]["importance"] == 0.9
    assert _db.meta_touch_many([]) is None


# ---- ① ingest 事务化：中途异常整体回滚，不留半成品 ----

def test_ingest_transaction_rollback_no_partial():
    """ingest 主写段中途异常 → 事务回滚：memory_replace 已写入的事实也不残留
    （无事务时是"事实已存但事件图/议题缺失"的半成品）。"""
    _db = _setup()
    _db.memory_add(_SCOPE, "", "旧记忆", "2026-08-01T10:00:00", None, 0.7, "user")
    before_rows = len(_db.memory_rows(_SCOPE))
    before_events = len(_db.event_rows(_SCOPE) or [])
    before_topics = len(_db.topic_rows(_SCOPE) or [])

    import memory.controller as ctl
    orig = ctl.graph.build_for_fact

    def _boom(*a, **k):
        raise RuntimeError("模拟主写段中途故障")

    ctl.graph.build_for_fact = _boom
    try:
        try:
            ctl.ingest(_SCOPE, "", "今天买了只橘猫", facts=["今天买了只橘猫，花了500块"])
        except RuntimeError:
            pass  # 事务内异常应向上传播（由调用方决定是否吞）
        else:
            assert False, "ingest 应把事务内异常向上传播"
    finally:
        ctl.graph.build_for_fact = orig

    after_rows = len(_db.memory_rows(_SCOPE))
    after_events = len(_db.event_rows(_SCOPE) or [])
    after_topics = len(_db.topic_rows(_SCOPE) or [])
    assert after_rows == before_rows, f"回滚后记忆数应不变: {before_rows} -> {after_rows}"
    assert after_events == before_events, f"事件图应回滚: {before_events} -> {after_events}"
    assert after_topics == before_topics, f"议题应回滚: {before_topics} -> {after_topics}"
