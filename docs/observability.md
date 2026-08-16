# 可观测性：Prometheus / Grafana / 日志 / Trace

## 1. 指标

Web 管理台暴露 Prometheus 文本指标：

```text
GET /metrics
```

内容示例：

```text
# HELP yuno_metric Yuno runtime metric
# TYPE yuno_metric gauge
yuno_web_requests_total 123.0
yuno_web_request_duration_ms 45.2
```

这些指标来自 `memory/telemetry.py` 的进程内计数，无需额外依赖。

### Prometheus 抓取配置

```yaml
scrape_configs:
  - job_name: yuno
    metrics_path: /metrics
    static_configs:
      - targets: ["127.0.0.1:8765"]
```

如果 Web 开了鉴权，建议用 basic auth 或单独放一个只读端口。

## 2. 日志

结构化事件日志写入：

```text
DATA_DIR/events.jsonl
```

每条 JSON 包含：

```json
{
  "event": "web.request",
  "ts": "2026-08-16T20:00:00+0800",
  "request_id": "abc123",
  "method": "POST",
  "path": "/api/tasks",
  "status": 200,
  "duration_ms": 12.3
}
```

采集方案：

- Filebeat / Fluent Bit 读取 `events.jsonl`
- 输出到 Loki / Elasticsearch
- Grafana 中建日志面板

## 3. Trace

当前 trace 通过 `request_id` 串联：

- Web 中间件生成 `request_id`
- 通过 `telemetry.set_request_id()` 注入上下文
- `agent.ask` 的 start/end 事件复用同一个 `request_id`
- 所有 `log_event()` 自动带上 `request_id`

因此可以按 `request_id` 串联：

```text
web.request
  → agent.ask.start
  → agent.ask.end
```

如果后续要接 Jaeger / OpenTelemetry，可以基于现在的 `request_id` 扩展为 `trace_id` / `span_id`。

## 4. Grafana 建议面板

- 请求量：`sum(yuno_web_requests_total)`
- P95 延迟：`histogram_quantile(0.95, sum(rate(yuno_web_request_duration_ms_bucket[5m])))`
- 最近错误：从 `events.jsonl` 里 `status >= 500` 过滤
- 评测回归：接入 `scripts/eval_ci.py` 输出的 baseline 变化

## 5. 定时任务

建议 cron：

```cron
# 每天凌晨审核 badcase 并自动合并
0 4 * * * cd /path/to/qq-bot && ./venv/bin/python scripts/auto_review_badcases.py --limit 20 --auto-merge --notify >> /var/log/yuno_badcase_review.log 2>&1
```
