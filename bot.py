"""YUNO 2.0 · QQ 聊天前台核心。

只负责：连接 QQ、消息路由、插件调度、分段回复。
管理/查询能力已按企划 2.0 迁出 QQ（服务注册表与 MCP 工具在 plugins/，
管理 App 另行部署），QQ 端只保留：聊天、记忆、游戏、播报。

插件协议（可选成员）：
  NAME / HELP / COMMANDS / chat_context / game_try / after_chat / loops
"""

import asyncio
import importlib
import os
import pathlib
import random
import re
import signal
import sys
import time

import botpy
from botpy.message import C2CMessage, GroupMessage

from plugins import _shared
import agent

PLUGIN_DIR = pathlib.Path(__file__).parent / "plugins"
MAX_MSG_LEN = 1000

_history = {}    # chat_key -> [{role, content}]
_chat_busy = set()   # chat_key -> 正在处理中（串行化，避免并发打爆 LLM）
_chat_pending = {}   # chat_key -> (text, ctx)，处理期间新到的消息记下，完成后补处理
_logged_sender_ids = set()  # 已打过日志的 sender，避免重复刷屏

BRIDGE_PHRASES = [
    "来啦～这个问题让我想想，稍等一下哦",
    "收到～我组织一下语言，马上回你",
    "嗯，有点意思，让我琢磨琢磨",
    "好嘞，给我几秒钟想想哈",
]
_bridge_last = {}  # chat_key -> 上次衔接时间（防止慢响应时刷屏）


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
PLUGIN_COMMANDS = [
    (mod, sorted((getattr(mod, "COMMANDS", {}) or {}).items(), key=lambda kv: -len(kv[0])))
    for mod in PLUGINS
]

APPID = os.getenv("APPID", "")
SECRET = os.getenv("SECRET", "")


def strip_mention(content: str) -> str:
    return re.sub(r"<@!?\d*>|<@\d+>", "", content or "").strip()


def log_sender_ids(message):
    """首次收到消息时把 openid 打进日志，供管理员配置 ADMIN_OPENIDS（替代 /我的id）。"""
    member, user = _shared.get_sender_ids(message)
    key = f"{member}|{user}"
    if not key.strip("|") or key in _logged_sender_ids:
        return
    _logged_sender_ids.add(key)
    print(
        f"[引导] 收到新消息：member_openid={member or '无'} user_openid={user or '无'}；"
        "如需配置管理员，把 user_openid 填入 .env 的 ADMIN_OPENIDS 后重启机器人。",
        flush=True,
    )


def push_history(ckey, role, content):
    _history.setdefault(ckey, []).append({"role": role, "content": content})
    _history[ckey] = _history[ckey][-12:]


def split_text(text, limit=MAX_MSG_LEN):
    """按换行优先把长回复拆成 QQ 可发送的多个片段。"""
    text = (text or "").strip()
    if not text:
        return []
    chunks, rest = [], text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        cut = cut if cut > 0 else limit
        chunks.append(rest[:cut].strip())
        rest = rest[cut:].lstrip("\n")
    if rest.strip():
        chunks.append(rest.strip())
    return chunks


def build_help() -> str:
    lines = ["📋 指令菜单", "· /帮助 —— 查看指令"]
    for mod in PLUGINS:
        lines.append(f"\n【{getattr(mod, 'NAME', mod.__name__)}】")
        for p in re.split(r"[｜|]", getattr(mod, "HELP", "")):
            p = p.strip()
            if p:
                lines.append("· " + p)
    lines.append("\n在群里 @ 我就能直接聊啦。")
    return "\n".join(lines)


class Ctx:
    """传给插件处理函数的上下文。"""

    def __init__(self, message=None, bot=None):
        self.message = message
        self.bot = bot
        self.api = bot.api if bot else None
        self.data_dir = _shared.DATA_DIR
        if message is not None:
            self.is_admin = _shared.is_admin(message)
            self.sender_ids = _shared.get_sender_ids(message)
            self.user_key = _shared.user_key_of(message)
            self.chat_key = _shared.chat_key_of(message)
            self.scene = _shared.scene_memory_keys(message)

    @property
    def config(self):
        return _shared.CONFIG


async def run_plugin_commands(text, ctx):
    """按触发词长度从长到短匹配插件指令，第一个返回非空结果的生效。"""
    cmd = text.strip().lower()
    for mod, commands in PLUGIN_COMMANDS:
        for trigger, handler in commands:
            if not cmd.startswith(trigger.lower()):
                continue
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
        asyncio.create_task(self._warmup())

    async def _warmup(self):
        """预热记忆向量模型，避免第一条消息被模型加载拖慢。"""
        try:
            import memory
            if memory.embed_enabled():
                memory.embed(["预热"])
                print("记忆向量模型预热完成")
        except Exception as e:
            print(f"记忆向量模型预热失败：{e}")

    async def _send_bridge(self, ctx):
        """慢响应时发一条自然衔接（有冷却，防止刷屏）。"""
        cfg = _shared.CONFIG.get("chat_bridge", {}) or {}
        min_interval = float(cfg.get("min_interval_s", 90))
        now = time.time()
        if now - _bridge_last.get(ctx.chat_key, 0) < min_interval:
            return
        _bridge_last[ctx.chat_key] = now
        kind, k1, _k2 = ctx.scene
        target_type, target = ("group", k1) if kind == "group" else ("c2c", k1)
        try:
            await asyncio.to_thread(
                _shared.send_message, self.api, target_type, target,
                random.choice(BRIDGE_PHRASES),
            )
        except Exception as e:
            print(f"衔接消息发送失败：{e}")

    async def _ask(self, text, ctx, scopes, extra):
        """调 Agent；响应超过阈值时先发衔接语，再等完整答案。"""
        cfg = _shared.CONFIG.get("chat_bridge", {}) or {}
        task = asyncio.create_task(
            asyncio.to_thread(
                agent.ask,
                text,
                history=_history.get(ctx.chat_key, [])[-6:],
                extra_context=extra,
                scopes=scopes,
                learn=False,
            )
        )
        if not cfg.get("enabled", True):
            return await task
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=float(cfg.get("timeout_s", 2.5)))
        except asyncio.TimeoutError:
            await self._send_bridge(ctx)
            return await task

    async def _handle(self, message) -> str | None:
        text = strip_mention(message.content)
        if not text:
            return None
        ctx = Ctx(message=message, bot=self)
        log_sender_ids(message)
        cmd = text.strip().lower()

        # 核心指令
        if cmd in ("/help", "/帮助", "帮助") or cmd.startswith("/帮助"):
            return build_help()

        # 插件指令
        reply = await run_plugin_commands(text, ctx)
        if reply:
            return reply

        # 非指令消息先交给游戏类插件
        for mod in PLUGINS:
            game = getattr(mod, "game_try", None)
            if not game:
                continue
            try:
                r = game(ctx, text)
                if asyncio.iscoroutine(r):
                    r = await r
                if r:
                    return r
            except Exception as e:
                print(f"游戏处理失败：{e}")

        # 决策顾问（v6）：一次一问，结合对用户的了解，考虑现实约束
        for mod in PLUGINS:
            dt = getattr(mod, "decision_try", None)
            if not dt:
                continue
            try:
                r = dt(ctx, text)
                if asyncio.iscoroutine(r):
                    r = await r
                if r:
                    return r
            except Exception as e:
                print(f"决策咨询处理失败：{e}")

        return await self._chat(text, ctx)

    async def _chat(self, text, ctx):
        key = ctx.chat_key
        if key in _chat_busy:
            # 上一轮还在处理：记下最新一条，完成后自动补处理（不再静默丢弃）
            _chat_pending[key] = (text, ctx)
            return None
        _chat_busy.add(key)
        try:
            return await self._chat_once(text, ctx)
        finally:
            _chat_busy.discard(key)
            pending = _chat_pending.pop(key, None)
            if pending:
                asyncio.create_task(self._chat(pending[0], pending[1]))

    async def _chat_once(self, text, ctx):
        key = ctx.chat_key
        extra_parts = []
        for mod in PLUGINS:
            cc = getattr(mod, "chat_context", None)
            if not cc:
                continue
            try:
                if s := cc(ctx, text):
                    extra_parts.append(s)
            except Exception as e:
                print(f"上下文注入失败：{e}")
        extra = "\n\n".join(extra_parts)

        kind, k1, k2 = ctx.scene
        scopes = (
            [f"group:{k1}", f"group_all:{k1}"] if kind == "group" else [f"c2c:{k1}"]
        ) if kind else None
        reply = None
        try:
            reply, _meta = await self._ask(text, ctx, scopes, extra)
        except Exception as e:
            print(f"[AI] 请求失败，使用兜底回复：{e}")
            reply = "（AI 服务暂时不可用，请稍后再试～）"
        # 无论 AI 是否成功都记录上下文并尝试提取记忆，避免偶发失败导致记忆断档
        push_history(key, "user", text)
        push_history(key, "assistant", reply)
        for mod in PLUGINS:
            ac = getattr(mod, "after_chat", None)
            if ac:
                try:
                    asyncio.create_task(ac(ctx, text, reply))
                except Exception as e:
                    print(f"after_chat 启动失败：{e}")
        return reply

    async def on_group_at_message_create(self, message: GroupMessage):
        if group := getattr(message, "group_openid", None):
            _shared.group_list_add(group)
        if reply := await self._handle(message):
            await self._reply_in_chunks(message, reply)

    async def on_c2c_message_create(self, message: C2CMessage):
        if reply := await self._handle(message):
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
    def _on_stop(_signum, _frame):
        """优雅关闭：把内存统计刷进 kv，避免重启丢失（v31）。"""
        try:
            import memory
            memory.flush_caches()
        except Exception:
            pass
        print("收到退出信号，已刷新缓存。", flush=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_stop)
    signal.signal(signal.SIGINT, _on_stop)
    intents = botpy.Intents(public_messages=True)
    bot = QQBot(intents=intents, bot_log=None)
    bot.run(appid=APPID, secret=SECRET)
