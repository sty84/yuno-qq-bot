# 裸机 / systemd 部署指南（非 Docker）

## 1. 环境要求

- Python 3.10+
- PostgreSQL 14+（生产默认）
- 可选：`pg_trgm` / `pg_bigm` 扩展（全文检索增强）

## 2. 安装依赖

```bash
cd /path/to/qq-bot
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
# PG 生产依赖
./venv/bin/pip install -r requirements-pg.txt
```

## 3. 配置

复制环境变量模板并填写：

```bash
cp .env.example .env
```

关键变量：

```bash
YUNO_DB_BACKEND=postgresql
YUNO_PG_HOST=127.0.0.1
YUNO_PG_PORT=5432
YUNO_PG_DB=yuno
YUNO_PG_USER=esp
YUNO_PG_PASSWORD=改成强密码
YUNO_PG_MINCONN=1
YUNO_PG_MAXCONN=8

# Web 管理台（公网务必设置）
YUNO_WEB_TOKEN=随机长token
YUNO_WEB_PASSWORD=管理员密码
YUNO_WEB_OPS_PASSWORD=运维密码
YUNO_WEB_READONLY_PASSWORD=只读密码
```

## 4. 初始化

```bash
# 建 PG schema
./venv/bin/python scripts/pg_init_schema.py

# 初始化 Persona Pack / 向量索引 / 评测集
./venv/bin/python tools.py init --pack yuno

# 校验配置
./venv/bin/python tools.py config-validate
```

## 5. 启动服务

### QQ Bot

```bash
./venv/bin/python bot.py
```

建议使用 systemd：

```ini
# /etc/systemd/system/qqbot.service
[Unit]
Description=Yuno QQ Bot
After=network.target postgresql.service

[Service]
WorkingDirectory=/path/to/qq-bot
ExecStart=/path/to/qq-bot/venv/bin/python bot.py
Restart=always
EnvironmentFile=/path/to/qq-bot/.env

[Install]
WantedBy=multi-user.target
```

### Web 管理台

```bash
./venv/bin/python -m uvicorn webapp:app --host 127.0.0.1 --port 8765
```

对应 systemd：

```ini
# /etc/systemd/system/yuno-web.service
[Unit]
Description=Yuno Web Ops
After=network.target postgresql.service

[Service]
WorkingDirectory=/path/to/qq-bot
ExecStart=/path/to/qq-bot/venv/bin/python -m uvicorn webapp:app --host 127.0.0.1 --port 8765
Restart=always
EnvironmentFile=/path/to/qq-bot/.env

[Install]
WantedBy=multi-user.target
```

## 6. 定时任务（cron）

```cron
# 每天 3 点：备份 + 健康检查 + PG 守护
0 3 * * * cd /path/to/qq-bot && ./venv/bin/python scripts/ops_cron.py >> /var/log/yuno_ops_cron.log 2>&1

# 每次代码更新后跑评测门禁（也可接入 CI）
0 4 * * * cd /path/to/qq-bot && ./venv/bin/python scripts/eval_ci.py >> /var/log/yuno_eval_ci.log 2>&1
```

## 7. 备份 / 恢复

```bash
# 手动备份
./venv/bin/python tools.py backup

# 恢复演练（只验证最新备份可读，不覆盖数据）
./venv/bin/python tools.py recover-drill

# PG 故障守护（故障时退出码 1，可配合监控）
./venv/bin/python tools.py pg-guard --notify
```

## 8. 安全注意

- 公网部署必须设置 `YUNO_WEB_TOKEN` 和三级密码。
- `YUNO_PG_PASSWORD` 不要写在命令行历史。
- Web 建议只监听 `127.0.0.1`，前方用 Nginx/Caddy 做 TLS。
- 定期执行 `recover-drill` 验证备份可恢复。
