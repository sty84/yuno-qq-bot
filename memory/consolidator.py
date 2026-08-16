# -*- coding: utf-8 -*-
"""记忆整合器：自动合并碎片、处理冲突、巩固/遗忘，形成定期维护闭环。"""

from memory import policy


def run(scope=None, apply=True) -> dict:
    """执行一轮记忆整合，返回报告。"""
    from memory import controller as ctl
    report = {}
    try:
        from memory.backfill import merge_fragments
        report["fragments_merged"] = merge_fragments(scope)
    except Exception as e:
        report["fragments_merged"] = f"ERROR: {e}"  # type: ignore[assignment]

    try:
        text, conflicts = ctl.conflict_scan(scope, apply=apply)
        report["conflicts"] = len(conflicts)
        report["conflict_text"] = text
    except Exception as e:
        report["conflicts"] = f"ERROR: {e}"  # type: ignore[assignment]

    try:
        report["promoted"] = policy.promote(scope)
    except Exception as e:
        report["promoted"] = f"ERROR: {e}"  # type: ignore[assignment]

    try:
        report["fuzzy"], report["forgotten"] = policy.forget(scope).values()
    except Exception as e:
        report["fuzzy"] = f"ERROR: {e}"  # type: ignore[assignment]

    return report
