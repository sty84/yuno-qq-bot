"""Agent 核心：分析 → 人格 → 记忆 → 云端 LLM → 反馈学习（成长）。"""

import re

from plugins import _db, _shared
from memory import analyze, assemble_context, ingest, session
from agent import persona

# 纯时间/日期提问（整句匹配，避免"你们几点开门"这类误触发）
_TIME_Q_RE = re.compile(
    r"^\s*(?:现在|目前|当前|今天)?\s*(?:是)?\s*(?:几点了?|几点钟|什么时间|几号|星期几|周几|日期|几点)\s*[？?]?\s*$"
)
_MEMORY_GAP_RE = re.compile(r"不记得|记不清|想不起来|没印象|忘记了|不太记得|什么事|怎么了|咋了|啥|嗯？|嗯\?|啊？")
_LAST_BOT_CLAIM_RE = re.compile(r"约好|约定|答应|说好|约了")
_APPT_TOPIC_RE = re.compile(r"约定|答应|约好|说好|约了|约的|约过|约什么|见面|放鸽子")
_STRUCTURED_DOMAIN_RE = re.compile(r"约定|约好|几点|什么时候|几号|演出|排练|日程|安排|设备|预算|表格|在哪|位置|哪里")

# 历史里任何具体钟点（"凌晨两点十四""快两点半""在想两点十四分的事"）
# 都不能当现行时间用——但"说好晚上8点见"这类约定陈述要保留
_TIME_REF_RE = re.compile(
    r"(?:凌晨|早上|上午|中午|下午|傍晚|晚上|半夜)?\s*[0-9一二两三四五六七八九十]{1,3}"
    r"\s*(?:点半?|点(?:\s*[0-9一二两三四五六七八九十]{1,2}\s*分?)?|:\d{1,2})"
)
_APPOINT_MARKERS = ("说好", "约", "明天", "后天", "周末", "下周", "到时候", "见面", "见", "集合", "碰头")

_COGNITIVE_INSTRUCTION = (
    "\n\n【认知输出要求（本条消息）】请只输出一个 JSON 对象（不要 Markdown 代码块、不要解释），字段："
    '{"appraisal": "你对当前情境的一句话解读（威胁/机会/无关）", '
    '"activated_goals": ["命中的目标列表（没有就空数组）"], '
    '"intention": "你当前承诺要做的事（没有就空字符串）", '
    '"chosen_action": "你选择的动作", '
    '"reply": "对用户的完整回复（保持人设与语气）"}'
)


def _mind_cfg(key, default):
    try:
        m = (_shared.CONFIG.get("memory", {}).get("core", {}) or {}).get("mind", {}) or {}
        return m.get(key, default)
    except Exception as e:
        _stats_err(e)
        return default


def _parse_cognitive(raw) -> dict | None:
    try:
        import json
        m = re.search(r"\{.*\}", raw or "", re.S)
        if not m:
            return None
        data = json.loads(m.group(0))
        if not str(data.get("reply") or "").strip():
            return None
        return data
    except Exception as e:
        _stats_err(e)
        return None


def _is_stale_time_ref(content) -> bool:
    """历史 AI 回复里的过时时间引用：含具体钟点且不是约定陈述 → 需要清洗。"""
    if not _TIME_REF_RE.search(str(content or "")):
        return False
    return not any(mk in str(content or "") for mk in _APPOINT_MARKERS)


def _clean_history(history):
    """清洗历史里的过时时间引用，防模型复读旧时间，同时保留对话语境。
    含具体钟点且非约定陈述的 AI 回复 → 替换为占位；
    "说好晚上8点见"这类约定陈述保留；用户说的话永远保留。"""
    out = []
    for m in history or []:
        content = str(m.get("content", ""))
        role = m.get("role", "")
        if role == "assistant" and _is_stale_time_ref(content):
            content = "[此前聊过时间话题]"
        out.append({**m, "content": content})
    return out


def _core_enabled() -> bool:
    core = _shared.CONFIG.get("memory", {}).get("core", {}) or {}
    rag_cfg = _shared.CONFIG.get("memory", {}).get("rag", {}) or {}
    return bool(core.get("enabled", rag_cfg.get("enabled", False)))


def _default_llm(text, extra_context, history, system) -> str:
    """默认云端 LLM：DeepSeek（OpenAI 兼容，可换）。"""
    return _shared.ask_deepseek(
        text, extra_context=extra_context, history=history, system=system
    )


def _scene_text(scopes) -> str:
    """场景定义：让 AI 自己理解公开/私密边界（替代 bot 硬编码“不要混用”提示）。"""
    if not scopes:
        return ""
    if any(s.startswith("c2c:") for s in scopes):
        return (
            "【当前场景】你在和用户私聊，对话私密，只有你们两人可见；"
            "可以提起用户在群聊里说过的事。涉及用户个人事实（名字/宠物/经历/关系）"
            "只能依据记忆回答，禁止编造具体细节，查不到就明说。"
        )
    gids = set()
    for s in scopes:
        if s.startswith("group:") or s.startswith("group_all:"):
            gids.add(s.split(":", 1)[1])
    if gids:
        return (
            "【当前场景】你在群聊，其他成员能看到你的回复。"
            "不要透露用户私聊中说过的话，除非那条记忆被标记为公开（public）。"
        )
    return ""


def _now_text() -> str:
    """当前时间参考（按用户时区，自然口语）。"""
    from memory import tz as tz_mod
    return tz_mod.now_text()


def _presence_text() -> str:
    """存在形态：让 AI 理解自己是线上实体，与用户有物理距离。"""
    return (
        "【存在形态】你在线上（网络聊天），用户在线下，和你有物理距离。"
        "用户提到地点、距离、见面、实物交接时：可以约定、安排、共情，"
        "但不能真的出现在现场或亲手拿到实物；按角色扮演自然应对即可。"
    )


def _extra_scopes(scopes) -> list[str]:
    """跨场景召回：群聊补充该群已绑定成员的 public 私聊记忆；私聊补充该用户群聊记忆。"""
    extra = []
    if not scopes:
        return extra
    gids = {
        s.split(":", 1)[1]
        for s in scopes
        if s.startswith("group:") or s.startswith("group_all:")
    }
    if gids:
        for uid, groups in _db.bindings_all().items():
            if any(gid in groups for gid in gids):
                extra.append(f"c2c:{uid}")
        return extra
    for s in scopes:
        if s.startswith("c2c:"):
            uid = s.split(":", 1)[1]
            for gid in (_db.binding_groups_for_user(uid) or {}):
                extra.append(f"group:{gid}")
                extra.append(f"group_all:{gid}")
            break
    return extra


_TIME_FRAG_RE = re.compile(r"周[一二三四五六日天]|月[底末]|\d{1,2}\s*[号日]|日期是|周[一二三四五六日天]")


def _time_fragment_hint(mem_ctx):
    """检测检索 context 里的孤立时间碎片（周日/30号/月底…），生成「关联到事件」的提示。
    碎片和主体事件（如「月底演出」）常常分条存储，生成层不引导就容易被忽略。"""
    frags = []
    for line in (mem_ctx or "").splitlines():
        line = line.strip().strip("·-* ")
        if line and len(line) < 16 and _TIME_FRAG_RE.search(line):
            frags.append(line)
    if not frags:
        return ""
    return (
        "【时间关联提示】上面记忆里有孤立的时间碎片（"
        + "、".join(f"「{f}」" for f in frags[:3])
        + "），它们很可能是某个事件（演出/约定/计划）的补充信息。"
        "回答时把日期/星期关联到对应事件给出完整答案；确实关联不上的才说记不清。"
    )


def ask(
    text,
    history=None,
    extra_context="",
    scopes=None,
    learn=False,
    learn_scope=None,
    learn_key="",
    facts=None,
    system=None,
    llm=None,
):
    """Agent 主入口：分析 → 记忆上下文 → 人格合成 → LLM → （可选）学习。

    返回 (reply, meta)。llm(text, extra_context, history, system) -> str，
    默认走 DeepSeek；learn=True 时自行 ingest（适合 Hermes/API 等无插件场景，
    QQ 前台由 plugins/memory.py 的 after_chat 学习，传 learn=False 避免重复）。"""
    meta = {"analysis": analyze(text or "")}
    try:
        from memory import emotion as emotion_mod
        emotion_mod.ai_apply(meta["analysis"], text, scopes[0] if scopes else "")  # AI 情绪状态机（v31）
        if scopes:
            emotion_mod.user_observe(scopes[0], meta["analysis"], text)  # 用户情绪观测（v31）
            emotion_mod.record_feedback(scopes[0], text)  # 情绪自述/纠正 → 训练标签（v31）
        from memory import sharing as sharing_mod
        sharing_mod.on_conversation(meta["analysis"], text, scopes[0] if scopes else "")  # 分享欲对话事件（v31）
        if scopes:
            sharing_mod.on_annoyed(scopes[0], text)  # 用户嫌烦反馈（v31）
    except Exception as e:
        _stats_err(e)
        pass
    try:
        # bandit（v2.2+）：用本次消息的情绪/反馈给"上一条回复用的策略"打奖励
        from memory import bandit as bandit_mod
        if scopes:
            bandit_mod.update(scopes[0], bandit_mod.reward_from_message(text, meta["analysis"]))
    except Exception as e:
        _stats_err(e)
        pass
    ctx_parts = []
    try:
        from memory import sleep as sleep_mod
        if scopes:
            scope0 = scopes[0]
            urgent = sleep_mod.is_urgent(text, meta["analysis"])
            mode = sleep_mod.sleep_mode()
            if scope0.startswith("group") and mode == "deep":
                mode = "standby"  # 群里不真离线，最多待机
            if mode == "deep":
                sleep_mod.queue_add(scope0, text, urgent)
                if sleep_mod.emergency_wake(scope0, urgent):
                    ctx_parts.append(
                        "【系统级紧急唤醒】用户在深睡时段连续发来紧急消息。"
                        "清醒、简短地回应一句，确认没事后让她继续睡。"
                    )
                else:
                    return "", meta  # 深睡档真离线：不回复，醒来统一补
            else:
                q = sleep_mod.queue_snapshot(scope0)
                if q.get("items"):
                    block = sleep_mod.queue_deliver_block(q)
                    sleep_mod.queue_take(scope0)
                    if block:
                        ctx_parts.append(block)
                if mode == "standby":
                    ctx_parts.append(sleep_mod.standby_block(scope0, text))
                    sleep_mod.record_interrupt(scope0)
    except Exception as e:
        _stats_err(e)
        pass
    try:
        from memory import schedule as schedule_mod
        cur = schedule_mod.current_activity()
        if cur and cur.get("activity") in ("rehearsal", "performance"):
            ctx_parts.append(
                "【忙碌中】此刻正处于忙碌日程（排练/演出），回复尽量简短，被追问才展开，别长篇大论。"
            )
    except Exception as e:
        _stats_err(e)
        pass
    try:
        from memory import tz as tz_mod
        ctx_parts.append(tz_mod.now_text(scopes[0] if scopes else None))
    except Exception as e:
        _stats_err(e)
        ctx_parts.append(_now_text())
    time_q = bool(_TIME_Q_RE.match(text or ""))
    if time_q:
        ctx_parts.append(
            "【时间问题·硬性要求】用户问的是当前时间/日期。"
            "必须严格以【时间参考】里的时间逐字为准，"
            "禁止参考历史对话或记忆里的时间，禁止编造其他时间。"
        )
    _t_trim = (text or "").strip()
    _last_bot = ""
    for _m in reversed(history or []):
        if _m.get("role") == "assistant":
            _last_bot = str(_m.get("content") or "")
            break
    _gap = bool(
        _MEMORY_GAP_RE.search(_t_trim)
        or (len(_t_trim) <= 6 and re.search(r"什么", _t_trim))
    )
    if _gap:
        # 记忆缺口（v2.3 修复 P0-1 生成层）：用户不记得/困惑时如实承认，禁止编造约定/细节/未来承诺
        ctx_parts.append(
            "【记忆缺口·硬性要求】用户明确表示不记得/记忆缺失。如实承认不确定："
            "可以基于已有记忆温和提示，但禁止编造任何约定、具体细节、未来承诺或人物关系；"
            "查不到就用你的角色语气含糊带过（'想不起来了''别较真'这类），"
            "别跳出角色说'我没有记录''作为 AI'这类话，绝不把推测说成事实。"
        )
    _confirm_words = ("好的", "好呀", "好啊", "行", "嗯好", "可以", "没问题", "当然", "OK", "ok")
    _is_confirm = any(w in _t_trim for w in _confirm_words)
    if _LAST_BOT_CLAIM_RE.search(_last_bot) and not _is_confirm:
        # 上文 bot 声称过"约好/答应"：除非约定表/记忆里有对应记录，否则不得继续坚持
        ctx_parts.append(
            "【约定核验·硬性要求】你上一条提到了'约好/答应'之类。先核验记忆库/约定表："
            "有对应记录才可继续提；查不到就明确收回（'我好像记岔了，没这回事'），"
            "禁止把推测的约定说成既成事实。"
        )
    if _APPT_TOPIC_RE.search(_t_trim):
        # 约定验证前置（最硬机制）：声称"约好的事"之前先检索，无记录禁止提
        try:
            from memory import appointment as appt_mod
            if scopes and not appt_mod.context_block(scopes[0]):
                ctx_parts.append(
                    "【约定验证·硬性要求】已检索：该用户的约定表/记忆里没有任何约定记录。"
                    "禁止声称'约好的事/你答应过/说好要…'，如实说'我好像没跟你约过这个'。"
                )
        except Exception as e:
            _stats_err(e)
    ctx_parts.append(_presence_text())
    scene = _scene_text(scopes)
    if scene:
        ctx_parts.append(scene)
    if scopes:
        try:
            from memory import context as context_mod
            from memory import emotion as emotion_mod
            from memory import sleep as sleep_mod
            from memory import schedule as schedule_mod
            from memory import environment as env_mod
            from memory import living as living_mod
            from memory import expression
            from memory import relationship
            from memory import appointment
            from memory import mistake
            from memory import relationship as rel_mod2
            if rel_mod2.note_return(scopes[0]):  # 久别重逢（v31.2）
                ctx_parts.append(
                    "【久别重逢】用户隔了挺久才回来。自然带一点'好久不见/有点生疏但还记得你'的感觉，"
                    "别太热情也别太冷淡，别主动提具体隔了多久。"
                )
            if s := context_mod.user_state_block(scopes[0]):
                ctx_parts.append(s)
            if s := emotion_mod.user_block(scopes[0]):  # 用户情绪块（v31）
                ctx_parts.append(s)
            if s := emotion_mod.attribution_block(scopes[0]):  # 情绪归因（v31.2）
                ctx_parts.append(s)
            if s := sleep_mod.context_block(scopes[0], text):  # 昨晚的梦（v31）
                ctx_parts.append(s)
            if s := schedule_mod.block(scopes[0]):  # 此刻状态（日程表 v31）
                ctx_parts.append(s)
            if s := env_mod.block(scopes[0], text):  # 周围环境（v31）
                ctx_parts.append(s)
            if s := living_mod.home_block(scopes[0], text):  # 生活层（v31）
                ctx_parts.append(s)
            if s := living_mod.birthday_hint_block(scopes[0], text):  # 生日暗示（v31.3）
                ctx_parts.append(s)
            if s := living_mod.birthday_reaction_block(scopes[0], text):  # 生日祝贺（v31.3）
                ctx_parts.append(s)
            if s := relationship.describe(scopes[0]):
                ctx_parts.append(s)
            if s := expression.describe(scopes[0]):  # 表达适配（v7）
                ctx_parts.append(s)
            if s := appointment.context_block(scopes[0]):  # 待履约约定（v23）
                ctx_parts.append(s)
            if s := mistake.context_block(scopes[0], text):  # 近期错误与原谅（v23）
                ctx_parts.append(s)
            if _STRUCTURED_DOMAIN_RE.search(text or ""):
                # 方向 1（确定性优先）：日程/约定/设备等先查结构化块，按它答，查不到就明说
                ctx_parts.append(
                    "【结构化事实优先·硬性要求】本问题的答案以上面的【待履约约定】【此刻状态】"
                    "【周围环境】等结构化块为准：查得到就按它答；查不到就用你的角色语气含糊带过，"
                    "别跳出角色说'我没查到'，禁止编造表格内容、金额、他人意见或具体日期。"
                )
        except Exception as e:
            _stats_err(e)
            pass
    if scopes and _core_enabled():
        s = session.touch(scopes[0], "", text)
        current = session.current(scopes[0], "")
        if current and current.get("topic"):
            ctx_parts.append(f"【当前话题】{current['topic']}（跨轮保持主线，别聊跑题）")
        recent_texts = [
            m.get("content", "") for m in (history or []) if m.get("role") == "user"
        ][-3:]
        if current and current.get("topic"):
            recent_texts.append(current["topic"])
        extra_scopes = _extra_scopes(scopes)
        try:
            from memory import character
            extra_scopes += character.match_scopes(text)
        except Exception as e:
            _stats_err(e)
            pass
        # 分层计算（v5 §P3）：极短查询用轻量检索（少召回、不扩展），控制成本
        light = len((text or "").strip()) <= 4
        mem_ctx = assemble_context(
            text,
            scopes,
            extra_scopes=extra_scopes,
            top_k=3 if light else 5,
            expand_query=not light,
            recent=recent_texts,
        )
        try:
            from memory import world
            if s := world.snapshot(scopes[0]):  # 用户中心世界模型（v8，硬预算+缓存）
                ctx_parts.append(s)
        except Exception as e:
            _stats_err(e)
            pass
        try:
            from memory import trace
            if mem_ctx:  # Memory Trace（v10）：记录回答依据（检索注入内容）
                trace.record(
                    scopes[0], speaker="system", raw_content=text,
                    candidate=mem_ctx[:200], action="inject", modules=["memory"],
                    reasoning="检索注入（回答依据）", hint="assemble_context",
                )
        except Exception as e:
            _stats_err(e)
            pass
        if mem_ctx:
            ctx_parts.append(mem_ctx)
            hint = _time_fragment_hint(mem_ctx)
            if hint:
                ctx_parts.append(hint)
        if mem_ctx and scopes:
            # 证据门控（v2.3 生成约束）：只有证据清单里"可引用"的内容才能当事实陈述
            ctx_parts.append(
                "【证据规则·硬性要求】只有上面检索注入记忆里能对应到的内容才能作为事实陈述："
                "用户亲口说的（·用户亲口说）与人设设定（·人设设定）可引用；"
                "'AI 推测'只能说'我好像记得'；"
                "查不到对应记录的细节/约定/人物关系就用你的角色语气含糊带过"
                "（'想不起来了''别较真'这类），别跳出角色说'我没记录'，禁止编造。"
            )
    if extra_context:
        ctx_parts.append(extra_context)
    if scopes:
        try:
            from memory import context as context_mod, subjects
            if names := subjects.detect(text):
                if s := context_mod.npc_memory_block(text, names):
                    ctx_parts.append(s)
        except Exception as e:
            _stats_err(e)
    if scopes and _mind_cfg("enabled", True):
        try:
            from memory import mind as mind_mod
            if s := mind_mod.block(scopes[0], text):
                ctx_parts.append(s)
            mind_mod.recompute_intention(scopes[0])
        except Exception as e:
            _stats_err(e)
            pass
    if scopes:
        try:
            # bandit（v2.2+）：Thompson 采样选回应策略，注入提示
            from memory import bandit as bandit_mod
            if bandit_mod._cfg("enabled", True):
                st = bandit_mod.select(scopes[0])
                meta["bandit"] = {"id": st["id"], "label": st["label"], "mean": st.get("mean")}
                ctx_parts.append(f"【回应策略】{st['label']}：{st['hint']}")
        except Exception as e:
            _stats_err(e)
            pass
    call = llm or _default_llm
    llm_text = text
    extra_ctx = "\n\n".join(ctx_parts)
    sys_prompt = system or persona.compose(query=text)
    # System 1：命中高成功率习惯 → 直接复用动作（省一次 LLM 调用）
    if llm is None and scopes and _mind_cfg("system1", True):
        try:
            from memory import procedures as procedures_mod
            hit = procedures_mod.match(text, scopes[0])
            if hit:
                reply = str(hit.get("action", ""))
                meta["system1"] = {
                    "situation": hit.get("situation", ""),
                    "success": hit.get("success", 0.0),
                    "tries": hit.get("tries", 0),
                }
                try:
                    import memory.stats as stats_mod
                    stats_mod.bump("system1_hit")
                except Exception as e:
                    _stats_err(e)
                    pass
                try:
                    from memory import living as living_mod2
                    living_mod2.sync_from_text(reply)
                except Exception as e:
                    _stats_err(e)
                    pass
                meta["reply"] = reply
                return reply, meta
            try:
                import memory.stats as stats_mod
                stats_mod.bump("system1_miss")
            except Exception as e:
                _stats_err(e)
                pass
        except Exception as e:
            _stats_err(e)
            pass
    if time_q:
        # 把实际时间锚进用户消息（模型最看重用户输入）+ 低温度防自由发挥
        try:
            from memory import tz as tz_mod
            now_head = tz_mod.now_text(scopes[0] if scopes else None).split("。")[0]
            llm_text = (
                f"（当前实际时间：{now_head}。用户问的就是这个时间，"
                "必须按它回答，不要猜、不要编造。）\n用户：" + text
            )
        except Exception as e:
            _stats_err(e)
            pass
    if time_q and llm is None:
        reply = _shared.ask_deepseek(
            llm_text, extra_context=extra_ctx, history=_clean_history(history or []),
            system=sys_prompt, temperature=0.3,
        )
    elif llm is None and _mind_cfg("cognitive_turn", False):
        # 单次结构化输出（认知循环）：appraisal/goals/intention/action/reply 压进一次调用
        raw = _shared.ask_deepseek(
            llm_text,
            extra_context=extra_ctx + _COGNITIVE_INSTRUCTION,
            history=_clean_history(history or []),
            system=sys_prompt,
            temperature=0.6,
            module="cognitive",
        )
        parsed = _parse_cognitive(raw)
        if parsed:
            reply = str(parsed.get("reply", ""))
            meta["cognitive"] = parsed
            try:
                import memory.stats as stats_mod
                stats_mod.bump("cognitive_ok")
            except Exception as e:
                _stats_err(e)
                pass
            try:
                if scopes:
                    from memory import mind as mind_mod
                    mind_mod.apply_cognitive(scopes[0], text, parsed)
            except Exception as e:
                _stats_err(e)
                pass
        else:
            try:
                import memory.stats as stats_mod
                stats_mod.bump("cognitive_fail")
            except Exception as e:
                _stats_err(e)
                pass
            reply = call(
                llm_text,
                extra_context=extra_ctx,
                history=_clean_history(history or []),
                system=sys_prompt,
            )
    else:
        reply = call(
            llm_text,
            extra_context=extra_ctx,
            history=_clean_history(history or []),  # 清洗时间断言而非整体砍历史（v30）
            system=sys_prompt,
        )
    try:
        from memory import living as living_mod
        living_mod.sync_from_text(reply or "")  # 生活细节回流（v31.2）
    except Exception as e:
        _stats_err(e)
        pass
    # 证据门控 v2（生成后验证，代码级拦截）：输出含无证据断言/黑名单词 → 重写
    try:
        from agent import evidence_gate
        from memory import context as ctx_mod, pack as pack_mod
        evidence = list(getattr(ctx_mod, "_last_evidence", None) or [])
        if scopes:
            try:
                from memory import appointment as appt_mod
                ab = appt_mod.context_block(scopes[0])
                if ab:
                    evidence.append(ab)
            except Exception:
                pass
        # 会话内证据（对话暴露的 bug）：用户当前消息本身就是证据——AI 确认用户刚说的事实
        # （"月底有场演出，是30号周日"）不该被门控当"推断/编造"打回成"记不太清"
        if text:
            evidence.append(str(text)[:200])
        banned = pack_mod.behavior().get("banned_claims") or []
        reason = evidence_gate.contains_unsupported_claim(reply, evidence, banned=banned, user_text=text)
        if not reason and evidence_gate.verify_reply_numbers(reply, evidence):
            # 数字硬门：回复里的数字/日期不在证据里 → 判编造（短回复也拦，不依赖语义自检）
            reason = "无证据数字"
        if not reason and evidence_gate.verify_reply_calendar(reply, evidence, user_text=text):
            # 日历推算硬门（v2.3 P1-2）："X号+周几"与真实日历推算不符（如 8 月"31号是周日"）
            # → 判编造/算错；日期类数字由代码推算验证，不靠 LLM 猜
            reason = "日历推算不符"
        if not reason and evidence_gate._sem_cfg("semantic", True):
            # 方向 3（语义自检）：正则放行但回复有实质内容时，让 LLM 标注断言依据
            reason = evidence_gate.semantic_annotate(reply, evidence, banned=banned, user_text=text)
        if reason:
            try:
                import memory.stats as _st
                if str(reason).startswith("语义推断"):
                    _st.bump("evidence_gate_hedge")  # 推断：只加含糊后缀，不重写
                else:
                    _st.bump("evidence_gate_block")  # 编造/黑名单：整句重写
            except Exception:
                pass
            meta["evidence_gate"] = reason
            if str(reason).startswith("语义推断"):
                # 推断放行（对话暴露的 bug）：LLM 标注"推断"= 合理猜测/不确定语气（如
                # "我好像没跟你约过什么吧？"）——曾经整句替换成"具体数字我记不太清了"模板，
                # 把正确的回答毁掉。编造由"语义编造"路径重写、数字由 verify_reply_numbers 拦。
                pass  # reply 保持原文
            else:
                # 编造/黑名单：用角色语气重新生成一次，而非套固定生硬句。
                # 约定类（无证据断言）要明确收回，不能含糊；其余一律自然带过。
                if "断言" in str(reason):
                    _reg = (
                        "【重写】你上一条提到了'约好/答应/说好'之类但没有证据。"
                        "用你的角色语气收回：明确说'我好像记岔了，没这回事'，"
                        "别再坚持，也别编造新的承诺。"
                    )
                else:
                    _reg = (
                        "【重写】你上一条回复被判定为编造了没有依据的具体细节（" + str(reason) + "）。"
                        "用你的角色语气重新回答用户，自然地含糊带过，"
                        "别编造具体的名字、数字、日期、金额、经历或承诺，"
                        "也别说'我没有记录''作为 AI'这类跳出角色的话。"
                    )
                try:
                    reply = call(
                        llm_text,
                        extra_context=extra_ctx + "\n\n" + _reg,
                        history=_clean_history(history or []),
                        system=sys_prompt,
                    )
                except Exception:
                    reply = evidence_gate.forgetful_reply("通用")
                # 二次校验（对话暴露的 bug）：LLM 重写可能再次编造假来源/无据细节
                # （"橘色。你亲口说的"重写后仍带假来源）——检测层确定、重写层概率，
                # 重写结果仍不干净 → 模板兜底（人设化诚实句，不含声称句式）
                try:
                    r2 = evidence_gate.contains_unsupported_claim(
                        reply, evidence, banned=banned, user_text=text
                    )
                    # v2.3 二次校验补语义层：词法门抓"来源声称"，抓不住"把推断当记忆"
                    # 的软编造（"我好像记得是橘色，因为煤球是橘猫对吧？"——无来源声称词、
                    # 无数字，但'我记得'是记忆断言而证据里没有'橘色偏好'）。
                    # 语义标注"编造"→ 模板兜底；"推断"→ 保留重写结果（与主流程一致：
                    # 推断含糊表达可放行，只有明确的记忆/事实断言必须干净）
                    if not r2:
                        _r2s = evidence_gate.semantic_annotate(
                            reply, evidence, banned=banned, user_text=text
                        )
                        if _r2s and "编造" in str(_r2s):
                            r2 = _r2s
                    if r2:
                        # 按话题类型选兜底表达（数字/约定/事实/通用），不再是固定一句
                        reply = evidence_gate.forgetful_reply("", topic=str(text or "")[:20])
                except Exception:
                    pass
    except Exception:
        pass
    meta["reply"] = reply
    if learn and learn_scope:
        meta["learn"] = ingest(learn_scope, learn_key, text, reply, facts=facts)
        # 主动自我编辑（MEMGPT 主动记忆，方案 B）：后置小调用，默认关
        try:
            from memory import controller as controller_mod
            meta["active_edit"] = controller_mod.active_edit(learn_scope, learn_key, text, reply)
        except Exception as e:
            _stats_err(e)
    return reply, meta


def learn(text, reply, scope, key, facts=None) -> dict:
    """显式学习一条对话（等价 memory.ingest，语义化命名）。"""
    return ingest(scope, key, text, reply, facts=facts)


def grow(scope=None, dry_run=False) -> dict:
    """成长接口（工程化）：返回结构化报告；dry_run=True 只出统计不写库。"""
    import memory
    if dry_run:
        return {"stats": memory.eval_report()}
    return memory.backfill_run(batch=64)



def _stats_err(e):
    """裸 except 审计（v2.2）：错误计数 + 日志，供消融/排查。"""
    try:
        import memory.stats as _st
        _st.bump_err("core", e)
    except Exception:
        pass
