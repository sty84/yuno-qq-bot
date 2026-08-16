"""数据导出/导入/运维/健康等管理接口。"""

import json
import os
import pathlib
import time

from fastapi import HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from plugins import _db, _shared
from web.auth import require_role

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"
STARTED_AT = time.time()


class DataImportRequest(BaseModel):
    data: dict = {}
    replace: bool = False
    confirm: bool = False


class NotifyRequest(BaseModel):
    content: str
    target_type: str = "group"
    target: str = ""
    confirm: bool = False


class ToolRun(BaseModel):
    name: str
    confirm: bool = False
    uid: str = ""
    scope: str = ""


def register(app, state):
    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/metrics")
    def metrics():
        """Prometheus 文本指标（供 Prometheus 抓取）。"""
        from memory import telemetry
        return Response(content=telemetry.metrics_text(), media_type="text/plain")

    @app.get("/api/status")
    def status():
        """运维状态：schema 版本、DB 大小、最近 grow、运行中任务数。"""
        db_size = 0
        if _db.DB_PATH:
            try:
                db_size = pathlib.Path(_db.DB_PATH).stat().st_size
            except Exception:
                pass
        return {
            "schema_version": _db._schema_version(),
            "db_size": db_size,
            "last_grow": _db.kv_get("memory", "last_grow_report"),
            "tasks_running": sum(1 for t in state.tasks.values() if t.get("status") == "running"),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(STARTED_AT)),
            "uptime_seconds": round(time.time() - STARTED_AT, 1),
            "auth_enabled": bool(os.getenv("YUNO_WEB_TOKEN")),
        }

    @app.get("/api/public/stats")
    def public_stats():
        """公开只读统计：用于展示页/监控，不暴露敏感数据。"""
        return {
            "memory_count": state.count("memories"),
            "event_count": state.count("events"),
            "conv_count": state.count("conv_log"),
            "trace_count": state.count("memory_trace"),
            "today_cost": state.cost_summary_today(),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(STARTED_AT)),
            "uptime_seconds": round(time.time() - STARTED_AT, 1),
        }

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
                "space_eval": state.baseline_file("space_eval_baseline.json"),
                "time_eval": state.baseline_file("time_eval_baseline.json"),
                "emotion_eval": _db.kv_get("memory", "emotion_baseline"),
                "subjects_eval": state.baseline_file("subjects_eval_baseline.json"),
            },
            "grow_report": _db.kv_get("memory", "last_grow_report"),
            "cost_today": state.cost_summary_today(),
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
            "query_log": state.count("query_log"),
            "item_events": state.count("item_events"),
            "explicit_events": state.count("events", "ts_source='explicit'"),
            "trace": state.count("memory_trace"),
            "conv_log": state.count("conv_log"),
            "feedback": state.count("feedback_log"),
            "procedures": state.count("procedures"),
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
        try:
            from memory import convreview
            out["conv_review"] = convreview.report()  # 对话五维诊断（v33，只诊断不调参）
        except Exception:
            pass
        return out

    @app.get("/api/data/dump")
    def data_dump(request: Request):
        """全量数据 JSON 导出（只读，含用户数据表；索引类由 grow 重建）。"""
        require_role(request, "ops", "admin")
        return _db.dump_all()

    @app.post("/api/data/import")
    def data_import(req: DataImportRequest, request: Request):
        """Web 数据导入（高危）：必须 confirm=true，导入前写审计。"""
        require_role(request, "admin")
        state.check_rate(
            f"import:{request.client.host if request.client else 'unknown'}",
            limit=5, window=60,
        )
        if not req.confirm:
            raise HTTPException(400, "数据导入为高危操作，需要 confirm=true")
        if not req.data:
            raise HTTPException(400, "data 不能为空")
        _db.audit_add(
            "web.data_import", "import",
            f"tables={len(req.data)} replace={req.replace}",
            operator="web",
        )
        counts = _db.restore_all(req.data, replace=req.replace)
        return {"ok": True, "counts": counts}

    @app.post("/api/ops/notify")
    def notify(req: NotifyRequest, request: Request):
        """发送运维/告警播报（高危/易骚扰，必须 confirm=true）。"""
        require_role(request, "admin")
        state.check_rate(
            f"notify:{request.client.host if request.client else 'unknown'}",
            limit=5, window=60,
        )
        if not req.confirm:
            raise HTTPException(400, "发送播报需要 confirm=true")
        if not req.content.strip():
            raise HTTPException(400, "content 不能为空")
        from plugins import _capability
        result = _capability.notify_send(req.target_type, req.target, req.content)
        return {"ok": True, "result": result}

    @app.post("/api/tools")
    def run_tool(req: ToolRun, request: Request):
        require_role(request, "ops", "admin")
        state.check_rate(
            f"tools:{request.client.host if request.client else 'unknown'}",
            limit=20, window=60,
        )
        """运维动作白名单（诊断页按钮）。
        高危操作（污染/矛盾/日历 apply、清用户）必须 confirm=true 并写审计。"""
        _HIGH_RISK = {
            "pollution-scan-apply",
            "conflict-scan-apply",
            "calendar-check-apply",
            "memory-clear-user",
        }
        if req.name in _HIGH_RISK and not req.confirm:
            raise HTTPException(400, "高危操作需要 confirm=true")
        if req.name in _HIGH_RISK:
            require_role(request, "admin")
            _db.audit_add(
                "web.high_risk", req.name,
                f"uid={req.uid} scope={req.scope}",
                operator="web",
            )
        if req.name == "appointment-clean":
            from memory import appointment
            return appointment.clean()
        if req.name == "memory-source-backfill":
            return _db.memory_source_normalize()
        if req.name == "pollution-scan-apply":
            import tools as tools_mod
            return tools_mod.cmd_pollution_scan(req.scope, apply=True)
        if req.name == "conflict-scan-apply":
            import tools as tools_mod
            return tools_mod.cmd_conflict_scan(req.scope, apply=True)
        if req.name == "calendar-check-apply":
            import tools as tools_mod
            return tools_mod.cmd_calendar_check(req.scope, apply=True)
        if req.name == "memory-clear-user":
            if not req.uid:
                raise HTTPException(400, "memory-clear-user 需要 uid")
            import tools as tools_mod
            return tools_mod.cmd_memory_clear_user(req.uid)
        raise HTTPException(400, f"未知工具：{req.name}")

    @app.get("/public")
    def public_page():
        return FileResponse(STATIC_DIR / "public.html")

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")
