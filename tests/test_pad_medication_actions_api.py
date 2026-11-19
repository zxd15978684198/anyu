"""长者端用药三动作的接口级测试：`confirm` / `undo` / `rendered`。

对应验收：

- FR-UI-03：关键操作必须有文字按钮——平板点「已服用」不依赖语音，TTS 失败也要成功；
- FR-UI-08：普通确认 10 秒内可撤销并记审计；
- FR-UI-10 + FR-ALT-03：卡片真的画到长者眼前才算送达，回执只能由客户端发。

三条端点的共同边界（设备凭证、未绑定 403、跨长者 403、未知 404）在这里逐条钉住；
状态机细节归服务层用例，本文件只验「HTTP 把服务层结果如实翻译成响应」。

隔离方式与 `tests/test_pad_offline_api.py` 同构：`NullPool` + 外层事务 +
`get_session` 覆盖成 savepoint；TTS 用空 PCM 替身，只走降级分支，不写真音频文件。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.api import pad as pad_api
from app.api.dialogue import get_tts
from app.core.config import get_settings
from app.core.security import sha256_hex, sign_device_session, sign_staff_access
from app.core.trace import TraceIdMiddleware
from app.db import get_session
from app.models import (
    AuditLog,
    Device,
    Elder,
    MedicationAttempt,
    MedicationEvent,
    MedicationPlan,
    Room,
    StaffAccount,
)
from app.services.medication_service import MedicationEventConflict
from conftest import require_db_connection

# 测试专用密钥：只为本地用例签发令牌，不是任何环境的真实配置。
TEST_JWT_SECRET = "pad-med-actions-test-secret-not-a-real-key-0123456789"

ROOM_ID = uuid.UUID("d1d1d1d1-d1d1-4d1d-8d1d-d1d1d1d1d1d1")
ELDER_A = uuid.UUID("d2d2d2d2-d2d2-4d2d-8d2d-d2d2d2d2d2d2")
ELDER_B = uuid.UUID("d3d3d3d3-d3d3-4d3d-8d3d-d3d3d3d3d3d3")
DEVICE_A = uuid.UUID("d4d4d4d4-d4d4-4d4d-8d4d-d4d4d4d4d4d4")
DEVICE_UNBOUND = uuid.UUID("d5d5d5d5-d5d5-4d5d-8d5d-d5d5d5d5d5d5")
DOCTOR_ID = uuid.UUID("d6d6d6d6-d6d6-4d6d-8d6d-d6d6d6d6d6d6")

PLAN_A = uuid.UUID("d7d7d7d7-d7d7-4d7d-8d7d-d7d7d7d7d7d7")
PLAN_B = uuid.UUID("d8d8d8d8-d8d8-4d8d-8d8d-d8d8d8d8d8d8")
EVENT_A = uuid.UUID("d9d9d9d9-d9d9-4d9d-8999-d9d9d9d9d9d9")
EVENT_B = uuid.UUID("dadadada-dada-4ada-8ada-dadadadadada")
UNKNOWN_EVENT = uuid.UUID("dbdbdbdb-dbdb-4bdb-8bdb-dbdbdbdbdbdb")

SECRET_A = "pad-med-actions-secret-a-not-a-real-value"
SECRET_UNBOUND = "pad-med-actions-secret-unbound-not-a-real-value"

SCHEDULED = datetime(2026, 11, 16, 8, 0, tzinfo=UTC)
DRUG_SNAPSHOT = {"drug_name": "二甲双胍", "dose": "0.5g 1 片"}


class _SilentTTS:
    """返回空 PCM：`synthesize_to_static` 判 empty_pcm → 播报降级，不写文件。"""

    async def synthesize(self, text: str) -> bytes:
        return b""


@pytest.fixture
def jwt_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


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
async def client(db: AsyncConnection, jwt_env: None) -> AsyncIterator[AsyncClient]:
    application = FastAPI()
    application.add_middleware(TraceIdMiddleware)
    application.include_router(pad_api.router)

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        session = AsyncSession(
            bind=db,
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

    application.dependency_overrides[get_session] = override_get_session
    application.dependency_overrides[get_tts] = _SilentTTS
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


async def _insert_plan_and_event(
    db: AsyncConnection,
    *,
    plan_id: uuid.UUID,
    event_id: uuid.UUID,
    elder_id: uuid.UUID,
    scheduled_at: datetime = SCHEDULED,
) -> None:
    await db.execute(
        MedicationPlan.__table__.insert().values(
            plan_id=plan_id,
            elder_id=elder_id,
            drug_name="二甲双胍",
            dose="0.5g 1 片",
            schedule_cron="08:00",
            start_date=date(2026, 11, 1),
            created_by=DOCTOR_ID,
            status="active",
        )
    )
    await db.execute(
        MedicationEvent.__table__.insert().values(
            reminder_id=event_id,
            plan_id=plan_id,
            elder_id=elder_id,
            scheduled_at=scheduled_at,
            drug_snapshot=DRUG_SNAPSHOT,
            payload_hash="hash-" + event_id.hex[:12],
            triggered_by="scheduler",
            status="waiting_confirm",
        )
    )


@pytest.fixture
async def seeded(db: AsyncConnection) -> None:
    """两位长者、两台设备（绑定 A / 未绑定）、一张医生账号、两条等待确认的事件。"""
    await db.execute(
        Room.__table__.insert().values(room_id=ROOM_ID, room_no="C-301", status="full")
    )
    await db.execute(
        Elder.__table__.insert(),
        [
            {"elder_id": ELDER_A, "name": "王秀兰", "status": "active"},
            {"elder_id": ELDER_B, "name": "孙德明", "status": "active"},
        ],
    )
    await db.execute(
        StaffAccount.__table__.insert().values(
            staff_id=DOCTOR_ID,
            name="王医生",
            role="doctor",
            password_hash="not-a-real-hash",
            token_version=0,
            status="active",
        )
    )
    await db.execute(
        Device.__table__.insert(),
        [
            {
                "device_id": DEVICE_A,
                "room_id": ROOM_ID,
                "elder_id": ELDER_A,
                "token_hash": sha256_hex(SECRET_A),
                "status": "online",
            },
            {
                "device_id": DEVICE_UNBOUND,
                "room_id": ROOM_ID,
                "elder_id": None,
                "token_hash": sha256_hex(SECRET_UNBOUND),
                "status": "online",
            },
        ],
    )
    await _insert_plan_and_event(db, plan_id=PLAN_A, event_id=EVENT_A, elder_id=ELDER_A)
    await _insert_plan_and_event(db, plan_id=PLAN_B, event_id=EVENT_B, elder_id=ELDER_B)


def _device_auth(device_id: uuid.UUID = DEVICE_A, secret: str = SECRET_A) -> dict[str, str]:
    token = sign_device_session(device_id=device_id, token_hash_prefix=sha256_hex(secret)[:16])
    return {"Authorization": f"Bearer {token}"}


def _staff_auth() -> dict[str, str]:
    token = sign_staff_access(kind="staff", staff_id=DOCTOR_ID, role="doctor", token_version=0)
    return {"Authorization": f"Bearer {token}"}


async def _event_row(db: AsyncConnection, event_id: uuid.UUID) -> tuple[object, ...]:
    result = await db.execute(
        select(
            MedicationEvent.status,
            MedicationEvent.terminal_state,
            MedicationEvent.confirm_channel,
            MedicationEvent.confirmed_at,
            MedicationEvent.late_taken,
        ).where(MedicationEvent.reminder_id == event_id)
    )
    return result.one()


async def _audit_count(db: AsyncConnection, action: str, actor_id: uuid.UUID) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.action == action, AuditLog.actor_id == actor_id)
    )
    return int(result.scalar_one())


async def _attempt_count(db: AsyncConnection, event_id: uuid.UUID) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(MedicationAttempt)
        .where(MedicationAttempt.event_id == event_id)
    )
    return int(result.scalar_one())


# ---------------------------------------------------------------- confirm


async def test_confirm_succeeds_even_when_tts_degrades(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    """FR-UI-03：按钮确认先落库；TTS 合成不出来只降级成字幕，确认不能失败。"""
    response = await client.post(f"/api/pad/medications/{EVENT_A}/confirm", headers=_device_auth())

    assert response.status_code == 200, response.text
    body = response.json()
    assert uuid.UUID(body["reminder_id"]) == EVENT_A
    assert body["status"] == "confirmed"
    assert body["terminal_state"] == "taken_reported"
    assert body["announcement"]["degraded"] is True
    assert body["announcement"]["audio_url"] is None
    assert body["announcement"]["text"]

    status, terminal, channel, confirmed_at, late_taken = await _event_row(db, EVENT_A)
    assert status == "confirmed"
    assert terminal == "taken_reported"
    assert channel == "button"
    assert confirmed_at is not None
    assert late_taken is False


async def test_confirm_requires_bound_device(client: AsyncClient, seeded: None) -> None:
    response = await client.post(
        f"/api/pad/medications/{EVENT_A}/confirm",
        headers=_device_auth(DEVICE_UNBOUND, SECRET_UNBOUND),
    )

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "device_not_bound"


async def test_confirm_rejects_other_elders_reminder(client: AsyncClient, seeded: None) -> None:
    """改一个 UUID 就能替别人确认是必须堵死的洞。"""
    response = await client.post(f"/api/pad/medications/{EVENT_B}/confirm", headers=_device_auth())

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "forbidden"


async def test_confirm_unknown_reminder_is_404(client: AsyncClient, seeded: None) -> None:
    response = await client.post(
        f"/api/pad/medications/{UNKNOWN_EVENT}/confirm", headers=_device_auth()
    )

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "reminder_not_found"


async def test_confirm_rejects_staff_credential(client: AsyncClient, seeded: None) -> None:
    response = await client.post(f"/api/pad/medications/{EVENT_A}/confirm", headers=_staff_auth())

    assert response.status_code == 401, response.text


async def test_confirm_translates_service_conflict_to_409(
    client: AsyncClient, seeded: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """服务层状态机冲突必须映射 409，不能被吞成 200 或漏成 500。"""

    async def fake_confirm(*_args: object, **_kwargs: object) -> tuple[object, bool]:
        raise MedicationEventConflict("event is already terminal")

    monkeypatch.setattr(pad_api.medication_service, "confirm_event", fake_confirm)
    response = await client.post(f"/api/pad/medications/{EVENT_A}/confirm", headers=_device_auth())

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "state_conflict"


# ---------------------------------------------------------------- undo


async def test_undo_reopens_event_and_writes_audit(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    """FR-UI-08：10 秒内撤销，事件退回等待确认，审计留下撤销前快照。"""
    confirmed = await client.post(f"/api/pad/medications/{EVENT_A}/confirm", headers=_device_auth())
    assert confirmed.status_code == 200, confirmed.text

    response = await client.post(f"/api/pad/medications/{EVENT_A}/undo", headers=_device_auth())

    assert response.status_code == 200, response.text
    body = response.json()
    assert uuid.UUID(body["reminder_id"]) == EVENT_A
    assert body["status"] == "waiting_confirm"
    assert body["terminal_state"] is None
    assert body["undone_at"]

    status, terminal, channel, confirmed_at, late_taken = await _event_row(db, EVENT_A)
    assert (status, terminal, channel, confirmed_at, late_taken) == (
        "waiting_confirm",
        None,
        None,
        None,
        False,
    )

    result = await db.execute(
        select(AuditLog.detail).where(
            AuditLog.action == "medication.event.undo",
            AuditLog.actor_id == DEVICE_A,
        )
    )
    details = result.scalars().all()
    assert len(details) == 1
    assert await _audit_count(db, "medication.event.undo", DEVICE_A) == 1
    detail = details[0]
    assert detail is not None
    assert detail["prior_terminal_state"] == "taken_reported"
    assert detail["prior_confirm_channel"] == "button"


async def test_undo_twice_is_409(client: AsyncClient, seeded: None) -> None:
    """重复撤销不是幂等成功：第二次没有可撤的终态，回 409。"""
    assert (
        await client.post(f"/api/pad/medications/{EVENT_A}/confirm", headers=_device_auth())
    ).status_code == 200
    assert (
        await client.post(f"/api/pad/medications/{EVENT_A}/undo", headers=_device_auth())
    ).status_code == 200

    response = await client.post(f"/api/pad/medications/{EVENT_A}/undo", headers=_device_auth())

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "state_conflict"


async def test_undo_after_window_is_409(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    """超过 10 秒的确认属于已收口事实，只能由护工在照护台纠正。"""
    stale = datetime.now(UTC) - timedelta(seconds=11)
    await db.execute(
        MedicationEvent.__table__.update()
        .where(MedicationEvent.reminder_id == EVENT_A)
        .values(
            status="confirmed",
            terminal_state="taken_reported",
            confirm_channel="button",
            confirmed_at=stale,
        )
    )

    response = await client.post(f"/api/pad/medications/{EVENT_A}/undo", headers=_device_auth())

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "state_conflict"


async def test_undo_rejects_other_elders_reminder(client: AsyncClient, seeded: None) -> None:
    response = await client.post(f"/api/pad/medications/{EVENT_B}/undo", headers=_device_auth())

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "forbidden"


async def test_undo_requires_bound_device(client: AsyncClient, seeded: None) -> None:
    response = await client.post(
        f"/api/pad/medications/{EVENT_A}/undo",
        headers=_device_auth(DEVICE_UNBOUND, SECRET_UNBOUND),
    )

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "device_not_bound"


async def test_undo_unknown_reminder_is_404(client: AsyncClient, seeded: None) -> None:
    response = await client.post(
        f"/api/pad/medications/{UNKNOWN_EVENT}/undo", headers=_device_auth()
    )

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "reminder_not_found"


# ---------------------------------------------------------------- rendered


async def test_rendered_records_card_rendered_receipt(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    """FR-ALT-03：客户端回执才算送达；枪号由服务端状态推，不信任客户端。"""
    await db.execute(
        MedicationEvent.__table__.update()
        .where(MedicationEvent.reminder_id == EVENT_A)
        .values(retry_count=2)
    )

    response = await client.post(f"/api/pad/medications/{EVENT_A}/rendered", headers=_device_auth())

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["attempt_no"] == 3, "attempt_no 必须是 retry_count + 1"
    assert body["receipt"] == "card_rendered"

    attempt = (
        await db.execute(
            select(MedicationAttempt.attempt_no, MedicationAttempt.receipt).where(
                MedicationAttempt.event_id == EVENT_A
            )
        )
    ).one()
    assert attempt.attempt_no == 3
    assert attempt.receipt == "card_rendered"
    assert await _attempt_count(db, EVENT_A) == 1


async def test_rendered_is_idempotent(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    headers = _device_auth()
    first = await client.post(f"/api/pad/medications/{EVENT_A}/rendered", headers=headers)
    second = await client.post(f"/api/pad/medications/{EVENT_A}/rendered", headers=headers)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["attempt_no"] == second.json()["attempt_no"] == 1
    assert await _attempt_count(db, EVENT_A) == 1


async def test_rendered_keeps_stronger_tts_receipt(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    """同一枪已被服务端记成 `tts_finished` 时，客户端回执不能把它降级覆盖。"""
    await db.execute(
        MedicationAttempt.__table__.insert().values(
            event_id=EVENT_A,
            attempt_no=1,
            sent_at=SCHEDULED,
            receipt="tts_finished",
        )
    )

    response = await client.post(f"/api/pad/medications/{EVENT_A}/rendered", headers=_device_auth())

    assert response.status_code == 200, response.text
    assert response.json()["receipt"] == "tts_finished"
    assert await _attempt_count(db, EVENT_A) == 1


async def test_rendered_rejects_other_elders_reminder(client: AsyncClient, seeded: None) -> None:
    response = await client.post(f"/api/pad/medications/{EVENT_B}/rendered", headers=_device_auth())

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "forbidden"


async def test_rendered_requires_bound_device(client: AsyncClient, seeded: None) -> None:
    response = await client.post(
        f"/api/pad/medications/{EVENT_A}/rendered",
        headers=_device_auth(DEVICE_UNBOUND, SECRET_UNBOUND),
    )

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "device_not_bound"


async def test_rendered_unknown_reminder_is_404(client: AsyncClient, seeded: None) -> None:
    response = await client.post(
        f"/api/pad/medications/{UNKNOWN_EVENT}/rendered", headers=_device_auth()
    )

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "reminder_not_found"
