"""N21 A/B：`elder_id` 由 runtime_context 注入（AGENTS §4.9）+ 服务端兜底拒绝。

两条口径分开钉：

1. **客户端「丢弃 + 覆盖」**：模型在 `create_work_order` 入参里塞别人的 `elder_id`，
   客户端在派发处删掉并用本次 runtime_context 的身份覆盖 —— 断言落在**库里的行**
   （工单记在发起请求的长者名下），而不是断言「注入函数被调用过」；日志只留
   「有没有传」，不留值（别人的 elder_id 本身是敏感标识）。
2. **服务端兜底**：MCP 没有传输层鉴权，服务端拿不到「调用方是谁」，所以这里只能守
   「elder_id 必须存在且合法」——缺失/非法回结构化 `param_error`，长者不存在回
   `not_found`，两种情况都**不落库**。覆盖责任在客户端，这条是绕过客户端直连时的防线。

除 §4.9 外，还覆盖「服务端不要 500、不回显内部异常栈」这条 MCP server 口径。
"""

from __future__ import annotations

import importlib
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from sqlalchemy import select
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
from app.core.runtime_context import Identity, reset_identity, set_identity
from app.mcp_client.client import McpClient
from app.mcp_client.registry import MOCK_OPERATIONS_TOOL_WHITELIST
from app.models import Elder, WorkOrder

OPS: Any = importlib.import_module("mcp-servers.mock_operations.ops")

ELDER = uuid.UUID("00000000-0000-4000-8000-00000000a067")
"""发起请求的长者（runtime_context 里的身份，测试事务回滚即消失）。"""

OTHER_ELDER = uuid.UUID("00000000-0000-4000-8000-00000000a068")
"""模型在入参里瞎传的「别人的」elder_id；库里没有这条长者。"""

TRACE_ID = "0123456789abcdef0123456789abcdef"


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
async def elder(db: AsyncConnection) -> uuid.UUID:
    await db.execute(
        Elder.__table__.insert().values(elder_id=ELDER, name="张桂芬", status="active")
    )
    return ELDER


def _client() -> McpClient:
    """只用来跑身份注入的 client：不建连（`_inject_identity_elder` 是纯函数）。"""
    from app.mcp_client.registry import McpRegistry

    return McpClient(
        name="mock-operations",
        url="http://127.0.0.1:9/mcp",
        registry=McpRegistry(MOCK_OPERATIONS_TOOL_WHITELIST),
    )


async def test_model_supplied_elder_id_is_discarded_and_row_lands_on_runtime_elder(
    maker: async_sessionmaker[AsyncSession],
    elder: uuid.UUID,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """模型传错人 → 工单仍记在 runtime_context 的长者名下；日志不回显那个 id。"""
    identity = Identity(kind="device", device_id=str(uuid.uuid4()), elder_id=str(ELDER))
    token = set_identity(identity)
    try:
        with caplog.at_level(logging.WARNING, logger="app.mcp_client.client"):
            wire_args = _client()._inject_identity_elder(
                "create_work_order",
                {"elder_id": str(OTHER_ELDER), "type": "sos", "severity": "red"},
                trace_id=TRACE_ID,
            )
    finally:
        reset_identity(token)

    # 覆盖发生在 schema 校验之前：入参里只剩 runtime 身份。
    assert wire_args["elder_id"] == str(ELDER)
    assert str(OTHER_ELDER) not in str(wire_args)

    record = next(r for r in caplog.records if r.getMessage() == "tool elder_id overridden")
    assert record.supplied is True
    assert record.injected is True
    # 「只记有没有传」：日志文本与 extra 里都不许出现具体 id。
    assert str(OTHER_ELDER) not in caplog.text
    assert str(ELDER) not in caplog.text
    # caplog.text 只渲染格式化后的 msg（extra 不进文本），所以再直接扫一遍 record 上的
    # 全部字段：将来有人把 elder_id 塞进 extra，这条会先红，而不是等日志落盘才发现。
    log_fields = repr(record.__dict__)
    assert str(OTHER_ELDER) not in log_fields
    assert str(ELDER) not in log_fields

    # 真的写库：用覆盖后的入参走服务端同一个入口，断言行落在哪个长者名下。
    payload = await OPS.create_work_order(
        elder_id=wire_args["elder_id"],
        order_type=wire_args["type"],
        severity=wire_args["severity"],
        factory=maker,
    )
    async with maker() as session:
        row = (
            await session.execute(
                select(WorkOrder).where(WorkOrder.order_no == payload["order_no"])
            )
        ).scalar_one()
        assert row.elder_id == ELDER
        assert row.elder_id != OTHER_ELDER
        assert (row.type, row.severity, row.status) == ("sos", "red", "new")


async def test_ops_rejects_missing_elder_id_without_writing(
    maker: async_sessionmaker[AsyncSession], elder: uuid.UUID
) -> None:
    """绕过客户端直连、不传 elder_id：结构化 param_error，且库里没有新增工单。"""
    note = f"missing-elder-{uuid.uuid4()}"
    with pytest.raises(OPS.OpsError) as excinfo:
        await OPS.create_work_order(
            elder_id=None, order_type="sos", severity="red", note=note, factory=maker
        )

    assert excinfo.value.error_code == "param_error"
    assert excinfo.value.detail == "elder_id_missing"
    assert await _rows_with_note(maker, note) == 0


async def test_ops_rejects_unknown_elder_without_writing(
    maker: async_sessionmaker[AsyncSession], elder: uuid.UUID
) -> None:
    """elder_id 合法但库里没有这个长者：not_found（不是 500、不是静默建单）。"""
    note = f"unknown-elder-{uuid.uuid4()}"
    with pytest.raises(OPS.OpsError) as excinfo:
        await OPS.create_work_order(
            elder_id=str(OTHER_ELDER), order_type="sos", severity="red", note=note, factory=maker
        )

    assert excinfo.value.error_code == "not_found"
    assert excinfo.value.detail == "elder_not_found"
    assert await _rows_with_note(maker, note) == 0


async def _rows_with_note(
    maker: async_sessionmaker[AsyncSession] | Callable[[], AsyncSession], note: str
) -> int:
    async with maker() as session:  # type: ignore[operator]
        rows = (await session.execute(select(WorkOrder).where(WorkOrder.note == note))).all()
    return len(rows)
