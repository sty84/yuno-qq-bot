"""服务器管理插件：/容器、/写文件、/读文件、/命令、/报告（含日报与异常告警后台）"""

import asyncio
import json
import re
from datetime import datetime, timedelta

from plugins import _shared

NAME = "服务器管理"
HELP = (
    "/容器 列表｜启动/停止/重启 关键词｜添加 关键词 路径｜删除 关键词｜"
    "/写文件 文件名 内容｜/读文件 文件名｜"
    "/命令 你的要求｜/报告 现在｜测试（均管理员）"
)

# ===== 容器 =====
def handle_container(text, ctx):
    if not ctx.is_admin:
        return "这个指令只有管理员能用。"
    rest = text[len("/容器"):].strip()
    if not rest or rest == "列表":
        return _shared.list_containers()
    parts = rest.split(maxsplit=1)
    if len(parts) == 2 and parts[0] in ("启动", "停止", "重启"):
        action = {"启动": "start", "停止": "stop", "重启": "restart"}[parts[0]]
        return _shared.control_container(action, parts[1])
    if len(parts) == 2 and parts[0] == "添加":
        args = parts[1].split(maxsplit=1)
        if len(args) != 2:
            return "用法：/容器 添加 关键词 路径"
        return _shared.add_container_config(args[0], args[1])
    if len(parts) == 2 and parts[0] == "删除":
        return _shared.remove_container_config(parts[1])
    return (
        "用法：\n"
        "/容器 列表\n"
        "/容器 启动/停止/重启 关键词\n"
        "/容器 添加 关键词 路径\n"
        "/容器 删除 关键词"
    )


# ===== 文件 =====
def handle_write(text, ctx):
    if not ctx.is_admin:
        return "这个指令只有管理员能用。"
    parts = text[len("/写文件"):].strip().split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        return "用法：/写文件 文件名 内容，例如：/写文件 测试.txt 你好"
    return _shared.write_file(parts[0], parts[1].strip())


def handle_read(text, ctx):
    if not ctx.is_admin:
        return "这个指令只有管理员能用。"
    path = text[len("/读文件"):].strip()
    if not path:
        return "用法：/读文件 文件名"
    return _shared.read_file(path)


# ===== 自然语言执行 =====
TOOL_SYSTEM_PROMPT = (
    "你是机器人内部的动作调度器。用户会用自然语言提出服务器操作需求，"
    "你只能返回一个 JSON 对象，禁止输出任何其他文字、注释或 Markdown。可选动作：\n"
    "1. write_file：在允许的路径下创建或覆盖文件，参数 path（相对路径）、content（内容）。\n"
    "2. read_file：读取允许路径下的文件，参数 path（相对路径）。\n"
    "3. status：查看服务器状态，无参数。\n"
    "4. weather：查天气，参数 city（城市名）。\n"
    "5. containers：查看容器列表，无参数。\n"
    "6. containers_stop：停止 config.json 允许的容器，参数 keyword（容器名关键词）。\n"
    "7. containers_start：启动 config.json 允许的容器，参数 keyword（容器名关键词）。\n"
    "如果需求不明确，或涉及删除文件、重启服务、执行任意命令等危险操作，"
    "返回 {\"action\":\"deny\",\"reason\":\"简短原因\"}。\n"
    "示例：{\"action\":\"write_file\",\"path\":\"test.txt\",\"content\":\"你好\"}"
)


def ask_ai_for_action(request_text):
    try:
        resp = _shared.deepseek.chat.completions.create(
            model=_shared.DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": TOOL_SYSTEM_PROMPT},
                {"role": "user", "content": request_text},
            ],
            max_tokens=500,
            temperature=0.2,
        )
        raw = (resp.choices[0].message.content or "").strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.S).strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end < 0:
            return "AI 没有返回可识别的动作，请换一种说法试试。"
        data = json.loads(raw[start:end + 1])
        action = data.get("action", "")
        if action == "deny":
            return "操作被拒绝：" + str(data.get("reason", "原因未知"))
        if action == "write_file":
            return _shared.write_file(str(data.get("path", "")), str(data.get("content", "")))
        if action == "read_file":
            return _shared.read_file(str(data.get("path", "")))
        if action == "status":
            return _shared.get_server_status()
        if action == "weather":
            return _shared.get_weather(str(data.get("city", "上海")))
        if action == "containers":
            return _shared.list_containers()
        if action == "containers_stop":
            return _shared.control_container("stop", str(data.get("keyword", "")))
        if action == "containers_start":
            return _shared.control_container("start", str(data.get("keyword", "")))
        return "不支持的指令类型：" + str(action)
    except Exception as e:
        return f"执行失败：{e}"


async def handle_ai_cmd(text, ctx):
    if not ctx.is_admin:
        return "这个指令只有管理员能用。"
    req = text[len("/命令"):].strip()
    if not req:
        return "用法：/命令 你的要求，例如：/命令 在data下新建文件test.txt，内容写你好"
    return await asyncio.to_thread(ask_ai_for_action, req)


# ===== 报告 =====
def handle_report(text, ctx):
    if not ctx.is_admin:
        return "这个指令只有管理员能用。"
    rest = text[len("/报告"):].strip()
    if "测试" in rest:
        asyncio.create_task(
            asyncio.to_thread(
                _shared.send_email, "【由乃】测试邮件", "这是一封测试邮件，SMTP 配置正常。"
            )
        )
        return "正在发送测试邮件。"
    if "现在" in rest:
        asyncio.create_task(run_report())
        return "正在生成日报并发送邮件，请稍候。"
    return "用法：/报告 现在　或　/报告 测试"


COMMANDS = {
    "/容器": handle_container,
    "/写文件": handle_write,
    "/读文件": handle_read,
    "/命令": handle_ai_cmd,
    "/报告": handle_report,
}


def loops(make_ctx):
    return [daily_loop(), anomaly_loop()]


async def daily_loop():
    while True:
        cfg = _shared.CONFIG.get("report", {})
        if not cfg.get("enabled"):
            await asyncio.sleep(300)
            continue
        now = datetime.now()
        nxt = now.replace(
            hour=int(cfg.get("hour", 9)),
            minute=int(cfg.get("minute", 0)),
            second=0,
            microsecond=0,
        )
        if nxt <= now:
            nxt += timedelta(days=1)
        await asyncio.sleep(max(1, (nxt - now).total_seconds()))
        if _shared.CONFIG.get("report", {}).get("enabled"):
            await run_report()


async def anomaly_loop():
    while True:
        cfg = _shared.CONFIG.get("report", {})
        if not cfg.get("enabled") or not cfg.get("anomaly_immediate"):
            await asyncio.sleep(900)
            continue
        await asyncio.sleep(900)
        try:
            material = await asyncio.to_thread(collect_material)
            hits = _shared.detect_anomalies(material)
            if hits:
                await asyncio.to_thread(
                    _shared.send_email,
                    "【由乃告警】检测到异常",
                    "\n".join(hits) + "\n\n详情：\n" + material[:2000],
                )
        except Exception as e:
            print(f"异常巡检失败：{e}")


def collect_material():
    parts = ["===== 系统状态 =====\n" + _shared.get_server_status()]
    log = _shared.DATA_DIR / "bot.log"
    if log.exists():
        try:
            lines = log.read_text(encoding="utf-8", errors="ignore").splitlines()[-120:]
            parts.append("===== 机器人日志（末尾120行）=====\n" + "\n".join(lines))
        except Exception as e:
            parts.append(f"读取机器人日志失败：{e}")
    for entry in _shared.CONFIG.get("containers", []):
        kw = entry.get("keyword", "")
        if not kw:
            continue
        ok, out = _shared.run_ctl([kw, "logs"])
        parts.append(f"===== 容器 {kw} 日志 =====\n" + (out[:1500] if ok else out))
    return "\n\n".join(parts)


def summarize_material(material):
    prompt = (
        "你正在以千石由乃的人设写一份服务器运维日报。"
        "请用她慵懒但靠谱的语气，分点总结下面的日志材料："
        "先讲系统健康，再讲各容器状态，最后点出任何异常。"
        "控制在 300 字以内，简洁明了。\n\n材料：\n" + material[:6000]
    )
    try:
        return _shared.ask_deepseek(prompt, system=_shared.BASE_SYSTEM_PROMPT)
    except Exception as e:
        return f"总结生成失败：{e}\n\n原始材料（节选）：\n{material[:1500]}"


async def run_report():
    print("生成日报...")
    material = await asyncio.to_thread(collect_material)
    summary = await asyncio.to_thread(summarize_material, material)
    subject = f"【由乃日报】{datetime.now():%Y-%m-%d}"
    await asyncio.to_thread(_shared.send_email, subject, summary)
    cfg = _shared.CONFIG.get("report", {})
    if cfg.get("anomaly_immediate"):
        hits = _shared.detect_anomalies(material)
        if hits:
            await asyncio.to_thread(
                _shared.send_email,
                "【由乃告警】检测到异常",
                "异常摘要：\n" + "\n".join(hits) + "\n\n详情：\n" + material[:2000],
            )
