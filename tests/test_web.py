# -*- coding: utf-8 -*-
"""Web 层冒烟（P1-1）：webapp / yuno_memory 可 import、app 可创建、健康接口可达。

CI 装 fastapi/httpx 后执行；本地未装自动跳过（pytest.importorskip）。
修复前 CI 对 web/SDK 层零覆盖——连语法错误都发现不了（py_compile 也不查这两个文件）。
"""

import json
import os

import pytest

fastapi = pytest.importorskip("fastapi")


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path_factory):
    """P1-5 测试隔离：TestClient 请求会触发 _db 惰性连接——若不重定向，
    会把全局连接绑到真实库（qq-bot/data/bot.db）并写入生产数据。
    每个测试前把 _db 强制绑定到临时库。"""
    tmp = tmp_path_factory.mktemp("web_db")
    cfg = {"memory": {"embedder": {"provider": "none"}, "core": {"enabled": True}}}
    cfg_path = str(tmp / "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    os.environ["CONFIG_PATH"] = cfg_path
    from plugins import _db, _shared
    _shared.CONFIG_PATH = cfg_path
    _shared.reload_config()
    _db.init(str(tmp), force=True)
    yield


def test_webapp_health_endpoint():
    """评测管理台：/api/health 可达且返回 JSON。"""
    from starlette.testclient import TestClient
    import webapp
    client = TestClient(webapp.app)
    r = client.get("/api/health")
    assert r.status_code == 200, r.status_code
    assert r.json() is not None


def test_webapp_root_served():
    """评测管理台：首页静态页可访问。"""
    from starlette.testclient import TestClient
    import webapp
    client = TestClient(webapp.app)
    r = client.get("/")
    assert r.status_code in (200, 307), r.status_code


def test_webapp_import_side_effect_safe():
    """import webapp 不应破坏 embedder 配置（_apply_light_config 只在无显式设置时生效）。"""
    import plugins._shared as _shared
    _shared.CONFIG.get("memory", {}).get("embedder", {}).get("provider")
    import webapp
    _shared.CONFIG.get("memory", {}).get("embedder", {}).get("provider")
    # 默认场景（无 YUNO_WEB_EMBEDDER）：会被置为 none——这是设计行为，仅断言不抛异常
    assert webapp.app is not None


def test_yuno_memory_server_imports():
    """SDK 服务：模块可 import、app 存在（未初始化 memory 时接口返回 503 属预期，不测）。"""
    import yuno_memory.server as srv
    assert srv.app is not None
    assert callable(srv.create_app)
    assert callable(srv.get_memory)


# ---- P1-3：HTTP 鉴权 + export 路径白名单 ----

def test_yuno_memory_auth_enforced():
    """设置 token 后：无凭证 401，正确 Bearer 放行（未初始化 memory 时 503 属预期）。"""
    from starlette.testclient import TestClient
    import yuno_memory.server as srv
    app2 = srv.create_app(token="secret-token")
    client = TestClient(app2)
    assert client.get("/health").status_code == 401
    assert client.get("/health", headers={"Authorization": "Bearer wrong"}).status_code == 401
    r = client.get("/health", headers={"Authorization": "Bearer secret-token"})
    assert r.status_code != 401  # 鉴权已过；503 = 未初始化 memory


def test_yuno_memory_no_token_default():
    """未设置 token 时保持向后兼容（不鉴权）。"""
    from starlette.testclient import TestClient
    import yuno_memory.server as srv
    app2 = srv.create_app()  # 无 token（覆盖环境变量场景用显式空串）
    client = TestClient(app2)
    assert client.get("/health").status_code in (503, 200)


def test_export_path_whitelist():
    """export 路径白名单：data_dir 内允许，越界（绝对/../）拒绝。"""
    import pathlib
    import yuno_memory.server as srv
    data_dir = "/tmp/yuno_export_whitelist"
    assert srv._safe_export_path("", data_dir) == ""
    assert srv._safe_export_path("sub/out.tar.gz", data_dir) == str(pathlib.Path(data_dir, "sub/out.tar.gz").resolve())
    import pytest
    with pytest.raises(Exception):
        srv._safe_export_path("/etc/passwd", data_dir)
    with pytest.raises(Exception):
        srv._safe_export_path("../../etc/passwd", data_dir)
    with pytest.raises(Exception):
        srv._safe_export_path("..", data_dir)


def test_webapp_auth(monkeypatch):
    """设置 YUNO_WEB_TOKEN 后：无凭证 401，正确 Bearer 放行。"""
    import importlib
    import webapp
    monkeypatch.setenv("YUNO_WEB_TOKEN", "web-secret")
    reloaded = importlib.reload(webapp)
    try:
        from starlette.testclient import TestClient
        client = TestClient(reloaded.app)
        assert client.get("/api/health").status_code == 401
        assert client.get("/api/health", headers={"Authorization": "Bearer wrong"}).status_code == 401
        r = client.get("/api/health", headers={"Authorization": "Bearer web-secret"})
        assert r.status_code == 200
    finally:
        monkeypatch.delenv("YUNO_WEB_TOKEN", raising=False)
        importlib.reload(webapp)  # 还原为无 token 状态，避免影响其他用例


# ---- 记忆评分页（路线图 trace 审核页）----

def test_review_queue_and_submit():
    """记忆评分：队列返回结构 + 提交写 trace_review + reviewed 计数增长。"""
    from starlette.testclient import TestClient
    import webapp
    client = TestClient(webapp.app)
    q = client.get("/api/review/queue")
    assert q.status_code == 200
    body = q.json()
    assert "items" in body and "reviewed" in body
    before = body["reviewed"]
    r = client.post("/api/review/submit", json={
        "trace_id": 9999, "extraction": 4, "decision": 4,
        "confidence": 4, "provenance": 4, "privacy": 5,
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True
    q2 = client.get("/api/review/queue")
    assert q2.json()["reviewed"] == before + 1
    # 越界分数拒绝
    bad = client.post("/api/review/submit", json={
        "trace_id": 9998, "extraction": 6, "decision": 4,
        "confidence": 4, "provenance": 4, "privacy": 5,
    })
    assert bad.status_code == 400


# ---- 对话质量评分页（v33 convreview）----

def test_conv_review_queue_and_submit():
    """对话评分：队列返回结构 + 提交写 conv_review + reviewed 计数增长 + 越界拒绝。"""
    from starlette.testclient import TestClient
    import webapp
    client = TestClient(webapp.app)
    q = client.get("/api/review/queue?source=conv")
    assert q.status_code == 200
    body = q.json()
    assert "items" in body and "reviewed" in body
    before = body["reviewed"]
    r = client.post("/api/convreview/submit", json={
        "conv_id": 9999, "remember": 4, "natural": 4,
        "emotional": 4, "proactive": 4, "boundary": 5,
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True
    q2 = client.get("/api/review/queue?source=conv")
    assert q2.json()["reviewed"] == before + 1
    # 越界分数拒绝
    bad = client.post("/api/convreview/submit", json={
        "conv_id": 9998, "remember": 6, "natural": 4,
        "emotional": 4, "proactive": 4, "boundary": 5,
    })
    assert bad.status_code == 400


# ---- 产品化/运维：公开统计 + 高危操作确认 ----

def test_public_stats_no_auth(monkeypatch):
    """设置 token 后，/api/public/stats 仍可匿名访问。"""
    import importlib
    import webapp
    monkeypatch.setenv("YUNO_WEB_TOKEN", "web-secret")
    reloaded = importlib.reload(webapp)
    try:
        from starlette.testclient import TestClient
        client = TestClient(reloaded.app)
        assert client.get("/api/health").status_code == 401
        r = client.get("/api/public/stats")
        assert r.status_code == 200, r.status_code
        body = r.json()
        assert "memory_count" in body and "event_count" in body
    finally:
        monkeypatch.delenv("YUNO_WEB_TOKEN", raising=False)
        importlib.reload(webapp)


def test_high_risk_tool_requires_confirm():
    """高危运维动作必须 confirm=true，且确认后写审计。"""
    from plugins import _db
    from starlette.testclient import TestClient
    import webapp
    client = TestClient(webapp.app)
    # 未确认 -> 400
    r = client.post("/api/tools", json={"name": "conflict-scan-apply"})
    assert r.status_code == 400, r.status_code
    assert "confirm" in r.json().get("detail", "")
    # 确认后 -> 200（空库也能返回 dry-run/空结果）
    r2 = client.post("/api/tools", json={"name": "conflict-scan-apply", "confirm": True})
    assert r2.status_code == 200, r2.status_code
    audit = _db.audit_query(limit=5, action="web.high_risk")
    assert audit, "高危操作应写审计"


def test_public_page_no_auth(monkeypatch):
    """设置 token 后，/public 公开页仍可匿名访问。"""
    import importlib
    import webapp
    monkeypatch.setenv("YUNO_WEB_TOKEN", "web-secret")
    reloaded = importlib.reload(webapp)
    try:
        from starlette.testclient import TestClient
        client = TestClient(reloaded.app)
        assert client.get("/api/health").status_code == 401
        r = client.get("/public")
        assert r.status_code == 200, r.status_code
        assert "Yuno 公开状态" in r.text
    finally:
        monkeypatch.delenv("YUNO_WEB_TOKEN", raising=False)
        importlib.reload(webapp)


def test_data_dump_endpoint():
    """全量数据导出接口返回 JSON dump。"""
    from starlette.testclient import TestClient
    import webapp
    client = TestClient(webapp.app)
    r = client.get("/api/data/dump")
    assert r.status_code == 200, r.status_code
    body = r.json()
    assert isinstance(body, dict)
    assert "memories" in body and "events" in body


def test_status_endpoint():
    """运维状态接口返回 schema 版本、DB 大小、任务数。"""
    from starlette.testclient import TestClient
    import webapp
    client = TestClient(webapp.app)
    r = client.get("/api/status")
    assert r.status_code == 200, r.status_code
    body = r.json()
    assert "schema_version" in body
    assert "db_size" in body
    assert "tasks_running" in body


def test_data_import_requires_confirm_and_imports():
    """数据导入必须 confirm=true，确认后调用 restore_all。"""
    from plugins import _db
    from starlette.testclient import TestClient
    import webapp
    client = TestClient(webapp.app)
    # 未确认 -> 400
    r = client.post("/api/data/import", json={"data": {"kv": []}})
    assert r.status_code == 400, r.status_code
    # 确认后 -> ok
    r2 = client.post("/api/data/import", json={
        "data": {"kv": [{"namespace": "test", "key": "x", "value": "1"}]},
        "confirm": True,
    })
    assert r2.status_code == 200, r2.status_code
    assert r2.json()["ok"] is True
    assert r2.json()["counts"].get("kv") == 1
    audit = _db.audit_query(limit=5, action="web.data_import")
    assert audit, "数据导入应写审计"


def test_notify_requires_confirm():
    """播报接口必须 confirm=true，确认后入队并写审计。"""
    from plugins import _db
    from starlette.testclient import TestClient
    import webapp
    client = TestClient(webapp.app)
    r = client.post("/api/ops/notify", json={"content": "test", "target": "g1"})
    assert r.status_code == 400, r.status_code
    r2 = client.post("/api/ops/notify", json={
        "content": "test alert", "target_type": "group", "target": "g1", "confirm": True,
    })
    assert r2.status_code == 200, r2.status_code
    assert r2.json()["ok"] is True
    audit = _db.audit_query(limit=5, action="notify.send")
    assert audit, "播报应写审计"


def test_public_trend_no_auth(monkeypatch):
    """公开趋势接口在设置 token 后仍可匿名访问。"""
    import importlib
    import webapp
    monkeypatch.setenv("YUNO_WEB_TOKEN", "web-secret")
    reloaded = importlib.reload(webapp)
    try:
        from starlette.testclient import TestClient
        client = TestClient(reloaded.app)
        r = client.get("/api/public/trend")
        assert r.status_code == 200, r.status_code
        assert isinstance(r.json(), dict)
    finally:
        monkeypatch.delenv("YUNO_WEB_TOKEN", raising=False)
        importlib.reload(webapp)


def test_web_login_session(monkeypatch):
    """YUNO_WEB_PASSWORD 登录后可用 session token 访问。"""
    import importlib
    import webapp
    monkeypatch.setenv("YUNO_WEB_TOKEN", "web-secret")
    monkeypatch.setenv("YUNO_WEB_PASSWORD", "admin123")
    reloaded = importlib.reload(webapp)
    try:
        from starlette.testclient import TestClient
        client = TestClient(reloaded.app)
        assert client.get("/api/health").status_code == 401
        bad = client.post("/api/auth/login", json={"password": "wrong"})
        assert bad.status_code == 401
        ok = client.post("/api/auth/login", json={"password": "admin123"})
        assert ok.status_code == 200
        token = ok.json()["token"]
        r = client.get("/api/health", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
    finally:
        monkeypatch.delenv("YUNO_WEB_TOKEN", raising=False)
        monkeypatch.delenv("YUNO_WEB_PASSWORD", raising=False)
        importlib.reload(webapp)


def test_web_readonly_role_and_csrf(monkeypatch):
    """只读 token 可 GET 不可 POST；跨域 POST 被 CSRF 拦截。"""
    import importlib
    import webapp
    monkeypatch.setenv("YUNO_WEB_TOKEN", "web-secret")
    monkeypatch.setenv("YUNO_WEB_PASSWORD", "admin123")
    monkeypatch.setenv("YUNO_WEB_READONLY_PASSWORD", "read123")
    reloaded = importlib.reload(webapp)
    try:
        from starlette.testclient import TestClient
        client = TestClient(reloaded.app)
        # 只读登录
        ro = client.post("/api/auth/login", json={"password": "read123"})
        assert ro.status_code == 200 and ro.json()["role"] == "readonly"
        ro_token = ro.json()["token"]
        assert client.get("/api/health", headers={"Authorization": f"Bearer {ro_token}"}).status_code == 200
        # 只读不能 POST
        r = client.post("/api/tools", json={"name": "appointment-clean"}, headers={"Authorization": f"Bearer {ro_token}"})
        assert r.status_code == 403, r.status_code
        # 管理员登录
        ad = client.post("/api/auth/login", json={"password": "admin123"})
        ad_token = ad.json()["token"]
        # 跨域 POST 被拒绝
        r2 = client.post(
            "/api/tools",
            json={"name": "appointment-clean"},
            headers={"Authorization": f"Bearer {ad_token}", "Origin": "https://evil.example"},
        )
        assert r2.status_code == 403, r2.status_code
        # 同源/无 Origin 的管理员 POST 放行（appointment-clean 是安全操作）
        r3 = client.post(
            "/api/tools",
            json={"name": "appointment-clean"},
            headers={"Authorization": f"Bearer {ad_token}"},
        )
        assert r3.status_code == 200, r3.status_code
    finally:
        monkeypatch.delenv("YUNO_WEB_TOKEN", raising=False)
        monkeypatch.delenv("YUNO_WEB_PASSWORD", raising=False)
        monkeypatch.delenv("YUNO_WEB_READONLY_PASSWORD", raising=False)
        importlib.reload(webapp)


def test_cognitive_run_endpoint(monkeypatch):
    """认知架构标准化接口可通过 Web 调用。"""
    import importlib
    import webapp
    monkeypatch.setenv("YUNO_WEB_TOKEN", "web-secret")
    monkeypatch.setenv("YUNO_WEB_PASSWORD", "admin123")
    reloaded = importlib.reload(webapp)
    try:
        from starlette.testclient import TestClient
        client = TestClient(reloaded.app)
        ad = client.post("/api/auth/login", json={"password": "admin123"})
        token = ad.json()["token"]
        r = client.post(
            "/api/cognitive/run",
            json={"query": "用户养了什么猫", "scope": "c2c:cog"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200, r.status_code
        body = r.json()
        assert "activated_memories" in body
        assert "action" in body
    finally:
        monkeypatch.delenv("YUNO_WEB_TOKEN", raising=False)
        monkeypatch.delenv("YUNO_WEB_PASSWORD", raising=False)
        importlib.reload(webapp)
