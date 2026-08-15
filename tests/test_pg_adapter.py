# -*- coding: utf-8 -*-
"""PostgreSQL 适配层冒烟测试（可选）。

默认跳过；设置环境变量 YUNO_PG_TEST=1 且 PostgreSQL 可用时运行。
"""

import os
import pytest

# 默认使用独立测试库，避免污染生产 yuno
os.environ.setdefault("YUNO_PG_DB", "yuno_test")
os.environ.setdefault("YUNO_PG_PASSWORD", "yuno")  # 仅本地测试用

pytestmark = pytest.mark.skipif(
    os.getenv("YUNO_PG_TEST") != "1",
    reason="需要设置 YUNO_PG_TEST=1 才运行 PostgreSQL 适配层测试",
)


def test_pg_core_read_write():
    from plugins import _db_pg as db

    scope = "c2c:pytest_pg"
    db.memory_clear(scope)
    try:
        db.kv_set("pgtest", "x", {"ok": True})
        assert db.kv_get("pgtest", "x") == {"ok": True}

        db.memory_add(scope, "", "用户喜欢猫", "2026-01-01T00:00:00", None, 0.7, "user")
        assert "用户喜欢猫" in db.memory_get(scope)
        rows = db.memory_rows(scope)
        assert any(r["fact"] == "用户喜欢猫" for r in rows)

        db.memory_set_status(scope, "", "用户喜欢猫", "superseded")
        assert all(r["status"] == "superseded" for r in db.memory_rows(scope))

        eid = db.event_add(scope, "", "event", "PG测试事件", ts="2026-01-01T00:00:00", ts_source="explicit")
        assert eid is not None
        assert any(r["title"] == "PG测试事件" for r in db.event_rows(scope))

        db.audit_add("pg.test", "smoke", "hello")
        assert db.audit_query(limit=1, action="pg.test")

        db.conv_add("pg1", scope, "2026-01-01T00:00:00", "你好", "你好呀")
        assert db.conv_rows(scope=scope)
    finally:
        db.memory_clear(scope)


def test_pgvector_optional():
    """pgvector 可用时验证原生检索；不可用时跳过（不影响默认测试）。"""
    from plugins import _db_pg
    if not _db_pg.pgvector_available():
        pytest.skip("pgvector 扩展未安装")
    # 简单插入一条向量并检索
    scope = "c2c:pgvector_test"
    _db_pg.memory_clear(scope)
    try:
        _db_pg.pgvector_build([(scope, "", "测试向量", [1.0, 0.0, 0.0])])
        rows = _db_pg.pgvector_search([1.0, 0.0, 0.0], [scope], top_k=1)
        assert rows and rows[0]["fact"] == "测试向量"
    finally:
        _db_pg.memory_clear(scope)
