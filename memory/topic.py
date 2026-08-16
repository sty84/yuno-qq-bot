"""议题化：大类（category）→ 议题（topic）→ 参数（fact / motive / background / mood / playful / confidence）。
话题按实体命名（如“MCP 项目”），同一议题的事实/情绪/玩笑语境聚合在一起，检索时按议题打包。"""

import json
import time
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
    tid = _db.topic_find(scope, key, category, name)  # type: ignore[attr-defined]
    if tid:
        _db.topic_add(scope, key, category, name, importance=importance, confidence=confidence)  # type: ignore[attr-defined]
        return tid
    return _db.topic_add(  # type: ignore[attr-defined]
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
    _db.topic_param_add(tid, "fact", fact, confidence, ts)  # type: ignore[attr-defined]
    if an.get("emotion") and an["emotion"] != "平静":
        _db.topic_param_add(tid, "mood", an["emotion"], confidence, ts)  # type: ignore[attr-defined]
    # v2.2+ 情绪打通：mood 标签保留兼容，并行补存 VAD 向量 + 复合情绪（analysis 结果里本来就带 VAD）
    try:
        vad = [
            round(float(an.get("valence", 0.0)), 4),
            round(float(an.get("arousal", 0.0)), 4),
            round(float(an.get("dominance", 0.0)), 4),
        ]
        _db.topic_param_add(tid, "vad", json.dumps(vad, ensure_ascii=False), confidence, ts)  # type: ignore[attr-defined]
        from memory import emotion as emotion_mod
        compound = emotion_mod.compound_of(fact, an.get("emotion") or "")
        if compound:
            _db.topic_param_add(tid, "compound", compound, confidence, ts)  # type: ignore[attr-defined]
    except Exception as e:
        _stats_err(e)
    _db.topic_param_add(  # type: ignore[attr-defined]
        tid, "playful",
        "true" if an.get("playful") else "false",
        1.0 if an.get("playful") else float(confidence),
        ts,
    )
    return tid


def mood_centroid_from_params(params, window_days=180) -> dict:
    """议题情绪质心：vad 参数按时间衰减 + 位置加权平均（复用 emotion.vad_centroid）。
    返回 {vad, label, intensity, label_zh, trend, n, compound}；无有效 vad 参数返回 None。"""
    from memory import emotion as emotion_mod
    vads = [(i, p) for i, p in enumerate(params or []) if p.get("param") == "vad"]
    if not vads:
        return None  # type: ignore[return-value]
    n = len(vads)
    now = time.time()
    samples = []
    for _i, p in vads:
        try:
            v = json.loads(str(p.get("value") or "[]"))
            v3 = [float(v[0]), float(v[1]), float(v[2])]
        except Exception:
            continue
        age_days = 0.0
        try:
            ut = str(p.get("updated_at") or "")
            if ut:
                age_days = max(0.0, (now - datetime.fromisoformat(ut[:19]).timestamp()) / 86400.0)
        except Exception:
            pass
        samples.append((age_days, v3))
    if not samples:
        return None  # type: ignore[return-value]
    c = emotion_mod.vad_centroid(samples, float(window_days))
    s = c["vad"]  # type: ignore[index]
    label, intensity, _ = emotion_mod.label_from_vad(s)
    compound = next((p.get("value") for p in reversed(params or []) if p.get("param") == "compound"), "")
    return {
        "vad": s,
        "label": label,
        "intensity": round(intensity, 2),
        "label_zh": emotion_mod.label_zh(s),
        "trend": c["trend"],  # type: ignore[index]
        "n": n,
        "compound": str(compound or ""),
    }


def mood_centroid(topic_id, window_days=180) -> dict:
    """按 topic_id 取议题情绪质心。"""
    return mood_centroid_from_params(_db.topic_params(topic_id), window_days)  # type: ignore[attr-defined]


def mood_text(topic_id=None, params=None) -> str:
    """议题情绪底色文本：'低落（偏无力）→ 近两周转开心'；复合时带底色。"""
    c = mood_centroid_from_params(params) if params is not None else (
        mood_centroid(topic_id) if topic_id else None
    )
    if not c or c["label"] == "平静":
        return ""
    parts = [c["label_zh"]]
    a = c["vad"]["a"]
    if abs(a) < 0.25:
        parts.append("偏平静/无力")
    elif a > 0.55:
        parts.append("偏强烈")
    if c["trend"] != "平稳":
        parts.append("近两周转开心" if c["trend"] == "变好" else "近两周转低落")
    if c.get("compound"):
        parts.append(f"复合底色：{c['compound']}")
    return "；".join(parts)


def mood_map(scopes, limit=200) -> dict:
    """fact → 议题情绪质心（供检索层心境一致性加权）。"""
    out = {}
    for scope in scopes or []:
        for t in _db.topic_rows(scope, limit=limit):  # type: ignore[attr-defined]
            params = _db.topic_params(t["id"])  # type: ignore[attr-defined]
            c = mood_centroid_from_params(params)
            if not c:
                continue
            for p in params:
                if p.get("param") == "fact":
                    out[p.get("value")] = c
    return out


def mood_eval(limit=200) -> dict:
    """议题 mood-VAD 一致性：
    1) write_consistency：同一时刻写入的 mood 标签 ↔ vad 向量应一致（来自同一 analysis）；
    2) centroid_consistency：聚合质心应贴近某个 mood 标签（混合情绪的议题可合理地不贴近）；
    3) compound_topics：复合情绪写入后不坍缩；
    4) vad_table_drift：analysis.EMOTION_METRICS 与 emotion.LABEL_VAD 跨表漂移。"""
    from memory import analysis, emotion as emotion_mod
    write_n = write_ok = centroid_n = centroid_ok = compound_n = 0
    samples = []
    for t in _db.topic_rows(limit=limit):  # type: ignore[attr-defined]
        params = _db.topic_params(t["id"])  # type: ignore[attr-defined]
        moods = sorted({p.get("value") for p in params if p.get("param") == "mood"})
        # 写入一致性：同一 updated_at 组内 mood/vad 按插入顺序配对
        by_ts = {}  # type: ignore[var-annotated]
        for p in params:
            by_ts.setdefault(str(p.get("updated_at") or ""), []).append(p)
        for ps in by_ts.values():
            ms = [p for p in ps if p.get("param") == "mood"]
            vs = [p for p in ps if p.get("param") == "vad"]
            for m, v in zip(ms, vs):
                write_n += 1
                mt = analysis.EMOTION_METRICS.get(m.get("value"))
                ok = True
                if mt:
                    try:
                        vv = json.loads(str(v.get("value") or "[]"))
                        d = emotion_mod.dist(
                            {"v": vv[0], "a": vv[1], "d": vv[2]},
                            {"v": mt["valence"], "a": mt["arousal"], "d": mt["dominance"]},
                        )
                        ok = d < 0.15
                    except Exception:
                        ok = False
                if ok:
                    write_ok += 1
        c = mood_centroid_from_params(params)
        if not c:
            continue
        centroid_n += 1
        cok = False
        for m in moods:
            mt = analysis.EMOTION_METRICS.get(m)
            if not mt:
                continue
            exp = {"v": mt["valence"], "a": mt["arousal"], "d": mt["dominance"]}
            if emotion_mod.dist(c["vad"], exp) < 0.45:
                cok = True
                break
        if cok:
            centroid_ok += 1
        comps = [p.get("value") for p in params if p.get("param") == "compound"]
        if comps:
            compound_n += 1
        samples.append({
            "topic": t.get("topic"), "centroid_label": c["label"],
            "moods": moods, "centroid_ok": cok,
            "compound": comps[-1] if comps else "",
        })
    return {
        "n": len(_db.topic_rows(limit=limit)),  # type: ignore[attr-defined]
        "write_consistency": round(write_ok / write_n, 3) if write_n else None,
        "write_n": write_n,
        "centroid_consistency": round(centroid_ok / centroid_n, 3) if centroid_n else None,
        "centroid_n": centroid_n,
        "compound_topics": compound_n,
        "samples": samples[:10],
        "vad_table_drift": _vad_table_drift(),
    }


def _vad_table_drift() -> dict:
    """analysis.EMOTION_METRICS 与 emotion.LABEL_VAD 的同名标签 VAD 漂移（跨表一致性）。"""
    from memory import analysis, emotion as emotion_mod
    drifts = {}
    for label, m in analysis.EMOTION_METRICS.items():
        lv = emotion_mod.LABEL_VAD.get(label)
        if not lv:
            continue
        d = emotion_mod.dist({"v": m["valence"], "a": m["arousal"], "d": m["dominance"]}, lv)
        if d > 0.25:
            drifts[label] = {
                "metrics": {k: m.get(k) for k in ("valence", "arousal", "dominance")},
                "label_vad": lv,
                "dist": round(d, 3),
            }
    return drifts


def backfill_vad(limit=200) -> dict:
    """给只有 mood 标签、没有 vad 参数的旧议题补近似 VAD（来自 analysis.EMOTION_METRICS）
    和复合情绪参数（emotion.compound_of）。幂等：已有 vad 的议题跳过。"""
    from memory import analysis, emotion as emotion_mod
    done = skipped = 0
    samples = []
    for t in _db.topic_rows(limit=limit):  # type: ignore[attr-defined]
        params = _db.topic_params(t["id"])  # type: ignore[attr-defined]
        if any(p.get("param") == "vad" for p in params):
            skipped += 1
            continue
        moods = [p for p in params if p.get("param") == "mood"]
        facts = [p.get("value") for p in params if p.get("param") == "fact"]
        added = 0
        last_mood = None
        for p in moods:
            m = analysis.EMOTION_METRICS.get(str(p.get("value") or ""))
            if not m:
                continue
            last_mood = p
            vad = [
                round(float(m["valence"]), 4),
                round(float(m["arousal"]), 4),
                round(float(m["dominance"]), 4),
            ]
            _db.topic_param_add(  # type: ignore[attr-defined]
                t["id"], "vad", json.dumps(vad, ensure_ascii=False),
                float(p.get("confidence") or 0.7), str(p.get("updated_at") or ""),
            )
            added += 1
        if not added:
            continue
        compound = ""
        try:
            fact = facts[-1] if facts else str(t.get("topic") or "")
            compound = emotion_mod.compound_of(fact, str(last_mood.get("value") or "") if last_mood else "")
            if compound and not any(pp.get("param") == "compound" for pp in params):
                _db.topic_param_add(  # type: ignore[attr-defined]
                    t["id"], "compound", compound, 0.6,
                    str(last_mood.get("updated_at") or "") if last_mood else "",
                )
        except Exception as e:
            _stats_err(e)
        done += 1
        samples.append({"topic": t.get("topic"), "vad_added": added, "compound": compound})
    return {"backfilled": done, "already_had_vad": skipped, "samples": samples[:10]}


def package(topic_id) -> dict:
    row = _db.topic_get(topic_id)  # type: ignore[attr-defined]
    if not row:
        return {}
    row["params"] = _db.topic_params(topic_id)  # type: ignore[attr-defined]
    return row


def search(query, scopes, limit=5) -> list:
    """按词元匹配议题，返回带参数包的议题列表（按重要度排序）。"""
    qt = fact_keywords(query or "")
    out = []
    for scope in scopes:
        for t in _db.topic_rows(scope):  # type: ignore[attr-defined]
            if not qt or (fact_keywords(t["topic"]) & qt):
                p = package(t["id"])
                out.append(p)
    out.sort(key=lambda x: -float(x.get("importance", 0.5)))
    return out[:limit]


def list_topics(scope=None, limit=50) -> list:
    return [_db.topic_get(t["id"]) for t in _db.topic_rows(scope, limit=limit)]  # type: ignore[attr-defined]


def invalidate_for_fact(scope, key, fact):
    """纠错联动：含该事实的议题参数降权（标记 stale，供重算/下次 build 修正）。"""
    try:
        _db.topic_param_invalidate(str(fact))
    except Exception as e:
        _stats_err(e)


def build(scope=None) -> int:
    """回填：为没有议题的事件建议题并挂参数、关联事件。返回新建/更新数。"""
    created = 0
    for ev in _db.event_rows(scope, limit=1000):  # type: ignore[attr-defined]
        if ev.get("topic_id"):
            continue
        tid = link_fact(
            ev["scope"], ev["key"], ev["title"], ev["etype"],
            confidence=float(ev.get("importance", 0.5)),
        )
        _db.event_set_topic(ev["id"], tid)  # type: ignore[attr-defined]
        created += 1
    return created


def _stats_err(e):
    """裸 except 审计（v2.2）：错误计数 + 日志，供消融/排查。"""
    try:
        import memory.stats as _st
        _st.bump_err("topic", e)
    except Exception:
        pass
