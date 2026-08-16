# -*- coding: utf-8 -*-
"""Agent 核心：认知架构入口 + 学习/成长接口。

实际记忆/决策/动作端口实现已拆分到 agent.ports。
"""

from memory import ingest
from memory.interfaces import CognitiveArchitecture

from agent.ports import _AgentMemoryPort, _AgentDecisionPort, _AgentActionPort


def ask(
    text,
    history=None,
    extra_context="",
    scopes=None,
    learn=False,
    learn_scope=None,
    learn_key="",
    facts=None,
    system=None,
    llm=None,
):
    """Agent 主入口：由 CognitiveArchitecture 端口编排。

    决策端口准备上下文与状态，记忆端口执行检索，动作端口生成回复；
    返回 (reply, meta)。
    """
    arch = CognitiveArchitecture(
        memory=_AgentMemoryPort(),
        decision=_AgentDecisionPort(),
        action=_AgentActionPort(),
    )
    turn = arch.run(
        text,
        scope=scopes[0] if scopes else "",
        context=extra_context,
        history=history,
        extra_context=extra_context,
        scopes=scopes,
        learn=learn,
        learn_scope=learn_scope,
        learn_key=learn_key,
        facts=facts,
        system=system,
        llm=llm,
    )
    return turn.reply, turn.meta


def learn(text, reply, scope, key, facts=None) -> dict:
    """显式学习一条对话（等价 memory.ingest，语义化命名）。"""
    return ingest(scope, key, text, reply, facts=facts)


def grow(scope=None, dry_run=False) -> dict:
    """成长接口（工程化）：返回结构化报告；dry_run=True 只出统计不写库。"""
    import memory
    if dry_run:
        return {"stats": memory.eval_report()}
    return memory.backfill_run(batch=64)
