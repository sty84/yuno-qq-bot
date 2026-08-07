"""Agent 核心：分析 → 人格 → 记忆 → 云端 LLM → 反馈学习（成长）。"""

from plugins import _db, _shared
from memory import analyze, assemble_context, ingest, session
from agent import persona


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
    ctx_parts = []
    scene = _scene_text(scopes)
    if scene:
        ctx_parts.append(scene)
    if scopes:
        try:
            from memory import context as context_mod
            from memory import expression
            from memory import relationship
            if s := context_mod.user_state_block(scopes[0]):
                ctx_parts.append(s)
            if s := relationship.describe(scopes[0]):
                ctx_parts.append(s)
            if s := expression.describe(scopes[0]):  # 表达适配（v7）
                ctx_parts.append(s)
        except Exception:
            pass
    if scopes and _core_enabled():
        s = session.touch(scopes[0], "", text)
        current = session.current(scopes[0], "")
        recent_texts = [
            m.get("content", "") for m in (history or []) if m.get("role") == "user"
        ][-3:]
        if current and current.get("topic"):
            recent_texts.append(current["topic"])
        extra_scopes = _extra_scopes(scopes)
        try:
            from memory import character
            extra_scopes += character.match_scopes(text)
        except Exception:
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
        except Exception:
            pass
        try:
            from memory import trace
            if mem_ctx:  # Memory Trace（v10）：记录回答依据（检索注入内容）
                trace.record(
                    scopes[0], speaker="system", raw_content=text,
                    candidate=mem_ctx[:200], action="inject", modules=["memory"],
                    reasoning="检索注入（回答依据）", hint="assemble_context",
                )
        except Exception:
            pass
        if mem_ctx:
            ctx_parts.append(mem_ctx)
    if extra_context:
        ctx_parts.append(extra_context)
    call = llm or _default_llm
    reply = call(
        text,
        extra_context="\n\n".join(ctx_parts),
        history=history or [],
        system=system or persona.compose(),
    )
    meta["reply"] = reply
    if learn and learn_scope:
        meta["learn"] = ingest(learn_scope, learn_key, text, reply, facts=facts)
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
