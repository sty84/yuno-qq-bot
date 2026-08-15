# -*- coding: utf-8 -*-
"""内部/测试专用 SQLite 存储。

生产数据（QQ、外部应用）走 PostgreSQL；
内部测试、后台诊断、开发脚本产生的数据写这里，避免污染生产 PG。
"""

import json
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

DB_PATH = None
_conn = None
_lock = threading.RLock()


def default_path() -> Path:
    env = os.getenv("YUNO_INTERNAL_DB", "")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "data" / "internal.db"


def init(path=None):
    global DB_PATH, _conn
    if _conn is not None and not path:
        return
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
    DB_PATH = Path(path) if path else default_path()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute(
        """
        CREATE TABLE IF NOT EXISTS test_runs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            scope TEXT NOT NULL DEFAULT '',
            payload TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
        """
    )
    _conn.commit()


def _connect():
    if _conn is None:
        init()
    return _conn


def record(kind, payload, scope=""):
    """写一条内部/测试记录。"""
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO test_runs(kind,scope,payload,created_at) VALUES(?,?,?,?)",
            (kind, scope, json.dumps(payload, ensure_ascii=False), datetime.now().isoformat(timespec="seconds")),
        )
        c.commit()


def recent(kind=None, limit=100):
    with _lock:
        c = _connect()
        if kind:
            rows = c.execute(
                "SELECT * FROM test_runs WHERE kind=? ORDER BY id DESC LIMIT ?", (kind, limit)
            ).fetchall()
        else:
            rows = c.execute("SELECT * FROM test_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d.get("payload") or "{}")
            except Exception:
                pass
            out.append(d)
        return out


def prune(days=30) -> int:
    """清理超过保留天数的内部/测试记录。"""
    from datetime import datetime, timedelta
    cutoff = (datetime.now() - timedelta(days=max(1, int(days)))).isoformat(timespec="seconds")
    with _lock:
        c = _connect()
        cur = c.execute("DELETE FROM test_runs WHERE created_at < ?", (cutoff,))
        c.commit()
        return cur.rowcount
