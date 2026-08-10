"""回填与巩固（工程化）：向量 → 事件图/树 → AI 观点 → 修剪 → 词法索引。
run() 返回结构化报告（供 agent.grow / 定时任务）；backfill() 返回可读文本（兼容 tools.py memory-embed）。"""

import json
from datetime import datetime

import math

from plugins import _db
from memory import advisor, controller, embedder, graph, lexical, policy, reasoning, topic, trace, vecindex


def _embed_count(batch=64) -> int:
    rows = [r for r in _db.memory_rows() if not _db.vec_loads(r.get("embedding"))]
    total = 0
    for i in range(0, len(rows), batch):
        part = rows[i:i + batch]
        vecs = embedder.embed([r["fact"] for r in part])
        if not vecs:
            break
        for r, vec in zip(part, vecs):
            _db.memory_update_embedding(r["scope"], r["key"], r["fact"], vec)
            total += 1
    return total


def build_graph(scope=None, key=None) -> int:
    """为尚无事件表示的事实补建事件与关系边。返回新增事件数。"""
    rows = _db.memory_rows(scope, key)
    added = 0
    for r in rows:
        if _db.event_id_by_title(r["scope"], r["key"], graph.title_of(r["fact"])):
            continue
        eid, _linked = graph.build_for_fact(
            r["scope"], r["key"], r["fact"], ts=r.get("updated_at") or ""
        )
        if eid:
            added += 1
    return added


def _llm_one(prompt) -> str:
    try:
        from plugins import _shared
        resp = _shared.deepseek.chat.completions.create(
            model=_shared.DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": "你是记忆巩固器。输出一句简洁结论，不要解释。"},
                {"role": "user", "content": prompt},
            ],
            max_tokens=120,
            temperature=0.3,
        )
        return (resp.choices[0].message.content or "").strip()[:100]
    except Exception:
        return ""


CONSOLIDATE_PROMPT = (
    "把以下关于「{topic}」的时间线记忆压缩成结构化总结，只输出 JSON：\n"
    '{{"summary":"≤40字的核心总结","duration":"持续时间（如：3天/两周/不明）",'
    '"trigger":"具体诱因（保留名字/数字）","turning_points":"情绪或状态转折（如：焦虑失眠→提交→想庆祝）",'
    '"result":"最终结果","preferences":"过程中透露的偏好或下一步想法"}}\n'
    "要求：保留具体数字、名字、情绪变化和偏好细节，不要写成'工作压力已解决'这类丢信息的空话。\n"
    "记忆（按时间顺序，括号内为情绪标注）：\n{items}"
)


def _llm_consolidate(topic, items) -> dict:
    """结构化压缩：保留诱因/时长/情绪转折/结果/偏好，返回 {summary, details{...}}。
    模型忽略 JSON 指令时直接把纯文本当核心总结；完全失败返回 {}（调用方回退过程链）。"""
    try:
        from plugins import _shared
        resp = _shared.deepseek.chat.completions.create(
            model=_shared.DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": "你是记忆巩固器。只输出 JSON，不要解释。"},
                {
                    "role": "user",
                    "content": CONSOLIDATE_PROMPT.format(
                        topic=topic, items="\n".join(f"- {i}" for i in items)
                    ),
                },
            ],
            max_tokens=260,
            temperature=0.3,
        )
        raw = (resp.choices[0].message.content or "").strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0:
            if raw and raw not in ("[]", "{}"):
                return {"summary": raw[:100], "details": {}}
            return {}
        data = json.loads(raw[start:end + 1])
        if not isinstance(data, dict):
            return {}
        summary = str(data.get("summary", "")).strip()[:100]
        if not summary:
            return {}
        details = {
            k: str(data.get(k, "")).strip()[:200]
            for k in ("duration", "trigger", "turning_points", "result", "preferences")
            if str(data.get(k, "")).strip()
        }
        return {"summary": summary, "details": details}
    except Exception:
        return {}


def consolidate(limit=30) -> int:
    """把同类型事件总结成 AI 观点（belief，可信度 0.5）。返回写入条数。"""
    rows = _db.event_rows(limit=limit)
    by_type = {}
    for ev in rows:
        if ev["etype"] != "event":
            by_type.setdefault(ev["etype"], []).append(ev["title"])
    written = 0
    for etype, titles in by_type.items():
        if len(titles) < 2:
            continue
        prompt = (
            f"以下是同一类型（{etype}）的若干记忆，请用一句不超过40字的话"
            "总结成一个可复用的经验/观点：\n" + "\n".join(f"- {t}" for t in titles[:8])
        )
        belief = _llm_one(prompt)
        if belief:
            _db.memory_add(
                "ai",
                "belief",
                belief,
                datetime.now().isoformat(timespec="seconds"),
                None,
                0.5,
                "belief",
            )
            written += 1
    return written


def consolidate_topics(limit=30) -> int:
    """议题级记忆巩固（v5 §P1-1 + v22 情绪价值修复）：
    同一议题事实 ≥5 条 → LLM 压缩成「核心总结 + 细节属性（时长/诱因/情绪转折/结果/偏好）」
    入 consolidated，原细节事实降权保留（不主导召回但可追溯）。
    情绪线索不再被压成一句空话；LLM 失败时回退为过程链（A → B → C），绝不丢链条。"""
    written = 0
    ts = datetime.now().isoformat(timespec="seconds")
    for t in _db.topic_rows(limit=limit):
        params = _db.topic_params(t["id"])
        facts = [p["value"] for p in params if p["param"] == "fact"]
        if len(facts) < 5:
            continue
        # 按记忆时间排序 + 情绪标注，保留过程与转折
        rows_by_fact = {}
        for r in _db.memory_rows(t["scope"], t["key"]):
            rows_by_fact[r["fact"]] = r
        ordered = [f for f in facts if f in rows_by_fact]
        if len(ordered) < 5:
            ordered = facts
        items = []
        for f in ordered[:12]:
            r = rows_by_fact.get(f)
            v = float((r or {}).get("valence", 0.0))
            tag = "低落" if v < -0.3 else ("开心" if v > 0.3 else "")
            items.append(f + (f"（{tag}）" if tag else ""))
        result = _llm_consolidate(t["topic"], items)
        if result:
            summary = result["summary"]
            details = result["details"]
        else:
            summary = _llm_one(
                f"把以下 {len(ordered)} 条关于「{t['topic']}」的记忆压缩成一句不超过40字的核心总结：\n"
                + "\n".join(f"- {i}" for i in items)
            )
            details = {}
        if not summary or summary in ("[]", "{}"):
            chain = " → ".join(items[:6])
            summary = (chain + (" …" if len(items) > 6 else ""))[:100]
        valences = [float((rows_by_fact.get(f) or {}).get("valence", 0.0)) for f in ordered]
        avg_v = round(sum(valences) / len(valences), 3) if valences else 0.0
        _db.memory_add(
            t["scope"], "consolidated", summary,
            ts, None, 0.7, "consolidation",
            valence=avg_v,
        )
        for field, value in details.items():
            _db.attr_set(t["scope"], "consolidated", f"consolidation:{field}", value, 0.7, ts)
        for f in facts:
            row = next(
                (r for r in _db.memory_rows(t["scope"], t["key"]) if r["fact"] == f), None
            )
            if row:
                cur = float(row.get("confidence", 0.7))
                _db.memory_set_confidence(t["scope"], t["key"], f, round(cur * 0.8, 3))
        written += 1
    return written


def run(batch=64) -> dict:
    """完整维护/成长流水线，返回结构化报告（幂等，可重复执行）。"""
    report = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "embedder": embedder.enabled(),
        "embedded": _embed_count(batch) if embedder.enabled() else 0,
        "events": build_graph(),
        "timeline": graph.link_follows(),
        "topics_built": topic.build(),
        "topics": _db.topics_count(),
        "beliefs": consolidate(),
        "consolidated": consolidate_topics(),
        "pruned": policy.prune(),
        "orphans": graph.cleanup_orphans(),
        "fuzzy": 0,
        "forgotten": 0,
        "promoted": 0,
        "lexicon": _db.lexicon_rebuild(),
        "bm25": lexical.bm25_rebuild(),
        "reflection": advisor.reflect_beliefs(),
        "insights": advisor.daily_reflect(),
        "vector_index": vecindex.build() if embedder.enabled() else {"skipped": True},
        "trace_pruned": trace.prune(),
        "query_log_pruned": _db.query_log_prune(30),
    }
    forget_result = policy.forget()
    report["fuzzy"] = forget_result["fuzzy"]
    report["forgotten"] = forget_result["forgotten"]
    report["promoted"] = policy.promote()
    try:
        from memory import space as space_mod
        report["space_events"] = space_mod.prune_events(7)  # 空间事件保留期（v31）
    except Exception as e:
        report["space_events"] = {"error": str(e)}
    try:
        from memory import living as living_mod
        report["living"] = living_mod.daily_tick()  # 生活演化（v31）
        report["birthday"] = living_mod.birthday_celebrate()  # 生日（v31.3）
    except Exception as e:
        report["living"] = {"error": str(e)}
    try:
        from memory import sleep as sleep_mod
        report["sleep"] = sleep_mod.night_run()  # 一夜：浅睡/深睡巩固 + REM 做梦（v31）
    except Exception as e:
        report["sleep"] = {"error": str(e)}
    report["sessions_closed"] = controller.close_old()
    report["entities"] = graph.build_entities()
    report["relations_tagged"] = graph.tag_relations()
    from plugins import _shared as _sh
    probes_path = _sh.DATA_DIR / "probes.json"
    if probes_path.exists():
        try:
            report["eval"] = eval_run_file(str(probes_path))
            baseline = _db.kv_get("memory", "eval_baseline", {}) or {}
            if baseline.get("recall_at_k") is not None:
                report["eval_delta_recall"] = round(
                    report["eval"].get("recall_at_k", 0.0) - float(baseline.get("recall_at_k", 0.0)), 3
                )
        except Exception as e:
            report["eval"] = {"error": str(e)}
    rows = _db.memory_rows()
    report["memories"] = len(rows)
    report["confidence_avg"] = (
        round(sum(float(r.get("confidence", 0.7)) for r in rows) / len(rows), 3) if rows else 0.0
    )
    return report


# ===== 评测（召回率 / MRR / NDCG）=====
def eval_run(probes, k=5) -> dict:
    """probes: [{"query": ..., "expected": [...], "scope": ...}]，返回评估指标。"""
    results = []
    cats = {}
    for p in probes:
        scopes = [p["scope"]] if p.get("scope") else list(
            dict.fromkeys(r["scope"] for r in _db.memory_rows())
        )
        hits = reasoning.retrieve(p["query"], scopes, top_k=max(1, int(k)), min_score=0.0)
        rank = next(
            (i for i, (f, _s, _sc) in enumerate(hits) if any(e in f or f in e for e in p["expected"])),
            None,
        )
        cat = p.get("category", "other")
        results.append({"query": p["query"], "hit": rank is not None, "rank": rank, "category": cat})
        c = cats.setdefault(cat, {"n": 0, "hit": 0, "rr": 0.0})
        c["n"] += 1
        if rank is not None:
            c["hit"] += 1
            c["rr"] += 1.0 / (rank + 1)
    n = len(results) or 1
    recall = sum(1 for r in results if r["hit"]) / n
    mrr = sum(1.0 / (r["rank"] + 1) for r in results if r["hit"]) / n
    ndcg = sum(1.0 / math.log2(r["rank"] + 2) for r in results if r["hit"]) / n
    return {
        "probes": len(results),
        "recall_at_k": round(recall, 3),
        "mrr": round(mrr, 3),
        "ndcg": round(ndcg, 3),
        "categories": {
            cat: {
                "n": v["n"],
                "recall": round(v["hit"] / max(1, v["n"]), 3),
                "mrr": round(v["rr"] / max(1, v["n"]), 3),
            }
            for cat, v in cats.items()
        },
        "details": results,
    }


def eval_run_file(path, k=5) -> dict:
    """从 JSON 文件加载评测集并运行。"""
    import json as _json
    with open(path, encoding="utf-8") as f:
        probes = _json.load(f)
    return eval_run(probes, k=k)


def eval_report() -> str:
    """记忆库统计 + 可信度概览（工程化成长报告的输入）。"""
    rows = _db.memory_rows()
    if not rows:
        return "记忆库为空"
    confs = [float(r.get("confidence", 0.7)) for r in rows]
    sources = {}
    for r in rows:
        sources[r.get("source") or "unknown"] = sources.get(r.get("source") or "unknown", 0) + 1
    return (
        f"记忆 {len(rows)} 条 · 平均可信度 {sum(confs) / len(confs):.2f} · "
        f"事件 {len(_db.event_rows())} · 属性 {len(_db.attr_rows())} · "
        f"词法索引 {'FTS5 BM25' if _db.fts_available() else 'LIKE 降级'} · "
        f"来源分布 {sources}"
    )


def backfill(batch=64) -> str:
    """可读摘要（兼容 tools.py memory-embed）。"""
    r = run(batch)
    lines = []
    if r["embedder"]:
        lines.append(f"向量回填：{r['embedded']} 条")
    else:
        lines.append("embedder 未启用（config.json → memory.embedder.provider），跳过向量回填")
    lines.append(f"事件图：补建 {r['events']} 个事件/关系")
    lines.append(f"事件树：补齐 {r['timeline']} 条时间线链")
    lines.append(f"议题：{r['topics']} 个（补建 {r['topics_built']}）")
    lines.append(f"AI 观点巩固：写入 {r['beliefs']} 条 belief")
    lines.append(f"低价值记忆修剪：{r['pruned']} 条")
    lines.append(f"孤立事件清理：{r['orphans']} 个")
    lines.append(f"渐进遗忘：{r['fuzzy']} 条转模糊 · {r['forgotten']} 条遗忘")
    lines.append(f"短期→长期巩固：{r['promoted']} 条")
    lines.append(f"会话关闭：{r.get('sessions_closed', 0)} · 实体：{r.get('entities', 0)} · 关系类型：{r.get('relations_tagged', 0)}")
    if r.get("eval"):
        lines.append(f"评测：recall@{r['eval'].get('probes', 0)}={r['eval'].get('recall_at_k')} MRR={r['eval'].get('mrr')}")
        if r.get("eval_delta_recall") is not None:
            lines.append(f"vs baseline：{r['eval_delta_recall']}")
    lines.append(f"词法索引重建：{r['lexicon']} 行")
    lines.append(f"BM25 倒排：{r['bm25']} 篇文档")
    rf = r["reflection"]
    lines.append(f"信念反思：审查 {rf.get('checked', 0)} · 接受 {rf.get('accepted', 0)} · 改写 {rf.get('revised', 0)} · 驳回 {rf.get('rejected', 0)}")
    if isinstance(r["vector_index"], dict) and "error" in r["vector_index"]:
        lines.append(f"向量索引：{r['vector_index']['error']}")
    else:
        vi = r["vector_index"]
        lines.append(f"向量索引：{vi.get('n', 0)} 条 / {vi.get('nlist', 0)} 质心 / dim {vi.get('dim', 0)}")
    return "\n".join(lines)
