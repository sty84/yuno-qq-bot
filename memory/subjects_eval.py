"""多主体记忆评测探针（v2.2）：写入成功 / 隐私门控 / 对话引用。
支持 --save 落基线 / --compare 对比（与 space/time-eval 同构）。
"""

import json
from datetime import datetime

from plugins import _db


def run(compare=False, save=False) -> dict:
    from memory import context, subjects
    names = subjects.registered()
    write_ok = sum(1 for n in names if _db.memory_rows(subjects.scope_of(n)))
    priv_leak = npc_total = 0
    for n in names:
        for r in _db.memory_rows(subjects.scope_of(n)):
            npc_total += 1
            if float(r.get("privacy", 0.0)) >= 0.8:
                priv_leak += 1
    ref_ok = 0
    for n in names[:1]:
        try:
            if context.npc_memory_block("在", [n], top_k=1):
                ref_ok = 1
        except Exception:
            pass
    metrics = {
        "subjects": len(names),
        "write_ok": write_ok,
        "write_rate": round(write_ok / max(1, len(names)), 3),
        "npc_memories": npc_total,
        "privacy_leak": priv_leak,
        "privacy_rate": round(1 - priv_leak / max(1, npc_total), 3) if npc_total else None,
        "reference_ok": ref_ok,
        "ts": datetime.now().isoformat(timespec="seconds"),
    }
    from plugins import _shared
    baseline_path = _shared.DATA_DIR / "subjects_eval_baseline.json"
    if save:
        try:
            baseline_path.parent.mkdir(parents=True, exist_ok=True)
            baseline_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
            metrics["baseline_saved"] = str(baseline_path)
        except Exception as e:
            metrics["baseline_save_error"] = str(e)
    if compare:
        try:
            if baseline_path.exists():
                base = json.loads(baseline_path.read_text(encoding="utf-8"))
                metrics["delta"] = {
                    "write_rate": round(metrics["write_rate"] - float(base.get("write_rate", 0)), 3),
                    "privacy_rate": round((metrics["privacy_rate"] or 0) - float(base.get("privacy_rate", 0) or 0), 3),
                }
            else:
                metrics["delta"] = {"error": "无 baseline（先 --save）"}
        except Exception as e:
            metrics["delta"] = {"error": str(e)}
    return metrics
