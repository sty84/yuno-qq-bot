# Yuno 2.5 部署指南

本文档覆盖 PostgreSQL 生产环境的裸机 / systemd 部署。轻量测试可直接使用 SQLite。

## 1. 环境要求

- Python 3.10+
- PostgreSQL 14+（生产默认，建议启用 pgvector）
- DeepSeek 或其他 OpenAI 兼容 API
- Linux（Debian / Ubuntu 示例路径 `/home/ubuntu/qq-bot`）

## 2. 安装依赖

```bash
cd /home/ubuntu/qq-bot
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/pip install -r requirements-pg.txt
```

CPU 服务器先装 CPU 版 torch，避免下载 CUDA 版本：

```bash
./venv/bin/pip install torch \
  -i https://pypi.tuna.tsinghua.edu.cn/simple \
  -f https://mirrors.aliyun.com/pytorch-wheels/cpu
```

## 3. 配置环境变量

```bash
cp .env.example .env
nano .env
```

生产必填项：

```bash
APPID=...
SECRET=...
DEEPSEEK_API_KEY=...
ADMIN_OPENIDS=...

YUNO_DB_BACKEND=postgresql
YUNO_PG_HOST=127.0.0.1
YUNO_PG_PORT=5432
YUNO_PG_DB=yuno
YUNO_PG_USER=yuno
YUNO_PG_PASSWORD=强密码
YUNO_PG_MINCONN=1
YUNO_PG_MAXCONN=8

# Web 管理台：公网暴露时三个 token / 密码都必须配置
YUNO_WEB_TOKEN=随机长token
YUNO_WEB_PASSWORD=管理员密码
YUNO_WEB_OPS_PASSWORD=运维密码
YUNO_WEB_READONLY_PASSWORD=只读密码
```

## 4. 初始化数据库

```bash
# 建库和扩展（pg_trgm / pgvector 可选但推荐）
sudo -u postgres psql -c "CREATE USER yuno WITH PASSWORD '强密码';"
sudo -u postgres psql -c "CREATE DATABASE yuno OWNER yuno;"
sudo -u postgres psql -d yuno -c "CREATE EXTENSION IF NOT EXISTS pg_trgm;"
sudo -u postgres psql -d yuno -c "CREATE EXTENSION IF NOT EXISTS vector;"

# 创建 schema（幂等）
./venv/bin/python scripts/pg_init_schema.py

# 初始化 Persona Pack、向量索引与评测集
./venv/bin/python tools.py init --pack yuno

# 校验配置
./venv/bin/python tools.py config-validate
```

数据库后端说明：

- `YUNO_DB_BACKEND=postgresql`（默认）：生产使用，连接池 1~8 个连接。
- `YUNO_DB_BACKEND=sqlite`：测试 / 轻量部署，写入 `data/persona-yuno/bot.db`。
- 向量检索优先使用 pgvector；未启用时回退自研 IVF 索引。

## 5. 启动服务

### QQ Bot

```bash
./venv/bin/python bot.py
```

systemd 单元：

```ini
# /etc/systemd/system/qqbot.service
[Unit]
Description=Yuno QQ Bot
After=network.target postgresql.service

[Service]
WorkingDirectory=/home/ubuntu/qq-bot
ExecStart=/home/ubuntu/qq-bot/venv/bin/python bot.py
Restart=always
EnvironmentFile=/home/ubuntu/qq-bot/.env

[Install]
WantedBy=multi-user.target
```

### Web 管理台

```bash
./venv/bin/python webapp.py --host 127.0.0.1 --port 8600
```

systemd 单元：

```ini
# /etc/systemd/system/yuno-web.service
[Unit]
Description=Yuno Web Ops
After=network.target postgresql.service

[Service]
WorkingDirectory=/home/ubuntu/qq-bot
ExecStart=/home/ubuntu/qq-bot/venv/bin/python webapp.py --host 127.0.0.1 --port 8600
Restart=always
EnvironmentFile=/home/ubuntu/qq-bot/.env

[Install]
WantedBy=multi-user.target
```

启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now qqbot yuno-web
```

## 6. 定时任务

```bash
sudo -u yuno crontab -e
```

```cron
# 每天 3:00：备份 + 健康检查 + PG 守护
0 3 * * * cd /home/ubuntu/qq-bot && ./venv/bin/python scripts/ops_cron.py >> /var/log/yuno_ops_cron.log 2>&1

# 每天 3:30：记忆成长 / 巩固 / 索引
30 3 * * * cd /home/ubuntu/qq-bot && ./venv/bin/python tools.py memory-grow >> /var/log/yuno_grow.log 2>&1

# 每周一 4:00：CI 评测门禁（自动写 docs/baselines/ci_eval.json）
0 4 * * 1 cd /home/ubuntu/qq-bot && ./venv/bin/python scripts/eval_ci.py >> /var/log/yuno_eval_ci.log 2>&1

# 每周一 4:30：内部数据清理
30 4 * * 1 cd /home/ubuntu/qq-bot && ./venv/bin/python tools.py internal-db-prune --days 30 >> /var/log/yuno_prune.log 2>&1
```

## 7. 备份与恢复

```bash
# 手动备份（默认保留 7 份）
./venv/bin/python tools.py backup

# 恢复演练：只验证最新备份可读，不覆盖生产数据
./venv/bin/python tools.py recover-drill

# PG 故障守护：健康检查失败时退出码 1，可配 --notify 播报
./venv/bin/python tools.py pg-guard --notify

# 全量数据导出 / 导入
./venv/bin/python tools.py data-export
./venv/bin/python tools.py data-import <file> --dry-run
```

## 8. 安全清单

- [ ] `YUNO_WEB_TOKEN` 与三级密码已配置，密码不与源码同仓库。
- [ ] Web 只监听 `127.0.0.1`，公网经 Nginx / Caddy 终止 TLS。
- [ ] `YUNO_API_TOKEN` 已配置，公网 SDK 服务必须鉴权。
- [ ] `YUNO_PG_PASSWORD` 不写入命令行历史与日志。
- [ ] 定期执行 `recover-drill`，验证备份可恢复。
- [ ] 定期执行 `scripts/secret_scan.py` 与 `pip-audit`。

## 9. 常见问题

### 启动报 `YUNO_PG_PASSWORD 未设置`

`.env` 未加载或变量缺失。检查服务单元的 `EnvironmentFile` 路径，并确认
`YUNO_DB_BACKEND=postgresql` 时 PG 五项变量均已配置。

### 迁移到新 PostgreSQL 实例

使用 `scripts/migrate_sqlite_to_pg.py` 从 SQLite 迁移，或先 `data-export` 全量导出、
在新库 `pg_init_schema.py` 后 `data-import`。

### pgvector 不可用

`memory/vecindex.py` 会回退自研 IVF。执行 `tools.py memory-index` 重建索引即可。
