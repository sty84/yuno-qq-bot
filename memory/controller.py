"""Memory Controller：分析 → 分类路由 → 提取 → 存储（多库）→ 记忆更新 → 策略反馈 → AI 自身记忆。"""

import re
import os
from datetime import datetime

from plugins import _db, _shared
from memory import analysis, embedder, extract, graph, lexical, policy, topic, trace, world


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
    向量兜底仅限强纠错（轻纠错必须词元命中，防止“我其实是外星人”这类弱信号攻击无关稳定事实）。
    玩笑/反讽（joke_probability≥0.5）且非强纠错时不动任何记忆；稳定事实/偏好按阻力降权。
    返回受影响明细 [{fact, confidence, new_confidence, kind}]。"""
    strong = bool((an or {}).get("correction_strong"))
    if not strong and float((an or {}).get("joke_probability", 0.0)) >= 0.5:
        return []  # 玩笑语境里的“其实/改一下”不构成纠错
    specific = (
        set(extract.tokenize(text or "")) - _CORRECTION_STOP
    ) | (extract.fact_keywords(text or "") - _CORRECTION_STOP_BIGRAMS)
    if not specific:
        return []
    kind = "dispute" if strong else "conflict"
    candidates = set()
    for r in _db.memory_rows(scope, key):
        if (extract.fact_keywords(r["fact"]) & specific) or (
            set(extract.tokenize(r["fact"])) & specific
        ):
            candidates.add(r["fact"])
    if not candidates and strong and embedder.enabled():
        try:
            from memory import reasoning

            hits = reasoning.retrieve(text, [scope], top_k=1, min_score=0.5)
            if hits:
                candidates.add(hits[0][0])
        except Exception as e:
            _stats_err(e)
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
        cls = policy.fact_class(scope, key, fact)
        if decision["action"] == "update":
            # 调查确认旧记忆过时 → 直接废弃（保留历史，不删除）
            _db.memory_set_status(scope, key, fact, "superseded", valid_to=now_ts)
            # 时间纠错联动（v2.2）：update 时顺带改事件时间
            try:
                from memory import time_extract, graph as _graph
                _te = time_extract.extract(text or "", scope)
                _nts = _te.get("start") or datetime.now()
                _db.event_set_ts_by_title(
                    scope, key, _graph.title_of(fact),
                    _nts.isoformat(timespec="seconds"),
                    "explicit" if _te.get("explicit") else "approx",
                )
            except Exception as e:
                _stats_err(e)
            _db.history_add(
                scope, key, fact, "supersede",
                reason=f"用户纠正并经核查（update：{decision['reason']}）",
                old_confidence=cur, new_confidence=0.0,
            )
            if row.get("mclass") == "core" or scope == "ai" or scope.startswith("ai:"):
                _db.audit_add("review_required", fact[:100], "核心记忆被纠正并更新", operator="auto")
            try:
                _db.invalidation_add(scope, key, fact, "supersede")
            except Exception as e:
                _stats_err(e)
            details.append(
                {"fact": fact, "confidence": cur, "new_confidence": 0.0, "kind": "update", "decision": "update"}
            )
        elif decision["action"] == "uncertain":
            # 无法确认 → 冲突降权 + 标记 contested 待核查（按事实类型加阻力）
            new_conf = policy.update(cur, "conflict", resistance=policy.resistance_for(cls))
            _db.memory_set_confidence(scope, key, fact, new_conf)
            _db.memory_set_status(scope, key, fact, "contested")
            try:
                _db.invalidation_add(scope, key, fact, "conflict")
            except Exception as e:
                _stats_err(e)
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


_CJK_SURNAMES = set(
    "王李张刘陈杨黄赵吴周徐孙马朱胡郭何林罗高郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤"
)


def _has_proper_noun(text) -> bool:
    """专名识别（v31）：拉丁词 ≥2 字符 / 项目实体表 / 中文人名·地名·机构名·专名。
    修复：中文专名（小白/林晓）此前在信息增益里拿不到"含专名"加分。"""
    t = str(text or "")
    if re.search(r"[A-Za-z]{2,}", t):
        return True
    try:
        from memory.extract import extract_entities
        if extract_entities(t):
            return True
    except Exception as e:
        _stats_err(e)
        pass
    try:
        import jieba.posseg as pseg
        for w, flag in pseg.cut(t):
            if flag in ("nr", "ns", "nt", "nz") and len(w.strip()) >= 2:
                return True
    except Exception as e:
        _stats_err(e)
        pass
    # 兜底（jieba 缺失时）：常见姓氏 + 1~2 个汉字（“林晓”“李四”），
    # 或 小/阿/老 + 姓氏（“小白”“老王”），姓氏后跟数字/符号也能命中（“小白3岁了”）
    surname = "".join(_CJK_SURNAMES)
    return bool(
        re.search(r"[" + surname + r"][\u4e00-\u9fff]{1,2}", t)
        or re.search(r"[小阿老][" + surname + r"][\u4e00-\u9fff]{0,2}", t)
    )


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
    if _has_proper_noun(t):
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
        out["dominance"] = metrics["dominance"]
        return out
    return an


def ingest(scope, key, text, reply="", facts=None, confidence=None, source=None):
    """主入口：分析 → 提取 → 相似合并 → 存事实 + 结构化属性 + 事件图 + 词法索引 → 策略 → AI 经历。
    返回 {"facts": 新增数, "events": 事件数, "refreshed": 合并数, "analysis": {...}, "confidence": ...}。"""
    an = analysis.analyze(text or "", reply or "")
    if text or reply:
        an = analysis.enrich(an, text or "", reply or "")
    an = _fuse_emotion(an, scope, key)
    try:
        from memory import tz as tz_mod
        tz_mod.remember(scope, text)  # 用户所在地时区检测（“我现在在美国”）
    except Exception as e:
        _stats_err(e)
        pass
    try:
        from memory import appointment
        appointment.extract(scope, text)  # 约定识别（“明天下午3点见”）
    except Exception as e:
        _stats_err(e)
        pass
    try:
        from memory import mistake
        mistake.process(scope, text)  # 错误记录/道歉（“又放鸽子了”“对不起”）
    except Exception as e:
        _stats_err(e)
        pass
    conf = float(confidence if confidence is not None else an.get("confidence", 0.7))
    # 评分驱动行为（v11）：置信度维度低分 → 抑制过度自信
    adj = trace.adjustments()
    conf = min(0.95, conf * float(adj.get("confidence_factor", 1.0)))
    ts = datetime.now().isoformat(timespec="seconds")
    # 时间感知（v2.2）：口语里的过去时间 → 事件时间；解析失败用 now + approx
    ev_ts, ev_source = ts, "approx"
    try:
        from memory import time_extract
        _te = time_extract.extract(text or "", scope)
        if _te.get("explicit") and _te.get("start"):
            ev_ts = _te["start"].isoformat(timespec="seconds")
            ev_source = "explicit"
    except Exception as e:
        _stats_err(e)
    # 多主体记忆（v2.2）：识别在场主体（队友/NPC），后续逐条扇出
    _participants = []
    try:
        from memory import subjects
        _participants = subjects.detect((text or "") + " " + (reply or ""))
    except Exception as e:
        _stats_err(e)
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
        try:
            for dd in disputed:
                if dd.get("decision") in ("update", "uncertain"):
                    reconcile(scope, key, dd.get("fact", ""), reason=f"correction:{dd.get('decision')}")
        except Exception as e:
            _stats_err(e)
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
        # 提取污染防护（v2.3 修复 P1-2）：只喂用户的话，bot 的回复不进提取输入——
        # 否则 LLM 会把 bot 编的"阿拉蕾是雪貂"当成事实固化进用户 scope
        new_facts = (
            extract.extract_with_structure(f"用户：{(text or '')[:500]}")
            if text
            else []
        )
    new_facts = [f for f in new_facts if str(f).strip()]
    # 提取污染防护（v29）：用户 scope 里不存"机器人/…"开头的 AI 自述（如"机器人只会带半个坐垫"）
    if scope.startswith(("c2c:", "group:")):
        new_facts = [f for f in new_facts if not str(f).startswith(("机器人", "YUNO"))]
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

    # 证据门控（v2.3）：用户 scope 的提取事实统一标 source="user"（用户亲口说，高可信）
    src = source or "user"
    audience, speaker, mclass = _scene_meta(scope, key)

    # 记忆更新：近似重复合并（刷新旧记录，不堆叠）
    rows = _db.memory_rows(scope, key)
    existing_rows = {r["fact"]: r for r in rows}
    additions, refreshed = [], 0
    for f in new_facts:
        if f in existing_rows:
            continue
        dup = find_near_dup(scope, key, f, rows=rows, threshold=0.9)
        if dup:
            old_conf = float(existing_rows[dup].get("confidence", 0.7))
            new_conf = max(conf, old_conf)
            refresh(scope, key, dup, confidence=new_conf, source=src)
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
            privacy, _labels = detect(f)
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
                scope, key, encrypt_text(f), ts,
                confidence=conf, source=src, audience="private",
                speaker=speaker, mclass=mclass, privacy=privacies[f],
            )
            continue
        if analysis.attr_of(f, an):
            _db.attr_set(scope, key, analysis.attr_of(f, an), f, conf, ts)
        eid, _linked = graph.build_for_fact(
            scope, key, f, etype=an.get("event_type"), importance=importance,
            ts=ev_ts, ts_source=ev_source,
        )
        if eid:
            event_count += 1
        tid = topic.link_fact(scope, key, f, an.get("event_type") or "event", conf, an)
        if eid and tid:
            _db.event_set_topic(eid, tid)
        policy.touch(scope, key, f, importance=importance)
        if _participants:
            try:
                from memory import subjects, world as world_mod
                nscope_aud = (
                    f"group:{scope.split(':', 1)[1]}"
                    if scope.startswith("group:")
                    else "public"
                )
                for pname in _participants[: subjects.top_k()]:
                    if not world_mod.subject_gate(scope, key, f):
                        continue
                    nscope = subjects.scope_of(pname)
                    nconf = round(min(subjects.confidence_cap(), world_mod.subject_confidence("overheard")), 2)
                    _db.memory_add(
                        nscope, "", f, ts, None, confidence=nconf,
                        source="overheard", audience=nscope_aud, mclass="short",
                    )
                    _db.event_add(
                        nscope, "", an.get("event_type") or "event",
                        graph.title_of(f), content=f, importance=importance,
                        ts=ev_ts, ts_source=ev_source,
                        memory_scope=nscope, memory_key="", memory_fact=f,
                    )
                    try:
                        from memory import lexical as lexical_mod
                        lexical_mod.bm25_upsert(nscope, "", [f])
                        _db.lexicon_sync(nscope, "")
                    except Exception as e:
                        _stats_err(e)
                    policy.touch(nscope, "", f, importance=importance * 0.8)
            except Exception as e:
                _stats_err(e)
    _db.lexicon_sync(scope, key)
    lexical.bm25_upsert(scope, key, [f for f in additions if privacies.get(f, 0.0) < 0.8])
    _record_ai_experience(scope, key, text, ts, importance)
    if _participants:
        try:
            from memory import subjects, relationship as rel_mod
            for pname in set(_participants):
                rel_mod.update(subjects.scope_of(pname), subject=key or scope, event="chat", detail=(text or "")[:40])
        except Exception as e:
            _stats_err(e)
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
    except Exception as e:
        _stats_err(e)
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


# ===== 双轨制一致性（v2.2，原 memory/consistency.py 并入）=====
def reconcile(scope, key, fact, reason="") -> dict:
    """对一条失效事实做状态重算（纠错后调用）：关系降 trust + 议题降权。"""
    changed = {}
    try:
        from memory import relationship as rel_mod
        rel_mod.update(scope, subject=key or scope, event="dispute", detail=f"纠错:{str(fact)[:40]}")
        changed["relationship"] = 1
    except Exception as e:
        _stats_err(e)
    try:
        from memory import topic as topic_mod
        topic_mod.invalidate_for_fact(scope, key, fact)
        changed["topic"] = 1
    except Exception as e:
        _stats_err(e)
    return changed


def reconcile_pending(limit=100) -> dict:
    """惰性重算：消费失效队列（assemble_context 开头调用，量小不热）。"""
    rows = _db.invalidation_rows(limit)
    n = 0
    for r in rows:
        reconcile(r.get("scope", ""), r.get("key", ""), r.get("fact", ""), r.get("reason", ""))
        n += 1
    if rows:
        _db.invalidation_clear_all()
    return {"reconciled": n}


# ===== 记忆更新（v31.3 合并自 memory/update.py）=====
def _similarity(a: str, b: str, a_vec=None, b_vec=None) -> float:
    if a_vec and b_vec:
        return embedder.cosine(a_vec, b_vec)
    ta, tb = extract.fact_keywords(a), extract.fact_keywords(b)
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
        scope, key, fact,
        datetime.now().isoformat(timespec="seconds"),
        None, conf, source,
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
        scope, key, fact,
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


# ===== 隐私检测与可选加密（v31.3 合并自 memory/sensitive.py）=====
SENSITIVE_PATTERNS = [
    (r"\d{11}", "手机号"),
    (r"\d{15,18}[Xx]?", "身份证"),
    (r"\d{16,19}", "银行卡"),
    (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "邮箱"),
    (r"密码|口令|验证码|密钥|token|Token|PIN码", "凭据"),
    (r"住址|门牌|小区|栋|单元|楼层|几零几", "住址"),
    (r"工资|月薪|年薪|存款|欠款|借款|理财|股票|基金|资产|负债|流水|房产", "财务"),
    (r"微信号|QQ号|支付宝|银行账号|银行卡号|收款码", "账号"),
    (r"护照|驾照|社保|医保|社保卡|工号|工牌", "证件"),
    (r"病历|体检报告|过敏史|处方|住院|手术|诊断", "健康"),
]

SENSITIVE_WORDS = [
    "家庭住址", "身份证", "手机号", "银行卡", "密码", "验证码", "工资", "生病", "住院",
    "微信号", "支付宝", "银行账号", "社保", "医保", "病历", "体检", "过敏", "护照",
    "驾照", "房产", "股票", "理财", "基金", "资产", "邮箱",
]


def detect(text) -> tuple[float, list[str]]:
    """返回 (隐私分 0~1, 命中标签列表)。"""
    text = str(text or "")
    labels = []
    for pat, label in SENSITIVE_PATTERNS:
        if re.search(pat, text):
            labels.append(label)
    for w in SENSITIVE_WORDS:
        if w in text:
            labels.append(w)
    if not labels:
        return 0.0, []
    return min(1.0, 0.6 + 0.1 * len(labels)), list(dict.fromkeys(labels))[:4]


_crypto_key = None


def available() -> bool:
    global _crypto_key
    if _crypto_key is not None:
        return True
    secret = os.getenv("MEMORY_KEY", "")
    if not secret:
        return False
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        import hashlib
        _crypto_key = AESGCM(hashlib.sha256(secret.encode("utf-8")).digest())
        return True
    except Exception as e:
        _stats_err(e)
        return False


def encrypt_text(text) -> str:
    if not available():
        return str(text)
    try:
        nonce = os.urandom(12)
        ct = _crypto_key.encrypt(nonce, str(text).encode("utf-8"), None)
        return "enc:" + nonce.hex() + ":" + ct.hex()
    except Exception as e:
        _stats_err(e)
        return str(text)


def decrypt_text(text) -> str:
    if not isinstance(text, str) or not text.startswith("enc:"):
        return text
    if not available():
        return text
    try:
        _, nonce_hex, ct_hex = text.split(":", 2)
        return _crypto_key.decrypt(bytes.fromhex(nonce_hex), bytes.fromhex(ct_hex), None).decode("utf-8")
    except Exception as e:
        _stats_err(e)
        return text


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
        except Exception as e:
            _stats_err(e)
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



def _stats_err(e):
    """裸 except 审计（v2.2）：错误计数 + 日志，供消融/排查。"""
    try:
        import memory.stats as _st
        _st.bump_err("controller", e)
    except Exception:
        pass
