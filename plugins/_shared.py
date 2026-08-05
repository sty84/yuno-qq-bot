"""插件共享基础层：配置、数据目录、AI 调用、服务器/容器/文件/邮件等通用能力。"""

import json
import os
import pathlib
import re
import shutil
import smtplib
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime
from email.message import EmailMessage

from openai import OpenAI

from plugins import _db

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv():
        pass

load_dotenv()

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
BASE_SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "你是一个乐于助人的 QQ 群 AI 助手，回答简洁、友好、使用中文。",
)
MAX_REPLY_LEN = 6000
ADMIN_OPENIDS = {
    x.strip() for x in os.getenv("ADMIN_OPENIDS", "").split(",") if x.strip()
}

deepseek = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

# ===== 配置 =====
CONFIG_PATH = os.getenv(
    "CONFIG_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.json"),
)


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


CONFIG = load_config()


def save_config():
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(CONFIG, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存 config.json 失败：{e}")


def reload_config():
    """配置被 root 脚本修改后，重新加载到内存。"""
    global CONFIG
    CONFIG = load_config()


def allowed_paths():
    return [pathlib.Path(p).resolve() for p in (CONFIG.get("allowed_paths") or [])]


def data_dir() -> pathlib.Path:
    paths = allowed_paths()
    if paths:
        return paths[0]
    return pathlib.Path(CONFIG_PATH).parent / "data"


DATA_DIR = data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)
_db.init(DATA_DIR)

def set_nickname(key, name):
    _db.nickname_set(str(key), str(name).strip()[:12])


def nickname_of(key):
    return _db.nickname_get(str(key))


def group_list_get():
    return _db.kv_get("groups", "list", [])


def group_list_add(gid):
    known = group_list_get()
    if gid not in known:
        known.append(gid)
        _db.kv_set("groups", "list", known)


# ===== 情绪 =====
state = _db.state_get() or {"mood": "慵懒"}
MOODS = ["慵懒", "开心", "元气", "困倦", "想打牌", "有点饿"]


def save_state():
    _db.state_set(state)


def set_mood(mood):
    state["mood"] = mood
    save_state()


def system_prompt() -> str:
    return BASE_SYSTEM_PROMPT + f"\n【当前心情：{state.get('mood', '慵懒')}】"


# ===== AI 调用 =====
def ask_deepseek(
    text: str,
    extra_context: str = "",
    history=None,
    system=None,
    max_tokens: int = 800,
) -> str:
    messages = [{"role": "system", "content": system or system_prompt()}]
    if extra_context:
        messages.append({"role": "system", "content": extra_context})
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": text})
    resp = deepseek.chat.completions.create(
        model=DEEPSEEK_MODEL,
        messages=messages,
        max_tokens=max_tokens,
    )
    reply = (resp.choices[0].message.content or "").strip()
    if len(reply) > MAX_REPLY_LEN:
        reply = reply[:MAX_REPLY_LEN] + "……"
    return reply


# ===== 身份 =====
def get_sender_ids(message) -> tuple[str, str]:
    author = getattr(message, "author", None)
    return (
        getattr(author, "member_openid", None) or "",
        getattr(author, "user_openid", None) or "",
    )


def is_admin(message) -> bool:
    if not ADMIN_OPENIDS:
        return False
    member, user = get_sender_ids(message)
    return member in ADMIN_OPENIDS or user in ADMIN_OPENIDS


def user_key_of(message) -> str:
    member, user = get_sender_ids(message)
    return user or member or "unknown"


def chat_key_of(message) -> str:
    group = getattr(message, "group_openid", None)
    if group:
        return "g:" + group
    return "c:" + user_key_of(message)


def scene_memory_keys(message):
    group = getattr(message, "group_openid", None)
    member, user = get_sender_ids(message)
    if group:
        return ("group", group, member or "unknown")
    return ("c2c", user or "unknown", None)


# ===== 余额 =====
def get_deepseek_balance_text() -> str:
    try:
        req = urllib.request.Request(
            "https://api.deepseek.com/user/balance",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        infos = data.get("balance_infos") or []
        if not infos:
            return "DeepSeek 账户：" + ("可用" if data.get("is_available") else "不可用")
        return "\n".join(
            f"{info.get('currency', '')} 总余额 {info.get('total_balance', '0')}"
            f"（赠送 {info.get('granted_balance', '0')}）"
            for info in infos
        )
    except Exception as e:
        return f"余额查询失败：{e}"


def get_balance_amount() -> float | None:
    try:
        req = urllib.request.Request(
            "https://api.deepseek.com/user/balance",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        total = 0.0
        for info in data.get("balance_infos") or []:
            if str(info.get("currency", "")).upper() == "CNY":
                total += float(info.get("total_balance", 0) or 0)
        return total
    except Exception:
        return None


# ===== 天气 =====
def get_weather(city: str) -> str:
    city = (city or "上海").strip()
    try:
        url = (
            "https://wttr.in/" + urllib.parse.quote(city)
            + "?format=%l:+%C+%t+%h+%w&lang=zh"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode("utf-8", errors="ignore").strip()
        return text or f"没查到 {city} 的天气。"
    except Exception as e:
        return f"天气查询失败：{e}"


# ===== 服务器状态 =====
def get_server_status() -> str:
    try:
        parts = []
        with open("/proc/loadavg", encoding="utf-8") as f:
            load = f.read().split()[:3]
        parts.append("负载: " + " ".join(load))
        parts.append(f"CPU 核数: {os.cpu_count()}")
        with open("/proc/meminfo", encoding="utf-8") as f:
            mem = {}
            for line in f:
                if ":" in line:
                    k, v = line.split(":", 1)
                    mem[k.strip()] = v.strip()
        total_mb = int(mem.get("MemTotal", "0 kB").split()[0]) // 1024
        avail_mb = int(mem.get("MemAvailable", "0 kB").split()[0]) // 1024
        parts.append(f"内存: {avail_mb}MB 可用 / {total_mb}MB 总共")
        with open("/proc/uptime", encoding="utf-8") as f:
            uptime = float(f.read().split()[0])
        parts.append(f"运行时间: {uptime / 86400:.1f} 天")
        du = shutil.disk_usage("/")
        parts.append(
            f"磁盘: 已用 {du.used // (2**30)}GB / 共 {du.total // (2**30)}GB"
            f"（剩余 {du.free // (2**30)}GB）"
        )
        return "\n".join(parts)
    except Exception:
        return "暂时读不到系统信息。"


# ===== 容器（白名单脚本）=====
def run_ctl(args: list[str]) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["sudo", "-n", "/usr/local/bin/qqbot-ctl"] + args,
            capture_output=True,
            text=True,
            timeout=300,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode == 0:
            return True, out or "操作完成。"
        return False, (out + "\n" + err).strip() or f"脚本返回错误码 {proc.returncode}"
    except subprocess.TimeoutExpired:
        return False, "操作超时。"
    except Exception as e:
        return False, f"调用白名单脚本失败：{e}"


def list_containers() -> str:
    ok, out = run_ctl(["list"])
    return out if ok else f"容器查询失败：{out}"


def control_container(action: str, keyword: str) -> str:
    keyword = keyword.strip().lower()
    if not keyword:
        return "请带上容器关键词，例如：/容器 停止 ainovel"
    if not re.fullmatch(r"[a-z0-9_\-]+", keyword):
        return "关键词只能包含字母、数字、下划线和中划线。"
    ok, out = run_ctl([keyword, action])
    return out if ok else f"操作失败：{out}"


def add_container_config(keyword: str, path: str) -> str:
    """添加容器：由 root 白名单脚本校验并写入 config.json。"""
    ok, out = run_ctl(["add", keyword.strip().lower(), path])
    if ok:
        reload_config()
    return out if ok else f"操作失败：{out}"


def remove_container_config(keyword: str) -> str:
    """删除容器：由 root 白名单脚本校验并从 config.json 移除。"""
    ok, out = run_ctl(["remove", keyword.strip().lower()])
    if ok:
        reload_config()
    return out if ok else f"操作失败：{out}"


# ===== 文件 =====
def _resolve_in_allowed(rel_path: str):
    rel = rel_path.strip().lstrip("/")
    if not rel or ".." in rel.split("/"):
        return None
    for base in allowed_paths():
        target = (base / rel).resolve()
        if target == base or base in target.parents:
            return target
    return None


def write_file(rel_path: str, content: str) -> str:
    target = _resolve_in_allowed(rel_path)
    if target is None:
        return "路径不在允许范围内（见 config.json 的 allowed_paths）。"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"已写入：{target}"
    except Exception as e:
        return f"写入失败：{e}"


def read_file(rel_path: str) -> str:
    target = _resolve_in_allowed(rel_path)
    if target is None:
        return "路径不在允许范围内（见 config.json 的 allowed_paths）。"
    try:
        text = target.read_text(encoding="utf-8")
        if len(text) > 1500:
            text = text[:1500] + "……（内容过长已截断）"
        return f"【{target}】\n{text}"
    except FileNotFoundError:
        return f"文件不存在：{target}"
    except Exception as e:
        return f"读取失败：{e}"


# ===== 邮件 =====
def send_email(subject: str, body: str) -> bool:
    host = os.getenv("SMTP_HOST", "")
    user = os.getenv("SMTP_USER", "")
    pwd = os.getenv("SMTP_PASS", "")
    to = os.getenv("MAIL_TO", "")
    if not (host and user and pwd and to):
        print("SMTP 未配置，跳过邮件发送")
        return False
    port = int(os.getenv("SMTP_PORT", "465"))
    mail_from = os.getenv("MAIL_FROM", user)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = to
    msg.set_content(body)
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=20) as s:
                s.login(user, pwd)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.starttls()
                s.login(user, pwd)
                s.send_message(msg)
        print(f"邮件已发送：{subject}")
        return True
    except Exception as e:
        print(f"邮件发送失败：{e}")
        return False


def detect_anomalies(text: str):
    keys = [
        "ERROR", "Traceback", "错误", "失败", "异常", "restarting",
        "Unhealthy", "OOMKilled", "timeout", "超时", "拒绝",
        "无法连接", "Connection refused", "5xx", "panic",
    ]
    hits = []
    for line in text.splitlines():
        for key in keys:
            if key in line:
                hits.append(line.strip()[:200])
                break
    return hits[:20]
