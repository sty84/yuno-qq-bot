# -*- coding: utf-8 -*-
"""认知架构标准化接口（借鉴 CoALA）。

不强制现有模块立即重构，先定义统一协议，后续逐步适配。
"""

from typing import Any, Protocol


class MemoryInterface(Protocol):
    def search(self, query: str, scopes: list[str], top_k: int = 5) -> list[dict]:
        ...

    def add(self, scope: str, key: str, fact: str, **kwargs) -> Any:
        ...

    def forget(self, scope: str = None) -> dict:
        ...


class ActionInterface(Protocol):
    def execute(self, action: str, **kwargs) -> Any:
        ...


class DecisionInterface(Protocol):
    def decide(self, query: str, scope: str = "", context: str = "") -> dict:
        ...


class CognitiveArchitecture:
    """最小认知架构：决策 → 记忆检索 → 动作/回复。"""

    def __init__(self, memory=None, decision=None, action=None):
        self.memory = memory
        self.decision = decision
        self.action = action

    def run(self, query: str, scope: str = "", context: str = ""):
        decision = self.decision.decide(query, scope, context) if self.decision else {}
        hits = []
        if self.memory is not None:
            hits = self.memory.search(query, [scope] if scope else [], top_k=5)
        action = self.action.execute(decision.get("action", "reply"), text=query) if self.action else None
        return {
            "decision": decision,
            "memory_hits": hits,
            "action": action,
        }


def default_architecture():
    """用当前项目模块组装一个默认架构实例。"""
    from memory import reasoning, mind

    class _Memory:
        def search(self, query, scopes, top_k=5):
            return [f for f, _s, _sc in reasoning.retrieve(query, scopes, top_k=top_k, min_score=0.0)]

        def add(self, scope, key, fact, **kwargs):
            from memory import controller
            return controller.ingest(scope, key, fact, facts=[fact])

        def forget(self, scope=None):
            from memory import policy
            return policy.forget(scope)

    class _Decision:
        def decide(self, query, scope="", context=""):
            return mind.snapshot(scope, query) if scope else {"options": []}

    class _Action:
        def execute(self, action, **kwargs):
            return {"action": action, "text": kwargs.get("text", "")}

    return CognitiveArchitecture(memory=_Memory(), decision=_Decision(), action=_Action())
