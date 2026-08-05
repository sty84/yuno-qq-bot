"""信息查询与监控插件：/状态、/天气、/余额（含低余额定时提醒）"""

import asyncio

from plugins import _shared

NAME = "信息查询"
HELP = (
    "/状态 —— 服务器负载、内存、运行时间｜"
    "/天气 城市 —— 查天气｜"
    "/余额 —— DeepSeek 余额（管理员，可 /余额 测试发测试提醒）"
)

def handle_status(text, ctx):
    return _shared.get_server_status()


def handle_weather(text, ctx):
    city = text[len("/天气"):].strip() or "上海"
    return _shared.get_weather(city)


def handle_balance(text, ctx):
    if not ctx.is_admin:
        return "这个指令只有管理员能用。"
    rest = text[len("/余额"):].strip()
    if "测试" in rest:
        asyncio.create_task(send_test_alert(ctx))
        return "已发送测试提醒，请查收。"
    return _shared.get_deepseek_balance_text()


COMMANDS = {
    "/状态": handle_status,
    "/天气": handle_weather,
    "/余额": handle_balance,
}


async def send_test_alert(ctx):
    cfg = _shared.CONFIG.get("balance_alert", {})
    target = str(cfg.get("target", "") or "")
    ttype = str(cfg.get("target_type", "group") or "group")
    if not target or ctx.api is None:
        return
    msg = "（余额提醒测试）这里是千石由乃，提醒通道正常～"
    try:
        if ttype == "c2c":
            await ctx.api.post_c2c_message(openid=target, content=msg)
        else:
            await ctx.api.post_group_message(group_openid=target, content=msg)
        print(f"余额提醒测试已发送到 {ttype} {target}")
    except Exception as e:
        print(f"余额提醒测试发送失败：{e}")


def loops(make_ctx):
    return [balance_alert_loop(make_ctx)]


async def balance_alert_loop(make_ctx):
    while True:
        cfg = _shared.CONFIG.get("balance_alert", {})
        if not cfg.get("enabled"):
            await asyncio.sleep(300)
            continue
        interval = max(1, int(cfg.get("check_interval_hours", 6))) * 3600
        threshold = float(cfg.get("threshold", 2.0))
        ttype = str(cfg.get("target_type", "group") or "group")
        target = str(cfg.get("target", "") or "")
        await check_balance(make_ctx(), threshold, ttype, target)
        await asyncio.sleep(interval)


async def check_balance(ctx, threshold, ttype, target):
    if not target or ctx.api is None:
        return
    amount = await asyncio.to_thread(_shared.get_balance_amount)
    if amount is None:
        return
    low = amount < threshold
    flagged = bool(_shared.state.get("balance_low_alerted", False))
    if low and not flagged:
        msg = await asyncio.to_thread(
            _shared.ask_deepseek,
            f"DeepSeek API 余额只剩 {amount:.2f} 元，已低于 {threshold:.2f} 元的提醒阈值。"
            "请用千石由乃的人设写一条简短提醒（100字内），催促管理员尽快充值。",
            system=_shared.BASE_SYSTEM_PROMPT,
        )
        try:
            if ttype == "c2c":
                await ctx.api.post_c2c_message(openid=target, content=msg[:500])
            else:
                await ctx.api.post_group_message(group_openid=target, content=msg[:500])
            _shared.state["balance_low_alerted"] = True
            _shared.save_state()
            print(f"余额提醒已发送到 {ttype} {target}")
        except Exception as e:
            print(f"余额提醒发送失败：{e}")
    elif not low and flagged:
        _shared.state["balance_low_alerted"] = False
        _shared.save_state()
