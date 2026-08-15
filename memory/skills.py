# -*- coding: utf-8 -*-
"""技能库（Voyager 式 skill library）：把成功经验/失败反思沉淀为可复用技能。

技能比 procedures 更丰富：包含 result / condition / failure_reason / source。
- 成功时记录 result，提升 success；
- 失败时记录 failure_reason，降低 success；
- 检索时按情境/动作/结果关键词匹配。
"""

from plugins import _db


def record(situation, action, result="", condition="", failure_reason="", source="", success=None):
    """记录或更新一条技能。success=None 时按是否有 result/failure_reason 自动给默认值。"""
    if success is None:
        success = 0.8 if result and not failure_reason else (0.3 if failure_reason else 0.5)
    _db.skill_add(
        situation=situation, action=action, result=result,
        condition=condition, failure_reason=failure_reason, source=source, success=success,
    )


def mark_success(situation, action, result="", source="skill"):
    record(situation, action, result=result, source=source, success=0.9)


def mark_failure(situation, action, reason="", source="reflection"):
    record(situation, action, failure_reason=reason, source=source, success=0.2)


def search(query, limit=5):
    return _db.skill_search(query, limit=limit)


def all(limit=200):
    return _db.skill_rows(limit=limit)


def update(situation, action, success=None, result=None, failure_reason=None, condition=None):
    _db.skill_update(
        situation=situation, action=action, success=success,
        result=result, failure_reason=failure_reason, condition=condition,
    )
