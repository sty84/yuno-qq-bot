"""记忆系统插件：按 ID 分文件、自动提取、场景隔离、私聊/群身份绑定"""

import asyncio
import json
import random
import re
import time
from datetime import datetime

from plugins import _db
from plugins import _shared

NAME = "记忆系统"
HELP = (
    "/记住 内容｜/我的记忆｜/群记忆｜/清除记忆｜/昵称 名字｜"
    "/绑定（群里生成码，私聊 /绑定 群ID 码）｜/解绑"
)

_binding_codes = {}
_last_extract = {}


def _safe_id(value):
    return re.sub(r"[^A-Za-z0-9_\-]", "_", value or "unknown")[:64]


def nice_fact(fact) -> str:
    """把可能残留的 {'info': '...'} 格式清洗成纯文本。"""
    s = str(fact).strip()
    m = re.match(r"^\{['\"]info['\"]\s*:\s*['\"](.*?)['\"]\s*\}$", s, re.S)
    if m:
        return m.group(1).strip()
    return s


def load_user_memory(ukey):
    return {
        "facts": _db.facts_get("c2c", str(ukey)),
        "updated_at": _db.facts_updated_at("c2c", str(ukey)),
    }


def save_user_memory(ukey, data):
    _db.facts_replace("c2c", str(ukey), data.get("facts", []), data.get("updated_at", ""))


def load_group_memory(gid):
    key = str(gid)
    return {
        "facts": _db.facts_get("group_all", key),
        "message_count": _db.kv_get("group", f"{key}:count", 0),
        "updated_at": _db.facts_updated_at("group_all", key),
    }


def save_group_memory(gid, data):
    key = str(gid)
    _db.facts_replace("group_all", key, data.get("facts", []), data.get("updated_at", ""))
    _db.kv_set("group", f"{key}:count", data.get("message_count", 0))


def load_member_memory(gid, mid):
    return {
        "facts": _db.facts_get("group", f"{gid}:{mid}"),
        "updated_at": _db.facts_updated_at("group", f"{gid}:{mid}"),
    }


def save_member_memory(gid, mid, data):
    _db.facts_replace("group", f"{gid}:{mid}", data.get("facts", []), data.get("updated_at", ""))


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


def merge_facts(existing, new, cap=30):
    seen = set(existing)
    out = list(existing)
    for fact in new:
        fact = nice_fact(fact).strip()
        if fact and fact not in seen:
            seen.add(fact)
            out.append(fact)
    return out[-cap:]


def load_scene_memory(message):
    kind, k1, k2 = _shared.scene_memory_keys(message)
    if kind == "group":
        return load_member_memory(k1, k2)
    return load_user_memory(k1)


def save_scene_memory(message, data):
    kind, k1, k2 = _shared.scene_memory_keys(message)
    if kind == "group":
        save_member_memory(k1, k2, data)
    else:
        save_user_memory(k1, data)


def add_scene_fact(message, fact):
    data = load_scene_memory(message)
    data["facts"] = merge_facts(data.get("facts", []), [fact])
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    save_scene_memory(message, data)


def clear_scene_memory(message):
    kind, k1, k2 = _shared.scene_memory_keys(message)
    if kind == "group":
        _db.facts_replace("group", f"{k1}:{k2}", [])
    else:
        _db.facts_replace("c2c", k1, [])


EXTRACT_SYSTEM_PROMPT = (
    "你是信息提取器。请从对话中提取值得长期记住的关键信息："
    "关于用户的姓名、喜好、习惯、身份、经历、约定；关于群聊的主题、成员特点、重要事件。"
    "只输出一个 JSON 字符串数组，每项是一句简短陈述（不超过25字），"
    "例如 [\"喜欢白巧克力\", \"养了一只猫\"]。禁止输出对象或键值对。"
    "没有值得记的信息就输出 []。不要输出任何其他内容。"
)


def _norm_extract_item(item) -> str:
    if isinstance(item, dict):
        for key in ("info", "fact", "content", "text", "name"):
            if item.get(key):
                return str(item[key]).strip()
        values = [str(v).strip() for v in item.values() if str(v).strip()]
        return values[0] if values else ""
    return str(item).strip()


def extract_facts(conversation):
    try:
        resp = _shared.deepseek.chat.completions.create(
            model=_shared.DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
                {"role": "user", "content": conversation},
            ],
            max_tokens=300,
            temperature=0.2,
        )
        raw = (resp.choices[0].message.content or "").strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.S).strip()
        start, end = raw.find("["), raw.rfind("]")
        if start < 0 or end < 0:
            return []
        data = json.loads(raw[start:end + 1])
        facts = []
        for x in data:
            cleaned = nice_fact(_norm_extract_item(x))
            if cleaned:
                facts.append(cleaned)
        return facts[:5]
    except Exception:
        return []


def cmd_remember(text, ctx):
    fact = text[len("/记住"):].strip()
    if not fact:
        return "用法：/记住 内容，例如：/记住 我喜欢吃白巧克力"
    add_scene_fact(ctx.message, fact)
    return "记住了～"


def cmd_my_memory(text, ctx):
    data = load_scene_memory(ctx.message)
    facts = data.get("facts", [])
    label = "你在本群的记忆" if ctx.scene[0] == "group" else "你在私聊中的记忆"
    shown = [nice_fact(f) for f in facts[-10:]]
    return label + "：\n" + ("\n".join("- " + f for f in shown) if shown else "（还没有记住什么）")


def cmd_group_memory(text, ctx):
    if ctx.scene[0] != "group":
        return "这个指令只能在群里使用。"
    facts = load_group_memory(ctx.scene[1]).get("facts", [])
    shown = [nice_fact(f) for f in facts[-10:]]
    return "本群的记忆：\n" + ("\n".join("- " + f for f in shown) if shown else "（还没有记录什么）")


def cmd_clear_memory(text, ctx):
    clear_scene_memory(ctx.message)
    return "已清除" + ("本群" if ctx.scene[0] == "group" else "私聊") + "中关于你的记忆。"


def cmd_nickname(text, ctx):
    name = text[len("/昵称"):].strip()
    if not name:
        return "用法：/昵称 名字，例如：/昵称 由乃酱"
    _shared.set_nickname(ctx.user_key, name)
    # 昵称同步到已绑定的另一场景（群 ↔ 私聊）
    kind, k1, k2 = ctx.scene
    synced = False
    if kind == "group":
        uid = find_user_for_member(k1, k2)
        if uid:
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


def cmd_bind(text, ctx):
    kind, k1, k2 = ctx.scene
    if kind == "group":
        now_t = time.time()
        for code in [c for c, v in _binding_codes.items() if v["expire"] < now_t]:
            _binding_codes.pop(code, None)
        code = f"{random.randint(100000, 999999)}"
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
            asyncio.create_task(post_group_notice(ctx, gid, k1, info["member_openid"]))
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


COMMANDS = {
    "/记住": cmd_remember,
    "/我的记忆": cmd_my_memory,
    "/群记忆": cmd_group_memory,
    "/清除记忆": cmd_clear_memory,
    "/昵称": cmd_nickname,
    "/绑定": cmd_bind,
    "/解绑": cmd_unbind,
}


def chat_context(ctx):
    """聊天时注入：当前场景记忆 + 群整体记忆 + 绑定提示。"""
    parts = []
    kind, k1, k2 = ctx.scene
    if kind == "group":
        m = load_member_memory(k1, k2)
        if m.get("facts"):
            parts.append("该用户在本群的记忆：\n" + "\n".join("- " + nice_fact(f) for f in m["facts"][-10:]))
        g = load_group_memory(k1)
        if g.get("facts"):
            parts.append("该群的长期记忆：\n" + "\n".join("- " + nice_fact(f) for f in g["facts"][-10:]))
        bound_user = find_user_for_member(k1, k2)
        if bound_user:
            parts.append(
                f"（该用户已绑定私聊身份，user_openid 前 8 位：{bound_user[:8]}，"
                "群聊记忆与私聊记忆分开保存，不要混用）"
            )
    else:
        u = load_user_memory(k1)
        if u.get("facts"):
            parts.append("该用户在私聊中的记忆：\n" + "\n".join("- " + nice_fact(f) for f in u["facts"][-10:]))
        binds = find_member_for_user(k1)
        if binds:
            parts.append(
                f"（该用户已绑定 {len(binds)} 个群的身份，私聊记忆与群聊记忆分开保存，不要混用）"
            )
    return "\n\n".join(parts)


async def after_chat(ctx, text, reply):
    """每次 AI 回复后自动提取记忆。"""
    if len(text) < 6:
        return
    kind, k1, k2 = ctx.scene
    key = f"{kind}:{k1}:{k2}"
    now = time.time()
    if now - _last_extract.get(key, 0) < 600:
        return
    _last_extract[key] = now
    try:
        facts = await asyncio.to_thread(
            extract_facts, f"用户：{text[:500]}\n机器人：{reply[:500]}"
        )
        if facts:
            if kind == "group":
                data = load_member_memory(k1, k2)
                data["facts"] = merge_facts(data.get("facts", []), facts)
                data["updated_at"] = datetime.now().isoformat(timespec="seconds")
                save_member_memory(k1, k2, data)
            else:
                data = load_user_memory(k1)
                data["facts"] = merge_facts(data.get("facts", []), facts)
                data["updated_at"] = datetime.now().isoformat(timespec="seconds")
                save_user_memory(k1, data)
            print(f"[记忆] {kind} {_safe_id(k2 or k1)} 新增 {len(facts)} 条")
    except Exception as e:
        print(f"[记忆] 提取失败：{e}")
    # 群整体记忆：每 8 条消息提取一次
    if kind == "group":
        try:
            gdata = load_group_memory(k1)
            gdata["message_count"] = gdata.get("message_count", 0) + 1
            gdata["updated_at"] = datetime.now().isoformat(timespec="seconds")
            if gdata["message_count"] % 8 == 0:
                gfacts = await asyncio.to_thread(
                    extract_facts,
                    f"（最近一次群聊片段）用户：{text[:300]}\n机器人：{reply[:300]}",
                )
                if gfacts:
                    gdata["facts"] = merge_facts(gdata.get("facts", []), gfacts)
            save_group_memory(k1, gdata)
        except Exception as e:
            print(f"[记忆] 群提取失败：{e}")
