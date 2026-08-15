"""犹豫层（v2.3）：模拟人类发消息前的斟酌——软硬分离 + 概率化 + 可消融。

- 硬门（证据门控/黑名单）不参与犹豫，直接拦死；
- 本层只调"节奏/措辞/时机"：LLM 内心独白评估 → 概率门（send/rewrite/hold/discard）；
- 超时/失败默认放行；discard 上限封顶（discard_cap）；延迟上限 ≤ delay_max_s；
- 计数（hesitation_eval / hesitation_send|rewrite|hold|discard）供管理台消融观测。

配置：memory.core.hesitation.{enabled, sample_rate, skip_kinds, delay_max_s,
discard_cap, rewrite_prob, hold_prob}。
"""

from memory._llmutil import parse_json_object
import random
from plugins import _shared


_SAFE_SHORT_RE = None


def _safe_short_re():
    global _SAFE_SHORT_RE
    if _SAFE_SHORT_RE is None:
        import re
        _SAFE_SHORT_RE = re.compile(
            r"^(晚安|早安|早|哈哈+|嗯+|好的?|知道啦?|拜拜|再见|睡啦?|加油|在吗|我回来了|我先去忙了)[！!。.]?$"
        )
    return _SAFE_SHORT_RE


def _obviously_safe(msg) -> bool:
    """规则预筛：极短、无断言、纯日常寒暄的主动消息不调 LLM 犹豫。"""
    return bool(_safe_short_re().match(str(msg or "").strip()))


def _cfg(key, default):
    return _shared.core_cfg("hesitation", key, default)
def enabled() -> bool:
    return bool(_cfg("enabled", True))


def _should_evaluate(kind) -> bool:
    """成本控制：低频/高价值才评估；可配置 skip_kinds 或采样率。"""
    if kind in (_cfg("skip_kinds", []) or []):
        return False
    return random.random() < float(_cfg("sample_rate", 1.0))


def _evaluate(msg, scope, kind, ctx=""):
    """LLM 内心独白评估 → 结构化 JSON；失败/超时返回 None（调用方默认放行）。"""
    try:
        from plugins import _shared
        prompt = (
            "你刚写完一条准备主动发给用户的消息，发之前你犹豫了一下。"
            "以角色内心独白的方式评估，只输出 JSON："
            '{"action":"send|rewrite|hold|discard","delay_s":3,"rewrite":"改口后的版本",'
            '"thought":"一句内心独白","basis":"日程表|记忆|推断|编造"}。\n'
            f"消息：{msg}\n"
            f"场景：{kind}\n"
            + (f"上下文：{ctx}\n" if ctx else "")
            + "要求：模拟'这话是不是有点烦/这个点他可能在忙/算了还是说吧'这类真实斟酌；"
            "delay_s 0~10；只有真的觉得不该说才 discard；rewrite 在改口时给出改后的文本；"
            "basis 标注这条消息的依据：日程表/记忆可发，推断要含糊，编造必须 discard。"
        )
        resp = _shared.deepseek_chat(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=160, temperature=0.8,
            module="hesitation", detail=kind,
        )
        raw = (resp.choices[0].message.content or "").strip()
        d = parse_json_object(raw)
        if d is None:
            return None
        action = str(d.get("action", "send")).strip().lower()
        if action not in ("send", "rewrite", "hold", "discard"):
            action = "send"
        return {
            "action": action,
            "delay_s": max(0, min(10, int(d.get("delay_s") or 0))),
            "rewrite": str(d.get("rewrite") or "").strip()[:200],
            "thought": str(d.get("thought") or "").strip()[:100],
            "basis": str(d.get("basis") or "").strip()[:10],
        }
    except Exception:
        return None


def _bump(key):
    try:
        import memory.stats as _st
        _st.bump(key)
    except Exception:
        pass


def gate(msg, scope="", kind="generic", ctx=""):
    """主动消息犹豫门：返回 {action, delay_s, msg, reason, monologue}。
    action ∈ send / rewrite / hold / discard；discard 才真正不发，其余都发（最多延迟/改口）。"""
    out = {"action": "send", "delay_s": 0, "msg": msg, "reason": "", "monologue": ""}
    if not enabled() or not msg:
        return out
    if _cfg("skip_safe", True) and _obviously_safe(msg):
        out["reason"] = "safe_rule"
        return out
    _bump("hesitation_eval")
    if not _should_evaluate(kind):
        out["reason"] = "skip"
        return out
    r = _evaluate(msg, scope, kind, ctx)
    if not r:
        out["reason"] = "eval_fail_send"  # 超时/失败 → 默认放行
        return out
    delay_max = float(_cfg("delay_max_s", 10))
    discard_cap = float(_cfg("discard_cap", 0.2))
    out["monologue"] = r.get("thought", "")
    if r.get("basis") == "编造":
        # 方向 3 折叠：依据标注为编造 → 强制 discard（不进入概率门）
        out["action"] = "discard"
        out["reason"] = "basis_fabricated"
        out["delay_s"] = 0
        _bump("hesitation_discard")
        return out
    action = r["action"]
    if action == "send":
        out["reason"] = "send"
        out["delay_s"] = min(delay_max, r.get("delay_s", 0))
    elif action == "rewrite":
        rw = r.get("rewrite") or ""
        if rw and rw != str(msg):
            if random.random() < float(_cfg("rewrite_prob", 0.6)):
                out["action"] = "rewrite"
                out["msg"] = rw
                out["reason"] = "rewrite"
                out["delay_s"] = min(delay_max, r.get("delay_s", 0))
            else:
                out["action"] = "hold"  # "打完又改回原样"
                out["reason"] = "rewrite_keep_original"
                out["delay_s"] = min(delay_max, r.get("delay_s", 0))
        else:
            out["action"] = "hold"
            out["reason"] = "rewrite_no_version"
            out["delay_s"] = min(delay_max, r.get("delay_s", 3))
    elif action == "hold":
        out["action"] = "hold"
        out["reason"] = "hold"
        out["delay_s"] = min(delay_max, r.get("delay_s", 3))
    elif action == "discard":
        if random.random() < discard_cap:  # 小概率"打了又删"，上限封顶
            out["action"] = "discard"
            out["reason"] = "discard"
        else:
            out["action"] = "hold"  # 犹豫后还是发了
            out["reason"] = "discard_softened"
            out["delay_s"] = min(delay_max, r.get("delay_s", 3))
    _bump(f"hesitation_{out['action']}")
    if out.get("reason") and out["reason"] != "skip":
        try:
            from datetime import datetime
            from plugins import _db
            _db.hesitation_log_add(
                datetime.now().isoformat(timespec="seconds"),
                scope, kind, out["action"], out["reason"], out["delay_s"], out.get("monologue", ""),
            )
        except Exception:
            pass
    return out


def stats() -> dict:
    """犹豫/门控统计（管理台数据源）：评估次数、各动作率、证据门控拦截数。"""
    try:
        import memory.stats as _st
        c = _st.counters()
        ev = int(c.get("hesitation_eval", 0))
        send = int(c.get("hesitation_send", 0))
        rw = int(c.get("hesitation_rewrite", 0))
        hold = int(c.get("hesitation_hold", 0))
        disc = int(c.get("hesitation_discard", 0))
        gate = int(c.get("evidence_gate_block", 0))
        hedge = int(c.get("evidence_gate_hedge", 0))
        return {
            "eval": ev,
            "send": send, "rewrite": rw, "hold": hold, "discard": disc,
            "send_rate": round(send / max(1, ev), 3),
            "rewrite_rate": round(rw / max(1, ev), 3),
            "hold_rate": round(hold / max(1, ev), 3),
            "discard_rate": round(disc / max(1, ev), 3),
            "evidence_gate_block": gate,
            "evidence_gate_hedge": hedge,
        }
    except Exception:
        return {}
