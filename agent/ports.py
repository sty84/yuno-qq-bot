# -*- coding: utf-8 -*-
"""Agent 认知架构端口：记忆/决策/动作三端口实现。

从 agent.core 拆分出来，保持 agent.ask 统一由 CognitiveArchitecture 编排。
"""
import re

from plugins import _db, _shared
from memory import analyze, assemble_context, ingest, session
from memory.interfaces import MemoryPort, DecisionPort, ActionPort
from agent import persona

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
    extra: list[str] = []
    if not scopes:
        return extra
    gids = {
        s.split(":", 1)[1]
        for s in scopes
        if s.startswith("group:") or s.startswith("group_all:")
    }
    if gids:
        for uid, groups in _db.bindings_all().items():  # type: ignore[attr-defined]
            if any(gid in groups for gid in gids):
                extra.append(f"c2c:{uid}")
        return extra
    for s in scopes:
        if s.startswith("c2c:"):
            uid = s.split(":", 1)[1]
            for gid in (_db.binding_groups_for_user(uid) or {}):  # type: ignore[attr-defined]
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




class _AgentMemoryPort(MemoryPort):
    """Agent 记忆端口：复用 assemble_context，保留上下文供 Action 使用。"""

    def __init__(self):
        self.last_mem_ctx = ""
        self.last_evidence = []
        self.last_scopes = []
        self.last_extra_scopes = []
        self.last_recent_texts = []
        self.last_current_topic = ""
        self.last_world_block = ""

    def search(self, query, scopes, top_k=5, **kwargs):
        full_scopes = kwargs.get("scopes") or scopes
        history = kwargs.get("history") or []
        self.last_scopes = list(full_scopes or [])
        if not self.last_scopes or not _core_enabled():
            self.last_mem_ctx = ""
            self.last_evidence = []
            self.last_current_topic = ""
            self.last_world_block = ""
            return []
        session.touch(self.last_scopes[0], "", query)
        current = session.current(self.last_scopes[0], "")
        self.last_current_topic = (current or {}).get("topic", "") or ""
        recent_texts = [
            m.get("content", "") for m in history if m.get("role") == "user"
        ][-3:]
        if current and current.get("topic"):
            recent_texts.append(current["topic"])
        self.last_recent_texts = recent_texts
        extra_scopes = _extra_scopes(self.last_scopes)
        try:
            from memory import character
            extra_scopes += character.match_scopes(query)
        except Exception as e:
            _stats_err(e)
            pass
        self.last_extra_scopes = extra_scopes
        light = len((query or "").strip()) <= 4
        mem_ctx = assemble_context(
            query,
            self.last_scopes,
            extra_scopes=extra_scopes,
            top_k=3 if light else 5,
            expand_query=not light,
            recent=recent_texts,
            evidence_out=self.last_evidence,
        )
        self.last_mem_ctx = mem_ctx
        try:
            from memory import world
            if s := world.snapshot(self.last_scopes[0]):
                self.last_world_block = s
        except Exception as e:
            _stats_err(e)
            pass
        try:
            from memory import trace
            if mem_ctx:
                trace.record(
                    self.last_scopes[0], speaker="system", raw_content=query,
                    candidate=mem_ctx[:200], action="inject", modules=["memory"],
                    reasoning="检索注入（回答依据）", hint="assemble_context",
                )
        except Exception as e:
            _stats_err(e)
            pass
        return [
            {"fact": f, "score": 0.0, "scope": self.last_scopes[0]}
            for f in self.last_evidence
        ]


class _AgentDecisionPort(DecisionPort):
    """Agent 决策端口：完成旧核心中记忆检索前的上下文与状态准备。"""

    def decide(self, query, scope="", context="", *args, **kwargs):
        text = query
        scopes = kwargs.get("scopes")
        history = kwargs.get("history")
        meta = {"analysis": analyze(text or "")}
        try:
            from memory import emotion as emotion_mod
            emotion_mod.ai_apply(meta["analysis"], text, scopes[0] if scopes else "")
            if scopes:
                emotion_mod.user_observe(scopes[0], meta["analysis"], text)
                emotion_mod.record_feedback(scopes[0], text)
            from memory import sharing as sharing_mod
            sharing_mod.on_conversation(meta["analysis"], text, scopes[0] if scopes else "")
            if scopes:
                sharing_mod.on_annoyed(scopes[0], text)
        except Exception as e:
            _stats_err(e)
            pass
        try:
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
                    mode = "standby"
                if mode == "deep":
                    sleep_mod.queue_add(scope0, text, urgent)
                    if sleep_mod.emergency_wake(scope0, urgent):
                        ctx_parts.append(
                            "【系统级紧急唤醒】用户在深睡时段连续发来紧急消息。"
                            "清醒、简短地回应一句，确认没事后让她继续睡。"
                        )
                    else:
                        return {
                            "early_reply": "",
                            "early_meta": meta,
                            "ctx_parts": ctx_parts,
                            "meta": meta,
                            "time_q": False,
                            "situation": {},
                            "goals": [],
                            "intention": {},
                            "options": [],
                        }
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
            ctx_parts.append(
                "【记忆缺口·硬性要求】用户明确表示不记得/记忆缺失。如实承认不确定："
                "可以基于已有记忆温和提示，但禁止编造任何约定、具体细节、未来承诺或人物关系；"
                "查不到就用你的角色语气含糊带过（'想不起来了''别较真'这类），"
                "别跳出角色说'我没有记录''作为 AI'这类话，绝不把推测说成事实。"
            )
        _confirm_words = ("好的", "好呀", "好啊", "行", "嗯好", "可以", "没问题", "当然", "OK", "ok")
        _is_confirm = any(w in _t_trim for w in _confirm_words)
        if _LAST_BOT_CLAIM_RE.search(_last_bot) and not _is_confirm:
            ctx_parts.append(
                "【约定核验·硬性要求】你上一条提到了'约好/答应'之类。先核验记忆库/约定表："
                "有对应记录才可继续提；查不到就明确收回（'我好像记岔了，没这回事'），"
                "禁止把推测的约定说成既成事实。"
            )
        if _APPT_TOPIC_RE.search(_t_trim):
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
                if rel_mod2.note_return(scopes[0]):
                    ctx_parts.append(
                        "【久别重逢】用户隔了挺久才回来。自然带一点'好久不见/有点生疏但还记得你'的感觉，"
                        "别太热情也别太冷淡，别主动提具体隔了多久。"
                    )
                if s := context_mod.user_state_block(scopes[0]):
                    ctx_parts.append(s)
                if s := emotion_mod.user_block(scopes[0]):
                    ctx_parts.append(s)
                if s := emotion_mod.attribution_block(scopes[0]):
                    ctx_parts.append(s)
                if s := sleep_mod.context_block(scopes[0], text):
                    ctx_parts.append(s)
                if s := schedule_mod.block(scopes[0]):
                    ctx_parts.append(s)
                if s := env_mod.block(scopes[0], text):
                    ctx_parts.append(s)
                if s := living_mod.home_block(scopes[0], text):
                    ctx_parts.append(s)
                if s := living_mod.birthday_hint_block(scopes[0], text):
                    ctx_parts.append(s)
                if s := living_mod.birthday_reaction_block(scopes[0], text):
                    ctx_parts.append(s)
                if s := relationship.describe(scopes[0]):
                    ctx_parts.append(s)
                if s := expression.describe(scopes[0]):
                    ctx_parts.append(s)
                if s := appointment.context_block(scopes[0]):
                    ctx_parts.append(s)
                if s := mistake.context_block(scopes[0], text):
                    ctx_parts.append(s)
                if _STRUCTURED_DOMAIN_RE.search(text or ""):
                    ctx_parts.append(
                        "【结构化事实优先·硬性要求】本问题的答案以上面的【待履约约定】【此刻状态】"
                        "【周围环境】等结构化块为准：查得到就按它答；查不到就用你的角色语气含糊带过，"
                        "别跳出角色说'我没查到'，禁止编造表格内容、金额、他人意见或具体日期。"
                    )
            except Exception as e:
                _stats_err(e)
                pass
        return {
            "early_reply": None,
            "early_meta": None,
            "ctx_parts": ctx_parts,
            "meta": meta,
            "time_q": time_q,
            "scopes": scopes,
            "situation": {},
            "goals": [],
            "intention": {},
            "options": [],
        }


class _AgentActionPort(ActionPort):
    """Agent 动作端口：基于决策上下文与记忆结果生成最终回复。"""

    def execute(self, action, **kwargs):
        text = kwargs.get("text", "")
        decision = kwargs.get("decision", {})
        memory = kwargs.get("memory")
        history = kwargs.get("history")
        extra_context = kwargs.get("extra_context", "")
        scopes = decision.get("scopes") or kwargs.get("scopes")
        learn = kwargs.get("learn", False)
        learn_scope = kwargs.get("learn_scope")
        learn_key = kwargs.get("learn_key", "")
        facts = kwargs.get("facts")
        system = kwargs.get("system")
        llm = kwargs.get("llm")

        meta = dict(decision.get("meta") or {})
        ctx_parts = list(decision.get("ctx_parts") or [])
        mem_ctx = (memory.last_mem_ctx if memory else "") or ""
        mem_evidence = list(memory.last_evidence) if memory else []

        from memory.controller import _is_social_message
        is_social = _is_social_message(text)

        if memory and memory.last_current_topic:
            ctx_parts.append(
                f"【当前话题】{memory.last_current_topic}（跨轮保持主线，别聊跑题）"
            )
        # 寒暄/短社交消息不注入世界模型和记忆检索，避免被旧记忆带偏
        if not is_social and memory and memory.last_world_block:
            ctx_parts.append(memory.last_world_block)

        if not is_social and mem_ctx:
            ctx_parts.append(mem_ctx)
            hint = _time_fragment_hint(mem_ctx)
            if hint:
                ctx_parts.append(hint)
        if not is_social and mem_ctx and scopes:
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
                from memory import bandit as bandit_mod
                if bandit_mod._cfg("enabled", True):
                    st = bandit_mod.select(scopes[0])
                    meta["bandit"] = {"id": st["id"], "label": st["label"], "mean": st.get("mean")}
                    if not is_social:
                        ctx_parts.append(f"【回应策略】{st['label']}：{st['hint']}")
            except Exception as e:
                _stats_err(e)
                pass
        if is_social:
            ctx_parts.append(
                "【寒暄模式·硬性要求】用户只是在寒暄/问你在干嘛。"
                "直接简短自然地回答当前状态（例如“没干嘛，在休息”），"
                "不要主动提起记忆、日程、旧话题、音乐术语或具体项目。"
            )

        call = llm or _default_llm
        llm_text = text
        extra_ctx = "\n\n".join(ctx_parts)
        sys_prompt = system or persona.compose(query=text)
        time_q = decision.get("time_q", False)

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
                    return {"reply": reply, "meta": meta}
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
                history=_clean_history(history or []),
                system=sys_prompt,
            )

        try:
            from memory import living as living_mod
            living_mod.sync_from_text(reply or "")
        except Exception as e:
            _stats_err(e)
            pass

        # 证据门控 v2（生成后验证，代码级拦截）
        try:
            from agent import evidence_gate
            from memory import pack as pack_mod
            evidence = list(mem_evidence)
            if scopes:
                try:
                    from memory import appointment as appt_mod
                    ab = appt_mod.context_block(scopes[0])
                    if ab:
                        evidence.append(ab)
                except Exception:
                    pass
            if text:
                evidence.append(str(text)[:200])
            banned = pack_mod.behavior().get("banned_claims") or []
            reason = evidence_gate.contains_unsupported_claim(reply, evidence, banned=banned, user_text=text)
            if not reason and evidence_gate.verify_reply_numbers(reply, evidence):
                reason = "无证据数字"
            if not reason and evidence_gate.verify_reply_calendar(reply, evidence, user_text=text):
                reason = "日历推算不符"
            if not reason and evidence_gate._sem_cfg("semantic", True):
                reason = evidence_gate.semantic_annotate(reply, evidence, banned=banned, user_text=text)
            if reason:
                try:
                    import memory.stats as _st
                    if str(reason).startswith("语义推断"):
                        _st.bump("evidence_gate_hedge")
                    else:
                        _st.bump("evidence_gate_block")
                except Exception:
                    pass
                meta["evidence_gate"] = reason
                if str(reason).startswith("语义推断"):
                    pass
                else:
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
                    try:
                        r2 = evidence_gate.contains_unsupported_claim(
                            reply, evidence, banned=banned, user_text=text
                        )
                        if not r2:
                            _r2s = evidence_gate.semantic_annotate(
                                reply, evidence, banned=banned, user_text=text
                            )
                            if _r2s and "编造" in str(_r2s):
                                r2 = _r2s
                        if r2:
                            reply = evidence_gate.forgetful_reply("", topic=str(text or "")[:20])
                    except Exception:
                        pass
        except Exception:
            pass

        meta["reply"] = reply
        if learn and learn_scope:
            meta["learn"] = ingest(learn_scope, learn_key, text, reply, facts=facts)
            try:
                from memory import controller as controller_mod
                meta["active_edit"] = controller_mod.active_edit(learn_scope, learn_key, text, reply)
            except Exception as e:
                _stats_err(e)
        return {"reply": reply, "meta": meta}




def _stats_err(e):
    """裸 except 审计（v2.2）：错误计数 + 日志，供消融/排查。"""
    try:
        import memory.stats as _st
        _st.bump_err("core", e)
    except Exception:
        pass
