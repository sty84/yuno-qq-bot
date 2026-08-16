"""鉴权：Bearer token、会话登录、CSRF 与只读权限。"""

import os
import time
import uuid

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel


class LoginRequest(BaseModel):
    password: str


def install_auth(app, state):
    """安装可选 token 中间件和登录路由。"""
    web_token = os.getenv("YUNO_WEB_TOKEN", "")

    # 可选鉴权（P1-3）：设置 YUNO_WEB_TOKEN 后所有请求需带 Authorization: Bearer <token>；
    # 未设置则不鉴权（默认 127.0.0.1 本机使用，向后兼容；公网暴露时务必设置）
    if web_token:
        @app.middleware("http")
        async def _require_web_token(request: Request, call_next):
            # 公开统计接口与公开页不鉴权，供只读展示页使用
            if request.url.path.startswith("/api/public/") or request.url.path == "/public" or request.url.path == "/api/auth/login":
                return await call_next(request)
            # 基础 CSRF：非 GET 请求若带 Origin，必须与 Host 一致
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                origin = request.headers.get("origin")
                if origin:
                    from urllib.parse import urlparse
                    parsed = urlparse(origin)
                    if parsed.netloc != request.headers.get("host", ""):
                        return JSONResponse({"detail": "跨域请求被拒绝"}, status_code=403)
            auth = request.headers.get("Authorization", "")
            token = auth[7:] if auth.startswith("Bearer ") else ""
            role = None
            if token == web_token:
                role = "admin"
            elif token in state.sessions and state.sessions[token]["exp"] > time.time():
                role = state.sessions[token]["role"]
            if role is None:
                return JSONResponse({"detail": "未授权"}, status_code=401)
            request.state.role = role
            if request.method not in ("GET", "HEAD", "OPTIONS") and role != "admin":
                return JSONResponse({"detail": "需要管理员权限"}, status_code=403)
            return await call_next(request)

    @app.post("/api/auth/login")
    def login(req: LoginRequest):
        """使用管理密码或只读密码换取短期会话 token。"""
        admin_pw = os.getenv("YUNO_WEB_PASSWORD", "")
        readonly_pw = os.getenv("YUNO_WEB_READONLY_PASSWORD", "")
        if admin_pw and req.password == admin_pw:
            role = "admin"
        elif readonly_pw and req.password == readonly_pw:
            role = "readonly"
        else:
            raise HTTPException(401, "密码错误")
        token = uuid.uuid4().hex
        state.sessions[token] = {"exp": time.time() + state.session_ttl, "role": role}
        return {"token": token, "role": role, "expires_in": state.session_ttl}
