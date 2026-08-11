"""双轨制一致性（v2.2）：纠错后的状态重算——
关系降 trust、议题降权、失效队列惰性重算（避开热路径扫描）。
"""


def reconcile(scope, key, fact, reason="") -> dict:
    """对一条失效事实做状态重算（纠错后调用）。"""
    changed = {}
    try:
        from memory import relationship as rel_mod
        rel_mod.update(scope, subject=key or scope, event="dispute", detail=f"纠错:{str(fact)[:40]}")
        changed["relationship"] = 1
    except Exception:
        pass
    try:
        from memory import topic as topic_mod
        topic_mod.invalidate_for_fact(scope, key, fact)
        changed["topic"] = 1
    except Exception:
        pass
    return changed


def reconcile_pending(limit=100) -> dict:
    """惰性重算：消费失效队列（assemble_context 开头调用，量小不热）。"""
    from plugins import _db
    rows = _db.invalidation_rows(limit)
    n = 0
    for r in rows:
        reconcile(r.get("scope", ""), r.get("key", ""), r.get("fact", ""), r.get("reason", ""))
        n += 1
    if rows:
        _db.invalidation_clear_all()
    return {"reconciled": n}
