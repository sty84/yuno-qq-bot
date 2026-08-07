# memory/ —— 统一记忆系统（Memory Core）

> 让 Agent 从"拥有知识库"升级为"拥有长期认知系统"。记忆不是聊天记录，而是
> 有**可信度演化、时间推理、遗忘巩固、纠错调查、人格与关系**的认知主体。

## 1. 框架总览

```mermaid
flowchart TD
    IN[输入: 用户消息 / AI回复 / 反馈] --> EXT[extract 两阶段提取]
    EXT --> AN[analysis 意图/情绪/重要度/表达]
    AN --> CTL[controller 决策: Consent/纠错调查/时间推理]
    CTL --> ST[memories 统一记忆表]
    ST --> IDX[索引: lexical BM25 / vecindex IVF / FTS / attrs]
    ST --> GR[graph 事件图+实体] & TP[topic 议题化]
    ST --> PL[policy 贝叶斯置信度/遗忘/巩固]
    Q[查询] --> RSN[reasoning 7路检索+RRF融合+重排+注意力]
    RSN --> CTX[context 预算分级注入] --> LLM[云端 LLM]
    LLM --> FB[反馈: 纠错/评分/关系证据]
    FB --> TR[trace 轨迹+五维评分] --> PL
    GR --> REF[reflect 反思] & REL[relationship 关系] & WD[world 世界模型]
```

## 2. 数据模型（SQLite 单库 data/bot.db）

核心是统一 `memories` 表：用户（`c2c:<uid>` / `group:<gid>` / `group_all:<gid>`）、
AI 自身（`ai` / `ai:<id>`）、人物档案（`char:<名>`）**同表同格式**。

| 表 | 作用 |
|---|---|
| `memories` | 唯一事实记忆：confidence/source/audience/privacy/valid_from/valid_to/status/embedding |
| `memory_meta` | 访问计数/最后访问（遗忘与巩固的输入） |
| `memory_attrs` | 结构化属性（偏好/家庭/健康…） |
| `events` / `event_relations` | 事件图：类型→事件→元素→关系/时间线 |
| `entities` / `entity_aliases` / `entity_events` | 实体归一与事件挂实体 |
| `topics` / `topic_params` | 议题化：大类→议题→参数（fact/mood/playful…） |
| `sessions` | 会话窗口+主题 |
| `goals` / `consultations` | 目标 / 决策咨询会话 |
| `relationships` | AI-用户关系状态机 |
| `feedback_log` / `memory_history` / `policy_log` | 反馈分级 / 记忆变更审计 / 策略日志 |
| `memory_trace` / `trace_review` | 记忆处理轨迹 / 五维人工评分 |
| `query_log` / `kv` | 查询遥测 / 标定·路由·baseline |
| `language_context` / `user_expression_profile` | 网络词候选池 / 用户表达画像 |

派生索引（`bm25_*` / `vec_index` / `memories_fts`）不参与导出，由 grow 重建。

## 3. 一条消息的完整旅程

```
用户消息
 → expression 语言语义层（网络词/玩笑概率/表达画像）
 → analysis 意图/情绪/重要度 + LLM 情绪兜底（节流）
 → controller.message_gain 信息增益评分（数字/专名/新事实/状态变化）
   低信息 → 缓存合并；高信息/纠错 → 立即处理
 → Consent：玩笑低信息不存、临时情绪不存、敏感加密
 → Temporal：状态变化（转/换/改用+专名）→ 旧记忆 superseded
 → 纠错调查：LLM（节流）判断 update/keep/uncertain
 → 两阶段提取（entities/attributes/facts）+ 无信息陈述过滤
 → 相似合并（merge 记历史）→ 存 memories + embedding
 → 事件图建边 + 议题化 + 四路索引同步 + 策略 touch
 → 关系证据更新 + 表达画像更新 + 轨迹记录
 → （检索时）7 路召回 → RRF → 重排 → 软置信度 → 预算注入 → LLM
 → （每日 grow）反思/巩固/遗忘/评测
```

## 4. 模块详解

### extract —— 提取与文本处理

- **作用**：从对话提取值得记住的事实；中文分词；查询理解。
- **算法**：两阶段提取——第一阶段 LLM 结构化抽取（entities/attributes/facts，不总结只提取、
  保留名字/数字/品种、禁止"X是人"无信息陈述）；第二阶段转成长期记忆事实；失败回退单段提取。
  分词优先 jieba，未装则中文相邻二元组（已修复 6 字截断 bug）。
  查询理解含指代消解（"那个项目"→前文实体）、同义扩展、**短查询前文补全**（错别字容错）。
- **为什么**：单段摘要会丢细节（"橘猫"变"狗"），结构化抽取 + 过滤能保真。

### analysis —— 状态分析

- **作用**：意图（求助/偏好/事件/回忆/情绪）、情绪、重要度、纠错信号、玩笑概率。
- **算法**：规则词表为快路径；规则判"平静且非玩笑"时节流调 LLM 兜底情绪；
  历史融合情绪（近 3 条 ≥2 消极 → 低落）只影响语气，不触发临时情绪拒绝。
- **为什么**：规则零成本覆盖常见场景，LLM 兜底补长尾，历史融合补"连续失败后该耐心"。

### controller —— 记忆控制器（决策核心）

- **作用**：决定是否保存、处理冲突、时间推理、合并更新、AI 经历沉淀。
- **算法**：
  - `message_gain` 信息增益评分 = 数字(+0.25) + 专名(+0.2) + 新颖度(+0.45×(1−重叠)) + 状态变化(+0.3)，替代固定 10 分钟节流
  - Consent：玩笑且低信息 → 不保存；文本规则情绪且无载体 → 不保存；敏感 → 加密
  - `_supersede_old` 时间推理：状态词 + 新旧都有专名 → 旧记忆 superseded（保留历史）
  - `_decay_conflicts` 纠错调查：特征词匹配 → 受限 LLM 判断 update（废弃）/ keep（保留）/ uncertain（降权+contested）
- **为什么**：让"存什么、改不改"成为可解释决策，而不是硬编码规则或 LLM 直接覆盖。

### graph / topic —— 事件图与议题化

- **事件图**：每个事实建事件，按语义相似建 related_to 边、按时间建 follows 边（事件树）；
  实体归一（canonical+别名），事件挂实体。支撑"为什么相关"的图谱召回与时间线。
- **议题化**：大类（规划/学习/项目/偏好…）→ 议题（实体+类别；无实体用"类别·摘要"语义聚类）→
  参数（fact/mood/playful/可信度）。检索时按议题打包注入。

### lexical / vecindex —— 检索通道

- **BM25**（真分词倒排，k1=1.5, b=0.75）：专名/数字的主通道；FTS5 trigram 兜底；LIKE 最后降级。
- **IVF 向量索引**（自研）：bge-small-zh-v1.5（512 维）→ kmeans 质心 → nprobe 探测打分；
  `memory-index --tune` 网格调 nlist/nprobe。
- **规则直查**：结构化属性问题（"喜欢什么"→preference）确定性命中。

### reasoning —— 融合推理

- **算法**：6 路候选（词法/向量/图谱/结构化/Rules/议题）→ RRF 融合（权重可配）→
  策略加权（重要度×时效）→ 置信度（含标定校准）→ 软过滤（min_score×0.6，低分降权不剔除）→
  重排（light/cross/LLM 三档）→ MMR 多样性。附加：查询时间窗加权（"最近"降旧记忆）、
  活跃目标注意力（+0.08）、超预算注入压缩（最多 8 条 + "等 N 条"）。
- **为什么**：单一通道都会漏，融合 + 可解释分数分解（rrf/policy/confidence）才能既召回又可控。

### policy / bayes —— 学习与可信度

- **贝叶斯置信度**：`后验 odds = 先验 odds × 似然比`；确认 LR=2.0、反驳 0.3、冲突 0.5（可配）。
- **遗忘曲线**：`recency = 0.5^(天数/半衰期)`，半衰期被情绪唤醒度拉长（情绪锚定）；
  三档遗忘：清晰 → 模糊（conf=0.25）→ 删除；核心记忆豁免。
- **巩固**：高重要度+多次提取 → 短期升长期；议题事实 ≥5 条 → LLM 压缩成核心总结，细节降权。
- **标定**：分桶映射（评测集命中率），评分驱动 confidence_factor 防过度自信。

### context —— 上下文组装

- 令牌预算分级注入：议题（核心）→ 记忆 → 事件脉络，逐级填充到 `context_budget_chars`（默认 2000）。
- 软置信度措辞：0.3~0.5 "我记得你好像提过…？"、<0.3 "（内部参考，不确定）"；
  contested 加"（待核实）"；来源标注（·用户/·人设/·档案/·总结/·观点）。
- 防编造约束：只能引用记忆内容回答个人问题，禁止编造细节。

### reflect / relationship / advisor / world / expression / trace

- **reflect**：每日反思（事件/关系/目标 → 洞察入 ai:reflection）+ 信念审查（accept/revise/reject，可回滚）。
- **relationship**：AI-用户关系状态机，行为证据（chat/share/praise/dispute/negative）驱动
  trust/familiarity/closeness，`关系分 = Σ 行为权重 × exp(-0.05×天数)`，阶段 陌生→初识→熟悉→深度伙伴。
- **advisor**：目标系统（motivation/confidence/current_state/paused）+ 决策顾问
  （一次一问、结合记忆、现实约束、去模板化、防夸张）。
- **world**：用户中心世界模型——只跟踪用户提到的内容，快照硬预算 400 字符 + 缓存，
  纠正调查（update/keep/uncertain）不盲从。
- **expression**：语言语义层——50+ 网络词多义意图、玩笑概率、表达画像
  （slang/irony/emoji/formal，驱动表达适配）。
- **trace**：记忆处理轨迹（create/merge/update/decay/reject/relationship/inject + 原因），
  五维人工评分（提取/决策/置信度/来源/隐私）驱动行为参数（confidence_factor/igt/privacy 阈值）。

### sensitive / character / embedder / update / backfill / eval

- **sensitive**：敏感检测（手机号/证件/账号/健康/财务…）+ 可选 AES-GCM 加密，检索过滤。
- **character**：`/设定 角色` 生成人物档案入 `char:<名>`，查询提到角色时自动注入。
- **embedder**：本地 sentence-transformers 或云端 OpenAI 兼容，单例缓存。
- **update**：近似重复合并（不堆叠）、确认刷新、公开标记。
- **backfill / eval**：每日成长管线（向量回填/事件图/议题/巩固/遗忘/反思/索引重建/评测对比 baseline）；
  评测 recall@K / MRR / NDCG。

## 5. 关键算法一览

| 算法 | 公式/要点 | 用在哪 |
|---|---|---|
| 信息增益 | `score = 0.25·数字 + 0.2·专名 + 0.45·新颖度 + 0.3·状态变化`，低分缓存合并 | 决定是否提取 |
| 贝叶斯置信度 | `odds' = odds × LR`；确认2.0 / 反驳0.3 / 冲突0.5 | 可信度演化 |
| 遗忘曲线 | `0.5^(days/half_life)`，half_life 受 arousal 拉长 | 时效降权 |
| RRF 融合 | `Σ w/(60+rank)`，7 路加权 | 多通道排序 |
| BM25 | `idf·tf(k1+1)/(tf+k1(1-b+b·dl/avgdl))`，k1=1.5 b=0.75 | 词法主通道 |
| IVF | kmeans 质心 + nprobe 探测余弦打分 | 向量检索 |
| MMR | `λ·相关 − (1−λ)·最大相似`，λ=0.7 | 多样性去重 |
| 软置信度 | `min_score×0.6` 保留低分 + 注入端分档措辞 | 不硬删除 |
| 关系分 | `Σ 行为权重 × exp(-0.05·天数)` | 关系成长 |

## 6. 设计决策（为什么这么做）

1. **一套记忆系统**：用户/AI/人物同表同格式，人格也是记忆，可统一检索/遗忘/评分；
   避免"记忆在 KV 里、人格在 prompt 里"的割裂。
2. **可解释优先于黑盒**：每个决策（保存/拒绝/合并/纠错）都有原因，轨迹可导出、可评分、
   可回滚——这是"工程化记忆"与"聊天记录堆"的根本区别。
3. **不盲从用户纠错**：先调查再更新，防止一句话推翻长期证据；历史永远保留。
4. **Token 预算硬约束**：快照 400 字符、调查 max_tokens=100 且节流、注入预算 2000、
   极短查询降级——认知深度不牺牲成本可控。
5. **评分驱动行为**：人工五维评分不仅存档，还实时调整 confidence_factor / 提取门槛 /
   隐私阈值，形成"评分→行为"闭环，为后续训练铺路。

## 7. 当前瓶颈

1. **提取仍依赖 LLM**：有过滤但无法 100% 保真（"林晓是人"类噪声靠 prompt+正则压，
   长尾仍会漏）；根治需提取质量门控模型（路线图 V14）。
2. **阈值未校准**：关系阶段、遗忘半衰期、表达画像阈值都是经验值，需真实数据校准。
3. **检索无数字基线**：评测管道就绪但 probes/评分还在积累，权重调整暂无数字依据。
4. **单 SQLite + 内存态**：`_chat_busy` / 缓存 / 节流状态重启即失；大流量需评估迁移
   PostgreSQL+pgvector（数据层已收敛在 `_db`，迁移路径清晰）。
5. **prompt 级约束的极限**：防编造、防模板、防夸张是提示词层面，无法 100% 根除；
   彻底解决依赖 AI 味抽查集 + 训练。

## 8. 相关文档

- [docs/Agent-OS-v6-架构评审.md](../docs/Agent-OS-v6-架构评审.md) —— 第三方架构评审
- [docs/Agent-OS-v6-系统测试报告.md](../docs/Agent-OS-v6-系统测试报告.md) —— 16 项能力测试
- [docs/ML-训练路线.md](../docs/ML-训练路线.md) —— 训练与调优路线
- [docs/SDK-使用.md](../docs/SDK-使用.md) —— SDK / HTTP 接入
