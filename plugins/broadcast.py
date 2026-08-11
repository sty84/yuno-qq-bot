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
    return [outbox_loop(make_ctx), random_event_loop(make_ctx), appointment_loop(make_ctx),
            sharing_loop(make_ctx), space_loop(make_ctx), inspection_loop(make_ctx)]


async def outbox_loop(make_ctx):
    """把 MCP notify.send 写入的待发消息推送到 QQ。"""
    while True:
        _shared.reload_if_changed()
        try:
            import memory.stats as _st
            _st.bump("tick:outbox_loop")
        except Exception as e:
            _stats_err(e)
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
                    _stats_err(e)
        await asyncio.sleep(30)


async def random_event_loop(make_ctx):
    """按配置间隔给目标群发一条人设化随机动态。"""
    while True:
        _shared.reload_if_changed()
        try:
            import memory.stats as _st
            _st.bump("tick:random_loop")
        except Exception as e:
            _stats_err(e)
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
                f"你现在是{_persona_name()}，当前心情「{_shared.state.get('mood', '慵懒')}」。"
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
            _stats_err(e)


async def appointment_loop(make_ctx):
    """约定迟到检查（v23）：约定时间 + 宽限期已过、用户没出现 → 主动催（带情绪，最多 2 次）。"""
    while True:
        _shared.reload_if_changed()
        try:
            import memory.stats as _st
            _st.bump("tick:appointment_loop")
        except Exception as e:
            _stats_err(e)
        try:
            from memory import appointment
            sent = await asyncio.to_thread(appointment.check_and_poke)
            for a in sent:
                print(f"[约定] 已催 {a.get('scope')}（第 {a.get('poked')} 次）：{a.get('text', '')[:40]}")
        except Exception as e:
            print(f"约定检查失败：{e}")
            _stats_err(e)
        cfg = (_shared.CONFIG.get("memory", {}).get("core", {}) or {}).get("appointment", {}) or {}
        await asyncio.sleep(float(cfg.get("check_interval_s", 60)))


async def sharing_loop(make_ctx):
    """分享欲驱动（v31）：定期检查，达到阈值且符合反骚扰条件 → 主动给用户发消息。"""
    while True:
        _shared.reload_if_changed()
        try:
            import memory.stats as _st
            _st.bump("tick:sharing_loop")
        except Exception as e:
            _stats_err(e)
        try:
            from memory import sharing
            sent = await asyncio.to_thread(sharing.drive_all)
            for s in sent:
                print(f"[分享] {s['scope']} 主动发消息（{s['reason']}）：{s['msg'][:40]}")
        except Exception as e:
            print(f"分享驱动失败：{e}")
            _stats_err(e)
        cfg = (_shared.CONFIG.get("memory", {}).get("core", {}) or {}).get("sharing", {}) or {}
        await asyncio.sleep(float(cfg.get("check_interval_min", 15)) * 60)


async def inspection_loop(make_ctx):
    """跨房间查看汇报（v31.4）：'我去看看' → 延迟一条自然汇报。"""
    while True:
        _shared.reload_if_changed()
        try:
            import memory.stats as _st
            _st.bump("tick:inspection_loop")
        except Exception as e:
            _stats_err(e)
        try:
            from memory import living
            from memory import sleep as sleep_mod
            due = living.due_inspections()
            if due:
                ctx = make_ctx()
                for item in due:
                    if ctx.api is None:
                        break
                    if sleep_mod.sleep_mode() == "deep":
                        continue
                    if item.get("kind") == "search":
                        # 先清当前 pending，再推进：search_progress 会写入下一次查看，
                        # 顺序反了会把下一次查看误删，导致搜索停摆（修复）
                        living.take_inspection(item["scope"])
                        prog = living.search_progress(item["scope"])
                        prompt = prog.get("prompt", "") if prog else ""
                        quiet = bool(prog.get("quiet")) if prog else False
                        paused = bool(prog.get("paused")) if prog else False
                    else:
                        prompt = living.inspection_prompt(item)
                        quiet = paused = False
                    if prompt:
                        msg = await asyncio.to_thread(
                            _shared.ask_deepseek,
                            prompt,
                            system=_shared.BASE_SYSTEM_PROMPT,
                        )
                        await _shared.send_message(ctx.api, item["target_type"], item["target"], msg)
                    elif quiet:
                        print(f"[检查] {item['scope']} 静默推进（{item['container']}）")
                    elif paused:
                        print(f"[检查] {item['scope']} 话题转移，搜索暂停")
                    if item.get("kind") != "search":
                        living.take_inspection(item["scope"])
                    print(f"[检查] {item['scope']} {item['container']} 已汇报")
        except Exception as e:
            print(f"inspection report failed: {e}")
            _stats_err(e)
        await asyncio.sleep(15)


async def space_loop(make_ctx):
    """空间推进（v31）：定期调 position，让人物/位置在没人问时也随时间走。"""
    while True:
        _shared.reload_if_changed()
        try:
            import memory.stats as _st
            _st.bump("tick:space_loop")
        except Exception as e:
            _stats_err(e)
        try:
            from memory import space
            space.position()  # 懒演化推进：自动出发/到达，事件自然生成
            try:
                space.room_position()  # 家内房间移动同样懒推进（P0 优化）
            except Exception as e:
                print(f"房间移动推进失败：{e}")
                _stats_err(e)
            try:
                from memory import sensors
                sensors.tick()  # 家庭设备日常演化
            except Exception as e:
                print(f"sensor tick failed: {e}")
                _stats_err(e)
        except Exception as e:
            print(f"空间推进失败：{e}")
            _stats_err(e)
        await asyncio.sleep(300)


def _stats_err(e):
    """裸 except 审计（v2.2）：错误计数 + 日志，供消融/排查。"""
    try:
        import memory.stats as _st
        _st.bump_err("broadcast", e)
    except Exception:
        pass


def _persona_name() -> str:
    try:
        from agent import persona
        return persona.persona_name()
    except Exception:
        return "YUNO"
