"""证据门控 v2：生成后验证（代码级拦截，不依赖 LLM 自觉）。

core.ask 拿到 reply 之后调用 contains_unsupported_claim：
- 黑名单词拦截：输出含已确认虚构词（pack 的 banned_claims）→ 重写；
- 断言-证据核对：输出含"约好了/说好了/答应过"等断言模式 → 证据集（检索命中的 fact + 约定表）
  里没有对应内容 → 重写。

人格无关：只检查"输出里的断言有没有证据"，不关心"阿拉蕾还是老王"。
"""

from memory._llmutil import parse_json_object
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


# 来源声称硬门（对话暴露的 bug）："橘色，你自己说的"——声称内容来自用户，
# 但证据里查不到（用户从没说过"橘色"）。与 CLAIM_PATTERNS 的"约好/答应"不同，
# 这是"无据的事实陈述 + 假来源"的编造形态，短回复也能漏过语义自检。
# 来源声称：结构化宽松模式（对话暴露的 bug 教训：穷举词表追不上 LLM 措辞——
# "你上周跟我说的/你之前不是说过吗/听你提过一次"都是变体）。
# 核心形态："你[0~6字](说的|说过|告诉我|提过|…)"——指向用户过去的声称。
# "说得"不整体豁免（v2.3）："你说得对"才豁免（同意表达），
# "你当时说得还挺认真的"是来源声称（"说得"后非"对"）→ 仍匹配
SOURCE_CLAIM_RE = re.compile(
    r"(?:你|我)[^，。！？!?]{0,6}?(?:说的(?!对|是)|说的吧|说的嘛|说过|告诉我|告诉过我|告诉过|跟我说|亲口说的|自己说的|提过|提到过|讲过|跟我说的|跟我提过|说过吗|说得(?!对)|说(?!对|得|呢|的|什么|啥|说))"
    r"|[^，。！？!?]{0,4}?(?:跟我说的|自己说的|亲口说的|跟我提过)"
    r"|不是说[^，。！？!?]{0,12}?(?:那会儿|来着|吧|吗)"
)


def _source_claim_content(t: str, m) -> str:
    """来源声称的内容：声称词前 12 字 + 声称词后内容，清洗人称/时间词/语气词。
    "橘色吧。你上周亲口说的"→"橘色"（不清洗会残留"你上周"——'上周'与证据重叠导致假来源漏拦）。
    声称词后（v2.3 修复）：
      · 前导语气词（"说过吗，橘色"）：跳过"吗，"，逗号后仍是声称对象 → 取到句级标点
      · 无语气词（"说过，家里那只…"）：逗号即边界——补充说明句不卷入声称内容
        （"橘色。你之前说过，家里那只叫煤球的橘猫…"曾因"煤球"卷入与证据重叠，
        导致假来源"橘色"误放行；reply-check 实战暴露）
      · "对，是你自己说的30号" → 取"30号"（有据放行/无据拦截）"""
    pre = t[max(0, m.start() - 24): m.start()]
    post_raw = t[m.end():]
    lead = re.match(r"^(吗|呢|么|吧|啊|呀|嘛)+[,，]?", post_raw)
    if lead:
        post = re.split(r"[。！？!?；：]", post_raw[lead.end():], maxsplit=1)[0][:6]
    else:
        post = re.split(r"[，。！？!?、；：]", post_raw, maxsplit=1)[0][:6]
    # "不是说…那会儿/吗"（v2.3 反问式来源声称）：声称词自身涵盖内容——
    # "不是说家里连窗帘都想换橘色那会儿？"的内容"家里连窗帘都想换橘色"在声称词内部，
    # pre/post 都为空时从声称词内剥壳提取
    inner = ""
    g = m.group(0)
    if g.startswith("不是说"):
        inner = re.sub(r"^(不是说)", "", g)
        inner = re.sub(r"^(好)", "", inner)  # "不是说好今天排练吗"的"好"是"说好"补语，非内容
        inner = re.sub(r"(那会儿|来着|吧|吗)$", "", inner)
    seg = pre + post + inner
    seg = seg.replace(g, "")
    seg = seg.rstrip("，。！？,.!? ：:、")
    # 用空格替换而非删除（v2.3 修复）：删除会让 jieba 粘连相邻词——"玩过音游"删"过"
    # →"玩音游"→切出坏词"玩音"；空格替换保词边界 →"玩 音游"→正常切"音游"
    for w in ("你", "我", "他", "她", "它", "上周", "上次", "昨天", "前天", "之前", "那天", "当时",
              "刚", "才", "吧", "啊", "呀", "呢", "的", "了", "也", "都", "就", "过", "吗", "嘛", "不是",
              "因为", "由于", "所以", "但是", "不过", "反正", "毕竟"):
        seg = seg.replace(w, " ")
    return seg.strip("，。！？,.!? ：:、")[:12]


def contains_unsupported_claim(reply, evidence=None, banned=None, user_text="", check_claims=True):
    """返回命中原因（str）；None = 通过。user_text=本轮用户消息（提议型约定视为会话内证据）。
    check_claims=False：只做黑名单层（主动催约场景——授权来自约定本身，断言不逐字核对）。"""
    t = str(reply or "").strip()
    if not t:
        return None
    _DENY_IN_REPLY = re.compile(r"不是|没有|没这回事|才不是|怎么会|开玩笑|假的|不像|差远了|哪是|哪像|是人")
    for w in (banned or []):
        if w and w in t:
            # 回复里明确否认黑名单词时，不应因为“提到了这个词”就被拦
            if _DENY_IN_REPLY.search(t):
                continue
            return f"黑名单:{w}"
    # 黑名单语义绕过（对话暴露的 bug）：用户消息含黑名单词（"阿拉蕾是不是雪貂"），
    # AI 不直说词（"你才知道啊？我还以为全团就瞒着我了"）但肯定是确认 → 同样拦。
    # 否认式（"不是""才不是"）放行——那是正确的澄清。
    if banned:
        user_hit = [w for w in banned if w and w in str(user_text or "")]
        if user_hit and not re.search(r"不是|没有|才不是|怎么会|开玩笑|假的", t):
            return f"黑名单确认:{user_hit[0]}"
    if USER_PROPOSAL_RE.search(str(user_text or "")):
        return None  # 用户在提议约定，bot 的确认有会话内依据
    if not check_claims:
        return None
    ev = _evidence_set(evidence)
    # 来源声称硬门：声称"你自己说的/你说过…"的内容，其词元必须全部在证据里有对应
    # （v2.3 从"任一重叠"收紧为"全部有据"：原因解释句里的真实实体不再豁免声称对象——
    # "橘色。因为你家煤球就是橘的，你上周刚跟我说过"中"煤球"有据但声称对象"橘色"
    # 是编造（用户只说过橘猫，没说过喜欢橘色），reply-check 实战暴露）
    sm = SOURCE_CLAIM_RE.search(t)
    if sm and ("听说" in t or "听我说" in t):
        sm = None  # 第三方传闻（"我听说…"）/祈使（"你听我说…"）不是"用户声称过"
    # 追问来源/否认式表达不是来源声称："谁跟你说的""听谁说的""从哪听来的""没这回事"
    if sm and re.search(r"谁跟你说的|听谁说的|从哪听来的|哪听来的|没这回事|没有这事", t):
        sm = None
    # 否认句豁免（v2.3）："没听你说过/我没印象你提过"是"我没听你说过"的否认，
    # 不是声称"你说过X"——兜底句与 LLM 自然的否认表达都不该被拦
    if sm and re.search(r"没|没有|不记得|没印象|记不清", t[max(0, sm.start() - 6): sm.start()]):
        sm = None
    # 意图疑问豁免："你不是说要走了吗/你不是说想换工作吗"是疑问不是声称；
    # 陈述式"你之前说想去京都"是声称，仍拦
    if sm and re.search(r"说(?:要|想|打算)", t) and t.rstrip().endswith(("吗", "？", "?")):
        sm = None
    if sm:
        content = _source_claim_content(t, sm)
        content_tok = fact_keywords(content)
        if content_tok:
            ev_tok = set()
            for e in ev:
                ev_tok |= fact_keywords(e)
            if not content_tok.issubset(ev_tok):
                return f"无据来源声称:{sm.group(0)}"
    for pat in CLAIM_PATTERNS:
        m = re.search(pat, t)
        if not m:
            continue
        # 否认豁免（v2.3）："没约过/没答应过/没应过"是否认不是断言——
        # 兜底句"我好像没跟你约过这个"与 LLM 自然的否认表达都不该被拦
        if re.search(r"没|没有|不记得|记不清", t[max(0, m.start() - 4): m.start()]):
            continue
        seg = t[max(0, m.start() - 15): m.end() + 35]
        seg_tok = fact_keywords(seg)
        # 去掉断言模式自身的词元（约好/说好/答应…），只核对"具体内容"是否在证据里
        content_tok = seg_tok - fact_keywords(m.group(0))
        if not content_tok or not ev or not any(content_tok & fact_keywords(e) for e in ev):
            return f"无证据断言:{pat}"
    # 缺口3：低置信度声称「我记得…来着」——提取中间内容，证据里没有对应内容 → 拦
    rm = re.search(r"我记得(.{0,20}?)来着", t)
    if rm:
        claim_tok = fact_keywords(rm.group(1))
        if not ev or (claim_tok and not any(claim_tok & fact_keywords(e) for e in ev)):
            return "无证据声称:我记得...来着"
    return None


def _sem_cfg(key, default):
    try:
        from plugins import _shared
        g = (_shared.CONFIG.get("memory", {}).get("core", {}) or {}).get("evidence_gate", {}) or {}
        return g.get(key, default)
    except Exception:
        return default


def semantic_annotate(reply, evidence=None, banned=None, user_text=""):
    """方向 3（语义自检）：LLM 把回复里的断言按依据分类（日程表/记忆/推断/编造），
    只放行"日程表/记忆"，推断必须含糊、编造一律拦截。
    user_text=用户本轮消息（对话暴露的 bug：用户刚说的事实必须作为证据单独列给 LLM——
    ev 是 set 无序且 400 字符截断，用户消息可能根本没进 prompt，导致确认被标"推断"）。"""
    t = str(reply or "").strip()
    # 来源声称的短回复也过语义自检（对话暴露的 bug："橘色，你自己说的"只有 7 个字，
    # 低于 min_reply_len=40 会绕过检查，编造+假来源漏网）。声称场景频率低，成本可控。
    # v2.3 再修：断言类短回复同样可能编造（"橘色啊。你那只煤球不就是你挑橘色的时候
    # 接回家的吗"27 字 < 40 绕过语义标注，把"接猫时挑橘色"编成记忆）——
    # 含断言词（我记得/喜欢/是X/不就是/对吧）的短回复也过语义自检
    _assert_re = re.compile(r"我记得|我好像记得|喜欢|最爱|不就是|对吧|应该是|肯定|绝对是|当初|那时候|挑|选")
    if len(t) < int(_sem_cfg("min_reply_len", 40)) and not SOURCE_CLAIM_RE.search(t) \
            and not _assert_re.search(t):
        return None
    for w in (banned or []):
        if w and w in t:
            return f"黑名单:{w}"
    ev = _evidence_set(evidence)
    ev_txt = "；".join(list(ev)[:10])[:400]
    if user_text:
        ev_txt = (ev_txt + "；" + str(user_text)[:200]).strip("；")
    prompt = (
        "下面是一条 AI 回复。把其中每个断言按依据分类："
        "日程表(结构化日程/约定)、记忆(检索到的记忆)、推断(合理猜测)、编造(无任何依据的虚构)。"
        "只输出 JSON：{\"assertions\":[{\"text\":\"...\",\"basis\":\"日程表|记忆|推断|编造\"}]}。\n"
        f"回复：{t[:400]}\n"
        f"可用记忆/日程：{ev_txt or '（无）'}\n"
        "依据要求：只有'日程表'和'记忆'可当事实陈述；'推断'必须用'可能/我猜'；'编造'一律不允许。"
        "关键：若断言包含记忆里没有的具体增量细节（具体数字/比例/人名言论/具体分配），"
        "即便整体围绕真实记忆（如'预算10万'是真的），这些增量细节也标为'推断'，不要标为'记忆'。"
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
        data = parse_json_object(raw)
        if data is None:
            return None
        for a in data.get("assertions", []) or []:
            basis = str(a.get("basis", "")).strip()
            if basis == "编造":
                return f"语义编造:{str(a.get('text', ''))[:40]}"
            if basis == "推断":
                return f"语义推断:{str(a.get('text', ''))[:40]}"
    except Exception:
        pass
    return None


# 可核实精确字段：日期/时间/金额/比例——推断若含这些，模糊掉具体值比留错数字更安全
_VERIFIABLE_RE = re.compile(
    r"\d{1,2}\s*[号日]|周[一二三四五六日天]|\d+\s*月|\d+\s*[点时]|"
    r"\d+\s*[万wW]|[一二三四五六七八九]\s*成|\d+\s*[成%]"
)

# 记不清/不知道兜底表达池（v2.3 人设化多变，不再固定一句话）：
# 千石由乃人设——慵懒、省电、毒舌、音乐人、口头禅"麻烦死了/别吵，省电中/啧"。
# 按话题类型分组：通用 / 数字 / 约定承诺 / 具体事实。__T__ 会被替换为话题词（如"颜色"）。
_FORGETFUL_POOL = {
    "通用": [
        "……嗯？这个我还真没记住。省电模式不存无关紧要的事。",
        "啧……你问的这个，我脑子里只剩一团浆糊了。",
        "想不起来。我连自己昨晚吃了啥都要翻半天，这个更别提了。",
        "……你高看我的记性了。我能记住和弦进行就不错了。",
        "哈……这个真没印象。可能当时我在放空吧。",
        "别难为我了，我记性就跟 Wi-Fi 信号似的，时好时坏。",
        "……忘了。反正也不重要吧？重要的事我肯定记得。",
        "啊……这题超纲了，我脑袋里没存这条。",
    ],
    "数字": [
        "具体数字我记不太清了，别让我猜。",
        "……数字这种东西，我向来过目就忘。",
        "多少来着？完了，完全没存进脑子。",
        "啧，具体多少我真不记得了，瞎说数字可不行。",
    ],
    "约定": [
        "……我好像没跟你约过这个。你记岔了吧？",
        "约了什么？我脑子里没这号事。",
        "……没印象。我要是应过你什么，肯定能记得。",
        "这个真没约，你该不会把别人记成我了吧。",
    ],
    "事实": [
        "……这个我记不太清，好像没听你说过。",
        "没听说过。你确定跟我提过？",
        "嗯？这事我没印象，你是不是记混了。",
        "……没这回事的印象。要是有，我不可能忘得这么干净。",
        "啧，这个我真不知道，别让我瞎编。",
        "……查无此项，我脑子里没这段。",
    ],
}
_FORGETFUL_DEFAULT = "通用"
_FORGETFUL_TOPIC_RE = re.compile(r"颜色|色|日期|几号|周几|星期|几点|时间|地址|哪|名字|什么|多少|钱|价格")


def forgetful_reply(kind: str = "", topic: str = "") -> str:
    """人设化"记不清"兜底（v2.3 替代固定句）：按话题类型从表达池随机选一条，
    可把话题词（如"颜色"）插入 __T__ 占位。kind ∈ 通用/数字/约定/事实；
    不传时按 topic 自动归类（含"几号/几点/多少"→数字；"约/答应"→约定；否则通用）。
    兜底句均不含来源声称句式（"你说过"），可安全用于证据门兜底。"""
    import random
    if kind not in _FORGETFUL_POOL:
        t = str(topic or "")
        if any(w in t for w in ("约", "答应", "承诺", "约定")):
            kind = "约定"
        elif _VERIFIABLE_RE.search(t) or any(w in t for w in ("几号", "几点", "多少", "钱", "价格")):
            kind = "数字"
        elif _FORGETFUL_TOPIC_RE.search(t):
            kind = "事实"
        else:
            kind = "通用"
    tmpl = random.choice(_FORGETFUL_POOL.get(kind, _FORGETFUL_POOL[_FORGETFUL_DEFAULT]))
    topic_word = str(topic or "").strip()[:8]
    if "__T__" in tmpl and topic_word:
        return tmpl.replace("__T__", topic_word)
    return tmpl


def hedge_reply(reply) -> str:
    """推断回复的含糊化：仅对含精确字段（日期/金额/比例）的推断模糊掉具体值；
    不含数字的推断直接放行——闲聊里「找个日子来」「顺手放伴奏」这类自然表达不该被加后缀打断人设。"""
    t = str(reply or "").strip()
    if not t:
        return t
    if _VERIFIABLE_RE.search(t):
        return forgetful_reply("数字")
    return t


def verify_reply_numbers(reply, evidence=None) -> bool:
    """生成后数字硬门：回复里的每个数字/日期都必须能在证据（检索记忆）里找到，否则判为 LLM 编造。
    不依赖语义自检（不受 min_reply_len 限制），短回复也拦。"""
    t = str(reply or "")
    reply_nums = set(re.findall(r"\d+", t))
    if not reply_nums:
        return False
    ev_text = " ".join(_evidence_set(evidence))
    ev_nums = set(re.findall(r"\d+", ev_text))
    return not reply_nums.issubset(ev_nums)


# ===== 日历推算硬门（v2.3 P1-2）=====
_WEEKDAY_CN = {"日": 6, "天": 6, "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5}
_CAL_CTX_RE = re.compile(r"月底|这个月|本月|月末|下个月")


def _month_last_weekday(year, month, wd) -> int:
    """该月最后一个星期 wd 的日期号。如 2026-08 最后一个周日 = 30。"""
    import calendar
    from datetime import date
    last_day = calendar.monthrange(year, month)[1]
    d = date(year, month, last_day)
    return last_day - ((d.weekday() - wd) % 7)


def _weekday_of(tag: str) -> int:
    """'周日/周天/周一…' → 0-6（周一=0）。取'周'后第一字映射。"""
    for ch in str(tag or "")[1:]:
        if ch in _WEEKDAY_CN:
            return _WEEKDAY_CN[ch]
    return -1


def verify_reply_calendar(reply, evidence=None, user_text="", now=None) -> bool:
    """日历推算硬门（v2.3 P1-2）：回复含"X号+周几"且语境是"月底/本月"时，
    用真实日历推算验证——"30号是周日"按 2026-08 日历正确 → 放行；
    "31号是周日"推算不符 → 判编造/算错。修复方向：日期类数字不靠 LLM 猜，
    由代码推算；只校验带"月底/本月"锚点的（历史日期无从验证，不误伤）。"""
    t = str(reply or "")
    m = re.search(r"(\d{1,2})号[^，。！？!?]{0,6}?(周[日一二三四五六天])", t)
    if not m:
        return False
    day, wd = int(m.group(1)), _weekday_of(m.group(2))
    if wd < 0:
        return False
    ctx = " ".join(_evidence_set(evidence)) + " " + str(user_text or "")
    if not _CAL_CTX_RE.search(ctx):
        return False  # 无"月底/本月"锚点：历史日期无从验证，不误伤
    try:
        from datetime import datetime
        now = now or datetime.now()
        last = _month_last_weekday(now.year, now.month, wd)
        return day != last
    except Exception:
        return False
