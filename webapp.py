"""Yuno 评测管理台后端（v2.2 MVP）：把散在 tools.py 的评测/维护命令包成 HTTP。

启动：
  python webapp.py                    # 默认 127.0.0.1:8600（只本机访问）
  python webapp.py --host 0.0.0.0     # 公网暴露（需自行加 nginx 密码/TLS）

设计：进程内直接调 memory/ 函数（薄壳，不改现有逻辑）；
任务用 task_id + 2 秒轮询；eval 并发上限 2；消融/回放为后续轮次。
"""

import argparse
import json
import pathlib
import threading
import time
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from plugins import _db, _shared

ROOT = pathlib.Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
MAX_CONCURRENT = 2  # eval 会触发 LLM/检索，控制并发

_tasks = {}
_lock = threading.Lock()
_sem = threading.BoundedSemaphore(MAX_CONCURRENT)


def _run_task(task_id, kind, fn):
    with _sem:
        try:
            result = fn()
            with _lock:
                _tasks[task_id]["status"] = "done"
                _tasks[task_id]["result"] = result
                _tasks[task_id]["finished_ts"] = time.time()
        except Exception as e:
            with _lock:
                _tasks[task_id]["status"] = "error"
                _tasks[task_id]["error"] = str(e)
                _tasks[task_id]["finished_ts"] = time.time()


def submit(kind, fn) -> str:
    task_id = uuid.uuid4().hex[:12]
    with _lock:
        _tasks[task_id] = {
            "id": task_id, "kind": kind, "status": "running",
            "ts": time.time(), "finished_ts": None, "result": None, "error": None,
        }
    threading.Thread(target=_run_task, args=(task_id, kind, fn), daemon=True).start()
    return task_id


def _baseline_file(name):
    p = _shared.DATA_DIR / name
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {"error": "baseline 解析失败"}
    return None


def _load_probes():
    p = _shared.DATA_DIR / "probes.json"
    if not p.exists():
        raise HTTPException(400, f"评测集不存在（{p}），先跑 tools.py memory-probes 生成")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _task_fn(kind):
    if kind == "space_eval":
        def fn():
            from memory import space_eval
            return space_eval.run(save=True)
    elif kind == "time_eval":
        def fn():
            from memory import time_eval
            return time_eval.run(save=True)
    elif kind == "memory_eval":
        def fn():
            import memory
            probes = _load_probes()
            res = memory.run_eval(probes, k=5)
            _db.kv_set("memory", "eval_baseline", res)
            return res
    elif kind == "grow":
        def fn():
            import memory
            report = memory.backfill_run(batch=64)
            _db.kv_set("memory", "last_grow_report", report)
            return report
    else:
        raise HTTPException(400, f"未知任务类型：{kind}")
    return fn


app = FastAPI(title="Yuno Ops Web", version="0.2.0")


class TaskRequest(BaseModel):
    kind: str  # space_eval | time_eval | memory_eval | grow


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/counters")
def counters():
    import memory.stats as stats_mod
    return stats_mod.counters()


@app.get("/api/dashboard")
def dashboard():
    import memory.stats as stats_mod
    return {
        "counters": stats_mod.counters(),
        "memory_counts": {
            "memories": len(_db.memory_rows()),
            "events": len(_db.event_rows()),
            "attrs": len(_db.attr_rows()),
        },
        "baselines": {
            "memory_eval": _db.kv_get("memory", "eval_baseline"),
            "space_eval": _baseline_file("space_eval_baseline.json"),
            "time_eval": _baseline_file("time_eval_baseline.json"),
        },
        "grow_report": _db.kv_get("memory", "last_grow_report"),
    }


@app.post("/api/tasks")
def create_task(req: TaskRequest):
    try:
        fn = _task_fn(req.kind)
    except HTTPException:
        raise
    return {"task_id": submit(req.kind, fn)}


@app.get("/api/tasks")
def tasks():
    return list(_tasks.values())


@app.get("/api/tasks/{task_id}")
def task(task_id: str):
    t = _tasks.get(task_id)
    if t is None:
        raise HTTPException(404, "任务不存在")
    return t


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def main():
    import uvicorn
    parser = argparse.ArgumentParser(description="Yuno 评测管理台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8600)
    args = parser.parse_args()
    print(f"Yuno Ops Web → http://{args.host}:{args.port}（并发上限 {MAX_CONCURRENT}）")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
