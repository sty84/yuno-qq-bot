"""空间评测探针（P2）：三类题——
1) "X 在哪"命中率：position_at 投影 vs 当前库存；
2) "X 某时刻在哪"召回：从事件流水抽样，按事件时刻投影 vs 事件记录；
3) 找东西模拟：给定难度，按候选容器顺序统计平均步数与失败率。
全部本地计算，不调 LLM；遗忘/搜索参数调没调对就看这些数字。
支持 --save 落基线 / --compare 对比上次，退化一目了然。
"""

import json

from plugins import _db


def _where_accuracy() -> dict:
    from memory import living
    items = living.all_items()
    total, hit = 0, 0
    for it in items:
        if it.get("status") == "没有了":
            continue
        total += 1
        pos = living.position_at(str(it.get("name", "")))
        ground = (str(it.get("room", "")), str(it.get("container", "")))
        if pos.get("known") and (str(pos.get("room", "")), str(pos.get("container", ""))) == ground:
            hit += 1
    return {"n": total, "hit": hit, "accuracy": round(hit / total, 3) if total else None}


def _where_recall() -> dict:
    """时刻召回（修口径）：对每条决定位置的事件，用"该事件与下一条相邻事件之间的
    中间时刻"作为查询点，ground truth = 该事件的 to_place（lost 的 ground truth 是"未知"）。
    避免"投影到事件自身时刻"的自证：那会让 recall 恒接近 100%，测不出遗忘/错位。"""
    from memory import living
    from datetime import datetime, timedelta
    events = _db.item_event_rows(limit=500)
    by_item = {}
    for e in events:
        by_item.setdefault(str(e.get("item", "")), []).append(e)
    total, hit = 0, 0
    for item, evs in by_item.items():
        evs.sort(key=lambda x: (str(x.get("ts", "")), int(x.get("id", 0) or 0)))
        for i, e in enumerate(evs):
            ev = str(e.get("event", ""))
            if ev not in ("move", "give", "see", "find", "lost"):
                continue
            to_place = str(e.get("to_place", ""))
            try:
                t0 = datetime.fromisoformat(str(e.get("ts", "")))
            except Exception as e:
                _stats_err(e)
                continue
            t1 = None
            if i + 1 < len(evs):
                try:
                    t1 = datetime.fromisoformat(str(evs[i + 1].get("ts", "")))
                except Exception as e:
                    _stats_err(e)
                    t1 = None
            if t1 is None or t1 <= t0:
                query_ts = (t0 + timedelta(seconds=1)).isoformat(timespec="seconds")
            else:
                query_ts = (t0 + (t1 - t0) / 2).isoformat(timespec="seconds")
            p = living.position_at(item, query_ts)
            total += 1
            if ev == "lost":
                ok = not p.get("known")  # 丢失事件的 ground truth = 未知
            else:
                ok = (
                    bool(to_place)
                    and p.get("known")
                    and f"{p.get('room', '')}/{p.get('container', '')}" == to_place
                )
            if ok:
                hit += 1
    return {"n": total, "hit": hit, "recall": round(hit / total, 3) if total else None}


def _search_sim() -> dict:
    """找东西模拟：候选容器顺序 = 最后已知容器优先；难度影响成功判定。
    简化模型：浅 = 第一次查看必中；深 = 每次查看 60% 命中，否则步数+1。"""
    import random
    from memory import living
    items = [i for i in living.all_items() if i.get("status") != "没有了"]
    if not items:
        return {"n": 0}
    rng = random.Random(42)
    steps_all, fail = [], 0
    for it in items:
        diff = str(it.get("difficulty", "浅"))
        steps = 1
        if diff == "深":
            while rng.random() > 0.6 and steps < 5:
                steps += 1
            if steps >= 5 and rng.random() > 0.5:
                fail += 1
        steps_all.append(steps)
    return {
        "n": len(items),
        "avg_steps": round(sum(steps_all) / len(steps_all), 2),
        "max_steps": max(steps_all),
        "fail_rate": round(fail / len(items), 3),
    }


def run(compare=False, save=False) -> dict:
    from plugins import _shared
    metrics = {
        "where_accuracy": _where_accuracy(),
        "where_recall": _where_recall(),
        "search_sim": _search_sim(),
        "ts": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
    }
    baseline_path = _shared.DATA_DIR / "space_eval_baseline.json"
    if save:
        try:
            baseline_path.parent.mkdir(parents=True, exist_ok=True)
            with open(baseline_path, "w", encoding="utf-8") as f:
                json.dump(metrics, f, ensure_ascii=False, indent=2)
            metrics["baseline_saved"] = str(baseline_path)
        except Exception as e:
            metrics["baseline_save_error"] = str(e)
    if compare:
        try:
            if baseline_path.exists():
                with open(baseline_path, encoding="utf-8") as f:
                    base = json.load(f)

                def _delta(a, b):
                    try:
                        return round(float(a) - float(b), 3)
                    except Exception as e:
                        _stats_err(e)
                        return None

                metrics["delta"] = {
                    "where_accuracy": _delta(
                        metrics["where_accuracy"].get("accuracy"),
                        base.get("where_accuracy", {}).get("accuracy"),
                    ),
                    "where_recall": _delta(
                        metrics["where_recall"].get("recall"),
                        base.get("where_recall", {}).get("recall"),
                    ),
                    "search_avg_steps": _delta(
                        metrics["search_sim"].get("avg_steps"),
                        base.get("search_sim", {}).get("avg_steps"),
                    ),
                    "search_fail_rate": _delta(
                        metrics["search_sim"].get("fail_rate"),
                        base.get("search_sim", {}).get("fail_rate"),
                    ),
                }
            else:
                metrics["delta"] = {"error": "无 baseline（先 --save）"}
        except Exception as e:
            metrics["delta"] = {"error": str(e)}
    return metrics



def _stats_err(e):
    """裸 except 审计（v2.2）：错误计数 + 日志，供消融/排查。"""
    try:
        import memory.stats as _st
        _st.bump_err("space_eval", e)
    except Exception:
        pass
