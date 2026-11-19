"""用药触达投递（`app/audio/medication_delivery.py`）的测试（task-4 验收）。

三组断言，分别对应任务卡的三条口径：

1. **话术**：第 1 枪说全「吃什么/吃多少/怎么确认」，四枪逐次变短且不超过 60 字，
   不出现诊断词；快照缺字段或 `attempt_no` 越界一律 `MedicationValidationError`
   （宁可拒发，也不要对着空药名广播）。
2. **设备**：无绑定设备、设备 `locked` 都回 `none`，且**不发起外呼**——这两种情况
   外呼注定失败，白白占一路连接还会在日志里制造假故障。
3. **结果映射**：`ok` → `tts_finished`；建连失败但降级文本送达 → `card_rendered`；
   两路都没到 / 随便哪里抛异常 → `none`，且异常不许冒泡（调度循环不能被投递打断）。

真实库部分沿用 `tests/test_scheduler_medication.py` 的隔离策略：`NullPool` 引擎 →
一条真实连接 → 外层事务 → `join_transaction_mode="create_savepoint"` 的 session 工厂，
用完整体 rollback，主库零残留。
"""

from __future__ import annotations

import datetime
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from tests.conftest import require_db_connection

from app.audio import medication_delivery, outbound, webrtc
from app.core.config import get_settings
from app.models import Elder
from app.models.organization import Device, Room
from app.scheduler.medication import MedicationTouchTarget
from app.services import medication_service

DELIVERY_LOGGER = "app.audio.medication_delivery"
DRUG_NAME = "氨氯地平"
DOSE = "1 片"
DRUG_SNAPSHOT: dict[str, object] = {"drug_name": DRUG_NAME, "dose": DOSE}


# ---------------------------------------------------------------------------
# 夹具与假实现
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    """独立 NullPool 引擎：连测试库，用完即断，不污染共享连接池。"""
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """一条真实连接 + 外层事务；所有写入随最后的 rollback 消失。"""
    conn = await require_db_connection(db_engine)
    trans = await conn.begin()
    session = AsyncSession(bind=conn, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
        await trans.rollback()
        await conn.close()


@pytest.fixture
async def session_factory(db: AsyncSession) -> async_sessionmaker[AsyncSession]:
    """投递函数自己开关会话，所以给它一个「commit 只释放 savepoint」的工厂。"""
    return async_sessionmaker(
        bind=db.bind,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )


@dataclass
class RecordingSpeaker:
    """假的 `call_and_speak`：记下实参，回放一条预置结果（或抛预置异常）。"""

    result: outbound.OutboundResult | None = None
    error: BaseException | None = None
    calls: list[dict[str, object]] = field(default_factory=list)

    async def __call__(
        self,
        *,
        device_id: str,
        text: str,
        offerer: outbound.Offerer,
        sender: outbound.Sender,
    ) -> outbound.OutboundResult:
        self.calls.append(
            {"device_id": device_id, "text": text, "offerer": offerer, "sender": sender}
        )
        if self.error is not None:
            raise self.error
        assert self.result is not None, "假 speaker 必须给 result 或 error"
        return self.result


def _result(
    *, ok: bool, rtc_result: str, degraded_text_sent: bool = False
) -> outbound.OutboundResult:
    """构造一条外呼结果（成功时 `degraded=False`，与生产口径一致）。"""
    return outbound.OutboundResult(
        ok=ok,
        rtc_result=rtc_result,
        degraded=not ok,
        reason=None if ok else "测试原因",
        elapsed_ms=7,
        degraded_text_sent=degraded_text_sent,
    )


def _target(
    *,
    elder_id: uuid.UUID | None = None,
    attempt_no: int = 1,
    drug_snapshot: dict[str, object] | None = None,
) -> MedicationTouchTarget:
    return MedicationTouchTarget(
        reminder_id=uuid.uuid4(),
        plan_id=uuid.uuid4(),
        elder_id=elder_id or uuid.uuid4(),
        scheduled_at=datetime.datetime.now(datetime.UTC),
        attempt_no=attempt_no,
        drug_snapshot=DRUG_SNAPSHOT if drug_snapshot is None else drug_snapshot,
    )


async def _seed_device(
    db: AsyncSession, *, status: str, with_device: bool = True
) -> tuple[uuid.UUID, uuid.UUID | None]:
    """一位长者（可选）一台设备；返回 `(elder_id, device_id 或 None)`。"""
    elder = Elder(name="张桂芬", status="active")
    db.add(elder)
    await db.flush()
    if not with_device:
        return elder.elder_id, None
    room = Room(room_no=f"A-{uuid.uuid4().hex[:6]}", status="available")
    db.add(room)
    await db.flush()
    device = Device(
        room_id=room.room_id,
        elder_id=elder.elder_id,
        token_hash="t" * 64,
        status=status,
    )
    db.add(device)
    await db.flush()
    return elder.elder_id, device.device_id


# ---------------------------------------------------------------------------
# 话术（纯函数）
# ---------------------------------------------------------------------------


def test_first_attempt_states_drug_dose_and_both_confirm_paths() -> None:
    """第 1 枪必须一次说清三件事：药名、剂量、怎么确认（语音或按钮）。"""
    text = medication_service.reminder_speech_text(DRUG_SNAPSHOT, 1)
    assert DRUG_NAME in text
    assert DOSE in text
    assert "吃了" in text
    assert "已服用" in text
    assert len(text) <= medication_service.REMINDER_SPEECH_MAX_LENGTH


def test_later_attempts_get_shorter() -> None:
    """四枪逐次变短：越晚听到的句子越短，长者才抓得住重点。"""
    texts = [medication_service.reminder_speech_text(DRUG_SNAPSHOT, n) for n in (1, 2, 3, 4)]
    lengths = [len(t) for t in texts]
    assert lengths[0] > lengths[1] > lengths[2] > lengths[3]
    assert all(0 < length <= medication_service.REMINDER_SPEECH_MAX_LENGTH for length in lengths)
    # 最后一枪仍要给出确认入口，否则只剩「催」没有「怎么办」。
    assert "已服用" in texts[3]


def test_speech_text_has_no_diagnosis_wording() -> None:
    """AGENTS §4 禁区：话术不碰诊断口径，哪怕药名像是降压药。"""
    diagnosis_terms = ("高血压", "低血压", "糖尿病", "冠心病", "心梗", "脑梗", "中风")
    for attempt_no in (1, 2, 3, 4):
        text = medication_service.reminder_speech_text(DRUG_SNAPSHOT, attempt_no)
        for term in diagnosis_terms:
            assert term not in text


def test_long_drug_name_is_truncated_but_keeps_confirm_hint() -> None:
    """药名可能来自导入文件的 100 字复方长串：截药名、保住「已服用」。"""
    snapshot = {"drug_name": "氨氯地平" * 20, "dose": DOSE}
    text = medication_service.reminder_speech_text(snapshot, 1)
    assert len(text) <= medication_service.REMINDER_SPEECH_MAX_LENGTH
    assert "已服用" in text


def test_missing_dose_does_not_leave_dangling_comma() -> None:
    """没有剂量就别拼出「药名，。」这种半句话。"""
    text = medication_service.reminder_speech_text({"drug_name": DRUG_NAME}, 1)
    assert "，。" not in text
    assert DRUG_NAME in text


@pytest.mark.parametrize("attempt_no", [0, -1, 99])
def test_out_of_range_attempt_no_is_rejected(attempt_no: int) -> None:
    with pytest.raises(medication_service.MedicationValidationError):
        medication_service.reminder_speech_text(DRUG_SNAPSHOT, attempt_no)


def test_attempt_no_above_configured_limit_is_rejected() -> None:
    """上限跟随配置（`medication_reminder_attempts`），不写死 4。"""
    upper = medication_service.max_attempts()
    assert medication_service.reminder_speech_text(DRUG_SNAPSHOT, upper)
    with pytest.raises(medication_service.MedicationValidationError):
        medication_service.reminder_speech_text(DRUG_SNAPSHOT, upper + 1)


@pytest.mark.parametrize("snapshot", [{}, {"drug_name": ""}, {"drug_name": "  "}, {"dose": DOSE}])
def test_snapshot_without_drug_name_is_rejected(snapshot: dict[str, object]) -> None:
    with pytest.raises(medication_service.MedicationValidationError):
        medication_service.reminder_speech_text(snapshot, 1)


def test_non_mapping_snapshot_is_rejected() -> None:
    with pytest.raises(medication_service.MedicationValidationError):
        medication_service.reminder_speech_text(["氨氯地平"], 1)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 设备查询与回执（真库）
# ---------------------------------------------------------------------------


async def test_no_device_bound_returns_none_without_calling_out(
    db: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """设备也没配的长者：回 `none`、只记一条 info、绝不出呼。"""
    elder_id, _ = await _seed_device(db, status="offline", with_device=False)
    speaker = RecordingSpeaker(result=_result(ok=True, rtc_result="ok"))
    deliver = medication_delivery.build_medication_deliver(
        session_factory=session_factory, speaker=speaker
    )

    with caplog.at_level(logging.INFO, logger=DELIVERY_LOGGER):
        receipt = await deliver(_target(elder_id=elder_id))

    assert receipt == medication_delivery.RECEIPT_NONE
    assert speaker.calls == []
    records = [r for r in caplog.records if r.name == DELIVERY_LOGGER]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    assert records[0].__dict__.get("reason") == medication_delivery.REASON_NO_DEVICE


async def test_locked_device_returns_none_without_calling_out(
    db: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """锁定态设备（护工停用/维护）接通了也不是本人，直接跳过。"""
    elder_id, device_id = await _seed_device(db, status="locked")
    assert device_id is not None
    speaker = RecordingSpeaker(result=_result(ok=True, rtc_result="ok"))
    deliver = medication_delivery.build_medication_deliver(
        session_factory=session_factory, speaker=speaker
    )

    with caplog.at_level(logging.INFO, logger=DELIVERY_LOGGER):
        receipt = await deliver(_target(elder_id=elder_id))

    assert receipt == medication_delivery.RECEIPT_NONE
    assert speaker.calls == []
    reasons = [r.__dict__.get("reason") for r in caplog.records if r.name == DELIVERY_LOGGER]
    assert medication_delivery.REASON_DEVICE_LOCKED in reasons


async def test_successful_call_returns_tts_finished_and_wires_offerer(
    db: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """`ok=True` → `tts_finished`；同时校验实参：设备 id、话术、offerer/sender 类型。"""
    elder_id, device_id = await _seed_device(db, status="offline")
    assert device_id is not None
    speaker = RecordingSpeaker(result=_result(ok=True, rtc_result="ok"))
    deliver = medication_delivery.build_medication_deliver(
        session_factory=session_factory, speaker=speaker
    )

    receipt = await deliver(_target(elder_id=elder_id))

    assert receipt == medication_delivery.RECEIPT_TTS_FINISHED
    assert len(speaker.calls) == 1
    call = speaker.calls[0]
    assert call["device_id"] == str(device_id)
    assert call["text"] == medication_service.reminder_speech_text(DRUG_SNAPSHOT, 1)
    assert isinstance(call["offerer"], webrtc.SignalingOfferer)
    assert isinstance(call["sender"], outbound.SignalingSender)


async def test_degraded_text_delivered_returns_card_rendered(
    db: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """建连失败但降级文本送达 → `card_rendered`（PRD line 314 的降级成功）。"""
    elder_id, _ = await _seed_device(db, status="offline")
    speaker = RecordingSpeaker(
        result=_result(ok=False, rtc_result="rtc_failed", degraded_text_sent=True)
    )
    deliver = medication_delivery.build_medication_deliver(
        session_factory=session_factory, speaker=speaker
    )

    assert await deliver(_target(elder_id=elder_id)) == medication_delivery.RECEIPT_CARD_RENDERED


async def test_degraded_text_failed_returns_none(
    db: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """两路都没送到 → `none`，让调度照常重试/升级，不能假装送达。"""
    elder_id, _ = await _seed_device(db, status="offline")
    speaker = RecordingSpeaker(
        result=_result(ok=False, rtc_result="no_answer", degraded_text_sent=False)
    )
    deliver = medication_delivery.build_medication_deliver(
        session_factory=session_factory, speaker=speaker
    )

    assert await deliver(_target(elder_id=elder_id)) == medication_delivery.RECEIPT_NONE


async def test_speaker_exception_is_swallowed_and_does_not_leak_text(
    db: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """投递侧异常不上抛；日志只记类型名，异常正文里的药名不能进日志。"""
    elder_id, _ = await _seed_device(db, status="offline")
    speaker = RecordingSpeaker(error=RuntimeError(f"boom {DRUG_NAME}"))
    deliver = medication_delivery.build_medication_deliver(
        session_factory=session_factory, speaker=speaker
    )

    with caplog.at_level(logging.INFO, logger=DELIVERY_LOGGER):
        receipt = await deliver(_target(elder_id=elder_id))

    assert receipt == medication_delivery.RECEIPT_NONE
    records = [r for r in caplog.records if r.name == DELIVERY_LOGGER]
    warnings = [r for r in records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].__dict__.get("error_type") == "RuntimeError"
    for record in records:
        assert DRUG_NAME not in record.getMessage()
        assert DRUG_NAME not in str(record.__dict__)


async def test_unbuildable_speech_text_returns_none_and_never_calls_out(
    db: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """快照坏了（没有药名）时连外呼都不发起：别对着一片空白广播。"""
    elder_id, _ = await _seed_device(db, status="offline")
    speaker = RecordingSpeaker(result=_result(ok=True, rtc_result="ok"))
    deliver = medication_delivery.build_medication_deliver(
        session_factory=session_factory, speaker=speaker
    )

    with caplog.at_level(logging.INFO, logger=DELIVERY_LOGGER):
        receipt = await deliver(_target(elder_id=elder_id, drug_snapshot={"dose": DOSE}))

    assert receipt == medication_delivery.RECEIPT_NONE
    assert speaker.calls == []
    assert any(r.levelno == logging.WARNING for r in caplog.records if r.name == DELIVERY_LOGGER)


async def test_device_lookup_failure_returns_none(caplog: pytest.LogCaptureFixture) -> None:
    """查库失败也不许冒泡（调度循环还要写重试与升级）。"""
    speaker = RecordingSpeaker(result=_result(ok=True, rtc_result="ok"))
    # 用一个连不上的库模拟「查库就炸」，不碰任何真实表。
    engine = create_async_engine(
        "postgresql+asyncpg://nobody:nobody@127.0.0.1:1/nothing", poolclass=NullPool
    )
    broken = async_sessionmaker(bind=engine, expire_on_commit=False)
    deliver = medication_delivery.build_medication_deliver(session_factory=broken, speaker=speaker)

    try:
        with caplog.at_level(logging.INFO, logger=DELIVERY_LOGGER):
            receipt = await deliver(_target())
    finally:
        await engine.dispose()

    assert receipt == medication_delivery.RECEIPT_NONE
    assert speaker.calls == []
    assert any(
        r.levelno == logging.WARNING and r.__dict__.get("error_type")
        for r in caplog.records
        if r.name == DELIVERY_LOGGER
    )


def test_receipt_mapping_covers_all_outbound_results() -> None:
    """`receipt_for_outbound` 对五种 `rtc_result` × 是否降级送达都必须是三值之一。"""
    receipts = set()
    for rtc_result in sorted(outbound.RTC_RESULTS):
        for degraded_text_sent in (False, True):
            ok = rtc_result == outbound.RTC_OK
            receipts.add(
                medication_delivery.receipt_for_outbound(
                    _result(
                        ok=ok,
                        rtc_result=rtc_result,
                        degraded_text_sent=degraded_text_sent,
                    )
                )
            )
    assert receipts <= set(medication_service.ATTEMPT_RECEIPTS)
