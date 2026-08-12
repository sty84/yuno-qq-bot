"""睡眠与梦境机制（v31）：把每日 grow 变成"一夜"；睡眠=拟人化节能待机。

- 浅睡：轻度巩固（短期→长期升迁、访问刷新）。
- 深睡：把当天与用户的对话巩固成"今日回忆"（ai:recall，long 类，月级保留）。
- REM：做梦——类型/内容/逻辑性随机，随机抽取几段记忆 + 人设元素拼装；
  梦本身只存在当晚的 kv 日志里，AI 大概率记不住内容，小概率留下一条
  模糊记忆（dream 类，1.5 天半衰期，很快忘光）。
- 清晨会主动告诉用户"做了个梦"（内容大多记不清）；用户追问时，
  模糊记忆还在就"想不太起来"，过期了就"完全陌生"。
- 睡眠模式（v31.1）：sleep_mode = awake / standby（节能待机，可唤醒） / deep（深睡档，真离线）。
  "睡觉"是人设化的省电待机，不是失联——待机档被消息唤醒是"被从省电里捞出来"；
  深睡档（默认凌晨 2~5 点）消息进未读队列，醒来补一句"你昨晚找我了？"。
"""

import random
import re
from datetime import datetime, date, timedelta

from plugins import _db, _shared

DREAM_TEMPLATES = {
    "冒险": "我梦见自己在{scene}，{action}，然后{twist}",
    "日常": "我梦见{scene}，{action}，{twist}",
    "荒诞": "我梦见{scene}突然{twist}，{action}，最后{ending}",
    "怪谈": "我梦见{scene}里{twist}，{action}，{ending}",
    "音乐": "我梦见自己在{scene}{action}，{twist}，{ending}",
    "追逐": "我梦见{twist}追着我跑，{action}，{ending}",
    "重逢": "我梦见{scene}，{action}，{twist}",
}

DREAM_FILLERS = [
    "不知道为什么",
    "一转眼",
    "忽然",
    "像电影一样",
    "背景里一直放着歌",
    "有人在远处喊我",
    "颜色全都不对劲",
    "时间一直在倒流",
    "门怎么都打不开",
    "所有东西都在慢慢消失",
    "我好像飘在半空",
    "灯光忽明忽暗",
]

# 人设元素（来自 Persona Pack），让梦有"她"的味道
PERSONA_FLAVOR = [
    "白巧克力", "能量饮料", "DJ台", "MewType", "千石AI", "麻花辫", "雪貂耳发饰",
    "虫子", "恐怖电影", "TCG卡牌", "漫画", "动画", "打碟", "作曲", "省电模式", "抱枕",
]

# LLM 做梦提示词：人设口吻 + 防 AI 味硬约束
DREAM_LLM_PROMPT = (
    "你是{name}——{role}。慵懒、毒舌、节能主义者。"
    "现在你在讲自己昨晚的梦，要像她醒来后随口嘟囔那样：第一人称、短句、口语化、具体。\n"
    "要求：\n"
    "1. 只讲梦的内容，不解释梦的寓意，不总结、不升华，不说'这大概就是…'；\n"
    "2. 禁止文学腔：不用'仿佛/犹如/宛如/恍若'，不用'梦境''梦醒时分'这类词；\n"
    "3. 禁止括号舞台提示，禁止'作为AI''理解你的感受''希望对你有帮助'这类 AI 味表达；\n"
    "4. 要有具体细节（东西、颜色、声音、动作），哪怕荒诞也要具体，像真在做梦；\n"
    "5. 长度 1~2 句话（60 字以内），说完了就停，不要加任何结尾升华。\n"
    "类型：偏「{dtype}」的梦。\n"
    "逻辑要求：{logic_hint}\n"
    "素材（可以随便混搭、扭曲、反转，不用全部用）：{materials}\n"
    "直接输出梦的内容，不要任何前缀。"
)

# 防 AI 味：命中任一即判为不合格，回退模板
_AI_SLOP_PHRASES = (
    "仿佛", "犹如", "宛如", "恍若", "梦醒时分", "这个梦告诉我", "这大概就是",
    "希望对你有帮助", "作为AI", "作为 AI", "理解你的感受", "建议您", "总的来说",
    "总之", "值得一提的是", "不禁让我想到", "仿佛在诉说", "在梦里我感悟到",
)
_BRACKET_RE = re.compile(r"[()（）【】\[\]]")


def _cfg(key, default):
    slp = (_shared.CONFIG.get("memory", {}).get("core", {}) or {}).get("sleep", {}) or {}
    return slp.get(key, default)


def _today() -> str:
    return date.today().isoformat()


def _now_ts() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _cfg_sleep(key, default):
    slp = (_shared.CONFIG.get("memory", {}).get("core", {}) or {}).get("sleep", {}) or {}
    return slp.get(key, default)


URGENT_WORDS = (
    "急", "快", "疼", "痛", "难受", "救命", "出事", "麻烦", "重要", "怎么办",
    "面试", "合同", "开会", "工作", "马上", "立刻", "紧急",
)


def is_urgent(text="", an=None) -> bool:
    if (an or {}).get("intent") == "求助":
        return True
    return any(w in (text or "") for w in URGENT_WORDS)


def _deep_window() -> tuple:
    w = _cfg_sleep("deep_window", None)
    if w:
        pass
    else:
        try:
            from memory import pack
            w = pack.behavior().get("sleep_deep_window")
        except Exception:
            w = None
        w = w or [2, 5]
    try:
        return (int(w[0]), int(w[1]))
    except Exception as e:
        _stats_err(e)
        return (2, 5)


def sleep_mode(now=None) -> str:
    """awake / standby（节能待机，可唤醒）/ deep（深睡档，真离线）。
    深睡档按时间窗优先；待机档 = 日程里的睡觉槽；其余清醒。"""
    try:
        import memory.stats as _st
        _st.bump("tick:sleep")
    except Exception as e:
        _stats_err(e)
    now = now or datetime.now()
    lo, hi = _deep_window()
    hour = now.hour
    deep = (lo <= hour < hi) if lo <= hi else (hour >= lo or hour < hi)
    if deep:
        return "deep"
    try:
        from memory import schedule as schedule_mod
        cur = schedule_mod.current_activity(now)
        act = cur.get("activity") if cur else ""
    except Exception as e:
        _stats_err(e)
        act = ""
    return "standby" if act == "sleep" else "awake"


# ===== 深睡档：未读队列（真离线，醒来补一句）=====
def queue_snapshot(scope) -> dict:
    if not scope:
        return {}
    return _db.kv_get("memory", f"sleep_queue:{scope}") or {}


def queue_add(scope, text, urgent=False):
    if not scope:
        return
    data = queue_snapshot(scope)
    items = list(data.get("items") or [])
    items.append({"ts": _now_ts(), "text": str(text or "")[:80], "urgent": bool(urgent)})
    data["items"] = items[-10:]
    _db.kv_set("memory", f"sleep_queue:{scope}", data)


def queue_take(scope):
    if not scope:
        return None
    data = queue_snapshot(scope)
    _db.kv_set("memory", f"sleep_queue:{scope}", None)
    return data


def queue_deliver_block(q) -> str:
    """醒来补一句：把未读消息的梗概注入上下文，让 AI 自然带出。"""
    items = (q or {}).get("items") or []
    if not items:
        return ""
    urgent_n = sum(1 for i in items if i.get("urgent"))
    parts = [f"深睡时段有 {len(items)} 条消息没看到"]
    if urgent_n:
        parts.append(f"其中 {urgent_n} 条看起来比较急")
    for i in items[:3]:
        parts.append(f"· {i.get('text', '')[:40]}")
    parts.append("自然地在回复里带一句'你昨晚找我了？'，别生硬播报，别复述全文")
    return "【未读消息（内部参考）】" + "；".join(parts)


def emergency_wake(scope, urgent, now=None) -> bool:
    """紧急通道：深睡档连续 N 条紧急消息 → 系统级紧急唤醒。"""
    if not urgent or not scope:
        return False
    now = now or datetime.now()
    data = queue_snapshot(scope)
    th = max(1, int(_cfg_sleep("emergency_threshold", 2)))
    window = timedelta(minutes=float(_cfg_sleep("emergency_window_min", 30)))
    recent = 0
    for i in (data.get("items") or []):
        if not i.get("urgent"):
            continue
        try:
            ts = datetime.fromisoformat(i["ts"])
        except Exception as e:
            _stats_err(e)
            continue
        if now - ts <= window:
            recent += 1
    return recent >= th


def queue_mark_woken(scope):
    """紧急唤醒后：移除刚回复的那条消息，防醒来重复提。"""
    if not scope:
        return
    data = queue_snapshot(scope)
    items = list(data.get("items") or [])
    if items:
        items.pop()
    _db.kv_set("memory", f"sleep_queue:{scope}", {"items": items})


# ===== 待机档：被打断记账（起床气 → 次日更懒）=====
def record_interrupt(scope, now=None):
    """同一槽位只记一次"被从省电里捞出来"。"""
    if not scope:
        return 0
    now = now or datetime.now()
    d = now.date().isoformat()
    key = f"sleep_interrupts:{d}"
    data = _db.kv_get("memory", key) or {"count": 0, "slots": []}
    try:
        from memory import schedule as schedule_mod
        slot = schedule_mod.slot_index(now.hour)
    except Exception as e:
        _stats_err(e)
        slot = -1
    slots = list(data.get("slots") or [])
    if slot in slots:
        return int(data.get("count", 0))
    slots.append(slot)
    data["count"] = int(data.get("count", 0)) + 1
    data["slots"] = slots
    _db.kv_set("memory", key, data)
    try:
        from memory import space as space_mod
        space_mod.emit("interrupt", "被从省电模式里捞出来")
    except Exception as e:
        _stats_err(e)
        pass
    return int(data["count"])


def interrupts_yesterday() -> int:
    y = (date.today() - timedelta(days=1)).isoformat()
    data = _db.kv_get("memory", f"sleep_interrupts:{y}") or {}
    return int(data.get("count", 0))


def standby_block(scope="", text="") -> str:
    """待机档注入块：被从省电里捞出来（内部参考）。"""
    return (
        "【省电模式】此刻她在节能待机（拟人化休息，不是失联），被这条消息从省电里捞出来了。"
        "普通私聊→半梦半醒、慵懒但照常回答（用语气表现，别说'我在睡觉'这类话）；"
        "紧急/重要→清醒认真；群聊→正常但带起床气；不要解释睡眠机制。"
        "被打断次数会累积，今天会更懒一点。"
    )


# ===== 睡眠周期控制 =====
def last_sleep() -> dict:
    return _db.kv_get("memory", "last_sleep") or {}


def _mark_slept():
    _db.kv_set("memory", "last_sleep", {"ts": _now_ts(), "date": _today()})


# ===== 浅睡：轻度巩固 =====
def _light_consolidate() -> dict:
    from memory import policy
    return {"promoted": policy.promote()}


# ===== 深睡：把当天对话巩固成"今日回忆" =====
def _llm_one(prompt) -> str:
    """轻量 LLM 总结（失败静默回退，绝不阻塞）。"""
    try:
        resp = _shared.deepseek_chat(
            messages=[
                {"role": "system", "content": "你是记忆巩固器。输出一句简洁的'今日回忆'，不要解释。"},
                {"role": "user", "content": prompt},
            ],
            max_tokens=100,
            temperature=0.4,
            module="sleep",
            detail="recall",
        )
        return (resp.choices[0].message.content or "").strip()[:120]
    except Exception as e:
        _stats_err(e)
        return ""


def _deep_consolidate() -> dict:
    """收集上次睡眠以来的用户事实，按 scope 聚合 → 写入 ai:recall（long 类）。"""
    last = last_sleep().get("ts") or ""
    if last:
        cutoff = last
    else:
        cutoff = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
    buckets = {}
    for r in _db.memory_rows():
        sc = str(r.get("scope") or "")
        if not (sc.startswith("c2c:") or sc.startswith("group:") or sc.startswith("group_all:")):
            continue
        ts = str(r.get("updated_at") or "")
        if ts and ts >= cutoff:
            buckets.setdefault(sc, []).append(r)
    min_facts = max(1, int(_cfg("min_facts", 3)))
    limit = max(1, int(_cfg("recall_limit", 5)))
    written = 0
    for sc, rows in sorted(buckets.items(), key=lambda kv: -len(kv[1]))[:limit]:
        if len(rows) < min_facts:
            continue
        rows.sort(key=lambda r: r.get("updated_at") or "")
        items = "；".join(r["fact"] for r in rows[:6])
        summary = _llm_one(f"用户今天聊了这些事，概括成一句'今日回忆'（保留关键细节）：{items}")
        if not summary:
            summary = f"今天和用户聊了：{items[:80]}"
        try:
            n = interrupts_yesterday()
            if n:
                summary = f"昨晚被吵醒{n}次没睡好；{summary}"[:120]
        except Exception as e:
            _stats_err(e)
            pass
        try:
            from memory import schedule as schedule_mod
            sch = schedule_mod.today_summary()
            if sch:
                summary = f"今天安排了{sch}；{summary}"[:120]
        except Exception as e:
            _stats_err(e)
            pass
        fact = f"{_today()}：{summary}"
        _db.memory_add(
            "ai", "recall", fact, _now_ts(), None,
            confidence=0.55, source="sleep:deep", mclass="long",
            audience="public", speaker="ai",
        )
        from memory import policy
        policy.touch("ai", "recall", fact, importance=0.6)
        written += 1
    return {"recalls": written, "scopes": len(buckets)}


# ===== REM：做梦 =====
def _dream_sources(n=4):
    rows = [
        r for r in _db.memory_rows()
        if float(r.get("confidence", 0.5)) >= 0.5
        and not str(r.get("scope") or "").startswith("char:")
        and str(r.get("key") or "") != "dream"
    ]
    if not rows:
        return []
    return random.sample(rows, min(n, len(rows)))


def _sanitize_dream(text) -> str:
    """梦文本清洗：去括号/引号/前缀，命中 AI 味词 → 返回空（触发回退）。"""
    t = (text or "").strip()
    if not t:
        return ""
    t = t.strip("“”\"'「」『』：: ")
    t = _BRACKET_RE.sub("", t)
    for p in _AI_SLOP_PHRASES:
        if p in t:
            return ""
    for pre in ("好的，", "好的 ", "我梦到了：", "我梦见："):
        if t.startswith(pre):
            t = t[len(pre):].strip()
            break
    return t[:140]


def _llm_dream(dtype, materials, logic_hint) -> str:
    """DeepSeek 生成梦（高温度随机）；失败/不合格返回空串。"""
    try:
        try:
            from agent import persona
            from memory import pack
            pname = persona.persona_name()
            prole = str(pack.world().get("role") or "")
        except Exception:
            pname, prole = "YUNO", ""
        resp = _shared.deepseek_chat(
            messages=[
                {"role": "system", "content": f"你是{pname}，正在讲述自己昨晚的梦。输出只包含梦的内容本身。"},
                {
                    "role": "user",
                    "content": DREAM_LLM_PROMPT.format(
                        name=pname, role=prole, dtype=dtype, logic_hint=logic_hint, materials=materials
                    ),
                },
            ],
            max_tokens=160,
            temperature=0.9,
            module="sleep",
            detail="dream",
        )
        return _sanitize_dream(resp.choices[0].message.content or "")
    except Exception as e:
        _stats_err(e)
        return ""


def _template_dream(dtype, logic, frags) -> str:
    """模板回退：LLM 不可用或输出不合格时拼装（保住'会做梦'这个机制）。"""
    if logic >= 0.7:
        return DREAM_TEMPLATES[dtype].format(
            scene=frags[0] if frags else "一个奇怪的地方",
            action=frags[1] if len(frags) > 1 else "我在里面走着",
            twist=frags[2] if len(frags) > 2 else random.choice(DREAM_FILLERS),
            ending=random.choice(DREAM_FILLERS),
        )
    if logic >= 0.3:
        a = frags[0] if frags else random.choice(DREAM_FILLERS)
        b = frags[1] if len(frags) > 1 else random.choice(DREAM_FILLERS)
        return f"我梦见{a}，然后{b}，{random.choice(DREAM_FILLERS)}"
    picked = frags[:3] or [random.choice(DREAM_FILLERS)]
    return "我梦见" + "，然后".join(picked) + f"，{random.choice(DREAM_FILLERS)}"


def _dream() -> dict:
    """生成一个梦：LLM 优先（类型/内容/逻辑随机，防 AI 味），失败回退模板。"""
    dtype = random.choice(list(DREAM_TEMPLATES.keys()))
    logic = round(random.random(), 2)
    srcs = _dream_sources(random.randint(2, 4))
    frags = [r["fact"] for r in srcs]
    if random.random() < 0.7:
        frags.append(random.choice(PERSONA_FLAVOR))
    try:
        from memory import living as living_mod
        if item := living_mod.random_flavor():
            frags.append(item)  # 家里物件进梦（v31）
    except Exception as e:
        _stats_err(e)
        pass
    try:
        from memory import space as space_mod
        if any("演出" in str(e.get("detail", "")) for e in space_mod.today_events()):
            frags.append("舞台灯光")  # 今天有演出 → 舞台进梦（v31）
    except Exception as e:
        _stats_err(e)
        pass
    random.shuffle(frags)

    if logic >= 0.7:
        logic_hint = "整体还算连贯，像个正常但奇怪的梦"
    elif logic >= 0.3:
        logic_hint = "半连贯，中间可以突然跳场景"
    else:
        logic_hint = "越荒诞越好，逻辑可以完全断裂，前后可以不搭"

    materials = "；".join(frags[:4]) if frags else "（没有素材，就随便梦）"
    text = _llm_dream(dtype, materials, logic_hint)
    if not text:
        text = _template_dream(dtype, logic, frags)

    return {
        "type": dtype,
        "text": text[:140],
        "logic": logic,
        "sources": [r["fact"] for r in srcs][:3],
        "ts": _now_ts(),
    }


# ===== 一夜流水线 =====
def night_run(force=False) -> dict:
    """浅睡 → 深睡 → REM。默认一夜一次（按日期去重）；force=True 可手动重跑。"""
    if not _cfg("enabled", True):
        return {"skipped": "disabled"}
    if not force and last_sleep().get("date") == _today():
        return {"skipped": "already"}
    _mark_slept()  # 先标记，避免崩溃后同夜重复跑

    report = {"ts": _now_ts(), "date": _today()}
    report["light"] = _light_consolidate()
    report["deep"] = _deep_consolidate()

    cycles = max(1, min(4, int(_cfg("cycles", 2))))
    dreams = [_dream() for _ in range(cycles)]
    remember = bool(dreams) and random.random() < float(_cfg("dream_remember_prob", 0.25))
    keyword = ""
    if dreams and (dreams[0].get("sources") or []):
        keyword = dreams[0]["sources"][0][:24]
    if remember:
        fact = f"昨晚做了个梦，好像和「{keyword or '什么'}」有关，但内容很模糊"
        _db.memory_add(
            "ai", "dream", fact, _now_ts(), None,
            confidence=0.3, source="sleep:rem", mclass="short",
            audience="public", speaker="ai",
        )
        from memory import policy
        policy.touch("ai", "dream", fact, importance=0.2)

    _db.kv_set(
        "memory", "dreams",
        {"date": _today(), "dreams": dreams, "told": False,
         "remembered": remember, "created_ts": _now_ts()},
    )
    if dreams:
        try:
            from memory import sharing as sharing_mod
            sharing_mod.add_delta(0.3, "dream")  # 做梦 → 分享欲（v31）
        except Exception as e:
            _stats_err(e)
            pass
    report["dreams"] = len(dreams)
    report["remembered"] = remember
    report["samples"] = [d["text"] for d in dreams]
    return report


# ===== 醒来后：主动告诉 / 回应追问 =====
def _age_hours(data) -> float:
    try:
        from datetime import datetime as _dt
        return (_dt.now() - _dt.fromisoformat(str(data.get("created_ts", "")))).total_seconds() / 3600.0
    except Exception as e:
        _stats_err(e)
        return 999.0


def _vague_dream_facts() -> list:
    return [r["fact"] for r in _db.memory_rows("ai", "dream")]


def context_block(scope="", text="") -> str:
    """注入块：用户问梦 → 按记忆状态回应；没问 → 醒来后主动提一次（不重复）。"""
    data = _db.kv_get("memory", "dreams") or {}
    if not data:
        return ""
    asked = any(w in (text or "") for w in ("梦", "做梦", "梦到", "梦见", "噩梦"))
    if asked:
        vague = _vague_dream_facts()
        if vague:
            head = str(vague[0]).split("，", 1)[0]
            return (
                "【梦（内部提示）】用户问起昨晚的梦。我记得好像梦到过" + head +
                "，但细节全模糊了——就按这种'想不太起来'的感觉回答，不要编造具体情节。"
                "梦的内容是乱的，别当真，也别往现实上套。"
            )
        return (
            "【梦（内部提示）】用户问起昨晚的梦。我已经完全不记得内容了，"
            "连'做过梦'的印象都很淡——感到陌生是正常的，就老实说不记得。"
            "梦本来就是乱的，别当真。"
        )
    if data.get("told"):
        return ""
    if _age_hours(data) > float(_cfg("proactive_hours", 12)):
        return ""
    # 用户状态（v31）：用户可能不在/深夜/潜水时，先不主动提
    try:
        from memory import interaction as interaction_mod
        if interaction_mod.user_mult(scope)["disturb"] < 0.8:
            return ""
    except Exception as e:
        _stats_err(e)
        pass
    _db.kv_set("memory", "dreams", {**data, "told": True})
    if data.get("remembered") and data.get("dreams"):
        sample = random.choice(data["dreams"])["text"][:60]
        return (
            "【昨晚的梦（内部提示）】醒来后还没和用户提过：我昨晚做了个梦，模模糊糊记得一点——"
            f"'{sample}'。可以在合适的时机自然提一句，不要生硬播报，也不要复述太多细节。"
            "梦是乱的，别当真，别把梦的内容当现实依据。"
        )
    return (
        "【昨晚的梦（内部提示）】醒来后还没和用户提过：我昨晚好像做了个梦，"
        "但内容一点都想不起来了。可以在合适的时机自然提一句，不要生硬播报。"
    )



def _stats_err(e):
    """裸 except 审计（v2.2）：错误计数 + 日志，供消融/排查。"""
    try:
        import memory.stats as _st
        _st.bump_err("sleep", e)
    except Exception:
        pass
