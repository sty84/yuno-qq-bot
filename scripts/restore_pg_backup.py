#!/usr/bin/env python3
"""从 pg_dump 自定义格式备份恢复 PostgreSQL。

用法：
  YUNO_PG_PASSWORD=xxx python scripts/restore_pg_backup.py backup.dump
"""

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    if len(sys.argv) < 2:
        print("用法: python scripts/restore_pg_backup.py <backup.dump>")
        return 1
    backup = sys.argv[1]
    password = os.getenv("YUNO_PG_PASSWORD")
    if not password:
        print("请设置 YUNO_PG_PASSWORD")
        return 1
    env = os.environ.copy()
    env["PGPASSWORD"] = password
    subprocess.run(
        [
            "pg_restore",
            "-h", os.getenv("YUNO_PG_HOST", "127.0.0.1"),
            "-p", os.getenv("YUNO_PG_PORT", "5432"),
            "-U", os.getenv("YUNO_PG_USER", "esp"),
            "-d", os.getenv("YUNO_PG_DB", "yuno"),
            "--clean",
            "--if-exists",
            backup,
        ],
        check=True,
        env=env,
    )
    print("恢复完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
