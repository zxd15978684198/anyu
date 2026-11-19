# 安语（Anyu）AI 使用说明

> 状态：进行中　｜　版本：v1.0　｜　日期：2026-11-08
> 关联：设计记录（交付与观测）（W7a 工作包）
> 上游依据：PRD line 440 / 455 / 456、§1.5 非目标｜技术设计文档 §6.3 line 847–857、§6.4 line 859–868
> 变更记录见文末

这份文档回答四个问题：**AI 用在哪、它不许做什么、它说的话怎么被拦住、它花多少钱。**
最后一节写「人怎么和 AI 一起改这个仓库」，因为这个问题评委会问。

---

## 1. 产品侧：AI 用在哪

| 环节 | 模型 | 输入 | 输出 | 它**不**决定什么 |
| --- | --- | --- | --- | --- |
| L1 意图分类 | Laya（本地开源权重，CPU 推理） | 一句话文本 | `intent` / `is_sensitive` / `confidence` / `is_out_of_domain` 四信号 | 不决定「要不要告警」——那只由 L0 规则与 `is_sensitive` 决定 |
| L2 主控对话 | DeepSeek / Qwen 兼容 API，经统一适配器 | system 固定前缀 + 最近 5 轮 + 工具观察 | 下一步动作（`speak` / `tool`）或一句话回复 | 不决定身份（`elder_id` 来自服务端）、不决定能否调工具（白名单 + 闸门） |
| 人设与话术 | 无独立模型，拼进 system 前缀 | 人设 personality / tone_examples / 称呼 / 当前时间 / 医疗边界 | system 消息 | 不决定阈值、不决定分级 |
| 记忆召回 | Embedding 模型（pgvector 存储，维度 1024） | 本轮 query | top-5 记忆片段（强制 `WHERE elder_id`） | 召回失败时**不**加载记忆，不得臆造 |

### 1.1 L1：Laya 只做分类，不做决策

Laya 一次前向出四个信号（AGENTS §1 表）。四信号里只有 `is_sensitive` 能触发告警语义，而它仍要经过 `decide_l1()` 的次序判定（见 `docs/HARNESS_DESIGN.md` §4.2）：sensitive **先于**置信度。

「模型说这句话很敏感」与「这句话要派人去」之间隔着一层代码。这个隔离是刻意的：若把处置权交给分类器，模型换一版、温度抖一次，告警就会时有时无。

### 1.2 L2：统一适配器，不直连 SDK

`app/services/llm_service.py` 是唯一的 LLM 出口（`build_llm()`，`app/services/llm_service.py:418`）。DeepSeek / Qwen / MiniMax 都提供 OpenAI 兼容的 `/chat/completions`，用一个 httpx POST 打平（模块 docstring 行 11）。

不直连 SDK 的三个理由：换供应商只改配置不改调用点；重试/超时/脱敏只写一处；测试可以整体替换成假实现。`_normalize_messages()`（`app/services/llm_service.py:281`）在发出去之前做消息规范化，避免上游对空消息或重复 role 的容忍度差异变成线上 bug。

### 1.3 人设 prompt 的实况（读目录得到的事实）

`app/prompts/` 目录当前只有两个文件：

- `app/prompts/__init__.py`（134 字节）
- `app/prompts/personas/README.md`（414 字节，9 行）

**没有任何 `.jinja` / `.md` 人设文件。** `README.md` 写的是三条约定：①固定前缀 = system 指令 + 人设 + 工具定义，改动只放在前缀之后；②不含真实长者数据、密钥、内网地址；③新增人设需同步 `personas` 表种子与 `eval/fixtures/`。

真正在跑的 system 前缀不在这个目录，而在两个地方：

| 位置 | 内容 |
| --- | --- |
| `app/core/context.py:193` `build_dialogue_messages()` | 五层上下文组装：①固定前缀 ②最近 5 轮（`DIALOGUE_MAX_ROUNDS = 5`，`app/core/context.py:117`）③历史摘要 ④结构化状态 ⑤长期记忆 |
| `app/core/context.py:124` `MEDICAL_BOUNDARY` | 医疗边界常量，拼进固定前缀 |
| `app/agents/companion.py:350` `AGENT_SYSTEM_PROMPT` | Agent 的 JSON 输出契约（只回一个 JSON、工具只有 `get_weather(city)`、不写 `elder_id`/`thread_id`） |

固定前缀由 `_build_system_prompt()`（`app/core/context.py:204`）组装：`_SYSTEM_PREFIX_HEAD`（行 104，含人设名）→ 人设段落 → 称呼（`_address_line`，行 235）→ 当前时间（`_time_line`，行 247，`Asia/Shanghai`）→ 医疗边界。顺序固定、内容逐字节稳定，这是后面 §7.3 缓存策略的前提。

人设文本本身存 DB（`personas` 表），由 `app/services/persona_service.py` 读取（`_resolve_persona_path` 行 342、`_read_full_text` 行 370、`_list_personas_blocking` 行 379、`PersonaNotFound` 行 252）。**目录是约定，数据库是事实** —— 这一点在读目录时容易误判，特此写明。

### 1.4 记忆：召回失败就当作没有

长期记忆走 pgvector，维度 1024（`EMBEDDING_DIM = 1024`，`app/models/memory.py:42`；由 `settings.embedding_dimension` 注入 provider，启动探活会打印实际维度；改维度 = 改配置 + 两处常量 + 一版迁移 + 旧向量重算）。召回 SQL 的单条 select 同时带 `elder_id` / `status='active'` / category / `embedding IS NOT NULL`（`设计记录（记忆用药娱乐）` §6.1 实测）。

AGENTS §6 的硬规则：**召回失败 → 本轮不加载记忆，不得臆造**。宁可回答得平淡，也不能把「记得您上周说过」编出来。技术设计文档 line 855 要求「强制 `WHERE elder_id`」，这不是性能优化而是隔离要求：跨长者的记忆串一条，就是一次数据泄露。

---

## 2. 三条不可越界的边界

| 边界 | 明文出处 | 落到哪 |
| --- | --- | --- |
| **不做医疗诊断** | PRD §1.5 非目标、AGENTS §4.4 | `MEDICAL_BOUNDARY`（`app/core/context.py:124`）写进固定前缀：只做陪伴与提醒、身体不适先说关心再建议「问一下医生或护工」、不说「您这是××病」 |
| **不接真实 HIS / 微信 / 短信 / 电话 / 支付** | AGENTS §4.5 | 除天气 API 外全部 Mock：`mcp-servers/mock-operations`（工单/通知）落库可查 |
| **异常值走「建议问医生」并同时人工告警** | AGENTS §4.4、PRD §4.6 | 健康红标（收缩压 ≥180 或舒张压 ≥120）由代码判，不经模型；告警走规则层与派单，不等模型发话 |

`MEDICAL_BOUNDARY` 的写法值得单说：它被写成**常量**而不是拼进每个分支（注释行 92–93），理由是「让它在 prompt 里逐字节稳定 —— 这段是固定前缀的一部分」。若它随分支变化，缓存命中率会掉，而命中率掉不会被任何人立刻发现（见 §7）。

---

## 3. 防护侧：注入三层

三层判定在 `app/core/injection.py`（534 行），层名闭集：`LAYER_INPUT="input"`（行 66）/ `LAYER_INTENT="intent"`（行 67）/ `LAYER_OUTPUT="output"`（行 68）/ `LAYER_NONE="none"`（行 69）。

### 3.1 第一层：输入分类（`classify_input()`，行 370）

七条规则（行 191–197），每条有稳定的 `pattern_id`：

| pattern_id | 拦什么 |
| --- | --- |
| `exfil_other_sensitive` | 索要他人信息 |
| `role_hijack_identity` | 「你现在是管理员/医生/root」（句尾疑问语气不算劫持，避免误伤「你是医生吗？」） |
| `tool_abuse_bulk` | 批量/滥用式工具调用 |
| `override_ignore_prior` | 「忽略以上所有指令」 |
| `override_system_prompt` | 尝试改写 system prompt |
| `override_privilege_grant` | 索要权限提升 |
| `encoding_pinyin` | 拼音/编码绕过；另有 base64 专用旁路（`_base64_bypass`，行 354，`pattern_id = "encoding_base64"`） |

命中即整轮短路：**不花 token、不触工具**，直接返回固定话术，`layer="L0"`、`degraded="injection_blocked"`、`finish_reason="injection_blocked"`（`app/agents/companion.py:1749-1755`）。前移到输入层是全篇最重要的一个决定：等模型读完了再检查输出，注入已经生效了一半。

### 3.2 第二层：意图白名单（`check_intent()`，行 413）

`INTENT_WHITELIST`（`app/core/injection.py:244-270`）是 **24 个**生产意图的闭集，逐字来自 `rules.py` 的 13 个 L0 意图（含 CON-03 的 `pause` / `resume`）、`routing.jsonl` 的 `expected.intent` 与 `safety.py` 的 `alert`；**不含**仅测试用的 `small_talk`，也不含代码里不存在的 `chat` / `clarify`。

两个子判定：

- `intent_not_whitelisted`：意图不在闭集，拒绝。白名单而不是黑名单，因为黑名单永远少一条。
- `intent_write_scope`：写意图叠加「批量/替他人办」措辞即越权。`WRITE_INTENTS = frozenset({"medication_take"})`（行 271）—— 当前唯一会真正写业务状态的意图；`_WRITE_SCOPE_PATTERN`（行 275）匹配 所有|全部|批量|全体|每个人|每位|大家|他人|别人|旁人|某某|某位，其中 `(?<!其)他` 专门避免把「其他」判成「他人」。

### 3.3 第三层：输出校验（`check_output()`，行 471）

四个检查：`output_phone`（手机号）、`output_id_card`（身份证）、`output_sensitive_term`（敏感词）、`output_other_name`（他人姓名）。同一 `elder_name` 放行 —— 说「王奶奶，该吃药了」是对的，说「隔壁李大爷血压高」是泄露。

命中后同样返 `FIXED_REFUSAL`（`app/agents/companion.py:1745`/`:1896`；话术定义在 `app/core/injection.py`），不把半截泄露放给 TTS。

### 3.4 固定话术

```python
FIXED_REFUSAL: Final[str] = "这个我不能帮您处理，我们聊点别的吧。"   # app/core/injection.py:72
```

一行常量、一个出口。三层命中都用它，理由和 `TOOL_FAILURE_SPEECH` 一致：同一个拒绝在同一轮里若换三种说法，长者会以为出了三件事。

话术**不含**「我不能泄露隐私」这类解释。解释本身就是信息：它告诉攻击者「这里有东西可以泄」。

### 3.5 三层之外还有一层

`Guardrail.check()`（`app/agents/guardrails.py:270`）管的是**工具**能不能调，不是文本能不能说。三层拦「话」，闸门拦「手」。两者不可互相替代：文本过了三层，仍可能在 `get_weather` 参数里塞一个注入载荷；反之工具白名单也拦不住一句越界的闲聊。

---

## 4. 脱敏规则表

唯一规则来源是 `REDACTION_RULES`（`app/core/redact.py:279`），四条：

| 类型 | 正则/判据 | 掩码 | 参与自由文本替换 |
| --- | --- | --- | --- |
| `secret` | Bearer 令牌 / `sk-*` / `ghp_*` / 长度 ≥32 的大小写数字混合 base64 | `Bearer ***` 或 前 4 字符 + `***` | ✅ |
| `id_card` | 18 位新证（**出生日期合法性校验**）与 15 位老证 | 前 6 + 星 + 后 4 | ✅ |
| `phone` | `(?<!\d)1[3-9]\d{9}(?!\d)`（`app/core/redact.py:60`） | 前 3 + `****` + 后 4 | ✅ |
| `address` | 含区/县/旗且带门牌号的完整地址 | 保留到区/县/旗 + `***` | ❌ `in_text=False`，**只扫描不进替换** |

三个工程细节：

1. **规则只写一份**。模块 docstring 行 3–13 解释原因：日志脱了、审计导出没脱、验收脚本用第三套正则，于是 grep 零命中而泄露照旧。现在日志格式化器、审计导出、`scripts/scan_logs.py` 三处共用同一张表。
2. **幂等**。`_is_masked()`（行 129）让已带 `*` 的值原样返回，因此 `redact_text` 可以对同一段文本跑多次而不会把掩码再掩一遍（`138****8000` 变成 `1*******000`）。
3. **键名规则是子串匹配、顺序命中即止**（`SENSITIVE_KEY_RULES`，行 293–317）：`password`/`token`/`authorization`/`cookie`/`sdp` → `mask_secret`；`id_card`/`cert_no` → `mask_id_card`；`phone`/`mobile`/`msisdn` → `mask_phone`；`addr` → `mask_address`。`identity` 只做**精确匹配**（行 313），因为 `identity_source` 是非隐私枚举（`device`/`voice`/`confirm`/`generic`），子串匹配会把它整列打星。

---

## 5. 什么内容绝不进 prompt / 日志 / 测试夹具

| 内容 | 为什么 |
| --- | --- |
| 任何密钥、JWT secret、TURN 凭据、API Key | AGENTS §4.2；`.env` 已在 `.gitignore`，`.env.example` 只有占位符 |
| 真实长者姓名、手机号、身份证、住址、完整病历 | AGENTS §4.3；夹具一律用构造数据 |
| 未脱敏的原始音频 | AGENTS §4.10；最长保留 72h 且只允许调试开关临时打开 |
| 注入载荷原文 | `injection_blocked` 的 detail 只留 `pattern_id`/`layer`/`reason`/`trace_id`（`app/agents/companion.py:643-676`） |
| 模型伪造的身份值全文 | `falsified_identity` 只留前 120 字符（`app/agents/companion.py:245-268`） |
| 密码明文 | 不存、不返回，`app/api/auth*.py` 全程只认哈希比对 |

「不进测试夹具」这条最容易被违反：夹具写起来最省事的办法就是抄一条真实日志。迭代 9 的 `scan_logs.py` 就是为此存在的 —— 它不只扫运行日志，也用作仓库级零命中验收（摘掉 logging 层脱敏后必须 exit 1，已实测）。

---

## 6. 成本与预算

| 项 | 值 | 实现/出处 |
| --- | --- | --- |
| 单轮工具调用上限 | **8** | `max_tool_calls_per_turn = 8`（`app/core/config.py:191`），`TurnBudget`（`app/agents/guardrails.py:78-109`） |
| 同 Tool 熔断 | 1 分钟内第 **3** 次 | `tool_repeat_break_threshold = 3`、`tool_repeat_window_seconds = 60`（`app/core/config.py:192-193`），`ToolBreaker`（`app/agents/guardrails.py:112-171`） |
| 同参重复短路 | 60s 内 | `RepeatCache`，`max_entries=256` 近似 LRU |
| LLM 重试 | **≤2 次** | `attempts_allowed = max(0, llm_max_retries) + 1`（`app/services/llm_service.py:152`），退避 `0.25s × attempt`（行 59、183–184） |
| ReAct 步数 | 9（预算 8 + 1 步收尾） | `MAX_STEPS`（`app/agents/companion.py:115`），生产默认走 `default_max_steps()` |

### 6.1 重试为什么是 2 次而不是 5 次

只对**可重试**错误退避：timeout / connect_error / 429 / 5xx；4xx 与坏载荷**不重试**（`app/services/llm_service.py:210-260`）。判据是「重试有没有可能成功」：4xx 是请求本身错了，重试 5 次只是把同一个错误打 5 遍，还多花 5 倍 token。

退避基数 0.25s 是刻意小：它必须远小于 `llm_timeout_seconds`，否则「重试」自己就成了延迟的主要来源（`app/services/llm_service.py:58` 注释）。

### 6.2 token 计量：W4 已落盘，尚未取证

`app/core/metrics.py` 的 `derived_metrics()`（`:540`）产出 `token_usage()`（prompt / completion 累计），`metrics_snapshot()` 固定含 `derived` 一节（`:541`）。HTTP 侧自 2026-11-13 S3 起 `GET /metrics` 改为 Prometheus 文本（`app/api/metrics.py`），token 走 `llm_prompt_tokens_total` / `llm_completion_tokens_total` 两条样本；原 `MetricsSnapshot.derived` 的 JSON 读法只保留在进程内测试里。**token 计量已可用**，但只有真实供应商返回 usage 时才计入（不回 usage 时不计 0 冒充样本）。

语义有一条别读错：「键存在但值为 null 表示暂无样本，不是 0」。

### 6.3 prompt cache：固定前缀策略

技术设计文档 line 851 要求固定前缀**逐字节稳定**以命中上游 prompt cache。落地成三条约束：

1. 前缀组成固定且有序：人设 → 称呼 → 时间 → 医疗边界（`app/core/context.py:204-233`）。顺序一变，前缀就变。
2. 需要变化的内容（历史、观察、本轮用户句）一律放在前缀**之后**（`app/prompts/personas/README.md` 约定①）。
3. 常量而非拼接：`MEDICAL_BOUNDARY`、`_SYSTEM_PREFIX_HEAD`、`AGENT_SYSTEM_PROMPT` 都是模块级常量，不做运行时格式化（唯一例外是人设名 `{name}`，它在人设切换时本该变化）。

命中率指标 `prompt_cache_hit_rate` 同样进 `derived`（配 `prompt_cache_samples` 给样本量，`None` 表示无样本）。在它合入并取到真实数据之前，「前缀稳定」只能靠代码评审保证，没有数据兜底 —— 这是本文最想让人记住的一条：缓存策略失效是**静默**的，账单上涨不会有人立刻发现。

---

## 7. 开发侧：AI 协作的约束

本仓库由 AI 与人工协作实现，约束写在 `AGENTS.md`（跨工具通用规则）。三条与 AI 使用直接相关：

### 7.1 红线（AGENTS §4，AI 不得自行决定）

不连生产库、不连真实医疗系统；不硬编码密钥；不把密钥/真实长者数据写进 prompt、夹具、日志、提交信息；不说医疗诊断口径；不接真实微信/HIS/短信/电话/支付；不改已合入的 Alembic 历史迁移；不新增第二种同类能力实现；不允许模型自报身份；不落未经脱敏的原始音频。

「不新增第二种同类能力实现」（§4.8）是 AI 协作场景下最容易踩的一条：模型很擅长「顺手再写一个类似的东西」，于是同一个能力出现两套实现、两套阈值。遇到这种情况的正确动作是删掉新的那套，而不是让两套并存。

### 7.2 必须人工逐行读 diff 的文件（AGENTS §5）

| 文件 / 目录 | 风险类别 | 必须确认 |
| --- | --- | --- |
| `app/core/security.py`、`app/core/auth*.py`、`app/api/auth*.py` | **auth** | 密钥只从配置读；JWT 算法与过期固定（Access 2h / Refresh 7d / 设备会话 24h）；`token_version` 失效逻辑；三套凭证不串用 |
| `app/models/**`、`alembic/versions/**` | 迁移与 schema | 字段语义/类型/可空性与技术文档 §3 一致；可回滚；不删改历史迁移 |
| `app/agents/tool_registry.py`、`app/agents/companion.py`、`app/mcp_client/**`、`app/core/runtime_context.py` | **Tool 权限** | 白名单与 schema 校验；身份只从 `runtime_context` 注入；越权必须拒绝并写 `audit_logs`；`MAX_TOOL_CALLS=8` 与熔断生效 |
| `mcp-servers/weather/**`、`mcp-servers/mock-operations/**` | MCP server | schema 与协议一致；入参强校验；返回脱敏；无自由外网访问；Mock 落库可查 |
| `Dockerfile`、`docker-compose.yml`、`.dockerignore` | 部署 | 非 root；密钥不进镜像层；依赖锁定；健康检查 |
| `app/router/safety.py`、`app/router/rules.py` | **安全一票否决** | 急症/敏感判定先于一切；规则条数 ≤10；阈值与分级写在代码而非 prompt |
| `app/graphs/**`（含 checkpointer） | 状态与断点恢复 | checkpoint 落独立 schema、独立连接池；`thread_id` 服务端生成 |
| `.env.example` | 配置泄露 | 只有占位符 |

自查口径为 **auth / memory / mcp_client** 三处（AGENTS §5 脚注），评审前优先自查。

### 7.3 给 AI 下任务的统一模板（AGENTS §8）

`改哪些文件 → 验收命令 → 禁止触碰的文件 → 完成后报告 diff 摘要`。涉及 §5 清单内文件时，**先出方案再落代码**。

「三次法则」（AGENTS §8/§9）：同一个坑第 3 次出现必须补进踩坑记录，否则不许继续往下做。这条对 AI 尤其重要 —— 模型的默认行为是绕过报错继续产出，而不是停下来记录为什么错。

---

## 8. 未验证项

| 项 | 状态 | 原因 |
| --- | --- | --- |
| token 计量与 prompt cache 命中率 | **未验证** | W4 已落盘 `token_usage()` / `prompt_cache_hit_rate()` 与 `derived` 节，但未合入本文基线，未取到样本 |
| 真实云 LLM 的成本数据 | **无数据** | 未接真实供应商，day05 基线全量 mock（`reply_source_total{provider=mock}`=20） |
| Langfuse 上的 prompt/token 归因（AC-31） | **未验证** | `app/core/observability.py` 尚未合入主干，无 Trace 可取证 |
| 人设文件与 `personas` 表种子的一致性 | **未验证** | 目录内无实际人设文件，无对照可做 |

本文不把预算写成实测。上表四项要等真实供应商接入与 W4 观测模块合入后才能填数。

---

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-08 | 首版。产品侧四类 AI 用法与三条边界；防护侧三层注入 + `FIXED_REFUSAL` + 脱敏规则表；成本预算六项；开发侧约束。人设目录、`derived` 字段、Langfuse 均按实读标注为「无实际文件 / 待 W4 落地」。 |
| v1.1 | 2026-11-08 | W4 落盘后回填：§6.2 token 计量与 §6.3 命中率由「待 W4 落地」改为「已落盘、尚未取证」；§8 两项状态同步；**仍不给任何未经实测的 token 数**。 |
| v1.2 | 2026-11-11 | 记忆召回与长期记忆两处的维度口径 1536 → 1024（`EMBEDDING_DIM = 1024`），补一句「维度由 `settings.embedding_dimension` 注入，启动探活会打印实际维度」。 |
