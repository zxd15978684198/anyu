"""MCP 工具注册台：发现（缓存）/白名单/最小 JSON-Schema 参数校验（迭代 6 W4-half）。

口径出处
- 白名单齐平 `app/agents/tool_registry`（W3 本地 Protocol 那套）的数据口径：
  `get_weather` 是唯一真实外部工具（PRD §1.5、架构约定天气=MCP Tool 唯一形态、AGENTS §4.8
  不新增第二种同类能力实现）。缺省白名单只有 `get_weather`，角色集为空元组＝未按角色限制。
- 越权（白名单外）→ `ToolForbidden`：客户端 `McpClient.call_tool` 第 ① 闸查 `allows()`，
  fail-closed 且**不建任何连接**（AGENTS §5 高风险验收点：越权必须拒绝；audit 落库由
  W3/W6 的 tool_registry/services 层负责，本模块只提供判定与结构化日志）。
- 参数 schema：**手写**最小 JSON-Schema 校验器，不引第三方 jsonschema（迭代 6 计划约束）。
  支持子集：required / type(str,int,float,bool,object,array,null) / enum /
  additionalProperties=False / properties.default（canonical 补缺省） / items（数组逐项）。
  int 不收 bool 与 float（比 JSON Schema 草案更严，测试钉住）；bool 不顶 number。
- 校验错误只给机器可读 token（`missing_required:<path>` 等），供回传模型重试与测试钉住，
  不回显内部栈细节（AGENTS §8 对外安全话术口径）。
- 发现缓存：进程内 dict（key=backend.name），只遵守**显式 invalidate()**，无 TTL 刷新；
  fail-closed：白名单外工具不下发 descriptors（agent 永远看不到）。
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 避免运行时环依赖：registry 只做 duck-typing，不真 import client
    from app.mcp_client.client import McpClient

_LOG = logging.getLogger(__name__)

#: 缺省白名单：齐平 W3 tool_registry —— get_weather 唯一真实外部接口，空的()＝不限角色。
DEFAULT_TOOL_WHITELIST: Mapping[str, Collection[str]] = {"get_weather": ()}

#: mock-operations 的白名单（N21）：两个写工具的闸门与天气分开放，不并进 DEFAULT ——
#: 天气客户端与运营客户端是两个进程、两套工具面，混成一张表会让「白名单放行但后端
#: 没接」的半可用状态变成默认值（AGENTS §4.8 一能力一形态，不等于一注册台一张表）。
#: 参数 schema 不在客户端手写：`validate` 优先取 `discover()` 缓存里的 inputSchema，
#: 服务端声明什么就校验什么，避免两份 schema 各自漂移。
MOCK_OPERATIONS_TOOL_WHITELIST: Mapping[str, Collection[str]] = {
    "create_work_order": (),
    "send_family_notification": (),
}


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    """一条工具描述（discover 的产物，随缓存冻结）。"""

    name: str
    description: str | None = None
    input_schema: dict[str, Any] = field(default_factory=dict)
    allowed_roles: tuple[str, ...] = ()  # 与白名单一致的静态角色集（空＝不限角色）


class McpRegistry:
    """工具发现 + 白名单 + 参数校验。线程不安全单实例；进程内缓存由 `invalidate()` 显式清空。

    白名单语义：`whitelist: Mapping[str, Collection[str]]`，键=工具名，值=允许角色集合；
    空集合＝该工具不限角色（`allows()` 给定 roles 时只要求交集命中）。
    白名单外一票否决：`discover` 不下发、`validate`/`allows` 直接拒绝（fail-closed）。
    """

    def __init__(
        self,
        whitelist: Mapping[str, Collection[str]] | None = None,
        *,
        schemas: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        """`whitelist` 缺省用 `DEFAULT_TOOL_WHITELIST`；`schemas` 是本地预注册的参数 schema（可选）。"""
        source = whitelist if whitelist is not None else DEFAULT_TOOL_WHITELIST
        self._whitelist: dict[str, tuple[str, ...]] = {
            str(name): tuple(str(role) for role in (roles or ())) for name, roles in source.items()
        }
        self._registered_schemas: dict[str, dict[str, Any]] = {
            str(name): dict(schema) for name, schema in (schemas or {}).items()
        }
        # 进程内发现缓存：backend.name → 白名单内 descriptors 元组；只有 invalidate() 清空。
        self._cache: dict[str, tuple[ToolDescriptor, ...]] = {}

    # ------------------------------------------------------------------ 白名单

    def allows(self, name: str, *, roles: Collection[str] | None = None) -> bool:
        """白名单（+可选角色）判定。不在白名单 → False；在白名单且未按角色限制 → True。"""
        if name not in self._whitelist:
            return False
        if roles is None:
            return True
        allowed = self._whitelist[name]
        if not allowed:  # 空＝未按角色限制
            return True
        return bool(set(roles) & set(allowed))

    def roles_for(self, name: str) -> tuple[str, ...]:
        """该工具在白名单里的角色集；白名单外同样返回空（配合 allows 先判白名单再用）。"""
        return self._whitelist.get(name, ())

    # ------------------------------------------------------------------- 发现

    async def discover(self, backend: McpClient) -> list[ToolDescriptor]:
        """发现工具并缓存。缓存命中（key=backend.name）不打网络；白名单外工具不下发。

        `backend` 只要求 `.name` 属性与 `async list_tools()`， tests 里可用假替身。
        list_tools 的任何异常原样抛出（连接级故障由 client 层四分类处理）。
        """
        key = getattr(backend, "name", None)
        if not key:
            raise ValueError("McpRegistry.discover(backend): backend 必须提供 name 属性")
        cached = self._cache.get(key)
        if cached is not None:
            return list(cached)
        raw_tools = await backend.list_tools()
        descriptors: list[ToolDescriptor] = []
        for tool in raw_tools or ():
            raw_name = getattr(tool, "name", None)
            if not raw_name or not self.allows(str(raw_name)):
                # fail-closed：白名单外工具连名字都不进 descriptors，agent 侧无法"撞库"。
                continue
            schema = getattr(tool, "input_schema", None)
            descriptors.append(
                ToolDescriptor(
                    name=str(raw_name),
                    description=getattr(tool, "description", None),
                    input_schema=copy.deepcopy(dict(schema)) if schema else {},
                    allowed_roles=self.roles_for(str(raw_name)),
                )
            )
        self._cache[key] = tuple(descriptors)
        return list(descriptors)

    def invalidate(self) -> None:
        """清空全部进程内发现缓存（唯一失效手段，无后台 TTL）。"""
        self._cache.clear()

    def schema_from_cache(self, name: str) -> dict[str, Any]:
        """从发现缓存里找该工具的 inputSchema；没发现过返回 {}。"""
        for descriptors in self._cache.values():
            for descriptor in descriptors:
                if descriptor.name == name:
                    return descriptor.input_schema
        return {}

    # ---------------------------------------------------------------- 校验

    def validate(
        self, name: str, args: Mapping[str, Any] | None
    ) -> tuple[dict[str, Any], str | None]:
        """白名单 + 最小 JSON-Schema 校验。

        返回 `(canonical_args, error)`：合法时 canonical 已按 properties.default 补缺省；
        出错时 canonical={}，error 是机器可读 token（见模块 docstring），原样交给上层决定
        怎么处置（client 层负责翻译成四分类 ToolResult）。

        schema 优先级：发现缓存（调用期最新）→ 构造期 schemas 参数 → 都没有则跳过校验放行
        （服务端未声明 inputSchema 的工具不瞎校验）。
        """
        if not self.allows(name):
            return {}, f"not_whitelisted:{name}"
        incoming = dict(args or {})
        schema = self.schema_from_cache(name) or self._registered_schemas.get(name) or {}
        if not schema:
            return incoming, None
        canonical, error = _validate_value(schema, incoming, path="")
        if error is not None:
            return {}, error
        if not isinstance(canonical, dict):
            return {}, "type_mismatch:$root"
        return canonical, None


# ---------------------------------------------------------------------------
# 手写最小 JSON-Schema 校验器（不引第三方 jsonschema）
# ---------------------------------------------------------------------------


def _validate_value(schema: Mapping[str, Any], value: Any, *, path: str) -> tuple[Any, str | None]:
    """递归校验单值，返回 `(规范化后的值, 错误token或None)`。错误 token 形如 kind:path。"""
    declared = schema.get("type")
    if declared is not None and not _type_ok(declared, value):
        return None, f"type_mismatch:{path or '$root'}"
    if "enum" in schema and value not in schema["enum"]:
        return None, f"enum_mismatch:{path or '$root'}"

    declared_kind = declared if isinstance(declared, str) else None
    if declared_kind == "object":
        properties: Mapping[str, Any] = schema.get("properties") or {}
        additional = schema.get("additionalProperties", True)
        for key in schema.get("required") or ():
            if key not in value:
                return None, f"missing_required:{_join(path, str(key))}"
        if additional is False:
            for key in value:
                if key not in properties:
                    return None, f"unexpected_property:{_join(path, str(key))}"
        canonical: dict[str, Any] = dict(value)
        for key, subschema in properties.items():
            if key not in canonical and isinstance(subschema, Mapping) and "default" in subschema:
                canonical[key] = copy.deepcopy(subschema["default"])
            if key in canonical and isinstance(subschema, Mapping):
                nested, err = _validate_value(subschema, canonical[key], path=_join(path, str(key)))
                if err is not None:
                    return None, err
                canonical[key] = nested
        return canonical, None

    if declared_kind == "array":
        items = schema.get("items")
        if isinstance(items, Mapping) and isinstance(value, list):
            canonical_items: list[Any] = []
            for index, item in enumerate(value):
                nested, err = _validate_value(items, item, path=f"{path or '$root'}[{index}]")
                if err is not None:
                    return None, err
                canonical_items.append(nested)
            return canonical_items, None
        return value, None

    return value, None


def _type_ok(declared: Any, value: Any) -> bool:
    """最小类型判定。bool 绝不顶 integer/number；int 不收 float（比草案更严，测试钉住）。"""
    if isinstance(declared, list):  # 联合类型任一命中即可
        return any(_type_ok(item, value) for item in declared)
    if not isinstance(declared, str):
        return True  # 未知类型声明形态 fail-open（校验器是"最小子集"而不冒充全集）
    if declared == "string":
        return isinstance(value, str)
    if declared == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if declared == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if declared == "boolean":
        return isinstance(value, bool)
    if declared == "object":
        return isinstance(value, dict)
    if declared == "array":
        return isinstance(value, list)
    if declared == "null":
        return value is None
    return True  # 自定义格式名等不认识的不拦


def _join(parent: str, child: str) -> str:
    """拼 schema 属性路径：根直接用属性名，嵌套用点号（city → location.city）。"""
    return f"{parent}.{child}" if parent else child
