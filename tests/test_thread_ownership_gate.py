"""对话入口的 thread 归属闸（攻防计划 §4.6-S2 / B3-01「拿他人 thread_id」）。

钉住的契约：

1. `thread_id` 由服务端派生之后、交给 `run_text_turn` 读写会话之前，**必须**过
   `app.api.dialogue.ensure_thread_access`——这是「已有 thread_id、即将以它的名义
   写库」的唯一收口点。
2. 判据只有一处：形态 / 存量兼容 / DB 复核全部走
   `identity_service.validate_thread_access`，本模块不养第二套判定。
3. 拒绝 = 403（`main.py` 兜底成 `ROLE_FORBIDDEN`）+ 固定话术 + 库里一条
   `audit_logs(action='thread_access_denied')`；四种 reason 各钉一例。
4. 放行路径的查询预算：`device:<本设备>`（正常打字链路）**零 DB 查询**；
   DB 里真有会话（话音链路 32 位 hex）**至多一次**归属查询。
5. 正常打字请求不回归：200，且回复真的来自编排层。

为什么用假 session / 假审计会话而不是真库：本文件只关心「闸在不在、判据走没走、
审计写没写」。`validate_thread_access` 对 DB 只有 `execute(select(...)).first()`
这一面；审计走 `app.db.session_factory` 的独立会话（拒绝后调用方事务会回滚，
所以它特意另开一条）——这里同样替掉它，直接拿 `AuditLog` 对象断言字段。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

import app.db as app_db
from app.api import dialogue as dialogue_api
from app.core.config import get_settings
from app.core.runtime_context import Identity
from app.models.audit import AuditLog
from app.router.decision import RouteDecision
from app.services import text_turn_service
from app.services.dialogue_service import DialogueService
from app.services.llm_service import LLMReply

DEVICE_ID = uuid.uuid4()
OTHER_DEVICE_ID = uuid.uuid4()
ELDER_ID = uuid.uuid4()
OTHER_ELDER_ID = uuid.uuid4()

LLM_TEXT = "我在听，您慢慢说。"


# ---------------------------------------------------------------------------
# 替身：归属闸只用到 session.execute(select(...)).first()
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self._row = row

    def first(self) -> tuple[Any, ...] | None:
        return self._row


class _FakeSession:
    """记录被查询的次数——「放行路径 ≤1 次额外 DB 查询」靠它证明，不靠读代码。"""

    def __init__(self, row: tuple[Any, ...] | None = None) -> None:
        self.row = row
        self.executed: list[Any] = []

    async def execute(self, statement: Any) -> _FakeResult:
        self.executed.append(statement)
        return _FakeResult(self.row)


class _FakeAuditSession:
    """`_audit_thread_denial` 的独立会话替身：把 AuditLog 收进 sink，不落库。"""

    def __init__(self, sink: list[AuditLog]) -> None:
        self._sink = sink
        self.commits = 0

    async def __aenter__(self) -> _FakeAuditSession:
        return self

    async def __aexit__(self, *exc_info: Any) -> bool:
        return False

    def add(self, row: AuditLog) -> None:
        self._sink.append(row)

    async def commit(self) -> None:
        self.commits += 1


@pytest.fixture
def audit_sink(monkeypatch: pytest.MonkeyPatch) -> list[AuditLog]:
    sink: list[AuditLog] = []
    monkeypatch.setattr(app_db, "session_factory", lambda: _FakeAuditSession(sink))
    return sink


# ---------------------------------------------------------------------------
# 判据：放行 / 拒绝（四种 reason）+ 审计
# ---------------------------------------------------------------------------


async def test_device_thread_of_this_device_passes_without_db_query(
    audit_sink: list[AuditLog],
) -> None:
    """正常打字链路 `device:<本设备>`：放行且**零** DB 查询（存量兼容分支）。"""
    session = _FakeSession()

    await dialogue_api.ensure_thread_access(
        session,
        thread_id=f"device:{DEVICE_ID}",
        device_id=str(DEVICE_ID),
        elder_id=str(ELDER_ID),
    )

    assert session.executed == [], "device: 形态不该查库——每条打字请求多一次查询"
    assert audit_sink == []


async def test_db_thread_of_same_elder_is_allowed_with_one_query(
    audit_sink: list[AuditLog],
) -> None:
    """DB 里确有该会话、长者一致 → 放行；查询恰好一次（不多查一次会话再来校验）。"""
    session = _FakeSession(row=(ELDER_ID,))

    await dialogue_api.ensure_thread_access(
        session,
        thread_id=uuid.uuid4().hex,
        device_id=str(DEVICE_ID),
        elder_id=str(ELDER_ID),
    )

    assert len(session.executed) == 1
    assert audit_sink == []


async def test_thread_of_another_elder_is_denied_and_audited(
    audit_sink: list[AuditLog],
) -> None:
    """跨长者：403 + 固定话术 + 审计 reason=cross_elder（B3-01 主攻击面）。"""
    session = _FakeSession(row=(OTHER_ELDER_ID,))

    with pytest.raises(HTTPException) as excinfo:
        await dialogue_api.ensure_thread_access(
            session,
            thread_id=uuid.uuid4().hex,
            device_id=str(DEVICE_ID),
            elder_id=str(ELDER_ID),
        )

    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == dialogue_api._THREAD_ACCESS_DENIED_DETAIL
    assert len(audit_sink) == 1
    row = audit_sink[0]
    assert row.action == "thread_access_denied"
    assert row.actor_type == "device"
    assert row.actor_id == DEVICE_ID
    assert row.resource_type == "conversation_threads"
    assert row.detail is not None
    assert row.detail["reason"] == "cross_elder"


async def test_unknown_thread_is_denied_and_audited(audit_sink: list[AuditLog]) -> None:
    """库里没有这号会话：403 + reason=unknown_thread（伪造 32 位 hex 的落点）。"""
    session = _FakeSession(row=None)

    with pytest.raises(HTTPException) as excinfo:
        await dialogue_api.ensure_thread_access(
            session,
            thread_id=uuid.uuid4().hex,
            device_id=str(DEVICE_ID),
            elder_id=str(ELDER_ID),
        )

    assert excinfo.value.status_code == 403
    assert len(session.executed) == 1
    assert len(audit_sink) == 1
    assert audit_sink[0].detail is not None
    assert audit_sink[0].detail["reason"] == "unknown_thread"


async def test_malformed_thread_is_denied_without_db_query(
    audit_sink: list[AuditLog],
) -> None:
    """带连字符的 UUID / 任意串：形态阶段就拒，连库都不查（宁可 403，也不猜）。"""
    session = _FakeSession(row=(ELDER_ID,))

    with pytest.raises(HTTPException) as excinfo:
        await dialogue_api.ensure_thread_access(
            session,
            thread_id=str(uuid.uuid4()),
            device_id=str(DEVICE_ID),
            elder_id=str(ELDER_ID),
        )

    assert excinfo.value.status_code == 403
    assert session.executed == []
    assert len(audit_sink) == 1
    assert audit_sink[0].detail is not None
    assert audit_sink[0].detail["reason"] == "malformed_thread_id"


async def test_other_devices_device_thread_is_denied_and_audited(
    audit_sink: list[AuditLog],
) -> None:
    """`device:<别人的设备>` 也是他家的 thread：403 + reason=device_thread_owner_mismatch。"""
    session = _FakeSession()

    with pytest.raises(HTTPException) as excinfo:
        await dialogue_api.ensure_thread_access(
            session,
            thread_id=f"device:{OTHER_DEVICE_ID}",
            device_id=str(DEVICE_ID),
            elder_id=str(ELDER_ID),
        )

    assert excinfo.value.status_code == 403
    assert session.executed == []
    assert len(audit_sink) == 1
    assert audit_sink[0].detail is not None
    assert audit_sink[0].detail["reason"] == "device_thread_owner_mismatch"


async def test_missing_device_identity_is_denied_fail_closed(
    audit_sink: list[AuditLog],
) -> None:
    """identity 里没有设备号（异常态）：当作拒绝，不放行也无法写「谁的」会话。"""
    session = _FakeSession(row=(ELDER_ID,))

    with pytest.raises(HTTPException) as excinfo:
        await dialogue_api.ensure_thread_access(
            session,
            thread_id=f"device:{DEVICE_ID}",
            device_id=None,
            elder_id=str(ELDER_ID),
        )

    assert excinfo.value.status_code == 403
    assert session.executed == []


# ---------------------------------------------------------------------------
# 接线：正常打字请求穿过这道闸，行为不回归
# ---------------------------------------------------------------------------


class _FakeLLM:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        trace_id: str | None = None,
    ) -> LLMReply:
        self.calls.append({"messages": messages, "trace_id": trace_id})
        return LLMReply(text=LLM_TEXT, degraded=False)


async def _l1_route(text: str, **kwargs: Any) -> RouteDecision:
    return RouteDecision(
        elder_id=str(kwargs.get("elder_id") or ""),
        thread_id=str(kwargs.get("thread_id") or "device:-"),
        layer="L1",
        intent="chat",
        confidence=0.92,
        trace_id=kwargs.get("trace_id"),
    )


async def _noop_log_decision(decision: Any) -> None:
    """决策流水是旁路；本用例不连库，替成 no-op（它自己也是永不抛的）。"""


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    from app.main import create_app

    application = create_app(get_settings())
    fake_llm = _FakeLLM()

    async def _fake_session() -> AsyncIterator[_FakeSession]:
        yield _FakeSession()

    application.dependency_overrides[dialogue_api.current_device] = lambda: Identity(
        kind="device", device_id=str(DEVICE_ID), elder_id=str(ELDER_ID)
    )
    application.dependency_overrides[dialogue_api.get_session] = _fake_session
    application.dependency_overrides[dialogue_api.get_dialogue_service] = lambda: DialogueService(
        llm=fake_llm
    )
    application.dependency_overrides[dialogue_api.get_route_text] = lambda: _l1_route
    application.dependency_overrides[dialogue_api.get_conversation_recorder] = lambda: None
    application.dependency_overrides[dialogue_api.get_memory_hook] = lambda: None
    monkeypatch.setattr(text_turn_service, "log_decision", _noop_log_decision)

    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


async def test_typed_turn_still_replies_through_the_gate(client: AsyncClient) -> None:
    """正常一轮：200、回复来自编排层（不是校验失败被吞成 200）。"""
    response = await client.post("/api/dialogue/text", json={"text": "今天有点闷"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reply"]["text"] == LLM_TEXT
    assert body["degraded"] is False
