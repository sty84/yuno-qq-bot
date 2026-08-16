"""cognitive-engine bandit（v2.2+）：Thompson Sampling 维护"什么回应策略对当前用户有效"的后验。

- 策略空间：共情优先 / 直接给方案 / 玩梗减压 / 陪伴倾听 / 轻松转移；
- 每次回复前按 Beta 后验采样选择策略，注入回应提示（agent.ask 的【回应策略】块）；
- 用户下一条消息的情绪/明确反馈作为奖励更新后验（正向→α+1，负向→β+1，中性→各+0.5）；
- 后验按 scope 持久化（kv），让系统自己学会哄人而不是写死。

配置：memory.core.bandit.{enabled, alpha0, beta0}。
"""

import random
import time

from plugins import _db, _shared

STRATEGIES = [
    {"id": "empathy", "label": "共情优先",
     "hint": "先接住情绪再回应：简短共情一句，再给内容，别一上来就讲道理。"},
    {"id": "direct", "label": "直接给方案",
     "hint": "用户要的是解决：给明确、可执行的方案，少寒暄。"},
    {"id": "joke", "label": "玩梗减压",
     "hint": "语气轻松，可以带点玩笑和梗，把气氛带起来。"},
    {"id": "companion", "label": "陪伴倾听",
     "hint": "多听少说：顺着对方讲，少给建议，保持陪伴感。"},
    {"id": "distract", "label": "轻松转移",
     "hint": "对方不想陷在情绪里：自然地转移话题到轻松的事。"},
]


def _cfg(key, default):
    return _shared.core_cfg("bandit", key, default)
def _key(scope):
    return f"bandit:{scope or 'default'}"


def _last_key(scope):
    return f"bandit_last:{scope or 'default'}"


def _posterior(scope) -> dict:
    """每个策略的 Beta 后验参数与均值。"""
    data = _db.kv_get("memory", _key(scope)) or {}
    params = data.get("params") or {}
    a0 = float(_cfg("alpha0", 1.0))
    b0 = float(_cfg("beta0", 1.0))
    out = {}
    for s in STRATEGIES:
        p = params.get(s["id"]) or {}
        a = float(p.get("alpha", a0))
        b = float(p.get("beta", b0))
        out[s["id"]] = {"alpha": round(a, 3), "beta": round(b, 3),
                        "mean": round(a / (a + b), 3) if a + b else 0.5}
    return out


def select(scope, store=True) -> dict:
    """Thompson 采样：从每个策略 Beta 后验采样，取最大样本。"""
    post = _posterior(scope)
    best, best_v = None, -1.0
    for s in STRATEGIES:
        p = post[s["id"]]
        sample = random.betavariate(p["alpha"], p["beta"])
        if sample > best_v:
            best, best_v = s, sample
    assert best is not None
    out = {"id": best["id"], "label": best["label"], "hint": best["hint"],
           "mean": post[best["id"]]["mean"]}
    if store:
        _db.kv_set("memory", _last_key(scope), {"id": out["id"], "ts": time.time()})
    return out


def update(scope, reward=0.5) -> dict:
    """用上次选择的策略 + 本次奖励更新后验（reward∈[0,1]）。"""
    if not scope:
        return {"updated": False}
    last = _db.kv_get("memory", _last_key(scope)) or {}
    sid = last.get("id")
    if not sid:
        return {"updated": False}
    data = _db.kv_get("memory", _key(scope)) or {}
    params = data.get("params") or {}
    p = dict(params.get(sid) or {"alpha": float(_cfg("alpha0", 1.0)), "beta": float(_cfg("beta0", 1.0))})
    r = max(0.0, min(1.0, float(reward)))
    p["alpha"] = round(float(p.get("alpha", 1.0)) + r, 3)
    p["beta"] = round(float(p.get("beta", 1.0)) + (1.0 - r), 3)
    params[sid] = p
    _db.kv_set("memory", _key(scope), {"params": params, "ts": time.time()})
    return {"updated": True, "strategy": sid, "reward": r}


def reward_from_message(text, an=None) -> float:
    """从用户消息推导奖励：明确感谢/夸→1；嫌烦/骂→0；中性按效价。"""
    t = str(text or "")
    if any(w in t for w in ("谢谢", "多谢", "太棒了", "好厉害", "帮大忙", "有道理", "说到点子", "你说得对", "懂了")):
        return 1.0
    if any(w in t for w in ("别烦", "烦不烦", "闭嘴", "走开", "滚", "不好笑", "说人话", "答非所问", "别理我", "别管我")):
        return 0.0
    if an:
        try:
            v = float(an.get("valence", 0.0) or 0.0)
            return max(0.0, min(1.0, 0.5 + v))
        except (TypeError, ValueError):
            pass
    return 0.5


def status(scope="") -> dict:
    """策略后验一览（bandit-status 数据源）。"""
    post = _posterior(scope)
    last = _db.kv_get("memory", _last_key(scope)) or {}
    rows = [{"id": s["id"], "label": s["label"], **post[s["id"]]} for s in STRATEGIES]
    return {
        "scope": scope or "default",
        "strategies": sorted(rows, key=lambda x: -x["mean"]),
        "last": last.get("id"),
        "enabled": bool(_cfg("enabled", True)),
    }
