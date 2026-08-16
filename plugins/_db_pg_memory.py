# -*- coding: utf-8 -*-
"""PostgreSQL 数据操作：记忆/事件/议题等核心数据。"""
import json
from datetime import datetime

from plugins._db_pg_core import _connect, _maybe_commit, _lock, RealDictCursor

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


# ===== pgvector 可选支持 =====
def pgvector_available() -> bool:
    """检测 PostgreSQL 是否安装并启用了 pgvector 扩展。"""
    try:
        with _connect().cursor() as cur:
            cur.execute("SELECT 1 FROM pg_extension WHERE extname='vector'")
            return cur.fetchone() is not None
    except Exception:
        return False


def pgvector_build(rows):
    """把 (scope,key,fact,embedding) 写入 vec_pg 表（pgvector 原生向量列）。"""
    if not pgvector_available():
        return False
    with _lock:
        cur = _connect().cursor()
        cur.execute("DELETE FROM vec_pg")
        for sc, k, f, emb in rows:
            cur.execute(
                "INSERT INTO vec_pg(scope,key,fact,embedding) VALUES(%s,%s,%s,%s) "
                "ON CONFLICT(scope,key,fact) DO UPDATE SET embedding=EXCLUDED.embedding",
                (sc, k or "", str(f), emb),
            )
        _maybe_commit()
        cur.close()
    return True


def pgvector_search(query_vec, scopes, top_k=5):
    """使用 pgvector 余弦距离检索；返回 [{scope,key,fact,score}]。"""
    if not pgvector_available():
        return []
    with _lock:
        cur = _connect().cursor(cursor_factory=RealDictCursor)
        sql = "SELECT scope,key,fact, 1 - (embedding <=> %s::vector) AS score FROM vec_pg"
        params = [query_vec]
        if scopes:
            marks = ",".join(["%s"] * len(scopes))
            sql += f" WHERE scope IN ({marks})"
            params += list(scopes)
        sql += " ORDER BY embedding <=> %s::vector LIMIT %s"
        params += [query_vec, max(1, int(top_k))]
        cur.execute(sql, params)
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


