#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""轻量密钥扫描：扫描 git 跟踪的源码文件，发现疑似密钥/私钥/云凭证即失败。

用于 CI 门禁，避免把真实密钥提交进仓库。
"""
import re
import subprocess
import sys
from pathlib import Path

WS = Path(__file__).resolve().parent.parent

# 常见密钥特征
PATTERNS = [
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS Access Key"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "OpenAI/常见 sk- 密钥"),
    (re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"), "私钥"),
    (re.compile(r"(?i)password\s*[:=]\s*['\"][^'\"]{8,}['\"]"), "硬编码密码"),
    (re.compile(r"(?i)secret\s*[:=]\s*['\"][^'\"]{8,}['\"]"), "硬编码 secret"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "GitHub Token"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "Slack Token"),
]

# 允许出现占位符/示例的文件
ALLOW_SUFFIX = {".example", ".md", ".json"}
ALLOW_PARTS = {".env.example", "docs/", "tests/"}


def main() -> int:
    files = subprocess.check_output(
        ["git", "ls-files"], cwd=WS, text=True
    ).splitlines()
    hits = []
    for rel in files:
        p = WS / rel
        if not p.is_file():
            continue
        if any(part in rel for part in ALLOW_PARTS):
            continue
        if p.suffix in ALLOW_SUFFIX:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for rx, name in PATTERNS:
                if rx.search(line):
                    hits.append((rel, lineno, name, line.strip()[:120]))
    if hits:
        print("密钥扫描失败，发现疑似密钥：")
        for rel, lineno, name, snippet in hits:
            print(f"  {rel}:{lineno} [{name}] {snippet}")
        return 1
    print("密钥扫描通过：未发现疑似密钥。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
