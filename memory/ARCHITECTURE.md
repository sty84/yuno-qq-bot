# Memory / Persona / Agent 架构文档

## 总览

```
QQ bot / Hermes / API
        │ agent.ask（分析 → 会话 → 场景 → 人格 → 记忆 → LLM → 学习）
        ▼
  ┌──────────────────────── 记忆核心 memory/ ────────────────────────┐
  │ 写入：analysis → classify → extract → controller.ingest         │
  │        → 多库（memories 统一表 / attrs / topics / events /      │
  │           bm25 / vec_index）+ 可信度 + 隐私                      │
  │ 检索：query(理解/指代/同义) → 7 路算法 → RRF → rerank → MMR      │
  │       → 缓存 → 预算分级注入 → 查询日志/评测                       │
  │ 成长：grow = 回填 + 巩固 + 反思 + 遗忘 + 会话/实体/关系维护       │
  └──────────────────────────────────────────────────────────────────┘
```

## 模块职责

| 模块 | 职责 |
|---|---|
| `analysis` | 意图/情绪/重要度/可信度/玩笑/纠错 |
| `classify` | 事实路由：词法/向量/图谱/属性/议题 |
| `extract` | LLM 事实提取 + 事件类型 + 分词词元 |
| `tokenize` | jieba（可选）→ 英文词 + 中文二元组 |
| `bm25` | 真分词 BM25 倒排（SQLite 持久化） |
| `lexical` | BM25 主通道 + FTS/LIKE 兜底 |
| `vecindex` | 自研 IVF（k-means + nprobe）；backend 可换 sqlite_vec/FAISS |
| `graph` | 事件图 + follows 时间线 + 加权邻居 + 最短路径 + 关系类型 |
| `rules` | 模板直查（"你喜欢什么"→属性表） |
| `topic` | 大类→议题→参数（fact/mood/playful） |
| `query` | 实体/时间/意图理解 + 指代消解 + 同义扩展 |
| `reasoning` | 7 路 RRF 融合 + 重排 + MMR + 缓存 + 分数归因 |
| `rerank` | light（精确+覆盖率+议题）/ cross（CrossEncoder）/ llm |
| `policy` | 访问计数/重要度/遗忘曲线/短期→长期巩固/确认反驳 |
| `bayes` | 置信度贝叶斯更新 |
| `calibrate` | 置信度标定（评测集分桶） |
| `update` | 相似合并/刷新/废弃/公开 |
| `reflect` | belief 审查（接受/改写/驳回）+ 版本日志 + 回滚 |
| `sensitive` | 敏感信息检测（隐私分） |
| `crypto` | 可选字段加密（MEMORY_KEY + cryptography） |
| `session` | 会话窗口 + 跨天同主题续接 |
| `entity` | 实体归一（canonical + 别名 + 事件挂实体） |
| `eval` | recall@k / MRR / nDCG + 分类分桶 |
| `backfill` | run() 结构化报告：回填/巩固/反思/遗忘/维护 |

## 数据表（单一 bot.db）

- `memories`：统一记忆（用户 + AI 同格式），含 confidence/source/audience/speaker/mclass/arousal/valence/privacy
- `memory_attrs`：结构化属性；`topics` + `topic_params`：议题；`events` + `event_relations`：图
- `bm25_terms` / `bm25_docs`：BM25 倒排；`vec_index` / `vec_centroids`：IVF 向量索引
- `memory_meta`：策略元数据；`belief_log`：成长版本日志
- `query_log`：查询遥测（导出评测集）；`sessions`：会话
- `entities` / `entity_aliases` / `entity_events`：实体归一

## 关键接口契约

```python
memory.retrieve(query, scopes, top_k=5, min_score=0.25, extra_scopes=None,
                expand_query=False, recent=None) -> [(fact, score, scope)]
memory.retrieve_detailed(...) -> [{fact, scope, score, rrf, policy, confidence, rerank}]
memory.ingest(scope, key, text, reply="", facts=None, confidence=None, source=None) -> dict
memory.assemble_context(query, scopes, ..., budget=None, expand_query=False, recent=None) -> str
memory.backfill_run(batch=64) -> dict   # grow 报告
memory.run_eval(probes, k=5) -> dict    # recall/MRR/nDCG/分类
```

## 配置（config.json → memory.core）

`weights`（7 路）、`vector_index{nlist,nprobe}`、`context_budget_chars`、`rerank{mode,cross_model}`、
`mmr`、`cache`、`telemetry{query_log}`、`session{window_min,llm}`、`reflection{enabled,llm}`、
`policy`（LR/遗忘/巩固阈值）、`query{rewrite_llm,multi_query}`。

## 运维

```bash
python tools.py memory-embed         # 回填 + 建索引 + 巩固 + 反思
python tools.py memory-grow          # 成长报告（JSON）
python tools.py memory-eval --file probes.json --save
python tools.py memory-probes        # 查询日志 → 评测集
python tools.py memory-calibrate --file probes.json
python tools.py memory-index --tune --file probes.json
python tools.py memory-clear-user <uid>   # 按用户删除（隐私权）
python tools.py memory-sessions / memory-topics / memory-route
```

## 隐私模型

`audience ∈ private / group:<gid> / public` + `privacy ∈ 0~1`：
检索按场景过滤（私聊不进群聊，除非 public）；`privacy ≥ 0.8` 的记忆加密存储且不进索引
（仅 `/我的记忆` 可见）；QQ 端 `/忘记 X`（废弃）、`/公开 X`（允许跨场景）。

## 已知边界

- 单 SQLite 单进程：万级记忆以内够用；更大需迁 sqlite-vec/FAISS + 独立索引服务；
- 置信度先验是启发式，需 `memory-calibrate` 用真实评测集标定；
- LLM 查询改写 / CrossEncoder 重排 / LLM 反思默认关闭，按成本按需开启。
