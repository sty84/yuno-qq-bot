# 评测基线 2026-08-15

> 用途：作为后续门控/检索改动的 before 基准。
> 数据源：当前 `data/bot.db` + 内置门控评测集。

## 证据门控

命令：

```bash
python tools.py evidence-gate-eval
```

结果：

- total: 26
- passed: 26
- failed: 0
- accuracy: 1.0

## 记忆检索

命令：

```bash
python tools.py memory-eval --file data/eval/retrieval_probes.json --save
```

结果：

- probes: 12
- recall_at_k: 1.0
- mrr: 0.808
- ndcg: 0.857

分类：

| 分类 | 数量 | Recall | MRR |
|---|---|---|---|
| 身份 | 2 | 1.0 | 1.0 |
| 日程 | 3 | 1.0 | 0.733 |
| 偏好 | 4 | 1.0 | 0.75 |
| 事务 | 2 | 1.0 | 0.75 |
| 空间 | 1 | 1.0 | 1.0 |

## 复现

```bash
python tools.py evidence-gate-eval > docs/baselines/2026-08-15/evidence_gate_baseline.json
python tools.py memory-eval --file data/eval/retrieval_probes.json --save > docs/baselines/2026-08-15/retrieval_baseline.json
```
