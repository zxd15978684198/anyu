# 安语（Anyu）设计问答（加分项）

> 状态：进行中　｜　版本：v1.0　｜　日期：2026-11-08
> 关联：设计记录（交付与观测）（W7a 工作包；§8 已裁决本文不进 §6.4 硬门）
> 上游依据：PRD / 技术设计文档（每题给 file:line，可 Select-String 直接复核）
> 变更记录见文末

12 个「为什么这么设计」的问题。答不上来的地方就说明那处还没落地 —— 不编。

## 1. 为什么三级路由，而不是直接调大模型？

① **安全不能被概率决定**：「我胸口疼」必须 100% 人工告警，交给模型就会随权重/温度时有时无。落在 `app/router/safety.py:115-134`（纯代码、`search` 高召回）。
② **延迟装不下**：端到端 P95 ≤1.5s（PRD line 521），路由段 ≤200ms（PRD line 267）；云端首 token 预算 800ms，把「换一段」送进去立刻破。故固定意图直路由（`app/router/rules.py:215-234`）。
③ **成本与稳定性**：高频指令不产生 token；Laya 挂降级 L2（`app/system_one/questions.py:107-126`），L2 挂退化「L1 结果 + 模板话术」（AGENTS §6）。

三级**升序不可逆**：L0 命中即返回，低层不得被高层覆盖。
## 2. 为什么固定意图用 `fullmatch`，安全规则却用 `search`？

误报与漏报的代价不对称。

固定意图有**副作用**：`match_rules()` 用 `fullmatch`（`app/router/rules.py:215-234`），若改用 `search`，「我今天不想吃药」会命中「我要吃药」并真的执行用药确认。
安全规则漏报的代价是不可逆的：`match_safety()` 用 `search`（`app/router/safety.py:115-134`），任一位置命中即 alert，多派一次人可接受。

为让 `fullmatch` 不至于一字不能多，`_clause_candidates()`（`app/router/rules.py:204-212`）给两级候选：整句在前、按 `[，,。！？?；;]` 切的分句在后。这是 迭代 6 复评 69.1% → 99.1% 的主因。
## 3. 为什么 L2 不在 `route()` 里？

因为 L2 是**循环**，`route()` 是**函数**。

`route()`（`app/router/routing.py` 模块 docstring）只做 L0→L1，返回一个 `RouteDecision`；L2 是 plan→tool→observe→plan→speak（技术设计文档 line 823–831），一次调用表达不了。L2 入口是 `CompanionAgent.run_turn()`，自己写 `layer="L2"`（`app/agents/companion.py:805`）。

**分层不按「谁重要」切，按「有没有内部循环」切。**
## 4. 为什么 `elder_id` 不能来自模型？

一旦允许，绑定表就形同虚设。

`elder_id`/`thread_id` 只从服务端 `runtime_context` 注入；`require_identity()`（`app/core/runtime_context.py:241`）读不到就抛 `IdentityNotSetError`，**绝不**退回模型传来的值（同行 docstring 行 55–56）。`app/core/runtime_context.py:114-118` 进一步禁止 staff 自带 `elder_id` —— 能操作哪位长者只能由服务端读绑定表推出。

模型侧是**递归摘除**：`_strip_identity()`（`app/agents/companion.py:245-268`），调用点 `app/agents/companion.py:412`，计数 `companion_identity_forged_total`（行 418）。**不中断对话**：长者没做错事，不该因模型胡写而听到无来由的拒绝。
## 5. 为什么预算是 8 次、熔断是 3 次？为什么两个数？

两个数管两种坏法，互不替代。

- **8** 挡「总量失控」（PRD FR-DLG-10，line 275）：一次 plan 可能不调工具、也可能连调三个，没有总量上限，一个坏 prompt 能烧穿单轮成本。
- **3** 挡「单点打转」（技术设计文档 line 841）：模型对同一工具反复试同一参数是典型循环退化 —— 8 次里可以 8 次都是 `get_weather`，总量上限管不住它。

窗口 60s（`app/core/config.py:192-193`），实现 `ToolBreaker`（`app/agents/guardrails.py:112-171`），半开区间 `(now-60s, now]`，用 `time.monotonic`（不受系统时间校正影响）。

反向验证：`max_tool_calls_per_turn=1` 时 `TurnBudget()` 连试返回 `[True, False, False]`，`tests/test_agents_companion.py` 2 红（迭代 6 §6.1）。
## 6. 预算与熔断为什么是**并联**闸门？

`Guardrail.check()`（`app/agents/guardrails.py:270`）顺序写死在 docstring 行 10–20：预算 → 熔断 → 消费预算+查缓存 → allow。

串联会出两个 bug：预算失败即**不记**熔断窗口 → 预算恢复后窗口是空的，坏工具瞬间打满；熔断失败仍**扣**预算 → 一个坏工具能烧光全轮预算。

故 `reject_budget` 不触碰熔断状态、`reject_breaker` 不消耗预算但计入窗口 + 本 turn latch（`app/agents/guardrails.py:40` 的 `GuardrailAction` 是四值闭集，调用方可一次穷举处置）。
## 7. 幂等键为什么这么设计？

两个键，管两件事（`app/models/medication.py` docstring 行 9–12）：

| 键 | 列 | 管什么 |
| --- | --- | --- |
| `uq_medication_events_plan_id_scheduled_at` | `(plan_id, scheduled_at)`（`app/models/medication.py:111-114`） | 同计划同到点只产生一条事件 —— 定时重放/重启/重试都不能多记一次服药 |
| `uq_medication_attempts_event_id_attempt_no` | `(event_id, attempt_no)`（`app/models/medication.py:228-229`） | 同一次触达只记一条 —— T0/+5/+10/+15 各算一次 |

为什么唯一约束里**没有** `resident_id`：`plan_id` 已唯一确定长者（药单挂长者名下），再拼一列是冗余，还让「同一计划误绑到两个长者」变成可能。**唯一约束应恰好覆盖业务唯一性，多一列就漏一种错误。**
## 8. checkpoint 为什么落独立 schema？

`LANGGRAPH_SCHEMA = "langgraph"`（`app/graphs/checkpointer.py:57`），业务表在 `public`（同文件行 5–8）；配置独立（`app/core/config.py:66-71`）。

① **迁移不打架**：`checkpoints`/`checkpoint_blobs`/`checkpoint_writes` 由 `PostgresSaver.setup()` 建（`app/graphs/checkpointer.py:6-8`），不是 Alembic 管；混进 `public` 会让 autogenerate 当成业务表差异。
② **连接池隔离**：checkpoint 写是小事务高频，不跟业务查询抢连接。
③ **可整体丢弃**：checkpoint 是「断点恢复」的增强能力，不是事实来源。

配套一个不对称：`health_checkup_graph` 取不到就降级 `MemorySaver`（`app/graphs/health_checkup_graph.py:333-356`，丢一次采集还能再采）；`medication_confirmation_graph` **原样抛** `CheckpointerUnavailableError`（`app/graphs/medication_confirmation_graph.py:41-45`）—— 用药状态丢了长者可能漏服。
## 9. pgvector 维度为什么是 1024？

`EMBEDDING_DIM = 1024`（`app/models/memory.py:42`、`app/models/knowledge.py:55`），配置位 `embedding_dimension`（`app/core/config.py:165`）。

**这个数是被一次事故改下来的**。原定 1536（对齐 text-embedding-3-small 一档）。2026-11-11 接上 `BAAI/bge-m3`（SiliconFlow）后实测 dense 输出是 **1024** 维：`HttpEmbeddingProvider._parse` 判 `bad_payload/bad_dimension` → `memory_service._embed_text` 接住 → **照常写行，`embedding=NULL`**。链路不报错，表现是「记忆写进去了、一条都召回不出来」，日志里还没有维度数字。当天定维 1024，迁移 `0013_embedding_dim_1024` 重建两列（先清空 9 行 1536 维的确定性噪声种子，`ALTER ... TYPE vector(1024)` 且**不带 `USING` cast**——那个 cast 是截断）。

为什么不做「运行时弹性」（无维度 `vector` + 行级 `embedding_model` 标签）：pgvector 的无维度列不能建索引、`<=>` 跨维度直接报错、每处召回 SQL 都要补维度过滤（漏一处就是错）。定死维度 + 一版迁移是更小的复杂度。

代价照旧：绑定供应商维度，换模型 = 改配置 + 改两处常量 + 一版迁移 + 旧向量全部重算。**启动探活**（`app/main.py::_probe_embedding`）会在启动时真打一次并打印 provider / 维度 / 耗时——「配了但不通」从此有日志，不用再靠读代码推断。

别混淆：声纹是 192 维（`voiceprint_embedding_dimension`，`app/core/config.py:202`，3D-Speaker CAM++ 输出），两回事。
## 10. 为什么召回必须带 `elder_id`？

跨长者串一条记忆 = 一次数据泄露，而它**看起来不像 bug**。

实测口径（`设计记录（记忆用药娱乐）` §6.1）：召回是**单条 select** 同时带 `elder_id` / `status='active'` / category / `embedding IS NOT NULL` —— 不是「先查再过滤」，那样漏一行就泄露。反向验收：去掉 `elder_id` 条件 → `tests/test_gates.py` 3 红（迭代 9 §6.1）。

更硬的一条：召回失败则**本轮不加载记忆，不得臆造**（AGENTS §6）。宁可回答平淡，也不编「记得您上周说过」。
## 11. 脱敏为什么是 fail-closed（缺件即红）？

因为「没跑通」和「没问题」看起来一样，后果相反。

`tests/eval_gate.py` docstring 行 15–18 写明：敏感召回是硬指标，缺件不能让门变绿。`GateMetric`（`tests/eval_gate.py:125-158`）区分三种非通过态：`threshold is None` 只报告、`value is None` 跳过、`error is not None` → **FAIL**。

更根本的是「规则只写一份」：`REDACTION_RULES`（`app/core/redact.py:279`）同时被日志格式化器、审计导出、`scripts/scan_logs.py` 消费 —— 三处各写一套就会出现「日志脱了、导出没脱」而 grep 零命中，泄露照旧、扫描器还说没事。

反向用例：摘掉 logging 层脱敏后 `scan_logs.py` 必须 exit 1（命中 phone + id_card，迭代 9 §6.1 已实测）。
## 12. 提示注入为什么前移到输入层？

因为等模型读完再查，注入已经生效了一半。

输入层命中直接整轮短路：**不花 token、不触工具**，`layer="L0"`、`degraded="injection_blocked"`（`app/agents/companion.py:708-736`）。

三层分工（`app/core/injection.py`）：输入分类 `classify_input()`（行 370）拦意图改写/越权话术；意图白名单 `check_intent()`（行 413）拦闭集外意图与「批量替他人办」；输出校验 `check_output()`（行 471）拦手机号/身份证/他人姓名。三层都返同一句 `FIXED_REFUSAL`（`app/core/injection.py:72`）—— 话术**不含解释**，解释本身就是信息，它告诉攻击者这里有东西可泄。

## 附：三个高频追问

- **为什么只澄清一次**：`turn_state` 的 `clarified` 标记，首次返澄清、二次升 L2（`app/system_one/questions.py:191-193` / `:176`）。反复追问会把「没听懂」变成「被审问」。
- **为什么两套降级话术**：`REST_DEGRADE`（书面，`app/core/degrade_speech.py:22-27`）与 `SPOKEN_DEGRADE`（口语，行 31–36）。把「联系工作人员」念给长者听会让他以为系统坏了。
- **为什么 `resolve_identity` 永不抛**：设备未绑定/UUID 非法/库异常一律 `(None, None)`（`app/router/wiring.py:76-105`）—— 宁可身份未知走通用模式，也不返回一个「猜的」`elder_id`。

**答题三纪律**：①不给没测过的数（`asr_*` count=0、token 计量未落地、Langfuse 未接 → 说「无数据 + 原因」，见 `docs/HARNESS_DESIGN.md` §10.3）；②不把预算说成实测（1.5s/800ms/300ms 是拆出的工程预算，只有 `route_ms p95 = 0.6ms` 是实测，见 `eval/reports/routing.md`）；③每题都指向行号 —— 说不出 `file:line` 的设计等于没实现。

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-08 | 首版：12 主问 + 3 追问 + 3 条答题纪律，覆盖三级路由、search/fullmatch 取向、L2 不在 route()、身份注入、8/3 双闸、并联闸门、两个幂等键、checkpoint 独立 schema、1536 维度、召回隔离、fail-closed、注入前移。file:line 全部复核。 |
| v1.1 | 2026-11-11 | 第 9 问由「为什么是 1536」改为「为什么是 1024」：记录一次真实事故链（`bad_dimension` → `embedding=NULL` → 召回永远为空 → 日志无维度数字）、迁移 0013 的取舍（不用 `USING` 截断式 cast、种子须重灌），以及「为什么不搞运行时弹性」的三条理由；补启动探活。删除旧结论「当前未接真实 embedding，1536 是契约不是实测」（已由 0013 迁移实测取代）。 |
