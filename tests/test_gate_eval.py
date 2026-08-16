# -*- coding: utf-8 -*-
"""证据门控评测集回归：使用 memory.gate_eval 的共享用例。"""

from memory.gate_eval import CASES
from agent import evidence_gate as eg


def test_gate_eval_set():
    failed = []
    for i, (reply, evidence, banned, user_text, should_block) in enumerate(CASES):
        got = eg.contains_unsupported_claim(
            reply, evidence=evidence, banned=banned, user_text=user_text
        )
        blocked = got is not None
        if blocked != should_block:
            failed.append((i, reply, got, should_block))
    assert not failed, failed


def test_gate_eval_evaluate():
    """覆盖 memory.gate_eval.evaluate() 聚合路径。"""
    from memory.gate_eval import evaluate
    res = evaluate()
    assert res["total"] == len(CASES)
    assert res["passed"] == len(CASES)
    assert res["accuracy"] == 1.0
    assert res["errors"] == []
