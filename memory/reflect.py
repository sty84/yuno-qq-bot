"""成长反思闭环：巩固出的 belief 先自我审查（LLM 或规则），
通过 → 确认升权；需改 → 改写并记版本；与证据矛盾 → 驳回（可信度压底）。
全部动作写入 belief_log（可审计、可回滚）。"""

import json

from plugins import _db, _shared
from memory import policy, reasoning
from memory.extract import tokenize


def _cfg(key, default):
    core = (_shared.CONFIG.get("memory", {}).get("core", {}) or {}).get("reflection", {}) or {}
    return core.get(key, default)


def enabled() -> bool:
    return bool(_cfg("enabled", True))


def llm_enabled() -> bool:
    return bool(_cfg("llm", True))


def _evidence_for(belief):
    """找相关证据：用户记忆里与 belief 相关的事实（含可信度/玩笑标记）。"""
    scopes = list(dict.fromkeys(r["scope"] for r in _db.memory_rows() if r["scope"] != "ai"))
    if not scopes:
        return []
    hits = reasoning.retrieve(belief, scopes, top_k=8, min_score=0.0)
    bt = set(tokenize(belief))
    hits = [h for h in hits if len(set(tokenize(h[0])) & bt) >= 2]
    playful_topic_ids = {
        t["id"]
        for t in _db.topic_rows()
        if any(p["param"] == "playful" and p["value"] == "true" for p in _db.topic_params(t["id"]))
    }
    playful_facts = {
        ev["title"]
        for ev in _db.event_rows()
        if ev.get("topic_id") in playful_topic_ids
    }
    conf_map = {}
    for scope in scopes:
        for r in _db.memory_rows(scope):
            conf_map.setdefault(r["fact"], float(r.get("confidence", 0.7)))
    evidence = []
    for f, _s, _sc in hits:
        evidence.append(
            {
                "fact": f,
                "confidence": conf_map.get(f, 0.7),
                "playful": f in playful_facts,
            }
        )
    return evidence


def _rule_review(belief, evidence):
    """规则审查：证据含低可信度/玩笑 → 驳回；完全无证据 → 降权存疑；否则接受。"""
    if any(e["confidence"] < 0.3 or e["playful"] for e in evidence):
        return {"action": "reject", "note": "证据可信度低或来自玩笑语境"}
    if not evidence:
        return {"action": "revise", "note": "无证据支撑，降低可信度"}
    return {"action": "accept", "note": "有证据支撑"}


def _llm_review(belief, evidence):
    prompt = (
        "你是记忆审查器。判断以下 AI 观点是否与证据矛盾。"
        "只输出 JSON：{\"action\": \"accept|revise|reject\", \"revised\": \"改写后的观点（可空）\", \"confidence\": 0.0-1.0}\n"
        f"观点：{belief}\n证据：\n"
        + "\n".join(
            f"- {e['fact']}（可信度{e['confidence']:.2f}{'，玩笑' if e['playful'] else ''}）"
            for e in evidence
        )
        or "（无证据）"
    )
    try:
        resp = _shared.deepseek.chat.completions.create(
            model=_shared.DEEPSEEK_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            temperature=0.1,
        )
        raw = (resp.choices[0].message.content or "").strip()
        start, end = raw.find("{"), raw.rfind("}")
        data = json.loads(raw[start:end + 1]) if start >= 0 else {}
        action = data.get("action", "accept")
        if action not in ("accept", "revise", "reject"):
            action = "accept"
        return {
            "action": action,
            "revised": str(data.get("revised") or "").strip(),
            "confidence": float(data.get("confidence", 0.5)),
            "note": "LLM 审查",
        }
    except Exception as e:
        print(f"LLM 审查失败，回退规则审查：{e}")
        return _rule_review(belief, evidence)


def reflect_beliefs(limit=10) -> dict:
    """对 belief 逐条审查并应用动作。返回统计。"""
    result = {"checked": 0, "accepted": 0, "revised": 0, "rejected": 0}
    if not enabled():
        return result
    beliefs = _db.memory_rows("ai", "belief")
    for b in beliefs[:limit]:
        fact, conf = b["fact"], float(b.get("confidence", 0.7))
        evidence = _evidence_for(fact)
        verdict = _llm_review(fact, evidence) if llm_enabled() else _rule_review(fact, evidence)
        result["checked"] += 1
        action = verdict["action"]
        note = verdict.get("note", "")
        if action == "accept":
            new_conf = policy.update(conf, "confirm")
            _db.memory_set_confidence("ai", "belief", fact, new_conf)
            _db.belief_log_add("belief", fact, "accept", new_conf, note, old_content=fact)
            result["accepted"] += 1
        elif action == "revise":
            revised = (verdict.get("revised") or "").strip()
            if revised and revised != fact:
                _db.memory_add(
                    "ai", "belief", revised,
                    confidence=float(verdict.get("confidence", conf * 0.7)),
                    source="reflection",
                )
                _db.memory_set_confidence("ai", "belief", fact, max(0.2, conf * 0.5))
                _db.belief_log_add("belief", revised, "revise", verdict.get("confidence"), note, old_content=fact)
            else:
                new_conf = max(0.2, conf * 0.7)
                _db.memory_set_confidence("ai", "belief", fact, new_conf)
                _db.belief_log_add("belief", fact, "revise", new_conf, note, old_content=fact)
            result["revised"] += 1
        else:
            _db.memory_set_confidence("ai", "belief", fact, 0.05)
            _db.belief_log_add("belief", fact, "reject", 0.05, note, old_content=fact)
            result["rejected"] += 1
    return result


def rollback_belief(log_id) -> str:
    """回滚 belief 到日志记录的旧版本（可审计）。"""
    entry = _db.belief_log_get(log_id)
    if not entry or entry["kind"] != "belief":
        return "日志不存在或不是 belief"
    old = entry.get("old_content") or entry["content"]
    conf = float(entry.get("confidence") or 0.5)
    _db.memory_add("ai", "belief", old, confidence=max(conf, 0.5), source="rollback")
    if entry["action"] in ("revise", "reject"):
        _db.memory_set_confidence("ai", "belief", entry["content"], 0.05)
    _db.belief_log_add(
        "belief", old, "rollback", conf,
        note=f"回滚自 #{log_id}", old_content=entry["content"],
    )
    return f"已回滚 belief 至：{old[:40]}"
