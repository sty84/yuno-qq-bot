"""Memory / Persona / Agent 系统对外入口。

  agent.ask(text, ...)            分析 → 人格 → 记忆 → 云端 LLM → 回复
  agent.learn(text, reply, scope, key)   显式学习一条对话
  agent.grow()                    成长：巩固观点 / 修剪 / 清理
  agent.compose()                 合成 system prompt（人设 + 心情 + AI 人格记忆）
  agent.snapshot()                Persona 状态快照
"""

from agent import core, persona

# 启动即把 persona.md 同步进统一记忆库（scope='ai'，key='identity'）
persona.sync_identity()

ask = core.ask
learn = core.learn
grow = core.grow
compose = persona.compose
snapshot = persona.snapshot
