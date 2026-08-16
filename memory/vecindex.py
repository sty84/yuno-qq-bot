"""自研 IVF 向量索引（SQLite 持久化）：k-means 聚类 + nprobe 探测 + 余弦打分。
无新依赖、弱 CPU 可跑；要升级 sqlite-vec / FAISS / LanceDB 时只改本文件与 reasoning 的调用。"""

import random

from plugins import _db, _shared
from memory import embedder


def _cfg(key, default):
    return _shared.core_cfg("vector_index", key, default)
def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def _kmeans(vectors, nlist, max_iter=20, seed=42):
    """kmeans++ 初始化 + 迭代（确定性随机）。返回 (centroids, assign)。"""
    rng = random.Random(seed)
    n = len(vectors)
    nlist = max(1, min(nlist, n))
    centroids = [vectors[rng.randrange(n)]]
    for _ in range(nlist - 1):
        dists = [1.0 - max(_cosine(v, c) for c in centroids) for v in vectors]
        total = sum(dists)
        if total <= 0:
            centroids.append(vectors[rng.randrange(n)])
            continue
        r = rng.random() * total
        acc, pick = 0.0, n - 1
        for i, d in enumerate(dists):
            acc += d
            if acc >= r:
                pick = i
                break
        centroids.append(vectors[pick])
    assign = [0] * n
    for _ in range(max_iter):
        changed = False
        for i, v in enumerate(vectors):
            best = max(range(nlist), key=lambda j: _cosine(v, centroids[j]))
            if assign[i] != best:
                assign[i] = best
                changed = True
        if not changed:
            break
        for j in range(nlist):
            members = [vectors[i] for i in range(n) if assign[i] == j]
            if not members:
                continue
            dim = len(members[0])
            mean = [sum(m[k] for m in members) / len(members) for k in range(dim)]
            norm = sum(x * x for x in mean) ** 0.5 or 1.0
            centroids[j] = [x / norm for x in mean]
    return centroids, assign


def build(nlist=None, scope=None) -> dict:
    """用现有带向量的记忆重建索引。返回 {n, nlist, dim} 或 {error}。"""
    rows = [r for r in _db.memory_rows(scope) if _db.vec_loads(r.get("embedding"))]
    if not rows:
        return {"error": "没有带向量的记忆，先启用 embedder 并回填（tools.py memory-embed）"}
    vectors = [_db.vec_loads(r["embedding"]) for r in rows]
    dim = len(vectors[0])
    # v31：nlist 随数据量自动缩放（√n，夹在 4~128）；配置 >0 时以配置为准
    nlist = nlist or int(_cfg("nlist", 0))
    if nlist <= 0:
        nlist = max(4, min(128, int(len(vectors) ** 0.5)))
    centroids, assign = _kmeans(vectors, nlist)
    _db.vec_clear()
    _db.vec_centroids_set([(c, assign.count(i)) for i, c in enumerate(centroids)])
    _db.vec_index_replace(
        [(r["scope"], r["key"], r["fact"], assign[i], vectors[i]) for i, r in enumerate(rows)]
    )
    try:
        _db.pgvector_build(
            [(r["scope"], r["key"], r["fact"], vectors[i]) for i, r in enumerate(rows)]
        )
    except Exception as e:
        _stats_err(e)
    return {"n": len(rows), "nlist": len(centroids), "dim": dim}


def enabled(scope=None) -> bool:
    try:
        return _db.vec_index_count() > 0 and bool(_db.vec_centroids_get())
    except Exception as e:
        _stats_err(e)
        return False


def backend_name() -> str:
    """当前向量后端：ivf（自研）/ sqlite_vec / faiss。
    装好对应库后自动切换（迁移入口：build/search 两个函数）。"""
    try:
        import sqlite_vec  # noqa: F401
        return "sqlite_vec"
    except Exception as e:
        _stats_err(e)
        pass
    try:
        import faiss  # noqa: F401
        return "faiss"
    except Exception as e:
        _stats_err(e)
        return "ivf"


def search(query_vec, scopes, top_k=5, nprobe=None) -> list:
    """优先 pgvector 原生检索；不可用时回退自研 IVF。"""
    try:
        pg_rows = _db.pgvector_search(query_vec, scopes, top_k=top_k)
        if pg_rows:
            return pg_rows
    except Exception as e:
        _stats_err(e)
    centroids = _db.vec_centroids_get()
    if not centroids:
        return []
    # v31：nprobe 随质心数自动缩放（√nlist）；配置 >0 时以配置为准
    nprobe = nprobe or int(_cfg("nprobe", 0))
    if nprobe <= 0:
        nprobe = max(1, min(len(centroids), int(len(centroids) ** 0.5)))
    scored = sorted(
        ((_cosine(query_vec, c["embedding"]), c["id"]) for c in centroids),
        key=lambda x: -x[0],
    )
    probe_ids = [cid for _s, cid in scored[: max(1, nprobe)]]
    out = []
    for r in _db.vec_index_by_centroid(probe_ids):
        if scopes and r["scope"] not in scopes:
            continue
        vec = _db.vec_loads(r["embedding"])
        if vec:
            out.append(
                {
                    "scope": r["scope"],
                    "key": r["key"],
                    "fact": r["fact"],
                    "score": _cosine(query_vec, vec),
                }
            )
    out.sort(key=lambda x: -x["score"])
    return out[:top_k]


def upsert(scope, key, fact, embedding=None) -> bool:
    """增量写一条向量索引（归到最近质心）。无质心/无向量时跳过（等下次 build 全量重建）。"""
    if not enabled(scope) or embedding is None:
        return False
    centroids = _db.vec_centroids_get()
    if not centroids:
        return False
    best = max(centroids, key=lambda c: _cosine(embedding, c["embedding"]))
    _db.vec_index_upsert(scope, key, fact, best["id"], embedding)
    return True


def tune(probes, nlists=(4, 8, 16), nprobes=(1, 2, 4), top_k=5) -> dict:
    """对照实验：不同 nlist/nprobe 下用评测集算 recall@k，返回最佳组合并恢复最优索引。"""
    results = []
    for nlist in nlists:
        build(nlist=nlist)
        for nprobe in nprobes:
            hit = 0
            for p in probes:
                vecs = embedder.embed([p["query"]])
                if not vecs:
                    continue
                scopes = [p["scope"]] if p.get("scope") else None
                hits = search(vecs[0], scopes, top_k=top_k, nprobe=nprobe)
                if any(any(e in h["fact"] or h["fact"] in e for e in p["expected"]) for h in hits):
                    hit += 1
            results.append(
                {
                    "nlist": nlist,
                    "nprobe": nprobe,
                    "recall@k": round(hit / max(1, len(probes)), 3),
                }
            )
    best = max(results, key=lambda r: r["recall@k"])
    build(nlist=best["nlist"])
    return {"results": results, "best": best}



def _stats_err(e):
    """裸 except 审计（v2.2）：错误计数 + 日志，供消融/排查。"""
    try:
        import memory.stats as _st
        _st.bump_err("vecindex", e)
    except Exception:
        pass
