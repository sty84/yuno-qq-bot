"""QQ 机器人核心：连接、消息路由、插件调度。

非核心功能全部放在 plugins/ 目录，每个 .py（非 _ 开头）自动加载。
插件协议（可选成员）：
  NAME      —— 插件名
  HELP      —— 在 /帮助 中展示的说明
  COMMANDS  —— {触发词: 处理函数(text, ctx)}，处理函数可返回 str/None 或协程
  chat_context(ctx)     —— 为聊天注入额外上下文，返回 str
  game_try(ctx, text)   —— 非指令消息先交给游戏类插件，返回 str/None
  after_chat(ctx, text, reply) —— AI 回复后的异步钩子（自动记忆等）
  loops(make_ctx)       —— 返回后台协程列表，make_ctx() 可获取带 api 的上下文
"""

import asyncio
import importlib
import os
import pathlib
import re
import time

import botpy
from botpy.message import C2CMessage, GroupMessage

from plugins import _shared

PLUGIN_DIR = pathlib.Path(__file__).parent / "plugins"


def load_plugins():
    mods = []
    for f in sorted(PLUGIN_DIR.glob("*.py")):
        if f.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"plugins.{f.stem}")
            mods.append(mod)
            print(f"已加载插件：{getattr(mod, 'NAME', f.stem)}")
        except Exception as e:
            print(f"插件加载失败 {f.name}：{e}")
    return mods


PLUGINS = load_plugins()

APPID = os.getenv("APPID", "")
SECRET = os.getenv("SECRET", "")

_history = {}
_last_call = {}
MAX_MSG_LEN = 1000


def strip_mention(content: str) -> str:
    content = re.sub(r"<@![^>]*>", "", content)
    content = re.sub(r"<@\d+>", "", content)
    return content.strip()


def push_history(ckey, role, content):
    history = _history.setdefault(ckey, [])
    history.append({"role": role, "content": content})
    _history[ckey] = history[-12:]


def split_text(text, limit=MAX_MSG_LEN):
    """把长回复按换行优先拆成 QQ 可发送的多个片段。"""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunk = text[:cut].strip()
        if chunk:
            chunks.append(chunk)
        text = text[cut:].lstrip("\n")
    if text.strip():
        chunks.append(text.strip())
    return chunks


def build_help() -> str:
    lines = [
        "📋 指令菜单",
        "· /聊天 —— 和我聊天",
        "· /帮助 —— 查看指令",
        "· /我的ID —— 查看自己的 ID（配管理员用）",
    ]
    for mod in PLUGINS:
        name = getattr(mod, "NAME", mod.__name__)
        help_text = getattr(mod, "HELP", "")
        lines.append(f"\n【{name}】")
        parts = [p.strip() for p in re.split(r"[｜|]", help_text) if p.strip()]
        for p in parts:
            lines.append("· " + p)
    lines.append("\n在群里 @ 我就能直接聊啦。")
    return "\n".join(lines)


class Ctx:
    """传给插件处理函数的上下文。"""

    def __init__(self, message=None, bot=None):
        self.message = message
        self.bot = bot
        self.api = bot.api if bot else None
        self.config = _shared.CONFIG
        self.save_config = _shared.save_config
        self.data_dir = _shared.DATA_DIR
        self.state = _shared.state
        self.set_mood = _shared.set_mood
        self.ask = _shared.ask_deepseek
        if message is not None:
            self.is_admin = _shared.is_admin(message)
            self.sender_ids = _shared.get_sender_ids(message)
            self.user_key = _shared.user_key_of(message)
            self.chat_key = _shared.chat_key_of(message)
            self.scene = _shared.scene_memory_keys(message)


async def run_plugin_commands(text, ctx):
    """按触发词长度从长到短匹配插件指令，第一个返回非空结果的生效。"""
    cmd = text.strip().lower()
    for mod in PLUGINS:
        cmds = getattr(mod, "COMMANDS", {}) or {}
        for trigger, handler in sorted(cmds.items(), key=lambda kv: -len(kv[0])):
            if cmd.startswith(trigger.lower()):
                try:
                    result = handler(text, ctx)
                    if asyncio.iscoroutine(result):
                        result = await result
                except Exception as e:
                    print(f"插件指令处理失败（{getattr(mod, 'NAME', mod.__name__)}/{trigger}）：{e}")
                    continue
                if result:
                    return result
    return None


class QQBot(botpy.Client):
    async def on_ready(self):
        print("机器人就绪，启动插件后台任务")
        for mod in PLUGINS:
            loops = getattr(mod, "loops", None)
            if not loops:
                continue
            try:
                for coro in loops(lambda: Ctx(bot=self)):
                    asyncio.create_task(coro)
            except Exception as e:
                print(f"插件 {getattr(mod, 'NAME', mod.__name__)} 后台任务启动失败：{e}")

    async def _handle(self, message) -> str | None:
        text = strip_mention(message.content)
        if not text:
            return None
        member, user = _shared.get_sender_ids(message)
        if member or user:
            print(f"[ID] member_openid={member} user_openid={user}")
        ctx = Ctx(message=message, bot=self)
        cmd = text.strip().lower()

        # 核心指令
        if cmd in ("/help", "/帮助", "帮助") or cmd.startswith("/帮助"):
            return build_help()
        if cmd in ("/我的id", "我的id"):
            member, user = _shared.get_sender_ids(message)
            return f"member_openid: {member or '无'}\nuser_openid: {user or '无'}"

        # 插件指令
        reply = await run_plugin_commands(text, ctx)
        if reply:
            return reply

        # 非指令消息先交给游戏类插件
        for mod in PLUGINS:
            game = getattr(mod, "game_try", None)
            if game:
                try:
                    r = game(ctx, text)
                except Exception as e:
                    print(f"游戏处理失败：{e}")
                    continue
                if r:
                    return r

        # 普通聊天：插件注入上下文 + 最近对话
        key = ctx.chat_key
        now = time.time()
        if now - _last_call.get(key, 0) < 3:
            return None
        _last_call[key] = now
        extra_parts = []
        for mod in PLUGINS:
            cc = getattr(mod, "chat_context", None)
            if cc:
                try:
                    s = cc(ctx)
                    if s:
                        extra_parts.append(s)
                except Exception as e:
                    print(f"上下文注入失败：{e}")
        extra = "\n\n".join(extra_parts)
        history = _history.get(key, [])[-6:]
        try:
            reply = await asyncio.to_thread(
                _shared.ask_deepseek, text, extra_context=extra, history=history
            )
            push_history(key, "user", text)
            push_history(key, "assistant", reply)
            for mod in PLUGINS:
                ac = getattr(mod, "after_chat", None)
                if ac:
                    try:
                        asyncio.create_task(ac(ctx, text, reply))
                    except Exception as e:
                        print(f"after_chat 失败：{e}")
            return reply
        except Exception:
            return "（AI 服务暂时不可用，请稍后再试～）"

    async def on_group_at_message_create(self, message: GroupMessage):
        group = getattr(message, "group_openid", None)
        if group:
            _shared.group_list_add(group)
        reply = await self._handle(message)
        if reply:
            await self._reply_in_chunks(message, reply)

    async def on_c2c_message_create(self, message: C2CMessage):
        reply = await self._handle(message)
        if reply:
            await self._reply_in_chunks(message, reply)

    async def _reply_in_chunks(self, message, reply):
        chunks = split_text(reply)
        for i, chunk in enumerate(chunks):
            try:
                await message.reply(content=chunk)
            except Exception as e:
                print(f"回复失败：{e}")
            if i < len(chunks) - 1:
                await asyncio.sleep(0.6)


if __name__ == "__main__":
    intents = botpy.Intents(public_messages=True)
    bot = QQBot(intents=intents, bot_log=None)
    bot.run(appid=APPID, secret=SECRET)
