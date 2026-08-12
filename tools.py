"""YUNO 2.0 运维/入口工具（原 5 个独立脚本合并）。

用法：
  python tools.py health [--notify]       # 独立健康检查（cron 用）
  python tools.py backup                    # 每日 SQLite 备份（保留 7 份）
  python tools.py recover [--notify]        # 一键恢复 services 注册表中未运行的服务
  python tools.py character 千石由乃          # 生成人物档案入记忆 + docs/characters/<名>.md
  python tools.py character-sync 千石由乃    # 把编辑后的 md 档案同步回记忆库（或传文件路径）
  python tools.py mcp                       # 启动 MCP Server（需 mcp SDK）
"""

import argparse
import json
import os
import pathlib
import shutil
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = pathlib.Path(__file__).resolve().parent
from plugins import _shared


def _plugins():
    """惰性导入：mcp 子命令无 SDK 时不会因 plugins 依赖而崩溃。"""
    from plugins import _capability, _db, _shared
    return _capability, _db, _shared


# ===== health =====
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


def cmd_health(notify: bool) -> str:
    _capability, _db, _shared = _plugins()
    _shared.reload_if_changed()
    results = _capability.check_all()
    lines = [
        f"{kw} [{('在线' if ok else '离线')}] {detail[:60]}" for kw, ok, detail in results
    ]
    messages = _sync_notify(results, notify)
    if messages:
        lines.append("播报：" + "；".join(messages))
    return "\n".join(lines) or "服务注册表为空。"


# ===== backup =====
def cmd_backup(keep: int = 7) -> str:
    _capability, _db, _shared = _plugins()
    src = _shared.DATA_DIR / "bot.db"
    if not src.exists():
        return "bot.db 不存在，跳过备份。"
    backup_dir = _shared.DATA_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f"bot-{datetime.now():%Y%m%d-%H%M%S}.db"
    src_conn = sqlite3.connect(str(src))
    dst_conn = sqlite3.connect(str(dest))
    try:
        with dst_conn:
            src_conn.backup(dst_conn)
    finally:
        src_conn.close()
        dst_conn.close()
    for old in sorted(backup_dir.glob("bot-*.db"), reverse=True)[keep:]:
        old.unlink(missing_ok=True)
    return f"备份完成：{dest}"


# ===== recover =====
def cmd_recover(notify: bool) -> int:
    _capability, _db, _shared = _plugins()
    results = _capability.run_recovery()
    text = _capability.summary(results)
    print(text)
    if notify:
        if target := _notify_group():
            print(_capability.notify_send("group", target, f"【服务恢复检查】\n{text}"))
        else:
            print("未配置播报目标群（random_events.group_openid），跳过 QQ 通知。")
    return 0


# ===== memory-embed =====
def cmd_memory_embed(batch: int = 64) -> str:
    import memory
    return memory.backfill(batch)


def cmd_memory_grow(dry_run: bool) -> str:
    """工程化成长：向量/事件图/巩固/修剪/词法索引 + 可信度报告。"""
    import agent
    return json.dumps(agent.grow(dry_run=dry_run), ensure_ascii=False, indent=2)


def cmd_memory_sleep(force: bool = False) -> str:
    """手动跑一夜：浅睡/深睡巩固 + REM 做梦。默认按日去重，--force 可重跑。"""
    import memory
    return json.dumps(memory.sleep_run(force=force), ensure_ascii=False, indent=2)


def cmd_emotion_eval(path: str = "") -> str:
    """情绪判断评测：分类准确率 + VAD MAE（--file 评测集 JSON）。"""
    import memory
    if not path:
        _capability, _db, _shared = _plugins()
        path = str(_shared.DATA_DIR / "emotion_probes.json")
    try:
        with open(path, encoding="utf-8") as f:
            probes = json.load(f)
    except OSError as e:
        return f"评测集不存在：{path}（{e}）"
    res = memory.emotion_eval(probes)
    try:
        # v2.2+ 议题 mood-VAD 一致性（写入标签↔存向量 + 跨表 VAD 漂移）
        from memory import topic as topic_mod
        res["topic_mood"] = topic_mod.mood_eval()
    except Exception:
        pass
    return json.dumps(res, ensure_ascii=False, indent=2)


def cmd_emotion_log(days: int = 14, out: str = "") -> str:
    """导出情绪判断日志（训练数据原料），--out 写 jsonl。"""
    import memory
    rows = memory.emotion_log_rows(days)
    if not out:
        return f"共 {len(rows)} 条情绪判断日志（近 {days} 天）。用 --out 导出 jsonl。"
    with open(out, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return f"已导出 {len(rows)} 条 → {out}"


def cmd_config_validate() -> str:
    """校验 config.json：未知段、数值字段类型、窗口类字段长度、分享参数取值。"""
    _capability, _db, _shared = _plugins()
    cfg = _shared.CONFIG
    errors, warnings = [], []
    core = (cfg.get("memory", {}) or {}).get("core", {}) or {}
    KNOWN = {
        "enabled", "top_k", "min_score", "throttle_s", "context_budget_chars", "query",
        "rerank", "mmr", "cache", "telemetry", "session", "reflection", "analysis",
        "persona", "world", "trace", "emotion", "sleep", "schedule", "weather",
        "environment", "sharing", "living", "space", "interaction", "weights",
        "vector_index", "policy", "mind", "sensors", "agents", "persona_pack",
    }
    for k in core:
        if k.startswith("_") or k in KNOWN:
            continue
        warnings.append(f"未知配置段 memory.core.{k}")
    for k in ("top_k", "min_score", "context_budget_chars", "throttle_s"):
        if k in core:
            try:
                float(core[k])
            except (TypeError, ValueError):
                errors.append(f"memory.core.{k} 应为数字，当前 {core[k]!r}")
    for sec, keys in (("sleep", ("deep_window",)), ("interaction", ("user_night_hours",))):
        for k in keys:
            v = core.get(sec, {}).get(k)
            if v is not None and (not isinstance(v, list) or len(v) != 2):
                errors.append(f"memory.core.{sec}.{k} 应为 [起始, 结束]，当前 {v!r}")
    for k in ("threshold", "max_per_day", "max_per_week", "penalty_hours"):
        v = core.get("sharing", {}).get(k)
        if v is not None:
            try:
                if float(v) <= 0:
                    errors.append(f"memory.core.sharing.{k} 应 > 0，当前 {v}")
            except (TypeError, ValueError):
                errors.append(f"memory.core.sharing.{k} 应为数字，当前 {v!r}")
    if not errors and not warnings:
        return "config-validate：全部通过"
    lines = ["config-validate 报告"]
    for e in errors:
        lines.append("ERROR " + e)
    for w in warnings:
        lines.append("WARN " + w)
    return "\n".join(lines)


def cmd_memory_eval(path: str, k: int, save: bool, dataset: str = "") -> str:
    """评测召回率/MRR：python tools.py memory-eval --file probes.json --k 5。"""
    import memory
    probes = None
    if dataset:
        from plugins import _db
        data = _db.kv_get("memory", f"dataset:{dataset}") or {}
        probes = data.get("probes")
        if not probes:
            return f"评测集不存在：{dataset}"
    if probes is None and not path:
        return (
            "请提供评测集文件：--file probes.json（[{\"query\":..., \"expected\":[...], \"scope\":...}]）\n"
            + memory.eval_report()
        )
    if probes is None:
        try:
            with open(path, encoding="utf-8") as f:
                probes = json.load(f)
        except Exception as e:
            return f"评测集读取失败：{e}"
    result = memory.run_eval(probes, k=k)
    if save:
        from plugins import _db
        _db.kv_set("memory", "eval_baseline", result)
        return json.dumps(result, ensure_ascii=False, indent=2) + "\n（已存为 baseline）\n" + memory.eval_report()
    return json.dumps(result, ensure_ascii=False, indent=2) + "\n" + memory.eval_report()


def cmd_eval_dataset_save(name: str, path: str) -> str:
    """保存命名评测集（版本化对比用）：memory-eval-dataset <名称> --file probes.json。"""
    from plugins import _db
    try:
        with open(path, encoding="utf-8") as f:
            probes = json.load(f)
    except Exception as e:
        return f"评测集读取失败：{e}"
    _db.kv_set(
        "memory",
        f"dataset:{name}",
        {"probes": probes, "created": datetime.now().isoformat(timespec="seconds")},
    )
    return f"已保存评测集 {name}（{len(probes)} 条），可用 memory-eval --dataset {name} --save 跑基线"


def cmd_memory_route(text: str) -> str:
    """诊断：显示一条消息的分类路由（存到哪些算法库）与查询理解。"""
    import memory
    return json.dumps(
        {
            "understand": memory.understand(text),
            "route": memory.route(text),
            "route_stats": memory.route_stats(),
        },
        ensure_ascii=False,
        indent=2,
    )


def cmd_memory_topics(scope: str, limit: int) -> str:
    """列出议题（大类 → 议题 → 参数）。"""
    import memory
    topics = memory.topic_list(scope or None, limit)
    return json.dumps(
        [
            {
                "id": t["id"],
                "category": t["category"],
                "topic": t["topic"],
                "status": t["status"],
                "importance": t["importance"],
                "confidence": t["confidence"],
                "params": len(memory.topic_package(t["id"]).get("params", [])),
            }
            for t in topics
        ],
        ensure_ascii=False,
        indent=2,
    )


def cmd_memory_index(tune: bool, file: str, nlist: str, nprobe: str) -> str:
    """重建/调优自研 IVF 向量索引。--tune 用评测集做 nlist/nprobe 对照。"""
    import memory
    if tune:
        if not file:
            return "请提供评测集：--tune --file probes.json"
        try:
            with open(file, encoding="utf-8") as f:
                probes = json.load(f)
        except Exception as e:
            return f"评测集读取失败：{e}"
        nlists = tuple(int(x) for x in nlist.split(",") if x.strip()) or (4, 8, 16)
        nprobes = tuple(int(x) for x in nprobe.split(",") if x.strip()) or (1, 2, 4)
        return json.dumps(memory.vec_tune(probes, nlists, nprobes), ensure_ascii=False, indent=2)
    return json.dumps(memory.index_vectors(), ensure_ascii=False, indent=2)


def cmd_memory_clear_user(uid: str) -> str:
    """按用户彻底清除（隐私权）：记忆/事件/议题/属性/索引。"""
    from plugins import _db
    scope = f"c2c:{uid}"
    _db.purge_scope(scope)
    _db.audit_add("memory.clear_user", scope)
    return f"已清除 {scope} 的全部记忆/事件/议题/索引"


_PROBE_SOCIAL_WORDS = (
    "你好", "您好", "哈喽", "嗨", "在吗", "早安", "晚安", "谢谢", "多谢", "拜拜",
    "再见", "辛苦了", "打扰", "哈哈", "嘻嘻", "好的", "收到", "嗯", "哦",
)
_PROBE_QUESTION_WORDS = ("？", "?", "吗", "呢", "哪", "什么", "怎么", "谁", "为什么", "啥", "几", "是不是", "有没有")


def _is_social_probe(query) -> bool:
    """评测集过滤（v2.3 修复 P2-2）：寒暄/短陈述句不构成检索需求，剔除。"""
    q = str(query or "").strip()
    if len(q) < 3:
        return True
    if any(w in q for w in _PROBE_SOCIAL_WORDS):
        return True
    if len(q) <= 6 and not any(w in q for w in _PROBE_QUESTION_WORDS):
        return True
    return False


def cmd_memory_probes(limit: int, out: str) -> str:
    """把查询日志导出为评测集（弱监督：当时返回的即期望）。"""
    from plugins import _db

    def _probe_category(query):
        q = str(query or "")
        if any(w in q for w in ("昨天", "前天", "上周", "这周", "上个月", "去年", "今天", "什么时候", "哪天", "最近")):
            return "time"
        if any(w in q for w in ("哪", "在哪", "哪里", "房间", "柜", "冰箱", "客厅", "卧室", "厨房", "找")):
            return "space"
        if any(w in q for w in ("开心", "难过", "生气", "烦", "怕", "心情", "情绪", "高兴", "哭", "气")):
            return "emotion"
        return "lexical"

    rows = _db.query_log_pending(limit)
    probes = []
    seen_q = set()
    for r in rows:
        q = str(r["query"] or "").strip()
        if not q or q in seen_q or _is_social_probe(q):
            continue
        seen_q.add(q)
        hits = json.loads(r["hits"] or "[]")
        scopes = json.loads(r["scopes"] or "[]")
        if not hits:
            continue
        probes.append(
            {
                "query": q,
                "expected": hits[:5],
                "scope": scopes[0] if scopes else None,
                "category": "subject" if scopes and str(scopes[0]).startswith("npc:") else _probe_category(q),
            }
        )
    if not probes:
        return "没有待导出的查询日志（先让机器人跑一阵子）"
    dest = pathlib.Path(out) if out else ROOT / "data" / "probes.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(probes, ensure_ascii=False, indent=2), encoding="utf-8")
    _db.query_log_mark_exported([r["id"] for r in rows])
    return f"已导出 {len(probes)} 条评测集到 {dest}（下次 memory-grow 自动跑 eval 对比 baseline）"


def cmd_memory_calibrate(file: str, k: int) -> str:
    """用评测集训练置信度标定映射。"""
    import memory
    try:
        with open(file, encoding="utf-8") as f:
            probes = json.load(f)
    except Exception as e:
        return f"评测集读取失败：{e}"
    return json.dumps(memory.calibrate_train(probes, k=k), ensure_ascii=False, indent=2)


def cmd_memory_sessions(scope: str, limit: int) -> str:
    """查看会话。"""
    import memory
    return json.dumps(
        memory.session_rows(scope or None, None, 0, limit), ensure_ascii=False, indent=2
    )


def cmd_memory_history(scope: str, limit: int) -> str:
    """查看记忆变更历史（合并/纠错/遗忘的旧值与新值）。"""
    from plugins import _db
    rows = _db.history_rows(scope or None, limit=limit)
    return json.dumps(rows, ensure_ascii=False, indent=2) or "（暂无历史）"


def cmd_memory_feedback(scope: str, limit: int) -> str:
    """查看用户反馈日志（纠错/确认/点赞），弱监督学习数据源。"""
    from plugins import _db
    rows = _db.feedback_rows(scope or None, limit=limit)
    return json.dumps(rows, ensure_ascii=False, indent=2) or "（暂无反馈）"


def cmd_relationship(scope: str) -> str:
    """查看 AI 与用户的关系状态（trust/familiarity/closeness/stage）。"""
    import memory
    if scope:
        return json.dumps(memory.relationship_describe(scope) or "（无记录）", ensure_ascii=False, indent=2)
    return json.dumps(memory.relationship_rows(), ensure_ascii=False, indent=2)


def cmd_memory_governance(scope: str) -> str:
    """Memory Governance 报告（v3.1 §9）：遗忘/巩固/冲突/隐私现状。"""
    import memory
    return json.dumps(memory.governance_report(scope or None), ensure_ascii=False, indent=2)


def cmd_memory_trace(scope: str, since: str, limit: int, out: str) -> str:
    """导出记忆处理轨迹（JSON，程序分析用）。"""
    from plugins import _db
    rows = _db.trace_rows(scope or None, since or None, limit)
    text = json.dumps(rows, ensure_ascii=False, indent=2)
    if out:
        pathlib.Path(out).write_text(text, encoding="utf-8")
        return f"已导出 {len(rows)} 条轨迹到 {out}"
    return text


def cmd_memory_trace_md(scope: str, since: str, limit: int) -> str:
    """导出记忆处理轨迹（Markdown，人工阅读用）。"""
    from plugins import _db
    import memory
    rows = _db.trace_rows(scope or None, since or None, limit)
    reviews = _db.trace_review_map([r["id"] for r in rows])
    return memory.trace_markdown(rows, reviews)


def cmd_memory_trace_review(
    trace_id: int,
    extraction=None,
    decision=None,
    confidence=None,
    provenance=None,
    privacy=None,
    comment: str = "",
    reviewer: str = "",
) -> str:
    """人工评分轨迹（v11）：多维度 1~5 分，评分驱动行为调整。"""
    import memory
    scores = {
        k: v
        for k, v in {
            "extraction": extraction,
            "decision": decision,
            "confidence": confidence,
            "provenance": provenance,
            "privacy": privacy,
        }.items()
        if v is not None
    }
    return memory.trace_score(int(trace_id), scores, comment, reviewer)


def cmd_memory_trace_adjust() -> str:
    """查看评分驱动的行为调整（v11）。"""
    import memory
    return json.dumps(memory.trace_adjustments(force=True), ensure_ascii=False, indent=2)


def cmd_data_dump_json(out: str) -> str:
    """全表 JSON 转储（v12）：数据可移植/可分析格式。"""
    from plugins import _db
    data = _db.dump_all()
    out = out or str(ROOT / "data" / "full-dump.json")
    pathlib.Path(out).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return f"已导出全表 JSON 到 {out}（{sum(len(v) for v in data.values())} 行）"


def cmd_data_export(out: str = "", with_config: bool = False) -> str:
    """全量数据打包导出（v12）：bot.db 安全备份 + 全表 JSON + 元信息 → tar.gz。"""
    import tarfile
    import tempfile
    from datetime import datetime
    from plugins import _db
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = out or str(ROOT / "data" / f"export-{ts}.tar.gz")
    out = pathlib.Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmpd:
        tmp = pathlib.Path(tmpd)
        db_path = tmp / "bot.db"
        _db.backup_to(db_path)
        data = _db.dump_all()
        (tmp / "data.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        meta = {
            "version": "v12",
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "tables": {t: len(rows) for t, rows in data.items()},
            "db_size": db_path.stat().st_size,
        }
        (tmp / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        for name in ("probes.json",):
            src = ROOT / "data" / name
            if src.exists():
                shutil.copyfile(src, tmp / name)
        if with_config:
            shutil.copyfile(ROOT / "config.json", tmp / "config.json")
        with tarfile.open(out, "w:gz") as tar:
            for f in tmp.iterdir():
                tar.add(f, arcname=f.name)
    return f"已导出全量数据到 {out}（{sum(meta['tables'].values())} 行）"


def cmd_data_import(path: str, replace: bool = False, dry_run: bool = False) -> str:
    """导入数据（v12）：支持 .tar.gz / .json / .db；merge 合并，replace 覆盖。"""
    import tarfile
    import tempfile
    from plugins import _db
    path = pathlib.Path(path)
    if not path.exists():
        return f"文件不存在：{path}"
    data, db_bytes = None, None
    if path.suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
    elif path.suffix == ".db":
        db_bytes = path.read_bytes()
    elif path.suffix == ".gz" or str(path).endswith(".tar.gz"):
        with tempfile.TemporaryDirectory() as tmpd:
            tmp = pathlib.Path(tmpd)
            with tarfile.open(path, "r:gz") as tar:
                try:
                    tar.extractall(tmp, filter="data")
                except TypeError:
                    tar.extractall(tmp)
            jp, dp = tmp / "data.json", tmp / "bot.db"
            if jp.exists():
                data = json.loads(jp.read_text(encoding="utf-8"))
            elif dp.exists():
                db_bytes = dp.read_bytes()
            else:
                return "包内未找到 data.json 或 bot.db"
    else:
        return "仅支持 .tar.gz / .json / .db"

    if db_bytes is not None:
        if dry_run:
            return "（dry-run）将用包内 bot.db 替换当前数据库（需重启生效）"
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = ROOT / "data" / f"bot.pre-import-{ts}.db"
        _db.backup_to(backup)
        dest = ROOT / "data" / "bot.db"
        dest.write_bytes(db_bytes)
        for suffix in ("-wal", "-shm"):
            stale = pathlib.Path(str(dest) + suffix)
            if stale.exists():
                stale.unlink()
        return f"已替换数据库（旧库备份：{backup}）。请 systemctl restart qqbot 生效。"

    if data is None:
        return "没有可导入的数据"
    counts = _db.restore_all(data, replace=replace)
    total = sum(v for v in counts.values() if isinstance(v, int) and v > 0)
    failed = [t for t, v in counts.items() if v == -1]
    if dry_run:
        return f"（dry-run）将导入 {total} 行，各表：{counts}"
    msg = f"已导入 {total} 行（replace={replace}）"
    if failed:
        msg += f"，跳过不兼容表：{failed}"
    return msg


def cmd_goal(action, title, priority, scope, motivation="", confidence=0.7) -> str:
    """目标规划（v6）：goal add/list/done。"""
    import memory
    scope = scope or "cli"
    if action == "add":
        return memory.goal_add(
            scope, title, priority=priority, motivation=motivation, confidence=confidence
        )
    if action == "list":
        return json.dumps(memory.goal_list(scope), ensure_ascii=False, indent=2)
    if action == "done":
        return memory.goal_update(scope, title, status="done")
    return "用法：goal add <标题> [--priority N] | goal list | goal done <标题>"


def cmd_consult(text: str, scope: str) -> str:
    """决策顾问单轮（v6）：一次一问。"""
    import memory
    return memory.consult_turn(scope or "cli", text)


def cmd_expression(text: str, scope: str) -> str:
    """语言语义解释层（v7）：表达分析 + 用户表达画像。"""
    import memory
    info = memory.expression_analyze(text)
    prof = memory.expression_profile(scope or "cli")
    return json.dumps({"analyze": info, "profile": prof}, ensure_ascii=False, indent=2)


def cmd_world(scope: str) -> str:
    """用户中心世界模型（v8）：快照 + 现状统计。"""
    import memory
    return json.dumps(
        {
            "snapshot": memory.world_snapshot(scope or "cli"),
            "stats": memory.world_stats(scope or None),
        },
        ensure_ascii=False,
        indent=2,
    )


def cmd_character_build(name: str) -> str:
    """输入人物名称，自动生成设定/经历档案并存入统一记忆（char:<名>），
    同时写入 docs/characters/<名>.md 供人工审阅/编辑。"""
    import memory
    info = memory.character_build(name)
    if info.get("error"):
        return json.dumps(info, ensure_ascii=False, indent=2)
    path = memory.character_write_md(name)  # 从记忆库渲染并写入 md（与记忆保持一致）
    return json.dumps(
        {**info, "md": str(path)}, ensure_ascii=False, indent=2
    )


def cmd_character_sync(arg: str) -> str:
    """把编辑后的 md 档案同步回记忆库（arg 为人物名或 md 文件路径）。"""
    import memory
    is_path = "/" in (arg or "") or "\\" in (arg or "") or (arg or "").lower().endswith(".md")
    info = memory.character_sync(path=arg) if is_path else memory.character_sync(name=arg)
    return json.dumps(info, ensure_ascii=False, indent=2)


# ===== mcp =====
def cmd_mcp() -> int:
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        print("未安装 mcp SDK（pip install mcp），跳过 MCP Server。")
        return 0
    _capability, _db, _shared = _plugins()
    cap = _capability
    mcp = FastMCP("yuno-mcp")

    @mcp.tool()
    def services_list():
        """列出服务注册表中的全部服务。"""
        return cap.services_list()

    @mcp.tool()
    def services_status(keyword: str = None):
        """查询服务健康状态，keyword 可省略以查询全部。"""
        return cap.services_status(keyword)

    @mcp.tool()
    def services_start(keyword: str):
        """启动注册表中的服务。"""
        return cap.service_start(keyword)

    @mcp.tool()
    def services_stop(keyword: str):
        """停止注册表中的服务。"""
        return cap.service_stop(keyword)

    @mcp.tool()
    def services_restart(keyword: str):
        """重启注册表中的服务。"""
        return cap.service_restart(keyword)

    @mcp.tool()
    def services_logs(keyword: str):
        """获取服务最近日志。"""
        return cap.service_logs(keyword)

    @mcp.tool()
    def config_get():
        """读取当前配置。"""
        return cap.config_get()

    @mcp.tool()
    def config_set(section: str, key: str, value: str):
        """修改白名单配置字段（经 qqbot-ctl 校验）。"""
        return cap.config_set(section, key, value)

    @mcp.tool()
    def audit_query(limit: int = 50, action: str = None):
        """查询操作审计记录。"""
        return cap.audit_query(limit, action)

    @mcp.tool()
    def disk_usage():
        """查看服务器磁盘占用概览。"""
        return cap.disk_usage()

    @mcp.tool()
    def memory_clear(kind: str, key: str):
        """后台清除指定场景的记忆（kind: user/member/group）。"""
        return cap.memory_clear(kind, key)

    @mcp.tool()
    def memory_search(query: str, scope: str = None, key: str = None, limit: int = 10):
        """统一记忆检索（关键词 + 可选向量）。"""
        return cap.memory_search(query, scope, key, limit)

    @mcp.tool()
    def memory_add(scope: str, key: str, fact: str):
        """写入统一记忆（scope: admin/c2c:<uid>/group:<gid>/group_all:<gid>）。"""
        return cap.memory_add(scope, key, fact)

    @mcp.tool()
    def notify_send(target_type: str, target: str, content: str):
        """向 QQ 群/私聊发送一条播报（入队）。"""
        return cap.notify_send(target_type, target, content)

    mcp.run()
    return 0


def cmd_mind_status(scope=""):
    """心智状态快照（mind_state + 意图 + 程序记忆统计）。"""
    from memory import mind, procedures
    import memory.stats as stats_mod
    snap = mind.snapshot(str(scope or ""), "")
    snap["procedures"] = procedures.stats()
    snap["counters"] = stats_mod.counters()
    return json.dumps(snap, ensure_ascii=False, indent=2)


def cmd_procedures_list():
    """列出程序记忆（System 1 习惯）。"""
    from memory import procedures
    return procedures.report()


def cmd_living_bootstrap():
    """人设→场景生成：按 persona 补齐家里物品（只新增不覆盖）。"""
    from memory import living
    return json.dumps(living.bootstrap_from_persona(), ensure_ascii=False, indent=2)


def cmd_space_eval(save=False, compare=False):
    """空间评测：X在哪命中 / 时刻召回 / 找东西模拟（--save 落基线 / --compare 对比）。"""
    from memory import space_eval
    return json.dumps(space_eval.run(save=save, compare=compare), ensure_ascii=False, indent=2)


def cmd_time_eval(save=False, compare=False):
    """时间感知评测：时间段召回 / 时间线序列 / 日期精确度（--save 落基线 / --compare 对比）。"""
    from memory import time_eval
    return json.dumps(time_eval.run(save=save, compare=compare), ensure_ascii=False, indent=2)


def cmd_subjects_status():
    """多主体记忆：列出已注册主体及各视角数据量。"""
    from memory import subjects
    from plugins import _db
    rows = []
    for name in subjects.registered():
        nscope = subjects.scope_of(name)
        rows.append({
            "name": name, "scope": nscope,
            "memories": len(_db.memory_rows(nscope)),
            "events": len(_db.event_rows(nscope)),
        })
    return json.dumps({"enabled": subjects.enabled(), "subjects": rows}, ensure_ascii=False, indent=2)


def cmd_subjects_eval(save=False, compare=False) -> str:
    """多主体评测：写入成功 / 隐私门控 / 对话引用（--save 落基线 / --compare 对比）。"""
    from memory import subjects
    return json.dumps(subjects.eval_run(save=save, compare=compare), ensure_ascii=False, indent=2)


def cmd_consistency_eval() -> str:
    """双轨制一致性：失效队列长度 + 本次重算数。"""
    from plugins import _db
    pending = len(_db.invalidation_rows(100))
    from memory import controller
    done = controller.reconcile_pending()
    return json.dumps({"pending": pending, "reconciled": done["reconciled"]}, ensure_ascii=False, indent=2)


def cmd_policy_classify() -> str:
    """事实分类探针：'含关键词但其实是过程' 的句子误判率（policy-classify）。"""
    from memory import policy
    return json.dumps(policy.classify_report(), ensure_ascii=False, indent=2)


def cmd_revive_status(scope: str = "") -> str:
    """主动消息决策状态：泊松概率 + 贝叶斯用户状态（只读，不消费触发）。"""
    from memory import revive
    return json.dumps(revive.peek(scope or None), ensure_ascii=False, indent=2)


def cmd_bandit_status(scope: str = "") -> str:
    """回应策略 bandit 后验：各策略均值/样本数 + 上次选择。"""
    from memory import bandit
    return json.dumps(bandit.status(scope), ensure_ascii=False, indent=2)


def cmd_topic_vad_backfill() -> str:
    """给只有 mood 标签的旧议题补近似 VAD/复合情绪（幂等，也可随 memory-grow 自动跑）。"""
    from memory import topic
    return json.dumps(topic.backfill_vad(), ensure_ascii=False, indent=2)


def cmd_persona_smoke() -> str:
    """Persona Pack 冒烟：加载校验 + 房间图连通 + 模板渲染 + 代码硬编码扫描。"""
    from agent import persona as persona_mod
    from memory import living, pack
    issues = []
    pk = pack.active()
    name = persona_mod.persona_name()
    if not name:
        issues.append("persona_name 为空")
    w = pack.world()
    layout = w.get("layout") or {}
    if not layout:
        issues.append("world.json 缺少 layout")
    if not w.get("items"):
        issues.append("world.json 缺少 items")
    rooms = list(layout.keys())
    edges = w.get("edges") or []
    if rooms:
        adj = {}
        for a, b in edges:
            adj.setdefault(a, []).append(b)
            adj.setdefault(b, []).append(a)
        seen, queue = {rooms[0]}, [rooms[0]]
        while queue:
            cur = queue.pop()
            for n in adj.get(cur, []):
                if n not in seen:
                    seen.add(n)
                    queue.append(n)
        if len(seen) != len(rooms):
            issues.append(f"房间图不连通：{set(rooms) - seen}")
    try:
        t = living.INSPECT_PROMPT.format(name=name, role=w.get("role", ""), room="客厅", container="茶几", items="空的")
        if name not in t:
            issues.append("INSPECT_PROMPT 渲染未包含名字")
    except Exception as e:
        issues.append(f"模板渲染失败：{e}")
    try:
        from memory import floorplan as fp
        for issue in fp.validate():
            issues.append("floorplan: " + issue)
    except Exception as e:
        issues.append(f"floorplan 校验异常：{e}")
    fp_info = {}
    try:
        from memory import floorplan as fp
        fp_info = {
            "enabled": fp.enabled(),
            "areas": {r: fp.room_area_m2(r) for r in fp.rooms()},
            "svg_available": bool(fp.render_svg()),
        }
    except Exception:
        pass
    config_leaks = []
    try:
        from plugins import _shared as _sh
        cfg = _sh.CONFIG.get("memory", {}).get("core", {}) or {}
        owned = {
            "emotion": ["baseline"],
            "sleep": ["deep_window"],
            "sharing": ["threshold"],
            "living": ["lazy_factor", "lazy_label", "birthday", "birth_year",
                       "birthday_hint_days", "birthday_threshold", "inspect_delay_s"],
            "mind": ["persona_weights"],
        }
        for block, keys in owned.items():
            seg = cfg.get(block) or {}
            for k in keys:
                if k in seg:
                    config_leaks.append(f"{block}.{k}={seg[k]}")
    except Exception:
        pass
    if config_leaks:
        issues.append("config 残留人设参数（读取已 pack 优先、不生效，但建议清理）：" + "、".join(config_leaks))
    example_issues = []
    try:
        # 示例区规范（v2.3）：禁约定/承诺/未来计划——示例会被 LLM 实例化成真实事件
        from memory import pack as pack_mod
        _pt = pack_mod.persona_text() or ""
        _idx = _pt.find("# 说话示例")
        if _idx >= 0:
            _tail = _pt[_idx:]
            _end = _tail.find("\n# ", 1)
            _sec = _tail[:_end if _end > 0 else len(_tail)]
            for _bad in ("约定", "承诺", "明天见", "答应", "约好", "说好", "见面", "放鸽子", "约了", "约过"):
                if _bad in _sec:
                    example_issues.append(_bad)
    except Exception:
        pass
    if example_issues:
        issues.append("示例区出现约定/承诺词（会被实例化成假事件）：" + "、".join(example_issues))
    hardcoded = []
    for root, _dirs, files in os.walk(ROOT):
        if ".git" in root or "personas" in root:
            continue
        for fn in files:
            if not fn.endswith(".py") or fn in ("games.py", "tools.py"):
                continue
            p = os.path.join(root, fn)
            try:
                text = open(p, encoding="utf-8").read()
            except Exception:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if "千石由乃" in line:
                    hardcoded.append(f"{os.path.relpath(p, ROOT)}:{i}")
    return json.dumps({
        "pack": pk, "persona_name": name, "world_rooms": len(rooms),
        "issues": issues,
        "floorplan": fp_info,
        "config_leaks": config_leaks,
        "example_issues": example_issues,
        "hardcoded_count": len(hardcoded),
        "hardcoded_code": hardcoded[:20],
    }, ensure_ascii=False, indent=2)


def cmd_persona_switch(pack_name: str) -> str:
    """切换 Persona Pack：校验 pack 文件 → 写 config（重启生效）。"""
    from memory import pack
    d = pack.pack_dir(pack_name)
    if not d.exists() or not (d / "world.json").exists():
        return f"pack 不存在或缺少 world.json：{d}"
    _capability, _db, _shared = _plugins()
    core = _shared.CONFIG.setdefault("memory", {}).setdefault("core", {})
    core.setdefault("persona_pack", {})["pack"] = pack_name
    # 清理 config 里残留的旧人设专属参数（读取已 pack 优先，双保险：不残留、不混淆）
    persona_owned = {
        "emotion": ["baseline"],
        "sleep": ["deep_window"],
        "sharing": ["threshold"],
        "living": ["lazy_factor", "lazy_label", "birthday", "birth_year",
                   "birthday_hint_days", "birthday_threshold", "inspect_delay_s"],
        "mind": ["persona_weights"],
    }
    for block, keys in persona_owned.items():
        seg = core.get(block)
        if isinstance(seg, dict):
            for k in keys:
                seg.pop(k, None)
            if not seg:
                core.pop(block, None)
    _shared.save_config()
    from memory import pack
    pack.invalidate()
    from agent import persona
    persona._persona_name_cache = None
    return f"已切换 persona pack → {pack_name}（重启后生效；记忆隔离需独立数据库，另见说明）"


def cmd_floorplan_render(pack_name: str = "", out: str = "") -> str:
    """渲染平面图 SVG 预览 + 房间几何事实表（面积/质心/离大门距离）。
    SVG 也可作为将来 floorplan-import 的回环样本。"""
    from plugins import _shared
    from memory import floorplan as fp
    name = pack_name or fp.active_pack()
    svg = fp.render_svg(name)
    if not svg:
        return f"pack {name} 没有 floorplan（personas/{name}/world.json 缺 floorplan 段）"
    dest = pathlib.Path(out) if out else _shared.DATA_DIR / "floorplans" / f"{name}.svg"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(svg, encoding="utf-8")
    except Exception as e:
        return f"SVG 写入失败：{e}"
    facts = {}
    for r in fp.rooms(name):
        facts[r] = {
            "area_m2": round(fp.room_area_m2(r, name), 1),
            "centroid": fp.room_centroid(r, name),
            "entrance_m": fp.dist_to_entrance(r, name),
            "facts": fp.facts_text(r, name),
        }
    return json.dumps(
        {"pack": name, "svg": str(dest), "issues": fp.validate(name), "rooms": facts},
        ensure_ascii=False, indent=2,
    )


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


def cmd_ablation() -> str:
    """机制消融：临时覆盖 config 单开关，同一套 probes 各跑一遍，输出贡献表 + 实验日志。"""
    _capability, _db, _shared = _plugins()
    probes_path = _shared.DATA_DIR / "probes.json"
    if not probes_path.exists():
        return "评测集不存在（先 tools.py memory-probes 生成）"
    probes = json.loads(probes_path.read_text(encoding="utf-8"))
    return json.dumps(run_ablation(probes), ensure_ascii=False, indent=2)


def cmd_experiments(limit=50) -> str:
    """实验日志：改动/评测的基线前后与回归标记。"""
    from plugins import _db
    return json.dumps(_db.exp_log_rows(limit), ensure_ascii=False, indent=2)


SCENARIO_RUBRIC = (
    "你是对话质量评委。按 5 个维度各打 1~5 分，只输出 JSON："
    '{"recall":n,"precision":n,"coherence":n,"consistency":n,"naturalness":n,"avg":n,"comment":"…"}。'
    "维度：recall=是否覆盖用户需求/记忆；precision=是否答非所问/编造；coherence=多轮是否连贯；"
    "consistency=是否前后矛盾；naturalness=是否自然像真人。"
)


def scenario_replay(path: str = "", score: bool = False, scenario_id=None) -> dict:
    """场景回放（可只回放单个场景），score=True 时附 DeepSeek 五维评分。
    场景集：data/eval/scenarios.json = [{"id","scope","messages":[{"user":...}],"expected":[...]}]。
    返回 {"replayed","scenarios"}；score=True 时附加 {"scored","avg"}。供 CLI 与 webapp 共用。
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
        history, replies = [], []
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
    out = {"replayed": len(results), "scenarios": results}
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
            s, e = raw.find("{"), raw.rfind("}")
            if s >= 0:
                s2 = json.loads(raw[s:e + 1])
                s2["avg"] = round(
                    sum(float(s2.get(k, 0)) for k in ("recall", "precision", "coherence", "consistency", "naturalness")) / 5,
                    2,
                )
        except Exception as ex:
            s2 = {"error": str(ex)}
        scored.append({"id": r["id"], "scores": s2})
    avg = {}
    vals = {k: [] for k in ("recall", "precision", "coherence", "consistency", "naturalness")}
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


def cmd_scenario_eval(path: str = "", score: bool = False) -> str:
    """场景回放评分：重放多轮对话（agent.ask 逐条），--score 用 DeepSeek 五维 rubric 打分。"""
    res = scenario_replay(path, score=score)
    if res.get("error"):
        return res["error"]
    if score:
        return json.dumps({"scored": res["scored"], "avg": res["avg"]}, ensure_ascii=False, indent=2)
    return json.dumps({"replayed": res["replayed"], "scenarios": res["scenarios"]}, ensure_ascii=False, indent=2)


def main() -> int:
    parser = argparse.ArgumentParser(description="YUNO 2.0 运维/入口工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("health", help="独立健康检查")
    p.add_argument("--notify", action="store_true", help="状态变化时播报到 QQ")
    p.set_defaults(func=lambda a: print(cmd_health(a.notify)) or 0)

    sub.add_parser("backup", help="每日 SQLite 备份").set_defaults(
        func=lambda a: print(cmd_backup()) or 0
    )

    p = sub.add_parser("recover", help="一键恢复服务")
    p.add_argument("--notify", action="store_true", help="结果播报到 QQ")
    p.set_defaults(func=lambda a: cmd_recover(a.notify))

    p = sub.add_parser("memory-embed", help="为缺少向量的记忆回填 embedding")
    p.add_argument("--batch", type=int, default=64)
    p.set_defaults(func=lambda a: print(cmd_memory_embed(a.batch)) or 0)

    p = sub.add_parser("memory-grow", help="成长/维护：向量+事件图+巩固+修剪+词法索引")
    p.add_argument("--dry-run", action="store_true", help="只出统计不写库")
    p.set_defaults(func=lambda a: print(cmd_memory_grow(a.dry_run)) or 0)

    p = sub.add_parser("memory-sleep", help="睡眠/梦境：浅睡+深睡巩固当天对话，REM 做梦")
    p.add_argument("--force", action="store_true", help="强制再跑一夜（跳过当日已睡检查）")
    p.set_defaults(func=lambda a: print(cmd_memory_sleep(a.force)) or 0)

    p = sub.add_parser("memory-eval", help="评测召回率/MRR（--file 或 --dataset）")
    p.add_argument("--file", default="", help="评测集 JSON 路径")
    p.add_argument("--dataset", default="", help="命名评测集（memory-eval-dataset 保存）")
    p.add_argument("--k", type=int, default=5)
    p.add_argument("--save", action="store_true", help="把结果存为 baseline")
    p.set_defaults(func=lambda a: print(cmd_memory_eval(a.file, a.k, a.save, a.dataset)) or 0)

    p = sub.add_parser("memory-eval-dataset", help="保存命名评测集（版本对比用）")
    p.add_argument("name", help="评测集名称（如 v1）")
    p.add_argument("--file", required=True, help="评测集 JSON 路径")
    p.set_defaults(func=lambda a: print(cmd_eval_dataset_save(a.name, a.file)) or 0)

    p = sub.add_parser("memory-route", help="诊断：显示一条消息的分类路由")
    p.add_argument("text")
    p.set_defaults(func=lambda a: print(cmd_memory_route(a.text)) or 0)

    p = sub.add_parser("memory-topics", help="列出议题（大类→议题→参数）")
    p.add_argument("--scope", default="", help="限定场景，如 c2c:xxx")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=lambda a: print(cmd_memory_topics(a.scope, a.limit)) or 0)

    p = sub.add_parser("memory-index", help="重建/调优自研 IVF 向量索引")
    p.add_argument("--tune", action="store_true", help="用评测集做 nlist/nprobe 对照实验")
    p.add_argument("--file", default="", help="评测集 JSON 路径（--tune 时必填）")
    p.add_argument("--nlist", default="4,8,16", help="候选 nlist 列表")
    p.add_argument("--nprobe", default="1,2,4", help="候选 nprobe 列表")
    p.set_defaults(func=lambda a: print(cmd_memory_index(a.tune, a.file, a.nlist, a.nprobe)) or 0)

    p = sub.add_parser("memory-clear-user", help="按用户彻底清除记忆（隐私权）")
    p.add_argument("uid")
    p.set_defaults(func=lambda a: print(cmd_memory_clear_user(a.uid)) or 0)

    p = sub.add_parser("memory-probes", help="把查询日志导出为评测集")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--out", default="", help="输出路径（默认 data/probes.json）")
    p.set_defaults(func=lambda a: print(cmd_memory_probes(a.limit, a.out)) or 0)

    p = sub.add_parser("memory-calibrate", help="用评测集训练置信度标定")
    p.add_argument("--file", required=True, help="评测集 JSON 路径")
    p.add_argument("--k", type=int, default=5)
    p.set_defaults(func=lambda a: print(cmd_memory_calibrate(a.file, a.k)) or 0)

    sub.add_parser("config-validate", help="校验 config.json：未知段/类型错误/取值越界").set_defaults(
        func=lambda a: print(cmd_config_validate()) or 0
    )

    p = sub.add_parser("emotion-eval", help="情绪判断评测：分类准确率 + VAD MAE")
    p.add_argument("--file", default="", help="评测集 JSON（默认 data/emotion_probes.json）")
    p.set_defaults(func=lambda a: print(cmd_emotion_eval(a.file)) or 0)

    p = sub.add_parser("emotion-log", help="导出情绪判断日志（训练数据原料）")
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--out", default="", help="输出 jsonl 路径（不填只打印条数）")
    p.set_defaults(func=lambda a: print(cmd_emotion_log(a.days, a.out)) or 0)

    p = sub.add_parser("memory-sessions", help="查看会话")
    p.add_argument("--scope", default="")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=lambda a: print(cmd_memory_sessions(a.scope, a.limit)) or 0)

    p = sub.add_parser("memory-history", help="查看记忆变更历史（v3）")
    p.add_argument("--scope", default="", help="限定场景（如 c2c:xxx）")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=lambda a: print(cmd_memory_history(a.scope, a.limit)) or 0)

    p = sub.add_parser("memory-feedback", help="查看用户反馈日志（v3）")
    p.add_argument("--scope", default="", help="限定场景（如 c2c:xxx）")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=lambda a: print(cmd_memory_feedback(a.scope, a.limit)) or 0)

    p = sub.add_parser("relationship", help="查看 AI 与用户关系状态（v3）")
    p.add_argument("--scope", default="", help="限定场景（如 c2c:xxx），省略则列出全部")
    p.set_defaults(func=lambda a: print(cmd_relationship(a.scope)) or 0)

    p = sub.add_parser("memory-governance", help="Memory Governance 报告（v3.1 §9）")
    p.add_argument("--scope", default="")
    p.set_defaults(func=lambda a: print(cmd_memory_governance(a.scope)) or 0)

    p = sub.add_parser("memory-trace", help="导出记忆处理轨迹 JSON（v10）")
    p.add_argument("--scope", default="")
    p.add_argument("--since", default="", help="起始时间，如 2026-08-01")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--out", default="", help="输出文件路径")
    p.set_defaults(func=lambda a: print(cmd_memory_trace(a.scope, a.since, a.limit, a.out)) or 0)

    p = sub.add_parser("memory-trace-md", help="导出记忆处理轨迹 Markdown（v10）")
    p.add_argument("--scope", default="")
    p.add_argument("--since", default="")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=lambda a: print(cmd_memory_trace_md(a.scope, a.since, a.limit)) or 0)

    p = sub.add_parser("memory-trace-review", help="人工评分轨迹（v11，多维度）")
    p.add_argument("trace_id", type=int)
    p.add_argument("--extraction", type=float, help="提取准确性 1~5")
    p.add_argument("--decision", type=float, help="决策合理性 1~5")
    p.add_argument("--confidence", type=float, help="置信度校准 1~5")
    p.add_argument("--provenance", type=float, help="来源可信度 1~5")
    p.add_argument("--privacy", type=float, help="隐私处理 1~5")
    p.add_argument("--comment", default="")
    p.add_argument("--reviewer", default="")
    p.set_defaults(
        func=lambda a: print(
            cmd_memory_trace_review(
                a.trace_id, a.extraction, a.decision, a.confidence, a.provenance, a.privacy,
                a.comment, a.reviewer,
            )
        )
        or 0
    )

    p = sub.add_parser("memory-trace-adjust", help="查看评分驱动的行为调整（v11）")
    p.set_defaults(func=lambda a: print(cmd_memory_trace_adjust()) or 0)

    p = sub.add_parser("data-export", help="全量数据打包导出（v12）")
    p.add_argument("--out", default="")
    p.add_argument("--with-config", action="store_true", help="附带 config.json")
    p.set_defaults(func=lambda a: print(cmd_data_export(a.out, a.with_config)) or 0)

    p = sub.add_parser("data-import", help="导入数据（v12）")
    p.add_argument("file")
    p.add_argument("--replace", action="store_true", help="覆盖目标表（或替换整个库）")
    p.add_argument("--dry-run", action="store_true", help="只预览不写入")
    p.set_defaults(func=lambda a: print(cmd_data_import(a.file, a.replace, a.dry_run)) or 0)

    p = sub.add_parser("data-dump-json", help="全表 JSON 转储（v12）")
    p.add_argument("--out", default="")
    p.set_defaults(func=lambda a: print(cmd_data_dump_json(a.out)) or 0)

    p = sub.add_parser("goal", help="目标规划（v6/v9）")
    gsub = p.add_subparsers(dest="action", required=True)
    ga = gsub.add_parser("add")
    ga.add_argument("title")
    ga.add_argument("--priority", type=int, default=3)
    ga.add_argument("--motivation", default="", help="目标动机")
    ga.add_argument("--confidence", type=float, default=0.7)
    ga.add_argument("--scope", default="")
    gl = gsub.add_parser("list")
    gl.add_argument("--scope", default="")
    gd = gsub.add_parser("done")
    gd.add_argument("title")
    gd.add_argument("--scope", default="")
    p.set_defaults(
        func=lambda a: print(
            cmd_goal(
                a.action,
                getattr(a, "title", ""),
                getattr(a, "priority", 3),
                a.scope,
                getattr(a, "motivation", ""),
                getattr(a, "confidence", 0.7),
            )
        )
        or 0
    )

    p = sub.add_parser("consult", help="决策顾问单轮（v6）")
    p.add_argument("text")
    p.add_argument("--scope", default="cli")
    p.set_defaults(func=lambda a: print(cmd_consult(a.text, a.scope)) or 0)

    p = sub.add_parser("expression", help="表达分析 + 用户画像（v7）")
    p.add_argument("text")
    p.add_argument("--scope", default="cli")
    p.set_defaults(func=lambda a: print(cmd_expression(a.text, a.scope)) or 0)

    p = sub.add_parser("world", help="用户中心世界模型快照（v8）")
    p.add_argument("--scope", default="cli")
    p.set_defaults(func=lambda a: print(cmd_world(a.scope)) or 0)

    p = sub.add_parser("character", help="输入人物名称，自动搜索设定/经历并存入记忆")
    p.add_argument("name", help="人物名称（如：千石由乃）")
    p.set_defaults(func=lambda a: print(cmd_character_build(a.name)) or 0)

    p = sub.add_parser("character-sync", help="把编辑后的 md 档案同步回记忆库")
    p.add_argument("arg", help="人物名或 md 文件路径")
    p.set_defaults(func=lambda a: print(cmd_character_sync(a.arg)) or 0)

    p = sub.add_parser("mind-status", help="心智状态快照（mind_state + 意图 + 程序记忆）")
    p.add_argument("--scope", default="", help="场景 scope（可选）")
    p.set_defaults(func=lambda a: print(cmd_mind_status(a.scope)) or 0)

    p = sub.add_parser("procedures-list", help="列出程序记忆（System 1 习惯）")
    p.set_defaults(func=lambda a: print(cmd_procedures_list()) or 0)

    p = sub.add_parser("living-bootstrap", help="人设→场景生成：按 persona 补齐家里物品（只新增不覆盖）")
    p.set_defaults(func=lambda a: print(cmd_living_bootstrap()) or 0)

    p = sub.add_parser("space-eval", help="空间评测：X在哪命中 / 时刻召回 / 找东西模拟")
    p.add_argument("--save", action="store_true", help="把结果存为 baseline")
    p.add_argument("--compare", action="store_true", help="与上次 baseline 对比")
    p.set_defaults(func=lambda a: print(cmd_space_eval(save=a.save, compare=a.compare)) or 0)

    p = sub.add_parser("floorplan-render", help="渲染平面图 SVG 预览 + 房间几何事实表")
    p.add_argument("--pack", default="", help="Persona pack 名（默认当前激活）")
    p.add_argument("--out", default="", help="SVG 输出路径（默认 data/floorplans/<pack>.svg）")
    p.set_defaults(func=lambda a: print(cmd_floorplan_render(a.pack, a.out)) or 0)

    p = sub.add_parser("time-eval", help="时间感知评测：时间段召回 / 时间线序列 / 日期精确度")
    p.add_argument("--save", action="store_true", help="把结果存为 baseline")
    p.add_argument("--compare", action="store_true", help="与上次 baseline 对比")
    p.set_defaults(func=lambda a: print(cmd_time_eval(save=a.save, compare=a.compare)) or 0)

    p = sub.add_parser("subjects-status", help="多主体记忆：列出已注册主体及各视角数据量")
    p.set_defaults(func=lambda a: print(cmd_subjects_status()) or 0)

    p = sub.add_parser("subjects-eval", help="多主体评测：写入成功 / 隐私门控 / 对话引用")
    p.add_argument("--save", action="store_true", help="把结果存为 baseline")
    p.add_argument("--compare", action="store_true", help="与上次 baseline 对比")
    p.set_defaults(func=lambda a: print(cmd_subjects_eval(save=a.save, compare=a.compare)) or 0)

    p = sub.add_parser("consistency-eval", help="双轨制一致性：失效队列长度 + 重算数")
    p.set_defaults(func=lambda a: print(cmd_consistency_eval()) or 0)

    p = sub.add_parser("policy-classify", help="事实分类探针：含关键词但其实是过程/指令的句子误判率")
    p.set_defaults(func=lambda a: print(cmd_policy_classify()) or 0)

    p = sub.add_parser("revive-status", help="主动消息决策：泊松概率 + 贝叶斯用户状态（只读）")
    p.add_argument("--scope", default="", help="用户 scope（如 c2c:xxx / group:xxx）")
    p.set_defaults(func=lambda a: print(cmd_revive_status(a.scope)) or 0)

    p = sub.add_parser("bandit-status", help="回应策略 bandit 后验：各策略均值 + 上次选择")
    p.add_argument("--scope", default="", help="用户 scope")
    p.set_defaults(func=lambda a: print(cmd_bandit_status(a.scope)) or 0)

    p = sub.add_parser("topic-vad-backfill", help="旧议题补近似 VAD/复合情绪（幂等）")
    p.set_defaults(func=lambda a: print(cmd_topic_vad_backfill()) or 0)

    p = sub.add_parser("persona-smoke", help="Persona Pack 冒烟：加载校验 + 房间连通 + 模板渲染 + 硬编码扫描")
    p.set_defaults(func=lambda a: print(cmd_persona_smoke()) or 0)

    p = sub.add_parser("persona-switch", help="切换 Persona Pack（校验 pack 文件并写 config）")
    p.add_argument("--pack", required=True, help="pack 名（personas/<名>/）")
    p.set_defaults(func=lambda a: print(cmd_persona_switch(a.pack)) or 0)

    p = sub.add_parser("ablation", help="机制消融矩阵：单开关 × probes，输出贡献表并写实验日志")
    p.set_defaults(func=lambda a: print(cmd_ablation()) or 0)

    p = sub.add_parser("experiments", help="实验日志：基线前后与回归标记")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=lambda a: print(cmd_experiments(a.limit)) or 0)

    p = sub.add_parser("scenario-eval", help="场景回放评分：重放多轮对话，--score 用 LLM 五维评分")
    p.add_argument("--file", default="", help="场景集 JSON（默认 data/eval/scenarios.json）")
    p.add_argument("--score", action="store_true", help="用 DeepSeek 按五维 rubric 打分")
    p.set_defaults(func=lambda a: print(cmd_scenario_eval(a.file, a.score)) or 0)

    sub.add_parser("mcp", help="启动 MCP Server").set_defaults(func=lambda a: cmd_mcp())

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
