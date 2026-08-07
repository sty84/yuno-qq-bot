# Agent OS 机器学习训练路线

> 目标：用系统自己沉淀的数据，把检索、置信度、提取、遗忘从"手调参数"升级为"数据驱动"。
> 硬件约束：服务器 CPU-only（40G 盘）；训练机 2060S 8G（embedding/reranker 微调够用，LLM 微调不建议）。

---

# 1. 系统已经沉淀的数据资产

| 数据 | 表/来源 | 能当什么训练数据 |
|---|---|---|
| 查询 + 命中 | `query_log` → `memory-probes` | 检索排序的弱监督正样本 |
| 用户反馈 | `feedback_log`（investigate:update 1.0 / uncertain 0.5 / keep 0.2 / confirm 0.5 / praise 0.3） | 排序/置信度的带权标签 |
| 人工多维评分 | `trace_review`（extraction/decision/confidence/provenance/privacy） | 提取质量、置信度校准的强标签 |
| 记忆变更轨迹 | `memory_history`（merge/supersede/conflict/investigate） | 冲突检测、更新策略样本 |
| 遗忘/巩固决策 | `policy_log` + `memory_meta` | 遗忘策略、重要性预测样本 |
| 表达画像 + 网络词候选 | `user_expression_profile` / `language_context` | 网络词收录、风格分类 |
| 评测基线 | `memory-eval --save` 的 baseline | 一切改动的验收门 |

**结论：训练数据管道已经存在，缺的只是"跑起来积累数据"。**

---

# 2. 可训练的模型清单（按优先级）

## P0 · 服务器 CPU 就能训（建议先做）

### 2.1 检索权重网格搜索

- **目标**：把 RRF 权重（lexical/vector/graph/…）从手调变成按评测集选优。
- **数据**：`probes.json`（query + expected）。
- **做法**：对权重组合做网格/贝叶斯搜索，目标 = 评测集 Recall@K / MRR，记录每次组合的 baseline 对比。
- **落地**：`tools.py memory-tune-weights --file probes.json`（现成 `memory-index --tune` 是 nlist/nprobe 版，扩展到全权重）。
- **依赖**：纯 Python，零新库。

### 2.2 学习排序 LTR（LightGBM LambdaRank）

- **目标**：替代手写融合公式，让模型按"查询类型"学出最优排序。
- **特征**（每个记忆一条样本）：6 路算法分（lexical/vector/graph/structured/rules/topics）+ policy 分 + confidence + importance + recency + access_count + 来源类型 + 时间窗。
- **标签**：`query_log.hits` 命中 = 相关（1），未命中但同 scope 召回候选 = 不相关（0）；`feedback_log` dispute 的记忆 = 强负样本（-1）。
- **做法**：LightGBM `lambdarank`（CPU 完全够），特征用 `retrieve_detailed` 的分数分解导出。
- **部署**：训练产物 `.txt` 模型；推理时 `reasoning.py` 的融合打分改为"特征 → 模型分数"（可先与 RRF 融合做 A/B）。

### 2.3 置信度校准升级（逻辑回归）

- **目标**：把分桶标定（calibrate）换成特征回归。
- **特征**：confidence、importance、recency、access_count、source 类型、是否 contested。
- **标签**：`trace_review.confidence` 维度分（1~5 映射到 0~1）；feedback 的 confirm/dispute 作为弱标签。
- **做法**：sklearn LogisticRegression，产出 `P(记忆为真 | 特征)`。
- **部署**：`policy.calibrate_adjust` 从查映射表改为查模型。

### 2.4 提取质量门控分类器

- **目标**：判断"这条提取的事实会被人工评低分"，低分预测 → 触发重提取或拒绝。
- **特征**：候选长度、实体数、是否含数字、来源文本长度、情绪强度、是否网络词。
- **标签**：`trace_review.extraction` 维度分（≤2 为负）。
- **做法**：LightGBM 二分类；**部署**：controller.ingest 提取后加一道门。

## P1 · 需要 2060S（8G）微调

### 2.5 Embedding 微调（bge-small-zh）

- **数据**：三元组（query, 正记忆, 负记忆）——正：query_log 命中 + feedback confirm；负：dispute 记忆 + 同 query 未命中候选。
- **做法**：sentence-transformers 对比学习（Contrastive/InfoNCE），batch 16~32，几个 epoch。
- **产物**：微调后的 embedding 模型（~100MB）替换 `embedder.py` 的模型路径。
- **注意**：bge 微调容易灾难性遗忘，需在原始语料上混合训练或保留原模型可回滚。

### 2.6 Reranker 微调（bge-reranker-base）

- **数据**：（query, 记忆）对，标签 1/0 来自 probes + feedback。
- **做法**：cross-encoder 训练；产物替换 `rerank.cross_rerank` 的模型。
- **收益**：二次排序质量提升最明显，但推理慢（每查询要对 top-k 打分），只在 `mode=cross` 时启用。

## P2 · 持续在线学习

### 2.7 权重 Bandit（Thompson Sampling）

- **奖励**：praise/confirm 为正反馈，dispute/低分评分为负反馈。
- **做法**：按查询类型维护权重分布，每次检索采样一组权重，反馈后更新后验。
- **落地**：`memory-tune-weights` 的在线版；必须有 eval 门防漂移（每周对照 baseline，退化即回滚）。

---

# 3. 训练数据构建（弱监督标签怎么来）

```
原始信号 → 标签：
  query_log.hits           → 相关=1（弱）
  feedback dispute         → 相关=-1（强负）
  feedback confirm/praise  → 相关=1（强正）
  trace_review 各维度 ≤2   → 负样本（强）
  trace_review 各维度 ≥4   → 正样本（强）
  memory_history supersede → 旧记忆=负（时间变化）
```

工具链（已存在）：

```bash
# 每周导出数据集
tools.py memory-probes
tools.py memory-eval-dataset v1 --file data/probes.json
# 人工评分补强标签
tools.py memory-trace-review <id> --extraction .. --decision .. --confidence .. --provenance .. --privacy ..
# 评估门
tools.py memory-eval --dataset v1 --save
```

## 3.1 多维评分 → 训练环节映射（v11）

`trace_review` 的五维分是**强标签**，每个维度对应一个训练任务和一个部署点：

| 评分维度 | 对应的训练任务 | 部署点 | 与实时调整（v11）的关系 |
|---|---|---|---|
| extraction 提取准确性 | 提取质量门控分类器（P0-4）：预测"这条候选会被评低分"→ 重提取/拒绝 | controller.ingest 提取后 | 当前只落 extraction_strict 标志；训练后模型替代标志 |
| decision 决策合理性 | 保存决策分类器：是否该 create/merge/reject（特征=信息增益分/情绪/玩笑概率/实体数/scope） | Consent + IGT 判定 | v11 用它实时调 igt_threshold；训练后模型替代规则阈值 |
| confidence 置信度校准 | 置信度回归（P0-3）：特征→真实校准概率，取代分桶标定 | policy.calibrate_adjust | v11 用它实时调 confidence_factor；训练后模型替代因子 |
| provenance 来源可信度 | 来源质量评估：学出"哪些来源更可信"的权重，检索时按来源降权 | reasoning 融合权重 | 当前仅记录；训练后可学出 per-source 权重 |
| privacy 隐私处理 | 隐私检测模型：text→隐私分，替代规则词表 | sensitive.detect + ingest 阈值 | v11 用它实时调 privacy_threshold；训练后模型替代词表 |

**双层结构**：

- **即时反馈层（v11，已生效）**：评分均值 <3 → confidence_factor/igt_threshold/privacy_threshold 实时调整，让系统"先学会不犯同样的错"。
- **长期模型层（训练后）**：评分作为强标签训练小模型，上线后**取代**规则参数，效果更好且可回滚。

**评分还承担两个辅助角色**：

1. **Bandit 奖励信号**：decision/privacy 低分 = 负奖励，喂给在线调权。
2. **验收指标**：模型上线后，人工评分均值（每维度）对比旧版，作为 eval 之外的"人工质量门"。

---

# 4. 版本时间线（v11 → v17，每个版本有数据门槛/命令/验收）

> 每个版本：**前置数据门槛 → 具体步骤（命令）→ 验收数字 → 回滚方式**。
> 数据积累估算：每天 50 条有效对话 ≈ 每周 350 条轨迹；每周人工评 200 条 → 2~3 周攒够 500 条带分样本。

## V11（当前版）· 数据采集 + 评分闭环

- **状态**：已实现（trace 导出 + 五维评分 + 评分驱动实时参数），**待部署上线**。
- **具体步骤**：
  1. 部署 v10/v11 代码（memory/ plugins/ tools.py + 重启）。
  2. 验证：聊几句 → `tools.py memory-trace-md --scope c2c:你的openid` 看到带 `[TRACE:id]` 的轨迹。
  3. 确认每天 cron 的 `memory-grow` 在跑（数据自动沉淀）。
- **验收**：轨迹表每周新增 ≥200 条；评分表开始有数据。

## V12 · 权重网格搜索（建立数字基线）

- **前置**：probes 数据集 ≥100 条（约 2 周业务数据）。
- **具体步骤**：
  1. 实现 `tools.py memory-tune-weights --file data/probes.json`（网格/贝叶斯搜 7 个融合权重，目标 Recall@K+MRR）。
  2. 跑搜索 → 得到 best weights → 写入 config 并记录结果：
  ```bash
  tools.py memory-eval --file data/probes.json --save   # 存 baseline
  ```
  3. 存档：`data/eval_report.md` 记下 recall/mrr/ndcg + 权重版本。
- **验收**：baseline 数字落库；以后任何改动（V13~V17）都与此对比。
- **回滚**：config 权重改回旧值即可。

## V13 · 置信度校准模型（逻辑回归）

- **前置**：`trace_review.confidence` 维度评分 ≥300 条。
- **具体步骤**：
  1. 导出训练集：`tools.py memory-train-export --task calibrate --out data/calib_train.json`
     （特征：confidence/importance/recency/access_count/source 类型；标签：人工 confidence 分映射 0~1）。
  2. 训练：sklearn `LogisticRegression`（训练机或服务器均可），评估分桶校准准确率。
  3. 部署：模型文件 + config 开关 `calibration.model`；`policy.calibrate_adjust` 改读模型。
  4. 验证：`tools.py memory-eval --file data/probes.json` 对比 V12 baseline **不降**。
- **验收**：校准准确率 ≥ 现有分桶标定；eval 不降。
- **回滚**：开关关掉回分桶标定。

## V14 · 提取质量门控

- **前置**：`trace_review.extraction` 评分 ≥300 条。
- **具体步骤**：
  1. 导出：`tools.py memory-train-export --task extraction --out data/extract_train.json`
     （特征：候选长度/实体数/是否含数字/来源文本长度/情绪/是否网络词；标签：extraction 分 ≤2 负 / ≥4 正）。
  2. 训练：LightGBM 二分类（CPU 即可）。
  3. 部署：`controller.ingest` 提取后加门控，低分预测 → 重提取或拒绝，写入 trace（action=reject, reasoning=门控）。
  4. 验证：被门控拒绝的样本中，人工低分占比显著高于随机（A/B 统计）。
- **回滚**：门控开关关闭。

## V15 · 学习排序 LTR（LightGBM LambdaRank）

- **前置**：probes + feedback 正负样本 ≥1000 条。
- **具体步骤**：
  1. 导出特征：`tools.py memory-train-export --task ltr --out data/ltr_train.json`
     （每记忆一行：6 路算法分 + policy + confidence + importance + recency + access_count + source；标签：hit=1 / dispute=-1 / 候选未命中=0）。
  2. 训练 `lambdarank`；评估 NDCG@5。
  3. 上线方式：**先 50% 混合**（final_score = 0.5×RRF + 0.5×模型），A/B 一周。
  4. 验证：MRR/NDCG 较 V12 baseline 提升 ≥2% 才全量。
- **回滚**：混合比例调回 0 或删模型文件。

## V16 · GPU 微调 embedding / reranker（2060S）

- **前置**：V15 排序数据正负对 ≥2000。
- **具体步骤**：
  1. 2060S 机器：对比学习微调 `bge-small-zh`（三元组 query/正/负，batch 16~32，2~3 epoch）。
  2. 交叉训练 `bge-reranker-base`（query+记忆对，标签 1/0）。
  3. `scp` 模型回服务器 → 改 `config.json` embedder.model / rerank.cross_model 路径。
  4. 验证：向量 Recall@10、reranker NDCG 提升；`memory-eval` 对比 V12。
- **回滚**：模型路径改回原 `BAAI/bge-small-zh-v1.5`。

## V17 · 在线学习（Bandit + 自动评测门）

- **前置**：V12~V15 稳定运行 4 周，反馈流稳定。
- **具体步骤**：
  1. 按查询类型维护权重分布（Thompson sampling），reward = feedback 分级 + 评分低分惩罚。
  2. 每周自动：导出 probes → `memory-eval` → 对比 baseline；**退化自动回滚**到上周权重。
  3. 人工评分均值（每维度）作为最终质量门。
- **验收**：连续 4 周无退化，且人工评分均值上升。

---

# 4.1 数据门槛速查

| 训练任务 | 需要的最少样本 | 来源 | 约需时间 |
|---|---|---|---|
| 权重搜索 V12 | 100 probes | query_log | 2 周 |
| 置信度回归 V13 | 300 条 confidence 评分 | trace_review | 2~3 周 |
| 提取门控 V14 | 300 条 extraction 评分 | trace_review | 2~3 周 |
| LTR V15 | 1000 正负对 | probes+feedback | 3~5 周 |
| 微调 V16 | 2000 正负对 | V15 数据 | 4~6 周 |

---

# 5. 训练产物部署回系统

| 模型 | 加载点 | 回滚方式 |
|---|---|---|
| 权重表 | `reasoning._weights()` 读 config | config 版本化 |
| LTR 模型 | `reasoning` 融合阶段 | 模型文件 + 开关 |
| 置信度模型 | `policy.calibrate_adjust` | 模型文件 + 开关 |
| 提取门控 | `controller.ingest` | 模型文件 + 开关 |
| embedding | `memory/embedder.py` | 保留原模型路径 |
| reranker | `memory/rerank.py` | mode=cross 开关 |

**统一原则**：所有模型都有开关（config）+ 评测门（eval baseline 对比）+ HITL（人工评分）；模型上线前必须 `memory-eval` 数字不低于当前 baseline。

---

# 6. 风险与防失控

- **弱标签噪声**：feedback 只有 0.3~1.0 权重，训练时按 weight 加权，别当硬标签。
- **过拟合小数据**：模型上线前至少要有 500+ 正负样本；样本少时优先做权重搜索和校准，别上复杂模型。
- **灾难性遗忘**（微调 embedding）：保留原模型 + 混合训练。
- **反馈污染**：Bandit/在线学习必须有 eval 门 + HITL 审核，防止用户恶意反馈把系统带偏。
- **成本**：reranker/embedding 推理有成本，按查询类型分层启用（简单查询不走 reranker）。

---

# 7. 立即执行清单（本周起）

## 本周三件事

**① 部署 v10/v11**（trace + 五维评分 + 评分驱动参数）——代码已就绪，覆盖 + 重启即可。

**② 实现两个入口工具**（数据管道补齐，等数据直接开训）：

- `tools.py memory-tune-weights --file probes.json`：全权重网格搜索（V12 入口）
- `tools.py memory-train-export --task calibrate|ltr|extraction --out 文件`：把 probes + trace 评分 + feedback 合并成标准训练集（V13/14/15 共用）

**③ 跑起来正常聊天**，让数据自动沉淀（cron 已在跑）。

## 每周固定流程（30 分钟）

```bash
# 1) 导出查询日志评测集
tools.py memory-probes

# 2) 导出本周轨迹，人工评分 100~200 条（五维）
tools.py memory-trace-md --since <本周一> --limit 300
tools.py memory-trace-review <id> --extraction .. --decision .. --confidence .. --provenance .. --privacy ..

# 3) 记录数字
tools.py memory-eval --file data/probes.json --save
tools.py memory-trace-adjust        # 看评分是否已在驱动参数
```

## 里程碑检查点

| 时间 | 检查 | 通过条件 |
|---|---|---|
| 第 2 周 | 数据量 | probes ≥100，轨迹 ≥400 条 |
| 第 3~4 周 | 评分量 | 五维评分 ≥300 条 |
| 第 5 周 | V12 基线 | baseline 数字落库 |
| 第 6~8 周 | V13/V14 | 置信度/提取模型上线且 eval 不降 |
| 第 8~10 周 | V15 | LTR 混合上线，MRR+2% |
| 数据 ≥2000 对后 | V16 | GPU 微调 embedding/reranker |
