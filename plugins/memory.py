"""记忆与身份插件：分场景记忆、自动提取、群 ↔ 私聊绑定。"""

import asyncio
import random
import re
import time
from datetime import datetime

from plugins import _db
from plugins import _shared
import memory

NAME = "记忆系统"
HELP = (
    "/我的记忆｜/群记忆｜/昵称 名字｜"
    "/绑定（群里生成码，私聊 /绑定 群ID 码）｜/解绑｜"
    "/忘记 关键词（不再提起相关记忆）｜/公开 关键词（允许跨场景提起）"
)

_binding_codes = {}  # type: ignore[var-annotated]
_last_extract = {}  # type: ignore[var-annotated]
_sess_buffer = {}   # type: ignore[var-annotated]  # key -> {"texts": [...], "reply": str, "last": ts} 低信息消息缓存
_last_group_extract = {}  # type: ignore[var-annotated]
IGT_FLUSH_SEC = 600
IGT_MAX_BUFFER = 5
IGT_THRESHOLD = 0.3


def _safe_id(value):
    return re.sub(r"[^A-Za-z0-9_\-]", "_", value or "unknown")[:64]


nice_fact = memory.nice_fact


def load_user_memory(ukey):
    return {
        "facts": _db.memory_get(f"c2c:{ukey}"),
        "updated_at": _db.memory_updated_at(f"c2c:{ukey}"),
    }


def save_user_memory(ukey, data, embeddings=None):
    _db.memory_replace_preserve(f"c2c:{ukey}", "", data.get("facts", []), data.get("updated_at", ""))


def load_group_memory(gid):
    key = str(gid)
    return {
        "facts": _db.memory_get(f"group_all:{key}"),
        "message_count": _db.kv_get("group", f"{key}:count", 0),
        "updated_at": _db.memory_updated_at(f"group_all:{key}"),
    }


def save_group_memory(gid, data, embeddings=None):
    key = str(gid)
    scope = f"group_all:{key}"
    _db.memory_replace_preserve(scope, "", data.get("facts", []), data.get("updated_at", ""))
    _db.kv_set("group", f"{key}:count", data.get("message_count", 0))


def load_member_memory(gid, mid):
    return {
        "facts": _db.memory_get(f"group:{gid}", str(mid)),
        "updated_at": _db.memory_updated_at(f"group:{gid}", str(mid)),
    }


def save_member_memory(gid, mid, data, embeddings=None):
    scope, sk = f"group:{gid}", str(mid)
    _db.memory_replace_preserve(scope, sk, data.get("facts", []), data.get("updated_at", ""))


def load_scene_memory(message):
    kind, k1, k2 = _shared.scene_memory_keys(message)
    return load_member_memory(k1, k2) if kind == "group" else load_user_memory(k1)


def save_scene_memory(message, data, embeddings=None):
    kind, k1, k2 = _shared.scene_memory_keys(message)
    if kind == "group":
        save_member_memory(k1, k2, data, embeddings)
    else:
        save_user_memory(k1, data, embeddings)


merge_facts = memory.merge_facts


def clear_memory(kind, key):
    """后台调试用：按场景清空记忆。kind: user / member / group。"""
    if kind == "user":
        _db.memory_clear(f"c2c:{key}")
    elif kind == "member":
        if ":" not in str(key):
            return False
        gid, _, mid = str(key).partition(":")
        _db.memory_clear(f"group:{gid}", mid)
    elif kind == "group":
        _db.memory_clear(f"group_all:{key}")
    else:
        return False
    return True


# ===== 绑定 =====
def bind_user_to_member(uid, gid, mid):
    _db.binding_set(uid, gid, mid)


def find_user_for_member(gid, mid):
    return _db.binding_find_user_for_member(gid, mid)


def find_member_for_user(uid):
    return _db.binding_groups_for_user(uid)


def unbind_member(gid, mid):
    existed = _db.binding_find_user_for_member(gid, mid) is not None
    _db.binding_delete_member(gid, mid)
    return existed


def unbind_user_group(uid, gid):
    existed = _db.binding_groups_for_user(uid).get(gid) is not None
    _db.binding_delete_user_group(uid, gid)
    return existed


# ===== 自动提取（实现已迁至 memory/extract.py）=====
extract_facts = memory.extract_facts


# ===== 指令 =====
def _show_facts(title, facts):
    shown = [nice_fact(f) for f in facts[-10:]]
    return title + "：\n" + ("\n".join("- " + f for f in shown) if shown else "（还没有记住什么）")


def cmd_my_memory(text, ctx):
    kind = ctx.scene[0]
    facts = load_scene_memory(ctx.message).get("facts", [])
    return _show_facts("你在本群的记忆" if kind == "group" else "你在私聊中的记忆", facts)


def cmd_group_memory(text, ctx):
    if ctx.scene[0] != "group":
        return "这个指令只能在群里使用。"
    return _show_facts("本群的记忆", load_group_memory(ctx.scene[1]).get("facts", []))


def cmd_nickname(text, ctx):
    name = text[len("/昵称"):].strip()
    if not name:
        return "用法：/昵称 名字，例如：/昵称 由乃酱"
    _shared.set_nickname(ctx.user_key, name)
    # 昵称同步到已绑定的另一场景（群 ↔ 私聊）
    kind, k1, k2 = ctx.scene
    synced = False
    if kind == "group":
        if uid := find_user_for_member(k1, k2):
            _shared.set_nickname(uid, name)
            synced = True
    else:
        for mid in find_member_for_user(k1).values():
            _shared.set_nickname(mid, name)
            synced = True
    return f"昵称已设为：{name}" + ("（已同步到绑定的群/私聊）" if synced else "")


async def post_group_notice(ctx, gid, uid, mid):
    try:
        await ctx.api.post_group_message(
            group_openid=gid,
            content=(
                f"【身份绑定】私聊用户 {uid[:8]}… 已与本群成员 {mid[:8]}… 完成绑定。"
                f"如非本人操作，请私聊发送 /解绑 {gid}，或在群内发送 /解绑。"
            ),
        )
    except Exception as e:
        print(f"群公告发送失败：{e}")


async def cmd_bind(text, ctx):
    kind, k1, k2 = ctx.scene
    if kind == "group":
        now_t = time.time()
        for code in [c for c, v in _binding_codes.items() if v["expire"] < now_t]:
            _binding_codes.pop(code, None)
        code = str(random.randint(100000, 999999))
        _binding_codes[code] = {"group_id": k1, "member_openid": k2, "expire": now_t + 300}
        return (
            f"绑定码：{code}（5 分钟内有效）\n"
            f"请到私聊给机器人发送：/绑定 {k1} {code}\n"
            f"（群ID：{k1}）"
        )
    rest = text[len("/绑定"):].strip()
    if rest in ("状态", "查看"):
        binds = find_member_for_user(k1)
        if not binds:
            return "还没有绑定任何群。"
        return "已绑定的群：\n" + "\n".join(
            f"{gid}（成员 {mid[:12]}…）" for gid, mid in binds.items()
        )
    parts = rest.split()
    if len(parts) == 2:
        gid, code = parts
        info = _binding_codes.pop(code, None)
        if not info or info["expire"] < time.time():
            return "绑定码无效或已过期，请重新在群里获取。"
        if info["group_id"] != gid:
            return "群ID不匹配，请核对后再试。"
        bind_user_to_member(k1, gid, info["member_openid"])
        if ctx.api is not None:
            await post_group_notice(ctx, gid, k1, info["member_openid"])
        return "绑定成功！之后我就知道私聊的你和该群里的是同一个人，但两边的记忆会分开保存。"
    return "用法（私聊）：/绑定 群ID 绑定码　或　/绑定 状态"


def cmd_unbind(text, ctx):
    kind, k1, k2 = ctx.scene
    if kind == "group":
        ok = unbind_member(k1, k2)
        return "已解除本群身份与私聊的绑定。" if ok else "本群身份没有绑定记录。"
    rest = text[len("/解绑"):].strip()
    if not rest:
        return "用法（私聊）：/解绑 群ID"
    ok = unbind_user_group(k1, rest.strip())
    return "已解除该群的绑定。" if ok else "没有找到该群的绑定记录。"


def cmd_forget(text, ctx):
    """用户主动遗忘：把相关记忆可信度压到 0.05（不再被召回）。"""
    keyword = text[len("/忘记"):].strip()
    kind, k1, k2 = ctx.scene
    scope, key = (f"group:{k1}", k2) if kind == "group" else (f"c2c:{k1}", "")
    removed = 0
    for r in _db.memory_rows(scope, key):
        if keyword and keyword not in r["fact"]:
            continue
        _db.memory_set_confidence(scope, key, r["fact"], 0.05)
        removed += 1
    return f"已忘记 {removed} 条相关记忆。" if removed else "没有找到相关记忆。"


def cmd_publicize(text, ctx):
    """用户明确允许公开：标记 audience=public，可跨场景提起。"""
    keyword = text[len("/公开"):].strip()
    kind, k1, k2 = ctx.scene
    scope, key = (f"group:{k1}", k2) if kind == "group" else (f"c2c:{k1}", "")
    done = 0
    for r in _db.memory_rows(scope, key):
        if keyword and keyword not in r["fact"]:
            continue
        memory.publicize(scope, key, r["fact"])
        done += 1
    return f"已标记 {done} 条记忆可公开。" if done else "没有找到相关记忆。"


async def cmd_character(text, ctx):
    """/设定 人物名：自动生成人物设定/经历档案并存入记忆，同时写入
    docs/characters/<名>.md 供人工审阅/编辑（改完可后台运行 tools.py character-sync 同步回）。"""
    parts = (text or "").strip().split(None, 1)
    name = parts[1].strip() if len(parts) > 1 else ""
    if not name:
        return "用法：/设定 人物名（例如：/设定 千石由乃）"
    info = await asyncio.to_thread(memory.character_build, name)
    if info.get("error"):
        return f"《{name}》档案生成失败：{info['error']}"
    try:
        path = await asyncio.to_thread(memory.character_write_md, name)
        md_note = f" · 档案已写入 {path.name}"
    except Exception as e:
        _stats_err(e)
        md_note = ""
    return (
        f"已收录《{name}》：{info['added']} 条设定/经历入库（可信度 70%，说错了可以纠正我）"
        f"{md_note}（改完 md 后运行 tools.py character-sync {name} 同步回）"
    )


def _print_dispute_details(info):
    """打印纠错明细：具体哪条记忆、可信度从多少降到多少。"""
    for d in info.get("dispute_details", []):
        kind = {
            "update": "核查后更新",
            "conflict": "待核查降权",
            "keep": "核查后保留",
        }.get(d.get("kind"), d.get("kind") or "纠错")
        print(f"[记忆] 纠错 · {kind}：「{d['fact'][:40]}」{d['confidence']:.2f} → {d['new_confidence']:.2f}")


def cmd_goal_add(text, ctx):
    """添加目标：/目标 内容（可带 优先级 数字）"""
    rest = text[len("/目标"):].strip()
    m = re.search(r"优先级\s*([1-5])", rest)
    priority = int(m.group(1)) if m else 3
    title = re.sub(r"优先级\s*[1-5]", "", rest).strip()
    if not title:
        return "用法：/目标 目标内容（例如：/目标 三个月内完成项目 优先级 2）"
    kind, k1, k2 = ctx.scene
    scope = f"c2c:{k1}" if kind != "group" else f"group:{k1}"
    return memory.goal_add(scope, title, priority=priority)


def cmd_goal_list(text, ctx):
    kind, k1, k2 = ctx.scene
    scope = f"c2c:{k1}" if kind != "group" else f"group:{k1}"
    goals = memory.goal_list(scope)
    if not goals:
        return "还没有记录目标，用 /目标 内容 添加。"
    lines = [
        f"{i + 1}. {g['title']}（{'进行中' if g['status'] == 'active' else '已完成'}·优先级{g['priority']}）"
        for i, g in enumerate(goals)
    ]
    return "你的目标：\n" + "\n".join(lines)


def cmd_goal_done(text, ctx):
    title = text[len("/目标完成"):].strip()
    if not title:
        return "用法：/目标完成 目标内容"
    kind, k1, k2 = ctx.scene
    scope = f"c2c:{k1}" if kind != "group" else f"group:{k1}"
    return memory.goal_update(scope, title, status="done")


def cmd_my_style(text, ctx):
    """查看用户表达画像（v6 建议 §5）：/我的风格"""
    kind, k1, k2 = ctx.scene
    scope = f"c2c:{k1}" if kind != "group" else f"group:{k1}"
    desc = memory.expression_describe(scope)
    if not desc:
        return "还没有足够数据判断你的表达风格，多聊几句再来吧。"
    p = memory.expression_profile(scope)
    return (
        f"{desc}\n"
        f"（网络用语 {float(p.get('slang_frequency', 0)):.0%} · "
        f"反讽 {float(p.get('irony_usage', 0)):.0%} · "
        f"表情 {float(p.get('emoji_usage', 0)):.0%} · "
        f"正式度 {float(p.get('formality_level', 0.5)):.0%}）"
    )


_DECISION_RE = re.compile(
    r"要不要|该不该|怎么选|给个建议|帮我决定|纠结|犹豫|选哪个|值不值得|帮我想想|拿不定主意"
)


async def decision_try(ctx, text):
    """决策顾问（v6）：触发词或进行中的咨询 → 一次一问的顾问流程。
    防劫持（v25）：顾问会话进行中，若新消息明显切走话题（词元不重叠且不是短回答），
    结束咨询交回正常聊天，避免把"我养了什么宠物"当成决策回答。"""
    kind, k1, k2 = ctx.scene
    scope = f"c2c:{k1}" if kind != "group" else f"group:{k1}"
    if not _DECISION_RE.search(text or "") and not memory.consult_active(scope):
        return None
    if not _DECISION_RE.search(text or "") and not memory.consult_related(scope, text):
        memory.consult_abort(scope)  # 话题已切走：结束咨询，交回正常聊天
        return None
    return await asyncio.to_thread(memory.consult_turn, scope, text)


COMMANDS = {
    "/我的记忆": cmd_my_memory,
    "/群记忆": cmd_group_memory,
    "/昵称": cmd_nickname,
    "/绑定": cmd_bind,
    "/解绑": cmd_unbind,
    "/忘记": cmd_forget,
    "/公开": cmd_publicize,
    "/设定": cmd_character,
    "/目标列表": cmd_goal_list,
    "/目标完成": cmd_goal_done,
    "/目标": cmd_goal_add,
    "/我的风格": cmd_my_style,
}

_PRAISE_RE = re.compile(r"谢谢|感谢|太棒了|厉害|爱了|辛苦了|牛啊|靠谱")
_SHARE_RE = re.compile(r"我.{0,8}(经历了|最近|今天|昨天|做了|去了|开始|完成|学会|参加了|搞了)")


def chat_context(ctx, text=None):
    """场景与可见性由 Agent 统一处理（audience 过滤 + 场景定义），这里不再注入绑定提示。"""
    return ""


async def after_chat(ctx, text, reply):
    """每次 AI 回复后由 Memory Controller 自动提取；群聊每 8 条顺带更新群整体记忆。
    信息增益触发（v3.1 §1）：高信息消息立即提取，低信息消息缓存合并，替代固定 10 分钟硬节流。"""
    if len(text) < 2:
        return
    kind, k1, k2 = ctx.scene
    if kind == "group":
        # 群整体记忆：每条消息都计数，每 8 条提取一次（不依赖成员节流）
        gdata = load_group_memory(k1)
        gdata["message_count"] = gdata.get("message_count", 0) + 1
        gdata["updated_at"] = datetime.now().isoformat(timespec="seconds")
        if gdata["message_count"] % 8 == 0 and time.time() - _last_group_extract.get(k1, 0) >= 3600:
            _last_group_extract[k1] = time.time()
            gfacts = await asyncio.to_thread(
                memory.extract_facts,
                f"（最近一次群聊片段）用户：{text[:300]}",
            )
            if gfacts:
                info = await asyncio.to_thread(
                    memory.ingest, f"group_all:{k1}", "", text, reply, gfacts
                )
                print(f"[记忆] group {_safe_id(k1)} 新增 {info['facts']} 条，事件 {info['events']}")
                if info.get("disputed"):
                    print(f"[记忆] 纠错：下调 {info['disputed']} 条旧记忆可信度")
                    _print_dispute_details(info)
                gdata = load_group_memory(k1)
        save_group_memory(k1, gdata)

    # 成员/用户记忆：信息增益触发（低信息消息进缓存，攒够或超时再合并提取）
    key = f"{kind}:{k1}:{k2}"
    now = time.time()
    is_correction = bool(memory.analyze(text).get("correction"))
    scope, sk = (f"group:{k1}", k2) if kind == "group" else (f"c2c:{k1}", "")
    # 世界状态解析（v32）：LLM 提议 → 引擎裁决；异步执行，不阻塞回复
    try:
        from memory import living as living_mod
        wd = await asyncio.to_thread(living_mod.propose_world_delta, scope, text)
        if wd and wd.get("rejected"):
            print(f"[世界] 拒绝 {wd['rejected']} 条：{wd.get('reasons')}")
    except Exception as e:
        print(f"世界状态解析失败：{e}")
    # 约定迟到检测（v23）：记录用户最近发言时间
    _db.kv_set("memory", f"lastmsg:{scope}", datetime.now().isoformat(timespec="seconds"))
    # 搜索话题感知（v2.2）：记录最近一条用户消息文本，供搜索"话题转移暂停"判断
    try:
        _db.kv_set(
            "memory", f"last_user_msg:{scope}",
            {"ts": datetime.now().isoformat(timespec="seconds"), "text": str(text or "")[:200]},
        )
    except Exception as e:
        _stats_err(e)
        pass
    # 关系引擎（v3.1 §4）：行为证据驱动（chat/share/praise/dispute）
    event = "chat"
    try:
        event = (
            "dispute" if is_correction
            else ("share" if _SHARE_RE.search(text or "")
                  else ("praise" if _PRAISE_RE.search(text or "") else "chat"))
        )
        if event == "praise":
            _db.feedback_add(scope, sk, "praise", weight=0.3, detail=(text or "")[:100])
        memory.relationship_update(
            scope,
            subject=k2 or k1,
            event=event,
            detail=(text or "")[:60],
        )
    except Exception as e:
        _stats_err(e)
        pass
    # 程序记忆学习（System 1）：praise → 这次回复有效；纠正 → 无效
    try:
        from memory import procedures
        if event == "praise":
            procedures.learn(scope, text, reply, 1.0)
        elif is_correction:
            procedures.learn(scope, text, reply, 0.0)
    except Exception as e:
        _stats_err(e)
        pass
    # Memory Trace（v10）：关系证据轨迹
    try:
        memory.trace_record(
            scope, speaker="user", raw_content=text,
            action="relationship", modules=["relationship"],
            reasoning=f"行为证据：{event} → 关系状态更新",
            confidence=0.6, hint="relationship",
        )
    except Exception as e:
        _stats_err(e)
        pass
    # 语言语义解释层（v7）：更新用户表达画像
    try:
        memory.expression_update(scope, text)
    except Exception as e:
        _stats_err(e)
        pass
    gain = memory.message_gain(text, scope, sk)
    igt_threshold = float(memory.trace_adjustments().get("igt_threshold", IGT_THRESHOLD))
    if not is_correction and gain["score"] < igt_threshold:
        buf = _sess_buffer.setdefault(
            key, {"texts": [], "reply": reply or "", "first": now}
        )
        buf["texts"].append(text)
        buf["reply"] = reply or buf["reply"]
        if len(buf["texts"]) < IGT_MAX_BUFFER and now - buf["first"] < IGT_FLUSH_SEC:
            return
        merged = "；".join(buf["texts"])
        _sess_buffer.pop(key, None)
        text, reply = merged, buf["reply"]
    else:
        _sess_buffer.pop(key, None)
    info = await asyncio.to_thread(memory.ingest, scope, sk, text, reply)
    # 主动自我编辑（MEMGPT 主动记忆，方案 B）：后置小调用，默认关（memory.core.active_edit.enabled）
    try:
        await asyncio.to_thread(memory.active_edit, scope, sk, text, reply)
    except Exception as e:
        _stats_err(e)
    if info["facts"]:
        print(f"[记忆] {kind} {_safe_id(k2 or k1)} 新增 {info['facts']} 条，事件 {info['events']}")
    if info.get("disputed"):
        print(f"[记忆] 纠错：下调 {info['disputed']} 条旧记忆可信度")
        _print_dispute_details(info)
    elif is_correction:
        print("[记忆] 收到纠错信号，但未命中相关旧记忆")
    # 对话质量评分（v33 convreview）：记录本轮（用户+AI），供人工评分北极星
    try:
        import memory.convreview as _cr
        _cr.record(scope, text, reply, conversation_id=f"{kind}:{k1}:{k2}")
    except Exception as e:
        _stats_err(e)



def _stats_err(e):
    """裸 except 审计（v2.2）：错误计数 + 日志，供消融/排查。"""
    try:
        import memory.stats as _st
        _st.bump_err("memory", e)
    except Exception:
        pass
