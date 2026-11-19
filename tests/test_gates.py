# owner=W2 · 迭代 9 W2 三道安全闸门 · 依据 PRD FR-MEM-02 / FR-DLG-10 / AGENTS 红线 9
"""迭代 9 W2：三道安全闸门的可红用例（身份注入 / 记忆跨长者隔离 / 工具越权）。

闸门① 身份注入
- REST：`elder_id` 从 query/body 传入时一律以服务端 `Identity` 为准，越界即 403。
- WS：`/ws/signal` 建连 query 里的 `elder_id` / `device_id` 不得影响服务端绑定，
  且端点结构上不读 query（写成静态断言，谁加读取谁红）。
- 模型：`run_turn` 的模型输出里塞 `elder_id` / `thread_id` → 必须进
  `CompanionTurnResult.falsified_identity`，且 `route_decision` 仍是服务端身份。

闸门② 记忆跨长者隔离
- A 写入的记忆永不出现在 B 的召回里；同一句内容由两位长者分别写入不得互相 supersede。
- 管理端导出（`GET /api/memory/chunks`）只返回目标长者的切片。
- 静态扫描：`app/services/memory_service.py` 里每条 `MemoryChunk` 查询都必须带
  `MemoryChunk.elder_id` 条件；扫描器另有「删掉条件就报错」的自检用例。

闸门③ 工具越权
- 白名单外的工具 → `forbidden` 且**不触达后端**；非法参数 → `param_error` 且不触达后端。
- `MAX_TOOL_CALLS_PER_TURN=8` 与「同一 Tool 1 分钟内第 3 次」熔断按真实配置生效。

每个用例都能「删掉防护就红」，判据写在各自的 docstring 里。
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import logging
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from tests.conftest import require_db_connection

from app.agents.companion import RuntimeContext, run_turn
from app.agents.guardrails import Guardrail
from app.agents.tool_registry import (
    TOOL_FAILURE_SPEECH,
    WHITELIST,
    ToolRegistry,
    ToolResult,
)
from app.api import elders as elders_api
from app.api import medication as medication_api
from app.api import memory as memory_api
from app.audio import media_bridge, signaling
from app.core.auth import get_current_principal
from app.core.config import Settings, get_settings
from app.core.degrade_speech import spoken_speech
from app.core.runtime_context import Identity
from app.core.security import sha256_hex, sign_device_session
from app.core.trace import TraceIdMiddleware
from app.db import get_session
from app.models import AuditLog, Elder, MedicationPlan
from app.models.organization import Bed, Device, Room
from app.services import memory_service, report_service
from app.services.embedding import DeterministicEmbeddingProvider
from app.services.llm_service import LLMService

pytestmark = pytest.mark.gate

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MEMORY_SERVICE_PATH = _REPO_ROOT / "app" / "services" / "memory_service.py"

#: 客户端/模型伪造的长者 id（必须被服务端身份完全覆盖）。
_FORGED_ELDER_ID = "11111111-1111-4111-8111-111111111111"

#: 假 LLM 的占位密钥（不是真实密钥，仅用于 httpx.MockTransport 构造适配器）。
_FAKE_LLM_KEY = "test-key-not-a-real-secret"
_FAKE_LLM_URL = "https://llm.invalid/v1"


# ---------------------------------------------------------------------------
# 数据库夹具：真库 + 外层事务回滚（沿用 tests/test_memory_api.py 的隔离范式）
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    """独立 NullPool 引擎：不跨事件循环复用连接，避免 `Event loop is closed`。"""
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """所有写入落在同一外层事务里，用例结束整体回滚，主库零残留。"""
    conn = await require_db_connection(db_engine)
    trans = await conn.begin()
    session = AsyncSession(bind=conn, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.rollback()
        await trans.rollback()
        await session.close()
        await conn.close()


@pytest.fixture
def audit_sink(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> async_sessionmaker[AsyncSession]:
    """把越权审计的「自己开会话」钉在同一条测试连接上，403 后仍能回查 audit_logs。

    `report_service._write_audit` 用模块级 `session_factory` 独立提交；不替换的话
    审计会真的落到主库（污染）。`create_savepoint` 保证 commit 只释放保存点。
    """
    bind = db.bind
    assert bind is not None
    factory = async_sessionmaker(
        bind=bind,  # type: ignore[arg-type]
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    monkeypatch.setattr(report_service, "session_factory", factory)
    return factory


# ---------------------------------------------------------------------------
# 造数与断言小工具
# ---------------------------------------------------------------------------


async def _seed_elder(session: AsyncSession, name: str) -> Elder:
    elder = Elder(name=name, status="active")
    session.add(elder)
    await session.flush()
    return elder


async def _seed_bound_elder(
    session: AsyncSession,
    *,
    room_no: str,
    bed_no: str,
    name: str,
    secret: str,
) -> dict[str, Any]:
    """一间房 + 一张床 + 一台绑定该长者的设备（WS 身份用例的最小真实数据）。"""
    room = Room(room_no=room_no)
    session.add(room)
    await session.flush()
    bed = Bed(room_id=room.room_id, bed_no=bed_no)
    device = Device(room_id=room.room_id, token_hash=sha256_hex(secret), status="offline")
    session.add_all([bed, device])
    await session.flush()
    elder = Elder(name=name, status="active")
    session.add(elder)
    await session.flush()
    bed.elder_id = elder.elder_id
    device.elder_id = elder.elder_id
    await session.flush()
    return {
        "elder_id": elder.elder_id,
        "room_id": room.room_id,
        "device_id": device.device_id,
    }


async def _audits(session: AsyncSession, *, action: str, actor_id: uuid.UUID) -> list[AuditLog]:
    """按 action + actor 精确取审计行（库里可能有历史行，必须带 actor 过滤）。"""
    result = await session.execute(
        select(AuditLog).where(
            AuditLog.action == action,
            AuditLog.actor_id == actor_id,
        )
    )
    return list(result.scalars().all())


def _plan_body(elder_id: uuid.UUID) -> dict[str, Any]:
    """下午用药计划的最小合法请求体。"""
    return {
        "elder_id": str(elder_id),
        "drug_name": "氨氯地平",
        "dose": "1 片",
        "schedule_cron": "08:00",
        "start_date": "2026-11-01",
        "end_date": "2026-12-01",
    }


@asynccontextmanager
async def _api_client(db: AsyncSession, identity: Identity | None) -> AsyncIterator[AsyncClient]:
    """按需装配 REST 应用：三道闸门相关路由 + trace 中间件 + 会话/身份覆盖。

    `identity=None` 时不覆盖 `get_current_principal`，走真实鉴权链（无 token 用例）。
    """
    application = FastAPI()
    application.add_middleware(TraceIdMiddleware)
    for router in (memory_api.router, medication_api.router, elders_api.router):
        application.include_router(router)

    async def _session_override() -> AsyncIterator[AsyncSession]:
        session = AsyncSession(
            bind=db.bind,  # type: ignore[arg-type]
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    application.dependency_overrides[get_session] = _session_override
    if identity is not None:
        application.dependency_overrides[get_current_principal] = lambda: identity
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


# ===========================================================================
# 闸门① 身份注入 · REST
# ===========================================================================


async def test_gate1_guardian_cannot_read_another_elder_memory(
    db: AsyncSession, audit_sink: async_sessionmaker[AsyncSession]
) -> None:
    """家属拿 B 的 elder_id 读 B 的记忆 → 403 SCOPE_FORBIDDEN + 落库审计。

    删掉 `app/api/memory.py::_require_admin` 的 `kind != "staff"` 分支就会 200 并泄漏 B 的记忆。
    """
    elder_a = await _seed_elder(db, "张桂芬")
    elder_b = await _seed_elder(db, "李福贵")
    provider = DeterministicEmbeddingProvider()
    await memory_service.remember(
        db, elder_id=elder_b.elder_id, content="李福贵的高血压用药记录", embedding_provider=provider
    )
    guardian_id = uuid.uuid4()
    identity = Identity(kind="guardian", guardian_id=str(guardian_id))

    async with _api_client(db, identity) as client:
        resp = await client.get("/api/memory/chunks", params={"elder_id": str(elder_b.elder_id)})

    assert resp.status_code == 403
    body = resp.json()
    assert body["code"] == "SCOPE_FORBIDDEN"
    assert body["trace_id"]
    assert "items" not in body, "403 响应体不得夹带记忆内容"

    audits = await _audits(db, action="memory.read", actor_id=guardian_id)
    assert len(audits) == 1
    assert audits[0].actor_type == "guardian"
    assert audits[0].resource_type == "memory_chunks"
    assert str(audits[0].resource_id) == str(elder_b.elder_id)
    assert audits[0].detail is not None
    assert audits[0].detail["reason"] == "non_admin"
    # A 的记忆在这一轮里根本不该被碰过（只是造数存在）
    assert elder_a.elder_id != elder_b.elder_id


async def test_gate1_care_worker_cannot_write_medication_plan(
    db: AsyncSession, audit_sink: async_sessionmaker[AsyncSession]
) -> None:
    """护工（只读角色）POST 用药计划，body 里指定别人的长者 → 403 ROLE_FORBIDDEN，药单不落库。

    删掉 `app/api/medication.py::_require_roles` 的 `role not in allowed` 分支即可写入成功。
    """
    elder = await _seed_elder(db, "李福贵")
    staff_id = uuid.uuid4()
    identity = Identity(kind="staff", staff_id=str(staff_id), role="care_worker")

    async with _api_client(db, identity) as client:
        resp = await client.post("/api/medication/plans", json=_plan_body(elder.elder_id))

    assert resp.status_code == 403
    body = resp.json()
    assert body["code"] == "ROLE_FORBIDDEN"
    assert body["trace_id"]

    audits = await _audits(db, action="medication.plan.create", actor_id=staff_id)
    assert len(audits) == 1
    assert audits[0].actor_type == "staff"
    assert audits[0].resource_type == "medication_plan"
    assert str(audits[0].resource_id) == str(elder.elder_id)
    assert audits[0].detail is not None
    assert audits[0].detail["reason"] == "role"

    plans = (
        (await db.execute(select(MedicationPlan).where(MedicationPlan.elder_id == elder.elder_id)))
        .scalars()
        .all()
    )
    assert list(plans) == [], "越权请求不得留下药单"


async def test_gate1_doctor_cannot_write_plan_outside_assignment(db: AsyncSession) -> None:
    """医生对未分配给自己的长者写药单 → 403 SCOPE_FORBIDDEN（elder_id 来自 body 也不行）。

    删掉 `elder_service._scope_relation` + 分配校验（或改成信任 body.elder_id）即可写入。
    """
    foreign_elder = await _seed_elder(db, "王秀兰")
    doctor_id = uuid.uuid4()
    identity = Identity(kind="staff", staff_id=str(doctor_id), role="doctor")

    async with _api_client(db, identity) as client:
        resp = await client.post("/api/medication/plans", json=_plan_body(foreign_elder.elder_id))

    assert resp.status_code == 403
    assert resp.json()["code"] == "SCOPE_FORBIDDEN"
    plans = (
        (
            await db.execute(
                select(MedicationPlan).where(MedicationPlan.elder_id == foreign_elder.elder_id)
            )
        )
        .scalars()
        .all()
    )
    assert list(plans) == []


async def test_gate1_family_cannot_reach_console_elder_list(db: AsyncSession) -> None:
    """家属访问照护台端点（长者列表）→ 403 SCOPE_FORBIDDEN，响应不含任何长者数据。

    删掉 `elder_service._scope_relation` 的 `kind != KIND_STAFF` 分支即返回列表。
    """
    guardian = Identity(kind="guardian", guardian_id=str(uuid.uuid4()))
    async with _api_client(db, guardian) as client:
        resp = await client.get("/api/elders", params={"elder_id": _FORGED_ELDER_ID})

    assert resp.status_code == 403
    body = resp.json()
    assert body["code"] == "SCOPE_FORBIDDEN"
    assert "items" not in body
    assert _FORGED_ELDER_ID not in json.dumps(body, ensure_ascii=False)


async def test_gate1_missing_or_forged_token_is_401(db: AsyncSession) -> None:
    """无 token / 伪造 token 访问受保护端点 → 401 TOKEN_INVALID，query 里的 elder_id 没有意义。

    去掉 `get_current_principal` 依赖（或让它无 token 时放行）即 200。
    """
    forged_elder = str(uuid.uuid4())
    async with _api_client(db, None) as client:
        no_token = await client.get("/api/memory/chunks", params={"elder_id": forged_elder})
        forged_token = await client.get(
            "/api/memory/chunks",
            params={"elder_id": forged_elder},
            headers={"Authorization": "Bearer not-a-real-token"},
        )

    for resp in (no_token, forged_token):
        assert resp.status_code == 401
        assert resp.json()["code"] == "TOKEN_INVALID"
    assert no_token.json()["trace_id"]


# ===========================================================================
# 闸门① 身份注入 · WebSocket
# ===========================================================================


class WsClient:
    """在 pytest 事件循环里直驱 ASGI app 的最小 WS 客户端（仿 uvicorn 握手语义）。

    与 `tests/test_audio_ws_auth.py` 同款，但支持自定义 `query_string`——本文件要证明
    伪造 `elder_id` / `device_id` query 参数对服务端身份没有任何影响。
    """

    def __init__(self, app: FastAPI, *, query: Mapping[str, str] | None = None) -> None:
        self._app = app
        self._query = dict(query or {})
        self._to_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._from_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    async def _receive(self) -> dict[str, Any]:
        return await self._to_app.get()

    async def _send(self, message: Mapping[str, Any]) -> None:
        msg = dict(message)
        await self._from_app.put(msg)
        if msg.get("type") == "websocket.close":
            await self._to_app.put({"type": "websocket.disconnect", "code": 1000})

    async def __aenter__(self) -> WsClient:
        query_string = urlencode(self._query).encode() if self._query else b""
        scope: dict[str, Any] = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "scheme": "ws",
            "path": "/ws/signal",
            "raw_path": b"/ws/signal",
            "root_path": "",
            "query_string": query_string,
            "headers": [],
            "client": ("testclient", 0),
            "server": ("testserver", 80),
            "subprotocols": [],
            "state": {},
        }
        self._task = asyncio.create_task(self._app(scope, self._receive, self._send))
        await self._to_app.put({"type": "websocket.connect"})
        accepted = await self._next_server_message()
        assert accepted["type"] == "websocket.accept"
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
        await self.wait_closed()

    async def _next_server_message(self) -> dict[str, Any]:
        """取下一条 server 帧；端点先结束时抛端点异常，避免死等。"""
        assert self._task is not None
        getter = asyncio.ensure_future(self._from_app.get())
        done, _ = await asyncio.wait({getter, self._task}, return_when=asyncio.FIRST_COMPLETED)
        if getter in done:
            return getter.result()
        getter.cancel()
        exc = self._task.exception()
        if exc is not None:
            raise exc
        raise RuntimeError("WS endpoint finished before the expected frame arrived")

    async def send_json(self, message: Mapping[str, Any]) -> None:
        await self._to_app.put(
            {"type": "websocket.receive", "text": json.dumps(message, ensure_ascii=False)}
        )

    async def receive_json(self) -> dict[str, Any]:
        msg = await self._next_server_message()
        assert msg["type"] == "websocket.send", f"期望数据帧，收到 {msg}"
        return json.loads(msg["text"])

    async def close(self) -> None:
        await self._to_app.put({"type": "websocket.disconnect", "code": 1000})

    async def wait_closed(self) -> None:
        assert self._task is not None
        await self._task


def _device_token(device_id: Any, secret_plain: str) -> str:
    """按 E4 口径自签设备会话（载荷 tv = token_hash 前 16 位）。"""
    return sign_device_session(
        device_id=str(device_id),
        token_hash_prefix=sha256_hex(secret_plain)[:16],
    )


@pytest.fixture
async def ws_app(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FastAPI]:
    """真实 app（app.main）+ get_session 覆盖为测试事务，其余行为保持生产口径。"""
    from app.main import app as real_app

    async def _override_get_session() -> AsyncIterator[AsyncSession]:
        yield db

    real_app.dependency_overrides[get_session] = _override_get_session
    # 只放宽首帧预算（防慢机误伤），不放宽身份判定
    monkeypatch.setattr(signaling, "WS_AUTH_TIMEOUT_MS", 2000)

    async def _no_identity_cascade(*_args: object, **_kwargs: object) -> None:
        return None

    # 身份级联会在 auth_ok 后插入 identity_*/greeting 帧，改变本文件的帧序断言；
    # 级联行为由 tests/test_identity_cascade.py 覆盖。
    monkeypatch.setattr(media_bridge, "start_identity_cascade", _no_identity_cascade)
    try:
        yield real_app
    finally:
        real_app.dependency_overrides.clear()


async def test_gate1_ws_forged_elder_id_query_is_ignored(db: AsyncSession, ws_app: FastAPI) -> None:
    """建连 query 伪造 `elder_id` + `device_id` → 服务端仍按 token 绑定的长者回 auth_ok。

    判据二：`signal_endpoint` 源码里不出现 `query_params`——谁把 query 读进身份谁红。
    """
    real = await _seed_bound_elder(
        db, room_no="A-301", bed_no="1 号床", name="张桂芬", secret="gate-device-secret-a"
    )
    other = await _seed_bound_elder(
        db, room_no="A-302", bed_no="2 号床", name="李福贵", secret="gate-device-secret-b"
    )
    token = _device_token(real["device_id"], "gate-device-secret-a")
    forged = {"elder_id": str(other["elder_id"]), "device_id": str(other["device_id"])}

    async with WsClient(ws_app, query=forged) as ws:
        await ws.send_json({"type": "auth", "token": token})
        auth_ok = await ws.receive_json()
        assert auth_ok["type"] == "auth_ok"
        assert str(auth_ok["elder_id"]) == str(real["elder_id"])
        conn = signaling._registry[str(real["device_id"])]
        assert str(conn.elder_id) == str(real["elder_id"])
        assert str(other["device_id"]) not in signaling._registry

    assert "query_params" not in inspect.getsource(signaling.signal_endpoint)


async def test_gate1_ws_forged_device_id_cannot_hijack_session(
    db: AsyncSession, ws_app: FastAPI
) -> None:
    """用 A 的 token 建连但 query 里伪造 B 的 `device_id` → 注册表键与房间仍属于 A。"""
    device_a = await _seed_bound_elder(
        db, room_no="B-201", bed_no="1 号床", name="张桂芬", secret="gate-device-secret-c"
    )
    device_b = await _seed_bound_elder(
        db, room_no="B-202", bed_no="2 号床", name="李福贵", secret="gate-device-secret-d"
    )
    token = _device_token(device_a["device_id"], "gate-device-secret-c")
    forged = {"device_id": str(device_b["device_id"]), "elder_id": str(device_b["elder_id"])}

    async with WsClient(ws_app, query=forged) as ws:
        await ws.send_json({"type": "auth", "token": token})
        auth_ok = await ws.receive_json()
        assert auth_ok["type"] == "auth_ok"
        assert str(auth_ok["elder_id"]) == str(device_a["elder_id"])
        assert str(auth_ok["room_id"]) == str(device_a["room_id"])
        assert str(device_b["device_id"]) not in signaling._registry
        # B 的长者 id 不得出现在服务端为这条连接建立的任何状态里
        assert str(signaling._registry[str(device_a["device_id"])].elder_id) == str(
            device_a["elder_id"]
        )


# ===========================================================================
# 闸门① 身份注入 · 模型输出
# ===========================================================================


def _fake_llm(script: list[str]) -> LLMService:
    """把 llm_service 换成脚本化假 provider（MockTransport，不打真网）。"""
    queue = list(script)

    def handler(request: httpx.Request) -> httpx.Response:
        _ = request
        text = (
            queue.pop(0)
            if queue
            else json.dumps({"next": "speak", "speech": "默认兜底话术。"}, ensure_ascii=False)
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = Settings(
        _env_file=None,
        llm_base_url=_FAKE_LLM_URL,
        llm_api_key=_FAKE_LLM_KEY,
        llm_model="test-model",
        llm_timeout_seconds=5,
        llm_max_retries=2,
        llm_temperature=0.3,
    )
    return LLMService(settings, client=client)


class _RecordingBackend:
    """本地 `ToolBackend` 替身：只记录是否被触达（用来证明「拒绝不触达后端」）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, name: str, args: dict) -> ToolResult:
        self.calls.append((name, dict(args)))
        return ToolResult(status="ok", content={"text": "晴，18℃。"})


async def test_gate1_model_payload_elder_id_is_discarded() -> None:
    """模型在 JSON 里塞 `elder_id` / `thread_id`（含嵌套）→ 进 falsified_identity，不生效。

    删掉 `companion._strip_identity` 或 `_plan_node` 的 forged 收集即红：
    `falsified_identity` 为空、`route_decision` 也可能被模型值污染。
    """
    forged = _FORGED_ELDER_ID
    server_elder = str(uuid.uuid4())
    payload = {
        "next": "speak",
        "speech": "我在呢，您慢慢说。",
        "elder_id": forged,
        "thread_id": "forged-thread",
        "nested": {"elder_id": forged},
    }
    llm = _fake_llm([json.dumps(payload, ensure_ascii=False)])
    registry = ToolRegistry(backend=_RecordingBackend())

    result = await run_turn(
        "你好呀",
        llm=llm,
        tool_registry=registry,
        runtime_context=RuntimeContext(elder_id=server_elder, thread_id="server-thread"),
    )

    assert result.finish_reason == "speak"
    assert result.spoken_text == "我在呢，您慢慢说。"
    names = sorted(record["name"] for record in result.falsified_identity)
    assert names == ["elder_id", "elder_id", "thread_id"]
    assert all(
        record["rejected_by"] == "identity_from_model" for record in result.falsified_identity
    )
    assert all("step" in record for record in result.falsified_identity)
    assert forged not in result.spoken_text
    # 身份唯一来源是服务端 runtime_context
    assert str(result.route_decision.elder_id) == server_elder
    assert result.route_decision.thread_id == "server-thread"


# ===========================================================================
# 闸门② 记忆跨长者隔离
# ===========================================================================


async def test_gate2_recall_never_crosses_elders(db: AsyncSession) -> None:
    """A 的记忆永不出现在 B 的召回里（两个方向都验）。

    删掉 `memory_service.recall` 里的 `MemoryChunk.elder_id == elder_id` 即红：
    用 A 的原句做 query，B 的召回第一条会是 A 的切片（distance≈0）。
    """
    provider = DeterministicEmbeddingProvider()
    elder_a = await _seed_elder(db, "张桂芬")
    elder_b = await _seed_elder(db, "李福贵")
    secret = "我孙子叫小宇，周末来看我"
    b_own = "我喜欢听评弹《玉蜻蜓》"
    await memory_service.remember(
        db, elder_id=elder_a.elder_id, content=secret, embedding_provider=provider
    )
    await memory_service.remember(
        db, elder_id=elder_a.elder_id, content="我爱吃红烧肉", embedding_provider=provider
    )
    await memory_service.remember(
        db, elder_id=elder_b.elder_id, content=b_own, embedding_provider=provider
    )

    recalled_a = await memory_service.recall(
        db, elder_id=elder_a.elder_id, query=secret, embedding_provider=provider
    )
    assert recalled_a[0].content == secret
    assert {chunk.elder_id for chunk in recalled_a} == {elder_a.elder_id}

    recalled_b = await memory_service.recall(
        db, elder_id=elder_b.elder_id, query=b_own, embedding_provider=provider
    )
    assert recalled_b, "B 自己的记忆必须能召回，否则这条用例没有判别力"
    assert {chunk.elder_id for chunk in recalled_b} == {elder_b.elder_id}
    assert secret not in {chunk.content for chunk in recalled_b}

    # 反向：A 用 B 的原句查，也只能拿到 A 自己的切片。
    # 判据不依赖「跨文本相似度」——哈希噪声余弦在召回下限之外时允许为空；
    # 一旦漏了 elder 过滤，B 的切片（distance≈0）必然出现，这条仍然红。
    recalled_a_from_b = await memory_service.recall(
        db, elder_id=elder_a.elder_id, query=b_own, embedding_provider=provider
    )
    assert {chunk.elder_id for chunk in recalled_a_from_b} <= {elder_a.elder_id}
    assert b_own not in {chunk.content for chunk in recalled_a_from_b}


async def test_gate2_same_content_written_by_two_elders_stays_isolated(
    db: AsyncSession,
) -> None:
    """同一句内容由 A、B 先后写入，不得互相 supersede（A 的切片仍是 active）。

    删掉 `_supersede_conflicts` 的 elder 条件即红：B 写入时会把 A 的同内容切片
    判成 superseded，A 的召回随即变空。
    """
    provider = DeterministicEmbeddingProvider()
    elder_a = await _seed_elder(db, "张桂芬")
    elder_b = await _seed_elder(db, "李福贵")
    content = "我喜欢听越剧《梁山伯与祝英台》"

    chunk_a = await memory_service.remember(
        db, elder_id=elder_a.elder_id, content=content, embedding_provider=provider
    )
    await memory_service.remember(
        db, elder_id=elder_b.elder_id, content=content, embedding_provider=provider
    )
    await db.refresh(chunk_a)

    assert chunk_a.status == "active"
    recalled = await memory_service.recall(
        db, elder_id=elder_a.elder_id, query=content, embedding_provider=provider
    )
    assert [chunk.chunk_id for chunk in recalled] == [chunk_a.chunk_id]


async def test_gate2_admin_export_returns_single_elder(db: AsyncSession) -> None:
    """管理端导出 B 的记忆不含 A 的任何内容（REST 列表 + 服务层 `export_elder_memories`）。

    删掉 `memory_service.list_chunks` 或 `memory_admin_service._load_records` 的 `elder_id`
    条件即红（导出会混进另一位长者的正文）。
    """
    from app.services.memory_admin_service import export_elder_memories

    provider = DeterministicEmbeddingProvider()
    elder_a = await _seed_elder(db, "张桂芬")
    elder_b = await _seed_elder(db, "李福贵")
    a_only = "我女儿每周三来看我"
    b_only = "我爱唱评弹"
    await memory_service.remember(
        db, elder_id=elder_a.elder_id, content=a_only, embedding_provider=provider
    )
    await memory_service.remember(
        db, elder_id=elder_b.elder_id, content=b_only, embedding_provider=provider
    )
    admin_id = uuid.uuid4()
    admin = Identity(kind="staff", staff_id=str(admin_id), role="admin")

    async with _api_client(db, admin) as client:
        resp = await client.get("/api/memory/chunks", params={"elder_id": str(elder_b.elder_id)})

    assert resp.status_code == 200
    body = resp.json()
    assert body["elder_id"] == str(elder_b.elder_id)
    assert body["total"] == 1
    assert {item["content"] for item in body["items"]} == {b_only}
    assert all(item["elder_id"] == str(elder_b.elder_id) for item in body["items"])

    exported_b = await export_elder_memories(
        db, elder_id=elder_b.elder_id, actor_id=admin_id, actor_type="staff"
    )
    assert [record["content"] for record in exported_b] == [b_only]
    assert a_only not in {record["content"] for record in exported_b}
    assert all(record["elder_id"] == str(elder_b.elder_id) for record in exported_b)
    assert "embedding" not in exported_b[0]

    # 反向导出 A：只拿到 A 自己的记录
    exported_a = await export_elder_memories(
        db, elder_id=elder_a.elder_id, actor_id=admin_id, actor_type="staff"
    )
    assert [record["content"] for record in exported_a] == [a_only]


# ---------------------------------------------------------------------------
# 闸门② 静态扫描：每条 MemoryChunk 查询都必须带 elder_id 条件
# ---------------------------------------------------------------------------

_QUERY_ROOT_NAMES = frozenset({"select", "update", "delete"})

#: 全库维护函数：允许「无 elder_id 即全库」语义，但条件必须在 `if elder_id is not None` 里。
_GLOBAL_MAINTENANCE = frozenset({"decay"})


@dataclass(frozen=True, slots=True)
class ScanReport:
    """静态扫描结果：每条记忆查询的 elder_id 保护点数，以及不合格项。"""

    findings: list[str]
    statements: dict[str, int]
    guards: dict[str, int]


def _call_name(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _parent_map(tree: ast.AST) -> dict[int, ast.AST]:
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    return parents


def _outermost_call(node: ast.AST, parents: dict[int, ast.AST]) -> ast.AST:
    """把 `select(...).where(...).order_by(...)` 收敛到最外层调用。"""
    current = node
    while True:
        attr = parents.get(id(current))
        if not isinstance(attr, ast.Attribute):
            return current
        call = parents.get(id(attr))
        if not isinstance(call, ast.Call) or call.func is not attr:
            return current
        current = call


def _mentions(node: ast.AST, name: str) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id == name:
            return True
        if isinstance(sub, ast.Attribute) and sub.attr == name:
            return True
    return False


def _is_elder_guard(node: ast.AST) -> bool:
    """`MemoryChunk.elder_id == ...` / `... == MemoryChunk.elder_id` 形式的比较。"""
    if not isinstance(node, ast.Compare):
        return False
    sides = [node.left, *node.comparators]
    return any(
        isinstance(side, ast.Attribute)
        and side.attr == "elder_id"
        and _mentions(side.value, "MemoryChunk")
        for side in sides
    )


def _inside_elder_conditional(node: ast.AST, parents: dict[int, ast.AST]) -> bool:
    """该节点是否位于 `if elder_id is not None:` 之类的分支里。"""
    current = node
    while True:
        parent = parents.get(id(current))
        if parent is None:
            return False
        if isinstance(parent, ast.If) and _mentions(parent.test, "elder_id"):
            return True
        current = parent


def scan_memory_queries(source: str) -> ScanReport:
    """扫描源码里每个 `MemoryChunk` 查询是否都有 elder_id 条件。

    规则（每个函数内）：`MemoryChunk` 查询语句数 ≤ `MemoryChunk.elder_id` 比较点数；
    非白名单函数的条件不得藏在条件分支里；全库维护函数必须把条件放在
    `if elder_id is not None` 分支里（证明「全库」是显式设计而非漏写）。
    """
    tree = ast.parse(source)
    parents = _parent_map(tree)
    findings: list[str] = []
    statements: dict[str, int] = {}
    guards: dict[str, int] = {}

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        roots: dict[int, ast.AST] = {}
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call) and _call_name(inner) in _QUERY_ROOT_NAMES:
                outer = _outermost_call(inner, parents)
                if _mentions(outer, "MemoryChunk"):
                    roots[id(outer)] = outer
        if not roots:
            continue
        guard_nodes = [sub for sub in ast.walk(node) if _is_elder_guard(sub)]
        statements[node.name] = len(roots)
        guards[node.name] = len(guard_nodes)
        if node.name in _GLOBAL_MAINTENANCE:
            if not guard_nodes:
                findings.append(f"{node.name}: 全库维护函数缺少 elder_id 条件分支")
            elif not all(_inside_elder_conditional(guard, parents) for guard in guard_nodes):
                findings.append(f"{node.name}: 全库维护函数的 elder_id 条件必须放在条件分支里")
            continue
        if len(guard_nodes) < len(roots):
            findings.append(
                f"{node.name}: {len(roots)} 条 MemoryChunk 查询只有 "
                f"{len(guard_nodes)} 处 elder_id 条件"
            )
        for guard in guard_nodes:
            if _inside_elder_conditional(guard, parents):
                findings.append(f"{node.name}: elder_id 条件被放进条件分支，可能整段查询失去隔离")
    return ScanReport(findings=findings, statements=statements, guards=guards)


def _strip_elder_guards(source: str, function_name: str) -> str:
    """把指定函数里所有 `MemoryChunk.elder_id` 比较摘掉（模拟「删掉防护」）。

    两种落点都要覆盖：`where(...)` 的调用实参，以及 `conditions = [...]` 这类列表元素
    （`list_chunks` 的守卫就在列表里，只摘实参会漏掉）。
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call):
                    inner.args = [arg for arg in inner.args if not _is_elder_guard(arg)]
                elif isinstance(inner, (ast.List, ast.Tuple, ast.Set)):
                    inner.elts = [elt for elt in inner.elts if not _is_elder_guard(elt)]
    return ast.unparse(tree)


def test_gate2_sql_scanner_flags_missing_elder_guard() -> None:
    """自检：逐个函数摘掉 elder_id 条件，扫描器必须点名该函数（证明它真的会红）。"""
    source = _MEMORY_SERVICE_PATH.read_text(encoding="utf-8")
    baseline = scan_memory_queries(source)
    assert baseline.findings == []

    for function_name in (
        "_supersede_conflicts",
        "recall",
        "list_chunks",
        "enforce_capacity",
        "delete_by_elder",
        "decay",
    ):
        mutated = scan_memory_queries(_strip_elder_guards(source, function_name))
        assert mutated.guards[function_name] == 0, f"{function_name} 的守卫没被摘干净"
        assert mutated.statements[function_name] == baseline.statements[function_name]
        assert any(finding.startswith(function_name) for finding in mutated.findings), (
            f"摘掉 {function_name} 的防护后扫描器未报警：{mutated.findings}"
        )

    # 摘 recall 只影响 recall：其它函数的守卫计数不变（扫描按函数切，不互相污染）
    only_recall = scan_memory_queries(_strip_elder_guards(source, "recall"))
    assert only_recall.guards["recall"] == 0
    assert only_recall.guards["list_chunks"] == baseline.guards["list_chunks"]


def test_gate2_every_memory_query_is_elder_scoped() -> None:
    """真源码扫描：`memory_service.py` 里每条 MemoryChunk 查询都带 elder_id。

    删掉任意一条 `WHERE elder_id`（如 recall 的 select 或 list_chunks 的 conditions）即红。
    """
    source = _MEMORY_SERVICE_PATH.read_text(encoding="utf-8")
    report = scan_memory_queries(source)

    assert report.findings == []
    for name in (
        "_supersede_conflicts",
        "recall",
        "list_chunks",
        "enforce_capacity",
        "delete_by_elder",
    ):
        assert report.statements.get(name, 0) >= 1, f"{name} 未被扫描到，扫描器可能空转"
        assert report.guards[name] >= report.statements[name]
    # 全库维护（decay）单列：条件写死，且必须显式在 `if elder_id is not None` 分支内
    assert report.statements["decay"] == 1
    assert report.guards["decay"] == 1
    # 覆盖足够多的查询点，避免扫描器因 AST 形状变化而静默空转
    assert sum(report.statements.values()) >= 8


# ===========================================================================
# 闸门③ 工具越权
# ===========================================================================


async def test_gate3_unknown_tool_is_forbidden_and_audited(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """白名单外的工具 → forbidden + gate=whitelist，且**一次都不触达后端**。

    删掉 `ToolRegistry.call` 的白名单分支即红：后端会被调用、状态变 ok。
    """
    backend = _RecordingBackend()
    registry = ToolRegistry(backend=backend)
    # 白名单是**精确集合**：N22 追加机构知识检索、N21 追加 mock-operations 两个工具后
    # 是四项。这条断言的作用是「任何新增放行都必须同时改这里」，所以它跟着白名单一起改，
    # 而不是放宽成 `<=`。
    assert (
        frozenset(
            {
                "get_weather",
                "search_institution_knowledge",
                "create_work_order",
                "send_family_notification",
            }
        )
        == WHITELIST
    )

    with caplog.at_level(logging.INFO, logger="app.agents.tool_registry"):
        outcome = await registry.call("delete_elder_memory", {"elder_id": str(uuid.uuid4())})

    assert outcome.result.status == "forbidden"
    assert outcome.result.content == {"reason": "tool_not_in_whitelist"}
    assert outcome.record.allowed is False
    assert outcome.record.executed is False
    assert outcome.record.gate == "whitelist"
    assert outcome.record.cost_steps == 1
    assert backend.calls == []
    assert any("tool denied by whitelist" in record.getMessage() for record in caplog.records)


async def test_gate3_whitelisted_tool_rejects_illegal_args() -> None:
    """白名单工具传非法参数 → param_error（四分类之一），且不触达后端。"""
    backend = _RecordingBackend()
    registry = ToolRegistry(backend=backend)
    cases: list[tuple[dict[str, Any], str]] = [
        ({}, "missing_field:city"),
        ({"city": 42}, "field_must_be_string:city"),
        ({"city": "   "}, "field_must_be_nonempty:city"),
        ({"city": "北京", "elder_id": str(uuid.uuid4())}, "unexpected_field:elder_id"),
        ({"elder_id": str(uuid.uuid4())}, "unexpected_field:elder_id"),
    ]

    for args, reason in cases:
        outcome = await registry.call("get_weather", args)
        assert outcome.result.status == "param_error", args
        assert outcome.result.content == {"reason": reason}, args
        assert outcome.record.allowed is False
        assert outcome.record.executed is False
        assert outcome.record.gate == "schema"

    assert backend.calls == []


async def test_gate3_turn_budget_blocks_ninth_call() -> None:
    """每轮最多 8 次工具调用（PRD FR-DLG-10）；第 9 次 reject_budget 且不污染熔断窗口。"""
    settings = get_settings()
    assert settings.max_tool_calls_per_turn == 8
    assert settings.tool_repeat_window_seconds == 60
    assert settings.tool_repeat_break_threshold == 3

    rail = Guardrail()  # 阈值全部来自配置，不传构造参数
    decisions = [rail.check(f"tool_{index}", {"n": index}) for index in range(8)]
    assert [decision.action for decision in decisions] == ["allow"] * 8
    assert rail.budget.used == 8

    ninth = rail.check("tool_8", {"n": 8})
    assert ninth.action == "reject_budget"
    assert ninth.rejected is True
    assert ninth.speech == spoken_speech("tool_budget")
    assert rail.budget.used == 8
    # 判定顺序：预算先于熔断，被预算拒掉的调用不进 60s 窗口
    assert rail.breaker.timestamps("tool_8") == ()


class _Clock:
    """可显式推进的钟，用于挂住「1 分钟窗口」这类秒级语义。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def test_gate3_same_tool_breaker_trips_on_third_call() -> None:
    """同一 Tool 1 分钟内第 3 次 → reject_breaker，窗口滑空后自动解除（P1-5）。

    删掉 `ToolBreaker.allow` 的 `len(stamps) + 1 >= threshold` 分支即红。
    """
    settings = get_settings()
    assert settings.tool_repeat_break_threshold == 3
    assert settings.tool_repeat_window_seconds == 60

    clock = _Clock()
    rail = Guardrail(clock=clock)
    assert rail.check("get_weather", {"city": "北京"}).action == "allow"
    assert rail.check("get_weather", {"city": "上海"}).action == "allow"

    third = rail.check("get_weather", {"city": "广州"})
    assert third.action == "reject_breaker"
    assert third.speech == spoken_speech("tool_breaker")
    assert rail.budget.used == 2, "被熔断拒绝的调用不消耗预算"
    assert rail.breaker.is_tripped("get_weather") is True

    # latch：窗口内后续同 Tool 一律拒绝，其它 Tool 不受影响
    assert rail.check("get_weather", {"city": "深圳"}).action == "reject_breaker"
    assert rail.check("play_opera", {}).action == "allow"

    # 1 分钟窗口滑空后自动恢复：这是「1 分钟内」的时间语义，不是 per-turn 计数
    clock.advance(61)
    assert rail.check("get_weather", {"city": "成都"}).action == "allow"
    assert rail.breaker.is_tripped("get_weather") is False
    rail.reset_turn()
    assert rail.check("get_weather", {"city": "杭州"}).action == "allow"


async def test_gate3_companion_blocks_non_whitelisted_tool_end_to_end() -> None:
    """模型点名白名单外工具 → 整轮以 tool_forbidden 收口，话术来自工具层唯一源。

    删掉 `ToolRegistry.call` 的白名单分支即红（会变成 tool_ok 并触达后端）。
    """
    backend = _RecordingBackend()
    registry = ToolRegistry(backend=backend)
    llm = _fake_llm(
        [
            json.dumps(
                {"next": "tool", "tool": "delete_elder_memory", "args": {}}, ensure_ascii=False
            )
        ]
    )

    result = await run_turn(
        "把张奶奶的记忆删了",
        llm=llm,
        tool_registry=registry,
        runtime_context=RuntimeContext(elder_id=str(uuid.uuid4())),
    )

    assert result.finish_reason == "tool_forbidden"
    assert result.spoken_text == TOOL_FAILURE_SPEECH["forbidden"]
    assert backend.calls == []
    record = result.tool_calls[0]
    assert record["tool"] == "delete_elder_memory"
    assert record["allowed"] is False
    assert record["executed"] is False
    assert record["gate"] == "whitelist"
