"""Memory Controller：分析 → 分类路由 → 提取 → 存储（多库）→ 记忆更新 → 策略反馈 → AI 自身记忆。"""

import logging
import re
import os
from datetime import datetime

from memory._llmutil import parse_json_object
from plugins import _db, _shared
from memory import analysis, embedder, extract, graph, lexical, policy, topic, trace, world

_log = logging.getLogger(__name__)


_GROUND_NOISE = ("是", "的", "了", "我", "你", "他", "她", "它", "只", "有", "在", "很",
                "都", "也", "就", "和", "与", "及", "一个", "这个", "那个", "什么", "吗",
                "吧", "啊", "呀", "呢", "刚", "才", "过", "被", "把", "让", "给", "对", "去", "来",
                "买", "吃", "喝", "玩", "看", "听", "说", "想", "要", "做", "用", "拿",
                "找", "带", "写", "读", "打", "养", "开", "关", "换", "改", "问", "答")


def _fact_grounded(fact, user_text) -> bool:
    """提取事实的字面支撑校验（防 LLM 提取幻觉固化——"颜色是橘色"曾从
    "月底有场演出，是30号周日"的消息中提取，成为假来源的"证据"）。
    规则：fact 去除泛词后，至少一个 >=2 字的片段必须字面出现在用户消息里
    （"橘色"不是任何用户消息的子串 → 拒；"橘猫"是"我买了只橘猫"的子串 → 放行）。"""
    t = str(user_text or "")
    if not t:
        return True
    cleaned = str(fact or "")
    for w in _GROUND_NOISE:
        cleaned = cleaned.replace(w, " ")
    # jieba 切词后逐词校验（"喜欢打游戏"→"打游戏"是消息子串 → 放行；
    # "颜色是橘色"→"橘色"不是任何用户消息子串 → 拒）
    try:
        from memory.extract import tokenize
        words = tokenize(cleaned)
    except Exception:
        words = cleaned.split()
    multi, single = [], []  # type: ignore[var-annotated]
    for seg in words:
        (multi if len(seg) >= 2 else single).append(seg)
    # 任一 >=2 字词字面在消息中 → 放行（"橘猫"⊂"我今天买了只橘猫"）
    if any(seg in t for seg in multi):
        return True
    # 只有单字词时须全部在消息中（"猫粮"→['猫','粮']，"粮"不在 → 拒；
    # "我养了只猫"→['猫']，"猫"⊂"橘猫" → 放行）
    if single and all(seg in t for seg in single):
        return True
    return False


def fact_keywords(fact) -> list:
    """提取事实的内容词（>=2 字，去噪音词），供污染扫描做"字面出处"反向校验。
    与 _fact_grounded 共用噪音表；分词噪声词（如 jieba 的"万买""室见"）无法
    命中任何历史消息，自然不参与支撑，无需额外清洗。"""
    cleaned = str(fact or "")
    for w in _GROUND_NOISE:
        cleaned = cleaned.replace(w, " ")
    try:
        from memory.extract import tokenize
        words = tokenize(cleaned)
    except Exception:
        words = cleaned.split()
    out = []
    for seg in words:
        if len(seg) >= 2:
            out.append(seg)
    return out


def pollution_level(fact, stmt_msgs, quest_msgs) -> str:
    """污染扫描分级（2026-08-16）：事实的内容词在用户历史消息中的字面出处覆盖率。
    - strong  陈述句出处 >=2/3 → 用户亲口说过，保留
    - partial 陈述出处 <2/3（>0）→ 部分是推断/概括 → 降级为 ai_edit
    - weak    仅问句命中 → 语义反转（用户问"你玩过吗"被提取成"用户玩过"）→ 降级
    - none    无任何出处 → 提取幻觉固化的污染 → 删除候选
    - empty   无内容词（全是噪音）→ 无法判定，跳过
    """
    kws = fact_keywords(fact)
    if not kws:
        return "empty"
    hit_stmt = sum(1 for w in kws if any(w in m for m in stmt_msgs))
    if hit_stmt * 3 >= len(kws) * 2:
        return "strong"
    if hit_stmt > 0:
        return "partial"
    if any(w in m for w in kws for m in quest_msgs):
        return "weak"
    return "none"


def merge_facts(existing, new, cap=30, protect=None):
    """合并事实（去重、截断到 cap 条；protect 内的身份锚点不参与截断）。
    修复（对话暴露的 bug）：用户身份事实（"我是你们乐队新来的经纪人助手"）曾被 cap=30
    无条件截掉——生成层从此不知道用户是谁，把经纪人助理当成上台表演的乐队成员。"""
    seen, out = set(existing), list(existing)
    for fact in new:
        fact = extract.nice_fact(fact).strip()
        if fact and fact not in seen:
            seen.add(fact)
            out.append(fact)
    protect = protect or set()
    kept = [f for f in out if f in protect]
    rest = [f for f in out if f not in protect]
    return kept + rest[-max(1, cap - len(kept)):]


def _maybe_embed(facts):
    if not facts or not embedder.enabled():
        return None
    vecs = embedder.embed(facts)
    return dict(zip(facts, vecs)) if vecs else None


# ===== 存量矛盾扫描（v2.3 P0-2）=====
# 主语字符类排除"不"（防贪婪把"煤球不"吃进主语）；宾语允许含"不"
_ATTR_RE = re.compile(r"([\u4e00-\u9fffA-Za-z0-9]{1,8}?)(不是|是)([\u4e00-\u9fffA-Za-z0-9]{1,12})")
_LIKE_RE = re.compile(r"([\u4e00-\u9fffA-Za-z0-9]{1,8}?)(不喜欢|讨厌|喜欢|爱吃|爱喝)([\u4e00-\u9fffA-Za-z0-9]{1,12})")
_GO_RE = re.compile(r"([\u4e00-\u9fffA-Za-z0-9]{1,8}?)(没去过|去过)([\u4e00-\u9fffA-Za-z0-9]{1,12})")
_PET_RE = re.compile(r"([\u4e00-\u9fffA-Za-z0-9]{1,8}?)(没养|养了)([\u4e00-\u9fffA-Za-z0-9]{1,12})")
# 无语义量宾语才跳过（"是人/是东西"）；"猫/狗/队友"是具体类别，必须参与冲突判定——
# "煤球是猫 vs 煤球是狗"正是最典型的矛盾形态
_ATTR_TRIVIAL = ("人", "东西", "事物", "机器人", "AI", "软件")
# 语气词/虚词不是实体：对话里"反正不是什么大事"的"反正"会被正则误当主语
_ATTR_FAKE_SUBJ = ("反正", "其实", "真的", "确实", "原来", "大概", "可能", "应该", "总之",
                   "不过", "但是", "然而", "反正就", "基本上", "说白了", "老实说")


def _attr_pairs(fact) -> list:
    """提取事实的 (主语, 谓词组, 宾语, 是否否定) 属性对。
    支持：X是Y / X不是Y；X喜欢/讨厌Y；X去过/没去过Y；X养了/没养Y。
    主语≤8 字宾语≤12 字；琐碎宾语（是人/是东西）跳过。"""
    out = []
    for m in _ATTR_RE.finditer(str(fact or "")):
        subj, neg, obj = m.group(1), m.group(2) == "不是", m.group(3)
        if subj in _GROUND_NOISE or obj in _ATTR_TRIVIAL or subj in _ATTR_FAKE_SUBJ:
            continue
        if len(subj) >= 2 or subj in ("我", "你", "它", "她", "他"):
            out.append((subj, "is", obj, neg))
    for m in _LIKE_RE.finditer(str(fact or "")):
        subj, verb, obj = m.group(1), m.group(2), m.group(3)
        if subj in _GROUND_NOISE or obj in _ATTR_TRIVIAL or subj in _ATTR_FAKE_SUBJ:
            continue
        if len(subj) >= 2 or subj in ("我", "你", "它", "她", "他"):
            out.append((subj, "like", obj, verb in ("不喜欢", "讨厌")))
    for m in _GO_RE.finditer(str(fact or "")):
        subj, verb, obj = m.group(1), m.group(2), m.group(3)
        if subj in _GROUND_NOISE or obj in _ATTR_TRIVIAL or subj in _ATTR_FAKE_SUBJ:
            continue
        if len(subj) >= 2 or subj in ("我", "你", "它", "她", "他"):
            out.append((subj, "go", obj, verb == "没去过"))
    for m in _PET_RE.finditer(str(fact or "")):
        subj, verb, obj = m.group(1), m.group(2), m.group(3)
        if subj in _GROUND_NOISE or obj in _ATTR_TRIVIAL or subj in _ATTR_FAKE_SUBJ:
            continue
        if len(subj) >= 2 or subj in ("我", "你", "它", "她", "他"):
            out.append((subj, "pet", obj, verb == "没养"))
    return out


def _attrs_conflict(a, b) -> bool:
    """两个属性对是否矛盾：同主语、同谓词组，宾语不同或无包含关系、否定性不一致即冲突。
    '阿拉蕾是雪貂' vs '阿拉蕾是队友' → 冲突；'煤球是猫' vs '煤球是橘猫' → 包含，不冲突；
    '用户喜欢猫' vs '用户讨厌猫' → 冲突。"""
    if a[0] != b[0] or a[1] != b[1]:
        return False
    oa, ob, na, nb = a[2], b[2], a[3], b[3]
    if oa == ob:
        return na != nb  # 同对象但肯定/否定不一致 → 冲突
    if oa in ob or ob in oa:
        return False  # 上下位（猫⊂橘猫）是细化不是矛盾
    return True


def conflict_scan(scope="", apply=False):
    """存量矛盾扫描（v2.3 P0-2）：同 scope 内 active 记忆中，同实体（主语）不同属性
    值且无上下位包含 → 矛盾候选（'阿拉蕾是雪貂' vs '阿拉蕾是队友'）。
    返回 (报告文本, 冲突列表)；apply=True 时把置信度低的一方降权并标 contested
    （core 记忆只降权不标 contested），audit 留痕 conflict_scan。"""
    rows = _db.memory_rows(scope=scope or None)
    rows = [r for r in rows if (r.get("status") or "") in ("", "active")]
    pairs = []
    for r in rows:
        for subj, group, obj, neg in _attr_pairs(r["fact"]):
            pairs.append((r, subj, group, obj, neg))
    conflicts = []
    for i in range(len(pairs)):
        for j in range(i + 1, len(pairs)):
            r1, s1, g1, o1, n1 = pairs[i]
            r2, s2, g2, o2, n2 = pairs[j]
            if r1["fact"] == r2["fact"] or r1["scope"] != r2["scope"]:
                continue
            if _attrs_conflict((s1, g1, o1, n1), (s2, g2, o2, n2)):
                conflicts.append({
                    "subject": s1, "a": o1, "b": o2,
                    "fact1": r1["fact"], "fact2": r2["fact"],
                    "conf1": r1.get("confidence"), "conf2": r2.get("confidence"),
                    "scope": r1["scope"],
                })
    # 去重（同对只报一次）
    seen, uniq = set(), []
    for c in conflicts:
        k = frozenset((c["fact1"], c["fact2"]))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(c)
    if apply:
        n_dem, n_contested = 0, 0
        for c in uniq:
            # 只处理置信度低的一方（高置信方保留——更可能是用户后来确认的正确记忆）
            r1 = next((r for r in rows if r["fact"] == c["fact1"] and r["scope"] == c["scope"]), None)
            r2 = next((r for r in rows if r["fact"] == c["fact2"] and r["scope"] == c["scope"]), None)
            low = None
            if r1 and r2:
                low = r1 if float(c.get("conf1", 0)) <= float(c.get("conf2", 0)) else r2
            elif r1:
                low = r1
            elif r2:
                low = r2
            if not low:
                continue
            is_core = (low.get("mclass") or "") == "core"
            new_conf = float(low.get("confidence", 0.7)) * 0.5
            _db.memory_set_confidence(low["scope"], low["key"], low["fact"], new_conf)
            if not is_core:
                _db.memory_set_status(low["scope"], low["key"], low["fact"], "contested")
                n_contested += 1
            _db.audit_add("conflict_scan", f"矛盾降权 {'core保护' if is_core else 'contested'} {low['fact'][:40]}", "auto")
            n_dem += 1
    lines = [f"矛盾扫描：{scope or '全部'} · 属性对 {len(pairs)} 个 · 冲突 {len(uniq)} 对"]
    if apply:
        lines.append(f"已执行：降权 {n_dem} 条（contested {n_contested}）")
    for c in uniq[:15]:
        lines.append(f"  [{c['subject']}] '{c['a']}' vs '{c['b']}'\n"
                     f"     · {c['fact1'][:50]} (conf {c['conf1']})\n"
                     f"     · {c['fact2'][:50]} (conf {c['conf2']})")
    if len(uniq) > 15:
        lines.append(f"  … 其余 {len(uniq) - 15} 对")
    if not uniq:
        lines.append("  无矛盾候选（或事实不含'X是Y'结构）")
    return "\n".join(lines), uniq


# ===== 存量日历校验（v2.3 P1-2）=====
_CAL_FACT_RE = re.compile(r"(\d{1,2})号[^，。！？!?]{0,8}?(?:是)?(?:周|星期)([日一二三四五六天])")


def calendar_check(scope="", apply=False, now=None):
    """存量日历校验（v2.3 P1-2）：库内"X号是周Y"类事实用真实日历验证——
    与当月日历不符（"31号是周日"在 2026-08 实际是周一）→ 错误事实候选，
    --apply 时降权 + 标 contested（core 只降权）。生成侧由 verify_reply_calendar
    拦截新回复，本工具清存量。"""
    from datetime import datetime, date
    import calendar as _cal
    now = now or datetime.now()
    rows = _db.memory_rows(scope=scope or None)
    rows = [r for r in rows if (r.get("status") or "") in ("", "active")]
    bad = []
    for r in rows:
        for m in _CAL_FACT_RE.finditer(r["fact"]):
            day, wdch = int(m.group(1)), m.group(2)
            wd = {"日": 6, "天": 6, "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5}.get(wdch)
            if wd is None:
                continue
            last_day = _cal.monthrange(now.year, now.month)[1]
            if not (1 <= day <= last_day):
                continue  # 超当月天数：可能是历史月份，跳过不误伤
            actual = date(now.year, now.month, day).weekday()
            if actual != wd:
                bad.append({"fact": r["fact"], "day": day, "weekday": wdch,
                            "actual": "日一二三四五六"[actual], "confidence": r.get("confidence"),
                            "scope": r["scope"], "key": r["key"]})
    if apply:
        n = 0
        for b in bad:
            _db.memory_set_confidence(b["scope"], b["key"], b["fact"],
                                      float(b.get("confidence") or 0.7) * 0.4)
            _db.memory_set_status(b["scope"], b["key"], b["fact"], "contested")
            _db.audit_add("calendar_check", f"日历不符降权 {b['fact'][:40]}（{b['day']}号实际是{b['actual']}）", "auto")
            n += 1
    lines = [f"日历校验：{scope or '全部'} · 检查 {len(rows)} 条 · 日历不符 {len(bad)} 条（当月）"]
    if apply:
        lines.append(f"已执行：降权 {len(bad)} 条")
    for b in bad[:15]:
        lines.append(f"  · {b['fact'][:44]}（{b['day']}号实际是{b['actual']}，非{b['weekday']}）conf {b['confidence']}")
    if not bad:
        lines.append("  无日历不符事实")
    return "\n".join(lines), bad


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
    candidates = set()
    for r in _db.memory_rows(scope, key):  # type: ignore[attr-defined]
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
        row = next((r for r in _db.memory_rows(scope, key) if r["fact"] == fact), None)  # type: ignore[attr-defined]
        if not row:
            continue
        cur = float(row.get("confidence", 0.7))
        cls = policy.fact_class(scope, key, fact)
        if decision["action"] == "update":
            # 调查确认旧记忆过时 → 直接废弃（保留历史，不删除）
            _db.memory_set_status(scope, key, fact, "superseded", valid_to=now_ts)  # type: ignore[attr-defined]
            # 时间纠错联动（v2.2）：update 时顺带改事件时间
            try:
                from memory import time_extract, graph as _graph
                _te = time_extract.extract(text or "", scope)
                _nts = _te.get("start") or datetime.now()
                _db.event_set_ts_by_title(  # type: ignore[attr-defined]
                    scope, key, _graph.title_of(fact),
                    _nts.isoformat(timespec="seconds"),
                    "explicit" if _te.get("explicit") else "approx",
                )
            except Exception as e:
                _stats_err(e)
            _db.history_add(  # type: ignore[attr-defined]
                scope, key, fact, "supersede",
                reason=f"用户纠正并经核查（update：{decision['reason']}）",
                old_confidence=cur, new_confidence=0.0,
            )
            if row.get("mclass") == "core" or scope == "ai" or scope.startswith("ai:"):
                _db.audit_add("review_required", fact[:100], "核心记忆被纠正并更新", operator="auto")  # type: ignore[attr-defined]
            try:
                _db.invalidation_add(scope, key, fact, "supersede")  # type: ignore[attr-defined]
            except Exception as e:
                _stats_err(e)
            details.append(
                {"fact": fact, "confidence": cur, "new_confidence": 0.0, "kind": "update", "decision": "update"}
            )
            reasoning.record_negative_feedback(fact, scope=scope)
        elif decision["action"] == "uncertain":
            # 无法确认 → 冲突降权 + 标记 contested 待核查（按事实类型加阻力）
            new_conf = policy.update(cur, "conflict", resistance=policy.resistance_for(cls))
            _db.memory_set_confidence(scope, key, fact, new_conf)  # type: ignore[attr-defined]
            _db.memory_set_status(scope, key, fact, "contested")  # type: ignore[attr-defined]
            try:
                _db.invalidation_add(scope, key, fact, "conflict")  # type: ignore[attr-defined]
            except Exception as e:
                _stats_err(e)
            _db.history_add(  # type: ignore[attr-defined]
                scope, key, fact, "conflict",
                reason=f"纠正待核查（uncertain：{decision['reason']}）",
                old_confidence=cur, new_confidence=new_conf,
            )
            details.append(
                {"fact": fact, "confidence": cur, "new_confidence": new_conf, "kind": "conflict", "decision": "uncertain"}
            )
            reasoning.record_negative_feedback(fact, scope=scope)
        else:
            # 调查后纠正不成立 → 保留旧记忆，只记审计
            _db.history_add(  # type: ignore[attr-defined]
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
    for r in _db.memory_rows(scope, key):  # type: ignore[attr-defined]
        if r.get("status") == "superseded":
            continue
        old_ents = [e.lower() for e in extract_entities(r["fact"])]
        if old_ents and new_ents:  # 双方都有专名 → 同一类话题的状态更新
            _db.memory_set_status(scope, key, r["fact"], "superseded", valid_to=ts)  # type: ignore[attr-defined]
            _db.history_add(  # type: ignore[attr-defined]
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
    for r in _db.memory_rows(scope, key):  # type: ignore[attr-defined]
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
    # 提取幻觉防护（对话暴露的 bug）：提取的事实必须在用户消息里有字面支撑——
    # "颜色是橘色"曾从"月底有场演出"的消息中提取固化，成为假来源的"证据"，
    # 让"橘色假来源"检测被合法化（自我强化污染循环的入口）
    if text:
        _before_nf = len(new_facts)
        new_facts = [f for f in new_facts if _fact_grounded(f, text)]
        if len(new_facts) < _before_nf:
            _db.audit_add("extract_grounded", f"提取幻觉拦截 {_before_nf - len(new_facts)} 条", "auto")
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
    # 语义反转防护（v2.3 P0-1）：用户消息是疑问句时（"你有玩过怪物猎人吗"），
    # 提取结果不能标"用户亲口说"——否则问句里的实体被固化成语义反转的假事实
    # （用户问 AI 玩过没 → 库记"用户玩过"），且字面支撑存在，_fact_grounded 拦不住。
    # 问句提取统一降级为 ai_edit + 置信度打折：记忆仍在、可检索，但 prompt 里
    # 不标"用户亲口说"，证据状态不进"高可信可引用"桶，来源声明也无据可引。
    if text and analysis.is_question(text):
        src = "ai_edit"
        conf = min(conf, 0.6) * 0.8
        _db.audit_add("extract_question_demote", f"问句提取降级 {len(new_facts)} 条: {(text or '')[:40]}", "auto")
    audience, speaker, mclass = _scene_meta(scope, key)

    # 记忆更新：近似重复合并（刷新旧记录，不堆叠）
    # ① 事务化（P1-5）：主写段（合并/替换/事件图/议题/词法/多主体/巩固/关系）单事务，
    # 中途失败整体回滚——不再留"事实已存但图/索引/议题缺失"的半成品状态
    with _db.transaction():
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

            # 身份锚点保护：stable 类事实（身份/职业/生日…）不参与 cap 截断，
        # 否则用户身份记忆会被截掉（"经纪人助理"被当成"乐队成员"的对话 bug）
        _protect = {
            r["fact"] for r in rows
            if policy.fact_class(scope, key, r["fact"]) == "stable"
        }
        merged = merge_facts(list(existing_rows), additions, protect=_protect)
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
        # 核心层巩固（MEMGPT 热层）：稳定事实升 mclass=core，供 context.core_memory_block 常驻注入
        try:
            policy.promote_core(scope)
        except Exception as e:
            _stats_err(e)
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


# ===== 主动自我编辑（MEMGPT 主动记忆，方案 B：后置小调用）=====
def _active_edit_cfg(key, default):
    core = (_shared.CONFIG.get("memory", {}).get("core", {}) or {}).get("active_edit", {}) or {}
    return core.get(key, default)


def active_edit(scope, key, text, reply) -> dict:
    """主动自我编辑入口：回复生成后，用一次轻量 LLM 判断这轮有哪些值得记/该删的用户记忆并应用。
    与 ingest 的规则被动提取平行——ingest 管「阈值/规则」漏掉的信息由这里兜住，反之亦然。
    触发门槛（min_gain）+ 失败不连坐（异常只审计，不影响已落地的回复）。
    config → memory.core.active_edit.enabled（默认 false，验证后再开）。"""
    if not _active_edit_cfg("enabled", False):
        return {"enabled": False}
    if not scope or not str(scope).startswith("c2c:"):
        return {"enabled": True, "skipped": "not_c2c"}
    if message_gain(text, scope, key)["score"] < float(_active_edit_cfg("min_gain", 0.3)):
        return {"enabled": True, "skipped": "low_gain"}
    core = ""
    try:
        from memory import context as context_mod
        core = context_mod.core_memory_block(scope)
    except Exception as e:
        _stats_err(e)
    ops = _decide_edits(text, reply, core)
    applied = _apply_ops(scope, key, ops)
    return {"enabled": True, "applied": applied, "ops": ops}


def _decide_edits(text, reply, core) -> list:
    """轻量 LLM 判断：输入 (用户消息, 回复, 已有核心记忆)，输出编辑操作列表。
    只输出 JSON：{"remember": [...], "forget": [...]}。解析失败/格式不符返回 []。"""
    prompt = (
        "你是记忆管理员。判断这轮对话里有哪些关于用户的信息值得长期记住，"
        "尤其关注规则可能漏掉、但重要且稳定的事实。\n"
        "重点记这几类（都是用户本人的稳定信息）：\n"
        "1) 身份/长期角色：用户是做什么的、和你是什么关系定位；\n"
        "2) 能力/擅长：用户对自己能力的表述——例如用户说「我还挺适合干这行」，应记为「用户擅长<某领域>」；\n"
        "3) 长期偏好/价值观：稳定的喜好、习惯、处事原则；\n"
        "4) 长期计划/目标：跨多天的重要安排。\n"
        "不要记：一次性琐事（吃了什么、闲聊寒暄）、转瞬即逝的当下状态，以及你（AI）自己说的话。\n"
        '只输出 JSON，格式：{"remember": ["值得记的事实（一句话，含具体名字/数字/时间）"], '
        '"forget": ["已有核心记忆里确实已过时的条目原文"]}。\n'
        "规则：remember 只记用户本人稳定且重要的事实，有把握才记；"
        "forget 只能从下面「已有核心记忆」里选已过时的，没有就填 []。\n"
        f"已有核心记忆：\n{core or '（无）'}\n"
        f"用户消息：{(text or '')[:400]}\n"
        f"你的回复：{(reply or '')[:200]}"
    )
    try:
        resp = _shared.deepseek_chat(
            messages=[
                {"role": "system", "content": "你是记忆管理员。只输出 JSON，不要解释。"},
                {"role": "user", "content": prompt},
            ],
            max_tokens=200,
            temperature=0.0,
            module="active_edit",
            detail="decide",
        )
        raw = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        _stats_err(e)
        return []
    data = parse_json_object(raw)
    if data is None:
        return []
    if not isinstance(data, dict):
        return []
    ops = []
    for f in data.get("remember") or []:
        f = str(f).strip()
        if f:
            # 不直接写入 core：先落 long，后续由 promote_core 的安全校验决定是否升级
            ops.append({"op": "remember", "fact": f, "mclass": "long"})
    for f in data.get("forget") or []:
        f = str(f).strip()
        if f:
            ops.append({"op": "forget", "fact": f})
    return ops


def _apply_ops(scope, key, ops) -> dict:
    """应用编辑操作：remember（事实存在则升核心/长期，否则新建）；forget（supersede 保留历史）。"""
    applied = {"remember": 0, "forget": 0}
    if not ops:
        return applied
    by_fact = {r["fact"]: r for r in _db.memory_rows(scope, key)}  # type: ignore[attr-defined]
    for op in ops:
        fact = extract.nice_fact(str(op.get("fact", ""))).strip()
        if not fact:
            continue
        kind = op.get("op")
        if kind == "remember":
            target = op.get("mclass", "long")
            if target == "core" and policy.fact_class(scope, key, fact) != "stable":
                target = "long"  # 防止绕过 promote_core 直接把非稳定事实塞进核心层
            row = by_fact.get(fact)
            if row:
                _db.memory_add(  # type: ignore[attr-defined]
                    scope, key, fact,
                    updated_at=row.get("updated_at") or "",
                    confidence=max(float(row.get("confidence", 0.7)), 0.6),
                    source=row.get("source", "") or "ai_edit",
                    audience=row.get("audience", ""),
                    speaker=row.get("speaker", ""),
                    mclass=target,
                    arousal=float(row.get("arousal", 0.0)),
                    valence=float(row.get("valence", 0.0)),
                    privacy=float(row.get("privacy", 0.0)),
                )
            else:
                _db.memory_add(scope, key, fact, confidence=0.6, source="ai_edit", mclass=target)  # type: ignore[attr-defined]
            applied["remember"] += 1
        elif kind == "forget":
            if fact in by_fact:
                _db.memory_set_status(scope, key, fact, "superseded")  # type: ignore[attr-defined]
                _db.history_add(scope, key, fact, "forget", reason="AI 主动编辑：判定已过时")  # type: ignore[attr-defined]
                applied["forget"] += 1
    if applied["remember"] or applied["forget"]:
        _db.lexicon_sync(scope, key)  # type: ignore[attr-defined]
    return applied


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
    rows = _db.invalidation_rows(limit)  # type: ignore[attr-defined]
    n = 0
    for r in rows:
        reconcile(r.get("scope", ""), r.get("key", ""), r.get("fact", ""), r.get("reason", ""))
        n += 1
    if rows:
        _db.invalidation_clear_all()  # type: ignore[attr-defined]
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
    """废弃替换：把旧事实标记 superseded（不再召回，保留历史），由新事实替代。"""
    _db.memory_set_status(scope, key, fact, "superseded")


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


_no_key_warned = [False]


def _warn_no_key(reason=""):
    """MEMORY_KEY 未配置时一次性告警（避免每条敏感数据刷屏）。"""
    if not _no_key_warned[0]:
        _no_key_warned[0] = True
        _log.warning("MEMORY_KEY 未配置，加密退化为明文%s", f"：{reason}" if reason else "")


def encrypt_text(text) -> str:
    if not available():
        _warn_no_key("写入明文")
        return str(text)
    try:
        nonce = os.urandom(12)
        ct = _crypto_key.encrypt(nonce, str(text).encode("utf-8"), None)  # type: ignore[union-attr]
        return "enc:" + nonce.hex() + ":" + ct.hex()
    except Exception as e:
        _stats_err(e)
        _log.warning("AES-GCM 加密失败，降级为明文存储：%s", e)
        return str(text)


def decrypt_text(text) -> str:
    if not isinstance(text, str) or not text.startswith("enc:"):
        return text
    if not available():
        _warn_no_key("存在 enc: 密文但无法解密")
        return text
    try:
        _, nonce_hex, ct_hex = text.split(":", 2)
        return _crypto_key.decrypt(bytes.fromhex(nonce_hex), bytes.fromhex(ct_hex), None).decode("utf-8")  # type: ignore[union-attr]
    except Exception as e:
        _stats_err(e)
        _log.warning("AES-GCM 解密失败（密钥变更或数据损坏），返回原文：%s", e)
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
    recent = _db.session_find_recent(scope, key, within_min=int(_session_cfg("window_min", 1440)))  # type: ignore[attr-defined]
    topic_name = _session_topic_of(text)
    if recent and _same_session_topic(text, recent):
        _db.session_bump(recent["id"], topic=topic_name, summary=summary)  # type: ignore[attr-defined]
        return recent["id"]
    fallback_topic = recent.get("topic", "") if recent else ""
    return _db.session_create(  # type: ignore[attr-defined]
        scope, key, topic=topic_name or fallback_topic, summary=summary or text[:100]
    )


def current(scope, key):
    """当前活跃会话（用于指代消解/上下文）。"""
    return _db.session_find_recent(scope, key, within_min=int(_session_cfg("window_min", 1440)))


def close_old(days=3) -> int:
    return _db.session_close_old(days=days)  # type: ignore[attr-defined]



def _stats_err(e):
    """裸 except 审计（v2.2）：错误计数 + 日志，供消融/排查。"""
    try:
        import memory.stats as _st
        _st.bump_err("controller", e)
    except Exception:
        pass
