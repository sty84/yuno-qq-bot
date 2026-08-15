#!/usr/bin/env python3
"""在空 PostgreSQL 库中初始化完整 schema（无数据）。

原理：先用 SQLite 建一份空表结构，再通过迁移脚本把 DDL 翻译到 PostgreSQL。
适合 CI / 新环境首次初始化。
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    # 1. 用 SQLite 生成空 schema
    os.environ["YUNO_DB_BACKEND"] = "sqlite"
    from plugins import _db
    tmp = tempfile.mkdtemp(prefix="pg_init_schema_")
    _db.init(tmp, force=True)
    sqlite_path = _db.DB_PATH

    # 2. 切回 PostgreSQL 并迁移空结构
    os.environ["YUNO_DB_BACKEND"] = "postgresql"
    from scripts.migrate_sqlite_to_pg import migrate
    dsn = (
        f"host={os.getenv('YUNO_PG_HOST', '127.0.0.1')} "
        f"port={os.getenv('YUNO_PG_PORT', '5432')} "
        f"dbname={os.getenv('YUNO_PG_DB', 'yuno')} "
        f"user={os.getenv('YUNO_PG_USER', 'esp')} "
        f"password={os.getenv('YUNO_PG_PASSWORD', '')}"
    )
    report = migrate(sqlite_path, dsn)
    print(f"schema initialized: {len(report['tables'])} tables")
    return 0


if __name__ == "__main__":
    sys.exit(main())
