# -*- coding: utf-8 -*-
"""PostgreSQL 适配层（迁移后使用，核心读写接口）。

当前项目默认仍走 SQLite；此模块提供 PG 连接与基础读写能力，
供迁移验证、运维脚本和后续完整适配使用。

环境变量：
  YUNO_PG_HOST / YUNO_PG_PORT / YUNO_PG_DB / YUNO_PG_USER / YUNO_PG_PASSWORD
"""

import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor, execute_values
except ImportError:
    psycopg2 = None
    RealDictCursor = None

DB_PATH = None
_conn = None
_lock = threading.RLock()
SCHEMA_VERSION = 1
_txn_depth = 0


def dsn() -> str:
    return (
        f"host={os.getenv('YUNO_PG_HOST', '127.0.0.1')} "
        f"port={os.getenv('YUNO_PG_PORT', '5432')} "
        f"dbname={os.getenv('YUNO_PG_DB', 'yuno')} "
        f"user={os.getenv('YUNO_PG_USER', 'esp')} "
        f"password={os.getenv('YUNO_PG_PASSWORD', 'yuno')}"
    )


def init(data_dir=None, force=False):
    global _conn
    if _conn is not None and not force:
        return
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
    _conn = psycopg2.connect(dsn())
    _conn.autocommit = False


def _connect():
    if _conn is None:
        init()
    return _conn


def _maybe_commit():
    if _txn_depth > 0:
        return
    _connect().commit()


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


def _schema_version():
    with _lock:
        cur = _connect().cursor()
        cur.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations")
        row = cur.fetchone()
        cur.close()
        return int(row[0]) if row else 0


def _ensure_schema_migrations():
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT)"
        )
        _connect().commit()
        cur.close()


# ===== kv =====
def kv_set(namespace, key, value):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO kv(namespace,key,value) VALUES(%s,%s,%s) "
            "ON CONFLICT(namespace,key) DO UPDATE SET value=EXCLUDED.value",
            (namespace, key, json.dumps(value, ensure_ascii=False)),
        )
        _maybe_commit()
        cur.close()


def kv_get(namespace, key, default=None):
    with _lock:
        cur = _connect().cursor()
        cur.execute("SELECT value FROM kv WHERE namespace=%s AND key=%s", (namespace, key))
        row = cur.fetchone()
        cur.close()
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except Exception:
            return default


# ===== memories =====
def _memory_cols():
    return (
        "scope,key,fact,embedding,updated_at,confidence,source,audience,speaker,mclass,"
        "arousal,valence,privacy,valid_from,valid_to,status"
    )


def memory_add(
    scope, key, fact, updated_at="", embedding=None, confidence=0.7,
    source="", audience="", speaker="", mclass="short", arousal=0.0,
    valence=0.0, privacy=0.0, valid_from="", valid_to="", status="active",
):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            f"INSERT INTO memories({_memory_cols()}) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(scope,key,fact) DO UPDATE SET "
            "embedding=COALESCE(EXCLUDED.embedding, memories.embedding), "
            "updated_at=EXCLUDED.updated_at, confidence=EXCLUDED.confidence, source=EXCLUDED.source, "
            "audience=EXCLUDED.audience, speaker=EXCLUDED.speaker, mclass=EXCLUDED.mclass, "
            "arousal=EXCLUDED.arousal, valence=EXCLUDED.valence, privacy=EXCLUDED.privacy, "
            "valid_from=EXCLUDED.valid_from, valid_to=EXCLUDED.valid_to, status=EXCLUDED.status",
            (
                scope, key, str(fact), json.dumps(embedding) if embedding is not None else None,
                updated_at, float(confidence), str(source), str(audience), str(speaker),
                str(mclass), float(arousal), float(valence), float(privacy),
                str(valid_from or updated_at), str(valid_to or ""), str(status) or "active",
            ),
        )
        _maybe_commit()
        cur.close()


def memory_get(scope, key=""):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "SELECT fact FROM memories WHERE scope=%s AND key=%s ORDER BY updated_at",
            (scope, key),
        )
        rows = [r[0] for r in cur.fetchall()]
        cur.close()
        return rows


def memory_clear(scope, key=None):
    with _lock:
        cur = _connect().cursor()
        if key is None:
            cur.execute("DELETE FROM memories WHERE scope=%s", (scope,))
        else:
            cur.execute("DELETE FROM memories WHERE scope=%s AND key=%s", (scope, key))
        _maybe_commit()
        cur.close()


def memory_rows(scope=None, key=None, exclude_status=None, limit=None):
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        sql = f"SELECT {_memory_cols()} FROM memories"
        conds, params = [], []
        if scope:
            conds.append("scope=%s"); params.append(scope)
        if key is not None:
            conds.append("key=%s"); params.append(key)
        if exclude_status:
            conds.append("status NOT IN (" + ",".join(["%s"] * len(exclude_status)) + ")")
            params.extend(exclude_status)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        if limit is not None:
            sql += " LIMIT %s"; params.append(int(limit))
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows


def memory_rows_by_facts(scope, facts, exclude_status=None):
    facts = list(dict.fromkeys(str(f) for f in (facts or [])))
    if not facts:
        return []
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        rows = []
        for i in range(0, len(facts), 500):
            chunk = facts[i:i + 500]
            sql = f"SELECT {_memory_cols()} FROM memories WHERE scope=%s"
            params = [scope]
            if exclude_status:
                sql += " AND status NOT IN (" + ",".join(["%s"] * len(exclude_status)) + ")"
                params.extend(exclude_status)
            sql += " AND fact IN (" + ",".join(["%s"] * len(chunk)) + ")"
            params.extend(chunk)
            cur.execute(sql, params)
            rows.extend(dict(r) for r in cur.fetchall())
        cur.close()
        return rows


def memory_set_status(scope, key, fact, status, valid_to=""):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "UPDATE memories SET status=%s, valid_to=%s WHERE scope=%s AND key=%s AND fact=%s",
            (str(status)[:20], str(valid_to or ""), scope, key or "", str(fact)),
        )
        _maybe_commit()
        cur.close()


def memory_set_confidence(scope, key, fact, confidence):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "UPDATE memories SET confidence=%s WHERE scope=%s AND key=%s AND fact=%s",
            (min(1.0, max(0.0, float(confidence))), scope, key or "", str(fact)),
        )
        _maybe_commit()
        cur.close()


def memory_delete(scope, key, fact):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "DELETE FROM memories WHERE scope=%s AND key=%s AND fact=%s",
            (scope, key or "", str(fact)),
        )
        _maybe_commit()
        cur.close()


def memory_source_normalize() -> dict:
    with _lock:
        cur = _connect().cursor()
        cur.execute("UPDATE memories SET source='user' WHERE source LIKE 'ingest:%' OR source='ingest'")
        n1 = cur.rowcount
        cur.execute("UPDATE memories SET source='pack' WHERE source='persona'")
        n2 = cur.rowcount
        _maybe_commit()
        cur.close()
        return {"ingest_to_user": n1, "persona_to_pack": n2}


# ===== meta =====
def meta_touch(scope, key, fact, importance=0.5, ts=""):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO memory_meta(scope,key,fact,access_count,last_access,importance) "
            "VALUES(%s,%s,%s,1,%s,%s) "
            "ON CONFLICT(scope,key,fact) DO UPDATE SET "
            "access_count=memory_meta.access_count+1, last_access=EXCLUDED.last_access, "
            "importance=GREATEST(memory_meta.importance, EXCLUDED.importance)",
            (scope, key or "", str(fact), ts or datetime.now().isoformat(timespec="seconds"), float(importance)),
        )
        _maybe_commit()
        cur.close()


def meta_touch_many(items):
    if not items:
        return None
    with _lock:
        cur = _connect().cursor()
        for scope, key, fact, importance, ts in items:
            cur.execute(
                "INSERT INTO memory_meta(scope,key,fact,access_count,last_access,importance) "
                "VALUES(%s,%s,%s,1,%s,%s) "
                "ON CONFLICT(scope,key,fact) DO UPDATE SET "
                "access_count=memory_meta.access_count+1, last_access=EXCLUDED.last_access, "
                "importance=GREATEST(memory_meta.importance, EXCLUDED.importance)",
                (scope, key or "", str(fact), ts or datetime.now().isoformat(timespec="seconds"), float(importance)),
            )
        _maybe_commit()
        cur.close()


def meta_rows(scope=None, key=None, min_importance=None, limit=None):
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        sql = "SELECT * FROM memory_meta"
        conds, params = [], []
        if scope:
            conds.append("scope=%s"); params.append(scope)
        if key is not None:
            conds.append("key=%s"); params.append(key)
        if min_importance is not None:
            conds.append("importance>=%s"); params.append(float(min_importance))
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        if limit is not None:
            sql += " LIMIT %s"; params.append(int(limit))
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows


# ===== events =====
def event_add(
    scope, key, etype, title, content="", importance=0.5, ts="", ts_source="approx",
    embedding=None, updated_at="", memory_scope="", memory_key="", memory_fact="",
):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO events(scope,key,etype,title,content,importance,ts,ts_source,embedding,updated_at,"
            "memory_scope,memory_key,memory_fact) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(scope,key,title) DO UPDATE SET "
            "etype=EXCLUDED.etype, content=EXCLUDED.content, importance=EXCLUDED.importance, "
            "ts=EXCLUDED.ts, ts_source=EXCLUDED.ts_source, embedding=EXCLUDED.embedding, "
            "updated_at=EXCLUDED.updated_at, memory_scope=EXCLUDED.memory_scope, "
            "memory_key=EXCLUDED.memory_key, memory_fact=EXCLUDED.memory_fact "
            "RETURNING id",
            (
                scope, key or "", str(etype)[:50], str(title)[:200], str(content)[:1000],
                float(importance), str(ts), str(ts_source)[:20] or "approx",
                json.dumps(embedding) if embedding is not None else None,
                updated_at or datetime.now().isoformat(timespec="seconds"),
                str(memory_scope), str(memory_key), str(memory_fact)[:200],
            ),
        )
        row = cur.fetchone()
        _maybe_commit()
        cur.close()
        return row[0] if row else None


def event_rows(scope=None, key=None, since=None, min_importance=None, limit=None):
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        sql = "SELECT * FROM events"
        conds, params = [], []
        if scope:
            conds.append("scope=%s"); params.append(scope)
        if key is not None:
            conds.append("key=%s"); params.append(key)
        if since:
            conds.append("ts>=%s"); params.append(since)
        if min_importance is not None:
            conds.append("importance>=%s"); params.append(min_importance)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY importance DESC, id DESC"
        if limit:
            sql += " LIMIT %s"; params.append(int(limit))
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows


# ===== audit =====
def audit_add(action, target="", detail="", operator=""):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO audit(ts,action,target,detail,operator) VALUES(%s,%s,%s,%s,%s)",
            (datetime.now().isoformat(timespec="seconds"), action, target, detail, operator),
        )
        _maybe_commit()
        cur.close()


def audit_query(limit=50, action=None):
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        sql = "SELECT ts,action,target,detail,operator FROM audit"
        params = []
        if action:
            sql += " WHERE action=%s"; params.append(action)
        sql += " ORDER BY id DESC LIMIT %s"; params.append(max(1, int(limit)))
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows


# ===== conv =====
def conv_add(conversation_id="", scope="", ts="", user_text="", ai_text=""):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO conv_log(conversation_id,scope,ts,user_text,ai_text) VALUES(%s,%s,%s,%s,%s)",
            (str(conversation_id)[:80], str(scope)[:80], str(ts)[:40], str(user_text)[:500], str(ai_text)[:800]),
        )
        _maybe_commit()
        cur.close()


def conv_rows(scope=None, since=None, limit=100):
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        sql = "SELECT * FROM conv_log"
        conds, params = [], []
        if scope:
            conds.append("scope=%s"); params.append(scope)
        if since:
            conds.append("ts>=%s"); params.append(since)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY id DESC LIMIT %s"; params.append(max(1, int(limit)))
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows


def conv_review_add(conv_id, score, scores=None, comment="", reviewer=""):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO conv_review(conv_id,score,scores,comment,reviewer,created_at) "
            "VALUES(%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(conv_id,reviewer) DO UPDATE SET "
            "score=EXCLUDED.score, scores=EXCLUDED.scores, comment=EXCLUDED.comment, created_at=EXCLUDED.created_at",
            (int(conv_id), max(1.0, min(5.0, float(score))), json.dumps(scores or {}, ensure_ascii=False),
             str(comment)[:300], str(reviewer)[:50], datetime.now().isoformat(timespec="seconds")),
        )
        _maybe_commit()
        cur.close()


# ===== notifications =====
def notif_add(target_type, target, content, scheduled_at=""):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO notifications(target_type,target,content,created_at,scheduled_at) VALUES(%s,%s,%s,%s,%s)",
            (target_type, target, str(content)[:500], datetime.now().isoformat(timespec="seconds"), str(scheduled_at or "")[:30]),
        )
        _maybe_commit()
        cur.close()


def notif_pending(limit=20):
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT id,target_type,target,content FROM notifications "
            "WHERE sent_at IS NULL AND failed=0 "
            "AND (scheduled_at IS NULL OR scheduled_at='' OR scheduled_at<=%s) "
            "ORDER BY id LIMIT %s",
            (datetime.now().isoformat(timespec="seconds"), limit),
        )
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows


# ===== health / counts =====
def health() -> dict:
    try:
        cur = _connect().cursor()
        cur.execute("SELECT version()")
        version = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
        table_count = cur.fetchone()[0]
        cur.close()
        return {"ok": True, "version": version, "table_count": table_count}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def table_counts() -> dict:
    conn = _connect()
    out = {}
    with conn.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
        tables = [r[0] for r in cur.fetchall()]
        for t in tables:
            try:
                cur.execute(f'SELECT COUNT(*) FROM "{t}"')
                out[t] = cur.fetchone()[0]
            except Exception:
                out[t] = None
    return out
