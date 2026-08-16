# -*- coding: utf-8 -*-
"""PostgreSQL 数据层核心：连接、连接池、schema、事务。

由 plugins/_db_pg.py 统一装配；对外仍通过 plugins._db 使用。
"""
import os
import threading
from contextlib import contextmanager
from datetime import datetime

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    psycopg2 = None
    RealDictCursor = None

try:
    from psycopg2.pool import ThreadedConnectionPool
except ImportError:
    ThreadedConnectionPool = None

DB_PATH = None
_conn = None
_pool = None
_tlocal = threading.local()
_lock = threading.RLock()
SCHEMA_VERSION = 1
_txn_depth = 0


def dsn() -> str:
    password = os.getenv("YUNO_PG_PASSWORD")
    if not password:
        raise RuntimeError("YUNO_PG_PASSWORD 未设置，拒绝使用默认弱密码")
    return (
        f"host={os.getenv('YUNO_PG_HOST', '127.0.0.1')} "
        f"port={os.getenv('YUNO_PG_PORT', '5432')} "
        f"dbname={os.getenv('YUNO_PG_DB', 'yuno')} "
        f"user={os.getenv('YUNO_PG_USER', 'esp')} "
        f"password={password}"
    )


def init(data_dir=None, force=False):
    global _conn, _pool
    if _pool is not None and not force:
        return
    _tlocal.conn = None
    if _pool is not None:
        try:
            _pool.close()
        except Exception:
            pass
        _pool = None
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
    # 用一次性连接做 schema 初始化
    _conn = psycopg2.connect(dsn(), keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3)
    _conn.autocommit = False
    _ensure_schema_migrations()
    with _conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM schema_migrations")
        if cur.fetchone()[0] == 0:
            cur.execute(
                "INSERT INTO schema_migrations(version,applied_at) VALUES(%s,%s)",
                (SCHEMA_VERSION, datetime.now().isoformat(timespec="seconds")),
            )
            _conn.commit()
    try:
        with _conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_memories_fact_trgm ON memories USING gin (fact gin_trgm_ops)")
            _conn.commit()
    except Exception:
        _conn.rollback()
    try:
        with _conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute(
                "CREATE TABLE IF NOT EXISTS vec_pg("
                "scope TEXT NOT NULL, key TEXT NOT NULL DEFAULT '', fact TEXT NOT NULL, "
                "embedding vector, PRIMARY KEY(scope,key,fact))"
            )
            cur.execute(
                "CREATE TABLE IF NOT EXISTS skills("
                "id SERIAL PRIMARY KEY, situation TEXT NOT NULL, action TEXT NOT NULL, "
                "result TEXT NOT NULL DEFAULT '', condition TEXT NOT NULL DEFAULT '', "
                "failure_reason TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '', "
                "success DOUBLE PRECISION NOT NULL DEFAULT 0.5, tries INTEGER NOT NULL DEFAULT 0, "
                "updated_at TEXT, UNIQUE(situation, action))"
            )
            _conn.commit()
    except Exception:
        _conn.rollback()
    _conn.close()
    _conn = None
    if ThreadedConnectionPool is None:
        raise RuntimeError("psycopg2.pool 不可用")
    minconn = max(1, int(os.getenv("YUNO_PG_MINCONN", "1")))
    maxconn = max(minconn, int(os.getenv("YUNO_PG_MAXCONN", "8")))
    _pool = ThreadedConnectionPool(
        minconn, maxconn,
        dsn(),
        keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3,
    )


def _connect():
    global _pool
    if _pool is None:
        init()
    conn = getattr(_tlocal, "conn", None)
    if conn is None:
        conn = _pool.getconn()
        _tlocal.conn = conn
    return conn


def _release():
    """把当前线程持有的连接归还连接池。"""
    global _pool
    conn = getattr(_tlocal, "conn", None)
    if conn is not None and _pool is not None:
        try:
            _pool.putconn(conn)
        finally:
            _tlocal.conn = None


def _maybe_commit():
    if _txn_depth > 0:
        return
    conn = getattr(_tlocal, "conn", None)
    if conn is not None:
        try:
            conn.commit()
        finally:
            _release()


@contextmanager
def transaction():
    global _txn_depth
    with _lock:
        if _txn_depth > 0:
            _txn_depth += 1
            try:
                yield
            finally:
                _txn_depth -= 1
            return
        _txn_depth = 1
        c = _connect()
        try:
            c.rollback()  # 开启新事务
            yield
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            _txn_depth = 0
            if getattr(_tlocal, "conn", None) is c:
                _release()


def _schema_version():
    with _lock:
        cur = _connect().cursor()
        cur.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations")
        row = cur.fetchone()
        cur.close()
        return int(row[0]) if row else 0


def _ensure_schema_migrations():
    with _lock:
        conn = _conn if _conn is not None else _connect()
        cur = conn.cursor()
        cur.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT)"
        )
        conn.commit()
        cur.close()




def set_audit_max(n):
    return None


