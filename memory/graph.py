"""事件图 + 事件树：把事实归类为事件，建关系边与时间线链，提供邻居/祖先/后代扩展。
“类型 → 事件 → 元素 → 关系/影响”落地为 events 表（etype/title/content）+ event_relations 邻接表；
rel=related_to 表示图谱关联，rel=follows 表示事件树/时间线先后。"""

from plugins import _db
from memory import embedder
from memory.extract import classify_event_type, extract_entities, fact_keywords, tokenize

LINK_THRESHOLD = 0.25
MAX_LINK_CANDIDATES = 50


def title_of(fact: str) -> str:
    """事件标题：事实的短摘要（与 _db.event_add 的截断保持一致）。"""
    fact = str(fact).strip()
    return fact if len(fact) <= 60 else fact[:60] + "…"


def _overlap(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / max(1, min(len(a), len(b)))


def build_for_fact(scope, key, fact, etype=None, importance=0.5, ts="", ts_source="approx", content=""):
    """把一个事实写成事件，并与同范围已有事件建边。返回 (event_id, linked_count)。"""
    fact = str(fact).strip()
    etype = etype or classify_event_type(fact)
    emb = None
    if embedder.enabled():
        vecs = embedder.embed([fact])
        emb = vecs[0] if vecs else None
    eid = _db.event_add(
        scope,
        key,
        etype,
        title_of(fact),
        content=content or fact,
        importance=float(importance),
        ts=ts,
        ts_source=ts_source,
        embedding=emb,
        memory_scope=scope,
        memory_key=key,
        memory_fact=fact,
    )
    linked = _link_to_existing(scope, key, eid, fact, emb)
    _link_previous(scope, key, eid)
    return eid, linked


def _link_to_existing(scope, key, eid, fact, emb=None) -> int:
    existing = _db.event_rows(scope, key, limit=MAX_LINK_CANDIDATES)
    tokens = fact_keywords(fact)
    connected = set()
    for e in _db.relations_for([eid]):
        connected.add(e["src"])
        connected.add(e["dst"])
    linked = 0
    for ev in existing:
        if ev["id"] == eid or ev["id"] in connected:
            continue
        sim = 0.0
        if emb and ev.get("embedding"):
            ev_vec = _db.vec_loads(ev["embedding"])
            if ev_vec:
                sim = embedder.cosine(emb, ev_vec)
        if sim == 0.0:
            ev_tokens = fact_keywords(ev.get("title") or "")
            sim = _overlap(tokens, ev_tokens)
        if sim >= LINK_THRESHOLD:
            _db.relation_add(eid, ev["id"], "related_to", weight=round(sim, 3))
            linked += 1
    return linked


def _link_previous(scope, key, eid):
    """事件树：把新事件接到同范围“最近一个已有事件”之后（rel=follows 时间线链）。"""
    existing = _db.event_rows(scope, key, limit=200)
    connected = set()
    for e in _db.relations_for([eid]):
        if e["rel"] == "follows":
            connected.add(e["src"])
            connected.add(e["dst"])
    prev = None
    for ev in existing:
        if ev["id"] == eid or ev["id"] in connected:
            continue
        if prev is None or ev["id"] > prev["id"]:
            prev = ev
    if prev is not None:
        _db.relation_add(prev["id"], eid, "follows", weight=1.0)


def link_follows(scope=None, key=None) -> int:
    """批量补全 follows 链（回填时调用，按事件 id 顺序连边，幂等）。"""
    rows = _db.event_rows(scope, key, limit=500)
    by_scope_key = {}
    for ev in rows:
        by_scope_key.setdefault((ev["scope"], ev["key"]), []).append(ev)
    linked = 0
    for (sc, k), evs in by_scope_key.items():
        evs.sort(key=lambda x: x["id"])
        for i in range(1, len(evs)):
            prev, cur = evs[i - 1], evs[i]
            edges = _db.relations_for([prev["id"], cur["id"]])
            if any(
                e["rel"] == "follows"
                and (
                    (e["src"] == prev["id"] and e["dst"] == cur["id"])
                    or (e["src"] == cur["id"] and e["dst"] == prev["id"])
                )
                for e in edges
            ):
                continue
            _db.relation_add(prev["id"], cur["id"], "follows", weight=1.0)
            linked += 1
    return linked


def _walk(event_ids, direction, rel=None, depth=2):
    """BFS 沿边扩展：direction=in 走入边（祖先），out 走出边（后代）。"""
    seen = set(event_ids)
    frontier = list(event_ids)
    for _ in range(depth):
        if not frontier:
            break
        edges = _db.relations_for(frontier, direction=direction)
        nxt = set()
        for e in edges:
            if rel and e["rel"] != rel:
                continue
            nid = e["dst"] if direction == "in" else e["src"]
            if nid not in seen:
                seen.add(nid)
                nxt.add(nid)
        frontier = list(nxt)
    return sorted(seen - set(event_ids))


def ancestors(event_ids, rel=None, depth=2):
    """事件树：沿入边向上走（更早发生的事件）。"""
    return _walk(event_ids, direction="in", rel=rel, depth=depth)


def descendants(event_ids, rel=None, depth=2):
    """事件树：沿出边向下走（更晚发生的事件）。"""
    return _walk(event_ids, direction="out", rel=rel, depth=depth)


def timeline(scope, key=None, limit=50):
    """事件时间线：从链头沿 follows 边走出的顺序事件（未成链的按 id 兜底追加）。"""
    evs = _db.event_rows(scope, key, limit=500)
    by_id = {ev["id"]: ev for ev in evs}
    incoming = set()
    for e in _db.relations_for(list(by_id)):
        if e["rel"] == "follows":
            incoming.add(e["dst"])
    heads = [eid for eid in by_id if eid not in incoming]
    order, seen = [], set()
    for head in heads:
        cur = head
        while cur and cur not in seen:
            seen.add(cur)
            order.append(by_id[cur])
            nxt = None
            for e in _db.relations_for([cur], direction="out"):
                if e["rel"] == "follows":
                    nxt = e["dst"]
                    break
            cur = nxt
    for ev in evs:
        if ev["id"] not in seen:
            order.append(ev)
    return order[:limit]


def neighbors(event_ids, depth=1, rel=None):
    """沿关系边扩展邻居事件 id（BFS，最多 depth 层；rel 过滤关系类型）。"""
    seen = set(event_ids)
    frontier = list(event_ids)
    for _ in range(depth):
        if not frontier:
            break
        edges = _db.relations_for(frontier)
        if rel:
            edges = [e for e in edges if e["rel"] == rel]
        nxt = set()
        for e in edges:
            for nid in (e["src"], e["dst"]):
                if nid not in seen:
                    seen.add(nid)
                    nxt.add(nid)
        frontier = list(nxt)
    return sorted(seen - set(event_ids))


def weighted_neighbors(event_ids, rel=None, max_depth=2) -> dict:
    """BFS 带深度衰减的邻居扩展：返回 {event_id: weight}，weight = 0.5**(depth-1)。"""
    result = {}
    frontier = list(event_ids)
    seen = set(event_ids)
    for depth in range(1, max_depth + 1):
        if not frontier:
            break
        edges = _db.relations_for(frontier)
        if rel:
            edges = [e for e in edges if e["rel"] == rel]
        nxt = set()
        for e in edges:
            for nid in (e["src"], e["dst"]):
                if nid not in seen:
                    seen.add(nid)
                    nxt.add(nid)
                    result[nid] = 0.5 ** (depth - 1)
        frontier = list(nxt)
    return result


def shortest_path(src, dst, max_depth=4):
    """BFS 找事件间最短路径，返回事件 id 列表；不可达返回 None。"""
    if src == dst:
        return [src]
    from collections import deque
    prev, seen = {}, {src}
    q = deque([src])
    while q:
        cur = q.popleft()
        if len(prev) > max_depth * 4:
            break
        for e in _db.relations_for([cur]):
            nid = e["dst"] if e["src"] == cur else e["src"]
            if nid in seen:
                continue
            seen.add(nid)
            prev[nid] = cur
            if nid == dst:
                path, node = [dst], dst
                while node != src:
                    node = prev[node]
                    path.append(node)
                return list(reversed(path))
            q.append(nid)
    return None


def cleanup_orphans(min_importance=0.2, limit=200) -> int:
    """清理低价值且无关联边的孤立事件（回填/维护时调用）。"""
    removed = 0
    for ev in _db.event_rows(min_importance=min_importance, limit=limit):
        if not _db.relations_for([ev["id"]]):
            _db.event_delete(ev["id"])
            removed += 1
    return removed


REL_TYPE_RULES = {
    ("规划", "学习"): "supports",
    ("学习", "项目"): "supports",
    ("项目", "经历"): "influences",
    ("经历", "规划"): "influences",
    ("项目", "工作"): "supports",
    ("偏好", "娱乐"): "supports",
    ("家庭", "健康"): "supports",
    ("健康", "工作"): "opposes",
}


def tag_relations(scope=None) -> int:
    """给 related_to 边补类型边（supports/influences/opposes），保留原边。返回补边数。"""
    evs = _db.event_rows(scope, limit=1000)
    by_id = {e["id"]: e for e in evs}
    tagged = 0
    for ev in evs:
        for e in _db.relations_for([ev["id"]]):
            if e["rel"] != "related_to":
                continue
            other_id = e["dst"] if e["src"] == ev["id"] else e["src"]
            other = by_id.get(other_id)
            if not other:
                continue
            rel = REL_TYPE_RULES.get((ev["etype"], other["etype"])) or REL_TYPE_RULES.get(
                (other["etype"], ev["etype"])
            )
            if not rel:
                continue
            _db.relation_add(ev["id"], other_id, rel, weight=e.get("weight", 1.0))
            tagged += 1
    return tagged


# ===== 实体归一：事件标题抽实体，同实体不同写法合并（canonical + 别名），事件挂实体 =====
def _entity_name_of(title) -> str:
    from memory.topic import GENERIC_ENTITIES
    ents = extract_entities(title or "")
    specific = [e for e in ents if e.lower() not in GENERIC_ENTITIES]
    return specific[0] if specific else (ents[0] if ents else "")


def entity_for(scope, key, name) -> int:
    """找/建 canonical 实体：词元重叠 ≥0.6 视为同一实体。"""
    if not name:
        return _db.entity_add(scope, key, "未命名")
    existing = _db.entity_rows(scope, key)
    best, best_sim = None, 0.0
    for e in existing:
        a, b = set(tokenize(name)), set(tokenize(e["canonical"]))
        sim = len(a & b) / max(1, min(len(a), len(b)))
        if sim > best_sim:
            best, best_sim = e, sim
    if best and best_sim >= 0.6:
        if name != best["canonical"]:
            _db.entity_alias_add(best["id"], name)
        return best["id"]
    return _db.entity_add(scope, key, name)


def build_entities(scope=None) -> int:
    """为事件建实体关联。返回处理事件数。"""
    linked = 0
    for ev in _db.event_rows(scope, limit=1000):
        name = _entity_name_of(ev["title"])
        if not name:
            continue
        eid = entity_for(ev["scope"], ev["key"], name)
        _db.entity_events_add(eid, ev["id"])
        linked += 1
    return linked


def entity_count() -> int:
    return len(_db.entity_rows())
