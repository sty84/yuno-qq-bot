"""时间感知评测探针（v2.2）：三类题——
1) 时间段召回：explicit 事件按当天窗口检索，能召回 memory_fact 才算命中；
2) 时间线序列：follows 链上 src.ts ≤ dst.ts 的比例；
3) 日期精确度：检索命中的事件日期 == 期望日期。
支持 --save 落基线 / --compare 对比上次（与 space-eval 同构）。
"""

import json
from datetime import datetime, timedelta

from plugins import _db


def _explicit_events(limit=500):
    return [
        e for e in _db.event_rows(limit=limit)
        if str(e.get("ts_source") or "approx") == "explicit"
        and e.get("memory_fact") and e.get("ts")
    ]


def _day_window(ts):
    day = ts.date()
    return (datetime(day.year, day.month, day.day),
            datetime(day.year, day.month, day.day) + timedelta(days=1))


def _window_recall() -> dict:
    from memory import reasoning
    total = hit = 0
    for ev in _explicit_events()[:100]:
        try:
            ts = datetime.fromisoformat(str(ev["ts"])[:19])
        except Exception:
            continue
        scope = str(ev.get("scope") or "")
        if not scope:
            continue
        q = str(ev.get("title") or "")[:40]
        hits = reasoning.retrieve(q, [scope], top_k=5, min_score=0.0, window=_day_window(ts))
        total += 1
        expected = str(ev.get("memory_fact") or "")
        if any(expected in f or f in expected for f, _s, _sc in hits):
            hit += 1
    return {"n": total, "hit": hit, "recall": round(hit / total, 3) if total else None}


def _query_all(sql, params=()):
    """兼容 SQLite/PostgreSQL 的查询助手，返回 dict 行列表。"""
    conn = _db._connect()
    if hasattr(conn, "cursor"):
        try:
            from psycopg2.extras import RealDictCursor
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]
            cur.close()
            return rows
        except TypeError:
            cur = conn.cursor()
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            cur.close()
            return rows
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _timeline_order() -> dict:
    ts_map = {}
    for r in _query_all("SELECT id, ts FROM events"):
        ts_map[r["id"]] = r["ts"]
    rows = _query_all("SELECT src, dst FROM event_relations WHERE rel='follows' LIMIT 300")
    total = ok = 0
    for r in rows:
        src, dst = r["src"], r["dst"]
        if src not in ts_map or dst not in ts_map:
            continue
        try:
            a = datetime.fromisoformat(str(ts_map[src])[:19])
            b = datetime.fromisoformat(str(ts_map[dst])[:19])
        except Exception:
            continue
        total += 1
        if a <= b:
            ok += 1
    return {"n": total, "ok": ok, "rate": round(ok / total, 3) if total else None}


def _date_accuracy() -> dict:
    from memory import reasoning
    total = hit = 0
    for ev in _explicit_events()[:80]:
        try:
            ts = datetime.fromisoformat(str(ev["ts"])[:19])
        except Exception:
            continue
        scope = str(ev.get("scope") or "")
        if not scope:
            continue
        q = str(ev.get("title") or "")[:40]
        hits = reasoning.retrieve(q, [scope], top_k=5, min_score=0.0, window=_day_window(ts))
        total += 1
        expected = str(ev.get("memory_fact") or "")
        ok_date = False
        for f, _s, _sc in hits:
            if not (expected in f or f in expected):
                continue
            info = reasoning._event_time_map([scope]).get(f)
            if info and info[0]:
                try:
                    if datetime.fromisoformat(str(info[0])[:19]).date() == ts.date():
                        ok_date = True
                except Exception:
                    pass
        if ok_date:
            hit += 1
    return {"n": total, "hit": hit, "accuracy": round(hit / total, 3) if total else None}


def run(compare=False, save=False) -> dict:
    from plugins import _shared
    metrics: dict = {
        "window_recall": _window_recall(),
        "timeline_order": _timeline_order(),
        "date_accuracy": _date_accuracy(),
        "ts": datetime.now().isoformat(timespec="seconds"),
    }
    baseline_path = _shared.DATA_DIR / "time_eval_baseline.json"
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
                    except Exception:
                        return None

                metrics["delta"] = {
                    "window_recall": _delta(
                        metrics["window_recall"].get("recall"),
                        base.get("window_recall", {}).get("recall"),
                    ),
                    "timeline_order": _delta(
                        metrics["timeline_order"].get("rate"),
                        base.get("timeline_order", {}).get("rate"),
                    ),
                    "date_accuracy": _delta(
                        metrics["date_accuracy"].get("accuracy"),
                        base.get("date_accuracy", {}).get("accuracy"),
                    ),
                }
            else:
                metrics["delta"] = {"error": "无 baseline（先 --save）"}
        except Exception as e:
            metrics["delta"] = {"error": str(e)}
    return metrics
