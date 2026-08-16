"""词法检索：真分词 BM25（jieba/二元组 + 倒排索引）+ FTS5 trigram + LIKE 降级。
BM25 索引存 SQLite（bm25_terms / bm25_docs），写入时同步、grow 时全量重建。"""

import math
import time

from plugins import _db
from memory.extract import fact_keywords, tokenize

# ===== 真分词 BM25 倒排（k1=1.5, b=0.75）=====
k1 = 1.5
b = 0.75
_stats_cache = {"ts": 0.0, "data": None}


def _bm25_stats():
    if time.time() - _stats_cache["ts"] < 30 and _stats_cache["data"]:
        return _stats_cache["data"]
    data = _db.bm25_stats()
    _stats_cache.update({"ts": time.time(), "data": data})
    return data


def bm25_sync(scope, key, facts):
    """为 scope+key 的事实重建倒排（写入记忆后调用）。"""
    _db.bm25_sync(scope, key, [(f, tokenize(f)) for f in facts])


def bm25_upsert(scope, key, facts):
    """增量更新给定事实的词项（ingest 新增事实时调用）。"""
    if facts:
        _db.bm25_upsert(scope, key, [(f, tokenize(f)) for f in facts])
        _stats_cache["ts"] = 0.0


def bm25_rebuild() -> int:
    """全量重建 BM25 倒排。返回索引文档数。"""
    _db.bm25_clear()
    rows = _db.memory_rows()
    by_scope_key: dict = {}
    for r in rows:
        by_scope_key.setdefault((r["scope"], r["key"]), []).append(r["fact"])
    for (sc, k), facts in by_scope_key.items():
        # 传纯 fact 列表，bm25_sync 内部自行 tokenize；预 tokenize 会导致 fact 被存成 tuple 字符串
        bm25_sync(sc, k, facts)
    _stats_cache["ts"] = 0.0
    return len(rows)


def bm25_search(query, scopes, limit=10):
    """BM25 检索：返回 [{scope,key,fact,score}]（score 归一化 0~1）。"""
    terms = list(dict.fromkeys(tokenize(query)))
    if not terms:
        return []
    stats = _bm25_stats()
    n = int(stats["n"])
    avgdl = float(stats["avgdl"]) or 1.0
    if n == 0:
        return []
    postings = _db.bm25_postings(terms, scopes)
    if not postings:
        return []
    by_term = {}
    for p in postings:
        by_term.setdefault(p["term"], []).append(p)
    keys = [(p["scope"], p["key"], p["fact"]) for p in postings]
    dl = _db.bm25_doc_lens(keys)
    scores = {}
    for term, plist in by_term.items():
        df = len(plist)
        idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
        for p in plist:
            key = (p["scope"], p["key"], p["fact"])
            dl_f = dl.get(key, 0) or len(tokenize(p["fact"]))
            tf = int(p["tf"])
            norm = tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl_f / avgdl))
            scores[key] = scores.get(key, 0.0) + idf * norm
    ranked = sorted(scores.items(), key=lambda x: -x[1])[:max(1, int(limit))]
    if not ranked:
        return []
    top = ranked[0][1]
    return [
        {"scope": sc, "key": k, "fact": f, "score": round(s / top, 4)}
        for (sc, k, f), s in ranked
    ]


# ===== 词法检索主通道 =====
RULES = [
    # (触发词列表, 属性名, 问题语义)
    (["喜欢什么", "喜欢吃什么", "爱吃什么", "口味", "最爱"], "preference", "preference"),
    (["喜欢喝", "喝什么"], "preference", "preference"),
    (["宠物", "养了什么", "有猫", "有狗", "猫", "狗"], "family", "family"),
    (["住哪", "住在", "哪里人", "老家"], "family", "family"),
    (["工作", "上班", "公司", "做什么的", "职业"], "work", "work"),
    (["身体", "健康", "生病", "医院"], "health", "health"),
    (["游戏", "动漫", "电影", "爱好"], "hobby", "hobby"),
]


def available() -> bool:
    return _db.fts_available()


def rule_match(query: str):
    """Rule 模板直查：结构化属性问题的确定性匹配（快且准）。返回 (attr, 描述) 或 None。"""
    q = query or ""
    for words, attr, label in RULES:
        if any(w in q for w in words):
            return attr, label
    return None


def rule_search(query, scopes, limit=5):
    """结构化属性直查：返回 [{scope, key, fact, score}]。"""
    hit = rule_match(query)
    if not hit:
        return []
    attr, _label = hit
    out = []
    for scope in scopes:
        for r in _db.attr_rows(scope, attr=attr):
            out.append(
                {
                    "scope": scope,
                    "key": r["key"],
                    "fact": r["value"],
                    "score": 1.0,
                }
            )
    return out[:limit]


# 兼容旧名
match = rule_match


def search(query, scopes, limit=10):
    """BM25 优先；短查询/无 FTS 时降级 LIKE。返回 [{scope,key,fact,score}]，score 0~1。"""
    q = str(query or "").strip()
    if not q:
        return []
    # 主通道：真分词 BM25
    rows = bm25_search(q, scopes, limit=max(1, int(limit)))
    if rows:
        return rows
    # 回退：FTS5 trigram（长查询精确子串）
    rows = _db.lexicon_search(q, scopes, limit=max(1, int(limit)))
    if rows:
        ranks = [r["rank"] for r in rows]
        lo, hi = min(ranks), max(ranks)
        span = hi - lo if hi > lo else 1.0
        out = []
        for r in rows:
            score = 1.0 - (r["rank"] - lo) / span
            out.append(
                {
                    "scope": r["scope"],
                    "key": r["key"],
                    "fact": r["fact"],
                    "score": max(0.0, min(1.0, score)),
                }
            )
        return out
    # 降级：LIKE 子串命中
    out = []
    for scope in scopes or [None]:
        for r in _db.memory_search(q, scope=scope, limit=max(1, int(limit))):
            out.append(
                {
                    "scope": r["scope"],
                    "key": r["key"],
                    "fact": r["fact"],
                    "score": 1.0 if q in r["fact"] else 0.5,
                }
            )
    out.sort(key=lambda x: -x["score"])
    # 词元重叠兜底：FTS/LIKE 都没命中时，按关键词覆盖率打分
    qt = fact_keywords(q)
    if not out and qt:
        rows = _db.memory_rows() if not scopes else []
        for scope in scopes or [None]:
            for r in (_db.memory_rows(scope) if scope else rows):
                ft = fact_keywords(r["fact"])
                if ft & qt:
                    out.append(
                        {
                            "scope": r["scope"],
                            "key": r["key"],
                            "fact": r["fact"],
                            "score": round(len(ft & qt) / len(qt), 3),
                        }
                    )
        out.sort(key=lambda x: -x["score"])
    seen, final = set(), []
    for r in out:
        if r["fact"] not in seen:
            seen.add(r["fact"])
            final.append(r)
    return final[:limit]
