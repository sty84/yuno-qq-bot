#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""定时运维入口（cron 用）：备份 + 健康检查 + PG 故障守护。

示例 crontab：
  0 3 * * * cd /path/to/qq-bot && ./venv/bin/python scripts/ops_cron.py >> /var/log/yuno_ops_cron.log 2>&1
"""
import os
import sys

WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WS not in sys.path:
    sys.path.insert(0, WS)

from tools.admin import cmd_backup, cmd_health, cmd_pg_guard  # noqa: E402


def main() -> int:
    lines = []
    exit_code = 0

    try:
        lines.append(cmd_backup())
    except Exception as e:
        lines.append(f"备份失败：{e}")
        exit_code = 1

    try:
        code, text = cmd_health(notify=True)
        lines.append(text)
        exit_code = exit_code or code
    except Exception as e:
        lines.append(f"健康检查失败：{e}")
        exit_code = 1

    try:
        code, text = cmd_pg_guard(notify=True)
        lines.append(text)
        exit_code = exit_code or code
    except Exception as e:
        lines.append(f"PG 守护失败：{e}")
        exit_code = 1

    print("\n".join(lines))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
