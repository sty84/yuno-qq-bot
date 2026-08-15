# 改进结合方案

> 目标：把论文/项目中的先进思路结合进当前 YUNO 架构，形成可落地、可验证的迭代路线。
> 原则：不推翻现有架构，在已有模块上做增量增强。

---

## 1. 记忆上下文管理（借鉴 MemGPT / Letta）

### 现状
- 已有 `core / long / short` 分层。
- 但上下文预算主要靠字符截断，缺少“压力感知”和“自动换页”。

### 结合方案
- 新增 `memory_pressure`：
  - 当注入上下文超过阈值时，自动把低价值记忆压缩成摘要。
  - 摘要进入 `ai:reflection` 或专门 `ai:summary`。
- 新增“换页”策略：
  - 核心记忆常驻；
  - 近期记忆保留；
  - 旧记忆自动转为摘要；
  - 用户主动追问时再展开。

### 涉及模块
- `memory/context.py`
- `memory/controller.py`
- `memory/policy.py`

---

## 2. 分层反思（借鉴 Generative Agents）

### 现状
- `daily_reflect` 是单层总结。
- 缺少“事件 → 观察 → 信念 → 计划”的层次。

### 结合方案
- 三层反思：
  1. **事件层**：每次重要交互写入 `memory_trace`。
  2. **观察层**：每天把事件归纳成观察。
  3. **信念层**：每周把观察归纳成信念/自我认知。
- 反思结果进入：
  - `ai:reflection`
  - `ai:belief`
- 信念更新时触发 `reflect_beliefs()` 审查。

### 涉及模块
- `memory/advisor.py`
- `memory/trace.py`

---

## 3. 失败驱动反思（借鉴 Reflexion）

### 现状
- 有反思，但主要是“总结”。
- 没有“失败 → 反思 → 改进 → 再试”闭环。

### 结合方案
- 新增失败记录表或字段：
  - 场景、失败原因、证据门控/检索/表达哪个环节出错。
- 失败后自动生成反思：
  - “这次为什么错？”
  - “下次遇到类似情况该怎么做？”
- 反思结果写入 `procedures` 或 `ai:reflection`。
- 下次相似场景优先注入该反思。

### 涉及模块
- `memory/mistake.py`
- `memory/procedures.py`
- `memory/advisor.py`
- `agent/core.py`

---

## 4. 技能库（借鉴 Voyager）

### 现状
- 有 `procedures`，但比较浅，只有“情境→动作→成功率”。

### 结合方案
- 升级为“技能库”：
  - 每个技能包含：情境、动作、结果、可复用条件。
  - 成功技能自动提升权重；
  - 失败技能自动降权或改写。
- 技能来源：
  - 用户纠错后的修正行为；
  - 反思后的改进策略；
  - 人工确认的高质量回复。

### 涉及模块
- `memory/procedures.py`
- `memory/policy.py`
- `plugins/memory.py`

---

## 5. 认知架构标准化（借鉴 CoALA）

### 现状
- 模块已有，但接口不统一。

### 结合方案
- 定义统一接口：
  - `MemoryInterface`：读、写、检索、遗忘。
  - `ActionInterface`：工具、回复、主动行为。
  - `DecisionInterface`：选择动作、判断是否需要检索。
- 现有模块逐步适配到接口，不改变内部实现。

### 涉及模块
- `memory/__init__.py`
- `agent/core.py`
- `plugins/`

---

## 6. 检索自我评估（借鉴 Self-RAG / CRAG / GraphRAG）

### 现状
- 有证据门控，但检索前不会主动判断“是否需要检索”。
- 检索结果不可信时不会自动再检索。

### 结合方案
- 增加“检索必要性判断”：
  - 简单闲聊不检索；
  - 事实/记忆类问题必须检索。
- 增加“检索置信度评估”：
  - 命中分数低时触发二次检索；
  - 二次检索仍低时，回复必须含糊。
- 引入 GraphRAG 式全局推理：
  - 多个碎片记忆之间建立跨事件关联。

### 涉及模块
- `memory/reasoning.py`
- `agent/evidence_gate.py`
- `memory/graph.py`

---

## 7. 记忆价值与整合（借鉴 Mem0 / MemoryOS / Zep）

### 现状
- 有 `importance` / `confidence` / `access_count`。
- 但还没有“记忆整合”和“冲突消解”的自动化。

### 结合方案
- 新增“记忆整合器”：
  - 相似记忆自动合并；
  - 矛盾记忆进入冲突队列；
  - 低价值记忆自动降权/遗忘。
- 把 `trace` 人工评分转成记忆价值信号。

### 涉及模块
- `memory/controller.py`
- `memory/policy.py`
- `memory/trace.py`

---

## 8. 评测集建设（借鉴 AgentBench / GAIA / ALFWorld）

### 现状
- 门控 40 条、检索 20 条、MBTI 3 轮。

### 结合方案
- 建立“长期记忆 Agent 评测集”：
  - 记忆召回
  - 防编造
  - 人设一致
  - 多轮一致
  - 纠错能力
  - 反思质量
- 每个维度 50~100 条，纳入 CI。

### 涉及模块
- `tests/`
- `docs/baselines/`

---

## 9. 人设一致性强化

### 现状
- 已有 MBTI 三轮测试。

### 结合方案
- 扩展成人设一致性评测：
  - 语气、价值观、边界、记忆引用。
- 使用 LLM-as-Judge 自动评分。
- 每次 persona 改动自动跑。

### 涉及模块
- `scripts/mbti_bot_test.py`
- `tests/test_persona_office.py`

---

## 10. 可观测性

### 现状
- 有 `stats` / `llm_cost` / `trace`。

### 结合方案
- 增加结构化日志。
- 增加请求级 trace_id。
- 把关键指标暴露到 `/api/status`。

### 涉及模块
- `plugins/_shared.py`
- `webapp.py`
- `memory/stats.py`

---

## 落地顺序建议

| 阶段 | 内容 | 预计收益 |
|---|---|---|
| P0 | 失败驱动反思 + 技能库升级 | 自我改进闭环 |
| P0 | 检索自我评估 + 二次检索 | 防编造更强 |
| P1 | 分层反思 + 信念更新 | 人格更稳定 |
| P1 | 记忆整合器 + 冲突队列 | 记忆更干净 |
| P1 | 评测集扩充 | 可量化验证 |
| P2 | 认知架构标准化 | 可维护性 |
| P2 | GraphRAG 全局推理 | 复杂问题能力 |
| P2 | 可观测性完善 | 运维/调试 |

---

## 验证方式

- 每个阶段都补测试。
- 跑双后端：
  - SQLite：`make check`
  - PostgreSQL：`YUNO_DB_BACKEND=postgresql ... pytest -q`
- 对比 `docs/baselines/` 中的指标。
