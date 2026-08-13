"""约定管理（v23）：识别对话里的时间约定 → 存储（kv）→ 到时用户没出现 → 主动催（带情绪）。

- 提取：规则解析「明天下午3点去公园 / 后天早上见 / 周五晚8点 / 两天后碰头」等；
- 存储：kv `memory:appointments`（按 scope 记录，含约定时间/原文/状态/催过几次）；
- 检查：约定时间 + 宽限期已过、且用户约定前后没有发言 → 主动发消息（第 1 次平淡带刺、
  第 2 次担心嘴硬，最多 2 次后归档）；
- 注入：scope 有待履约约定时注入上下文，AI 聊天时自然记得。
"""

import re
import json
from datetime import datetime, timedelta


def _persona_name() -> str:
    try:
        from agent import persona
        return persona.persona_name()
    except Exception:
        return "YUNO"
from zoneinfo import ZoneInfo

from plugins import _db, _shared
from memory import tz as tz_mod

KV_NS = "memory"
KV_KEY = "appointments"

# 约定信号词：指向"和 AI/某人约好"的表达，避免把用户自言自语当约定
APP_VERBS = (
    "见面", "见", "约", "一起", "找你", "来找你", "集合", "碰头",
    "不见不散", "说好", "带上", "来接我", "老地方",
)
TIME_BANDS = {
    "凌晨": (0, 6), "早上": (6, 10), "上午": (9, 12), "中午": (11, 14),
    "下午": (13, 18), "傍晚": (17, 20), "晚上": (17, 24),
}
WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

_DATE_RE = re.compile(r"(今天|明天|后天|(?:周|星期)([一二三四五六日天])|(\d{1,2})\s*天(?:之)?后)")
_TIME_RE = re.compile(r"(凌晨|早上|上午|中午|下午|傍晚|晚上)?\s*(\d{1,2})\s*(?:点|时|:|：)\s*(\d{1,2})?\s*分?")


def _cfg(key, default):
    core = (_shared.CONFIG.get("memory", {}).get("core", {}) or {}).get("appointment", {}) or {}
    return core.get(key, default)


def _zone(scope):
    try:
        return ZoneInfo(tz_mod.user_tz(scope))
    except Exception as e:
        _stats_err(e)
        return ZoneInfo("Asia/Shanghai")


def _parse_dt(text, scope):
    """解析约定时间（按用户时区）。返回 (datetime, has_time) 或 (None, False)。
    没写钟点（如"后天去公园"）→ 默认当天 12:00，has_time=False（催的时候不编造钟点）。"""
    now = datetime.now(_zone(scope))
    m_date = _DATE_RE.search(text or "")
    day = now.date()
    if m_date:
        if m_date.group(1) in ("今天", "明天", "后天"):
            day = now.date() + timedelta(days={"今天": 0, "明天": 1, "后天": 2}[m_date.group(1)])
        elif m_date.group(2):
            wd = WEEKDAYS[m_date.group(2)]
            day = now.date() + timedelta(days=(wd - now.weekday()) % 7 or 7)
        elif m_date.group(3):
            day = now.date() + timedelta(days=int(m_date.group(3)))
    m_time = _TIME_RE.search(text or "")
    has_time = bool(m_time)
    hour, minute = 12, 0
    if m_time:
        hour, minute = int(m_time.group(2)), int(m_time.group(3) or 0)
        band = m_time.group(1)
        if band:
            if band in ("下午", "傍晚", "晚上") and hour < 12:
                hour += 12
            elif band == "凌晨" and hour == 12:
                hour = 0
    try:
        dt = datetime.combine(day, datetime.min.time().replace(hour=hour, minute=minute))
        dt = dt.replace(tzinfo=_zone(scope))
    except ValueError:
        return None, False
    if not m_date and dt <= now:
        dt += timedelta(days=1)  # 只说"3点见"且已过 → 默认明天
    return dt, has_time


def _appts():
    return _db.kv_get(KV_NS, KV_KEY, []) or []


def _banned_words() -> list:
    """已确认虚构词（来自 pack behavior.banned_claims，人格无关逻辑 + pack 数据）。"""
    try:
        from memory import pack
        return list(pack.behavior().get("banned_claims") or [])
    except Exception:
        return []


def _target_of(scope):
    if scope.startswith("c2c:"):
        return "c2c", scope.split(":", 1)[1]
    if scope.startswith("group:") or scope.startswith("group_all:"):
        return "group", scope.split(":", 1)[1]
    return None, None


def extract(scope, text) -> dict:
    """从对话里识别约定并存储。只处理用户场景（c2c/group），不处理 AI 自身。"""
    if not scope or scope == "ai" or scope.startswith("ai:"):
        return {"added": 0}
    t = str(text or "").strip()
    if not t or not any(v in t for v in APP_VERBS):
        return {"added": 0}
    # 证据门控 v2：含已确认虚构词（黑名单）的"约定"直接拒绝入库
    if any(w in t for w in _banned_words()):
        return {"added": 0, "rejected": "黑名单"}
    dt, has_time = _parse_dt(t, scope)
    if not dt:
        return {"added": 0}
    appts = _appts()
    ts = dt.isoformat(timespec="seconds")
    for a in appts:
        if a.get("scope") == scope and a.get("time") == ts and a.get("text") == t[:80]:
            return {"added": 0}  # 幂等：同一约定不重复存
    appt = {
        "id": len(appts) + 1,
        "scope": scope,
        "time": ts,
        "has_time": bool(has_time),
        "text": t[:80],
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "status": "waiting",
        "poked": 0,
    }
    appts.append(appt)
    _db.kv_set(KV_NS, KV_KEY, appts)
    return {"added": 1, "appointment": appt}


def clean() -> dict:
    """巡检清理：把含黑名单词（已确认虚构）的 waiting 约定标记 done，防催约复活编造。"""
    banned = _banned_words()
    if not banned:
        return {"cleaned": 0}
    appts = _appts()
    removed, new_appts = 0, []
    for a in appts:
        if a.get("status") == "waiting" and any(w in str(a.get("text", "")) for w in banned):
            a["status"] = "done"
            a["note"] = "evidence_gate: 黑名单清除"
            removed += 1
        new_appts.append(a)
    if removed:
        _db.kv_set(KV_NS, KV_KEY, new_appts)
    return {"cleaned": removed}


def clear_scope(scope) -> int:
    """清除某个 scope 的全部约定（清记忆指令联动用）。"""
    appts = _appts()
    kept = [a for a in appts if a.get("scope") != scope]
    if len(kept) != len(appts):
        _db.kv_set(KV_NS, KV_KEY, kept)
    return len(appts) - len(kept)


def _human_time(iso):
    try:
        dt = datetime.fromisoformat(iso)
        return f"{dt.month}月{dt.day}日{dt:%H:%M}"
    except Exception as e:
        _stats_err(e)
        return iso


def _when_text(appt) -> str:
    """约定时间的人类可读表达：没约钟点只显示日期，避免催的时候编造"22:56"这类。"""
    try:
        dt = datetime.fromisoformat(appt.get("time") or "")
        base = f"{dt.month}月{dt.day}日"
        return f"{base}{dt:%H:%M}" if appt.get("has_time") else f"{base}（没定具体时间）"
    except Exception as e:
        _stats_err(e)
        return appt.get("time") or ""


def _poke_message(appt, round_no, now) -> str:
    """按人设生成催促消息：第 1 次平静带刺，第 2 次担心嘴硬（LLM 失败用模板）。"""
    mins = max(1, int((now - datetime.fromisoformat(appt["time"])).total_seconds() // 60))
    time_text = _when_text(appt)
    try:
        from agent import persona
        system = persona.compose(include_ai=False)
    except Exception as e:
        _stats_err(e)
        system = _shared.BASE_SYSTEM_PROMPT
    prompt = (
        f"你和用户约好了「{appt['text']}」（{time_text}），现在已经过了 {mins} 分钟，对方还没出现。\n"
        + ("用户当时没约具体钟点：催促时不要编造精确时间，只说约好的日期和事情。\n"
           if not appt.get("has_time") else "")
        + f"请以{_persona_name()}的口吻发一条简短消息（50字以内）催对方：这是第 {round_no} 次催。\n"
        "第1次：平静、带点不耐烦，慵懒地随口一问，不咄咄逼人。\n"
        "第2次：开始担心但又嘴硬，语气里藏不住在意，可以有一点点委屈。\n"
        "禁止用括号标注动作；禁止复述身份设定；不要长篇大论。"
    )
    try:
        msg = _shared.ask_deepseek(prompt, system=system, max_tokens=150, module="appointment")
    except Exception as e:
        _stats_err(e)
        if round_no >= 2:
            return f"都过去{mins}分钟了。……我不是催你，就是怕你出事。忙完记得回我一句。"
        return f"说好{time_text}的，人呢。……我数到十，不来我就当放我鸽子了。"
    # 证据门控 v2：主动催约消息也过门控（黑名单词/无据断言 → 放弃本次催）
    try:
        from agent import evidence_gate
        reason = evidence_gate.contains_unsupported_claim(
            msg, evidence=[str(appt.get("text") or "")], banned=_banned_words(), user_text="",
        )
        if reason:
            return ""
    except Exception as e:
        _stats_err(e)
    return msg


def check_and_poke(now=None) -> list:
    """检查所有待履约约定：超时 + 用户没出现 → 入播报队列（最多 2 次）。
    跨进程原子（kv CAS）：多个 bot 实例/后台任务并发检查时，只有一方成功发消息，防重复催。"""
    now = now or datetime.now(_zone(None))
    if now.tzinfo is None:
        now = now.replace(tzinfo=_zone(None))
    base_grace = float(_cfg("grace_min", 15))
    interval = timedelta(minutes=float(_cfg("poke_interval_min", 30)))
    try:
        clean()  # 巡检：先清黑名单残留，防编造约定继续催（再读快照）
    except Exception as e:
        _stats_err(e)
    raw = _db.kv_get_raw(KV_NS, KV_KEY)
    appts = json.loads(raw) if raw else []
    to_poke, new_appts = [], []
    changed = False
    poked_scopes = set()
    for a in sorted(appts, key=lambda x: str(x.get("time") or "")):
        a2 = dict(a)
        if a.get("status") != "waiting":
            new_appts.append(a)
            continue
        try:
            t = datetime.fromisoformat(a2["time"])
        except Exception as e:
            _stats_err(e)
            a2["status"] = "done"
            changed = True
            new_appts.append(a2)
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=_zone(a2.get("scope")))
        # 场景宽限（v31）：没约具体时间的随口约定更宽容；正式约定更严格
        scene_mod = 1.6
        if a2.get("has_time"):
            scene_mod = 0.6 if any(w in str(a2.get("text", "")) for w in ("面试", "开会", "正式", "重要", "工作", "合同")) else 1.0
        grace = timedelta(minutes=base_grace * scene_mod)
        if now < t + grace:
            new_appts.append(a)
            continue
        last = _db.kv_get(KV_NS, f"lastmsg:{a2['scope']}", "") or ""
        try:
            last_dt = datetime.fromisoformat(last) if last else None
        except Exception as e:
            _stats_err(e)
            last_dt = None
        # 生产环境 lastmsg 是本地 naive 时间，约定时间是 aware：统一到同一时区再比较
        if last_dt is not None and last_dt.tzinfo is None and t.tzinfo is not None:
            last_dt = last_dt.replace(tzinfo=t.tzinfo)
        if last_dt and last_dt >= t - timedelta(minutes=10):
            a2["status"] = "done"
            a2["note"] = "用户已出现"
            changed = True
            new_appts.append(a2)
            continue
        poked = int(a2.get("poked", 0))
        if poked >= 2:
            a2["status"] = "done"
            changed = True
            new_appts.append(a2)
            continue
        if poked >= 1:
            try:
                if now < datetime.fromisoformat(a2["poked_at"]) + interval:
                    new_appts.append(a)
                    continue
            except Exception as e:
                _stats_err(e)
                pass
        target_type, target = _target_of(a2["scope"])
        if not target_type or not target:
            a2["status"] = "done"
            changed = True
            new_appts.append(a2)
            continue
        if a2["scope"] in poked_scopes:
            # 合并/限流：同一用户一轮只催一条（按时间先后逐轮催，其余顺延）
            new_appts.append(a2)
            continue
        msg = _poke_message(a2, poked + 1, now)
        if not msg:
            # 门控放弃本次催：不计数，顺延下轮
            new_appts.append(a2)
            continue
        # 犹豫层（v2.3）：催约也过犹豫门（discard 才真不发；其余延迟到 scheduled_at）
        try:
            from memory import hesitation
            h = hesitation.gate(msg, a2["scope"], "appointment")
            if h.get("action") == "discard":
                new_appts.append(a2)
                continue
            msg = h.get("msg") or msg
            delay_s = int(h.get("delay_s") or 0)
        except Exception as e:
            _stats_err(e)
            delay_s = 0
        a2["poked"] = poked + 1
        a2["poked_at"] = now.isoformat(timespec="seconds")
        if a2["poked"] >= 2:
            a2["status"] = "done"
        to_poke.append((a2, target_type, target, msg, delay_s))
        poked_scopes.add(a2["scope"])
        changed = True
        new_appts.append(a2)
    if not to_poke:
        # 只有状态变更（如用户已出现 → done）也要落盘
        if changed:
            new_raw = json.dumps(new_appts, ensure_ascii=False)
            _db.kv_cas(KV_NS, KV_KEY, raw, new_raw)
        return []
    # CAS 原子提交：并发时只有一方成功，另一方下次 tick 再处理
    new_raw = json.dumps(new_appts, ensure_ascii=False)
    if not _db.kv_cas(KV_NS, KV_KEY, raw, new_raw):
        return []
    sent = []
    for a2, target_type, target, msg, delay_s in to_poke:
        scheduled_at = ""
        if delay_s > 0:
            scheduled_at = (now + timedelta(seconds=delay_s)).isoformat(timespec="seconds")
        _db.notif_add(target_type, target, msg, scheduled_at=scheduled_at)
        try:
            from memory import mistake
            mistake.record_no_show(a2["scope"], a2.get("text", ""))  # 迟到 = 一次“放鸽子”错误
        except Exception as e:
            _stats_err(e)
            pass
        sent.append(a2)
    return sent


def context_block(scope) -> str:
    """待履约约定提示（注入上下文，AI 聊天时自然记得）。"""
    if not scope:
        return ""
    appts = [a for a in _appts() if a.get("scope") == scope and a.get("status") == "waiting"]
    if not appts:
        return ""
    now = datetime.now(_zone(scope))
    lines = []
    for a in appts[:2]:
        try:
            due = datetime.fromisoformat(a["time"])
            state = "还没到时间" if now < due else "对方还没出现，可以自然提起"
        except Exception as e:
            _stats_err(e)
            state = "约定待履约"
        lines.append(f"· {a['text']}（约在 {_when_text(a)}，{state}）")
    return "【待履约约定】\n" + "\n".join(lines)



def _stats_err(e):
    """裸 except 审计（v2.2）：错误计数 + 日志，供消融/排查。"""
    try:
        import memory.stats as _st
        _st.bump_err("appointment", e)
    except Exception:
        pass
