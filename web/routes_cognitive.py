"""认知架构相关接口。"""

from fastapi import Request
from pydantic import BaseModel

from web.auth import require_role


class CognitiveRunRequest(BaseModel):
    query: str
    scope: str = ""


def register(app, state):
    @app.post("/api/cognitive/run")
    def cognitive_run(req: CognitiveRunRequest, request: Request):
        """运行标准化认知架构：决策 + 记忆检索 + 动作描述。"""
        require_role(request, "ops", "admin")
        state.check_rate(
            f"cognitive:{request.client.host if request.client else 'unknown'}",
            limit=20, window=60,
        )
        from memory.interfaces import default_architecture
        return default_architecture().run_to_dict(req.query, req.scope)
