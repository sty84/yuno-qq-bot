"""YUNO 2.0 运维/入口工具（薄入口，实现已拆分到 tools/ 包）。

用法：
  python tools.py health [--notify]       # 独立健康检查（cron 用）
  python tools.py backup                    # 每日 SQLite 备份（保留 7 份）
  python tools.py recover [--notify]        # 一键恢复 services 注册表中未运行的服务
  python tools.py character 千石由乃          # 生成人物档案入记忆 + docs/characters/<名>.md
  python tools.py character-sync 千石由乃    # 把编辑后的 md 档案同步回记忆库（或传文件路径）
  python tools.py mcp                       # 启动 MCP Server（需 mcp SDK）
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tools.main import main

if __name__ == "__main__":
    sys.exit(main())
