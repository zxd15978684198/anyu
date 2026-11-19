# owner=W3 · 依据 PRD FR-DLG-10 / TD §6.2 · 工具白名单注册表（迭代 6 W3）

"""迭代 6 工具注册表：白名单 + 入参 schema 校验 + 失败四分类 + 调用留痕。

周界（本波写锁）
- `app/mcp_client/registry.py`（W4 在途）尚无稳定接口：后端契约用**本地只读 Protocol**
  `ToolBackend`（`async def call(name, args) -> ToolResult`）定义，本模块**不 import
  `app.mcp_client` 的任何实现**；W4 落地后由 Lead 在集成时对齐接缝。
- guardrails 三闸（预算 / 熔断 / 重复短路）由 companion 在**调用外侧**统一过
  （`Guardrail.check`），本模块不重复建闸；参数规范化复用同包 迭代 5 的
  `canonical_args`（一能力一形态，不重写）。

对抗口径（AGENTS §4.9 / §8）
- 白名单 = {"get_weather", "search_institution_knowledge"}：白名单外的工具名一律
  `forbidden`，**绝不触达后端**；
- **按工具选后端**：`get_weather` 走 MCP（`_McpToolBackend` → weather server），
  `search_institution_knowledge` 走**本地**后端（`KnowledgeToolBackend` →
  `app/services/knowledge_service.py`）。本地工具不经过 MCP、不依赖天气服务在线，
  但同样受白名单与 schema 闸约束（N22 注册；同一能力一种形态，别再造第二套检索）；
- 入参 schema 校验（当前唯一 schema：get_weather 需要非空字符串 city；未注册
  schema 的工具与多余字段同样拒绝，拒绝理由只带字段名/规则码，不回显外部值）；
- 失败四分类 `ToolResult.status`：ok / param_error / not_found / transient / forbidden
  （参数错 → 由 companion 回传模型重试并已计入预算；目标不存在 → 如实告知不编造；
  临时错误 → 本层只转告，退避重试 ≤2 次归 W4 的 mcp_client 工具层；越权 → 拒绝）；
- 每次调用都产出 `(tool, args_canonical, allowed, cost_steps=1)` 的留痕交给
  companion（`ToolCallRecord`），其 `gate` 字段额外说明这次调用被哪道闸判定。
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol

from app.agents.guardrails import canonical_args

logger = logging.getLogger("app.agents.tool_registry")

#: 工具失败四分类（AGENTS §8 的工具层映射）。
ToolStatus = Literal["ok", "param_error", "not_found", "transient", "forbidden"]

TOOL_STATUS_OK: Final[ToolStatus] = "ok"
TOOL_STATUS_PARAM_ERROR: Final[ToolStatus] = "param_error"
TOOL_STATUS_NOT_FOUND: Final[ToolStatus] = "not_found"
TOOL_STATUS_TRANSIENT: Final[ToolStatus] = "transient"
TOOL_STATUS_FORBIDDEN: Final[ToolStatus] = "forbidden"

#: 工具白名单（迭代 6 放行 get_weather；N22 追加机构知识检索；N21 追加运营两个写工具）。
#: 新增工具在同一处收口并同步 schema/后端，**不允许**在别处再开第二个白名单。
#: `create_work_order` / `send_family_notification` 走 mock-operations MCP，不走天气
#: 客户端：`ToolRegistry(local_backends=...)` 按工具名分流（见 runtime_factory）。
WHITELIST: Final[frozenset[str]] = frozenset(
    {
        "get_weather",
        "search_institution_knowledge",
        "create_work_order",
        "send_family_notification",
    }
)

#: 本地工具（不经 MCP，由进程内后端直接服务）。与 `WHITELIST` 的交集必须等于它本身：
#: 只登记在这里而没进白名单，等于谁也调不到。
LOCAL_TOOLS: Final[frozenset[str]] = frozenset({"search_institution_knowledge"})


@dataclass(frozen=True, slots=True)
class ToolResult:
    """后端协议返回：失败四分类 + 内容字典（内容形状由各后端自定）。"""

    status: ToolStatus
    content: dict[str, Any]


class ToolBackend(Protocol):
    """本地只读后端契约（W4 `mcp_client.registry` 在途；集成时由 Lead 对齐接缝）。"""

    async def call(self, name: str, args: dict[str, Any]) -> ToolResult: ...


class KnowledgeToolBackend:
    """机构知识检索的**本地**后端（N22）：直接调 `knowledge_service`，不经 MCP。

    为什么单独一个后端而不是塞进 weather 的 `_McpToolBackend`：知识库在 Postgres 里，
    走 MCP 要么多一个 server、要么让 weather server「顺带」查业务库——前者是新的第二种
    实现形态（AGENTS §4 第 8 条），后者让一个只读外部天气的进程拿到业务库连接。

    每次调用开一个短会话（`app.db.session_factory`），读完即关；**只读**，不写库，
    也不在这层做权限判断（工具调用方是受约束 Agent，权限在 register/whitelist 与本层之上）。

    降级语义：`reason="degraded"`（没有可用 embedding）返回 `transient` —— 让编排层用
    「查询出了点小状况」这类转告，而不是让长者听到「没有这条规定」（那是 `no_match`）。
    """

    async def call(self, name: str, args: dict[str, Any]) -> ToolResult:
        if name not in LOCAL_TOOLS:
            return ToolResult(
                status=TOOL_STATUS_NOT_FOUND, content={"reason": "unknown_local_tool"}
            )
        query = str(args.get("query", "")).strip()
        # 延迟导入：本模块在无数据库的纯单测里也会被 import，不要在模块顶部建引擎。
        from app.db import session_factory
        from app.services import knowledge_service

        async with session_factory() as session:
            result = await knowledge_service.search_knowledge_result(session, query=query)
        if result.degraded:
            return ToolResult(status=TOOL_STATUS_TRANSIENT, content=result.as_tool_payload())
        return ToolResult(status=TOOL_STATUS_OK, content=result.as_tool_payload())


def default_local_backends() -> dict[str, ToolBackend]:
    """本地工具 → 后端。`ToolRegistry` 缺省就用它，集成层无需再写一行。"""
    return {name: KnowledgeToolBackend() for name in LOCAL_TOOLS}


#: 失败四分类对长者的「转告」话术（诚实、不透露内部细节；AGENTS §8）。
#: 设计口径：这是**工具结果转告**，不是系统级降级 —— 不与
#: `app/core/degrade_speech.py`（asr/tts/llm/tool_breaker/internal）共用键，
#: 也不在那里追加键（迭代 5 已收口的话术源保持不动）；本表是工具层唯一源。
TOOL_FAILURE_SPEECH: Final[dict[str, str]] = {
    TOOL_STATUS_PARAM_ERROR: "麻烦您再说一遍，我来帮您查。",
    TOOL_STATUS_NOT_FOUND: "这个地点的天气我暂时查不到，您可以换种说法试试。",
    TOOL_STATUS_TRANSIENT: "查询出了点小状况，我们等一小会儿再试。",
    TOOL_STATUS_FORBIDDEN: "这个查询现在不能帮您做，我去请工作人员来帮忙。",
}


@dataclass(frozen=True, slots=True)
class ToolParamSpec:
    """单字段 schema（迭代 6 本地版；W4 换 MCP inputSchema 时收口进来）。"""

    name: str
    required: bool = True


_TOOL_PARAM_SPECS: Final[dict[str, tuple[ToolParamSpec, ...]]] = {
    "get_weather": (ToolParamSpec("city"),),
    # 只收一个 `query`：类别过滤与条数上限由服务层默认值兜住，别把「模型随手编个
    # category」变成 param_error（知识检索的失败该是"没查到"，不是"参数不对"）。
    "search_institution_knowledge": (ToolParamSpec("query"),),
    # N21：`elder_id` 声明为**非必填**是有意的 —— 它是被 `McpClient.call_tool` 用
    # runtime_context 覆盖的服务端注入字段（AGENTS §4.9）。这里必填会把「模型不知道
    # 自己是谁」变成参数错；可选则「模型传了别人的 id」也能走到覆盖那一步再被丢弃。
    # `note` 可选：给药工单常只带类型与严重度，长备注由服务端截断到 1000 字。
    "create_work_order": (
        ToolParamSpec("elder_id", required=False),
        ToolParamSpec("type"),
        ToolParamSpec("severity"),
        ToolParamSpec("note", required=False),
    ),
    "send_family_notification": (
        ToolParamSpec("elder_id", required=False),
        ToolParamSpec("kind"),
        ToolParamSpec("message"),
    ),
}


def _validate_args(tool: str, args: Mapping[str, Any]) -> tuple[dict[str, str], str | None]:
    """校验并规范化入参：返回 `(cleaned, reason)`。

    理由只带字段名/规则码（不回显外部值）；多余字段与未注册 schema 一律算参数错。

    `required=False` 的字段（`elder_id`）**只在缺失时豁免**：它是服务端用
    runtime_context 覆盖的注入字段，模型不传才是常态；一旦传了（哪怕是空串/数字），
    仍按普通字段走类型与非空校验 —— 否则「可选」会被读成「不校验」。
    """
    specs = _TOOL_PARAM_SPECS.get(tool)
    if specs is None:
        return {}, "schema_not_registered"
    spec_map = {spec.name: spec for spec in specs}
    cleaned: dict[str, str] = {}
    for key, value in args.items():
        if key not in spec_map:
            return {}, f"unexpected_field:{key[:24]}"
        if not isinstance(value, str):
            return {}, f"field_must_be_string:{key}"
        cleaned[key] = value.strip()
    for spec in specs:
        if spec.name not in cleaned:
            if not spec.required:
                continue  # 缺失才豁免；存在时下面照样校验（别把「可选」读成「不校验」）
            return {}, f"missing_field:{spec.name}"
        if not cleaned[spec.name]:
            return {}, f"field_must_be_nonempty:{spec.name}"
    return cleaned, None


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    """一次工具调用的留痕（本模块 → companion 的四字段契约打包处）。

    契约四件：`(tool, args_canonical, allowed, cost_steps=1)`；
    `executed` / `gate` 是补充位（是否真触达后端、被哪道闸判定），便于审计回放。
    """

    tool: str
    args_canonical: str
    allowed: bool
    cost_steps: int = 1
    executed: bool = False
    gate: str | None = None


@dataclass(frozen=True, slots=True)
class ToolCallOutcome:
    """`call()` 的返回：留痕 + 分类结果成对给出。"""

    record: ToolCallRecord
    result: ToolResult


class ToolRegistry:
    """工具白名单注册表：白名单 → schema 校验 → 后端执行 → 分类结果 + 留痕。

    guardrails 三闸不在这里判定（companion 在调用前过 `Guardrail.check`），
    因此本模块重复接受同一调用也不会绕过任何护栏。
    """

    def __init__(
        self,
        *,
        backend: ToolBackend,
        whitelist: Collection[str] | None = None,
        local_backends: Mapping[str, ToolBackend] | None = None,
    ) -> None:
        self._backend = backend
        self._whitelist: frozenset[str] = (
            frozenset(whitelist) if whitelist is not None else WHITELIST
        )
        # 本地工具缺省就有后端（`default_local_backends()`）：调用方不必为了知识检索
        # 多写一行装配，也就不会出现「白名单放行但后端没接」的半可用状态。
        merged: dict[str, ToolBackend] = dict(default_local_backends())
        if local_backends is not None:
            merged.update(local_backends)
        self._local_backends: dict[str, ToolBackend] = merged

    def is_whitelisted(self, name: str) -> bool:
        """白名单只读查询（测试与集成排障用）。"""
        return name in self._whitelist

    def build_record(
        self,
        tool: str,
        args: Mapping[str, Any],
        *,
        allowed: bool,
        executed: bool,
        gate: str | None = None,
    ) -> ToolCallRecord:
        """统一留痕构造（companion 的 short_circuit 路径也用它，保证形状一致）。"""
        return ToolCallRecord(
            tool=tool,
            args_canonical=canonical_args(args),
            allowed=allowed,
            cost_steps=1,
            executed=executed,
            gate=gate,
        )

    async def call(self, tool: str, args: Mapping[str, Any] | None) -> ToolCallOutcome:
        """校验后执行一次工具；白名单外与 schema 不合格**绝不触达后端**。"""
        raw = dict(args or {})
        if tool not in self._whitelist:
            logger.info("tool denied by whitelist", extra={"tool": tool})
            record = self.build_record(tool, raw, allowed=False, executed=False, gate="whitelist")
            return ToolCallOutcome(
                record=record,
                result=ToolResult(
                    status=TOOL_STATUS_FORBIDDEN,
                    content={"reason": "tool_not_in_whitelist"},
                ),
            )
        cleaned, reason = _validate_args(tool, raw)
        if reason is not None:
            logger.info("tool rejected by schema", extra={"tool": tool, "reason": reason})
            record = self.build_record(tool, raw, allowed=False, executed=False, gate="schema")
            return ToolCallOutcome(
                record=record,
                result=ToolResult(status=TOOL_STATUS_PARAM_ERROR, content={"reason": reason}),
            )
        try:
            backend = self._local_backends.get(tool, self._backend)
            result = await backend.call(tool, cleaned)
        except Exception:  # 兜底：后端崩溃按临时错误转告，不把异常漏给编排层。
            logger.exception("tool backend crashed", extra={"tool": tool})
            result = ToolResult(status=TOOL_STATUS_TRANSIENT, content={"reason": "backend_error"})
        record = self.build_record(tool, raw, allowed=True, executed=True)
        return ToolCallOutcome(record=record, result=result)


__all__ = [
    "LOCAL_TOOLS",
    "TOOL_FAILURE_SPEECH",
    "TOOL_STATUS_FORBIDDEN",
    "TOOL_STATUS_NOT_FOUND",
    "TOOL_STATUS_OK",
    "TOOL_STATUS_PARAM_ERROR",
    "TOOL_STATUS_TRANSIENT",
    "WHITELIST",
    "KnowledgeToolBackend",
    "ToolBackend",
    "ToolCallOutcome",
    "ToolCallRecord",
    "ToolParamSpec",
    "ToolRegistry",
    "ToolResult",
    "ToolStatus",
    "default_local_backends",
]
