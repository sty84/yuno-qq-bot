"""多主体记忆（v2.2）：注册主体（队友/NPC）→ 独立 scope 的记忆写入 / 检索 / 注入。

- registered(): 从 config memory.core.agents.cast 读主体名单（空则回退 environment.cast）；
- detect(text): 文本里出现的主体名（支持短名/二元组匹配）；
- scope_of(name): npc:<name> 独立命名空间。
"""

from plugins import _shared


def _cfg(key, default):
    return _shared.core_cfg("agents", key, default)
def enabled() -> bool:
    return bool(_cfg("enabled", False))


def registered() -> list:
    cast = [str(x).strip() for x in (_cfg("cast", []) or []) if str(x).strip()]
    if not cast:
        try:
            from memory import pack
            cs = pack.world().get("cast_schedule")
            if isinstance(cs, dict):
                cast = [str(x) for x in cs.keys()]
        except Exception:
            pass
    if not cast:
        try:
            env_cfg = (_shared.CONFIG.get("memory", {}).get("core", {}) or {}).get("environment", {}) or {}
            cast = [str(x).strip() for x in (env_cfg.get("cast") or []) if str(x).strip()]
        except Exception:
            cast = []
    return cast


def detect(text) -> list:
    """文本里出现的已注册主体名（全名或名字片段）。"""
    t = str(text or "")
    out = []
    for name in registered():
        if name and (name in t or any(name[i:i + 2] in t for i in range(max(0, len(name) - 1)))):
            out.append(name)
    return out


def scope_of(name) -> str:
    return f"npc:{str(name).strip()}"


def top_k() -> int:
    try:
        return max(1, int(_cfg("npc_top_k", 2)))
    except (TypeError, ValueError):
        return 2


def confidence_cap() -> float:
    try:
        return min(1.0, max(0.0, float(_cfg("npc_confidence_cap", 0.8))))
    except (TypeError, ValueError):
        return 0.8


def _decay_probe() -> dict:
    """NPC 可信度治理探针（P2-2）：
    1) cap_rate：写入是否尊重 npc_confidence_cap；
    2) source_ceiling_rate：overheard/experienced/inferred 来源上限是否被遵守（overheard≤0.6 等）；
    3) decay_rate：超过所属记忆类半衰期的 NPC 记忆是否真的发生了可信度衰减（低于来源上限）。
    """
    from datetime import datetime
    from plugins import _db
    from memory import policy
    cap = confidence_cap()
    rows = []
    for n in registered():
        rows += [r for r in _db.memory_rows(scope_of(n))]
    n = len(rows)
    if not n:
        return {"n": 0, "cap_rate": None, "source_ceiling_rate": None,
                "decay_eligible": 0, "decay_rate": None}
    source_ceil = {"overheard": 0.6, "experienced": 0.9, "inferred": 0.4}
    cap_hit = src_ok = decay_ok = decay_n = 0
    now = datetime.now()
    for r in rows:
        conf = float(r.get("confidence", 0.7))
        src = str(r.get("source") or "")
        ceil = min(cap, source_ceil.get(src, 0.8))
        if conf <= cap + 1e-9:
            cap_hit += 1
        if conf <= ceil + 1e-9:
            src_ok += 1
        try:
            hl = policy.half_life_for(
                str(r.get("scope") or ""), str(r.get("key") or ""), str(r.get("fact") or "")
            )
            if hl is not None:
                # 与 policy.stats_for/memory_strength 同口径：情绪锚定调制半衰期
                hl = hl * policy.arousal_half_factor(r.get("arousal", 0.0))
            valid_from = str(r.get("valid_from") or r.get("updated_at") or "")
            if hl and valid_from:
                age_days = (now - datetime.fromisoformat(valid_from[:19])).total_seconds() / 86400
                if age_days > hl:
                    decay_n += 1
                    if conf < ceil - 0.05:
                        decay_ok += 1
        except Exception:
            pass
    return {
        "n": n,
        "cap_rate": round(cap_hit / n, 3),
        "source_ceiling_rate": round(src_ok / n, 3),
        "decay_eligible": decay_n,
        "decay_rate": round(decay_ok / max(1, decay_n), 3) if decay_n else None,
    }


def eval_run(compare=False, save=False) -> dict:
    """多主体评测（原 memory/subjects_eval.py 并入）：写入成功 / 隐私门控 / 对话引用。"""
    import json
    from datetime import datetime

    from plugins import _db
    names = registered()
    write_ok = sum(1 for n in names if _db.memory_rows(scope_of(n)))
    priv_leak = npc_total = 0
    for n in names:
        for r in _db.memory_rows(scope_of(n)):
            npc_total += 1
            if float(r.get("privacy", 0.0)) >= 0.8:
                priv_leak += 1
    ref_ok = 0
    for n in names[:1]:
        try:
            from memory import context
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
        "decay": _decay_probe(),
        "ts": datetime.now().isoformat(timespec="seconds"),
    }
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
                    "cap_rate": round(
                        (metrics["decay"].get("cap_rate") or 0)
                        - float((base.get("decay") or {}).get("cap_rate", 0) or 0), 3,
                    ),
                    "decay_rate": round(
                        (metrics["decay"].get("decay_rate") or 0)
                        - float((base.get("decay") or {}).get("decay_rate", 0) or 0), 3,
                    ) if metrics["decay"].get("decay_eligible") else None,
                }
            else:
                metrics["delta"] = {"error": "无 baseline（先 --save）"}
        except Exception as e:
            metrics["delta"] = {"error": str(e)}
    return metrics
