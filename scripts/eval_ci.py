#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CI 评测门禁：跑核心评测集并自动落 baseline + 回归检查。

覆盖：
- 证据门控评测集（50 条）
- 检索命中率评测集（eval/retrieval_probes.json，当前 50 条）
- 情绪评测集（data/persona-yuno/emotion_probes.json / example）

行为：
- 使用临时 SQLite 库，自动把检索评测集里的 expected 事实作为种子写入。
- 结果写入 docs/baselines/ci_eval.json（自动 baseline）。
- 与上一次 baseline 对比，任一核心指标下降超过阈值则退出码 1。
"""
import json
import os
import pathlib
import sys
import tempfile

WS = pathlib.Path(__file__).resolve().parent.parent
if str(WS) not in sys.path:
    sys.path.insert(0, str(WS))

# 强制使用隔离 SQLite，避免 CI 误连生产/测试 PG
os.environ["YUNO_DB_BACKEND"] = "sqlite"

RETRIEVAL_PROBES = WS / "eval" / "retrieval_probes.json"
EMOTION_PROBES = WS / "data" / "persona-yuno" / "emotion_probes.json"
EMOTION_EXAMPLE = WS / "memory" / "emotion_probes.example.json"
BASELINE_FILE = WS / "docs" / "baselines" / "ci_eval.json"

REGRESSION_DELTA = 0.05


def _make_config(tmp: str) -> str:
    cfg = {
        "memory": {
            "embedder": {"provider": "none"},
            "core": {"enabled": True},
        }
    }
    cfg_path = os.path.join(tmp, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg_path


def _seed_retrieval_facts(_db):
    """把检索评测集里的 expected 事实写入对应 scope，保证评测可复现。"""
    data = json.loads(RETRIEVAL_PROBES.read_text(encoding="utf-8"))
    items = data.get("items", data) if isinstance(data, dict) else data
    added = 0
    for item in items:
        scope = str(item.get("scope") or "c2c:evp")
        for fact in item.get("expected", []):
            fact = str(fact)
            if not fact:
                continue
            _db.memory_add(scope, "", fact, "", None, 0.8, "user")
            added += 1
    return added


def _load_emotion_probes():
    p = EMOTION_PROBES if EMOTION_PROBES.exists() else EMOTION_EXAMPLE
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))


def _run():
    from plugins import _db, _shared

    tmp = tempfile.mkdtemp(prefix="yuno_eval_ci_")
    cfg_path = _make_config(tmp)
    os.environ["CONFIG_PATH"] = cfg_path
    _shared.CONFIG_PATH = cfg_path
    _shared.reload_config()
    _db.init(tmp, force=True)

    seeded = _seed_retrieval_facts(_db)

    # 1) 证据门控
    from memory import gate_eval
    gate = gate_eval.evaluate()

    # 2) 检索命中率
    import memory
    probes = json.loads(RETRIEVAL_PROBES.read_text(encoding="utf-8"))
    items = probes.get("items", probes) if isinstance(probes, dict) else probes
    retrieval = memory.run_eval(items, k=5)

    # 3) 情绪评测
    emotion_probes = _load_emotion_probes()
    emotion = memory.emotion_eval(emotion_probes) if emotion_probes else {"error": "无情绪评测集"}

    return {
        "seeded_facts": seeded,
        "gate": {"total": gate.get("total"), "passed": gate.get("passed"), "accuracy": gate.get("accuracy")},
        "retrieval": {
            "probes": retrieval.get("probes"),
            "recall_at_k": retrieval.get("recall_at_k"),
            "mrr": retrieval.get("mrr"),
            "ndcg": retrieval.get("ndcg"),
        },
        "emotion": {
            "n": emotion.get("n") or emotion.get("total"),
            "accuracy": emotion.get("accuracy"),
            "vad_mae": emotion.get("vad_mae"),
        },
    }


def _regression(before: dict, after: dict) -> list[str]:
    regressions = []
    checks = [
        ("gate.accuracy", "gate", "accuracy"),
        ("retrieval.recall_at_k", "retrieval", "recall_at_k"),
        ("retrieval.mrr", "retrieval", "mrr"),
        ("emotion.accuracy", "emotion", "accuracy"),
    ]
    for label, section, key in checks:
        old = (before.get(section) or {}).get(key)
        new = (after.get(section) or {}).get(key)
        if old is None or new is None:
            continue
        try:
            if float(new) < float(old) - REGRESSION_DELTA:
                regressions.append(f"{label}: {old} -> {new}")
        except (TypeError, ValueError):
            continue
    return regressions


def main() -> int:
    current = _run()
    before = {}
    if BASELINE_FILE.exists():
        try:
            before = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
        except Exception:
            before = {}

    BASELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_FILE.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(current, ensure_ascii=False, indent=2))
    if before:
        regressions = _regression(before, current)
        if regressions:
            print("\n回归告警：")
            for r in regressions:
                print(" -", r)
            return 1
        print("\n与上次 baseline 对比：无回归。")
    else:
        print("\n已生成初始 baseline。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
