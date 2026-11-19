"""B3-07 回归：审计里的「谁」必须来自服务端身份，而不是长者本人。

背景（攻防计划 §4.6 / R-9）：`tool.denied` 与 `injection_blocked` 一直有写，但
`RuntimeContext` 在装配处被写死 `None`，于是 `actor_id` 只能回退成 `resource_id`
（长者），事后查不到「哪个设备/员工想对这位长者做什么」。这里锁住三件事：

1. `_audit_ids()` 优先取 `actor_*`，工牌按员工记账，缺身份时退回 `system` + 全零；
2. `_audit_runtime_context()` 从请求级 `Identity` 取主体、从 `DialogueIdentity` 取长者；
3. MCP 成功侧审计也带 `trace_id`，能与 app 侧日志对上。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from app.agents.companion import RuntimeContext, _audit_ids
from app.agents.runtime_factory import _audit_runtime_context
from app.core.context import DialogueIdentity
from app.core.runtime_context import Identity, identity_scope
from app.services.audit_service import NIL_RESOURCE_ID

ELDER = uuid.UUID("11111111-1111-4111-8111-111111111111")
ACTOR = uuid.UUID("22222222-2222-4222-8222-222222222222")


def _context(**overrides: Any) -> RuntimeContext:
    base: dict[str, Any] = {"elder_id": str(ELDER)}
    base.update(overrides)
    return RuntimeContext(**base)


def test_audit_ids_prefers_actor_identity() -> None:
    actor_type, actor_id, resource_id = _audit_ids(
        _context(actor_type="device", actor_id=str(ACTOR))
    )
    assert (actor_type, actor_id, resource_id) == ("device", ACTOR, ELDER)


def test_audit_ids_maps_staff_card_to_staff() -> None:
    actor_type, actor_id, _ = _audit_ids(_context(actor_type="staff_card", actor_id=str(ACTOR)))
    assert actor_type == "staff"
    assert actor_id == ACTOR


def test_audit_ids_falls_back_without_actor() -> None:
    actor_type, actor_id, resource_id = _audit_ids(_context())
    # 没有主体时退回旧口径（资源即主体），但要落在 system，不能猜身份。
    assert (actor_type, actor_id, resource_id) == ("system", ELDER, ELDER)


def test_audit_ids_falls_back_to_nil_without_elder() -> None:
    actor_type, actor_id, resource_id = _audit_ids(RuntimeContext())
    assert (actor_type, actor_id, resource_id) == ("system", NIL_RESOURCE_ID, NIL_RESOURCE_ID)


def test_audit_ids_ignores_non_uuid_values() -> None:
    actor_type, actor_id, resource_id = _audit_ids(
        _context(actor_type="device", actor_id="not-a-uuid", elder_id="also-not-a-uuid")
    )
    assert (actor_type, actor_id, resource_id) == ("system", NIL_RESOURCE_ID, NIL_RESOURCE_ID)


def test_runtime_context_carries_request_identity() -> None:
    request_identity = Identity(kind="device", device_id=str(ACTOR), elder_id=str(ELDER))
    dialogue_identity = DialogueIdentity(source="device", elder_id=ELDER, display_name=None)
    with identity_scope(request_identity):
        context = _audit_runtime_context(dialogue_identity)

    assert context is not None
    assert context.elder_id == str(ELDER)
    assert context.actor_type == "device"
    assert context.actor_id == str(ACTOR)


def test_runtime_context_falls_back_to_dialogue_identity() -> None:
    context = _audit_runtime_context(
        DialogueIdentity(source="device", elder_id=ELDER, display_name=None)
    )
    # 请求级身份缺失（后台任务/直调）时主体记 system，但长者仍按本轮身份归行。
    assert context is not None
    assert context.actor_type == "system"
    assert context.actor_id is None
    assert context.elder_id == str(ELDER)


def test_runtime_context_is_none_without_identity() -> None:
    assert _audit_runtime_context(None) is None


@pytest.mark.asyncio
async def test_mcp_work_order_audit_carries_trace_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """MCP 成功侧审计必须能对回 app 的 trace（B3-07 的追溯口径）。"""
    import importlib

    ops = importlib.import_module("mcp-servers.mock_operations.ops")

    captured: dict[str, Any] = {}

    class _Order:
        order_id = uuid.uuid4()
        order_no = "WO-TEST-0001"
        status = "new"
        type = "missed_medication"
        severity = "yellow"
        elder_id = ELDER

    class _Session:
        async def commit(self) -> None:
            return None

    @asynccontextmanager
    async def _factory() -> AsyncIterator[_Session]:
        yield _Session()

    async def _no_ensure(session: Any, elder_id: uuid.UUID) -> None:
        return None

    async def _fake_create_work_order(session: Any, **kwargs: Any) -> _Order:
        return _Order()

    async def _capture_write_audit(session: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(ops, "_ensure_elder", _no_ensure)
    monkeypatch.setattr(ops.work_order_service, "create_work_order", _fake_create_work_order)
    monkeypatch.setattr(ops, "write_audit", _capture_write_audit)

    result = await ops.create_work_order(
        str(ELDER), "missed_medication", "yellow", factory=_factory, trace_id="trace-abc"
    )

    assert result["order_no"] == "WO-TEST-0001"
    assert captured["trace_id"] == "trace-abc"
    assert captured["actor_type"] == "system"
