"""时间感知回忆（v2.2）：口语时间 → 事件时间/检索窗口。

- extract(text)：把"上周三买了猫/前几天去的"解析成 (start, end) 日期区间；
  解析失败回退 now，并标记 explicit=False（approx）；
- label_for(ts, ts_source)：给注入层渲染时间标签——explicit 给具体日期/相对日，
  approx 只给"大概"。
原则：时间当元数据，不写进事实文本（不污染去重与 BM25/向量索引）。
"""

import re
from datetime import date, datetime, timedelta

_WEEK_CN = ("一", "二", "三", "四", "五", "六", "日")
_WEEK_MAP = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def _now(scope=None):
    try:
        from memory import tz as tz_mod
        if hasattr(tz_mod, "now"):
            n = tz_mod.now(scope)
            if n:
                return n
    except Exception:
        pass
    return datetime.now()


def _day_range(day: date):
    start = datetime(day.year, day.month, day.day)
    return start, start + timedelta(days=1)


def _weekday_in(text) -> int | None:
    m = re.search(r"[周星期]([一二三四五六日天])", str(text or ""))
    if m:
        return _WEEK_MAP.get(m.group(1))
    return None


def _month_end(year, month) -> date:
    """该月最后一天。"""
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def _find_weekday(start: date, end: date, wd: int):
    """在 [start, end] 内找第一个星期 wd（0=周一,6=周日），无则返回 None。"""
    d = start + timedelta(days=(wd - start.weekday()) % 7)
    return d if d <= end else None


def _daynum_in(text) -> int | None:
    m = re.search(r"(\d{1,2})\s*[号日]", str(text or ""))
    return int(m.group(1)) if m else None


def extract(text, scope=None) -> dict:
    """解析口语时间。返回 {start, end, explicit, detected, label}。"""
    t = str(text or "")
    now = _now(scope)
    today = now.date()
    out = {"start": None, "end": None, "explicit": False, "detected": False, "label": ""}

    if re.search(r"今天|今日", t):
        s, e = _day_range(today)
        out.update(start=s, end=e, explicit=True, detected=True, label="今天")
    elif "昨天" in t:
        s, e = _day_range(today - timedelta(days=1))
        out.update(start=s, end=e, explicit=True, detected=True, label="昨天")
    elif "前天" in t:
        s, e = _day_range(today - timedelta(days=2))
        out.update(start=s, end=e, explicit=True, detected=True, label="前天")
    elif re.search(r"上周|上个星期|上一周", t):
        last_monday = today - timedelta(days=today.weekday() + 7)
        wd = _weekday_in(t)
        if wd is not None:
            s, e = _day_range(last_monday + timedelta(days=wd))
            out.update(start=s, end=e, explicit=True, detected=True, label=f"上周{_WEEK_CN[wd]}")
        else:
            s = datetime(last_monday.year, last_monday.month, last_monday.day)
            out.update(start=s, end=s + timedelta(days=7), explicit=True, detected=True, label="上周")
    elif "下下周" in t:
        next_monday = today - timedelta(days=today.weekday()) + timedelta(days=14)
        wd = _weekday_in(t)
        if wd is not None:
            s, e = _day_range(next_monday + timedelta(days=wd))
            out.update(start=s, end=e, explicit=True, detected=True, label=f"下下周{_WEEK_CN[wd]}")
        else:
            s = datetime(next_monday.year, next_monday.month, next_monday.day)
            out.update(start=s, end=s + timedelta(days=7), explicit=True, detected=True, label="下下周")
    elif "下周末" in t:
        next_monday = today - timedelta(days=today.weekday()) + timedelta(days=7)
        sat = next_monday + timedelta(days=5)
        s = datetime(sat.year, sat.month, sat.day)
        out.update(start=s, end=s + timedelta(days=2), explicit=True, detected=True, label="下周末")
    elif re.search(r"下周|下个星期|下一周", t):
        next_monday = today - timedelta(days=today.weekday()) + timedelta(days=7)
        wd = _weekday_in(t)
        if wd is not None:
            s, e = _day_range(next_monday + timedelta(days=wd))
            out.update(start=s, end=e, explicit=True, detected=True, label=f"下周{_WEEK_CN[wd]}")
        else:
            s = datetime(next_monday.year, next_monday.month, next_monday.day)
            out.update(start=s, end=s + timedelta(days=7), explicit=True, detected=True, label="下周")
    elif re.search(r"周末|这周末|本周末", t):
        monday = today - timedelta(days=today.weekday())
        sat = monday + timedelta(days=5)
        s = datetime(sat.year, sat.month, sat.day)
        out.update(start=s, end=s + timedelta(days=2), explicit=True, detected=True, label="这周末")
    elif re.search(r"这周|本周|这个星期", t):
        monday = today - timedelta(days=today.weekday())
        s = datetime(monday.year, monday.month, monday.day)
        out.update(start=s, end=s + timedelta(days=7), explicit=True, detected=True, label="这周")
    elif re.search(r"下个月|下月", t):
        if today.month == 12:
            ny, nm = today.year + 1, 1
        else:
            ny, nm = today.year, today.month + 1
        dn = _daynum_in(t)
        if dn is not None:
            d = date(ny, nm, min(dn, _month_end(ny, nm).day))
            s, e = _day_range(d)
            out.update(start=s, end=e, explicit=True, detected=True, label=f"下个月{dn}号")
        else:
            s = datetime(ny, nm, 1)
            end = _month_end(ny, nm)
            out.update(start=s, end=datetime(end.year, end.month, end.day) + timedelta(days=1),
                       explicit=True, detected=True, label="下个月")
    elif "上个月" in t:
        first_this = today.replace(day=1)
        last_month_end = first_this - timedelta(days=1)
        s = datetime(last_month_end.year, last_month_end.month, 1)
        out.update(start=s, end=datetime(first_this.year, first_this.month, 1),
                   explicit=True, detected=True, label="上个月")
    elif re.search(r"月底|月末|这个月最后|本月最后|最后.{0,6}[周星期]", t):
        month_end = _month_end(today.year, today.month)
        month_end_start = month_end - timedelta(days=6)
        wd = _weekday_in(t)
        target = _find_weekday(month_end_start, month_end, wd) if wd is not None else None
        if target:
            s, e = _day_range(target)
            out.update(start=s, end=e, explicit=True, detected=True, label=f"月底周{_WEEK_CN[wd]}")
        else:
            s = datetime(month_end_start.year, month_end_start.month, month_end_start.day)
            out.update(start=s, end=datetime(month_end.year, month_end.month, month_end.day) + timedelta(days=1),
                       explicit=True, detected=True, label="月底")
    elif "前几天" in t or "几天前" in t:
        out.update(start=now - timedelta(days=7), end=now, explicit=True, detected=True, label="前几天")
    elif "去年" in t:
        s = datetime(today.year - 1, 1, 1)
        out.update(start=s, end=datetime(today.year, 1, 1), explicit=True, detected=True, label="去年")
    elif "今年" in t:
        s = datetime(today.year, 1, 1)
        out.update(start=s, end=datetime(today.year + 1, 1, 1), explicit=True, detected=True, label="今年")
    elif re.search(r"那天|当时|之前|以前|上回|上次", t):
        # 指代/相对过去：无法确定具体日期 → 仅"检测到时间感"，不启用精确窗口
        out.update(detected=True, label="以前")

    if out["start"] is None and not out["detected"]:
        out.update(start=now, end=now, explicit=False, label="最近")
    return out


def label_for(ts, ts_source="approx", now=None, scope=None) -> str:
    """事件时间 → 注入标签：explicit 给具体，approx 给"大概"。"""
    if not ts:
        return ""
    try:
        ev = datetime.fromisoformat(str(ts)[:19])
    except Exception:
        return ""
    now = now or _now(scope)  # 与 extract 同源时区，避免日期边界差一天
    days = (now.date() - ev.date()).days
    if days < 0:
        return ""
    if str(ts_source) == "explicit":
        if days == 0:
            return "【今天】"
        if days == 1:
            return "【昨天】"
        if days <= 7:
            return f"【{days}天前】"
        if days <= 14:
            return "【上周】"
        return f"【{ev.date().isoformat()}】"
    # approx：只给模糊档
    if days == 0:
        return "【大概今天】"
    if days <= 7:
        return f"【大概{days}天前】"
    if days <= 14:
        return "【大概上周】"
    return f"【大概{max(1, days // 30)}个月前】"
