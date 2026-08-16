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
RETRIEVAL_BADCASES = WS / "eval" / "badcases" / "retrieval_badcases.jsonl"
GATE_BADCASES = WS / "eval" / "badcases" / "gate_badcases.jsonl"
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


def _append_badcases(path: pathlib.Path, rows: list[dict]):
    """把 badcase 追加到 JSONL，按稳定 key 去重。"""
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    seen = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                seen.add(obj.get("_key", ""))
            except Exception:
                continue
    with open(path, "a", encoding="utf-8") as f:
        for row in rows:
            key = row.get("_key", "")
            if key in seen:
                continue
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            seen.add(key)


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

    # badcase 自动回流：检索未命中 + 门控误判写入 JSONL，供后续扩充评测集
    retrieval_bad = []
    for item, detail in zip(items, retrieval.get("details", [])):
        if not detail.get("hit"):
            retrieval_bad.append({
                "_key": f"{detail.get('query')}|{item.get('scope')}|{item.get('category')}",
                "query": detail.get("query"),
                "expected": item.get("expected", []),
                "scope": item.get("scope"),
                "category": item.get("category"),
            })
    _append_badcases(RETRIEVAL_BADCASES, retrieval_bad)
    gate_bad = []
    for err in gate.get("errors", []):
        gate_bad.append({
            "_key": f"{err.get('index')}|{err.get('reply')}",
            "reply": err.get("reply"),
            "got": err.get("got"),
            "expected_block": err.get("expected_block"),
        })
    _append_badcases(GATE_BADCASES, gate_bad)

    # 3) 情绪评测
    emotion_probes = _load_emotion_probes()
    emotion = memory.emotion_eval(emotion_probes) if emotion_probes else {"error": "无情绪评测集"}

    # 4) 空间 / 时间 / 多主体评测（作为模块级 smoke + 回归基线）
    from memory import space_eval, subjects, time_eval
    space_raw = space_eval.run()
    time_raw = time_eval.run()
    subj_raw = subjects.eval_run()
    space = {
        "where_accuracy": (space_raw.get("where_accuracy") or {}).get("accuracy"),
        "where_recall": (space_raw.get("where_recall") or {}).get("recall"),
    }
    time = {
        "window_recall": (time_raw.get("window_recall") or {}).get("recall"),
        "date_accuracy": (time_raw.get("date_accuracy") or {}).get("accuracy"),
    }
    subj = {
        "write_rate": subj_raw.get("write_rate"),
        "privacy_rate": subj_raw.get("privacy_rate"),
    }

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
        "space": space,
        "time": time,
        "subjects": subj,
    }


def _regression(before: dict, after: dict) -> list[str]:
    regressions = []
    checks = [
        ("gate.accuracy", "gate", "accuracy"),
        ("retrieval.recall_at_k", "retrieval", "recall_at_k"),
        ("retrieval.mrr", "retrieval", "mrr"),
        ("emotion.accuracy", "emotion", "accuracy"),
        ("space.where_accuracy", "space", "where_accuracy"),
        ("space.where_recall", "space", "where_recall"),
        ("time.window_recall", "time", "window_recall"),
        ("time.date_accuracy", "time", "date_accuracy"),
        ("subjects.write_rate", "subjects", "write_rate"),
        ("subjects.privacy_rate", "subjects", "privacy_rate"),
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
