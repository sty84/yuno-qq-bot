"""程序记忆层（System 1 习惯表）：情境模式 → 动作 → 成功率/使用次数。

- System 1：命中高激活习惯（tries≥3 且成功率≥阈值）→ 直接复用动作，省一次 LLM 调用；
- System 2：没命中 → 走正常深思（agent.ask 原有路径）；
- 学习：用户在 after_chat 的 praise/纠正 反馈会更新对应（情境→回复）的成功率，
  让 AI 学会"这类情况这么说有效、那么说会挨骂"。
"""

import time
from datetime import datetime

from plugins import _db, _shared


def _cfg(key, default):
    return _shared.core_cfg("mind", key, default)
def _tokens(text) -> set:
    try:
        from memory import extract
        return set(extract.tokenize(str(text or "")))
    except Exception as e:
        _stats_err(e)
        t = str(text or "")
        return {x for x in t.replace("，", " ").replace("。", " ").replace("？", " ").split() if len(x) >= 2}


_match_cache = {"ts": 0.0, "text": "", "result": None}


def learn(scope, situation, action, success):
    """记录一次结果：situation = 用户消息，action = AI 回复（或行为），success ∈ {0,1}。"""
    try:
        situation = str(situation or "")[:120]
        action = str(action or "")[:400]
        if not situation or not action:
            return
        _db.procedure_upsert(
            situation, action, 1.0 if float(success) >= 0.5 else 0.0,
            datetime.now().isoformat(timespec="seconds"),
        )
    except Exception as e:
        _stats_err(e)
        pass


def match(text, scope=None) -> dict | None:
    """System 1 查找：词元重叠 ≥ 阈值 且 成功率达标 的习惯。返回 {situation, action, success, tries}。"""
    try:
        import memory.stats as _st
        _st.bump("tick:procedures")
    except Exception as e:
        _stats_err(e)
    if not _cfg("system1", True) or not str(text or "").strip():
        return None
    ttl = float(_cfg("system1_cache_ttl", 30))
    if _match_cache["text"] == text and time.time() - _match_cache["ts"] < ttl:  # type: ignore[operator]
        r = _match_cache["result"]
        return dict(r) if r else None  # type: ignore[call-overload]
    qt = _tokens(text)
    if not qt:
        return None
    min_tries = int(_cfg("system1_min_tries", 3))
    min_success = float(_cfg("system1_min_success", 0.75))
    min_overlap = float(_cfg("system1_min_overlap", 0.5))
    best = None
    for r in _db.procedure_rows(min_tries=min_tries, limit=200):
        st = _tokens(r["situation"])
        if not st:
            continue
        overlap = len(qt & st) / max(1, len(qt))
        if overlap < min_overlap:
            continue
        if float(r.get("success", 0.0)) < min_success:
            continue
        score = float(r.get("success", 0.0)) * overlap
        if best is None or score > best[0]:
            best = (score, r)
    if best is None:
        _match_cache.update({"ts": time.time(), "text": text, "result": None})
        return None
    out = dict(best[1])
    _match_cache.update({"ts": time.time(), "text": text, "result": out})
    return dict(out)


def stats() -> dict:
    rows = _db.procedure_rows()
    if not rows:
        return {"count": 0}
    good = [r for r in rows if float(r.get("success", 0)) >= 0.75 and int(r.get("tries", 0)) >= 3]
    return {
        "count": len(rows),
        "system1_ready": len(good),
        "avg_success": round(sum(float(r.get("success", 0.0)) for r in rows) / len(rows), 3),
    }


def report() -> str:
    rows = _db.procedure_rows()
    if not rows:
        return "程序记忆为空（还没有学出习惯）"
    lines = [f"共 {len(rows)} 条习惯："]
    for r in rows[:30]:
        lines.append(
            f"- 情境「{r['situation'][:40]}」→ 动作「{r['action'][:40]}」"
            f"（成功率 {r['success']:.0%}，{r['tries']} 次）"
        )
    return "\n".join(lines)



def _stats_err(e):
    """裸 except 审计（v2.2）：错误计数 + 日志，供消融/排查。"""
    try:
        import memory.stats as _st
        _st.bump_err("procedures", e)
    except Exception:
        pass
