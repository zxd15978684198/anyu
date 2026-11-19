# 安语（Anyu）Agent Harness 设计

> 状态：已冻结　｜　版本：v1.2　｜　日期：2026-11-17
> 关联：设计记录（交付与观测）（W7a 工作包）
> 上游依据：PRD line 124 / 131 / 267 / 275 / 521 / 539（验收）｜技术设计文档 line 819–870（实现）
> 变更记录见文末

本文回答一个问题：**一句话从进来到出去，经过了哪几层、每层凭什么做决定、做不了时退到哪里、怎么被观测与卡住。**

不含：数据模型 DDL、接口字段契约、威胁模型（各归 ER / 接口契约 / `docs/SECURITY.md`）。

---

## 1. 为什么是三级，而不是直接调大模型

直接调大模型的三个已知问题在本项目都会真实发生：

1. **安全不能被概率决定。** 「我胸口疼」必须 100% 落到人工告警，而不能指望模型这一次恰好把意图判成 `alert`。PRD line 124 把三级路由写成硬约束，正是为了让安全判定发生在**代码**里，而不是在采样里。
2. **延迟预算装不下。** 端到端 P95 ≤ 1.5s（PRD line 521 AC-40、line 539 §5.3），其中路由段预算 ≤200ms（AGENTS §6、PRD line 267 FR-DLG-02）。云端 LLM 首 token 预算是 800ms，把「换一段」「大声点」这类高频指令也送进去，预算立刻破。
3. **成本与稳定性。** 高频固定意图走本地分类器，不产生 token；云端 LLM 抖动时可退化到 L1 结果 + 模板话术，不必整轮失败。

三级是**升序且不可逆**的：L0 命中即返回，不进入 L1；L1 高置信即返回，不进入 L2。低层不得被高层覆盖（AGENTS §6）。

---

## 2. 三级总览与延迟预算

| 层级 | 承担者 | 实现落点 | 延迟预算 | 超预算/不可用时的行为 |
| --- | --- | --- | --- | --- |
| **L0 规则层** | 关键词/正则 ≤10 条，进程内纯代码 | `app/router/safety.py`、`app/router/rules.py`、`app/router/routing.py` | 路由 ≤200ms | 无降级：规则层失效即 P0 缺陷 |
| **L1 System One** | Laya 本地开源权重，CPU 推理，OpenAI 兼容 HTTP | `app/system_one/client.py`、`app/system_one/questions.py` | 单句 ≤200ms | 加载/连接失败 → 全部走 L2 并打 `laya_unavailable` |
| **L2 System Two** | DeepSeek / Qwen 兼容 API，经统一 LLM 适配器 | `app/services/llm_service.py`、`app/agents/companion.py` | 首 token ≤800ms | 重试 ≤2 次后退化为「L1 结果 + 模板话术」并提示人工（AGENTS §6） |
| 语音识别 | 第三方流式 ASR | `app/audio/**` | 首包 ≤300ms | 按住说话整段上传 → 页面输入框 |
| 语音合成 | 第三方流式 TTS | `app/audio/**` | 首包 ≤300ms | 大字幕 + 预置音频 |

**总预算**：端到端 P95 ≤ 1.5s（ASR + 路由 + 生成 + TTS 合计，PRD line 521）。上表逐层毫秒数是按 1.5s 目标拆分的**工程预算**，不是 PRD 明文数值；调整必须与本文同步（AGENTS §6 脚注）。

预算的可行性由本地实测支撑，但实测的**不是真实云 API**，见 §10 的三条口径说明。

---

## 3. L0 规则层

### 3.1 职责与扫描顺序

`match_l0(text)`（`app/router/rules.py:237`）是 L0 的统一入口，扫描顺序本身就是契约：

```
match_l0 = safety → rules → None
           ↑         ↑
    一票否决     固定意图直路由
```

顺序不可调换：若先跑固定意图，「我不舒服，帮我放段越剧」会命中 `opera` 而被当成点播请求，急症被吞掉。因此 `safety` 先跑、命中即返回（`app/router/safety.py:115-134`）。

### 3.2 安全规则：3 条（`RULE_COUNT = 3`，`app/router/safety.py:62`）

| rule_id | 触发面 | 实现 | 命中后 |
| --- | --- | --- | --- |
| `safety_emergency` | 胸痛 / 喘不上气 / 摔倒 / 坠床 / 出血 / 咽不下 / 呛 / 认不得 / 糊涂等 | `_EMERGENCY_PATTERN`（`app/router/safety.py:47-58`） | `intent="alert"`、`sensitive=True`、直接进人工告警 |
| `safety_harm` | 自伤 / 他伤表述 | `_HARM_PATTERN`（`app/router/safety.py:63-73`），三处**窄负向排除**：`不想活(?!动)`、`打我(?!电话\|手机)`、`有人打(?!门\|电话)` | 同上 |
| `safety_request_human` | 主动要人（「要人工」「叫护工」） | `app/router/safety.py:77-82`，`(?<!不)要人工` | 同上 |

三个工程要点：

- **高召回优先**：`match_safety()` 用 `pattern.search`，命中**任意位置**即判 alert，不做全句锚定。误报的代价是「多派一次人」，漏报的代价是不可逆的。
- **窄负向排除而非长负向表**：只排除「不想活**动**」「打我**电话**」「有人打**门**」这类日常语，避免为了压误报把真阳性一起压掉。
- **词表写死在代码**（`app/router/safety.py`），不进 prompt、不进配置。理由：配置可被降级加载、prompt 可被模型改写；阈值与分级必须写在代码里（AGENTS §5 高风险文件条款）。
- 模块级 `assert len(SAFETY_RULES) == RULE_COUNT`（`app/router/safety.py:265`）在 import 期就把「条数漂移」变成启动失败。

### 3.3 固定意图：7 条（`RULE_COUNT = 7`，`app/router/rules.py:57`）

`DIRECT_RULES`（`app/router/rules.py:83-199`）覆盖：播放控制（换一段/停）、音量（大声点/小声点）、戏曲（越剧/京剧）、信息（几点了/今天天气）、用药（我要吃药）、告别（再见）、重复（再说一遍/重复一遍）。

匹配用 `fullmatch` 而非 `search`（`app/router/rules.py:215-234`），因为固定意图一旦用 `search`，「我今天不想吃药」就会命中「我要吃药」并**真的**执行用药确认。

为了不让「太严」变成「一个字都不能多」，`_clause_candidates()`（`app/router/rules.py:204-212`）提供两级候选：**整句在前、分句在后**，分句按 `[，,。！？?；;]` 切（`_CLAUSE_SPLIT`，`app/router/rules.py:57`）。「奶奶，今天天气怎么样？」

→ 分句「今天天气怎么样」`fullmatch` 通过。这条口径是 迭代 6 复评从 69.1% 提到 99.1% 的主要改动之一（`设计记录（三级路由）` §6.1）。

### 3.4 L0 的输入输出

- 输入：用户文本 `str`（已由 ASR 落成文本，本层不接触音频）。
- 输出：`RouteDecision` 或 `None`。`RouteDecision` 是 frozen dataclass（`app/router/decision.py`），字段顺序即接口契约：`elder_id` / `thread_id` / `layer` 必填，其余带默认值。
- 身份补全在 `route()` 里做（`app/router/routing.py:79-89`）：L0 短路命中后用 `dataclasses.replace` 补齐 `elder_id` / `thread_id` / `trace_id` 与 `route_ms`，L0 规则本身**不碰身份**。

### 3.5 L0 无降级

规则层是纯文本纯函数，无 I/O、无网络、无日志。它不存在「不可用」这个状态：如果它跑不起来，就是 P0 缺陷，不是降级场景（AGENTS §6 表内「不存在降级」一栏）。

---

## 4. L1 System One（Laya）

### 4.1 一次前向出四个信号

Laya 一次前向输出 `intent` / `is_sensitive` / `confidence` / `is_out_of_domain` 四个信号（AGENTS §1 表、技术设计文档 §4）。七类 intent：闲聊 / 用药查询 / 戏曲点播 / 电影点播 / 天气查询 / 健康采集 / 其他；另有双人间隐私收敛标记 `privacy`。

### 4.2 `decide_l1()` 的六级判定（`app/system_one/questions.py:91-201`）

判定矩阵的次序在模块 docstring 行 9–18 写死，**次序本身是设计**：

| 序 | 条件 | 结果 | 落点 |
| --- | --- | --- | --- |
| ① | `is_sensitive = True` | `layer=L1`、`intent=alert` —— **先于置信度** | `app/system_one/questions.py:133` |
| ② | `is_out_of_domain = True` | 升 L2 | `app/system_one/questions.py:150` |
| ③ | `confidence >= threshold` | `layer=L1` 返回 | `app/system_one/questions.py:165` |
| ④ | 低置信且**本 turn 未澄清过** | `layer=L1` + `degraded=low_confidence_clarify`，写 `turn_state` | `app/system_one/questions.py:191-193` |
| ⑤ | 低置信且**已澄清过** | 升 L2 + `degraded=low_confidence_twice` | `app/system_one/questions.py:176` |
| ⑥ | Laya 抛任何异常 | 升 L2 + `degraded=laya_unavailable`（warning，不抛） | `app/system_one/questions.py:107-126` |

阈值来自配置而非写死：`threshold = client.settings.laya_confidence_threshold`（`app/system_one/questions.py:130`），默认 0.85（`app/core/config.py:92`，口径 PRD line 131）。

①先于③是关键：一个被判为 sensitive 的句子即使 `confidence=0.99` 也只能走 alert，不能因为「很有把握」而被当成普通闲聊。

### 4.3 「只澄清一次」怎么实现

`turn_state` 是一个每轮清空的 dict（`app/router/wiring.py:52-73`），键为 `clarified` / `clarified_at` / `last_confidence`（`app/system_one/questions.py:50-52`）。第一次低置信写入 `clarified=True` 并返回澄清话术；第二次进 `decide_l1` 时命中④分支的兄弟⑤，直接升 L2。PRD line 131 与 FR-DLG-03（line 268）要求「只澄清一次」，若连续两次低置信仍反复追问，长者体验会崩。

注意 `wire_route()` 每轮把 `turn_state` 置空——**垫层没有多轮状态**，这是刻意取舍：多轮状态一旦跨轮存活，就要处理清理与并发，而当前澄清一次的场景在单轮内即可闭合。代价是「澄清后第二轮」必须由调用方把 `turn_state` 传回来，这在 `run_turn` 的调用链里成立，在裸调 `route()` 时不成立。

### 4.4 L1 的降级

- **Laya 不可用**（加载失败 / 连接拒绝 / 超时 / 返回结构不对）：任何异常在 ④ 分支被吞成 warning，返回 `layer=L2` + `degraded=laya_unavailable`。全量 110 条在断 Laya 情况下跑通的实测见 `设计记录（三级路由）` §6.1「反向验收」。
- **低置信**：澄清一次，仍模糊升 L2（上表④⑤）。
- L1 永不返回 `degraded=None` 之外的未定义值：`DEGRADED_LAYA_UNAVAILABLE` / `DEGRADED_LOW_CONFIDENCE_CLARIFY` / `DEGRADED_LOW_CONFIDENCE_TWICE` 三个常量（`app/system_one/questions.py:63-66`）是闭集。

---

## 5. L2 主控 Agent

### 5.1 L2 不在 `route()` 里

`route()` 只做 L0 → L1，不产生 L2 决策（`app/router/routing.py` 模块 docstring）。原因：L2 是**循环**（ReAct），一次 `route()` 调用无法表达「工具调用 → 观察 → 再决策」。L2 的入口是 `CompanionAgent.run_turn()`，它自己把 `layer="L2"` 写进结果（`app/agents/companion.py:805`）。

### 5.2 ReAct 循环与两道独立的闸

五个动作（技术设计文档 line 823–831）：`plan`（出下一步 JSON）→ `tool`（调工具）→ `observe`（回填观察）→ 再 `plan` → `speak` 收尾。

两道闸**互不替代**：

| 闸门 | 值 | 实现 | 作用 |
| --- | --- | --- | --- |
| 步数上限 | `MAX_STEPS = 4` | `app/agents/companion.py:67` | 防「想不完」，超限 `_terminate("max_steps")` |
| 工具预算 | `MAX_TOOL_CALLS_PER_TURN = 8` | `app/core/config.py:191`，`TurnBudget` 读配置 | 防「调不完」，超限 `reject_budget` |

两者是不同资源：一次 `plan` 可能不调工具，一次工具调用也可能只花一步。把两者压成一个数，要么限制了思考、要么放宽了成本。

### 5.3 结构化输出与失败兜底

`plan` 要求模型只回一个 JSON `{"next":"speak"|"tool", ...}`（固定前缀写在 `AGENT_SYSTEM_PROMPT`，`app/agents/companion.py:105-114`）。三种兜底：

1. **解析失败** → 当普通闲聊把文本念出来，不编造工具调用。
2. **`reply.degraded`**（LLM 超时/限流）→ `_terminate("llm_unavailable", degrade="llm_timeout")`。
3. **超过 `MAX_STEPS`** → `_terminate("max_steps")`。

三种收尾都走同一个 `_assemble_result()`（`app/agents/companion.py:791-820`），保证 `layer` / `degraded` / `finish_reason` 三字段永远齐备，不会出现「失败时少一个字段」的下游解析异常。

### 5.4 L2 的降级

AGENTS §6：超时/限流 → 重试 2 次 → 退化为「L1 结果 + 模板话术」并提示人工；**不得自动执行高危写操作**（改药单/关工单/删数据）。重试实现见 `app/services/llm_service.py:152`（`attempts_allowed = max(0, llm_max_retries) + 1`）与退避 `RETRY_BACKOFF_SECONDS = 0.25`（`app/services/llm_service.py:59`）。退化话术复用 `SPOKEN_DEGRADE["llm_timeout"]`（`app/core/degrade_speech.py`，`app/services/llm_service.py:75` 取用），不新写第四套文案。

---

## 6. 路由判定矩阵（含实现落点）

| 输入特征 | 判定 | 层 | `degraded` | 实现落点 |
| --- | --- | --- | --- | --- |
| 命中安全 3 条之一 | 一票否决，`intent=alert`、`sensitive=True` | L0 | — | `app/router/safety.py:115-134` |
| 命中固定意图 7 条之一 | 直路由，`route_ms≈0` | L0 | — | `app/router/rules.py:215-234` |
| 未命中 L0 且 `is_sensitive` | `intent=alert`（先于置信度） | L1 | — | `app/system_one/questions.py:133` |
| 未命中 L0 且 `is_out_of_domain` | 升 L2 | L2 | — | `app/system_one/questions.py:150` |
| `confidence >= 0.85` | 采纳 L1 | L1 | — | `app/system_one/questions.py:165` |
| `confidence < 0.85`，首次 | L1 + 澄清一次 | L1 | `low_confidence_clarify` | `app/system_one/questions.py:191-193` |
| `confidence < 0.85`，已澄清 | 升 L2 | L2 | `low_confidence_twice` | `app/system_one/questions.py:176` |
| Laya 异常 | 升 L2 | L2 | `laya_unavailable` | `app/system_one/questions.py:107-126` |

**全仓不存在第二套判定入口。** L0 的 `match_l0()`、L1 的 `decide_l1()`、L2 的 `run_turn()` 三处之外，任何模块都不得再实现「这句话该谁答」的判断（AGENTS §4.8 不新增同类能力实现）。

---

## 7. 工具预算与熔断

### 7.1 四道闸的判定顺序

`Guardrail.check()`（`app/agents/guardrails.py:270`）的判定顺序写死在模块 docstring 行 10–20，**顺序即语义**：

```
① 预算闸门      → reject_budget   不触碰熔断状态
② 熔断闸门      → reject_breaker  不消耗预算，但计入窗口 + latch
③ 两道都过 → budget.try_consume() → 查 RepeatCache → short_circuit（已消耗预算）
④ 全过 → allow
```

四个返回值是闭集：`GuardrailAction = Literal["allow", "reject_budget", "reject_breaker", "short_circuit"]`（`app/agents/guardrails.py:40`）。闭集的意义是：`_act_node` 可以用一次 `match` 穷举处置，不会出现「新加一种拒绝理由但调用方没处理」的漏网。

为什么①②并联而不是串联：预算耗尽时仍要**继续记录**熔断窗口（否则预算恢复后熔断窗口是空的、可能瞬间被打满）；熔断触发时不该**白扣**预算（否则一个坏工具能把预算烧光，其他工具没得用）。

### 7.2 预算：`MAX_TOOL_CALLS_PER_TURN = 8`

`TurnBudget`（`app/agents/guardrails.py:78-109`）：`max_calls` 默认读 `get_settings().max_tool_calls_per_turn`（`app/core/config.py:191` = 8），**不在模块里写死** —— 反向验收就是把它改成 1 来验证闸门生效（`设计记录（三级路由）` §6.1）。`would_allow()` 行 96 只读不动，`try_consume()` 行 100 才扣，`reset()` 行 107 在每轮 `run_turn` 开头（`app/agents/companion.py:699`）归零。

### 7.3 熔断：同一 Tool 1 分钟内第 3 次

`ToolBreaker`（`app/agents/guardrails.py:112-171`）：

- 窗口是**半开区间** `(now - 60s, now]`，`_prune` 用 `stamps[0] <= cutoff` 剔旧。
- `allow()` 追加时间戳后，`len(stamps) >= threshold`（默认 3，`app/core/config.py:193`）即 `_tripped` 并返回 False。
- **latch**：一旦某 Tool 被熔断，本 turn 内它后续调用**直接 False 且不再累积时间戳**，避免「熔断后还在涨计数」导致下一轮开局即熔断。
- 时间源默认 `time.monotonic`（单调时钟，不受系统时钟校正影响）。

三个可复核的测试名（`设计记录（三级路由）` §6.1）：
`test_same_tool_third_call_inside_a_minute_trips_breaker`、`test_breaker_counts_by_tool_name_across_distinct_args`、`test_breaker_trips_on_third_call_within_window`。

### 7.4 重复调用短路

`RepeatCache`（`app/agents/guardrails.py:182-234`）：键是 `(tool_name, canonical_args(args))`，TTL 复用 `tool_repeat_window_seconds` = 60（`app/core/config.py:192`），不新增配置字段；`max_entries=256` 近似 LRU。`canonical_args`（行 46–56）用 `sort_keys + ensure_ascii=False + default=str`，保证 UUID / Decimal 不炸、中文可读。

短路命中时 `short_circuit` **已经消耗过预算**（顺序在 `try_consume` 之后），因为「模型确实做了一次工具决策」这件事要计入成本统计。`reset_turn()` 清预算与熔断但**保留缓存**（`app/agents/guardrails.py:170` 与 `:316` 两处实现）—— 跨 turn 的同参调用仍可短路。

### 7.5 四类失败的处置

`ToolStatus` 是五值闭集（`app/agents/tool_registry.py:36`）：`ok` / `param_error` / `not_found` / `transient` / `forbidden`。

| 失败分类 | 判据 | 处置 | 引用的口语话术 |
| --- | --- | --- | --- |
| 参数错 | schema 校验不过（`_validate_args`，`app/agents/tool_registry.py:87-108`，理由只带字段名，不回显模型原文） | 回传模型重试，**计入预算** | 「麻烦您再说一遍，我来帮您查。」 |
| 目标不存在 | 后端明确 not found | 如实告知，**不编造**；不改参数硬重试 | 「这个地点的天气我暂时查不到…」 |
| 临时错误 | 超时/5xx/连接失败/后端崩溃（按 `transient`） | 退避重试 ≤2 次，仍失败转降级话术 | 「查询出了点小状况…」 |
| 越权 | 不在白名单 / 身份来自模型 / 角色不足 | 拒绝 + 写 `audit_logs`（`action="tool.denied"`）+ 带 `trace_id` | 「这个查询现在不能帮您做，我去请工作人员来帮忙。」 |

四句话术在 `TOOL_FAILURE_SPEECH`（`app/agents/tool_registry.py:121`）集中定义，**不在调用点散写**。理由：同一个失败在不同层级被兜底时，若各写一句，长者会听到两套语气、两套承诺。

白名单是**唯一入口校验**（`WHITELIST = frozenset({"get_weather"})`，`app/agents/tool_registry.py:45`）：不在白名单的调用绝不触达后端，`gate="whitelist"`；schema 不过 `gate="schema"`。越权审计在 `app/agents/companion.py:599-640` 收口，失败被吞掉——审计不能反过来打断对话。

---

## 8. 观测口径

### 8.1 `trace_id` 全链路

| 环节 | 来源 | 说明 |
| --- | --- | --- |
| HTTP 入口 | `TraceIdMiddleware`（`app/core/trace.py`，纯 ASGI） | 请求带合法 `X-Trace-Id` 则透传，否则新生成；响应头一定回写；无论成败都 reset contextvar（防协程复用串号） |
| 路由 | `RouteDecision.trace_id` | L0 短路时由 `route()` 补齐（`app/router/routing.py:79-89`） |
| 工具 | `ToolCallRecord` + 审计 detail | 越权记录 `tool.denied` 的 detail 带 `trace_id`（`app/agents/companion.py:631`） |
| 注入拦截 | `audit_logs.action='injection_blocked'` | detail 带 `trace_id`，**不存原文**（`app/agents/companion.py:1749-1755`） |
| 决策日志 | `decision_logs` | 技术设计文档 line 625：`trace_id` 与 Langfuse trace 一一对应、`thread_id` 串会话 |

选择纯 ASGI 而非 `BaseHTTPMiddleware` 的理由写在 `app/core/trace.py` docstring：后者用任务组包装下游，对 SSE / WebRTC 信令这类长连接有已知副作用，而 迭代 5 起就要用。

### 8.2 `/metrics` 的键

> **v1.2 更正（2026-11-17）**：本节首段写于 迭代 5 前的 JSON+admin-JWT 形态；2026-11-13 迭代 5 S3 起 `GET /metrics` 已改写为 **Prometheus 文本抓取面**（`app/api/metrics.py` 头注释），要点：①响应体为 `text/plain; version=0.0.4`，由 OTel Meter + Prometheus reader 的 `render_metrics()` 渲染；②**仅回环可读**（`127.0.0.1` / `::1`，其余一律 403），不再要求 admin JWT——Prometheus 抓取端不带 JWT；③`metrics_enabled=False` 时端点仍 200 但响应体为空。`metrics_snapshot()` / `derived_metrics()` 保留为测试断言与兼容期读法（同一批打点，见 `app/core/metrics.py` 头部对照表）。下文的键清单对两种读法均适用。

`GET /metrics` 原需 `admin` 角色、返回 `MetricsSnapshot` JSON。**（历史口径，已被上方 v1.2 更正取代：现为 Prometheus 文本 + 仅回环；`MetricsSnapshot` 类已移除，保留留痕）**

**`timings`（`metrics_snapshot()` 产出，`app/core/metrics.py:553`）** 每项带 `count` / `sum_ms` / `max_ms` / `p50_ms` / `p95_ms`，分位数用 nearest-rank（`_nearest_rank`，`app/core/metrics.py:114`），不做线性插值——20 个样本时插值会造出一次没发生过的耗时。已有键：`route_ms`、`llm_first_token_ms`、`tts_first_packet_ms`、`tts_total_ms`、`asr_first_packet_ms`、`asr_total_ms`（键集合由实测报告 `eval/reports/latency-table.md` 反证）。采样上限 `_TIMING_SAMPLE_MAX = 2048`（`app/core/metrics.py:45`），有界环形缓冲。

**`counters`** 已验证存在的键：

| 键 | 落点 |
| --- | --- |
| `companion_turn_total` | `app/agents/companion.py:787` |
| `companion_identity_forged_total` | `app/agents/companion.py:418` |
| `companion_tool_gate_total{gate=...}` | `app/agents/companion.py:454` |
| `llm_chat_total{outcome=ok\|degraded}` | `app/services/llm_service.py:164` / `:188` |
| `decision_log_skipped_total{reason=...}` | `设计记录（三级路由）` §6.1 |
| `reply_source_total{provider=...}` | `eval/reports/latency-table.md` |
| `route_placeholder_total{source=...}` | `eval/reports/latency-table.md` |

`metrics_enabled=False` 时全部 no-op 且**不建 key**（`app/core/metrics.py`），因此「键不存在」≠「值为 0」，看板不能把两者混为一谈。

**`derived` 四类派生指标：已合入主干（2026-11-17 核实，`app/core/metrics.py:485/501/540` 均在 HEAD）。** `layer_hit_rates()` / `s1_closure_rate()` / `token_usage()` / `prompt_cache_hit_rate()` 与聚合入口 `derived_metrics()` 已落 `app/core/metrics.py`，`metrics_snapshot()` 的结构固定为 `{"enabled", "counters", "timings", "derived"}`（**既有两节键名一个不动**）。契约要点两条：`prompt_cache_hit_rate` 的 `None` 表示「暂无样本」而非 0，并另给 `prompt_cache_samples` 标样本量；`layer_hit_rates` 的键是实际命中层（L0/L1/L2）。行号以 `app/core/metrics.py` 的实现为准。

### 8.3 Langfuse span 结构与落地状态

W4 新增 `app/core/observability.py`：OTel 语义约定 + Langfuse ingestion 协议，**手写而不直接用 `opentelemetry-sdk` / `langfuse`** —— 理由是开关关掉必须真的零开销、import 无副作用、导出可被 `httpx.MockTransport` 直接测。**该文件已合入主干（2026-11-17 核实：`_langfuse_headers` 在 `app/core/observability.py:418`）**，行号以该文件实现为准：

- 现状（配置）：`app/core/config.py:168-172` —— `langfuse_enabled=False`、host 指向云实例 `https://cloud.langfuse.com`、public/secret key 为空、`sample_rate=1.0`，即默认关闭。
- 选型：Langfuse 专注 LLM 观察、OTel 覆盖全链路，两者协同，Langfuse 可作 OTel exporter（技术设计文档 line 205）；已裁决接云实例、不自托管（`设计记录（交付与观测）` §7 不排期项）。
- span 结构（契约）：`turn`（根）→ `route`（L0/L1 决策，带 layer/degraded/route_ms）→ `llm.plan`（带 model/attempts/token）→ `tool.call`（带 tool/status/gate）→ `tts`。所有 span 共用同一个 `trace_id`（`app/core/context.py:57-79`），与 `decision_logs` / `audit_logs` 的 `trace_id` 同源 —— 这是 AC-31「一条完整 Trace」的实现口径。
- 上传前必须过 `app/core/redact.py`（技术设计文档 line 651）。
- `langfuse_enabled=False` 时必须零开销：不建 TracerProvider、不建 network client、不启后台线程。

### 8.4 敏感内容不进观测

三条硬规则（`docs/SECURITY.md` 分工见 §4 脱敏规则表）：

1. 注入拦截的 detail 只留 `pattern_id` / `layer` / `reason` / `trace_id`，**不存原文**（`app/agents/companion.py:666` 附近）。
2. 工具审计的 `args_canonical` 只存规范化后的参数，不存模型原始输出。
3. 日志经 `app/core/redact.py` 统一脱敏，规则表唯一来源 `REDACTION_RULES`（`app/core/redact.py:279`），验收脚本 `scripts/scan_logs.py` 与它共用同一张表——避免「一份规则三处实现」导致 grep 零命中而泄露照旧。

---

## 9. 评测与门禁

### 9.1 `tests/eval_gate.py` 怎么算

这个文件（589 行）只做「加载 → 调用 → 汇总 → 定退出码」，**自己不含任何判定逻辑**（不属于它）。

- **两套固定集**：路由集 + 注入集。默认门槛 `--min-sensitive 1.0`、`--min-injection 1.0`（`tests/eval_gate.py:568-579`）。
- **敏感召回**：路由集逐条跑 harness，把 `decision.sensitive` 映射成评测器认的 `alert` 位（`tests/eval_gate.py:260-261`），再取 `summary.sensitive_recall`（行 277）。口径是「被判 L0 **且** `sensitive=True`」双条件，不是只看文本里有没有敏感词。
- **注入拦截率**：逐条调 `app/core/injection.py` 的分类入口（`_pick_classifier`，行 311），与 `_expect_block` 期望比对，`rate` 写进 `GateMetric`（行 453）。
- **缺件按 FAIL**：思路写在 docstring 行 15–18 —— 敏感召回是硬指标，「没跑通」不等于「没问题」，缺件不能让门变绿。同理 `GateMetric`（行 125–158）区分三种非通过态：`threshold=None` → 只报告、`value=None` → 跳过、`error!=None` → FAIL。
- **退出码**：只要有一个指标 `passed is False` 就 `EXIT_GATE_FAILED=1`，否则 `EXIT_OK=0`（行 550–554）。`main()` 用 `raise SystemExit(main())`（行 589）把结果交给 pre-push 钩子。

反向验证的做法：`--min-sensitive 1.01 --min-injection 1.01` 必定 FAIL —— 这是唯一能证明「门闩真的连着」的手段，不是把真实阈值往上调（`设计记录（权限与安全）` §6.1 已实测 exit 1）。

### 9.2 固定集规模（实测行数）

| 文件 | 条数 | 实测命令 |
| --- | --- | --- |
| `eval/fixtures/routing.jsonl` | **110** | `Get-Content eval\fixtures\routing.jsonl \| Measure-Object -Line` |
| `eval/fixtures/injection.jsonl` | **20** | `Get-Content eval\fixtures\injection.jsonl \| Measure-Object -Line` |

PRD line 456（FR-TC-07）要求注入集 ≥10 条、100% 拦截；当前 20 条，实测拦截 20/20、误拦 0（`设计记录（权限与安全）` §6.1）。路由集的分层占比见 §10。

### 9.3 其它门禁

- `pytest --cov=app` ≥70%（PRD line 455 FR-TC-06）；最近一次实测 83.87%（迭代 9）、83.70%（迭代 7）、81.89%（迭代 6），三次独立运行的报告见各日计划 §6.1。
- 核心 Tool 与权限场景 100% 覆盖（PRD line 455）——由 `tests/test_agents_companion.py`、`tests/test_gates.py`、`tests/test_injection.py` 承载。
- `scripts/scan_logs.py` 必须 exit 0；把 logging 层的脱敏摘掉后必须 exit 1（迭代 9 反向验收已实测，命中 phone + id_card 两类）。

---

## 10. 延迟实测

### 10.1 day05 基线（`eval/reports/latency-table.md` 与 `day05-latency.json`，20 次实测）

| 分段 | P50 | P95 | max | n |
| --- | --- | --- | --- | --- |
| `llm_first_token_ms` | 5.8 | 6.4 | 7.0 | 20 |
| `tts_first_packet_ms` | 5.8 | 6.4 | 7.0 | 20 |
| `tts_total_ms` | 5.8 | 6.5 | 7.0 | 20 |
| 首包（整轮） | **5.79** | **6.47** | 7.03 | 20 |
| `route_ms` | 0.0 | 0.0 | — | 20 |
| `asr_first_packet_ms` / `asr_total_ms` | — | — | — | **0（N/A）** |

判定：20 次中 20 次首包 ≤1500ms，达到 PRD line 543 的「20 次中 ≥19 次」判据。

**三条口径必须一起读，否则这些数字会被误用：**

1. **不是真实云 API。** 计数 `reply_source_total{provider=mock}` = 20、`route_placeholder_total{source=not_implemented}` = 20 —— 这份基线跑在 mock 语音与占位路由上，量的是**链路开销**（序列化、事件循环、落库），不是 ASR/TTS 供应商的耗时。
2. **`route_ms` 是占位 0。** 真实路由耗时以 day06 为准（下节）。
3. **ASR 段无数据。** 脚本只测回复侧，`asr_*` 两项 count=0，标 N/A 而不是填一个近似值。端到端 `≤1.5s` 的 AC-40 因此**尚未被端到端实测覆盖**，只能确认「回复侧占 6.5ms 量级、剩余预算留给 ASR/TTS」。

### 10.2 day06 路由实测（`eval/reports/routing.md`）

| 指标 | 值 |
| --- | --- |
| 总条数 | 110 |
| 命中 | 109（99.1%） |
| 敏感召回 | 100.0%（20/20） |
| `route_ms` **p50** | **0.2ms** |
| `route_ms` **p95** | **0.6ms** |
| 分层占比 | L0 52（47.3%）/ L1 31（28.2%）/ L2 27（24.5%） |
| 分类别 | normal 50/50、sensitive 20/20、boundary 19/20（95%）、ambiguous 20/20 |

对比 L0 预算 ≤200ms，`route_ms` p95 0.6ms 有约三个数量级余量 —— 说明真正的瓶颈不在路由，而在 ASR/TTS/LLM 三段（当前两段是 mock、一段是占位）。

唯一 bad case：`b001`（boundary 类，期望 L1 实际 L0），见 `eval/reports/routing.md` 行 71。

### 10.3 未验证项

| 项 | 状态 | 原因 |
| --- | --- | --- |
| 端到端 P95 ≤1.5s（含真实 ASR/TTS） | **未验证** | 第三方 key 未接入，day05 用 mock 语音；`asr_*` 两项 count=0 |
| L2 首 token ≤800ms | **无数据** | 未接真实云 LLM，`reply_source_total{provider=mock}` 全量 mock |
| Langfuse 链路可见性（AC-31） | **已合入，待本节重取数** | `app/core/observability.py` 已合入主干（2026-11-17 核实；迭代 10 验收另见 Langfuse 云实例 6 span 树实测，`设计记录（交付与观测）` v1.4） |
| `derived` 四类指标 | **已合入，待本节重取数** | `derived_metrics()` 已在主干（`app/core/metrics.py:540`，2026-11-17 核实）；真实样本采集口径自 迭代 5 S3 起走 Prometheus 文本面 |

本文不把「预算」写成「实测」。上表前两项要等真实供应商接入后才能填数；后两项的模块已合入，重取数属后续观测任务。

---

## 11. 与其它文档的边界

| 想知道 | 去看 |
| --- | --- |
| 身份从哪来、怎么被伪造、越权写哪条审计 | `docs/IDENTITY_RULES.md` |
| AI 用在哪、边界、成本预算、脱敏规则 | `docs/AI_USAGE.md` |
| 威胁模型、密钥轮换、三道闸门位置图 | `docs/SECURITY.md`（迭代 9 已冻结） |
| 面试口径的「为什么这么设计」 | `docs/INTERVIEW_QA.md` |
| 表结构、字段、索引 | `docs/ER_数据模型设计.md`、技术设计文档 §3 |

本文只写 Harness 的行为口径。任何数值若要调整（阈值、预算、闸门条数），必须同时改代码常量、本文与 AGENTS §7 三处，缺一即视为口径漂移。

---

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-08 | 首版。三级路由的层职责、判定矩阵、工具预算与熔断、观测口径、评测门禁、延迟实测与未验证项。所有 file:line 与数字均经 Select-String / Measure-Object 复核；`derived` 与 Langfuse span 如实标注「待 W4 落地」。 |
| v1.1 | 2026-11-08 | W4 落盘后回填（不 git commit，仅改本文）：§8.2 `derived` 与 §8.3 Langfuse 由「待 W4 落地／文件不存在」改为「已落盘、尚未合入主干」，并去掉对未合入文件的行号依赖；§10.3 对应两项状态由「未实现／未落地」改为「未验证」；§10.1 标题补引 `day05-latency.json`（分段 P50/P95 出自该文件）、`tts_total_ms` 的 max 补为 7.0。 |
| v1.2 | 2026-11-17 | 状态行由「进行中 v1.0」收敛为「已冻结 v1.2」（依据：`docs/plans/README.md` 早已登记为已冻结，双头状态收敛）。§8.2 补 v1.2 更正块——`GET /metrics` 自 2026-11-13 迭代 5 S3 起已是 **Prometheus 文本 + 仅回环**（非 admin JWT + JSON），`metrics_snapshot()`/`derived_metrics()` 退为测试与兼容期读法；原 admin 口径段落保留并标注为历史留痕。§8.2 `derived` 段与 §10.3 两行由「尚未合入主干／未验证」改为「已合入主干，待重取数」——核实依据 `app/core/metrics.py:485/501/540`（`layer_hit_rates`/`s1_closure_rate`/`derived_metrics`）与 `app/core/observability.py`（含 `_langfuse_headers`、`langfuse_enabled` 分支）均在 HEAD。本轮为历史包袱清理（AGENTS §10），未改动任何实现口径与数值。 |
