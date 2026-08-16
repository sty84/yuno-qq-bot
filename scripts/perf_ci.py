#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""性能基准门禁：跑 load_test 200 并发，超过阈值即失败。"""
import os
import subprocess
import sys
import time
from pathlib import Path

WS = Path(__file__).resolve().parent.parent
THRESHOLD_SECONDS = float(os.getenv("YUNO_PERF_THRESHOLD_SECONDS", "5"))


def main() -> int:
    env = os.environ.copy()
    env.setdefault("YUNO_DB_BACKEND", "sqlite")
    start = time.time()
    proc = subprocess.run(
        [sys.executable, "load_test.py", "200"],
        cwd=WS,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    elapsed = time.time() - start
    print(proc.stdout)
    if proc.stderr:
        print(proc.stderr)
    print(f"\n性能基准：200 并发耗时 {elapsed:.2f}s（阈值 {THRESHOLD_SECONDS}s）")
    if proc.returncode != 0:
        print("性能测试失败：load_test 返回非零")
        return 1
    if elapsed > THRESHOLD_SECONDS:
        print("性能门禁失败：超过阈值")
        return 1
    print("性能门禁通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
