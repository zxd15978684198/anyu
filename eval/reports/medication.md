# 迭代 10 用药触达链路端到端验收（回执 → 终态归因）

> 状态：已完成 ｜ 版本：v1.0 ｜ 日期：2026-11-12
> 关联：共享任务板 `task-6`（用药触达链路端到端验收：回执 → 终态归因真库链路）；`设计记录（用药触达接通）`
> 上游依据：PRD line 305-339（四次触达时间线与终态分流）、323（`missed_unconfirmed`）、324（`notification_failed`）、332（FR-MED-02 / AC-06 / AC-20）｜技术设计文档 §3.4（`medication_events` / `medication_attempts` / `work_orders`）、§7.1（调度入口）
> 实测环境：Windows / Python 3.12（仓库 `.venv`）/ pytest 9.x / PostgreSQL 15（`127.0.0.1:5432`，独立测试库 `anyu_test`）
> 变更记录见文末

## 0. 结论（先说人话）

| # | 验收链路 | 本轮实测结论 | 证据 |
| --- | --- | --- | --- |
| 1 | 四次触达零回执 → 升级 | 四枪回执逐条 `none`、`tts_result` 全 NULL；升级落 `notification_failed` / `rtc_failed`；唯一红单停在 `new` | §2 用例 1、§3 判据③④ |
| 2 | 四次触达有回执、无人确认 → 升级 | `card_rendered` ×4 且 `tts_result` 全 `tts_failed`（已送达、播报失败）；升级落 `missed_unconfirmed` / `no_answer` | §2 用例 2、§3 判据②③ |
| 3 | 整窗错过、一枪未打 → 升级 | 零出站（`deliver.targets == []`）、零 attempts、`retry_count=0`；终态 `notification_failed` / `rtc_failed`，**不落** `missed_unconfirmed` | §2 用例 3、§3 判据③ |
| 4 | 装配根 `build_scheduler(medication_deliver=...)` → 终态 | job 上绑的是同一个通道对象（`partial` 的 `keywords["deliver"] is deliver`），用该 job 驱动能一路把回执写进库并落到终态 | §2 用例 4、§3 判据⑤ |
| 5 | 升级后重复驱动（幂等） | 后续三轮 `handled == 0`；事件 / 工单 / 通知 / 触达条数均不变 | §2 用例 5、§3 判据⑥ |

**一句话**：本轮新增 5 例真库端到端用例，**5 passed / 0 failed**，把「回执落库 → 按回执归因 → 唯一工单/通知 → 重复驱动不重复建单」这条链路钉住；**未发现生产缺陷**。唯一需要裁决的是「漏服升级不写 `risk_events`」——代码注释与偏离登记都写明是刻意选择，但 PRD:332 字面有「生成唯一事件和工单」的措辞，见 §5 M1。

## 1. 被测能力清单与落点

| # | 能力 | 承载模块（文件:行） | 本轮覆盖的验收点 |
| --- | --- | --- | --- |
| 1 | 到点事件预生成 | `app/scheduler/medication.py:392 run_medication_scan()` | 当天有效药单 → 恰好一条 `(plan_id, scheduled_at)` 事件（`triggered_by="scheduler"`） |
| 2 | 到点触达与回执落库 | `app/scheduler/medication.py:496 run_medication_touch()`、`:796 _svc_record_attempt()` | 每次出站记一行 attempt（`attempt_no` / `sent_at` / `receipt` / `tts_result`） |
| 3 | 回执 → TTS 结果派生 | `app/scheduler/medication.py:313 _tts_result_for()` | `tts_finished→ok`、`card_rendered→tts_failed`、`none→NULL` |
| 4 | 触达节拍与升级判定 | `app/scheduler/medication.py:201 decide_event_action()`、`:565 run_medication_due_scan()` | T0/+5/+10/+15 打满四次；T+16 升级；过期窗口不补枪 |
| 5 | 升级与终态归因 | `app/scheduler/medication.py:695 _escalate_event()`、`:744 _attempt_receipts()`；`app/services/medication_service.py:368 escalation_attribution()` | 读 attempts 回执集 → 写回 `terminal_state` / `failure_reason` |
| 6 | 升级建单 | `app/services/medication_service.py:629 escalate_to_work_order()` → `app/services/work_order_service.py:create_work_order()` | `missed_medication` / `red` / `new`，`note` 含 reminder_id 与 plan_id |
| 7 | 升级通知 | `app/scheduler/medication.py:848 _svc_notify_escalation()` | `medication_escalation` / `red`，assignee=主责护工，payload 六字段与工单号对应 |
| 8 | 装配根接线 | `app/main.py:221-236`、`app/scheduler/__init__.py:70 build_scheduler()`、`app/scheduler/medication.py:344 register_medication_jobs()` | 通道经 `partial(run_medication_due_scan, deliver=...)` 注册，运行期吃同一个对象 |

## 2. 用例规模与实测（逐个用例贴断言要点）

命令：

```
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/test_medication_chain_e2e.py
```

| # | 用例（`tests/test_medication_chain_e2e.py`） | 覆盖范围 | 实测结果 |
| --- | --- | --- | --- |
| 1 | `test_zero_receipt_chain_lands_notification_failed`（`:405`） | 四枪 `none`；attempts 1..4 与 `sent_at` 精确；`tts_result` 全 NULL；升级不再出站；终态 + 唯一事件/工单/通知 | passed |
| 2 | `test_receipted_chain_lands_missed_unconfirmed`（`:451`） | `card_rendered` ×4 → `tts_failed` ×4；终态 `missed_unconfirmed` / `no_answer`；工单/通知各 1 | passed |
| 3 | `test_window_missed_without_any_touch_lands_notification_failed`（`:481`） | T+60 驱动；零出站、零 attempts、`retry_count=0`；终态 `notification_failed` / `rtc_failed`；工单文案固定口径 | passed |
| 4 | `test_build_scheduler_deliver_reaches_chain_terminal_state`（`:530`） | `build_scheduler` 注入后 `partial` 绑定同一对象、`args==()`/`kwargs=={}`；用 `job.func` 驱动四枪 + 升级到终态 | passed |
| 5 | `test_repeated_drive_after_escalation_does_not_duplicate`（`:575`） | 升级后 T+16/+20/+60 再驱动 `handled==0`；事件/工单/通知仍各 1，attempts 仍 4，通道不再出站 | passed |
| — | 合计 | 1 个文件 / 5 例 | **5 passed** in 3.70s（首跑 3.63s），0 failed |

质量门（同轮实测）：

```
.\.venv\Scripts\ruff.exe check tests/test_medication_chain_e2e.py        → All checks passed!
.\.venv\Scripts\ruff.exe format --check tests/test_medication_chain_e2e.py → 1 file already formatted
```

三条环境注记（复跑能重现的前提，不是缺陷）：

1. **必须设独立测试库**：`ANYU_TEST_DATABASE_URL` 指向 `anyu_test`（账号口令同本地 docker compose 开发库，此处不复述明文）。不设时 `tests/conftest.py` 会把 `DATABASE_URL` 指回开发库，跑全量会清空演示数据（AGENTS §9 第三次踩坑）。
2. **必须连 `127.0.0.1`**：Windows 上 `localhost` 先解析 `::1`，PostgreSQL 只监听 IPv4，每次连接白等约 2 秒；`tests/conftest.py` 已强制回环 IPv4。
3. **主库零残留**：`db` 夹具开一条真连接 + 外层事务，任务体自己的 `commit()` 经 `join_transaction_mode="create_savepoint"` 只释放 savepoint，用例结束 `rollback` 全抹掉；本文件不使用 `tmp_path`（沙箱建不了目录）。

## 3. 关键判据（数值都能指到代码行）

| # | 判据 | 落点 | 本轮核对方式 |
| --- | --- | --- | --- |
| ① | 四次触达、T+16 才升级 | `app/scheduler/medication.py:201 decide_event_action()`：`timeline_end = scheduled_at + 5min × (4-1) + 60s`；`RETRY_SPACING_MINUTES=5` | 用例 1 断言 T0/T+5/T+10/T+15 各 `handled==1`、T+15 时 `retry_count==3` 且 `terminal_state is None`，T+16 才升级；`sent_at` 逐个精确相等 |
| ② | 卡片渲染成功但 TTS 未播完 → `tts_failed` | `app/scheduler/medication.py:313 _tts_result_for()` | 用例 2 断言 4 行 attempt 的 `receipt` 全 `card_rendered`、`tts_result` 全 `tts_failed`；用例 1 断言零回执时 `tts_result` 全 NULL |
| ③ | 终态只由回执集决定 | `app/services/medication_service.py:368 escalation_attribution()`：任一 `card_rendered`/`tts_finished` → `("missed_unconfirmed","no_answer")`；空集 / 全 `none` → `("notification_failed","rtc_failed")` | 用例 1（全 none）、用例 2（全 card_rendered）、用例 3（空集）三个输入各得一条终态；三者走同一个 `_escalate_event`，差异只来自库里回执 |
| ④ | 红线单口径：`missed_medication` / `red` / `new`，不许自造 `acknowledged` | `app/services/medication_service.py:629 escalate_to_work_order()`；`app/services/work_order_service.py create_work_order()` 默认 `status="new"` | 三个升级用例共用 `_assert_single_escalation_artifacts()`（`:352`）：工单恰 1 张、类型/严重度/状态逐值断言，`note` 含 `reminder_id` 与 `plan_id` |
| ⑤ | 装配根通道是同一个对象 | `app/scheduler/medication.py:344 register_medication_jobs()` 把通道绑进 `partial`，不走 `Job.kwargs` | 用例 4 断言 `isinstance(job.func, functools.partial)`、`job.func.keywords["deliver"] is deliver`、`job.args==()`、`job.kwargs=={}`，再用该 job 跑完整链路 |
| ⑥ | 升级幂等闸门 | `app/scheduler/medication.py:695 _escalate_event()` 开头 `status ∈ ("confirmed","escalated") → return` | 用例 5 断言重复驱动 `handled==0`，且事件/工单/通知/attempts 条数全部不变（不产生第二条） |
| ⑦ | 通知 payload 与工单号可对账 | `app/scheduler/medication.py:848 _svc_notify_escalation()` | `_assert_single_escalation_artifacts()` 断言 payload 的 `module/state/reminder_id/work_order_no/scheduled_at/summary`，其中 `work_order_no` 必须等于工单行的 `order_no` |

## 4. 反向验收（为什么这些断言不是假绿）

本轮工作包只允许新增测试、**禁止改生产代码**（`app/**` 正由他人修改中，见 §6），因此不做「改一行看变红」的破坏性验证。改用**同一代码路径、不同输入必须给出不同结论**的结构性反证，以及**若关键行为消失必红**的断言链：

| # | 反证 | 若行为缺失会怎样 |
| --- | --- | --- |
| 1 | 用例 1 与 2 走同一个 `_escalate_event`，唯一差别是假通道回执 `none` vs `card_rendered` | 归因若不读库（例如写死常量），两条必有一条红——当前两端各自通过，说明终态确实来自 attempts |
| 2 | 用例 3 断言 `deliver.targets == []` 且 attempts 为空 | 若扫描对过期槽补打一枪（PRD 不允许的「迟到触达」），这条立刻红 |
| 3 | 用例 1 断言通道恰好收到 4 个目标、`attempt_no` 依次 1..4；升级轮后仍为 4 | 若任务体只写库不出站，或升级轮再出站一次，两条断言分别红 |
| 4 | 用例 5 断言 `len(orders)==1`、`len(notifications)==1`，且 `order_no`/`notification_id` 与首次相同 | 去掉 `_escalate_event` 的 closed 闸门，第二次驱动会再建一单/再发一条通知，条数断言红 |
| 5 | 用例 3 断言工单 `note` 仍含「四次触达」固定口径 | 若有人把文案改成按实际触达次数动态生成，这条红——先把「当前口径」钉住，避免静默改文案 |

## 5. 已知偏离 / 口径待裁决

| 编号 | 内容 | 影响 | 来源 |
| --- | --- | --- | --- |
| M1 | 漏服升级**不写 `risk_events`**：`escalate_to_work_order()` 调 `create_work_order()` 时不传 `event_id`，也不建 `RiskEvent`（注释：`work_orders.event_id` 指向 `risk_events.event_id`，漏服升级没有风险事件可指，「不是缺陷」） | PRD:332 字面「生成唯一事件和工单」。本卡把「唯一事件」解释为「同 `(plan_id, scheduled_at)` 只有一条 `medication_event`」并据此断言。若产品负责人裁决必须建 `RiskEvent`，属新增生产代码（超出本卡写作用域），届时需补一条 `risk_events` 条数断言 | `app/services/medication_service.py:629` 注释 + 模块 docstring S-D7-C2；PRD:312 / 332 |
| M2 | 升级**不写 `work_order_transitions`**（`escalated` 不在六态 CHECK 内） | 不影响建单与终态，仅状态留痕缺失；已在 迭代 7 偏离 D5 / S-D7-C2 登记 | 同上 |
| M3 | 工单文案固定「四次触达（T0/+5/+10/+15）均未获有效确认」，即使整窗错过、一枪未打也照写 | 链路 3 已把这个「不随实际次数改写」的行为钉住；话术与事实（零触达）不完全一致，属产品文案问题，待裁决 | `app/services/medication_service.py:629` note 模板 |

## 6. 未覆盖项（没测到就是没测到）

| # | 未验证项 | 原因 | 影响 / 后续动作 |
| --- | --- | --- | --- |
| 1 | 真实外呼建连（`rtc_failed` 的物理成因） | 假通道直接返回 `none`，不建 aiortc 连接、不起 coturn；`app/audio/medication_delivery.py` 只在单元层有覆盖 | 「页面回执」与「建连失败」的真实信号源未端到端验证；演示环境联调补 |
| 2 | 跨进程并发重复升级 | 本卡串行驱动同一事件；幂等靠 `_escalate_event` 的事务内状态判断，DB 侧没有唯一约束兜底 | 两个 worker 同时驱动是否会产生两张工单未验证；属并发口径风险，建议在调度器侧确认 `max_instances=1` 的实际生效范围 |
| 3 | 通知投递与工单状态流转 | 只断言落库行（`notifications` / `work_orders`），不断言推送触达、`acknowledged` / `resolved` 流转 | console 侧接手链路由 `test_work_order*.py` 等覆盖，本卡不重测 |
| 4 | 补确认 / 拒服 / 伴不适链路 | 属 `test_medication_api.py`、`test_medication_graph.py` 的写作用域 | 本卡只覆盖「无响应」这一支的两条终态 |
| 5 | `risk_events` 条数 | 刻意的口径解释，见 §5 M1 | 待裁决后再决定是否加断言 |
| 6 | 生产改动的全量门禁 | 本卡只跑目标文件与 ruff；生产改动（`app/scheduler/**`、`app/services/medication_service.py`、`app/main.py`、`app/audio/medication_delivery.py`、迁移 0015）尚未提交、正由他人修改，全量 pytest 会被在途改动干扰 | 合并前须跑全量 `pytest -q`（先设 `ANYU_TEST_DATABASE_URL`） |

## 7. 复跑与证据链

```powershell
# 0) 前置：独立测试库（库名 anyu_test，账号口令同本地 docker compose 开发库）
#    不设此变量时 conftest 会把 DATABASE_URL 指回开发库；库不可达时用例直接 fail，不会静默跳绿。
$env:ANYU_TEST_DATABASE_URL = "<anyu_test 的 asyncpg DSN>"

# 1) 本卡用例
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/test_medication_chain_e2e.py

# 2) 质量门
.\.venv\Scripts\ruff.exe check tests/test_medication_chain_e2e.py
.\.venv\Scripts\ruff.exe format --check tests/test_medication_chain_e2e.py

# 3) 判据③⑥落点核对
Select-String -Path app/services/medication_service.py -Pattern '^def escalation_attribution'
Select-String -Path app/scheduler/medication.py -Pattern '_CLOSED_EVENT_STATUSES|async def _escalate_event'
```

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-12 | 首版（task-6）：新增 `tests/test_medication_chain_e2e.py` 5 例真库 e2e（零回执 / 有回执 / 整窗错过 / 装配根注入 / 升级幂等），实测 5 passed；登记 M1-M3 口径张力与 6 项未覆盖 |
