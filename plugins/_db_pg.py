# -*- coding: utf-8 -*-
"""PostgreSQL 适配层骨架（迁移后使用）。

当前项目默认仍走 SQLite；此模块提供 PG 连接与基础只读能力，
供迁移验证、运维脚本和后续完整适配使用。

环境变量：
  YUNO_PG_HOST / YUNO_PG_PORT / YUNO_PG_DB / YUNO_PG_USER / YUNO_PG_PASSWORD
"""

import os

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:  # 未安装时保持可导入
    psycopg2 = None
    RealDictCursor = None


def dsn() -> str:
    return (
        f"host={os.getenv('YUNO_PG_HOST', '127.0.0.1')} "
        f"port={os.getenv('YUNO_PG_PORT', '5432')} "
        f"dbname={os.getenv('YUNO_PG_DB', 'yuno')} "
        f"user={os.getenv('YUNO_PG_USER', 'esp')} "
        f"password={os.getenv('YUNO_PG_PASSWORD', 'yuno')}"
    )


def connect():
    if psycopg2 is None:
        raise RuntimeError("未安装 psycopg2-binary，请先 pip install -r requirements-pg.txt")
    return psycopg2.connect(dsn())


def health() -> dict:
    """连接检查 + 基础信息。"""
    try:
        conn = connect()
        with conn.cursor() as cur:
            cur.execute("SELECT version()")
            version = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
            table_count = cur.fetchone()[0]
        conn.close()
        return {"ok": True, "version": version, "table_count": table_count}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def table_counts() -> dict:
    """返回 public schema 下各表行数（用于迁移验证）。"""
    conn = connect()
    out = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
        )
        tables = [r[0] for r in cur.fetchall()]
        for t in tables:
            try:
                cur.execute(f'SELECT COUNT(*) FROM "{t}"')
                out[t] = cur.fetchone()[0]
            except Exception:
                out[t] = None
    conn.close()
    return out
