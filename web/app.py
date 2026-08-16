"""Web app 工厂：创建 FastAPI 实例并挂载各路由模块。

原 webapp.py 中与进程状态/后台任务/评测辅助相关的逻辑集中在这里，
每个 create_app() 调用都会得到一份独立的任务/会话/限流状态，便于测试 reload。
"""

import json
import os
import pathlib
import shutil
import threading
import time
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from plugins import _db, _shared

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"
MAX_CONCURRENT = 2


def _apply_light_config():
    """4C4G 约束：webapp 是独立进程，默认关闭 embedding——
    否则跑 memory_eval/grow 时会和 bot 进程各自加载一份模型（torch ~1G+）挤爆内存。
    需要向量参与评测时设环境变量 YUNO_WEB_EMBEDDER=local。"""
    if os.getenv("YUNO_WEB_EMBEDDER", "none") == "none":
        try:
            _shared.CONFIG.setdefault("memory", {}).setdefault("embedder", {})["provider"] = "none"
        except Exception:
            pass


class AppState:
    """每个 app 实例独立的运行时状态（任务、锁、限流、会话）。"""

    def __init__(self):
        self.tasks = {}
        self.lock = threading.Lock()
        self.sem = threading.BoundedSemaphore(MAX_CONCURRENT)
        self.rate = {}
        self.sessions = {}
        self.session_ttl = 12 * 3600

    def stats_err(self, e):
        """裸 except 审计（与项目其他模块一致）。"""
        try:
            import memory.stats as _st
            _st.bump_err("webapp", e)
        except Exception:
            pass

    def check_rate(self, key: str, limit: int = 10, window: float = 60.0):
        """极简内存速率限制：同一 key 在窗口内最多 limit 次。"""
        now = time.time()
        arr = self.rate.setdefault(key, [])
        arr[:] = [t for t in arr if t > now - window]
        if len(arr) >= limit:
            raise HTTPException(429, "请求过于频繁，请稍后再试")
        arr.append(now)

    def run_task(self, task_id, kind, fn):
        with self.sem:
            try:
                result = fn()
                with self.lock:
                    self.tasks[task_id]["status"] = "done"
                    self.tasks[task_id]["result"] = result
                    self.tasks[task_id]["finished_ts"] = time.time()
                if kind in ("memory_eval", "space_eval", "time_eval", "emotion_eval", "subjects_eval"):
                    try:
                        hist = _db.kv_get("memory", "baseline_history") or []
                        hist.append(self.history_entry(kind, result))
                        _db.kv_set("memory", "baseline_history", hist[-200:])
                        # 回归门禁：与上一次同类型评测对比，退化写入实验日志
                        if len(hist) >= 2:
                            before = hist[-2]["metrics"]
                            after = self.history_entry(kind, result)["metrics"]
                            delta = {
                                k: round(float(after[k]) - float(before[k]), 3)
                                for k in after
                                if k in before and after[k] is not None and before[k] is not None
                            }
                            regression = any(v < -0.03 for v in delta.values())
                            _db.exp_log_add(
                                kind, detail="webapp eval", before=before, after=after,
                                delta=delta, regression=regression,
                            )
                    except Exception:
                        pass
            except Exception as e:
                with self.lock:
                    self.tasks[task_id]["status"] = "error"
                    self.tasks[task_id]["error"] = str(e)
                    self.tasks[task_id]["finished_ts"] = time.time()

    def history_entry(self, kind, result):
        """把一次评测结果压成一行历史（趋势图数据源）。"""
        m = {}
        if isinstance(result, dict):
            if kind == "memory_eval":
                m = {"recall": result.get("recall_at_k"), "mrr": result.get("mrr"), "ndcg": result.get("ndcg")}
            elif kind == "space_eval":
                m = {
                    "where_acc": (result.get("where_accuracy") or {}).get("accuracy"),
                    "where_recall": (result.get("where_recall") or {}).get("recall"),
                    "avg_steps": (result.get("search_sim") or {}).get("avg_steps"),
                }
            elif kind == "time_eval":
                m = {
                    "window_recall": (result.get("window_recall") or {}).get("recall"),
                    "timeline_rate": (result.get("timeline_order") or {}).get("rate"),
                    "date_acc": (result.get("date_accuracy") or {}).get("accuracy"),
                }
            elif kind == "emotion_eval":
                m = {"accuracy": result.get("accuracy"), "vad_mae": result.get("vad_mae")}
            elif kind == "subjects_eval":
                m = {"write_rate": result.get("write_rate"), "privacy_rate": result.get("privacy_rate")}
        return {"ts": time.strftime("%Y-%m-%d %H:%M"), "kind": kind, "metrics": m}

    def cost_prices(self):
        cfg = (_shared.CONFIG.get("memory", {}).get("core", {}) or {}).get("cost", {}) or {}
        try:
            pp = float(cfg.get("prompt_price_per_1m", 1.0))
            cp = float(cfg.get("completion_price_per_1m", 2.0))
        except (TypeError, ValueError):
            pp, cp = 1.0, 2.0
        return pp, cp

    def money(self, prompt, completion):
        pp, cp = self.cost_prices()
        return round(float(prompt) / 1e6 * pp + float(completion) / 1e6 * cp, 4)

    def cost_summary_today(self):
        t = _db.llm_cost_summary(1)["total"]
        t["cost"] = self.money(t["prompt"], t["completion"])
        return t

    def submit(self, kind, fn):
        task_id = uuid.uuid4().hex[:12]
        with self.lock:
            self.tasks[task_id] = {
                "id": task_id, "kind": kind, "status": "running",
                "ts": time.time(), "finished_ts": None, "result": None, "error": None,
            }
        threading.Thread(target=self.run_task, args=(task_id, kind, fn), daemon=True).start()
        return task_id

    def baseline_file(self, name):
        p = _shared.DATA_DIR / name
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return {"error": "baseline 解析失败"}
        return None

    def load_probes(self):
        p = _shared.DATA_DIR / "probes.json"
        if not p.exists():
            raise HTTPException(400, f"评测集不存在（{p}），先跑 tools.py memory-probes 生成")
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def emotion_probes(self):
        """情绪评测集：没有就把 memory/emotion_probes.example.json 种子成 data/emotion_probes.json。"""
        p = _shared.DATA_DIR / "emotion_probes.json"
        if not p.exists():
            seed = ROOT / "memory" / "emotion_probes.example.json"
            if seed.exists():
                shutil.copyfile(seed, p)
        if not p.exists():
            raise HTTPException(400, f"情绪评测集不存在（{p}）")
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def count(self, table, where=""):
        try:
            sql = f"SELECT COUNT(*) FROM {table}"
            if where:
                sql += f" WHERE {where}"
            conn = _db._connect()
            if hasattr(conn, "cursor"):
                with conn.cursor() as cur:
                    cur.execute(sql)
                    return int(cur.fetchone()[0])
            return int(conn.execute(sql).fetchone()[0])
        except Exception:
            return 0

    def task_fn(self, kind):
        if kind == "space_eval":
            def fn():
                from memory import space_eval
                return space_eval.run(save=True)
        elif kind == "time_eval":
            def fn():
                from memory import time_eval
                return time_eval.run(save=True)
        elif kind == "emotion_eval":
            def fn():
                import memory
                probes = self.emotion_probes()
                res = memory.emotion_eval(probes)
                try:
                    from memory import topic as topic_mod
                    res["topic_mood"] = topic_mod.mood_eval()
                except Exception:
                    pass
                _db.kv_set("memory", "emotion_baseline", res)
                return res
        elif kind == "subjects_eval":
            def fn():
                from memory import subjects
                return subjects.eval_run(save=True)
        elif kind == "memory_eval":
            def fn():
                import memory
                probes = self.load_probes()
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


def create_app():
    """创建新的 FastAPI app；每次调用都使用独立运行时状态。"""
    from . import auth, routes_admin, routes_cognitive, routes_eval

    state = AppState()
    app = FastAPI(title="Yuno Ops Web", version="0.3.0")

    from memory import telemetry

    @app.middleware("http")
    async def _log_request(request, call_next):
        rid = telemetry.request_id()
        request.state.request_id = rid
        start = time.time()
        response = await call_next(request)
        telemetry.log_event(
            "web.request",
            request_id=rid,
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round((time.time() - start) * 1000, 2),
        )
        return response

    auth.install_auth(app, state)
    routes_eval.register(app, state)
    routes_cognitive.register(app, state)
    routes_admin.register(app, state)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
