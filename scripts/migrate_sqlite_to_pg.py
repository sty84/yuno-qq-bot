#!/usr/bin/env python3
"""SQLite -> PostgreSQL 数据迁移脚本。

用法：
  python scripts/migrate_sqlite_to_pg.py \
    --sqlite data/bot.db \
    --pg-host 127.0.0.1 --pg-port 5432 --pg-db yuno --pg-user esp --pg-password yuno

默认从环境变量读取：
  YUNO_PG_HOST / YUNO_PG_PORT / YUNO_PG_DB / YUNO_PG_USER / YUNO_PG_PASSWORD
"""

import argparse
import os
import re
import sqlite3
import sys

import psycopg2
from psycopg2.extras import execute_values


def translate_ddl(sql: str) -> str | None:
    """把 SQLite CREATE TABLE 转成 PostgreSQL 可用的 DDL。"""
    if not sql:
        return None
    if sql.startswith("CREATE VIRTUAL TABLE"):
        return None
    # 跳过 FTS 内部影子表
    if "memories_fts" in sql and ("_config" in sql or "_content" in sql or "_data" in sql or "_docsize" in sql or "_idx" in sql):
        return None
    s = sql.strip().rstrip(";")
    # 去掉 SQLite 的 WITHOUT ROWID
    s = re.sub(r"\s+WITHOUT\s+ROWID\s*$", "", s)
    # 自增主键
    s = re.sub(r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", "SERIAL PRIMARY KEY", s, flags=re.I)
    s = re.sub(r"INTEGER\s+PRIMARY\s+KEY", "SERIAL PRIMARY KEY", s, flags=re.I)
    # 类型映射
    s = re.sub(r"\bBLOB\b", "BYTEA", s, flags=re.I)
    s = re.sub(r"\bREAL\b", "DOUBLE PRECISION", s, flags=re.I)
    return s + ";"


def migrate(sqlite_path: str, pg_dsn: str, dry_run: bool = False) -> dict:
    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    dst = psycopg2.connect(pg_dsn)
    dst.autocommit = False
    report = {"tables": {}, "skipped": []}  # type: ignore[var-annotated]

    tables = src.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()

    with dst.cursor() as cur:
        for row in tables:
            name = row["name"]
            ddl = translate_ddl(row["sql"])
            if ddl is None:
                report["skipped"].append(name)  # type: ignore[attr-defined]
                continue
            if dry_run:
                report["tables"][name] = "dry-run"  # type: ignore[index]
                continue
            try:
                cur.execute(f'DROP TABLE IF EXISTS "{name}" CASCADE')
                cur.execute(ddl)
                # 读取数据
                rows = src.execute(f'SELECT * FROM "{name}"').fetchall()
                if rows:
                    cols = list(rows[0].keys())
                    col_sql = ",".join(f'"{c}"' for c in cols)
                    execute_values(
                        cur,
                        f'INSERT INTO "{name}" ({col_sql}) VALUES %s',
                        [tuple(r[c] for c in cols) for r in rows],
                        page_size=500,
                    )
                report["tables"][name] = len(rows)  # type: ignore[index]
            except Exception as e:
                dst.rollback()
                report["tables"][name] = f"ERROR: {e}"  # type: ignore[index]
                # 继续其他表
                dst.rollback()

        # 修复自增序列，避免插入冲突
        with dst.cursor() as cur:
            cur.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND column_default LIKE 'nextval(%'"
            )
            for t, c in cur.fetchall():
                try:
                    cur.execute(
                        f'SELECT setval(pg_get_serial_sequence(\'{t}\',\'{c}\'), COALESCE(MAX("{c}"),1)) FROM "{t}"'
                    )
                except Exception:
                    pass
        dst.commit()
    src.close()
    dst.close()
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite", default="data/bot.db")
    parser.add_argument("--pg-host", default=os.getenv("YUNO_PG_HOST", "127.0.0.1"))
    parser.add_argument("--pg-port", default=int(os.getenv("YUNO_PG_PORT", "5432")))
    parser.add_argument("--pg-db", default=os.getenv("YUNO_PG_DB", "yuno"))
    parser.add_argument("--pg-user", default=os.getenv("YUNO_PG_USER", "esp"))
    parser.add_argument("--pg-password", default=os.getenv("YUNO_PG_PASSWORD", "yuno"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    dsn = f"host={args.pg_host} port={args.pg_port} dbname={args.pg_db} user={args.pg_user} password={args.pg_password}"
    report = migrate(args.sqlite, dsn, dry_run=args.dry_run)
    import json
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not any(str(v).startswith("ERROR") for v in report["tables"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
