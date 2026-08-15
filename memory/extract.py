"""记忆提取模块：LLM 从对话中提取关键信息，并把事实归类到事件类型。"""

from memory._llmutil import parse_json_object
import json
import re

from plugins import _shared

EXTRACT_SYSTEM_PROMPT = (
    "你是信息提取器。请从对话中提取值得长期记住的关键信息："
    "关于用户的姓名、喜好、习惯、身份、经历、约定；关于群聊的主题、成员特点、重要事件。"
    "不要总结，只提取原话里明确出现的细节，务必保留：名字、数字、品种、时间。"
    "禁止输出'X是人''X是猫'这类无信息量的陈述。"
    "如果用户在纠正/否定旧信息，直接输出纠正后的新事实（如'林晓没在琴行兼职'），"
    "不要输出'用户指出/用户纠正'这类元描述。"
    "对话格式是'用户：…机器人：…'：只提取用户说的话里的信息；"
    "'机器人：'后面是 AI 自己的话，不要提取成用户记忆。"
    "只输出一个 JSON 字符串数组，每项是一句简短陈述（不超过40字），"
    "例如 [\"养了一只叫小白的橘猫\", \"小白3岁\"]。禁止输出对象或键值对。"
    "没有值得记的信息就输出 []。不要输出任何其他内容。"
)

STRUCTURED_EXTRACT_PROMPT = (
    "你是信息抽取器。从对话中抽取值得长期记住的信息，不总结、只提取原话里的明确细节"
    "（保留名字/数字/品种/时间）。禁止输出'X是人'这类无信息量陈述；"
    "用户纠正/否定旧信息时，直接输出纠正后的新事实，不要输出'用户指出/用户纠正'等元描述。"
    "只输出一个 JSON 对象，结构为："
    "{\"entities\":[{\"name\":\"小白\",\"type\":\"猫\"}],"
    "\"attributes\":{\"颜色\":\"橘色\",\"年龄\":3},"
    "\"facts\":[\"用户领养了一只叫小白的橘猫\"]}。"
    "entities 是对话中出现的具体人或物；attributes 是它们的属性；facts 是值得记住的陈述。"
    "对话格式是'用户：…机器人：…'：只提取用户说的话里的信息；"
    "'机器人：'后面是 AI 自己的话，不要提取。"
    "没有的键给空数组/空对象。不要输出任何其他内容。"
)

EVENT_TYPE_RULES = [
    ("规划", ["规划", "计划", "目标", "打算", "职业"]),
    ("学习", ["学习", "课程", "教程", "读书", "研究", "agent", "Agent"]),
    ("项目", ["项目", "开发", "部署", "上线", "MCP", "服务器", "代码", "仓库", "实现", "编程"]),
    ("经历", ["去过", "做过", "参加", "经历", "遇到", "完成", "毕业", "入职", "学会"]),
    ("健康", ["生病", "感冒", "医院", "体检", "运动", "失眠"]),
    ("家庭", ["家人", "父母", "孩子", "猫", "狗", "宠物"]),
    ("工作", ["上班", "加班", "同事", "老板", "面试", "offer", "项目"]),
    ("娱乐", ["游戏", "动漫", "电影", "音乐", "小说"]),
    ("偏好", ["喜欢", "讨厌", "最爱", "爱吃", "爱喝", "偏好"]),
]


def nice_fact(fact) -> str:
    """把可能残留的 {'info': '...'} 格式清洗成纯文本。"""
    s = str(fact).strip()
    m = re.match(r"^\{['\"]info['\"]\s*:\s*['\"](.*?)['\"]\s*\}$", s, re.S)
    return m.group(1).strip() if m else s


def _norm_extract_item(item) -> str:
    if isinstance(item, dict):
        for key in ("info", "fact", "content", "text", "name"):
            if item.get(key):
                return str(item[key]).strip()
        values = [str(v).strip() for v in item.values() if str(v).strip()]
        return values[0] if values else ""
    return str(item).strip()


_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def _verify_numbers(facts, conversation):
    """防 LLM 编造数字：事实里出现的每个数字都必须能在原始对话里找到，否则丢弃该事实。
    「月底几号演出」被脑补成「月底28号演出」时，对话里没有 28 → 丢弃。"""
    conv_nums = set(_NUM_RE.findall(str(conversation or "")))
    out = []
    for f in facts:
        fact_nums = set(_NUM_RE.findall(str(f)))
        if fact_nums and not fact_nums.issubset(conv_nums):
            continue
        out.append(f)
    return out


def extract_facts(conversation) -> list[str]:
    """LLM 提取事实；任何失败都返回 []，不阻塞聊天。"""
    try:
        resp = _shared.deepseek_chat(
            messages=[
                {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
                {"role": "user", "content": conversation},
            ],
            max_tokens=300,
            temperature=0.2,
            module="extract",
        )
        raw = re.sub(
            r"^```(?:json)?\s*|\s*```$",
            "",
            (resp.choices[0].message.content or "").strip(),
            flags=re.S,
        )
        start, end = raw.find("["), raw.rfind("]")
        if start < 0 or end < 0:
            return []
        data = json.loads(raw[start:end + 1])
        facts = [f for f in (nice_fact(_norm_extract_item(x)) for x in data) if f][:5]
        return _verify_numbers(facts, conversation)
    except Exception as e:
        _stats_err(e)
        return []


def structured_extract(conversation) -> dict:
    """第一阶段：结构化抽取（entities/attributes/facts）；任何失败返回 {}。"""
    try:
        resp = _shared.deepseek_chat(
            messages=[
                {"role": "system", "content": STRUCTURED_EXTRACT_PROMPT},
                {"role": "user", "content": conversation},
            ],
            max_tokens=400,
            temperature=0.1,
            module="extract",
            detail="structured",
        )
        raw = re.sub(
            r"^```(?:json)?\s*|\s*```$",
            "",
            (resp.choices[0].message.content or "").strip(),
            flags=re.S,
        )
        data = parse_json_object(raw)
        if data is None:
            return {}
        out = {
            "entities": data.get("entities") or [],
            "attributes": data.get("attributes") or {},
            "facts": data.get("facts") or [],
        }
        return out if any(out.values()) else {}
    except Exception as e:
        _stats_err(e)
        return {}


def extract_with_structure(conversation) -> list[str]:
    """两阶段提取：结构化抽取 → 转成长期记忆事实；失败回退 extract_facts。"""
    data = structured_extract(conversation)
    if not data:
        return extract_facts(conversation)
    facts = []
    for e in data.get("entities", []):
        name = str(e.get("name", "")).strip() if isinstance(e, dict) else str(e).strip()
        etype = str(e.get("type", "")).strip() if isinstance(e, dict) else ""
        if name and etype:
            facts.append(f"{name}是{etype}")
        elif name and len(name) <= 12:
            facts.append(f"存在角色{name}")
    for k, v in (data.get("attributes") or {}).items():
        if str(v).strip() and str(k).strip():
            facts.append(f"{k}是{v}")
    for f in data.get("facts", []):
        if str(f).strip():
            facts.append(str(f).strip())
    seen, out = set(), []
    generic_re = re.compile(r"^[\u4e00-\u9fffA-Za-z]{1,8}是(人|机器人|AI|软件|东西|事物)$")
    for f in facts:
        if generic_re.match(f.strip()):  # 过滤无信息量陈述（如"林晓是人"）
            continue
        if f not in seen:
            seen.add(f)
            out.append(f[:60])
    return _verify_numbers(out[:8], conversation) or extract_facts(conversation)


def classify_event_type(fact: str) -> str:
    """规则式事件分类：命中关键词返回对应类型，否则 'event'。"""
    for etype, words in EVENT_TYPE_RULES:
        if any(w in fact for w in words):
            return etype
    return "event"


def fact_keywords(text: str) -> set[str]:
    """轻量分词：英文/数字词（>=2 字符）+ 中文相邻二元组，用于相似度匹配。"""
    tokens = set()
    for m in re.finditer(r"[A-Za-z0-9_\-]+", text or ""):
        w = m.group().lower()
        if len(w) >= 2:
            tokens.add(w)
    for seg in re.findall(r"[\u4e00-\u9fff]+", text or ""):
        for i in range(len(seg) - 1):
            tokens.add(seg[i:i + 2])
    return tokens


# ===== 中文分词：优先 jieba（纯 Python 轻量）；未安装时回退英文/数字词 + 中文相邻二元组 =====
try:
    import jieba
    _HAS_JIEBA = True
except ImportError:
    _HAS_JIEBA = False


def has_jieba() -> bool:
    return _HAS_JIEBA


def _is_cjk(w: str) -> bool:
    return bool(w) and all("\u4e00" <= c <= "\u9fff" for c in w)


def tokenize(text) -> list[str]:
    """返回带重复的词项列表（tf 需要重复计数）。"""
    text = str(text or "")
    if _HAS_JIEBA:
        out = []
        for w in jieba.lcut(text):
            w = w.strip().lower()
            if not w:
                continue
            if len(w) == 1 and not _is_cjk(w) and not w.isdigit():
                continue
            out.append(w)
        return out
    tokens = []
    for m in re.finditer(r"[A-Za-z0-9_\-]+", text):
        w = m.group().lower()
        if len(w) >= 2:
            tokens.append(w)
    for seg in re.findall(r"[\u4e00-\u9fff]+", text):
        for i in range(len(seg) - 1):
            tokens.append(seg[i:i + 2])
    return tokens


# ===== 查询理解：实体/时间/意图抽取，作为"按需调用"各算法的依据 =====
TIME_WORDS = {
    "昨天": 1, "前天": 2, "上周": 14, "上个月": 30, "最近": 7,
    "几天前": 5, "之前": 90, "以前": 365, "当时": 365, "去年": 365,
}

PROJECT_ENTITIES = [
    "MCP", "项目", "服务器", "部署", "代码", "仓库", "API",
    "模型", "数据库", "Docker", "Git", "Python", "QQ",
]
ATTRIBUTE_WORDS = [
    "喜欢", "讨厌", "最爱", "名字", "几岁", "住在", "什么",
    "哪", "多少", "怎么样", "是什么", "有没有",
]
EVENT_WORDS = [
    "做过", "去过", "参加", "发生", "后来", "然后", "进展",
    "怎么做的", "结果", "完成", "开始",
]

SYNONYMS = {
    "部署": ["上线", "发布", "迁移"],
    "上线": ["部署", "发布"],
    "发布": ["部署", "上线"],
    "开发": ["做", "写", "构建", "搞"],
    "学习": ["学", "研究", "了解"],
    "喜欢": ["爱", "最爱", "偏爱"],
    "项目": ["工程", "服务"],
    "进展": ["进度", "怎么样了", "如何了"],
}

COREF_PATTERNS = [
    "那个项目", "那个", "这个", "那件事", "上次说的", "上次", "之前说的",
    "之前提到", "之前聊的", "之前那个", "前面说的", "刚才", "它", "你懂的",
    "你说过的", "上回", "老样子", "那玩意儿", "那东西",
]


def extract_entities(query: str) -> list[str]:
    """抽取专名/项目/技术词，供词法路由。"""
    ents = [w for w in PROJECT_ENTITIES if w.lower() in (query or "").lower()]
    for m in re.finditer(r"[A-Za-z][A-Za-z0-9_\-]{1,}", query or ""):
        w = m.group()
        if w.lower() not in [e.lower() for e in ents]:
            ents.append(w)
    return ents[:8]


def time_hint_days(query: str):
    for w, days in TIME_WORDS.items():
        if w in (query or ""):
            return days
    return None


def intent_of(query: str) -> str:
    q = query or ""
    if any(w in q for w in ATTRIBUTE_WORDS):
        return "attribute"
    if time_hint_days(q) or any(w in q for w in EVENT_WORDS):
        return "event"
    if len(q.strip()) <= 6:
        return "lexical"
    return "semantic"


def understand(query: str) -> dict:
    """理解查询：实体、时间、意图、词元。"""
    q = str(query or "").strip()
    tokens = fact_keywords(q)
    return {
        "original": q,
        "entities": extract_entities(q),
        "time_hint": time_hint_days(q),
        "intent": intent_of(q),
        "tokens": sorted(tokens),
        "short": len(tokens) <= 1 or len(q) < 4,
    }


def resolve_coreference(query: str, recent=None) -> str:
    """规则版指代消解：'那个项目' → 从最近对话的实体里补全。"""
    q = str(query or "").strip()
    if not q or not any(p in q for p in COREF_PATTERNS):
        return q
    entities = []
    for r in (recent or [])[-3:]:
        entities.extend(extract_entities(r))
    if not entities:
        # 没有实体时用最近一条消息本身作为上下文提示（检索兜底）
        return q + (" " + str((recent or [])[-1])[:40] if recent else "")
    ent = entities[0]
    for p in COREF_PATTERNS:
        if p in q:
            return q.replace(p, ent, 1).strip()
    return q


def expand(query: str, recent=None) -> list[str]:
    """查询变体：原句 + 指代消解 + 短查询前文补全 + 同义替换（最多 4 条，供 multi-query 合并检索）。
    前文补全：短/抽象/可能错别字的查询，附加最近对话实体作为检索变体，提升召回。"""
    variants = [str(query or "").strip()]
    resolved = resolve_coreference(query, recent)
    if resolved and resolved != variants[0]:
        variants.append(resolved)
    q = str(query or "").strip()
    if len(q) <= 8 and recent:
        # 从最近 4 条消息抽取实体/词元，附加成检索变体（容错：错别字查询也能靠实体召回）
        ctx_terms = []
        for r in (recent or [])[-4:]:
            ctx_terms.extend(extract_entities(r))
        if not ctx_terms:  # 无专名实体时用中文二元组兜底（抓小白/橘猫这类名字）
            for r in (recent or [])[-4:]:
                ctx_terms.extend(sorted(fact_keywords(r)))
        if ctx_terms:
            variant = q + " " + " ".join(dict.fromkeys(ctx_terms))[:60]
            if variant != variants[0] and variant not in variants:
                variants.append(variant)
    for word, alts in SYNONYMS.items():
        if word in variants[0] and alts:
            variants.append(variants[0].replace(word, alts[0], 1))
            break
    return list(dict.fromkeys(v for v in variants if v))[:4]



def _stats_err(e):
    """裸 except 审计（v2.2）：错误计数 + 日志，供消融/排查。"""
    try:
        import memory.stats as _st
        _st.bump_err("extract", e)
    except Exception:
        pass
