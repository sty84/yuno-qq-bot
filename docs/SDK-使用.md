# yuno-memory SDK 使用指南

把统一记忆系统暴露为可独立使用的接口。QQ 机器人（bot.py）是内置客户端；本 SDK 供任意程序 / Agent / 平台接入，多端可共享同一份记忆（人格用 `ai:<id>` 隔离）。

## Python SDK

```python
from yuno_memory import Memory

# 一个进程一个实例；data_dir 决定记忆库存放位置
m = Memory(
    data_dir="./memory-data",
    api_key="sk-xxx",            # DeepSeek / OpenAI 兼容
    embedder="local",            # 或 {"provider":"openai_compatible", ...}；缺省不启用向量
)

# 写入（纠错调查自动发生）
m.ingest("c2c:u1", "我养了一只黑猫叫煤球", "收到")

# 检索
for fact, score, scope in m.search("我家的猫", ["c2c:u1"], min_score=0.0):
    print(round(score, 3), fact)

# 轨迹 + 人工五维评分（评分会驱动行为参数）
traces = m.trace("c2c:u1", limit=10)
m.review(traces[0]["id"], {"extraction": 5, "decision": 4, "confidence": 4, "provenance": 4, "privacy": 5}, "好", "reviewer1")

# 目标 / 决策顾问 / 评测 / 导出
m.goal_add("c2c:u1", "年底上台表演", priority=1, motivation="学吉他")
m.consult("c2c:u1", "我要不要买那把三千块的吉他")
m.eval([{"query": "我家的猫", "expected": ["养了一只黑猫叫煤球"], "scope": "c2c:u1"}])
m.export("memory-data/export.tar.gz")
```

## FastAPI 服务（任意语言 / Agent 接入）

```bash
python -m yuno_memory --host 127.0.0.1 --port 8457 --data-dir ./memory-data --api-key sk-xxx --embedder local
```

```bash
# 写入
curl -X POST http://127.0.0.1:8457/memory/ingest \
  -H 'Content-Type: application/json' \
  -d '{"scope":"c2c:u1","text":"我养了一只黑猫叫煤球","reply":"收到"}'

# 检索
curl -X POST http://127.0.0.1:8457/memory/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"我家的猫","scopes":["c2c:u1"],"min_score":0.0}'

# 轨迹 / 评分 / 顾问 / 导出
curl http://127.0.0.1:8457/memory/trace?scope=c2c:u1
curl -X POST http://127.0.0.1:8457/memory/review -H 'Content-Type: application/json' -d '{"trace_id":1,"scores":{"extraction":5,"confidence":4}}'
curl -X POST http://127.0.0.1:8457/consult -H 'Content-Type: application/json' -d '{"scope":"c2c:u1","text":"我要不要买那把三千块的吉他"}'
curl -X POST http://127.0.0.1:8457/memory/export
```

## 接入 Hermes / 其他 Agent

Hermes 的 MCP 可直接换成 HTTP 工具：

```yaml
mcp_servers:
  yuno:
    url: "http://127.0.0.1:8457/sse"
```

或任何支持 OpenAI 工具调用的 Agent，用 `POST /memory/search` + `POST /memory/ingest` 即可获得完整记忆能力。

## 与 QQ 机器人共用

QQ 机器人默认用自己的 `data/bot.db`。想让 QQ 和 SDK 共享同一份记忆：

- 方案 A：SDK 服务指向 QQ 的 `data` 目录（`--data-dir /home/ubuntu/qq-bot/data`），两者读写同一个 SQLite（WAL 支持并发）
- 方案 B：SDK 独立目录，通过 `data-export` / 导入同步

注意：同一进程内只能初始化一个 Memory 实例（配置在导入时固定）；多端共享建议走独立进程的 HTTP 服务。
