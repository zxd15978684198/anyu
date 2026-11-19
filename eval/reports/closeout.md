# 迭代 10 收口三卡 · 端到端验收报告（FR-MEM-01/03/05/06 · FR-ALT-01 · FR-UI-01）

> 状态：已完成 ｜ 版本：v1.0 ｜ 日期：2026-11-12
> 关联：团队任务板 task-14（owner med-e2e，依赖 task-11 / task-12 / task-13）；用例 `tests/test_closeout_e2e.py`；计划件 `设计记录（记忆治理与监护人通知）`
> 上游依据：PRD FR-MEM-01（抽取限定）/ FR-MEM-03（取代判据）/ FR-MEM-05（召回下限）/ FR-MEM-06（取代可追溯）/ FR-ALT-01（家属通知 + 超时升级管理员）/ FR-UI-01（降级录音 120 秒）；实现口径见技术设计文档记忆与通知章节
> 实测环境：Windows / Python 3.12（`.venv`）/ pytest 9.2.x / ruff 0.17.x / SQLAlchemy 2.1.x / 本地 PostgreSQL `anyu_test`（127.0.0.1:5432，已是 alembic head；本轮**未**建库 / upgrade / drop / 起 docker）
> 变更记录见文末

## 0. 结论（先说人话）

| 项 | 实测结论 |
| --- | --- |
| 新用例 | `tests/test_closeout_e2e.py` **6 passed / 0 failed**（首跑 2.88s，复跑 3.05s） |
| 相关六个文件合计 | **157 passed / 0 failed**（15.60s，含本文件 6 例；记忆/告警/升级/录音四个既有文件全绿） |
| 三卡链路是否真的通了 | 是：真库、真服务入口、真 SQL。记忆走 `remember` / `recall` 的库内状态与 SQL 下限；家属通知走 `handle_safety_alert` 的完整建单建通知链；升级走 `escalate_overdue_red_events(now=...)`；录音闸直调 `audio.utterance` |
| ①② 记忆取代与召回 | 有真库证据：矛盾→旧 `superseded` / 新 `active` 且四条都在；高相似不矛盾→都 `active`；`recall` 不返 `superseded`、也不返 distance > 0.65 的条目 |
| ③ 抽取限定 | 有证据：PRD 原句经抽取管线 **0 候选 → 0 行**；对照组「我对青霉素过敏」1 候选 → 1 行（证明是过滤判据，不是链路断了） |
| ④ 监护人通知 | 有证据：红色事件恰 **1** 条 guardian 通知、`payload.recipients == [绑定监护人]`、重放 `duplicate=True` 后仍 1 条、`Identity(kind="guardian")` 经 `list_for_staff` 真能读到 |
| ⑤ 120 秒升级管理员 | 有证据：`now=+121s` 驱动 → 事件 `escalated`、管理员恰 1 条、再拨 10 分钟返回 `[]`、全程仍 1 个 `risk_event` + 1 张工单 |
| ⑥ 录音上限 | 有证据：120 秒 WAV 头 → 200 且 ASR/对话链非空；121 秒 → `HTTPException 413`，`detail` 引用真源 `MAX_AUDIO_SECONDS` |
| 生产缺陷 | **本轮未发现阻断性缺陷**（6 例首跑即绿，无需改 app/）；两条观察见 §4.3，属待裁决口径而非已证缺陷 |
| 未覆盖项 | 7 条，全部登记在 §5；沙箱环境限制（`tmp_path` 不可写 / `node --test` EPERM / `docker` 不可用）如实注明，**不计为失败** |

**一句话**：三张卡「零件单测」之外的**衔接缝**本轮真库跑通了；测不到的部分（真语义 embedding、通知投递状态、前端构建、HTTP 鉴权层、并发）没有装作测到，全部列在 §5。

## 1. 能力 ↔ 用例映射

| 能力 / 验收口径 | 承载实现（只读核对） | 对应用例（`tests/test_closeout_e2e.py`） |
| --- | --- | --- |
| **FR-MEM-03 取代判据**：够像 **且** 语义矛盾才取代 | `app/services/memory_service.py:505` `_supersede_conflicts`（`similarity <= SUPERSEDE_SIMILARITY(0.9)` 跳过；`duplicate` / `contradiction` 才 supersede）+ `:400` `memories_contradict` | `test_contradiction_supersedes_but_similar_preference_coexists`（:244） |
| **FR-MEM-06 取代可追溯**：软失效，两条都在 | `MemoryChunk.status`（active/superseded/deleted） | 同上：四条行齐全、旧条 `superseded` 而非删除 |
| **FR-MEM-05 召回下限**：不相关的不召回、失效的不召回 | `:585` `recall`：SQL 带 `status == "active"` 与 `distance <= 1 - RECALL_MIN_SIMILARITY(0.35)` | `test_recall_hides_superseded_and_below_floor`（:306） |
| **FR-MEM-01 抽取限定**：推测性第三方事件不落库 | `app/services/memory_extraction.py:237` `_drop_reason`（`_SPECULATION_RE` → `speculation`；`_THIRD_PARTY_RE and _FAMILY_EVENT_RE` → `third_party_event`） | `test_speculative_family_event_writes_nothing`（:364） |
| **FR-ALT-01 家属通知**：红色事件通知绑定监护人，只一条 | `app/services/alert_service.py:383` `handle_safety_alert` → `notify_safety_alert_guardians`（:1110） | `test_red_alert_notifies_guardian_once_and_is_readable`（:471） |
| 家属可见范围 | `notification_service.py:227` `_visibility_filters` 的 guardian 分支（按 `ElderGuardian.status='active'`） | 同上：`Identity(kind="guardian")` + `list_for_staff` |
| **FR-ALT-01 超时升级**：120 秒未确认升管理员，幂等 | `app/services/risk_event_service.py:101` `escalate_overdue_red_events`（`status="escalated"` 作幂等闸门） | `test_overdue_red_event_escalates_to_admin_only_once`（:548） |
| **FR-UI-01 录音上限**：单次最长 120 秒 | `app/api/audio.py:121` `MAX_AUDIO_SECONDS=120`；`:129` `MAX_AUDIO_BYTES` 由时长派生 | `test_utterance_accepts_120s_and_rejects_121s`（:666） |

## 2. 跑过的命令与结果（原样）

先设测试库（AGENTS §9 第三条：不设会 TRUNCATE 开发库）：

```powershell
$env:ANYU_TEST_DATABASE_URL="postgresql+asyncpg://anyu:anyu@127.0.0.1:5432/anyu_test"
```

### 2.1 静态门禁

```powershell
.venv\Scripts\ruff.exe check tests/test_closeout_e2e.py
# All checks passed!（首轮 1 个 F401 未用 import，已删；非豁免）

.venv\Scripts\ruff.exe format --check tests/test_closeout_e2e.py
# 1 file already formatted
```

### 2.2 本文件用例

```powershell
.venv\Scripts\python.exe -m pytest tests/test_closeout_e2e.py -q -p no:cacheprovider
# ......                                                                   [100%]
# 6 passed in 2.88s
```

复跑一次（确认无时序依赖、无残留行）：`6 passed in 3.05s`。

### 2.3 相邻六个文件（确认没把别人的用例带红）

```powershell
.venv\Scripts\python.exe -m pytest tests/test_memory_service.py tests/test_memory_extraction.py `
  tests/test_alert_service.py tests/test_risk_event_escalation.py tests/test_audio_utterance.py `
  tests/test_closeout_e2e.py -q -p no:cacheprovider
# 157 passed in 15.60s
```

没跑全量 pytest（任务卡明确禁止）；上表是这三张卡直接相邻的六个文件，其余模块本轮未触碰。

## 3. 用例怎么造出来的（关键判据与依据）

### 3.1 距离可控的假 embedding：把「相似度」变成用例里的显式参数

`_FixedProvider`（本文件内）把每条文本映射到 1024 维单位向量的某一轴：

| 文本 | 轴 | 与同轴查询的距离 |
| --- | --- | --- |
| 「我喜欢吃苹果」/「我不喜欢吃苹果」 | 0 | 0.0（`> SUPERSEDE_SIMILARITY`，够像） |
| 「我每天早上喝一杯热水」/「…温水」 | 1 | 0.0（够像，但**不矛盾**） |
| 「今天天气怎么样」 | 7 | 1.0（`> 1 - 0.35 = 0.65`，被召回下限挡掉） |

于是「够像且矛盾」「够像但不矛盾」「不够像」三种输入都能稳定复现，不依赖任何外部 embedding 服务、不 monkeypatch 检索层。
契约与真 provider 一致（`dimension` / `available` / `async embed`），所以能从 `remember` / `recall` 的服务入口一路走到 SQL。

### 3.2 savepoint 让「服务内部 commit」只释放到外层事务

夹具与 `tests/test_medication_chain_e2e.py` 同款：`NullPool` 引擎 → 一条真连接 → 外层事务；
`AsyncSession(..., join_transaction_mode="create_savepoint")`，`db_session_factory` 在其它文件里绑的是同一连接。

本文件的服务入口都接收 `session`（`remember` / `recall` / `handle_safety_alert` / `escalate_overdue_red_events` / `utterance`），
内部 `commit()` 只是释放 savepoint，用例结束 `transaction.rollback()` 一次清干净 —— **真库、真事务语义、零残留**，不需要 teardown 逆向删除。

### 3.3 时间靠参数注入，不 sleep

- ② ⑤ 的锚点写死 `OCCURRED_AT = 2026-11-12T04:30:00Z`（UTC 直写，避免机器时区漂）；
- ⑤ 的「超时」用 `escalate_overdue_red_events(db, now=OCCURRED_AT + 121s)` 拨表，**不是**等 120 秒；
- 幂等重放再拨 10 分钟（`+600s`），断言候选集不再命中。
- 告警幂等键按「分钟」取桶，同一 `OCCURRED_AT` 的两次调用才落在同一个桶里 —— 重放断言因此是确定的。

### 3.4 ⑥ 用 44 字节 WAV 头声明时长，不搬几 MB 静音

`_wav_header_only(duration_ms)`（:600）只写 RIFF/fmt/data 三个块头，`data_size = frames * 2`。
服务端 `_wav_duration_ms`（`app/api/audio.py:191`）按 `wave.getnframes()/framerate` 算时长，**在 ASR 之前**判闸，
所以 120s 与 121s 的分界不需要真实采样数据；这也顺带证明「时长判定不依赖客户端 `duration_ms` 字段」。

### 3.5 ④ 断言必须带 `audience`，否则会和护工那条撞车

一次红色告警会建 **两条** `kind="safety_alert"`：护工一条（`assignee_staff_id=主责护工`）、家属一条（`assignee_staff_id=None`）。
家属查询因此同时带 `payload['audience'].astext == 'guardian'` 与 `payload['risk_event_id'].astext`（与生产侧 `_find_guardian_alert_notification` 的命中条件一致），
再断言「总数 == 2」证明重放没加行、且护工那条没被算进家属名额。

### 3.6 ⑤ 的幂等靠状态闸门，不靠代码去重

`escalate_overdue_red_events` 的候选条件是 `severity='red' AND status='open' AND acknowledged_at IS NULL AND created_at <= cutoff`。
第一次升级把 `status` 改成 `escalated` 后，第二次调用天然不命中（返回 `[]`），所以断言的是「返回空 + 通知条数仍 1 + 事件/工单仍各 1」，而不是某个内部计数器。

## 4. 反向验收（证明「闸门真的在拦」）

| # | 反向断言 | 若闸门失效会怎样 |
| --- | --- | --- |
| 1 | ③ 对照组「我对青霉素过敏」能抽出 1 候选并落 1 行 | 若链路断了，两边都是 0 行，测试会误判「过滤有效」 |
| 2 | ② 同轴查询只回新条（旧条被 `status` 过滤）；正交查询返回 `[]`（被 `distance` 下限过滤） | 去掉任一条 SQL 条件，这两个断言立刻变红 |
| 3 | ④ 重放后 `guardian_rows` 仍 1 条、`safety_alert` 总数仍 2 | 幂等键失效会变 3、4 条 |
| 4 | ⑤ 再拨 10 分钟返回 `[]`，且 `risk_event` / 工单各 1 | 状态闸门失效会重复升级、重复发管理员通知 |
| 5 | ⑥ 121 秒抛 413 而不是 422/200，`detail` 含 `120s` | 时长闸写成静态值/被客户端字段绕过，这条会变绿或抛 422 |
| 6 | ⑥ `MAX_AUDIO_BYTES == MAX_AUDIO_SECONDS * 16_000 * 2 + WAV_CONTAINER_OVERHEAD_BYTES` | 体积闸另写一个数（两处真源），派生断言变红 |

### 4.3 两条观察（口径待裁决，不判缺陷）

- **O1 升级只看 `risk_event.acknowledged_at`，不看工单状态**：若护工在 120 秒内把工单 `acknowledged` 了、但服务端没有把 `risk_event.acknowledged_at` 置位，事件仍会被升级并向管理员再发一条。本轮未验证 ack 路径是否回填 `acknowledged_at`（属 task-12 之外的工单流转范围），建议后续在 `tests/test_risk_event_escalation.py` 补一条「工单已确认后不再升级」的用例；本卡只读，未改生产代码。
- **O2 家属可见范围按「当前绑定」算**：家属解绑（`status != 'active'`）后就看不到历史通知了（`_visibility_filters` 用当前绑定子查询）。若产品要求「历史通知保留可读」，这是口径问题而非缺陷；本轮未造解绑场景，登记为未覆盖。

## 5. 未覆盖项（如实登记，不许当已通过）

| # | 未覆盖 | 为什么没覆盖 | 影响 / 建议 |
| --- | --- | --- | --- |
| **U1** | 真语义 embedding 未接 | 用例全程 `_FixedProvider`（只证明链路与两个阈值方向，不证明「相似度算得准」） | 线上「该取代的没取代 / 该召回的没召回」本轮测不出；建议单独用真模型做一次取代/召回评测 |
| **U2** | 通知**投递**状态未验 | 只断言生成与可见范围（`delivered_at` 为空是正常的未投递态），未驱动 `deliver` 端点 | 「通知建了但从没送达」这类问题测不出；`test_notification_*` 覆盖端点本身，不在本卡 |
| **U3** | 家属端前端未验 | 沙箱 `node --test` 报 EPERM、前端未构建（已知限制，非失败） | 家属端渲染/推送另需真机验证；本报告已 grep 核对前端回执文案与数字 |
| **U4** | 多管理员 / 并发升级未验 | 单管理员、单线程顺序调用；`escalate_overdue_red_events` 的 50 条批量与并发两条升级互斥未造 | 并发下可能各发一条管理员通知；幂等靠 `status` 状态闸门，未做行锁验证 |
| **U5** | ⑥ 未走 HTTP 鉴权链 | 直调 `audio.utterance`（`identity` 显式传入），不经 device JWT / multipart 解析 | 鉴权与 multipart 体积预检由 `test_audio_utterance.py` / `test_audio_security.py` 覆盖；真实 120 秒字节上传也在那边 |
| **U6** | 抽取的热点路径用脚本化假 LLM | `_ScriptedLLM` 直接吐 JSON；真模型是否会自己吐出「可能失业了」这类句子未验 | 过滤判定在 `_drop_reason` 里是确定性的，与模型无关；但真模型的「说 vs 不说」本轮未测 |
| **U7** | 未跑全量 pytest | 任务卡禁止（避免误伤/长时门禁） | 已跑相邻六文件 157 passed；全量门禁留给集成阶段 |

**环境限制（如实记录，不计为失败）**：`tmp_path` 在本沙箱建不了目录 → 全部用例不使用临时目录；`node --test` 因沙箱 EPERM 跑不了 → 前端数字只用 grep 复核；`docker` 不可用 → 未重建库、未起容器，直接连既有 `anyu_test`。

## 6. 复跑

```powershell
$env:ANYU_TEST_DATABASE_URL="postgresql+asyncpg://anyu:anyu@127.0.0.1:5432/anyu_test"
.venv\Scripts\ruff.exe check tests/test_closeout_e2e.py
.venv\Scripts\ruff.exe format --check tests/test_closeout_e2e.py
.venv\Scripts\python.exe -m pytest tests/test_closeout_e2e.py -q -p no:cacheprovider
```

前置：`anyu_test` 库已存在且是 alembic head（本轮未建库、未迁移、未起 docker）。用例自造长者/护工/管理员/监护人、自造向量，
不依赖种子数据、不依赖任何真实 API、不起 APScheduler、不起 aiortc。

## 7. 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-12 | 首版：task-14 三卡衔接验收 6 例（记忆取代/并存、召回下限、抽取限定、监护人通知、超时升级、录音上限）、相邻六文件 157 passed、未覆盖项 U1~U7、观察 O1~O2 |
