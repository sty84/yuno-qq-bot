"""时区处理：默认 Asia/Shanghai；检测用户所在地（“我现在在美国”）并记住，按当地时区回答时间。"""

import os
import re

from plugins import _db

DEFAULT_TZ = "Asia/Shanghai"

# 地点 → IANA 时区（城市在前，避免“洛杉矶”被“美国”抢先）
_LOCATIONS = [
    ("洛杉矶", "America/Los_Angeles"),
    ("旧金山", "America/Los_Angeles"),
    ("加州", "America/Los_Angeles"),
    ("西雅图", "America/Los_Angeles"),
    ("纽约", "America/New_York"),
    ("波士顿", "America/New_York"),
    ("多伦多", "America/Toronto"),
    ("温哥华", "America/Vancouver"),
    ("东京", "Asia/Tokyo"),
    ("大阪", "Asia/Tokyo"),
    ("首尔", "Asia/Seoul"),
    ("伦敦", "Europe/London"),
    ("巴黎", "Europe/Paris"),
    ("柏林", "Europe/Berlin"),
    ("悉尼", "Australia/Sydney"),
    ("墨尔本", "Australia/Melbourne"),
    ("奥克兰", "Pacific/Auckland"),
    ("曼谷", "Asia/Bangkok"),
    ("新加坡", "Asia/Singapore"),
    ("迪拜", "Asia/Dubai"),
    ("北京", "Asia/Shanghai"),
    ("上海", "Asia/Shanghai"),
    ("深圳", "Asia/Shanghai"),
    ("台北", "Asia/Taipei"),
    ("香港", "Asia/Hong_Kong"),
    ("美国", "America/New_York"),
    ("加拿大", "America/Toronto"),
    ("日本", "Asia/Tokyo"),
    ("韩国", "Asia/Seoul"),
    ("英国", "Europe/London"),
    ("法国", "Europe/Paris"),
    ("德国", "Europe/Berlin"),
    ("澳洲", "Australia/Sydney"),
    ("新西兰", "Pacific/Auckland"),
    ("泰国", "Asia/Bangkok"),
    ("中国", "Asia/Shanghai"),
    ("台湾", "Asia/Taipei"),
]

# “在/人在/现在在/到/到了/去/去了” 后跟地点才算“身处该地”（整体分组，避免 | 优先级问题）
_HERE_RE = re.compile(r"(?:(?:现在|人)?在|到(?:了)?|去(?:了)?)")


def _tz_setting() -> str:
    return os.getenv("TIMEZONE", "").strip() or DEFAULT_TZ


def detect(text) -> str | None:
    """从消息判断用户所在地时区；无明确地点返回 None。"""
    t = str(text or "")
    for name, tz in _LOCATIONS:
        if name in t and re.search(_HERE_RE.pattern + re.escape(name), t):
            return tz
    return None


def remember(scope, text) -> str | None:
    """检测并记住该用户时区（存 kv，按 scope 隔离）。"""
    tz = detect(text)
    if tz and scope:
        _db.kv_set("memory", f"tz:{scope}", {"tz": tz})
    return tz


def user_tz(scope=None) -> str:
    """该用户的时区：先查记忆，再用 TIMEZONE/默认。"""
    if scope:
        data = _db.kv_get("memory", f"tz:{scope}") or {}
        if data.get("tz"):
            return data["tz"]
    return _tz_setting()


def now_text(scope=None) -> str:
    """自然口语的时间参考（按用户时区）。
    用户问当前时间/日期时必须以本块时间为准，防止 LLM 复读历史里的旧时间。"""
    from datetime import datetime
    tz_name = user_tz(scope)
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(tz_name))
    except Exception:
        now = datetime.now()
    wd = "一二三四五六日"[now.weekday()]
    note = "（按你的当地时间）" if scope and user_tz(scope) else ""
    return (
        f"【时间参考】现在约是 {now:%Y年%m月%d日} 周{wd} {now:%H:%M}{note}。"
        "提到时间时自然口语带过即可，不要报完整时间戳，不要显得机械。"
        "如果用户问现在几点/几号/星期几，必须以此时间为准，"
        "不要参考历史对话或记忆里的时间。"
    )
