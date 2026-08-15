"""对话质量评分（v33 convreview）：评分对象从"记忆处理轨迹"换成"对话回合"。

北极星 = 对话质量。每条记录 = 用户消息 + AI 回复（一轮），五维评分：
  记得 / 自然 / 有情绪 / 主动 / 边界。
低分写审计 + 归因提示（对应模块）；第一版**不做自动调参**——对话五维对应的
是模块行为，盲目自动调只会雪上加霜，先攒数据再决定哪些维度能自动调。

结构镜像 memory/trace.py（v11 多维度评分），复用同一套表/命令/管理台模式：
  memory_trace / trace_review  →  conv_log / conv_review
数据源：
  ① 场景回放（tools.py scenario-eval --review-export）
  ② 真实对话（plugins/memory.py after_chat 自动记录）
"""

import json
import time
from datetime import datetime

from plugins import _db, _shared

# 对话质量五维（v33）：每维 1~5 分
CONV_DIMENSIONS = ("remember", "natural", "emotional", "proactive", "boundary")
CONV_DIMENSION_LABELS = {
    "remember": "记得（引用历史不穿帮）",
    "natural": "自然（像人话不机械）",
    "emotional": "有情绪（情绪连贯）",
    "proactive": "主动（会主动分享/推进）",
    "boundary": "边界（不乱编不泄密）",
}
# 低分归因方向：维度 → 该查哪个模块
DIMENSION_HINTS = {
    "remember": "检索失败→query_log 样本 + 缺口守卫触发率",
    "natural": "expression / hesitation / 人设表达",
    "emotional": "emotion VAD / 议题 mood 一致性",
    "proactive": "sharing / appointment / revive 触发",
    "boundary": "证据门控 / 缺口守卫 / privacy 阈值",
}


def _cfg(key, default):
    return _shared.core_cfg("convreview", key, default)


def enabled() -> bool:
    return bool(_cfg("enabled", True))


def record(scope, text, reply, conversation_id=""):
    """记一轮对话（用户消息 + AI 回复）。失败静默，不影响主流程。"""
    if not enabled():
        return
    try:
        ts = datetime.now().isoformat(timespec="seconds")
        _db.conv_add(
            conversation_id=conversation_id or f"{scope}:{ts[:10]}",
            scope=scope,
            ts=ts,
            user_text=str(text)[:500],
            ai_text=str(reply)[:800],
        )
    except Exception:
        pass


def queue(limit=50):
    """对话评分待办：未评过、排除测试/敏感 scope。"""
    rows = _db.conv_rows(limit=300)
    reviewed = _db.conv_review_map([r["id"] for r in rows])
    TEST_MARK = ("guard", "ev:", "poke", "pdtest", "testg", "t:", "priv")
    return [
        r for r in rows
        if r["id"] not in reviewed
        and not any(k in str(r.get("scope", "")) for k in TEST_MARK)
    ][: max(1, int(limit))]


def score(conv_id, scores=None, comment="", reviewer="", total=None):
    """人工评分接口（v33）：五维 1~5；低分自动进 HITL 审计 + 归因提示。"""
    if isinstance(scores, (int, float)):
        total = float(scores)
        scores = {}
    scores = {
        k: max(1.0, min(5.0, float(v)))
        for k, v in dict(scores or {}).items()
        if k in CONV_DIMENSIONS
    }
    if total is None:
        total = sum(scores.values()) / len(scores) if scores else 3.0
    _db.conv_review_add(int(conv_id), float(total), scores, comment, reviewer)
    low = [CONV_DIMENSION_LABELS.get(k, k) for k, v in scores.items() if v <= 2]
    if low or float(total) <= 2:
        hints = "；".join(
            DIMENSION_HINTS.get(k, "") for k, v in scores.items() if v <= 2
        )
        _db.audit_add(
            "conv_low_score", f"conv#{conv_id}",
            f"低分维度：{'、'.join(low) if low else '总分'}；归因：{hints}；评论：{comment[:100]}",
            operator=reviewer or "auto",
        )
    return f"已记录评分（conv #{conv_id}）：{float(total):g}/5"


def _review_text(rev) -> str:
    try:
        s = json.loads(rev.get("scores") or "{}")
    except Exception:
        s = {}
    dims = " · ".join(f"{CONV_DIMENSION_LABELS.get(k, k)}{v:g}" for k, v in s.items())
    base = f"评分 {rev.get('score'):g}/5" + (f"（{dims}）" if dims else "")
    if rev.get("comment"):
        base += f" · {rev['comment']}"
    if rev.get("reviewer"):
        base += f" · {rev['reviewer']}"
    return base


def render_markdown(rows, reviews=None) -> str:
    """把对话渲染成人工可评分的 Markdown 报告（镜像 trace.render_markdown）。
    每条对话带机器可解析标记：
      <!-- CONV_REVIEW:{"conv_id":123,"conversation_id":"..."} -->  JSON 注释（接口解析用）
      [CONV:123] 用户：… / AI：…                            可见锚点（人工/grep 用）
    """
    reviews = reviews or {}
    lines = ["# Conversation Quality Review", ""]
    for r in rows:
        lines.append(f"## {r.get('ts', '')} · {r.get('scope', '')}")
        lines.append("")
        lines.append(
            f"<!-- CONV_REVIEW:{json.dumps({'conv_id': r['id'], 'conversation_id': r.get('conversation_id', '')}, ensure_ascii=False)} -->"
        )
        lines.append(f"[CONV:{r['id']}] 用户：{r.get('user_text', '')}")
        lines.append(f"        AI  ：{r.get('ai_text', '')}")
        rev = reviews.get(r["id"])
        if rev:
            lines.append(f"（{_review_text(rev)}）")
        lines.append("")
        lines.append("---")
    return "\n".join(lines)


_report_cache = {"ts": 0.0, "data": None}


def report(force=False) -> dict:
    """对话五维诊断（第一版只出诊断，不自动调参）：
    各维度均值 + 低分归因方向，供每周看板/消融使用。"""
    now = time.time()
    if _report_cache["data"] and not force and now - _report_cache["ts"] < 600:
        return _report_cache["data"]
    reviews = _db.conv_review_recent(limit=200)
    dim_avg = {}
    for r in reviews:
        try:
            s = json.loads(r.get("scores") or "{}")
        except Exception:
            s = {}
        for k, v in s.items():
            if k in CONV_DIMENSIONS:
                dim_avg.setdefault(k, []).append(float(v))
    avg = {k: round(sum(v) / len(v), 2) for k, v in dim_avg.items()}
    low = {k: DIMENSION_HINTS[k] for k, v in avg.items() if v < 3}
    data = {
        "reviews": len(reviews),
        "dimension_averages": avg,
        "low_dimension_hints": low,
        "auto_adjust": False,  # 明确：第一版不自动调参
    }
    _report_cache.update({"ts": now, "data": data})
    return data
