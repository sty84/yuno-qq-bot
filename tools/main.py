"""argparse entrypoint for YUNO CLI tools (split from tools.py)."""

import argparse

from tools.admin import (
    cmd_backup,
    cmd_config_validate,
    cmd_data_dump_json,
    cmd_data_export,
    cmd_data_import,
    cmd_floorplan_render,
    cmd_health,
    cmd_internal_db_prune,
    cmd_mcp,
    cmd_persona_freshcheck,
    cmd_persona_smoke,
    cmd_persona_switch,
    cmd_pg_guard,
    cmd_recover,
    cmd_recover_drill,
)
from tools.core import _emit
from tools.eval import (
    cmd_ablation,
    cmd_emotion_eval,
    cmd_eval_dataset_save,
    cmd_evidence_gate_eval,
    cmd_experiments,
    cmd_memory_eval,
    cmd_reply_check,
    cmd_scenario_eval,
    cmd_space_eval,
    cmd_subjects_eval,
    cmd_time_eval,
)
from tools.memory import (
    cmd_appointment_clean,
    cmd_bandit_status,
    cmd_calendar_check,
    cmd_calibrate_feedback,
    cmd_character_build,
    cmd_character_sync,
    cmd_conflict_scan,
    cmd_consistency_eval,
    cmd_consult,
    cmd_emotion_log,
    cmd_emotion_train,
    cmd_expression,
    cmd_goal,
    cmd_init,
    cmd_living_bootstrap,
    cmd_memory_calibrate,
    cmd_memory_clear_user,
    cmd_memory_consolidate,
    cmd_memory_conv_adjust,
    cmd_memory_conv_md,
    cmd_memory_conv_report,
    cmd_memory_conv_review,
    cmd_memory_embed,
    cmd_memory_feedback,
    cmd_memory_governance,
    cmd_memory_grow,
    cmd_memory_history,
    cmd_memory_index,
    cmd_memory_merge,
    cmd_memory_probes,
    cmd_memory_route,
    cmd_memory_sessions,
    cmd_memory_sleep,
    cmd_memory_source_backfill,
    cmd_memory_topics,
    cmd_memory_trace,
    cmd_memory_trace_adjust,
    cmd_memory_trace_md,
    cmd_memory_trace_review,
    cmd_mind_status,
    cmd_persona_probes,
    cmd_policy_classify,
    cmd_pollution_scan,
    cmd_procedures_list,
    cmd_reflection_report,
    cmd_reflection_stats,
    cmd_relationship,
    cmd_revive_status,
    cmd_subjects_status,
    cmd_topic_vad_backfill,
    cmd_world,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="YUNO 2.0 运维/入口工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("health", help="独立健康检查")
    p.add_argument("--notify", action="store_true", help="状态变化时播报到 QQ")
    p.set_defaults(func=lambda a: _emit(cmd_health(a.notify)) or 0)

    sub.add_parser("backup", help="每日 SQLite 备份").set_defaults(
        func=lambda a: _emit(cmd_backup()) or 0
    )

    p = sub.add_parser("recover", help="一键恢复服务")
    p.add_argument("--notify", action="store_true", help="结果播报到 QQ")
    p.set_defaults(func=lambda a: cmd_recover(a.notify))

    p = sub.add_parser("pg-guard", help="PG 故障守护：检查健康，离线时审计/可选播报")
    p.add_argument("--notify", action="store_true", help="故障时播报到 QQ")
    p.set_defaults(func=lambda a: _emit(cmd_pg_guard(a.notify)) or 0)

    p = sub.add_parser("recover-drill", help="恢复演练：验证最新备份可读，不覆盖数据")
    p.set_defaults(func=lambda a: _emit(cmd_recover_drill()) or 0)

    p = sub.add_parser("memory-embed", help="为缺少向量的记忆回填 embedding")
    p.add_argument("--batch", type=int, default=64)
    p.set_defaults(func=lambda a: _emit(cmd_memory_embed(a.batch)) or 0)

    p = sub.add_parser("memory-consolidate", help="记忆整合：碎片合并+冲突处理+巩固/遗忘")
    p.add_argument("--scope", default="", help="限定 scope")
    p.add_argument("--dry-run", action="store_true", help="只报告不执行")
    p.set_defaults(func=lambda a: _emit(cmd_memory_consolidate(a.scope, a.dry_run)) or 0)

    p = sub.add_parser("memory-grow", help="成长/维护：向量+事件图+巩固+修剪+词法索引")
    p.add_argument("--dry-run", action="store_true", help="只出统计不写库")
    p.set_defaults(func=lambda a: _emit(cmd_memory_grow(a.dry_run)) or 0)

    p = sub.add_parser("memory-sleep", help="睡眠/梦境：浅睡+深睡巩固当天对话，REM 做梦")
    p.add_argument("--force", action="store_true", help="强制再跑一夜（跳过当日已睡检查）")
    p.set_defaults(func=lambda a: _emit(cmd_memory_sleep(a.force)) or 0)

    p = sub.add_parser("memory-eval", help="评测召回率/MRR（--file 或 --dataset）")
    p.add_argument("--file", default="", help="评测集 JSON 路径")
    p.add_argument("--dataset", default="", help="命名评测集（memory-eval-dataset 保存）")
    p.add_argument("--k", type=int, default=5)
    p.add_argument("--save", action="store_true", help="把结果存为 baseline")
    p.set_defaults(func=lambda a: _emit(cmd_memory_eval(a.file, a.k, a.save, a.dataset)) or 0)

    p = sub.add_parser("memory-eval-dataset", help="保存命名评测集（版本对比用）")
    p.add_argument("name", help="评测集名称（如 v1）")
    p.add_argument("--file", required=True, help="评测集 JSON 路径")
    p.set_defaults(func=lambda a: _emit(cmd_eval_dataset_save(a.name, a.file)) or 0)

    p = sub.add_parser("memory-route", help="诊断：显示一条消息的分类路由")
    p.add_argument("text")
    p.set_defaults(func=lambda a: _emit(cmd_memory_route(a.text)) or 0)

    p = sub.add_parser("memory-topics", help="列出议题（大类→议题→参数）")
    p.add_argument("--scope", default="", help="限定场景，如 c2c:xxx")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=lambda a: _emit(cmd_memory_topics(a.scope, a.limit)) or 0)

    p = sub.add_parser("memory-index", help="重建/调优自研 IVF 向量索引")
    p.add_argument("--tune", action="store_true", help="用评测集做 nlist/nprobe 对照实验")
    p.add_argument("--file", default="", help="评测集 JSON 路径（--tune 时必填）")
    p.add_argument("--nlist", default="4,8,16", help="候选 nlist 列表")
    p.add_argument("--nprobe", default="1,2,4", help="候选 nprobe 列表")
    p.set_defaults(func=lambda a: _emit(cmd_memory_index(a.tune, a.file, a.nlist, a.nprobe)) or 0)

    p = sub.add_parser("memory-clear-user", help="按用户彻底清除记忆（隐私权）")
    p.add_argument("uid")
    p.set_defaults(func=lambda a: _emit(cmd_memory_clear_user(a.uid)) or 0)

    p = sub.add_parser("memory-probes", help="把查询日志导出为评测集")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--out", default="", help="输出路径（默认 DATA_DIR/probes.json，即 persona-<pack> 活库）")
    p.set_defaults(func=lambda a: _emit(cmd_memory_probes(a.limit, a.out)) or 0)

    p = sub.add_parser("persona-probes", help="从 persona 记忆自动生成评测探针（换人设后无需手写）")
    p.add_argument("--out", default="", help="输出路径（默认 DATA_DIR/probes_persona.json）")
    p.set_defaults(func=lambda a: _emit(cmd_persona_probes(a.out)) or 0)

    p = sub.add_parser("init", help="一键初始化新实例：播种人设+向量化+建索引+生成评测集+落基线")
    p.add_argument("--pack", default="", help="Persona Pack 名（默认 config 里的 persona_pack）")
    p.set_defaults(func=lambda a: _emit(cmd_init(a.pack)) or 0)

    p = sub.add_parser("memory-merge", help="时序引导碎片合并：同一时间窗口内的孤立短事实合并成完整事实")
    p.add_argument("--scope", default="", help="限定 scope（默认全部）")
    p.add_argument("--window", type=int, default=10, help="时间窗口（分钟），默认 10")
    p.set_defaults(func=lambda a: _emit(cmd_memory_merge(a.scope, a.window)) or 0)

    p = sub.add_parser("memory-calibrate", help="用评测集训练置信度标定")
    p.add_argument("--file", required=True, help="评测集 JSON 路径")
    p.add_argument("--k", type=int, default=5)
    p.set_defaults(func=lambda a: _emit(cmd_memory_calibrate(a.file, a.k)) or 0)

    sub.add_parser("config-validate", help="校验 config.json：未知段/类型错误/取值越界").set_defaults(
        func=lambda a: _emit(cmd_config_validate()) or 0
    )
    sub.add_parser("evidence-gate-eval", help="证据门控评测：准确率 + 错误明细").set_defaults(
        func=lambda a: _emit(cmd_evidence_gate_eval()) or 0
    )


    p = sub.add_parser("emotion-eval", help="情绪判断评测：分类准确率 + VAD MAE")
    p.add_argument("--file", default="", help="评测集 JSON（默认 data/emotion_probes.json）")
    p.set_defaults(func=lambda a: _emit(cmd_emotion_eval(a.file)) or 0)

    p = sub.add_parser("emotion-log", help="导出情绪判断日志（训练数据原料）")
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--out", default="", help="输出 jsonl 路径（不填只打印条数）")
    p.set_defaults(func=lambda a: _emit(cmd_emotion_log(a.days, a.out)) or 0)

    p = sub.add_parser("emotion-train", help="训练本地情绪分类器（bge-large 编码 + 逻辑回归）")
    p.add_argument("--file", required=True, help="标注训练集 JSON：[{text, emotion}]")
    p.add_argument("--out", default="", help="输出 pickle 路径（默认 DATA_DIR/models/emotion_clf.pkl）")
    p.set_defaults(func=lambda a: _emit(cmd_emotion_train(a.file, a.out)) or 0)

    p = sub.add_parser("memory-sessions", help="查看会话")
    p.add_argument("--scope", default="")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=lambda a: _emit(cmd_memory_sessions(a.scope, a.limit)) or 0)

    p = sub.add_parser("memory-history", help="查看记忆变更历史（v3）")
    p.add_argument("--scope", default="", help="限定场景（如 c2c:xxx）")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=lambda a: _emit(cmd_memory_history(a.scope, a.limit)) or 0)

    p = sub.add_parser("memory-feedback", help="查看用户反馈日志（v3）")
    p.add_argument("--scope", default="", help="限定场景（如 c2c:xxx）")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=lambda a: _emit(cmd_memory_feedback(a.scope, a.limit)) or 0)

    p = sub.add_parser("relationship", help="查看 AI 与用户关系状态（v3）")
    p.add_argument("--scope", default="", help="限定场景（如 c2c:xxx），省略则列出全部")
    p.set_defaults(func=lambda a: _emit(cmd_relationship(a.scope)) or 0)

    p = sub.add_parser("memory-governance", help="Memory Governance 报告（v3.1 §9）")
    p.add_argument("--scope", default="")
    p.set_defaults(func=lambda a: _emit(cmd_memory_governance(a.scope)) or 0)

    p = sub.add_parser("memory-trace", help="导出记忆处理轨迹 JSON（v10）")
    p.add_argument("--scope", default="")
    p.add_argument("--since", default="", help="起始时间，如 2026-08-01")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--out", default="", help="输出文件路径")
    p.set_defaults(func=lambda a: _emit(cmd_memory_trace(a.scope, a.since, a.limit, a.out)) or 0)

    p = sub.add_parser("memory-trace-md", help="导出记忆处理轨迹 Markdown（v10）")
    p.add_argument("--scope", default="")
    p.add_argument("--since", default="")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=lambda a: _emit(cmd_memory_trace_md(a.scope, a.since, a.limit)) or 0)

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
        func=lambda a: _emit(
            cmd_memory_trace_review(
                a.trace_id, a.extraction, a.decision, a.confidence, a.provenance, a.privacy,
                a.comment, a.reviewer,
            )
        )
        or 0
    )

    p = sub.add_parser("memory-trace-adjust", help="查看评分驱动的行为调整（v11）")
    p.set_defaults(func=lambda a: _emit(cmd_memory_trace_adjust()) or 0)

    p = sub.add_parser("memory-conv-md", help="导出对话评分报告 Markdown（v33）")
    p.add_argument("--scope", default="")
    p.add_argument("--since", default="")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=lambda a: _emit(cmd_memory_conv_md(a.scope, a.since, a.limit)) or 0)

    p = sub.add_parser("memory-conv-review", help="人工评分对话（v33，五维 1~5）")
    p.add_argument("conv_id", type=int)
    p.add_argument("--remember", type=float, help="记得（引用历史不穿帮）1~5")
    p.add_argument("--natural", type=float, help="自然（像人话不机械）1~5")
    p.add_argument("--emotional", type=float, help="有情绪（情绪连贯）1~5")
    p.add_argument("--proactive", type=float, help="主动（会主动分享/推进）1~5")
    p.add_argument("--boundary", type=float, help="边界（不乱编不泄密）1~5")
    p.add_argument("--comment", default="")
    p.add_argument("--reviewer", default="")
    p.set_defaults(
        func=lambda a: _emit(
            cmd_memory_conv_review(
                a.conv_id, a.remember, a.natural, a.emotional, a.proactive, a.boundary,
                a.comment, a.reviewer,
            )
        )
        or 0
    )

    p = sub.add_parser("memory-conv-report", help="查看对话五维诊断（v33，只诊断不调参）")
    p.set_defaults(func=lambda a: _emit(cmd_memory_conv_report()) or 0)
    sub.add_parser("reflection-stats", help="反思质量统计：产出/过滤/写入").set_defaults(
        func=lambda a: _emit(cmd_reflection_stats()) or 0
    )
    p = sub.add_parser("internal-db-prune", help="清理内部/测试 SQLite 过期记录")
    p.add_argument("--days", type=int, default=30)
    p.set_defaults(func=lambda a: _emit(cmd_internal_db_prune(a.days)) or 0)

    p = sub.add_parser("reflection-report", help="反思抽检报告：最近写入内容 + 质量统计")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=lambda a: _emit(cmd_reflection_report(a.limit)) or 0)

    p = sub.add_parser("memory-conv-adjust", help="对话评分调参框架：查看/应用/回滚")
    p.add_argument("--apply", action="store_true", help="把建议写入 kv（auto_adjust=false 时仅 dry-run）")
    p.add_argument("--rollback", action="store_true", help="清除已写入的调参记录")
    p.set_defaults(func=lambda a: _emit(cmd_memory_conv_adjust(a.apply, a.rollback)) or 0)



    p = sub.add_parser("data-export", help="全量数据打包导出（v12）")
    p.add_argument("--out", default="")
    p.add_argument("--with-config", action="store_true", help="附带 config.json")
    p.set_defaults(func=lambda a: _emit(cmd_data_export(a.out, a.with_config)) or 0)

    p = sub.add_parser("data-import", help="导入数据（v12）")
    p.add_argument("file")
    p.add_argument("--replace", action="store_true", help="覆盖目标表（或替换整个库）")
    p.add_argument("--dry-run", action="store_true", help="只预览不写入")
    p.set_defaults(func=lambda a: _emit(cmd_data_import(a.file, a.replace, a.dry_run)) or 0)

    p = sub.add_parser("data-dump-json", help="全表 JSON 转储（v12）")
    p.add_argument("--out", default="")
    p.set_defaults(func=lambda a: _emit(cmd_data_dump_json(a.out)) or 0)

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
        func=lambda a: _emit(
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
    p.set_defaults(func=lambda a: _emit(cmd_consult(a.text, a.scope)) or 0)

    p = sub.add_parser("expression", help="表达分析 + 用户画像（v7）")
    p.add_argument("text")
    p.add_argument("--scope", default="cli")
    p.set_defaults(func=lambda a: _emit(cmd_expression(a.text, a.scope)) or 0)

    p = sub.add_parser("world", help="用户中心世界模型快照（v8）")
    p.add_argument("--scope", default="cli")
    p.set_defaults(func=lambda a: _emit(cmd_world(a.scope)) or 0)

    p = sub.add_parser("character", help="输入人物名称，自动搜索设定/经历并存入记忆")
    p.add_argument("name", help="人物名称（如：千石由乃）")
    p.set_defaults(func=lambda a: _emit(cmd_character_build(a.name)) or 0)

    p = sub.add_parser("character-sync", help="把编辑后的 md 档案同步回记忆库")
    p.add_argument("arg", help="人物名或 md 文件路径")
    p.set_defaults(func=lambda a: _emit(cmd_character_sync(a.arg)) or 0)

    p = sub.add_parser("mind-status", help="心智状态快照（mind_state + 意图 + 程序记忆）")
    p.add_argument("--scope", default="", help="场景 scope（可选）")
    p.set_defaults(func=lambda a: _emit(cmd_mind_status(a.scope)) or 0)

    p = sub.add_parser("procedures-list", help="列出程序记忆（System 1 习惯）")
    p.set_defaults(func=lambda a: _emit(cmd_procedures_list()) or 0)

    p = sub.add_parser("living-bootstrap", help="人设→场景生成：按 persona 补齐家里物品（只新增不覆盖）")
    p.set_defaults(func=lambda a: _emit(cmd_living_bootstrap()) or 0)

    p = sub.add_parser("space-eval", help="空间评测：X在哪命中 / 时刻召回 / 找东西模拟")
    p.add_argument("--save", action="store_true", help="把结果存为 baseline")
    p.add_argument("--compare", action="store_true", help="与上次 baseline 对比")
    p.set_defaults(func=lambda a: _emit(cmd_space_eval(save=a.save, compare=a.compare)) or 0)

    p = sub.add_parser("floorplan-render", help="渲染平面图 SVG 预览 + 房间几何事实表")
    p.add_argument("--pack", default="", help="Persona pack 名（默认当前激活）")
    p.add_argument("--out", default="", help="SVG 输出路径（默认 data/floorplans/<pack>.svg）")
    p.set_defaults(func=lambda a: _emit(cmd_floorplan_render(a.pack, a.out)) or 0)

    p = sub.add_parser("time-eval", help="时间感知评测：时间段召回 / 时间线序列 / 日期精确度")
    p.add_argument("--save", action="store_true", help="把结果存为 baseline")
    p.add_argument("--compare", action="store_true", help="与上次 baseline 对比")
    p.set_defaults(func=lambda a: _emit(cmd_time_eval(save=a.save, compare=a.compare)) or 0)

    p = sub.add_parser("subjects-status", help="多主体记忆：列出已注册主体及各视角数据量")
    p.set_defaults(func=lambda a: _emit(cmd_subjects_status()) or 0)

    p = sub.add_parser("subjects-eval", help="多主体评测：写入成功 / 隐私门控 / 对话引用")
    p.add_argument("--save", action="store_true", help="把结果存为 baseline")
    p.add_argument("--compare", action="store_true", help="与上次 baseline 对比")
    p.set_defaults(func=lambda a: _emit(cmd_subjects_eval(save=a.save, compare=a.compare)) or 0)

    p = sub.add_parser("consistency-eval", help="双轨制一致性：失效队列长度 + 重算数")
    p.set_defaults(func=lambda a: _emit(cmd_consistency_eval()) or 0)

    p = sub.add_parser("policy-classify", help="事实分类探针：含关键词但其实是过程/指令的句子误判率")
    p.set_defaults(func=lambda a: _emit(cmd_policy_classify()) or 0)

    p = sub.add_parser("revive-status", help="主动消息决策：泊松概率 + 贝叶斯用户状态（只读）")
    p.add_argument("--scope", default="", help="用户 scope（如 c2c:xxx / group:xxx）")
    p.set_defaults(func=lambda a: _emit(cmd_revive_status(a.scope)) or 0)

    p = sub.add_parser("bandit-status", help="回应策略 bandit 后验：各策略均值 + 上次选择")
    p.add_argument("--scope", default="", help="用户 scope")
    p.set_defaults(func=lambda a: _emit(cmd_bandit_status(a.scope)) or 0)

    p = sub.add_parser("topic-vad-backfill", help="旧议题补近似 VAD/复合情绪（幂等）")
    p.set_defaults(func=lambda a: _emit(cmd_topic_vad_backfill()) or 0)

    p = sub.add_parser("memory-source-backfill", help="证据门控：历史记忆 source 归一（ingest→user / persona→pack）")
    p.set_defaults(func=lambda a: _emit(cmd_memory_source_backfill()) or 0)

    p = sub.add_parser("pollution-scan", help="存量污染扫描：source=user 记忆反向出处校验（--apply 执行降级/删除）")
    p.add_argument("--scope", default="", help="只扫该 scope（默认全部）")
    p.add_argument("--apply", action="store_true", help="执行降级（partial/weak→ai_edit）与删除（none）；默认 dry-run")
    p.set_defaults(func=lambda a: _emit(cmd_pollution_scan(a.scope, a.apply)) or 0)

    p = sub.add_parser("conflict-scan", help="存量矛盾扫描：同实体不同属性值冲突检测（--apply 降权+contested）")
    p.add_argument("--scope", default="", help="只扫该 scope（默认全部）")
    p.add_argument("--apply", action="store_true", help="低置信方降权并标 contested；默认 dry-run")
    p.set_defaults(func=lambda a: _emit(cmd_conflict_scan(a.scope, a.apply)) or 0)

    p = sub.add_parser("calendar-check", help="存量日历校验：'X号是周Y'事实与真实日历比对（--apply 降权）")
    p.add_argument("--scope", default="", help="只扫该 scope（默认全部）")
    p.add_argument("--apply", action="store_true", help="日历不符事实降权+contested；默认 dry-run")
    p.set_defaults(func=lambda a: _emit(cmd_calendar_check(a.scope, a.apply)) or 0)

    sub.add_parser("calibrate-feedback", help="校准闭环：用户纠错结论回流置信度标定").set_defaults(
        func=lambda a: _emit(cmd_calibrate_feedback()) or 0
    )

    p = sub.add_parser("appointment-clean", help="巡检清理：含黑名单词的约定条目标记 done（防催约复活编造）")
    p.set_defaults(func=lambda a: _emit(cmd_appointment_clean()) or 0)

    p = sub.add_parser("persona-smoke", help="Persona Pack 冒烟：加载校验 + 房间连通 + 模板渲染 + 硬编码扫描")
    p.set_defaults(func=lambda a: _emit(cmd_persona_smoke()) or 0)

    p = sub.add_parser("reply-check", help="回复质量评测：逐题真实 LLM 跑，--score 自动 rubric 判分")
    p.add_argument("--scope", default="", help="场景（c2c:xxx 或 group:xxx）")
    p.add_argument("--limit", type=int, default=0, help="只跑前 N 题")
    p.add_argument("--save", action="store_true", help="记录到 data/reply_eval_history.jsonl")
    p.add_argument("--score", action="store_true", help="LLM 四维 rubric 自动判分（准确/合理/人设/防编造）")
    p.set_defaults(func=lambda a: _emit(cmd_reply_check(a.scope, a.limit, a.save, a.score)) or 0)

    p = sub.add_parser("persona-freshcheck", help="新 pack 可迁移性验收：干净环境跑核心链路（身份/检索/约定/防编造/情绪）")
    p.add_argument("--pack", default="", help="pack 名（默认当前 config 的 pack）")
    p.set_defaults(func=lambda a: _emit(cmd_persona_freshcheck(a.pack)) or 0)

    p = sub.add_parser("persona-switch", help="切换 Persona Pack（校验 pack 文件并写 config）")
    p.add_argument("--pack", required=True, help="pack 名（personas/<名>/）")
    p.set_defaults(func=lambda a: _emit(cmd_persona_switch(a.pack)) or 0)

    p = sub.add_parser("ablation", help="机制消融矩阵：单开关 × probes，输出贡献表并写实验日志")
    p.add_argument("--save", action="store_true", help="把矩阵落成 ablation_baseline.json（第一次跑=改前基线）")
    p.set_defaults(func=lambda a: _emit(cmd_ablation(a.save)) or 0)

    p = sub.add_parser("experiments", help="实验日志：基线前后与回归标记")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=lambda a: _emit(cmd_experiments(a.limit)) or 0)

    p = sub.add_parser("scenario-eval", help="场景回放评分：重放多轮对话，--score 用 LLM 五维评分")
    p.add_argument("--file", default="", help="场景集 JSON（默认 data/eval/scenarios.json）")
    p.add_argument("--score", action="store_true", help="用 DeepSeek 按五维 rubric 打分")
    p.add_argument("--review-export", action="store_true", help="把回放对话写入 conv_log 供人工评分（v33）")
    p.set_defaults(func=lambda a: _emit(cmd_scenario_eval(a.file, a.score, a.review_export)) or 0)

    sub.add_parser("mcp", help="启动 MCP Server").set_defaults(func=lambda a: cmd_mcp())

    args = parser.parse_args()
    return args.func(args)
