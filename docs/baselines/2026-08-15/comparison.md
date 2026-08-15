# Before / After 对比（2026-08-15）

> 目的：确认这轮优化没有引入回归，并记录门控评测集扩充后的当前结果。

## 证据门控

| 项目 | Before | After | 说明 |
|---|---|---|---|
| 评测集规模 | 26 条 | 33 条 | 从真实对话补充 7 条 |
| 通过数 | 26 | 33 | 全部通过 |
| Accuracy | 1.0 | 1.0 | 无回归 |

额外验证：

- 用**旧的 26 条**在当前代码上重跑：26/26 通过，说明扩充和修复没有破坏原有行为（结果见 `original_26_regression.json`）。
- 修复了真实误拦：否认黑名单词的回复不再被黑名单直接拦截。

## 记忆检索

| 指标 | Before | After |
|---|---:|---:|
| probes | 12 | 12 |
| recall@k | 1.0 | 1.0 |
| MRR | 0.808 | 0.808 |
| NDCG | 0.857 | 0.857 |

结论：检索指标与存档基线一致，无回归。

## 复现

```bash
# 门控 after
python tools.py evidence-gate-eval > docs/baselines/2026-08-15/after_evidence_gate.json

# 检索 after
python tools.py memory-eval --file data/eval/retrieval_probes.json > docs/baselines/2026-08-15/after_retrieval.json
```
