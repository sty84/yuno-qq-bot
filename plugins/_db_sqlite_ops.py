# -*- coding: utf-8 -*-
"""SQLite 数据操作：索引/日志/会话/导出等。"""
import json
import sqlite3
from collections import Counter
from datetime import datetime

from plugins import _db_sqlite_core as _core
from plugins._db_sqlite_core import _connect, _maybe_commit, _lock, _stats_err
from plugins._db_sqlite_memory import vec_dumps, vec_loads

# ===== 自研 IVF 向量索引（SQLite 持久化）=====
def vec_clear():
    with _lock:
        c = _connect()
        c.execute("DELETE FROM vec_index")
        c.execute("DELETE FROM vec_centroids")
        _maybe_commit()


def vec_centroids_set(centroids):
    """centroids: [(embedding, n)]，整体替换。"""
    with _lock:
        c = _connect()
        c.execute("DELETE FROM vec_centroids")
        for i, (emb, n) in enumerate(centroids):
            c.execute(
                "INSERT INTO vec_centroids(id,dim,embedding,n) VALUES(?,?,?,?)",
                (i, len(emb), vec_dumps(emb), int(n)),
            )
        _maybe_commit()


def vec_centroids_get() -> list:
    with _lock:
        rows = _connect().execute(
            "SELECT id,dim,embedding,n FROM vec_centroids ORDER BY id"
        ).fetchall()
        out = []
        for r in rows:
            emb = vec_loads(r["embedding"])
            if emb:
                out.append({"id": r["id"], "dim": r["dim"], "embedding": emb, "n": r["n"]})
        return out


def vec_index_replace(rows):
    """rows: [(scope,key,fact,centroid_id,embedding)]，整体替换。"""
    with _lock:
        c = _connect()
        c.execute("DELETE FROM vec_index")
        c.executemany(
            "INSERT INTO vec_index(scope,key,fact,centroid_id,embedding) VALUES(?,?,?,?,?)",
            [(sc, k, f, int(cid), vec_dumps(emb)) for sc, k, f, cid, emb in rows],
        )
        _maybe_commit()


def vec_index_upsert(scope, key, fact, centroid_id, embedding):
    """增量写一条向量索引（同 fact 覆盖；最近质心归属由调用方算好）。"""
    with _lock:
        c = _connect()
        c.execute("DELETE FROM vec_index WHERE scope=? AND key=? AND fact=?", (scope, key, fact))
        c.execute(
            "INSERT INTO vec_index(scope,key,fact,centroid_id,embedding) VALUES(?,?,?,?,?)",
            (scope, key, fact, int(centroid_id), vec_dumps(embedding)),
        )
        _maybe_commit()


def vec_index_count() -> int:
    with _lock:
        return _connect().execute("SELECT COUNT(*) FROM vec_index").fetchone()[0]


def vec_index_by_centroid(centroid_ids) -> list:
    if not centroid_ids:
        return []
    marks = ",".join("?" * len(centroid_ids))
    with _lock:
        rows = _connect().execute(
            f"SELECT scope,key,fact,embedding FROM vec_index WHERE centroid_id IN ({marks})",
            list(centroid_ids),
        ).fetchall()
        return [dict(r) for r in rows]


# ===== 真分词 BM25 倒排索引 =====
def bm25_sync(scope, key, tokenized):
    """tokenized: [(fact, [term,...])]，替换该 scope+key 的倒排与文档长度。"""
    key = key or ""
    with _lock:
        c = _connect()
        c.execute("DELETE FROM bm25_terms WHERE scope=? AND key=?", (scope, key))
        c.execute("DELETE FROM bm25_docs WHERE scope=? AND key=?", (scope, key))
        for fact, terms in tokenized:
            c.execute(
                "INSERT OR REPLACE INTO bm25_docs(scope,key,fact,doc_len) VALUES(?,?,?,?)",
                (scope, key, str(fact), len(terms)),
            )
            for term, tf in Counter(terms).items():
                c.execute(
                    "INSERT OR IGNORE INTO bm25_terms(term,scope,key,fact,tf) VALUES(?,?,?,?,?)",
                    (term, scope, key, str(fact), tf),
                )
        _maybe_commit()


def bm25_upsert(scope, key, tokenized):
    """增量：只更新给定事实的词项与文档长度（不重建整个 scope）。"""
    key = key or ""
    with _lock:
        c = _connect()
        for fact, terms in tokenized:
            c.execute(
                "DELETE FROM bm25_terms WHERE scope=? AND key=? AND fact=?",
                (scope, key, str(fact)),
            )
            c.execute(
                "DELETE FROM bm25_docs WHERE scope=? AND key=? AND fact=?",
                (scope, key, str(fact)),
            )
            c.execute(
                "INSERT OR REPLACE INTO bm25_docs(scope,key,fact,doc_len) VALUES(?,?,?,?)",
                (scope, key, str(fact), len(terms)),
            )
            for term, tf in Counter(terms).items():
                c.execute(
                    "INSERT OR IGNORE INTO bm25_terms(term,scope,key,fact,tf) VALUES(?,?,?,?,?)",
                    (term, scope, key, str(fact), tf),
                )
        _maybe_commit()


def bm25_clear():
    with _lock:
        c = _connect()
        c.execute("DELETE FROM bm25_terms")
        c.execute("DELETE FROM bm25_docs")
        _maybe_commit()


def bm25_stats():
    with _lock:
        row = _connect().execute(
            "SELECT COUNT(*) AS n, COALESCE(AVG(doc_len),1) AS avgdl FROM bm25_docs"
        ).fetchone()
        return {"n": int(row["n"]), "avgdl": float(row["avgdl"])}


def bm25_postings(terms, scopes):
    if not terms:
        return []
    t_marks = ",".join("?" * len(terms))
    sql = f"SELECT term,scope,key,fact,tf FROM bm25_terms WHERE term IN ({t_marks})"
    params = list(terms)
    if scopes:
        s_marks = ",".join("?" * len(scopes))
        sql += f" AND scope IN ({s_marks})"
        params += list(scopes)
    with _lock:
        return [dict(r) for r in _connect().execute(sql, params).fetchall()]


def bm25_doc_lens(keys):
    """keys: [(scope,key,fact)] → {(scope,key,fact): doc_len}（批量一次查询，避免 N+1）。"""
    if not keys:
        return {}
    out = {}
    with _lock:
        c = _connect()
        for start in range(0, len(keys), 200):
            chunk = keys[start:start + 200]
            marks = ",".join("(?,?,?)" for _ in chunk)
            params = []
            for sc, k, f in chunk:
                params.extend([sc, k or "", str(f)])
            for r in c.execute(
                f"SELECT scope,key,fact,doc_len FROM bm25_docs WHERE (scope,key,fact) IN ({marks})",
                params,
            ).fetchall():
                out[(r["scope"], r["key"], r["fact"])] = r["doc_len"]
    return out


# ===== belief 版本日志（成长反思）=====
def belief_log_add(kind, content, action, confidence=None, note="", old_content=""):
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO belief_log(kind,content,old_content,action,confidence,note,ts) VALUES(?,?,?,?,?,?,?)",
            (
                str(kind)[:30],
                str(content)[:500],
                str(old_content)[:500],
                str(action)[:30],
                float(confidence) if confidence is not None else None,
                str(note)[:300],
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        _maybe_commit()


def belief_log_rows(limit=20):
    with _lock:
        rows = _connect().execute(
            "SELECT id,kind,content,old_content,action,confidence,note,ts FROM belief_log ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        return [dict(r) for r in rows]


def belief_log_get(log_id):
    with _lock:
        row = _connect().execute(
            "SELECT id,kind,content,old_content,action,confidence,note,ts FROM belief_log WHERE id=?",
            (int(log_id),),
        ).fetchone()
        return dict(row) if row else None


# ===== 查询日志（遥测/评测集）=====
def query_log_add(query, scopes, top_k, hits):
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO query_log(ts,query,scopes,top_k,hits) VALUES(?,?,?,?,?)",
            (
                datetime.now().isoformat(timespec="seconds"),
                str(query)[:200],
                json.dumps(list(scopes or []), ensure_ascii=False)[:500],
                int(top_k or 5),
                json.dumps(list(hits or []), ensure_ascii=False)[:3000],
            ),
        )
        _maybe_commit()


def query_log_pending(limit=200):
    with _lock:
        rows = _connect().execute(
            "SELECT id,ts,query,scopes,top_k,hits,followup FROM query_log "
            "WHERE exported=0 ORDER BY id LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        return [dict(r) for r in rows]


def query_log_mark_exported(ids):
    if not ids:
        return
    marks = ",".join("?" * len(ids))
    with _lock:
        c = _connect()
        c.execute(f"UPDATE query_log SET exported=1 WHERE id IN ({marks})", list(ids))
        _maybe_commit()


def query_log_prune(days=30) -> int:
    """清理超过保留期的查询日志（默认 30 天），防表无限膨胀。"""
    import datetime as _dt
    cutoff = (_dt.datetime.now() - _dt.timedelta(days=max(1, int(days)))).isoformat(timespec="seconds")
    with _lock:
        c = _connect()
        cur = c.execute("DELETE FROM query_log WHERE ts<?", (cutoff,))
        _maybe_commit()
        return cur.rowcount


def query_log_followup(query):
    """用户短时间内追问同一问题（弱反馈：说明第一次没答好/还想深入）。"""
    with _lock:
        c = _connect()
        c.execute(
            "UPDATE query_log SET followup=followup+1 WHERE query=? "
            "AND id=(SELECT id FROM query_log WHERE query=? ORDER BY id DESC LIMIT 1)",
            (str(query)[:200], str(query)[:200]),
        )
        _maybe_commit()


# ===== 会话（session）=====
def session_find_recent(scope, key, within_min=1440):
    with _lock:
        rows = _connect().execute(
            "SELECT * FROM sessions WHERE scope=? AND key=? AND closed=0 "
            "ORDER BY updated_at DESC, id DESC LIMIT 1",
            (scope, key or ""),
        ).fetchall()
        if not rows:
            return None
        s = dict(rows[0])
        try:
            from datetime import datetime as _dt
            last = _dt.fromisoformat(s.get("updated_at") or "")
            minutes = (_dt.now() - last).total_seconds() / 60
        except Exception as e:
            _stats_err(e)
            minutes = 0
        return s if minutes <= within_min else None


def session_create(scope, key, topic="", summary=""):
    with _lock:
        c = _connect()
        now = datetime.now().isoformat(timespec="seconds")
        c.execute(
            "INSERT INTO sessions(scope,key,topic,started_at,updated_at,message_count,summary) "
            "VALUES(?,?,?,?,?,1,?)",
            (scope, key or "", str(topic)[:100], now, now, str(summary)[:500]),
        )
        _maybe_commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]


def session_bump(session_id, topic="", summary="", text=""):
    with _lock:
        c = _connect()
        now = datetime.now().isoformat(timespec="seconds")
        topic = topic or ""
        summary = summary or ""
        c.execute(
            "UPDATE sessions SET updated_at=?, message_count=message_count+1, "
            "topic=COALESCE(NULLIF(?,''),topic), summary=COALESCE(NULLIF(?,''),summary) WHERE id=?",
            (now, str(topic)[:100], str(summary)[:500], int(session_id)),
        )
        _maybe_commit()


def session_close_old(days=3):
    with _lock:
        c = _connect()
        before = c.total_changes
        c.execute(
            "UPDATE sessions SET closed=1 WHERE closed=0 AND "
            "updated_at < datetime('now', ?)",
            (f"-{int(days)} days",),
        )
        _maybe_commit()
        return c.total_changes - before


def session_rows(scope=None, key=None, closed=0, limit=20):
    with _lock:
        sql = "SELECT * FROM sessions"
        params, conds = [], []
        if scope:
            conds.append("scope=?")
            params.append(scope)
        if key is not None:
            conds.append("key=?")
            params.append(key)
        conds.append("closed=?")
        params.append(int(closed))
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        return [dict(r) for r in _connect().execute(sql, params).fetchall()]


def sessions_count() -> int:
    with _lock:
        return _connect().execute("SELECT COUNT(*) FROM sessions").fetchone()[0]


# ===== 实体归一 =====
def entity_find(scope, key, canonical):
    with _lock:
        row = _connect().execute(
            "SELECT id FROM entities WHERE scope=? AND key=? AND canonical=?",
            (scope, key or "", str(canonical)[:100]),
        ).fetchone()
        return row[0] if row else None


def entity_add(scope, key, canonical):
    with _lock:
        c = _connect()
        c.execute(
            "INSERT OR IGNORE INTO entities(scope,key,canonical) VALUES(?,?,?)",
            (scope, key or "", str(canonical)[:100]),
        )
        _maybe_commit()
        row = c.execute(
            "SELECT id FROM entities WHERE scope=? AND key=? AND canonical=?",
            (scope, key or "", str(canonical)[:100]),
        ).fetchone()
        return row[0] if row else None


def entity_alias_add(entity_id, alias):
    with _lock:
        c = _connect()
        c.execute(
            "INSERT OR IGNORE INTO entity_aliases(entity_id,alias) VALUES(?,?)",
            (int(entity_id), str(alias)[:100]),
        )
        _maybe_commit()


def entity_aliases(entity_id):
    with _lock:
        rows = _connect().execute(
            "SELECT alias FROM entity_aliases WHERE entity_id=?", (int(entity_id),)
        ).fetchall()
        return [r[0] for r in rows]


def entity_events_add(entity_id, event_id):
    with _lock:
        c = _connect()
        c.execute(
            "INSERT OR IGNORE INTO entity_events(entity_id,event_id) VALUES(?,?)",
            (int(entity_id), int(event_id)),
        )
        _maybe_commit()


def entity_rows(scope=None, key=None):
    with _lock:
        sql = "SELECT id,scope,key,canonical FROM entities"
        params, conds = [], []
        if scope:
            conds.append("scope=?")
            params.append(scope)
        if key is not None:
            conds.append("key=?")
            params.append(key)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        return [dict(r) for r in _connect().execute(sql, params).fetchall()]


# 纯记忆/画像类：purge_scope 默认清（「彻底删除」必须覆盖的个人数据，含审计与轨迹）
_MEM_SCOPE_TABLES = (
    "memories", "memory_meta", "memory_attrs", "memory_history",
    "memory_trace", "feedback_log", "state_invalidations", "user_expression_profile",
    "bm25_terms", "bm25_docs", "memories_fts", "vec_index", "entities", "sessions",
)
# 子系统状态类：默认不清，subsystems=True 才一并清（关系/目标/咨询/意图/场景评分等）
_SUBSYS_SCOPE_TABLES = (
    "relationships", "goals", "consultations", "mind_intention_state",
    "scenario_scores", "hesitation_log", "item_search_state", "ai_actions_state",
)


def _archive_scope(scope, tables):
    """删除前把待删的 scope 行归档到 .trash/，误删可手工恢复。返回备份文件路径或 None。

    只归档带 scope 列的内容表；event_relations/entity_events/topic_params 这类
    无 scope 列的联结/索引表不归档（属派生物，重放 ingest 可重建）。
    """
    c = _connect()
    dump = {}
    for t in tables:
        try:
            rows = c.execute(f"SELECT * FROM {t} WHERE scope=?", (scope,)).fetchall()
            if rows:
                dump[t] = [dict(r) for r in rows]
        except Exception:
            continue
    if not dump:
        return None
    try:
        trash = _core.DB_PATH.parent / ".trash"
        trash.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        safe = str(scope).replace(":", "_").replace("/", "_").replace("\\", "_")
        p = trash / f"purge_{safe}_{ts}.json"
        p.write_text(json.dumps(dump, ensure_ascii=False, default=str), encoding="utf-8")
        return p
    except Exception:
        return None


def purge_scope(scope, subsystems=False, confirm=None):
    """按用户/场景彻底删除：记忆/属性/事件/议题/索引/日志。

    安全闸：必须显式传 confirm == scope 才执行，防误触（漏传/传错 → 直接返回 0，不删）。
    删除前先把待删行归档到 .trash/ 下的 JSON，误删可手工恢复。
    subsystems=True 时额外清关系/目标/咨询/意图等子系统状态表。
    返回删除的总行数（confirm 未通过时为 0）。
    """
    if confirm != scope:
        return 0
    with _lock:
        c = _connect()
        event_ids = [r[0] for r in c.execute("SELECT id FROM events WHERE scope=?", (scope,)).fetchall()]
        topic_ids = [r[0] for r in c.execute("SELECT id FROM topics WHERE scope=?", (scope,)).fetchall()]
        tables = list(_MEM_SCOPE_TABLES)
        if subsystems:
            tables += list(_SUBSYS_SCOPE_TABLES)
        _archive_scope(scope, tables + ["events", "topics"])
        for eid in event_ids:
            c.execute("DELETE FROM event_relations WHERE src=? OR dst=?", (eid, eid))
            c.execute("DELETE FROM entity_events WHERE event_id=?", (eid,))
        for eid in event_ids:
            c.execute("DELETE FROM events WHERE id=?", (eid,))
        for tid in topic_ids:
            c.execute("DELETE FROM topic_params WHERE topic_id=?", (tid,))
        for tid in topic_ids:
            c.execute("DELETE FROM topics WHERE id=?", (tid,))
        removed = len(event_ids) + len(topic_ids)
        for t in tables:
            removed += c.execute(f"DELETE FROM {t} WHERE scope=?", (scope,)).rowcount
        _maybe_commit()
    return removed


# ===== 结构化属性（偏好/画像类记忆）=====
def attr_set(scope, key, attr, value, confidence=0.7, updated_at=""):
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO memory_attrs(scope,key,attr,value,confidence,updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(scope,key,attr,value) DO UPDATE SET "
            "confidence=excluded.confidence, updated_at=excluded.updated_at",
            (
                scope,
                key or "",
                str(attr)[:50],
                str(value)[:500],
                float(confidence),
                updated_at or datetime.now().isoformat(timespec="seconds"),
            ),
        )
        _maybe_commit()


def attr_rows(scope=None, key=None, attr=None):
    with _lock:
        sql = "SELECT scope,key,attr,value,confidence,updated_at FROM memory_attrs"
        params = []
        conds = []
        if scope:
            conds.append("scope=?")
            params.append(scope)
        if key is not None:
            conds.append("key=?")
            params.append(key)
        if attr:
            conds.append("attr=?")
            params.append(attr)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY confidence DESC, updated_at DESC"
        return [dict(r) for r in _connect().execute(sql, params).fetchall()]


def attr_delete(scope, key=None):
    with _lock:
        c = _connect()
        if key is None:
            c.execute("DELETE FROM memory_attrs WHERE scope=?", (scope,))
        else:
            c.execute("DELETE FROM memory_attrs WHERE scope=? AND key=?", (scope, key))
        _maybe_commit()


# ===== 词法索引（FTS5 BM25，trigram 支持中文）=====
def fts_available() -> bool:
    try:
        _connect().execute("SELECT 1 FROM memories_fts LIMIT 0")
        return True
    except sqlite3.OperationalError:
        return False


def lexicon_sync(scope, key):
    """按 scope+key 重建 FTS 行（写入/更新记忆后调用）。"""
    if not fts_available():
        return
    key = key or ""
    with _lock:
        c = _connect()
        c.execute("DELETE FROM memories_fts WHERE scope=? AND key=?", (scope, key))
        rows = c.execute(
            "SELECT scope,key,fact FROM memories WHERE scope=? AND key=?",
            (scope, key),
        ).fetchall()
        c.executemany(
            "INSERT INTO memories_fts(scope,key,fact) VALUES(?,?,?)",
            [(r[0], r[1], r[2]) for r in rows],
        )
        _maybe_commit()


def lexicon_rebuild() -> int:
    """全量重建 FTS 索引。返回索引行数。"""
    if not fts_available():
        return 0
    with _lock:
        c = _connect()
        c.execute("DELETE FROM memories_fts")
        rows = c.execute("SELECT scope,key,fact FROM memories").fetchall()
        c.executemany(
            "INSERT INTO memories_fts(scope,key,fact) VALUES(?,?,?)",
            [(r[0], r[1], r[2]) for r in rows],
        )
        _maybe_commit()
        return len(rows)


def lexicon_search(query, scopes, limit=10):
    """FTS5 trigram BM25 检索；查询过短或失败时返回 []（调用方降级 LIKE）。"""
    if not fts_available() or not query or len(str(query).strip()) < 3:
        return []
    match_q = '"' + str(query).replace('"', " ").strip() + '"'
    sql = (
        "SELECT scope,key,fact,bm25(memories_fts) AS rank FROM memories_fts "
        "WHERE memories_fts MATCH ? "
    )
    params = [match_q]
    if scopes:
        marks = ",".join("?" * len(scopes))
        sql += f"AND scope IN ({marks}) "
        params += list(scopes)
    sql += "ORDER BY rank LIMIT ?"
    params.append(max(1, int(limit)))
    with _lock:
        try:
            rows = _connect().execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return []
        return [
            {
                "scope": r["scope"],
                "key": r["key"],
                "fact": r["fact"],
                "rank": float(r["rank"]),
            }
            for r in rows
        ]


# ===== v3：记忆历史 / 反馈日志 / 关系引擎 / 策略日志 =====
def history_add(scope, key, fact, action, reason="", old_value="", new_value="", old_confidence=None, new_confidence=None):
    """记录一条记忆变更（冲突合并/纠错下调/确认上调/遗忘删除）。"""
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO memory_history(scope,key,fact,action,old_value,new_value,old_confidence,new_confidence,reason,ts) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                scope,
                key or "",
                str(fact)[:500],
                str(action)[:30],
                str(old_value or "")[:500],
                str(new_value or "")[:500],
                float(old_confidence) if old_confidence is not None else None,
                float(new_confidence) if new_confidence is not None else None,
                str(reason)[:300],
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        _maybe_commit()


def history_rows(scope=None, key=None, limit=50):
    with _lock:
        sql = "SELECT * FROM memory_history"
        conds, params = [], []
        if scope:
            conds.append("scope=?")
            params.append(scope)
        if key is not None:
            conds.append("key=?")
            params.append(key)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        return [dict(r) for r in _connect().execute(sql, params).fetchall()]


def feedback_add(scope, key, kind, fact="", detail="", source="chat", weight=1.0):
    """记录用户反馈（确认/纠错/否定/点赞），带分级权重（v5）：点赞0.3 / 确认0.5 / 明确纠正1.0。"""
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO feedback_log(ts,scope,key,kind,fact,detail,source,weight) VALUES(?,?,?,?,?,?,?,?)",
            (
                datetime.now().isoformat(timespec="seconds"),
                scope,
                key or "",
                str(kind)[:30],
                str(fact or "")[:500],
                str(detail or "")[:300],
                str(source)[:30],
                float(weight),
            ),
        )
        _maybe_commit()


def feedback_rows(scope=None, limit=50):
    with _lock:
        sql = "SELECT * FROM feedback_log"
        params = []
        if scope:
            sql += " WHERE scope=?"
            params.append(scope)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        return [dict(r) for r in _connect().execute(sql, params).fetchall()]


def relationship_get(scope):
    with _lock:
        row = _connect().execute("SELECT * FROM relationships WHERE scope=?", (scope,)).fetchone()
        return dict(row) if row else None


def relationship_upsert(
    scope,
    subject="",
    trust=None,
    familiarity=None,
    closeness=None,
    stage=None,
    history=None,
    updated_at="",
):
    """写入关系状态（None 表示保持原值）。"""
    with _lock:
        c = _connect()
        cur = relationship_get(scope) or {}
        c.execute(
            "INSERT INTO relationships(scope,subject,trust,familiarity,closeness,stage,history,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(scope) DO UPDATE SET "
            "subject=excluded.subject, trust=excluded.trust, familiarity=excluded.familiarity, "
            "closeness=excluded.closeness, stage=excluded.stage, history=excluded.history, updated_at=excluded.updated_at",
            (
                scope,
                str(subject or cur.get("subject", ""))[:100],
                float(trust if trust is not None else cur.get("trust", 0.3)),
                float(familiarity if familiarity is not None else cur.get("familiarity", 0.0)),
                float(closeness if closeness is not None else cur.get("closeness", 0.0)),
                str(stage or cur.get("stage", "陌生"))[:20],
                json.dumps(history if history is not None else json.loads(cur.get("history", "[]")), ensure_ascii=False),
                updated_at or datetime.now().isoformat(timespec="seconds"),
            ),
        )
        _maybe_commit()


def relationship_rows():
    with _lock:
        return [dict(r) for r in _connect().execute("SELECT * FROM relationships ORDER BY updated_at DESC").fetchall()]


def policy_log_add(trigger, behavior, priority=None, detail=""):
    """记录一条策略操作（遗忘/巩固/修剪/升迁），便于复盘记忆为什么变化。"""
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO policy_log(ts,trigger,behavior,priority,detail) VALUES(?,?,?,?,?)",
            (
                datetime.now().isoformat(timespec="seconds"),
                str(trigger)[:50],
                str(behavior)[:100],
                float(priority) if priority is not None else None,
                str(detail)[:500],
            ),
        )
        _maybe_commit()


def policy_log_rows(limit=50):
    with _lock:
        rows = _connect().execute(
            "SELECT * FROM policy_log ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)
        ).fetchall()
        return [dict(r) for r in rows]


# ===== v6：目标规划 / 决策咨询会话 =====
def goal_add(scope, title, priority=3, deadline="", note="", motivation="", confidence=0.7, current_state=None):
    with _lock:
        c = _connect()
        ts = datetime.now().isoformat(timespec="seconds")
        c.execute(
            "INSERT INTO goals(scope,title,status,priority,deadline,progress,note,motivation,confidence,current_state,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(scope,title) DO UPDATE SET "
            "status='active', priority=excluded.priority, deadline=excluded.deadline, "
            "motivation=excluded.motivation, confidence=excluded.confidence, "
            "current_state=excluded.current_state, updated_at=excluded.updated_at",
            (
                scope,
                str(title)[:100],
                "active",
                max(1, min(5, int(priority))),
                str(deadline)[:30],
                0.0,
                str(note)[:300],
                str(motivation)[:200],
                max(0.05, min(1.0, float(confidence))),
                json.dumps(current_state or {}, ensure_ascii=False)[:500],
                ts,
                ts,
            ),
        )
        _maybe_commit()


def goal_rows(scope=None, status=None, limit=50):
    with _lock:
        sql = "SELECT * FROM goals"
        conds, params = [], []
        if scope:
            conds.append("scope=?")
            params.append(scope)
        if status:
            conds.append("status=?")
            params.append(status)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY status='done', priority ASC, updated_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        return [dict(r) for r in _connect().execute(sql, params).fetchall()]


def goal_update(scope, title, progress=None, status=None, note=None, motivation=None, confidence=None, current_state=None):
    with _lock:
        c = _connect()
        sets, params = ["updated_at=?"], [datetime.now().isoformat(timespec="seconds")]
        if progress is not None:
            sets.append("progress=?")
            params.append(max(0.0, min(1.0, float(progress))))
        if status:
            sets.append("status=?")
            params.append(status)
        if note is not None:
            sets.append("note=?")
            params.append(str(note)[:300])
        if motivation is not None:
            sets.append("motivation=?")
            params.append(str(motivation)[:200])
        if confidence is not None:
            sets.append("confidence=?")
            params.append(max(0.05, min(1.0, float(confidence))))
        if current_state is not None:
            sets.append("current_state=?")
            params.append(json.dumps(current_state, ensure_ascii=False)[:500])
        params += [scope, str(title)[:100]]
        c.execute(f"UPDATE goals SET {', '.join(sets)} WHERE scope=? AND title=?", params)
        _maybe_commit()


def consult_get(scope):
    with _lock:
        row = _connect().execute(
            "SELECT * FROM consultations WHERE scope=? AND status='active'", (scope,)
        ).fetchone()
        return dict(row) if row else None


def consult_save(scope, topic, status, stage, answers, created_at=""):
    with _lock:
        c = _connect()
        ts = datetime.now().isoformat(timespec="seconds")
        c.execute(
            "INSERT INTO consultations(scope,topic,status,stage,answers,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(scope) DO UPDATE SET "
            "topic=excluded.topic, status=excluded.status, stage=excluded.stage, "
            "answers=excluded.answers, created_at=excluded.created_at, updated_at=excluded.updated_at",
            (
                scope,
                str(topic)[:100],
                str(status)[:20],
                max(0, int(stage)),
                json.dumps(list(answers), ensure_ascii=False)[:3000],
                str(created_at or ts),
                ts,
            ),
        )
        _maybe_commit()


# ===== v7：语言语义解释层 =====
def expr_log_add(raw_expression, normalized_meaning="", possible_intents=None, confidence=0.5, context=""):
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO language_context(raw_expression,normalized_meaning,possible_intents,confidence,context,created_time) "
            "VALUES(?,?,?,?,?,?)",
            (
                str(raw_expression)[:100],
                str(normalized_meaning or "")[:200],
                json.dumps(possible_intents or [], ensure_ascii=False)[:1000],
                float(confidence),
                str(context or "")[:200],
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        _maybe_commit()


def expr_log_rows(limit=50):
    with _lock:
        rows = _connect().execute(
            "SELECT * FROM language_context ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)
        ).fetchall()
        return [dict(r) for r in rows]


def expr_profile_get(scope):
    with _lock:
        row = _connect().execute(
            "SELECT * FROM user_expression_profile WHERE scope=?", (scope,)
        ).fetchone()
        return dict(row) if row else None


def expr_profile_upsert(
    scope,
    slang_frequency=0.0,
    irony_usage=0.0,
    emoji_usage=0.0,
    serious_mode_switch=0,
    humor_style="unknown",
    communication_style="unknown",
    formality_level=0.5,
):
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO user_expression_profile("
            "scope,slang_frequency,irony_usage,emoji_usage,serious_mode_switch,humor_style,communication_style,formality_level,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(scope) DO UPDATE SET "
            "slang_frequency=excluded.slang_frequency, irony_usage=excluded.irony_usage, "
            "emoji_usage=excluded.emoji_usage, serious_mode_switch=excluded.serious_mode_switch, "
            "humor_style=excluded.humor_style, communication_style=excluded.communication_style, "
            "formality_level=excluded.formality_level, updated_at=excluded.updated_at",
            (
                scope,
                max(0.0, min(1.0, float(slang_frequency))),
                max(0.0, min(1.0, float(irony_usage))),
                max(0.0, min(1.0, float(emoji_usage))),
                1 if serious_mode_switch else 0,
                str(humor_style)[:20],
                str(communication_style)[:20],
                max(0.0, min(1.0, float(formality_level))),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        _maybe_commit()


# ===== v10：Memory Trace Export =====
def trace_add(
    conversation_id="",
    ts="",
    scope="",
    speaker="user",
    raw_content="",
    semantic_analysis="{}",
    intent="",
    entities="[]",
    events="[]",
    emotion="",
    slang_interpretation="[]",
    memory_candidate="",
    memory_action="",
    memory_id="",
    confidence=None,
    source="",
    reasoning="",
    affected_modules="[]",
    context_hint="",
):
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO memory_trace("
            "conversation_id,ts,scope,speaker,raw_content,semantic_analysis,intent,entities,events,emotion,"
            "slang_interpretation,memory_candidate,memory_action,memory_id,confidence,source,reasoning,"
            "affected_modules,context_hint) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(conversation_id)[:80],
                str(ts)[:40],
                str(scope)[:80],
                str(speaker)[:20],
                str(raw_content)[:500],
                str(semantic_analysis)[:2000],
                str(intent)[:50],
                str(entities)[:500],
                str(events)[:500],
                str(emotion)[:30],
                str(slang_interpretation)[:800],
                str(memory_candidate)[:300],
                str(memory_action)[:30],
                str(memory_id)[:100],
                float(confidence) if confidence is not None else None,
                str(source)[:50],
                str(reasoning)[:300],
                str(affected_modules)[:300],
                str(context_hint)[:200],
            ),
        )
        _maybe_commit()


def trace_rows(scope=None, since=None, limit=100):
    with _lock:
        sql = "SELECT * FROM memory_trace"
        conds, params = [], []
        if scope:
            conds.append("scope=?")
            params.append(scope)
        if since:
            conds.append("ts>=?")
            params.append(since)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        return [dict(r) for r in _connect().execute(sql, params).fetchall()]


def trace_prune(days=7) -> int:
    """清理超过保留期的轨迹（默认 7 天），防表膨胀。"""
    import datetime as _dt
    cutoff = (_dt.datetime.now() - _dt.timedelta(days=max(1, int(days)))).isoformat(timespec="seconds")
    with _lock:
        c = _connect()
        cur = c.execute("DELETE FROM memory_trace WHERE ts<?", (cutoff,))
        _maybe_commit()
        return cur.rowcount


def trace_review_add(trace_id, score, scores=None, comment="", reviewer=""):
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO trace_review(trace_id,score,scores,comment,reviewer,created_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(trace_id,reviewer) DO UPDATE SET "
            "score=excluded.score, scores=excluded.scores, comment=excluded.comment, created_at=excluded.created_at",
            (
                int(trace_id),
                max(1.0, min(5.0, float(score))),
                json.dumps(scores or {}, ensure_ascii=False),
                str(comment)[:300],
                str(reviewer)[:50],
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        _maybe_commit()


def trace_review_map(trace_ids):
    """返回 {trace_id: review_row}，供导出时展示人工评分。"""
    ids = [int(i) for i in trace_ids if i]
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    with _lock:
        rows = _connect().execute(
            f"SELECT * FROM trace_review WHERE trace_id IN ({marks})", ids
        ).fetchall()
        return {r["trace_id"]: dict(r) for r in rows}


def trace_review_recent(limit=100):
    with _lock:
        rows = _connect().execute(
            "SELECT * FROM trace_review ORDER BY created_at DESC, rowid DESC LIMIT ?", (max(1, int(limit)),)
        ).fetchall()
        return [dict(r) for r in rows]


# ===== v33：对话质量评分（convreview）——用户消息+AI 回复一条记录 =====

def conv_add(conversation_id="", scope="", ts="", user_text="", ai_text=""):
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO conv_log(conversation_id,scope,ts,user_text,ai_text) VALUES(?,?,?,?,?)",
            (
                str(conversation_id)[:80],
                str(scope)[:80],
                str(ts)[:40],
                str(user_text)[:500],
                str(ai_text)[:800],
            ),
        )
        _maybe_commit()


def conv_rows(scope=None, since=None, limit=100):
    with _lock:
        sql = "SELECT * FROM conv_log"
        conds, params = [], []
        if scope:
            conds.append("scope=?")
            params.append(scope)
        if since:
            conds.append("ts>=?")
            params.append(since)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        return [dict(r) for r in _connect().execute(sql, params).fetchall()]


def conv_prune(days=30) -> int:
    """清理超过保留期的对话记录（默认 30 天），防表膨胀。"""
    import datetime as _dt
    cutoff = (_dt.datetime.now() - _dt.timedelta(days=max(1, int(days)))).isoformat(timespec="seconds")
    with _lock:
        c = _connect()
        cur = c.execute("DELETE FROM conv_log WHERE ts<?", (cutoff,))
        _maybe_commit()
        return cur.rowcount


def conv_review_add(conv_id, score, scores=None, comment="", reviewer=""):
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO conv_review(conv_id,score,scores,comment,reviewer,created_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(conv_id,reviewer) DO UPDATE SET "
            "score=excluded.score, scores=excluded.scores, comment=excluded.comment, created_at=excluded.created_at",
            (
                int(conv_id),
                max(1.0, min(5.0, float(score))),
                json.dumps(scores or {}, ensure_ascii=False),
                str(comment)[:300],
                str(reviewer)[:50],
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        _maybe_commit()


def conv_review_map(conv_ids):
    """返回 {conv_id: review_row}，供导出时展示人工评分。"""
    ids = [int(i) for i in conv_ids if i]
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    with _lock:
        rows = _connect().execute(
            f"SELECT * FROM conv_review WHERE conv_id IN ({marks})", ids
        ).fetchall()
        return {r["conv_id"]: dict(r) for r in rows}


def conv_review_recent(limit=100):
    with _lock:
        rows = _connect().execute(
            "SELECT * FROM conv_review ORDER BY created_at DESC, rowid DESC LIMIT ?", (max(1, int(limit)),)
        ).fetchall()
        return [dict(r) for r in rows]


# ===== v12：全量数据打包导出 / 导入 =====
# 用户数据表（可导出）；派生索引表（bm25/vec/fts）跳过，导入后由 grow 重建
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
    """全表导出为 {table: [row_dict]}（仅用户数据表）。"""
    out = {}
    for t in DUMP_TABLES:
        try:
            rows = _connect().execute(f"SELECT * FROM {t}").fetchall()
            out[t] = [dict(r) for r in rows]
        except sqlite3.OperationalError:
            out[t] = []
    return out


def restore_all(data, replace=False) -> dict:
    """从 dump_all 的结构导入；replace=True 先清空目标表，否则 INSERT OR REPLACE 合并。"""
    counts = {}
    with _lock:
        c = _connect()
        for t, rows in (data or {}).items():
            if t not in DUMP_TABLES or not isinstance(rows, list):
                continue
            try:
                if replace:
                    c.execute(f"DELETE FROM {t}")
                if rows:
                    cols = list(rows[0].keys())
                    marks = ",".join("?" * len(cols))
                    c.executemany(
                        f"INSERT OR REPLACE INTO {t}({','.join(cols)}) VALUES({marks})",
                        [tuple(r.get(col) for col in cols) for r in rows],
                    )
                counts[t] = len(rows)
            except Exception as e:
                _stats_err(e)
                counts[t] = -1  # 该表导入失败（可能字段不兼容），跳过
        # 自增序列对齐，保证后续插入不冲突
        for t in (
            "events", "event_relations", "topics", "memory_history", "feedback_log",
            "policy_log", "belief_log", "query_log", "audit", "notifications",
            "sessions", "entities", "language_context", "memory_trace",
        ):
            try:
                mx = c.execute(f"SELECT COALESCE(MAX(id),0) FROM {t}").fetchone()[0]
                c.execute("UPDATE sqlite_sequence SET seq=? WHERE name=?", (int(mx or 0), t))
            except Exception as e:
                _stats_err(e)
                pass
        _maybe_commit()
    return counts


def backup_to(path):
    """SQLite 在线安全备份（WAL 模式下也一致）。"""
    dst = sqlite3.connect(str(path))
    try:
        with dst:
            _connect().backup(dst)
    finally:
        dst.close()

