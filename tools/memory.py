"""Memory-related CLI commands (split from tools.py)."""

import json
import pathlib

from plugins import _shared


def cmd_memory_embed(batch: int = 64) -> str:
    import memory
    return memory.backfill(batch)


def cmd_memory_grow(dry_run: bool) -> str:
    """工程化成长：向量/事件图/巩固/修剪/词法索引 + 可信度报告。"""
    import agent
    return json.dumps(agent.grow(dry_run=dry_run), ensure_ascii=False, indent=2)


def cmd_memory_sleep(force: bool = False) -> str:
    """手动跑一夜：浅睡/深睡巩固 + REM 做梦。默认按日去重，--force 可重跑。"""
    import memory
    return json.dumps(memory.sleep_run(force=force), ensure_ascii=False, indent=2)


def cmd_emotion_log(days: int = 14, out: str = "") -> str:
    """导出情绪判断日志（训练数据原料），--out 写 jsonl。"""
    import memory
    rows = memory.emotion_log_rows(days)
    if not out:
        return f"共 {len(rows)} 条情绪判断日志（近 {days} 天）。用 --out 导出 jsonl。"
    with open(out, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return f"已导出 {len(rows)} 条 → {out}"


def cmd_emotion_train(file: str, out: str = "") -> str:
    """训练本地情绪分类器（bge-large 编码 + 逻辑回归），替换 analysis 的 LLM 兜底。
    训练集 JSON：[{"text":"气死我了","emotion":"愤怒"}, ...]，emotion ∈ 9 类（开心/低落/焦虑/兴奋/愤怒/恐惧/惊讶/厌恶/平静）。
    产物：data/models/emotion_clf.pkl，emotion.py 检测到即自动启用（回退 LLM）。"""
    import pathlib
    import pickle
    from plugins import _shared

    try:
        with open(file, encoding="utf-8") as f:
            rows = json.load(f)
    except Exception as e:
        return f"训练集读取失败：{e}"
    texts, labels = [], []
    for r in rows:
        t = str(r.get("text", "")).strip()
        label = str(r.get("emotion", "")).strip()
        if t and label:
            texts.append(t)
            labels.append(label)
    if len(texts) < 30:
        return f"训练样本太少（{len(texts)} 条），建议 ≥300 条覆盖长尾情绪"

    from sentence_transformers import SentenceTransformer
    import torch
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = SentenceTransformer("BAAI/bge-large-zh-v1.5", device=device)
    X = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)

    from sklearn.linear_model import LogisticRegression
    clf = LogisticRegression(max_iter=1000).fit(X, labels)
    acc = clf.score(X, labels)

    out_path = pathlib.Path(out) if out else _shared.DATA_DIR / "models" / "emotion_clf.pkl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as f:
        pickle.dump({"clf": clf, "labels": sorted(set(labels))}, f)
    return f"训练完成：{len(texts)} 条，训练集准确率 {acc:.3f}，已保存 → {out_path}"


def cmd_memory_route(text: str) -> str:
    """诊断：显示一条消息的分类路由（存到哪些算法库）与查询理解。"""
    import memory
    return json.dumps(
        {
            "understand": memory.understand(text),
            "route": memory.route(text),
            "route_stats": memory.route_stats(),
        },
        ensure_ascii=False,
        indent=2,
    )


def cmd_memory_topics(scope: str, limit: int) -> str:
    """列出议题（大类 → 议题 → 参数）。"""
    import memory
    topics = memory.topic_list(scope or None, limit)
    return json.dumps(
        [
            {
                "id": t["id"],
                "category": t["category"],
                "topic": t["topic"],
                "status": t["status"],
                "importance": t["importance"],
                "confidence": t["confidence"],
                "params": len(memory.topic_package(t["id"]).get("params", [])),
            }
            for t in topics
        ],
        ensure_ascii=False,
        indent=2,
    )


def cmd_memory_index(tune: bool, file: str, nlist: str, nprobe: str) -> str:
    """重建/调优自研 IVF 向量索引。--tune 用评测集做 nlist/nprobe 对照。"""
    import memory
    if tune:
        if not file:
            return "请提供评测集：--tune --file probes.json"
        try:
            with open(file, encoding="utf-8") as f:
                probes = json.load(f)
        except Exception as e:
            return f"评测集读取失败：{e}"
        nlists = tuple(int(x) for x in nlist.split(",") if x.strip()) or (4, 8, 16)
        nprobes = tuple(int(x) for x in nprobe.split(",") if x.strip()) or (1, 2, 4)
        return json.dumps(memory.vec_tune(probes, nlists, nprobes), ensure_ascii=False, indent=2)
    return json.dumps(memory.index_vectors(), ensure_ascii=False, indent=2)


def cmd_memory_clear_user(uid: str) -> str:
    """按用户彻底清除（隐私权）：记忆/事件/议题/属性/索引。"""
    from plugins import _db
    scope = f"c2c:{uid}"
    _db.purge_scope(scope, subsystems=True, confirm=scope)
    removed_appts = 0
    try:
        from memory import appointment
        removed_appts = appointment.clear_scope(scope)  # 约定在 kv，不在 memories，需单独清
    except Exception:
        pass
    _db.audit_add("memory.clear_user", scope)
    return f"已清除 {scope} 的全部记忆/事件/议题/索引（约定 {removed_appts} 条）"


_PROBE_SOCIAL_WORDS = (
    "你好", "您好", "哈喽", "嗨", "在吗", "早安", "晚安", "谢谢", "多谢", "拜拜",
    "再见", "辛苦了", "打扰", "哈哈", "嘻嘻", "好的", "收到", "嗯", "哦",
)


_PROBE_QUESTION_WORDS = ("？", "?", "吗", "呢", "哪", "什么", "怎么", "谁", "为什么", "啥", "几", "是不是", "有没有")


def _is_social_probe(query) -> bool:
    """评测集过滤（v2.3 修复 P2-2）：寒暄/短陈述句不构成检索需求，剔除。"""
    q = str(query or "").strip()
    if len(q) < 3:
        return True
    if any(w in q for w in _PROBE_SOCIAL_WORDS):
        return True
    if len(q) <= 6 and not any(w in q for w in _PROBE_QUESTION_WORDS):
        return True
    return False


def cmd_memory_probes(limit: int, out: str) -> str:
    """把查询日志导出为评测集（弱监督：当时返回的即期望）。"""
    from plugins import _db

    def _probe_category(query):
        q = str(query or "")
        if any(w in q for w in ("昨天", "前天", "上周", "这周", "上个月", "去年", "今天", "什么时候", "哪天", "最近")):
            return "time"
        if any(w in q for w in ("哪", "在哪", "哪里", "房间", "柜", "冰箱", "客厅", "卧室", "厨房", "找")):
            return "space"
        if any(w in q for w in ("开心", "难过", "生气", "烦", "怕", "心情", "情绪", "高兴", "哭", "气")):
            return "emotion"
        return "lexical"

    rows = _db.query_log_pending(limit)
    probes = []
    seen_q = set()
    for r in rows:
        q = str(r["query"] or "").strip()
        if not q or q in seen_q or _is_social_probe(q):
            continue
        seen_q.add(q)
        hits = json.loads(r["hits"] or "[]")
        scopes = json.loads(r["scopes"] or "[]")
        if not hits:
            continue
        probes.append(
            {
                "query": q,
                "expected": hits[:5],
                "scope": scopes[0] if scopes else None,
                "category": "subject" if scopes and str(scopes[0]).startswith("npc:") else _probe_category(q),
            }
        )
    if not probes:
        return "没有待导出的查询日志（先让机器人跑一阵子）"
    # 证据门控/评测集联动修复：必须写活库 DATA_DIR（persona-<pack>），否则消融/管理台读不到
    dest = pathlib.Path(out) if out else _shared.DATA_DIR / "probes.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(probes, ensure_ascii=False, indent=2), encoding="utf-8")
    _db.query_log_mark_exported([r["id"] for r in rows])
    return f"已导出 {len(probes)} 条评测集到 {dest}（下次 memory-grow 自动跑 eval 对比 baseline）"


def cmd_persona_probes(out: str = "") -> str:
    """从 ai scope 身份/偏好记忆自动生成评测探针（换人设后无需手写「你是谁」类探针）。"""
    from plugins import _db
    probes = []

    def _add(key, queries, category):
        facts = [r["fact"] for r in _db.memory_rows("ai") if r.get("key") == key]
        if not facts:
            return
        for q in queries:
            probes.append(
                {"query": q, "expected": [f[:24] for f in facts[:3]], "scope": "ai", "category": category}
            )

    _add("identity", ["你是谁", "你是做什么的"], "identity")
    _add("experience_persona", ["你是怎么出道的"], "identity")

    # 偏好按喜欢/讨厌方向区分，expected 各取对应方向的 fact 子串
    pref = [r["fact"] for r in _db.memory_rows("ai") if r.get("key") == "preference"]
    likes = [f for f in pref if "喜欢" in f]
    dislikes = [f for f in pref if "讨厌" in f or "不喜欢" in f]
    if likes:
        probes.append({"query": "你喜欢什么", "expected": [f[:24] for f in likes], "scope": "ai", "category": "attribute"})
    if dislikes:
        probes.append({"query": "你讨厌什么", "expected": [f[:24] for f in dislikes], "scope": "ai", "category": "attribute"})

    if not probes:
        return "ai scope 没有 identity/preference 记忆（先同步 persona）"

    dest = pathlib.Path(out) if out else _shared.DATA_DIR / "probes_persona.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(probes, ensure_ascii=False, indent=2), encoding="utf-8")
    return f"已从 persona 记忆生成 {len(probes)} 条评测探针 -> {dest}"


def cmd_init(pack_name: str = "") -> str:
    """一键初始化新实例（换人设/换用户后开箱即用）：
    播种人设 → 向量化 → 建 BM25/向量索引 → 生成 persona 评测探针 → 落基线。"""
    from plugins import _db, _shared
    from memory import embedder, lexical, vecindex
    import agent
    import memory

    steps = []
    if pack_name:
        try:
            from tools.admin import cmd_persona_switch
            steps.append("切 pack: " + cmd_persona_switch(pack_name))
        except Exception as e:
            steps.append(f"切 pack 失败: {e}")

    # 1. 播种人设（persona.md → ai scope 结构化字段）
    try:
        steps.append("人设: " + str(agent.persona.sync_identity()))
    except Exception as e:
        steps.append(f"人设失败: {e}")

    # 2. 向量化缺少 embedding 的记忆
    rows = [r for r in _db.memory_rows() if not _db.vec_loads(r.get("embedding"))]
    embedded = 0
    if embedder.enabled() and rows:
        for i in range(0, len(rows), 64):
            part = rows[i:i + 64]
            vecs = embedder.embed([r["fact"] for r in part])
            if not vecs:
                break
            for r, vec in zip(part, vecs):
                _db.memory_update_embedding(r["scope"], r["key"], r["fact"], vec)
                embedded += 1
    steps.append(f"向量化: {embedded} 条")

    # 3. 建索引
    steps.append(f"BM25 索引: {lexical.bm25_rebuild()} 文档")
    steps.append(f"向量索引: {vecindex.build() if embedder.enabled() else '跳过（embedder 未启用）'}")

    # 4. 生成 persona 探针 + 落基线
    try:
        probes_path = _shared.DATA_DIR / "probes.json"
        cmd_persona_probes(str(probes_path))
        probes = json.loads(probes_path.read_text(encoding="utf-8"))
        result = memory.run_eval(probes, k=5)
        _db.kv_set("memory", "eval_baseline", result)
        steps.append(f"基线: recall={result.get('recall_at_k')}（{len(probes)} 条探针）")
    except Exception as e:
        steps.append(f"基线跳过: {e}")

    return "\n".join(steps)


def cmd_memory_merge(scope: str = "", window: int = 10) -> str:
    """时序引导碎片合并：把同一时间窗口（valid_from 事件时间）内的孤立短事实合并成完整事实。"""
    from memory.backfill import merge_fragments
    n = merge_fragments(scope or None, window_minutes=window)
    return f"碎片合并：写入 {n} 条完整事实"


def cmd_memory_calibrate(file: str, k: int) -> str:
    """用评测集训练置信度标定映射。"""
    import memory
    try:
        with open(file, encoding="utf-8") as f:
            probes = json.load(f)
    except Exception as e:
        return f"评测集读取失败：{e}"
    return json.dumps(memory.calibrate_train(probes, k=k), ensure_ascii=False, indent=2)


def cmd_memory_sessions(scope: str, limit: int) -> str:
    """查看会话。"""
    import memory
    return json.dumps(
        memory.session_rows(scope or None, None, 0, limit), ensure_ascii=False, indent=2
    )


def cmd_memory_history(scope: str, limit: int) -> str:
    """查看记忆变更历史（合并/纠错/遗忘的旧值与新值）。"""
    from plugins import _db
    rows = _db.history_rows(scope or None, limit=limit)
    return json.dumps(rows, ensure_ascii=False, indent=2) or "（暂无历史）"


def cmd_memory_feedback(scope: str, limit: int) -> str:
    """查看用户反馈日志（纠错/确认/点赞），弱监督学习数据源。"""
    from plugins import _db
    rows = _db.feedback_rows(scope or None, limit=limit)
    return json.dumps(rows, ensure_ascii=False, indent=2) or "（暂无反馈）"


def cmd_relationship(scope: str) -> str:
    """查看 AI 与用户的关系状态（trust/familiarity/closeness/stage）。"""
    import memory
    if scope:
        return json.dumps(memory.relationship_describe(scope) or "（无记录）", ensure_ascii=False, indent=2)
    return json.dumps(memory.relationship_rows(), ensure_ascii=False, indent=2)


def cmd_memory_governance(scope: str) -> str:
    """Memory Governance 报告（v3.1 §9）：遗忘/巩固/冲突/隐私现状。"""
    import memory
    return json.dumps(memory.governance_report(scope or None), ensure_ascii=False, indent=2)


def cmd_memory_trace(scope: str, since: str, limit: int, out: str) -> str:
    """导出记忆处理轨迹（JSON，程序分析用）。"""
    from plugins import _db
    rows = _db.trace_rows(scope or None, since or None, limit)
    text = json.dumps(rows, ensure_ascii=False, indent=2)
    if out:
        pathlib.Path(out).write_text(text, encoding="utf-8")
        return f"已导出 {len(rows)} 条轨迹到 {out}"
    return text


def cmd_memory_trace_md(scope: str, since: str, limit: int) -> str:
    """导出记忆处理轨迹（Markdown，人工阅读用）。"""
    from plugins import _db
    import memory
    rows = _db.trace_rows(scope or None, since or None, limit)
    reviews = _db.trace_review_map([r["id"] for r in rows])
    return memory.trace_markdown(rows, reviews)


def cmd_memory_trace_review(
    trace_id: int,
    extraction=None,
    decision=None,
    confidence=None,
    provenance=None,
    privacy=None,
    comment: str = "",
    reviewer: str = "",
) -> str:
    """人工评分轨迹（v11）：多维度 1~5 分，评分驱动行为调整。"""
    import memory
    scores = {
        k: v
        for k, v in {
            "extraction": extraction,
            "decision": decision,
            "confidence": confidence,
            "provenance": provenance,
            "privacy": privacy,
        }.items()
        if v is not None
    }
    return memory.trace_score(int(trace_id), scores, comment, reviewer)


def cmd_memory_trace_adjust() -> str:
    """查看评分驱动的行为调整（v11）。"""
    import memory
    return json.dumps(memory.trace_adjustments(force=True), ensure_ascii=False, indent=2)


def cmd_memory_conv_md(scope: str, since: str, limit: int) -> str:
    """导出对话评分报告（v33，Markdown 人工阅读用）。"""
    from plugins import _db
    import memory
    rows = _db.conv_rows(scope or None, since or None, limit)
    reviews = _db.conv_review_map([r["id"] for r in rows])
    return memory.conv_markdown(rows, reviews)


def cmd_memory_conv_review(
    conv_id: int,
    remember=None,
    natural=None,
    emotional=None,
    proactive=None,
    boundary=None,
    comment: str = "",
    reviewer: str = "",
) -> str:
    """人工评分对话（v33）：五维 1~5，低分写审计+归因（不自动调参）。"""
    import memory
    scores = {
        k: v
        for k, v in {
            "remember": remember,
            "natural": natural,
            "emotional": emotional,
            "proactive": proactive,
            "boundary": boundary,
        }.items()
        if v is not None
    }
    return memory.conv_score(int(conv_id), scores, comment, reviewer)


def cmd_memory_conv_report() -> str:
    """查看对话五维诊断（v33）：维度均值 + 低分归因方向 + 可执行建议。"""
    import memory
    data = memory.conv_report(force=True)
    data["suggested_adjustments"] = memory.conv_adjustments()
    return json.dumps(data, ensure_ascii=False, indent=2)


def cmd_reflection_report(limit: int = 20) -> str:
    """反思抽检报告：输出最近写入的反思 + 质量统计，供人工审阅。"""
    from plugins import _db
    import memory.stats as st
    rows = _db.memory_rows("ai", "reflection")
    rows.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    c = st.counters()
    lines = [
        "# 反思抽检报告",
        "",
        f"- 产出(raw)：{int(c.get('reflect_raw', 0))}",
        f"- 过滤(rejected)：{int(c.get('reflect_rejected', 0))}",
        f"- 写入(insight)：{int(c.get('reflect_insight', 0))}",
        "",
        f"最近 {min(limit, len(rows))} 条：",
        "",
    ]
    for r in rows[:limit]:
        lines.append(f"- {r.get('updated_at', '')} | {r['fact']}")
    return "\n".join(lines)


def cmd_memory_conv_adjust(apply: bool = False, rollback: bool = False) -> str:
    """对话评分调参框架：--apply 写入建议/参数，--rollback 回滚，默认只读。"""
    import memory
    if rollback:
        return json.dumps(memory.conv_rollback_adjustments(), ensure_ascii=False, indent=2)
    if apply:
        return json.dumps(memory.conv_apply_adjustments(), ensure_ascii=False, indent=2)
    return json.dumps(memory.conv_adjustments(), ensure_ascii=False, indent=2)


def cmd_memory_consolidate(scope: str = "", dry_run: bool = False) -> str:
    """记忆整合：合并碎片 + 冲突处理 + 巩固/遗忘。"""
    from memory import consolidator
    return json.dumps(consolidator.run(scope=scope or None, apply=not dry_run), ensure_ascii=False, indent=2)


def cmd_reflection_stats() -> str:
    """反思质量统计：最近 daily_reflect 产出/过滤/写入计数。"""
    import memory.stats as _st
    c = _st.counters()
    return json.dumps({
        "reflect_raw": int(c.get("reflect_raw", 0)),
        "reflect_rejected": int(c.get("reflect_rejected", 0)),
        "reflect_insight": int(c.get("reflect_insight", 0)),
        "reject_rate": round(
            int(c.get("reflect_rejected", 0)) / max(1, int(c.get("reflect_raw", 0))),
            3,
        ),
    }, ensure_ascii=False, indent=2)


def cmd_goal(action, title, priority, scope, motivation="", confidence=0.7) -> str:
    """目标规划（v6）：goal add/list/done。"""
    import memory
    scope = scope or "cli"
    if action == "add":
        return memory.goal_add(
            scope, title, priority=priority, motivation=motivation, confidence=confidence
        )
    if action == "list":
        return json.dumps(memory.goal_list(scope), ensure_ascii=False, indent=2)
    if action == "done":
        return memory.goal_update(scope, title, status="done")
    return "用法：goal add <标题> [--priority N] | goal list | goal done <标题>"


def cmd_consult(text: str, scope: str) -> str:
    """决策顾问单轮（v6）：一次一问。"""
    import memory
    return memory.consult_turn(scope or "cli", text)


def cmd_expression(text: str, scope: str) -> str:
    """语言语义解释层（v7）：表达分析 + 用户表达画像。"""
    import memory
    info = memory.expression_analyze(text)
    prof = memory.expression_profile(scope or "cli")
    return json.dumps({"analyze": info, "profile": prof}, ensure_ascii=False, indent=2)


def cmd_world(scope: str) -> str:
    """用户中心世界模型（v8）：快照 + 现状统计。"""
    import memory
    return json.dumps(
        {
            "snapshot": memory.world_snapshot(scope or "cli"),
            "stats": memory.world_stats(scope or None),
        },
        ensure_ascii=False,
        indent=2,
    )


def cmd_character_build(name: str) -> str:
    """输入人物名称，自动生成设定/经历档案并存入统一记忆（char:<名>），
    同时写入 docs/characters/<名>.md 供人工审阅/编辑。"""
    import memory
    info = memory.character_build(name)
    if info.get("error"):
        return json.dumps(info, ensure_ascii=False, indent=2)
    path = memory.character_write_md(name)  # 从记忆库渲染并写入 md（与记忆保持一致）
    return json.dumps(
        {**info, "md": str(path)}, ensure_ascii=False, indent=2
    )


def cmd_character_sync(arg: str) -> str:
    """把编辑后的 md 档案同步回记忆库（arg 为人物名或 md 文件路径）。"""
    import memory
    is_path = "/" in (arg or "") or "\\" in (arg or "") or (arg or "").lower().endswith(".md")
    info = memory.character_sync(path=arg) if is_path else memory.character_sync(name=arg)
    return json.dumps(info, ensure_ascii=False, indent=2)


def cmd_mind_status(scope=""):
    """心智状态快照（mind_state + 意图 + 程序记忆统计）。"""
    from memory import mind, procedures
    import memory.stats as stats_mod
    snap = mind.snapshot(str(scope or ""), "")
    snap["procedures"] = procedures.stats()
    snap["counters"] = stats_mod.counters()
    return json.dumps(snap, ensure_ascii=False, indent=2)


def cmd_procedures_list():
    """列出程序记忆（System 1 习惯）。"""
    from memory import procedures
    return procedures.report()


def cmd_living_bootstrap():
    """人设→场景生成：按 persona 补齐家里物品（只新增不覆盖）。"""
    from memory import living
    return json.dumps(living.bootstrap_from_persona(), ensure_ascii=False, indent=2)


def cmd_subjects_status():
    """多主体记忆：列出已注册主体及各视角数据量。"""
    from memory import subjects
    from plugins import _db
    rows = []
    for name in subjects.registered():
        nscope = subjects.scope_of(name)
        rows.append({
            "name": name, "scope": nscope,
            "memories": len(_db.memory_rows(nscope)),
            "events": len(_db.event_rows(nscope)),
        })
    return json.dumps({"enabled": subjects.enabled(), "subjects": rows}, ensure_ascii=False, indent=2)


def cmd_consistency_eval() -> str:
    """双轨制一致性：失效队列长度 + 本次重算数。"""
    from plugins import _db
    pending = len(_db.invalidation_rows(100))
    from memory import controller
    done = controller.reconcile_pending()
    return json.dumps({"pending": pending, "reconciled": done["reconciled"]}, ensure_ascii=False, indent=2)


def cmd_policy_classify() -> str:
    """事实分类探针：'含关键词但其实是过程' 的句子误判率（policy-classify）。"""
    from memory import policy
    return json.dumps(policy.classify_report(), ensure_ascii=False, indent=2)


def cmd_revive_status(scope: str = "") -> str:
    """主动消息决策状态：泊松概率 + 贝叶斯用户状态（只读，不消费触发）。"""
    from memory import revive
    return json.dumps(revive.peek(scope or None), ensure_ascii=False, indent=2)


def cmd_bandit_status(scope: str = "") -> str:
    """回应策略 bandit 后验：各策略均值/样本数 + 上次选择。"""
    from memory import bandit
    return json.dumps(bandit.status(scope), ensure_ascii=False, indent=2)


def cmd_topic_vad_backfill() -> str:
    """给只有 mood 标签的旧议题补近似 VAD/复合情绪（幂等，也可随 memory-grow 自动跑）。"""
    from memory import topic
    return json.dumps(topic.backfill_vad(), ensure_ascii=False, indent=2)


def cmd_memory_source_backfill() -> str:
    """证据门控：历史记忆 source 归一（ingest→user / persona→pack），幂等。"""
    from plugins import _db
    return json.dumps(_db.memory_source_normalize(), ensure_ascii=False, indent=2)


def cmd_pollution_scan(scope: str = "", apply: bool = False) -> str:
    """存量污染扫描（2026-08-16）：对库内 source=user 的记忆做反向出处校验——
    事实的内容词必须在用户历史消息（conv_log.user_text + sessions.summary）里有
    字面出处。分级：
      strong   陈述句出处 >=2/3 → 用户亲口说过 → 保留
      partial  部分出处 → 推断/概括 → 降级为 ai_edit
      weak     仅问句命中 → 语义反转（"你玩过吗"→"用户玩过"）→ 降级
      none     无出处 → 提取幻觉固化 → 删除候选
    --apply 才真正执行降级/删除（core 身份记忆保护：只降级不删除）；
    默认 dry-run 只报告。修复目标：早前"颜色是橘色"式污染在库里积压的同类条目，
    让假来源声明不再有 source=user 的"证据"可引用。"""
    import re as _re
    from plugins import _db
    from memory import controller as ctl
    # 1) 出处池：用户历史消息（conv_log 全文 + sessions 摘要）
    rows = _db.conv_rows(limit=10 ** 6)
    msgs = [(r.get("user_text") or "").strip() for r in rows]
    for s in _db.session_rows(limit=10 ** 6):
        sm = (s.get("summary") or "").strip()
        if sm:
            msgs.append(sm)
    msgs = [m for m in msgs if m]
    quest = _re.compile(r"[？?]|吗$|呢$|么$|是不是|有没有|什么|多少|哪|几号|如何|怎么|累不累|对不对")
    stmt_msgs = [m for m in msgs if not quest.search(m)]
    quest_msgs = [m for m in msgs if quest.search(m)]
    # 2) 待检记忆：source=user 且 active
    rows = _db.memory_rows(scope=scope or None)
    cand = [r for r in rows if (r.get("source") or "") == "user" and (r.get("status") or "") == "active"]
    if not cand:
        return "污染扫描：无 source=user 的记忆"
    # 3) 逐条分级
    buckets = {"strong": [], "partial": [], "weak": [], "none": [], "empty": []}
    for r in cand:
        lv = ctl.pollution_level(r["fact"], stmt_msgs, quest_msgs)
        buckets.setdefault(lv, []).append(r)
    # 4) 报告
    lines = [f"污染扫描：user 记忆 {len(cand)} 条（出处池：陈述 {len(stmt_msgs)} / 问句 {len(quest_msgs)}）"]
    if apply:
        n_del = n_dem = 0
        # weak（仅问句）与 none（无出处）都是"用户从未陈述过"→ 删除；
        # partial（部分出处，用户说过大部分）→ 降级；core 身份记忆只降不删
        for r in buckets["none"] + buckets["weak"] + buckets["partial"]:
            if (r.get("mclass") or "") == "core":
                _db.memory_set_source(r["scope"], r["key"], r["fact"], "ai_edit")
                _db.audit_add("pollution_demote", f"core保护降级 {r['fact'][:40]}", "auto")
                n_dem += 1
                continue
            if r in buckets["none"] or r in buckets["weak"]:
                _db.memory_delete(r["scope"], r["key"], r["fact"])
                _db.audit_add("pollution_del", f"无陈述出处删除 {r['fact'][:40]}", "auto")
                n_del += 1
            else:
                _db.memory_set_source(r["scope"], r["key"], r["fact"], "ai_edit")
                _db.audit_add("pollution_demote", f"部分支撑降级 {r['fact'][:40]}", "auto")
                n_dem += 1
        lines.append(f"已执行：删除 {n_del}，降级 {n_dem}")
    for lv, label in (("strong", "保留（有陈述出处）"), ("partial", "降级候选（部分出处）"),
                      ("weak", "降级候选（仅问句→语义反转）"), ("none", "删除候选（无出处）"),
                      ("empty", "无法判定（无内容词）")):
        items = buckets.get(lv, [])
        if not items:
            continue
        lines.append(f"  [{lv}] {label} × {len(items)}")
        for r in items[:12]:
            lines.append(f"    · ({r.get('confidence', '?')}) {r['fact'][:56]}")
        if len(items) > 12:
            lines.append(f"    … 其余 {len(items) - 12} 条")
    return "\n".join(lines)


def cmd_conflict_scan(scope: str = "", apply: bool = False) -> str:
    """存量矛盾扫描（v2.3 P0-2）：同 scope 内 active 记忆中同实体（"X是Y"主语）不同
    属性值且无上下位包含 → 矛盾候选。--apply 时低置信一方降权 + 标 contested
    （core 只降权不标），audit 留痕。默认 dry-run。"""
    from memory import controller as ctl
    text, conflicts = ctl.conflict_scan(scope, apply)
    return text


def cmd_calendar_check(scope: str = "", apply: bool = False) -> str:
    """存量日历校验（v2.3 P1-2）：库内"X号是周Y"事实与当月真实日历比对
    （"31号是周日"在 2026-08 实际是周一 → 错误事实）。--apply 降权+contested。"""
    from memory import controller as ctl
    text, bad = ctl.calendar_check(scope, apply)
    return text


def cmd_calibrate_feedback() -> str:
    """校准闭环（v2.3 P2）：用户纠错调查结论（feedback investigate:*）回流为
    置信度校准映射——update=证伪/keep=证实/uncertain=弱样本，分桶统计实际正确率。"""
    import memory
    return json.dumps(memory.calibrate_from_feedback(), ensure_ascii=False, indent=2)


def cmd_appointment_clean() -> str:
    """巡检清理：含黑名单词的约定条目标记 done（防催约复活编造）。"""
    from memory import appointment
    return json.dumps(appointment.clean(), ensure_ascii=False, indent=2)
