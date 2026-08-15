"""用户中心世界模型（v8 简化版）。

原则：
1. 只跟踪用户提到过的相关内容（scope 内），不做大范围世界搜索。
2. 快照有硬预算（默认 400 字符）并按 scope 缓存，防止 token 异常消耗。
3. 用户纠正时 AI 不盲从：受限 LLM（或规则兜底）调查后决定 update/keep/uncertain。
"""

from memory._llmutil import parse_json_object
import time
from datetime import datetime

from plugins import _db, _shared
from memory import policy

_snapshot_cache = {}
_investigate_state = {}  # scope -> 上次调查时间（按 scope 节流，v6 建议 §2）

INVESTIGATE_PROMPT = (
    "用户纠正了一条旧记忆，请判断是否应更新。只输出 JSON："
    '{{"action":"update|keep|uncertain","reason":"一句话"}}。\n'
    "依据：新说法是否更具体/更近；旧记忆是否有多次确认；两者是否真的矛盾。\n"
    "旧记忆：{old}（可信度{conf}，记录于{valid}）\n"
    "用户纠正：{new}\n"
    "候选相关记忆：{candidates}\n"
    "update=旧记忆过时应更新；keep=纠正不成立保留旧记忆；uncertain=无法判断降权待定。"
)


def _cfg(key, default):
    return _shared.core_cfg("world", key, default)
def snapshot(scope, budget=None, force=False) -> str:
    """用户中心世界模型快照：活跃记忆 + 近期事件 + 目标，硬预算截断 + 缓存。"""
    if not _cfg("enabled", True) or not scope:
        return ""
    budget = int(budget or _cfg("budget_chars", 400))
    ttl = float(_cfg("cache_ttl_s", 600))
    now = time.time()
    cached = _snapshot_cache.get(scope)
    if cached and not force and now - cached["ts"] < ttl:
        return cached["text"]

    parts = []
    rows = [r for r in _db.memory_rows(scope) if r.get("status", "active") == "active"]
    rows.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    for r in rows[:10]:
        parts.append("· " + str(r["fact"])[:50])
    evs = _db.event_rows(scope, limit=4)
    if evs:
        parts.append("最近动态：" + "；".join(str(e["title"])[:20] for e in evs))
    try:
        from memory import advisor
        goals = advisor.goal_active(scope)
        if goals:
            parts.append("目标：" + "；".join(str(g["title"])[:20] for g in goals[:3]))
    except Exception as e:
        _stats_err(e)
        pass

    text = "【用户的世界】" + ("\n".join(parts) if parts else "（暂无活跃内容）")
    if len(text) > budget:  # 硬预算：防止 token 异常消耗
        text = text[:budget].rsplit("\n", 1)[0] + "\n…"
    _snapshot_cache[scope] = {"ts": now, "text": text}
    return text


def subject_gate(scope, key, fact) -> bool:
    """多主体可见性门控：私聊 / 高隐私事实不传播到 NPC 视角。"""
    try:
        row = next((r for r in _db.memory_rows(scope, key or "") if r["fact"] == fact), None)
    except Exception:
        row = None
    if row is None:
        return False
    aud = str(row.get("audience") or "")
    privacy = float(row.get("privacy", 0.0))
    if str(scope).startswith("c2c:") or aud == "private":
        return False
    if privacy >= 0.8:
        return False
    return True


def subject_confidence(source) -> float:
    """NPC 视角可信度：experienced=0.9 / overheard=0.6 / inferred=0.4。"""
    return {"experienced": 0.9, "overheard": 0.6, "inferred": 0.4}.get(str(source or ""), 0.5)


def _rule_decision(old_conf, valid_from, scope="", access_count=0) -> dict:
    try:
        age_days = (
            (datetime.now() - datetime.fromisoformat(str(valid_from)[:19])).total_seconds() / 86400
            if valid_from
            else 0.0
        )
    except Exception as e:
        _stats_err(e)
        age_days = 0.0
    if scope == "ai" or scope.startswith("ai:"):
        # AI 自我认知：保守更新，避免人格漂移
        if old_conf <= 0.5 or age_days > 90:
            return {"action": "update", "reason": f"AI自我认知可信度低({old_conf:.2f})或过时，更新"}
        if old_conf >= 0.8:
            return {"action": "keep", "reason": "AI核心认知高可信，保留"}
        return {"action": "uncertain", "reason": "AI自我认知，降权待定"}
    # 用户自己的事：以用户最新说法为准，除非旧记忆被多次确认过
    if access_count >= 3 and old_conf >= 0.85:
        return {"action": "keep", "reason": f"旧记忆被反复确认({access_count}次)，保留"}
    if age_days > 7 or old_conf <= 0.7:
        return {"action": "update", "reason": "用户纠正优先，更新"}
    return {"action": "uncertain", "reason": "无法确定，降权待定"}


def investigate_correction(scope, key, text, candidate_facts, an=None) -> dict:
    """调查一条纠正：LLM（节流 + 小预算）判断 update/keep/uncertain；失败回退规则。
    若纠正疑似玩笑，或轻纠错（“其实/改一下”）触及客观稳定事实（生日/血型/身份等），
    一律保守 keep——单次弱信号不更新高可信锚点，也不消耗 LLM 预算。"""
    if not candidate_facts:
        return {"action": "keep", "reason": "无候选记忆"}
    row = next(
        (r for r in _db.memory_rows(scope, key) if r["fact"] == candidate_facts[0]), None
    )
    if not row:
        return {"action": "keep", "reason": "未找到旧记忆"}
    old_conf = float(row.get("confidence", 0.7))
    valid_from = row.get("valid_from") or row.get("updated_at") or ""
    access_count = 0
    for m in _db.meta_rows(scope, key):
        if m["fact"] == row["fact"]:
            access_count = int(m.get("access_count", 0))
            break
    cls = policy.fact_class(scope, key, row["fact"])
    joke = float((an or {}).get("joke_probability", 0.0))
    if not (an or {}).get("correction_strong") and (
        joke >= 0.5 or cls == "stable"
    ):
        return {"action": "keep", "reason": "玩笑或轻纠错，稳定事实不更新"}
    if not (an or {}).get("correction_strong"):
        return {"action": "uncertain", "reason": "轻纠错，按冲突降权"}
    if not _cfg("llm_investigate", True):
        return _rule_decision(old_conf, valid_from, scope, access_count)
    now = time.time()
    if now - _investigate_state.get(scope, 0.0) < float(_cfg("investigate_throttle_s", 600)):
        return _rule_decision(old_conf, valid_from, scope, access_count)
    try:
        resp = _shared.deepseek_chat(
            messages=[
                {
                    "role": "system",
                    "content": "你是记忆核查器。判断旧记忆是否应被用户的新说法替换。只输出 JSON。",
                },
                {
                    "role": "user",
                    "content": INVESTIGATE_PROMPT.format(
                        old=candidate_facts[0][:60],
                        conf=round(old_conf, 2),
                        valid=str(valid_from)[:10],
                        new=(text or "")[:120],
                        candidates="；".join(candidate_facts[:2])[:100],
                    ),
                },
            ],
            max_tokens=100,
            temperature=0.0,
            module="world",
        )
        raw = (resp.choices[0].message.content or "").strip()
        data = parse_json_object(raw)
        if data is None:  # 无有效 JSON → 回退规则，不默认 uncertain
            return _rule_decision(old_conf, valid_from, scope, access_count)
        action = str(data.get("action", "uncertain"))
        if action not in ("update", "keep", "uncertain"):
            action = "uncertain"
        _investigate_state[scope] = now
        return {"action": action, "reason": str(data.get("reason", ""))[:100]}
    except Exception as e:
        _stats_err(e)
        return _rule_decision(old_conf, valid_from, scope, access_count)


def stats(scope=None) -> dict:
    """世界模型现状（治理/调试）。"""
    n_active = 0
    n_contested = 0
    rows = _db.memory_rows(scope)
    for r in rows:
        st = r.get("status", "active")
        if st == "active":
            n_active += 1
        elif st == "contested":
            n_contested += 1
    return {
        "active": n_active,
        "contested": n_contested,
        "snapshot_cache_entries": len(_snapshot_cache),
        "investigate_throttle_s": float(_cfg("investigate_throttle_s", 600)),
        "budget_chars": int(_cfg("budget_chars", 400)),
    }



def _stats_err(e):
    """裸 except 审计（v2.2）：错误计数 + 日志，供消融/排查。"""
    try:
        import memory.stats as _st
        _st.bump_err("world", e)
    except Exception:
        pass
