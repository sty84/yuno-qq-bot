# -*- coding: utf-8 -*-
"""轻量结构化日志/指标/trace：JSON Lines 写入 DATA_DIR/events.jsonl。

作为统一可观测性入口，后续可替换为正式日志采集（如 structlog + OTEL）。
"""
import json
import threading
import time
import uuid
from pathlib import Path

_lock = threading.Lock()


def request_id() -> str:
    return uuid.uuid4().hex[:12]


def log_event(event: str, **fields):
    """写一条结构化事件日志；失败静默，不影响业务。"""
    try:
        from plugins import _shared
        data = {
            "event": event,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            **fields,
        }
        line = json.dumps(data, ensure_ascii=False, default=str)
        p = Path(_shared.DATA_DIR) / "events.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            with open(p, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass
