"""上下文组装：令牌预算分级注入（议题 → 记忆 → 事件脉络），超出预算截断。"""

from plugins import _db, _shared
from memory import extract, graph, reasoning, topic


def related_events(query, scopes, top_n=3) -> list[str]:
    """返回与查询相关的事件及其邻居（供 LLM 理解“为什么相关”）。"""
    qt = extract.fact_keywords(query)
    out = []
    for scope in scopes:
        evs = _db.event_rows(scope, limit=100)
        seed = [
            ev["id"]
            for ev in evs
            if qt and (extract.fact_keywords(ev.get("title") or "") & qt)
        ]
        if not seed:
            continue
        nids = graph.neighbors(seed, depth=1)
        for ev in evs:
            if ev["id"] in nids:
                out.append(f"{ev['etype']} · {ev['title']}")
    return out[:top_n]


def ai_memory_block(limit=4) -> str:
    """AI 自身记忆（统一格式）：experience / belief，标注可信度。"""
    rows = _db.memory_rows("ai")
    if not rows:
        return ""
    rows.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    lines = ["【我的经历与观点】"]
    for r in rows[:limit]:
        kind = r["key"] or "experience"
        conf = float(r.get("confidence", 0.7))
        if conf < 0.35:
            continue
        lines.append(f"- [{kind} · 可信度{conf:.0%}] {r['fact']}")
    return "\n".join(lines)


def _memory_block(query, scopes, top_k, min_score, extra_scopes, expand_query, recent) -> str:
    hits = reasoning.retrieve(
        query,
        scopes,
        top_k=top_k,
        min_score=min_score,
        extra_scopes=extra_scopes,
        expand_query=expand_query,
        recent=recent,
    )
    if hits:
        conf_map = {}
        src_map = {}
        status_map = {}
        time_map = {}
        for scope in list(scopes) + list(extra_scopes or []):
            for r in _db.memory_rows(scope):
                conf_map[r["fact"]] = float(r.get("confidence", 0.7))
                src_map.setdefault(r["fact"], r.get("source", ""))
                status_map.setdefault(r["fact"], r.get("status", "active"))
        try:
            # 复用 reasoning 的 60 秒缓存，避免每轮 O(3000×scopes) 扫描
            from memory import reasoning as reasoning_mod
            time_map = reasoning_mod._event_time_map(list(scopes) + list(extra_scopes or []))
        except Exception:
            time_map = {}
        lines = []
        shown = 0
        has_approx_time = False
        for f, _s, sc in hits:
            if shown >= 8:  # 记忆压缩（v5 §P1-4）：最多注入 8 条完整，其余合并
                break
            label = extract.nice_fact(f)
            tinfo = time_map.get(f)
            if tinfo and tinfo[0]:
                try:
                    from memory import time_extract
                    ttag = time_extract.label_for(tinfo[0], tinfo[1], scope=sc)
                    if ttag:
                        label = ttag + label
                        if str(tinfo[1]) != "explicit":
                            has_approx_time = True
                except Exception:
                    pass
            if status_map.get(f, "active") == "contested":  # v6 建议 §11：待核实标注
                label = f"（待核实）{label}"
            conf = conf_map.get(f, 0.7)
            # 软置信度（v3.1 §3）：0.8+ 直接使用；0.5-0.8 正常；0.3-0.5 模糊表达；<0.3 仅内部参考
            if conf < 0.3:
                label = f"（内部参考，不确定）{label}"
            elif conf < 0.5:
                label = f"我记得你好像提过……{label}？"
            tag = _scope_tag(sc)
            src_label = _source_label(src_map.get(f, ""))
            lines.append("- " + tag + label + src_label)
            shown += 1
        # 证据门控（v2.3）：按来源统计证据状态，注入分桶说明
        src_buckets = {}
        for f, _s, _sc in hits:
            s = src_map.get(f, "")
            if s in ("user", "pack", "ai_speculation", "news"):
                src_buckets[s] = src_buckets.get(s, 0) + 1
        if src_buckets:
            parts = []
            if src_buckets.get("user"):
                parts.append(f"用户亲口陈述 {src_buckets['user']} 条（高可信，可引用）")
            if src_buckets.get("pack"):
                parts.append(f"人设设定 {src_buckets['pack']} 条（可引用）")
            if src_buckets.get("ai_speculation"):
                parts.append(f"AI 推测 {src_buckets['ai_speculation']} 条（只能说'我好像记得'）")
            if src_buckets.get("news"):
                parts.append(f"资讯 {src_buckets['news']} 条（标注来源）")
            lines.append("（证据状态：" + "；".join(parts) + "——只把'可引用'的当事实说）")
        if has_approx_time:
            lines.append("（注：以上部分时间记得不确切——用户追问具体日期时如实说'大概'，别编造具体日子；"
                         "能查证就帮 TA 确认。）")
            try:
                # P2-1：追问"到底是哪天"时，沿 follows 链找邻近显式时间锚点，让 AI 可查证而非编造
                from memory import reasoning as reasoning_mod
                anchor = reasoning_mod.anchor_time(query, scopes)
                if anchor and anchor.get("anchored") and anchor.get("hint"):
                    lines.append(anchor["hint"])
            except Exception:
                pass
        if shown < len(hits):  # 记忆压缩（v5 §P1-4）：超出预算部分合并
            lines.append(f"- …等 {len(hits) - shown} 条相关记忆（已压缩）")
        return (
            "相关的历史记忆（只能引用这些内容回答用户个人问题，禁止编造记忆中没有的具体细节"
            "如地点/时间/他人特征/事件，查不到就明说）：\n" + "\n".join(lines)
        )
    return ""


def _source_label(src) -> str:
    """来源可信标注（v5 Provenance）。"""
    if not src:
        return ""
    if src == "user":
        return "·用户亲口说"
    if src == "pack":
        return "·人设设定"
    if src == "ai_speculation":
        return "·AI 推测"
    if src == "news":
        return "·资讯"
    if src.startswith("persona"):
        return "·人设"
    if src.startswith("character"):
        return "·档案"
    if src == "consolidation":
        return "·总结"
    if src == "belief":
        return "·观点"
    if src.startswith("ingest") or src.startswith("refresh"):
        return "·用户"
    return ""


def _scope_tag(scope) -> str:
    """记忆来源标注，避免 AI 把用户记忆当成自己的经历。"""
    if scope == "ai":
        return "（我自己的记忆）"
    if scope.startswith("c2c:"):
        return "（用户私聊告诉我的）"
    if scope.startswith("group:"):
        return "（群里提到的）"
    if scope.startswith("char:"):
        return "（角色档案）"
    return ""


def npc_memory_block(query, names, top_k=2) -> str:
    """多主体视角注入：独立的【队友视角·<名>】块（不混进主记忆块，避免身份混淆）。"""
    try:
        from memory import extract as extract_mod, reasoning, subjects, time_extract
        parts = []
        for name in names[: int(top_k)]:
            nscope = subjects.scope_of(name)
            hits = reasoning.retrieve(query, [nscope], top_k=int(top_k), min_score=0.25)
            if not hits:
                continue
            tmap = reasoning._event_time_map([nscope])
            lines = []
            for f, _s, _sc in hits:
                label = extract_mod.nice_fact(f)
                tinfo = tmap.get(f)
                if tinfo and tinfo[0]:
                    ttag = time_extract.label_for(tinfo[0], tinfo[1], scope=nscope)
                    if ttag:
                        label = ttag + label
                lines.append("- " + label)
            parts.append(f"【队友视角·{name}】她记得：\n" + "\n".join(lines))
        if not parts:
            return ""
        return "\n".join(parts) + "\n（以上是队友视角记忆，可能不准；引用时区分'我亲眼所见'和'她告诉我的'）"
    except Exception:
        return ""


def user_state_block(scope) -> str:
    """用户近期状态：从最近记忆的情绪维度判断，注入 prompt 供 AI 调整语气（v3 §14）。"""
    if not scope or not scope.startswith("c2c:"):
        return ""
    rows = [r for r in _db.memory_rows(scope) if abs(float(r.get("valence", 0.0))) > 0.01]
    rows.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    if not rows:
        return ""
    recent = rows[:3]
    neg = sum(1 for r in recent if float(r["valence"]) < 0)
    if neg >= 2:
        return (
            "【内部提示】基于历史记忆，用户之前提到过情绪低落的内容（这是记忆推断，不是实时状态）。"
            "回复时注意耐心、少开玩笑。（内部参考，不要向用户复述）"
        )
    if neg == 1:
        return (
            "【内部提示】用户之前提到过一次情绪低落的事（记忆推断，非实时状态）。"
            "适当关心但别过度。（内部参考，不要复述）"
        )
    return ""


def _event_block(query, scopes) -> str:
    evs = related_events(query, scopes)
    if evs:
        return "相关事件脉络：\n" + "\n".join("- " + e for e in evs)
    return ""


def _topic_block(query, scopes) -> str:
    ts = topic.search(query, scopes, limit=3)
    if ts:
        lines = []
        for t in ts:
            facts = [p["value"] for p in t.get("params", []) if p["param"] == "fact"]
            moods = sorted({p["value"] for p in t.get("params", []) if p["param"] == "mood"})
            playful = any(
                p["param"] == "playful" and p["value"] == "true" for p in t.get("params", [])
            )
            line = f"- [{t['category']}] {t['topic']}（{t.get('status', 'active')}）"
            if facts:
                line += "\n  · " + "\n  · ".join(facts[:3])
            try:
                from memory import topic as topic_mod
                mtext = topic_mod.mood_text(params=t.get("params"))
                if mtext:
                    line += f"\n  情绪底色：{mtext}"
                elif moods:
                    line += f"\n  情绪：{'/'.join(moods)}"
            except Exception:
                if moods:
                    line += f"\n  情绪：{'/'.join(moods)}"
            if playful:
                line += "\n  （玩笑语境为主）"
            lines.append(line)
        return "相关议题：\n" + "\n".join(lines)
    return ""


def _trim(text, limit) -> str:
    if len(text) <= limit:
        return text
    cut = text[: max(1, int(limit))]
    idx = cut.rfind("\n")
    if idx > int(limit) * 0.5:
        cut = cut[:idx]
    return cut.rstrip() + "\n…（其余略）"


def assemble_context(
    query,
    scopes,
    top_k=5,
    min_score=0.25,
    extra_scopes=None,
    budget=None,
    expand_query=False,
    recent=None,
) -> str:
    """令牌预算分级注入：议题（核心）→ 记忆 → 事件脉络，逐级填充到预算上限。
    budget 单位为字符（中文 1 字 ≈ 1 token 量级），默认 2000，可配
    config.json → memory.core.context_budget_chars。"""
    try:
        from memory import controller as controller_mod
        controller_mod.reconcile_pending()  # 双轨制一致性：纠错失效队列惰性重算
    except Exception:
        pass
    core = _shared.CONFIG.get("memory", {}).get("core", {}) or {}
    budget = int(budget or core.get("context_budget_chars", 2000))
    blocks = [
        _topic_block(query, scopes),
        _memory_block(query, scopes, top_k, min_score, extra_scopes, expand_query, recent),
        _event_block(query, scopes),
    ]
    parts, used = [], 0
    for block in blocks:
        if not block:
            continue
        remaining = budget - used
        if remaining < 60:
            break
        b = _trim(block, remaining)
        parts.append(b)
        used += len(b)
    return "\n\n".join(parts)
