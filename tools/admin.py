"""Admin/ops CLI commands (split from tools.py)."""

import json
import os
import pathlib
import shutil
import sqlite3
import time
from datetime import datetime

from plugins import _shared

from tools.core import ROOT, _notify_group, _plugins, _sync_notify


def cmd_health(notify: bool):
    """独立健康检查：返回 (退出码, 文本)；有服务离线时退出码 1（供 cron 门控）。"""
    _capability, _db, _shared = _plugins()
    _shared.reload_if_changed()
    results = _capability.check_all()
    lines = [
        f"{kw} [{('在线' if ok else '离线')}] {detail[:60]}" for kw, ok, detail in results
    ]
    import os as _os
    if _os.getenv("YUNO_DB_BACKEND", "postgresql").strip().lower() != "sqlite":
        try:
            pg = _db.health()
            if pg.get("ok"):
                lines.append(f"PostgreSQL [在线] {pg.get('version', '')[:60]} 表数={pg.get('table_count')}")
            else:
                lines.append(f"PostgreSQL [离线] {pg.get('error', '')[:60]}")
                results.append(("PostgreSQL", False, pg.get("error", "连接失败")))
        except Exception as e:
            lines.append(f"PostgreSQL [离线] {e}")
            results.append(("PostgreSQL", False, str(e)))
    # 备份新鲜度检查
    try:
        backup_dir = _shared.DATA_DIR / "backups"
        backups = sorted(backup_dir.glob("bot-*"), key=lambda x: x.stat().st_mtime, reverse=True)
        if backups:
            age_h = (time.time() - backups[0].stat().st_mtime) / 3600
            if age_h > 24:
                lines.append(f"备份 [过期] 最近备份 {age_h:.1f} 小时前")
                results.append(("备份", False, f"最近备份 {age_h:.1f} 小时前"))
            else:
                lines.append(f"备份 [在线] 最近 {age_h:.1f} 小时前")
        else:
            lines.append("备份 [缺失] 还没有备份文件")
            results.append(("备份", False, "无备份文件"))
    except Exception as e:
        lines.append(f"备份 [检查失败] {e}")
    messages = _sync_notify(results, notify)
    if messages:
        lines.append("播报：" + "；".join(messages))
    text = "\n".join(lines) or "服务注册表为空。"
    return (1 if any(not ok for _, ok, _ in results) else 0), text


def cmd_backup(keep: int = 7) -> str:
    import os
    _capability, _db, _shared = _plugins()
    backup_dir = _shared.DATA_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backend = os.getenv("YUNO_DB_BACKEND", "postgresql").strip().lower()
    if backend == "sqlite":
        src = _shared.DATA_DIR / "bot.db"
        if not src.exists():
            return "bot.db 不存在，跳过备份。"
        dest = backup_dir / f"bot-{datetime.now():%Y%m%d-%H%M%S}.db"
        src_conn = sqlite3.connect(str(src))
        dst_conn = sqlite3.connect(str(dest))
        try:
            with dst_conn:
                src_conn.backup(dst_conn)
        finally:
            src_conn.close()
            dst_conn.close()
    else:
        dest = backup_dir / f"bot-{datetime.now():%Y%m%d-%H%M%S}.dump"
        _db.backup_to(dest)
    for old in sorted(backup_dir.glob("bot-*"), reverse=True)[keep:]:
        old.unlink(missing_ok=True)
    return f"备份完成：{dest}"


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


def cmd_pg_guard(notify: bool = False):
    """PG 故障守护：检查 PostgreSQL 健康，离线时写审计并可选播报。

    返回 (退出码, 文本)，供 cron 使用：0=正常，1=故障。
    """
    import os
    _capability, _db, _shared = _plugins()
    if os.getenv("YUNO_DB_BACKEND", "postgresql").strip().lower() == "sqlite":
        return 0, "当前后端为 SQLite，无需 PG 守护。"
    try:
        pg = _db.health()
    except Exception as e:
        pg = {"ok": False, "error": str(e)}
    ts = datetime.now().isoformat(timespec="seconds")
    if pg.get("ok"):
        _db.kv_set("memory", "pg_health", {"ok": True, "ts": ts, "detail": pg.get("version", "")[:200]})
        return 0, f"PostgreSQL [在线] {pg.get('version', '')[:60]} 表数={pg.get('table_count')}"
    _db.kv_set("memory", "pg_health", {"ok": False, "ts": ts, "error": str(pg.get("error", ""))[:200]})
    _db.audit_add("pg_guard.fail", "PostgreSQL", str(pg.get("error", "连接失败"))[:300])
    text = f"PostgreSQL [离线] {pg.get('error', '')[:100]}"
    if notify:
        try:
            if target := _notify_group():
                _capability.notify_send("group", target, f"【PG 故障告警】\n{text}")
            else:
                text += "\n未配置播报目标群，跳过 QQ 通知。"
        except Exception as e:
            text += f"\n通知失败：{e}"
    return 1, text


def cmd_recover_drill() -> str:
    """恢复演练：不实际覆盖数据，只验证最新备份可读/可恢复。"""
    import os
    _capability, _db, _shared = _plugins()
    backup_dir = _shared.DATA_DIR / "backups"
    backups = sorted(backup_dir.glob("bot-*"), key=lambda x: x.stat().st_mtime, reverse=True)
    if not backups:
        return "没有可用备份，无法演练。"
    latest = backups[0]
    if latest.suffix == ".dump":
        import subprocess
        env = os.environ.copy()
        password = os.getenv("YUNO_PG_PASSWORD", "")
        if password:
            env["PGPASSWORD"] = password
        proc = subprocess.run(
            ["pg_restore", "--list", str(latest)],
            capture_output=True, text=True, env=env, timeout=60,
        )
        if proc.returncode != 0:
            return f"恢复演练失败：备份文件不可读。\n{proc.stderr[:500]}"
        entries = [ln for ln in proc.stdout.splitlines() if ln and not ln.startswith(";")]
        return f"恢复演练通过：{latest.name} 可读，包含 {len(entries)} 个对象。"
    import sqlite3
    conn = sqlite3.connect(f"file:{latest}?mode=ro", uri=True)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
    finally:
        conn.close()
    if row and row[0] == "ok":
        return f"恢复演练通过：{latest.name} SQLite 完整性检查 OK。"
    return f"恢复演练失败：{latest.name} SQLite 完整性检查异常：{row}"


def cmd_config_validate() -> tuple[int, str]:
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
        "active_edit", "convreview", "hesitation", "evidence_gate", "core_layer",
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
        return 0, "config-validate：全部通过"
    lines = ["config-validate 报告"]
    for e in errors:
        lines.append("ERROR " + e)
    for w in warnings:
        lines.append("WARN " + w)
    return (1 if errors else 0), "\n".join(lines)


def cmd_internal_db_prune(days: int = 30) -> str:
    """清理内部/测试 SQLite 中的过期记录。"""
    from plugins import _db_internal
    n = _db_internal.prune(days)
    return f"已清理 {n} 条内部测试记录（保留 {days} 天）"


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
    out_path = pathlib.Path(out or str(ROOT / "data" / f"export-{ts}.tar.gz"))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmpd:
        tmp = pathlib.Path(tmpd)
        db_path = tmp / "bot.db"
        _db.backup_to(db_path)
        data = _db.dump_all()
        (tmp / "data.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        table_sizes = {t: len(rows) for t, rows in data.items()}
        total_rows = sum(table_sizes.values())
        meta = {
            "version": "v12",
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "tables": table_sizes,
            "db_size": db_path.stat().st_size,
        }
        (tmp / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        for name in ("probes.json",):
            src = _shared.DATA_DIR / name
            if src.exists():
                shutil.copyfile(src, tmp / name)
        if with_config:
            shutil.copyfile(ROOT / "config.json", tmp / "config.json")
        with tarfile.open(out_path, "w:gz") as tar:
            for f in tmp.iterdir():
                tar.add(f, arcname=f.name)
    return f"已导出全量数据到 {out_path}（{total_rows} 行）"


def cmd_data_import(path: str, replace: bool = False, dry_run: bool = False) -> str:
    """导入数据（v12）：支持 .tar.gz / .json / .db；merge 合并，replace 覆盖。"""
    import tarfile
    import tempfile
    from plugins import _db
    path_obj = pathlib.Path(path)
    if not path_obj.exists():
        return f"文件不存在：{path_obj}"
    data, db_bytes = None, None
    if path_obj.suffix == ".json":
        data = json.loads(path_obj.read_text(encoding="utf-8"))
    elif path_obj.suffix == ".db":
        db_bytes = path_obj.read_bytes()
    elif path_obj.suffix == ".gz" or str(path_obj).endswith(".tar.gz"):
        with tempfile.TemporaryDirectory() as tmpd:
            tmp = pathlib.Path(tmpd)
            with tarfile.open(path_obj, "r:gz") as tar:
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
    def services_status(keyword: str | None = None):
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
    def audit_query(limit: int = 50, action: str | None = None):
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
    def memory_search(query: str, scope: str | None = None, key: str | None = None, limit: int = 10):
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
        adj: dict[str, list[str]] = {}
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


def cmd_persona_freshcheck(pack: str = "") -> tuple[int, str] | str:
    """新 pack 可迁移性验收（2026-08-15 起）：在完全干净的临时环境（空库+该 pack）
    跑核心链路——身份 stable 分类/core 升迁/常驻注入、记忆检索、约定（归属+内容+问句过滤）、
    防编造（来源声称硬门+会话内证据）、情绪词表、概念分类回归。
    子进程隔离：不碰当前库。离线（stub LLM），无网络依赖。"""
    import subprocess
    import sys
    import json as _json

    pack = (pack or "").strip()
    script = r"""
import os, sys, tempfile, json, types
tmp = tempfile.mkdtemp(prefix="yuno_fresh_")
cfg = {
    "memory": {"embedder": {"provider": "none"}, "core": {
        "enabled": True,
        "persona_pack": {"pack": "__PACK__"},
        "living": {"enabled": True, "bootstrap": True},
        "space": {"enabled": True},
        "mind": {"enabled": True},
    }}
}
cfg_path = os.path.join(tmp, "config.json")
with open(cfg_path, "w", encoding="utf-8") as f:
    json.dump(cfg, f)
os.environ["CONFIG_PATH"] = cfg_path
class _OpenAI:
    def __init__(self, *a, **k):
        self.chat = types.SimpleNamespace(completions=None)
stub = types.ModuleType("openai"); stub.OpenAI = _OpenAI
sys.modules["openai"] = stub
sys.path.insert(0, os.getcwd())
import memory
from plugins import _db, _shared
from memory import policy, context, reasoning, appointment as ap, analysis
from agent import evidence_gate as eg
scope = "c2c:fresh"
out = {"pack": "__PACK__", "data_dir": str(_shared.DATA_DIR)}
# 1) 身份：stable 分类 + core 升迁 + 常驻注入
identity = "我是本环境的测试用户，职业是程序员"
memory.add_fact(scope, "", identity, importance=0.9, confidence=0.85, source="user")
out["identity_stable"] = policy.fact_class(scope, "", identity) == "stable"
policy.promote_core(scope)
out["identity_injected"] = "程序员" in context.core_memory_block(scope)
# 2) 记忆检索
memory.add_fact(scope, "", "我养了一只布偶猫叫团子", importance=0.7, confidence=0.8, source="user")
hits = reasoning.retrieve("我的猫叫什么", [scope], top_k=3, min_score=0.0)
out["retrieve"] = bool(hits) and "团子" in hits[0][0]
# 3) 约定：with_ai + content + 问句过滤
r = ap.extract(scope, "我们明天晚上一起打游戏吧")
a = r.get("appointment") or {}
out["appt_with_ai"] = r.get("added") == 1 and a.get("with_ai") is True and a.get("content") == "打游戏"
out["appt_question_skip"] = ap.extract(scope, "你记得我们约了什么吗").get("skipped") is not None
ap.clear_scope(scope)
# 4) 防编造：来源声称硬门 + 会话内证据
out["gate_source_claim"] = eg.contains_unsupported_claim(
    "你说过喜欢蓝色", evidence=["我养了一只布偶猫叫团子"], banned=[]) is not None
out["gate_session_evidence"] = eg.contains_unsupported_claim(
    "对，猫叫团子", evidence=["我养了一只布偶猫叫团子"], banned=[]) is None
# 5) 情绪词表（通用）
out["emotion_fear"] = analysis.analyze("好可怕").get("emotion") == "恐惧"
out["emotion_disgust"] = analysis.analyze("恶心死了").get("emotion") == "厌恶"
# 6) 概念分类回归
out["class_process"] = policy.fact_class(scope, "", "今天工作很累") == "process"
out["class_stable"] = policy.fact_class(scope, "", "我在腾讯工作") == "stable"
print(json.dumps(out, ensure_ascii=False, indent=2))
""".replace("__PACK__", pack or "yuno")
    r = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, cwd=str(ROOT), timeout=180,
    )
    if r.returncode != 0:
        return f"验收失败：{r.stderr[-500:]}"
    try:
        res = _json.loads(r.stdout[r.stdout.index("{"):])
    except Exception as e:
        return f"解析失败：{e}\n{r.stdout[-300:]}"
    fails = [k for k, v in res.items() if v is False]
    lines = [f"  {k}: {v}" for k, v in res.items() if k not in ("pack", "data_dir")]
    summary = f"pack={res.get('pack')} · 全过 {len(res) - 2 - len(fails)}/{len(res) - 2}"
    text = (summary + (" · 失败项: " + ", ".join(fails) if fails else "") + "\n" + "\n".join(lines))
    return (1 if fails else 0), text  # CI 门控：有失败项退出码 1
