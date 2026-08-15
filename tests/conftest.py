# -*- coding: utf-8 -*-
"""测试默认使用 SQLite 隔离库，避免误连 PostgreSQL 生产库。
如需跑 PG 后端测试，可显式设置 YUNO_DB_BACKEND=postgresql 并配合独立测试库。
"""
import os
import pytest

os.environ.setdefault("YUNO_DB_BACKEND", "sqlite")


@pytest.fixture(autouse=True)
def _force_sleep_awake(monkeypatch):
    """测试不依赖真实时钟：深睡窗口（凌晨 2~5 点）会让 agent.ask 直接离线返回。

    这里统一把 sleep_mode 固定为 awake，避免测试套件在深睡时段随机失败。
    """
    from memory import sleep
    monkeypatch.setattr(sleep, "sleep_mode", lambda now=None: "awake")


@pytest.fixture(scope="module", autouse=True)
def _pg_reset_per_module():
    """PG 模式下每个测试模块开始前清空业务表，尽量模拟 SQLite 临时库隔离。"""
    if os.getenv("YUNO_DB_BACKEND", "sqlite").strip().lower() != "postgresql":
        yield
        return
    from plugins import _db
    conn = _db._connect()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename != 'schema_migrations'"
        )
        tables = [r[0] for r in cur.fetchall()]
        for t in tables:
            try:
                cur.execute(f'TRUNCATE "{t}" RESTART IDENTITY CASCADE')
            except Exception:
                pass
        conn.commit()
    yield
