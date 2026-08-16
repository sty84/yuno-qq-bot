"""revive-companion（v2.2+）：泊松触发 + 贝叶斯用户状态推断，决定"现在该不该主动找你"。

- 泊松过程：随机触发间隔 ~ Exp(λ)，事件率按天计（默认 2 次/天），替代固定定时；
- 贝叶斯状态：P(活跃/忙/睡/需要关心) = prior(时段) × likelihood(消息活跃度) 归一化；
- 门控：只在"活跃/需要关心"态且泊松命中时触发；睡眠/忙碌态自动让路；
- 冷却：两次主动消息之间至少 cooldown_min 分钟。

配置：memory.core.revive.{rate_per_day, cooldown_min, asleep_range}。
"""

import math
import random
import time
from datetime import datetime

from plugins import _db, _shared

STATES = ("active", "busy", "asleep", "need_care")
STATE_ZH = {"active": "活跃", "busy": "忙", "asleep": "睡觉", "need_care": "需要关心"}


def _cfg(key, default):
    return _shared.core_cfg("revive", key, default)
def _hour():
    try:
        return datetime.now().hour
    except Exception:
        return 12


def _time_prior(hour=None) -> dict:
    """时段先验：睡眠窗口默认 0-7 点；白天偏忙、晚上偏活跃。"""
    h = _hour() if hour is None else int(hour)
    asleep = _cfg("asleep_range", [0, 7])
    try:
        lo, hi = int(asleep[0]), int(asleep[1])
    except Exception:
        lo, hi = 0, 7
    if lo <= h < hi:
        return {"active": 0.05, "busy": 0.05, "asleep": 0.85, "need_care": 0.05}
    if 7 <= h < 9:
        return {"active": 0.40, "busy": 0.30, "asleep": 0.10, "need_care": 0.20}
    if 9 <= h < 18:
        return {"active": 0.15, "busy": 0.60, "asleep": 0.00, "need_care": 0.25}
    if 18 <= h < 23:
        return {"active": 0.55, "busy": 0.20, "asleep": 0.05, "need_care": 0.20}
    return {"active": 0.35, "busy": 0.10, "asleep": 0.35, "need_care": 0.20}


def _evidence(scope=None) -> dict:
    """消息活跃度似然：最近有消息→活跃；沉默 4h+→需要关心；1 天+→疏远/需关心。"""
    if not scope:
        return {"active": 0.30, "busy": 0.30, "asleep": 0.15, "need_care": 0.25}
    row = _db.kv_get("memory", f"last_user_msg:{scope}")  # type: ignore[attr-defined]
    last = None
    if isinstance(row, str):
        try:
            last = datetime.fromisoformat(str(row)[:19]).timestamp()
        except Exception:
            last = None
    elif isinstance(row, dict):
        ts = row.get("ts")
        if ts:
            try:
                last = float(ts) if isinstance(ts, (int, float)) else datetime.fromisoformat(str(ts)[:19]).timestamp()
            except Exception:
                last = None
    if last is None:
        return {"active": 0.20, "busy": 0.30, "asleep": 0.20, "need_care": 0.30}
    mins = max(0.0, (time.time() - last) / 60.0)
    if mins < 30:
        return {"active": 0.90, "busy": 0.05, "asleep": 0.00, "need_care": 0.05}
    if mins < 240:
        return {"active": 0.40, "busy": 0.30, "asleep": 0.05, "need_care": 0.25}
    if mins < 1440:
        return {"active": 0.05, "busy": 0.15, "asleep": 0.10, "need_care": 0.70}
    return {"active": 0.02, "busy": 0.08, "asleep": 0.30, "need_care": 0.60}


def state_posterior(scope=None) -> dict:
    """贝叶斯后验：P(state) ∝ prior(时段) × likelihood(消息活跃度)。"""
    prior, lik = _time_prior(), _evidence(scope)
    post = {s: prior[s] * lik[s] for s in STATES}
    total = sum(post.values()) or 1.0
    return {s: round(post[s] / total, 3) for s in STATES}


def poisson_p(last_ts, rate_per_day=2.0) -> float:
    """自上次触发以来到现在的泊松触发概率：1 - exp(-λ Δt)。"""
    if last_ts is None:
        return 1.0
    try:
        dt_days = max(0.0, (time.time() - float(last_ts)) / 86400.0)
    except (TypeError, ValueError):
        return 1.0
    return 1.0 - math.exp(-max(0.0, float(rate_per_day)) * dt_days)


def _last_ts():
    last = _db.kv_get("memory", "revive_last") or {}
    return last.get("ts")


def decide(scope=None, force=False) -> dict:
    """综合决策：泊松命中 && 状态门控 && 冷却。force=True 跳过门控（仍守冷却），供测试/调试。"""
    rate = float(_cfg("rate_per_day", 2.0))
    cooldown_min = float(_cfg("cooldown_min", 30))
    last_ts = _last_ts()
    p = poisson_p(last_ts, rate)
    post = state_posterior(scope)
    top = max(STATES, key=lambda s: post[s])
    if last_ts and (time.time() - float(last_ts)) / 60.0 < cooldown_min:
        return {"fire": False, "state": top, "posterior": post, "reason": "冷却中", "p": round(p, 3)}
    if not force:
        if top in ("asleep", "busy"):
            return {"fire": False, "state": top, "posterior": post, "reason": f"用户{STATE_ZH[top]}", "p": round(p, 3)}
        if random.random() >= p:
            return {"fire": False, "state": top, "posterior": post, "reason": "泊松未命中", "p": round(p, 3)}
    _db.kv_set("memory", "revive_last", {"ts": time.time()})  # type: ignore[attr-defined]
    return {"fire": True, "state": top, "posterior": post, "reason": "泊松命中" if not force else "强制触发", "p": round(p, 3)}


def peek(scope=None) -> dict:
    """只读状态（不消费泊松/不写 revive_last），供 revive-status 展示。"""
    rate = float(_cfg("rate_per_day", 2.0))
    cooldown_min = float(_cfg("cooldown_min", 30))
    last_ts = _last_ts()
    p = poisson_p(last_ts, rate)
    post = state_posterior(scope)
    top = max(STATES, key=lambda s: post[s])
    cooldown = bool(last_ts and (time.time() - float(last_ts)) / 60.0 < cooldown_min)
    return {
        "state": top,
        "state_zh": STATE_ZH[top],
        "posterior": post,
        "p": round(p, 3),
        "cooldown": cooldown,
        "would_fire": (not cooldown) and top not in ("asleep", "busy") and p > 0.5,
        "rate_per_day": rate,
    }
