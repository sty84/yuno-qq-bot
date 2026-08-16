# -*- coding: utf-8 -*-
"""SQLite 数据层核心：连接、schema、迁移、事务。

由 plugins/_db.py 统一装配；对外仍通过 plugins._db 使用。
"""
import json
import os
import pathlib
import sqlite3
import threading
from contextlib import contextmanager

DB_PATH = None
_conn = None
# RLock：允许同一线程在持锁时触发惰性初始化（_connect→init→_create_tables 也会加锁），
# 避免 memory_rows 等函数首次调用时自锁死。
_lock = threading.RLock()
_migrated = False
AUDIT_MAX = 5000
SCHEMA_VERSION = 1


def set_audit_max(n):
    global AUDIT_MAX
    AUDIT_MAX = max(1, int(n))


_txn_depth = 0


@contextmanager
def transaction():
    """写事务上下文（① ingest 事务化）：事务内 helper 的提交点被 _maybe_commit 挂起，
    统一在出口 COMMIT / 异常 ROLLBACK。嵌套安全（深度计数，外层统一提交）。
    注意：事务体内不要放 LLM/网络调用（会长时间持锁）。"""
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
            c.execute("BEGIN")
            yield
            c.commit()
        except Exception:
            try:
                c.rollback()
            except Exception:
                pass
            raise
        finally:
            _txn_depth = 0


def _maybe_commit():
    """helper 的提交点：在 transaction() 内不提交（由外层统一），避免半成品状态。"""
    if _txn_depth > 0:
        return
    _connect().commit()


def init(data_dir, force=False):
    """初始化数据库连接。force=True 用于测试隔离：重定向到新的临时库。"""
    global DB_PATH, _conn, _migrated
    if DB_PATH is not None and not force:
        return
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
    DB_PATH = pathlib.Path(data_dir) / "bot.db"
    _conn = sqlite3.connect(str(DB_PATH), timeout=10, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    # WAL 模式下 synchronous=NORMAL 安全且大幅降低提交延迟（性能优化）
    _conn.execute("PRAGMA synchronous=NORMAL")
    _create_tables()
    _migrate_schema()
    if force:
        _migrated = False
    if not _migrated:
        _migrate_legacy(pathlib.Path(data_dir))
        _migrated = True
    _migrate_facts_to_memories()
    _migrate_ai_to_unified()


def _schema_version():
    return int(_connect().execute("PRAGMA user_version").fetchone()[0])


def _set_schema_version(v):
    _connect().execute(f"PRAGMA user_version={int(v)}")
    _maybe_commit()


def _migrate_schema():
    """数据库 schema 版本迁移：从 PRAGMA user_version 逐步升级到 SCHEMA_VERSION。
    新表/新索引只增不删，老库可安全升级。"""
    with _lock:
        current = _schema_version()
        if current >= SCHEMA_VERSION:
            return
        migrations = {
            1: [
                # 多用户/多 NPC 基础元数据
                "CREATE TABLE IF NOT EXISTS scope_meta("
                "scope TEXT PRIMARY KEY,"
                "kind TEXT NOT NULL DEFAULT 'user',"
                "agent_id TEXT NOT NULL DEFAULT '',"
                "enabled INTEGER NOT NULL DEFAULT 1,"
                "created_at TEXT,"
                "updated_at TEXT)",
                # 常用检索/治理索引
                "CREATE INDEX IF NOT EXISTS idx_memories_scope_key_fact ON memories(scope,key,fact)",
                "CREATE INDEX IF NOT EXISTS idx_memory_meta_scope_key_fact ON memory_meta(scope,key,fact)",
                "CREATE INDEX IF NOT EXISTS idx_events_scope_title ON events(scope,key,title)",
                "CREATE INDEX IF NOT EXISTS idx_conv_log_scope_ts ON conv_log(scope,ts)",
                "CREATE INDEX IF NOT EXISTS idx_trace_scope_ts ON memory_trace(scope,ts)",
            ],
        }
        for version in range(current + 1, SCHEMA_VERSION + 1):
            for stmt in migrations.get(version, []):
                try:
                    _connect().execute(stmt)
                    _maybe_commit()
                except Exception as e:
                    # 已存在/重复执行不阻断升级
                    print(f"schema migration {version} skip: {e}")
            _set_schema_version(version)


def _connect():
    if _conn is None:
        # 未显式初始化时用默认数据目录。优先取 _shared.DATA_DIR（Persona Pack 隔离目录，
        # 修复：只 import _db 的脚本会连到 CONFIG_PATH.parent/data 的空库而读不到真实数据）。
        try:
            from plugins import _shared
            default = _shared.DATA_DIR
        except Exception:
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
            CREATE TABLE IF NOT EXISTS scope_meta(
                scope TEXT PRIMARY KEY,
                kind TEXT NOT NULL DEFAULT 'user',
                agent_id TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT,
                updated_at TEXT);
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

            CREATE TABLE IF NOT EXISTS skills(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                situation TEXT NOT NULL,
                action TEXT NOT NULL,
                result TEXT NOT NULL DEFAULT '',
                condition TEXT NOT NULL DEFAULT '',
                failure_reason TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                success REAL NOT NULL DEFAULT 0.5,
                tries INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT,
                UNIQUE(situation, action));
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
            CREATE TABLE IF NOT EXISTS conv_log(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL DEFAULT '',
                scope TEXT NOT NULL DEFAULT '',
                ts TEXT NOT NULL DEFAULT '',
                user_text TEXT NOT NULL DEFAULT '',
                ai_text TEXT NOT NULL DEFAULT '');
            CREATE INDEX IF NOT EXISTS idx_conv_scope_ts ON conv_log(scope, ts);
            CREATE TABLE IF NOT EXISTS conv_review(
                conv_id INTEGER NOT NULL,
                score REAL NOT NULL,
                scores TEXT NOT NULL DEFAULT '{}',
                comment TEXT NOT NULL DEFAULT '',
                reviewer TEXT NOT NULL DEFAULT '',
                created_at TEXT,
                UNIQUE(conv_id, reviewer));
            """
        )
        _maybe_commit()
        for col, ddl in (
            ("retries", "INTEGER NOT NULL DEFAULT 0"),
            ("failed", "INTEGER NOT NULL DEFAULT 0"),
        ):
            try:
                c.execute(f"ALTER TABLE notifications ADD COLUMN {col} {ddl}")
                _maybe_commit()
            except sqlite3.OperationalError:
                pass
        try:
            c.execute("ALTER TABLE memories ADD COLUMN confidence REAL NOT NULL DEFAULT 0.7")
            _maybe_commit()
        except sqlite3.OperationalError:
            pass
        try:
            c.execute("ALTER TABLE memories ADD COLUMN source TEXT NOT NULL DEFAULT ''")
            _maybe_commit()
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
                _maybe_commit()
            except sqlite3.OperationalError:
                pass
        try:
            c.execute("ALTER TABLE events ADD COLUMN topic_id INTEGER")
            _maybe_commit()
        except sqlite3.OperationalError:
            pass
        for col, ddl in (
            ("memory_scope", "TEXT"),
            ("memory_key", "TEXT"),
            ("memory_fact", "TEXT"),
        ):
            try:
                c.execute(f"ALTER TABLE events ADD COLUMN {col} {ddl}")
                _maybe_commit()
            except sqlite3.OperationalError:
                pass
        try:
            c.execute("ALTER TABLE events ADD COLUMN ts_source TEXT NOT NULL DEFAULT 'approx'")
            _maybe_commit()
        except sqlite3.OperationalError:
            pass
        try:
            c.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
            _maybe_commit()
        except sqlite3.OperationalError:
            pass
        for col, ddl in (
            ("valid_from", "TEXT"),
            ("valid_to", "TEXT"),
            ("status", "TEXT NOT NULL DEFAULT 'active'"),
        ):
            try:
                c.execute(f"ALTER TABLE memories ADD COLUMN {col} {ddl}")
                _maybe_commit()
            except sqlite3.OperationalError:
                pass
        try:
            c.execute("ALTER TABLE feedback_log ADD COLUMN weight REAL NOT NULL DEFAULT 1.0")
            _maybe_commit()
        except sqlite3.OperationalError:
            pass
        try:
            c.execute("ALTER TABLE trace_review ADD COLUMN scores TEXT NOT NULL DEFAULT '{}'")
            _maybe_commit()
        except sqlite3.OperationalError:
            pass
        try:
            # 犹豫层（v2.3）：通知可延后发送（scheduled_at 之前不发）
            c.execute("ALTER TABLE notifications ADD COLUMN scheduled_at TEXT")
            _maybe_commit()
        except sqlite3.OperationalError:
            pass
        for col, ddl in (
            ("motivation", "TEXT NOT NULL DEFAULT ''"),
            ("confidence", "REAL NOT NULL DEFAULT 0.7"),
            ("current_state", "TEXT NOT NULL DEFAULT '{}'"),
        ):
            try:
                c.execute(f"ALTER TABLE goals ADD COLUMN {col} {ddl}")
                _maybe_commit()
            except sqlite3.OperationalError:
                pass
        try:
            c.execute("ALTER TABLE belief_log ADD COLUMN old_content TEXT NOT NULL DEFAULT ''")
            _maybe_commit()
        except sqlite3.OperationalError:
            pass
        # FTS5 词法索引（trigram 适合中文；不可用时走 LIKE 降级）
        try:
            c.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5("
                "scope UNINDEXED, key UNINDEXED, fact, tokenize='trigram')"
            )
            _maybe_commit()
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
            from plugins._db_sqlite_data import facts_replace, kv_set
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
        _maybe_commit()
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
    _maybe_commit()
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
    _maybe_commit()
    _ai_migrated = True




def _stats_err(e):
    """裸 except 审计（v2.2）：错误计数 + 日志，供消融/排查。"""
    try:
        import memory.stats as _st
        _st.bump_err("_db", e)
    except Exception:
        pass
