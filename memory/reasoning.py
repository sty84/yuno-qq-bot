"""推理层：多算法并存（BM25 词法 / 向量 / 图谱 / 结构化属性）+ 按需路由 + RRF 融合。

每个算法独立产出有序候选，再用 Reciprocal Rank Fusion 融合（可配置权重），
最后叠加 Memory Policy（重要度/时效）与可信度。换新算法 = 往 _ranked_lists 加一个分支。"""

import time
from datetime import datetime

from plugins import _db, _shared
from memory import embedder, extract, graph, lexical, policy, topic, trace, vecindex
from memory.extract import fact_keywords, tokenize

_query_cache = {"ts": 0.0, "text": "", "vec": None}
_route_cache = None
_route_flush_ts = {"ts": 0.0}
RRF_K = 60
_event_time_cache = {"key": None, "map": {}, "ts": 0.0}


def _event_time_map(scopes) -> dict:
    """fact → (事件 ts, ts_source)，供时间窗口加权（时间当元数据，不污染事实文本）。"""
    key = tuple(sorted(scopes or []))
    if _event_time_cache["key"] == key and time.time() - _event_time_cache["ts"] < 60:
        return _event_time_cache["map"]
    m = {}
    for scope in scopes or []:
        try:
            for ev in _db.event_rows(scope, limit=3000):
                mf = str(ev.get("memory_fact") or "")
                if mf:
                    m.setdefault(mf, (str(ev.get("ts") or ""), str(ev.get("ts_source") or "approx")))
        except Exception as e:
            _stats_err(e)
    _event_time_cache.update({"key": key, "map": m, "ts": time.time()})
    return m


def _cfg(key, default):
    core = _shared.CONFIG.get("memory", {}).get("core", {}) or {}
    return core.get(key, default)


def _weights() -> dict:
    w = _cfg("weights", {}) or {}
    return {
        "lexical": float(w.get("lexical", 0.6)),
        "vector": float(w.get("vector", 0.7)),
        "graph": float(w.get("graph", 0.4)),
        "structured": float(w.get("structured", 0.3)),
        "rules": float(w.get("rules", 0.5)),
        "topics": float(w.get("topics", 0.4)),
        "policy": float(w.get("policy", 0.5)),
        "confidence": float(w.get("confidence", 0.3)),
    }


def _query_vec(text):
    """查询向量（按文本节流缓存，省 API 调用）。"""
    global _query_cache
    throttle = float(_cfg("throttle_s", 60))
    if _query_cache["text"] == text and time.time() - _query_cache["ts"] < throttle:
        return _query_cache["vec"]
    vecs = embedder.embed([text])
    vec = vecs[0] if vecs else None
    _query_cache = {"ts": time.time(), "text": text, "vec": vec}
    return vec


def _plan(query_text) -> dict:
    """按需调用：根据查询理解调整各算法权重（乘数）。"""
    ud = extract.understand(query_text)
    m = {"lexical": 1.0, "vector": 1.0, "graph": 1.0, "structured": 1.0, "rules": 1.0, "topics": 1.0}
    if ud["intent"] == "attribute":
        m = {"lexical": 0.6, "vector": 0.5, "graph": 0.2, "structured": 2.0, "rules": 1.6, "topics": 0.6}
    elif ud["time_hint"]:
        m = {"lexical": 0.6, "vector": 0.4, "graph": 1.6, "structured": 0.4, "rules": 0.4, "topics": 1.2}
    elif ud["short"]:
        m = {"lexical": 1.6, "vector": 0.5, "graph": 0.4, "structured": 0.5, "rules": 0.6, "topics": 0.8}
    m = _adaptive_plan(m)
    return m


def _route_stats() -> dict:
    global _route_cache
    if _route_cache is None:
        _route_cache = _db.kv_get("memory", "route_stats", {}) or {}
    return _route_cache


def _flush_route_stats():
    """节流落盘：最多每 30 秒写一次 kv（高频群聊减少 SQLite 写）。"""
    if time.time() - _route_flush_ts["ts"] >= 30:
        _route_flush_ts["ts"] = time.time()
        _db.kv_set("memory", "route_stats", _route_cache)


def _adaptive_plan(plan) -> dict:
    """自适应路由：按历史“哪类算法命中率高”微调乘数（0.5~1.5），学出来的按需调用。"""
    stats = _route_stats()
    rates = {
        algo: s["hits"] / s["trials"]
        for algo, s in stats.items()
        if s.get("trials", 0) >= 5 and s.get("hits", 0) >= 0
    }
    if not rates:
        return plan
    avg = sum(rates.values()) / len(rates)
    out = dict(plan)
    for algo, rate in rates.items():
        if algo in out:
            out[algo] = min(1.5, max(0.5, 1.0 + (rate - avg) * 1.5))
    return out


def _record_route(lists, hits):
    """把本次各算法的 trial/hit 累积进 kv（自适应路由的反馈）。"""
    stats = _route_stats()
    hit_facts = {f for f, _s, _sc in hits}
    for algo, lst in lists.items():
        if not lst:
            continue
        s = stats.setdefault(algo, {"trials": 0, "hits": 0})
        s["trials"] += 1
        if any(f in hit_facts for f in lst[:3]):
            s["hits"] += 1
    _flush_route_stats()


def _rerank_cfg() -> dict:
    r = _cfg("rerank", {}) or {}
    return {"enabled": bool(r.get("enabled", True)), "mode": str(r.get("mode", "light"))}


def _graph_boost_facts(query_text, scopes) -> dict:
    """图谱：查询命中的种子事件扩展。语义边（related_to）权重 1.0，时间线（follows）权重 0.5。"""
    boost = {}
    qt = fact_keywords(query_text)
    for scope in scopes:
        evs = _db.event_rows(scope, limit=200)
        if not evs:
            continue
        seed = [
            ev["id"]
            for ev in evs
            if (qt and (fact_keywords(ev.get("title") or "") & qt))
            or (query_text in (ev.get("title") or ""))
        ]
        if not seed:
            continue
        for nid, w in graph.weighted_neighbors(seed, rel="related_to", max_depth=2).items():
            for ev in evs:
                if ev["id"] == nid:
                    boost[ev["title"]] = max(boost.get(ev["title"], 0.0), w)
                    break
        for nid in graph.descendants(seed, rel="follows", depth=1):
            for ev in evs:
                if ev["id"] == nid:
                    boost[ev["title"]] = max(boost.get(ev["title"], 0.0), 0.5)
                    break
    return boost


def _structured_facts(query_text, scopes, limit):
    """结构化属性：attr/value 命中查询词的属性值（即事实文本）。"""
    out, seen = [], set()
    qt = fact_keywords(query_text)
    if not qt:
        return out
    for scope in scopes:
        for r in _db.attr_rows(scope):
            if r["value"] in seen:
                continue
            if fact_keywords(r["value"] + " " + r["attr"]) & qt:
                seen.add(r["value"])
                out.append(r["value"])
    return out[:limit]


def _ranked_lists(query_text, scopes, top_k, weights, plan) -> dict:
    """每个算法独立产出有序候选（供 RRF）。"""
    lists = {"lexical": [], "vector": [], "graph": [], "structured": [], "rules": [], "topics": []}
    seen = {k: set() for k in lists}

    if weights["lexical"] > 0 and plan["lexical"] > 0:
        for r in lexical.search(query_text, scopes, limit=top_k * 3):
            if r["fact"] not in seen["lexical"]:
                seen["lexical"].add(r["fact"])
                lists["lexical"].append(r["fact"])

    if weights["vector"] > 0 and plan["vector"] > 0:
        qvec = _query_vec(query_text)
        if qvec:
            if vecindex.enabled():
                for r in vecindex.search(qvec, scopes, top_k * 3):
                    if r["fact"] not in seen["vector"]:
                        seen["vector"].add(r["fact"])
                        lists["vector"].append(r["fact"])
            else:
                scored = []
                for scope in scopes:
                    for r in _db.memory_rows(scope):
                        if r["fact"] in seen["vector"]:
                            continue
                        seen["vector"].add(r["fact"])
                        vec = _db.vec_loads(r.get("embedding"))
                        if vec:
                            scored.append((embedder.cosine(qvec, vec), r["fact"]))
                scored.sort(key=lambda x: -x[0])
                lists["vector"] = [f for _s, f in scored[: top_k * 3]]

    if weights["graph"] > 0 and plan["graph"] > 0:
        boost = _graph_boost_facts(query_text, scopes)
        lists["graph"] = [f for f, _w in sorted(boost.items(), key=lambda x: -x[1])]

    if weights["structured"] > 0 and plan["structured"] > 0:
        lists["structured"] = _structured_facts(query_text, scopes, top_k * 2)

    if weights["rules"] > 0 and plan["rules"] > 0:
        for r in lexical.rule_search(query_text, scopes, limit=top_k * 2):
            if r["fact"] not in seen["rules"]:
                seen["rules"].add(r["fact"])
                lists["rules"].append(r["fact"])

    if weights["topics"] > 0 and plan["topics"] > 0:
        for t in topic.search(query_text, scopes, limit=top_k * 2):
            for p in t.get("params", []):
                if p["param"] == "fact" and p["value"] not in seen["topics"]:
                    seen["topics"].add(p["value"])
                    lists["topics"].append(p["value"])

    return lists


def _visible(row, scopes) -> bool:
    """场景可见性：私聊记忆不进群聊；群记忆只进本群；public 全场景；AI 自身记忆恒可见。"""
    if row["scope"] == "ai" or row["scope"].startswith("ai:"):
        return True
    aud = row.get("audience") or ""
    if not aud:
        if row["scope"].startswith("c2c:"):
            aud = "private"
        elif row["scope"].startswith("group:"):
            aud = "group:" + row["scope"].split(":", 1)[1]
        elif row["scope"].startswith("group_all:"):
            aud = "group:" + row["scope"].split(":", 1)[1]
        else:
            return True
    if aud == "public":
        return True
    scene = _scene_kind(scopes)
    if scene == "private":
        return aud == "private" or aud.startswith("group:")
    if scene and scene[0] == "group":
        return aud == f"group:{scene[1]}"
    return True


def _scene_kind(scopes):
    if any(s.startswith("c2c:") for s in scopes):
        return "private"
    for s in scopes:
        if s.startswith("group:") or s.startswith("group_all:"):
            return ("group", s.split(":", 1)[1])
    return None


_result_cache = {"ts": 0.0, "key": None, "hits": None}
_last_details = {}


def _cache_cfg() -> dict:
    c = _cfg("cache", {}) or {}
    return {"enabled": bool(c.get("enabled", True)), "ttl": float(c.get("ttl_s", 60))}


def _retrieve_single(query_text, scopes, top_k=5, min_score=0.25, extra_scopes=None, use_cache=True, location=None, window=None):
    """单查询检索（含结果缓存）：返回 [(fact, score, scope)]（score 已归一化 0~1）。"""
    if not query_text or not scopes:
        return []
    cc = _cache_cfg()
    cache_key = hash((query_text, tuple(scopes), tuple(extra_scopes or []), top_k, min_score, location, window))
    if use_cache and cc["enabled"] and _result_cache["key"] == cache_key:
        if time.time() - _result_cache["ts"] < cc["ttl"]:
            return list(_result_cache["hits"])
    all_scopes = list(scopes) + list(extra_scopes or [])
    loc_mark = f"[地点：{location}]" if location else ""

    # 注意力系统（v6）：活跃目标相关记忆加权
    try:
        from memory import advisor
        goal_token_sets = [
            set(fact_keywords(g["title"])) for g in advisor.goal_active()
        ]
    except Exception as e:
        _stats_err(e)
        goal_token_sets = []

    weights = _weights()
    plan = _plan(query_text)
    time_hint = extract.understand(query_text).get("time_hint")
    lists = _ranked_lists(query_text, all_scopes, top_k, weights, plan)
    structured_hits = set(lists["structured"])
    # P2-3 检索优化：只加载通道命中的候选记忆行（避免全量扫描），可见性/隐私仍逐行校验
    candidates = {f for lst in lists.values() for f in lst}
    rows = []
    if candidates:
        for scope in all_scopes:
            privacy_th = float(trace.adjustments().get("privacy_threshold", 0.8))
            rows += [
                r
                for r in _db.memory_rows(scope, exclude_status=("superseded",))
                if r["fact"] in candidates
                and _visible(r, scopes)
                and not str(r.get("fact", "")).startswith("enc:")
                and (not loc_mark or loc_mark in str(r.get("fact", "")))
                and float(r.get("privacy", 0.0)) < privacy_th
            ]
    if not rows:
        return []

    # RRF 融合
    fusion = {}
    for algo, lst in lists.items():
        w = weights[algo] * plan[algo]
        if w <= 0:
            continue
        for rank, fact in enumerate(lst):
            fusion[fact] = fusion.get(fact, 0.0) + w / (RRF_K + rank + 1)

    # 策略（重要度/时效）+ 可信度
    stats = {}
    for r in rows:
        stats.setdefault((r["scope"], r["key"]), []).append(r["fact"])
    for (sc, k), facts in stats.items():
        stats[(sc, k)] = policy.stats_for(sc, k, facts)
    conf_by_fact = {r["fact"]: policy.calibrate_adjust(float(r.get("confidence", 0.7))) for r in rows}

    scored, policy_contrib = [], {}
    goal_boost = float(_cfg("goal_boost", 0.08))
    et_map = _event_time_map(all_scopes) if window else {}
    for r in rows:
        fact = r["fact"]
        s = fusion.get(fact, 0.0)
        # 相关度优先（v31）：没有任何检索通道命中的记忆不参与排序，
        # 避免“重要/最近/置信度高但无关”的记忆靠加性权重挤进上下文。
        if s <= 0:
            continue
        # 时间窗口加权（v6 建议 §13）：超出查询时间窗的历史记忆降权（乘性）
        if time_hint and r.get("valid_from"):
            try:
                age_days = (datetime.now() - datetime.fromisoformat(str(r["valid_from"])[:19])).total_seconds() / 86400
                if age_days > float(time_hint) * 1.5:
                    s *= 0.8
            except Exception as e:
                _stats_err(e)
                pass
        if window:
            et = et_map.get(fact)
            if et and et[0]:
                try:
                    ev_dt = datetime.fromisoformat(str(et[0])[:19])
                    if window[0] <= ev_dt < window[1]:
                        s *= 1.5  # 窗口内强提升
                    else:
                        s *= 0.7  # 窗口外软惩罚（不做硬过滤，防漏召回）
                except Exception as e:
                    _stats_err(e)
        if goal_token_sets:
            ft = fact_keywords(fact)
            if any(ft & gt for gt in goal_token_sets):
                s *= 1.0 + goal_boost  # 注意力加权（乘性微调，不再加性主导）
        if fact in structured_hits:
            # 结构化命中加成（属性类问题主通道；乘性，避免压过 RRF 相关度）
            s *= 1.0 + 0.5 * plan["structured"]
        st = stats.get((r["scope"], r["key"]), {}).get(fact, {})
        imp = float(st.get("importance", 0.5))
        rec = float(st.get("recency", 0.5))
        conf = conf_by_fact.get(fact, policy.calibrate_adjust(0.7))
        pc = weights["policy"] * imp * rec
        policy_contrib[fact] = pc
        # 策略/置信度作为乘性微调因子（v31）：相关度为主排序，强度只做微调
        s *= 1.0 + pc
        s *= 1.0 + weights["confidence"] * conf
        scored.append((s, fact, r["scope"]))

    max_score = max((s for s, _f, _sc in scored), default=0.0) or 1.0
    scored.sort(key=lambda x: -x[0])
    # 软过滤（v3.1 §3）：低分记忆不硬剔除，降权保留，由注入端按置信度分档措辞
    soft_min = min_score * 0.6
    ranked = [
        (fact, round(s / max_score, 4), scope)
        for s, fact, scope in scored
        if s / max_score >= soft_min
    ]
    # 重排（light / cross / llm）
    rc = _rerank_cfg()
    rerank_map = {}
    if rc["enabled"] and ranked:
        candidates = [f for f, _s, _sc in ranked[: top_k * 3]]
        topic_facts = {
            p["value"]
            for t in topic.search(query_text, all_scopes, limit=3)
            for p in t.get("params", [])
            if p["param"] == "fact"
        }
        if rc["mode"] == "llm":
            rk = llm_rerank(query_text, candidates, top_k)
        elif rc["mode"] == "cross":
            rk = cross_rerank(query_text, candidates, top_k, topic_facts)
        else:
            rk = light_rerank(query_text, candidates, top_k, topic_facts)
        rerank_map = {f: s for f, s in rk}
        order = {f: i for i, (f, _s) in enumerate(rk)}
        ranked.sort(key=lambda x: (order.get(x[0], 99), -x[1]))
    hits = ranked[:top_k]
    _last_details.clear()
    for fact, _s, _sc in hits:
        _last_details[fact] = {
            "rrf": round(fusion.get(fact, 0.0), 4),
            "policy": round(policy_contrib.get(fact, 0.0), 4),
            "confidence": conf_by_fact.get(fact, 0.7),
            "rerank": rerank_map.get(fact),
        }
    _record_route(lists, hits)
    if use_cache:
        _result_cache.update({"ts": time.time(), "key": cache_key, "hits": hits})
    return hits


def retrieve_detailed(query_text, scopes, top_k=5, min_score=0.25, extra_scopes=None, location=None, window=None):
    """带分数来源分解的检索（单查询）：[{fact, scope, score, rrf, policy, confidence, rerank}]。
    location 可限定只召回带 [地点：X] 标签的空间记忆（P0-2）。"""
    hits = _retrieve_single(
        query_text, scopes, top_k, min_score, extra_scopes,
        use_cache=False, location=location, window=window,
    )
    return [
        {"fact": f, "scope": sc, "score": s, **_last_details.get(f, {})}
        for f, s, sc in hits
    ]


def retrieve(
    query_text,
    scopes,
    top_k=5,
    min_score=0.25,
    extra_scopes=None,
    expand_query=False,
    recent=None,
    location=None,
    window=None,
):
    """对外检索入口：多查询变体（指代消解/同义扩展）合并 → MMR 多样性 → 隐式反馈。
    返回 [(fact, score, scope)]（score 已归一化 0~1），兼容旧签名。"""
    try:
        import memory.stats as _st
        _st.bump("tick:retrieve")
    except Exception as e:
        _stats_err(e)
    if not query_text or not scopes:
        return []
    if window is None and not _cfg("ablation_disable_time", False):
        try:
            from memory import time_extract
            te = time_extract.extract(query_text or "", scopes[0] if scopes else None)
            # 只有显式时间词（昨天/上周三…）才启用窗口；指代类（那天/上次/之前）不设窗口，
            # 否则 (now,now) 会误压历史记忆（v2.2 修复）
            if te.get("explicit") and te.get("start"):
                window = (te["start"], te["end"])
        except Exception as e:
            _stats_err(e)
    variants = extract.expand(query_text, recent) if expand_query else [query_text]
    if len(variants) == 1:
        hits = _retrieve_single(query_text, scopes, top_k, min_score, extra_scopes, location=location, window=window)
    else:
        merged = {}
        for v in variants:
            for f, s, sc in _retrieve_single(v, scopes, top_k, min_score, extra_scopes, location=location, window=window):
                if f not in merged or s > merged[f][0]:
                    merged[f] = (s, sc)
        hits = sorted(
            ((f, s, sc) for f, (s, sc) in merged.items()), key=lambda x: -x[1]
        )[:top_k]
    mmr_cfg = _cfg("mmr", {}) or {}
    if mmr_cfg.get("enabled", True) and len(hits) > 1:
        hits = mmr(query_text, hits, top_k, lam=float(mmr_cfg.get("lambda", 0.7)))
    if min_score >= 0.05 and (_cfg("telemetry", {}) or {}).get("query_log", True):
        _db.query_log_add(query_text, scopes, top_k, [f for f, _s, _sc in hits])
    # 隐式反馈：只对真正返回的记忆计一次调用
    rows = {}
    for scope in list(scopes) + list(extra_scopes or []):
        for r in _db.memory_rows(scope):
            rows.setdefault(r["fact"], (r["scope"], r["key"]))
    for _s, fact, _scope in hits:
        info = rows.get(fact)
        if info:
            policy.touch(info[0], info[1], fact)
    return hits


_ANCHOR_RE = None


def _anchor_re():
    """追问日期类句式（P2-1）：approx 事件 + 用户追问 → 沿 follows 链锚定。"""
    global _ANCHOR_RE
    if _ANCHOR_RE is None:
        import re
        _ANCHOR_RE = re.compile(r"到底是哪天|具体是哪天|具体哪天|具体.{0,6}时候|几号|哪天来着|是哪一天")
    return _ANCHOR_RE


def anchor_time(query_text, scopes, top_k=3, max_depth=3) -> dict:
    """时间锚定（P2-1）：approx 事件 + "到底是哪天"式追问 → 沿 follows 链找邻近 explicit 锚点。

    返回 {"anchored", "fact", "approx_ts", "before": [...], "after": [...], "hint"}；
    hint 可直接注入上下文，让模型"能查证就帮 TA 确认"，而不是编造具体日期。
    """
    try:
        if not _anchor_re().search(str(query_text or "")):
            return {}
        hits = retrieve(query_text, scopes, top_k=max(1, int(top_k)), min_score=0.1)
        if not hits:
            return {}
        evs, seen = [], set()
        for scope in list(scopes or []):
            for ev in _db.event_rows(scope, limit=3000):
                if ev["id"] in seen:
                    continue
                seen.add(ev["id"])
                evs.append(ev)
        by_fact = {}
        for ev in evs:
            by_fact.setdefault(str(ev.get("memory_fact") or ev.get("title") or ""), []).append(ev)
        for fact, _s, _sc in hits:
            for ev in by_fact.get(fact, []):
                if str(ev.get("ts_source") or "approx") != "explicit":
                    return _anchor_from_event(ev, evs, max_depth)
    except Exception as e:
        _stats_err(e)
    return {}


def _anchor_from_event(ev, evs, max_depth) -> dict:
    """沿 follows 链（前后各 max_depth 跳）收集显式时间事件，锚定 approx 事件。"""
    try:
        from memory import graph, time_extract
        by_id = {e["id"]: e for e in evs}
        nids = set(graph.ancestors([ev["id"]], rel="follows", depth=max_depth))
        nids |= set(graph.descendants([ev["id"]], rel="follows", depth=max_depth))
        anchors = []
        for nid in nids:
            nb = by_id.get(nid)
            if not nb or str(nb.get("ts_source") or "approx") != "explicit":
                continue
            ts = str(nb.get("ts") or "")
            if not ts:
                continue
            try:
                dt = datetime.fromisoformat(ts[:19])
            except Exception:
                continue
            anchors.append((dt, ts, str(nb.get("memory_fact") or nb.get("title") or "")))
        if not anchors:
            return {}
        anchors.sort(key=lambda x: x[0])
        try:
            ev_dt = datetime.fromisoformat(str(ev.get("ts") or "")[:19])
        except Exception:
            ev_dt = None
        before = [a for a in anchors if ev_dt is None or a[0] <= ev_dt]
        after = [a for a in anchors if ev_dt is None or a[0] > ev_dt]

        def _brief(dt, ts, title):
            return {
                "ts": ts,
                "title": title,
                "days_offset": round((dt - ev_dt).total_seconds() / 86400, 1) if ev_dt else None,
            }

        out = {
            "anchored": True,
            "fact": str(ev.get("memory_fact") or ev.get("title") or ""),
            "approx_ts": str(ev.get("ts") or ""),
            "before": [_brief(*a) for a in before[-2:]],
            "after": [_brief(*a) for a in after[:2]],
        }
        parts = []
        if before:
            _dt, ts, title = before[-1]
            parts.append(f"在「{title}」（{time_extract.label_for(ts, 'explicit', scope=str(ev.get('scope') or ''))}）之后")
        if after:
            _dt, ts, title = after[0]
            parts.append(f"在「{title}」（{time_extract.label_for(ts, 'explicit', scope=str(ev.get('scope') or ''))}）之前")
        if parts:
            out["hint"] = "（时间锚定：这条记忆" + "、".join(parts) + "——用这些前后事件回答具体日期，但别把“大概”说成确定）"
        return out
    except Exception as e:
        _stats_err(e)
        return {}


# ===== 重排：轻量（子串+词元覆盖+议题一致）始终可用；CrossEncoder/LLM 可选 =====
def retrieve_subject(name, query, top_k=3, min_score=0.3):
    """按主体视角检索（薄封装：npc:<name> 作为 scope，复用六路检索）。"""
    try:
        from memory import subjects
        return retrieve(query, [subjects.scope_of(name)], top_k=max(1, int(top_k)), min_score=float(min_score))
    except Exception as e:
        _stats_err(e)
        return []


def light_rerank(query, candidates, top_k, topic_facts=None):
    """按 精确子串(0.6) + 查询词覆盖率(0.4) + 议题一致性(0.15) 重排。返回 [(fact, score)]。"""
    qt = set(tokenize(query))
    topic_facts = topic_facts or set()
    scored = []
    for fact in candidates:
        ft = set(tokenize(fact))
        coverage = len(qt & ft) / max(1, len(qt)) if qt else 0.0
        exact = 1.0 if str(query) in fact else 0.0
        topic_bonus = 0.15 if fact in topic_facts else 0.0
        scored.append((fact, round(min(1.0, exact * 0.6 + coverage * 0.4 + topic_bonus), 4)))
    scored.sort(key=lambda x: -x[1])
    return scored[: max(1, int(top_k))]


def llm_rerank(query, candidates, top_k):
    """LLM 重排（best-effort，失败回退 light）。"""
    try:
        prompt = (
            f"按与问题的相关度给以下记忆排序，只输出最相关的 {top_k} 条原文，每条一行，不要解释。\n"
            f"问题：{query}\n" + "\n".join(f"{i}. {c}" for i, c in enumerate(candidates))
        )
        resp = _shared.deepseek.chat.completions.create(
            model=_shared.DEEPSEEK_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0.0,
        )
        picked = []
        for line in (resp.choices[0].message.content or "").splitlines():
            line = line.strip()
            if not line:
                continue
            line = line.split(".", 1)[-1].strip()
            if any(line == c for c in candidates):
                picked.append(line)
        if picked:
            return [(f, 1.0 - i * 0.01) for i, f in enumerate(picked[:top_k])]
    except Exception as e:
        print(f"LLM 重排失败，回退轻量重排：{e}")
    return light_rerank(query, candidates, top_k)


_cross = None


def cross_rerank(query, candidates, top_k, topic_facts=None):
    """本地 CrossEncoder 重排（首次加载模型）；失败回退 light。"""
    global _cross
    try:
        if _cross is None:
            from sentence_transformers import CrossEncoder
            model = (_shared.CONFIG.get("memory", {}).get("core", {}) or {}).get("rerank", {}).get(
                "cross_model", "BAAI/bge-reranker-base"
            )
            _cross = CrossEncoder(str(model))
        scores = _cross.predict([(str(query), str(c)) for c in candidates])
        ranked = sorted(zip(candidates, scores), key=lambda x: -float(x[1]))[: max(1, int(top_k))]
        return [(f, round(float(s), 4)) for f, s in ranked]
    except Exception as e:
        print(f"CrossEncoder 重排不可用，回退轻量重排：{e}")
        return light_rerank(query, candidates, top_k, topic_facts)


def mmr(query, hits, top_k, lam=0.7):
    """MMR 多样性：score = λ·相关度 − (1−λ)·与已选的最大相似度（避免 top-k 重复）。"""
    qt = set(tokenize(query))
    picked, rest = [], list(hits)
    while rest and len(picked) < max(1, int(top_k)):
        best, best_i = None, 0
        for i, (fact, score, scope) in enumerate(rest):
            if qt:
                sim_q = len(qt & set(tokenize(fact))) / max(1, len(qt))
            else:
                sim_q = 0.0
            sim_sel = 0.0
            if picked:
                ft = set(tokenize(fact))
                sim_sel = max(
                    len(ft & set(tokenize(p[0]))) / max(1, min(len(ft), len(set(tokenize(p[0])))))
                    for p in picked
                )
            m = float(lam) * (sim_q + float(score)) - (1 - float(lam)) * sim_sel
            if best is None or m > best:
                best, best_i = m, i
        picked.append(rest.pop(best_i))
    return picked


def search(query, scope=None, key=None, limit=10):
    """统一记忆检索（Hermes MCP 用）：融合打分优先，降级关键词 LIKE。"""
    if not query:
        return []
    if scope:
        scopes = [scope]
    else:
        scopes = list(dict.fromkeys(r["scope"] for r in _db.memory_rows()))
    if not scopes:
        return []
    hits = retrieve(query, scopes, top_k=max(1, int(limit)), min_score=0.0)
    if hits:
        conf_by_fact = {}
        for scope_s in scopes:
            for r in _db.memory_rows(scope_s):
                conf_by_fact.setdefault(r["fact"], float(r.get("confidence", 0.7)))
        return [
            {
                "scope": sc,
                "key": key or "",
                "fact": f,
                "score": round(s, 4),
                "confidence": conf_by_fact.get(f, 0.7),
            }
            for f, s, sc in hits
        ]
    rows = _db.memory_search(query, scope, key, limit=max(1, int(limit)))
    return [
        {"scope": r["scope"], "key": r["key"], "fact": r["fact"], "score": 0.0}
        for r in rows
    ]


REL_LABELS = {"related_to": "相关", "follows": "时间线上紧随其后", "contains": "包含"}


def explain(query_text, scopes, top_k=3) -> str:
    """解释“为什么想起这段记忆”：命中的事实 + 事件图关联（带关系类型）。"""
    hits = retrieve(query_text, scopes, top_k=max(1, int(top_k)), min_score=0.0)
    if not hits:
        return "（暂时没有相关的历史记忆）"
    lines = [f"因为你提到「{query_text}」，我想起了："]
    for fact, _score, scope in hits:
        lines.append(f"- {fact}")
        evs = _db.event_rows(scope, limit=200)
        ev = next((e for e in evs if e["title"] == graph.title_of(fact)), None)
        if ev:
            related = []
            for e in _db.relations_for([ev["id"]])[:3]:
                other_id = e["dst"] if e["src"] == ev["id"] else e["src"]
                other = next((x for x in evs if x["id"] == other_id), None)
                if other:
                    related.append(f"{other['title']}（{REL_LABELS.get(e['rel'], e['rel'])}）")
            if related:
                lines.append("  相关事件：" + "；".join(related))
    return "\n".join(lines)



def _stats_err(e):
    """裸 except 审计（v2.2）：错误计数 + 日志，供消融/排查。"""
    try:
        import memory.stats as _st
        _st.bump_err("reasoning", e)
    except Exception:
        pass
