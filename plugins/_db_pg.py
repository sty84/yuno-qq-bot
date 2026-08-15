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
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta

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
    global _conn
    if _conn is not None and not force:
        return
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
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
    # pg_trgm 加速 ILIKE 中文子串检索（可用则建索引，不可用不阻塞）
    try:
        with _conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_memories_fact_trgm ON memories USING gin (fact gin_trgm_ops)")
            _conn.commit()
    except Exception:
        _conn.rollback()


def _connect():
    global _conn
    if _conn is None or _conn.closed:
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


# ===== 补充：memory / meta / event =====
def memory_replace(
    scope, key, facts, updated_at="", embeddings=None, confidences=None,
    sources=None, audience="", speaker="", mclass="short", arousal=0.0,
    valence=0.0, privacy=0.0, audiences=None, speakers=None, mclasses=None,
    arousals=None, valences=None, privacies=None, valid_from="", valid_to="",
    status="active",
):
    """整组替换某 scope/key 的记忆（兼容 SQLite 全参数）。"""
    with _lock:
        cur = _connect().cursor()
        cur.execute("DELETE FROM memories WHERE scope=%s AND key=%s", (scope, key or ""))
        emb = embeddings or {}
        conf = confidences or {}
        srcs = sources or {}
        aud_map = audiences or {}
        spk_map = speakers or {}
        cls_map = mclasses or {}
        ar_map = arousals or {}
        va_map = valences or {}
        pr_map = privacies or {}
        for fact in facts:
            f = str(fact)
            memory_add(
                scope, key or "", f, updated_at,
                emb.get(f) if isinstance(emb, dict) else None,
                float(conf.get(f, 0.7)) if isinstance(conf, dict) else 0.7,
                str(srcs.get(f, "")) if isinstance(srcs, dict) else "",
                str(aud_map.get(f, audience)) if isinstance(aud_map, dict) else audience,
                str(spk_map.get(f, speaker)) if isinstance(spk_map, dict) else speaker,
                str(cls_map.get(f, mclass)) if isinstance(cls_map, dict) else mclass,
                float(ar_map.get(f, arousal)) if isinstance(ar_map, dict) else arousal,
                float(va_map.get(f, valence)) if isinstance(va_map, dict) else valence,
                float(pr_map.get(f, privacy)) if isinstance(pr_map, dict) else privacy,
                str(valid_from or updated_at), str(valid_to or ""), str(status) or "active",
            )
        cur.close()

def memory_updated_at(scope, key=""):
    with _lock:
        cur = _connect().cursor()
        cur.execute("SELECT MAX(updated_at) FROM memories WHERE scope=%s AND key=%s", (scope, key))
        row = cur.fetchone()
        cur.close()
        return row[0] or ""


def memory_search(q, scope=None, key=None, limit=10):
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        sql = "SELECT scope,key,fact,embedding,updated_at,confidence FROM memories WHERE fact ILIKE %s"
        params = [f"%{q}%"]
        if scope:
            sql += " AND scope=%s"; params.append(scope)
        if key is not None:
            sql += " AND key=%s"; params.append(key)
        sql += " ORDER BY updated_at DESC LIMIT %s"; params.append(max(1, int(limit)))
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows


def memory_set_source(scope, key, fact, source):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "UPDATE memories SET source=%s WHERE scope=%s AND key=%s AND fact=%s",
            (str(source)[:20], scope, key or "", str(fact)),
        )
        _maybe_commit()
        cur.close()


def meta_delete(scope, key, fact):
    with _lock:
        cur = _connect().cursor()
        cur.execute("DELETE FROM memory_meta WHERE scope=%s AND key=%s AND fact=%s", (scope, key or "", str(fact)))
        _maybe_commit()
        cur.close()


def event_set_ts(event_id, ts, ts_source="explicit"):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "UPDATE events SET ts=%s, ts_source=%s, updated_at=%s WHERE id=%s",
            (str(ts), str(ts_source)[:20] or "approx", datetime.now().isoformat(timespec="seconds"), int(event_id)),
        )
        _maybe_commit()
        cur.close()


def event_set_ts_by_title(scope, key, title, ts, ts_source="explicit"):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "UPDATE events SET ts=%s, ts_source=%s, updated_at=%s WHERE scope=%s AND key=%s AND title=%s",
            (str(ts), str(ts_source)[:20] or "approx", datetime.now().isoformat(timespec="seconds"),
             str(scope), str(key or ""), str(title)),
        )
        _maybe_commit()
        cur.close()


def event_delete(event_id):
    with _lock:
        cur = _connect().cursor()
        cur.execute("DELETE FROM event_relations WHERE src=%s OR dst=%s", (int(event_id), int(event_id)))
        cur.execute("DELETE FROM events WHERE id=%s", (int(event_id),))
        _maybe_commit()
        cur.close()


def relation_add(src, dst, rel="influences", weight=1.0):
    if not src or not dst or src == dst:
        return
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO event_relations(src,dst,rel,weight,updated_at) VALUES(%s,%s,%s,%s,%s) "
            "ON CONFLICT(src,dst,rel) DO UPDATE SET weight=EXCLUDED.weight, updated_at=EXCLUDED.updated_at",
            (int(src), int(dst), str(rel)[:50], float(weight), datetime.now().isoformat(timespec="seconds")),
        )
        _maybe_commit()
        cur.close()


def relations_for(event_ids, direction="both"):
    if not event_ids:
        return []
    marks = ",".join(["%s"] * len(event_ids))
    params = list(event_ids)
    if direction == "out":
        sql = f"SELECT * FROM event_relations WHERE src IN ({marks})"
    elif direction == "in":
        sql = f"SELECT * FROM event_relations WHERE dst IN ({marks})"
    else:
        sql = f"SELECT * FROM event_relations WHERE src IN ({marks}) OR dst IN ({marks})"
        params = params + params
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows


def event_id_by_title(scope, key, title):
    with _lock:
        cur = _connect().cursor()
        cur.execute("SELECT id FROM events WHERE scope=%s AND key=%s AND title=%s", (scope, key or "", str(title)[:200]))
        row = cur.fetchone()
        cur.close()
        return row[0] if row else None


# ===== AI 自身记忆 =====
def ai_memory_set(kind, content, importance=0.5, embedding=None, updated_at=""):
    memory_add(
        "ai", kind, str(content), updated_at or datetime.now().isoformat(timespec="seconds"),
        embedding, float(importance), "pack", "public", "ai", "short",
    )


def ai_memory_rows(kind=None, limit=None):
    return memory_rows("ai", kind, limit=limit)


def ai_memory_clear(kind=None):
    memory_clear("ai", kind)


# ===== 议题 =====
def topic_add(scope, key, category, topic, importance=0.5, confidence=0.7, status="active",
              started_at="", updated_at=""):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO topics(scope,key,category,topic,importance,confidence,status,started_at,updated_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(scope,key,category,topic) DO UPDATE SET "
            "importance=EXCLUDED.importance, confidence=EXCLUDED.confidence, status=EXCLUDED.status, "
            "updated_at=EXCLUDED.updated_at RETURNING id",
            (scope, key or "", str(category), str(topic), float(importance), float(confidence),
             str(status), str(started_at or updated_at or datetime.now().isoformat(timespec="seconds")),
             str(updated_at or datetime.now().isoformat(timespec="seconds"))),
        )
        row = cur.fetchone()
        _maybe_commit()
        cur.close()
        return row[0] if row else None


def topic_find(scope, key, category, topic):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "SELECT id FROM topics WHERE scope=%s AND key=%s AND category=%s AND topic=%s",
            (scope, key or "", category, topic),
        )
        row = cur.fetchone()
        cur.close()
        return row[0] if row else None


def topic_get(topic_id):
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM topics WHERE id=%s", (int(topic_id),))
        row = cur.fetchone()
        cur.close()
        return dict(row) if row else None


def topic_rows(scope=None, key=None, category=None, limit=None):
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        sql = "SELECT * FROM topics"
        conds, params = [], []
        if scope:
            conds.append("scope=%s"); params.append(scope)
        if key is not None:
            conds.append("key=%s"); params.append(key)
        if category:
            conds.append("category=%s"); params.append(category)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        if limit is not None:
            sql += " LIMIT %s"; params.append(int(limit))
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows


def topics_count(scope=None) -> int:
    with _lock:
        cur = _connect().cursor()
        if scope:
            cur.execute("SELECT COUNT(*) FROM topics WHERE scope=%s", (scope,))
        else:
            cur.execute("SELECT COUNT(*) FROM topics")
        row = cur.fetchone()
        cur.close()
        return int(row[0]) if row else 0


def topic_clear(scope):
    with _lock:
        cur = _connect().cursor()
        cur.execute("DELETE FROM topics WHERE scope=%s", (scope,))
        _maybe_commit()
        cur.close()


def topic_param_add(topic_id, param, value, confidence=0.7, updated_at=""):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO topic_params(topic_id,param,value,confidence,updated_at) VALUES(%s,%s,%s,%s,%s) "
            "ON CONFLICT(topic_id,param,value) DO UPDATE SET confidence=EXCLUDED.confidence, updated_at=EXCLUDED.updated_at",
            (int(topic_id), str(param), str(value), float(confidence),
             updated_at or datetime.now().isoformat(timespec="seconds")),
        )
        _maybe_commit()
        cur.close()


def topic_params(topic_id) -> list:
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM topic_params WHERE topic_id=%s", (int(topic_id),))
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows


# ===== 属性 =====
def attr_set(scope, key, attr, value, confidence=0.7, updated_at=""):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO memory_attrs(scope,key,attr,value,confidence,updated_at) VALUES(%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(scope,key,attr,value) DO UPDATE SET confidence=EXCLUDED.confidence, updated_at=EXCLUDED.updated_at",
            (scope, key or "", str(attr), str(value), float(confidence),
             updated_at or datetime.now().isoformat(timespec="seconds")),
        )
        _maybe_commit()
        cur.close()


def attr_rows(scope=None, key=None, attr=None):
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        sql = "SELECT * FROM memory_attrs"
        conds, params = [], []
        if scope:
            conds.append("scope=%s"); params.append(scope)
        if key is not None:
            conds.append("key=%s"); params.append(key)
        if attr:
            conds.append("attr=%s"); params.append(attr)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows


def attr_delete(scope, key=None):
    with _lock:
        cur = _connect().cursor()
        if key is None:
            cur.execute("DELETE FROM memory_attrs WHERE scope=%s", (scope,))
        else:
            cur.execute("DELETE FROM memory_attrs WHERE scope=%s AND key=%s", (scope, key))
        _maybe_commit()
        cur.close()


# ===== history / feedback =====
def history_add(scope, key, fact, action, reason="", old_value="", new_value="", old_confidence=None, new_confidence=None):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO memory_history(scope,key,fact,action,old_value,new_value,old_confidence,new_confidence,reason,ts) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (scope, key or "", str(fact), str(action), str(old_value or ""), str(new_value or ""),
             old_confidence, new_confidence, str(reason or ""), datetime.now().isoformat(timespec="seconds")),
        )
        _maybe_commit()
        cur.close()


def history_rows(scope=None, key=None, limit=50):
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        sql = "SELECT * FROM memory_history"
        conds, params = [], []
        if scope:
            conds.append("scope=%s"); params.append(scope)
        if key is not None:
            conds.append("key=%s"); params.append(key)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY id DESC LIMIT %s"; params.append(max(1, int(limit)))
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows


def feedback_add(scope, key, kind, fact="", detail="", source="chat", weight=1.0):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO feedback_log(ts,scope,key,kind,fact,detail,source,weight) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (datetime.now().isoformat(timespec="seconds"), scope, key or "", str(kind), str(fact or ""),
             str(detail or ""), str(source), float(weight)),
        )
        _maybe_commit()
        cur.close()


def feedback_rows(scope=None, limit=50):
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        sql = "SELECT * FROM feedback_log"
        params = []
        if scope:
            sql += " WHERE scope=%s"; params.append(scope)
        sql += " ORDER BY id DESC LIMIT %s"; params.append(max(1, int(limit)))
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows


# ===== relationship =====
def relationship_get(scope):
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM relationships WHERE scope=%s", (scope,))
        row = cur.fetchone()
        cur.close()
        return dict(row) if row else None


def relationship_upsert(scope, subject="", object="ai", trust=0.3, familiarity=0.0,
                        closeness=0.0, stage="陌生", history=None, updated_at=""):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO relationships(scope,subject,object,trust,familiarity,closeness,stage,history,updated_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(scope) DO UPDATE SET "
            "subject=EXCLUDED.subject, object=EXCLUDED.object, trust=EXCLUDED.trust, "
            "familiarity=EXCLUDED.familiarity, closeness=EXCLUDED.closeness, stage=EXCLUDED.stage, "
            "history=EXCLUDED.history, updated_at=EXCLUDED.updated_at",
            (scope, str(subject), str(object), float(trust), float(familiarity), float(closeness),
             str(stage), str(history or "[]"), updated_at or datetime.now().isoformat(timespec="seconds")),
        )
        _maybe_commit()
        cur.close()


def relationship_rows():
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM relationships")
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows


# ===== policy log =====
def policy_log_add(trigger, behavior, priority=None, detail=""):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO policy_log(ts,trigger,behavior,priority,detail) VALUES(%s,%s,%s,%s,%s)",
            (datetime.now().isoformat(timespec="seconds"), str(trigger), str(behavior), priority, str(detail or "")),
        )
        _maybe_commit()
        cur.close()


def policy_log_rows(limit=50):
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM policy_log ORDER BY id DESC LIMIT %s", (max(1, int(limit)),))
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows


# ===== goals =====
def goal_add(scope, title, priority=3, deadline="", note="", motivation="", confidence=0.7, current_state=None):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO goals(scope,title,status,priority,deadline,note,motivation,confidence,current_state,created_at,updated_at) "
            "VALUES(%s,%s,'active',%s,%s,%s,%s,%s,%s,%s,%s)",
            (scope, str(title), int(priority), str(deadline), str(note), str(motivation), float(confidence),
             str(current_state or "{}"), datetime.now().isoformat(timespec="seconds"),
             datetime.now().isoformat(timespec="seconds")),
        )
        _maybe_commit()
        cur.close()


def goal_rows(scope=None, status=None, limit=50):
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        sql = "SELECT * FROM goals"
        conds, params = [], []
        if scope:
            conds.append("scope=%s"); params.append(scope)
        if status:
            conds.append("status=%s"); params.append(status)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY updated_at DESC LIMIT %s"; params.append(max(1, int(limit)))
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows


def goal_update(scope, title, progress=None, status=None, note=None, motivation=None, confidence=None, current_state=None):
    with _lock:
        cur = _connect().cursor()
        sets, params = [], []
        if progress is not None:
            sets.append("progress=%s"); params.append(float(progress))
        if status is not None:
            sets.append("status=%s"); params.append(str(status))
        if note is not None:
            sets.append("note=%s"); params.append(str(note))
        if motivation is not None:
            sets.append("motivation=%s"); params.append(str(motivation))
        if confidence is not None:
            sets.append("confidence=%s"); params.append(float(confidence))
        if current_state is not None:
            sets.append("current_state=%s"); params.append(str(current_state))
        if not sets:
            return
        sets.append("updated_at=%s"); params.append(datetime.now().isoformat(timespec="seconds"))
        params.extend([scope, str(title)])
        cur.execute(f"UPDATE goals SET {', '.join(sets)} WHERE scope=%s AND title=%s", params)
        _maybe_commit()
        cur.close()


# ===== consultations =====
def consult_get(scope):
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM consultations WHERE scope=%s", (scope,))
        row = cur.fetchone()
        cur.close()
        return dict(row) if row else None


def consult_save(scope, topic, status, stage, answers, created_at=""):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO consultations(scope,topic,status,stage,answers,created_at,updated_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(scope) DO UPDATE SET topic=EXCLUDED.topic, status=EXCLUDED.status, "
            "stage=EXCLUDED.stage, answers=EXCLUDED.answers, updated_at=EXCLUDED.updated_at",
            (scope, str(topic), str(status), int(stage), str(answers),
             created_at or datetime.now().isoformat(timespec="seconds"),
             datetime.now().isoformat(timespec="seconds")),
        )
        _maybe_commit()
        cur.close()


# ===== trace =====
def trace_add(
    conversation_id="", ts="", scope="", speaker="user", raw_content="", semantic_analysis="{}",
    intent="", entities="[]", events="[]", emotion="", slang_interpretation="[]",
    memory_candidate="", memory_action="", memory_id="", confidence=None, source="",
    reasoning="", affected_modules="[]", context_hint="",
):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO memory_trace("
            "conversation_id,ts,scope,speaker,raw_content,semantic_analysis,intent,entities,events,emotion,"
            "slang_interpretation,memory_candidate,memory_action,memory_id,confidence,source,reasoning,"
            "affected_modules,context_hint) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                str(conversation_id or ""), str(ts or datetime.now().isoformat(timespec="seconds")),
                str(scope or ""), str(speaker)[:20], str(raw_content or ""), str(semantic_analysis or "{}"),
                str(intent or ""), str(entities or "[]"), str(events or "[]"), str(emotion or ""),
                str(slang_interpretation or "[]"), str(memory_candidate or ""), str(memory_action or ""),
                str(memory_id or ""), confidence, str(source or ""), str(reasoning or ""),
                str(affected_modules or "[]"), str(context_hint or ""),
            ),
        )
        _maybe_commit()
        cur.close()


def trace_rows(scope=None, since=None, limit=100):
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        sql = "SELECT * FROM memory_trace"
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


def trace_prune(days=7) -> int:
    with _lock:
        cur = _connect().cursor()
        cur.execute("DELETE FROM memory_trace WHERE ts < %s", (datetime.now().isoformat(timespec="seconds"),))
        # 简化：按天数删除由调用方传 cutoff，这里保留接口
        _maybe_commit()
        n = cur.rowcount
        cur.close()
        return n


def trace_review_add(trace_id, score, scores=None, comment="", reviewer=""):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO trace_review(trace_id,score,scores,comment,reviewer,created_at) "
            "VALUES(%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(trace_id,reviewer) DO UPDATE SET score=EXCLUDED.score, scores=EXCLUDED.scores, "
            "comment=EXCLUDED.comment, created_at=EXCLUDED.created_at",
            (int(trace_id), max(1.0, min(5.0, float(score))), str(scores or "{}"), str(comment or ""),
             str(reviewer or ""), datetime.now().isoformat(timespec="seconds")),
        )
        _maybe_commit()
        cur.close()


def trace_review_map(trace_ids):
    if not trace_ids:
        return {}
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        marks = ",".join(["%s"] * len(trace_ids))
        cur.execute(f"SELECT * FROM trace_review WHERE trace_id IN ({marks})", list(trace_ids))
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return {r["trace_id"]: r for r in rows}


def trace_review_recent(limit=100):
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM trace_review ORDER BY created_at DESC LIMIT %s", (max(1, int(limit)),))
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows


# ===== conv =====
def conv_prune(days=30) -> int:
    with _lock:
        cur = _connect().cursor()
        cur.execute("DELETE FROM conv_log WHERE ts < %s", (datetime.now().isoformat(timespec="seconds"),))
        _maybe_commit()
        n = cur.rowcount
        cur.close()
        return n


def conv_review_map(conv_ids):
    if not conv_ids:
        return {}
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        marks = ",".join(["%s"] * len(conv_ids))
        cur.execute(f"SELECT * FROM conv_review WHERE conv_id IN ({marks})", list(conv_ids))
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return {r["conv_id"]: r for r in rows}


def conv_review_recent(limit=100):
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM conv_review ORDER BY created_at DESC LIMIT %s", (max(1, int(limit)),))
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows


# ===== query log =====
def query_log_add(query, scopes, top_k, hits):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO query_log(ts,query,scopes,top_k,hits) VALUES(%s,%s,%s,%s,%s)",
            (datetime.now().isoformat(timespec="seconds"), str(query), json.dumps(scopes or [], ensure_ascii=False),
             int(top_k or 0), json.dumps(hits or [], ensure_ascii=False)),
        )
        _maybe_commit()
        cur.close()


def query_log_pending(limit=200):
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM query_log WHERE exported=0 ORDER BY id LIMIT %s", (max(1, int(limit)),))
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows


def query_log_mark_exported(ids):
    if not ids:
        return
    with _lock:
        cur = _connect().cursor()
        marks = ",".join(["%s"] * len(ids))
        cur.execute(f"UPDATE query_log SET exported=1 WHERE id IN ({marks})", list(ids))
        _maybe_commit()
        cur.close()


def query_log_prune(days=30) -> int:
    with _lock:
        cur = _connect().cursor()
        cur.execute("DELETE FROM query_log WHERE ts < %s", (datetime.now().isoformat(timespec="seconds"),))
        _maybe_commit()
        n = cur.rowcount
        cur.close()
        return n


# ===== sessions =====
def session_find_recent(scope, key, within_min=1440):
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT * FROM sessions WHERE scope=%s AND key=%s AND closed=0 ORDER BY updated_at DESC LIMIT 1",
            (scope, key or ""),
        )
        row = cur.fetchone()
        cur.close()
        return dict(row) if row else None


def session_create(scope, key, topic="", summary=""):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO sessions(scope,key,topic,summary,started_at,updated_at,message_count) "
            "VALUES(%s,%s,%s,%s,%s,%s,0) RETURNING id",
            (scope, key or "", str(topic or ""), str(summary or ""),
             datetime.now().isoformat(timespec="seconds"), datetime.now().isoformat(timespec="seconds")),
        )
        row = cur.fetchone()
        _maybe_commit()
        cur.close()
        return row[0] if row else None


def session_bump(session_id, topic="", summary="", text=""):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "UPDATE sessions SET topic=COALESCE(NULLIF(%s,''), topic), summary=COALESCE(NULLIF(%s,''), summary), "
            "message_count=message_count+1, updated_at=%s WHERE id=%s",
            (str(topic or ""), str(summary or ""), datetime.now().isoformat(timespec="seconds"), int(session_id)),
        )
        _maybe_commit()
        cur.close()


def session_close_old(days=3):
    with _lock:
        cur = _connect().cursor()
        cur.execute("UPDATE sessions SET closed=1 WHERE updated_at < %s", (datetime.now().isoformat(timespec="seconds"),))
        _maybe_commit()
        n = cur.rowcount
        cur.close()
        return n


def session_rows(scope=None, key=None, closed=0, limit=20):
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        sql = "SELECT * FROM sessions WHERE closed=%s"
        params = [int(closed)]
        if scope:
            sql += " AND scope=%s"; params.append(scope)
        if key is not None:
            sql += " AND key=%s"; params.append(key)
        sql += " ORDER BY updated_at DESC LIMIT %s"; params.append(max(1, int(limit)))
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows


def sessions_count() -> int:
    with _lock:
        cur = _connect().cursor()
        cur.execute("SELECT COUNT(*) FROM sessions")
        row = cur.fetchone()
        cur.close()
        return int(row[0]) if row else 0


# ===== entities =====
def entity_find(scope, key, canonical):
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM entities WHERE scope=%s AND key=%s AND canonical=%s", (scope, key or "", canonical))
        row = cur.fetchone()
        cur.close()
        return dict(row) if row else None


def entity_add(scope, key, canonical):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO entities(scope,key,canonical) VALUES(%s,%s,%s) ON CONFLICT(scope,key,canonical) DO NOTHING RETURNING id",
            (scope, key or "", canonical),
        )
        row = cur.fetchone()
        _maybe_commit()
        cur.close()
        if row:
            return row[0]
        ent = entity_find(scope, key, canonical)
        return ent["id"] if ent else None


def entity_alias_add(entity_id, alias):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO entity_aliases(entity_id,alias) VALUES(%s,%s) ON CONFLICT DO NOTHING",
            (int(entity_id), str(alias)),
        )
        _maybe_commit()
        cur.close()


def entity_aliases(entity_id):
    with _lock:
        cur = _connect().cursor()
        cur.execute("SELECT alias FROM entity_aliases WHERE entity_id=%s", (int(entity_id),))
        rows = [r[0] for r in cur.fetchall()]
        cur.close()
        return rows


def entity_events_add(entity_id, event_id):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO entity_events(entity_id,event_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",
            (int(entity_id), int(event_id)),
        )
        _maybe_commit()
        cur.close()


def entity_rows(scope=None, key=None):
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        sql = "SELECT * FROM entities"
        conds, params = [], []
        if scope:
            conds.append("scope=%s"); params.append(scope)
        if key is not None:
            conds.append("key=%s"); params.append(key)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows


# ===== invalidation =====
def invalidation_add(scope, key, fact, reason=""):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO state_invalidations(scope,key,fact,reason,ts) VALUES(%s,%s,%s,%s,%s)",
            (scope, key or "", str(fact), str(reason or ""), datetime.now().isoformat(timespec="seconds")),
        )
        _maybe_commit()
        cur.close()


def invalidation_rows(limit=100):
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM state_invalidations ORDER BY id DESC LIMIT %s", (max(1, int(limit)),))
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows


def invalidation_clear_all():
    with _lock:
        cur = _connect().cursor()
        cur.execute("DELETE FROM state_invalidations")
        _maybe_commit()
        cur.close()


# ===== llm cost =====
def llm_cost_add(ts, module="chat", detail="", prompt_tokens=0, completion_tokens=0, chars=0):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO llm_cost(ts,module,detail,prompt_tokens,completion_tokens,chars) VALUES(%s,%s,%s,%s,%s,%s)",
            (str(ts), str(module), str(detail), int(prompt_tokens), int(completion_tokens), int(chars)),
        )
        _maybe_commit()
        cur.close()


def llm_cost_clear():
    with _lock:
        cur = _connect().cursor()
        cur.execute("DELETE FROM llm_cost")
        _maybe_commit()
        cur.close()


def llm_cost_summary(days=30) -> dict:
    cutoff = (datetime.now() - timedelta(days=max(1, int(days)))).isoformat(timespec="seconds")
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT ts,module,detail,prompt_tokens,completion_tokens FROM llm_cost WHERE ts >= %s ORDER BY ts",
            (cutoff,),
        )
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
    total = {"calls": len(rows), "prompt": 0, "completion": 0}
    by_day, by_module, by_path = {}, {}, {}
    for r in rows:
        p, c = int(r["prompt_tokens"]), int(r["completion_tokens"])
        total["prompt"] += p
        total["completion"] += c
        day = str(r["ts"])[:10]
        d = by_day.setdefault(day, {"calls": 0, "prompt": 0, "completion": 0})
        d["calls"] += 1; d["prompt"] += p; d["completion"] += c
        m = by_module.setdefault(str(r["module"] or "chat"), {"calls": 0, "prompt": 0, "completion": 0})
        m["calls"] += 1; m["prompt"] += p; m["completion"] += c
        if str(r["module"]) == "rerank":
            for path in str(r["detail"] or "").split(","):
                path = path.strip()
                if not path:
                    continue
                q = by_path.setdefault(path, {"calls": 0, "prompt": 0, "completion": 0})
                q["calls"] += 1; q["prompt"] += p; q["completion"] += c
    return {
        "days": int(days),
        "total": total,
        "by_day": [{"date": k, **v} for k, v in sorted(by_day.items())],
        "by_module": [{"module": k, **v} for k, v in sorted(by_module.items(), key=lambda x: -(x[1]["prompt"] + x[1]["completion"]))],
        "by_path": [{"path": k, **v} for k, v in sorted(by_path.items(), key=lambda x: -(x[1]["prompt"] + x[1]["completion"]))],
    }


# ===== procedures =====
def procedure_upsert(situation, action, success, updated_at=""):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO procedures(situation,action,success,tries,updated_at) VALUES(%s,%s,%s,1,%s) "
            "ON CONFLICT(situation,action) DO UPDATE SET success=EXCLUDED.success, tries=procedures.tries+1, updated_at=EXCLUDED.updated_at",
            (str(situation), str(action), float(success), updated_at or datetime.now().isoformat(timespec="seconds")),
        )
        _maybe_commit()
        cur.close()


def procedure_rows(min_tries=0, limit=200):
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM procedures WHERE tries>=%s ORDER BY tries DESC LIMIT %s", (int(min_tries), max(1, int(limit))))
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows


def procedure_clear():
    with _lock:
        cur = _connect().cursor()
        cur.execute("DELETE FROM procedures")
        _maybe_commit()
        cur.close()


# ===== scenario scores =====
def scenario_score_add(scenario_id, scope, scores, comment="", mode="manual"):
    try:
        vals = [float(v) for v in (scores or {}).values() if v is not None]
        avg = round(sum(vals) / len(vals), 2) if vals else 0.0
    except Exception:
        avg = 0.0
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO scenario_scores(ts,scenario_id,scope,mode,scores,comment,avg) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (datetime.now().isoformat(timespec="seconds"), str(scenario_id), str(scope), str(mode),
             str(scores or "{}"), str(comment or ""), avg),
        )
        _maybe_commit()
        cur.close()


def scenario_score_rows(limit=100):
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM scenario_scores ORDER BY id DESC LIMIT %s", (max(1, int(limit)),))
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows


# ===== space / item / mind / exp =====
def space_state_get():
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM space_state WHERE id=1")
        row = cur.fetchone()
        cur.close()
        if row is None:
            return None
        d = dict(row)
        try:
            d["path"] = json.loads(d.get("path") or "[]")
        except Exception:
            d["path"] = []
        return d

def space_state_set(st):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO space_state(id,room,state,from_room,to_room,path,depart_ts,arrive_ts,updated_ts) "
            "VALUES(1,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(id) DO UPDATE SET room=EXCLUDED.room, state=EXCLUDED.state, from_room=EXCLUDED.from_room, "
            "to_room=EXCLUDED.to_room, path=EXCLUDED.path, depart_ts=EXCLUDED.depart_ts, arrive_ts=EXCLUDED.arrive_ts, "
            "updated_ts=EXCLUDED.updated_ts",
            (str(st.get("room", "客厅")), str(st.get("state", "在场")), str(st.get("from_room", "")),
             str(st.get("to_room", "")), json.dumps(st.get("path", [])), str(st.get("depart_ts", "")),
             str(st.get("arrive_ts", "")), str(st.get("updated_ts", ""))),
        )
        _maybe_commit()
        cur.close()


def space_event_add(ts, kind, detail, memorable=False):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO space_events_state(ts,kind,detail,memorable) VALUES(%s,%s,%s,%s)",
            (str(ts), str(kind), str(detail), 1 if memorable else 0),
        )
        _maybe_commit()
        cur.close()


def space_event_rows(limit=200):
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM space_events_state ORDER BY id DESC LIMIT %s", (max(1, int(limit)),))
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows


def space_event_prune(days=7) -> int:
    with _lock:
        cur = _connect().cursor()
        cur.execute("DELETE FROM space_events_state WHERE ts < %s", (datetime.now().isoformat(timespec="seconds"),))
        _maybe_commit()
        n = cur.rowcount
        cur.close()
        return n


def ai_action_add(ts, scope, action, detail=""):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO ai_actions_state(ts,scope,action,detail) VALUES(%s,%s,%s,%s)",
            (str(ts), str(scope), str(action), str(detail or "")),
        )
        _maybe_commit()
        cur.close()


def ai_action_rows(scope="", limit=60):
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        sql = "SELECT * FROM ai_actions_state"
        params = []
        if scope:
            sql += " WHERE scope=%s"; params.append(scope)
        sql += " ORDER BY id DESC LIMIT %s"; params.append(max(1, int(limit)))
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows


def item_event_add(item, ts, event, from_place="", to_place="", cause="", seen_by=""):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO item_events(item,ts,event,from_place,to_place,cause,seen_by) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (str(item), str(ts), str(event), str(from_place), str(to_place), str(cause), str(seen_by)),
        )
        _maybe_commit()
        cur.close()


def item_event_rows(item=None, limit=500):
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        sql = "SELECT * FROM item_events"
        params = []
        if item:
            sql += " WHERE item=%s"; params.append(item)
        sql += " ORDER BY id DESC LIMIT %s"; params.append(max(1, int(limit)))
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows


def item_events_prune(days=90) -> int:
    with _lock:
        cur = _connect().cursor()
        cur.execute("DELETE FROM item_events WHERE ts < %s", (datetime.now().isoformat(timespec="seconds"),))
        _maybe_commit()
        n = cur.rowcount
        cur.close()
        return n


def item_activation_rows():
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT item, seen_ts, count FROM item_activation_state")
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return {r["item"]: {"seen_ts": r["seen_ts"], "count": r["count"]} for r in rows}


def item_activation_set(items):
    if not items:
        return
    with _lock:
        cur = _connect().cursor()
        for k, v in items.items():
            cur.execute(
                "INSERT INTO item_activation_state(item,seen_ts,count) VALUES(%s,%s,%s) "
                "ON CONFLICT(item) DO UPDATE SET seen_ts=EXCLUDED.seen_ts, count=EXCLUDED.count",
                (str(k)[:60], str(v.get("seen_ts", "")), int(v.get("count", 0))),
            )
        _maybe_commit()
        cur.close()

def item_search_rows():
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT scope, name, queue, step, started_at FROM item_search_state")
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        out = {}
        for r in rows:
            try:
                q = json.loads(r["queue"] or "[]")
            except Exception:
                q = []
            out[r["scope"]] = {
                "name": r["name"], "queue": q, "step": r["step"],
                "started_at": r["started_at"],
            }
        return out

def item_search_set(scope, data):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO item_search_state(scope,name,queue,step,started_at,updated_ts) "
            "VALUES(%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(scope) DO UPDATE SET name=EXCLUDED.name, queue=EXCLUDED.queue, "
            "step=EXCLUDED.step, started_at=EXCLUDED.started_at, updated_ts=EXCLUDED.updated_ts",
            (str(scope), str(data.get("name", "")), json.dumps(data.get("queue", [])),
             int(data.get("step", 0)), str(data.get("started_at", "")), str(data.get("updated_ts", ""))),
        )
        _maybe_commit()
        cur.close()


def item_search_delete(scope):
    with _lock:
        cur = _connect().cursor()
        cur.execute("DELETE FROM item_search_state WHERE scope=%s", (str(scope),))
        _maybe_commit()
        cur.close()


def mind_intention_rows():
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT scope,title,source,strength,state,due,condition,started_at,updated_at FROM mind_intention_state")
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return {r["scope"]: r for r in rows}

def mind_intention_set(scope, data):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO mind_intention_state(scope,title,source,strength,state,due,condition,started_at,updated_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(scope) DO UPDATE SET title=EXCLUDED.title, source=EXCLUDED.source, "
            "strength=EXCLUDED.strength, state=EXCLUDED.state, due=EXCLUDED.due, condition=EXCLUDED.condition, "
            "started_at=EXCLUDED.started_at, updated_at=EXCLUDED.updated_at",
            (str(scope), str(data.get("title", "")), str(data.get("source", "")), float(data.get("strength", 0.0)),
             str(data.get("state", "committed")), str(data.get("due", "")), str(data.get("condition", "")),
             str(data.get("started_at", "")), str(data.get("updated_at", ""))),
        )
        _maybe_commit()
        cur.close()


def mind_intention_delete(scope):
    with _lock:
        cur = _connect().cursor()
        cur.execute("DELETE FROM mind_intention_state WHERE scope=%s", (str(scope),))
        _maybe_commit()
        cur.close()


def exp_log_add(action, detail="", before=None, after=None, delta=None, regression=False):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO experiment_log(ts,action,detail,before,after,delta,regression) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (datetime.now().isoformat(timespec="seconds"), str(action), str(detail or ""),
             str(before or ""), str(after or ""), str(delta or ""), 1 if regression else 0),
        )
        _maybe_commit()
        cur.close()


def exp_log_rows(limit=50):
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM experiment_log ORDER BY id DESC LIMIT %s", (max(1, int(limit)),))
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows


# ===== notif =====
def notif_mark_sent(nid):
    with _lock:
        cur = _connect().cursor()
        cur.execute("UPDATE notifications SET sent_at=%s WHERE id=%s", (datetime.now().isoformat(timespec="seconds"), int(nid)))
        _maybe_commit()
        cur.close()


def notif_mark_failed(nid, max_retries=3):
    with _lock:
        cur = _connect().cursor()
        cur.execute("UPDATE notifications SET retries=retries+1 WHERE id=%s", (int(nid),))
        cur.execute("UPDATE notifications SET failed=1 WHERE id=%s AND retries>=%s", (int(nid), int(max_retries)))
        _maybe_commit()
        cur.close()


def notif_failed_retries(nid):
    with _lock:
        cur = _connect().cursor()
        cur.execute("SELECT retries FROM notifications WHERE id=%s", (int(nid),))
        row = cur.fetchone()
        cur.close()
        return row[0] if row else 0


# ===== dump / restore =====
DUMP_TABLES = [
    "memories", "memory_meta", "memory_attrs", "events", "event_relations",
    "entities", "entity_aliases", "entity_events", "topics", "topic_params",
    "sessions", "goals", "consultations", "relationships",
    "feedback_log", "memory_history", "belief_log", "policy_log",
    "memory_trace", "trace_review", "query_log", "kv", "audit",
    "notifications", "bindings", "scores", "nicknames", "state",
    "language_context", "user_expression_profile",
]


def dump_all() -> dict:
    out = {}
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        for t in DUMP_TABLES:
            try:
                cur.execute(f'SELECT * FROM "{t}"')
                out[t] = [dict(r) for r in cur.fetchall()]
            except Exception:
                out[t] = []
        cur.close()
    return out


def restore_all(data, replace=False) -> dict:
    counts = {}
    with _lock:
        cur = _connect().cursor()
        for t, rows in (data or {}).items():
            if t not in DUMP_TABLES or not isinstance(rows, list):
                continue
            try:
                if replace:
                    cur.execute(f'DELETE FROM "{t}"')
                if rows:
                    cols = list(rows[0].keys())
                    placeholders = ",".join(["%s"] * len(cols))
                    col_sql = ",".join(f'"{c}"' for c in cols)
                    from psycopg2.extras import execute_values
                    execute_values(
                        cur,
                        f'INSERT INTO "{t}" ({col_sql}) VALUES %s',
                        [tuple(r.get(c) for c in cols) for r in rows],
                        page_size=500,
                    )
                counts[t] = len(rows)
            except Exception as e:
                counts[t] = -1
        _maybe_commit()
        cur.close()
    return counts


# ===== facts =====
def facts_replace(scene, key, facts, updated_at=""):
    with _lock:
        cur = _connect().cursor()
        cur.execute("DELETE FROM facts WHERE scene=%s AND key=%s", (scene, key))
        for i, fact in enumerate(facts):
            cur.execute(
                "INSERT INTO facts(scene,key,idx,fact,updated_at) VALUES(%s,%s,%s,%s,%s)",
                (scene, key, i, str(fact), updated_at),
            )
        _maybe_commit()
        cur.close()


def facts_get(scene, key):
    with _lock:
        cur = _connect().cursor()
        cur.execute("SELECT fact FROM facts WHERE scene=%s AND key=%s ORDER BY idx", (scene, key))
        rows = [r[0] for r in cur.fetchall()]
        cur.close()
        return rows


def facts_updated_at(scene, key):
    with _lock:
        cur = _connect().cursor()
        cur.execute("SELECT MAX(updated_at) FROM facts WHERE scene=%s AND key=%s", (scene, key))
        row = cur.fetchone()
        cur.close()
        return row[0] or ""


# ===== kv raw/cas =====
def kv_get_raw(namespace, key, default=None):
    with _lock:
        cur = _connect().cursor()
        cur.execute("SELECT value FROM kv WHERE namespace=%s AND key=%s", (namespace, key))
        row = cur.fetchone()
        cur.close()
        return row[0] if row else default


def kv_cas(namespace, key, old_raw, new_raw):
    with _lock:
        cur = _connect().cursor()
        cur.execute("SELECT value FROM kv WHERE namespace=%s AND key=%s", (namespace, key))
        row = cur.fetchone()
        if row is None or row[0] != old_raw:
            cur.close()
            return False
        cur.execute(
            "UPDATE kv SET value=%s WHERE namespace=%s AND key=%s AND value=%s",
            (new_raw, namespace, key, old_raw),
        )
        _maybe_commit()
        cur.close()
        return True


# ===== item events =====
def item_event_add_many(rows):
    if not rows:
        return
    with _lock:
        cur = _connect().cursor()
        for r in rows:
            cur.execute(
                "INSERT INTO item_events(item,ts,event,from_place,to_place,cause,seen_by) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (str(r.get("item", ""))[:60], str(r.get("ts", "")), str(r.get("event", ""))[:20],
                 str(r.get("from_place", ""))[:80], str(r.get("to_place", ""))[:80],
                 str(r.get("cause", ""))[:80], str(r.get("seen_by", ""))[:20]),
            )
        _maybe_commit()
        cur.close()


def item_position_at(item, ts):
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT * FROM item_events WHERE item=%s AND ts<=%s "
            "AND event IN ('move','give','see','find','lost') "
            "ORDER BY ts DESC, id DESC LIMIT 1",
            (str(item)[:60], str(ts)),
        )
        row = cur.fetchone()
        cur.close()
        if not row:
            return None
        r = dict(row)
        to_place = str(r.get("to_place") or "")
        room, container = "", ""
        if "/" in to_place:
            room, container = to_place.split("/", 1)
        elif to_place:
            room = to_place
        return {
            "item": r["item"], "ts": r["ts"], "event": r["event"],
            "room": room, "container": container,
            "known": r["event"] != "lost",
            "cause": r.get("cause", ""),
        }


# ===== bindings / scores / nicknames / state =====
def binding_set(uid, gid, mid):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO bindings(user_openid,group_id,member_openid) VALUES(%s,%s,%s) "
            "ON CONFLICT(user_openid,group_id) DO UPDATE SET member_openid=EXCLUDED.member_openid",
            (uid, gid, mid),
        )
        _maybe_commit()
        cur.close()


def binding_delete_user_group(uid, gid):
    with _lock:
        cur = _connect().cursor()
        cur.execute("DELETE FROM bindings WHERE user_openid=%s AND group_id=%s", (uid, gid))
        _maybe_commit()
        cur.close()


def binding_delete_member(gid, mid):
    with _lock:
        cur = _connect().cursor()
        cur.execute("DELETE FROM bindings WHERE group_id=%s AND member_openid=%s", (gid, mid))
        _maybe_commit()
        cur.close()


def binding_find_user_for_member(gid, mid):
    with _lock:
        cur = _connect().cursor()
        cur.execute("SELECT user_openid FROM bindings WHERE group_id=%s AND member_openid=%s", (gid, mid))
        row = cur.fetchone()
        cur.close()
        return row[0] if row else None


def binding_groups_for_user(uid):
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT group_id, member_openid FROM bindings WHERE user_openid=%s", (uid,))
        rows = cur.fetchall()
        cur.close()
        return {r["group_id"]: r["member_openid"] for r in rows}


def bindings_all():
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM bindings")
        rows = cur.fetchall()
        cur.close()
        result = {}
        for r in rows:
            result.setdefault(r["user_openid"], {})[r["group_id"]] = r["member_openid"]
        return result


def score_add(player, game, delta=1):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO scores(player,game,score) VALUES(%s,%s,%s) "
            "ON CONFLICT(player,game) DO UPDATE SET score=scores.score+EXCLUDED.score",
            (player, game, int(delta)),
        )
        _maybe_commit()
        cur.close()


def scores_all():
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM scores")
        rows = cur.fetchall()
        cur.close()
        result = {}
        for r in rows:
            result.setdefault(r["player"], {})[r["game"]] = r["score"]
        return result


def nickname_set(player, nickname):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO nicknames(player,nickname) VALUES(%s,%s) "
            "ON CONFLICT(player) DO UPDATE SET nickname=EXCLUDED.nickname",
            (player, nickname),
        )
        _maybe_commit()
        cur.close()


def nickname_get(player):
    with _lock:
        cur = _connect().cursor()
        cur.execute("SELECT nickname FROM nicknames WHERE player=%s", (player,))
        row = cur.fetchone()
        cur.close()
        return row[0] if row else None


def state_get():
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT k,v FROM state")
        rows = cur.fetchall()
        cur.close()
        result = {}
        for r in rows:
            try:
                result[r["k"]] = json.loads(r["v"])
            except Exception:
                result[r["k"]] = r["v"]
        return result


def state_set(data):
    with _lock:
        cur = _connect().cursor()
        cur.execute("DELETE FROM state")
        for k, v in data.items():
            cur.execute("INSERT INTO state(k,v) VALUES(%s,%s)", (k, json.dumps(v, ensure_ascii=False)))
        _maybe_commit()
        cur.close()


# ===== belief log =====
def belief_log_add(kind, content, action, confidence=None, note="", old_content=""):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO belief_log(kind,content,action,confidence,note,old_content,ts) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (kind, content, action, confidence, note, old_content, datetime.now().isoformat(timespec="seconds")),
        )
        _maybe_commit()
        cur.close()


def belief_log_rows(limit=20):
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM belief_log ORDER BY id DESC LIMIT %s", (max(1, int(limit)),))
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows


def belief_log_get(log_id):
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM belief_log WHERE id=%s", (int(log_id),))
        row = cur.fetchone()
        cur.close()
        return dict(row) if row else None


# ===== expr / hesitation =====
def expr_log_add(raw_expression, normalized_meaning="", possible_intents=None, confidence=0.5, context=""):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO language_context(raw_expression,normalized_meaning,possible_intents,confidence,context,created_time) "
            "VALUES(%s,%s,%s,%s,%s,%s)",
            (str(raw_expression), str(normalized_meaning), str(possible_intents or "[]"),
             float(confidence), str(context), datetime.now().isoformat(timespec="seconds")),
        )
        _maybe_commit()
        cur.close()


def expr_log_rows(limit=50):
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM language_context ORDER BY id DESC LIMIT %s", (max(1, int(limit)),))
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows


def expr_profile_get(scope):
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM user_expression_profile WHERE scope=%s", (scope,))
        row = cur.fetchone()
        cur.close()
        return dict(row) if row else None


def expr_profile_upsert(scope, slang_frequency=0.0, irony_usage=0.0, emoji_usage=0.0,
                        serious_mode_switch=0, humor_style="unknown", communication_style="unknown",
                        formality_level=0.5, updated_at=""):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO user_expression_profile(scope,slang_frequency,irony_usage,emoji_usage,serious_mode_switch,"
            "humor_style,communication_style,formality_level,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(scope) DO UPDATE SET slang_frequency=EXCLUDED.slang_frequency, "
            "irony_usage=EXCLUDED.irony_usage, emoji_usage=EXCLUDED.emoji_usage, "
            "serious_mode_switch=EXCLUDED.serious_mode_switch, humor_style=EXCLUDED.humor_style, "
            "communication_style=EXCLUDED.communication_style, formality_level=EXCLUDED.formality_level, "
            "updated_at=EXCLUDED.updated_at",
            (scope, float(slang_frequency), float(irony_usage), float(emoji_usage), int(serious_mode_switch),
             str(humor_style), str(communication_style), float(formality_level),
             updated_at or datetime.now().isoformat(timespec="seconds")),
        )
        _maybe_commit()
        cur.close()


def hesitation_log_add(ts, scope, kind, action, reason, delay_s=0, monologue=""):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "INSERT INTO hesitation_log(ts,scope,kind,action,reason,delay_s,monologue) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (str(ts), str(scope), str(kind), str(action), str(reason), int(delay_s), str(monologue)),
        )
        _maybe_commit()
        cur.close()


def hesitation_log_rows(limit=50):
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM hesitation_log ORDER BY id DESC LIMIT %s", (max(1, int(limit)),))
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows


# ===== event topic / topic invalidate =====
def event_set_topic(event_id, topic_id):
    with _lock:
        cur = _connect().cursor()
        cur.execute("UPDATE events SET topic_id=%s WHERE id=%s", (int(topic_id), int(event_id)))
        _maybe_commit()
        cur.close()


def topic_param_invalidate(value):
    with _lock:
        cur = _connect().cursor()
        cur.execute("UPDATE topic_params SET confidence=LEAST(confidence, 0.3) WHERE value=%s", (str(value),))
        _maybe_commit()
        cur.close()


# ===== vec =====
def vec_dumps(vec):
    return json.dumps(vec, ensure_ascii=False) if vec else None


def vec_loads(raw):
    try:
        return json.loads(raw) if raw else None
    except Exception:
        return None


def vec_clear():
    with _lock:
        cur = _connect().cursor()
        cur.execute("DELETE FROM vec_index")
        cur.execute("DELETE FROM vec_centroids")
        _maybe_commit()
        cur.close()


def vec_centroids_set(centroids):
    with _lock:
        cur = _connect().cursor()
        cur.execute("DELETE FROM vec_centroids")
        for i, (emb, n) in enumerate(centroids):
            cur.execute(
                "INSERT INTO vec_centroids(id,dim,embedding,n) VALUES(%s,%s,%s,%s)",
                (i, len(emb), vec_dumps(emb), int(n)),
            )
        _maybe_commit()
        cur.close()


def vec_centroids_get() -> list:
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT id,dim,embedding,n FROM vec_centroids ORDER BY id")
        rows = cur.fetchall()
        cur.close()
        out = []
        for r in rows:
            emb = vec_loads(r["embedding"])
            if emb:
                out.append({"id": r["id"], "dim": r["dim"], "embedding": emb, "n": r["n"]})
        return out


def vec_index_replace(rows):
    with _lock:
        cur = _connect().cursor()
        cur.execute("DELETE FROM vec_index")
        for sc, k, f, cid, emb in rows:
            cur.execute(
                "INSERT INTO vec_index(scope,key,fact,centroid_id,embedding) VALUES(%s,%s,%s,%s,%s)",
                (sc, k, f, int(cid), vec_dumps(emb)),
            )
        _maybe_commit()
        cur.close()


def vec_index_upsert(scope, key, fact, centroid_id, embedding):
    with _lock:
        cur = _connect().cursor()
        cur.execute("DELETE FROM vec_index WHERE scope=%s AND key=%s AND fact=%s", (scope, key, fact))
        cur.execute(
            "INSERT INTO vec_index(scope,key,fact,centroid_id,embedding) VALUES(%s,%s,%s,%s,%s)",
            (scope, key, fact, int(centroid_id), vec_dumps(embedding)),
        )
        _maybe_commit()
        cur.close()


def vec_index_count() -> int:
    with _lock:
        cur = _connect().cursor()
        cur.execute("SELECT COUNT(*) FROM vec_index")
        row = cur.fetchone()
        cur.close()
        return int(row[0]) if row else 0


def vec_index_by_centroid(centroid_ids) -> list:
    if not centroid_ids:
        return []
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        marks = ",".join(["%s"] * len(centroid_ids))
        cur.execute(f"SELECT * FROM vec_index WHERE centroid_id IN ({marks})", list(centroid_ids))
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows


# ===== memory misc =====
def memory_replace_preserve(scope, key, facts, updated_at=""):
    """保留现有 confidence/source 等，只替换 fact 集合。"""
    existing = {r["fact"]: r for r in memory_rows(scope, key)}
    with _lock:
        cur = _connect().cursor()
        cur.execute("DELETE FROM memories WHERE scope=%s AND key=%s", (scope, key or ""))
        for fact in facts:
            old = existing.get(str(fact))
            if old:
                memory_add(
                    scope, key or "", str(fact), updated_at or old.get("updated_at", ""),
                    old.get("embedding"), float(old.get("confidence", 0.7)), old.get("source", ""),
                    old.get("audience", ""), old.get("speaker", ""), old.get("mclass", "short"),
                    float(old.get("arousal", 0.0)), float(old.get("valence", 0.0)),
                    float(old.get("privacy", 0.0)), old.get("valid_from", ""), old.get("valid_to", ""),
                    old.get("status", "active"),
                )
            else:
                memory_add(scope, key or "", str(fact), updated_at)
        _maybe_commit()
        cur.close()


def memory_update_embedding(scope, key, fact, embedding):
    with _lock:
        cur = _connect().cursor()
        cur.execute(
            "UPDATE memories SET embedding=%s WHERE scope=%s AND key=%s AND fact=%s",
            (vec_dumps(embedding), scope, key or "", str(fact)),
        )
        _maybe_commit()
        cur.close()


# ===== bm25 / lexicon (简化) =====
def bm25_clear():
    with _lock:
        cur = _connect().cursor()
        cur.execute("DELETE FROM bm25_terms")
        cur.execute("DELETE FROM bm25_docs")
        _maybe_commit()
        cur.close()


def bm25_stats():
    with _lock:
        cur = _connect().cursor()
        cur.execute("SELECT COUNT(*) AS n, COALESCE(AVG(doc_len),1) AS avgdl FROM bm25_docs")
        row = cur.fetchone()
        cur.close()
        return {"n": int(row[0]) if row else 0, "avgdl": float(row[1]) if row else 1.0}


def bm25_upsert(scope, key, tokenized):
    """tokenized: [(fact, [term,...])]，增量更新给定事实。"""
    key = key or ""
    with _lock:
        cur = _connect().cursor()
        for fact, terms in tokenized:
            cur.execute("DELETE FROM bm25_terms WHERE scope=%s AND key=%s AND fact=%s", (scope, key, str(fact)))
            cur.execute("DELETE FROM bm25_docs WHERE scope=%s AND key=%s AND fact=%s", (scope, key, str(fact)))
            cur.execute(
                "INSERT INTO bm25_docs(scope,key,fact,doc_len) VALUES(%s,%s,%s,%s) "
                "ON CONFLICT(scope,key,fact) DO UPDATE SET doc_len=EXCLUDED.doc_len",
                (scope, key, str(fact), len(terms)),
            )
            for term, tf in Counter(terms).items():
                cur.execute(
                    "INSERT INTO bm25_terms(term,scope,key,fact,tf) VALUES(%s,%s,%s,%s,%s) "
                    "ON CONFLICT(term,scope,key,fact) DO UPDATE SET tf=EXCLUDED.tf",
                    (term, scope, key, str(fact), tf),
                )
        _maybe_commit()
        cur.close()


def bm25_sync(scope, key, tokenized):
    """tokenized: [(fact, [term,...])]，替换该 scope+key 的倒排与文档长度。"""
    key = key or ""
    with _lock:
        cur = _connect().cursor()
        cur.execute("DELETE FROM bm25_terms WHERE scope=%s AND key=%s", (scope, key))
        cur.execute("DELETE FROM bm25_docs WHERE scope=%s AND key=%s", (scope, key))
        for fact, terms in tokenized:
            cur.execute(
                "INSERT INTO bm25_docs(scope,key,fact,doc_len) VALUES(%s,%s,%s,%s) "
                "ON CONFLICT(scope,key,fact) DO UPDATE SET doc_len=EXCLUDED.doc_len",
                (scope, key, str(fact), len(terms)),
            )
            for term, tf in Counter(terms).items():
                cur.execute(
                    "INSERT INTO bm25_terms(term,scope,key,fact,tf) VALUES(%s,%s,%s,%s,%s) "
                    "ON CONFLICT(term,scope,key,fact) DO UPDATE SET tf=EXCLUDED.tf",
                    (term, scope, key, str(fact), tf),
                )
        _maybe_commit()
        cur.close()


def bm25_postings(terms, scopes):
    if not terms:
        return []
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        marks = ",".join(["%s"] * len(terms))
        sql = f"SELECT term,scope,key,fact,tf FROM bm25_terms WHERE term IN ({marks})"
        params = list(terms)
        if scopes:
            s_marks = ",".join(["%s"] * len(scopes))
            sql += f" AND scope IN ({s_marks})"
            params += list(scopes)
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows


def bm25_doc_lens(keys):
    if not keys:
        return {}
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        out = {}
        for sc, k, f in keys:
            cur.execute(
                "SELECT scope,key,fact,doc_len FROM bm25_docs WHERE scope=%s AND key=%s AND fact=%s",
                (sc, k or "", str(f)),
            )
            row = cur.fetchone()
            if row:
                out[(row["scope"], row["key"], row["fact"])] = row["doc_len"]
        cur.close()
        return out


def lexicon_sync(scope, key):
    # 词法索引在 PG 里可后续用 pg_trgm；当前保留空实现避免切换报错
    return None


def lexicon_rebuild() -> int:
    return 0


def lexicon_search(query, scopes, limit=10):
    """PG 简易词法检索：用 ILIKE 子串匹配（后续可换 pg_trgm）。"""
    q = str(query or "").strip()
    if not q:
        return []
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        sql = "SELECT scope,key,fact,0 AS rank FROM memories WHERE fact ILIKE %s"
        params = [f"%{q}%"]
        if scopes:
            marks = ",".join(["%s"] * len(scopes))
            sql += f" AND scope IN ({marks})"
            params += list(scopes)
        sql += " LIMIT %s"; params.append(max(1, int(limit)))
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows


def fts_available() -> bool:
    return True


def set_audit_max(n):
    return None


def query_log_followup(query):
    return None


def purge_scope(scope, subsystems=False, confirm=None):
    if confirm != scope:
        return
    with _lock:
        cur = _connect().cursor()
        for t in ("memories", "memory_meta", "memory_attrs", "events", "event_relations",
                  "topics", "topic_params", "sessions", "goals", "consultations",
                  "relationships", "feedback_log", "memory_history", "query_log",
                  "memory_trace", "state_invalidations", "notifications", "conv_log"):
            try:
                cur.execute(f'DELETE FROM "{t}" WHERE scope=%s', (scope,))
            except Exception:
                pass
        _maybe_commit()
        cur.close()


def backup_to(path):
    """使用 pg_dump 备份 PostgreSQL 到自定义格式文件。"""
    import subprocess
    env = os.environ.copy()
    password = os.getenv("YUNO_PG_PASSWORD")
    if not password:
        raise RuntimeError("YUNO_PG_PASSWORD 未设置")
    env["PGPASSWORD"] = password
    subprocess.run(
        [
            "pg_dump",
            "-h", os.getenv("YUNO_PG_HOST", "127.0.0.1"),
            "-p", os.getenv("YUNO_PG_PORT", "5432"),
            "-U", os.getenv("YUNO_PG_USER", "esp"),
            "-d", os.getenv("YUNO_PG_DB", "yuno"),
            "-Fc",
            "-f", str(path),
        ],
        check=True,
        env=env,
    )
    return str(path)
