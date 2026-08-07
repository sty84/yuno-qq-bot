# Hermes Agent × YUNO 2.0 结合方案

把 Hermes Agent 作为“管理端大脑”，对接本仓库已有的能力层。
QQ 聊天前台（bot.py）保持不动，Hermes 通过 MCP 调用本机服务/配置/审计/播报工具。

## 架构

```
              ┌──────────────────────────────────┐
              │ Hermes Agent（管理端大脑）        │
              │  SOUL.md = 千石由乃人设           │
              │  AGENTS.md = 项目用法规则          │
              │  skills/ = 游戏、播报等能力        │
              └───────┬──────────────────────────┘
                      │ MCP（stdio）
                      ▼
              tools.py mcp   ←→ plugins/_capability.py
              （本机工具箱）
                      │
                      ├── 服务注册表 services（config.json）
                      ├── 审计 audit（SQLite）
                      └── 播报队列 notifications
                              │（notify.send 入队）
                              ▼
                      bot.py 播报插件 → QQ 群
```

## 三个接缝

1. **MCP 接缝**：Hermes 的 `config.yaml` 注册 `yuno` 这个 stdio MCP Server，
   工具自动变成 `mcp__yuno__services_list` 这种名字，Hermes 直接调用。
2. **个性接缝**：人设**单一来源是项目根目录的 `persona.md`**——QQ 机器人直接读它，
   Hermes 用 `tools.py sync-persona` 同步成 `~/.hermes/SOUL.md`，两边永远一致，改一处即可。
3. **规则接缝**：项目层面的用法（哪个工具管什么、高危操作要确认）放
   `~/.hermes/AGENTS.md`；玩法类能力放 `~/.hermes/skills/`。

## 部署步骤

```bash
# 1. 安装 Hermes（Linux）
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash

# 2. 把本目录的文件放到 Hermes 主目录（~/.hermes/）
cp hermes/config.yaml ~/.hermes/config.yaml        # 合并进已有配置
cp hermes/SOUL.md ~/.hermes/SOUL.md
python tools.py sync-persona                      # 或直接由 persona.md 同步生成 SOUL.md
cp hermes/AGENTS.md ~/.hermes/AGENTS.md
cp -r hermes/skills/* ~/.hermes/skills/

# 3. 重启 Hermes，然后：
/reload-mcp     # 加载 MCP 服务器
# 问一句：你现在有哪些可用工具？
```

## 验证

```text
你：查一下 qqbot 服务状态
Hermes：调用 mcp__yuno__services_status → 返回运行中/已停止
你：给群里发一条公告：今晚维护
Hermes：调用 mcp__yuno__notify_send → QQ 播报插件 30 秒内推送
```

## 注意事项

- **运行用户**：Hermes 建议以 `aiagent` 运行（或给运行用户配 qqbot-ctl 的
  sudoers），否则 `services.start/stop/restart`、`config.set` 这类写操作会被拒；
  读操作（list/status/logs/config.get/audit.query）不受影响。
- **工具名映射**：MCP 工具名里的 `.` 会被替换成 `_`，
  `services.list` → `mcp__yuno__services_list`。
- **版本漂移**：Hermes 迭代很快，SKILL.md 格式以
  [agentskills.io](https://agentskills.io) 标准为准，装好后按实际版本微调。
