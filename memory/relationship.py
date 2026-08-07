"""Relationship Engine（v3 §13）：AI 与用户的关系状态机。

字段：trust（信任）/ familiarity（熟悉度）/ closeness（亲密）/ stage（阶段）/ history（轨迹）。
对话中自动累积：正常聊天涨熟悉度，纠错降信任，确认/感谢涨信任，公开记忆涨亲密。
阶段（按熟悉度）：陌生 <0.2 / 初识 <0.4 / 熟悉 <0.65 / 深度伙伴 ≥0.65。
影响：注入 prompt 的关系块 → 称呼 / 语气 / 建议方式。
"""

import json
from datetime import datetime

from plugins import _db

STAGE_THRESHOLDS = [
    (0.65, "深度伙伴"),
    (0.4, "熟悉"),
    (0.2, "初识"),
]

# 行为证据 → 状态增量（v3.1 §4）
EVIDENCE_WEIGHTS = {
    "chat": {"familiarity": 0.02},
    "share": {"trust": 0.04, "familiarity": 0.03, "closeness": 0.02},
    "praise": {"trust": 0.04, "closeness": 0.02},
    "dispute": {"trust": -0.05},
    "negative": {"trust": -0.03, "closeness": -0.01},
    "correct": {"familiarity": 0.02, "trust": -0.03},
}


def _stage_of(familiarity: float) -> str:
    for th, stage in STAGE_THRESHOLDS:
        if familiarity >= th:
            return stage
    return "陌生"


def _clamp(v, lo, hi):
    return round(min(hi, max(lo, float(v))), 3)


def update(
    scope,
    subject="",
    trust_delta=0.0,
    familiarity_delta=0.0,
    closeness_delta=0.0,
    event="chat",
    detail="",
):
    """按增量更新关系状态；返回更新后的行。"""
    scope = scope or ""
    if not scope:
        return None
    cur = _db.relationship_get(scope) or {
        "scope": scope,
        "subject": subject or "",
        "trust": 0.3,
        "familiarity": 0.0,
        "closeness": 0.0,
        "stage": "陌生",
        "history": "[]",
    }
    ev = EVIDENCE_WEIGHTS.get(event or "chat", {})
    trust_delta = float(trust_delta) + float(ev.get("trust", 0.0))
    familiarity_delta = float(familiarity_delta) + float(ev.get("familiarity", 0.0))
    closeness_delta = float(closeness_delta) + float(ev.get("closeness", 0.0))
    trust = _clamp(float(cur.get("trust", 0.3)) + trust_delta, 0.05, 1.0)
    familiarity = _clamp(float(cur.get("familiarity", 0.0)) + familiarity_delta, 0.0, 1.0)
    closeness = _clamp(float(cur.get("closeness", 0.0)) + closeness_delta, 0.0, 1.0)
    history = json.loads(cur.get("history") or "[]")
    if event:
        history.append(
            {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "event": event,
                "detail": (detail or "")[:100],
            }
        )
    history = history[-20:]
    _db.relationship_upsert(
        scope,
        subject=subject or cur.get("subject", ""),
        trust=trust,
        familiarity=familiarity,
        closeness=closeness,
        stage=_stage_of(familiarity),
        history=history,
    )
    return _db.relationship_get(scope)


def describe(scope) -> str:
    """生成可注入 prompt 的关系描述；无记录返回空串。"""
    row = _db.relationship_get(scope)
    if not row:
        return ""
    return (
        f"【与用户的关系】阶段：{row.get('stage', '陌生')} · "
        f"熟悉度 {float(row.get('familiarity', 0)):.0%} · "
        f"信任 {float(row.get('trust', 0.3)):.0%} · "
        f"亲密 {float(row.get('closeness', 0)):.0%} · "
        f"关系分 {score_of(row)}"
    )


def score_of(row) -> float:
    """relationship_score = Σ 历史行为权重 × exp(-λ·age)（时间衰减，λ=0.05/天）。"""
    import math
    total = 0.0
    now = datetime.now()
    for h in json.loads(row.get("history") or "[]"):
        w = sum(EVIDENCE_WEIGHTS.get(h.get("event"), {}).values())
        if not w:
            continue
        try:
            age_days = (now - datetime.fromisoformat(h["ts"])).total_seconds() / 86400
        except Exception:
            age_days = 0.0
        total += w * math.exp(-0.05 * max(0.0, age_days))
    return round(total, 3)


def rows():
    return _db.relationship_rows()
