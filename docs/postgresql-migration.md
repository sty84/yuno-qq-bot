# PostgreSQL 迁移记录

> 日期：2026-08-15
> 状态：数据迁移完成并验证一致；应用默认使用 PostgreSQL，SQLite 仅作为测试/兼容开关保留。

## 已完成

- 安装并启动 PostgreSQL 17
- 创建数据库 `yuno`，用户 `esp`
- 编写迁移脚本：`scripts/migrate_sqlite_to_pg.py`
- 编写验证脚本：`scripts/verify_pg_migration.py`
- 新增 PG 适配层骨架：`plugins/_db_pg.py`

## 迁移结果

从 `data/persona-yuno/bot.db` 迁移到 PostgreSQL `yuno`：

- 迁移表数：52
- 跳过 FTS 内部表：6
- 数据一致性：52/52 表行数一致
- 主要数据：
  - memories: 134
  - events: 155
  - memory_trace: 625
  - query_log: 1304
  - llm_cost: 996
  - topics: 203

## 验证命令

```bash
# 迁移
python scripts/migrate_sqlite_to_pg.py --sqlite data/persona-yuno/bot.db

# 验证行数一致
python scripts/verify_pg_migration.py

# 查看 PG 健康/表行数
python -c "from plugins import _db_pg; print(_db_pg.health()); print(_db_pg.table_counts())"
```

## 环境变量

```bash
export YUNO_PG_HOST=127.0.0.1
export YUNO_PG_PORT=5432
export YUNO_PG_DB=yuno
export YUNO_PG_USER=esp
export YUNO_PG_PASSWORD=yuno
```

## 尚未完成

- `plugins/_db.py` 已默认切换为 PostgreSQL；设 `YUNO_DB_BACKEND=sqlite` 可强制 SQLite（主要用于测试隔离）。
- 需要继续实现 PG adapter，覆盖现有 `_db` 的读写接口。
- 需要处理 FTS5 → PostgreSQL 全文检索 / pgvector。
