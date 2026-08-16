"""Memory Trace Export（v10）：记录记忆形成过程的可解释轨迹。

原则：不保存完整思维链，只记录可解释的处理结果、决策理由和结构化信息，
供调试错误记忆来源、分析回答依据、人工审核记忆质量、生成评测集。
"""

import json
import re
import time
from datetime import datetime

from plugins import _db, _shared

GOAL_KEYWORDS = ("目标", "打算", "计划", "想达成", "争取", "为了")

_LOW_INFO_WORDS = (
    "在吗", "在么", "在不在", "你在吗", "你在么", "你在干嘛", "在干嘛",
    "干嘛呢", "干什么呢", "忙吗", "忙不忙", "你忙吗", "有空吗", "你有空吗",
    "你好", "嗨", "哈喽", "再见", "拜拜", "晚安", "早安", "早上好", "下午好",
    "晚上好", "辛苦了", "谢谢", "感谢", "哈哈", "呵呵", "嗯", "哦", "好的",
    "好", "行", "ok", "OK", "知道啦", "睡啦", "加油", "我回来了", "我先去忙了",
)
_LOW_INFO_RE = re.compile(
    r"^(?:(?:你|您)?(?:在|忙|有空|干嘛|干什么|咋|怎么)[吗么呢吧]?|"
    r"你好|嗨|哈喽|再见|拜拜|晚安|早安|辛苦了|谢谢|哈哈+|嗯+|哦+|好的?|知道啦?|睡啦?|加油|我回来了|我先去忙了)"
    r"[！!。.？?]?$"
)

_dedup_cache: dict[tuple[str, str], float] = {}


def _cfg(key, default):
    return _shared.core_cfg("trace", key, default)
def enabled() -> bool:
    return bool(_cfg("enabled", True))


def is_low_information(text) -> bool:
    """判断是否为低信息密度句子：问候/寒暄/再见/语气词等。"""
    t = re.sub(r"\s+", "", str(text or "")).strip("？?。！!，,、")
    if not t:
        return True
    if t.lower() in _LOW_INFO_WORDS:
        return True
    if len(t) <= 2:
        return True
    return _LOW_INFO_RE.fullmatch(t) is not None


def _is_duplicate(scope, raw_content, window_min=None) -> bool:
    """同 scope 同句子在时间窗口内只记录一条。"""
    window = int(window_min if window_min is not None else _cfg("dedup_window_min", 10))
    key = (str(scope or ""), str(raw_content or ""))
    now = time.time()
    last = _dedup_cache.get(key)
    if last is not None and now - last < window * 60:
        return True
    _dedup_cache[key] = now
    return False

# 多维度评分（v11）：每维 1~5 分，评分驱动行为调整
DIMENSIONS = ("extraction", "decision", "confidence", "provenance", "privacy")
DIMENSION_LABELS = {
    "extraction": "提取准确性",
    "decision": "决策合理性",
    "confidence": "置信度校准",
    "provenance": "来源可信度",
    "privacy": "隐私处理",
}


def record(
    scope,
    speaker="user",
    raw_content="",
    semantic=None,
    intent="",
    entities=None,
    events=None,
    emotion="",
    slang=None,
    candidate="",
    action="",
    memory_id="",
    confidence=None,
    source="",
    reasoning="",
    modules=None,
    hint="",
    conversation_id="",
):
    """写一条记忆处理轨迹（失败静默，不影响主流程）。"""
    if not enabled():
        return
    if is_low_information(raw_content):
        return
    if _is_duplicate(scope, raw_content):
        return
    try:
        semantic = dict(semantic or {})
        ts = datetime.now().isoformat(timespec="seconds")
        _db.trace_add(
            conversation_id=conversation_id or f"{scope}:{ts[:10]}",
            ts=ts,
            scope=scope,
            speaker=str(speaker)[:20],
            raw_content=raw_content,
            semantic_analysis=json.dumps(semantic, ensure_ascii=False),
            intent=intent or str(semantic.get("intent", "")),
            entities=json.dumps(entities if entities is not None else semantic.get("entities", []), ensure_ascii=False),
            events=json.dumps(events or [], ensure_ascii=False),
            emotion=emotion or str(semantic.get("emotion", "")),
            slang_interpretation=json.dumps(slang if slang is not None else semantic.get("expressions", []), ensure_ascii=False),
            memory_candidate=candidate,
            memory_action=action,
            memory_id=memory_id,
            confidence=confidence,
            source=source,
            reasoning=reasoning,
            affected_modules=json.dumps(modules or ["memory"], ensure_ascii=False),
            context_hint=hint,
        )
    except Exception as e:
        _stats_err(e)
        pass


def detect_modules(scope, key="", text="") -> list:
    """按处理对象推断影响的模块（可解释，非思维链）。"""
    modules = ["memory"]
    if scope == "ai" or scope.startswith("ai:"):
        modules.append("persona")
        if key in ("belief", "reflection", "reasoning"):
            modules.append("belief")
    if any(w in (text or "") for w in GOAL_KEYWORDS):
        modules.append("goal")
    return modules


def prune(days=None) -> int:
    """清理超过保留期的轨迹（默认 7 天）。"""
    return _db.trace_prune(int(days if days is not None else _cfg("retention_days", 7)))


def score(trace_id, scores=None, comment="", reviewer="", total=None):
    """人工评分接口（v11）：支持多维度 dict 或单个分数；低分自动进 HITL 审核。"""
    if isinstance(scores, (int, float)):
        total = float(scores)
        scores = {}
    scores = {
        k: max(1.0, min(5.0, float(v)))
        for k, v in dict(scores or {}).items()
        if k in DIMENSIONS
    }
    if total is None:
        total = sum(scores.values()) / len(scores) if scores else 3.0
    _db.trace_review_add(int(trace_id), float(total), scores, comment, reviewer)
    low = [DIMENSION_LABELS.get(k, k) for k, v in scores.items() if v <= 2]
    if low or float(total) <= 2:
        _db.audit_add(
            "review_low_score", f"trace#{trace_id}",
            f"低分维度：{'、'.join(low) if low else '总分'}；评论：{comment[:100]}",
            operator=reviewer or "auto",
        )
    return f"已记录评分（trace #{trace_id}）：{float(total):g}/5"


_adjust_cache = {"ts": 0.0, "data": None}


def _compute_adjustments() -> dict:
    """按近期人工评分聚合各维度平均分，推导行为调整参数。"""
    reviews = _db.trace_review_recent(limit=100)
    dim_avg: dict = {}
    for r in reviews:
        try:
            s = json.loads(r.get("scores") or "{}")
        except Exception as e:
            _stats_err(e)
            s = {}
        for k, v in s.items():
            if k in DIMENSIONS:
                dim_avg.setdefault(k, []).append(float(v))
    avg = {k: sum(v) / len(v) for k, v in dim_avg.items()}
    return {
        "reviews": len(reviews),
        "dimension_averages": {k: round(v, 2) for k, v in avg.items()},
        # 置信度维度低 → 抑制过度自信（新记忆初始置信度打折）
        "confidence_factor": 0.85 if avg.get("confidence") is not None and avg["confidence"] < 3
        else (1.05 if avg.get("confidence") is not None and avg["confidence"] >= 4.2 else 1.0),
        # 决策维度低 → 更严格的信息增益门槛（少写错）
        "igt_threshold": 0.4 if avg.get("decision") is not None and avg["decision"] < 3 else 0.3,
        # 隐私维度低 → 更敏感（更早标记/加密）
        "privacy_threshold": 0.5 if avg.get("privacy") is not None and avg["privacy"] < 3 else 0.8,
        # 提取维度低 → 严格提取模式（提高保留细节要求）
        "extraction_strict": bool(avg.get("extraction") is not None and avg["extraction"] < 3),
    }


def adjustments(force=False) -> dict:
    """当前评分驱动的行为调整（10 分钟缓存）。"""
    now = time.time()
    if _adjust_cache["data"] and not force and now - _adjust_cache["ts"] < 600:  # type: ignore[operator]
        return _adjust_cache["data"]  # type: ignore[return-value]
    data = _compute_adjustments()
    try:
        conv = _db.kv_get("memory", "conv_adjustments") or {}
        if conv:
            data["convreview"] = {
                "auto_adjust": bool(conv.get("auto_adjust", False)),
                "suggestions": conv.get("suggestions", {}),
                "updated_at": conv.get("updated_at", ""),
            }
            if conv.get("auto_adjust") and conv.get("params"):
                for _k, _v in conv["params"].items():
                    data[_k] = _v
                data["convreview_applied"] = True
    except Exception:
        pass
    _adjust_cache.update({"ts": now, "data": data})  # type: ignore[dict-item]
    return data


def _review_text(rev) -> str:
    try:
        s = json.loads(rev.get("scores") or "{}")
    except Exception as e:
        _stats_err(e)
        s = {}
    dims = " · ".join(f"{DIMENSION_LABELS.get(k, k)}{v:g}" for k, v in s.items())
    base = f"评分 {rev.get('score'):g}/5" + (f"（{dims}）" if dims else "")
    if rev.get("comment"):
        base += f" · {rev['comment']}"
    if rev.get("reviewer"):
        base += f" · {rev['reviewer']}"
    return base


def render_markdown(rows, reviews=None) -> str:
    """把轨迹渲染成人工可读的 Markdown 报告。
    每条轨迹的用户输入带机器可解析标记：
      <!-- TRACE_REVIEW:{"trace_id":123,"conversation_id":"..."} -->  JSON 注释（接口解析用）
      [TRACE:123] 用户："..."                                      可见锚点（人工/grep 用）
    """
    reviews = reviews or {}
    lines = ["# Conversation Memory Trace", ""]
    for r in rows:
        speaker = "用户" if r.get("speaker") == "user" else "系统"
        lines.append(f"## {r.get('ts', '')} · {r.get('scope', '')}")
        lines.append("")
        lines.append("### 用户输入")
        lines.append(
            f"<!-- TRACE_REVIEW:{json.dumps({'trace_id': r['id'], 'conversation_id': r.get('conversation_id', '')}, ensure_ascii=False)} -->"
        )
        lines.append(f"[TRACE:{r['id']}] {speaker}：“{r.get('raw_content', '')}”")
        rev = reviews.get(r["id"])
        if rev:
            lines.append(f"（{_review_text(rev)}）")
        lines.append("")
        lines.append("### AI理解")
        lines.append(f"- 意图：{r.get('intent') or '未知'}")
        lines.append(f"- 情绪：{r.get('emotion') or '平静'}")
        entities = json.loads(r.get("entities") or "[]")
        lines.append(f"- 实体：{'、'.join(str(e) for e in entities) if entities else '无'}")
        evs = json.loads(r.get("events") or "[]")
        lines.append(f"- 事件：{'、'.join(str(e)[:20] for e in evs) if evs else '无'}")
        slang = json.loads(r.get("slang_interpretation") or "[]")
        if slang:
            parts = []
            for s in slang[:3]:
                if isinstance(s, dict):
                    intents = "、".join(
                        f"{m.get('meaning')}({m.get('confidence', '')})" for m in s.get("possible_intents", [])
                    )
                    parts.append(f"{s.get('expression')}→{intents}")
            if parts:
                lines.append(f"- 网络用语：{'；'.join(parts)}")
        lines.append("")
        lines.append("### Memory处理")
        lines.append(f"- 动作：{r.get('memory_action') or '—'}")
        lines.append(f"- 候选记忆：{r.get('memory_candidate') or '（无）'}")
        if r.get("memory_id"):
            lines.append(f"- 涉及记忆：{r.get('memory_id')}")
        if r.get("confidence") is not None:
            lines.append(f"- 置信度：{r.get('confidence')}")
        if r.get("source"):
            lines.append(f"- 来源：{r.get('source')}")
        lines.append(f"- 原因：{r.get('reasoning') or '—'}")
        lines.append("")
        modules = json.loads(r.get("affected_modules") or "[]")
        lines.append("### 影响模块")
        lines.append("、".join(modules) if modules else "memory")
        if r.get("context_hint"):
            lines.append("")
            lines.append(f"未来检索提示：{r.get('context_hint')}")
        lines.append("")
        lines.append("---")
    return "\n".join(lines)



def _stats_err(e):
    """裸 except 审计（v2.2）：错误计数 + 日志，供消融/排查。"""
    try:
        import memory.stats as _st
        _st.bump_err("trace", e)
    except Exception:
        pass
