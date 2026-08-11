"""Memory Core 模块：替代原 rag/，实现
分析（analysis）→ 提取（extract）→ 事件图（graph）→ 融合检索（reasoning）
→ 策略（policy）→ 上下文组装（context）→ 回填巩固（backfill）。

对外统一入口（与旧 rag 兼容）：
  memory.embed(texts)                批量向量化（未配置返回 None）
  memory.embed_enabled()             是否启用 embedding
  memory.cosine(a, b)                余弦相似度
  memory.retrieve(query, scopes, top_k, min_score)  融合检索 → [(fact, score, scope)]
  memory.search(query, scope, key, limit)           统一记忆检索（Hermes MCP 用）
  memory.backfill(batch)              向量/事件图/巩固/修剪 全量回填

新增能力：
  memory.analyze(text)                意图/状态/重要度分析
  memory.ingest(scope, key, text, reply, facts)     控制器主入口
  memory.add_fact(scope, key, fact)   单条写入（含事件图与策略）
  memory.assemble_context(query, scopes)            组装 LLM 上下文
  memory.ai_memory_rows/set/clear     AI 自身记忆
"""

from memory import (
    advisor,
    analysis,
    appointment,
    backfill,
    character,
    context,
    controller,
    embedder,
    emotion,
    environment,
    expression,
    extract,
    graph,
    interaction,
    lexical,
    living,
    mistake,
    mind,
    policy,
    procedures,
    relationship,
    reasoning,
    schedule,
    sharing,
    sleep,
    space_eval,
    space,
    subjects,
    time_eval,
    time_extract,
    topic,
    trace,
    tz,
    vecindex,
    world,
)

# v31.3 模块合并别名：外部 `from memory import X` 继续可用
reflect = advisor
sensitive = controller
update = controller
weather = environment
from plugins import _db
from memory import backfill as backfill_mod

# session 会话管理已并入 controller（保留别名，兼容 from memory import session）
session = controller
# 兼容别名：bayes/rules 已并入 policy/lexical
bayes = policy
rules = lexical
query = extract  # 查询理解已并入 extract（兼容 memory.query.expand/understand）
rerank = reasoning  # 重排已并入 reasoning（兼容 memory.rerank.mmr/light_rerank）

# 兼容旧 rag 接口
embed = embedder.embed
embed_enabled = embedder.enabled
cosine = embedder.cosine
retrieve = reasoning.retrieve
search = reasoning.search
backfill = backfill_mod.backfill

# Memory Core 新增接口
analyze = analysis.analyze
ingest = controller.ingest
add_fact = controller.add_fact
message_gain = controller.message_gain
expression_analyze = expression.analyze
expression_profile = expression.profile_get
expression_update = expression.profile_update
expression_describe = expression.describe
trace_record = trace.record
trace_rows = _db.trace_rows
trace_markdown = trace.render_markdown
trace_prune = trace.prune
trace_score = trace.score
trace_adjustments = trace.adjustments
user_tz = tz.user_tz
tz_detect = tz.detect
appointment_extract = appointment.extract
appointment_check = appointment.check_and_poke
appointment_context = appointment.context_block
mistake_process = mistake.process
mistake_context = mistake.context_block
mistake_record = mistake.record
mistake_forgive_probability = mistake.forgive_probability
world_snapshot = world.snapshot
world_stats = world.stats
sleep_run = sleep.night_run
dream_context = sleep.context_block
schedule_block = schedule.block
schedule_today = schedule.today_summary
weather_fetch = weather.fetch
env_block = environment.block
env_snapshot = environment.snapshot
sharing_drive = sharing.drive_all
sharing_desire = sharing.desire
emotion_judge = emotion.judge
emotion_eval = emotion.eval_probes
emotion_log_rows = emotion.emotion_log_rows
living_block = living.home_block
travel_time = living.travel_time
item_give = living.give
item_take = living.take
item_find = living.find
item_history = living.item_history
item_position_at = living.position_at
item_activation = living.activation
item_where = living.where_is_block
item_search_progress = living.search_progress
space_position = space.position
space_events = space.today_events
travel_between = space.travel_between
space_room_position = space.room_position
space_room_now = space.room_now
space_move_room = space.move_room
space_route = space.shortest_route
space_cast_location = space.cast_location
interaction_modulate = interaction.modulate
mind_snapshot = mind.snapshot
mind_block = mind.block
mind_intention = mind.intention_current
mind_recompute = mind.recompute_intention
procedures_stats = procedures.stats
space_eval_run = space_eval.run
time_extract_extract = time_extract.extract
time_label = time_extract.label_for
time_eval_run = time_eval.run
subjects_eval_run = subjects.eval_run
retrieve_subject = reasoning.retrieve_subject
npc_memory_block = context.npc_memory_block
subjects_registered = subjects.registered
subjects_detect = subjects.detect
subjects_scope_of = subjects.scope_of
consistency_reconcile = controller.reconcile
consistency_pending = controller.reconcile_pending
goal_add = advisor.goal_add
goal_list = advisor.goal_list
goal_update = advisor.goal_update
goal_active = advisor.goal_active
consult_turn = advisor.consult_turn
consult_active = advisor.consult_active
consult_status = advisor.consult_status
consult_related = advisor.consult_related
consult_abort = advisor.consult_abort
assemble_context = context.assemble_context
merge_facts = controller.merge_facts
nice_fact = extract.nice_fact
extract_facts = extract.extract_facts
classify_event_type = extract.classify_event_type

# AI 自身记忆 / 事件图
def ai_memory_rows(kind=None, limit=None):
    """AI 自身记忆（统一格式：scope='ai'，key=kind，含可信度）。"""
    rows = _db.memory_rows("ai")
    if kind:
        rows = [r for r in rows if r["key"] == kind]
    rows.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    out = [
        {
            "kind": r["key"] or "experience",
            "content": r["fact"],
            "confidence": float(r.get("confidence", 0.7)),
            "updated_at": r.get("updated_at"),
        }
        for r in rows
    ]
    return out[:limit] if limit else out


def ai_memory_set(kind, content, confidence=0.7, embedding=None, updated_at=""):
    """写 AI 自身记忆（与用户记忆同一张 memories 表）。"""
    _db.memory_add("ai", kind, content, updated_at, embedding, confidence)


def ai_memory_clear(kind=None):
    if kind:
        _db.memory_clear("ai", kind)
    else:
        _db.memory_clear("ai")


event_rows = _db.event_rows
relations_for = _db.relations_for
timeline = graph.timeline
ancestors = graph.ancestors
descendants = graph.descendants
explain = reasoning.explain
confirm = policy.confirm
dispute = policy.dispute

# 多算法 / 查询理解 / 路由 / 记忆更新 / 评估
understand = extract.understand
route = analysis.route_fact
refresh = update.refresh
supersede = update.supersede
run_eval = backfill_mod.eval_run
eval_report = backfill_mod.eval_report
backfill_run = backfill_mod.run
route_stats = reasoning._route_stats
publicize = update.publicize
forget = policy.forget
promote = policy.promote
governance_report = policy.governance
topic_search = topic.search
topic_list = topic.list_topics
topic_package = topic.package
topic_build = topic.build
index_vectors = vecindex.build
vector_index = vecindex
bm25_search = lexical.bm25_search
rerank_light = reasoning.light_rerank
reflect_beliefs = reflect.reflect_beliefs
belief_log = _db.belief_log_rows
tokenize_text = extract.tokenize
eval_run_file = backfill_mod.eval_run_file
shortest_path = graph.shortest_path
vec_tune = vecindex.tune
retrieve_detailed = reasoning.retrieve_detailed
calibrate_train = policy.calibrate_train
calibrate_adjust = policy.calibrate_adjust
calibrate_report = policy.calibrate_report
relationship_update = relationship.update
relationship_describe = relationship.describe
relationship_rows = relationship.rows
relationship_score = relationship.score_of
history_rows = _db.history_rows
feedback_rows = _db.feedback_rows
policy_log_rows = _db.policy_log_rows
character_build = character.build
character_search = character.search
character_list = character.list_names
character_match = character.match_scopes
character_md_path = character.md_path
character_render_md = character.render_markdown
character_write_md = character.write_markdown
character_sync = character.sync_from_markdown
sensitive_detect = sensitive.detect
crypto_available = sensitive.available
session_touch = controller.touch
session_current = controller.current
session_rows = _db.session_rows
entity_build = graph.build_entities
rollback_belief = reflect.rollback_belief
purge_scope = _db.purge_scope
query_log_pending = _db.query_log_pending
backend_name = vecindex.backend_name


def stats() -> dict:
    """记忆系统概览（调试/后台用）。"""
    return {
        "memories": len(_db.memory_rows()),
        "events": len(_db.event_rows()),
        "ai_memory": len(_db.memory_rows("ai")),
        "attrs": len(_db.attr_rows()),
    }


def flush_caches():
    """优雅关闭：把需要落盘的内存统计刷进 kv（性能缓存丢失无影响）。"""
    try:
        import memory.stats as stats_mod
        stats_mod.flush()  # 计数器缓冲落盘（v2.2）
    except Exception as e:
        _stats_err(e)
        pass
    try:
        reasoning._flush_route_stats()
    except Exception as e:
        _stats_err(e)
        pass



def _stats_err(e):
    """裸 except 审计（v2.2）：错误计数 + 日志，供消融/排查。"""
    try:
        import memory.stats as _st
        _st.bump_err("__init__", e)
    except Exception:
        pass
