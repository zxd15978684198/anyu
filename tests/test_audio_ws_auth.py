"""迭代 3 T2 · WS /ws/signal 首帧鉴权与信令转发测试（设计记录（信令WS与鉴权） §W6）。

口径（沿用项目攻击面测试约定）：

- **真实 app 实例（app.main:create_app 产物）+ 真实 DB + 每用例事务回滚，禁 mock 应用与数据库**；
- WS 客户端不用 Starlette TestClient：它自带独立事件循环，会把依赖注入的测试事务
  （pytest 循环里创建的 AsyncSession）跨循环使用（test_auth_api.py 文件头登记的同一坑）。
  这里改为**进程内 ASGI 直驱 harness**——在 pytest 的事件循环里直接调用 ASGI app，
  完整走 TraceIdMiddleware → FastAPI 路由 → 依赖注入真实链路，等价于 TestClient 且不跨循环；
- A3 媒体层未就位，offer 应答方用**进程内测试替身**承接（`set_media_handler`），
  app 与 DB 仍真实。此替身口径已登记任务卡变更记录；
- 每条写操作「回查 DB」再断言（契约 §10 硬约束）——本文件的 DB 读侧都是 seed 数据，
  在 test_session 事务内回查。

11 条用例清单与计划 §W6 一一对应；用例 12–14 是 task-44（批B-解绑踢 WS）补充的
凭证吊销踢线用例。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import secrets
import time
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.audio import signaling
from app.audio.asr import ASREvent, ASREventKind, ASRResult, ASRUnavailable
from app.core.config import get_settings
from app.core.security import sha256_hex, sign_device_session, sign_staff_access
from app.db import get_session as _app_get_session
from app.models.organization import Bed, Device, Elder, Room
from app.models.staff import StaffAccount
from conftest import truncate_identity_tables

# ---------------------------------------------------------------------------
# 常量与令牌工具
# ---------------------------------------------------------------------------

_DEVICE_SECRET_A = "device-long-secret-plain-a"
_DEVICE_SECRET_B = "device-long-secret-plain-b"


def _device_token(device_id: Any, secret_plain: str) -> str:
    """按 E4 口径自签设备会话（载荷 tv = token_hash 前 16 位）。"""
    return sign_device_session(
        device_id=str(device_id),
        token_hash_prefix=sha256_hex(secret_plain)[:16],
    )


def _expired_device_token(device_id: Any, secret_plain: str) -> str:
    """exp 已过的设备会话（TTL 24h，签发时刻回拨 25h）。"""
    return sign_device_session(
        device_id=str(device_id),
        token_hash_prefix=sha256_hex(secret_plain)[:16],
        now=datetime.now(UTC) - timedelta(hours=25),
    )


# ---------------------------------------------------------------------------
# 进程内 ASGI WebSocket harness（文件头口径说明）
# ---------------------------------------------------------------------------


class WsClient:
    """在 pytest 事件循环里直驱 ASGI app 的最小 WS 客户端。

    模拟 uvicorn 行为：握手后先向 app 投递 websocket.connect；server 侧 close 后
    自动向 app 注入 websocket.disconnect。
    """

    def __init__(self, app: FastAPI) -> None:
        self._app = app
        self._to_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._from_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    async def _receive(self) -> dict[str, Any]:
        return await self._to_app.get()

    async def _send(self, message: Mapping[str, Any]) -> None:
        msg = dict(message)
        await self._from_app.put(msg)
        if msg.get("type") == "websocket.close":
            # 真实 ASGI 服务器行为：close 完成后向应用投递 disconnect
            await self._to_app.put({"type": "websocket.disconnect", "code": 1000})

    async def __aenter__(self) -> WsClient:
        scope: dict[str, Any] = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "scheme": "ws",
            "path": "/ws/signal",
            "raw_path": b"/ws/signal",
            "root_path": "",
            "query_string": b"",
            "headers": [],
            "client": ("testclient", 0),
            "server": ("testserver", 80),
            "subprotocols": [],
            "state": {},
        }
        self._task = asyncio.create_task(self._app(scope, self._receive, self._send))
        # ASGI 规范：握手完成后由服务器先向 app 发 websocket.connect，app 才能 accept
        await self._to_app.put({"type": "websocket.connect"})
        accepted = await self._next_server_message()
        assert accepted["type"] == "websocket.accept"
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
        await self.wait_closed()

    async def _next_server_message(self) -> dict[str, Any]:
        """取下一条 server 帧；若端点任务先结束（异常/早退），直接抛出端点异常而不是死等。

        注意：竞速只取消自己创建的 getter future，**绝不取消 app 任务本身**——
        它是长连接载体，误取消会让 CancelledError 在下一次 await 时炸掉端点。
        """
        assert self._task is not None
        getter = asyncio.ensure_future(self._from_app.get())
        done, _ = await asyncio.wait({getter, self._task}, return_when=asyncio.FIRST_COMPLETED)
        if getter in done:
            if self._task in done:
                # 帧与端点结束同刻：帧已拿到，端点异常留给 wait_closed 抛出
                return getter.result()
            return getter.result()
        getter.cancel()  # 输掉竞速的 getter 才允许取消
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

    async def expect_close(self) -> int:
        """读下一条 server 帧，必须是 close，返回关闭码。"""
        msg = await self._next_server_message()
        assert msg["type"] == "websocket.close", f"期望 close 帧，收到 {msg}"
        return int(msg.get("code", 1000))

    async def close(self) -> None:
        await self._to_app.put({"type": "websocket.disconnect", "code": 1000})

    async def wait_closed(self) -> None:
        assert self._task is not None
        await self._task  # 端点异常会在这里抛出，测试失败可见


# ---------------------------------------------------------------------------
# Fixtures：真实 DB + 真实 app + 事务回滚（test_auth_api 同款隔离策略）
# ---------------------------------------------------------------------------


@pytest.fixture
async def test_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        await truncate_identity_tables(engine)
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def test_session(test_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    sf = async_sessionmaker(test_engine, expire_on_commit=False)
    async with sf() as session:
        try:
            yield session
        finally:
            await session.rollback()


@pytest.fixture
async def seeded_db(test_session: AsyncSession) -> dict[str, Any]:
    """两间房各一台已绑定长者的设备（用例 10 双连接隔离需要两台设备）。"""
    data: dict[str, Any] = {}
    for idx, (room_no, bed_no, name, secret) in enumerate(
        [
            ("A-301", "1 号床", "张桂芬", _DEVICE_SECRET_A),
            ("A-302", "2 号床", "李福贵", _DEVICE_SECRET_B),
        ]
    ):
        room = Room(room_no=room_no)
        test_session.add(room)
        await test_session.flush()
        bed = Bed(room_id=room.room_id, bed_no=bed_no)
        device = Device(
            room_id=room.room_id,
            token_hash=sha256_hex(secret),
            status="offline",
        )
        test_session.add_all([bed, device])
        await test_session.flush()
        elder = Elder(name=name, status="active")
        test_session.add(elder)
        await test_session.flush()
        bed.elder_id = elder.elder_id
        device.elder_id = elder.elder_id
        data[f"elder_id_{idx}"] = elder.elder_id
        data[f"room_id_{idx}"] = room.room_id
        data[f"device_id_{idx}"] = device.device_id
    await test_session.flush()
    return data


@pytest.fixture
async def ws_app(
    test_session: AsyncSession, seeded_db: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[FastAPI]:
    """真实 app（app.main 工厂产物）+ get_session 覆盖为测试事务（同循环共享 seed 数据）。

    测试期放宽 auth 首帧预算到 2s：harness 与 seed 全在事件循环内，正常毫秒级完成；
    放宽只防 CI 慢机误伤，超时分支由用例 2 专门 monkeypatch 回小值验证。
    """
    from app.main import app as real_app

    async def _override_get_session() -> AsyncIterator[AsyncSession]:
        yield test_session

    real_app.dependency_overrides[_app_get_session] = _override_get_session
    monkeypatch.setattr(signaling, "WS_AUTH_TIMEOUT_MS", 2000)

    # 身份级联（C5/C6）会在 auth_ok 之后插入 identity_*/greeting 帧，改变本文件的帧序断言。
    # 本文件只测 WS 传输语义（心跳、踢旧留新、路由、清理），级联行为由
    # tests/test_identity_cascade.py 端到端覆盖（含 signal_endpoint 与 thread 落库）。
    async def _no_identity_cascade(*_args: object, **_kwargs: object) -> None:
        return None

    from app.audio import media_bridge

    monkeypatch.setattr(media_bridge, "start_identity_cascade", _no_identity_cascade)

    yield real_app
    real_app.dependency_overrides.clear()


def _connect(ws_app: FastAPI) -> WsClient:
    return WsClient(ws_app)


# ---------------------------------------------------------------------------
# 用例 1：不发 auth 直接发 offer → auth_fail invalid + 4401
# ---------------------------------------------------------------------------


async def test_case1_offer_before_auth_rejected(ws_app: FastAPI) -> None:
    async with _connect(ws_app) as ws:
        await ws.send_json({"type": "offer", "sdp": "v=0 fake"})
        assert (await ws.receive_json())["code"] == "invalid"
        assert await ws.expect_close() == signaling.WS_CLOSE_AUTH_FAILED


# ---------------------------------------------------------------------------
# 用例 2：首帧窗口内无首帧 → auth_fail timeout + 4401
# ---------------------------------------------------------------------------


async def test_case2_first_frame_timeout(ws_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    # 断言只依赖「窗口内没等到首帧」这件事，与窗口绝对值无关：压到 100ms 让用例快，
    # 不跟着默认 3000ms 空等（默认值另有 test_default_auth_window_is_3000ms 钉死）。
    monkeypatch.setattr(signaling, "WS_AUTH_TIMEOUT_MS", 100)
    async with _connect(ws_app) as ws:
        assert (await ws.receive_json())["code"] == "timeout"
        assert await ws.expect_close() == signaling.WS_CLOSE_AUTH_FAILED


def test_default_auth_window_is_3000ms() -> None:
    """默认首帧活体窗口 = 3000ms（不是安全边界，见 signaling.py 常量注释）。

    钉死默认值：窗口被无意改小会重新打开「刷新时首帧晚到 → 误判 timeout →
    前端清会话回登录页」的 P1（2026-11-13 实测刷新时首帧可达 0.5–1s）。
    """
    assert signaling.WS_AUTH_TIMEOUT_MS == 3000


async def test_case2b_delayed_auth_within_window_gets_ok(
    ws_app: FastAPI, monkeypatch: pytest.MonkeyPatch, seeded_db: dict[str, Any]
) -> None:
    """延迟约 0.9s 才发 auth，仍应拿到 auth_ok。

    复现口径：pad 刷新后主线程忙于 10.7MB VRM 解析，ws.onopen 里同步发出的
    auth 帧实际到达被推迟到 0.5–1s；旧 300ms 窗口下这里收到的是 auth_fail/timeout。
    窗口压到 1200ms 而不是默认 3000ms：既证明「秒级延迟可通过」，又不让用例真等 3s。
    """
    monkeypatch.setattr(signaling, "WS_AUTH_TIMEOUT_MS", 1200)
    token = _device_token(seeded_db["device_id_0"], _DEVICE_SECRET_A)
    async with _connect(ws_app) as ws:
        await asyncio.sleep(0.9)
        await ws.send_json({"type": "auth", "token": token})
        assert (await ws.receive_json())["type"] == "auth_ok"


# ---------------------------------------------------------------------------
# 用例 3：伪造 token → 4401 + 审计五字段 + 日志无 token 明文
# ---------------------------------------------------------------------------


async def test_case3_forged_token_audited(
    ws_app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    forged = "eyJhbGciOiJIUzI1NiJ9.forged-payload.not-a-signature"
    with caplog.at_level(logging.INFO, logger="app.audio.signaling"):
        async with _connect(ws_app) as ws:
            await ws.send_json({"type": "auth", "token": forged})
            assert (await ws.receive_json())["code"] == "invalid"
            assert await ws.expect_close() == signaling.WS_CLOSE_AUTH_FAILED

    audits = [r for r in caplog.records if getattr(r, "audit_action", None) == "ws_auth_failed"]
    assert audits, "必须落一条 ws_auth_failed 审计日志"
    record = audits[-1]
    # 审计五字段齐全（trace_id / audit_action / actor / target / result）
    assert record.trace_id and len(record.trace_id) == 32
    assert record.audit_action == "ws_auth_failed"
    assert record.actor == "unknown"
    assert record.target == "/ws/signal"
    assert record.result == "invalid"
    # 整个日志流里 token 明文零出现（S-23 ④）
    all_text = " ".join(r.getMessage() for r in caplog.records)
    assert forged not in all_text


# ---------------------------------------------------------------------------
# 用例 4：过期 token → code=expired + 4401
# ---------------------------------------------------------------------------


async def test_case4_expired_token(ws_app: FastAPI, seeded_db: dict[str, Any]) -> None:
    token = _expired_device_token(seeded_db["device_id_0"], _DEVICE_SECRET_A)
    async with _connect(ws_app) as ws:
        await ws.send_json({"type": "auth", "token": token})
        assert (await ws.receive_json())["code"] == "expired"
        assert await ws.expect_close() == signaling.WS_CLOSE_AUTH_FAILED


# ---------------------------------------------------------------------------
# 用例 5：解绑设备（token_hash 轮换，AC-24）后旧 token → 4401
# ---------------------------------------------------------------------------


async def test_case5_unbound_device_token_rejected(
    ws_app: FastAPI, test_session: AsyncSession, seeded_db: dict[str, Any]
) -> None:
    device_id = seeded_db["device_id_0"]
    token = _device_token(device_id, _DEVICE_SECRET_A)

    # 走 E10 的解绑语义（device_service._clear_binding）：清长者 + 置 offline + 轮换哈希
    device = await test_session.get(Device, device_id)
    assert device is not None
    device.elder_id = None
    device.status = "offline"
    device.token_hash = sha256_hex(secrets.token_urlsafe(32))
    await test_session.flush()

    # 回查 DB 确认哈希已轮换（写操作回查口径）
    await test_session.refresh(device)
    assert device.token_hash != sha256_hex(_DEVICE_SECRET_A)

    async with _connect(ws_app) as ws:
        await ws.send_json({"type": "auth", "token": token})
        assert (await ws.receive_json())["code"] == "invalid"
        assert await ws.expect_close() == signaling.WS_CLOSE_AUTH_FAILED


# ---------------------------------------------------------------------------
# 用例 6：合法 token → auth_ok；elder_id/room_id 回查 DB 一致
# ---------------------------------------------------------------------------


async def test_case6_valid_token_auth_ok(
    ws_app: FastAPI, test_session: AsyncSession, seeded_db: dict[str, Any]
) -> None:
    device_id = seeded_db["device_id_0"]
    token = _device_token(device_id, _DEVICE_SECRET_A)

    async with _connect(ws_app) as ws:
        await ws.send_json({"type": "auth", "token": token})
        body = await ws.receive_json()
        assert body["type"] == "auth_ok"

        # 回查 DB：绑定关系才是事实来源
        device = await test_session.get(Device, device_id)
        assert device is not None
        assert str(device.elder_id) == body["elder_id"]
        assert str(device.room_id) == body["room_id"]

        # 注册表状态回查
        conn = signaling._registry.get(str(device_id))
        assert conn is not None and conn.authed


# ---------------------------------------------------------------------------
# 用例 7：ping/pong 即时性
# ---------------------------------------------------------------------------


async def test_case7_ping_pong(ws_app: FastAPI, seeded_db: dict[str, Any]) -> None:
    token = _device_token(seeded_db["device_id_0"], _DEVICE_SECRET_A)
    async with _connect(ws_app) as ws:
        await ws.send_json({"type": "auth", "token": token})
        await ws.receive_json()  # auth_ok
        await ws.send_json({"type": "ping"})
        assert (await ws.receive_json())["type"] == "pong"


# ---------------------------------------------------------------------------
# 用例 8：心跳判死（monkeypatch 常量）→ 踢线 + ws_dead 日志
# ---------------------------------------------------------------------------


async def test_case8_dead_connection_kicked(
    ws_app: FastAPI,
    seeded_db: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(signaling, "WS_DEAD_AFTER_S", 0.2)
    monkeypatch.setattr(signaling, "_DEAD_CHECK_INTERVAL_S", 0.05)
    token = _device_token(seeded_db["device_id_0"], _DEVICE_SECRET_A)

    with caplog.at_level(logging.INFO, logger="app.audio.signaling"):
        async with _connect(ws_app) as ws:
            await ws.send_json({"type": "auth", "token": token})
            await ws.receive_json()  # auth_ok
            # 静默 0.3s（> 0.2s 判死线），不发任何帧
            await asyncio.sleep(0.3)
            assert await ws.expect_close() == 1000  # 判死走正常 close
            await ws.wait_closed()

    assert any(r.getMessage() == "ws_dead" for r in caplog.records)
    dead = [r for r in caplog.records if r.getMessage() == "ws_dead"][-1]
    assert getattr(dead, "trace_id", None) and getattr(dead, "device_id", None)

    # 清理后注册表无残留
    assert str(seeded_db["device_id_0"]) not in signaling._registry


# ---------------------------------------------------------------------------
# 用例 9：offer/answer 往返 + 四类下行消息逐字段一致（测试替身承接 offer）
# ---------------------------------------------------------------------------


async def test_case9_offer_answer_roundtrip_and_downlink(
    ws_app: FastAPI, seeded_db: dict[str, Any]
) -> None:
    device_id = str(seeded_db["device_id_0"])
    token = _device_token(seeded_db["device_id_0"], _DEVICE_SECRET_A)

    # 替身实现（闭包内只引用 signaling 公开面，不触碰 WebSocket 实例）
    async def media_handler(conn: signaling.ConnectionContext, message: dict[str, Any]) -> None:
        assert message["type"] == "offer"
        signaling.deliver(conn.device_id or "", {"type": "answer", "sdp": message["sdp"]})
        # 四类下行逐字段验证（计划 §W4 自检）；C1 已冻结，直接用真实 ASREvent
        signaling.deliver_asr_event(
            conn.device_id or "",
            ASREvent(
                kind=ASREventKind.PARTIAL,
                result=ASRResult(text="今天天", is_final=False, confidence=None),
                error=None,
            ),
        )
        signaling.deliver_asr_event(
            conn.device_id or "",
            ASREvent(
                kind=ASREventKind.FINAL,
                result=ASRResult(text="今天天气怎么样", is_final=True, confidence=None),
                error=None,
            ),
        )
        signaling.deliver_asr_event(
            conn.device_id or "",
            ASREvent(
                kind=ASREventKind.ERROR, result=None, error=ASRUnavailable("内部细节不该透出")
            ),
        )
        assert signaling.deliver(device_id, {"type": "tts_text", "text": "今天晴，适合出门走走。"})
        # 非本设备投递应失败（下行寻址按 device_id）
        assert signaling.deliver("nonexistent-device", {"type": "pong"}) is False

    signaling.set_media_handler(media_handler)
    try:
        async with _connect(ws_app) as ws:
            await ws.send_json({"type": "auth", "token": token})
            await ws.receive_json()  # auth_ok

            await ws.send_json({"type": "offer", "sdp": "v=0 offer-sdp-123"})
            assert (await ws.receive_json()) == {"type": "answer", "sdp": "v=0 offer-sdp-123"}
            assert (await ws.receive_json()) == {"type": "asr_partial", "text": "今天天"}
            assert (await ws.receive_json()) == {"type": "asr_final", "text": "今天天气怎么样"}
            error_msg = await ws.receive_json()
            assert error_msg["type"] == "error"
            assert error_msg["code"] == "asr_unavailable"
            # 脱敏：内部异常细节不得透出
            assert "内部细节不该透出" not in error_msg["message"]
            assert error_msg["message"] == signaling._ERROR_TALKBACK["asr_unavailable"]
            assert (await ws.receive_json()) == {
                "type": "tts_text",
                "text": "今天晴，适合出门走走。",
            }
    finally:
        signaling.set_media_handler(None)


# ---------------------------------------------------------------------------
# 用例 10：双连接隔离 —— A 的下行不会出现在 B
# ---------------------------------------------------------------------------


async def test_case10_two_connections_isolated(ws_app: FastAPI, seeded_db: dict[str, Any]) -> None:
    token_a = _device_token(seeded_db["device_id_0"], _DEVICE_SECRET_A)
    token_b = _device_token(seeded_db["device_id_1"], _DEVICE_SECRET_B)

    async def media_handler(conn: signaling.ConnectionContext, message: dict[str, Any]) -> None:
        # 只向发起连接投递 answer
        signaling.deliver(conn.device_id or "", {"type": "answer", "sdp": message["sdp"]})

    signaling.set_media_handler(media_handler)
    try:
        async with _connect(ws_app) as ws_a, _connect(ws_app) as ws_b:
            await ws_a.send_json({"type": "auth", "token": token_a})
            await ws_a.receive_json()  # auth_ok
            await ws_b.send_json({"type": "auth", "token": token_b})
            await ws_b.receive_json()  # auth_ok

            await ws_a.send_json({"type": "offer", "sdp": "sdp-from-a"})
            assert (await ws_a.receive_json()) == {"type": "answer", "sdp": "sdp-from-a"}

            # B 队列里不允许出现任何 A 的 answer（越线即缺陷）；短超时读空
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(ws_b.receive_json(), timeout=0.2)
    finally:
        signaling.set_media_handler(None)


# ---------------------------------------------------------------------------
# 用例 11：断线清理 —— 注册表空、重连可重复、无悬挂任务
# ---------------------------------------------------------------------------


async def test_case11_disconnect_cleanup_and_reconnect(
    ws_app: FastAPI, seeded_db: dict[str, Any]
) -> None:
    device_id = str(seeded_db["device_id_0"])
    token = _device_token(seeded_db["device_id_0"], _DEVICE_SECRET_A)
    cleaned: list[str] = []

    async def media_handler(conn: signaling.ConnectionContext, message: dict[str, Any]) -> None:
        signaling.deliver(conn.device_id or "", {"type": "answer", "sdp": message["sdp"]})

    def on_disconnect() -> None:
        cleaned.append(device_id)

    signaling.set_media_handler(media_handler)
    try:
        # 第一条连接：auth → offer → 主动断开
        async with _connect(ws_app) as ws:
            await ws.send_json({"type": "auth", "token": token})
            await ws.receive_json()
            await ws.send_json({"type": "offer", "sdp": "round-1"})
            assert (await ws.receive_json())["type"] == "answer"
            conn = signaling._registry[device_id]
            conn.disconnect_callbacks.append(on_disconnect)
        await ws.wait_closed()

        # 清理回查：注册表无残留、回调已触发
        assert device_id not in signaling._registry
        assert cleaned == [device_id]

        # 让取消的 writer / dead watcher 任务收尾
        await asyncio.sleep(0.05)
        hanging = {
            task.get_coro().__qualname__
            for task in asyncio.all_tasks()
            if not task.done() and task.get_coro() is not None
        }
        assert not any(name.startswith("app.audio.signaling") for name in hanging), hanging

        # 同设备重连：auth → offer 全流程可完整重复，无旧状态串扰
        async with _connect(ws_app) as ws2:
            await ws2.send_json({"type": "auth", "token": token})
            body = await ws2.receive_json()
            assert body["type"] == "auth_ok"
            await ws2.send_json({"type": "offer", "sdp": "round-2"})
            assert (await ws2.receive_json()) == {"type": "answer", "sdp": "round-2"}
        await ws2.wait_closed()
        assert device_id not in signaling._registry
    finally:
        signaling.set_media_handler(None)


# ---------------------------------------------------------------------------
# 契约裁决④/②补充：非法消息不关连接；重复 auth 拒；业务帧续命
# ---------------------------------------------------------------------------


async def test_extra_unknown_message_keeps_connection(
    ws_app: FastAPI, seeded_db: dict[str, Any]
) -> None:
    """裁决④：非法/未知消息回 error internal（脱敏），不关连接；trace_id 在日志里可查。"""
    token = _device_token(seeded_db["device_id_0"], _DEVICE_SECRET_A)
    async with _connect(ws_app) as ws:
        await ws.send_json({"type": "auth", "token": token})
        await ws.receive_json()

        await ws.send_json({"type": "time_travel"})  # 未知类型
        error_msg = await ws.receive_json()
        assert error_msg["type"] == "error" and error_msg["code"] == "internal"
        assert error_msg["message"] == signaling._ERROR_TALKBACK["internal"]

        # 连接仍可用：ping → pong
        await ws.send_json({"type": "ping"})
        assert (await ws.receive_json())["type"] == "pong"


async def test_extra_duplicate_auth_rejected(
    ws_app: FastAPI, seeded_db: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    """裁决②：auth 后重复发 auth → auth_fail invalid + 4401，审计 actor= device:<id>。"""
    device_id = str(seeded_db["device_id_0"])
    token = _device_token(seeded_db["device_id_0"], _DEVICE_SECRET_A)
    with caplog.at_level(logging.INFO, logger="app.audio.signaling"):
        async with _connect(ws_app) as ws:
            await ws.send_json({"type": "auth", "token": token})
            await ws.receive_json()  # auth_ok
            await ws.send_json({"type": "auth", "token": token})
            assert (await ws.receive_json())["code"] == "invalid"
            assert await ws.expect_close() == signaling.WS_CLOSE_AUTH_FAILED

    audits = [r for r in caplog.records if getattr(r, "audit_action", None) == "ws_auth_failed"]
    assert audits and audits[-1].actor == f"device:{device_id}"


async def test_extra_business_frame_keeps_alive(
    ws_app: FastAPI, seeded_db: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """W3：业务帧（asr_start）同样刷新活跃时间戳——持续发业务帧不误判死。"""
    monkeypatch.setattr(signaling, "WS_DEAD_AFTER_S", 0.15)
    monkeypatch.setattr(signaling, "_DEAD_CHECK_INTERVAL_S", 0.05)
    token = _device_token(seeded_db["device_id_0"], _DEVICE_SECRET_A)
    async with _connect(ws_app) as ws:
        await ws.send_json({"type": "auth", "token": token})
        await ws.receive_json()
        # 静默线 0.15s 内持续发业务帧，总时长 0.4s 远超判死线仍存活。
        # （未注册媒体替身：每个 asr_start 回一条 error internal，逐条收掉防积压）
        for _ in range(8):
            await asyncio.sleep(0.05)
            await ws.send_json({"type": "asr_start"})
            error_msg = await ws.receive_json()
            assert error_msg["type"] == "error" and error_msg["code"] == "internal"
        # 连接未被判死（还能收 pong）
        await ws.send_json({"type": "ping"})
        assert (await ws.receive_json())["type"] == "pong"
        assert time.monotonic()  # 保持 time 引用（判死逻辑依赖 monotonic 时钟）


# ---------------------------------------------------------------------------
# 依赖注入 / 分层自检（W7 收口辅助断言）
# ---------------------------------------------------------------------------


def test_media_handler_registration_roundtrip() -> None:
    """set_media_handler 注册/注销是 A3 的唯一接线面。"""

    async def handler(conn: signaling.ConnectionContext, message: dict[str, Any]) -> None:
        return None

    assert inspect.iscoroutinefunction(signaling.signal_endpoint)
    signaling.set_media_handler(handler)
    assert signaling._media_handler is handler
    signaling.set_media_handler(None)
    assert signaling._media_handler is None


# ---------------------------------------------------------------------------
# 用例 12–14：凭证吊销踢线（task-44 / 批B-解绑踢 WS，L9 验证 F-2 落点）
#
# 为什么必须主动关：鉴权只在握手做一次，解绑轮换 `token_hash` 只挡**新**连接；
# 已经建立的连接不会因此断开（实测解绑后 15s 仍 open 并继续收到 `tts_text`）。
# 4403 与 4002 分开：前者「设备被管理员解绑/禁用」，后者「同一设备别处登录」。
# ---------------------------------------------------------------------------


async def _seed_admin_token(test_session: AsyncSession) -> str:
    """插一个 admin 账号并自签 Access（E13/E14 类端点要求 admin 角色）。"""
    staff = StaffAccount(
        name="解绑管理员",
        role="admin",
        phone="13800000099",
        password_hash=sha256_hex("admin-plain-for-test"),
        token_version=1,
    )
    test_session.add(staff)
    await test_session.flush()
    return sign_staff_access(kind="staff", staff_id=staff.staff_id, role="admin", token_version=1)


async def test_case12_unbind_endpoint_closes_live_connection(
    ws_app: FastAPI, seeded_db: dict[str, Any], test_session: AsyncSession
) -> None:
    """解绑端点必须把该设备**已经在线**的连接也关掉（4403），不只是让旧 token 再连 401。"""
    device_id = str(seeded_db["device_id_0"])
    token = _device_token(seeded_db["device_id_0"], _DEVICE_SECRET_A)
    admin_token = await _seed_admin_token(test_session)

    async with _connect(ws_app) as ws:
        await ws.send_json({"type": "auth", "token": token})
        assert (await ws.receive_json())["type"] == "auth_ok"
        assert signaling.authed_connection(device_id) is not None
        assert signaling.deliver(device_id, {"type": "pong"}) is True
        assert (await ws.receive_json())["type"] == "pong"  # 在线时下行确实送达

        transport = ASGITransport(app=ws_app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http:
            response = await http.post(
                f"/api/devices/{device_id}/unbind",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
        assert response.status_code == 200, response.text

        # 解绑后客户端立刻收到 4403，而不是继续挂在线上收下行消息
        assert await ws.expect_close() == signaling.WS_CLOSE_DEVICE_REVOKED
        await ws.wait_closed()

    # 注册表与多连接表都干净了；再投递被判「不在线」
    assert signaling.authed_connection(device_id) is None
    assert device_id not in signaling._registry
    assert device_id not in signaling._sessions
    assert signaling.deliver(device_id, {"type": "pong"}) is False


class _RecordingWebSocket:
    """只记 close(code, reason) 的 WebSocket 替身（多连接用例不真起两路 ASGI）。"""

    def __init__(self) -> None:
        self.closed: list[tuple[int, str]] = []

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed.append((int(code), reason))


async def test_case13_close_device_sessions_closes_every_connection() -> None:
    """同一设备的多条在册连接（顶号瞬间的旧连接 + 新连接）一次全关，返回条数。"""
    device_id = "0f4d3e2a-1111-4a2b-9c3d-000000000001"
    newer = signaling.ConnectionContext(trace_id="t-new", websocket=_RecordingWebSocket())
    older = signaling.ConnectionContext(trace_id="t-old", websocket=_RecordingWebSocket())
    for conn in (older, newer):
        conn.device_id = device_id
        conn.authed = True

    async with signaling._registry_lock:
        signaling._registry[device_id] = newer
        signaling._sessions[device_id] = [older, newer]

    assert await signaling.close_device_sessions(device_id) == 2

    older_ws = older.websocket
    newer_ws = newer.websocket
    assert isinstance(older_ws, _RecordingWebSocket)
    assert isinstance(newer_ws, _RecordingWebSocket)
    assert older_ws.closed == [(signaling.WS_CLOSE_DEVICE_REVOKED, "device_unbound")]
    assert newer_ws.closed == [(signaling.WS_CLOSE_DEVICE_REVOKED, "device_unbound")]
    # 先摘鉴权态：关的过程中并发投递不会写进一条将死的连接
    assert older.authed is False and newer.authed is False
    assert device_id not in signaling._registry and device_id not in signaling._sessions
    # 已清空后再调是幂等空转，不抛异常
    assert await signaling.close_device_sessions(device_id) == 0


async def test_case14_close_device_sessions_custom_code() -> None:
    """关闭码/原因可由调用方覆盖（如禁用设备传 4403 + reason=locked）。"""
    device_id = "0f4d3e2a-2222-4a2b-9c3d-000000000002"
    conn = signaling.ConnectionContext(trace_id="t", websocket=_RecordingWebSocket())
    conn.device_id = device_id
    conn.authed = True
    async with signaling._registry_lock:
        signaling._registry[device_id] = conn
        signaling._sessions[device_id] = [conn]

    assert await signaling.close_device_sessions(device_id, code=4000, reason="locked") == 1

    websocket = conn.websocket
    assert isinstance(websocket, _RecordingWebSocket)
    assert websocket.closed == [(4000, "locked")]
    # 4403 与 4002 必须是不同值，前端才能区分「被解绑」与「别处登录」
    assert signaling.WS_CLOSE_DEVICE_REVOKED != signaling.WS_CLOSE_SUPERSEDED
