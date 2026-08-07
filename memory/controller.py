"""Memory Controller：分析 → 分类路由 → 提取 → 存储（多库）→ 记忆更新 → 策略反馈 → AI 自身记忆。"""

import re
import os
from datetime import datetime

from plugins import _db, _shared
from memory import analysis, embedder, extract, graph, lexical, policy, sensitive, topic, trace, update, world


def merge_facts(existing, new, cap=30):
    """合并事实（去重、截断到 cap 条）。"""
    seen, out = set(existing), list(existing)
    for fact in new:
        fact = extract.nice_fact(fact).strip()
        if fact and fact not in seen:
            seen.add(fact)
            out.append(fact)
    return out[-cap:]


def _maybe_embed(facts):
    if not facts or not embedder.enabled():
        return None
    vecs = embedder.embed(facts)
    return dict(zip(facts, vecs)) if vecs else None


def _ai_experience_min_importance() -> float:
    from plugins import _shared
    policy_cfg = (_shared.CONFIG.get("memory", {}).get("core", {}) or {}).get("policy", {}) or {}
    return float(policy_cfg.get("ai_experience_min_importance", 0.75))


def _scene_meta(scope, key):
    """场景元数据：audience（private/group:<gid>/public）+ speaker。"""
    if scope == "ai" or scope.startswith("ai:"):
        return "public", "ai", "short"
    if scope.startswith("c2c:"):
        return "private", key or scope.split(":", 1)[1], "short"
    if scope.startswith("group:"):
        return "group:" + scope.split(":", 1)[1], key, "short"
    if scope.startswith("group_all:"):
        return "group:" + scope.split(":", 1)[1], "", "short"
    return "", key, "short"


# 纠错匹配时的通用词/二元组（不参与"特征词"判断，避免"没养/记错"这类词误连同类事实）
_CORRECTION_STOP = {
    "我", "你", "他", "她", "它", "们", "没", "不", "是", "了", "的", "就", "在", "有",
    "吗", "吧", "啊", "呢", "什么", "怎么", "根本", "不是", "其实", "更正", "纠正",
    "记错", "错了", "说错", "撤销", "忘掉", "别记", "反了", "才", "很", "太", "也",
    "还", "都", "给", "把", "被", "这", "那",
}
_CORRECTION_STOP_BIGRAMS = {
    "你记", "记错", "错了", "不是", "其实", "根本", "本没", "了是", "了一", "么说",
}
_TRANSIENT_EMOTION_KEEP = ("养", "喜欢", "讨厌", "项目", "工作", "猫", "狗", "买", "学", "做", "去", "追")


def _decay_conflicts(scope, key, text, an=None) -> list:
    """纠错反馈：用户否定时，降低与当前文本相关记忆的可信度。
    强纠错（“记错了/不对/不是”）用 dispute LR=0.3，轻纠错（“其实/改一下”）用 conflict LR=0.5。
    匹配收紧：只影响与纠错内容同主题的记忆（特征词 = 去掉通用词后的词/二元组），
    向量兜底仅在特征词零命中时取相似度最高的 1 条（阈值 0.5），避免误伤同类但无关的事实。
    返回受影响明细 [{fact, confidence, new_confidence, kind}]。"""
    specific = (
        set(extract.tokenize(text or "")) - _CORRECTION_STOP
    ) | (extract.fact_keywords(text or "") - _CORRECTION_STOP_BIGRAMS)
    if not specific:
        return []
    strong = bool((an or {}).get("correction_strong"))
    kind = "dispute" if strong else "conflict"
    candidates = set()
    for r in _db.memory_rows(scope, key):
        if (extract.fact_keywords(r["fact"]) & specific) or (
            set(extract.tokenize(r["fact"])) & specific
        ):
            candidates.add(r["fact"])
    if not candidates and embedder.enabled():
        try:
            from memory import reasoning

            hits = reasoning.retrieve(text, [scope], top_k=1, min_score=0.5)
            if hits:
                candidates.add(hits[0][0])
        except Exception:
            pass
    if not candidates:
        return []
    # 纠正调查（v8）：AI 不盲从，先调查再决定 update/keep/uncertain
    decision = world.investigate_correction(scope, key, text, sorted(candidates)[:1], an)
    details = []
    now_ts = datetime.now().isoformat(timespec="seconds")
    for fact in sorted(candidates)[:2]:  # 每次最多影响 2 条，防误伤扩散
        row = next((r for r in _db.memory_rows(scope, key) if r["fact"] == fact), None)
        if not row:
            continue
        cur = float(row.get("confidence", 0.7))
        if decision["action"] == "update":
            # 调查确认旧记忆过时 → 直接废弃（保留历史，不删除）
            _db.memory_set_status(scope, key, fact, "superseded", valid_to=now_ts)
            _db.history_add(
                scope, key, fact, "supersede",
                reason=f"用户纠正并经核查（update：{decision['reason']}）",
                old_confidence=cur, new_confidence=0.0,
            )
            if row.get("mclass") == "core" or scope == "ai" or scope.startswith("ai:"):
                _db.audit_add("review_required", fact[:100], "核心记忆被纠正并更新", operator="auto")
            details.append(
                {"fact": fact, "confidence": cur, "new_confidence": 0.0, "kind": "update", "decision": "update"}
            )
        elif decision["action"] == "uncertain":
            # 无法确认 → 冲突降权 + 标记 contested 待核查
            new_conf = policy.update(cur, "conflict")
            _db.memory_set_confidence(scope, key, fact, new_conf)
            _db.memory_set_status(scope, key, fact, "contested")
            _db.history_add(
                scope, key, fact, "conflict",
                reason=f"纠正待核查（uncertain：{decision['reason']}）",
                old_confidence=cur, new_confidence=new_conf,
            )
            details.append(
                {"fact": fact, "confidence": cur, "new_confidence": new_conf, "kind": "conflict", "decision": "uncertain"}
            )
        else:
            # 调查后纠正不成立 → 保留旧记忆，只记审计
            _db.history_add(
                scope, key, fact, "investigate",
                reason=f"核查后保留旧记忆（keep：{decision['reason']}）",
                old_confidence=cur,
            )
            details.append(
                {"fact": fact, "confidence": cur, "new_confidence": cur, "kind": "keep", "decision": "keep"}
            )
    return details


def _record_ai_experience(scope, key, text, ts, importance):
    """AI 自身记忆：重要对话沉淀为 experience（与用户记忆同表同格式，可信度 0.6）。"""
    if importance < _ai_experience_min_importance() or not text:
        return
    ai_scope = _ai_scope()
    snippet = str(text)[:60]
    content = f"在 {scope} 场景与用户交流了「{snippet}」"
    _db.memory_add(ai_scope, "experience", content, ts, None, 0.6, "experience")
    policy.touch(ai_scope, "experience", content, importance=0.6)


def _ai_scope() -> str:
    """与 agent/persona 一致的多 Agent 命名空间。"""
    aid = os.getenv("AGENT_ID", "").strip()
    return f"ai:{aid}" if aid else "ai"


def _supersede_old(scope, key, text, ts) -> int:
    """时间推理（v5）：检测状态变化（转/换/改用/现在/戒）→ 旧相关记忆标记 superseded 并记历史。
    规则：状态词 + 新旧事实都有专名实体（如 Python→Java），视为同一话题的状态更新。"""
    if not any(w in (text or "") for w in ("转", "换", "改用", "现在", "不喜欢", "戒", "重新")):
        return 0
    from memory.extract import extract_entities
    new_ents = [e.lower() for e in extract_entities(text or "")]
    if not new_ents:
        return 0
    n = 0
    for r in _db.memory_rows(scope, key):
        if r.get("status") == "superseded":
            continue
        old_ents = [e.lower() for e in extract_entities(r["fact"])]
        if old_ents and new_ents:  # 双方都有专名 → 同一类话题的状态更新
            _db.memory_set_status(scope, key, r["fact"], "superseded", valid_to=ts)
            _db.history_add(
                scope, key, r["fact"], "supersede",
                reason="状态变化（时间推理）", new_value=(text or "")[:100],
            )
            n += 1
    return n


def message_gain(text, scope, key="") -> dict:
    """信息增益评分（0~1）：新实体/新事实/数字专名/状态变化 → 高分；语气词 → 低分。
    用于替代固定时间节流：只有低信息消息才跳过提取。"""
    t = str(text or "").strip()
    if not t:
        return {"score": 0.0, "reasons": ["空消息"]}
    if len(t) <= 2 or t.lower() in ("哈哈", "呵呵", "嗯", "哦", "好", "好的", "行", "ok", "在", "是"):
        return {"score": 0.01, "reasons": ["语气词"]}
    score = 0.0
    reasons = []
    if re.search(r"\d", t):
        score += 0.25
        reasons.append("含数字")
    if re.search(r"[A-Za-z]{2,}", t):
        score += 0.2
        reasons.append("含专名")
    if analysis.detect_correction(t):
        score += 0.3
        reasons.append("状态改变")
    existing = set()
    for r in _db.memory_rows(scope, key):
        existing |= extract.fact_keywords(r["fact"])
    qt = extract.fact_keywords(t)
    if qt:
        novelty = 1.0 - len(qt & existing) / max(1, len(qt))
        score += 0.45 * novelty
        if novelty >= 0.6:
            reasons.append("新事实")
    if analysis.detect_playful(t) and len(t) <= 8:
        score = min(score, 0.15)
        reasons.append("玩笑")
    return {"score": round(min(1.0, score), 2), "reasons": reasons}


def _fuse_emotion(an, scope, key):
    """情绪多源融合（v3.1 §7）：规则 + LLM 之后，叠加历史状态与反馈。
    近 3 条带情绪的记忆中 ≥2 条消极、且当前文本无积极信号时，倾向判低落。"""
    if not an or an.get("emotion") != "平静":
        return an
    rows = [r for r in _db.memory_rows(scope, key) if abs(float(r.get("valence", 0.0))) > 0.01]
    rows.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    recent = rows[:3]
    if len(recent) >= 2 and sum(1 for r in recent if float(r["valence"]) < 0) >= 2:
        out = dict(an)
        out["emotion"] = "低落"
        out["emotion_fused"] = True  # 历史状态融合而来（仅影响语气，不触发临时情绪拒绝）
        metrics = analysis.EMOTION_METRICS["低落"]
        out["valence"] = metrics["valence"]
        out["arousal"] = metrics["arousal"]
        return out
    return an


def ingest(scope, key, text, reply="", facts=None, confidence=None, source=None):
    """主入口：分析 → 提取 → 相似合并 → 存事实 + 结构化属性 + 事件图 + 词法索引 → 策略 → AI 经历。
    返回 {"facts": 新增数, "events": 事件数, "refreshed": 合并数, "analysis": {...}, "confidence": ...}。"""
    an = analysis.analyze(text or "", reply or "")
    if text or reply:
        an = analysis.enrich(an, text or "", reply or "")
    an = _fuse_emotion(an, scope, key)
    conf = float(confidence if confidence is not None else an.get("confidence", 0.7))
    # 评分驱动行为（v11）：置信度维度低分 → 抑制过度自信
    adj = trace.adjustments()
    conf = min(0.95, conf * float(adj.get("confidence_factor", 1.0)))
    ts = datetime.now().isoformat(timespec="seconds")
    consent = {"keep": True, "reason": ""}
    # Memory Consent Layer（v5 §P1-3 + v7 语言层）：玩笑/夸张表达且无实质信息 → 不长期保存
    joke_prob = float(an.get("joke_probability", 0.0)) if an else 0.0
    if (joke_prob >= 0.7 or an.get("playful")) and not an.get("correction"):
        if message_gain(text, scope, key)["score"] < float(adj.get("igt_threshold", 0.3)):
            consent = {"keep": False, "reason": f"玩笑/夸张表达（p={joke_prob}）且无实质信息"}
            trace.record(
                scope, raw_content=text, semantic=an, action="reject", confidence=conf,
                reasoning=consent["reason"], modules=trace.detect_modules(scope, key, text),
            )
            return {
                "facts": 0, "events": 0, "refreshed": 0,
                "disputed": 0, "dispute_details": [],
                "analysis": an, "confidence": conf,
                "consent": consent,
            }
        conf = min(conf, 0.45)
        consent = {"keep": True, "reason": f"玩笑语境（p={joke_prob}），低可信度短期保存"}
    elif joke_prob >= 0.4 and not an.get("correction"):
        conf = min(conf, 0.6)
        consent = {"keep": True, "reason": "吐槽/反讽表达，适度降权"}
    if (
        consent["keep"]
        and analysis._emotion_of(text or "") != "平静"  # 只认文本规则情绪，历史融合情绪不触发拒绝
        and not an.get("correction")
        and len((text or "").strip()) <= 8
        and not any(w in (text or "") for w in _TRANSIENT_EMOTION_KEEP)
    ):
        # 临时情绪宣泄（v6 建议 §14）：无事实载体的情绪句不长期保存
        emotion_label = analysis._emotion_of(text or "")
        consent = {"keep": False, "reason": f"临时情绪宣泄（{emotion_label}），不长期保存"}
        trace.record(
            scope, raw_content=text, semantic=an, action="reject", confidence=conf,
            reasoning=consent["reason"], modules=trace.detect_modules(scope, key, text),
        )
        return {
            "facts": 0, "events": 0, "refreshed": 0,
            "disputed": 0, "dispute_details": [],
            "analysis": an, "confidence": conf,
            "consent": consent,
        }
    _supersede_old(scope, key, text, ts)
    disputed = []
    if an.get("correction"):
        disputed = _decay_conflicts(scope, key, text, an)
        if any(d.get("decision") == "update" for d in disputed):
            conf = max(conf, 0.7)  # 用户纠正被核实 → 新事实给更高可信度
        action = disputed[0]["decision"] if disputed else "uncertain"
        _db.feedback_add(  # 调查结果分级反馈（v6 建议 §2）：update=1.0 / uncertain=0.5 / keep=0.2
            scope, key, f"investigate:{action}",
            weight={"update": 1.0, "uncertain": 0.5, "keep": 0.2}.get(action, 0.5),
            fact=disputed[0]["fact"] if disputed else "",
            detail=(text or "")[:200],
        )
    new_facts = facts
    if not new_facts:
        new_facts = (
            extract.extract_with_structure(f"用户：{(text or '')[:500]}\n机器人：{(reply or '')[:500]}")
            if text
            else []
        )
    new_facts = [f for f in new_facts if str(f).strip()]
    if an.get("correction"):
        # 纠错消息：剥离"用户指出/用户纠正"等元描述前缀，保留被纠正后的新事实
        cleaned = []
        for f in new_facts:
            for pre in ("用户指出", "用户纠正", "用户说", "用户表示", "用户否认"):
                if f.startswith(pre):
                    f = f[len(pre):].strip()
            if f:
                cleaned.append(f)
        new_facts = cleaned
    if not new_facts:
        trace.record(
            scope, raw_content=text, semantic=an, action="reject", confidence=conf,
            reasoning="无值得保存的信息", modules=trace.detect_modules(scope, key, text),
        )
        return {
            "facts": 0,
            "events": 0,
            "refreshed": 0,
            "disputed": len(disputed),
            "dispute_details": disputed,
            "consent": consent,
            "analysis": an,
            "confidence": conf,
        }

    src = source or f"ingest:{ts[:19]}"
    audience, speaker, mclass = _scene_meta(scope, key)

    # 记忆更新：近似重复合并（刷新旧记录，不堆叠）
    rows = _db.memory_rows(scope, key)
    existing_rows = {r["fact"]: r for r in rows}
    additions, refreshed = [], 0
    for f in new_facts:
        if f in existing_rows:
            continue
        dup = update.find_near_dup(scope, key, f, rows=rows, threshold=0.9)
        if dup:
            old_conf = float(existing_rows[dup].get("confidence", 0.7))
            new_conf = max(conf, old_conf)
            update.refresh(scope, key, dup, confidence=new_conf, source=src)
            _db.history_add(
                scope, key, dup, "merge", reason="近似重复合并",
                old_value=dup, new_value=f,
                old_confidence=old_conf, new_confidence=new_conf,
            )
            refreshed += 1
            continue
        additions.append(f)

    merged = merge_facts(list(existing_rows), additions)
    confidences, sources, privacies = {}, {}, {}
    audiences, speakers, mclasses, arousals, valences = {}, {}, {}, {}, {}
    for f in merged:
        if f in existing_rows:
            confidences[f] = float(existing_rows[f].get("confidence", 0.7))
            sources[f] = existing_rows[f].get("source", "")
            audiences[f] = existing_rows[f].get("audience", "")
            speakers[f] = existing_rows[f].get("speaker", "")
            mclasses[f] = existing_rows[f].get("mclass") or "short"
            arousals[f] = float(existing_rows[f].get("arousal", 0.0))
            valences[f] = float(existing_rows[f].get("valence", 0.0))
            privacies[f] = float(existing_rows[f].get("privacy", 0.0))
        else:
            confidences[f] = conf
            sources[f] = src
            audiences[f] = audience
            speakers[f] = speaker
            mclasses[f] = mclass
            arousals[f] = float(an.get("arousal", 0.0))
            valences[f] = float(an.get("valence", 0.0))
            privacy, _labels = sensitive.detect(f)
            privacies[f] = privacy
            if privacy >= float(adj.get("privacy_threshold", 0.8)):
                audiences[f] = "private"
    emb = _maybe_embed(merged)
    _db.memory_replace(
        scope,
        key,
        merged,
        ts,
        emb,
        confidences,
        sources,
        audience=audience,
        speaker=speaker,
        mclass=mclass,
        arousal=float(an.get("arousal", 0.0)),
        valence=float(an.get("valence", 0.0)),
        audiences=audiences,
        speakers=speakers,
        mclasses=mclasses,
        arousals=arousals,
        valences=valences,
        privacies=privacies,
    )

    importance = float(an.get("importance", 0.5))
    event_count = 0
    for f in additions:
        if privacies.get(f, 0.0) >= float(adj.get("privacy_threshold", 0.8)):
            # 高隐私：加密且不进索引（只有 /我的记忆 能看）
            _db.memory_delete(scope, key, f)
            _db.memory_add(
                scope, key, sensitive.encrypt_text(f), ts,
                confidence=conf, source=src, audience="private",
                speaker=speaker, mclass=mclass, privacy=privacies[f],
            )
            continue
        if analysis.attr_of(f, an):
            _db.attr_set(scope, key, analysis.attr_of(f, an), f, conf, ts)
        eid, _linked = graph.build_for_fact(
            scope, key, f, etype=an.get("event_type"), importance=importance, ts=ts
        )
        if eid:
            event_count += 1
        tid = topic.link_fact(scope, key, f, an.get("event_type") or "event", conf, an)
        if eid and tid:
            _db.event_set_topic(eid, tid)
        policy.touch(scope, key, f, importance=importance)
    _db.lexicon_sync(scope, key)
    lexical.bm25_upsert(scope, key, [f for f in additions if privacies.get(f, 0.0) < 0.8])
    _record_ai_experience(scope, key, text, ts, importance)
    # Memory Trace（v10）：可解释的处理结果与决策理由
    try:
        modules = trace.detect_modules(scope, key, text)
        if an.get("correction"):
            modules.append("relationship")
        action, memory_id, reasoning = "reject", "", "无新事实"
        if any(d.get("decision") == "update" for d in disputed):
            action, memory_id = "update", disputed[0]["fact"][:100]
            reasoning = f"用户纠正并经核查：{disputed[0].get('decision', '')}"
        elif any(d.get("decision") == "uncertain" for d in disputed):
            action, memory_id = "decay", disputed[0]["fact"][:100]
            reasoning = "纠正待核查，降权并标记 contested"
        elif any(d.get("decision") == "keep" for d in disputed):
            action, memory_id = "reject", disputed[0]["fact"][:100]
            reasoning = "核查后保留旧记忆（用户纠正不成立）"
        elif refreshed > 0:
            action = "merge"
            memory_id = additions[0][:100] if additions else ""
            reasoning = f"近似重复合并 {refreshed} 条"
        elif additions:
            action, memory_id = "create", additions[0][:100]
            reasoning = "提取/确认后新建"
        trace.record(
            scope, raw_content=text, semantic=an,
            candidate="；".join(additions[:3]),
            action=action, memory_id=memory_id,
            confidence=conf, source=src, reasoning=reasoning,
            modules=modules, hint=str(an.get("event_type", "")),
        )
    except Exception:
        pass
    return {
        "facts": len(additions),
        "events": event_count,
        "refreshed": refreshed,
        "disputed": len(disputed),
        "dispute_details": disputed,
        "consent": consent,
        "analysis": an,
        "confidence": conf,
        "ai_experience": bool(importance >= _ai_experience_min_importance()),
    }


def add_fact(scope, key, fact, importance=0.5, confidence=0.8, source="mcp"):
    """单条写入（Hermes MCP / 后台工具用）：写事实 + 属性 + 事件图 + 词法索引 + 策略。"""
    fact = extract.nice_fact(fact).strip()
    if not fact:
        return None
    emb = embedder.embed([fact]) if embedder.enabled() else None
    ts = datetime.now().isoformat(timespec="seconds")
    audience, speaker, mclass = _scene_meta(scope, key)
    _db.memory_add(
        scope,
        key,
        fact,
        ts,
        emb[0] if emb else None,
        confidence=confidence,
        source=source,
        audience=audience,
        speaker=speaker,
        mclass=mclass,
    )
    an = analysis.analyze(fact)
    if analysis.attr_of(fact, an):
        _db.attr_set(scope, key, analysis.attr_of(fact, an), fact, confidence, ts)
    graph.build_for_fact(scope, key, fact, importance=float(importance), ts=ts)
    policy.touch(scope, key, fact, importance=float(importance))
    _db.lexicon_sync(scope, key)
    lexical.bm25_upsert(scope, key, [fact])
    return fact


# ===== 会话结构：时间窗口 + 主题，跨天同主题续接（规则版 + 可选 LLM）=====
def _session_cfg(key, default):
    core = (_shared.CONFIG.get("memory", {}).get("core", {}) or {}).get("session", {}) or {}
    return core.get(key, default)


def _session_topic_of(text) -> str:
    from memory.extract import extract_entities
    ents = extract_entities(text or "")
    return ents[0] if ents else ""


def _session_similar(a, b) -> float:
    ta, tb = set(extract.tokenize(a)), set(extract.tokenize(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(1, min(len(ta), len(tb)))


def _same_session_topic(text, sess) -> bool:
    """规则版：与最近会话主题/摘要词元重叠 ≥0.5；LLM 版可选。"""
    if _session_cfg("llm", False):
        try:
            from memory.backfill import _llm_one
            ans = _llm_one(
                f"以下消息是否与主题「{sess.get('topic', '')}」属于同一件事？只回答 是/否。\n消息：{text[:120]}"
            )
            return "是" in (ans or "")
        except Exception:
            pass
    base = f"{sess.get('topic', '')} {sess.get('summary', '')}"
    return _session_similar(text, base) >= 0.5 or bool(sess.get("topic")) and sess["topic"] in text


def touch(scope, key, text, summary=None) -> int:
    """把一条消息归入会话：窗口内且同主题 → 续接；否则新开会话。返回 session_id。"""
    recent = _db.session_find_recent(scope, key, within_min=int(_session_cfg("window_min", 1440)))
    topic_name = _session_topic_of(text)
    if recent and _same_session_topic(text, recent):
        _db.session_bump(recent["id"], topic=topic_name, summary=summary)
        return recent["id"]
    fallback_topic = recent.get("topic", "") if recent else ""
    return _db.session_create(
        scope, key, topic=topic_name or fallback_topic, summary=summary or text[:100]
    )


def current(scope, key):
    """当前活跃会话（用于指代消解/上下文）。"""
    return _db.session_find_recent(scope, key, within_min=int(_session_cfg("window_min", 1440)))


def close_old(days=3) -> int:
    return _db.session_close_old(days=days)
