"""播报插件：后台随机动态 + 消费通知队列（App/MCP 触发的 QQ 播报）。

手动播报指令已移除（企划 2.0：播报统一由管理端 / MCP notify.send 触发）。
"""

import asyncio
import random

from plugins import _db
from plugins import _shared

NAME = "播报"
HELP = "后台播报：随机动态 + 管理端通知推送（无需指令）"


def _target_group():
    return str(_shared.CONFIG.get("random_events", {}).get("group_openid", "") or "")


def loops(make_ctx):
    return [outbox_loop(make_ctx), random_event_loop(make_ctx)]


async def outbox_loop(make_ctx):
    """把 MCP notify.send 写入的待发消息推送到 QQ。"""
    while True:
        _shared.reload_if_changed()
        ctx = make_ctx()
        if ctx.api is not None:
            for item in _db.notif_pending():
                try:
                    await _shared.send_message(
                        ctx.api, item["target_type"], item["target"], item["content"]
                    )
                    _db.notif_mark_sent(item["id"])
                except Exception as e:
                    _db.notif_mark_failed(item["id"])
                    print(f"播报发送失败（第 {_db.notif_failed_retries(item['id'])} 次）：{e}")
        await asyncio.sleep(30)


async def random_event_loop(make_ctx):
    """按配置间隔给目标群发一条人设化随机动态。"""
    while True:
        _shared.reload_if_changed()
        cfg = _shared.CONFIG.get("random_events", {})
        if not cfg.get("enabled"):
            await asyncio.sleep(300)
            continue
        target = _target_group()
        min_m = max(1, int(cfg.get("min_interval_min", 60)))
        max_m = max(min_m, int(cfg.get("max_interval_min", 240)))
        await asyncio.sleep(random.uniform(min_m, max_m) * 60)
        ctx = make_ctx()
        if not target or ctx.api is None:
            continue
        try:
            prompt = (
                f"你现在是千石由乃，当前心情「{_shared.state.get('mood', '慵懒')}」。"
                "请以她的口吻给群里的大家发一条简短的生活动态（60字以内），"
                "自然、不刻意、像日常发言。"
            )
            msg = await asyncio.to_thread(
                _shared.ask_deepseek, prompt, system=_shared.BASE_SYSTEM_PROMPT
            )
            await _shared.send_message(ctx.api, "group", target, msg)
            if random.random() < 0.4:
                _shared.set_mood(random.choice(_shared.MOODS))
        except Exception as e:
            print(f"随机动态失败：{e}")
