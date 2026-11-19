# owner=W3 · 依据 设计记录（编排接线） §3 / §4-W3 · 编排接线：工具层与 Agent runner

"""编排接线的装配工厂（迭代 10 · W3）：把工具层与 Agent runner 接到对话主链上。

## 接的是什么

`McpClient`（协议/超时/退避/四分类）→ `McpRegistry`（发现缓存 + 白名单 + 最小 JSON-Schema）
→ `ToolRegistry`（第二道白名单/schema 闸 + 留痕）→ `companion.run_turn`（ReAct 循环）。
本模块只负责「把已有零件接起来并注入 `DialogueService`」，**不重写任何一层的能力**：
白名单口径取 `app/agents/tool_registry.WHITELIST`，注册台缺省取
`app/mcp_client/registry.DEFAULT_TOOL_WHITELIST`，两处都不在这里另立一份。

## 失败口径（与 `app/main.py::_build_dialogue_runtime` 同一口径）

装配失败**一律返回 `None` + `warning`，绝不抛**：工具层是增强依赖，起不来只该让长者
「查不了天气」，不该让后端起不来（AGENTS §4.8：天气=MCP Tool 唯一形态，失败走四分类
`transient` 的降级话术）。`build_agent_runner` 在 `tool_registry is None` 时同样返回 `None`
—— 没有工具就不假装有 Agent，否则 L2 会拿一个只能闲聊的 runner 顶掉模板话术，反而更糟。

## 为什么启动探活之后立刻断开

`McpClient` 的类 docstring 明写「此对象不支持并发调用」，而 SDK `Client.__aenter__/__aexit__`
是 anyio cancel scope：**跨任务拆连接会踩 task-affinity**（`app/mcp_client/client.py` 模块头
同一条记账规则）。因此启动时只做一次性探活（`connect` + `discover`），既确认「真的连得上」，
也顺手把真实 `inputSchema` 灌进 `McpRegistry`；探完立刻 `close()`。真正的工作连接由
**首个工具调用的那个请求任务**懒建（`call_tool` 内部 `_ensure_connected`），建与拆同任务，
不跨任务。lifespan 关停时再用 `close_tool_registry()` 兜一次（幂等，通常是空连接）。

## W2 契约对齐

`AgentResult` / `AgentRunner` 由 W2 定义在 `app/services/dialogue_service.py`（计划 §3），
本模块**直接 import 那几个名字**，不另立一份同形 Protocol（一能力一形态，AGENTS §4.8）。
本模块只实现一个 `_AgentReply`（`frozen` dataclass，只读字段即满足属性协议）作为载体。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlsplit

from app.agents.companion import (
    HealthCheckup,
    KnowledgeLookup,
    MedicationConfirm,
    MemoryRecaller,
    ProgressSink,
    RuntimeContext,
    run_turn,
)
from app.agents.tool_registry import (
    TOOL_STATUS_TRANSIENT,
    WHITELIST,
    ToolRegistry,
    ToolResult,
)
from app.core.config import Settings
from app.core.context import DialogueIdentity, PersonaContext, get_thread_id, get_trace_id
from app.mcp_client.client import McpClient
from app.mcp_client.client import ToolResult as McpToolResult
from app.mcp_client.registry import MOCK_OPERATIONS_TOOL_WHITELIST, McpRegistry

# W2 的交叉契约（计划 §3）：本模块只消费并在 `__all__` 里再导出，不另立同形 Protocol。
from app.services.dialogue_service import AgentResult, AgentRunner, RunnerResult
from app.services.llm_service import LLMService

# 每轮召回器的工厂：入参是本轮身份，出参是一个只认「这一句话」的 recaller（或 None）。
# 由 `app/services/memory_recall.py` 提供，本模块只消费类型 —— 召回的 SQL/脱敏归 services。
from app.services.memory_recall import RecallFactory

logger = logging.getLogger("app.agents.runtime_factory")

#: 本工厂唯一接入的 MCP server 名（也是 `McpRegistry.discover` 的发现缓存键）。
WEATHER_SERVER_NAME: Final[str] = "weather"

#: mock-operations 的 server 名（发现缓存键）与工具面（N21：白名单与天气分开）。
OPERATIONS_SERVER_NAME: Final[str] = "mock-operations"
OPERATIONS_TOOLS: Final[frozenset[str]] = frozenset(MOCK_OPERATIONS_TOOL_WHITELIST)


# --------------------------------------------------------------------------- Agent 契约


@dataclass(frozen=True, slots=True)
class _AgentReply:
    """`AgentResult` 的本地载体（属性协议靠结构满足，不继承、不注册）。

    四个只读字段（frozen）正好对上 W2 的 `AgentResult`：`degraded` 由
    `CompanionTurnResult.degrade_reason` 是否为空决定 ——「真降级」与「只是没触发工具」
    不许混为一谈；`tool_calls` 只留次数（列表细节归 `companion` 的 trace/日志）。
    """

    text: str
    degraded: bool
    tool_calls: int
    finish_reason: str


# --------------------------------------------------------------------------- 工具层


class _McpToolBackend:
    """把 `McpClient.call_tool` 适配成 `ToolBackend`（`app/agents/tool_registry.py` 的本地 Protocol）。

    四分类状态由 client 给出（两处是同一个闭集），这里只把 `content: dict | None` 归一成
    `dict`：`companion` 的 observations 按 dict 消费（`getattr(result, "content", {}) or {}`）。
    超时/退避/白名单三闸都在 client 内部，本适配层**不重复实现**，只补一层 trace_id 透传。
    """

    def __init__(self, client: McpClient) -> None:
        self._client = client

    @property
    def client(self) -> McpClient:
        """底层客户端（`tool_client_of` / 关停路径取用）。"""
        return self._client

    async def call(self, name: str, args: dict[str, Any]) -> ToolResult:
        raw: McpToolResult = await self._client.call_tool(name, args, trace_id=get_trace_id())
        return ToolResult(status=raw.status, content=_as_content(raw))


def _as_content(result: McpToolResult) -> dict[str, Any]:
    """把 client 的结构化返回归一成非空 dict（空 dict 会让 observation 丢信息）。"""
    if result.content:
        return dict(result.content)
    return {"reason": result.status, "message": result.message}


class _UnavailableMcpBackend:
    """天气 MCP 不可达时的占位后端：**只废天气，不连坐知识检索**。

    为什么不再返回 `None`：`build_tool_registry` 曾经在天气探活失败时 `return None`，
    而 `build_agent_runner` 见注册表为 `None` 就整体不建 —— 于是一个外部 HTTP 服务抖动
    会把**本地知识检索**一起拖死（两者唯一的共同点是「都叫工具」）。这里的取舍是：
    工具层始终装配成功，MCP 侧每一项调用返回 `transient`（编排层按「临时错误」退避/降级
    话术处理），本地后端的工具照常可用。

    不实现 `close`/`client`：`tool_client_of` 靠 `getattr(..., "client", None)` 取不到
    客户端，关停路径自然 no-op，不需要额外分支。
    """

    async def call(self, name: str, args: dict[str, Any]) -> ToolResult:
        """任何 MCP 工具调用都回 `transient`（临时错误），不伪装成 `not_found`。"""
        logger.warning(
            "mcp tool call while weather server unreachable",
            extra={"server": WEATHER_SERVER_NAME, "tool": name},
        )
        return ToolResult(
            status=TOOL_STATUS_TRANSIENT,
            content={"reason": "mcp_unavailable", "server": WEATHER_SERVER_NAME},
        )


class _UnavailableOperationsBackend:
    """mock-operations 不可达时的占位后端：只废这两项，不连坐天气与本地知识。

    与 `_UnavailableMcpBackend` 同一取舍（见上）：工具层始终装配成功，派单/通知回
    `transient`（编排层按临时错误处理并给降级话术），而不是伪装成 `not_found` ——
    「服务挂了」和「没有这张单」是两种事实，混起来会让 Agent 说错话。
    """

    async def call(self, name: str, args: dict[str, Any]) -> ToolResult:
        logger.warning(
            "mcp tool call while mock-operations server unreachable",
            extra={"server": OPERATIONS_SERVER_NAME, "tool": name},
        )
        return ToolResult(
            status=TOOL_STATUS_TRANSIENT,
            content={"reason": "operations_unavailable", "server": OPERATIONS_SERVER_NAME},
        )


async def _build_operations_backend(
    settings: Settings,
) -> _McpToolBackend | _UnavailableOperationsBackend:
    """装配 mock-operations 后端（N21）：探活 + 发现，失败降级为 transient 占位。

    单独建一个 `McpClient`/`McpRegistry` 而不是复用天气那个：`McpRegistry.discover`
    的缓存键是 `backend.name`，两套 server 的白名单与工具面互不相干；混用一个 client
    会让「天气挂了」把派单一起判成不可用（反过来也一样）。
    """
    url = settings.mcp_operations_server_url.strip()
    if not url:
        logger.warning("mock-operations tool disabled: mcp_operations_server_url is empty")
        return _UnavailableOperationsBackend()
    if not urlsplit(url).path.strip("/"):
        # 缺省配置只写到 host:port（`config.py` 的 `http://localhost:7102`），而本服务的
        # streamable-http 端点在 `/mcp`（`server._http_target` 同口径）。补一次而不是让
        # 每次启动都握手失败降级 —— 这条降级链太安静，根本没人会发现。
        url = f"{url.rstrip('/')}/mcp"

    registry = McpRegistry(MOCK_OPERATIONS_TOOL_WHITELIST)
    client = McpClient(
        name=OPERATIONS_SERVER_NAME,
        url=url,
        timeout_seconds=float(settings.mcp_call_timeout_seconds),
        registry=registry,
    )
    try:
        await client.connect()
        descriptors = await registry.discover(client)
    except Exception as exc:  # 连接级异常含 SDK 的 ExceptionGroup，一律降级
        logger.warning(
            "tool layer degraded: mock-operations server unreachable",
            extra={"error_type": type(exc).__name__, "server": OPERATIONS_SERVER_NAME},
        )
        await _close_quietly(client)
        return _UnavailableOperationsBackend()

    await _close_quietly(client)
    discovered = {descriptor.name for descriptor in descriptors}
    missing = OPERATIONS_TOOLS.difference(discovered)
    if missing:
        logger.warning(
            "mock-operations whitelisted tool missing on server",
            extra={"server": OPERATIONS_SERVER_NAME, "missing": sorted(missing)},
        )
    else:
        logger.info(
            "mock-operations tool layer ready",
            extra={"server": OPERATIONS_SERVER_NAME, "tools": sorted(discovered)},
        )
    return _McpToolBackend(client)


async def build_tool_registry(settings: Settings) -> ToolRegistry | None:
    """装配工具层：`McpClient` + `McpRegistry` + `ToolRegistry`；失败返回 `None`（不抛）。

    探活（`connect` + `discover`）在 lifespan 任务里做，探完立刻断开 ——
    工作连接由首个工具调用的请求任务懒建（见模块 docstring「为什么启动探活之后立刻断开」）。
    """
    operations_backend = await _build_operations_backend(settings)
    local_backends: dict[str, Any] = {name: operations_backend for name in OPERATIONS_TOOLS}

    url = settings.mcp_weather_server_url.strip()
    if not url:
        logger.warning("mcp weather tool disabled: mcp_weather_server_url is empty")
        # 天气没配不等于工具层没配好：知识检索是本地后端，必须照常可用。
        return ToolRegistry(
            backend=_UnavailableMcpBackend(), whitelist=WHITELIST, local_backends=local_backends
        )

    registry = McpRegistry()  # 缺省白名单 = {"get_weather": ()}（DEFAULT_TOOL_WHITELIST）
    client = McpClient(
        name=WEATHER_SERVER_NAME,
        url=url,
        timeout_seconds=float(settings.mcp_call_timeout_seconds),
        registry=registry,
    )
    try:
        await client.connect()
        descriptors = await registry.discover(client)
    except Exception as exc:  # 连接级异常含 SDK 的 ExceptionGroup，一律降级
        logger.warning(
            "tool layer unavailable: mcp weather server unreachable",
            extra={"error_type": type(exc).__name__, "server": WEATHER_SERVER_NAME},
        )
        if not urlsplit(url).path.strip("/"):
            # 本项目 weather-mcp 的 streamable-http 端点在 `/mcp`（`_parse_bind` 的缺省 path），
            # 只写 host:port 必然握手失败 —— 把这条最常见原因直接写进日志，别让它静默降级。
            logger.warning(
                "mcp weather url has no path; weather server serves at /mcp "
                "(check MCP_WEATHER_SERVER_URL / mcp_weather_server_url)",
                extra={"server": WEATHER_SERVER_NAME},
            )
        await _close_quietly(client)
        return ToolRegistry(
            backend=_UnavailableMcpBackend(), whitelist=WHITELIST, local_backends=local_backends
        )

    # 探活成功就断开：连接由首个调用的请求任务懒建（跨任务拆连接会踩 anyio task-affinity）。
    await _close_quietly(client)
    names = [descriptor.name for descriptor in descriptors]
    logger.info(
        "tool layer ready",
        extra={"server": WEATHER_SERVER_NAME, "tools": len(names)},
    )
    if WHITELIST.isdisjoint(names):
        # 连得上但没有白名单内的工具：不判装配失败（可能只是发现缓存被服务端拒），
        # 但把话说清楚，免得 W5 验收时「工具层装配成功却永远 not_found」无迹可查。
        logger.warning(
            "tool layer whitelisted tool missing on server",
            extra={"server": WEATHER_SERVER_NAME, "expected": sorted(WHITELIST)},
        )
    return ToolRegistry(
        backend=_McpToolBackend(client), whitelist=WHITELIST, local_backends=local_backends
    )


def tool_client_of(tool_registry: ToolRegistry | None) -> McpClient | None:
    """从注册表取回底层 `McpClient`（`app/main.py` 关停用）；非本工厂造的注册表返回 `None`。"""
    if tool_registry is None:
        return None
    client = getattr(getattr(tool_registry, "_backend", None), "client", None)
    return client if isinstance(client, McpClient) else None


def _clients_of(tool_registry: ToolRegistry | None) -> list[McpClient]:
    """所有由本工厂造出来的 client：天气后端 + `local_backends` 里挂的 mock-operations。

    `local_backends` 是 `ToolRegistry` 的私有属性，没有回读口（见 `tool_client_of` 同一取舍）；
    关停必须把它们都关掉，否则 `docker compose down` 时 ops client 的连接会拖着 lifespan 退出。
    """
    if tool_registry is None:
        return []
    clients: list[McpClient] = []
    weather = tool_client_of(tool_registry)
    if weather is not None:
        clients.append(weather)
    for backend in getattr(tool_registry, "_local_backends", {}).values():
        client = getattr(backend, "client", None)
        if isinstance(client, McpClient) and client not in clients:
            clients.append(client)
    return clients


async def close_tool_registry(tool_registry: ToolRegistry | None) -> None:
    """关停工具层（幂等；失败只 warning —— 关停期错误不许影响退出码）。"""
    for client in _clients_of(tool_registry):
        await _close_quietly(client)


async def _close_quietly(client: McpClient) -> None:
    try:
        await client.close()
    except Exception as exc:
        logger.warning("mcp client close failed", extra={"error_type": type(exc).__name__})


def _audit_runtime_context(identity: DialogueIdentity | None) -> RuntimeContext | None:
    """给审计 / 工具权限层造本轮上下文；拿不到服务端核实的主体就返回 `None`。

    为什么要造：`run_turn(..., runtime_context=None)` 让 `tool.denied` /
    `injection_blocked` 落库时 `resource_id` 与 `actor_id` 只能是全零 UUID，
    事后查不到「谁想对哪位长者做什么」（B3-07）。身份仍在服务端核实——
    主体取请求级 `get_identity()`（`kind` / `principal_id`），长者取本轮
    `DialogueIdentity.elder_id`；两者都没有时宁可返回 `None` 让审计回退，
    也绝不从文本或模型输出里猜（AGENTS §4.9）。
    """
    if identity is None:
        return None
    from app.core.runtime_context import get_identity

    request_identity = get_identity()
    actor_type = "system"
    actor_id: str | None = None
    elder_id: str | None = None
    if request_identity is not None:
        actor_type = str(getattr(request_identity, "kind", "system") or "system")
        principal_id = getattr(request_identity, "principal_id", None)
        actor_id = str(principal_id) if principal_id else None
        elder_id = getattr(request_identity, "elder_id", None)
    if not elder_id:
        elder_id = str(identity.elder_id)
    return RuntimeContext(
        elder_id=elder_id,
        thread_id=get_thread_id(),
        trace_id=get_trace_id(),
        actor_type=actor_type,
        actor_id=actor_id,
    )


# --------------------------------------------------------------------------- Agent runner


def build_agent_runner(
    *,
    settings: Settings,
    llm: LLMService,
    tool_registry: ToolRegistry | None,
    memory_recall: MemoryRecaller | None = None,
    recall_factory: RecallFactory | None = None,
    knowledge_lookup: KnowledgeLookup | None = None,
    health_checkup: HealthCheckup | None = None,
    medication_confirm: MedicationConfirm | None = None,
) -> AgentRunner | None:
    """把 `companion.run_turn` 包成 `AgentRunner`；没有工具层就返回 `None`。

    `settings` 当前不读：留形参只为给 `app/main.py` 一个稳定的调用面（步数预算/trace 采样
    这类配置若要接进来，从这里走，不动调用方）。`runtime_context=None` 是**故意的**：
    身份只由服务端注入，空上下文意味着「本轮没有已核实的身份」，`run_turn` 内部按
    `falsified_identity` 处理，绝不从文本里猜 `elder_id`/`thread_id`（AGENTS §4.9）。

    召回身份走**独立的 `identity` 形参**，不构造 `RuntimeContext`：那是审计/工具权限层
    才需要的载体（还带着 `thread_id`/`trace_id` 的注入口径），而记忆召回只要「这一轮是
    哪位长者」。两个用途挤进同一个对象，等于让召回顺手拿到它不该有的写权限面。

    `memory_recall` 与 `recall_factory` 的关系是「静态兜底 + 按轮覆盖」：前者是启动期
    装配好的固定召回器（没有身份时也照用），后者按本轮 `identity` 现造 —— 一旦这一轮
    带来了身份，就用它造出来的那个**覆盖**静态值（召回必须按 `elder_id` 过滤，静态
    recaller 拿不到身份，继续用它就是把上一位长者的记忆带进这一轮）。

    `knowledge_lookup` 也**原样转发**：机构知识是**全机构共用**的规章制度，不按长者
    切片，所以不像记忆那样有「按轮覆盖」——启动期造一次就够（FR-KNW-01~05）。

    `history` / `privacy` / `persona` 三个形参是**原样转发**，本模块不对它们做任何
    解释或加工：短期记忆的读写全在 `DialogueService`（记忆所有权一处，见那边的模块
    docstring），隐私收敛的判定在 `companion.is_privacy_converged`，人设的拼装也在
    `companion`。中间这一层只要「不丢、不改」就够了 —— 任何在这里补一手「顺手规整」
    的念头，都会让同一口径出现第二个实现。

    `identity` 与 `on_progress` 是**本轮级**的入口：前者决定召回按谁切片，后者是
    FR-CON-02 ① 的进度投递缝（`DialogueRuntime` 传进来，`companion` 在天气慢查询
    超过 3 秒时用它先说一句「正在查询」）。两者都由调用方按轮给，本层不缓存。
    """
    if tool_registry is None:
        logger.info("agent runner disabled: no tool registry")
        return None
    registry: ToolRegistry = tool_registry  # 绑定局部量：闭包内不让 mypy 退回 Optional

    async def runner(
        text: str,
        *,
        identity: DialogueIdentity | None = None,
        history: Sequence[Mapping[str, str]] | None = None,
        privacy: Any = None,
        persona: PersonaContext | None = None,
        on_progress: ProgressSink | None = None,
    ) -> AgentResult:
        # 静态值先落地，再按本轮身份覆盖：两处都写「recaller」一个变量，
        # 避免在 `run_turn(...)` 调用处出现 if/else 两份形参（漏改一边就是静默回归）。
        recaller: MemoryRecaller | None = memory_recall
        if identity is not None and recall_factory is not None:
            try:
                recaller = recall_factory(identity)
            except Exception as exc:
                # 记忆是**增强依赖**，与 `_probe_embedding`、`MemoryTurnHook` 同一口径：
                # 它挂了只许降级，不许连累这一轮回话。`companion._recall_memories` 已经把
                # 「召回**执行期**异常」收敛成 `[]`；factory 的「**构造期**异常」是同一类
                # 失败的另一半，两半口径必须一致 —— 否则「召回装配失败」会一路冒到
                # `respond` 的 except 里把整轮 L2 打回 `llm.chat`，连工具能力一起丢掉，
                # 爆炸半径明显错配。
                #
                # 这里**不重新赋值**：`recaller` 上一行已是静态值（有兜底就用它，没有就是
                # `None` = 本轮不召回），两种情况都不会把别位长者的记忆混进来。
                logger.warning(
                    "memory_recall_factory_failed",
                    extra={"error_type": type(exc).__name__},
                )
        result = await run_turn(
            text,
            llm=llm,
            tool_registry=registry,
            runtime_context=_audit_runtime_context(identity),
            trace_id=get_trace_id(),
            history=history,
            privacy=privacy,
            persona=persona,
            memory_recall=recaller,
            knowledge_lookup=knowledge_lookup,
            # 健康主诉 / 用药确认的前置路由（FR-HLT-07 / AC-34、AC-35）：
            # elder_id 只从本轮 identity 取——这是服务端核实的身份，不是模型自报。
            health_checkup=health_checkup,
            medication_confirm=medication_confirm,
            elder_id=str(identity.elder_id) if identity is not None else None,
            progress_sink=on_progress,
        )
        return _AgentReply(
            text=result.spoken_text,
            degraded=bool(result.degrade_reason),
            tool_calls=len(result.tool_calls),
            finish_reason=result.finish_reason,
        )

    return runner


__all__ = [
    "WEATHER_SERVER_NAME",
    "AgentResult",
    "AgentRunner",
    "RunnerResult",
    "build_agent_runner",
    "build_tool_registry",
    "close_tool_registry",
    "tool_client_of",
]
