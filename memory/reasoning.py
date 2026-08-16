"""推理层：多算法并存（BM25 词法 / 向量 / 图谱 / 结构化属性）+ 按需路由 + RRF 融合。

每个算法独立产出有序候选，再用 Reciprocal Rank Fusion 融合（可配置权重），
最后叠加 Memory Policy（重要度/时效）与可信度。换新算法 = 往 _ranked_lists 加一个分支。"""

import contextvars
import re
import time
from collections import OrderedDict
from datetime import datetime

from plugins import _db, _shared
from memory import embedder, extract, graph, lexical, policy, topic, trace, vecindex
from memory.extract import fact_keywords, tokenize

_query_cache = {"ts": 0.0, "text": "", "vec": None}
_route_cache = None
_route_flush_ts = {"ts": 0.0}
RRF_K = 60
_event_time_cache: dict = {"key": None, "map": {}, "ts": 0.0}
_rewrite_cache: dict = {}

# ai scope 里的人设元字段（说话示例/行为规则/风格参数/性格设定/关系/动机）不该进普通记忆检索——
# 否则宽泛查询会命中"你是做什么的"这类示例文本（v2.3 改动 1）。
# 保留可检索的事实型字段：identity（你是谁）/ preference（你喜欢什么）/ experience_persona（你怎么出道的）。
AI_META_EXCLUDE_KEYS = frozenset((
    "examples", "avoid", "defaults", "behavior_policy", "value",
    "conflict", "style", "catchphrase", "mood_profile",
    "personality", "relationship", "motivation",
))

# 宽泛/指代查询：LLM 改写为具体名词后再检索（v2.3 改动 2）
_DEICTIC_RE = re.compile(
    r"我(?:最近|上次|之前|以前)?(?:说过|说过什么|做过|做过什么|是做什么的|是谁|有什么)"
    r"|你(?:是做什么的|是谁)|我们(?:是|做过|说过)|我(?:想|要|有)什么"
)


def _event_time_map(scopes) -> dict:
    """fact → (事件 ts, ts_source)，供时间窗口加权（时间当元数据，不污染事实文本）。"""
    key = tuple(sorted(scopes or []))
    if _event_time_cache["key"] == key and time.time() - _event_time_cache["ts"] < 60:
        return _event_time_cache["map"]
    m: dict = {}
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
    return _shared.core_cfg("", key, default)
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


def _rewrite_enabled() -> bool:
    """memory.core.query.rewrite_llm：LLM 查询改写开关（缺省开启）。"""
    try:
        q = (_shared.CONFIG.get("memory", {}).get("core", {}) or {}).get("query", {}) or {}
        return bool(q.get("rewrite_llm", True))
    except Exception:
        return True


def rewrite_query(query) -> str:
    """LLM 查询改写（治本）：宽泛/指代查询 → 具体名词短语，再走检索。
    已具体不改写（省 token）；LLM 失败降级原句；同 query 缓存不重复调用。"""
    global _rewrite_cache
    q = str(query or "").strip()
    if not q or not _DEICTIC_RE.search(q):
        return q
    if q in _rewrite_cache:
        return _rewrite_cache[q]
    try:
        from plugins import _shared
        prompt = (
            "把下面这句宽泛/指代式的提问改写成适合记忆检索的具体名词短语。"
            "输出一行（主语+主题+关键词，中文，不超过 15 字），不要解释、不要引号。\n"
            f"原句：{q[:100]}"
        )
        resp = _shared.deepseek_chat(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=30, temperature=0.1,
            module="query_rewrite", detail="rewrite",
        )
        out = (resp.choices[0].message.content or "").strip().strip('"“”')
        if not out or len(out) > 30 or out == q:
            return q
        if len(_rewrite_cache) > 200:
            _rewrite_cache = {}
        _rewrite_cache[q] = out
        return out
    except Exception:
        return q


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


def record_negative_feedback(fact, scope=None) -> bool:
    """真实负反馈：用户纠正/否认了某条已召回记忆时，降低对应检索通道的命中计数。
    用于缓解“把被召回当成召回正确”的自反馈偏差。
    传入 scope 时会做对齐保护：只惩罚最近一次该 scope 检索确实命中的记忆。
    优先使用当前请求上下文中的检索明细，避免并发串用。"""
    if not fact:
        return False
    ctx = _retrieval_ctx.get()
    ctx_details = ctx.get("last_details", {}) if ctx else {}
    ctx_retrieval = ctx.get("last_retrieval", {}) if ctx else {}
    detail = None
    if scope is not None:
        entry = ctx_retrieval.get(scope) or _last_retrieval.get(scope)
        if not entry:
            return False
        if time.time() - entry["ts"] > _RETRIEVAL_FEEDBACK_WINDOW:
            return False
        if fact not in entry["facts"]:
            return False
        detail = entry["details"].get(fact)
    if detail is None:
        detail = ctx_details.get(fact) or _last_details.get(fact)
    if not detail:
        return False
    stats = _route_stats()
    for algo in detail.get("channels", []):
        s = stats.setdefault(algo, {"trials": 0, "hits": 0})
        s["hits"] = max(0, int(s.get("hits", 0)) - 1)
        s["misses"] = int(s.get("misses", 0)) + 1
    _flush_route_stats()
    return True


def _rerank_cfg() -> dict:
    r = _cfg("rerank", {}) or {}
    return {"enabled": bool(r.get("enabled", True)), "mode": str(r.get("mode", "light"))}


def _graph_boost_facts(query_text, scopes) -> dict:
    """图谱：查询命中的种子事件扩展。语义边（related_to）权重 1.0，时间线（follows）权重 0.5。"""
    boost: dict = {}
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
    lists: dict[str, list] = {"lexical": [], "vector": [], "graph": [], "structured": [], "rules": [], "topics": []}
    seen: dict[str, set] = {k: set() for k in lists}

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


_result_cache: OrderedDict = OrderedDict()
_RESULT_CACHE_MAX = 16
_last_details: dict = {}
_last_retrieval: dict = {}
_RETRIEVAL_FEEDBACK_WINDOW = 600.0
_retrieval_ctx: contextvars.ContextVar = contextvars.ContextVar("reasoning_retrieval", default={})


def _ctx() -> dict:
    """当前请求/线程的检索上下文，避免多用户/多 NPC 并发串用全局明细。"""
    d = _retrieval_ctx.get()
    if not d:
        d = {"last_details": {}, "last_retrieval": {}}
        _retrieval_ctx.set(d)
    return d


def current_details() -> dict:
    """当前上下文中的检索通道明细；无上下文时回退全局（兼容测试/旧调用）。"""
    ctx = _retrieval_ctx.get()
    return ctx.get("last_details", {}) if ctx else _last_details


def _cache_cfg() -> dict:
    c = _cfg("cache", {}) or {}
    return {"enabled": bool(c.get("enabled", True)), "ttl": float(c.get("ttl_s", 60))}


def _retrieve_single(query_text, scopes, top_k=5, min_score=0.25, extra_scopes=None, use_cache=True, location=None, window=None):
    """单查询检索（含结果缓存）：返回 [(fact, score, scope)]（score 已归一化 0~1）。"""
    if not query_text or not scopes:
        return []
    all_scopes = list(scopes) + list(extra_scopes or [])
    cc = _cache_cfg()
    cache_key = hash((query_text, tuple(scopes), tuple(extra_scopes or []), top_k, min_score, location, window))
    if use_cache and cc["enabled"]:
        _cached = _result_cache.get(cache_key)
        if _cached and time.time() - _cached[0] < cc["ttl"]:
            _result_cache.move_to_end(cache_key)
            _last_details.clear()
            _last_details.update(_cached[2])
            _now = time.time()
            _retrieval_map = {}
            for _sc in all_scopes:
                _retrieval_map[_sc] = {
                    "ts": _now,
                    "facts": {f for f, _s, _sc2 in _cached[1]},
                    "details": _cached[2],
                }
            _last_retrieval.clear()
            _last_retrieval.update(_retrieval_map)
            _ctx()["last_details"] = dict(_cached[2])
            _ctx()["last_retrieval"] = _retrieval_map
            return list(_cached[1])
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
    channel_map = {}
    for _algo, _lst in lists.items():
        for _fact in _lst:
            channel_map.setdefault(_fact, []).append(_algo)
    structured_hits = set(lists["structured"])
    # P2-3 检索优化：只加载通道命中的候选记忆行（避免全量扫描），可见性/隐私仍逐行校验
    candidates = {f for lst in lists.values() for f in lst}
    rows = []
    if candidates:
        for scope in all_scopes:
            privacy_th = float(trace.adjustments().get("privacy_threshold", 0.8))
            # ② 检索下推：fact IN (...) 下推 SQL，不再全表拉取后 Python 过滤
            rows += [
                r
                for r in _db.memory_rows_by_facts(scope, candidates, exclude_status=("superseded",))
                if r["fact"] in candidates
                and _visible(r, scopes)
                and not str(r.get("fact", "")).startswith("enc:")
                # v2.3 改动 1：ai scope 的人设元字段（说话示例/规则/风格）不进普通检索
                and not (
                    str(r.get("scope") or "").startswith("ai")
                    and str(r.get("key") or "") in AI_META_EXCLUDE_KEYS
                )
                # 人物档案（char:）是静态设定，不进普通记忆检索（避免抢占「你是谁」等查询）
                and not str(r.get("scope") or "").startswith("char:")
                and (not loc_mark or loc_mark in str(r.get("fact", "")))
                and float(r.get("privacy", 0.0)) < privacy_th
            ]
    if not rows:
        return []

    # 心境一致性检索（v2.2+）：当前用户情绪 VAD × 议题情绪质心距离 → 乘数加权
    mood_boost = float(_cfg("mood_boost", 0.12))
    user_vad, mood_facts, emotion_mod = None, {}, None
    if mood_boost > 0:
        try:
            from memory import emotion as _emotion_mod, topic as topic_mod
            emotion_mod = _emotion_mod
            scope0 = all_scopes[0] if all_scopes else ""
            # 慢通道桥接（v2.2+）：快状态 × mood_alpha_fast + 日级底色 × (1-fast)
            user_vad = _emotion_mod.blended_estimate(scope0, float(_cfg("mood_alpha_fast", 0.7)))
            if user_vad:
                mood_facts = topic_mod.mood_map(all_scopes)
        except Exception as e:
            _stats_err(e)

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
        if user_vad and fact in mood_facts and emotion_mod is not None:
            try:
                d = min(1.0, emotion_mod.dist(user_vad, mood_facts[fact]["vad"]) / 1.8)
                s *= 1.0 + mood_boost * (1.0 - d)  # 越贴近当前情绪，权重越高
            except Exception as e:
                _stats_err(e)
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
            rk = llm_rerank(query_text, candidates, top_k, paths=[a for a, lst in lists.items() if lst])
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
            "channels": sorted(channel_map.get(fact, [])),
        }
    _record_route(lists, hits)
    _now = time.time()
    _details_snapshot = {f: dict(d) for f, d in _last_details.items()}
    _retrieval_map = {}
    for _sc in all_scopes:
        _retrieval_map[_sc] = {
            "ts": _now,
            "facts": {f for f, _s, _sc2 in hits},
            "details": _details_snapshot,
        }
    _last_retrieval.clear()
    _last_retrieval.update(_retrieval_map)
    _ctx()["last_details"] = _details_snapshot
    _ctx()["last_retrieval"] = _retrieval_map
    if use_cache:
        _result_cache[cache_key] = (time.time(), hits, _details_snapshot)
        _result_cache.move_to_end(cache_key)
        while len(_result_cache) > _RESULT_CACHE_MAX:
            _result_cache.popitem(last=False)
    return hits


def retrieve_detailed(query_text, scopes, top_k=5, min_score=0.25, extra_scopes=None, location=None, window=None):
    """带分数来源分解的检索（单查询）：[{fact, scope, score, rrf, policy, confidence, rerank}]。
    location 可限定只召回带 [地点：X] 标签的空间记忆（P0-2）。"""
    hits = _retrieve_single(
        query_text, scopes, top_k, min_score, extra_scopes,
        use_cache=False, location=location, window=window,
    )
    _details = current_details()
    return [
        {"fact": f, "scope": sc, "score": s, **_details.get(f, {})}
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
    orig_query = query_text
    if _rewrite_enabled():
        # LLM 查询改写（v2.3 改动 2）：宽泛/指代查询 → 具体名词；已具体/失败/缓存直接返回原句
        query_text = rewrite_query(query_text)
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
    # 空间自动提取：查询含地点词（排练室/客厅…）→ 限定带 [地点：X] 标签的空间记忆
    if location is None:
        try:
            from memory import space as _space_mod
            from memory import pack as _pack_mod
            _places = list(_space_mod.memorable_places())
            _layout = (_pack_mod.world() or {}).get("layout") or {}
            for _k in _layout.keys():
                if _k and _k not in _places:
                    _places.append(_k)
            for _p in _places:
                if _p and str(_p) in query_text:
                    location = _p
                    break
        except Exception as e:
            _stats_err(e)
    # 主体自动识别：查询含已注册主体名（队友/NPC）→ 扩展 npc scope（多主体记忆）
    try:
        from memory import subjects as _subj_mod
        _names = _subj_mod.detect(query_text)
        if _names:
            _scopes = list(scopes or [])
            for _n in _names:
                _ns = _subj_mod.scope_of(_n)
                if _ns not in _scopes:
                    _scopes.append(_ns)
            scopes = _scopes
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
    # 检索自我评估（P0）：结果太弱时自动做一次查询扩展再检索
    low_conf = not hits or hits[0][1] < float(_cfg("low_conf_retrieval_threshold", 0.3))
    if low_conf and not expand_query:
        _variants = extract.expand(query_text, recent)
        if len(_variants) > 1:
            _merged = {f: (s, sc) for f, s, sc in hits}
            for _v in _variants[1:]:
                for _f, _s, _sc in _retrieve_single(
                    _v, scopes, top_k, min_score, extra_scopes,
                    location=location, window=window,
                ):
                    if _f not in _merged or _s > _merged[_f][0]:
                        _merged[_f] = (_s, _sc)
            hits = sorted(
                ((f, s, sc) for f, (s, sc) in _merged.items()), key=lambda x: -x[1]
            )[:top_k]
    mmr_cfg = _cfg("mmr", {}) or {}
    if mmr_cfg.get("enabled", True) and len(hits) > 1:
        hits = mmr(query_text, hits, top_k, lam=float(mmr_cfg.get("lambda", 0.7)))
    # 情绪寻址复核（v2.2+）：语义检索结果弱/空时，用当前用户情绪 VAD 做二级检索键，
    # 纠正语义寻址抓错对象的情况（affective-episodic-memory 的复核机制）
    if (
        _cfg("emotion_address", True)
        and (not hits or hits[0][1] < float(_cfg("emotion_address_threshold", 0.35)))
    ):
        try:
            from memory import emotion as emotion_mod
            est = emotion_mod.user_estimate(scopes[0] if scopes else "")
            if est and est.get("vad"):
                u = est["vad"]
                cands = []
                for scope in list(scopes) + list(extra_scopes or []):
                    for r in _db.memory_rows(scope, exclude_status=("superseded",)):
                        v = float(r.get("valence", 0.0) or 0.0)
                        a = float(r.get("arousal", 0.0) or 0.0)
                        d = float(r.get("dominance", 0.0) or 0.0)
                        if abs(v) < 0.05 and abs(a) < 0.05 and abs(d) < 0.05:
                            continue
                        cands.append(
                            (emotion_mod.dist(u, {"v": v, "a": a, "d": d}), r["fact"], r["scope"])
                        )
                cands.sort(key=lambda x: x[0])
                existing = {f for f, _s, _sc in hits}
                top_n = max(1, int(_cfg("emotion_address_top_k", 2)))
                for d, f, sc in cands[:top_n]:
                    if f in existing:
                        continue
                    hits.append((f, round(max(0.3, 1.0 - d / 2.0), 3), sc))
                hits = hits[:top_k]
        except Exception as e:
            _stats_err(e)
    if min_score >= 0.05 and (_cfg("telemetry", {}) or {}).get("query_log", True):
        _db.query_log_add(orig_query, scopes, top_k, [f for f, _s, _sc in hits])
    # 隐式反馈：只对真正返回的记忆计一次调用（② 批量：fact IN 下推 + 一次事务提交）
    hit_facts = [f for _s, f, _scope in hits]
    rows = {}
    for scope in list(scopes) + list(extra_scopes or []):
        for r in _db.memory_rows_by_facts(scope, hit_facts):
            rows.setdefault(r["fact"], (r["scope"], r["key"]))
    items = []
    for _s, fact, _scope in hits:
        info = rows.get(fact)
        if info:
            items.append((info[0], info[1], fact, 0.5, ""))
    _db.meta_touch_many(items)
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
        by_fact: dict = {}
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


def llm_rerank(query, candidates, top_k, paths=None):
    """LLM 重排（best-effort，失败回退 light）；paths=本次参与检索的算法（成本归因用）。"""
    try:
        prompt = (
            f"按与问题的相关度给以下记忆排序，只输出最相关的 {top_k} 条原文，每条一行，不要解释。\n"
            f"问题：{query}\n" + "\n".join(f"{i}. {c}" for i, c in enumerate(candidates))
        )
        resp = _shared.deepseek_chat(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0.0,
            module="rerank",
            detail=",".join(paths or []),
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
        _stats_err(e)
    return light_rerank(query, candidates, top_k)


_cross = None


def cross_rerank(query, candidates, top_k, topic_facts=None):
    """本地 CrossEncoder 重排（首次加载模型）；失败回退 light。"""
    global _cross
    try:
        if _cross is None:
            import os
            import pathlib
            from sentence_transformers import CrossEncoder
            model = (_shared.CONFIG.get("memory", {}).get("core", {}) or {}).get("rerank", {}).get(
                "cross_model", "BAAI/bge-reranker-base"
            )
            # 本地模型目录优先（避免联网下载被墙）；相对路径按项目根目录解析
            model_path = pathlib.Path(str(model))
            if not model_path.is_absolute():
                model_path = pathlib.Path(__file__).resolve().parent.parent / model_path
            if model_path.is_dir():
                os.environ.setdefault("HF_HUB_OFFLINE", "1")
                os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
                model = str(model_path)
            device = None  # None = 让 CrossEncoder 自选（新版会走 GPU）；显式检测更稳妥
            try:
                import torch
                device = "cuda" if torch.cuda.is_available() else "cpu"
            except Exception:
                device = "cpu"
            _cross = CrossEncoder(str(model), device=device)
        scores = _cross.predict([(str(query), str(c)) for c in candidates])
        ranked = sorted(zip(candidates, scores), key=lambda x: -float(x[1]))[: max(1, int(top_k))]
        return [(f, round(float(s), 4)) for f, s in ranked]
    except Exception as e:
        _stats_err(e)
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
