"""记忆更新：相似合并（不重复堆叠）、确认刷新、反驳/废弃替换。"""

from datetime import datetime

from plugins import _db
from memory import embedder
from memory.extract import fact_keywords


def _similarity(a: str, b: str, a_vec=None, b_vec=None) -> float:
    if a_vec and b_vec:
        return embedder.cosine(a_vec, b_vec)
    ta, tb = fact_keywords(a), fact_keywords(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(1, min(len(ta), len(tb)))


def find_near_dup(scope, key, fact, rows=None, threshold=0.9):
    """在现有记忆里找近似重复（合并用，避免事实堆叠）。返回重复 fact 或 None。"""
    rows = rows if rows is not None else _db.memory_rows(scope, key)
    a_vec = None
    if embedder.enabled():
        vecs = embedder.embed([fact])
        a_vec = vecs[0] if vecs else None
    best, best_score = None, 0.0
    for r in rows:
        if r["fact"] == fact:
            continue
        b_vec = _db.vec_loads(r.get("embedding"))
        s = _similarity(fact, r["fact"], a_vec, b_vec)
        if s > best_score:
            best, best_score = r["fact"], s
    return best if best_score >= threshold else None


def refresh(scope, key, fact, confidence=None, source="refresh"):
    """记忆更新：确认即刷新（时间戳 + 可信度只增不减）。"""
    row = next((r for r in _db.memory_rows(scope, key) if r["fact"] == fact), None)
    cur = float(row.get("confidence", 0.7)) if row else 0.7
    conf = max(cur, float(confidence or cur))
    _db.memory_add(
        scope,
        key,
        fact,
        datetime.now().isoformat(timespec="seconds"),
        None,
        conf,
        source,
        audience=(row or {}).get("audience", ""),
        speaker=(row or {}).get("speaker", ""),
        mclass=(row or {}).get("mclass") or "short",
        arousal=float((row or {}).get("arousal", 0.0)),
        valence=float((row or {}).get("valence", 0.0)),
    )
    return conf


def supersede(scope, key, fact):
    """废弃替换：把旧事实可信度压到 0.05（不再被召回），由新事实替代。"""
    _db.memory_set_confidence(scope, key, fact, 0.05)


def publicize(scope, key, fact):
    """公开一条记忆：audience=public，任何场景可召回（用户明确“可以告诉他们”）。"""
    row = next((r for r in _db.memory_rows(scope, key) if r["fact"] == fact), None)
    if not row:
        return None
    _db.memory_add(
        scope,
        key,
        fact,
        updated_at=row.get("updated_at") or "",
        confidence=float(row.get("confidence", 0.7)),
        source=row.get("source", ""),
        audience="public",
        speaker=row.get("speaker", ""),
        mclass=row.get("mclass") or "short",
        arousal=float(row.get("arousal", 0.0)),
        valence=float(row.get("valence", 0.0)),
    )
    return fact
