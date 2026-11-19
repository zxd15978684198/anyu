"""FR-TC-03 · MCP 工具越权拒绝与审计（task-22，mcp-servers/mock_operations）。

三层口径分开钉（每一层都能独立失效，故不合并成一条用例）：

1. **客户端第一道**：派发处把 `runtime_context` 的 elder_id 写进传输层 header
   `X-Anyu-Elder`，同时 body 里的值仍被覆盖（AGENTS §4.9）——「模型改不到 header」
   是这套机制成立的前提，所以 header 只在 streamable-http 上发，stdio/SSE 不走这条。
2. **服务端第二道**：用 header 复核 body。两者不一致 → `forbidden/elder_mismatch`，
   写一条 `tool.call` 审计（`detail.error_code` 落细分码、`requested_elder` 脱敏），
   **业务行一行不写**。直连服务端（不经客户端）时只有这一道，所以必须真的断库。
3. **缺 header 保持旧口径**：`param_error/elder_id_missing`，不伪装成越权。

审计断言一律**查库**（`audit_logs`）而不是 caplog：`audit_rejection` 走独立会话
（请求事务会被回滚，同事务写等于把证据一起抹掉），只有真库能证明它落下来了。
"""

from __future__ import annotations

import importlib
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from tests.conftest import require_db_connection

from app.core.config import get_settings
from app.db import session_factory
from app.models import AuditLog, Elder, WorkOrder

OPS: Any = importlib.import_module("mcp-servers.mock_operations.ops")
SERVER: Any = importlib.import_module("mcp-servers.mock_operations.server")

ELDER_A = uuid.UUID("00000000-0000-4000-8000-00000000b001")
"""请求真实归属的长者（header 里的身份）。"""
ELDER_B = uuid.UUID("00000000-0000-4000-8000-00000000b002")
"""被塞进 body 的「别人的」elder_id —— 模型编得出来、也真的存在于库里。"""


# ------------------------------------------------------------------ 夹具


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db(db_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    conn = await require_db_connection(db_engine)
    trans = await conn.begin()
    try:
        yield conn
    finally:
        await trans.rollback()
        await conn.close()


@pytest.fixture
def maker(db: AsyncConnection) -> async_sessionmaker[AsyncSession]:
    """ops 的 `factory` 入参：每次调用一个 session，在外层事务里用 savepoint。"""
    return async_sessionmaker(
        bind=db, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )


@pytest.fixture
async def elders(db: AsyncConnection) -> tuple[uuid.UUID, uuid.UUID]:
    for elder_id, name in ((ELDER_A, "张大娘"), (ELDER_B, "李大爷")):
        await db.execute(
            Elder.__table__.insert().values(elder_id=elder_id, name=name, status="active")
        )
    return ELDER_A, ELDER_B


@pytest.fixture
def trace_id() -> AsyncIterator[str]:
    """一次用例一个 trace：被拒审计走独立会话提交，只能靠它精确回收。"""
    value = f"tc03{uuid.uuid4().hex[:24]}"
    yield value


@pytest.fixture(autouse=True)
async def _isolated_app_engine() -> AsyncIterator[None]:
    """全局 `app.db` 引擎是跨模块共享的池，而 pytest-asyncio 每个用例一个新 loop。

    连接池里残留「绑在已关闭 loop」的连接时，下一次取连接会报
    `AttributeError: 'NoneType' object has no attribute 'send'` / `Event loop is closed`，
    并且被 `audit_rejection` 吞掉 → 审计静默丢失。用例前后各 pool.dispose() 一次即可。
    """
    from app.db import engine as app_engine

    await app_engine.dispose()
    yield
    await app_engine.dispose()


@pytest.fixture(autouse=True)
async def _purge_isolated_audit(trace_id: str) -> AsyncIterator[None]:
    """清掉本用例提交过的拒绝审计（隔离会话已 commit，外层事务回滚带不走）。"""
    yield
    async with session_factory() as session:
        rows = (await session.execute(select(AuditLog.audit_id, AuditLog.detail))).all()
        stale = [
            row.audit_id for row in rows if str(row.detail.get("trace_id", "")).startswith(trace_id)
        ]
        if stale:
            await session.execute(delete(AuditLog).where(AuditLog.audit_id.in_(stale)))
            await session.commit()


@pytest.fixture(autouse=True)
def _bind_maker(monkeypatch: pytest.MonkeyPatch, maker: async_sessionmaker[AsyncSession]) -> None:
    """把工具闭包里的落库调用绑到本用例的事务上（否则会提交到真库、回滚不掉）。"""

    async def create(
        elder_id: Any,
        order_type: str,
        severity: str,
        note: str | None = None,
        *,
        trace_id: str | None = None,
    ) -> Any:
        return await OPS.create_work_order(
            elder_id, order_type, severity, note, factory=maker, trace_id=trace_id
        )

    async def notify(elder_id: Any, kind: str, message: str, *, trace_id: str | None = None) -> Any:
        return await OPS.send_family_notification(
            elder_id, kind, message, factory=maker, trace_id=trace_id
        )

    monkeypatch.setattr(SERVER, "_create_work_order", create)
    monkeypatch.setattr(SERVER, "_send_family_notification", notify)


# ------------------------------------------------------------------ 辅助


def _ctx(headers: dict[str, str] | None) -> Any:
    """造一个带 header 的 `Context`（真传输下由 SDK 填，这里直接注入同一属性）。"""
    from mcp.server.mcpserver import Context

    ctx = Context()
    ctx._request_context = SimpleNamespace(request=SimpleNamespace(headers=headers))
    return ctx


async def _call(tool_name: str, *, headers: dict[str, str] | None, **args: Any) -> Any:
    """走**注册过的工具闭包**：这样 `ctx.headers -> _run_tool` 的接线也在被测范围里。"""
    tool = SERVER.build_server()._tool_manager.get_tool(tool_name)
    return await tool.fn(ctx=_ctx(headers), **args)


async def _audit_rows(trace_id: str) -> list[Any]:
    """独立会话查 `audit_logs`：断言的是真落库的行，不是日志。"""
    async with session_factory() as session:
        rows = (await session.execute(select(AuditLog.action, AuditLog.detail))).all()
    return [
        row for row in rows if row.action == "tool.call" and row.detail.get("trace_id") == trace_id
    ]


async def _order_count(maker: async_sessionmaker[AsyncSession], *elder_ids: uuid.UUID) -> int:
    async with maker() as session:
        rows = (
            await session.execute(
                select(WorkOrder.order_id).where(WorkOrder.elder_id.in_(elder_ids))
            )
        ).all()
    return len(rows)


# ------------------------------------------------------------------ 纯函数：身份对齐


def test_resolve_prefers_header_when_body_absent() -> None:
    assert OPS.resolve_elder_authority(None, str(ELDER_A)) == str(ELDER_A)


def test_resolve_accepts_matching_body_and_header() -> None:
    assert OPS.resolve_elder_authority(str(ELDER_A), str(ELDER_A)) == str(ELDER_A)


def test_resolve_rejects_mismatch_with_masked_request() -> None:
    with pytest.raises(OPS.OpsError) as excinfo:
        OPS.resolve_elder_authority(str(ELDER_B), str(ELDER_A))

    err = excinfo.value
    assert (err.error_code, err.detail) == ("forbidden", "elder_mismatch")
    # 信封对外只有四分类码，审计里才落细分码与脱敏证据。
    assert err.audit_error_code == "elder_mismatch"
    masked = err.audit_detail["requested_elder"]
    assert masked != str(ELDER_B)
    assert str(ELDER_B) not in masked
    assert masked.endswith(str(ELDER_B)[-4:])


def test_resolve_requires_header() -> None:
    with pytest.raises(OPS.OpsError) as excinfo:
        OPS.resolve_elder_authority(str(ELDER_B), None)

    assert (excinfo.value.error_code, excinfo.value.detail) == ("param_error", "elder_id_missing")


async def test_registered_tool_schema_has_no_context_param() -> None:
    """`ctx` 只用于注入，绝不能变成模型要填的参数（否则模型会去伪造 header）。"""
    tools = await SERVER.build_server().list_tools()
    schema = next(tool.input_schema for tool in tools if tool.name == "create_work_order")
    assert "ctx" not in schema["properties"]
    assert schema["required"] == ["type", "severity"]


# ------------------------------------------------------------------ 验收三条


async def test_body_elder_mismatch_is_rejected_with_audit_and_no_business_row(
    elders: tuple[uuid.UUID, uuid.UUID],
    maker: async_sessionmaker[AsyncSession],
    trace_id: str,
) -> None:
    """① body 说 B、header 说 A → 拒绝 + 一条越权审计 + 工单 0 行。"""
    result = await _call(
        "create_work_order",
        headers={OPS.ELDER_HEADER: str(ELDER_A), OPS.TRACE_HEADER: trace_id},
        type="follow_up",
        severity="yellow",
        elder_id=str(ELDER_B),
    )

    assert result.ok is False
    assert result.error_code == "forbidden"
    assert result.detail == "elder_mismatch"
    assert await _order_count(maker, *elders) == 0

    rows = await _audit_rows(trace_id)
    assert len(rows) == 1
    detail = rows[0].detail
    assert detail["error_code"] == "elder_mismatch"
    assert detail["tool"] == "create_work_order"
    assert detail["requested_elder"] == f"{str(ELDER_B)[:8]}…{str(ELDER_B)[-4:]}"


async def test_missing_header_keeps_legacy_param_error(
    elders: tuple[uuid.UUID, uuid.UUID],
    maker: async_sessionmaker[AsyncSession],
    trace_id: str,
) -> None:
    """② body 有值但 header 缺失 → 旧口径 param_error/elder_id_missing，不写成越权。"""
    result = await _call(
        "create_work_order",
        headers={OPS.TRACE_HEADER: trace_id},
        type="follow_up",
        severity="yellow",
        elder_id=str(ELDER_B),
    )

    assert result.ok is False
    assert result.error_code == "param_error"
    assert result.detail == "elder_id_missing"
    assert await _order_count(maker, *elders) == 0

    rows = await _audit_rows(trace_id)
    assert len(rows) == 1
    # 旧口径不动：信封码仍是 param_error，细分码在 detail.detail 里（历史审计行的形状）。
    assert rows[0].detail["error_code"] == "param_error"
    assert rows[0].detail["detail"] == "elder_id_missing"


async def test_header_only_call_writes_row_on_runtime_elder(
    elders: tuple[uuid.UUID, uuid.UUID],
    maker: async_sessionmaker[AsyncSession],
    trace_id: str,
) -> None:
    """③ 不传 body elder_id、只给 header → 正常建单，行落在 header 指的长者名下。"""
    result = await _call(
        "create_work_order",
        headers={OPS.ELDER_HEADER: str(ELDER_A), OPS.TRACE_HEADER: trace_id},
        type="follow_up",
        severity="yellow",
    )

    assert result.ok is True
    assert result.data is not None
    assert result.data["elder_id"] == str(ELDER_A)

    async with maker() as session:
        row = (
            await session.execute(
                select(WorkOrder.elder_id).where(WorkOrder.order_no == result.data["order_no"])
            )
        ).scalar_one()
        # 成功调用的 `tool.call` 审计与业务同事务，所以在同一 session 里就能看到。
        audits = (
            await session.execute(
                select(AuditLog.detail).where(AuditLog.resource_type == "work_order")
            )
        ).all()
    assert row == ELDER_A
    assert audits, "成功调用必须有 tool.call 审计"


async def test_matching_body_and_header_is_not_over_rejected(
    elders: tuple[uuid.UUID, uuid.UUID], maker: async_sessionmaker[AsyncSession], trace_id: str
) -> None:
    """同一身份写在两处（客户端当前行为）不应被误判成越权。"""
    result = await _call(
        "create_work_order",
        headers={OPS.ELDER_HEADER: str(ELDER_A), OPS.TRACE_HEADER: trace_id},
        type="follow_up",
        severity="yellow",
        elder_id=str(ELDER_A),
    )
    assert result.ok is True
    assert result.data is not None
    assert result.data["elder_id"] == str(ELDER_A)


async def test_family_notification_mismatch_is_rejected_too(
    elders: tuple[uuid.UUID, uuid.UUID], maker: async_sessionmaker[AsyncSession], trace_id: str
) -> None:
    """第二道防线两个工具都要过：通知工具漏一个就是一个越权口子。"""
    result = await _call(
        "send_family_notification",
        headers={OPS.ELDER_HEADER: str(ELDER_A), OPS.TRACE_HEADER: trace_id},
        kind="family_report",
        message="今天状态不错",
        elder_id=str(ELDER_B),
    )
    assert result.ok is False
    assert result.error_code == "forbidden"
    assert result.detail == "elder_mismatch"
    rows = await _audit_rows(trace_id)
    assert len(rows) == 1
    assert rows[0].detail["tool"] == "send_family_notification"
