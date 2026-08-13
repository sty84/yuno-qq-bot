"""证据门控 v2：生成后验证（代码级拦截，不依赖 LLM 自觉）。

core.ask 拿到 reply 之后调用 contains_unsupported_claim：
- 黑名单词拦截：输出含已确认虚构词（pack 的 banned_claims）→ 重写；
- 断言-证据核对：输出含"约好了/说好了/答应过"等断言模式 → 证据集（检索命中的 fact + 约定表）
  里没有对应内容 → 重写。

人格无关：只检查"输出里的断言有没有证据"，不关心"阿拉蕾还是老王"。
"""

import re

from memory.extract import fact_keywords

# 断言模式：只取"陈述已有承诺"的高精度形式，避免误伤正常提议/告别
# （"去看电影吧"是提议、"明天见"是告别，都不拦；"我们约好了明天见"才是无据断言）
CLAIM_PATTERNS = (
    r"约好了", r"说好了", r"答应过", r"答应你", r"答应我了", r"约过", r"约的", r"不是约好", r"约好过",
)
# 用户"提议型"约定句式：bot 是在回应刚提出的约定（会话内即证据），不拦
USER_PROPOSAL_RE = re.compile(
    r"明天.*(?:见|去)|下午.*(?:见|去)|一起去|去看|到时候见|见个面|约.*吧|什么时候.*见"
)


def _evidence_set(evidence) -> set:
    """归一证据集：检索命中的 fact / 约定表条目文本。"""
    out = set()
    for f in (evidence or []):
        if isinstance(f, str):
            out.add(f)
        elif isinstance(f, dict):
            for k in ("fact", "text", "content"):
                if f.get(k):
                    out.add(str(f[k]))
    return out


def contains_unsupported_claim(reply, evidence=None, banned=None, user_text="", check_claims=True):
    """返回命中原因（str）；None = 通过。user_text=本轮用户消息（提议型约定视为会话内证据）。
    check_claims=False：只做黑名单层（主动催约场景——授权来自约定本身，断言不逐字核对）。"""
    t = str(reply or "").strip()
    if not t:
        return None
    for w in (banned or []):
        if w and w in t:
            return f"黑名单:{w}"
    if USER_PROPOSAL_RE.search(str(user_text or "")):
        return None  # 用户在提议约定，bot 的确认有会话内依据
    if not check_claims:
        return None
    ev = _evidence_set(evidence)
    for pat in CLAIM_PATTERNS:
        m = re.search(pat, t)
        if not m:
            continue
        seg = t[max(0, m.start() - 15): m.end() + 35]
        seg_tok = fact_keywords(seg)
        # 去掉断言模式自身的词元（约好/说好/答应…），只核对"具体内容"是否在证据里
        content_tok = seg_tok - fact_keywords(m.group(0))
        if not content_tok or not ev or not any(content_tok & fact_keywords(e) for e in ev):
            return f"无证据断言:{pat}"
    return None


def _sem_cfg(key, default):
    try:
        from plugins import _shared
        g = (_shared.CONFIG.get("memory", {}).get("core", {}) or {}).get("evidence_gate", {}) or {}
        return g.get(key, default)
    except Exception:
        return default


def semantic_annotate(reply, evidence=None, banned=None):
    """方向 3（语义自检）：LLM 把回复里的断言按依据分类（日程表/记忆/推断/编造），
    只放行"日程表/记忆"，推断必须含糊、编造一律拦截。
    正则预筛 + min_reply_len 控制成本；失败返回 None（默认放行，硬门仍在）。"""
    t = str(reply or "").strip()
    if len(t) < int(_sem_cfg("min_reply_len", 40)):
        return None
    for w in (banned or []):
        if w and w in t:
            return f"黑名单:{w}"
    ev = _evidence_set(evidence)
    ev_txt = "；".join(list(ev)[:10])[:400]
    prompt = (
        "下面是一条 AI 回复。把其中每个断言按依据分类："
        "日程表(结构化日程/约定)、记忆(检索到的记忆)、推断(合理猜测)、编造(无任何依据的虚构)。"
        "只输出 JSON：{\"assertions\":[{\"text\":\"...\",\"basis\":\"日程表|记忆|推断|编造\"}]}。\n"
        f"回复：{t[:400]}\n"
        f"可用记忆/日程：{ev_txt or '（无）'}\n"
        "依据要求：只有'日程表'和'记忆'可当事实陈述；'推断'必须用'可能/我猜'；'编造'一律不允许。"
    )
    try:
        import json
        from plugins import _shared
        resp = _shared.deepseek_chat(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=220, temperature=0.1,
            module="evidence_annotate",
        )
        raw = (resp.choices[0].message.content or "").strip()
        s, e = raw.find("{"), raw.rfind("}")
        if s < 0:
            return None
        data = json.loads(raw[s:e + 1])
        for a in data.get("assertions", []) or []:
            basis = str(a.get("basis", "")).strip()
            if basis == "编造":
                return f"语义编造:{str(a.get('text', ''))[:40]}"
            if basis == "推断":
                return f"语义推断:{str(a.get('text', ''))[:40]}"
    except Exception:
        pass
    return None
