"""yuno-memory FastAPI 服务：任意语言 / Agent 通过 HTTP 接入记忆系统。

启动：
  python -m yuno_memory --host 127.0.0.1 --port 8457 --data-dir ./data

鉴权（P1-3）：设置 YUNO_API_TOKEN（或 --token）后，所有请求需带
  Authorization: Bearer <token>；未设置则不鉴权（默认本机使用，向后兼容）。
"""

import os
import pathlib

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .core import Memory

_memory = None


def _safe_export_path(out: str, data_dir) -> str:
    """export 路径白名单：空=默认（data_dir 内）；相对路径拼 data_dir；
    解析后必须在 data_dir 内，否则拒绝（防任意写盘）。"""
    if not out:
        return ""
    p = pathlib.Path(out)
    if not p.is_absolute():
        p = pathlib.Path(data_dir) / p
    try:
        p = p.resolve()
        if not p.is_relative_to(pathlib.Path(data_dir).resolve()):
            raise HTTPException(400, "export 路径必须在 data_dir 内")
    except (OSError, ValueError):
        raise HTTPException(400, "export 路径非法")
    return str(p)


def _bearer_middleware(token: str):
    async def _require_token(request: Request, call_next):
        if request.headers.get("Authorization", "") != f"Bearer {token}":
            return JSONResponse({"detail": "未授权"}, status_code=401)
        return await call_next(request)
    return _require_token


def init_memory(config=None, data_dir=None, api_key=None, base_url=None, model=None, embedder=None, persona=None):
    global _memory
    if _memory is None:
        _memory = Memory(
            config=config, data_dir=data_dir, api_key=api_key,
            base_url=base_url, model=model, embedder=embedder, persona=persona,
        )
    return _memory


def get_memory() -> Memory:
    if _memory is None:
        raise HTTPException(503, "记忆服务未初始化（先调用 init_memory）")
    return _memory


class IngestRequest(BaseModel):
    scope: str
    text: str
    reply: str = ""
    facts: list | None = None
    confidence: float | None = None


class SearchRequest(BaseModel):
    query: str
    scopes: list[str]
    top_k: int = 5
    min_score: float = 0.25
    detailed: bool = False


class ReviewRequest(BaseModel):
    trace_id: int
    scores: dict = {}
    comment: str = ""
    reviewer: str = "api"


class GoalRequest(BaseModel):
    action: str  # add | update | done
    scope: str
    title: str
    priority: int = 3
    motivation: str = ""
    confidence: float = 0.7
    status: str | None = None
    progress: float | None = None


class ConsultRequest(BaseModel):
    scope: str
    text: str


class EvalRequest(BaseModel):
    probes: list
    k: int = 5


def create_app(memory: Memory | None = None, token: str = "") -> FastAPI:
    global _memory
    if memory is not None:
        _memory = memory
    app = FastAPI(title="Yuno Memory API", version="1.0.0")

    token = token or os.getenv("YUNO_API_TOKEN", "")
    if token:
        app.middleware("http")(_bearer_middleware(token))

    @app.get("/health")
    def health():
        return {"status": "ok", "memory": get_memory().stats()}

    @app.post("/memory/ingest")
    def ingest(req: IngestRequest):
        return get_memory().ingest(req.scope, req.text, req.reply, req.facts, req.confidence)

    @app.post("/memory/search")
    def search(req: SearchRequest):
        return get_memory().search(req.query, req.scopes, req.top_k, req.min_score, req.detailed)

    @app.get("/memory/trace")
    def trace(scope: str = "", limit: int = 50):
        return get_memory().trace(scope or None, limit)

    @app.post("/memory/review")
    def review(req: ReviewRequest):
        return get_memory().review(req.trace_id, req.scores, req.comment, req.reviewer)

    @app.get("/memory/adjust")
    def adjust():
        return get_memory().adjustments()

    @app.get("/memory/goals")
    def goals(scope: str = ""):
        return get_memory().goals(scope or None)

    @app.post("/memory/goal")
    def goal(req: GoalRequest):
        m = get_memory()
        if req.action == "add":
            return m.goal_add(req.scope, req.title, req.priority, req.motivation, req.confidence)
        if req.action == "done":
            return m.goal_update(req.scope, req.title, status="done")
        if req.action == "update":
            return m.goal_update(req.scope, req.title, status=req.status, progress=req.progress)
        raise HTTPException(400, "action 只能是 add/update/done")

    @app.post("/consult")
    def consult(req: ConsultRequest):
        return get_memory().consult(req.scope, req.text)

    @app.post("/memory/eval")
    def eval_probes(req: EvalRequest):
        return get_memory().eval(req.probes, req.k)

    @app.post("/memory/export")
    def export(out: str = ""):
        m = get_memory()
        safe = _safe_export_path(out, m.data_dir)
        return {"path": m.export(safe or None)}

    return app


app = create_app()
