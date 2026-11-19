# 迭代 8 健康采集 / 红标 / 日报 / 趋势评测汇表

> 状态：已完成 ｜ 版本：v1.0 ｜ 日期：2026-11-08
> 关联：`设计记录（交付与观测）` §3.1（`eval/reports/` 补齐）；`设计记录（健康日报通知）`；`设计记录（day08-任务进度清单）`
> 上游依据：PRD §4.6 健康采集与红标、FR-HLT-04/06/09、§4.8 家属日报 ｜ 技术设计文档 §3 健康表、§6 `health_checkup_graph` 子图与 Interrupt/HITL
> 实测环境：Windows / Python 3.12.13 / pytest 9.2.0 / 本地 PostgreSQL(127.0.0.1:5432) + Redis(6379)
> 命名说明：迭代 8 计划的主题是「健康日报通知」（`设计记录（健康日报通知）`），本文件按 W7b 工作包指定的 `day08-health-checkup.md` 落盘，内容覆盖当天的采集窗口、红标、子图、日报与趋势
> 变更记录见文末

## 0. 结论（先说人话）

| 能力 | 本轮实测结论 | 证据 |
| --- | --- | --- |
| 采集窗口 19:00–20:00 | 两个 APScheduler 任务按 19:00 / 20:00 注册，19:00 无记录发提醒、20:00 仍无记录派 `collection_missed` | §1 / §4 判据② |
| 红标阈值（≥180 / ≥120，单分量即红） | 阈值写死在常量里，**单分量即判红**；接口层与子图层各有一例专门钉住 | §3（点名两例） |
| 健康子图 | 37 例全绿；红档走 `review` interrupt（HITL） | §2 |
| 日报 / 趋势 | 12 例全绿；日报按上海自然日生成，`state` 字段区分 `green` / `yellow` / `red` | §2 / §4 判据④ |

**一句话**：迭代 8 四块在本轮 4 个测试文件上共 75 例全绿；红标「单分量即判红」有专门用例（不是靠覆盖率蒙对）；曾让 CI 跨日必红的**日报日期炸弹已在 迭代 7 修掉**（`tests/test_health_api.py:60` 现为运行时取上海当天）。

## 1. 被测能力清单与落点

| # | 能力 | 承载模块（文件:行） | 本轮覆盖点 |
| --- | --- | --- | --- |
| 1 | 采集窗口配置 | `app/core/config.py:181-183`（`scheduler_timezone="Asia/Shanghai"`、`health_checkup_start="19:00"`、`health_checkup_end="20:00"`） | 窗口起止值、时区 |
| 2 | 19:00 开窗任务 | `app/scheduler/health.py:108 run_health_checkup_start()`（说明见 `:5`） | 当日无记录 → 每人一条 `health_review` 提醒（不主动拨号） |
| 3 | 20:00 漏采任务 | `app/scheduler/health.py:148 run_missed_scan()`（说明见 `:6`） | 当日仍零记录 → `collection_missed`，派 `primary_care` 护工（`:157`） |
| 4 | 红标阈值 | `app/services/health_service.py:60-62`：`SYSTOLIC_RED = 180`、`DIASTOLIC_RED = 120` | 单分量即判 `red` |
| 5 | 黄档阈值 | `app/services/health_service.py:64-71`：收缩压 140–179 / 舒张压 90–119 / <90 / <60 / 睡眠 <5h / mood ≤2 / 任一缺失 | 绿黄红三档划分 |
| 6 | 心率异常 | `app/services/health_service.py:75-76`：`HEART_RATE_LOW = 50`、`HEART_RATE_HIGH = 110`（缺失不判黄） | 异常入黄，不拖累 PRD 五问的绿档 |
| 7 | 健康子图 | `app/graphs/health_checkup_graph.py`（409 行） | 采集 → 分级 → 红档 `review` interrupt |
| 8 | 采集 / 补录 / 复核接口 | `app/api/health.py:253 fill_vitals`、`:270 list_records`、`:310 supersede_record`、`:329 get_review`、`:339 resolve_review` | 代填、补录、复核闭环 |
| 9 | 趋势 | `app/api/health.py:295 list_trend`；服务层 `app/services/health_service.py:914 list_trend()`（`:922` 近 N 天、按日期升序） | 医生 / 护工按负责范围看趋势 |
| 10 | 家属日报 | `app/api/reports.py:87 get_daily_report`、`:107 list_daily_reports`；生成时刻 `app/core/config.py:184 daily_report_generate_hour="07:00"` | 日报详情 + 列表 |
| 11 | 记录日期口径 | `app/services/health_service.py:84-86 today_in_shanghai()` | 业务日是**北京时间自然日**，不受 UTC 偏移影响 |

## 2. 用例规模与结果（逐个文件贴实际 passed）

命令（每行替换 `<file>`，见 §7 复跑）：

```
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp=.pytest-tmp-w7b <file>
```

| 测试文件 | 覆盖范围 | 实测结果 |
| --- | --- | --- |
| `tests/test_health_api.py` | 代填 / 趋势 / 补录 / 复核 / 权限范围 / 幂等 | **19 passed** in 4.83s |
| `tests/test_health_graph.py` | 分级表、红档 interrupt、子图流转 | **37 passed** in 2.88s |
| `tests/test_health.py` | 19:00 / 20:00 两个调度任务 | **7 passed** in 5.54s |
| `tests/test_reports_api.py` | 日报详情 / 列表 / 权限 / 字段结构 | **12 passed** in 5.42s |
| 合计 | 4 个文件 | **75 passed**，0 failed |

补充说明：

- 计划里写的 `test_health_checkup_graph.py` **不存在**，实际文件名是 `tests/test_health_graph.py`（`ls tests | Select-String health` 可复核）。
- 本轮 4 个文件全绿、无 warning 失败；`test_reports_api.py` 输出 12 条 `SAWarning: transaction already deassociated from connection`，属共享 dev 库的事务收尾噪声，不影响判定。
- 迭代 8 当天记录的全量门禁（供参照，非本轮重跑）：**1181 passed, 1 skipped, 25 warnings in 136.83s**；`ruff check` 全绿；覆盖率 **73.99%**（`fail_under=70` 达标，计划 §5 的 ≥80% 口径未达）；alembic 0007→0008→0007→0008 回放 exit 0。来源：`设计记录（day08-任务进度清单）`（v1.2，2026-11-07 16:23）。

## 3. 红标用例点名（这一条最容易糊过去，所以单独列）

| 用例 | 位置 | 钉住什么 |
| --- | --- | --- |
| `test_fill_red_single_component` | `tests/test_health_api.py:347` | 采集接口：**只给一个分量**到 180/120 就判 `red`，不要求两个分量同时越线 |
| `test_red_single_component_goes_to_review_interrupt` | `tests/test_health_graph.py:171` | 子图层：单分量红 → 直接进 `review` interrupt（HITL），不静默落库 |
| `test_grade_vitals_table` | `tests/test_health_graph.py:89` | 表驱动覆盖绿 / 黄 / 红边界值组合 |
| `test_grade_reasons_are_specific` | `tests/test_health_graph.py:93` | 分级理由要具体（不能只返回一个 `red`，要能说出是哪一项越线） |

阈值本身与 PRD §4.6 / §7 的硬性数值表一致：收缩压 ≥180 或舒张压 ≥120（`app/services/health_service.py:60-62`），不是各处各写一套。

## 4. 关键判据

| # | 判据 | 落点 | 核对方式 |
| --- | --- | --- | --- |
| ① | 红标：收缩压 ≥180 **或** 舒张压 ≥120，单分量即红 | `app/services/health_service.py:60-62` | 代码核对 + §3 两例全绿 |
| ② | 窗口 19:00 起、20:00 强制结束 | `app/core/config.py:182-183`；任务注册 `app/scheduler/health.py:5-6` | 代码核对 + `tests/test_health.py` 7 例全绿 |
| ③ | 20:00 仍无记录 → `collection_missed` 派给 `primary_care` 护工 | `app/scheduler/health.py:148-165`（派单 `:157`） | 代码核对 + 调度用例全绿 |
| ④ | 日报按 **上海自然日**生成，07:00 出 | `app/services/health_service.py:84-86`；`app/core/config.py:184` | 代码核对 + `tests/test_reports_api.py` 12 例全绿 |
| ⑤ | 同一天同一长者只发一次 `health_review` / `collection_missed` | `app/scheduler/health.py:79 _notification_exists()`（幂等去重） | 代码核对 + 调度用例全绿 |

## 5. 已知问题与偏离

| # | 事项 | 现状 | 影响 / 处理 |
| --- | --- | --- | --- |
| 1 | **日报日期炸弹** | `tests/test_health_api.py:60` 曾写死 `DAY = 2026-11-07`，与 `today_in_shanghai()` 一旦跨日就不一致，CI 次日必红。现为 `DAY = datetime.datetime.now(tz=datetime.timezone(datetime.timedelta(hours=8))).date()`，运行时取上海当天（迭代 7 清单 D12） | 已修，本轮 19 例全绿可证；后续不要再把业务日写成常量 |
| 2 | `created` 撞 `LogRecord` 的 `KeyError` | 调度日志字段名曾用 `created`，与 logging 保留属性冲突导致任务抛错；现改为 `created_count`（`app/scheduler/health.py:143`、`:182`） | 已修；当时表现为 2 failed / 1084 passed，修复后全量 1181 passed |
| 3 | 采集请求契约是**五字段平铺** | 实际锁定 `{elder_id, 五字段, source, note?}` 单层结构，不包 `measurements` | 与计划措辞不同，接口已冻结并在 `tests/test_health_api.py` 内锁定 |
| 4 | 日报 payload 结构 | 实为 `health{state,counts,total,sources,latest}` + `medication{module,state,summary}` + `sources{health,medication}`（`app/services/report_service.py:270-287`） | 与计划早期的「health 单块」不同；已由 `tests/test_reports_api.py` 锁定 |
| 5 | 心率按 50 / 110 判黄 | 心率不是 PRD 五问字段，但基座保留列；**缺失不判黄**，仅异常入黄（`app/services/health_service.py:73-76`） | 避免每条绿档被「没量心率」拖成黄 |
| 6 | 迁移文件名 | 实际是 `alembic/versions/0007_health_reports.py`（计划写 `0007_day8_health_daily_notifications.py`） | 仅命名差异，迁移已回放验证 |
| 7 | `app/core/config.py` 未新增键 | 窗口与日报时刻的键在 迭代 7 已存在（`:181-184`） | 与计划「本日改 config」的预期不同，属实现更省 |

## 6. 未验证项

| # | 未验证项 | 原因 | 影响 / 后续动作 |
| --- | --- | --- | --- |
| 1 | **真实时钟触发**（等到 19:00 / 20:00 由 APScheduler 真跑一次） | 用例是直接调用任务函数（`run_health_checkup_start()` / `run_missed_scan()`），没有真等到点 | 调度器注册与到点触发没被端到端验证；交付演示时建议真等到窗口，或临时把窗口改到下一分钟再跑一次 |
| 2 | pad 端采集 UI（语音五问 → 页面填写） | 本轮只跑后端接口与子图 | 前端交互、语音识别到采集字段的映射未验证，属演示范围 |
| 3 | 日报真实推送 | 通知走 `mock-operations`（Mock 落库） | 「家属真收到」不可测，只验证到落库与接口 |
| 4 | 趋势图前端渲染 | 本轮只验证 `GET /api/health/{elder_id}/trend` 的数据口径 | 图表口径（坐标轴、缺测日的空档）未验证 |
| 5 | 语音侧采集（ASR → 采集字段） | 语音链路不在本轮范围 | 若 demo 走语音采集，需另测这条链路 |

## 7. 复跑与证据链

```powershell
# 0) 前置：本地库与缓存
docker start anyu-postgres anyu-redis

# 1) 四个测试文件
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp=.pytest-tmp-w7b tests/test_health_api.py
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp=.pytest-tmp-w7b tests/test_health_graph.py
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp=.pytest-tmp-w7b tests/test_health.py
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp=.pytest-tmp-w7b tests/test_reports_api.py

# 2) 窗口与阈值（期望：19:00 / 20:00、180 / 120）
Select-String -Path app/core/config.py -Pattern 'health_checkup_(start|end)'
Select-String -Path app/services/health_service.py -Pattern 'SYSTOLIC_RED|DIASTOLIC_RED'

# 3) 红标起点用例名（点名用）
Select-String -Path tests/test_health_api.py,tests/test_health_graph.py -Pattern 'red_single_component'
```

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-08 | 首版（W7b）：补齐 迭代 8 采集窗口 / 红标 / 子图 / 日报与趋势评测；实测 4 文件 75 passed；点名两例单分量红标；记录日报日期炸弹已修与 7 条已知偏离 |
