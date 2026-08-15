#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从评分清单批量提交评分（数据闭环）。

用法：
  1) 在 docs/评分清单-20260814.md 里，对要评的轨迹行尾追加 5 个空格分隔的数字：
       - `6` | conf 0.5 | 我是你们乐队新来的经纪人助手 | 4 4 4 4 5
     （五维顺序：extraction decision confidence provenance privacy，均 1~5）
  2) python review_from_checklist.py docs/评分清单-20260814.md
  3) python tools.py memory-trace-adjust   # 看评分是否在驱动参数
"""

import re
import sys

LINE_RE = re.compile(r"- `(\d+)`.*?\|\s*([1-5])\s+([1-5])\s+([1-5])\s+([1-5])\s+([1-5])\s*$")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "docs/评分清单-20260814.md"
    try:
        text = open(path, encoding="utf-8").read()
    except FileNotFoundError:
        print(f"找不到文件：{path}")
        return 1
    pending = []
    for line in text.splitlines():
        m = LINE_RE.match(line)
        if m:
            trace_id, e, d, c, p, pr = m.groups()
            pending.append((int(trace_id), e, d, c, p, pr))
    if not pending:
        print("清单里没有带分数的行。请在轨迹行尾追加 `| 4 4 4 4 5` 这样的 5 个分数。")
        return 1
    print(f"待提交 {len(pending)} 条评分…")
    import subprocess
    ok = 0
    for trace_id, e, d, c, p, pr in pending:
        r = subprocess.run(
            [sys.executable, "tools.py", "memory-trace-review", str(trace_id),
             "--extraction", e, "--decision", d, "--confidence", c,
             "--provenance", p, "--privacy", pr],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            ok += 1
        else:
            print(f"  ID {trace_id} 失败: {r.stderr.strip()[:80]}")
    print(f"完成：{ok}/{len(pending)} 条已提交（重复提交会覆盖旧评分）")


if __name__ == "__main__":
    sys.exit(main())
