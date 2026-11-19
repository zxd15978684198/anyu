# 迭代 6 W5 三级路由分层评测汇表

- route_fn: `wired_day06_offline_reeval`
- fixtures: eval/fixtures/routing.jsonl
- badcase（追加）: eval/fixtures/routing_badcase.jsonl

## 总览

| 指标 | 值 |
| --- | --- |
| 总条数 | 110 |
| 命中 | 109 (99.1%) |
| 敏感召回 | 100.0% |
| route_ms p50 | 0.2ms |
| route_ms p95 | 0.6ms |

## 分层占比（实际 layer）

| layer | 条数 | 占比 |
| --- | --- | --- |
| L0 | 52 | 47.3% |
| L1 | 31 | 28.2% |
| L2 | 27 | 24.5% |

## 分类别命中

| category | 条数 | 命中 | 未中 | 命中率 |
| --- | --- | --- | --- | --- |
| normal | 50 | 50 | 0 | 100.0% |
| sensitive | 20 | 20 | 0 | 100.0% |
| boundary | 20 | 19 | 1 | 95.0% |
| ambiguous | 20 | 20 | 0 | 100.0% |

## Bad case（本轮新增）

| 行号 | id | 类别 | 期望 | 实际 | 原因 |
| --- | --- | --- | --- | --- | --- |
| 71 | b001 | boundary | L1 | L0 | 期望 L1，实际 L0 |
