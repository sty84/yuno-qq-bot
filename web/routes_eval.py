"""评测/消融/回放/评分相关接口。"""

import json
import pathlib
import shutil

from fastapi import HTTPException
from pydantic import BaseModel

from plugins import _db, _shared

ROOT = pathlib.Path(__file__).resolve().parent.parent


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


class ReviewSubmit(BaseModel):
    """记忆评分提交（路线图 trace 审核页）：五维均 1~5。"""
    trace_id: int
    extraction: float
    decision: float
    confidence: float
    provenance: float
    privacy: float
    comment: str = ""


class ConvReviewSubmit(BaseModel):
    """对话评分提交（v33 对话质量审核页）：五维均 1~5。"""
    conv_id: int
    remember: float
    natural: float
    emotional: float
    proactive: float
    boundary: float
    comment: str = ""


def register(app, state):
    @app.get("/api/public/trend")
    def public_trend(limit: int = 50):
        """公开只读趋势：返回各类评测的历史指标（不含敏感数据）。"""
        hist = _db.kv_get("memory", "baseline_history") or []
        out = {}
        for kind in ("memory_eval", "space_eval", "time_eval", "emotion_eval", "subjects_eval"):
            items = [h for h in hist if h.get("kind") == kind][-max(1, int(limit)):]
            if items:
                out[kind] = items
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
            fn = state.task_fn(req.kind)
        except HTTPException:
            raise
        return {"task_id": state.submit(req.kind, fn)}

    @app.get("/api/tasks")
    def tasks():
        return list(state.tasks.values())

    @app.get("/api/tasks/{task_id}")
    def task(task_id: str):
        t = state.tasks.get(task_id)
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
        return {"task_id": state.submit("scenario_replay", fn)}

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
            probes = state.load_probes()
            return run_ablation(probes, names=req.switches or None)
        return {"task_id": state.submit("ablation", fn)}

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
        s["total_cost"] = state.money(s["total"]["prompt"], s["total"]["completion"])
        for d in s["by_day"]:
            d["cost"] = state.money(d["prompt"], d["completion"])
        for m in s["by_module"]:
            m["cost"] = state.money(m["prompt"], m["completion"])
        for q in s["by_path"]:
            q["cost"] = state.money(q["prompt"], q["completion"])
        pp, cp = state.cost_prices()
        s["prices"] = {"prompt_per_1m": pp, "completion_per_1m": cp}
        return s

    @app.get("/api/hesitation")
    def hesitation():
        """犹豫层统计 + 最近决策明细（管理台回看）。"""
        from memory import hesitation as hesitation_mod
        return {"stats": hesitation_mod.stats(), "recent": _db.hesitation_log_rows(30)}

    @app.get("/api/review/queue")
    def review_queue(limit: int = 20, source: str = "trace"):
        """评分待办：source=trace（记忆轨迹，默认）或 conv（对话质量 v33）。"""
        if source == "conv":
            from memory import convreview
            items = convreview.queue(limit)
            return {
                "items": [{
                    "id": r["id"],
                    "scope": r.get("scope"),
                    "conversation_id": r.get("conversation_id"),
                    "user_text": (r.get("user_text") or "")[:200],
                    "ai_text": (r.get("ai_text") or "")[:400],
                    "ts": r.get("ts"),
                } for r in items[: max(1, int(limit))]],
                "reviewed": len(_db.conv_review_recent(limit=1000)),
            }
        rows = _db.trace_rows(limit=300)
        reviewed = _db.trace_review_map([r["id"] for r in rows])
        TEST_MARK = ("guard", "ev:", "poke", "pdtest", "testg", "scenario", "c2c:t:", "c2c:priv")
        queue = [
            r for r in rows
            if r["id"] not in reviewed
            and r.get("memory_action") in ("create", "reject")
            and not any(k in str(r.get("scope", "")) for k in TEST_MARK)
        ]
        # create（存了什么）优先于 reject（该不该存）
        queue.sort(key=lambda r: 0 if r.get("memory_action") == "create" else 1)
        return {
            "items": [{
                "id": r["id"],
                "scope": r.get("scope"),
                "action": r.get("memory_action"),
                "content": (r.get("raw_content") or "")[:120],
                "confidence": r.get("confidence"),
                "reasoning": (r.get("reasoning") or "")[:60],
                "ts": r.get("ts"),
            } for r in queue[:max(1, int(limit))]],
            "reviewed": len(_db.trace_review_recent(limit=1000)),
        }

    @app.post("/api/review/submit")
    def review_submit(req: ReviewSubmit):
        """提交五维评分并刷新评分驱动缓存（confidence_factor/igt/privacy/extraction）。"""
        scores = {
            "extraction": float(req.extraction),
            "decision": float(req.decision),
            "confidence": float(req.confidence),
            "provenance": float(req.provenance),
            "privacy": float(req.privacy),
        }
        for k, v in scores.items():
            if not 1 <= v <= 5:
                raise HTTPException(400, f"{k} 必须在 1~5")
        avg = sum(scores.values()) / 5
        _db.trace_review_add(req.trace_id, round(avg, 2), scores, comment=req.comment or "", reviewer="web")
        try:
            from memory import trace
            trace.adjustments(force=True)  # 评分驱动参数立即生效
        except Exception as e:
            state.stats_err(e)
        return {"ok": True, "score": round(avg, 2), "reviews": len(_db.trace_review_recent(limit=1000))}

    @app.post("/api/convreview/submit")
    def convreview_submit(req: ConvReviewSubmit):
        """提交对话五维评分（v33）：写 conv_review + 低分审计归因（不自动调参）。"""
        scores = {
            "remember": float(req.remember),
            "natural": float(req.natural),
            "emotional": float(req.emotional),
            "proactive": float(req.proactive),
            "boundary": float(req.boundary),
        }
        for k, v in scores.items():
            if not 1 <= v <= 5:
                raise HTTPException(400, f"{k} 必须在 1~5")
        avg = sum(scores.values()) / 5
        from memory import convreview
        convreview.score(req.conv_id, scores, comment=req.comment or "", reviewer="web")
        return {"ok": True, "score": round(avg, 2), "reviews": len(_db.conv_review_recent(limit=1000))}
