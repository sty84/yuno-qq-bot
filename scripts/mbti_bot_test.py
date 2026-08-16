#!/usr/bin/env python3
"""让 bot 按人设做 3 轮 MBTI，统计答案偏移/类型稳定性。

用法：
  python scripts/mbti_bot_test.py
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import os
import tempfile

# 测试数据与 LLM 成本写入临时 SQLite，避免污染生产 PostgreSQL
os.environ["YUNO_DB_BACKEND"] = "sqlite"
from plugins import _db  # noqa: E402
_db.init(tempfile.mkdtemp(prefix="mbti_test_"), force=True)  # type: ignore[attr-defined]

from plugins import _shared  # noqa: E402
from agent import persona  # noqa: E402
from plugins.mbti import DIMENSIONS, QUESTIONS  # noqa: E402
from plugins import _db_internal  # noqa: E402


def ask_choice(q, system):
    prompt = (
        f"你是千石由乃。请根据你的人设和直觉回答下面这道 MBTI 测试题。\n"
        f"题目：{q[0]}\n{q[1]}\n{q[2]}\n"
        "只回答 A 或 B，不要解释，不要加标点。"
    )
    try:
        reply = _shared.ask_deepseek(
            prompt,
            system=system,
            max_tokens=10,
            temperature=0.7,
            module="mbti_test",
            detail="choice",
        )
    except Exception as e:
        print("调用失败", e)
        return None
    m = re.search(r"[AB]", reply or "")
    return m.group(0) if m else None


def compute(answers):
    scores = {"E": 0, "I": 0, "S": 0, "N": 0, "T": 0, "F": 0, "J": 0, "P": 0}
    for i, ans in enumerate(answers):
        letter = QUESTIONS[i][3]
        if ans == "A":
            scores[letter] += 1
        elif ans == "B":
            for a, b in DIMENSIONS:
                if letter == a:
                    scores[b] += 1
                elif letter == b:
                    scores[a] += 1
    return "".join(a if scores[a] >= scores[b] else b for a, b in DIMENSIONS), scores


def main():
    system = persona.compose(query="MBTI 测试")
    rounds = []
    for r in range(1, 4):
        answers = []
        print(f"\n===== 第 {r} 轮 =====")
        for i, q in enumerate(QUESTIONS):
            ans = ask_choice(q, system)
            answers.append(ans)
            print(f"Q{i+1}: {ans}  | {q[0]}")
            if ans is None:
                print("跳过（无有效答案）")
        mbti, scores = compute(answers)
        rounds.append({"round": r, "answers": answers, "mbti": mbti, "scores": scores})
        _db_internal.record(
            "mbti_run",
            {"round": r, "answers": answers, "mbti": mbti, "scores": scores},
            scope="mbti_bot_test",
        )
        print(f"第 {r} 轮 MBTI: {mbti}  {scores}")

    # 对比
    types = [x["mbti"] for x in rounds]
    agree = sum(1 for i in range(8) if len({x["answers"][i] for x in rounds}) == 1)
    stable_dims = sum(
        1 for i in range(0, 8, 2)
        if len({x["mbti"][i // 2] for x in rounds}) == 1
    )
    print("\n===== 三轮对比 =====")
    for x in rounds:
        print(f"第 {x['round']} 轮: {x['mbti']}  答案: {''.join(a or '?' for a in x['answers'])}")
    print(f"8 题中三轮完全一致: {agree}/8")
    print(f"4 个维度中三轮稳定: {stable_dims}/4")
    print(f"最终类型集合: {sorted(set(types))}")

    report = {
        "rounds": rounds,
        "agree_answers": agree,
        "stable_dimensions": stable_dims,
        "types": sorted(set(types)),
    }
    out = Path(__file__).resolve().parents[1] / "docs" / "baselines" / "mbti_bot_test.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _db_internal.record("mbti_summary", report, scope="mbti_bot_test")
    print(f"\n报告已保存: {out}")
    print(f"内部记录已写入: {_db_internal.DB_PATH}")


if __name__ == "__main__":
    main()
