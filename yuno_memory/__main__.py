"""python -m yuno_memory --host 127.0.0.1 --port 8457 [--data-dir ...]"""

import argparse
import os

import uvicorn


def main():
    p = argparse.ArgumentParser(description="yuno-memory 记忆服务")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8457)
    p.add_argument("--config", default="", help="配置 JSON 路径")
    p.add_argument("--data-dir", default="", help="数据目录（默认 ./data）")
    p.add_argument("--api-key", default="", help="LLM API key")
    p.add_argument("--base-url", default="https://api.deepseek.com")
    p.add_argument("--model", default="deepseek-chat")
    p.add_argument("--embedder", default="", help="local 或 openai_compatible")
    p.add_argument("--persona", default="", help="人设文本路径")
    p.add_argument("--token", default="", help="Bearer token（默认读 YUNO_API_TOKEN；空=不鉴权）")
    args = p.parse_args()
    if args.token:
        # server 模块 import 时创建 app，须在 import 前注入环境变量
        os.environ["YUNO_API_TOKEN"] = args.token
    from .server import app, init_memory
    init_memory(
        config=args.config or None,
        data_dir=args.data_dir or None,
        api_key=args.api_key or None,
        base_url=args.base_url,
        model=args.model,
        embedder=args.embedder or None,
        persona=args.persona or None,
    )
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
