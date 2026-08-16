"""Yuno 评测管理台后端（v2.3）：把散在 tools.py 的评测/维护命令包成 HTTP。

启动：
  python webapp.py                    # 默认 127.0.0.1:8600（只本机访问）
  python webapp.py --host 0.0.0.0     # 公网暴露（需自行加 nginx 密码/TLS）

设计：进程内直接调 memory/ 函数（薄壳，不改现有逻辑）；
任务用 task_id + 2 秒轮询；eval 并发上限 2；消融/回放为后续轮次。

本文件现为薄入口/门面：实际 app 与路由在 web/ 包中实现，import webapp
仍可得到 webapp.app，并保持原请求模型/鉴权/路由行为不变。
"""

import argparse

from web.app import MAX_CONCURRENT, _apply_light_config, create_app
from web.auth import LoginRequest as LoginRequest
from web.routes_admin import DataImportRequest as DataImportRequest
from web.routes_admin import NotifyRequest as NotifyRequest
from web.routes_admin import ToolRun as ToolRun
from web.routes_cognitive import CognitiveRunRequest as CognitiveRunRequest
from web.routes_eval import AblationRun as AblationRun
from web.routes_eval import AblationToggle as AblationToggle
from web.routes_eval import ConvReviewSubmit as ConvReviewSubmit
from web.routes_eval import ReplayRequest as ReplayRequest
from web.routes_eval import ReviewSubmit as ReviewSubmit
from web.routes_eval import ScoreRequest as ScoreRequest
from web.routes_eval import TaskRequest as TaskRequest

# 保持原 webapp 模块级副作用：import webapp 时按原顺序应用轻量配置。
_apply_light_config()

app = create_app()


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
