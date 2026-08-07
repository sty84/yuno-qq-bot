---
name: qq-broadcast
description: 向 QQ 群发公告/播报：用 mcp__yuno__notify_send，内容简短。
---

# QQ 群播报

## 用法
- 发公告：`mcp__yuno__notify_send group <group_openid> <内容>`
- 内容控制在 500 字以内，语气用当前 SOUL 人设（千石由乃）。
- 目标群未配置时，先 `mcp__yuno__config_get` 看 `random_events.group_openid`。

## 注意
- 播报是入队异步发送（约 30 秒内到群），回复用户时说明“已加入播报队列”。
- 定时播报用 Hermes 内置 cron 触发同一工具即可。
