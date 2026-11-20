# PRD 收口验收报告（迭代 10 · 第二轮）

> 状态：已完成 ｜ 版本：v1.0 ｜ 日期：2026-11-12
> 关联计划：`设计记录（记忆治理与监护人通知）`（v1.1）、`设计记录（安全层与日报推送）`（v1.3）
> 上游依据：`docs/安语3D数字人_PRD.md` §4.5–§4.12、§5.1（AC-11/12/17/20/22/25/34/35/38/43–47）

## 1. 这一轮修的是什么

上一轮审计（99 条 FR）里判「缺失/简化」且属 **P0** 的，分两批落地：

| 批次 | 内容 | 提交 |
| --- | --- | --- |
| 第一批 | 机构知识链路（KNW-01/04/05）、用药触达（MED-02/06）、日报推送（RPT-03/AC-25）、安全层语境（DLG-07/01/09） | `a69cfe6`、`b8e70e8` |
| 第二批 | 记忆治理（MEM-01/03/05/06）、监护人通知（ALT-01）、录音上限（UI-01）、天气兜底（CON-02）、健康采集入口与 60 秒（HLT-01/03）、主动主诉（HLT-07）、用药子图接生产（MED-05） | `296a736` 及本轮 |

## 2. 逐条 AC ↔ 证据

| AC | 要求（摘要） | 实现落点 | 验收用例 |
| --- | --- | --- | --- |
| AC-11 | 连续两次血压无法解析 → 标缺失并继续问睡眠，生成黄色复核 | `app/graphs/health_checkup_graph.py`（`MAX_ATTEMPTS`/`missing`/`grade`）、`health_service.grade_vitals` | `tests/test_health_graph.py`、`tests/test_scheduler_health.py` |
| AC-12 | 红色事件 1 个事件 + 1 张工单，不重复 | `app/services/alert_service.py`、`risk_event_service.py` | `tests/test_alt_guardian_notify.py`、`tests/test_medication_chain_e2e.py` |
| AC-17 | 重启后恢复原计时，不重复触达/建单 | `app/scheduler/medication.py`（`_attach_confirmation_wait`、`_pending_confirmation`） | `tests/test_medication_graph.py::test_confirm_survives_a_checkpointer_restart`、`tests/test_scheduler_medication.py` |
| AC-20 | A=missed_unconfirmed / B=notification_failed | `medication_service.escalation_attribution`、`_escalate_event` | `tests/test_medication_chain_e2e.py` |
| AC-22 | 黄事件补录到红色 → 原事件升级、立即通知、不重复建单 | `app/services/health_service.py` 复核路径 | `tests/test_health_api.py`、`tests/test_health_graph.py` |
| AC-25 | 日报推送（family_report）+ 版本 + 1/5/15 重试 | `notification_service.notify_daily_report` / `deliver_due_daily_reports`、`app/scheduler/reports.py` | `tests/test_report_service.py`、`tests/test_notification_service.py`、`tests/test_scheduler_reports.py` |
| AC-34 | 非采集时段说「我头晕」→ 健康图 `source=voice_active`，大模型绕过不了 | `app/agents/companion.py` 前置路由 + `app/turn_wiring.py` | `tests/test_dialogue_health_intent.py`、`tests/test_prd_closeout_e2e.py` |
| AC-35 | 对话说「我吃过药了」→ 同一张图 `triggered_by=dialog` | `medication_service.confirm_from_dialog` + `app/turn_wiring.py` | `tests/test_medication_graph.py`、`tests/test_prd_closeout_e2e.py` |
| AC-38 | 2 分钟无人确认 → escalated 并通知管理员 | `app/scheduler/risks.py` → `risk_event_service.escalate_overdue_red_events` | `tests/test_risk_event_escalation.py`、`tests/test_alt_guardian_notify.py` |
| AC-43/44/47 | 知识回答带来源/生效时间、仅生效版本可检索、查不到要说查不到 | `knowledge_service` + `knowledge_lookup` + `companion` 预检索 | `tests/test_knowledge_e2e.py`、`tests/test_agent_context_knowledge.py`、`eval/fixtures/knowledge.jsonl` |
| AC-46 | 知识正文当参考资料，指令不执行 | `app/core/knowledge_guard.py` | `tests/test_knowledge_guard.py` |

## 3. 本轮（第二批）的关键判据

- **HLT-01/03**：19:00 真起图（不是只建提醒），每问 60 秒；超时先把 `timeouts` +1
  并重问一次，第二次超时标 `missing` 继续问；长者说「不测了」置 `ended`，已答项照常
  落库分级。判据落在 `health_checkup_sessions` 行状态与 `health_records` 终态。
- **HLT-07**：主诉识别是**确定性正则**（`HEALTH_COMPLAINT_PATTERN`），命中即前置路由到
  图，`tests/test_dialogue_health_intent.py` 断言「LLM 调用 0 次」——大模型根本没有机会
  自己编一段问诊。
- **MED-05**：调度器触达后把图推到 `wait_confirm`，`hitl_resumed` 的含义改为
  「终态由图层落（True）/ 直写落（False）」，**不是成败标志**；升级路径仍走直写，
  避免 resume timeout 覆盖调度器按回执算出的归因。
- **CON-02**：工具 3 秒未回先投递「我看看今天的天气，您稍等。」；provider 失败且
  有 24h 内的快照 → 按 `observed_at` 回答并标注「可能不是最新的」；无快照直说查不到。
- **UI-01**：上限 30 秒 → 120 秒，`MAX_AUDIO_BYTES` 同源派生；前端按住说话 119 秒
  自动收口，`UTTERANCE_TIMEOUT_MS` 15s → 60s（否则录满 2 分钟会先超时）。

## 4. 验证命令与结果

```powershell
$env:ANYU_TEST_DATABASE_URL="postgresql+asyncpg://anyu:anyu@127.0.0.1:5432/anyu_test"  # pragma: allowlist secret
.venv\Scripts\python.exe -m pytest tests\test_prd_closeout_e2e.py -q
.venv\Scripts\python.exe -m pytest tests\test_dialogue_health_intent.py -q
.venv\Scripts\python.exe -m pytest -q --ignore=tests/test_audio_tts.py -k "not audio_cleanup"
.venv\Scripts\ruff.exe check app tests
```

- `test_prd_closeout_e2e.py`：**5 passed**（AC-34 起图 + 同日起一场 + 坏身份不猜；
  AC-35 对话确认落 `taken_reported` + 无待确认提醒如实回）
- `test_dialogue_health_intent.py`：**8 passed**
- 全量：见提交时的实测输出（`2869 passed` 之上只增不减）

## 5. 已知残留（不计入本轮 P0）

| 项 | 说明 | 处置 |
| --- | --- | --- |
| 天气提示只有 `response_segment_delivered` 埋点 | 没有 `assistant_said` 那一路 | 建议下一轮补埋点，不改行为 |
| 工具预算 3.0s 与提示阈值同值 | 提示可能只比超时早一点点 | 建议把 `TOOL_TIMEOUT_SECONDS` 提到 6s（另开卡） |
| `escalate_overdue_red_events` 只看 `risk_event.acknowledged_at` | 工单已确认但字段未回填时会误升级（O1） | 留待下一轮，涉及工单↔事件两表口径 |
| 家属解绑后历史通知不可见（O2） | 可见性按当前绑定关系算 | 产品口径未定，不动 |
| 真机麦克风/唤醒词 | 用户已明确「没设备，不修」 | 不做 |
| 真语义 embedding 的召回质量 | 单测用可控向量，未跑 recall@k | 需要真实 provider + 标注集，另立评测卡 |

## 6. 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-12 | 首版：第二批 P0 收口，AC-34/35 用 `turn_wiring` 真库端到端验 |
