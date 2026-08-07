"""后端能力层：服务注册表 + 健康检查 + 一键恢复 + MCP 工具。

由 tools.py mcp 子命令暴露为标准 MCP 工具；不注册任何 QQ 指令。
"""

import socket
import subprocess
import time
from datetime import datetime

from plugins import _db
from plugins import _shared
from plugins import memory
import memory as memory_core

CTL = ["sudo", "-n", "/usr/local/bin/qqbot-ctl"]
SERVICE_FIELDS = ("keyword", "path", "type", "unit", "port", "health_check", "log_path", "allow")


# ===== 服务注册表 =====
def validate_service(entry) -> list[str]:
    errs = []
    stype = entry.get("type", "docker")
    if not str(entry.get("keyword", "")).strip():
        errs.append("keyword 必填")
    if stype not in ("docker", "systemd", "process"):
        errs.append(f"type 只能是 docker/systemd/process，当前：{stype}")
    if stype == "systemd" and not entry.get("unit"):
        errs.append("type=systemd 时 unit 必填")
    if stype == "docker" and not entry.get("path"):
        errs.append("type=docker 时 path 必填")
    if entry.get("health_check") not in (None, "port", "unit", "command"):
        errs.append("health_check 只能是 port/unit/command")
    if entry.get("health_check") == "port" and not entry.get("port"):
        errs.append("health_check=port 时需配置 port")
    for op in entry.get("allow") or []:
        if op not in ("list", "status", "start", "stop", "restart", "logs"):
            errs.append(f"allow 含非法操作：{op}")
    return errs


def services() -> list[dict]:
    return _shared.CONFIG.get("services") or []


def find_service(keyword) -> dict | None:
    keyword = str(keyword or "").strip().lower()
    return next((s for s in services() if str(s.get("keyword", "")).lower() == keyword), None)


def run_ctl(args: list[str]) -> tuple[bool, str]:
    try:
        proc = subprocess.run(CTL + args, capture_output=True, text=True, timeout=300)
        out, err = (proc.stdout or "").strip(), (proc.stderr or "").strip()
        if proc.returncode == 0:
            return True, out or "操作完成。"
        return False, (out + "\n" + err).strip() or f"脚本返回错误码 {proc.returncode}"
    except subprocess.TimeoutExpired:
        return False, "操作超时。"
    except Exception as e:
        return False, f"调用白名单脚本失败：{e}"


def check_port(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except OSError:
        return False


def health_of(entry) -> tuple[bool, str]:
    """返回 (在线, 详情)。port 走本地 TCP 探测，其余走 qqbot-ctl status。"""
    if entry.get("health_check") == "port" and entry.get("port"):
        host = str(entry.get("host") or "127.0.0.1")
        ok = check_port(host, int(entry["port"]))
        return ok, f"端口 {host}:{entry['port']} " + ("可达" if ok else "不可达")
    ok, out = run_ctl([str(entry.get("keyword", "")), "status"])
    if not ok:
        return False, out
    return any(x in out for x in ("运行中", "active", "Up")), out


def services_list():
    return [{k: s.get(k) for k in SERVICE_FIELDS} for s in services()]


def services_status(keyword=None):
    entries = [find_service(keyword)] if keyword else services()
    result = []
    for s in entries:
        if not s:
            continue
        online, detail = health_of(s)
        result.append({"keyword": s.get("keyword"), "online": online, "detail": detail})
    return result


def control_service(keyword, action):
    s = find_service(keyword)
    if not s:
        return f"服务不在注册表：{keyword}"
    allow = s.get("allow") or _shared.CONFIG.get("default_allow") or []
    if action not in allow:
        return f"config.json 未允许该操作：{keyword}/{action}"
    ok, out = run_ctl([keyword, action])
    if ok:
        _db.audit_add(f"services.{action}", keyword)
    return out if ok else f"操作失败：{out}"


def service_logs(keyword):
    s = find_service(keyword)
    if not s:
        return f"服务不在注册表：{keyword}"
    ok, out = run_ctl([keyword, "logs"])
    return out if ok else f"日志获取失败：{out}"


def check_all():
    """健康检查：结果写 kv + 异常写审计，返回 [(keyword, online, detail)]。"""
    results = []
    for s in services():
        online, detail = health_of(s)
        kw = str(s.get("keyword", ""))
        results.append((kw, online, detail))
        _db.kv_set(
            "health",
            kw,
            {"online": online, "detail": detail, "ts": datetime.now().isoformat(timespec="seconds")},
        )
        if not online:
            _db.audit_add("health.fail", kw, detail)
    return results


# ===== 一键恢复 =====
def _startable(entry):
    allow = entry.get("allow") or _shared.CONFIG.get("default_allow") or []
    return "start" in allow


def _wait_healthy(entry, tries=3, wait=5):
    for _ in range(tries):
        online, detail = health_of(entry)
        if online:
            return True, detail
        time.sleep(wait)
    return False, detail


def recover_one(entry) -> dict:
    kw = str(entry.get("keyword", ""))
    online, detail = health_of(entry)
    if online:
        return {"keyword": kw, "status": "ok", "detail": detail}
    if not _startable(entry):
        return {"keyword": kw, "status": "skipped", "detail": "allow 未含 start，跳过"}
    ok, out = run_ctl([kw, "start"])
    online2, detail2 = _wait_healthy(entry)
    if ok and online2:
        return {"keyword": kw, "status": "recovered", "detail": detail2}
    return {"keyword": kw, "status": "failed", "detail": f"{out}｜{detail2}"}


def run_recovery() -> list[dict]:
    results = [recover_one(s) for s in services()]
    _db.audit_add(
        "recovery.run",
        "all",
        "；".join(f"{r['keyword']}:{r['status']}" for r in results),
    )
    return results


def summary(results) -> str:
    ok = [r for r in results if r["status"] == "ok"]
    rec = [r for r in results if r["status"] == "recovered"]
    skip = [r for r in results if r["status"] == "skipped"]
    fail = [r for r in results if r["status"] == "failed"]
    lines = []
    if ok:
        lines.append(f"正常：{len(ok)} 个")
    if rec:
        lines.append("已拉起：" + "、".join(r["keyword"] for r in rec))
    if skip:
        lines.append("跳过（未授权 start）：" + "、".join(r["keyword"] for r in skip))
    if fail:
        lines.append(
            "恢复失败：" + "、".join(f"{r['keyword']}（{r['detail'][:60]}）" for r in fail)
        )
    return "\n".join(lines) or "服务注册表为空。"


# ===== MCP 工具 =====
def service_start(keyword):
    return control_service(keyword, "start")


def service_stop(keyword):
    return control_service(keyword, "stop")


def service_restart(keyword):
    return control_service(keyword, "restart")


def config_get():
    return _shared.CONFIG


def config_set(section, key, value):
    ok, out = run_ctl(["config-set", section, key, str(value)])
    if ok:
        _shared.reload_config()
    _db.audit_add("config.set", f"{section}.{key}", out)
    return out


def audit_query(limit=50, action=None):
    return _db.audit_query(limit=limit, action=action)


def disk_usage():
    try:
        proc = subprocess.run(["df", "-h"], capture_output=True, text=True, timeout=15)
        return proc.stdout.strip() or "(df 无输出)"
    except Exception as e:
        return f"磁盘查询失败：{e}"


def memory_clear(kind, key):
    if not memory.clear_memory(kind, key):
        return "参数错误：kind 为 user/member/group"
    _db.audit_add("memory.clear", f"{kind}:{key}")
    return f"已清除 {kind}:{key} 的记忆。"


def memory_search(query, scope=None, key=None, limit=10):
    """统一记忆检索：Memory Core 融合评分（关键词 + 向量 + 事件图 + 策略）。"""
    query = str(query or "").strip()
    if not query:
        return "参数错误：query 必填"
    return memory_core.search(query, scope, key, limit)


def memory_add(scope, key, fact):
    """写入统一记忆。scope: admin / c2c:<uid> / group:<gid> / group_all:<gid>。"""
    scope = str(scope or "").strip()
    fact = str(fact or "").strip()
    if not scope or not fact:
        return "参数错误：scope 与 fact 必填"
    memory_core.add_fact(scope, str(key or ""), fact)
    _db.audit_add("memory.add", f"{scope}:{key}", fact[:100])
    return f"已写入统一记忆：{scope}:{key}"


def notify_send(target_type, target, content):
    if target_type not in ("group", "c2c") or not str(target or "").strip() or not str(content or "").strip():
        return "参数错误：target_type 为 group/c2c，target 与 content 必填"
    allow = _shared.CONFIG.get("notify", {}).get("allow_targets") or []
    if allow and str(target).strip() not in allow:
        return f"目标不在播报白名单：{target}"
    _db.notif_add(target_type, str(target).strip(), str(content).strip())
    _db.audit_add("notify.send", f"{target_type}:{target}", str(content)[:100])
    return "已加入播报队列。"


TOOLS = [
    {"name": "services.list", "description": "列出服务注册表中的全部服务", "handler": services_list},
    {"name": "services.status", "description": "查询服务健康状态（可指定 keyword）", "handler": services_status},
    {"name": "services.start", "description": "启动注册表中的服务", "handler": service_start},
    {"name": "services.stop", "description": "停止注册表中的服务", "handler": service_stop},
    {"name": "services.restart", "description": "重启注册表中的服务", "handler": service_restart},
    {"name": "services.logs", "description": "获取服务最近日志", "handler": service_logs},
    {"name": "config.get", "description": "读取当前配置", "handler": config_get},
    {"name": "config.set", "description": "修改白名单配置字段（经 qqbot-ctl 校验）", "handler": config_set},
    {"name": "audit.query", "description": "查询操作审计记录", "handler": audit_query},
    {"name": "disk_usage", "description": "查看服务器磁盘占用概览", "handler": disk_usage},
    {"name": "memory.search", "description": "统一记忆检索（融合评分：关键词+向量+事件图+策略）", "handler": memory_search},
    {"name": "memory.add", "description": "写入统一记忆并建事件（scope: admin/c2c/group/group_all）", "handler": memory_add},
    {"name": "memory.clear", "description": "后台清除指定场景的记忆（kind: user/member/group）", "handler": memory_clear},
    {"name": "notify.send", "description": "向 QQ 群/私聊发送一条播报（入队）", "handler": notify_send},
]
