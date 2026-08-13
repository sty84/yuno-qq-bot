"""SQLite 数据层：记忆、绑定、分数、昵称、状态、群列表。

替代散落的 JSON 文件；首次启动会自动把旧 JSON 数据迁移进来。
"""

import json
import os
import pathlib
import sqlite3
import threading
from collections import Counter
from datetime import datetime, timedelta

DB_PATH = None
_conn = None
# RLock：允许同一线程在持锁时触发惰性初始化（_connect→init→_create_tables 也会加锁），
# 避免 memory_rows 等函数首次调用时自锁死。
_lock = threading.RLock()
_migrated = False
AUDIT_MAX = 5000


def set_audit_max(n):
    global AUDIT_MAX
    AUDIT_MAX = max(1, int(n))


def init(data_dir):
    global DB_PATH, _conn, _migrated
    if DB_PATH is not None:
        return
    DB_PATH = pathlib.Path(data_dir) / "bot.db"
    _conn = sqlite3.connect(str(DB_PATH), timeout=10, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    # WAL 模式下 synchronous=NORMAL 安全且大幅降低提交延迟（性能优化）
    _conn.execute("PRAGMA synchronous=NORMAL")
    _create_tables()
    if not _migrated:
        _migrate_legacy(pathlib.Path(data_dir))
        _migrated = True
    _migrate_facts_to_memories()
    _migrate_ai_to_unified()


def _connect():
    if _conn is None:
        # 未显式初始化时用默认 data 目录
        default = pathlib.Path(
            os.getenv(
                "CONFIG_PATH",
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.json"),
            )
        ).parent / "data"
        init(default)
    return _conn


def _create_tables():
    with _lock:
        c = _connect()
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS facts(
                scene TEXT NOT NULL, key TEXT NOT NULL, idx INTEGER NOT NULL,
                fact TEXT NOT NULL, updated_at TEXT,
                PRIMARY KEY(scene,key,idx));
            CREATE TABLE IF NOT EXISTS kv(
                namespace TEXT NOT NULL, key TEXT NOT NULL, value TEXT,
                PRIMARY KEY(namespace,key));
            CREATE TABLE IF NOT EXISTS item_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item TEXT NOT NULL,
                ts TEXT NOT NULL,
                event TEXT NOT NULL DEFAULT 'move',
                from_place TEXT NOT NULL DEFAULT '',
                to_place TEXT NOT NULL DEFAULT '',
                cause TEXT NOT NULL DEFAULT '',
                seen_by TEXT NOT NULL DEFAULT '');
            CREATE INDEX IF NOT EXISTS idx_item_events_item_ts ON item_events(item, ts);
            CREATE TABLE IF NOT EXISTS bindings(
                user_openid TEXT NOT NULL, group_id TEXT NOT NULL,
                member_openid TEXT NOT NULL,
                PRIMARY KEY(user_openid,group_id));
            CREATE TABLE IF NOT EXISTS scores(
                player TEXT NOT NULL, game TEXT NOT NULL,
                score INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(player,game));
            CREATE TABLE IF NOT EXISTS nicknames(
                player TEXT NOT NULL, nickname TEXT NOT NULL,
                PRIMARY KEY(player));
            CREATE TABLE IF NOT EXISTS state(
                k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS audit(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL, action TEXT NOT NULL,
                target TEXT, detail TEXT, operator TEXT);
            CREATE TABLE IF NOT EXISTS notifications(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_type TEXT NOT NULL, target TEXT NOT NULL,
                content TEXT NOT NULL, created_at TEXT, sent_at TEXT,
                scheduled_at TEXT,
                retries INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS memories(
                scope TEXT NOT NULL, key TEXT NOT NULL,
                fact TEXT NOT NULL, embedding TEXT,
                updated_at TEXT,
                confidence REAL NOT NULL DEFAULT 0.7,
                source TEXT NOT NULL DEFAULT '',
                audience TEXT NOT NULL DEFAULT '',
                speaker TEXT NOT NULL DEFAULT '',
                mclass TEXT NOT NULL DEFAULT 'short',
                arousal REAL NOT NULL DEFAULT 0.0,
                valence REAL NOT NULL DEFAULT 0.0,
                privacy REAL NOT NULL DEFAULT 0.0,
                PRIMARY KEY(scope,key,fact));
            CREATE TABLE IF NOT EXISTS memory_attrs(
                scope TEXT NOT NULL, key TEXT NOT NULL DEFAULT '',
                attr TEXT NOT NULL,
                value TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.7,
                updated_at TEXT,
                PRIMARY KEY(scope,key,attr,value));
            CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope,key);
            CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL, key TEXT NOT NULL DEFAULT '',
                etype TEXT NOT NULL DEFAULT 'event',
                title TEXT NOT NULL,
                content TEXT,
                importance REAL NOT NULL DEFAULT 0.5,
                ts TEXT NOT NULL DEFAULT '',
                ts_source TEXT NOT NULL DEFAULT 'approx',
                embedding TEXT,
                updated_at TEXT,
                topic_id INTEGER,
                UNIQUE(scope,key,title));
            CREATE INDEX IF NOT EXISTS idx_events_scope ON events(scope,key);
            CREATE TABLE IF NOT EXISTS event_relations(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                src INTEGER NOT NULL,
                dst INTEGER NOT NULL,
                rel TEXT NOT NULL DEFAULT 'influences',
                weight REAL NOT NULL DEFAULT 1.0,
                updated_at TEXT,
                UNIQUE(src,dst,rel));
            CREATE INDEX IF NOT EXISTS idx_relations_src ON event_relations(src);
            CREATE INDEX IF NOT EXISTS idx_relations_dst ON event_relations(dst);
            CREATE TABLE IF NOT EXISTS ai_memory(
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                importance REAL NOT NULL DEFAULT 0.5,
                embedding TEXT,
                updated_at TEXT,
                PRIMARY KEY(kind,content));
            CREATE TABLE IF NOT EXISTS memory_meta(
                scope TEXT NOT NULL, key TEXT NOT NULL DEFAULT '',
                fact TEXT NOT NULL,
                access_count INTEGER NOT NULL DEFAULT 0,
                last_access TEXT,
                importance REAL NOT NULL DEFAULT 0.5,
                PRIMARY KEY(scope,key,fact));
            CREATE TABLE IF NOT EXISTS topics(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL, key TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL,
                topic TEXT NOT NULL,
                importance REAL NOT NULL DEFAULT 0.5,
                confidence REAL NOT NULL DEFAULT 0.7,
                status TEXT NOT NULL DEFAULT 'active',
                started_at TEXT, updated_at TEXT,
                UNIQUE(scope,key,category,topic));
            CREATE INDEX IF NOT EXISTS idx_topics_scope ON topics(scope,key);
            CREATE TABLE IF NOT EXISTS topic_params(
                topic_id INTEGER NOT NULL,
                param TEXT NOT NULL,
                value TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.7,
                updated_at TEXT,
                PRIMARY KEY(topic_id,param,value));
            CREATE TABLE IF NOT EXISTS vec_index(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL, key TEXT NOT NULL, fact TEXT NOT NULL,
                centroid_id INTEGER NOT NULL,
                embedding TEXT NOT NULL,
                UNIQUE(scope,key,fact));
            CREATE INDEX IF NOT EXISTS idx_vec_centroid ON vec_index(centroid_id);
            CREATE TABLE IF NOT EXISTS vec_centroids(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dim INTEGER NOT NULL,
                embedding TEXT NOT NULL,
                n INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS bm25_terms(
                term TEXT NOT NULL,
                scope TEXT NOT NULL, key TEXT NOT NULL, fact TEXT NOT NULL,
                tf INTEGER NOT NULL,
                PRIMARY KEY(term,scope,key,fact));
            CREATE INDEX IF NOT EXISTS idx_bm25_scope ON bm25_terms(scope,key);
            CREATE TABLE IF NOT EXISTS bm25_docs(
                scope TEXT NOT NULL, key TEXT NOT NULL, fact TEXT NOT NULL,
                doc_len INTEGER NOT NULL,
                PRIMARY KEY(scope,key,fact));
            CREATE TABLE IF NOT EXISTS belief_log(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                old_content TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL,
                confidence REAL,
                note TEXT,
                ts TEXT);
            CREATE TABLE IF NOT EXISTS query_log(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                query TEXT NOT NULL,
                scopes TEXT NOT NULL DEFAULT '',
                top_k INTEGER,
                hits TEXT NOT NULL DEFAULT '[]',
                followup INTEGER NOT NULL DEFAULT 0,
                exported INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS sessions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL, key TEXT NOT NULL DEFAULT '',
                topic TEXT NOT NULL DEFAULT '',
                started_at TEXT, updated_at TEXT,
                message_count INTEGER NOT NULL DEFAULT 0,
                summary TEXT NOT NULL DEFAULT '',
                closed INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS entities(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL, key TEXT NOT NULL DEFAULT '',
                canonical TEXT NOT NULL,
                UNIQUE(scope,key,canonical));
            CREATE TABLE IF NOT EXISTS entity_aliases(
                entity_id INTEGER NOT NULL, alias TEXT NOT NULL,
                PRIMARY KEY(entity_id,alias));
            CREATE TABLE IF NOT EXISTS entity_events(
                entity_id INTEGER NOT NULL, event_id INTEGER NOT NULL,
                PRIMARY KEY(entity_id,event_id));
            CREATE TABLE IF NOT EXISTS memory_history(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL, key TEXT NOT NULL,
                fact TEXT NOT NULL,
                action TEXT NOT NULL,
                old_value TEXT, new_value TEXT,
                old_confidence REAL, new_confidence REAL,
                reason TEXT,
                ts TEXT);
            CREATE INDEX IF NOT EXISTS idx_mem_hist_scope ON memory_history(scope,key);
            CREATE TABLE IF NOT EXISTS feedback_log(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                scope TEXT NOT NULL, key TEXT NOT NULL,
                kind TEXT NOT NULL,
                fact TEXT,
                detail TEXT,
                source TEXT NOT NULL DEFAULT 'chat');
            CREATE INDEX IF NOT EXISTS idx_feedback_scope ON feedback_log(scope,key);
            CREATE TABLE IF NOT EXISTS relationships(
                scope TEXT PRIMARY KEY,
                subject TEXT NOT NULL DEFAULT '',
                object TEXT NOT NULL DEFAULT 'ai',
                trust REAL NOT NULL DEFAULT 0.3,
                familiarity REAL NOT NULL DEFAULT 0.0,
                closeness REAL NOT NULL DEFAULT 0.0,
                stage TEXT NOT NULL DEFAULT '陌生',
                history TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT);
            CREATE TABLE IF NOT EXISTS policy_log(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                trigger TEXT NOT NULL,
                behavior TEXT NOT NULL,
                priority REAL,
                detail TEXT);
            CREATE TABLE IF NOT EXISTS goals(
                scope TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                priority INTEGER NOT NULL DEFAULT 3,
                deadline TEXT NOT NULL DEFAULT '',
                progress REAL NOT NULL DEFAULT 0.0,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT, updated_at TEXT,
                PRIMARY KEY(scope,title));
            CREATE TABLE IF NOT EXISTS consultations(
                scope TEXT PRIMARY KEY,
                topic TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                stage INTEGER NOT NULL DEFAULT 0,
                answers TEXT NOT NULL DEFAULT '[]',
                created_at TEXT, updated_at TEXT);
            CREATE TABLE IF NOT EXISTS procedures(
                situation TEXT NOT NULL,
                action TEXT NOT NULL,
                success REAL NOT NULL DEFAULT 0.5,
                tries INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT,
                PRIMARY KEY(situation, action));
            CREATE TABLE IF NOT EXISTS state_invalidations(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                key TEXT NOT NULL DEFAULT '',
                fact TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                ts TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS experiment_log(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                action TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                before TEXT NOT NULL DEFAULT '',
                after TEXT NOT NULL DEFAULT '',
                delta TEXT NOT NULL DEFAULT '',
                regression INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS scenario_scores(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                scenario_id TEXT NOT NULL DEFAULT '',
                scope TEXT NOT NULL DEFAULT '',
                mode TEXT NOT NULL DEFAULT 'manual',
                scores TEXT NOT NULL DEFAULT '{}',
                comment TEXT NOT NULL DEFAULT '',
                avg REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS llm_cost(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                module TEXT NOT NULL DEFAULT 'chat',
                detail TEXT NOT NULL DEFAULT '',
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                chars INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS hesitation_log(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                delay_s INTEGER NOT NULL DEFAULT 0,
                monologue TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS space_state(
                id INTEGER PRIMARY KEY CHECK(id=1),
                room TEXT NOT NULL DEFAULT '客厅',
                state TEXT NOT NULL DEFAULT '在场',
                from_room TEXT NOT NULL DEFAULT '',
                to_room TEXT NOT NULL DEFAULT '',
                path TEXT NOT NULL DEFAULT '[]',
                depart_ts TEXT NOT NULL DEFAULT '',
                arrive_ts TEXT NOT NULL DEFAULT '',
                updated_ts TEXT);
            CREATE TABLE IF NOT EXISTS item_activation_state(
                item TEXT PRIMARY KEY,
                seen_ts TEXT NOT NULL DEFAULT '',
                count INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS item_search_state(
                scope TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                queue TEXT NOT NULL DEFAULT '[]',
                step INTEGER NOT NULL DEFAULT 0,
                started_at TEXT NOT NULL DEFAULT '',
                updated_ts TEXT);
            CREATE TABLE IF NOT EXISTS mind_intention_state(
                scope TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                strength REAL NOT NULL DEFAULT 0.0,
                state TEXT NOT NULL DEFAULT 'committed',
                due TEXT NOT NULL DEFAULT '',
                condition TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS space_events_state(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT '',
                memorable INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS ai_actions_state(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS language_context(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                raw_expression TEXT NOT NULL,
                normalized_meaning TEXT,
                possible_intents TEXT NOT NULL DEFAULT '[]',
                confidence REAL NOT NULL DEFAULT 0.5,
                context TEXT NOT NULL DEFAULT '',
                created_time TEXT);
            CREATE TABLE IF NOT EXISTS user_expression_profile(
                scope TEXT PRIMARY KEY,
                slang_frequency REAL NOT NULL DEFAULT 0.0,
                irony_usage REAL NOT NULL DEFAULT 0.0,
                emoji_usage REAL NOT NULL DEFAULT 0.0,
                serious_mode_switch INTEGER NOT NULL DEFAULT 0,
                humor_style TEXT NOT NULL DEFAULT 'unknown',
                communication_style TEXT NOT NULL DEFAULT 'unknown',
                formality_level REAL NOT NULL DEFAULT 0.5,
                updated_at TEXT);
            CREATE TABLE IF NOT EXISTS memory_trace(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL DEFAULT '',
                ts TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT '',
                speaker TEXT NOT NULL DEFAULT 'user',
                raw_content TEXT NOT NULL DEFAULT '',
                semantic_analysis TEXT NOT NULL DEFAULT '{}',
                intent TEXT NOT NULL DEFAULT '',
                entities TEXT NOT NULL DEFAULT '[]',
                events TEXT NOT NULL DEFAULT '[]',
                emotion TEXT NOT NULL DEFAULT '',
                slang_interpretation TEXT NOT NULL DEFAULT '[]',
                memory_candidate TEXT NOT NULL DEFAULT '',
                memory_action TEXT NOT NULL DEFAULT '',
                memory_id TEXT NOT NULL DEFAULT '',
                confidence REAL,
                source TEXT NOT NULL DEFAULT '',
                reasoning TEXT NOT NULL DEFAULT '',
                affected_modules TEXT NOT NULL DEFAULT '[]',
                context_hint TEXT NOT NULL DEFAULT '');
            CREATE INDEX IF NOT EXISTS idx_trace_scope_ts ON memory_trace(scope, ts);
            CREATE TABLE IF NOT EXISTS trace_review(
                trace_id INTEGER NOT NULL,
                score REAL NOT NULL,
                comment TEXT NOT NULL DEFAULT '',
                reviewer TEXT NOT NULL DEFAULT '',
                created_at TEXT,
                UNIQUE(trace_id, reviewer));
            """
        )
        c.commit()
        for col, ddl in (
            ("retries", "INTEGER NOT NULL DEFAULT 0"),
            ("failed", "INTEGER NOT NULL DEFAULT 0"),
        ):
            try:
                c.execute(f"ALTER TABLE notifications ADD COLUMN {col} {ddl}")
                c.commit()
            except sqlite3.OperationalError:
                pass
        try:
            c.execute("ALTER TABLE memories ADD COLUMN confidence REAL NOT NULL DEFAULT 0.7")
            c.commit()
        except sqlite3.OperationalError:
            pass
        try:
            c.execute("ALTER TABLE memories ADD COLUMN source TEXT NOT NULL DEFAULT ''")
            c.commit()
        except sqlite3.OperationalError:
            pass
        for col, ddl in (
            ("audience", "TEXT NOT NULL DEFAULT ''"),
            ("speaker", "TEXT NOT NULL DEFAULT ''"),
            ("mclass", "TEXT NOT NULL DEFAULT 'short'"),
            ("arousal", "REAL NOT NULL DEFAULT 0.0"),
            ("valence", "REAL NOT NULL DEFAULT 0.0"),
            ("privacy", "REAL NOT NULL DEFAULT 0.0"),
        ):
            try:
                c.execute(f"ALTER TABLE memories ADD COLUMN {col} {ddl}")
                c.commit()
            except sqlite3.OperationalError:
                pass
        try:
            c.execute("ALTER TABLE events ADD COLUMN topic_id INTEGER")
            c.commit()
        except sqlite3.OperationalError:
            pass
        for col, ddl in (
            ("memory_scope", "TEXT"),
            ("memory_key", "TEXT"),
            ("memory_fact", "TEXT"),
        ):
            try:
                c.execute(f"ALTER TABLE events ADD COLUMN {col} {ddl}")
                c.commit()
            except sqlite3.OperationalError:
                pass
        try:
            c.execute("ALTER TABLE events ADD COLUMN ts_source TEXT NOT NULL DEFAULT 'approx'")
            c.commit()
        except sqlite3.OperationalError:
            pass
        try:
            c.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
            c.commit()
        except sqlite3.OperationalError:
            pass
        for col, ddl in (
            ("valid_from", "TEXT"),
            ("valid_to", "TEXT"),
            ("status", "TEXT NOT NULL DEFAULT 'active'"),
        ):
            try:
                c.execute(f"ALTER TABLE memories ADD COLUMN {col} {ddl}")
                c.commit()
            except sqlite3.OperationalError:
                pass
        try:
            c.execute("ALTER TABLE feedback_log ADD COLUMN weight REAL NOT NULL DEFAULT 1.0")
            c.commit()
        except sqlite3.OperationalError:
            pass
        try:
            c.execute("ALTER TABLE trace_review ADD COLUMN scores TEXT NOT NULL DEFAULT '{}'")
            c.commit()
        except sqlite3.OperationalError:
            pass
        try:
            # 犹豫层（v2.3）：通知可延后发送（scheduled_at 之前不发）
            c.execute("ALTER TABLE notifications ADD COLUMN scheduled_at TEXT")
            c.commit()
        except sqlite3.OperationalError:
            pass
        for col, ddl in (
            ("motivation", "TEXT NOT NULL DEFAULT ''"),
            ("confidence", "REAL NOT NULL DEFAULT 0.7"),
            ("current_state", "TEXT NOT NULL DEFAULT '{}'"),
        ):
            try:
                c.execute(f"ALTER TABLE goals ADD COLUMN {col} {ddl}")
                c.commit()
            except sqlite3.OperationalError:
                pass
        try:
            c.execute("ALTER TABLE belief_log ADD COLUMN old_content TEXT NOT NULL DEFAULT ''")
            c.commit()
        except sqlite3.OperationalError:
            pass
        # FTS5 词法索引（trigram 适合中文；不可用时走 LIKE 降级）
        try:
            c.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5("
                "scope UNINDEXED, key UNINDEXED, fact, tokenize='trigram')"
            )
            c.commit()
        except sqlite3.OperationalError as e:
            print(f"FTS5 不可用，词法检索降级为 LIKE：{e}")


def _count(table):
    return _connect().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _migrate_legacy(data_dir):
    d = pathlib.Path(data_dir)
    c = _connect()
    try:
        bf = d / "bindings.json"
        if bf.exists() and _count("bindings") == 0:
            data = json.loads(bf.read_text(encoding="utf-8"))
            for uid, groups in data.items():
                for gid, mid in groups.items():
                    c.execute(
                        "INSERT OR IGNORE INTO bindings(user_openid,group_id,member_openid) VALUES(?,?,?)",
                        (uid, gid, mid),
                    )
        sf = d / "scores.json"
        if sf.exists() and _count("scores") == 0:
            data = json.loads(sf.read_text(encoding="utf-8"))
            for player, games in data.items():
                for game, score in games.items():
                    c.execute(
                        "INSERT OR IGNORE INTO scores(player,game,score) VALUES(?,?,?)",
                        (player, game, int(score)),
                    )
        nf = d / "nicknames.json"
        if nf.exists() and _count("nicknames") == 0:
            data = json.loads(nf.read_text(encoding="utf-8"))
            for player, name in data.items():
                c.execute(
                    "INSERT OR IGNORE INTO nicknames(player,nickname) VALUES(?,?)",
                    (player, name),
                )
        stf = d / "state.json"
        if stf.exists() and _count("state") == 0:
            data = json.loads(stf.read_text(encoding="utf-8"))
            for k, v in data.items():
                c.execute("INSERT OR IGNORE INTO state(k,v) VALUES(?,?)", (k, json.dumps(v)))
        mem = d / "memory"
        if mem.exists() and _count("facts") == 0:
            users = mem / "users"
            if users.exists():
                for f in users.glob("*.json"):
                    data = json.loads(f.read_text(encoding="utf-8"))
                    facts_replace("c2c", f.stem, data.get("facts", []), data.get("updated_at", ""))
            members = mem / "members"
            if members.exists():
                for gdir in members.iterdir():
                    if not gdir.is_dir():
                        continue
                    for f in gdir.glob("*.json"):
                        data = json.loads(f.read_text(encoding="utf-8"))
                        facts_replace("group", f"{gdir.name}:{f.stem}", data.get("facts", []), data.get("updated_at", ""))
            groups = mem / "groups"
            if groups.exists():
                for f in groups.glob("*.json"):
                    data = json.loads(f.read_text(encoding="utf-8"))
                    facts_replace("group_all", f.stem, data.get("facts", []), data.get("updated_at", ""))
                    kv_set("group", f"{f.stem}:count", data.get("message_count", 0))
        c.commit()
    except Exception as e:
        print(f"[db] 旧数据迁移失败（可忽略）：{e}")
        c.rollback()


_memories_migrated = False


def _migrate_facts_to_memories():
    """一次性把旧 facts 表迁移到统一 memories 表（QQ 场景 → scope/key）。"""
    global _memories_migrated
    if _memories_migrated:
        return
    c = _connect()
    if c.execute("SELECT COUNT(*) FROM memories").fetchone()[0] > 0:
        return
    rows = c.execute("SELECT scene,key,fact,updated_at FROM facts").fetchall()
    for r in rows:
        scene, k = r["scene"], r["key"]
        if scene == "c2c":
            scope, sk = f"c2c:{k}", ""
        elif scene == "group":
            gid, _, mid = k.partition(":")
            scope, sk = f"group:{gid}", mid
        elif scene == "group_all":
            scope, sk = f"group_all:{k}", ""
        else:
            continue
        c.execute(
            "INSERT OR IGNORE INTO memories(scope,key,fact,updated_at) VALUES(?,?,?,?)",
            (scope, sk, r["fact"], r["updated_at"]),
        )
    c.commit()
    _memories_migrated = True


_ai_migrated = False


def _migrate_ai_to_unified():
    """把旧 ai_memory 表并入统一 memories 表（scope='ai'，key=kind，importance→confidence）。"""
    global _ai_migrated
    if _ai_migrated:
        return
    c = _connect()
    if c.execute("SELECT COUNT(*) FROM memories WHERE scope='ai'").fetchone()[0] > 0:
        return
    rows = c.execute(
        "SELECT kind,content,importance,embedding,updated_at FROM ai_memory"
    ).fetchall()
    for r in rows:
        c.execute(
            "INSERT OR IGNORE INTO memories(scope,key,fact,embedding,updated_at,confidence) "
            "VALUES('ai',?,?,?,?,?)",
            (r["kind"], r["content"], r["embedding"], r["updated_at"] or "", float(r["importance"])),
        )
    c.commit()
    _ai_migrated = True


# ===== facts（记忆）=====
def facts_replace(scene, key, facts, updated_at=""):
    with _lock:
        c = _connect()
        c.execute("DELETE FROM facts WHERE scene=? AND key=?", (scene, key))
        for i, fact in enumerate(facts):
            c.execute(
                "INSERT INTO facts(scene,key,idx,fact,updated_at) VALUES(?,?,?,?,?)",
                (scene, key, i, str(fact), updated_at),
            )
        c.commit()


def facts_get(scene, key):
    with _lock:
        rows = _connect().execute(
            "SELECT fact FROM facts WHERE scene=? AND key=? ORDER BY idx", (scene, key)
        ).fetchall()
        return [r[0] for r in rows]


def facts_updated_at(scene, key):
    with _lock:
        row = _connect().execute(
            "SELECT MAX(updated_at) FROM facts WHERE scene=? AND key=?", (scene, key)
        ).fetchone()
        return row[0] or ""


# ===== kv（群计数、群列表等）=====
def kv_set(namespace, key, value):
    with _lock:
        c = _connect()
        c.execute(
            "INSERT OR REPLACE INTO kv(namespace,key,value) VALUES(?,?,?)",
            (namespace, key, json.dumps(value, ensure_ascii=False)),
        )
        c.commit()


def kv_get(namespace, key, default=None):
    with _lock:
        row = _connect().execute(
            "SELECT value FROM kv WHERE namespace=? AND key=?", (namespace, key)
        ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except Exception as e:
            _stats_err(e)
            return default


def kv_get_raw(namespace, key, default=None):
    """读取 kv 原始 JSON 字符串（CAS 用：保证与库内字节完全一致）。"""
    with _lock:
        row = _connect().execute(
            "SELECT value FROM kv WHERE namespace=? AND key=?", (namespace, key)
        ).fetchone()
        return row[0] if row else default


def kv_cas(namespace, key, old_raw, new_raw):
    """Compare-and-swap：仅当现值等于 old_raw 时写入 new_raw（跨进程原子，防并发重复）。"""
    with _lock:
        c = _connect()
        cur = c.execute(
            "SELECT value FROM kv WHERE namespace=? AND key=?", (namespace, key)
        ).fetchone()
        if cur is None or cur[0] != old_raw:
            return False
        c.execute(
            "UPDATE kv SET value=? WHERE namespace=? AND key=? AND value=?",
            (new_raw, namespace, key, old_raw),
        )
        c.commit()
        return c.total_changes > 0


# ===== 物品事件溯源（P0-1：位置历史 / 激活 / 找东西）=====
def item_event_add(item, ts, event, from_place="", to_place="", cause="", seen_by=""):
    """追加一条物品事件（move/give/see/take/consume/lost/find…）。"""
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO item_events(item,ts,event,from_place,to_place,cause,seen_by) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                str(item)[:60], str(ts), str(event)[:20],
                str(from_place)[:80], str(to_place)[:80],
                str(cause)[:80], str(seen_by)[:20],
            ),
        )
        c.commit()


def item_event_add_many(rows):
    """批量追加物品事件（see 批量用，一次事务）。rows: dict 列表。"""
    if not rows:
        return
    with _lock:
        c = _connect()
        c.executemany(
            "INSERT INTO item_events(item,ts,event,from_place,to_place,cause,seen_by) "
            "VALUES(?,?,?,?,?,?,?)",
            [
                (
                    str(r.get("item", ""))[:60], str(r.get("ts", "")), str(r.get("event", ""))[:20],
                    str(r.get("from_place", ""))[:80], str(r.get("to_place", ""))[:80],
                    str(r.get("cause", ""))[:80], str(r.get("seen_by", ""))[:20],
                )
                for r in rows
            ],
        )
        c.commit()


def item_event_rows(item=None, limit=500):
    """物品事件流水（新→旧）。"""
    with _lock:
        c = _connect()
        if item:
            cur = c.execute(
                "SELECT * FROM item_events WHERE item=? ORDER BY ts DESC, id DESC LIMIT ?",
                (str(item)[:60], int(limit)),
            )
        else:
            cur = c.execute(
                "SELECT * FROM item_events ORDER BY ts DESC, id DESC LIMIT ?", (int(limit),)
            )
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in rows]


def item_position_at(item, ts):
    """ts 时刻该物品的位置（事件溯源投影）：最后一次决定位置的
    move/give/see/find 事件；lost 表示那时已丢失（known=False）。"""
    with _lock:
        c = _connect()
        cur = c.execute(
            "SELECT * FROM item_events WHERE item=? AND ts<=? "
            "AND event IN ('move','give','see','find','lost') "
            "ORDER BY ts DESC, id DESC LIMIT 1",
            (str(item)[:60], str(ts)),
        )
        rows = cur.fetchall()
        if not rows:
            return None
        cols = [d[0] for d in cur.description]
        r = dict(zip(cols, rows[0]))
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


def item_events_prune(days=90) -> int:
    """清理超过保留期的物品事件（默认 90 天）。"""
    cutoff = (datetime.now() - timedelta(days=int(days))).isoformat(timespec="seconds")
    with _lock:
        c = _connect()
        cur = c.execute("DELETE FROM item_events WHERE ts < ?", (cutoff,))
        c.commit()
        return cur.rowcount


# ===== 程序记忆（System 1 习惯表）=====
def procedure_upsert(situation, action, success, updated_at=""):
    """记录一次"情境→动作"的结果（成功率 = 累计平均）。"""
    situation = str(situation or "")[:120]
    action = str(action or "")[:400]
    if not situation or not action:
        return
    success = 1.0 if float(success) >= 0.5 else 0.0
    with _lock:
        c = _connect()
        row = c.execute(
            "SELECT success, tries FROM procedures WHERE situation=? AND action=?",
            (situation, action),
        ).fetchone()
        if row:
            tries = int(row[1]) + 1
            # 指数滑动平均（EMA）：旧样本衰减，新反馈能真正改变成功率（学得动）
            alpha = 0.3
            s = round((1.0 - alpha) * float(row[0]) + alpha * success, 3)
            c.execute(
                "UPDATE procedures SET success=?, tries=?, updated_at=? WHERE situation=? AND action=?",
                (s, tries, str(updated_at), situation, action),
            )
        else:
            c.execute(
                "INSERT INTO procedures(situation,action,success,tries,updated_at) VALUES(?,?,?,1,?)",
                (situation, action, success, str(updated_at)),
            )
        c.commit()


def procedure_rows(min_tries=0, limit=200):
    """程序记忆列表（按成功率排序）。"""
    with _lock:
        cur = _connect().execute(
            "SELECT situation, action, success, tries, updated_at FROM procedures "
            "WHERE tries>=? ORDER BY success DESC, tries DESC LIMIT ?",
            (int(min_tries), int(limit)),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def procedure_clear():
    with _lock:
        c = _connect()
        c.execute("DELETE FROM procedures")
        c.commit()


def invalidation_add(scope, key, fact, reason=""):
    """双轨制一致性：纠错后写入"待重算队列"。"""
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO state_invalidations(scope,key,fact,reason,ts) VALUES(?,?,?,?,?)",
            (str(scope), str(key or ""), str(fact)[:200], str(reason)[:40],
             datetime.now().isoformat(timespec="seconds")),
        )
        c.commit()


def invalidation_rows(limit=100):
    with _lock:
        cur = _connect().execute(
            "SELECT id, scope, key, fact, reason FROM state_invalidations "
            "ORDER BY id ASC LIMIT ?",
            (max(1, int(limit)),),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def invalidation_clear_all():
    with _lock:
        c = _connect()
        c.execute("DELETE FROM state_invalidations")
        c.commit()


def exp_log_add(action, detail="", before=None, after=None, delta=None, regression=False):
    """实验日志：每次改动/评测的基线前后与偏差（回归门禁）。"""
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO experiment_log(ts,action,detail,before,after,delta,regression) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                datetime.now().isoformat(timespec="seconds"),
                str(action)[:60], str(detail)[:200],
                json.dumps(before, ensure_ascii=False)[:400] if before is not None else "",
                json.dumps(after, ensure_ascii=False)[:400] if after is not None else "",
                json.dumps(delta, ensure_ascii=False)[:400] if delta is not None else "",
                1 if regression else 0,
            ),
        )
        c.commit()


def exp_log_rows(limit=50):
    with _lock:
        cur = _connect().execute(
            "SELECT id, ts, action, detail, before, after, delta, regression "
            "FROM experiment_log ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),),
        )
        cols = [d[0] for d in cur.description]
        out = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            for k in ("before", "after", "delta"):
                try:
                    d[k] = json.loads(d[k]) if d[k] else None
                except Exception:
                    pass
            out.append(d)
        return out


# ===== 对话回放五维评分（人工 / LLM 双模式）=====
SCORE_DIMS = ("recall", "precision", "coherence", "consistency", "naturalness")


def scenario_score_add(scenario_id, scope, scores, comment="", mode="manual"):
    """保存一次五维评分（manual=人工，llm=机器分）。"""
    scores = {k: float(scores.get(k, 0)) for k in SCORE_DIMS}
    avg = round(sum(scores.values()) / len(SCORE_DIMS), 2)
    with _lock:
        c = _connect()
        cur = c.execute(
            "INSERT INTO scenario_scores(ts,scenario_id,scope,mode,scores,comment,avg) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                datetime.now().isoformat(timespec="seconds"),
                str(scenario_id or "")[:120],
                str(scope or "")[:120],
                str(mode or "manual")[:20],
                json.dumps(scores, ensure_ascii=False),
                str(comment or "")[:300],
                avg,
            ),
        )
        c.commit()
        return {
            "id": cur.lastrowid,
            "ts": datetime.now().isoformat(timespec="seconds"),
            "scenario_id": str(scenario_id or ""),
            "scope": str(scope or ""),
            "mode": str(mode or "manual")[:20],
            "scores": scores,
            "comment": str(comment or "")[:300],
            "avg": avg,
        }


def scenario_score_rows(limit=100):
    with _lock:
        cur = _connect().execute(
            "SELECT id, ts, scenario_id, scope, mode, scores, comment, avg "
            "FROM scenario_scores ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),),
        )
        cols = [d[0] for d in cur.description]
        out = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            try:
                d["scores"] = json.loads(d["scores"]) if d["scores"] else {}
            except Exception:
                d["scores"] = {}
            out.append(d)
        return out


# ===== LLM token / 成本观测 =====
def llm_cost_add(ts, module="chat", detail="", prompt_tokens=0, completion_tokens=0, chars=0):
    """记录一次 LLM 调用的 token 消耗（按模块/细节归因，供成本页与机制权衡）。"""
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO llm_cost(ts,module,detail,prompt_tokens,completion_tokens,chars) "
            "VALUES(?,?,?,?,?,?)",
            (
                str(ts or datetime.now().isoformat(timespec="seconds")),
                str(module or "chat")[:40],
                str(detail or "")[:200],
                max(0, int(prompt_tokens or 0)),
                max(0, int(completion_tokens or 0)),
                max(0, int(chars or 0)),
            ),
        )
        c.commit()


def llm_cost_summary(days=30) -> dict:
    """按天 / 按模块 / 按检索路径聚合 token 消耗。"""
    cutoff = (datetime.now() - timedelta(days=max(1, int(days)))).isoformat(timespec="seconds")
    with _lock:
        cur = _connect().execute(
            "SELECT ts,module,detail,prompt_tokens,completion_tokens "
            "FROM llm_cost WHERE ts >= ? ORDER BY ts",
            (cutoff,),
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    total = {"calls": len(rows), "prompt": 0, "completion": 0}
    by_day, by_module = {}, {}
    by_path = {}
    for r in rows:
        p, c = int(r["prompt_tokens"]), int(r["completion_tokens"])
        total["prompt"] += p
        total["completion"] += c
        day = str(r["ts"])[:10]
        d = by_day.setdefault(day, {"calls": 0, "prompt": 0, "completion": 0})
        d["calls"] += 1
        d["prompt"] += p
        d["completion"] += c
        m = by_module.setdefault(str(r["module"] or "chat"), {"calls": 0, "prompt": 0, "completion": 0})
        m["calls"] += 1
        m["prompt"] += p
        m["completion"] += c
        # 检索路径：rerank 的 detail 里记录参与路径（lexical/vector/graph/…）
        if str(r["module"]) == "rerank":
            for path in str(r["detail"] or "").split(","):
                path = path.strip()
                if not path:
                    continue
                q = by_path.setdefault(path, {"calls": 0, "prompt": 0, "completion": 0})
                q["calls"] += 1
                q["prompt"] += p
                q["completion"] += c
    return {
        "days": int(days),
        "total": total,
        "by_day": [{"date": k, **v} for k, v in sorted(by_day.items())],
        "by_module": [{"module": k, **v} for k, v in sorted(by_module.items(), key=lambda x: -(x[1]["prompt"] + x[1]["completion"]))],
        "by_path": [{"path": k, **v} for k, v in sorted(by_path.items(), key=lambda x: -(x[1]["prompt"] + x[1]["completion"]))],
    }


# ===== 犹豫层日志（管理台可回看）=====
def hesitation_log_add(ts, scope, kind, action, reason, delay_s=0, monologue=""):
    """记录一次犹豫决策（保留最近 500 条）。"""
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO hesitation_log(ts,scope,kind,action,reason,delay_s,monologue) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                str(ts or datetime.now().isoformat(timespec="seconds")),
                str(scope or "")[:80], str(kind or "")[:30],
                str(action or "")[:20], str(reason or "")[:40],
                max(0, int(delay_s or 0)), str(monologue or "")[:120],
            ),
        )
        c.execute(
            "DELETE FROM hesitation_log WHERE id NOT IN "
            "(SELECT id FROM hesitation_log ORDER BY id DESC LIMIT 500)"
        )
        c.commit()


def hesitation_log_rows(limit=50):
    with _lock:
        cur = _connect().execute(
            "SELECT id,ts,scope,kind,action,reason,delay_s,monologue "
            "FROM hesitation_log ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# ===== 空间/心智状态表（P0 数据模型优化：kv JSON → 正规表，带一次性迁移）=====
_MIGRATED = {"space_state": False, "item_activation": False, "item_search": False,
             "mind_intention": False, "space_events": False, "ai_actions": False}


def space_state_get():
    """家内房间状态（单行）。"""
    with _lock:
        if not _MIGRATED["space_state"]:
            _MIGRATED["space_state"] = True
            old = kv_get("memory", "space_room")
            if old:
                space_state_set(old)
                c = _connect()
                c.execute("DELETE FROM kv WHERE namespace='memory' AND key='space_room'")
                c.commit()
        row = _connect().execute("SELECT * FROM space_state WHERE id=1").fetchone()
        if row is None:
            return None
        d = dict(row)
        try:
            d["path"] = json.loads(d.get("path") or "[]")
        except Exception as e:
            _stats_err(e)
            d["path"] = []
        return d


def space_state_set(st):
    st = st or {}
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO space_state(id,room,state,from_room,to_room,path,depart_ts,arrive_ts,updated_ts) "
            "VALUES(1,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET room=excluded.room,state=excluded.state,"
            "from_room=excluded.from_room,to_room=excluded.to_room,path=excluded.path,"
            "depart_ts=excluded.depart_ts,arrive_ts=excluded.arrive_ts,updated_ts=excluded.updated_ts",
            (
                str(st.get("room", "客厅")), str(st.get("state", "在场")),
                str(st.get("from", "")), str(st.get("to", "")),
                json.dumps(st.get("path", []), ensure_ascii=False),
                str(st.get("depart_ts", "")), str(st.get("arrive_ts", "")),
                str(st.get("updated_ts", "")),
            ),
        )
        c.commit()


def item_activation_rows():
    """物品激活全量 {item: {seen_ts, count}}。"""
    with _lock:
        if not _MIGRATED["item_activation"]:
            _MIGRATED["item_activation"] = True
            old = kv_get("memory", "item_activation")
            if old:
                item_activation_set(old)
                c = _connect()
                c.execute("DELETE FROM kv WHERE namespace='memory' AND key='item_activation'")
                c.commit()
        rows = _connect().execute(
            "SELECT item, seen_ts, count FROM item_activation_state"
        ).fetchall()
        return {r["item"]: {"seen_ts": r["seen_ts"], "count": r["count"]} for r in rows}


def item_activation_set(items):
    """批量 upsert 物品激活。"""
    if not items:
        return
    with _lock:
        c = _connect()
        c.executemany(
            "INSERT INTO item_activation_state(item,seen_ts,count) VALUES(?,?,?) "
            "ON CONFLICT(item) DO UPDATE SET seen_ts=excluded.seen_ts,count=excluded.count",
            [(str(k)[:60], str(v.get("seen_ts", "")), int(v.get("count", 0))) for k, v in items.items()],
        )
        c.commit()


def item_search_rows():
    with _lock:
        if not _MIGRATED["item_search"]:
            _MIGRATED["item_search"] = True
            old = kv_get("memory", "item_search")
            if old:
                for scope, d in old.items():
                    item_search_set(scope, d)
                c = _connect()
                c.execute("DELETE FROM kv WHERE namespace='memory' AND key='item_search'")
                c.commit()
        rows = _connect().execute(
            "SELECT scope, name, queue, step, started_at FROM item_search_state"
        ).fetchall()
        out = {}
        for r in rows:
            try:
                q = json.loads(r["queue"] or "[]")
            except Exception as e:
                _stats_err(e)
                q = []
            out[r["scope"]] = {
                "name": r["name"], "queue": q, "step": r["step"],
                "started_at": r["started_at"],
            }
        return out


def item_search_set(scope, data):
    data = data or {}
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO item_search_state(scope,name,queue,step,started_at,updated_ts) "
            "VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(scope) DO UPDATE SET name=excluded.name,queue=excluded.queue,"
            "step=excluded.step,started_at=excluded.started_at,updated_ts=excluded.updated_ts",
            (
                str(scope), str(data.get("name", "")),
                json.dumps(data.get("queue", []), ensure_ascii=False),
                int(data.get("step", 0)), str(data.get("started_at", "")),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        c.commit()


def item_search_delete(scope):
    with _lock:
        c = _connect()
        c.execute("DELETE FROM item_search_state WHERE scope=?", (str(scope),))
        c.commit()


def mind_intention_rows():
    with _lock:
        if not _MIGRATED["mind_intention"]:
            _MIGRATED["mind_intention"] = True
            old = kv_get("memory", "mind_intention")
            if old:
                for scope, d in old.items():
                    mind_intention_set(scope, d)
                c = _connect()
                c.execute("DELETE FROM kv WHERE namespace='memory' AND key='mind_intention'")
                c.commit()
        rows = _connect().execute(
            "SELECT scope,title,source,strength,state,due,condition,started_at,updated_at "
            "FROM mind_intention_state"
        ).fetchall()
        return {r["scope"]: dict(r) for r in rows}


def mind_intention_set(scope, data):
    data = data or {}
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO mind_intention_state("
            "scope,title,source,strength,state,due,condition,started_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(scope) DO UPDATE SET title=excluded.title,source=excluded.source,"
            "strength=excluded.strength,state=excluded.state,due=excluded.due,"
            "condition=excluded.condition,started_at=excluded.started_at,updated_at=excluded.updated_at",
            (
                str(scope), str(data.get("title", "")), str(data.get("source", "")),
                float(data.get("strength", 0.0)), str(data.get("state", "committed")),
                str(data.get("due", "")), str(data.get("condition", ""))[:120],
                str(data.get("started_at", "")), str(data.get("updated_at", "")),
            ),
        )
        c.commit()


def mind_intention_delete(scope):
    with _lock:
        c = _connect()
        c.execute("DELETE FROM mind_intention_state WHERE scope=?", (str(scope),))
        c.commit()


def space_event_add(ts, kind, detail, memorable=False):
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO space_events_state(ts,kind,detail,memorable) VALUES(?,?,?,?)",
            (str(ts), str(kind)[:20], str(detail)[:80], 1 if memorable else 0),
        )
        c.commit()


def space_event_rows(limit=200):
    """空间事件（新→旧）。"""
    with _lock:
        if not _MIGRATED["space_events"]:
            _MIGRATED["space_events"] = True
            old = kv_get("memory", "space_events")
            if old and old.get("rows"):
                for r in old["rows"][-200:]:
                    space_event_add(r.get("ts", ""), r.get("kind", ""), r.get("detail", ""), r.get("memorable", False))
                c = _connect()
                c.execute("DELETE FROM kv WHERE namespace='memory' AND key='space_events'")
                c.commit()
        rows = _connect().execute(
            "SELECT id, ts, kind, detail, memorable FROM space_events_state "
            "ORDER BY ts DESC, id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["memorable"] = bool(d.get("memorable"))
            out.append(d)
        return out


def space_event_prune(days=7) -> int:
    cutoff = (datetime.now() - timedelta(days=int(days))).isoformat(timespec="seconds")
    with _lock:
        c = _connect()
        cur = c.execute("DELETE FROM space_events_state WHERE ts < ?", (cutoff,))
        c.commit()
        return cur.rowcount


def ai_action_add(ts, scope, action, detail=""):
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO ai_actions_state(ts,scope,action,detail) VALUES(?,?,?,?)",
            (str(ts), str(scope)[:40], str(action)[:40], str(detail)[:80]),
        )
        c.commit()


def ai_action_rows(scope="", limit=60):
    with _lock:
        if not _MIGRATED["ai_actions"]:
            _MIGRATED["ai_actions"] = True
            old = kv_get("memory", "ai_actions")
            if old and old.get("items"):
                for r in old["items"][-60:]:
                    ai_action_add(r.get("ts", ""), r.get("scope", ""), r.get("action", ""), r.get("detail", ""))
                c = _connect()
                c.execute("DELETE FROM kv WHERE namespace='memory' AND key='ai_actions'")
                c.commit()
        sql = "SELECT ts, scope, action, detail FROM ai_actions_state"
        params = []
        if scope:
            sql += " WHERE scope=?"
            params.append(scope)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        return [dict(r) for r in _connect().execute(sql, params).fetchall()]


# ===== 绑定 =====
def binding_set(uid, gid, mid):
    with _lock:
        c = _connect()
        c.execute(
            "INSERT OR REPLACE INTO bindings(user_openid,group_id,member_openid) VALUES(?,?,?)",
            (uid, gid, mid),
        )
        c.commit()


def binding_delete_user_group(uid, gid):
    with _lock:
        c = _connect()
        c.execute("DELETE FROM bindings WHERE user_openid=? AND group_id=?", (uid, gid))
        c.commit()


def binding_delete_member(gid, mid):
    with _lock:
        c = _connect()
        c.execute("DELETE FROM bindings WHERE group_id=? AND member_openid=?", (gid, mid))
        c.commit()


def binding_find_user_for_member(gid, mid):
    with _lock:
        row = _connect().execute(
            "SELECT user_openid FROM bindings WHERE group_id=? AND member_openid=?",
            (gid, mid),
        ).fetchone()
        return row[0] if row else None


def binding_groups_for_user(uid):
    with _lock:
        rows = _connect().execute(
            "SELECT group_id, member_openid FROM bindings WHERE user_openid=?", (uid,)
        ).fetchall()
        return {r[0]: r[1] for r in rows}


def bindings_all():
    with _lock:
        rows = _connect().execute("SELECT * FROM bindings").fetchall()
        result = {}
        for r in rows:
            result.setdefault(r["user_openid"], {})[r["group_id"]] = r["member_openid"]
        return result


# ===== 分数 =====
def score_add(player, game, delta=1):
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO scores(player,game,score) VALUES(?,?,?) "
            "ON CONFLICT(player,game) DO UPDATE SET score=score+?",
            (player, game, delta, delta),
        )
        c.commit()


def scores_all():
    with _lock:
        rows = _connect().execute("SELECT * FROM scores").fetchall()
        result = {}
        for r in rows:
            result.setdefault(r["player"], {})[r["game"]] = r["score"]
        return result


# ===== 昵称 =====
def nickname_set(player, nickname):
    with _lock:
        c = _connect()
        c.execute(
            "INSERT OR REPLACE INTO nicknames(player,nickname) VALUES(?,?)",
            (player, nickname),
        )
        c.commit()


def nickname_get(player):
    with _lock:
        row = _connect().execute(
            "SELECT nickname FROM nicknames WHERE player=?", (player,)
        ).fetchone()
        return row[0] if row else None


# ===== 状态 =====
def state_get():
    with _lock:
        rows = _connect().execute("SELECT k,v FROM state").fetchall()
        result = {}
        for r in rows:
            try:
                result[r["k"]] = json.loads(r["v"])
            except Exception as e:
                _stats_err(e)
                result[r["k"]] = r["v"]
        return result


def state_set(data):
    with _lock:
        c = _connect()
        c.execute("DELETE FROM state")
        for k, v in data.items():
            c.execute("INSERT INTO state(k,v) VALUES(?,?)", (k, json.dumps(v, ensure_ascii=False)))
        c.commit()


# ===== 审计 =====
def audit_add(action, target="", detail="", operator=""):
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO audit(ts,action,target,detail,operator) VALUES(?,?,?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), action, target, detail, operator),
        )
        c.execute(
            "DELETE FROM audit WHERE id NOT IN "
            "(SELECT id FROM audit ORDER BY id DESC LIMIT ?)",
            (AUDIT_MAX,),
        )
        c.commit()


def audit_query(limit=50, action=None):
    with _lock:
        sql = "SELECT ts,action,target,detail,operator FROM audit"
        params = []
        if action:
            sql += " WHERE action=?"
            params.append(action)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        rows = _connect().execute(sql, params).fetchall()
        return [dict(r) for r in rows]


# ===== 通知队列（App/MCP 触发，QQ 播报插件消费）=====
def notif_add(target_type, target, content, scheduled_at=""):
    """入通知队列；scheduled_at（ISO，可空=立即）用于犹豫层延迟发送。"""
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO notifications(target_type,target,content,created_at,scheduled_at) VALUES(?,?,?,?,?)",
            (
                target_type, target, str(content)[:500],
                datetime.now().isoformat(timespec="seconds"),
                str(scheduled_at or "")[:30],
            ),
        )
        c.commit()


def notif_pending(limit=20):
    with _lock:
        rows = _connect().execute(
            "SELECT id,target_type,target,content FROM notifications "
            "WHERE sent_at IS NULL AND failed=0 "
            "AND (scheduled_at IS NULL OR scheduled_at='' OR scheduled_at<=?) "
            "ORDER BY id LIMIT ?",
            (datetime.now().isoformat(timespec="seconds"), limit),
        ).fetchall()
        return [dict(r) for r in rows]


def notif_mark_sent(nid):
    with _lock:
        c = _connect()
        c.execute(
            "UPDATE notifications SET sent_at=? WHERE id=?",
            (datetime.now().isoformat(timespec="seconds"), nid),
        )
        c.commit()


def notif_mark_failed(nid, max_retries=3):
    """发送失败计数；达到上限后标记 failed（不再重试，避免堵队列）。"""
    with _lock:
        c = _connect()
        c.execute("UPDATE notifications SET retries=retries+1 WHERE id=?", (nid,))
        c.execute(
            "UPDATE notifications SET failed=1 WHERE id=? AND retries>=?",
            (nid, max_retries),
        )
        c.commit()


def notif_failed_retries(nid):
    with _lock:
        row = _connect().execute(
            "SELECT retries FROM notifications WHERE id=?", (nid,)
        ).fetchone()
        return row[0] if row else 0


# ===== 统一记忆库（QQ bot 与 Hermes 共用，向量化就绪）=====
def vec_dumps(vec):
    return json.dumps(vec, ensure_ascii=False) if vec else None


def vec_loads(raw):
    try:
        return json.loads(raw) if raw else None
    except Exception as e:
        _stats_err(e)
        return None


def memory_replace(
    scope,
    key,
    facts,
    updated_at="",
    embeddings=None,
    confidences=None,
    sources=None,
    audience="",
    speaker="",
    mclass="short",
    arousal=0.0,
    valence=0.0,
    privacy=0.0,
    audiences=None,
    speakers=None,
    mclasses=None,
    arousals=None,
    valences=None,
    privacies=None,
    valid_from="",
    valid_to="",
    status="active",
):
    with _lock:
        c = _connect()
        existing_rows = {r["fact"]: r for r in memory_rows(scope, key)}
        c.execute("DELETE FROM memories WHERE scope=? AND key=?", (scope, key))
        emb = embeddings or {}
        conf = confidences or {}
        srcs = sources or {}
        aud_map = audiences or {}
        spk_map = speakers or {}
        cls_map = mclasses or {}
        ar_map = arousals or {}
        va_map = valences or {}
        pr_map = privacies or {}
        st_map = {
            f: existing_rows[f].get("status", "active")
            for f in existing_rows
        }
        vf_map = {f: existing_rows[f].get("valid_from", "") for f in existing_rows}
        vt_map = {f: existing_rows[f].get("valid_to", "") for f in existing_rows}
        for fact in facts:
            c.execute(
                "INSERT OR IGNORE INTO memories("
                "scope,key,fact,embedding,updated_at,confidence,source,audience,speaker,mclass,arousal,valence,privacy,"
                "valid_from,valid_to,status) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    scope,
                    key,
                    str(fact),
                    vec_dumps(emb.get(str(fact))),
                    updated_at,
                    float(conf.get(str(fact), 0.7)),
                    str(srcs.get(str(fact), "")),
                    str(aud_map.get(str(fact), audience)),
                    str(spk_map.get(str(fact), speaker)),
                    str(cls_map.get(str(fact), mclass)),
                    float(ar_map.get(str(fact), arousal)),
                    float(va_map.get(str(fact), valence)),
                    float(pr_map.get(str(fact), privacy)),
                    str(vf_map.get(str(fact), valid_from or updated_at)),
                    str(vt_map.get(str(fact), valid_to or "")),
                    str(st_map.get(str(fact), status)) or "active",
                ),
            )
        c.commit()


def memory_replace_preserve(scope, key, facts, updated_at=""):
    """整段重写时保留每条记忆原有字段（向量/可信度/来源/audience/mclass/情绪），
    只更新事实列表与时间戳。用于群/成员/用户记忆的节流重写。"""
    existing = {r["fact"]: r for r in memory_rows(scope, key)}
    with _lock:
        c = _connect()
        c.execute("DELETE FROM memories WHERE scope=? AND key=?", (scope, key))
        for f in facts:
            r = existing.get(str(f), {})
            c.execute(
                "INSERT OR IGNORE INTO memories("
                "scope,key,fact,embedding,updated_at,confidence,source,audience,speaker,mclass,arousal,valence,privacy) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    scope,
                    key,
                    str(f),
                    r.get("embedding"),
                    updated_at,
                    float(r.get("confidence", 0.7)),
                    r.get("source", ""),
                    r.get("audience", ""),
                    r.get("speaker", ""),
                    r.get("mclass") or "short",
                    float(r.get("arousal", 0.0)),
                    float(r.get("valence", 0.0)),
                    float(r.get("privacy", 0.0)),
                ),
            )
        c.commit()


def memory_update_embedding(scope, key, fact, embedding):
    """只更新单条记忆的向量（回填用，不碰其他字段）。"""
    with _lock:
        c = _connect()
        c.execute(
            "UPDATE memories SET embedding=? WHERE scope=? AND key=? AND fact=?",
            (vec_dumps(embedding), scope, key or "", str(fact)),
        )
        c.commit()


def memory_add(
    scope,
    key,
    fact,
    updated_at="",
    embedding=None,
    confidence=0.7,
    source="",
    audience="",
    speaker="",
    mclass="short",
    arousal=0.0,
    valence=0.0,
    privacy=0.0,
    valid_from="",
    valid_to="",
    status="active",
):
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO memories("
            "scope,key,fact,embedding,updated_at,confidence,source,audience,speaker,mclass,arousal,valence,privacy,"
            "valid_from,valid_to,status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(scope,key,fact) DO UPDATE SET "
            "embedding=COALESCE(excluded.embedding, memories.embedding), "
            "updated_at=excluded.updated_at, confidence=excluded.confidence, source=excluded.source, "
            "audience=excluded.audience, speaker=excluded.speaker, mclass=excluded.mclass, "
            "arousal=excluded.arousal, valence=excluded.valence, privacy=excluded.privacy, "
            "valid_from=excluded.valid_from, valid_to=excluded.valid_to, status=excluded.status",
            (
                scope,
                key,
                str(fact),
                vec_dumps(embedding),
                updated_at,
                float(confidence),
                str(source),
                str(audience),
                str(speaker),
                str(mclass),
                float(arousal),
                float(valence),
                float(privacy),
                str(valid_from or updated_at),
                str(valid_to or ""),
                str(status) or "active",
            ),
        )
        c.commit()


def memory_get(scope, key=""):
    with _lock:
        rows = _connect().execute(
            "SELECT fact FROM memories WHERE scope=? AND key=? ORDER BY updated_at, rowid",
            (scope, key),
        ).fetchall()
        return [r[0] for r in rows]


def memory_updated_at(scope, key=""):
    with _lock:
        row = _connect().execute(
            "SELECT MAX(updated_at) FROM memories WHERE scope=? AND key=?", (scope, key)
        ).fetchone()
        return row[0] or ""


def memory_clear(scope, key=None):
    with _lock:
        c = _connect()
        if key is None:
            c.execute("DELETE FROM memories WHERE scope=?", (scope,))
        else:
            c.execute("DELETE FROM memories WHERE scope=? AND key=?", (scope, key))
        c.commit()


def memory_search(q, scope=None, key=None, limit=10):
    with _lock:
        q = str(q or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        sql = "SELECT scope,key,fact,embedding,updated_at,confidence FROM memories WHERE fact LIKE ? ESCAPE '\\'"
        params = [f"%{q}%"]
        if scope:
            sql += " AND scope=?"
            params.append(scope)
        if key is not None:
            sql += " AND key=?"
            params.append(key)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        return [dict(r) for r in _connect().execute(sql, params).fetchall()]


def memory_rows(scope=None, key=None, exclude_status=None, limit=None):
    """带 embedding 的行（供向量检索）；exclude_status/limit 可下推到 SQL 减扫描。"""
    with _lock:
        sql = (
            "SELECT scope,key,fact,embedding,updated_at,confidence,source,audience,speaker,mclass,"
            "arousal,valence,privacy,valid_from,valid_to,status FROM memories"
        )
        params = []
        if scope:
            sql += " WHERE scope=?"
            params.append(scope)
        if key is not None:
            sql += " AND key=?" if "WHERE" in sql else " WHERE key=?"
            params.append(key)
        if exclude_status:
            if "WHERE" not in sql:
                sql += " WHERE 1=1"
            sql += " AND status NOT IN (" + ",".join("?" * len(exclude_status)) + ")"
            params.extend(exclude_status)
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(1, int(limit)))
        return [dict(r) for r in _connect().execute(sql, params).fetchall()]


def memory_set_status(scope, key, fact, status, valid_to=""):
    """时间推理（v5）：把记忆标记为 superseded/history 等状态。"""
    with _lock:
        c = _connect()
        c.execute(
            "UPDATE memories SET status=?, valid_to=? WHERE scope=? AND key=? AND fact=?",
            (str(status)[:20], str(valid_to or ""), scope, key or "", str(fact)),
        )
        c.commit()


def memory_set_confidence(scope, key, fact, confidence):
    """调整单条记忆的可信度（确认上调 / 反驳下调）。"""
    with _lock:
        c = _connect()
        c.execute(
            "UPDATE memories SET confidence=? WHERE scope=? AND key=? AND fact=?",
            (
                min(1.0, max(0.0, float(confidence))),
                scope,
                key or "",
                str(fact),
            ),
        )
        c.commit()


def memory_source_normalize() -> dict:
    """证据门控（v2.3）：历史 source 归一——ingest:* → user（用户亲口说），persona → pack（人设设定）。
    幂等，可随 memory-grow 自动跑。"""
    with _lock:
        c = _connect()
        cur = c.execute(
            "UPDATE memories SET source='user' WHERE source LIKE 'ingest:%' OR source='ingest'"
        )
        n1 = cur.rowcount
        cur = c.execute("UPDATE memories SET source='pack' WHERE source='persona'")
        n2 = cur.rowcount
        c.commit()
        return {"ingest_to_user": n1, "persona_to_pack": n2}


def memory_delete(scope, key, fact):
    """单条记忆删除（回收策略用）。"""
    with _lock:
        c = _connect()
        c.execute(
            "DELETE FROM memories WHERE scope=? AND key=? AND fact=?",
            (scope, key or "", str(fact)),
        )
        c.commit()


# ===== 事件图（events + event_relations 邻接表）=====
def event_add(
    scope,
    key,
    etype,
    title,
    content="",
    importance=0.5,
    ts="",
    ts_source="approx",
    embedding=None,
    updated_at="",
    memory_scope="",
    memory_key="",
    memory_fact="",
):
    """写入/更新一个事件（按 scope+key+title 去重，保留 id 以不破坏关系图）。返回 event id。"""
    title = str(title)[:200]
    key = key or ""
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO events(scope,key,etype,title,content,importance,ts,ts_source,embedding,updated_at,"
            "memory_scope,memory_key,memory_fact) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(scope,key,title) DO UPDATE SET "
            "etype=excluded.etype, content=excluded.content, importance=excluded.importance, "
            "ts=excluded.ts, ts_source=excluded.ts_source, embedding=excluded.embedding, "
            "updated_at=excluded.updated_at, "
            "memory_scope=excluded.memory_scope, memory_key=excluded.memory_key, memory_fact=excluded.memory_fact",
            (
                scope,
                key,
                str(etype)[:50],
                title,
                str(content)[:1000],
                float(importance),
                str(ts),
                str(ts_source)[:20] or "approx",
                vec_dumps(embedding),
                updated_at or datetime.now().isoformat(timespec="seconds"),
                str(memory_scope),
                str(memory_key),
                str(memory_fact)[:200],
            ),
        )
        c.commit()
        row = c.execute(
            "SELECT id FROM events WHERE scope=? AND key=? AND title=?",
            (scope, key, title),
        ).fetchone()
        return row[0] if row else None


def event_set_ts(event_id, ts, ts_source="explicit"):
    """纠正/精化事件时间（保持事件图不变）。"""
    with _lock:
        c = _connect()
        c.execute(
            "UPDATE events SET ts=?, ts_source=?, updated_at=? WHERE id=?",
            (str(ts), str(ts_source)[:20] or "approx",
             datetime.now().isoformat(timespec="seconds"), int(event_id)),
        )
        c.commit()


def event_set_ts_by_title(scope, key, title, ts, ts_source="explicit"):
    """按 scope+key+title 更新事件时间（纠错联动用）。"""
    with _lock:
        c = _connect()
        c.execute(
            "UPDATE events SET ts=?, ts_source=?, updated_at=? WHERE scope=? AND key=? AND title=?",
            (str(ts), str(ts_source)[:20] or "approx",
             datetime.now().isoformat(timespec="seconds"), str(scope), str(key or ""), str(title)),
        )
        c.commit()


def event_rows(scope=None, key=None, since=None, min_importance=None, limit=None):
    with _lock:
        sql = "SELECT * FROM events"
        params = []
        conds = []
        if scope:
            conds.append("scope=?")
            params.append(scope)
        if key is not None:
            conds.append("key=?")
            params.append(key)
        if since:
            conds.append("ts>=?")
            params.append(since)
        if min_importance is not None:
            conds.append("importance>=?")
            params.append(min_importance)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY importance DESC, id DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [dict(r) for r in _connect().execute(sql, params).fetchall()]


def event_delete(event_id):
    """删除事件及其关联边（当前 PRAGMA foreign_keys 未开启，手动删边）。"""
    with _lock:
        c = _connect()
        c.execute("DELETE FROM event_relations WHERE src=? OR dst=?", (event_id, event_id))
        c.execute("DELETE FROM events WHERE id=?", (event_id,))
        c.commit()


def relation_add(src, dst, rel="influences", weight=1.0):
    if not src or not dst or src == dst:
        return
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO event_relations(src,dst,rel,weight,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(src,dst,rel) DO UPDATE SET weight=excluded.weight, updated_at=excluded.updated_at",
            (int(src), int(dst), str(rel)[:50], float(weight), datetime.now().isoformat(timespec="seconds")),
        )
        c.commit()


def relations_for(event_ids, direction="both"):
    """查询事件的关联边。direction: out / in / both。"""
    if not event_ids:
        return []
    marks = ",".join("?" * len(event_ids))
    params = list(event_ids)
    if direction == "out":
        sql = f"SELECT * FROM event_relations WHERE src IN ({marks})"
    elif direction == "in":
        sql = f"SELECT * FROM event_relations WHERE dst IN ({marks})"
    else:
        sql = f"SELECT * FROM event_relations WHERE src IN ({marks}) OR dst IN ({marks})"
        params = params + params
    with _lock:
        return [dict(r) for r in _connect().execute(sql, params).fetchall()]


def event_id_by_title(scope, key, title):
    with _lock:
        row = _connect().execute(
            "SELECT id FROM events WHERE scope=? AND key=? AND title=?",
            (scope, key or "", str(title)[:200]),
        ).fetchone()
        return row[0] if row else None


# ===== AI 自身记忆（identity / experience / belief）=====
def ai_memory_set(kind, content, importance=0.5, embedding=None, updated_at=""):
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO ai_memory(kind,content,importance,embedding,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(kind,content) DO UPDATE SET "
            "importance=excluded.importance, embedding=excluded.embedding, updated_at=excluded.updated_at",
            (kind, str(content)[:1000], float(importance), vec_dumps(embedding), updated_at or datetime.now().isoformat(timespec="seconds")),
        )
        c.commit()


def ai_memory_rows(kind=None, limit=None):
    with _lock:
        sql = "SELECT kind,content,importance,embedding,updated_at FROM ai_memory"
        params = []
        if kind:
            sql += " WHERE kind=?"
            params.append(kind)
        sql += " ORDER BY importance DESC, updated_at DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [dict(r) for r in _connect().execute(sql, params).fetchall()]


def ai_memory_clear(kind=None):
    with _lock:
        c = _connect()
        if kind:
            c.execute("DELETE FROM ai_memory WHERE kind=?", (kind,))
        else:
            c.execute("DELETE FROM ai_memory")
        c.commit()


# ===== 记忆元数据（Memory Policy 的原始数据）=====
def meta_touch(scope, key, fact, importance=0.5, ts=""):
    """被提取/注入时更新访问计数和最后访问时间；importance 只增不减（简单隐式反馈）。"""
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO memory_meta(scope,key,fact,access_count,last_access,importance) VALUES(?,?,?,1,?,?) "
            "ON CONFLICT(scope,key,fact) DO UPDATE SET "
            "access_count=access_count+1, last_access=excluded.last_access, "
            "importance=MAX(importance,excluded.importance)",
            (scope, key or "", str(fact), ts or datetime.now().isoformat(timespec="seconds"), float(importance)),
        )
        c.commit()


def meta_rows(scope=None, key=None, min_importance=None, limit=None):
    with _lock:
        sql = "SELECT scope,key,fact,access_count,last_access,importance FROM memory_meta"
        params = []
        conds = []
        if scope:
            conds.append("scope=?")
            params.append(scope)
        if key is not None:
            conds.append("key=?")
            params.append(key)
        if min_importance is not None:
            conds.append("importance>=?")
            params.append(min_importance)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY importance DESC, access_count DESC, last_access DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [dict(r) for r in _connect().execute(sql, params).fetchall()]


def meta_delete(scope, key, fact):
    with _lock:
        c = _connect()
        c.execute(
            "DELETE FROM memory_meta WHERE scope=? AND key=? AND fact=?",
            (scope, key or "", str(fact)),
        )
        c.commit()


# ===== 议题（大类 → 议题 → 参数）=====
def topic_add(scope, key, category, topic, importance=0.5, confidence=0.7, status="active",
              started_at="", updated_at=""):
    with _lock:
        c = _connect()
        now = updated_at or datetime.now().isoformat(timespec="seconds")
        c.execute(
            "INSERT INTO topics(scope,key,category,topic,importance,confidence,status,started_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(scope,key,category,topic) DO UPDATE SET "
            "importance=excluded.importance, confidence=excluded.confidence, updated_at=excluded.updated_at",
            (scope, key or "", str(category)[:50], str(topic)[:100], float(importance),
             float(confidence), str(status)[:20], started_at or now, now),
        )
        c.commit()
        row = c.execute(
            "SELECT id FROM topics WHERE scope=? AND key=? AND category=? AND topic=?",
            (scope, key or "", str(category)[:50], str(topic)[:100]),
        ).fetchone()
        return row[0] if row else None


def topic_find(scope, key, category, topic):
    with _lock:
        row = _connect().execute(
            "SELECT id FROM topics WHERE scope=? AND key=? AND category=? AND topic=?",
            (scope, key or "", str(category)[:50], str(topic)[:100]),
        ).fetchone()
        return row[0] if row else None


def topic_get(topic_id):
    with _lock:
        row = _connect().execute(
            "SELECT * FROM topics WHERE id=?", (int(topic_id),)
        ).fetchone()
        return dict(row) if row else None


def topic_rows(scope=None, key=None, category=None, limit=None):
    with _lock:
        sql = "SELECT * FROM topics"
        params = []
        conds = []
        if scope:
            conds.append("scope=?")
            params.append(scope)
        if key is not None:
            conds.append("key=?")
            params.append(key)
        if category:
            conds.append("category=?")
            params.append(category)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY importance DESC, updated_at DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [dict(r) for r in _connect().execute(sql, params).fetchall()]


def topics_count(scope=None) -> int:
    with _lock:
        if scope:
            return _connect().execute(
                "SELECT COUNT(*) FROM topics WHERE scope=?", (scope,)
            ).fetchone()[0]
        return _connect().execute("SELECT COUNT(*) FROM topics").fetchone()[0]


def topic_clear(scope):
    """清空某 scope 的议题及其参数/关联（人物档案重建等场景，避免陈旧事实经议题通道召回）。"""
    with _lock:
        c = _connect()
        ids = [r[0] for r in c.execute(
            "SELECT id FROM topics WHERE scope=?", (scope,)
        ).fetchall()]
        if ids:
            marks = ",".join("?" * len(ids))
            c.execute(f"DELETE FROM topic_params WHERE topic_id IN ({marks})", ids)
            c.execute(f"DELETE FROM topics WHERE id IN ({marks})", ids)
        c.commit()


def topic_param_add(topic_id, param, value, confidence=0.7, updated_at=""):
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO topic_params(topic_id,param,value,confidence,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(topic_id,param,value) DO UPDATE SET "
            "confidence=excluded.confidence, updated_at=excluded.updated_at",
            (int(topic_id), str(param)[:30], str(value)[:500], float(confidence),
             updated_at or datetime.now().isoformat(timespec="seconds")),
        )
        c.commit()


def topic_params(topic_id) -> list:
    with _lock:
        rows = _connect().execute(
            "SELECT param,value,confidence,updated_at FROM topic_params WHERE topic_id=?",
            (int(topic_id),),
        ).fetchall()
        return [dict(r) for r in rows]


def event_set_topic(event_id, topic_id):
    with _lock:
        c = _connect()
        c.execute("UPDATE events SET topic_id=? WHERE id=?", (int(topic_id), int(event_id)))
        c.commit()


def topic_param_invalidate(value):
    """纠错联动：含该事实的议题参数降权（标记 stale，供重算）。"""
    with _lock:
        c = _connect()
        c.execute("UPDATE topic_params SET confidence=MIN(confidence, 0.3) WHERE value=?", (str(value),))
        c.commit()


# ===== 自研 IVF 向量索引（SQLite 持久化）=====
def vec_clear():
    with _lock:
        c = _connect()
        c.execute("DELETE FROM vec_index")
        c.execute("DELETE FROM vec_centroids")
        c.commit()


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
        c.commit()


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
        c.commit()


def vec_index_upsert(scope, key, fact, centroid_id, embedding):
    """增量写一条向量索引（同 fact 覆盖；最近质心归属由调用方算好）。"""
    with _lock:
        c = _connect()
        c.execute("DELETE FROM vec_index WHERE scope=? AND key=? AND fact=?", (scope, key, fact))
        c.execute(
            "INSERT INTO vec_index(scope,key,fact,centroid_id,embedding) VALUES(?,?,?,?,?)",
            (scope, key, fact, int(centroid_id), vec_dumps(embedding)),
        )
        c.commit()


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
        c.commit()


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
        c.commit()


def bm25_clear():
    with _lock:
        c = _connect()
        c.execute("DELETE FROM bm25_terms")
        c.execute("DELETE FROM bm25_docs")
        c.commit()


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
        c.commit()


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
        c.commit()


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
        c.commit()


def query_log_prune(days=30) -> int:
    """清理超过保留期的查询日志（默认 30 天），防表无限膨胀。"""
    import datetime as _dt
    cutoff = (_dt.datetime.now() - _dt.timedelta(days=max(1, int(days)))).isoformat(timespec="seconds")
    with _lock:
        c = _connect()
        cur = c.execute("DELETE FROM query_log WHERE ts<?", (cutoff,))
        c.commit()
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
        c.commit()


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
        c.commit()
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
        c.commit()


def session_close_old(days=3):
    with _lock:
        c = _connect()
        before = c.total_changes
        c.execute(
            "UPDATE sessions SET closed=1 WHERE closed=0 AND "
            "updated_at < datetime('now', ?)",
            (f"-{int(days)} days",),
        )
        c.commit()
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
        c.commit()
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
        c.commit()


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
        c.commit()


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


def purge_scope(scope):
    """按用户/场景彻底删除：记忆/属性/事件/议题/索引/日志。"""
    with _lock:
        c = _connect()
        event_ids = [r[0] for r in c.execute("SELECT id FROM events WHERE scope=?", (scope,)).fetchall()]
        topic_ids = [r[0] for r in c.execute("SELECT id FROM topics WHERE scope=?", (scope,)).fetchall()]
        for eid in event_ids:
            c.execute("DELETE FROM event_relations WHERE src=? OR dst=?", (eid, eid))
            c.execute("DELETE FROM entity_events WHERE event_id=?", (eid,))
        for eid in event_ids:
            c.execute("DELETE FROM events WHERE id=?", (eid,))
        for tid in topic_ids:
            c.execute("DELETE FROM topic_params WHERE topic_id=?", (tid,))
        for tid in topic_ids:
            c.execute("DELETE FROM topics WHERE id=?", (tid,))
        c.execute("DELETE FROM memories WHERE scope=?", (scope,))
        c.execute("DELETE FROM memory_meta WHERE scope=?", (scope,))
        c.execute("DELETE FROM memory_attrs WHERE scope=?", (scope,))
        c.execute("DELETE FROM bm25_terms WHERE scope=?", (scope,))
        c.execute("DELETE FROM bm25_docs WHERE scope=?", (scope,))
        c.execute("DELETE FROM memories_fts WHERE scope=?", (scope,))
        c.execute("DELETE FROM vec_index WHERE scope=?", (scope,))
        c.execute("DELETE FROM entities WHERE scope=?", (scope,))
        c.execute("DELETE FROM sessions WHERE scope=?", (scope,))
        c.commit()


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
        c.commit()


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
        c.commit()


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
        c.commit()


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
        c.commit()
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
        c.commit()


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
        c.commit()


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
        c.commit()


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
        c.commit()


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
        c.commit()


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
        c.commit()


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
        c.commit()


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
        c.commit()


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
        c.commit()


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
        c.commit()


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
        c.commit()
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
        c.commit()


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
        c.commit()
    return counts


def backup_to(path):
    """SQLite 在线安全备份（WAL 模式下也一致）。"""
    dst = sqlite3.connect(str(path))
    try:
        with dst:
            _connect().backup(dst)
    finally:
        dst.close()



def _stats_err(e):
    """裸 except 审计（v2.2）：错误计数 + 日志，供消融/排查。"""
    try:
        import memory.stats as _st
        _st.bump_err("_db", e)
    except Exception:
        pass
