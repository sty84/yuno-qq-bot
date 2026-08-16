#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""badcase 定期审核合并流程。

支持：
- 默认：LLM 审核（无 API Key 时规则审核）
- --auto-merge：审核通过后自动合并进 eval/retrieval_probes.json
- --notify：合并结果播报到 QQ
- --limit：单次最多审核条数

用法：
  python scripts/auto_review_badcases.py --limit 20 --auto-merge --notify
"""
import argparse
import json
import os
import pathlib
import sys

WS = pathlib.Path(__file__).resolve().parent.parent
if str(WS) not in sys.path:
    sys.path.insert(0, str(WS))

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


def _rule_review(b):
    """无 LLM 时的保守规则：query/expected 非空且 query 不是纯寒暄。"""
    q = str(b.get("query") or "").strip()
    exp = b.get("expected") or []
    if len(q) < 2 or not exp:
        return False, "query/expected 不完整"
    from memory.trace import is_low_information
    if is_low_information(q):
        return False, "低信息 query"
    return True, "规则通过"


def _llm_review(b):
    """LLM 审核：判断 badcase 是否适合作为正式检索评测集条目。"""
    from plugins import _shared
    prompt = (
        "你是评测集审核员。判断下面这条检索评测候选是否适合加入正式评测集。\n"
        f"query: {b.get('query')}\n"
        f"expected: {json.dumps(b.get('expected'), ensure_ascii=False)}\n"
        f"scope: {b.get('scope')}\n"
        f"category: {b.get('category')}\n"
        "只输出 JSON：{\"approved\": true/false, \"reason\": \"...\"}"
    )
    try:
        raw = _shared.ask_deepseek(prompt, max_tokens=100, temperature=0.0, module="badcase_review")
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0:
            data = json.loads(raw[start:end + 1])
            return bool(data.get("approved")), str(data.get("reason", "") or "LLM 通过")
    except Exception:
        pass
    return _rule_review(b)


def _merge_approved(approved):
    if not approved:
        return 0
    data = json.loads(RETRIEVAL_PROBES.read_text(encoding="utf-8"))
    items = data.setdefault("items", [])
    seen = {(str(it.get("query")), str(it.get("scope"))) for it in items}
    added = 0
    for b in approved:
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
    data["说明"] = f"检索命中率评测集（含自动审核 badcase）：{len(items)} 条"
    RETRIEVAL_PROBES.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return added


def main() -> int:
    parser = argparse.ArgumentParser(description="badcase 定期审核合并")
    parser.add_argument("--limit", type=int, default=0, help="最多审核条数，0=全部")
    parser.add_argument("--auto-merge", action="store_true", help="审核通过后自动合并进正式评测集")
    parser.add_argument("--notify", action="store_true", help="合并结果播报到 QQ")
    args = parser.parse_args()

    badcases = _load_badcases(RETRIEVAL_BADCASES)
    if args.limit:
        badcases = badcases[:args.limit]
    if not badcases:
        print("没有待审核 badcase。")
        return 0

    approved: list[dict] = []
    rejected: list[dict] = []
    use_llm = bool(os.getenv("DEEPSEEK_API_KEY"))
    for b in badcases:
        if use_llm:
            ok, reason = _llm_review(b)
        else:
            ok, reason = _rule_review(b)
        b["_review_reason"] = reason
        (approved if ok else rejected).append(b)

    print(f"审核完成：通过 {len(approved)} / 拒绝 {len(rejected)}（LLM={use_llm}）")
    for b in approved:
        print(f"  通过 [{b.get('category')}] {b.get('query')}")
    for b in rejected:
        print(f"  拒绝 [{b.get('category')}] {b.get('query')}：{b.get('_review_reason')}")

    if args.auto_merge and approved:
        added = _merge_approved(approved)
        # 只清掉已合并的 badcase；拒绝的保留待人工
        remaining = [
            b for b in _load_badcases(RETRIEVAL_BADCASES)
            if b not in approved
        ]
        RETRIEVAL_BADCASES.write_text(
            "".join(json.dumps(b, ensure_ascii=False) + "\n" for b in remaining),
            encoding="utf-8",
        )
        print(f"已合并 {added} 条到正式评测集。")
        if args.notify:
            try:
                from tools.core import _notify_group
                from plugins import _capability
                if target := _notify_group():
                    _capability.notify_send(
                        "group", target,
                        f"【badcase 自动审核】通过 {len(approved)} 条，合并 {added} 条。",
                    )
            except Exception as e:
                print(f"通知失败：{e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
