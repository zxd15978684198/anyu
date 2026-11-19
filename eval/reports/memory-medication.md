# 迭代 7 记忆 / 用药 / 娱乐 / 反向外呼评测汇表

> 状态：已完成 ｜ 版本：v1.0 ｜ 日期：2026-11-08
> 关联：`设计记录（交付与观测）` §3.1（`eval/reports/` 补齐）；`设计记录（记忆用药娱乐）` §6.1；`设计记录（进度清单）`
> 上游依据：PRD FR-MEM-01/03/07、FR-MED-04/06、FR-CON-06 ｜ 技术设计文档 §3.4 `memory_chunks`、§6 两个业务子图与 checkpointer
> 实测环境：Windows / Python 3.12.13 / pytest 9.2.0 / 本地 PostgreSQL(127.0.0.1:5432) + Redis(6379)
> 变更记录见文末

## 0. 结论（先说人话）

| 能力 | 本轮实测结论 | 证据 |
| --- | --- | --- |
| 记忆五步（写入 / 召回 / 归并 / 衰减 / 容量） | 72 例全绿；召回的每条查询都带 `elder_id` 条件 | §2 / §3 判据① |
| 用药子图（T0 / +5 / +10 / +15，第 4 次超时升级） | 56 例全绿；attempt 上限写死 4，越界直接拒绝 | §2 / §3 判据③ |
| 用药调度与音频清理 | 9 例全绿（19:00 起的 beat 与 72h 清理） | §2 |
| 娱乐点播（越剧 / 京剧 / 换一段 / 停） | 47 例全绿；播放会话落 `media_history`，版权状态走白名单 | §2 / §3 判据⑥ |
| 反向外呼（ICE / 超时 / 无应答 / 播报失败） | 23 例全绿；`rtc_result` 是 5 值闭集，失败一律降级 `fallback_send_text` | §2 / §3 判据⑤ |

**一句话**：迭代 7 五块能力在本轮 5 个测试文件上共 207 例全绿，四条硬判据（`elder_id` 强制、相似度 0.9、attempt 上限 4、重试键幂等）都在代码里可指认；唯一没闭合的是**外呼端到端真实链路**（信令层不分发客户端上行 `answer`），见 §6。

## 1. 被测能力清单与落点

| # | 能力 | 承载模块（文件:行） | 本轮覆盖的验收点 |
| --- | --- | --- | --- |
| 1 | 记忆写入 | `app/services/memory_service.py:205 remember()` | 写入即带回 `elder_id`、`category`、`importance`；provider 不可用不阻断本轮 |
| 2 | 记忆召回 | `app/services/memory_service.py:316 recall()` | top-k 只含本人；`status='active'`；排序按相似度 |
| 3 | 归并 / 失效 | `app/services/memory_service.py:264 _supersede_conflicts()` | 同长者同类相似度 > 0.9 → 旧条 `superseded`，**不物理删** |
| 4 | 衰减 | `app/services/memory_service.py:455 decay()` | 90 天未召回 → `importance × 0.5`（`:71-72`） |
| 5 | 容量 | `app/services/memory_service.py:506 enforce_capacity()` | 超 500 条按「最不重要 + 最久没被召回」软删（`:75`） |
| 6 | 记忆删除留痕 | `app/services/memory_service.py:583 delete_by_elder()` | 删除与 `AuditLog(action="delete_memory")` 同事务 |
| 7 | 用药子图 | `app/graphs/medication_confirmation_graph.py`（1088 行） | `remind → await_confirm → escalate → terminal`；attempt 越界拒绝（`:406`） |
| 8 | 用药触达幂等 | `app/services/medication_service.py:392 record_attempt()` | 键 `(event_id, attempt_no)`，重复请求返回原行 |
| 9 | 用药调度 | `app/scheduler/medication.py`（757 行）、`app/scheduler/maintenance.py`（283 行） | 到点提醒、5 分钟 beat、原始音频 72h 清理 |
| 10 | 娱乐点播 | `app/services/media_service.py:214 play()` / `:269 finish()` | 一次播放会话写一行 `media_history`；版权白名单 `public_domain` / `licensed_sample`（`:76`） |
| 11 | 反向外呼 | `app/audio/outbound.py`（487 行） | `RTC_RESULTS` 5 值闭集（`:104-109`）；失败降级文本播报 |

## 2. 用例规模与结果（逐个文件贴实际 passed）

命令（每行替换 `<file>`，见 §7 复跑）：

```
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp=.pytest-tmp-w7b <file>
```

| 测试文件 | 覆盖范围 | 实测结果 |
| --- | --- | --- |
| `tests/test_memory_service.py` | 记忆五步 + 召回隔离 + 删除留痕 | **72 passed** in 7.79s |
| `tests/test_medication_graph.py` | 子图流转 / 四次超时 / 拒绝 / 重试幂等 / 断点恢复 | **56 passed** in 9.31s |
| `tests/test_scheduler_medication.py` | 到点提醒、5 分钟 beat、72h 音频清理 | **9 passed** in 3.93s |
| `tests/test_media_service.py` | 点播 / 换一段 / 停 / `media_history` / 版权白名单 | **47 passed** in 3.42s |
| `tests/test_outbound.py` | ICE 失败 / 超时 / 无应答 / 播报失败降级 | **23 passed** in 1.39s |
| 合计 | 5 个文件 | **207 passed**，0 failed |

两条环境注记（都是为了复跑能重现，不是缺陷）：

1. **数据库必须先起来**：本轮开跑前 `anyu-postgres` / `anyu-redis` 均为 `Exited (255)`，已用 `docker start anyu-postgres anyu-redis` 恢复。`tests/conftest.py:28-44` 在库不可达时直接 `pytest.fail`（只有 `ANYU_ALLOW_DB_SKIP=1` 才 skip），不会静默跳绿。
2. **basetemp 不能放 `%TEMP%`**：首轮用 `--basetemp=%TEMP%\sm-w7b-base` 时，`tests/test_scheduler_medication.py` 的 2 例 `test_audio_cleanup_*` 在 tmp 目录清理阶段报 `PermissionError: [WinError 5]`（用例体已通过，错误发生在 teardown 的 `rmtree`）；换成仓库内 `--basetemp=.pytest-tmp-w7b`（`.gitignore:112` 已忽略 `.pytest-tmp/` 家族）后 **9 passed**。本报告采用仓库内 basetemp 的结果。

## 3. 关键判据（数值都能指到代码行）

| # | 判据 | 落点 | 本轮核对方式 |
| --- | --- | --- | --- |
| ① | 记忆查询**强制** `WHERE elder_id` | `app/services/memory_service.py:282`、`:353`、`:375`、`:420`、`:477`、`:531`、`:546`、`:563`、`:605` | `Select-String -Path app/services/memory_service.py -Pattern 'MemoryChunk\.elder_id == elder_id'` → **命中 10 处**（其中 `:18` 是模块文档字符串里的口径说明，其余 9 处是真实查询条件） |
| ② | 冲突归并相似度 **> 0.9** | `app/services/memory_service.py:68 SUPERSEDE_SIMILARITY: Final[float] = 0.9`；判定 `:295`；置 `superseded` `:299` | 代码核对 + `test_memory_service.py` 全绿 |
| ③ | 触达上限 **4 次** | `app/graphs/medication_confirmation_graph.py:203 max_attempts: int = 4`；越界拒绝 `:406`；服务层 `app/services/medication_service.py:212 max_attempts()` | 代码核对 + 四次超时用例在 `test_medication_graph.py` 内全绿 |
| ④ | 重试键幂等 `(event_id, attempt_no)` | 表约束 `app/models/medication.py:228-229 uq_medication_attempts_event_id_attempt_no`；写入 `app/services/medication_service.py:392`（先查同键，命中即返回原行 `:408` / `:434`） | 代码核对 + 幂等用例全绿 |
| ⑤ | 外呼结果 5 值闭集 | `app/audio/outbound.py:104-109`：`ok` / `rtc_failed` / `timeout` / `no_answer` / `ice_failed` | 代码核对 + `test_outbound.py` 23 例全绿 |
| ⑥ | 娱乐版权白名单 | `app/services/media_service.py:76 AUTHORIZED_COPYRIGHT_STATUSES = ("public_domain", "licensed_sample")` | 代码核对 + `test_media_service.py` 47 例全绿 |

补充两条与 PRD 口径对齐的说明：

- 衰减与容量是 **FR-MEM-07** 的默认值（90 天 / ×0.5 / 500 条），在模块常量里写明，`D7-Q2` 曾列为待裁决项，当前实现取文档默认值。
- `superseded` 只置状态不物理删，与「记忆归并留痕」的口径一致；`superseded_by` 列**不存在**（见 §5 D11），新旧条靠 `category + 相似度 + 时间` 关联，这是与计划原文的已知差异。

## 4. 三条反向验收（复述 迭代 7 执行实况）

来源：`设计记录（记忆用药娱乐）`（§6.1 执行实况）。本轮 W7b 只补报告，按工作包约束**未改 `app/**`、未重跑**这三条；阶段结果如下。

| 反向操作 | 预期 | 实测结果 | 说明 |
| --- | --- | --- | --- |
| 把 `app/services/memory_service.py:282` 的 `MemoryChunk.elder_id == elder_id` 改成 `true()` | 跨长者记忆串门 → 召回用例大量变红 | **17 failed / 55 passed** | 17 红分布在该文件的隔离与召回用例上，说明 `elder_id` 条件确实被用例钉住 |
| 把 `medication_service.max_attempts()` 改成返回 1 | 只剩 1 次触达 → 升级链路全断 | **3 failed** | 三个用例：`test_escalate_creates_red_work_order_and_terminalizes_event`、`test_graph_four_timeouts_create_work_order`、`test_timeout_schedules_next_attempt_on_the_five_minute_beat` |
| 让 `get_checkpointer()` 临时返回 `MemorySaver()`（内存态） | 重启后 attempt 丢失 → 断点恢复失败 | **1 failed** | `test_graph_recovers_after_checkpointer_restart`，红在 `snapshot["attempt_no"] == 1`（真 PG checkpointer 时该值为 2） |

这三条的价值在于：它们不是「功能演示」，而是把「去掉关键约束就必然变红」钉成了回归网——后续任何重构若悄悄放开 `elder_id`、抬高/压低 attempt 上限、或把 checkpointer 换成内存实现，CI 会直接红。

## 5. 已知偏离（照 迭代 7 进度清单）

| 编号 | 偏离内容 | 影响 | 来源 |
| --- | --- | --- | --- |
| D5 | 四次超时后**只建工单不写 `escalated` 流转** | 计划原文写「created → escalated」，但 `work_orders.status` 六态 CHECK 里没有 `escalated`；实现取「建单 `new` + 事件 `escalated`」，`test_escalation_writes_no_transition_because_schema_forbids_escalated` 把「不写」也钉住 | `设计记录（进度清单）` D5 |
| D9 | 外呼端到端只到**媒体层** | `create_offer()` / `accept_answer()` 已用真对端测过，但信令层不消费客户端上行 `answer`，pad 无接听分支 → 真实端到端未通（见 §6） | 同上 D9 / R1 |
| D10 | 拒绝服药的派单由 Lead 补齐 | `medication_service.refuse_event()` + 子图拒服钩子；原实现只记风险事件不建单，不满足 PRD line 321 | 同上 D10 |
| D11 | 记忆表无 `superseded_by` 列；`category` 而非 `visibility`；`importance` 取值 [0,1]；归并只在同 category 内做 | 与计划原文措辞有差，语义不变；跨类别相似不 supersede，避免误删家属可见记忆 | 同上 D11 |
| D12 | `tests/test_health_api.py:60` 的日期改运行时取上海当天 | 属 迭代 8 文件，但登记在 迭代 7 清单；消除「跨日即红」的假失败（详见 `eval/reports/health-checkup.md`） | 同上 D12 |
| 质量门残留 | `ruff format --check` 余 2 个外部会话新文件（`app/models/audit.py`、`app/scheduler/health.py`） | 不影响功能；`ruff check` 全绿 | 迭代 7 §6.1 未达成项② |

## 6. 未验证项（没测到就是没测到）

| # | 未验证项 | 原因 | 影响 / 后续动作 |
| --- | --- | --- | --- |
| 1 | **外呼真实端到端**（长辈端振铃 → 接听 → 音频接通） | 信令层无 inbound `answer` 分发，pad 未实现接听分支；当前只验证了媒体层 offer/answer 与服务层降级 | PRD FR-MED-05 的「真外呼」未闭合。补齐三步：signaling 增加 inbound `answer` 分发 → pad `createAnswer` 回发 → 处理 glare（同一设备并发 offer） |
| 2 | 真实 WebRTC 媒体面（本机无 TURN / 无浏览器对端） | 本轮只跑单元与集成用例，未起 coturn + 浏览器 | 端到端音质、丢包、ICE 候选收集未验证；属交付演示范围，由端到端联调补 |
| 3 | 娱乐内容真实版权源 | 白名单是枚举值（`public_domain` / `licensed_sample`），不接真实版权库 | 内容合规判定正确，但「片源可播」未验证 |
| 4 | 记忆 embedding 远端 provider 的真实延迟 | 用例走注入的 provider 桩；真实 embedding 服务的 p95 未计时 | 召回延迟不进本轮结论；如需要，按 迭代 5 的方式另采 |

## 7. 复跑与证据链

```powershell
# 0) 前置：本地库与缓存
docker start anyu-postgres anyu-redis
docker ps --format '{{.Names}}  {{.Status}}  {{.Ports}}'

# 1) 五个测试文件（逐个跑，贴各自 passed）
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp=.pytest-tmp-w7b tests/test_memory_service.py
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp=.pytest-tmp-w7b tests/test_medication_graph.py
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp=.pytest-tmp-w7b tests/test_scheduler_medication.py
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp=.pytest-tmp-w7b tests/test_media_service.py
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp=.pytest-tmp-w7b tests/test_outbound.py

# 2) 判据①：召回是否处处带 elder_id（期望命中 10 处）
Select-String -Path app/services/memory_service.py -Pattern 'MemoryChunk\.elder_id == elder_id' | Measure-Object

# 3) 判据③④：上限与幂等键
Select-String -Path app/graphs/medication_confirmation_graph.py -Pattern 'max_attempts: int = 4'
Select-String -Path app/models/medication.py -Pattern 'uq_medication_attempts_event_id_attempt_no'
```

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-08 | 首版（W7b）：补齐 迭代 7 记忆 / 用药 / 娱乐 / 反向外呼评测汇表；实测 5 文件 207 passed，判据与三条反向验收按 迭代 7 §6.1 复述，外呼端到端列为未验证项 |
