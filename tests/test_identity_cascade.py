"""迭代 4 T2 · 身份级联（C5 / C6）测试。

覆盖四类主路径 + 契约裁决里的「忽略」分支 + 30s 超时 + metrics 三个打点位：

1. 声纹命中直通 voice（`identity_result{voice}` → `tts_text`，thread 落库 voice）
2. 低置信 → `identity_ask`（字段逐条对齐 C5）→ `identity_confirm` → `identity_result{confirm}`
3. 30s 无确认 → 服务端主动转 generic（定时器到点写库 + 通知）
4. `ask_id` 不匹配 / 候选集外 `elder_id` / 重复 confirm → **静默忽略**，不回 error、不关连接，
   且不占掉这次 ask 的 30s 窗口（C5 裁决 ③）
5. `client_diag` 只记日志、不回执
6. `metrics_enabled` 开关

测试口径（与仓库既有约定一致）：
- **真实 services / 真实 DB**（conftest 的 `db_conn` 事务 + 回滚），只替身两处：
  ①声纹推理（`cosine_compare_against_elder`，会连推理容器）；
  ②WS 连接对象（`WebSocket`，本模块只用到 send/close/receive 四个方法）。
- 级联层接受**注入会话**（`session=`），因此不会真的开独立连接写主库，用例结束零残留。
- 每条写操作都「回查 DB」再断言，不靠日志推断落库结果。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from starlette.websockets import WebSocketDisconnect

from app.audio import media_bridge, signaling
from app.core import metrics
from app.core.config import get_settings
from app.core.runtime_context import Identity
from app.core.security import sha256_hex
from app.models.conversation import ConversationThread
from app.models.organization import Bed, Device, Elder, Room
from app.services import identity_service
from app.services import voiceprint_service as vs
from app.services.device_service import CandidateList, ElderCandidate
from conftest import require_db_connection

_DEVICE_TOKEN_SECRET = "device-long-secret-plain-cascade"
_ELDER_NAME = "张桂芬"
_ELDER_NAME_MASKED = "张*芬"


# ---------------------------------------------------------------------------
# 替身：够用的 WebSocket
# ---------------------------------------------------------------------------


class _FakeWebSocket:
    """只实现本模块用到的方法；记录发出的帧与关闭码，供断言「没关连接」。

    `frames` 里可以放 **bytes**：真 Starlette 的 `receive_text()` 对二进制帧会在
    `message["text"]` 上抛 KeyError，这里如实复现，才能覆盖 P1 二进制帧那条路径。
    """

    def __init__(self, frames: list[dict[str, Any] | bytes] | None = None) -> None:
        self._frames: list[str | bytes] = [
            frame if isinstance(frame, bytes) else json.dumps(frame, ensure_ascii=False)
            for frame in (frames or [])
        ]
        self.sent: list[dict[str, Any]] = []
        self.close_codes: list[int] = []
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True

    async def receive_text(self) -> str:
        if not self._frames:
            # 给 writer 协程一个把 auth_ok 排空的机会（真实链路里客户端在等网络）
            await asyncio.sleep(0.02)
            raise WebSocketDisconnect(code=1000)
        frame = self._frames.pop(0)
        if isinstance(frame, bytes):
            raise KeyError("text")  # 与 Starlette 二进制帧行为一致
        return frame

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))

    async def close(self, code: int = 1000) -> None:
        self.close_codes.append(code)


# ---------------------------------------------------------------------------
# 夹具：真实 DB 事务 + seed + 已注册连接
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    """本文件自建 NullPool 引擎（与 test_pad_avatar / test_elders_api 同构）。

    为什么不用 `app.db.engine`：模块级连接池会把上一个用例（上一个事件循环）的
    asyncpg 连接带进本用例，取连接时报 `'NoneType' object has no attribute 'send'`。
    NullPool 每用例现连现关，连不上就 skip（绝不静默通过）。
    """
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db_conn(db_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """每用例一个事务、结束回滚；连不上库按 conftest 的统一策略处置（默认 fail，`ANYU_ALLOW_DB_SKIP=1` 才 skip）。"""
    conn = await require_db_connection(db_engine)

    trans = await conn.begin()
    try:
        yield conn
    finally:
        await trans.rollback()
        await conn.close()


@pytest.fixture
async def db_session(db_conn: AsyncConnection) -> AsyncIterator[AsyncSession]:
    """把夹具那条未提交事务借给被测代码；残留由夹具回滚（同 test_voiceprint_service）。

    被测代码（identity_service）内部会 `commit()`：在已有事务的连接上它会退化成
    SAVEPOINT 释放，于是写入对本会话可见、对主库不可见，用例结束整体回滚。
    """
    session = AsyncSession(bind=db_conn, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()


@pytest.fixture
async def seeded(db_session: AsyncSession) -> SimpleNamespace:
    """一间房、一张床、一台已绑定长者的设备（单人间口径，候选恒 1 位）。"""
    room = Room(room_no="A-301")
    db_session.add(room)
    await db_session.flush()

    bed = Bed(room_id=room.room_id, bed_no="1 号床")
    device = Device(
        room_id=room.room_id, token_hash=sha256_hex(_DEVICE_TOKEN_SECRET), status="offline"
    )
    db_session.add_all([bed, device])
    await db_session.flush()

    elder = Elder(name=_ELDER_NAME, status="active")
    db_session.add(elder)
    await db_session.flush()

    bed.elder_id = elder.elder_id
    device.elder_id = elder.elder_id
    await db_session.flush()

    identity = Identity(
        kind="device",
        device_id=str(device.device_id),
        elder_id=str(elder.elder_id),
    )
    return SimpleNamespace(
        room=room,
        bed=bed,
        device=device,
        elder=elder,
        identity=identity,
        device_id=str(device.device_id),
        elder_id=elder.elder_id,
    )


@asynccontextmanager
async def registered(
    device_id: str, frames: list[dict[str, Any] | bytes] | None = None
) -> AsyncIterator[tuple[signaling.ConnectionContext, _FakeWebSocket]]:
    """把一个「已鉴权」的连接放进 signaling 注册表（`deliver()` 只认注册表 + authed）。

    退出时清注册表并取消级联定时器 —— 与真实连接关闭走同一条清理路径。
    """
    ws = _FakeWebSocket(frames)
    conn = signaling.ConnectionContext(
        trace_id="trace-identity-test",
        websocket=cast(Any, ws),
        device_id=device_id,
        room_id="room-test",
        authed=True,
    )
    signaling._registry[device_id] = conn
    try:
        yield conn, ws
    finally:
        signaling._registry.pop(device_id, None)
        await media_bridge.cancel_identity_cascade(device_id)


@pytest.fixture(autouse=True)
def _isolate_metrics() -> AsyncIterator[None]:
    """每个用例前后的进程内指标清零，避免用例间互相污染计数断言。"""
    metrics.reset_metrics()
    yield
    metrics.reset_metrics()


@pytest.fixture(autouse=True)
def _no_browser_attached(monkeypatch: pytest.MonkeyPatch) -> None:
    """这些用例只验级联的消息序列，不模拟浏览器。

    `_emit_greeting` 现在会等「数字人立起来」（`media_bridge.wait_avatar_ready`）：本模块的
    `registered()` 会放一条**已鉴权**连接进 signaling，于是闸门生效、开场白被推迟一个事件循环，
    用例「立刻断言消息序列」就会缺 `tts_text`。这里直接声明「没有浏览器」，走同步下发路径；
    「有浏览器时要等数字人」的行为由
    `tests/test_dialogue_runtime.py::test_greeting_held_until_avatar_ready` 单独覆盖。
    """

    monkeypatch.setattr(media_bridge, "_has_media_session", lambda _device_id: False)


@pytest.fixture
def matched_voice(monkeypatch: pytest.MonkeyPatch, seeded: SimpleNamespace) -> None:
    """声纹命中（cosine 0.93 ≥ 0.55）。真实实现要连推理容器，这里只替身这一层。"""

    async def _fake(session: AsyncSession, elder_id: UUID, audio: bytes) -> vs.CosineMatch:
        return vs.CosineMatch(matched=True, score=0.93, threshold=0.55, elder_id=elder_id)

    monkeypatch.setattr(identity_service.vs, "cosine_compare_against_elder", _fake)


@pytest.fixture
def low_confidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """声纹低置信（0.42 < 0.55）→ 走主动确认（C5 reason=low_confidence）。"""

    async def _fake(session: AsyncSession, elder_id: UUID, audio: bytes) -> vs.CosineMatch:
        return vs.CosineMatch(matched=False, score=0.42, threshold=0.55, elder_id=None)

    monkeypatch.setattr(identity_service.vs, "cosine_compare_against_elder", _fake)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def drain(conn: signaling.ConnectionContext) -> list[dict[str, Any]]:
    """取走下行队列里的全部消息（writer 协程未启动，队列即「已发出的帧」）。"""
    out: list[dict[str, Any]] = []
    while not conn.downstream.empty():
        out.append(conn.downstream.get_nowait())
    return out


async def threads_of(session: AsyncSession, device: Device) -> list[ConversationThread]:
    rows = await session.execute(
        select(ConversationThread).where(ConversationThread.device_id == device.device_id)
    )
    return list(rows.scalars().all())


async def open_threads_of(session: AsyncSession, device: Device) -> list[ConversationThread]:
    return [row for row in await threads_of(session, device) if row.ended_at is None]


def only_type(messages: list[dict[str, Any]], msg_type: str) -> dict[str, Any]:
    hits = [m for m in messages if m["type"] == msg_type]
    assert len(hits) == 1, f"期望恰好一条 {msg_type}，实际 {messages}"
    return hits[0]


# ---------------------------------------------------------------------------
# 1. voice：命中直通
# ---------------------------------------------------------------------------


async def test_voice_pass_through_result_then_greeting(
    db_session: AsyncSession,
    seeded: SimpleNamespace,
    matched_voice: None,
) -> None:
    async with registered(seeded.device_id) as (conn, _ws):
        cascade = await media_bridge.start_identity_cascade(
            conn, seeded.identity, audio_bytes=b"fake-audio-frame", session=db_session
        )

        messages = drain(conn)
        # C5/C6：先 identity_result，再问候；voice 分支不该出现 identity_ask
        assert [m["type"] for m in messages] == ["identity_result", "tts_text"]
        result = messages[0]
        assert result["source"] == "voice"
        assert result["elder_id"] == str(seeded.elder_id)
        assert messages[1]["text"] == media_bridge.DEFAULT_GREETING_TEXT
        # C6：开场白不过 LLM，degraded 恒 False —— 但字段必须在（防被删回去）
        assert messages[1]["degraded"] is False

        assert cascade is not None
        assert cascade.resolved is True
        assert cascade.source == "voice"
        assert cascade.timeout_task is None

    # 回查 DB：voice 终态已经落 thread
    rows = await threads_of(db_session, seeded.device)
    assert len(rows) == 1
    assert rows[0].identity_source == "voice"
    assert rows[0].elder_id == seeded.elder_id


# ---------------------------------------------------------------------------
# 2. confirm：ask → confirm
# ---------------------------------------------------------------------------


async def test_low_confidence_ask_matches_contract_then_confirm_opens_thread(
    db_session: AsyncSession, seeded: SimpleNamespace, low_confidence: None
) -> None:
    async with registered(seeded.device_id) as (conn, ws):
        cascade = await media_bridge.start_identity_cascade(
            conn, seeded.identity, audio_bytes=b"fake-audio-frame", session=db_session
        )

        messages = drain(conn)
        assert [m["type"] for m in messages] == ["identity_ask"]
        ask = messages[0]
        # C5 逐字段核对（字段名不许自创，值域不许扩）
        assert set(ask) == {"type", "ask_id", "reason", "candidates", "timeout_s"}
        assert ask["reason"] == "low_confidence"
        assert ask["timeout_s"] == 30
        assert isinstance(ask["ask_id"], str) and len(ask["ask_id"]) == 32
        assert ask["candidates"] == [
            {"elder_id": str(seeded.elder_id), "display_name": _ELDER_NAME_MASKED}
        ]

        # 首句通用模式：确认前先开一条 provisional generic thread 接住这句话，
        # 但它是「待确认」状态（confirm_pending），没有 elder_id。
        assert cascade is not None
        assert cascade.resolved is True and cascade.confirm_pending is True
        provisional = await open_threads_of(db_session, seeded.device)
        assert len(provisional) == 1
        assert provisional[0].identity_source == "generic"
        assert provisional[0].elder_id is None

        await media_bridge.handle_identity_confirm(
            conn,
            {"type": "identity_confirm", "ask_id": ask["ask_id"], "elder_id": str(seeded.elder_id)},
        )

        follow = drain(conn)
        assert [m["type"] for m in follow] == ["identity_result", "tts_text"]
        assert follow[0] == {
            "type": "identity_result",
            "source": "confirm",
            "elder_id": str(seeded.elder_id),
        }
        assert follow[1]["text"] == media_bridge.DEFAULT_GREETING_TEXT
        # C6：greeting 走本地话术，degraded 恒 False；字段必须显式出现
        assert follow[1]["degraded"] is False
        # 流程性约束：确认之后连接没被关（用户还在正常说话）
        assert ws.close_codes == []

    rows = await threads_of(db_session, seeded.device)
    assert len(rows) == 2
    by_source = {row.identity_source: row for row in rows}
    assert by_source["generic"].ended_at is not None  # provisional 被原子替换
    assert by_source["confirm"].ended_at is None
    assert by_source["confirm"].elder_id == seeded.elder_id


async def test_no_audio_defers_identity_until_first_utterance(
    db_session: AsyncSession, seeded: SimpleNamespace
) -> None:
    """D 方案（2026-11-10）：登录没有音频时**不再索取确认**，只播中性问候。

    原来这条路径是「服务层 no_audio → 映射成 low_confidence → 立刻弹确认层」，
    长者的体感是「刚坐下就被要求证明自己是谁」。裁决 D 后改成等老人开口，
    拿第一句真音频跑声纹（`resolve_identity_from_audio`），命中不了才问。
    """
    async with registered(seeded.device_id) as (conn, _ws):
        cascade = await media_bridge.start_identity_cascade(
            conn, seeded.identity, session=db_session
        )
        messages = drain(conn)
        assert [m["type"] for m in messages] == ["identity_pending", "tts_text"]
        assert messages[0] == {"type": "identity_pending", "reason": "await_wake"}
        # 开场白必须是中性话术：这个时刻服务端还不敢念人名
        assert messages[1]["text"] == media_bridge.GENERIC_GREETING_TEXT
        assert messages[1]["degraded"] is False
        assert cascade is not None
        assert cascade.resolved is False
        assert cascade.reason == "await_wake"
        # 没有确认层就没有 30s 计时器（计时只在真弹 ask 之后才挂）
        assert cascade.timeout_task is None

    assert await threads_of(db_session, seeded.device) == []


async def test_vp_unavailable_ask_reason(
    db_session: AsyncSession, seeded: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(session: AsyncSession, elder_id: UUID, audio: bytes) -> vs.CosineMatch:
        raise vs.VoiceprintExtractUnavailable("容器不可用")

    monkeypatch.setattr(identity_service.vs, "cosine_compare_against_elder", _boom)

    async with registered(seeded.device_id) as (conn, _ws):
        await media_bridge.start_identity_cascade(
            conn, seeded.identity, audio_bytes=b"fake-audio-frame", session=db_session
        )
        ask = only_type(drain(conn), "identity_ask")
        assert ask["reason"] == "vp_unavailable"


# ---------------------------------------------------------------------------
# 3. 契约裁决③：三类「忽略」分支都不得回 error / 关连接 / 占掉 30s 窗口
# ---------------------------------------------------------------------------


async def test_wrong_ask_id_ignored_then_valid_confirm_still_works(
    db_session: AsyncSession,
    seeded: SimpleNamespace,
    low_confidence: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with registered(seeded.device_id) as (conn, ws):
        await media_bridge.start_identity_cascade(
            conn, seeded.identity, audio_bytes=b"fake-audio-frame", session=db_session
        )
        ask = only_type(drain(conn), "identity_ask")

        with caplog.at_level(logging.WARNING, logger="app.audio.identity"):
            await media_bridge.handle_identity_confirm(
                conn,
                {"type": "identity_confirm", "ask_id": "0" * 32, "elder_id": str(seeded.elder_id)},
            )

        # 不回执：队列里既没有 error 也没有 identity_result
        assert drain(conn) == []
        assert ws.close_codes == []
        # 只有那条待确认的 provisional generic，没有 confirm thread
        provisional = await open_threads_of(db_session, seeded.device)
        assert len(provisional) == 1 and provisional[0].identity_source == "generic"
        assert any(
            record.message == "identity_confirm_ignored"
            and getattr(record, "reason", None) == "ask_id_mismatch"
            for record in caplog.records
        )

        # 关键：错误 ask_id 不占用这次询问，正确 ask_id 仍然可以确认
        await media_bridge.handle_identity_confirm(
            conn,
            {"type": "identity_confirm", "ask_id": ask["ask_id"], "elder_id": str(seeded.elder_id)},
        )
        assert only_type(drain(conn), "identity_result")["source"] == "confirm"

    rows = await threads_of(db_session, seeded.device)
    assert {row.identity_source for row in rows} == {"generic", "confirm"}
    assert next(row for row in rows if row.ended_at is None).identity_source == "confirm"


async def test_elder_outside_candidates_ignored_and_creates_no_thread(
    db_session: AsyncSession,
    seeded: SimpleNamespace,
    low_confidence: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stranger = uuid4()
    async with registered(seeded.device_id) as (conn, ws):
        await media_bridge.start_identity_cascade(
            conn, seeded.identity, audio_bytes=b"fake-audio-frame", session=db_session
        )
        ask = only_type(drain(conn), "identity_ask")

        with caplog.at_level(logging.WARNING, logger="app.audio.identity"):
            await media_bridge.handle_identity_confirm(
                conn,
                {"type": "identity_confirm", "ask_id": ask["ask_id"], "elder_id": str(stranger)},
            )

        assert drain(conn) == []
        assert ws.close_codes == []
        assert any(
            getattr(record, "reason", None) == "elder_id_not_candidate" for record in caplog.records
        )
        # 完工标准 §7.2：候选集外的 elder_id 绝不产生该 elder 的 thread
        rows = await threads_of(db_session, seeded.device)
        assert all(row.elder_id != stranger for row in rows)

        cascade = media_bridge.active_cascade(seeded.device_id)
        assert cascade is not None and cascade.resolved is True and cascade.confirm_pending is True


async def test_duplicate_confirm_ignored(
    db_session: AsyncSession, seeded: SimpleNamespace, low_confidence: None
) -> None:
    async with registered(seeded.device_id) as (conn, ws):
        await media_bridge.start_identity_cascade(
            conn, seeded.identity, audio_bytes=b"fake-audio-frame", session=db_session
        )
        ask = only_type(drain(conn), "identity_ask")
        confirm = {
            "type": "identity_confirm",
            "ask_id": ask["ask_id"],
            "elder_id": str(seeded.elder_id),
        }

        await media_bridge.handle_identity_confirm(conn, confirm)
        assert [m["type"] for m in drain(conn)] == ["identity_result", "tts_text"]

        # 重复 confirm：不再下发任何消息，也不产生第二个 thread
        await media_bridge.handle_identity_confirm(conn, confirm)
        assert drain(conn) == []
        assert ws.close_codes == []

    rows = await threads_of(db_session, seeded.device)
    assert len(rows) == 2  # provisional generic 被结束 + confirm 留下
    assert next(row for row in rows if row.ended_at is None).identity_source == "confirm"


async def test_confirm_without_any_cascade_is_ignored(seeded: SimpleNamespace) -> None:
    """连接上还没起过级联（例如重连瞬间）：忽略，不回 error、不关连接。"""
    async with registered(seeded.device_id) as (conn, ws):
        await media_bridge.handle_identity_confirm(
            conn,
            {"type": "identity_confirm", "ask_id": "x" * 32, "elder_id": str(seeded.elder_id)},
        )
        assert drain(conn) == []
        assert ws.close_codes == []


# ---------------------------------------------------------------------------
# 4. 30s 超时 → generic
# ---------------------------------------------------------------------------


async def test_timeout_switches_to_generic(
    db_session: AsyncSession,
    seeded: SimpleNamespace,
    low_confidence: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 30s 不真的等：把定时器时长压到 50ms（口径不变，只改常量）
    monkeypatch.setattr(media_bridge, "IDENTITY_ASK_TIMEOUT_S", 0.05)

    async with registered(seeded.device_id) as (conn, ws):
        # D 方案后 ask 只在「有音频且声纹低置信」时出现，因此这里必须带音频。
        await media_bridge.start_identity_cascade(
            conn, seeded.identity, audio_bytes=b"fake-audio-frame", session=db_session
        )
        ask = only_type(drain(conn), "identity_ask")
        assert ask["timeout_s"] == 0  # int(0.05)：只在本用例里出现的压缩值
        cascade = media_bridge.active_cascade(seeded.device_id)
        assert cascade is not None
        provisional_id = cascade.provisional_thread_id
        assert provisional_id is not None

        # 到点前不该有任何后续消息
        await asyncio.sleep(0.01)
        assert drain(conn) == []

        await asyncio.sleep(0.2)  # 等定时器到点并完成写库
        follow = drain(conn)
        assert [m["type"] for m in follow] == ["identity_result", "tts_text"]
        assert follow[0] == {"type": "identity_result", "source": "generic", "elder_id": None}
        assert follow[1]["text"] == media_bridge.GENERIC_GREETING_TEXT
        # 通用话术同样是本地模板，degraded 恒 False（C6 字段必须存在）
        assert follow[1]["degraded"] is False
        assert ws.close_codes == []

        cascade = media_bridge.active_cascade(seeded.device_id)
        assert cascade is not None and cascade.resolved is True and cascade.source == "generic"
        # 超时复用那条 provisional，不另开第二条
        assert cascade.thread_id == provisional_id
        assert cascade.provisional_thread_id is None

    rows = await threads_of(db_session, seeded.device)
    assert len(rows) == 1
    assert rows[0].thread_id == provisional_id
    assert rows[0].identity_source == "generic"
    assert rows[0].elder_id is None


async def test_confirm_cancels_timeout_timer(
    db_session: AsyncSession, seeded: SimpleNamespace, low_confidence: None
) -> None:
    """确认成功后定时器必须被取消：否则 30s 后会多出一行 generic thread。"""
    async with registered(seeded.device_id) as (conn, _ws):
        await media_bridge.start_identity_cascade(
            conn, seeded.identity, audio_bytes=b"fake-audio-frame", session=db_session
        )
        ask = only_type(drain(conn), "identity_ask")
        cascade = media_bridge.active_cascade(seeded.device_id)
        assert cascade is not None and cascade.timeout_task is not None

        await media_bridge.handle_identity_confirm(
            conn,
            {"type": "identity_confirm", "ask_id": ask["ask_id"], "elder_id": str(seeded.elder_id)},
        )
        await asyncio.sleep(0)
        assert cascade.timeout_task is None

    rows = await threads_of(db_session, seeded.device)
    assert {row.identity_source for row in rows} == {"generic", "confirm"}
    open_rows = [row for row in rows if row.ended_at is None]
    assert len(open_rows) == 1 and open_rows[0].identity_source == "confirm"


async def test_cancel_identity_cascade_stops_pending_timer(
    db_session: AsyncSession,
    seeded: SimpleNamespace,
    low_confidence: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连接关闭 → 定时器取消 → 不写任何 thread（防泄漏）。"""
    monkeypatch.setattr(media_bridge, "IDENTITY_ASK_TIMEOUT_S", 0.05)

    async with registered(seeded.device_id) as (conn, _ws):
        await media_bridge.start_identity_cascade(
            conn, seeded.identity, audio_bytes=b"fake-audio-frame", session=db_session
        )
        only_type(drain(conn), "identity_ask")
        cascade = media_bridge.active_cascade(seeded.device_id)
        assert cascade is not None and cascade.timeout_task is not None

        task = cascade.timeout_task
        await media_bridge.cancel_identity_cascade(seeded.device_id)
        assert task.done() is True
        assert media_bridge.active_cascade(seeded.device_id) is None

        await asyncio.sleep(0.15)
        assert drain(conn) == []
        rows = await threads_of(db_session, seeded.device)
        assert len(rows) == 1 and rows[0].identity_source == "generic"
        assert rows[0].ended_at is not None  # provisional 被结束，不留 open 会话


# ---------------------------------------------------------------------------
# 5. client_diag：只记日志、不回执
# ---------------------------------------------------------------------------


async def test_client_diag_logs_but_never_replies(caplog: pytest.LogCaptureFixture) -> None:
    async with registered(
        str(uuid4()),
        frames=[{"type": "client_diag", "stage": "vrm_load", "ok": False, "error": "vrm 加载超时"}],
    ) as (
        conn,
        ws,
    ):
        with (
            caplog.at_level(logging.INFO, logger="app.audio.signaling"),
            pytest.raises(WebSocketDisconnect),
        ):
            await signaling._read_loop(conn)

        assert drain(conn) == []  # 不回执（尤其不回 error）
        assert ws.close_codes == []
        record = next(r for r in caplog.records if r.message == "ws_client_diag")
        assert record.stage == "vrm_load"
        assert record.ok is False
        assert record.error == "vrm 加载超时"


async def test_client_diag_avatar_ready_releases_greeting_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`avatar_ready ok=true` 与 `vrm_load ok=true` 一样算「数字人立起来了」。

    长者端在无 WebGL / VRM 加载失败时会退到静态头像（FR-3D-02），那种情况下 `vrm_load`
    永远是 `false`；若只认 `vrm_load`，老人就得等到 12s 上限才听见开场白。
    """
    notified: list[str] = []
    monkeypatch.setattr(
        "app.audio.media_bridge.notify_avatar_ready", lambda device_id: notified.append(device_id)
    )
    device_id = str(uuid4())
    async with registered(
        device_id,
        frames=[{"type": "client_diag", "stage": "avatar_ready", "ok": True, "error": ""}],
    ) as (conn, _ws):
        with pytest.raises(WebSocketDisconnect):
            await signaling._read_loop(conn)

        assert notified == [conn.device_id]
        assert drain(conn) == []


async def test_client_diag_avatar_ready_failure_does_not_release_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notified: list[str] = []
    monkeypatch.setattr(
        "app.audio.media_bridge.notify_avatar_ready", lambda device_id: notified.append(device_id)
    )
    async with registered(
        str(uuid4()),
        frames=[{"type": "client_diag", "stage": "avatar_ready", "ok": False, "error": "no_image"}],
    ) as (conn, _ws):
        with pytest.raises(WebSocketDisconnect):
            await signaling._read_loop(conn)

        assert notified == []


async def test_client_diag_unknown_stage_still_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with registered(
        str(uuid4()),
        frames=[{"type": "client_diag", "stage": "mystery", "ok": True, "error": ""}],
    ) as (conn, _ws):
        with (
            caplog.at_level(logging.INFO, logger="app.audio.signaling"),
            pytest.raises(WebSocketDisconnect),
        ):
            await signaling._read_loop(conn)

        record = next(r for r in caplog.records if r.message == "ws_client_diag")
        assert record.stage == "unknown"
        assert record.stage_raw == "mystery"
        assert drain(conn) == []


async def test_client_diag_playback_feeds_metric(caplog: pytest.LogCaptureFixture) -> None:
    """⑥ 前端播放节点：`stage="playback"` 的 elapsed_ms 落到 client_playback_ms。

    PRD:543 的 1.5s 口径是「首段回答音频开始播放」，这一半在浏览器里；服务端能拿到的
    唯一证据就是这条上报。这里同时断言「不回执」与「计数/耗时各一笔」。
    """
    async with registered(
        str(uuid4()),
        frames=[
            {
                "type": "client_diag",
                "stage": "playback",
                "ok": True,
                "error": "",
                "elapsed_ms": 512,
            }
        ],
    ) as (conn, _ws):
        with (
            caplog.at_level(logging.INFO, logger="app.audio.signaling"),
            pytest.raises(WebSocketDisconnect),
        ):
            await signaling._read_loop(conn)
        assert drain(conn) == []

    counters = cast(dict[str, int], metrics.metrics_snapshot()["counters"])
    timings = cast(dict[str, dict[str, float]], metrics.metrics_snapshot()["timings"])
    assert counters["client_diag_total{stage=playback,ok=true}"] == 1
    assert timings["client_playback_ms"]["count"] == 1.0
    assert timings["client_playback_ms"]["max_ms"] == 512.0


async def test_client_diag_playback_clamps_absurd_elapsed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """端侧算错（或时钟跳变）不该造出一个 1e9 的耗时样本：按 30s 上限记。"""
    async with registered(
        str(uuid4()),
        frames=[
            {
                "type": "client_diag",
                "stage": "playback",
                "ok": True,
                "error": "",
                "elapsed_ms": 10**9,
            }
        ],
    ) as (conn, _ws):
        with (
            caplog.at_level(logging.INFO, logger="app.audio.signaling"),
            pytest.raises(WebSocketDisconnect),
        ):
            await signaling._read_loop(conn)
        assert drain(conn) == []

    timings = cast(dict[str, dict[str, float]], metrics.metrics_snapshot()["timings"])
    assert timings["client_playback_ms"]["max_ms"] == 30_000.0


# ---------------------------------------------------------------------------
# 6. 分发接线：identity_confirm 走级联层，不走媒体上行
# ---------------------------------------------------------------------------


async def test_read_loop_routes_confirm_to_cascade(
    db_session: AsyncSession,
    seeded: SimpleNamespace,
    low_confidence: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media_calls: list[str] = []

    async def _media_handler(conn: signaling.ConnectionContext, message: dict[str, Any]) -> None:
        media_calls.append(message["type"])

    monkeypatch.setattr(signaling, "_media_handler", _media_handler, raising=False)

    async with registered(seeded.device_id) as (conn, _ws):
        await media_bridge.start_identity_cascade(
            conn, seeded.identity, audio_bytes=b"fake-audio-frame", session=db_session
        )
        ask = only_type(drain(conn), "identity_ask")
        confirm = {
            "type": "identity_confirm",
            "ask_id": ask["ask_id"],
            "elder_id": str(seeded.elder_id),
        }
        conn.websocket._frames.append(json.dumps(confirm, ensure_ascii=False))

        with pytest.raises(WebSocketDisconnect):
            await signaling._read_loop(conn)

        assert media_calls == []  # identity_confirm 不该进 A3 上行处理器
        assert only_type(drain(conn), "identity_result")["source"] == "confirm"

    rows = await threads_of(db_session, seeded.device)
    assert {row.identity_source for row in rows} == {"generic", "confirm"}
    assert next(row for row in rows if row.ended_at is None).identity_source == "confirm"


# ---------------------------------------------------------------------------
# 7. 端点接线：auth_ok 之后起级联，连接关闭时取消
# ---------------------------------------------------------------------------


async def test_signal_endpoint_starts_cascade_and_cancels_on_close(
    db_session: AsyncSession, seeded: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[tuple[str | None, str]] = []
    cancelled: list[tuple[str | None, bool]] = []

    async def _fake_start(
        conn: signaling.ConnectionContext,
        identity: Identity,
        *,
        audio_bytes: bytes | None = None,
        session: AsyncSession | None = None,
    ) -> None:
        started.append((conn.device_id, identity.kind))

    async def _fake_cancel(
        device_id: str | None, *, conn: signaling.ConnectionContext | None = None
    ) -> None:
        # 端点必须带上 conn（P1 归属守卫），否则旧连接的 finally 会误删新连接的定时器
        cancelled.append((device_id, conn is not None))

    async def _fake_authenticate(
        conn: signaling.ConnectionContext, session: AsyncSession
    ) -> Identity:
        conn.device_id = seeded.device_id
        conn.elder_id = str(seeded.elder_id)
        conn.last_frame_at = 0.0
        return seeded.identity

    async def _fake_candidates(
        session: AsyncSession, identity: Identity, device_id: UUID
    ) -> CandidateList:
        return CandidateList(
            room_id=seeded.room.room_id,
            mode="single",
            candidates=[
                ElderCandidate(
                    elder_id=seeded.elder_id, bed_no="1 号床", display_name=_ELDER_NAME_MASKED
                )
            ],
        )

    monkeypatch.setattr(media_bridge, "start_identity_cascade", _fake_start)
    monkeypatch.setattr(media_bridge, "cancel_identity_cascade", _fake_cancel)
    monkeypatch.setattr(signaling, "_authenticate", _fake_authenticate)
    monkeypatch.setattr(signaling, "list_elder_candidates", _fake_candidates)

    ws = _FakeWebSocket()  # 无后续帧：accept → auth_ok → 立刻 WebSocketDisconnect
    await signaling.signal_endpoint(websocket=cast(Any, ws), session=db_session)

    assert ws.accepted is True
    assert ws.sent and ws.sent[0]["type"] == "auth_ok"
    assert started == [(seeded.device_id, "device")]
    # 关闭时取消，防 30s 孤儿任务；且必须带 conn（P1：只清自己那条级联）
    assert cancelled == [(seeded.device_id, True)]


# ---------------------------------------------------------------------------
# 8. metrics 三个打点位 + 开关
# ---------------------------------------------------------------------------


async def test_metrics_count_three_contract_points(
    db_session: AsyncSession, seeded: SimpleNamespace, low_confidence: None
) -> None:
    async with registered(seeded.device_id) as (conn, _ws):
        await media_bridge.start_identity_cascade(
            conn, seeded.identity, audio_bytes=b"fake-audio-frame", session=db_session
        )
        ask = only_type(drain(conn), "identity_ask")
        await media_bridge.handle_identity_confirm(
            conn,
            {"type": "identity_confirm", "ask_id": ask["ask_id"], "elder_id": str(seeded.elder_id)},
        )

    counters = cast(dict[str, int], metrics.metrics_snapshot()["counters"])
    assert counters["identity_ask_sent{reason=low_confidence}"] == 1
    assert counters["identity_confirm_received{result=accepted}"] == 1
    assert counters["identity_result_sent{source=confirm}"] == 1
    # 没发生过的分支不建 key（快照里出现 0 会被误读成有流量）
    assert "identity_result_sent{source=voice}" not in counters
    assert "identity_confirm_received{result=ignored,reason=no_cascade}" not in counters


async def test_metrics_disabled_is_noop(
    db_session: AsyncSession,
    seeded: SimpleNamespace,
    low_confidence: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(metrics, "get_settings", lambda: SimpleNamespace(metrics_enabled=False))

    async with registered(seeded.device_id) as (conn, _ws):
        await media_bridge.start_identity_cascade(
            conn, seeded.identity, audio_bytes=b"fake-audio-frame", session=db_session
        )
        ask = only_type(drain(conn), "identity_ask")
        await media_bridge.handle_identity_confirm(
            conn,
            {"type": "identity_confirm", "ask_id": ask["ask_id"], "elder_id": str(seeded.elder_id)},
        )

    snapshot = metrics.metrics_snapshot()
    assert snapshot["enabled"] is False
    assert snapshot["counters"] == {}
    assert snapshot["timings"] == {}


# ---------------------------------------------------------------------------
# 9. P1 回归：重连竞态（归属守卫）与二进制帧（不许打穿成 1011）
# ---------------------------------------------------------------------------


async def test_old_connection_close_keeps_new_cascade(
    db_session: AsyncSession, seeded: SimpleNamespace, low_confidence: None
) -> None:
    """P1-1：旧连接的 finally 迟到时，不得删掉新连接刚建的那条级联。

    时序：同设备重连 → 新连接已接管（新 ask_id 已下发）→ 旧连接的 finally 这时才跑。
    若无归属守卫，`cancel_identity_cascade(device_id)` 会把新连接的 30s 定时器一起
    取消，新连接的确认点下去永远等不到 `identity_result`。
    """
    async with registered(seeded.device_id) as (old_conn, _old_ws):
        await media_bridge.start_identity_cascade(
            old_conn, seeded.identity, audio_bytes=b"fake-audio-frame", session=db_session
        )
        old_ask = only_type(drain(old_conn), "identity_ask")

        # 同设备新连接接管（旧连接还没走 finally，两条级联短暂共存）
        async with registered(seeded.device_id) as (new_conn, _new_ws):
            await media_bridge.start_identity_cascade(
                new_conn, seeded.identity, audio_bytes=b"fake-audio-frame", session=db_session
            )
            new_ask = only_type(drain(new_conn), "identity_ask")
            assert new_ask["ask_id"] != old_ask["ask_id"]

            cascade = media_bridge.active_cascade(seeded.device_id)
            assert cascade is not None
            assert cascade.owner is new_conn
            assert cascade.ask_id == new_ask["ask_id"]

            # 旧连接的 finally 此刻才跑：必须整条跳过
            await media_bridge.cancel_identity_cascade(seeded.device_id, conn=old_conn)

            survivor = media_bridge.active_cascade(seeded.device_id)
            assert survivor is cascade  # 没被 pop
            assert survivor.timeout_task is not None
            assert not survivor.timeout_task.done()  # 30s 定时器还活着

            # 新连接的确认依然有效（证明它那条级联没被清掉状态）
            await media_bridge.handle_identity_confirm(
                new_conn,
                {
                    "type": "identity_confirm",
                    "ask_id": new_ask["ask_id"],
                    "elder_id": str(seeded.elder_id),
                },
            )
            assert only_type(drain(new_conn), "identity_result")["source"] == "confirm"

            # 归属匹配时仍然要能清干净（守卫生效但不误伤正常清理）
            await media_bridge.cancel_identity_cascade(seeded.device_id, conn=new_conn)
            assert media_bridge.active_cascade(seeded.device_id) is None

    open_rows = await open_threads_of(db_session, seeded.device)
    assert len(open_rows) == 1 and open_rows[0].identity_source == "confirm"
    # 旧连接那条 provisional（被新连接 supersede 时结束）不得残留成 open
    assert all(
        row.ended_at is not None
        for row in await threads_of(db_session, seeded.device)
        if row not in open_rows
    )


async def test_binary_frame_after_auth_keeps_connection(
    seeded: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    """P1-2：已认证连接收到二进制帧 → 回一条 error，继续听，绝不 1011 关连接。

    Starlette 的 `receive_text()` 对二进制帧抛 KeyError；不接就冒到 ASGI 层，
    Uvicorn 以 1011 关闭。C2 里没有「二进制消息」类型，按非法帧处理（error internal）。
    """
    caplog.set_level(logging.INFO, logger="app.audio.signaling")

    async with registered(seeded.device_id, frames=[b"\x00\x01bin", {"type": "ping"}]) as (
        conn,
        ws,
    ):
        with pytest.raises(WebSocketDisconnect):
            await signaling._read_loop(conn)

        messages = drain(conn)
        # 二进制帧之后还能继续处理后续帧（ping→pong 排在 error 之后）
        assert [m["type"] for m in messages] == ["error", "pong"]
        assert messages[0]["code"] == "internal"
        assert ws.close_codes == []  # 没有任何 close(1011)

    record = next(r for r in caplog.records if r.message == "ws_bad_message")
    assert record.reason == "binary_frame"


async def test_binary_frame_before_auth_rejected_with_4401(db_session: AsyncSession) -> None:
    """P1-2（更危险的那条）：**未认证**客户端发一帧二进制即可打穿连接（1011）。

    auth 前非法帧的 C2 口径 = `auth_fail{code=invalid}` + 4401，不是 1011。
    """
    ws = _FakeWebSocket(frames=[b"\x00\x01bin"])
    conn = signaling.ConnectionContext(
        trace_id="trace-identity-test", websocket=cast(Any, ws), device_id=None
    )

    identity = await signaling._authenticate(conn, db_session)

    assert identity is None
    assert ws.sent == [{"type": "auth_fail", "code": "invalid"}]
    assert ws.close_codes == [signaling.WS_CLOSE_AUTH_FAILED]  # 4401


# ---------------------------------------------------------------------------
# 10. vp_unavailable：声纹「本进程配置坏掉」时用户唯一的出口
# ---------------------------------------------------------------------------


def _break_voiceprint_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """让声纹比对在「解密已注册样本」那一步抛 `VoiceprintUnavailable`。

    真实故障链：`cosine_compare_against_elder` → `decrypt_embedding` →
    `_get_fernet()`；`voiceprint_fernet_key` 缺失/非法时抛 `VoiceprintUnavailable`
    （`voiceprint_service.py:69-77`，就是那个让语音身份静默降级成 generic 的 P0）。
    这条异常**不在** `identity_service` 的捕获表里（它只捕
    `VoiceprintExtractUnavailable`），所以一路冒到 `media_bridge` 的
    `except VoiceprintUnavailable`，由级联层决定「问一句」还是「降 generic」。

    为什么不在测试里直接 patch `_get_fernet`：真实顺序是「先容器抽 embedding、
    再解密样本」（`voiceprint_service.py:308-327`），测试环境没有推理容器，第一步
    就抛 `VoiceprintExtractUnavailable` 了，永远走不到 Fernet。所以在同一个异常
    边界（`cosine_compare_against_elder` 出口）注入 —— 覆盖的是真实分支，不是
    另一种实现。
    """

    async def _raising(session: AsyncSession, elder_id: UUID, audio: bytes) -> vs.CosineMatch:
        raise vs.VoiceprintUnavailable("voiceprint_fernet_key 未配置或非法")

    monkeypatch.setattr(identity_service.vs, "cosine_compare_against_elder", _raising)


async def test_vp_unavailable_bound_device_asks_then_confirm_opens_thread(
    db_session: AsyncSession, seeded: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已绑长者 + 声纹配置坏：必须问一句「是您吗」，点确认后正常落 thread。

    这是配置坏掉时的**唯一出口**：静默 generic 会让「声纹没配好」表现成
    「系统不认识您」，长者连点一下的机会都没有。
    """
    _break_voiceprint_config(monkeypatch)

    async with registered(seeded.device_id) as (conn, ws):
        cascade = await media_bridge.start_identity_cascade(
            conn, seeded.identity, audio_bytes=b"fake-audio-frame", session=db_session
        )

        messages = drain(conn)
        assert [m["type"] for m in messages] == ["identity_ask"]
        ask = messages[0]
        assert ask["reason"] == "vp_unavailable"  # C5 闭集里的第二个值
        assert ask["timeout_s"] == 30
        assert ask["candidates"] == [
            {"elder_id": str(seeded.elder_id), "display_name": _ELDER_NAME_MASKED}
        ]
        assert cascade is not None
        assert cascade.resolved is True and cascade.confirm_pending is True  # 已能对话，等人点确认
        provisional = await open_threads_of(db_session, seeded.device)
        assert len(provisional) == 1 and provisional[0].identity_source == "generic"

        await media_bridge.handle_identity_confirm(
            conn,
            {
                "type": "identity_confirm",
                "ask_id": ask["ask_id"],
                "elder_id": str(seeded.elder_id),
            },
        )

        follow = drain(conn)
        assert [m["type"] for m in follow] == ["identity_result", "tts_text"]
        assert follow[0] == {
            "type": "identity_result",
            "source": "confirm",
            "elder_id": str(seeded.elder_id),
        }
        assert follow[1]["degraded"] is False
        assert ws.close_codes == []

    rows = await threads_of(db_session, seeded.device)
    assert {row.identity_source for row in rows} == {"generic", "confirm"}
    open_rows = [row for row in rows if row.ended_at is None]
    assert len(open_rows) == 1
    assert open_rows[0].identity_source == "confirm"
    assert open_rows[0].elder_id == seeded.elder_id


async def test_vp_unavailable_unbound_device_falls_back_to_generic(
    db_session: AsyncSession, seeded: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未绑长者 + 声纹配置坏：没有可确认的人，只能 generic，且不发 identity_ask。

    发 ask 就等于「让长者确认一个系统并不知道是谁的身份」，点下去也落不了 thread。

    注入点比上一个用例更外层（直接让 `verify_session_identity` 抛）：设备未绑长者时
    服务层在 `resolve_device_elder` 就返回了（`identity_service.py:191-206`），
    **根本走不到声纹调用**，所以真实链路上这个组合只会得到正常的 generic。这里
    必须让服务层自己抛，才能驱动级联层 `except VoiceprintUnavailable` 里
    「重查绑定 → 无绑定 → generic」那条分支（也是唯一能覆盖它的方式）。
    """

    async def _raising(
        session: AsyncSession, *, device_id: UUID, audio_bytes: bytes | None = None
    ) -> None:
        raise vs.VoiceprintUnavailable("voiceprint_fernet_key 未配置或非法")

    monkeypatch.setattr(identity_service, "verify_session_identity", _raising)

    # 解绑：设备没有绑定长者
    seeded.device.elder_id = None
    await db_session.flush()

    async with registered(seeded.device_id) as (conn, ws):
        cascade = await media_bridge.start_identity_cascade(
            conn, seeded.identity, audio_bytes=b"fake-audio-frame", session=db_session
        )

        messages = drain(conn)
        assert [m["type"] for m in messages] == ["identity_result", "tts_text"]
        assert messages[0] == {"type": "identity_result", "source": "generic", "elder_id": None}
        assert messages[1]["text"] == media_bridge.GENERIC_GREETING_TEXT
        assert messages[1]["degraded"] is False
        assert ws.close_codes == []

        assert cascade is not None
        assert cascade.resolved is True
        assert cascade.source == "generic"

    # 兜底路径不发 ask；但必须开一条 generic thread，否则 dialogue_runtime 拿不到
    # thread_id，首句无法进入对话（P1 修复的核心）。
    rows = await threads_of(db_session, seeded.device)
    assert len(rows) == 1 and rows[0].identity_source == "generic"
    assert rows[0].elder_id is None and rows[0].ended_at is None


# ---------------------------------------------------------------------------
# 11. P1-4：重连复用 open thread（不再重复弹「请问是哪位在说话？」）
# ---------------------------------------------------------------------------


async def test_reconnect_reuses_open_thread_without_new_ask(
    db_session: AsyncSession, seeded: SimpleNamespace, low_confidence: None
) -> None:
    """第一次 auth_ok 走 ask→confirm 开 thread；第二次（无音频）直接复用，不发 ask。

    这正是断线重连的真实时序：退避 1/2/4/8/15s 每一次都会重跑首轮判定，
    没有复用就会反复弹确认，长者刚点完「是我」又要再按一次。
    """
    async with registered(seeded.device_id) as (conn, _ws):
        await media_bridge.start_identity_cascade(
            conn, seeded.identity, audio_bytes=b"fake-audio-frame", session=db_session
        )
        ask = only_type(drain(conn), "identity_ask")
        await media_bridge.handle_identity_confirm(
            conn,
            {
                "type": "identity_confirm",
                "ask_id": ask["ask_id"],
                "elder_id": str(seeded.elder_id),
            },
        )
        assert only_type(drain(conn), "identity_result")["source"] == "confirm"

    # ---- 断线重连：新连接、无音频 ----
    async with registered(seeded.device_id) as (conn2, _ws2):
        cascade = await media_bridge.start_identity_cascade(
            conn2, seeded.identity, session=db_session
        )

        messages = drain(conn2)
        # 关键断言：一条 identity_ask 都没有
        assert [m["type"] for m in messages] == ["identity_result", "tts_text"]
        assert messages[0] == {
            "type": "identity_result",
            "source": "confirm",  # 按库内 identity_source 映射回 C5 闭集
            "elder_id": str(seeded.elder_id),
        }
        assert messages[1]["degraded"] is False
        assert cascade is not None
        assert cascade.resolved is True
        assert cascade.source == "confirm"
        assert cascade.timeout_task is None  # 复用不挂 30s 定时器

        # P2-E：复用必须单独计数，否则「重连回放」会被算成用户确认/声纹命中。
        counters = metrics.metrics_snapshot()["counters"]
        assert counters["identity_thread_reused{source=confirm}"] == 1
        # result 计数包含「首次确认」+「复用回放」两条，正好说明两者必须分开看：
        # 真实确认数 = identity_result_sent − identity_thread_reused
        assert counters["identity_result_sent{source=confirm}"] == 2

    rows = await threads_of(db_session, seeded.device)
    assert len(rows) == 2  # provisional generic 已在确认时结束；复用没有新增行
    open_rows = [row for row in rows if row.ended_at is None]
    assert len(open_rows) == 1 and open_rows[0].identity_source == "confirm"


async def test_stale_open_thread_is_ended_and_identity_deferred(
    db_session: AsyncSession, seeded: SimpleNamespace, low_confidence: None
) -> None:
    """open thread 的 elder 与设备当前绑定长者不一致（换人/重绑）→ 结束旧 thread。

    D 方案后不再当场弹确认，而是先播中性问候、等第一句音频再判（`identity_pending`）。
    """
    # 1) 先让设备以 elder A 建立一段 open thread
    async with registered(seeded.device_id) as (conn, _ws):
        await media_bridge.start_identity_cascade(
            conn, seeded.identity, audio_bytes=b"fake-audio-frame", session=db_session
        )
        ask = only_type(drain(conn), "identity_ask")
        await media_bridge.handle_identity_confirm(
            conn,
            {
                "type": "identity_confirm",
                "ask_id": ask["ask_id"],
                "elder_id": str(seeded.elder_id),
            },
        )
        assert only_type(drain(conn), "identity_result")["source"] == "confirm"

    # 2) 设备重绑到 elder B（床位同步换人，候选集才会有 B 的脱敏姓名）
    elder_b = Elder(name="李秀英", status="active")
    db_session.add(elder_b)
    await db_session.flush()
    seeded.bed.elder_id = elder_b.elder_id
    seeded.device.elder_id = elder_b.elder_id
    await db_session.flush()

    # 3) 重连：旧 thread 属于 A，不能代表当前设备的 B
    async with registered(seeded.device_id) as (conn2, _ws2):
        cascade = await media_bridge.start_identity_cascade(
            conn2, seeded.identity, session=db_session
        )

        messages = drain(conn2)
        assert [m["type"] for m in messages] == ["identity_pending", "tts_text"]
        assert messages[0] == {"type": "identity_pending", "reason": "await_wake"}
        assert cascade is not None
        assert cascade.resolved is False  # 身份未定，等首句音频
        assert cascade.candidate_elder_id == str(elder_b.elder_id)  # 候选已按新绑定长者收敛

    rows = await threads_of(db_session, seeded.device)
    assert len(rows) == 2  # provisional generic + confirm，均已结束
    assert all(row.ended_at is not None for row in rows)
    confirm_row = next(row for row in rows if row.identity_source == "confirm")
    assert confirm_row.elder_id == seeded.elder_id  # 仍然是 A 那一行
    assert await open_threads_of(db_session, seeded.device) == []


async def test_expired_open_thread_is_ended_and_identity_deferred(
    db_session: AsyncSession, seeded: SimpleNamespace, low_confidence: None
) -> None:
    """open thread 超过复用时效 → 结束旧 thread，身份重新等待首句音频判定（裁决 A+D）。

    没有这道闸时的现象：早上确认过一次，晚上再登录仍然直接显示「身份 · 已确认」，
    而这一轮其实没有声纹、也没有人点过「是我」——库里那条 thread 的 `ended_at`
    永远为空（全仓只有换人/重绑一处会结束它）。
    """
    # 1) 先建立一段 open thread（confirm）
    async with registered(seeded.device_id) as (conn, _ws):
        await media_bridge.start_identity_cascade(
            conn, seeded.identity, audio_bytes=b"fake-audio-frame", session=db_session
        )
        ask = only_type(drain(conn), "identity_ask")
        await media_bridge.handle_identity_confirm(
            conn,
            {
                "type": "identity_confirm",
                "ask_id": ask["ask_id"],
                "elder_id": str(seeded.elder_id),
            },
        )
        assert only_type(drain(conn), "identity_result")["source"] == "confirm"

    # 2) 把会话起点拨到「复用窗口之外」——模拟隔了几小时才再上线
    rows = await threads_of(db_session, seeded.device)
    open_rows = [row for row in rows if row.ended_at is None]
    assert len(open_rows) == 1 and open_rows[0].identity_source == "confirm"
    window = get_settings().identity_reuse_window_seconds
    open_rows[0].started_at = datetime.now(UTC) - timedelta(seconds=window + 60)
    await db_session.flush()

    # 3) 重连：过期的身份结论不得复用，必须重新问「是哪位在说话」
    async with registered(seeded.device_id) as (conn2, _ws2):
        cascade = await media_bridge.start_identity_cascade(
            conn2, seeded.identity, session=db_session
        )
        messages = drain(conn2)
        assert [m["type"] for m in messages] == ["identity_pending", "tts_text"]
        assert messages[0] == {"type": "identity_pending", "reason": "await_wake"}
        assert cascade is not None
        assert cascade.resolved is False

    assert await open_threads_of(db_session, seeded.device) == []  # 旧 thread 全部结束
    counters = metrics.metrics_snapshot()["counters"]
    assert counters.get("identity_thread_reused{source=confirm}", 0) == 0  # 过期即不计复用


async def test_no_open_thread_defers_until_first_utterance(
    db_session: AsyncSession, seeded: SimpleNamespace
) -> None:
    """库里没有 open thread（首次上线）→ D 方案：先中性问候，等首句音频再判身份。"""
    async with registered(seeded.device_id) as (conn, _ws):
        cascade = await media_bridge.start_identity_cascade(
            conn, seeded.identity, session=db_session
        )
        messages = drain(conn)
        assert [m["type"] for m in messages] == ["identity_pending", "tts_text"]
        assert messages[0]["reason"] == "await_wake"
        assert messages[1]["text"] == media_bridge.GENERIC_GREETING_TEXT
        assert cascade is not None and cascade.resolved is False
        assert cascade.timeout_task is None  # 没弹确认层，就没有 30s 计时

    assert await threads_of(db_session, seeded.device) == []


async def test_ensure_generic_thread_lets_first_utterance_reach_dialogue(
    db_session: AsyncSession, seeded: SimpleNamespace
) -> None:
    """首句拿不到音频 / 声纹判定超时：必须补开 generic thread，这一句才有人接。

    `dialogue_runtime._resolve()` 只要 `thread_id` 为空就返回 None，首句会被
    `skipped unresolved` 吞掉。这条路径是 P1「第一句不许被吞」的最后一道兜底。
    """
    async with registered(seeded.device_id) as (conn, _ws):
        cascade = await media_bridge.start_identity_cascade(
            conn, seeded.identity, session=db_session
        )
        drain(conn)  # identity_pending + 中性问候
        assert cascade is not None and cascade.resolved is False

        await media_bridge.ensure_generic_thread(seeded.device_id)

        messages = drain(conn)
        assert [m["type"] for m in messages] == ["identity_result", "tts_text"]
        assert messages[0] == {"type": "identity_result", "source": "generic", "elder_id": None}
        assert cascade.resolved is True and cascade.source == "generic"
        assert cascade.thread_id  # dialogue_runtime 拿它才会编排这一句

    rows = await threads_of(db_session, seeded.device)
    assert len(rows) == 1 and rows[0].identity_source == "generic"
    assert rows[0].elder_id is None and rows[0].ended_at is None


async def test_first_utterance_audio_resolves_voice_and_writes_thread(
    db_session: AsyncSession,
    seeded: SimpleNamespace,
    matched_voice: None,
) -> None:
    """D 方案主线：登录只播中性问候；老人开口后拿这段音频跑声纹，命中即确认。

    这条用例钉的是「延迟判定」本身：登录那一刻一条 `identity_result` 都不许有
    （否则前端会提前显示「已确认」），必须等 `resolve_identity_from_audio()` 被
    首句音频驱动之后才出结论。
    """
    async with registered(seeded.device_id) as (conn, _ws):
        await media_bridge.start_identity_cascade(conn, seeded.identity, session=db_session)
        assert [m["type"] for m in drain(conn)] == ["identity_pending", "tts_text"]

        acted = await media_bridge.resolve_identity_from_audio(seeded.device_id, b"pcm-16k")
        assert acted is True

        messages = drain(conn)
        assert [m["type"] for m in messages] == ["identity_result", "tts_text"]
        assert messages[0] == {
            "type": "identity_result",
            "source": "voice",
            "elder_id": str(seeded.elder_id),
        }

    rows = await threads_of(db_session, seeded.device)
    assert len(rows) == 1
    assert rows[0].identity_source == "voice"
    assert rows[0].elder_id == seeded.elder_id


async def test_first_utterance_audio_low_confidence_asks_then_confirm(
    db_session: AsyncSession, seeded: SimpleNamespace, low_confidence: None
) -> None:
    """D 方案兜底：声纹没命中 → 这时候才弹确认层（而不是登录时弹）。"""
    async with registered(seeded.device_id) as (conn, _ws):
        await media_bridge.start_identity_cascade(conn, seeded.identity, session=db_session)
        drain(conn)

        assert await media_bridge.resolve_identity_from_audio(seeded.device_id, b"pcm-16k")
        ask = only_type(drain(conn), "identity_ask")
        assert ask["reason"] == "low_confidence"
        assert ask["candidates"] == [{"elder_id": str(seeded.elder_id), "display_name": "张*芬"}]

        await media_bridge.handle_identity_confirm(
            conn,
            {"type": "identity_confirm", "ask_id": ask["ask_id"], "elder_id": str(seeded.elder_id)},
        )
        assert only_type(drain(conn), "identity_result")["source"] == "confirm"

    rows = await threads_of(db_session, seeded.device)
    assert {row.identity_source for row in rows} == {"generic", "confirm"}
    open_rows = [row for row in rows if row.ended_at is None]
    assert len(open_rows) == 1 and open_rows[0].identity_source == "confirm"


async def test_resolve_identity_from_audio_is_noop_without_cascade(
    db_session: AsyncSession, seeded: SimpleNamespace
) -> None:
    """没有级联 / 空音频 → False，不得凭空再判定一次。"""
    assert await media_bridge.resolve_identity_from_audio(seeded.device_id, b"pcm") is False

    async with registered(seeded.device_id) as (conn, _ws):
        await media_bridge.start_identity_cascade(conn, seeded.identity, session=db_session)
        drain(conn)
        # 空音频（ASR 只出 partial）不许触发
        assert await media_bridge.resolve_identity_from_audio(seeded.device_id, b"") is False


# ---------------------------------------------------------------------------
# 12. P1-A：生产路径（session=None）自开短会话
# ---------------------------------------------------------------------------


async def test_production_path_self_opens_session_and_confirm_writes_thread(
    db_conn: AsyncConnection,
    db_session: AsyncSession,
    seeded: SimpleNamespace,
    low_confidence: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`session=None`（生产口径）时，级联每次操作自开短会话，confirm 仍能落 thread。

    P1-A：端点曾把请求级 session 传进来，注入分支恒命中 → 自开短会话成了死代码，
    而那个会话在端点 teardown 后已关闭，confirm / 30s 定时器却还在用它。
    """

    def _factory() -> AsyncSession:
        # 生产路径每次现开一条会话；这里绑到用例事务上，好让写入可回滚。
        return AsyncSession(bind=db_conn, expire_on_commit=False)

    monkeypatch.setattr(media_bridge, "session_factory", _factory)

    async with registered(seeded.device_id) as (conn, _ws):
        cascade = await media_bridge.start_identity_cascade(
            conn, seeded.identity, audio_bytes=b"fake-audio-frame"
        )
        assert cascade is not None
        assert cascade.session is None  # 生产口径：没有注入会话

        ask = only_type(drain(conn), "identity_ask")
        await media_bridge.handle_identity_confirm(
            conn,
            {
                "type": "identity_confirm",
                "ask_id": ask["ask_id"],
                "elder_id": str(seeded.elder_id),
            },
        )
        assert only_type(drain(conn), "identity_result")["source"] == "confirm"

    rows = await threads_of(db_session, seeded.device)
    assert {row.identity_source for row in rows} == {"generic", "confirm"}
    open_rows = [row for row in rows if row.ended_at is None]
    assert len(open_rows) == 1 and open_rows[0].identity_source == "confirm"


# ---------------------------------------------------------------------------
# 13. P1-B：兜底分支自己失败，也必须有 generic 结果
# ---------------------------------------------------------------------------


async def test_vp_unavailable_fallback_failure_still_emits_generic(
    db_session: AsyncSession,
    seeded: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`VoiceprintUnavailable` 后补兜底时 DB 又抖：不许穿出，仍要发 generic 结果。

    P1-B：兜底块里两个 await 原先在 `except Exception` 的 try 之外，一次 DB 抖动
    会让 `start_identity_cascade` 抛错 → WS 1011 断开，且既无 identity_result
    也无 greeting，长者屏幕直接白等。
    """

    async def _raise_config(*args: object, **kwargs: object) -> object:
        raise vs.VoiceprintUnavailable("FERNET_KEY 未配置")

    async def _raise_db(*args: object, **kwargs: object) -> object:
        raise RuntimeError("DB 抖动")

    monkeypatch.setattr(identity_service, "verify_session_identity", _raise_config)
    monkeypatch.setattr(identity_service, "resolve_device_elder", _raise_db)

    async with registered(seeded.device_id) as (conn, _ws):
        cascade = await media_bridge.start_identity_cascade(
            conn, seeded.identity, audio_bytes=b"fake-audio-frame", session=db_session
        )

        messages = drain(conn)
        assert [m["type"] for m in messages] == ["identity_result", "tts_text"]
        assert messages[0] == {"type": "identity_result", "source": "generic", "elder_id": None}
        assert messages[1]["text"] == media_bridge.GENERIC_GREETING_TEXT
        assert cascade is not None and cascade.resolved is True
        assert cascade.source == "generic"


async def test_confirm_thread_failure_keeps_provisional_dialogue_alive(
    db_session: AsyncSession,
    seeded: SimpleNamespace,
    low_confidence: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """确认落库失败：沿用 provisional generic，不能让会话停在确认页后无人可答。"""

    async def _boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("DB 抖动")

    monkeypatch.setattr(identity_service, "open_thread_from_confirm", _boom)

    async with registered(seeded.device_id) as (conn, _ws):
        await media_bridge.start_identity_cascade(
            conn, seeded.identity, audio_bytes=b"fake-audio-frame", session=db_session
        )
        ask = only_type(drain(conn), "identity_ask")
        cascade = media_bridge.active_cascade(seeded.device_id)
        assert cascade is not None
        provisional_id = cascade.provisional_thread_id
        assert provisional_id is not None

        await media_bridge.handle_identity_confirm(
            conn,
            {"type": "identity_confirm", "ask_id": ask["ask_id"], "elder_id": str(seeded.elder_id)},
        )

        messages = drain(conn)
        assert [m["type"] for m in messages] == ["identity_result", "tts_text"]
        assert messages[0] == {"type": "identity_result", "source": "generic", "elder_id": None}
        assert cascade.source == "generic"
        assert cascade.thread_id == provisional_id  # 仍指向那条 open thread

    open_rows = await open_threads_of(db_session, seeded.device)
    assert len(open_rows) == 1 and open_rows[0].thread_id == provisional_id


# ---------------------------------------------------------------------------
# 14. P2-C：supersede 窄窗口（cancel 早于装 task）
# ---------------------------------------------------------------------------


async def test_superseded_cascade_does_not_fire_timeout(
    db_session: AsyncSession,
    seeded: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧级联在「已写 `_cascades`、未装 `timeout_task`」的窗口被新连接取代。

    此时 `cancel_identity_cascade` 看到的 `timeout_task` 还是 None 直接 return；
    若装完 task 不自检归属，这条定时器就再没人能取消，30s 后会替新连接写一行
    generic thread 并把 `identity_result{generic}` 推给新连接。
    """
    monkeypatch.setattr(media_bridge, "IDENTITY_ASK_TIMEOUT_S", 0.05)

    async with registered(seeded.device_id) as (conn, _ws):
        # 新连接接管后的级联（registry 里现在放的是它）
        newer = media_bridge._Cascade(device_id=seeded.device_id, trace_id="trace-new", owner=conn)
        media_bridge._cascades[seeded.device_id] = newer
        # 旧连接那条：窗口期状态 —— 还不在 registry 里，timer 尚未装
        stale = media_bridge._Cascade(device_id=seeded.device_id, trace_id="trace-old", owner=conn)

        await media_bridge._apply_confirm_request(
            stale,
            seeded.identity,
            identity_service.IdentityDecision(
                source="confirm",
                elder_id=seeded.elder_id,
                thread_id=None,
                reason="low_confidence",
            ),
            db_session,
        )

        assert only_type(drain(conn), "identity_ask")  # ask 已发出（旧连接期间）
        assert stale.timeout_task is None  # 自检发现被取代 → 立刻撤掉
        assert newer.timeout_task is None  # 没有误伤接管者

        # 过掉原定时器时点：不取消的话这里会多出 identity_result{generic}
        await asyncio.sleep(0.15)
        assert [m["type"] for m in drain(conn)] == []

    rows = await threads_of(db_session, seeded.device)
    assert len(rows) == 1 and rows[0].identity_source == "generic"
    assert rows[0].ended_at is not None  # provisional 当场结束，不留 open 会话
    assert await open_threads_of(db_session, seeded.device) == []


def test_greeting_caption_matches_pcm_duration() -> None:
    """开场白字幕计划：16kHz mono s16le → 32 字节 1ms，119040B = 3720ms。

    开场白音频走「系统播报」，前端拿不到 reply 流水线的 `tts_segments`；
    这个计划让开场白也能逐字出字幕，且时间轴对齐首帧出声。
    """
    plan = media_bridge.greeting_caption("您好呀，我是小满。", 119040)

    assert plan["type"] == "tts_segments"
    segments = plan["segments"]
    assert isinstance(segments, list) and len(segments) == 1
    assert segments[0]["text"] == "您好呀，我是小满。"
    assert segments[0]["start_ms"] == 0
    assert segments[0]["duration_ms"] == 3720


def test_greeting_caption_never_zero_duration() -> None:
    """极短音频（或空 PCM）也必须 ≥1ms：0 会让前端 `pickSegmentIndex` 落到 -1。"""
    plan = media_bridge.greeting_caption("嗯", 3)

    segments = plan["segments"]
    assert isinstance(segments, list)
    assert segments[0]["duration_ms"] == 1


def test_greeting_caption_carries_char_offsets_when_pcm_given() -> None:
    """给了 PCM 就带 `char_ms`：开场白与回复走同一份句内坐标，打字机不必另起时钟。"""
    text = "您好呀，我是小满。"
    pcm = b"\x00" * 119040  # 3720ms 静音（Mock provider 形态）
    plan = media_bridge.greeting_caption(text, len(pcm), pcm=pcm)

    segments = plan["segments"]
    assert isinstance(segments, list)
    offsets = segments[0]["char_ms"]
    assert len(offsets) == len(text)
    assert offsets == sorted(offsets)
    assert offsets[-1] == segments[0]["duration_ms"] == 3720


def test_greeting_caption_without_pcm_omits_char_offsets() -> None:
    """没有 PCM（估算时长那条降级路径）就不发 `char_ms`，前端回退匀速比例法。"""
    plan = media_bridge.greeting_caption("您好呀，我是小满。", 119040)

    segments = plan["segments"]
    assert isinstance(segments, list)
    assert "char_ms" not in segments[0]


async def test_recheck_skips_when_elder_has_no_voiceprint_samples(
    db_session: AsyncSession,
    seeded: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未注册声纹的长者确认身份后不该每句话都被追问「是我」。

    `cosine_compare_against_elder` 在零样本时返回 score=0 且 enrolled=False；
    复检若只看分数会判成「疑似换人」，这里钉住 skipped 分支。
    """

    async def _no_samples(session: AsyncSession, elder_id: UUID, audio: bytes) -> vs.CosineMatch:
        return vs.CosineMatch(
            matched=False, score=0.0, threshold=0.55, elder_id=None, enrolled=False
        )

    monkeypatch.setattr(identity_service.vs, "cosine_compare_against_elder", _no_samples)

    decision = await identity_service.open_thread_from_confirm(
        db_session,
        device_id=seeded.device.device_id,
        confirmed_elder_id=seeded.elder_id,
        candidate_elder_id=seeded.elder_id,
    )
    result = await identity_service.recheck_session_identity(
        db_session,
        device_id=seeded.device.device_id,
        current_thread_id=decision.thread_id,
        audio_bytes=b"fake-audio-frame",
    )

    assert result.status == "skipped"
    thread = (
        await db_session.execute(
            select(ConversationThread).where(ConversationThread.thread_id == decision.thread_id)
        )
    ).scalar_one()
    assert thread.ended_at is None
