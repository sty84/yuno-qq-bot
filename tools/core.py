"""Shared helpers for YUNO CLI/admin tools (split from tools.py)."""

import json
import os
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _plugins():
    """惰性导入：mcp 子命令无 SDK 时不会因 plugins 依赖而崩溃。"""
    from plugins import _capability, _db, _shared
    return _capability, _db, _shared


def _notify_group():
    _capability, _db, _shared = _plugins()
    return str(_shared.CONFIG.get("random_events", {}).get("group_openid", "") or "")


def _sync_notify(results, notify: bool) -> list[str]:
    """状态变化时播报：在线→离线告警，离线→在线恢复通知（去重）。"""
    _capability, _db, _shared = _plugins()
    target = _notify_group() if notify else ""
    messages = []
    for kw, online, _detail in results:
        flag = f"{kw}:notified"
        prev = _db.kv_get("health", flag, False)
        if not online and not prev:
            _db.kv_set("health", flag, True)
            msg = f"【告警】服务 {kw} 异常"
            messages.append(msg)
            if target:
                _capability.notify_send("group", target, msg)
        elif online and prev:
            _db.kv_set("health", flag, False)
            msg = f"【恢复】服务 {kw} 已恢复正常"
            messages.append(msg)
            if target:
                _capability.notify_send("group", target, msg)
    return messages


def ablation_switches(core) -> dict:
    """消融开关定义：name → (label, apply_fn)。apply_fn 作用于 memory.core。
    每个开关独立作用于干净基线（run_ablation 逐个恢复），不再是累积式。"""
    return {
        "off_vector": ("关向量检索", lambda: core.setdefault("weights", {}).update({"vector": 0})),
        "off_graph": ("关图谱", lambda: core.setdefault("weights", {}).update({"graph": 0})),
        "off_lexical": ("关词法", lambda: core.setdefault("weights", {}).update({"lexical": 0})),
        "off_time_window": ("关时间窗口", lambda: core.update({"ablation_disable_time": True})),
        "off_emotion": ("关情绪", lambda: core.update({"emotion": {"enabled": False}})),
        "off_sharing": ("关分享", lambda: core.update({"sharing": {"enabled": False}})),
        "off_space": ("关空间", lambda: core.update({"space": {"enabled": False}})),
        "off_system1": ("关 System1", lambda: core.setdefault("mind", {}).update({"system1": False})),
        "on_cognitive": ("开认知循环", lambda: core.setdefault("mind", {}).update({"cognitive_turn": True})),
        "off_mood_boost": ("关心境一致加权", lambda: core.update({"mood_boost": 0.0})),
        "off_emotion_address": ("关情绪寻址复核", lambda: core.update({"emotion_address": False})),
        "off_bandit": ("关回应策略 bandit", lambda: core.setdefault("bandit", {}).update({"enabled": False})),
        "off_revive": ("关泊松主动触发", lambda: core.setdefault("revive", {}).update({"rate_per_day": 0})),
        "off_hesitation": ("关犹豫层", lambda: core.setdefault("hesitation", {}).update({"enabled": False})),
        "off_evidence_semantic": ("关语义自检", lambda: core.setdefault("evidence_gate", {}).update({"semantic": False})),
    }


def ablation_state() -> dict:
    """当前开关状态（热插拔面板数据源）。"""
    from plugins import _shared
    core = (_shared.CONFIG.get("memory", {}).get("core", {}) or {})
    w = core.get("weights") or {}
    m = core.get("mind") or {}
    return {
        "vector": float(w.get("vector", 0.7)) > 0,
        "graph": float(w.get("graph", 0.4)) > 0,
        "lexical": float(w.get("lexical", 0.6)) > 0,
        "time_window": not bool(core.get("ablation_disable_time", False)),
        "emotion": bool((core.get("emotion") or {}).get("enabled", True)),
        "sharing": bool((core.get("sharing") or {}).get("enabled", True)),
        "space": bool((core.get("space") or {}).get("enabled", True)),
        "system1": bool(m.get("system1", True)),
        "cognitive_turn": bool(m.get("cognitive_turn", False)),
        "mood_boost": float(core.get("mood_boost", 0.12)) > 0,
        "emotion_address": bool(core.get("emotion_address", True)),
        "bandit": bool((core.get("bandit") or {}).get("enabled", True)),
        "revive": float((core.get("revive") or {}).get("rate_per_day", 2.0)) > 0,
        "hesitation": bool((core.get("hesitation") or {}).get("enabled", True)),
        "evidence_semantic": bool((core.get("evidence_gate") or {}).get("semantic", True)),
    }


def apply_switch(name, value) -> dict:
    """热插拔：改 config（内存 + 落盘），bot 进程由 reload_if_changed 生效。"""
    from plugins import _shared
    core = _shared.CONFIG.setdefault("memory", {}).setdefault("core", {})
    v = bool(value)
    setters = {
        "vector": lambda: core.setdefault("weights", {}).update({"vector": 0.7 if v else 0}),
        "graph": lambda: core.setdefault("weights", {}).update({"graph": 0.4 if v else 0}),
        "lexical": lambda: core.setdefault("weights", {}).update({"lexical": 0.6 if v else 0}),
        "time_window": lambda: core.update({"ablation_disable_time": not v}),
        "emotion": lambda: core.update({"emotion": {"enabled": v}}),
        "sharing": lambda: core.update({"sharing": {"enabled": v}}),
        "space": lambda: core.update({"space": {"enabled": v}}),
        "system1": lambda: core.setdefault("mind", {}).update({"system1": v}),
        "cognitive_turn": lambda: core.setdefault("mind", {}).update({"cognitive_turn": v}),
        "mood_boost": lambda: core.update({"mood_boost": 0.12 if v else 0.0}),
        "emotion_address": lambda: core.update({"emotion_address": v}),
        "bandit": lambda: core.setdefault("bandit", {}).update({"enabled": v}),
        "revive": lambda: core.setdefault("revive", {}).update({"rate_per_day": 2.0 if v else 0}),
        "hesitation": lambda: core.setdefault("hesitation", {}).update({"enabled": v}),
        "evidence_semantic": lambda: core.setdefault("evidence_gate", {}).update({"semantic": v}),
    }
    if name not in setters:
        return {"error": f"未知开关：{name}"}
    setters[name]()
    try:
        _shared.save_config()
    except Exception as e:
        return {"error": f"保存 config 失败：{e}"}
    return {"switch": name, "value": v, "state": ablation_state()}


def run_ablation(probes, names=None) -> dict:
    """机制消融：每个开关独立作用于干净基线（逐个恢复），清空检索缓存防串数据。
    返回 {"baseline", "matrix"}；与 webapp 共用，写实验日志。"""
    import copy
    import memory
    from plugins import _db, _shared
    core = _shared.CONFIG.setdefault("memory", {}).setdefault("core", {})
    base = copy.deepcopy(core)
    switches = ablation_switches(core)
    names = [n for n in (names or list(switches.keys())) if n in switches]

    def _run():
        try:
            # 消融隔离：每轮从同一状态出发——
            # 检索/时间缓存 + 路由自适应（route_stats 在 _route_cache 里跨轮累积，
            # 会让"基线跑（无历史）"和"所有开关跑（有历史）"系统性不同，出现
            # 不相关开关 delta 完全一致的假象）+ 查询向量缓存
            from memory import reasoning as reasoning_mod
            reasoning_mod._result_cache.update({"ts": 0.0, "key": None, "hits": None})
            reasoning_mod._event_time_cache.update({"ts": 0.0, "key": None, "map": {}})
            reasoning_mod._route_cache = {}
            reasoning_mod._route_flush_ts = {"ts": 1e18}  # 消融期间不把假统计写回 kv
            reasoning_mod._query_cache = {"ts": 0.0, "text": "", "vec": None}
            res = memory.run_eval(probes, k=5)
            return {"recall": res.get("recall_at_k"), "mrr": res.get("mrr"), "ndcg": res.get("ndcg")}
        except Exception as e:
            return {"error": str(e)}

    base_res = _run()
    rows = [{"switch": "all_on", "label": "全部开启", **base_res}]
    for name in names:
        label, apply = switches[name]
        core.clear()
        core.update(copy.deepcopy(base))
        apply()
        r = _run()
        delta = {
            k: round((r.get(k) or 0) - (base_res.get(k) or 0), 3)
            for k in ("recall", "mrr", "ndcg")
            if r.get(k) is not None and base_res.get(k) is not None
        }
        regression = any(v < -0.03 for v in delta.values())
        _db.exp_log_add("ablation", detail=name, before=base_res, after=r, delta=delta, regression=regression)
        rows.append({"switch": name, "label": label, **r, "delta": delta, "regression": regression})
    core.clear()
    core.update(base)
    try:
        # 恢复：消融结束后路由统计回 kv（消融期间禁用了 flush），下次真实调用重新加载
        from memory import reasoning as reasoning_mod
        reasoning_mod._route_cache = None
        reasoning_mod._route_flush_ts = {"ts": 0.0}
    except Exception:
        pass
    return {"baseline": base_res, "matrix": rows}


SCENARIO_RUBRIC = (
    "你是对话质量评委。按 5 个维度各打 1~5 分，只输出 JSON："
    '{"recall":n,"precision":n,"coherence":n,"consistency":n,"naturalness":n,"avg":n,"comment":"…"}。'
    "维度：recall=是否覆盖用户需求/记忆；precision=是否答非所问/编造；coherence=多轮是否连贯；"
    "consistency=是否前后矛盾；naturalness=是否自然像真人。"
)


def scenario_replay(path: str = "", score: bool = False, scenario_id=None, review_export: bool = False) -> dict:
    """场景回放（可只回放单个场景），score=True 时附 DeepSeek 五维评分。
    场景集：data/eval/scenarios.json = [{"id","scope","messages":[{"user":...}],"expected":[...]}]。
    返回 {"replayed","scenarios"}；score=True 时附加 {"scored","avg"}。
    review_export=True 时把回放对话写入 conv_log（scope=c2c:scenario:<id>），
    供"对话质量评分"（v33 convreview）队列人工评分。供 CLI 与 webapp 共用。
    """
    from plugins import _shared
    p = path or str(_shared.DATA_DIR / "eval" / "scenarios.json")
    if not os.path.exists(p):
        # 无场景集时用内置示例播种（同 emotion_probes 的模式）
        seed = ROOT / "memory" / "scenarios.example.json"
        if seed.exists():
            try:
                pathlib.Path(p).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(seed, p)
            except Exception:
                pass
    if not os.path.exists(p):
        return {"error": f"场景集不存在：{p}（可建 data/eval/scenarios.json）"}
    with open(p, encoding="utf-8") as f:
        scenarios = json.load(f)
    if scenario_id:
        scenarios = [s for s in (scenarios or []) if str(s.get("id")) == str(scenario_id)]
    import agent
    results = []
    for sc in scenarios or []:
        scope = str(sc.get("scope") or "c2c:scenario")
        history: list[dict] = []
        replies: list[dict] = []
        for m in (sc.get("messages") or []):
            if "user" not in m:
                continue
            try:
                reply, _meta = agent.ask(
                    str(m["user"]), history=history[-6:], scopes=[scope], learn=False,
                )
            except Exception as e:
                reply = f"（回放失败：{e}）"
            history.append({"role": "user", "content": str(m["user"])})
            history.append({"role": "assistant", "content": reply})
            replies.append({"user": str(m["user"]), "ai": reply})
        results.append({"id": sc.get("id"), "scope": scope, "replies": replies})
    exported = 0
    if review_export:
        try:
            import memory.convreview as _cr
            for r in results:
                for x in r["replies"]:
                    _cr.record(
                        scope=f"c2c:scenario:{r['id']}",
                        text=x["user"],
                        reply=x["ai"],
                        conversation_id=f"scenario:{r['id']}",
                    )
                    exported += 1
        except Exception:
            exported = 0
    out = {"replayed": len(results), "scenarios": results}
    if review_export:
        out["review_exported"] = exported
    if not score:
        return out
    scored = []
    for r in results:
        conv = "\n".join(f"用户：{x['user']}\nAI：{x['ai']}" for x in r["replies"])
        s2 = {}
        try:
            raw = _shared.ask_deepseek(
                SCENARIO_RUBRIC + "\n对话：\n" + conv,
                max_tokens=200, temperature=0.2, module="scenario",
            )
            start, end = raw.find("{"), raw.rfind("}")
            if start >= 0:
                s2 = json.loads(raw[start:end + 1])
                s2["avg"] = round(
                    sum(float(s2.get(k, 0)) for k in ("recall", "precision", "coherence", "consistency", "naturalness")) / 5,
                    2,
                )
        except Exception as ex:
            s2 = {"error": str(ex)}
        scored.append({"id": r["id"], "scores": s2})
    avg: dict[str, float] = {}
    vals: dict[str, list[float]] = {k: [] for k in ("recall", "precision", "coherence", "consistency", "naturalness")}
    for r in scored:
        s = r.get("scores") or {}
        for k in vals:
            if s.get(k) is not None:
                vals[k].append(float(s[k]))
    for k, v in vals.items():
        if v:
            avg[k] = round(sum(v) / len(v), 2)
    out["scored"] = scored
    out["avg"] = avg
    return out


def _emit(result) -> int:
    """统一输出：cmd 返回 str 或 (code, text)；非零 code 供 cron/脚本门控。"""
    if isinstance(result, tuple):
        code, text = result
    else:
        code, text = 0, result
    print(text)
    return int(code)


def _rubric_judge(query, reply, expected, category) -> dict:
    """LLM rubric 自动判分（v2.3 P1-1）：四维 0-2 分（准确性/合理性/人设/防编造）+ 总评。
    返回 {"scores": {…}, "total": 0-8, "comment": 一句话}。"""
    from plugins import _shared
    prompt = (
        "你是回复质量评审。评分（每维 0-2 分，整数）：\n"
        "准确：内容与事实/记忆相符，不编造；合理：逻辑与语境自洽；\n"
        "人设：符合千石由乃人设（慵懒、音乐人、乐队成员）；防编造：不虚构没说过的事。\n"
        f"题目类别：{category}\n用户问题：{query}\n预期要点：{expected}\n"
        f"实际回复：{reply}\n"
        "输出 JSON：{\"accuracy\":0-2,\"reasonableness\":0-2,\"persona\":0-2,\"no_fabrication\":0-2,\"comment\":\"一句话\"}"
    )
    try:
        import json as _json
        out = _shared.ask_deepseek(prompt, module="reply_judge", max_tokens=300)
        data = _json.loads(out[out.index("{"):out.rindex("}") + 1])
        scores = {k: max(0, min(2, int(data.get(k, 0)))) for k in
                  ("accuracy", "reasonableness", "persona", "no_fabrication")}
        return {"scores": scores, "total": sum(scores.values()),
                "comment": str(data.get("comment", ""))[:80]}
    except Exception as e:
        return {"scores": {}, "total": -1, "comment": f"判分失败:{e}"}
