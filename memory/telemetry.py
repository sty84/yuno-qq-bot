# -*- coding: utf-8 -*-
"""轻量结构化日志/指标/trace：JSON Lines 写入 DATA_DIR/events.jsonl + Prometheus 文本指标。

作为统一可观测性入口：
- log_event()：结构化事件日志
- inc()/observe()：进程内指标计数
- metrics_text()：输出 Prometheus 文本格式
- request_id()/set_request_id()：跨 Web / agent.ask 的 request_id 透传
"""
import contextvars
import json
import threading
import time
import uuid
from pathlib import Path

_lock = threading.Lock()
_metrics_lock = threading.Lock()
_metrics: dict[str, float] = {}
_current_request_id: contextvars.ContextVar = contextvars.ContextVar("request_id", default="")


def set_request_id(rid: str):
    _current_request_id.set(rid)


def request_id() -> str:
    rid = _current_request_id.get()
    if rid:
        return rid
    rid = uuid.uuid4().hex[:12]
    _current_request_id.set(rid)
    return rid


def log_event(event: str, **fields):
    """写一条结构化事件日志；失败静默，不影响业务。"""
    try:
        from plugins import _shared
        data = {
            "event": event,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "request_id": fields.pop("request_id", None) or request_id(),
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


def inc(name: str, value: float = 1.0):
    """计数器 +value。"""
    with _metrics_lock:
        _metrics[name] = _metrics.get(name, 0.0) + float(value)


def observe(name: str, value: float):
    """Gauge/Histogram 简化：覆盖写入当前值。"""
    with _metrics_lock:
        _metrics[name] = float(value)


def metrics_text() -> str:
    """Prometheus 文本格式输出（无外部依赖）。"""
    lines = ["# HELP yuno_metric Yuno runtime metric", "# TYPE yuno_metric gauge"]
    with _metrics_lock:
        for name in sorted(_metrics):
            lines.append(f"yuno_{name} {_metrics[name]}")
    return "\n".join(lines) + "\n"
