"""QQ 聊天前台共享层：配置、AI 调用、身份、情绪、通知。

企划 2.0 起不再包含服务器/容器/文件/邮件等管理能力
（已迁至 plugins/_capability.py 与 MCP 能力层，QQ 端不暴露）。
"""

import json
import os
import pathlib
import time

from openai import OpenAI

from plugins import _db

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs):
        pass

# 显式定位项目根目录的 .env（不依赖启动时的工作目录），
# 否则从别处 python bot.py 会因找不到 .env 而读不到凭证/镜像配置。
_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

# HF_HOME 若是相对路径（如 ./data/hf_cache），固定到项目根目录下。
_hf_home = os.getenv("HF_HOME", "")
if _hf_home and not os.path.isabs(_hf_home):
    os.environ["HF_HOME"] = str(_PROJECT_ROOT / _hf_home)

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEFAULT_SYSTEM_PROMPT = "你是一个乐于助人的 QQ 群 AI 助手，回答简洁、友好、使用中文。"
MAX_REPLY_LEN = 6000
ADMIN_OPENIDS = {x.strip() for x in os.getenv("ADMIN_OPENIDS", "").split(",") if x.strip()}

deepseek = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)


def _load_persona() -> str:
    """人设单一来源：优先 .env 的 SYSTEM_PROMPT，否则读 Persona Pack 的 persona.md。"""
    if os.getenv("SYSTEM_PROMPT"):
        return os.getenv("SYSTEM_PROMPT")  # type: ignore[return-value]
    try:
        try:
            pk = str(
                (CONFIG.get("memory", {}) or {}).get("core", {})
                .get("persona_pack", {}).get("pack", "yuno") or "yuno"
            ).strip() or "yuno"
            text = (
                pathlib.Path(__file__).resolve().parent.parent / "personas" / pk / "persona.md"
            ).read_text(encoding="utf-8").strip()
        except Exception:
            text = ""
        if not text:
            text = (
                pathlib.Path(__file__).resolve().parent.parent / "persona.md"
            ).read_text(encoding="utf-8").strip()
        if text:
            return text
    except OSError:
        pass
    return DEFAULT_SYSTEM_PROMPT


# ===== 配置 =====
CONFIG_PATH = os.getenv(
    "CONFIG_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.json"),
)


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        _stats_err(e)
        return {}


CONFIG = load_config()
BASE_SYSTEM_PROMPT = _load_persona()  # 必须在 CONFIG 定义之后（读取 persona_pack 需要 CONFIG）
try:
    _config_mtime = os.path.getmtime(CONFIG_PATH)
except OSError:
    _config_mtime = None  # type: ignore[assignment]


def _sync_config_deps():
    _db.set_audit_max(CONFIG.get("audit", {}).get("max_entries", 5000))


def save_config():
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(CONFIG, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存 config.json 失败：{e}")


def core_cfg(section, key, default=None):
    """读 CONFIG['memory']['core'][section][key]；section='' 读 core 顶层。
    memory 各模块统一配置入口（替代各模块重复的 _cfg 实现）。"""
    try:
        core = CONFIG.get("memory", {}).get("core", {}) or {}
        seg = core.get(section) if section else core
        return (seg or {}).get(key, default)
    except Exception:
        return default


def reload_config():
    """配置被 root 脚本修改后，重新加载到内存。"""
    global CONFIG, BASE_SYSTEM_PROMPT
    CONFIG = load_config()
    BASE_SYSTEM_PROMPT = _load_persona()  # 切 pack 后刷新人设（bot 后台循环 reload 生效）
    _sync_config_deps()
    try:
        from memory import pack
        pack.invalidate()
        from agent import persona
        persona._persona_name_cache = None
    except Exception:
        pass


def reload_if_changed():
    """配置文件 mtime 变化时热加载（后台循环/独立脚本调用）。"""
    global _config_mtime
    try:
        mtime = os.path.getmtime(CONFIG_PATH)
    except OSError:
        return
    if _config_mtime is not None and mtime == _config_mtime:
        return
    _config_mtime = mtime
    reload_config()


def data_dir() -> pathlib.Path:
    paths = [pathlib.Path(p).resolve() for p in (CONFIG.get("allowed_paths") or [])]
    base = paths[0] if paths else pathlib.Path(CONFIG_PATH).parent / "data"
    # 记忆隔离（v2.2 P3）：每个 Persona Pack 独立数据目录，换人设不污染
    try:
        pk = str(
            (CONFIG.get("memory", {}) or {}).get("core", {})
            .get("persona_pack", {}).get("pack", "yuno") or "yuno"
        ).strip() or "yuno"
    except Exception:
        pk = "yuno"
    d = base / ("persona-" + pk)
    try:
        d.mkdir(parents=True, exist_ok=True)
        if not (d / "bot.db").exists() and (base / "bot.db").exists():
            import shutil
            shutil.copy(base / "bot.db", d / "bot.db")
    except Exception:
        pass
    return d


DATA_DIR = data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)
_db.init(DATA_DIR)  # type: ignore[attr-defined]
_sync_config_deps()

# ===== 昵称 / 群列表 =====
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
MOODS = ["慵懒", "开心", "元气", "困倦", "想打牌", "有点饿"]
state = _db.state_get() or {"mood": "慵懒"}  # type: ignore[attr-defined]


def set_mood(mood):
    state["mood"] = mood
    _db.state_set(state)


def system_prompt() -> str:
    return BASE_SYSTEM_PROMPT + f"\n【当前心情：{state.get('mood', '慵懒')}】"


# ===== AI 调用 =====
def _bump_counter(key, n=1):
    """运行计数器：交给 memory.stats（内存缓冲 + 定时落盘）。"""
    try:
        import memory.stats as stats_mod
        stats_mod.bump(key, n)
    except Exception as e:
        _stats_err(e)
        pass


def record_llm_usage(module="chat", detail="", resp=None, chars=0):
    """LLM 调用成本观测：从响应 usage 取 prompt/completion token，落 llm_cost 表 + 计数器。
    供成本页与"机制 × token × 消融"权衡用。"""
    try:
        pt = ct = 0
        if resp is not None:
            usage = getattr(resp, "usage", None)
            if usage is not None:
                pt = int(getattr(usage, "prompt_tokens", 0) or 0)
                ct = int(getattr(usage, "completion_tokens", 0) or 0)
        _db.llm_cost_add(
            time.strftime("%Y-%m-%dT%H:%M:%S"), module, detail, pt, ct, int(chars or 0)
        )
        _bump_counter("llm_prompt_tokens", pt)
        _bump_counter("llm_completion_tokens", ct)
    except Exception:
        pass


def deepseek_chat(messages, max_tokens=800, temperature=None, module="chat", detail=""):
    """LLM 调用的统一入口（成本观测）：create + record_llm_usage，返回 resp。
    所有 direct deepseek.chat.completions.create 都迁到这里，避免漏埋点。"""
    kwargs = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "max_tokens": int(max_tokens),
        "timeout": 30,
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    resp = deepseek.chat.completions.create(**kwargs)
    chars = sum(len(str(m.get("content") or "")) for m in (messages or []))
    record_llm_usage(module, detail, resp, chars)
    return resp


def ask_deepseek(
    text: str,
    extra_context: str = "",
    history=None,
    system=None,
    max_tokens: int = 800,
    temperature=None,
    module: str = "chat",
    detail: str = "",
) -> str:
    messages = [{"role": "system", "content": system or system_prompt()}]
    if extra_context:
        messages.append({"role": "system", "content": extra_context})
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": text})
    last_err = None
    for attempt in range(3):
        try:
            resp = deepseek_chat(messages, max_tokens=max_tokens, temperature=temperature, module=module, detail=detail)
            reply = (resp.choices[0].message.content or "").strip()
            _bump_counter("llm_calls", 1)
            _bump_counter("llm_chars", len(str(text)) + len(str(extra_context)) + len(reply))
            return reply[:MAX_REPLY_LEN] + ("……" if len(reply) > MAX_REPLY_LEN else "")
        except Exception as e:
            last_err = e
            print(f"[AI] DeepSeek 第 {attempt + 1} 次调用失败：{e}")
            if attempt < 2:
                time.sleep(1 + attempt)
    raise last_err  # type: ignore[misc]


# ===== 身份 =====
def get_sender_ids(message) -> tuple[str, str]:
    author = getattr(message, "author", None)
    return (
        getattr(author, "member_openid", None) or "",
        getattr(author, "user_openid", None) or "",
    )


def is_admin(message) -> bool:
    member, user = get_sender_ids(message)
    return bool(ADMIN_OPENIDS) and (member in ADMIN_OPENIDS or user in ADMIN_OPENIDS)


def user_key_of(message) -> str:
    member, user = get_sender_ids(message)
    return user or member or "unknown"


def chat_key_of(message) -> str:
    group = getattr(message, "group_openid", None)
    return ("g:" + group) if group else ("c:" + user_key_of(message))


def scene_memory_keys(message):
    group = getattr(message, "group_openid", None)
    member, user = get_sender_ids(message)
    if group:
        return ("group", group, member or "unknown")
    return ("c2c", user or "unknown", None)


# ===== 通知（播报/告警统一出口）=====
def send_message(api, target_type: str, target: str, content: str):
    content = content[:500]
    if target_type == "c2c":
        return api.post_c2c_message(openid=target, content=content)
    return api.post_group_message(group_openid=target, content=content)



def _stats_err(e):
    """裸 except 审计（v2.2）：错误计数 + 日志，供消融/排查。"""
    try:
        import memory.stats as _st
        _st.bump_err("_shared", e)
    except Exception:
        pass
