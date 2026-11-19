"""迭代 6 · W1：把三级路由接进实时语音主链路（`app/dialogue_runtime.py`）。

唯一依据：`设计记录（编排接线）` §3（交叉契约）、§4-W1、§5（写锁）。

在此之前路由只注入在**降级垫层**（`app/audio/webrtc.py` 的 `ReplyStub(router=wire_route)`）：
主链路 `DialogueRuntime.on_asr_final` 直接把 ASR 文本交给 `DialogueService.respond`，
`RouteDecision` 在真编排路径上从来没被产出过。本文件钉住 W1 接上的四件事：

1. `router` 被调用**恰好 1 次**且拿到本轮 `trace_id`；`RouteDecision` 原样传给
   `respond(decision=…)`（W2 的 L2 分支靠它挑实现）；
2. 有决策就落 `decision_logs` 一行，`layer` 等于 router 返回的层（真库、基线增量）；
3. router 抛异常只 warning：仍然照常回话，且 `decision_logs` 不新增；
4. `privacy` 收敛为真：`signaling.deliver` 的文本以 `PRIVACY_NOTICE_TEXT` 开头，
   且 `audit_logs` 多一行 `action="privacy_converged"`。

## 为什么用假 service / 假 signaling

本卡测的是**接线语义**，不是 `DialogueService` 的组装或 TTS 的波形。假 service 把
`respond` 收到的 kwargs 原样记下来（W2 的 `decision` 形参是否被喂到就是断言点本身）；
假 signaling 拦住下行文本。`tts=None` 让 `_deliver_audio` 直接早退，不碰 WebRTC。

## 为什么是「基线增量」而不是全表总数

`decision_logs` / `audit_logs` 都是只增表，测试库里本来就有历史行。断言一律写成
「调用前基线 + 1」，既精确到个位，又在有历史行的库上不假红（
`tests/test_privacy_convergence.py` 同款口径）。

## 两个写库入口怎么在事务里查得到

`log_decision` 走 `app.db.session_factory`，`write_audit_isolated` 走
`audit_service.session_factory`，各自开独立会话。本文件把**两个**工厂都钉到外层测试
事务上（`join_transaction_mode="create_savepoint"`）：写下的行同一事务内可查，
又随用例整体回滚、不污染库。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from tests.conftest import require_db_connection

from app import db as db_module
from app.audio import media_bridge, signaling
from app.audio.reply_stub import (
    PRIVACY_AUDIT_ACTION,
    PRIVACY_AUDIT_RESOURCE_TYPE,
    PRIVACY_NOTICE_TEXT,
)
from app.core.config import get_settings
from app.dialogue_runtime import DialogueRuntime
from app.models import AuditLog, DecisionLog
from app.router.decision import RouteDecision
from app.services import audit_service

#: 设备 / 轮次 / 会话 id：`_resolve` 只读假级联，device_id 本身不参与解析。
_DEVICE_ID = "dev-routing-1"
_TURN_ID = "turn-routing-1"
_THREAD_ID = "thread-routing-1"

#: 长者身份走生产口径：`DialogueIdentity.elder_id` 是 str（UUID 文本）。
_ELDER_UUID = uuid.UUID("6f1a1d2c-0000-4000-8000-00000000b001")
_ELDER_ID = str(_ELDER_UUID)

#: 假 service 的正文：不含标点歧义，拼接断言可以逐字比。
_REPLY_TEXT = "今天挺暖和的。"


# --------------------------------------------------------------- 测试替身


class _Cascade:
    """`media_bridge.active_cascade` 的最小可用替身（`_resolve` 只读这几个字段）。"""

    def __init__(
        self,
        *,
        thread_id: str = _THREAD_ID,
        source: str = "device",
        elder_id: str = _ELDER_ID,
        display_name: str = "王阿姨",
    ) -> None:
        self.resolved = True
        self.thread_id = thread_id
        self.source = source
        self.candidate_elder_id = elder_id
        self.candidates = ({"display_name": display_name},)


class _FakeService:
    """记录 `respond` 收到的 kwargs；返回一个只有 `.text` / `.degraded` 的假 reply。"""

    def __init__(self, *, text: str = _REPLY_TEXT, degraded: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self._text = text
        self._degraded = degraded

    async def respond(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(text=self._text, degraded=self._degraded)


class _DeliverRecorder:
    """`signaling.deliver` 的替身：记下行、总说送达（掉线路径另有既有用例覆盖）。"""

    def __init__(self, ok: bool = True) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._ok = ok

    def __call__(self, device_id: str, payload: dict[str, Any]) -> bool:
        self.calls.append((device_id, payload))
        return self._ok


class _RouterRecorder:
    """`router(text, *, elder_id, thread_id, trace_id)` 的替身：记调用、按剧本返回/抛。"""

    def __init__(
        self,
        *,
        decision: RouteDecision | None = None,
        raises: BaseException | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._decision = decision
        self._raises = raises

    async def __call__(self, text: str, **kwargs: Any) -> RouteDecision | None:
        self.calls.append((text, kwargs))
        if self._raises is not None:
            raise self._raises
        return self._decision


# --------------------------------------------------------------- 真库夹具


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    """NullPool：每个用例一个新事件循环，池内连接跨循环复用会 `Event loop is closed`。"""
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """外层事务：本文件的写入最后整体回滚。"""
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


@pytest.fixture(autouse=True)
def pinned_session_factories(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> async_sessionmaker[AsyncSession]:
    """把两个「独立会话」工厂都钉在同一条测试连接上，否则查不到刚写的行。"""
    bind = db.bind
    assert bind is not None
    factory = async_sessionmaker(
        bind=bind,  # type: ignore[arg-type]
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    monkeypatch.setattr(audit_service, "session_factory", factory)
    monkeypatch.setattr(db_module, "session_factory", factory)
    return factory


# --------------------------------------------------------------- 小工具


def _decision(
    *,
    layer: str = "L1",
    privacy: str | None = None,
    trace_id: str,
    rule_id: str = "rule_weather",
    intent: str = "weather",
) -> RouteDecision:
    return RouteDecision(
        elder_id=_ELDER_ID,
        thread_id=_THREAD_ID,
        layer=layer,
        rule_id=rule_id,
        intent=intent,
        confidence=0.93,
        privacy=privacy,
        route_ms=3,
        trace_id=trace_id,
    )


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cascade: _Cascade | None = None,
    deliver_ok: bool = True,
) -> _DeliverRecorder:
    """把身份级联与下行都换成替身（本卡不测这两处的既有语义）。"""
    active = cascade if cascade is not None else _Cascade()
    monkeypatch.setattr(media_bridge, "active_cascade", lambda device_id: active)
    delivered = _DeliverRecorder(ok=deliver_ok)
    monkeypatch.setattr(signaling, "deliver", delivered)
    return delivered


async def _decision_log_count(db: AsyncSession) -> int:
    total = await db.scalar(select(func.count()).select_from(DecisionLog))
    return int(total or 0)


async def _decision_logs_for_trace(db: AsyncSession, trace_id: str) -> list[DecisionLog]:
    result = await db.execute(select(DecisionLog).where(DecisionLog.trace_id == trace_id))
    return list(result.scalars().all())


async def _privacy_audit_count(db: AsyncSession) -> int:
    total = await db.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.action == PRIVACY_AUDIT_ACTION)
    )
    return int(total or 0)


async def _privacy_audits_for_trace(db: AsyncSession, trace_id: str) -> list[AuditLog]:
    result = await db.execute(select(AuditLog).where(AuditLog.action == PRIVACY_AUDIT_ACTION))
    return [row for row in result.scalars().all() if (row.detail or {}).get("trace_id") == trace_id]


# --------------------------------------------- ① router 调用 + decision 下传


async def test_router_called_once_and_decision_passed_to_service(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """router 恰好 1 次且拿到 trace_id；`RouteDecision` 进 `respond(decision=…)`。"""
    trace_id = "trace-routing-pass"
    before = await _decision_log_count(db)
    decision = _decision(trace_id=trace_id)
    router = _RouterRecorder(decision=decision)
    service = _FakeService()
    delivered = _wire(monkeypatch)

    runtime = DialogueRuntime(service=service, router=router)
    await runtime.on_asr_final(_DEVICE_ID, _TURN_ID, trace_id, "今天天气怎么样")

    assert len(router.calls) == 1
    text, kwargs = router.calls[0]
    assert text == "今天天气怎么样"
    assert kwargs["trace_id"] == trace_id
    assert kwargs["elder_id"] == _ELDER_ID
    assert kwargs["thread_id"] == _THREAD_ID

    assert len(service.calls) == 1
    assert service.calls[0]["decision"] is decision
    assert service.calls[0]["user_text"] == "今天天气怎么样"

    # 非隐私轮：文本原样下行；未配 TTS 时 P2-4 会再补一条可见降级提示（不碰播放轨）
    assert [payload["type"] for _, payload in delivered.calls] == ["tts_text", "error"]
    assert delivered.calls[0][1]["text"] == _REPLY_TEXT
    assert delivered.calls[0][1]["degraded"] is False
    assert delivered.calls[1][1]["code"] == "tts_unavailable"

    # 基线增量：本轮恰好落一行
    assert await _decision_log_count(db) == before + 1


# --------------------------------------------- ② decision_logs 的 layer


async def test_decision_logged_with_router_layer(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`decision_logs` 新增 1 行，且 `layer` 就是 router 返回的层（真库断言）。"""
    trace_id = "trace-routing-log"
    before = await _decision_log_count(db)
    router = _RouterRecorder(decision=_decision(layer="L2", trace_id=trace_id))
    _wire(monkeypatch)

    runtime = DialogueRuntime(service=_FakeService(), router=router)
    await runtime.on_asr_final(_DEVICE_ID, _TURN_ID, trace_id, "给我讲讲越剧")

    assert await _decision_log_count(db) == before + 1
    rows = await _decision_logs_for_trace(db, trace_id)
    assert len(rows) == 1
    assert rows[0].layer == "L2"
    assert rows[0].elder_id == _ELDER_ID
    assert rows[0].intent == "weather"


# --------------------------------------------- ③ decision 为 None 仍回话


async def test_router_returning_none_still_replies(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """router 什么都没判出来（None）：照常回话，`decision_logs` 不新增。"""
    trace_id = "trace-routing-none"
    before = await _decision_log_count(db)
    router = _RouterRecorder(decision=None)
    service = _FakeService()
    delivered = _wire(monkeypatch)

    runtime = DialogueRuntime(service=service, router=router)
    await runtime.on_asr_final(_DEVICE_ID, _TURN_ID, trace_id, "嗯……")

    assert len(router.calls) == 1
    assert len(service.calls) == 1
    assert service.calls[0]["decision"] is None
    assert delivered.calls[0][1]["text"] == _REPLY_TEXT
    assert await _decision_log_count(db) == before


async def test_no_router_configured_still_replies(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没注入 router（迭代 5 行为）：不路由、`decision=None`，仍照常回话。"""
    trace_id = "trace-routing-absent"
    before = await _decision_log_count(db)
    service = _FakeService()
    delivered = _wire(monkeypatch)

    runtime = DialogueRuntime(service=service)  # 不传 router
    await runtime.on_asr_final(_DEVICE_ID, _TURN_ID, trace_id, "几点了")

    assert len(service.calls) == 1
    assert service.calls[0]["decision"] is None
    assert delivered.calls[0][1]["text"] == _REPLY_TEXT
    assert await _decision_log_count(db) == before


# --------------------------------------------- ④ router 抛异常仍回话


async def test_router_failure_still_replies_without_decision_log(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """路由挂了：只 warning，照常回话；`decision_logs` 不新增。"""
    trace_id = "trace-routing-boom"
    before = await _decision_log_count(db)
    router = _RouterRecorder(raises=RuntimeError("laya down"))
    service = _FakeService()
    delivered = _wire(monkeypatch)

    runtime = DialogueRuntime(service=service, router=router)
    await runtime.on_asr_final(_DEVICE_ID, _TURN_ID, trace_id, "今天天气怎么样")

    assert len(router.calls) == 1
    assert len(service.calls) == 1
    assert service.calls[0]["decision"] is None
    assert delivered.calls[0][1]["text"] == _REPLY_TEXT
    assert await _decision_log_count(db) == before


# --------------------------------------------- ⑤ privacy 收敛：话术 + 审计


async def test_privacy_converged_prepends_notice_and_writes_audit(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """privacy 为真：下行文本以收敛短句开头，`audit_logs` 多一行 `privacy_converged`。"""
    trace_id = "trace-routing-privacy"
    decision = _decision(
        privacy="true", trace_id=trace_id, rule_id="privacy_double_room", intent="chitchat"
    )
    router = _RouterRecorder(decision=decision)
    delivered = _wire(monkeypatch)
    audit_before = await _privacy_audit_count(db)

    runtime = DialogueRuntime(service=_FakeService(), router=router)
    await runtime.on_asr_final(_DEVICE_ID, _TURN_ID, trace_id, "今天屋里怎么这么热闹")

    payload = delivered.calls[0][1]
    assert payload["text"].startswith(PRIVACY_NOTICE_TEXT)
    # 先短句、后正文：逐字等于「话术 + 正文」，没改写正文
    assert payload["text"] == PRIVACY_NOTICE_TEXT + _REPLY_TEXT

    assert await _privacy_audit_count(db) == audit_before + 1
    rows = await _privacy_audits_for_trace(db, trace_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.action == PRIVACY_AUDIT_ACTION
    assert row.resource_type == PRIVACY_AUDIT_RESOURCE_TYPE == "dialogue_turn"
    assert row.actor_type == "system"
    assert row.actor_id == _ELDER_UUID
    assert row.resource_id == _ELDER_UUID
    # detail 只留「怎么判出来的」，不落话术正文
    assert row.detail == {
        "trace_id": trace_id,
        "rule_id": "privacy_double_room",
        "intent": "chitchat",
    }


async def test_privacy_false_keeps_text_and_writes_no_audit(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """privacy 明确为假：不加短句、不写隐私审计（避免「宁可多说」的反向副作用）。"""
    trace_id = "trace-routing-not-privacy"
    router = _RouterRecorder(decision=_decision(privacy="false", trace_id=trace_id))
    delivered = _wire(monkeypatch)
    audit_before = await _privacy_audit_count(db)

    runtime = DialogueRuntime(service=_FakeService(), router=router)
    await runtime.on_asr_final(_DEVICE_ID, _TURN_ID, trace_id, "今天天气怎么样")

    assert delivered.calls[0][1]["text"] == _REPLY_TEXT
    assert await _privacy_audit_count(db) == audit_before
    assert await _privacy_audits_for_trace(db, trace_id) == []
