"""Yuno 评测管理台后端（v2.3）：把散在 tools.py 的评测/维护命令包成 HTTP。

启动：
  python webapp.py                    # 默认 127.0.0.1:8600（只本机访问）
  python webapp.py --host 0.0.0.0     # 公网暴露（需自行加 nginx 密码/TLS）

设计：进程内直接调 memory/ 函数（薄壳，不改现有逻辑）；
任务用 task_id + 2 秒轮询；eval 并发上限 2；消融/回放为后续轮次。
"""

import argparse
import json
import os
import pathlib
import shutil
import threading
import time
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from plugins import _db, _shared


def _apply_light_config():
    """4C4G 约束：webapp 是独立进程，默认关闭 embedding——
    否则跑 memory_eval/grow 时会和 bot 进程各自加载一份模型（torch ~1G+）挤爆内存。
    需要向量参与评测时设环境变量 YUNO_WEB_EMBEDDER=local。"""
    if os.getenv("YUNO_WEB_EMBEDDER", "none") == "none":
        try:
            _shared.CONFIG.setdefault("memory", {}).setdefault("embedder", {})["provider"] = "none"
        except Exception:
            pass


_apply_light_config()

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
            if kind in ("memory_eval", "space_eval", "time_eval", "emotion_eval", "subjects_eval"):
                try:
                    hist = _db.kv_get("memory", "baseline_history") or []
                    hist.append(_history_entry(kind, result))
                    _db.kv_set("memory", "baseline_history", hist[-200:])
                    # 回归门禁：与上一次同类型评测对比，退化写入实验日志
                    if len(hist) >= 2:
                        before = hist[-2]["metrics"]
                        after = _history_entry(kind, result)["metrics"]
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
            with _lock:
                _tasks[task_id]["status"] = "error"
                _tasks[task_id]["error"] = str(e)
                _tasks[task_id]["finished_ts"] = time.time()


def _history_entry(kind, result):
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


def _cost_prices() -> tuple:
    cfg = (_shared.CONFIG.get("memory", {}).get("core", {}) or {}).get("cost", {}) or {}
    try:
        pp = float(cfg.get("prompt_price_per_1m", 1.0))
        cp = float(cfg.get("completion_price_per_1m", 2.0))
    except (TypeError, ValueError):
        pp, cp = 1.0, 2.0
    return pp, cp


def _money(prompt, completion) -> float:
    pp, cp = _cost_prices()
    return round(float(prompt) / 1e6 * pp + float(completion) / 1e6 * cp, 4)


def _cost_summary_today() -> dict:
    t = _db.llm_cost_summary(1)["total"]
    t["cost"] = _money(t["prompt"], t["completion"])
    return t


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


def _emotion_probes():
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


def _count(table, where=""):
    try:
        sql = f"SELECT COUNT(*) FROM {table}"
        if where:
            sql += f" WHERE {where}"
        return int(_db._connect().execute(sql).fetchone()[0])
    except Exception:
        return 0


def _task_fn(kind):
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
            probes = _emotion_probes()
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


app = FastAPI(title="Yuno Ops Web", version="0.3.0")


class TaskRequest(BaseModel):
    kind: str  # space_eval | time_eval | memory_eval | grow


class ReplayRequest(BaseModel):
    scenario_id: str = ""
    score: bool = False


class ScoreRequest(BaseModel):
    scenario_id: str = ""
    scope: str = ""
    scores: dict = {}
    comment: str = ""
    mode: str = "manual"


class AblationToggle(BaseModel):
    switch: str
    value: bool = True


class AblationRun(BaseModel):
    switches: list = []


class ToolRun(BaseModel):
    name: str


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
    try:
        import memory
        emotion_log_count = len(memory.emotion_log_rows(7))
    except Exception:
        emotion_log_count = 0
    return {
        "counters": stats_mod.counters(),
        "emotion_log_count": emotion_log_count,
        "memory_counts": {
            "memories": len(_db.memory_rows()),
            "events": len(_db.event_rows()),
            "attrs": len(_db.attr_rows()),
        },
        "baselines": {
            "memory_eval": _db.kv_get("memory", "eval_baseline"),
            "space_eval": _baseline_file("space_eval_baseline.json"),
            "time_eval": _baseline_file("time_eval_baseline.json"),
            "emotion_eval": _db.kv_get("memory", "emotion_baseline"),
            "subjects_eval": _baseline_file("subjects_eval_baseline.json"),
        },
        "grow_report": _db.kv_get("memory", "last_grow_report"),
        "cost_today": _cost_summary_today(),
    }


@app.get("/api/diagnostics")
def diagnostics():
    """数据与诊断：路由命中率 / 程序记忆 / 数据量 / 人工评分进度 / 标定状态。"""
    out = {"route_stats": {}, "procedures": {}, "calibrate": "", "data_counts": {}, "review": {}}
    try:
        from memory import procedures, reasoning
        rs = reasoning._route_stats()
        for algo, s in rs.items():
            trials = int(s.get("trials", 0))
            hits = int(s.get("hits", 0))
            out["route_stats"][algo] = {
                "trials": trials, "hits": hits,
                "rate": round(hits / trials, 3) if trials else None,
            }
        out["procedures"] = procedures.stats()
    except Exception:
        pass
    try:
        from memory import policy
        out["calibrate"] = str(policy.calibrate_report())[:600]
    except Exception:
        pass
    probes = 0
    try:
        p = _shared.DATA_DIR / "probes.json"
        if p.exists():
            probes = len(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        pass
    out["data_counts"] = {
        "probes": probes,
        "query_log": _count("query_log"),
        "item_events": _count("item_events"),
        "explicit_events": _count("events", "ts_source='explicit'"),
        "trace": _count("memory_trace"),
        "feedback": _count("feedback_log"),
        "procedures": _count("procedures"),
    }
    try:
        rows = _db.trace_review_recent(limit=200)
        dims = ("extraction", "decision", "confidence", "provenance", "privacy")
        sums = {d: 0 for d in dims}
        total_score = 0
        n = len(rows)
        for r in rows:
            total_score += float(r.get("score") or 0)
            scores = r.get("scores")
            if isinstance(scores, str):
                try:
                    scores = json.loads(scores)
                except Exception:
                    scores = {}
            if isinstance(scores, dict):
                for d in dims:
                    try:
                        sums[d] += float(scores.get(d) or 0)
                    except (TypeError, ValueError):
                        pass
        out["review"] = {
            "n": n,
            "avg_total": round(total_score / n, 2) if n else None,
            **{f"avg_{d}": round(sums[d] / n, 2) if n else None for d in dims},
        }
    except Exception:
        pass
    return out


@app.get("/api/history")
def history():
    """基线历史趋势（每次跑评测自动记录，最多 200 条）。"""
    return _db.kv_get("memory", "baseline_history") or []


@app.get("/api/experiments")
def experiments(limit: int = 50):
    """实验日志：基线前后与回归标记。"""
    return _db.exp_log_rows(limit)


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


@app.get("/api/scenarios")
def scenarios():
    """场景集列表（对话回放/五维评分用）。"""
    p = _shared.DATA_DIR / "eval" / "scenarios.json"
    if not p.exists():
        # 无场景集时用内置示例播种
        seed = ROOT / "memory" / "scenarios.example.json"
        if seed.exists():
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(seed, p)
            except Exception:
                pass
    if not p.exists():
        return {"exists": False, "path": str(p), "scenarios": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8")) or []
    except Exception as e:
        raise HTTPException(500, f"场景集解析失败：{e}")
    return {
        "exists": True,
        "path": str(p),
        "scenarios": [
            {
                "id": s.get("id"),
                "scope": s.get("scope"),
                "n_messages": len(s.get("messages") or []),
                "expected": s.get("expected") or [],
            }
            for s in data
        ],
    }


@app.post("/api/scenarios/replay")
def replay(req: ReplayRequest):
    """回放场景（可选 DeepSeek 五维评分）。异步任务，前端轮询。"""
    def fn():
        from tools import scenario_replay
        return scenario_replay(scenario_id=req.scenario_id or None, score=req.score)
    return {"task_id": submit("scenario_replay", fn)}


@app.post("/api/scenarios/score")
def save_score(req: ScoreRequest):
    """保存一次人工五维评分。"""
    return _db.scenario_score_add(req.scenario_id, req.scope, req.scores, req.comment, req.mode)


@app.get("/api/scenario-scores")
def scenario_scores(limit: int = 100):
    """已保存的五维评分历史（人工 + LLM）。"""
    return _db.scenario_score_rows(limit)


@app.get("/api/ablation/state")
def ablation_state():
    """消融开关当前状态（热插拔面板）。"""
    from tools import ablation_state as _state
    return _state()


@app.post("/api/ablation/toggle")
def ablation_toggle(req: AblationToggle):
    """热插拔开关：改 config 并落盘（bot 进程 reload_if_changed 生效）。"""
    from tools import apply_switch
    return apply_switch(req.switch, req.value)


@app.post("/api/ablation/run")
def ablation_run(req: AblationRun):
    """按选定开关跑消融矩阵（异步任务）。"""
    def fn():
        from tools import run_ablation
        probes = _load_probes()
        return run_ablation(probes, names=req.switches or None)
    return {"task_id": submit("ablation", fn)}


@app.get("/api/bandit")
def bandit_status(scope: str = ""):
    """回应策略 bandit 后验。"""
    from memory import bandit
    return bandit.status(scope)


@app.get("/api/revive")
def revive_status(scope: str = ""):
    """主动消息决策状态（泊松 + 贝叶斯，只读）。"""
    from memory import revive
    return revive.peek(scope or None)


@app.get("/api/policy-classify")
def policy_classify():
    """事实分类探针：含关键词但其实是过程/指令的句子误判率。"""
    from memory import policy
    return policy.classify_report()


@app.get("/api/consistency")
def consistency():
    """双轨制一致性：失效队列长度 + 本次重算数（与 CLI 同口径，幂等）。"""
    pending = len(_db.invalidation_rows(100))
    from memory import controller
    done = controller.reconcile_pending()
    return {"pending": pending, "reconciled": done.get("reconciled", 0)}


@app.get("/api/costs")
def costs(days: int = 30):
    """LLM token / 成本：总量、按天、按模块、按检索路径（rerank 归因）。"""
    s = _db.llm_cost_summary(days)
    s["total_cost"] = _money(s["total"]["prompt"], s["total"]["completion"])
    for d in s["by_day"]:
        d["cost"] = _money(d["prompt"], d["completion"])
    for m in s["by_module"]:
        m["cost"] = _money(m["prompt"], m["completion"])
    for q in s["by_path"]:
        q["cost"] = _money(q["prompt"], q["completion"])
    pp, cp = _cost_prices()
    s["prices"] = {"prompt_per_1m": pp, "completion_per_1m": cp}
    return s


@app.get("/api/hesitation")
def hesitation():
    """犹豫层统计 + 最近决策明细（管理台回看）。"""
    from memory import hesitation as hesitation_mod
    return {"stats": hesitation_mod.stats(), "recent": _db.hesitation_log_rows(30)}


@app.post("/api/tools")
def run_tool(req: ToolRun):
    """运维动作白名单（诊断页按钮）：约定表巡检清理 / 记忆来源归一。"""
    if req.name == "appointment-clean":
        from memory import appointment
        return appointment.clean()
    if req.name == "memory-source-backfill":
        return _db.memory_source_normalize()
    raise HTTPException(400, f"未知工具：{req.name}")


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
