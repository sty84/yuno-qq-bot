"""YUNO 2.0 运维/入口工具（原 5 个独立脚本合并）。

用法：
  python tools.py health [--notify]       # 独立健康检查（cron 用）
  python tools.py backup                    # 每日 SQLite 备份（保留 7 份）
  python tools.py recover [--notify]        # 一键恢复 services 注册表中未运行的服务
  python tools.py sync-persona [--target]   # 人设同步到 Hermes SOUL.md
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
    return json.dumps(memory.emotion_eval(probes), ensure_ascii=False, indent=2)


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
        "vector_index", "policy", "mind", "sensors",
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


def cmd_memory_probes(limit: int, out: str) -> str:
    """把查询日志导出为评测集（弱监督：当时返回的即期望）。"""
    from plugins import _db
    rows = _db.query_log_pending(limit)
    probes = []
    for r in rows:
        hits = json.loads(r["hits"] or "[]")
        scopes = json.loads(r["scopes"] or "[]")
        if not hits:
            continue
        probes.append(
            {
                "query": r["query"],
                "expected": hits[:5],
                "scope": scopes[0] if scopes else None,
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


# ===== sync-persona =====
def cmd_sync_persona(target: str) -> str:
    source = ROOT / "persona.md"
    text = source.read_text(encoding="utf-8").strip()
    if not text:
        return "persona.md 为空，未同步。"
    dest = pathlib.Path(target).expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, dest)
    try:
        import agent
        agent.persona.sync_identity()
    except Exception as e:
        print(f"同步记忆库 identity 失败：{e}")
    env_line = 'SYSTEM_PROMPT="' + text.replace('"', '\\"').replace("\n", "\\n") + '"'
    return (
        f"已同步到 {dest}（并写入统一记忆库人设字段：向量化 + 事件图 + 议题化）\n"
        f"如需用 .env 覆盖（服务器快速改），复制这一行到 .env：\n{env_line}"
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

    p = sub.add_parser("sync-persona", help="人设同步到 Hermes SOUL.md")
    p.add_argument(
        "--target",
        default=os.path.expanduser("~/.hermes/SOUL.md"),
        help="目标文件（默认 ~/.hermes/SOUL.md）",
    )
    p.set_defaults(func=lambda a: print(cmd_sync_persona(a.target)) or 0)

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

    p = sub.add_parser("time-eval", help="时间感知评测：时间段召回 / 时间线序列 / 日期精确度")
    p.add_argument("--save", action="store_true", help="把结果存为 baseline")
    p.add_argument("--compare", action="store_true", help="与上次 baseline 对比")
    p.set_defaults(func=lambda a: print(cmd_time_eval(save=a.save, compare=a.compare)) or 0)

    sub.add_parser("mcp", help="启动 MCP Server").set_defaults(func=lambda a: cmd_mcp())

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
