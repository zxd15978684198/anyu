# Mock Operations MCP（迭代 8 · N20）

把「T3 框」从画出来的架构图变成能落库的真服务：两个 Mock 工具
`create_work_order` 与 `send_family_notification`，传输与 weather-mcp 同款
（streamable-http，`/mcp` 协议面 + `/health` 探活）。

```
mcp-servers/mock_operations/
├── __init__.py    # 包说明，不放 eager 导入
├── server.py      # MCP 工具面 + /health + CLI（stdio / http）
├── ops.py         # 参数校验 + 复用 app/services 的唯一写实现 + 审计
└── README.md      # 本文件：决策、契约、验收、边界
```

## §1 落库实现：只有一个，MCP 复用 service 层

**决策**：MCP 进程直接导入 backend 的 service 层写库，MCP 自己的代码里**没有任何
建单/通知的 INSERT**。生产入口与 backend 同镜像（compose 里同为
`${BACKEND_IMAGE:-anyu-backend:local}`），只挂只读源码目录、换
`command`，所以「同一份实现」不是约定，是同一份物理代码。

**仓内唯一的写实现**（N20 之前就存在，未新增第二份）：

| 能力 | 唯一实现 | 现有调用方 |
| --- | --- | --- |
| 工单插入 | `app/services/medication_service.py:717 _insert_work_order`（含 `WO-YYYYMMDD-NNNN` 单号、唯一冲突重算一次） | `escalate_to_work_order`（漏服升级）、`refuse_event`（拒服跟进） |
| 通知创建 | `app/services/notification_service.py:248 create_notification`（kind/severity 闭集、`generated` 初始态、payload 落 JSONB） | 健康采集调度、用药调度、健康服务 |
| 审计写入 | `app/services/audit_service.py:477 write_audit` / `:503 write_audit_isolated` | 各业务服务与编排层 |

`ops.py` 只是把「校验 → 调唯一实现 → 写审计 → commit」串起来，不新增写库代码。

**为什么不用「MCP 通过内部 HTTP 调 backend」**（卡片允许的另一条路）：

1. `app/api/**` 里没有内部建单/通知端点 —— 今天不存在，新增又要过三套鉴权与
   `runtime_context` 注入（AGENTS §4.9：身份只能服务端注入，客户端传入一律丢弃），
   属于 N21 之后的改造面；
2. 走 HTTP 会多一层失败模式（backend 挂了 MCP 还得自己判重试），而 MCP 与 backend
   本来就同库同源码；
3. 直接把 service 函数当库调用，验收时「一套写库」可以靠 grep 证明，而不是靠文档承诺。

**代价（如实记）**：

- mock-operations 容器必须能连库，compose 里给它注入了与 backend 同源的
  `DATABASE_URL`（postgres 服务名）；
- 它依赖 `app/` 目录在镜像里（Dockerfile 已 `COPY app/`），且必须与 backend 同版本
  发布 —— 换镜像时两个服务一起重建；
- 工单插入点目前是**私有名** `_insert_work_order`。N20 只做「复用」，不改
  `medication_service.py`（那份文件有他人在途）；N21 再把它升成公开名或让
  `escalate_to_work_order` 反向走 MCP，届时这里的私有 import 一并收口。

## §2 工具契约

两个工具的出参都是同一个信封（`server.py:ToolResult`）：

```json
{"ok": true,  "data": {...}, "error_code": null, "detail": null, "retryable": false}
{"ok": false, "data": null,  "error_code": "param_error", "detail": "unknown_type", "retryable": false}
```

业务失败**不抛协议级异常**（`is_error` 恒 false），与 weather-mcp 的消费契约一致：
客户端（`app/mcp_client/`）按 `error_code` 走四分类，模型只看到稳定细分码，看不到异常栈。

### `create_work_order(elder_id, type, severity, note?)`

| 参数 | 校验 |
| --- | --- |
| `elder_id` | 必须是 UUID 且 `elders` 表存在；不存在 → `not_found / elder_not_found` |
| `type` | `medication_service.WORK_ORDER_TYPES` 八值闭集（TD §3.4） |
| `severity` | 仅 `red` **或** `yellow` —— 库内 CHECK 只有这两档，没有 green |
| `note` | 可选，≤1000 字；空则写成 `MCP create_work_order（type）` |

成功 `data`：`order_id / order_no / status(new) / type / severity / elder_id`。

### `send_family_notification(elder_id, kind, message)`

| 参数 | 校验 |
| --- | --- |
| `elder_id` | 同上 |
| `kind` | `NOTIFICATION_KINDS` 四值：`health_review` / `medication_escalation` / `collection_missed` / `family_report` |
| `message` | 必填，≤500 字，写进 `payload.summary`（家属端摘要优先取它，见 `frontend/src/family/notifications.ts`）与 `payload.message` |

`severity` 不在入参里，按 kind 推导（`medication_escalation→red`，
`health_review/collection_missed→yellow`，`family_report→green`）：严重度跟事件性质走，
不允许调用方随意拔高或压低。

### 错误码

| error_code | detail 示例 | 客户端处置 |
| --- | --- | --- |
| `param_error` | `invalid_elder_id` / `unknown_type` / `invalid_severity` / `note_too_long` / `unknown_kind` / `message_too_long` | 修正参数后重试 |
| `not_found` | `elder_not_found` | 如实告知，不编造 |
| `transient` | `timeout` / `db_unavailable` | 可重试（mcp_client 退避 ≤2 次） |

**缺必填字段不进上面的信封**：入参 JSON-Schema 由签名生成，缺字段先被协议层挡掉
（`isError` + 校验文本）；backend 侧更早——`app/mcp_client/client.py:216` 的
registry schema 校验在**建连前**就把缺字段判成 `param_error`，不消耗一次调用。
上面的信封只负责语义校验（uuid 形态、枚举闭集、长度上限）。

## §3 审计：`audit_logs.action = 'tool.call'`

- 成功路径：与业务写**同事务**（`write_audit`）—— 提交成功才有「单 + 账」，回滚则两者
  都不留。`actor_type='system'`，`actor_id=elder_id`，`resource_type` 为
  `work_order` / `notification`，`resource_id` 是刚落库的那一行。
- 被拒路径（参数非法/目标不存在）：用**独立会话**写（`write_audit_isolated`），
  因为业务事务已经不存在；`resource_type='tool'`，`detail.ok=false` 并带
  `error_code/detail`。`elder_id` 解析不出 UUID 时用 `NIL_RESOURCE_ID`。
- 家属通知正文不进审计原文，只留 `{length, preview(≤40 字)}`（AGENTS §4.3/§4.10）。

查证据：

```bash
docker exec anyu-postgres psql -U anyu -d anyu -c \
  "select action, resource_type, detail from audit_logs where action='tool.call' order by created_at desc limit 5;"
```

## §4 运行与验收

```bash
# 起服务（compose 里已从「内联 501 桩」换成真服务）
docker compose up -d mock-operations-mcp
curl -s http://127.0.0.1:7102/health        # {"ok":true,"service":"mock-operations-mcp","tools":[...]}

# 真 MCP 客户端调用（容器内跑，避免宿主机依赖）
docker exec anyu-backend python - <<'PY'
import asyncio
from mcp import Client
async def main():
    async with Client("http://mock-operations-mcp:7102/mcp") as c:
        print(await c.list_tools())
        print(await c.call_tool("create_work_order", {"elder_id": "<uuid>", "type": "follow_up", "severity": "yellow", "note": "验收"}))
asyncio.run(main())
PY
```

## §5 已知边界

1. **没有幂等键**：工具 schema（PRD 口径）只有四个参数，同一次调用若在「落库成功、
   响应丢失」后重试，会再建一张单。当前由调用方（编排层）按业务事件去重；真要根治
   得在 schema 加 `idempotency_key`，属契约变更，不在 N20。
2. **身份注入在客户端，不在本服务**：`elder_id` 由 `app/mcp_client/client.py` 的
   `_inject_identity_elder` 在派发处用 `runtime_context` 覆盖（模型传什么都丢弃，
   AGENTS §4.9）。本服务拿不到「调用方是谁」，只守「值存在且合法」：缺失/非法回
   `param_error/elder_id_missing`，长者不存在回 `not_found/elder_not_found`。
3. **不接真实推送**：`send_family_notification` 只落库，不调短信/微信（AGENTS §4.5）。
4. **`/health` 不探库**：库挂了进程也该能起，失败由工具信封表达；探库的职责在 backend。
5. **compose 的 healthcheck 仍是 TCP/urllib 二选一**：当前用 `/health` GET，与桩时期
   的语义一致（200 即健康）。
6. **目录名用下划线 `mock_operations`，不是 `mock-operations`**：`mypy` 门禁跑的是
   `uv run --locked mypy app mcp-servers`，它会把带 `__init__.py` 的目录当包；连字符目录
   直接被判 `contains __init__.py but is not a valid Python package name`（EXIT 2）。
   所以模块路径是 `mcp-servers.mock_operations.server`；compose 的**服务名**仍是
   `mock-operations-mcp`（那是网络标识，不是 Python 标识符）。
7. **观察区 · 传输层鉴权（mTLS / 签名头）**：客户端强制覆盖能守住「模型/调用方传错人」
   的越权写，但守不住「别的进程绕过 `app/mcp_client` 直连 7102 写库」——本服务无法验证
   调用方身份。补法是在传输层带可验证凭据（内网 mTLS，或签名头 + 共享密钥），属独立
   议题，不在 迭代 8/N20-N21 范围内（2026-11-10 产品负责人裁决）。当前缓解：端口只绑
   `127.0.0.1`，且 compose 网络内部可达。
