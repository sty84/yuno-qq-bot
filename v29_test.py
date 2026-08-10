# -*- coding: utf-8 -*-
"""v29 验收：用户记忆不被 AI 自述污染
1) 提取 prompt 明确"只提取用户说的话"
2) 用户 scope 里"机器人…"开头的自述事实被过滤，用户事实保留
"""
import os
import sys

TEST_DIR = r"C:\Users\STY\.codex\visualizations\2026\08\07\019fda20-33c8-7031-851a-541c0036115a\yuno_mem_test"
os.environ["CONFIG_PATH"] = os.path.join(TEST_DIR, "config_v22.json")
sys.path.insert(0, TEST_DIR)
sys.path.insert(0, r"C:\Users\STY\Desktop\qq-bot-github")

import memory  # noqa: E402
from memory import extract  # noqa: E402
from plugins import _db  # noqa: E402

checks = []


def check(name, cond, extra=""):
    checks.append(bool(cond))
    print(("PASS" if cond else "FAIL"), name, extra if not cond else "")


# 1) prompt 指令
check(
    "污染-prompt有指令",
    "只提取用户说的话" in extract.EXTRACT_SYSTEM_PROMPT
    and "只提取用户说的话" in extract.STRUCTURED_EXTRACT_PROMPT,
)

# 2) 确定性过滤
_db.memory_clear("c2c:v29")
memory.ingest(
    "c2c:v29", "", "测试", "好的",
    facts=["机器人只会带半个坐垫", "用户养了一只橘猫", "YUNO 负责带能量饮料"],
)
rows = [r["fact"] for r in _db.memory_rows("c2c:v29")]
check("污染-机器人开头被过滤", "机器人只会带半个坐垫" not in rows, rows)
check("污染-YUNO开头被过滤", "YUNO 负责带能量饮料" not in rows, rows)
check("污染-用户事实保留", "用户养了一只橘猫" in rows, rows)

failed = [i for i, c in enumerate(checks) if not c]
print("\nRESULT:", "ALL PASS" if not failed else f"FAILED #{failed}", f"({len(checks)} checks)")
sys.exit(1 if failed else 0)
