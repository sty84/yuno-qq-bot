"""Evaluation-related CLI commands (split from tools.py)."""

import json
from datetime import datetime

from tools.core import ROOT, _plugins, _rubric_judge, run_ablation, scenario_replay


def cmd_emotion_eval(path: str = "") -> str:
    """情绪判断评测：分类准确率 + VAD MAE（--file 评测集 JSON）。"""
    import memory
    if not path:
        _capability, _db, _shared = _plugins()
        path = str(_shared.DATA_DIR / "emotion_probes.json")
    try:
        with open(path, encoding="utf-8") as f:
            probes = json.load(f)
    except OSError as e:
        return f"评测集不存在：{path}（{e}）"
    res = memory.emotion_eval(probes)
    try:
        # v2.2+ 议题 mood-VAD 一致性（写入标签↔存向量 + 跨表 VAD 漂移）
        from memory import topic as topic_mod
        res["topic_mood"] = topic_mod.mood_eval()
    except Exception:
        pass
    return json.dumps(res, ensure_ascii=False, indent=2)


def cmd_memory_eval(path: str, k: int, save: bool, dataset: str = "") -> str:
    """评测召回率/MRR：python tools.py memory-eval --file probes.json --k 5。"""
    import memory
    probes = None
    if dataset:
        from plugins import _db
        data = _db.kv_get("memory", f"dataset:{dataset}") or {}
        probes = data.get("probes")
        if not probes:
            return f"评测集不存在：{dataset}"
    if probes is None and not path:
        return (
            "请提供评测集文件：--file probes.json（[{\"query\":..., \"expected\":[...], \"scope\":...}]）\n"
            + memory.eval_report()
        )
    if probes is None:
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            # 兼容 {"items":[...]} 包装（带说明的评测集文件）与裸列表
            probes = raw.get("items") if isinstance(raw, dict) else raw
        except Exception as e:
            return f"评测集读取失败：{e}"
    result = memory.run_eval(probes, k=k)
    if save:
        from plugins import _db
        _db.kv_set("memory", "eval_baseline", result)
        return json.dumps(result, ensure_ascii=False, indent=2) + "\n（已存为 baseline）\n" + memory.eval_report()
    return json.dumps(result, ensure_ascii=False, indent=2) + "\n" + memory.eval_report()


def cmd_eval_dataset_save(name: str, path: str) -> str:
    """保存命名评测集（版本化对比用）：memory-eval-dataset <名称> --file probes.json。"""
    from plugins import _db
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        # 兼容 {"items":[...]} 包装（带说明）与裸列表
        probes = raw.get("items") if isinstance(raw, dict) else raw
    except Exception as e:
        return f"评测集读取失败：{e}"
    _db.kv_set(
        "memory",
        f"dataset:{name}",
        {"probes": probes, "created": datetime.now().isoformat(timespec="seconds")},
    )
    return f"已保存评测集 {name}（{len(probes)} 条），可用 memory-eval --dataset {name} --save 跑基线"


def cmd_evidence_gate_eval() -> str:
    """证据门控评测：跑内置评测集，输出准确率/错误明细（before/after 对比用）。"""
    from memory.gate_eval import evaluate
    return json.dumps(evaluate(), ensure_ascii=False, indent=2)


def cmd_space_eval(save=False, compare=False):
    """空间评测：X在哪命中 / 时刻召回 / 找东西模拟（--save 落基线 / --compare 对比）。"""
    from memory import space_eval
    return json.dumps(space_eval.run(save=save, compare=compare), ensure_ascii=False, indent=2)


def cmd_time_eval(save=False, compare=False):
    """时间感知评测：时间段召回 / 时间线序列 / 日期精确度（--save 落基线 / --compare 对比）。"""
    from memory import time_eval
    return json.dumps(time_eval.run(save=save, compare=compare), ensure_ascii=False, indent=2)


def cmd_subjects_eval(save=False, compare=False) -> str:
    """多主体评测：写入成功 / 隐私门控 / 对话引用（--save 落基线 / --compare 对比）。"""
    from memory import subjects
    return json.dumps(subjects.eval_run(save=save, compare=compare), ensure_ascii=False, indent=2)


def cmd_ablation(save=False) -> str:
    """机制消融：临时覆盖 config 单开关，同一套 probes 各跑一遍，输出贡献表 + 实验日志。
    --save 把矩阵落成 data/persona-yuno/ablation_baseline.json（第一次跑 = 改前基线）。"""
    _capability, _db, _shared = _plugins()
    probes_path = _shared.DATA_DIR / "probes.json"
    if not probes_path.exists():
        return "评测集不存在（先 tools.py memory-probes 生成）"
    probes = json.loads(probes_path.read_text(encoding="utf-8"))
    res = run_ablation(probes)
    if save:
        dest = _shared.DATA_DIR / "ablation_baseline.json"
        dest.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        res["baseline_saved"] = str(dest)
    return json.dumps(res, ensure_ascii=False, indent=2)


def cmd_experiments(limit=50) -> str:
    """实验日志：改动/评测的基线前后与回归标记。"""
    from plugins import _db
    return json.dumps(_db.exp_log_rows(limit), ensure_ascii=False, indent=2)


def cmd_scenario_eval(path: str = "", score: bool = False, review_export: bool = False) -> str:
    """场景回放评分：重放多轮对话（agent.ask 逐条），--score 用 DeepSeek 五维 rubric 打分；
    --review-export 把回放对话写入 conv_log 供人工评分（v33 convreview）。"""
    res = scenario_replay(path, score=score, review_export=review_export)
    if res.get("error"):
        return res["error"]
    if score:
        return json.dumps({"scored": res["scored"], "avg": res["avg"]}, ensure_ascii=False, indent=2)
    return json.dumps(
        {"replayed": res["replayed"], "scenarios": res["scenarios"], "review_exported": res.get("review_exported", 0)},
        ensure_ascii=False, indent=2,
    )


def cmd_reply_check(scope: str = "", limit: int = 0, save: bool = False, score: bool = False):
    """回复质量评测（2026-08-15）：逐题调 agent.ask（真实 LLM），输出回复 + 预期供人工判分。
    题集：data/reply_probes.json（独立题，无前置依赖）。learn=False 不写记忆。
    --save 记录本轮结果到 data/reply_eval_history.jsonl。
    --score（v2.3 P1-1）：LLM rubric 四维自动判分（准确/合理/人设/防编造 0-2），
    结果写回 history 的 results[].scores，摘要含平均分——回复质量可量化、可跨轮对比。"""
    import json as _json

    probes_path = ROOT / "data" / "reply_probes.json"
    if not probes_path.exists():
        return 1, f"题集不存在：{probes_path}"
    probes = _json.loads(probes_path.read_text(encoding="utf-8"))["items"]
    if limit:
        probes = probes[:int(limit)]
    if not scope:
        return 1, "需要 --scope（如 c2c:xxxx 或 c2c:B889...）"
    import agent
    lines, results = [], []
    for i, it in enumerate(probes, 1):
        try:
            reply, meta = agent.ask(str(it["query"]), scopes=[scope], learn=False)
        except Exception as e:
            reply = f"<调用失败: {e}>"
        lines.append(f"[{i:02d}] {it['category']}｜{it['query']}")
        lines.append(f"     预期: {it['expected']}")
        lines.append(f"     回复: {str(reply or '')[:120]}")
        row = {"query": it["query"], "reply": (reply or "")[:200],
               "expected": it["expected"], "category": it["category"]}
        if score and not str(reply or "").startswith("<调用失败"):
            j = _rubric_judge(it["query"], (reply or "")[:200], it["expected"], it["category"])
            row["scores"] = j["scores"]
            row["score_total"] = j["total"]
            row["score_comment"] = j["comment"]
            lines.append(f"     评分: {j['total']}/8 ({j['scores']}) {j['comment']}")
        results.append(row)
    if score:
        totals = [r["score_total"] for r in results if r.get("score_total", -1) >= 0]
        if totals:
            avg = sum(totals) / len(totals)
            dims = {}
            for k in ("accuracy", "reasonableness", "persona", "no_fabrication"):
                vals = [r["scores"][k] for r in results if r.get("scores") and k in r["scores"]]
                if vals:
                    dims[k] = round(sum(vals) / len(vals), 2)
            lines.append(f"平均 {avg:.2f}/8 · 维度 {dims}")
    if save:
        import datetime
        hist = ROOT / "data" / "reply_eval_history.jsonl"
        row = {"ts": datetime.datetime.now().isoformat(timespec="seconds"),
               "scope": scope, "results": results}
        with open(hist, "a", encoding="utf-8") as f:
            f.write(_json.dumps(row, ensure_ascii=False) + "\n")
        lines.append(f"（已记录 {len(results)} 题到 {hist.name}）")
    return 0, "\n".join(lines)
