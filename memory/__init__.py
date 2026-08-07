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
    backfill,
    character,
    context,
    controller,
    embedder,
    expression,
    extract,
    graph,
    lexical,
    policy,
    reflect,
    relationship,
    reasoning,
    sensitive,
    topic,
    trace,
    update,
    vecindex,
    world,
)
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
world_snapshot = world.snapshot
world_stats = world.stats
goal_add = advisor.goal_add
goal_list = advisor.goal_list
goal_update = advisor.goal_update
goal_active = advisor.goal_active
consult_turn = advisor.consult_turn
consult_active = advisor.consult_active
consult_status = advisor.consult_status
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
