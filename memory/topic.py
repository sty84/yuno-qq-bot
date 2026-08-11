"""议题化：大类（category）→ 议题（topic）→ 参数（fact / motive / background / mood / playful / confidence）。
话题按实体命名（如“MCP 项目”），同一议题的事实/情绪/玩笑语境聚合在一起，检索时按议题打包。"""

from datetime import datetime

from plugins import _db
from memory import analysis
from memory.extract import classify_event_type, fact_keywords
from memory.extract import extract_entities

CATEGORY_LABELS = {
    "规划": "规划", "学习": "学习", "项目": "项目", "偏好": "偏好", "经历": "经历",
    "健康": "健康", "家庭": "家庭", "工作": "工作", "娱乐": "娱乐", "event": "其他",
}
GENERIC_ENTITIES = {"项目", "服务器", "代码", "部署", "API", "模型", "数据库", "仓库", "学习", "工作"}


def topic_name_of(fact: str, etype=None) -> str:
    """议题名：优先“具体实体 + 类别”（MCP 项目）；无实体时用“领域·事实摘要”语义聚类（v3.1 §6）。"""
    etype = etype or classify_event_type(fact)
    label = CATEGORY_LABELS.get(etype, etype or "其他")
    specific = [e for e in extract_entities(fact) if e.lower() not in GENERIC_ENTITIES]
    if specific:
        return f"{specific[0]} {label}"
    head = str(fact).strip().replace("：", "·")[:10]
    return f"{label}·{head}" if head else label


def find_or_create(scope, key, category, name, importance=0.5, confidence=0.7) -> int:
    tid = _db.topic_find(scope, key, category, name)
    if tid:
        _db.topic_add(scope, key, category, name, importance=importance, confidence=confidence)
        return tid
    return _db.topic_add(
        scope, key, category, name,
        importance=importance, confidence=confidence,
        started_at=datetime.now().isoformat(timespec="seconds"),
    )


def link_fact(scope, key, fact, category, confidence=0.7, an=None) -> int:
    """把一条事实挂到（或新建）议题，写入 fact/mood/playful 参数。返回 topic_id。"""
    an = an or analysis.analyze(fact)
    name = topic_name_of(fact, category)
    tid = find_or_create(
        scope, key, category, name,
        importance=float(an.get("importance", 0.5)),
        confidence=float(confidence),
    )
    ts = datetime.now().isoformat(timespec="seconds")
    _db.topic_param_add(tid, "fact", fact, confidence, ts)
    if an.get("emotion") and an["emotion"] != "平静":
        _db.topic_param_add(tid, "mood", an["emotion"], confidence, ts)
    _db.topic_param_add(
        tid, "playful",
        "true" if an.get("playful") else "false",
        1.0 if an.get("playful") else float(confidence),
        ts,
    )
    return tid


def package(topic_id) -> dict:
    row = _db.topic_get(topic_id)
    if not row:
        return {}
    row["params"] = _db.topic_params(topic_id)
    return row


def search(query, scopes, limit=5) -> list:
    """按词元匹配议题，返回带参数包的议题列表（按重要度排序）。"""
    qt = fact_keywords(query or "")
    out = []
    for scope in scopes:
        for t in _db.topic_rows(scope):
            if not qt or (fact_keywords(t["topic"]) & qt):
                p = package(t["id"])
                out.append(p)
    out.sort(key=lambda x: -float(x.get("importance", 0.5)))
    return out[:limit]


def list_topics(scope=None, limit=50) -> list:
    return [_db.topic_get(t["id"]) for t in _db.topic_rows(scope, limit=limit)]


def invalidate_for_fact(scope, key, fact):
    """纠错联动：含该事实的议题参数降权（标记 stale，供重算/下次 build 修正）。"""
    try:
        _db.topic_param_invalidate(str(fact))
    except Exception:
        pass


def build(scope=None) -> int:
    """回填：为没有议题的事件建议题并挂参数、关联事件。返回新建/更新数。"""
    created = 0
    for ev in _db.event_rows(scope, limit=1000):
        if ev.get("topic_id"):
            continue
        tid = link_fact(
            ev["scope"], ev["key"], ev["title"], ev["etype"],
            confidence=float(ev.get("importance", 0.5)),
        )
        _db.event_set_topic(ev["id"], tid)
        created += 1
    return created
