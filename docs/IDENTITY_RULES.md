# 安语（Anyu）身份与权限规则

> 状态：进行中　｜　版本：v1.0　｜　日期：2026-11-08
> 关联：设计记录（交付与观测）（W7a 工作包）
> 上游依据：PRD line 440（FR-ADM-06 闸门③）、line 456（FR-TC-07）｜技术设计文档 §3.5 / §6.2
> 变更记录见文末

本文回答三个问题：**你是谁（凭证）、这个身份凭什么被信任（注入）、身份不够时被怎么处置（拒绝与审计）。**

不回答：威胁建模与密钥轮换（见 `docs/SECURITY.md`）、接口字段契约（见 `docs/接口契约_身份与权限.md`）、Harness 行为（见 `docs/HARNESS_DESIGN.md`）。

---

## 1. 三套凭证体系

三套凭证**不能串用**，串用是审计里最容易被抓到的错误（`docs/SECURITY.md` §2.2，迭代 9 demo 第 2 步专门演练「反向串用两件令牌」）。

| 主体 | kind | token `typ` | 有效期 | 载荷白名单 | 撤销手段 |
| --- | --- | --- | --- | --- | --- |
| 长者设备（pad） | `device` | `device` | **24h** | `sub` `typ` `iat` `exp` `kind` `dev` `tv` | `dev`（设备 `token_hash` 前 16 hex）对不上即 revoke |
| 员工工牌 | `staff_card` | `staff_card` | **≤86400s** | `sub` `typ` `iat` `exp` `kind` `dev` `scope` | **只有到期**：工牌无版本号，`expires_at` 过了即失效 |
| 员工账号 | `staff` | `access` + `refresh` | Access **2h** / Refresh **7d** | access：`sub` `typ` `iat` `exp` `kind` `tv` `role`；refresh 同减 `role` | `token_version`（`tv`）不等即 revoke |
| 家属账号 | `guardian` | `access` + `refresh` | Access **2h** / Refresh **7d** | 同员工 | `token_version`（`tv`） |

四个 `typ` 是闭集（`app/core/security.py:257-260`）：`access` / `refresh` / `device` / `staff_card`。**`refresh` 不能当 principal 用**：`PRINCIPAL_TYPE_LITERALS`（`app/core/security.py:263`）只含 `(access, device, staff_card)`，刷新令牌只允许走刷新流程（`app/api/auth.py:134-139`）。

`_ALLOWED_CLAIMS_BY_TYP`（`app/core/security.py:300-305`）是逐 `typ` 的**双向白名单**：签发时少一个必需声明直接报错，解码时多一个声明直接拒绝。这样「给设备令牌塞一个 `role`」这种越权尝试在解码层就死了，不必依赖上游校验。

### 1.1 三档有效期的**唯一**口径

有效期不写死在签发函数里，只从三个函数出（`app/core/security.py`）：

| 函数 | 行号 | 表达式 | 配置默认值 |
| --- | --- | --- | --- |
| `access_ttl_seconds()` | `app/core/security.py:214-219` | `settings.access_token_expire_minutes * 60` | `120`（`app/core/config.py:48`）→ **2h** |
| `refresh_ttl_seconds()` | `app/core/security.py:222-227` | `settings.refresh_token_expire_days * 86400` | `7`（`app/core/config.py:49`）→ **7d** |
| `device_ttl_seconds()` | `app/core/security.py:230-235` | `settings.elder_session_expire_hours * 3600` | `24`（`app/core/config.py:50`）→ **24h** |

`_require_positive_ttl()`（`app/core/security.py:238-249`）拒绝 0、负数与 `bool` —— 最后一个不是洁癖：`isinstance(True, int)` 为真，配置里写成 `true` 会签发一个 1 秒有效期的令牌，而且看起来「配了」。

**读法提醒**：任务卡里写的 `human_access` / `human_refresh` 是旧命名，代码里是 `access` / `refresh`。本文以代码为准。

### 1.2 工牌为什么没有版本号

工牌是**物理介质**：挂失、离职、换岗都不改工牌本身，改的是 `staff_cards.expires_at`。因此工牌的失效只靠到期（`app/core/auth.py:377-378`：`if db_expires_at <= moment: raise CredentialRevokedError`）。这是刻意的不对称：给工牌加版本号意味着每次剪卡都要回写数据库，而剪卡的现场往往没有网络。

---

## 2. `token_version`：唯一的服务端撤销手段

`CredentialRevokedError`（`app/core/auth.py:109-114`）覆盖四种「令牌本身有效但库里不再承认」：

1. 账号 `status != active`；
2. **`token_version` 与载荷 `tv` 不等**；
3. 令牌里记的角色/设备与库里当前值不一致；
4. 设备 `token_hash` 前缀对不上（解绑）；工牌 `expires_at` 已过或员工已停用。

判定在 `_require_active_and_current_version()`（`app/core/auth.py:217`），核心一行是 `app/core/auth.py:268` 处的调用与版本比对。

失效场景与效果：

| 动作 | 谁 +1 | 效果 |
| --- | --- | --- |
| 停用账号 | `token_version` | 该账号所有已签发 Access/Refresh **立即**失效，不必等 2h/7d |
| 改角色 | `token_version` | 旧令牌里的 `role` 不再被信任（否则降权要等 Access 过期才生效） |
| 解绑设备 | `devices.token_hash` 前缀变 | 设备令牌 revoke，不必重启 pad |
| 工牌到期/员工停用 | 无写入 | 读取时比对 `expires_at` 判定 |

设备令牌的 `tv` **不是**版本号而是一个 16 位前缀（`_DEVICE_TOKEN_HASH_PREFIX_LENGTH = 16`，`app/core/security.py:275`），取自 `devices.token_hash`。为什么是前缀而不是全量：数据库里存的是哈希，前缀足够区分「同一台设备的当前绑定的那把钥匙」，同时避免把完整哈希放进令牌。

---

## 3. 身份注入铁律

### 3.1 一句话规则

**`elder_id` / `thread_id` 只能来自服务端 `runtime_context`；客户端传入、模型传入一律丢弃。**

这条规则不是「尽量」，是「唯一」。`app/core/runtime_context.py` 的模块 docstring 行 1–16 写明它**故意不提供**「按 `elder_id` 查身份」与「从请求体取身份」两个入口——没有函数，就不存在「顺手用一下」的可能。

### 3.2 `Identity` 构造即合法

`Identity` 是 frozen dataclass（`app/core/runtime_context.py:97-197`），`__post_init__`（行 129–178）把「结构合法」变成构造的一部分，因此 `dataclasses.replace()` 也逃不掉。

四个 `kind` 的字段矩阵（docstring 行 105–118）：

| kind | 必填 | 一律 `None` |
| --- | --- | --- |
| `staff` | `staff_id` + `role` | `guardian_id` / `device_id` / **`elder_id`** / `scope` |
| `guardian` | `guardian_id` | 其余 |
| `device` | `device_id`，`elder_id` **可填**（未绑定则 `None`） | `staff_id` / `role` / `guardian_id` / `scope` |
| `staff_card` | `staff_id` + `device_id` + **非空** `scope` | `elder_id` 可填 |

关键一行是「**staff 不许自带 `elder_id`**」（行 114–118）：工牌能操作哪个长者，只能由服务端读 `devices`/绑定表推出。如果允许 staff 自带 `elder_id`，那张绑定表就形同虚设，越权只需拼一个请求体。

### 3.3 读不到就报错，不退化

```python
_identity_ctx: ContextVar[Identity | None] = ContextVar("anyu_identity", default=None)   # 行 218
```

- `set_identity()`（行 221）用 `isinstance` 硬拦非 `Identity` 对象；
- `get_identity()`（行 232）返回 `Optional`，只给「有没有」这类判断用；
- `require_identity()`（行 241）抛 `IdentityNotSetError`。docstring 行 55–56 明写：**绝不允许「读不到就退回模型传来的 `elder_id`」**；
- `identity_scope()`（行 257–269）用 `try/finally` 复位，防 uvicorn 协程复用导致串号——一个请求结束没复位，下一个请求会继承上一个长者的身份。

### 3.4 WebSocket 首帧

语音链路走 WS，身份同样只从服务端定。信封在 `app/audio/signaling.py:99-101`：

```json
{"type": "auth", "token": "<设备会话 access_token>"}
```

- **连接后 3000ms 内必须首发**（`WS_AUTH_TIMEOUT_MS = 3000`，`app/audio/signaling.py:63-71`，2026-11-13 由 300ms 放宽）；超时回 `{"type":"auth_fail","code":"timeout"}` 并以 **4401** 关闭。**这是防挂死的活体窗口，不是安全边界**——鉴权强度仍由 token 校验（签名 / exp / 设备绑定）决定；依据是 pad 刷新时主线程被 VRM 解析占住、首帧实际到达 0.5–1s，旧 300ms 窗口把正常刷新误判成鉴权失败（P1）。端侧因此必须把 `code=timeout` 当瞬时故障退避重连，**只有 `invalid` / `expired` 才回登录页**；
- 首帧判定在 `app/audio/signaling.py:598-642`：不是 `type=="auth"` 或 `token` 不是字符串 → `auth_fail`（`code ∈ timeout/invalid/expired`，信封见行 110–114）；
- **`token` 不进 URL**（行 82 注释）。放 URL 会被写进 nginx/代理访问日志、浏览器历史、Referer；
- `auth_ok` 回 `elder_id` / `room_id`（行 920），但该 `elder_id` **仅展示用**，业务归属一律由服务端裁决（行 106 注释）——前端拿到它也不能当写权限用；
- 心跳 20s（`WS_PING_INTERVAL_S`，行 73）、45s 无帧判死（`WS_DEAD_AFTER_S`，行 75）、鉴权失败 4401（`WS_CLOSE_AUTH_FAILED`，行 80）、同设备踢旧留新关闭码 4002（`WS_CLOSE_SUPERSEDED`，行 81）。

### 3.5 设备 → 长者的解析

`resolve_identity(device_id)`（`app/router/wiring.py:76-105`）是唯一把设备映射到「当前在陪哪位长者」的地方：只读、不可抛 —— UUID 解析失败、设备未绑定、数据库异常，一律返回 `(None, None)`，绝不返回一个「猜的」`elder_id`。优先级是 `devices.elder_id` 权威，open thread 的 `elder_id` 仅作回退。

---

## 4. 模型自报身份：`falsified_identity`

模型在工具参数里写 `{"elder_id": "..."}` 是**最常见的一次注入成功形态**，也是闸门③要拦的东西（PRD line 440）。

`_strip_identity()`（`app/agents/companion.py:245-268`）在每次解析 `plan` 输出后**递归**摘除 `elder_id` / `thread_id` 两个键，并把被拒内容记成：

```python
{"name": "elder_id", "value": "<前 120 字符>", "rejected_by": "identity_from_model"}
```

调用点与效果：

| 环节 | 行号 | 行为 |
| --- | --- | --- |
| 摘除 | `app/agents/companion.py:412` | `cleaned_payload, forged = _strip_identity(payload)`，后续只用 `cleaned_payload` |
| 计数 | `app/agents/companion.py:418` | `counter("companion_identity_forged_total", value=len(forged))` + warning |
| 累积 | `app/agents/companion.py:413-416` | 每条记录追加 `step`，写回 `state["falsified_identity"]` |
| 出口 | `app/agents/companion.py:817` | 结果里的 `falsified_identity` 字段，供测试与排查断言 |

三个设计取舍：

1. **不中断对话**。伪造身份不是「拒绝回答」的理由——长者没做错事，不能因为模型胡写就让他听到一句无来由的拒绝。丢弃 + 计数 + 记录，对话继续。
2. **不计入审计**。`falsified_identity` 走 counter + warning，**不写** `audit_logs`：它不是一次越权操作（没有真的执行），写成审计会污染「越权」口径。真到了调工具那一步，再由闸门拦下并写 `tool.denied`。
3. **只留值的前 120 字符**。防止模型把一长串真实数据塞进伪造字段，从而把审计/日志变成写入通道。

`_REJECTED_BY` 的取值固定为 `"identity_from_model"`（`app/agents/companion.py:71`），是全文唯一可用的线索字段——排查时 grep 这一个词就能定位全部伪造事件。

---

## 5. 越权判定与审计映射

### 5.1 401 与 403 的分界

| 状态 | 含义 | 处理 |
| --- | --- | --- |
| 401 | 凭证本身不可信（缺失 / 签名错 / 过期 / 已撤销） | 客户端应重新登录或刷新 |
| 403 | 凭证可信、主体合法，**权限不够** | 客户端不该重登，应提示无权限 |

`RoleForbiddenError`（`app/core/auth.py:120-123`）注释直说：凭证有效、主体合法，但角色不够 —— 403，不是 401（契约 §3 的四类拒绝）。

### 5.2 四类拒绝的落点与审计

| 拒绝类型 | HTTP / 出口 | code | 判定落点 | 写入的 `audit_logs.action` |
| --- | --- | --- | --- | --- |
| **角色不足** | 403 | `ROLE_FORBIDDEN`（`app/core/auth.py:64`） | `RoleForbiddenError`（`app/core/auth.py:120-123` 类体、`:123` 码值）、`check_roles()`（`app/core/auth.py:410`） | 按端点：`memory.read`（`app/api/memory.py:220`）、`memory.delete`（:270）、`audit.read`、`audit.export`、`stats.overview.read`（`app/api/stats.py:99`）、`stats.security.read`（`app/api/stats.py:100`）、`medication.plan.read`（`app/api/medication.py:413`）、`medication.plan.create`（:440）、`medication.event.read`（:466）、`medication.event.confirm`（:508） |
| **范围不足** | 403 | `SCOPE_FORBIDDEN` | `ElderScopeForbiddenError`（`app/services/elder_service.py:67`），由 API 层映射为 `_error_response(403, "SCOPE_FORBIDDEN", ...)`（如 `app/api/elders.py`、`app/api/health.py:266`、`app/api/reports.py`） | 同上（同一 action，`detail.reason="scope"`） |
| **工具越权** | 对话话术（非 HTTP） | — | `ToolRegistry.call()` 白名单 / schema（`app/agents/tool_registry.py:176-203`）、`Guardrail.check()`（`app/agents/guardrails.py:270`） | `tool.denied`（`app/agents/companion.py:631`） |
| **注入拦截** | `FIXED_REFUSAL` 话术 | — | `classify_input()` / `check_intent()` / `check_output()`（`app/core/injection.py:387` / `:430` / `:488`） | `injection_blocked`（`app/agents/companion.py:1749-1755`） |

三条容易搞错的细节：

- **先审计再抛**。`app/api/memory.py:191-198` 的注释是「先判主体种类、再判角色；两种拒绝都**先写审计**再抛 403」。顺序反了就会出现「客户端看到 403，但库里查不到这次 403」——审计的作用之一是事后取证，不是事后补记。
- **`tool.denied` 允许写失败**。`_audit_denied_tool_calls()`（`app/agents/companion.py:599-640`）用 `write_audit_isolated`，失败被吞掉。理由：审计链路坏了不该让长者听不到回答；但 `detail` 里必须带 `trace_id`，这样即便审计丢了，Langfuse/日志还能对上。
- **`injection_blocked` 不存原文**。detail 只留 `pattern_id` / `layer` / `reason` / `trace_id`（`app/agents/companion.py:666` 附近的 `_detail`），因为注入载荷本身就是「想被写进日志的那段话」。

---

## 6. 三条闸门的现场演示

命令与预期输出**不在此重复**，只给入口：

| 闸门 | 演示脚本位置 | 一句话摘要 |
| --- | --- | --- |
| ① auth（三套凭证不串用） | `设计记录（三处现场演示脚本）` 第 1 节（行 33 起） | 五步：正常登录 → **反向串用两件令牌（第 2 步，最重要）** → 改角色令旧令牌立即失效 → 三套凭证各自可用域 → 兜底 |
| ② memory（召回隔离） | 同文件第 2 节（行 127 起） | 静态扫描 AST → 故意改坏证明扫描器会红 → 召回 SQL 必带 `elder_id` → 退房清理 → HTTP 层隔离 |
| ③ mcp_client（工具权限） | 同文件第 3 节（行 202 起） | 白名单与 schema → 失败四分类 → 越权=拒绝+审计+`trace_id` → 预算与熔断 → 收尾 |

三节都是一页可照读的脚本（共 247 行），另有第 4 节「一页速查」（行 312）与第 5 节「验证状态」（行 322）。

---

## 7. 已知缺口与未验证项

| 项 | 状态 | 说明 |
| --- | --- | --- |
| 工牌吊销的即时性 | **已知限制** | 无版本号，只能等 `expires_at`；目前只有到期这一条路（第 1.2 节） |
| 声纹比对（cosine ≥0.55） | **未接入** | PRD line 283 要求，`voiceprint_embedding_dimension = 192`（`app/core/config.py:202`）已配，比对链路未落 |
| 演示脚本第 5 步 auth/memory 的 HTTP 实跑 | **未验证** | 迭代 9 §6.1 已如实登记：共享 dev 库被并发 pytest 占用，只做了静态与单元层验证 |
| `identity_scope()` 并发压测 | **无数据** | 未做并发下 ContextVar 串号的实测，只有单元测试覆盖 |

---

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-08 | 首版。三套凭证的有效期以 `app/core/security.py` 实读为准（Access 2h / Refresh 7d / 设备会话 24h / 工牌 ≤86400s）；身份注入、`falsified_identity`、越权审计映射全部标注 file:line；未验证项单列。 |
| v1.1 | 2026-11-13 | §3.4 WS 首帧窗口 300ms → **3000ms**（`WS_AUTH_TIMEOUT_MS`），并校正该节全部漂移行号（信封 99–101、首帧判定 598–642、`auth_ok` 920、心跳/判死/关闭码 73/75/80/81）。依据 2026-11-13 P1 实测（trace `81ef2928c54b475e82069be408c7da7f`）：pad 刷新时主线程被 10.7MB VRM 解析占住，auth 帧到达 0.5–1s，旧窗口误判 timeout → 4401 → 清 sessionStorage 踢回登录页；窗口是活体防护而非安全边界，鉴权强度不变。影响面：C2 消费方 `frontend/src/pad/signal-socket.ts` 对 `code=timeout` 改走退避重连；不改任何表与字段。 |
