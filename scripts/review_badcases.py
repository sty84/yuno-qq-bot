#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""badcase 审核合并流程。

默认只列出当前 badcase；
--merge 会把检索 badcase 合并进 eval/retrieval_probes.json（按 query+scope 去重），
然后清空 badcase 文件，作为人工审核后的扩集动作。
"""
import argparse
import json
import pathlib
import sys

WS = pathlib.Path(__file__).resolve().parent.parent
RETRIEVAL_PROBES = WS / "eval" / "retrieval_probes.json"
RETRIEVAL_BADCASES = WS / "eval" / "badcases" / "retrieval_badcases.jsonl"


def _load_badcases(path):
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="badcase 审核合并")
    parser.add_argument("--merge", action="store_true", help="把检索 badcase 合并进正式评测集")
    args = parser.parse_args()

    badcases = _load_badcases(RETRIEVAL_BADCASES)
    if not args.merge:
        print(f"当前检索 badcase：{len(badcases)} 条")
        for b in badcases:
            print(f"  [{b.get('category')}] {b.get('query')} -> {b.get('expected')}")
        return 0

    if not badcases:
        print("没有 badcase 可合并。")
        return 0

    data = json.loads(RETRIEVAL_PROBES.read_text(encoding="utf-8"))
    items = data.setdefault("items", [])
    seen = {(str(it.get("query")), str(it.get("scope"))) for it in items}
    added = 0
    for b in badcases:
        key = (str(b.get("query")), str(b.get("scope")))
        if key in seen:
            continue
        items.append({
            "query": b.get("query"),
            "expected": b.get("expected", []),
            "scope": b.get("scope"),
            "category": b.get("category", "其他"),
        })
        seen.add(key)
        added += 1
    data["items"] = items
    data["说明"] = f"检索命中率评测集（含人工审核 badcase）：{len(items)} 条"
    RETRIEVAL_PROBES.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    RETRIEVAL_BADCASES.write_text("", encoding="utf-8")
    print(f"已合并 {added} 条 badcase，当前检索评测集 {len(items)} 条。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
