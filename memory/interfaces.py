# -*- coding: utf-8 -*-
"""认知架构标准化接口（借鉴 CoALA）。

提供统一的内存/决策/动作接口，并给出基于当前模块的默认适配器。
现有业务代码可以逐步迁移到这些接口，降低耦合、方便替换实现。
"""

from dataclasses import dataclass, field
from typing import Any, Protocol


class MemoryInterface(Protocol):
    def search(self, query: str, scopes: list[str], top_k: int = 5) -> list[dict]:
        ...

    def add(self, scope: str, key: str, fact: str, **kwargs) -> Any:
        ...

    def forget(self, scope: str = None) -> dict:
        ...


class DecisionInterface(Protocol):
    def decide(self, query: str, scope: str = "", context: str = "", *args, **kwargs) -> dict:
        ...


class ActionInterface(Protocol):
    def execute(self, action: str, **kwargs) -> Any:
        ...


@dataclass
class CognitiveTurn:
    """一次标准化认知回合的输出。"""
    query: str
    scope: str = ""
    situation: dict = field(default_factory=dict)
    goals: list = field(default_factory=list)
    intention: dict = field(default_factory=dict)
    activated_memories: list = field(default_factory=list)
    options: list = field(default_factory=list)
    action: Any = None
    reply: str = ""
    meta: dict = field(default_factory=dict)


class MemoryPort:
    """默认记忆端口：包装 reasoning / controller / policy。"""

    def search(self, query, scopes, top_k=5, **kwargs):
        from memory import reasoning
        return [
            {"fact": f, "score": s, "scope": sc}
            for f, s, sc in reasoning.retrieve(query, scopes, top_k=top_k, min_score=0.0)
        ]

    def add(self, scope, key, fact, **kwargs):
        from memory import controller
        return controller.ingest(scope, key, fact, facts=[fact])

    def forget(self, scope=None):
        from memory import policy
        return policy.forget(scope)


class DecisionPort:
    """默认决策端口：包装 mind.snapshot。"""

    def decide(self, query, scope="", context="", *args, **kwargs):
        from memory import mind
        snap = mind.snapshot(scope, query) if scope else {}
        return {
            "situation": snap.get("situation", {}),
            "goals": snap.get("goals", []),
            "intention": snap.get("intention", {}),
            "options": snap.get("options", []),
        }


class ActionPort:
    """默认动作端口：目前只描述动作，不直接执行外部副作用。"""

    def execute(self, action, **kwargs):
        return {"action": action, "text": kwargs.get("text", "")}


class CognitiveArchitecture:
    """组合记忆/决策/动作端口，形成统一认知流程。"""

    def __init__(self, memory=None, decision=None, action=None):
        self.memory = memory or MemoryPort()
        self.decision = decision or DecisionPort()
        self.action = action or ActionPort()

    def run(self, query: str, scope: str = "", context: str = "", *args, **kwargs) -> CognitiveTurn:
        decision = self.decision.decide(query, scope, context, *args, **kwargs)
        # 决策端口可返回 early_reply/early_meta 表示无需进入动作阶段（如深睡离线）
        if decision.get("early_reply") is not None:
            return CognitiveTurn(
                query=query,
                scope=scope,
                reply=decision.get("early_reply", ""),
                meta=decision.get("early_meta", {}),
                situation=decision.get("situation", {}),
                goals=decision.get("goals", []),
                intention=decision.get("intention", {}),
                activated_memories=decision.get("activated_memories", []),
                options=decision.get("options", []),
                action={"action": "early_exit", "text": query},
            )

        search_scopes = kwargs.get("scopes") if kwargs.get("scopes") is not None else ([scope] if scope else [])
        search_kwargs = {k: v for k, v in kwargs.items() if k != "scopes"}
        hits = self.memory.search(query, search_scopes, top_k=5, *args, **search_kwargs)
        action = self.action.execute(
            decision.get("options", [{}])[0].get("action", "reply") if decision.get("options") else "reply",
            text=query,
            decision=decision,
            memory=self.memory,
            context=context,
            **kwargs,
        )
        reply = ""
        meta = {}
        if isinstance(action, dict):
            reply = action.get("reply", "")
            meta = action.get("meta", {})
        return CognitiveTurn(
            query=query,
            scope=scope,
            situation=decision.get("situation", {}),
            goals=decision.get("goals", []),
            intention=decision.get("intention", {}),
            activated_memories=hits,
            options=decision.get("options", []),
            action=action,
            reply=reply,
            meta=meta,
        )

    def run_to_dict(self, query: str, scope: str = "", context: str = "") -> dict:
        turn = self.run(query, scope, context)
        return {
            "query": turn.query,
            "scope": turn.scope,
            "situation": turn.situation,
            "goals": turn.goals,
            "intention": turn.intention,
            "activated_memories": turn.activated_memories,
            "options": turn.options,
            "action": turn.action,
        }


def default_architecture() -> CognitiveArchitecture:
    """返回使用当前项目模块的默认认知架构。"""
    return CognitiveArchitecture()
