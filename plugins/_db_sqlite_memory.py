# -*- coding: utf-8 -*-
"""SQLite 数据操作：记忆/事件/议题等核心数据。"""
import json
from datetime import datetime, timedelta

from plugins import _db_sqlite_core as _core
from plugins._db_sqlite_core import _connect, _maybe_commit, _lock, _stats_err

# ===== facts（记忆）=====
def facts_replace(scene, key, facts, updated_at=""):
    with _lock:
        c = _connect()
        c.execute("DELETE FROM facts WHERE scene=? AND key=?", (scene, key))
        for i, fact in enumerate(facts):
            c.execute(
                "INSERT INTO facts(scene,key,idx,fact,updated_at) VALUES(?,?,?,?,?)",
                (scene, key, i, str(fact), updated_at),
            )
        _maybe_commit()


def facts_get(scene, key):
    with _lock:
        rows = _connect().execute(
            "SELECT fact FROM facts WHERE scene=? AND key=? ORDER BY idx", (scene, key)
        ).fetchall()
        return [r[0] for r in rows]


def facts_updated_at(scene, key):
    with _lock:
        row = _connect().execute(
            "SELECT MAX(updated_at) FROM facts WHERE scene=? AND key=?", (scene, key)
        ).fetchone()
        return row[0] or ""


# ===== kv（群计数、群列表等）=====
def kv_set(namespace, key, value):
    with _lock:
        c = _connect()
        c.execute(
            "INSERT OR REPLACE INTO kv(namespace,key,value) VALUES(?,?,?)",
            (namespace, key, json.dumps(value, ensure_ascii=False)),
        )
        _maybe_commit()


def kv_get(namespace, key, default=None):
    with _lock:
        row = _connect().execute(
            "SELECT value FROM kv WHERE namespace=? AND key=?", (namespace, key)
        ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except Exception as e:
            _stats_err(e)
            return default


def kv_get_raw(namespace, key, default=None):
    """读取 kv 原始 JSON 字符串（CAS 用：保证与库内字节完全一致）。"""
    with _lock:
        row = _connect().execute(
            "SELECT value FROM kv WHERE namespace=? AND key=?", (namespace, key)
        ).fetchone()
        return row[0] if row else default


def kv_cas(namespace, key, old_raw, new_raw):
    """Compare-and-swap：仅当现值等于 old_raw 时写入 new_raw（跨进程原子，防并发重复）。"""
    with _lock:
        c = _connect()
        cur = c.execute(
            "SELECT value FROM kv WHERE namespace=? AND key=?", (namespace, key)
        ).fetchone()
        if cur is None or cur[0] != old_raw:
            return False
        c.execute(
            "UPDATE kv SET value=? WHERE namespace=? AND key=? AND value=?",
            (new_raw, namespace, key, old_raw),
        )
        _maybe_commit()
        return c.total_changes > 0



# ===== 技能库（Voyager 式 skill library）=====
def skill_add(situation, action, result="", condition="", failure_reason="", source="", success=0.5, updated_at=""):
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO skills(situation,action,result,condition,failure_reason,source,success,tries,updated_at) "
            "VALUES(?,?,?,?,?,?,?,1,?) "
            "ON CONFLICT(situation,action) DO UPDATE SET "
            "result=excluded.result, condition=excluded.condition, failure_reason=excluded.failure_reason, "
            "source=excluded.source, success=excluded.success, tries=tries+1, updated_at=excluded.updated_at",
            (str(situation)[:200], str(action)[:400], str(result or "")[:500], str(condition or "")[:200],
             str(failure_reason or "")[:300], str(source or "")[:50], float(success),
             updated_at or datetime.now().isoformat(timespec="seconds")),
        )
        _maybe_commit()


def skill_rows(limit=200):
    with _lock:
        rows = _connect().execute(
            "SELECT * FROM skills ORDER BY success DESC, tries DESC LIMIT ?", (max(1, int(limit)),)
        ).fetchall()
        return [dict(r) for r in rows]


def skill_update(situation, action, success=None, result=None, failure_reason=None, condition=None):
    with _lock:
        c = _connect()
        sets, params = [], []
        if success is not None:
            sets.append("success=?")
            params.append(float(success))
        if result is not None:
            sets.append("result=?")
            params.append(str(result)[:500])
        if failure_reason is not None:
            sets.append("failure_reason=?")
            params.append(str(failure_reason)[:300])
        if condition is not None:
            sets.append("condition=?")
            params.append(str(condition)[:200])
        if not sets:
            return
        sets.append("updated_at=?")
        params.append(datetime.now().isoformat(timespec="seconds"))
        params.extend([str(situation), str(action)])
        c.execute(f"UPDATE skills SET {', '.join(sets)} WHERE situation=? AND action=?", params)
        _maybe_commit()


def skill_search(query, limit=10):
    """按情境关键词粗略检索技能（后续可换语义检索）。"""
    q = str(query or "").strip()
    if not q:
        return []
    with _lock:
        rows = _connect().execute(
            "SELECT * FROM skills WHERE situation LIKE ? OR action LIKE ? OR result LIKE ? "
            "ORDER BY success DESC, tries DESC LIMIT ?",
            (f"%{q}%", f"%{q}%", f"%{q}%", max(1, int(limit))),
        ).fetchall()
        return [dict(r) for r in rows]

# ===== 物品事件溯源（P0-1：位置历史 / 激活 / 找东西）=====
def item_event_add(item, ts, event, from_place="", to_place="", cause="", seen_by=""):
    """追加一条物品事件（move/give/see/take/consume/lost/find…）。"""
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO item_events(item,ts,event,from_place,to_place,cause,seen_by) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                str(item)[:60], str(ts), str(event)[:20],
                str(from_place)[:80], str(to_place)[:80],
                str(cause)[:80], str(seen_by)[:20],
            ),
        )
        _maybe_commit()


def item_event_add_many(rows):
    """批量追加物品事件（see 批量用，一次事务）。rows: dict 列表。"""
    if not rows:
        return
    with _lock:
        c = _connect()
        c.executemany(
            "INSERT INTO item_events(item,ts,event,from_place,to_place,cause,seen_by) "
            "VALUES(?,?,?,?,?,?,?)",
            [
                (
                    str(r.get("item", ""))[:60], str(r.get("ts", "")), str(r.get("event", ""))[:20],
                    str(r.get("from_place", ""))[:80], str(r.get("to_place", ""))[:80],
                    str(r.get("cause", ""))[:80], str(r.get("seen_by", ""))[:20],
                )
                for r in rows
            ],
        )
        _maybe_commit()


def item_event_rows(item=None, limit=500):
    """物品事件流水（新→旧）。"""
    with _lock:
        c = _connect()
        if item:
            cur = c.execute(
                "SELECT * FROM item_events WHERE item=? ORDER BY ts DESC, id DESC LIMIT ?",
                (str(item)[:60], int(limit)),
            )
        else:
            cur = c.execute(
                "SELECT * FROM item_events ORDER BY ts DESC, id DESC LIMIT ?", (int(limit),)
            )
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in rows]


def item_position_at(item, ts):
    """ts 时刻该物品的位置（事件溯源投影）：最后一次决定位置的
    move/give/see/find 事件；lost 表示那时已丢失（known=False）。"""
    with _lock:
        c = _connect()
        cur = c.execute(
            "SELECT * FROM item_events WHERE item=? AND ts<=? "
            "AND event IN ('move','give','see','find','lost') "
            "ORDER BY ts DESC, id DESC LIMIT 1",
            (str(item)[:60], str(ts)),
        )
        rows = cur.fetchall()
        if not rows:
            return None
        cols = [d[0] for d in cur.description]
        r = dict(zip(cols, rows[0]))
        to_place = str(r.get("to_place") or "")
        room, container = "", ""
        if "/" in to_place:
            room, container = to_place.split("/", 1)
        elif to_place:
            room = to_place
        return {
            "item": r["item"], "ts": r["ts"], "event": r["event"],
            "room": room, "container": container,
            "known": r["event"] != "lost",
            "cause": r.get("cause", ""),
        }


def item_events_prune(days=90) -> int:
    """清理超过保留期的物品事件（默认 90 天）。"""
    cutoff = (datetime.now() - timedelta(days=int(days))).isoformat(timespec="seconds")
    with _lock:
        c = _connect()
        cur = c.execute("DELETE FROM item_events WHERE ts < ?", (cutoff,))
        _maybe_commit()
        return cur.rowcount


# ===== 程序记忆（System 1 习惯表）=====
def procedure_upsert(situation, action, success, updated_at=""):
    """记录一次"情境→动作"的结果（成功率 = 累计平均）。"""
    situation = str(situation or "")[:120]
    action = str(action or "")[:400]
    if not situation or not action:
        return
    success = 1.0 if float(success) >= 0.5 else 0.0
    with _lock:
        c = _connect()
        row = c.execute(
            "SELECT success, tries FROM procedures WHERE situation=? AND action=?",
            (situation, action),
        ).fetchone()
        if row:
            tries = int(row[1]) + 1
            # 指数滑动平均（EMA）：旧样本衰减，新反馈能真正改变成功率（学得动）
            alpha = 0.3
            s = round((1.0 - alpha) * float(row[0]) + alpha * success, 3)
            c.execute(
                "UPDATE procedures SET success=?, tries=?, updated_at=? WHERE situation=? AND action=?",
                (s, tries, str(updated_at), situation, action),
            )
        else:
            c.execute(
                "INSERT INTO procedures(situation,action,success,tries,updated_at) VALUES(?,?,?,1,?)",
                (situation, action, success, str(updated_at)),
            )
        _maybe_commit()


def procedure_rows(min_tries=0, limit=200):
    """程序记忆列表（按成功率排序）。"""
    with _lock:
        cur = _connect().execute(
            "SELECT situation, action, success, tries, updated_at FROM procedures "
            "WHERE tries>=? ORDER BY success DESC, tries DESC LIMIT ?",
            (int(min_tries), int(limit)),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def procedure_clear():
    with _lock:
        c = _connect()
        c.execute("DELETE FROM procedures")
        _maybe_commit()


def invalidation_add(scope, key, fact, reason=""):
    """双轨制一致性：纠错后写入"待重算队列"。"""
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO state_invalidations(scope,key,fact,reason,ts) VALUES(?,?,?,?,?)",
            (str(scope), str(key or ""), str(fact)[:200], str(reason)[:40],
             datetime.now().isoformat(timespec="seconds")),
        )
        _maybe_commit()


def invalidation_rows(limit=100):
    with _lock:
        cur = _connect().execute(
            "SELECT id, scope, key, fact, reason FROM state_invalidations "
            "ORDER BY id ASC LIMIT ?",
            (max(1, int(limit)),),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def invalidation_clear_all():
    with _lock:
        c = _connect()
        c.execute("DELETE FROM state_invalidations")
        _maybe_commit()


def exp_log_add(action, detail="", before=None, after=None, delta=None, regression=False):
    """实验日志：每次改动/评测的基线前后与偏差（回归门禁）。"""
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO experiment_log(ts,action,detail,before,after,delta,regression) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                datetime.now().isoformat(timespec="seconds"),
                str(action)[:60], str(detail)[:200],
                json.dumps(before, ensure_ascii=False)[:400] if before is not None else "",
                json.dumps(after, ensure_ascii=False)[:400] if after is not None else "",
                json.dumps(delta, ensure_ascii=False)[:400] if delta is not None else "",
                1 if regression else 0,
            ),
        )
        _maybe_commit()


def exp_log_rows(limit=50):
    with _lock:
        cur = _connect().execute(
            "SELECT id, ts, action, detail, before, after, delta, regression "
            "FROM experiment_log ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),),
        )
        cols = [d[0] for d in cur.description]
        out = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            for k in ("before", "after", "delta"):
                try:
                    d[k] = json.loads(d[k]) if d[k] else None
                except Exception:
                    pass
            out.append(d)
        return out


# ===== 对话回放五维评分（人工 / LLM 双模式）=====
SCORE_DIMS = ("recall", "precision", "coherence", "consistency", "naturalness")


def scenario_score_add(scenario_id, scope, scores, comment="", mode="manual"):
    """保存一次五维评分（manual=人工，llm=机器分）。"""
    scores = {k: float(scores.get(k, 0)) for k in SCORE_DIMS}
    avg = round(sum(scores.values()) / len(SCORE_DIMS), 2)
    with _lock:
        c = _connect()
        cur = c.execute(
            "INSERT INTO scenario_scores(ts,scenario_id,scope,mode,scores,comment,avg) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                datetime.now().isoformat(timespec="seconds"),
                str(scenario_id or "")[:120],
                str(scope or "")[:120],
                str(mode or "manual")[:20],
                json.dumps(scores, ensure_ascii=False),
                str(comment or "")[:300],
                avg,
            ),
        )
        _maybe_commit()
        return {
            "id": cur.lastrowid,
            "ts": datetime.now().isoformat(timespec="seconds"),
            "scenario_id": str(scenario_id or ""),
            "scope": str(scope or ""),
            "mode": str(mode or "manual")[:20],
            "scores": scores,
            "comment": str(comment or "")[:300],
            "avg": avg,
        }


def scenario_score_rows(limit=100):
    with _lock:
        cur = _connect().execute(
            "SELECT id, ts, scenario_id, scope, mode, scores, comment, avg "
            "FROM scenario_scores ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),),
        )
        cols = [d[0] for d in cur.description]
        out = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            try:
                d["scores"] = json.loads(d["scores"]) if d["scores"] else {}
            except Exception:
                d["scores"] = {}
            out.append(d)
        return out


# ===== LLM token / 成本观测 =====
def llm_cost_add(ts, module="chat", detail="", prompt_tokens=0, completion_tokens=0, chars=0):
    """记录一次 LLM 调用的 token 消耗（按模块/细节归因，供成本页与机制权衡）。"""
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO llm_cost(ts,module,detail,prompt_tokens,completion_tokens,chars) "
            "VALUES(?,?,?,?,?,?)",
            (
                str(ts or datetime.now().isoformat(timespec="seconds")),
                str(module or "chat")[:40],
                str(detail or "")[:200],
                max(0, int(prompt_tokens or 0)),
                max(0, int(completion_tokens or 0)),
                max(0, int(chars or 0)),
            ),
        )
        _maybe_commit()


def llm_cost_clear():
    """清空 LLM 成本观测表（测试隔离 / 运维重置）。"""
    with _lock:
        c = _connect()
        c.execute("DELETE FROM llm_cost")
        _maybe_commit()


def llm_cost_summary(days=30) -> dict:
    """按天 / 按模块 / 按检索路径聚合 token 消耗。"""
    cutoff = (datetime.now() - timedelta(days=max(1, int(days)))).isoformat(timespec="seconds")
    with _lock:
        cur = _connect().execute(
            "SELECT ts,module,detail,prompt_tokens,completion_tokens "
            "FROM llm_cost WHERE ts >= ? ORDER BY ts",
            (cutoff,),
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    total = {"calls": len(rows), "prompt": 0, "completion": 0}
    by_day: dict = {}
    by_module: dict = {}
    by_path: dict = {}
    for r in rows:
        p, c = int(r["prompt_tokens"]), int(r["completion_tokens"])
        total["prompt"] += p
        total["completion"] += c
        day = str(r["ts"])[:10]
        d = by_day.setdefault(day, {"calls": 0, "prompt": 0, "completion": 0})
        d["calls"] += 1
        d["prompt"] += p
        d["completion"] += c
        m = by_module.setdefault(str(r["module"] or "chat"), {"calls": 0, "prompt": 0, "completion": 0})
        m["calls"] += 1
        m["prompt"] += p
        m["completion"] += c
        # 检索路径：rerank 的 detail 里记录参与路径（lexical/vector/graph/…）
        if str(r["module"]) == "rerank":
            for path in str(r["detail"] or "").split(","):
                path = path.strip()
                if not path:
                    continue
                q = by_path.setdefault(path, {"calls": 0, "prompt": 0, "completion": 0})
                q["calls"] += 1
                q["prompt"] += p
                q["completion"] += c
    return {
        "days": int(days),
        "total": total,
        "by_day": [{"date": k, **v} for k, v in sorted(by_day.items())],
        "by_module": [{"module": k, **v} for k, v in sorted(by_module.items(), key=lambda x: -(x[1]["prompt"] + x[1]["completion"]))],
        "by_path": [{"path": k, **v} for k, v in sorted(by_path.items(), key=lambda x: -(x[1]["prompt"] + x[1]["completion"]))],
    }


# ===== 犹豫层日志（管理台可回看）=====
def hesitation_log_add(ts, scope, kind, action, reason, delay_s=0, monologue=""):
    """记录一次犹豫决策（保留最近 500 条）。"""
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO hesitation_log(ts,scope,kind,action,reason,delay_s,monologue) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                str(ts or datetime.now().isoformat(timespec="seconds")),
                str(scope or "")[:80], str(kind or "")[:30],
                str(action or "")[:20], str(reason or "")[:40],
                max(0, int(delay_s or 0)), str(monologue or "")[:120],
            ),
        )
        c.execute(
            "DELETE FROM hesitation_log WHERE id NOT IN "
            "(SELECT id FROM hesitation_log ORDER BY id DESC LIMIT 500)"
        )
        _maybe_commit()


def hesitation_log_rows(limit=50):
    with _lock:
        cur = _connect().execute(
            "SELECT id,ts,scope,kind,action,reason,delay_s,monologue "
            "FROM hesitation_log ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# ===== 空间/心智状态表（P0 数据模型优化：kv JSON → 正规表，带一次性迁移）=====
_MIGRATED = {"space_state": False, "item_activation": False, "item_search": False,
             "mind_intention": False, "space_events": False, "ai_actions": False}


def space_state_get():
    """家内房间状态（单行）。"""
    with _lock:
        if not _MIGRATED["space_state"]:
            _MIGRATED["space_state"] = True
            old = kv_get("memory", "space_room")
            if old:
                space_state_set(old)
                c = _connect()
                c.execute("DELETE FROM kv WHERE namespace='memory' AND key='space_room'")
                _maybe_commit()
        row = _connect().execute("SELECT * FROM space_state WHERE id=1").fetchone()
        if row is None:
            return None
        d = dict(row)
        try:
            d["path"] = json.loads(d.get("path") or "[]")
        except Exception as e:
            _stats_err(e)
            d["path"] = []
        return d


def space_state_set(st):
    st = st or {}
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO space_state(id,room,state,from_room,to_room,path,depart_ts,arrive_ts,updated_ts) "
            "VALUES(1,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET room=excluded.room,state=excluded.state,"
            "from_room=excluded.from_room,to_room=excluded.to_room,path=excluded.path,"
            "depart_ts=excluded.depart_ts,arrive_ts=excluded.arrive_ts,updated_ts=excluded.updated_ts",
            (
                str(st.get("room", "客厅")), str(st.get("state", "在场")),
                str(st.get("from", "")), str(st.get("to", "")),
                json.dumps(st.get("path", []), ensure_ascii=False),
                str(st.get("depart_ts", "")), str(st.get("arrive_ts", "")),
                str(st.get("updated_ts", "")),
            ),
        )
        _maybe_commit()


def item_activation_rows():
    """物品激活全量 {item: {seen_ts, count}}。"""
    with _lock:
        if not _MIGRATED["item_activation"]:
            _MIGRATED["item_activation"] = True
            old = kv_get("memory", "item_activation")
            if old:
                item_activation_set(old)
                c = _connect()
                c.execute("DELETE FROM kv WHERE namespace='memory' AND key='item_activation'")
                _maybe_commit()
        rows = _connect().execute(
            "SELECT item, seen_ts, count FROM item_activation_state"
        ).fetchall()
        return {r["item"]: {"seen_ts": r["seen_ts"], "count": r["count"]} for r in rows}


def item_activation_set(items):
    """批量 upsert 物品激活。"""
    if not items:
        return
    with _lock:
        c = _connect()
        c.executemany(
            "INSERT INTO item_activation_state(item,seen_ts,count) VALUES(?,?,?) "
            "ON CONFLICT(item) DO UPDATE SET seen_ts=excluded.seen_ts,count=excluded.count",
            [(str(k)[:60], str(v.get("seen_ts", "")), int(v.get("count", 0))) for k, v in items.items()],
        )
        _maybe_commit()


def item_search_rows():
    with _lock:
        if not _MIGRATED["item_search"]:
            _MIGRATED["item_search"] = True
            old = kv_get("memory", "item_search")
            if old:
                for scope, d in old.items():
                    item_search_set(scope, d)
                c = _connect()
                c.execute("DELETE FROM kv WHERE namespace='memory' AND key='item_search'")
                _maybe_commit()
        rows = _connect().execute(
            "SELECT scope, name, queue, step, started_at FROM item_search_state"
        ).fetchall()
        out = {}
        for r in rows:
            try:
                q = json.loads(r["queue"] or "[]")
            except Exception as e:
                _stats_err(e)
                q = []
            out[r["scope"]] = {
                "name": r["name"], "queue": q, "step": r["step"],
                "started_at": r["started_at"],
            }
        return out


def item_search_set(scope, data):
    data = data or {}
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO item_search_state(scope,name,queue,step,started_at,updated_ts) "
            "VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(scope) DO UPDATE SET name=excluded.name,queue=excluded.queue,"
            "step=excluded.step,started_at=excluded.started_at,updated_ts=excluded.updated_ts",
            (
                str(scope), str(data.get("name", "")),
                json.dumps(data.get("queue", []), ensure_ascii=False),
                int(data.get("step", 0)), str(data.get("started_at", "")),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        _maybe_commit()


def item_search_delete(scope):
    with _lock:
        c = _connect()
        c.execute("DELETE FROM item_search_state WHERE scope=?", (str(scope),))
        _maybe_commit()


def mind_intention_rows():
    with _lock:
        if not _MIGRATED["mind_intention"]:
            _MIGRATED["mind_intention"] = True
            old = kv_get("memory", "mind_intention")
            if old:
                for scope, d in old.items():
                    mind_intention_set(scope, d)
                c = _connect()
                c.execute("DELETE FROM kv WHERE namespace='memory' AND key='mind_intention'")
                _maybe_commit()
        rows = _connect().execute(
            "SELECT scope,title,source,strength,state,due,condition,started_at,updated_at "
            "FROM mind_intention_state"
        ).fetchall()
        return {r["scope"]: dict(r) for r in rows}


def mind_intention_set(scope, data):
    data = data or {}
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO mind_intention_state("
            "scope,title,source,strength,state,due,condition,started_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(scope) DO UPDATE SET title=excluded.title,source=excluded.source,"
            "strength=excluded.strength,state=excluded.state,due=excluded.due,"
            "condition=excluded.condition,started_at=excluded.started_at,updated_at=excluded.updated_at",
            (
                str(scope), str(data.get("title", "")), str(data.get("source", "")),
                float(data.get("strength", 0.0)), str(data.get("state", "committed")),
                str(data.get("due", "")), str(data.get("condition", ""))[:120],
                str(data.get("started_at", "")), str(data.get("updated_at", "")),
            ),
        )
        _maybe_commit()


def mind_intention_delete(scope):
    with _lock:
        c = _connect()
        c.execute("DELETE FROM mind_intention_state WHERE scope=?", (str(scope),))
        _maybe_commit()


def space_event_add(ts, kind, detail, memorable=False):
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO space_events_state(ts,kind,detail,memorable) VALUES(?,?,?,?)",
            (str(ts), str(kind)[:20], str(detail)[:80], 1 if memorable else 0),
        )
        _maybe_commit()


def space_event_rows(limit=200):
    """空间事件（新→旧）。"""
    with _lock:
        if not _MIGRATED["space_events"]:
            _MIGRATED["space_events"] = True
            old = kv_get("memory", "space_events")
            if old and old.get("rows"):
                for r in old["rows"][-200:]:
                    space_event_add(r.get("ts", ""), r.get("kind", ""), r.get("detail", ""), r.get("memorable", False))
                c = _connect()
                c.execute("DELETE FROM kv WHERE namespace='memory' AND key='space_events'")
                _maybe_commit()
        rows = _connect().execute(
            "SELECT id, ts, kind, detail, memorable FROM space_events_state "
            "ORDER BY ts DESC, id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["memorable"] = bool(d.get("memorable"))
            out.append(d)
        return out


def space_event_prune(days=7) -> int:
    cutoff = (datetime.now() - timedelta(days=int(days))).isoformat(timespec="seconds")
    with _lock:
        c = _connect()
        cur = c.execute("DELETE FROM space_events_state WHERE ts < ?", (cutoff,))
        _maybe_commit()
        return cur.rowcount


def ai_action_add(ts, scope, action, detail=""):
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO ai_actions_state(ts,scope,action,detail) VALUES(?,?,?,?)",
            (str(ts), str(scope)[:40], str(action)[:40], str(detail)[:80]),
        )
        _maybe_commit()


def ai_action_rows(scope="", limit=60):
    with _lock:
        if not _MIGRATED["ai_actions"]:
            _MIGRATED["ai_actions"] = True
            old = kv_get("memory", "ai_actions")
            if old and old.get("items"):
                for r in old["items"][-60:]:
                    ai_action_add(r.get("ts", ""), r.get("scope", ""), r.get("action", ""), r.get("detail", ""))
                c = _connect()
                c.execute("DELETE FROM kv WHERE namespace='memory' AND key='ai_actions'")
                _maybe_commit()
        sql = "SELECT ts, scope, action, detail FROM ai_actions_state"
        params = []
        if scope:
            sql += " WHERE scope=?"
            params.append(scope)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        return [dict(r) for r in _connect().execute(sql, params).fetchall()]


# ===== 绑定 =====
def binding_set(uid, gid, mid):
    with _lock:
        c = _connect()
        c.execute(
            "INSERT OR REPLACE INTO bindings(user_openid,group_id,member_openid) VALUES(?,?,?)",
            (uid, gid, mid),
        )
        _maybe_commit()


def binding_delete_user_group(uid, gid):
    with _lock:
        c = _connect()
        c.execute("DELETE FROM bindings WHERE user_openid=? AND group_id=?", (uid, gid))
        _maybe_commit()


def binding_delete_member(gid, mid):
    with _lock:
        c = _connect()
        c.execute("DELETE FROM bindings WHERE group_id=? AND member_openid=?", (gid, mid))
        _maybe_commit()


def binding_find_user_for_member(gid, mid):
    with _lock:
        row = _connect().execute(
            "SELECT user_openid FROM bindings WHERE group_id=? AND member_openid=?",
            (gid, mid),
        ).fetchone()
        return row[0] if row else None


def binding_groups_for_user(uid):
    with _lock:
        rows = _connect().execute(
            "SELECT group_id, member_openid FROM bindings WHERE user_openid=?", (uid,)
        ).fetchall()
        return {r[0]: r[1] for r in rows}


def bindings_all():
    with _lock:
        rows = _connect().execute("SELECT * FROM bindings").fetchall()
        result = {}
        for r in rows:
            result.setdefault(r["user_openid"], {})[r["group_id"]] = r["member_openid"]
        return result


# ===== 分数 =====
def score_add(player, game, delta=1):
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO scores(player,game,score) VALUES(?,?,?) "
            "ON CONFLICT(player,game) DO UPDATE SET score=score+?",
            (player, game, delta, delta),
        )
        _maybe_commit()


def scores_all():
    with _lock:
        rows = _connect().execute("SELECT * FROM scores").fetchall()
        result = {}
        for r in rows:
            result.setdefault(r["player"], {})[r["game"]] = r["score"]
        return result


# ===== 昵称 =====
def nickname_set(player, nickname):
    with _lock:
        c = _connect()
        c.execute(
            "INSERT OR REPLACE INTO nicknames(player,nickname) VALUES(?,?)",
            (player, nickname),
        )
        _maybe_commit()


def nickname_get(player):
    with _lock:
        row = _connect().execute(
            "SELECT nickname FROM nicknames WHERE player=?", (player,)
        ).fetchone()
        return row[0] if row else None


# ===== 状态 =====
def state_get():
    with _lock:
        rows = _connect().execute("SELECT k,v FROM state").fetchall()
        result = {}
        for r in rows:
            try:
                result[r["k"]] = json.loads(r["v"])
            except Exception as e:
                _stats_err(e)
                result[r["k"]] = r["v"]
        return result


def state_set(data):
    with _lock:
        c = _connect()
        c.execute("DELETE FROM state")
        for k, v in data.items():
            c.execute("INSERT INTO state(k,v) VALUES(?,?)", (k, json.dumps(v, ensure_ascii=False)))
        _maybe_commit()


# ===== 审计 =====
def audit_add(action, target="", detail="", operator=""):
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO audit(ts,action,target,detail,operator) VALUES(?,?,?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), action, target, detail, operator),
        )
        c.execute(
            "DELETE FROM audit WHERE id NOT IN "
            "(SELECT id FROM audit ORDER BY id DESC LIMIT ?)",
            (_core.AUDIT_MAX,),
        )
        _maybe_commit()


def audit_query(limit=50, action=None):
    with _lock:
        sql = "SELECT ts,action,target,detail,operator FROM audit"
        params = []
        if action:
            sql += " WHERE action=?"
            params.append(action)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        rows = _connect().execute(sql, params).fetchall()
        return [dict(r) for r in rows]


# ===== 通知队列（App/MCP 触发，QQ 播报插件消费）=====
def notif_add(target_type, target, content, scheduled_at=""):
    """入通知队列；scheduled_at（ISO，可空=立即）用于犹豫层延迟发送。"""
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO notifications(target_type,target,content,created_at,scheduled_at) VALUES(?,?,?,?,?)",
            (
                target_type, target, str(content)[:500],
                datetime.now().isoformat(timespec="seconds"),
                str(scheduled_at or "")[:30],
            ),
        )
        _maybe_commit()


def notif_pending(limit=20):
    with _lock:
        rows = _connect().execute(
            "SELECT id,target_type,target,content FROM notifications "
            "WHERE sent_at IS NULL AND failed=0 "
            "AND (scheduled_at IS NULL OR scheduled_at='' OR scheduled_at<=?) "
            "ORDER BY id LIMIT ?",
            (datetime.now().isoformat(timespec="seconds"), limit),
        ).fetchall()
        return [dict(r) for r in rows]


def notif_mark_sent(nid):
    with _lock:
        c = _connect()
        c.execute(
            "UPDATE notifications SET sent_at=? WHERE id=?",
            (datetime.now().isoformat(timespec="seconds"), nid),
        )
        _maybe_commit()


def notif_mark_failed(nid, max_retries=3):
    """发送失败计数；达到上限后标记 failed（不再重试，避免堵队列）。"""
    with _lock:
        c = _connect()
        c.execute("UPDATE notifications SET retries=retries+1 WHERE id=?", (nid,))
        c.execute(
            "UPDATE notifications SET failed=1 WHERE id=? AND retries>=?",
            (nid, max_retries),
        )
        _maybe_commit()


def notif_failed_retries(nid):
    with _lock:
        row = _connect().execute(
            "SELECT retries FROM notifications WHERE id=?", (nid,)
        ).fetchone()
        return row[0] if row else 0


# ===== 统一记忆库（QQ bot 与 Hermes 共用，向量化就绪）=====
def vec_dumps(vec):
    return json.dumps(vec, ensure_ascii=False) if vec else None


def vec_loads(raw):
    try:
        return json.loads(raw) if raw else None
    except Exception as e:
        _stats_err(e)
        return None


def memory_replace(
    scope,
    key,
    facts,
    updated_at="",
    embeddings=None,
    confidences=None,
    sources=None,
    audience="",
    speaker="",
    mclass="short",
    arousal=0.0,
    valence=0.0,
    privacy=0.0,
    audiences=None,
    speakers=None,
    mclasses=None,
    arousals=None,
    valences=None,
    privacies=None,
    valid_from="",
    valid_to="",
    status="active",
):
    with _lock:
        c = _connect()
        existing_rows = {r["fact"]: r for r in memory_rows(scope, key)}
        c.execute("DELETE FROM memories WHERE scope=? AND key=?", (scope, key))
        emb = embeddings or {}
        conf = confidences or {}
        srcs = sources or {}
        aud_map = audiences or {}
        spk_map = speakers or {}
        cls_map = mclasses or {}
        ar_map = arousals or {}
        va_map = valences or {}
        pr_map = privacies or {}
        st_map = {
            f: existing_rows[f].get("status", "active")
            for f in existing_rows
        }
        vf_map = {f: existing_rows[f].get("valid_from", "") for f in existing_rows}
        vt_map = {f: existing_rows[f].get("valid_to", "") for f in existing_rows}
        for fact in facts:
            c.execute(
                "INSERT OR IGNORE INTO memories("
                "scope,key,fact,embedding,updated_at,confidence,source,audience,speaker,mclass,arousal,valence,privacy,"
                "valid_from,valid_to,status) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    scope,
                    key,
                    str(fact),
                    vec_dumps(emb.get(str(fact))),
                    updated_at,
                    float(conf.get(str(fact), 0.7)),
                    str(srcs.get(str(fact), "")),
                    str(aud_map.get(str(fact), audience)),
                    str(spk_map.get(str(fact), speaker)),
                    str(cls_map.get(str(fact), mclass)),
                    float(ar_map.get(str(fact), arousal)),
                    float(va_map.get(str(fact), valence)),
                    float(pr_map.get(str(fact), privacy)),
                    str(vf_map.get(str(fact), valid_from or updated_at)),
                    str(vt_map.get(str(fact), valid_to or "")),
                    str(st_map.get(str(fact), status)) or "active",
                ),
            )
        _maybe_commit()


def memory_replace_preserve(scope, key, facts, updated_at=""):
    """整段重写时保留每条记忆原有字段（向量/可信度/来源/audience/mclass/情绪），
    只更新事实列表与时间戳。用于群/成员/用户记忆的节流重写。"""
    existing = {r["fact"]: r for r in memory_rows(scope, key)}
    with _lock:
        c = _connect()
        c.execute("DELETE FROM memories WHERE scope=? AND key=?", (scope, key))
        for f in facts:
            r = existing.get(str(f), {})
            c.execute(
                "INSERT OR IGNORE INTO memories("
                "scope,key,fact,embedding,updated_at,confidence,source,audience,speaker,mclass,arousal,valence,privacy) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    scope,
                    key,
                    str(f),
                    r.get("embedding"),
                    updated_at,
                    float(r.get("confidence", 0.7)),
                    r.get("source", ""),
                    r.get("audience", ""),
                    r.get("speaker", ""),
                    r.get("mclass") or "short",
                    float(r.get("arousal", 0.0)),
                    float(r.get("valence", 0.0)),
                    float(r.get("privacy", 0.0)),
                ),
            )
        _maybe_commit()


def memory_update_embedding(scope, key, fact, embedding):
    """只更新单条记忆的向量（回填用，不碰其他字段）。"""
    with _lock:
        c = _connect()
        c.execute(
            "UPDATE memories SET embedding=? WHERE scope=? AND key=? AND fact=?",
            (vec_dumps(embedding), scope, key or "", str(fact)),
        )
        _maybe_commit()


def memory_add(
    scope,
    key,
    fact,
    updated_at="",
    embedding=None,
    confidence=0.7,
    source="",
    audience="",
    speaker="",
    mclass="short",
    arousal=0.0,
    valence=0.0,
    privacy=0.0,
    valid_from="",
    valid_to="",
    status="active",
):
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO memories("
            "scope,key,fact,embedding,updated_at,confidence,source,audience,speaker,mclass,arousal,valence,privacy,"
            "valid_from,valid_to,status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(scope,key,fact) DO UPDATE SET "
            "embedding=COALESCE(excluded.embedding, memories.embedding), "
            "updated_at=excluded.updated_at, confidence=excluded.confidence, source=excluded.source, "
            "audience=excluded.audience, speaker=excluded.speaker, mclass=excluded.mclass, "
            "arousal=excluded.arousal, valence=excluded.valence, privacy=excluded.privacy, "
            "valid_from=excluded.valid_from, valid_to=excluded.valid_to, status=excluded.status",
            (
                scope,
                key,
                str(fact),
                vec_dumps(embedding),
                updated_at,
                float(confidence),
                str(source),
                str(audience),
                str(speaker),
                str(mclass),
                float(arousal),
                float(valence),
                float(privacy),
                str(valid_from or updated_at),
                str(valid_to or ""),
                str(status) or "active",
            ),
        )
        _maybe_commit()


def memory_get(scope, key=""):
    with _lock:
        rows = _connect().execute(
            "SELECT fact FROM memories WHERE scope=? AND key=? ORDER BY updated_at, rowid",
            (scope, key),
        ).fetchall()
        return [r[0] for r in rows]


def memory_updated_at(scope, key=""):
    with _lock:
        row = _connect().execute(
            "SELECT MAX(updated_at) FROM memories WHERE scope=? AND key=?", (scope, key)
        ).fetchone()
        return row[0] or ""


def memory_clear(scope, key=None):
    with _lock:
        c = _connect()
        if key is None:
            c.execute("DELETE FROM memories WHERE scope=?", (scope,))
        else:
            c.execute("DELETE FROM memories WHERE scope=? AND key=?", (scope, key))
        _maybe_commit()


def memory_search(q, scope=None, key=None, limit=10):
    with _lock:
        q = str(q or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        sql = "SELECT scope,key,fact,embedding,updated_at,confidence FROM memories WHERE fact LIKE ? ESCAPE '\\'"
        params = [f"%{q}%"]
        if scope:
            sql += " AND scope=?"
            params.append(scope)
        if key is not None:
            sql += " AND key=?"
            params.append(key)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        return [dict(r) for r in _connect().execute(sql, params).fetchall()]


def memory_rows(scope=None, key=None, exclude_status=None, limit=None):
    """带 embedding 的行（供向量检索）；exclude_status/limit 可下推到 SQL 减扫描。"""
    with _lock:
        sql = (
            "SELECT scope,key,fact,embedding,updated_at,confidence,source,audience,speaker,mclass,"
            "arousal,valence,privacy,valid_from,valid_to,status FROM memories"
        )
        params = []
        if scope:
            sql += " WHERE scope=?"
            params.append(scope)
        if key is not None:
            sql += " AND key=?" if "WHERE" in sql else " WHERE key=?"
            params.append(key)
        if exclude_status:
            if "WHERE" not in sql:
                sql += " WHERE 1=1"
            sql += " AND status NOT IN (" + ",".join("?" * len(exclude_status)) + ")"
            params.extend(exclude_status)
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(1, int(limit)))
        return [dict(r) for r in _connect().execute(sql, params).fetchall()]


def memory_rows_by_facts(scope, facts, exclude_status=None):
    """按 fact 集合下推查询（检索候选行，避免全表拉取后 Python 过滤）。
    facts 为空返回 []；自动分块防 SQLite 参数上限（默认 999/32766）。"""
    facts = list(dict.fromkeys(str(f) for f in (facts or [])))
    if not facts:
        return []
    with _lock:
        rows = []
        for i in range(0, len(facts), 500):
            chunk = facts[i:i + 500]
            sql = (
                "SELECT scope,key,fact,embedding,updated_at,confidence,source,audience,speaker,mclass,"
                "arousal,valence,privacy,valid_from,valid_to,status FROM memories WHERE scope=?"
            )
            params = [scope]
            if exclude_status:
                sql += " AND status NOT IN (" + ",".join("?" * len(exclude_status)) + ")"
                params.extend(exclude_status)
            sql += " AND fact IN (" + ",".join("?" * len(chunk)) + ")"
            params.extend(chunk)
            rows.extend(dict(r) for r in _connect().execute(sql, params).fetchall())
        return rows


def memory_set_status(scope, key, fact, status, valid_to=""):
    """时间推理（v5）：把记忆标记为 superseded/history 等状态。"""
    with _lock:
        c = _connect()
        c.execute(
            "UPDATE memories SET status=?, valid_to=? WHERE scope=? AND key=? AND fact=?",
            (str(status)[:20], str(valid_to or ""), scope, key or "", str(fact)),
        )
        _maybe_commit()


def memory_set_confidence(scope, key, fact, confidence):
    """调整单条记忆的可信度（确认上调 / 反驳下调）。"""
    with _lock:
        c = _connect()
        c.execute(
            "UPDATE memories SET confidence=? WHERE scope=? AND key=? AND fact=?",
            (
                min(1.0, max(0.0, float(confidence))),
                scope,
                key or "",
                str(fact),
            ),
        )
        _maybe_commit()


def memory_source_normalize() -> dict:
    """证据门控（v2.3）：历史 source 归一——ingest:* → user（用户亲口说），persona → pack（人设设定）。
    幂等，可随 memory-grow 自动跑。"""
    with _lock:
        c = _connect()
        cur = c.execute(
            "UPDATE memories SET source='user' WHERE source LIKE 'ingest:%' OR source='ingest'"
        )
        n1 = cur.rowcount
        cur = c.execute("UPDATE memories SET source='pack' WHERE source='persona'")
        n2 = cur.rowcount
        _maybe_commit()
        return {"ingest_to_user": n1, "persona_to_pack": n2}


def memory_delete(scope, key, fact):
    """单条记忆删除（回收策略用）。"""
    with _lock:
        c = _connect()
        c.execute(
            "DELETE FROM memories WHERE scope=? AND key=? AND fact=?",
            (scope, key or "", str(fact)),
        )
        _maybe_commit()


def memory_set_source(scope, key, fact, source):
    """污染扫描降级用：改写单条记忆的来源（user → ai_edit），证明链不再把它当"用户亲口说"。"""
    with _lock:
        c = _connect()
        c.execute(
            "UPDATE memories SET source=? WHERE scope=? AND key=? AND fact=?",
            (str(source)[:20], scope, key or "", str(fact)),
        )
        _maybe_commit()


# ===== 事件图（events + event_relations 邻接表）=====
def event_add(
    scope,
    key,
    etype,
    title,
    content="",
    importance=0.5,
    ts="",
    ts_source="approx",
    embedding=None,
    updated_at="",
    memory_scope="",
    memory_key="",
    memory_fact="",
):
    """写入/更新一个事件（按 scope+key+title 去重，保留 id 以不破坏关系图）。返回 event id。"""
    title = str(title)[:200]
    key = key or ""
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO events(scope,key,etype,title,content,importance,ts,ts_source,embedding,updated_at,"
            "memory_scope,memory_key,memory_fact) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(scope,key,title) DO UPDATE SET "
            "etype=excluded.etype, content=excluded.content, importance=excluded.importance, "
            "ts=excluded.ts, ts_source=excluded.ts_source, embedding=excluded.embedding, "
            "updated_at=excluded.updated_at, "
            "memory_scope=excluded.memory_scope, memory_key=excluded.memory_key, memory_fact=excluded.memory_fact",
            (
                scope,
                key,
                str(etype)[:50],
                title,
                str(content)[:1000],
                float(importance),
                str(ts),
                str(ts_source)[:20] or "approx",
                vec_dumps(embedding),
                updated_at or datetime.now().isoformat(timespec="seconds"),
                str(memory_scope),
                str(memory_key),
                str(memory_fact)[:200],
            ),
        )
        _maybe_commit()
        row = c.execute(
            "SELECT id FROM events WHERE scope=? AND key=? AND title=?",
            (scope, key, title),
        ).fetchone()
        return row[0] if row else None


def event_set_ts(event_id, ts, ts_source="explicit"):
    """纠正/精化事件时间（保持事件图不变）。"""
    with _lock:
        c = _connect()
        c.execute(
            "UPDATE events SET ts=?, ts_source=?, updated_at=? WHERE id=?",
            (str(ts), str(ts_source)[:20] or "approx",
             datetime.now().isoformat(timespec="seconds"), int(event_id)),
        )
        _maybe_commit()


def event_set_ts_by_title(scope, key, title, ts, ts_source="explicit"):
    """按 scope+key+title 更新事件时间（纠错联动用）。"""
    with _lock:
        c = _connect()
        c.execute(
            "UPDATE events SET ts=?, ts_source=?, updated_at=? WHERE scope=? AND key=? AND title=?",
            (str(ts), str(ts_source)[:20] or "approx",
             datetime.now().isoformat(timespec="seconds"), str(scope), str(key or ""), str(title)),
        )
        _maybe_commit()


def event_rows(scope=None, key=None, since=None, min_importance=None, limit=None):
    with _lock:
        sql = "SELECT * FROM events"
        params = []
        conds = []
        if scope:
            conds.append("scope=?")
            params.append(scope)
        if key is not None:
            conds.append("key=?")
            params.append(key)
        if since:
            conds.append("ts>=?")
            params.append(since)
        if min_importance is not None:
            conds.append("importance>=?")
            params.append(min_importance)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY importance DESC, id DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [dict(r) for r in _connect().execute(sql, params).fetchall()]


def event_delete(event_id):
    """删除事件及其关联边（当前 PRAGMA foreign_keys 未开启，手动删边）。"""
    with _lock:
        c = _connect()
        c.execute("DELETE FROM event_relations WHERE src=? OR dst=?", (event_id, event_id))
        c.execute("DELETE FROM events WHERE id=?", (event_id,))
        _maybe_commit()


def relation_add(src, dst, rel="influences", weight=1.0):
    if not src or not dst or src == dst:
        return
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO event_relations(src,dst,rel,weight,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(src,dst,rel) DO UPDATE SET weight=excluded.weight, updated_at=excluded.updated_at",
            (int(src), int(dst), str(rel)[:50], float(weight), datetime.now().isoformat(timespec="seconds")),
        )
        _maybe_commit()


def relations_for(event_ids, direction="both"):
    """查询事件的关联边。direction: out / in / both。"""
    if not event_ids:
        return []
    marks = ",".join("?" * len(event_ids))
    params = list(event_ids)
    if direction == "out":
        sql = f"SELECT * FROM event_relations WHERE src IN ({marks})"
    elif direction == "in":
        sql = f"SELECT * FROM event_relations WHERE dst IN ({marks})"
    else:
        sql = f"SELECT * FROM event_relations WHERE src IN ({marks}) OR dst IN ({marks})"
        params = params + params
    with _lock:
        return [dict(r) for r in _connect().execute(sql, params).fetchall()]


def event_id_by_title(scope, key, title):
    with _lock:
        row = _connect().execute(
            "SELECT id FROM events WHERE scope=? AND key=? AND title=?",
            (scope, key or "", str(title)[:200]),
        ).fetchone()
        return row[0] if row else None


# ===== AI 自身记忆（identity / experience / belief）=====
def ai_memory_set(kind, content, importance=0.5, embedding=None, updated_at=""):
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO ai_memory(kind,content,importance,embedding,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(kind,content) DO UPDATE SET "
            "importance=excluded.importance, embedding=excluded.embedding, updated_at=excluded.updated_at",
            (kind, str(content)[:1000], float(importance), vec_dumps(embedding), updated_at or datetime.now().isoformat(timespec="seconds")),
        )
        _maybe_commit()


def ai_memory_rows(kind=None, limit=None):
    with _lock:
        sql = "SELECT kind,content,importance,embedding,updated_at FROM ai_memory"
        params = []
        if kind:
            sql += " WHERE kind=?"
            params.append(kind)
        sql += " ORDER BY importance DESC, updated_at DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [dict(r) for r in _connect().execute(sql, params).fetchall()]


def ai_memory_clear(kind=None):
    with _lock:
        c = _connect()
        if kind:
            c.execute("DELETE FROM ai_memory WHERE kind=?", (kind,))
        else:
            c.execute("DELETE FROM ai_memory")
        _maybe_commit()


# ===== 记忆元数据（Memory Policy 的原始数据）=====
def meta_touch(scope, key, fact, importance=0.5, ts=""):
    """被提取/注入时更新访问计数和最后访问时间；importance 只增不减（简单隐式反馈）。"""
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO memory_meta(scope,key,fact,access_count,last_access,importance) VALUES(?,?,?,1,?,?) "
            "ON CONFLICT(scope,key,fact) DO UPDATE SET "
            "access_count=access_count+1, last_access=excluded.last_access, "
            "importance=MAX(importance,excluded.importance)",
            (scope, key or "", str(fact), ts or datetime.now().isoformat(timespec="seconds"), float(importance)),
        )
        _maybe_commit()


def meta_touch_many(items):
    """批量隐式反馈（一次事务）：items = [(scope, key, fact, importance, ts)]。
    语义等价逐条 meta_touch，但只 commit 一次（修复检索命中多条时的提交放大）。"""
    if not items:
        return
    now = datetime.now().isoformat(timespec="seconds")
    with _lock:
        c = _connect()
        c.executemany(
            "INSERT INTO memory_meta(scope,key,fact,access_count,last_access,importance) VALUES(?,?,?,1,?,?) "
            "ON CONFLICT(scope,key,fact) DO UPDATE SET "
            "access_count=access_count+1, last_access=excluded.last_access, "
            "importance=MAX(importance,excluded.importance)",
            [(s, k or "", str(f), ts or now, float(imp)) for s, k, f, imp, ts in items],
        )
        _maybe_commit()


def meta_rows(scope=None, key=None, min_importance=None, limit=None):
    with _lock:
        sql = "SELECT scope,key,fact,access_count,last_access,importance FROM memory_meta"
        params = []
        conds = []
        if scope:
            conds.append("scope=?")
            params.append(scope)
        if key is not None:
            conds.append("key=?")
            params.append(key)
        if min_importance is not None:
            conds.append("importance>=?")
            params.append(min_importance)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY importance DESC, access_count DESC, last_access DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [dict(r) for r in _connect().execute(sql, params).fetchall()]


def meta_delete(scope, key, fact):
    with _lock:
        c = _connect()
        c.execute(
            "DELETE FROM memory_meta WHERE scope=? AND key=? AND fact=?",
            (scope, key or "", str(fact)),
        )
        _maybe_commit()


# ===== 议题（大类 → 议题 → 参数）=====
def topic_add(scope, key, category, topic, importance=0.5, confidence=0.7, status="active",
              started_at="", updated_at=""):
    with _lock:
        c = _connect()
        now = updated_at or datetime.now().isoformat(timespec="seconds")
        c.execute(
            "INSERT INTO topics(scope,key,category,topic,importance,confidence,status,started_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(scope,key,category,topic) DO UPDATE SET "
            "importance=excluded.importance, confidence=excluded.confidence, updated_at=excluded.updated_at",
            (scope, key or "", str(category)[:50], str(topic)[:100], float(importance),
             float(confidence), str(status)[:20], started_at or now, now),
        )
        _maybe_commit()
        row = c.execute(
            "SELECT id FROM topics WHERE scope=? AND key=? AND category=? AND topic=?",
            (scope, key or "", str(category)[:50], str(topic)[:100]),
        ).fetchone()
        return row[0] if row else None


def topic_find(scope, key, category, topic):
    with _lock:
        row = _connect().execute(
            "SELECT id FROM topics WHERE scope=? AND key=? AND category=? AND topic=?",
            (scope, key or "", str(category)[:50], str(topic)[:100]),
        ).fetchone()
        return row[0] if row else None


def topic_get(topic_id):
    with _lock:
        row = _connect().execute(
            "SELECT * FROM topics WHERE id=?", (int(topic_id),)
        ).fetchone()
        return dict(row) if row else None


def topic_rows(scope=None, key=None, category=None, limit=None):
    with _lock:
        sql = "SELECT * FROM topics"
        params = []
        conds = []
        if scope:
            conds.append("scope=?")
            params.append(scope)
        if key is not None:
            conds.append("key=?")
            params.append(key)
        if category:
            conds.append("category=?")
            params.append(category)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY importance DESC, updated_at DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [dict(r) for r in _connect().execute(sql, params).fetchall()]


def topics_count(scope=None) -> int:
    with _lock:
        if scope:
            return _connect().execute(
                "SELECT COUNT(*) FROM topics WHERE scope=?", (scope,)
            ).fetchone()[0]
        return _connect().execute("SELECT COUNT(*) FROM topics").fetchone()[0]


def topic_clear(scope):
    """清空某 scope 的议题及其参数/关联（人物档案重建等场景，避免陈旧事实经议题通道召回）。"""
    with _lock:
        c = _connect()
        ids = [r[0] for r in c.execute(
            "SELECT id FROM topics WHERE scope=?", (scope,)
        ).fetchall()]
        if ids:
            marks = ",".join("?" * len(ids))
            c.execute(f"DELETE FROM topic_params WHERE topic_id IN ({marks})", ids)
            c.execute(f"DELETE FROM topics WHERE id IN ({marks})", ids)
        _maybe_commit()


def topic_param_add(topic_id, param, value, confidence=0.7, updated_at=""):
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO topic_params(topic_id,param,value,confidence,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(topic_id,param,value) DO UPDATE SET "
            "confidence=excluded.confidence, updated_at=excluded.updated_at",
            (int(topic_id), str(param)[:30], str(value)[:500], float(confidence),
             updated_at or datetime.now().isoformat(timespec="seconds")),
        )
        _maybe_commit()


def topic_params(topic_id) -> list:
    with _lock:
        rows = _connect().execute(
            "SELECT param,value,confidence,updated_at FROM topic_params WHERE topic_id=?",
            (int(topic_id),),
        ).fetchall()
        return [dict(r) for r in rows]


def event_set_topic(event_id, topic_id):
    with _lock:
        c = _connect()
        c.execute("UPDATE events SET topic_id=? WHERE id=?", (int(topic_id), int(event_id)))
        _maybe_commit()


def topic_param_invalidate(value):
    """纠错联动：含该事实的议题参数降权（标记 stale，供重算）。"""
    with _lock:
        c = _connect()
        c.execute("UPDATE topic_params SET confidence=MIN(confidence, 0.3) WHERE value=?", (str(value),))
        _maybe_commit()

