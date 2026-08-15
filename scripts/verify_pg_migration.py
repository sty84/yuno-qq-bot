#!/usr/bin/env python3
"""验证 SQLite 与 PostgreSQL 迁移后行数一致。"""

import argparse
import os
import sqlite3
import sys

import psycopg2


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite", default="data/persona-yuno/bot.db")
    parser.add_argument("--pg-host", default=os.getenv("YUNO_PG_HOST", "127.0.0.1"))
    parser.add_argument("--pg-port", default=int(os.getenv("YUNO_PG_PORT", "5432")))
    parser.add_argument("--pg-db", default=os.getenv("YUNO_PG_DB", "yuno"))
    parser.add_argument("--pg-user", default=os.getenv("YUNO_PG_USER", "esp"))
    parser.add_argument("--pg-password", default=os.getenv("YUNO_PG_PASSWORD", "yuno"))
    args = parser.parse_args()

    src = sqlite3.connect(args.sqlite)
    dst = psycopg2.connect(
        host=args.pg_host, port=args.pg_port, dbname=args.pg_db,
        user=args.pg_user, password=args.pg_password,
    )
    tables = [r[0] for r in src.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'memories_fts%' ORDER BY name"
    ).fetchall()]
    mismatches = []
    total_ok = 0
    with dst.cursor() as cur:
        for t in tables:
            try:
                sc = src.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                cur.execute(f'SELECT COUNT(*) FROM "{t}"')
                pc = cur.fetchone()[0]
                if sc != pc:
                    mismatches.append((t, sc, pc))
                else:
                    total_ok += 1
            except Exception as e:
                mismatches.append((t, f"ERR {e}", None))
    src.close()
    dst.close()
    print(f"对比表数: {len(tables)}，一致: {total_ok}，不一致: {len(mismatches)}")
    for m in mismatches:
        print("  ", m)
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
