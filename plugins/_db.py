"""SQLite 数据层：记忆、绑定、分数、昵称、状态、群列表。

替代散落的 JSON 文件；首次启动会自动把旧 JSON 数据迁移进来。
"""

import json
import os
import pathlib
import sqlite3
import threading

DB_PATH = None
_conn = None
_lock = threading.Lock()
_migrated = False


def init(data_dir):
    global DB_PATH, _conn, _migrated
    if DB_PATH is not None:
        return
    DB_PATH = pathlib.Path(data_dir) / "bot.db"
    _conn = sqlite3.connect(str(DB_PATH), timeout=10, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _create_tables()
    if not _migrated:
        _migrate_legacy(pathlib.Path(data_dir))
        _migrated = True


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
            """
        )
        c.commit()


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
        except Exception:
            return default


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
            except Exception:
                result[r["k"]] = r["v"]
        return result


def state_set(data):
    with _lock:
        c = _connect()
        c.execute("DELETE FROM state")
        for k, v in data.items():
            c.execute("INSERT INTO state(k,v) VALUES(?,?)", (k, json.dumps(v, ensure_ascii=False)))
        c.commit()
