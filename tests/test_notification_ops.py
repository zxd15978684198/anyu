"""通知运维三件事的**接口层**与边界证据（N42，迭代 8）。

为什么单独一个文件、为什么不与 `tests/test_notify_ops_t60.py` 合并：

- t60 打的是服务层（`notification_service.escalate_unacknowledged / resend / revoke`）。
  本文件打的是 **HTTP 入口**（`app/api/notifications.py`）：证明领域异常真的被翻译成
  契约码 —— 409 `invalid_transition`、409 `revoke_window_expired`、403 `ROLE_FORBIDDEN`。
  服务层全绿而 API 映射写错，照护台看到的仍是「操作失败」三个字，这是最容易漏的一环。
- t60 用「连跑两轮仍返回 `[]`」证明升级幂等。本文件补三条**最容易被写成假绿**的证据：
  1. 119s 不升 / 120s（含）升 / 121s 升，且升级**不新增行**（按行数断言，不是按返回值为空）；
  2. 重发两次后行数不变、`resend_count` 逐次递增（复用同一行，不是又插一条）；
  3. 撤销的审计**同事务**——回滚保存点后状态与审计一起消失。若实现改用独立会话
     （`audit_service.write_audit_isolated`），那条审计会活过回滚，本用例必红。

时间一律由用例注入（`now=` 形参），不 sleep、不依赖真实时钟；`_BASE` 写死是为了
证据可复现，而不是「跑得快就绿」。

跑法（**必须指独立测试库**，否则会与其它 agent 的 `TRUNCATE` 夹具互相踩）：

    ANYU_TEST_DATABASE_URL=postgresql+asyncpg://anyu:anyu@127.0.0.1:5432/anyu_test_ge
    uv run pytest tests/test_notification_ops.py -q
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from tests.conftest import require_db_connection

from app.api import notifications as notifications_api
from app.core.auth import get_current_principal
from app.core.config import get_settings
from app.core.runtime_context import Identity
from app.core.trace import TraceIdMiddleware
from app.db import get_session
from app.models import AuditLog, Elder, ElderStaffAssignment, Notification, StaffAccount
from app.services import notification_service as svc

_BASE = datetime.datetime(2026, 11, 10, 12, 0, tzinfo=datetime.UTC)
"""所有用例的基准时刻：边界证据必须可复现，不能靠「跑得快」碰巧卡在窗口内。"""


# --------------------------------------------------------------------- fixtures


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """每用例一个事务，结束整体回滚：不写种子数据、不污染别人。"""
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


@dataclass
class Seed:
    elder_id: uuid.UUID
    admin_id: uuid.UUID
    care_worker_id: uuid.UUID
    doctor_id: uuid.UUID


@pytest.fixture
async def seeded(db: AsyncSession) -> Seed:
    elder_id, admin_id = uuid.uuid4(), uuid.uuid4()
    care_worker_id, doctor_id = uuid.uuid4(), uuid.uuid4()
    await db.execute(
        Elder.__table__.insert(),
        [{"elder_id": elder_id, "name": "测试长者", "status": "active"}],
    )
    await db.execute(
        StaffAccount.__table__.insert(),
        [
            {"staff_id": admin_id, "name": "管理员", "role": "admin", "password_hash": "x"},
            {
                "staff_id": care_worker_id,
                "name": "护工",
                "role": "care_worker",
                "password_hash": "x",
            },
            {"staff_id": doctor_id, "name": "医生", "role": "doctor", "password_hash": "x"},
        ],
    )
    # 护工必须真的「负责这户」才看得见通知：可见范围是读路径，不是角色开关。
    await db.execute(
        ElderStaffAssignment.__table__.insert(),
        [
            {
                "staff_id": care_worker_id,
                "elder_id": elder_id,
                "relation": "primary_care",
            }
        ],
    )
    await db.flush()
    return Seed(
        elder_id=elder_id,
        admin_id=admin_id,
        care_worker_id=care_worker_id,
        doctor_id=doctor_id,
    )


@dataclass
class Bundle:
    client: AsyncClient
    holder: list[Identity]


@pytest.fixture
async def client(db: AsyncSession) -> AsyncIterator[Bundle]:
    application = FastAPI()
    application.add_middleware(TraceIdMiddleware)
    application.include_router(notifications_api.router)

    async def override_session() -> AsyncIterator[AsyncSession]:
        # savepoint 模式：路由里的 commit 只释放保存点，外层夹具事务结束时整体回滚。
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

    holder: list[Identity] = []

    def override_principal() -> Identity:
        return holder[0]

    application.dependency_overrides[get_session] = override_session
    application.dependency_overrides[get_current_principal] = override_principal

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver"
    ) as ac:
        yield Bundle(client=ac, holder=holder)


# ----------------------------------------------------------------------- helpers


def _identity(staff_id: uuid.UUID, role: str) -> Identity:
    return Identity(kind="staff", staff_id=str(staff_id), role=role)


async def _insert(
    db: AsyncSession,
    seed: Seed,
    *,
    severity: str = "green",
    status: str = "delivered",
    created_at: datetime.datetime | None = None,
    delivered_at: datetime.datetime | None = None,
    payload: dict[str, object] | None = None,
) -> uuid.UUID:
    """直接插一行通知：状态/时间都能指定，才构造得出 119s 与 121s 的差异。"""
    nid = uuid.uuid4()
    await db.execute(
        Notification.__table__.insert(),
        [
            {
                "notification_id": nid,
                "elder_id": seed.elder_id,
                "kind": "health_review",
                "severity": severity,
                "status": status,
                "payload": payload if payload is not None else {"summary": "测试通知"},
                "delivered_at": delivered_at,
                "created_at": created_at or _BASE,
            }
        ],
    )
    await db.flush()
    return nid


async def _reload(db: AsyncSession, nid: uuid.UUID) -> Notification:
    """从库里重读：服务层 flush 过，但身份映射里的旧对象不会被自动刷新。"""
    db.expire_all()
    return (
        await db.execute(select(Notification).where(Notification.notification_id == nid))
    ).scalar_one()


async def _row_count(db: AsyncSession, seed: Seed) -> int:
    """按长者数「通知行」，用来证明升级/重发是原地改，而不是又插了一条。"""
    db.expire_all()
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(Notification)
                .where(Notification.elder_id == seed.elder_id)
            )
        ).scalar_one()
    )


async def _revoke_audits(db: AsyncSession, nid: uuid.UUID) -> list[AuditLog]:
    db.expire_all()
    return list(
        (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.resource_id == nid,
                    AuditLog.action == "notification.revoke",
                )
            )
        )
        .scalars()
        .all()
    )


# ------------------------------------------------- 一 · 120 秒升级（边界与幂等）


@pytest.mark.parametrize(
    ("age_seconds", "should_escalate"),
    [
        (119, False),
        (120, True),  # 判定是 `created_at <= now - 120s`，正好 120s 属「到点」侧。
        (121, True),
    ],
)
async def test_escalation_boundary_uses_inclusive_120s(
    db: AsyncSession,
    seeded: Seed,
    age_seconds: int,
    should_escalate: bool,
) -> None:
    """119s 不升、120s 升、121s 升：差一秒就把「到点」判反，是这类逻辑最常见的错。"""
    nid = await _insert(
        db,
        seeded,
        severity="green",
        status="delivered",
        created_at=_BASE - datetime.timedelta(seconds=age_seconds),
    )

    escalated = await svc.escalate_unacknowledged(db, now=_BASE)

    row = await _reload(db, nid)
    if should_escalate:
        assert escalated == [nid]
        assert row.severity == "yellow"
        assert row.payload["escalated_from"] == "green"
        assert row.payload["escalation_count"] == 1
    else:
        assert escalated == []
        assert row.severity == "green"
        assert "escalated_at" not in row.payload


async def test_escalation_lifts_only_once_and_never_adds_a_row(
    db: AsyncSession, seeded: Seed
) -> None:
    """连跑三轮只升一次：第二轮起 `escalated_at` 已存在，第三条路不能再抬到 red。"""
    nid = await _insert(
        db,
        seeded,
        severity="green",
        status="delivered",
        created_at=_BASE - datetime.timedelta(seconds=121),
    )
    rows_before = await _row_count(db, seeded)

    first = await svc.escalate_unacknowledged(db, now=_BASE)
    assert first == [nid]
    assert await _row_count(db, seeded) == rows_before  # 原地改 severity，不是新增行

    second = await svc.escalate_unacknowledged(db, now=_BASE + datetime.timedelta(seconds=1))
    third = await svc.escalate_unacknowledged(db, now=_BASE + datetime.timedelta(seconds=2))
    assert second == []
    assert third == []

    row = await _reload(db, nid)
    assert row.severity == "yellow"  # 第二次没有继续抬到 red
    assert row.payload["escalation_count"] == 1
    assert await _row_count(db, seeded) == rows_before  # 三轮之后仍然只有一行


@pytest.mark.parametrize(
    ("severity", "expected"),
    [("green", "yellow"), ("yellow", "red"), ("red", "red")],
)
async def test_escalation_ladder_is_one_step_in_place(
    db: AsyncSession, seeded: Seed, severity: str, expected: str
) -> None:
    """绿→黄→红各只抬一档；红已在顶上，停原档但仍记 `escalated_at`（否则下一轮还会升）。"""
    nid = await _insert(
        db,
        seeded,
        severity=severity,
        status="delivered",
        created_at=_BASE - datetime.timedelta(seconds=121),
    )

    assert await svc.escalate_unacknowledged(db, now=_BASE) == [nid]

    row = await _reload(db, nid)
    assert row.severity == expected
    assert row.payload["escalated_from"] == severity
    assert row.payload["escalation_count"] == 1


def test_escalation_and_revoke_windows_are_pinned_as_literals() -> None:
    """把 120 / 10 钉成字面量。

    用例里的偏移全部写成裸秒数（119/120/121、10/10.001），**从不拿常量自己算偏移**；
    这条再从常量侧反向钉一次。若有人把 `ESCALATION_DELAY_SECONDS` 改成 0，上面
    「119s 不升」会立刻变红；若有人把用例改成「常量 - 1s」，这条挡住那种自洽式假绿。
    """
    assert svc.ESCALATION_DELAY_SECONDS == 120
    assert svc.REVOKE_WINDOW_SECONDS == 10


async def test_escalation_skips_acknowledged_rows(db: AsyncSession, seeded: Seed) -> None:
    """已确认 = 闭环：哪怕放了 600 秒也不再升，否则「处理完了还被抬红」会惊动护工。"""
    nid = await _insert(
        db,
        seeded,
        severity="green",
        status="acknowledged",
        created_at=_BASE - datetime.timedelta(seconds=600),
    )

    assert await svc.escalate_unacknowledged(db, now=_BASE) == []

    row = await _reload(db, nid)
    assert row.severity == "green"
    assert row.payload.get("escalated_at") is None


async def test_escalation_skips_generated_rows_even_when_old(
    db: AsyncSession, seeded: Seed
) -> None:
    """`generated` 还没发出去：超时也不升，否则「没送到」会被误记成「没人管」。"""
    nid = await _insert(
        db,
        seeded,
        severity="green",
        status="generated",
        created_at=_BASE - datetime.timedelta(seconds=600),
    )

    assert await svc.escalate_unacknowledged(db, now=_BASE) == []

    row = await _reload(db, nid)
    assert row.severity == "green"
    assert row.payload.get("escalated_at") is None


# --------------------------------------------- 一·五 · 红档置顶（时间与档位相反）


async def test_list_puts_red_first_even_when_it_is_the_oldest(
    db: AsyncSession, seeded: Seed
) -> None:
    """数据形状故意与「按时间倒序」相反：绿最新、黄居中、红最旧。

    若排序丢掉档位前缀，结果会是 green, yellow, red —— 红档事件被挤到列表末尾，
    照护台首页就看不见最该处置的那条。只补这一条：分页与 tie-break 归 task-63。
    """
    green = await _insert(
        db, seeded, severity="green", created_at=_BASE - datetime.timedelta(seconds=1)
    )
    yellow = await _insert(
        db, seeded, severity="yellow", created_at=_BASE - datetime.timedelta(seconds=60)
    )
    red = await _insert(
        db, seeded, severity="red", created_at=_BASE - datetime.timedelta(seconds=600)
    )

    page = await svc.list_for_staff(db, _identity(seeded.admin_id, "admin"), page=1, page_size=10)

    assert [item.notification_id for item in page.items] == [red, yellow, green]


# ----------------------------------------------------- 二 · 重发（HTTP 入口）


async def test_api_resend_reuses_the_row_and_counts_up(
    db: AsyncSession, seeded: Seed, client: Bundle
) -> None:
    """重发两次：同一条 id、`resend_count` 1→2、行数始终 1（不许制造重复通知）。"""
    nid = await _insert(
        db,
        seeded,
        status="delivered",
        delivered_at=_BASE,
        created_at=_BASE,
    )
    client.holder[:] = [_identity(seeded.care_worker_id, "care_worker")]

    for expected in (1, 2):
        response = await client.client.post(f"/api/notifications/{nid}/resend")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["notification_id"] == str(nid)
        assert body["status"] == "delivered"
        assert body["payload"]["resend_count"] == expected
        assert await _row_count(db, seeded) == 1


async def test_api_resend_rejects_doctor_with_403(
    db: AsyncSession, seeded: Seed, client: Bundle
) -> None:
    """医生能看能确认，但重发是照护排班侧的运营动作：403，且错误码是契约里的 ROLE_FORBIDDEN。"""
    nid = await _insert(db, seeded, status="delivered", delivered_at=_BASE, created_at=_BASE)
    client.holder[:] = [_identity(seeded.doctor_id, "doctor")]

    response = await client.client.post(f"/api/notifications/{nid}/resend")

    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "ROLE_FORBIDDEN"
    assert body["trace_id"] != "-"  # 中间件把 trace_id 带进错误体，排障才串得起来


@pytest.mark.parametrize("terminal", ["acknowledged", "failed", "simulated"])
async def test_api_resend_terminal_status_maps_to_409_invalid_transition(
    db: AsyncSession, seeded: Seed, client: Bundle, terminal: str
) -> None:
    """三条终态一律 409 `invalid_transition`（不是 400、也不是悄悄成功）。"""
    nid = await _insert(db, seeded, status=terminal, delivered_at=_BASE, created_at=_BASE)
    client.holder[:] = [_identity(seeded.admin_id, "admin")]

    response = await client.client.post(f"/api/notifications/{nid}/resend")

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "invalid_transition"
    # detail 由 `InvalidTransitionError` 拼出：两侧状态都要在，排障才知道卡在哪一跳。
    assert terminal in body["detail"]
    assert "delivered" in body["detail"]

    row = await _reload(db, nid)
    assert row.status == terminal  # 被拒的请求不能留下半截状态
    assert "resend_count" not in (row.payload or {})


# --------------------------------------------------- 三 · 10 秒撤销（HTTP 入口）


async def test_api_revoke_within_window_marks_failed_and_writes_audit(
    db: AsyncSession, seeded: Seed, client: Bundle
) -> None:
    """3 秒内撤销：状态置 failed、payload 留痕、审计行与状态在同一个事务里。"""
    nid = await _insert(
        db,
        seeded,
        status="delivered",
        delivered_at=datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=3),
        created_at=_BASE,
    )
    client.holder[:] = [_identity(seeded.admin_id, "admin")]

    response = await client.client.post(
        f"/api/notifications/{nid}/revoke", params={"reason": "发错人了"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "failed"
    assert body["payload"]["revoked"] is True
    assert body["payload"]["revoke_reason"] == "发错人了"
    assert await _row_count(db, seeded) == 1  # 撤销不是删除，行还在

    audits = await _revoke_audits(db, nid)
    assert len(audits) == 1
    audit = audits[0]
    assert audit.resource_type == "notifications"
    assert audit.actor_type == "staff"
    assert audit.detail["from_status"] == "delivered"
    assert audit.detail["to_status"] == "failed"
    assert audit.detail["reason"] == "发错人了"


async def test_revoke_window_boundary_is_strictly_greater_than_10s(
    db: AsyncSession, seeded: Seed
) -> None:
    """正好 10 秒仍可撤（判定是 `>`，不是 `>=`）；10 秒零 1 秒就超窗。

    HTTP 层拿不到注入时钟（`now` 只存在于服务层），所以边界放在服务层钉；
    API 层只验「窗口内 200 / 明显超窗 409」，见下一条。
    """
    on_time = await _insert(
        db,
        seeded,
        status="delivered",
        delivered_at=_BASE - datetime.timedelta(seconds=10),
        created_at=_BASE,
    )
    late = await _insert(
        db,
        seeded,
        status="delivered",
        delivered_at=_BASE - datetime.timedelta(seconds=10, milliseconds=1),
        created_at=_BASE,
    )
    admin = _identity(seeded.admin_id, "admin")

    revoked = await svc.revoke(db, admin, on_time, now=_BASE)
    assert revoked.status == "failed"

    with pytest.raises(svc.RevokeWindowExpiredError) as caught:
        await svc.revoke(db, admin, late, now=_BASE)
    assert caught.value.window_seconds == 10


async def test_revoke_counts_window_from_created_at_when_never_delivered(
    db: AsyncSession, seeded: Seed
) -> None:
    """没送达过就从创建时间起算：刚生成（3s）能撤；躺了 11s 的生成态不能当「刚发出」。"""
    fresh = await _insert(
        db,
        seeded,
        status="generated",
        created_at=_BASE - datetime.timedelta(seconds=3),
    )
    stale = await _insert(
        db,
        seeded,
        status="generated",
        created_at=_BASE - datetime.timedelta(seconds=11),
    )
    admin = _identity(seeded.admin_id, "admin")

    assert (await svc.revoke(db, admin, fresh, now=_BASE)).status == "failed"

    with pytest.raises(svc.RevokeWindowExpiredError):
        await svc.revoke(db, admin, stale, now=_BASE)


async def test_api_revoke_after_window_is_409_revoke_window_expired(
    db: AsyncSession, seeded: Seed, client: Bundle
) -> None:
    """超窗必须是 `revoke_window_expired`，不能退化成含糊的 `invalid_transition`。"""
    nid = await _insert(
        db,
        seeded,
        status="delivered",
        delivered_at=datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=30),
        created_at=_BASE,
    )
    client.holder[:] = [_identity(seeded.admin_id, "admin")]

    response = await client.client.post(f"/api/notifications/{nid}/revoke")

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "revoke_window_expired"
    assert "10s" in body["detail"]
    assert await _revoke_audits(db, nid) == []  # 被拒的撤销不留审计噪音
    assert (await _reload(db, nid)).status == "delivered"


async def test_api_revoke_rejects_non_staff_sessions(
    db: AsyncSession, seeded: Seed, client: Bundle
) -> None:
    """设备/家属会话不能替照护台撤通知：403，且状态不变。"""
    nid = await _insert(
        db,
        seeded,
        status="delivered",
        delivered_at=datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=2),
        created_at=_BASE,
    )
    client.holder[:] = [Identity(kind="device", device_id=str(uuid.uuid4()))]

    response = await client.client.post(f"/api/notifications/{nid}/revoke")

    assert response.status_code == 403
    assert response.json()["code"] == "ROLE_FORBIDDEN"
    assert (await _reload(db, nid)).status == "delivered"


async def test_revoke_audit_rolls_back_with_the_status(db: AsyncSession, seeded: Seed) -> None:
    """审计必须与状态**同事务**：回滚保存点后两者一起消失。

    判定逻辑：若实现改用独立会话（`write_audit_isolated`），它会在别的连接上提交，
    保存点回滚管不到它 —— 审计行会活下来，下面 `audits == []` 就会红。
    这条比「撤销后能查到审计」强得多：后者在两种实现下都绿。
    """
    nid = await _insert(
        db,
        seeded,
        status="delivered",
        delivered_at=_BASE - datetime.timedelta(seconds=2),
        created_at=_BASE,
    )
    admin = _identity(seeded.admin_id, "admin")

    savepoint = await db.begin_nested()
    await svc.revoke(db, admin, nid, reason="回滚验证", now=_BASE)
    assert (await _revoke_audits(db, nid))[0].detail["reason"] == "回滚验证"
    await savepoint.rollback()

    row = await _reload(db, nid)
    assert row.status == "delivered"  # 状态回到撤销前
    assert not row.payload.get("revoked")  # payload 也没留下撤销痕迹
    assert await _revoke_audits(db, nid) == []  # 审计没有跨事务逃逸
