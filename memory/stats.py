"""轻量运行计数器（内存缓冲 + 定时落 kv）：System1 / 认知 / 找东西 / 意图 / LLM /
tick（各状态模块运行次数）/ err（裸 except 审计）。

用途：A/B 与消融实验的数据来源；grow 报告和 tools.py mind-status 会带上这些数字。
热路径只改内存，flush 间隔落盘，避免每条消息刷 SQLite。
"""

import logging
import time

from plugins import _db

_log = logging.getLogger(__name__)

_buf = {}  # type: ignore[var-annotated]
_last_flush = [0.0]
_FLUSH_INTERVAL = 30.0  # 秒：计数器缓冲落盘周期


def _flush():
    if not _buf:
        return
    try:
        d = _db.kv_get("memory", "run_stats") or {}
        for k, v in _buf.items():
            d[str(k)] = int(d.get(str(k), 0)) + int(v)
        _buf.clear()
        _db.kv_set("memory", "run_stats", d)
    except Exception:
        pass
    _last_flush[0] = time.time()


def bump(key, n=1):
    _buf[str(key)] = _buf.get(str(key), 0) + int(n)
    if time.time() - _last_flush[0] >= _FLUSH_INTERVAL:
        _flush()


def bump_err(module, e=None):
    """裸 except 审计：计数 err:<module> + logging 告警（消融/排查用）。"""
    bump(f"err:{module}")
    _log.warning("[err:%s] %s", module, e if e is not None else "")


def counters() -> dict:
    """落盘并返回全部计数器。"""
    _flush()
    try:
        return _db.kv_get("memory", "run_stats") or {}  # type: ignore[attr-defined]
    except Exception:
        return {}


def flush():
    _flush()


def summary() -> str:
    c = counters()
    if not c:
        return "运行计数器为空"
    return "；".join(f"{k}={v}" for k, v in sorted(c.items()))
