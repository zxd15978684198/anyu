"""`medication_events.failure_reason` 增加 `rtc_failed` 的测试（task-4 验收）。

三个层面必须同时对得上，否则这条枚举就是「代码里能写、库里写不进」或反之：

1. **服务层闭集**：`FAILURE_REASONS` 含 `rtc_failed`，`update_event_state` 放行它、
   拒绝闭集外的值（`MedicationValidationError`，不是 500）；
2. **表定义**：ORM 的 CHECK 与迁移落地后的库内 CHECK 都认这个值——迁移用裸 SQL 手写
   约束体，写错列名在 PostgreSQL 侧语法上能过，只有真跑一次才看得出来；
3. **归因映射**：`failure_reason_for_outbound` 把五种 `rtc_result` 收敛到
   `FAILURE_REASONS` 里的值，且与 `app/audio/outbound.RTC_RESULTS` 不漂移。

真库部分沿用 `NullPool` + 外层事务 + savepoint 的隔离策略（主库零残留）。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import CheckConstraint, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from tests.conftest import require_db_connection

from app.audio import outbound
from app.core.config import get_settings
from app.models import Elder, MedicationEvent, MedicationPlan, StaffAccount
from app.services import medication_service

CONSTRAINT_NAME = "ck_medication_events_failure_reason"
DRUG_SNAPSHOT: dict[str, object] = {"drug_name": "氨氯地平", "dose": "1 片"}


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    conn = await require_db_connection(db_engine)
    trans = await conn.begin()
    session = AsyncSession(bind=conn, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
        await trans.rollback()
        await conn.close()


async def _seed_event(db: AsyncSession) -> uuid.UUID:
    """一位长者 + 一张药单 + 一条事件；返回 `reminder_id`。"""
    staff_id, elder_id, plan_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    now = datetime.datetime.now(datetime.UTC)
    await db.execute(
        StaffAccount.__table__.insert(),
        {"staff_id": staff_id, "name": "医生李", "role": "doctor", "password_hash": "x"},
    )
    await db.execute(
        Elder.__table__.insert(),
        {"elder_id": elder_id, "name": "张桂芬", "status": "active"},
    )
    await db.execute(
        MedicationPlan.__table__.insert(),
        {
            "plan_id": plan_id,
            "elder_id": elder_id,
            "drug_name": DRUG_SNAPSHOT["drug_name"],
            "dose": DRUG_SNAPSHOT["dose"],
            "schedule_cron": "08:00",
            "start_date": now.date(),
            "end_date": None,
            "version": 1,
            "created_by": staff_id,
            "status": "active",
        },
    )
    reminder_id = uuid.uuid4()
    await db.execute(
        MedicationEvent.__table__.insert(),
        {
            "reminder_id": reminder_id,
            "plan_id": plan_id,
            "elder_id": elder_id,
            "scheduled_at": now,
            "drug_snapshot": DRUG_SNAPSHOT,
            "payload_hash": medication_service.drug_snapshot_hash(DRUG_SNAPSHOT),
            "triggered_by": "scheduler",
        },
    )
    return reminder_id


# ---------------------------------------------------------------------------
# 闭集与表定义一致
# ---------------------------------------------------------------------------


def test_failure_reasons_now_has_five_values() -> None:
    """四值 → 五值；新增的是 `rtc_failed`（PRD FR-MED-06 的建连失败归因）。"""
    assert medication_service.FAILURE_REASONS == (
        "no_answer",
        "denied",
        "tts_failed",
        "offline",
        "rtc_failed",
    )


def test_model_check_constraint_mirrors_service_closed_set() -> None:
    """ORM 的 CHECK 必须与 `FAILURE_REASONS` 同集合：少一个值就是「服务放行、库里报错」。"""
    checks = [
        constraint
        for constraint in MedicationEvent.__table__.constraints
        if isinstance(constraint, CheckConstraint) and "failure_reason" in str(constraint.sqltext)
    ]
    assert len(checks) == 1, f"期望 1 条 failure_reason CHECK，实际 {len(checks)} 条"
    sqltext = str(checks[0].sqltext)
    for reason in medication_service.FAILURE_REASONS:
        assert f"'{reason}'" in sqltext
    # 反向：CHECK 里不许有闭集外的取值（否则库比代码更宽松）。
    assert sqltext.count("'") == 2 * len(medication_service.FAILURE_REASONS)


def test_rtc_results_mirror_matches_outbound_module() -> None:
    """服务层镜像的 `rtc_result` 闭集必须等于 `outbound.RTC_RESULTS`（分层不许互相 import）。"""
    assert set(medication_service.OUTBOUND_RTC_RESULTS) == set(outbound.RTC_RESULTS)
    assert len(medication_service.OUTBOUND_RTC_RESULTS) == len(
        set(medication_service.OUTBOUND_RTC_RESULTS)
    )


# ---------------------------------------------------------------------------
# 纯函数归因映射
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rtc_result", "degraded_text_sent", "device_offline", "expected"),
    [
        ("ok", False, False, None),
        ("ok", True, False, None),
        ("rtc_failed", False, False, "rtc_failed"),
        ("timeout", False, False, "rtc_failed"),
        ("no_answer", False, False, "rtc_failed"),
        ("ice_failed", False, False, "rtc_failed"),
        ("rtc_failed", True, False, "tts_failed"),
        ("timeout", True, False, "tts_failed"),
        ("rtc_failed", False, True, "offline"),
        ("ok", True, True, "offline"),
    ],
)
def test_failure_reason_mapping(
    rtc_result: str, degraded_text_sent: bool, device_offline: bool, expected: str | None
) -> None:
    """计划 §2.4 归因表逐行落地：设备离线压过建连码，降级送达归 TTS 侧。"""
    assert (
        medication_service.failure_reason_for_outbound(
            rtc_result,
            degraded_text_sent=degraded_text_sent,
            device_offline=device_offline,
        )
        == expected
    )


def test_mapping_never_leaves_closed_set() -> None:
    """映射结果必须是 `FAILURE_REASONS` 的成员或 None，否则写库会撞 CHECK。"""
    allowed = set(medication_service.FAILURE_REASONS) | {None}
    for rtc_result in medication_service.OUTBOUND_RTC_RESULTS:
        for degraded in (False, True):
            for offline in (False, True):
                mapped = medication_service.failure_reason_for_outbound(
                    rtc_result, degraded_text_sent=degraded, device_offline=offline
                )
                assert mapped in allowed


def test_unknown_rtc_result_is_rejected() -> None:
    with pytest.raises(medication_service.MedicationValidationError):
        medication_service.failure_reason_for_outbound("boom")


# ---------------------------------------------------------------------------
# 真库：CHECK 约束
# ---------------------------------------------------------------------------


async def test_rtc_failed_passes_database_check(db: AsyncSession) -> None:
    """服务层写 `rtc_failed` 必须真的落库（CHECK 已扩到五值）。"""
    reminder_id = await _seed_event(db)

    event = await medication_service.update_event_state(
        db, reminder_id=reminder_id, failure_reason="rtc_failed"
    )

    assert event.failure_reason == "rtc_failed"
    stored = (
        await db.execute(
            select(MedicationEvent.failure_reason).where(MedicationEvent.reminder_id == reminder_id)
        )
    ).scalar_one()
    assert stored == "rtc_failed"


async def test_database_constraint_definition_has_five_values(db: AsyncSession) -> None:
    """库内约束定义（迁移 0015 落地后）必须包含五个取值，而不只是代码里看着对。"""
    definition = (
        await db.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = :name AND conrelid = 'medication_events'::regclass"
            ),
            {"name": CONSTRAINT_NAME},
        )
    ).scalar_one_or_none()

    assert definition is not None, f"库里没有 {CONSTRAINT_NAME}，迁移 0015 未应用？"
    for reason in medication_service.FAILURE_REASONS:
        assert f"'{reason}'" in definition


async def test_unknown_reason_is_rejected_by_service_before_database(db: AsyncSession) -> None:
    """闭集外的值在服务层就拦下，报 4xx 语义的错误而不是让 CHECK 抛 IntegrityError。"""
    reminder_id = await _seed_event(db)

    with pytest.raises(medication_service.MedicationValidationError):
        await medication_service.update_event_state(
            db, reminder_id=reminder_id, failure_reason="bogus"
        )


async def test_database_check_still_rejects_unknown_value(db: AsyncSession) -> None:
    """绕过服务层直写也必须被 CHECK 拦住（兜底断言，故意放在最后一步）。

    放在最后一个断言是因为：约束违例会让本连接的事务作废，之后同事务里任何查询都会
    报 `InFailedSqlTransaction`。
    """
    reminder_id = await _seed_event(db)

    with pytest.raises(IntegrityError):
        await db.execute(
            update(MedicationEvent)
            .where(MedicationEvent.reminder_id == reminder_id)
            .values(failure_reason="bogus")
        )
