"""迭代 8 通知中心（T4）：状态机、可见范围、推送与端侧回执。

压测的三个点都来自验收条款：

- **状态机**（PRD §4.7 / line 398）：`generated → pending → delivered → acknowledged|failed`，
  `deliver` 只是**推送尝试**（`generated → pending` + `payload.pushed_at`），只有端侧设备
  回执 `POST /{id}/receipt` 才写 `delivered`；表外跳转一律 409 `invalid_transition`。
- **可见范围**（FR-ALT-02）：admin 全量；`care_worker` 只看 `primary_care` 长者 +
  **派给本人**的通知；`doctor` 只看 `doctor` 关系；家属/设备读通知 403。
- **回执越权**（FR-ALT-03 红线）：设备只能替自己绑定的长者回执，别的 elder → 403 且状态不变。

库与事务的处理与 `tests/test_reports_api.py` 相同：一切写入落在外层事务里，
`get_session` 被覆盖成 savepoint 模式，teardown 整体回滚，不污染种子数据。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from tests.conftest import require_db_connection

from app.api import notifications as notifications_api
from app.core.auth import get_current_principal
from app.core.config import get_settings
from app.core.runtime_context import Identity
from app.core.trace import TraceIdMiddleware
from app.db import get_session
from app.models import (
    Elder,
    ElderGuardian,
    ElderStaffAssignment,
    GuardianAccount,
    Notification,
    StaffAccount,
)
from app.services import notification_service

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
    admin_id: uuid.UUID
    doctor_id: uuid.UUID
    care_worker_id: uuid.UUID
    guardian_id: uuid.UUID
    elder_id: uuid.UUID
    other_elder_id: uuid.UUID
    own_ids: dict[str, uuid.UUID]


async def _seed(db: AsyncSession) -> Seed:
    admin_id, doctor_id, care_worker_id, guardian_id = (uuid.uuid4() for _ in range(4))
    elder_id, other_elder_id = uuid.uuid4(), uuid.uuid4()

    await db.execute(
        StaffAccount.__table__.insert(),
        [
            {"staff_id": admin_id, "name": "管理员", "role": "admin", "password_hash": "x"},
            {"staff_id": doctor_id, "name": "医生李", "role": "doctor", "password_hash": "x"},
            {
                "staff_id": care_worker_id,
                "name": "护工王",
                "role": "care_worker",
                "password_hash": "x",
            },
        ],
    )
    await db.execute(
        GuardianAccount.__table__.insert(),
        [{"guardian_id": guardian_id, "name": "家属张", "password_hash": "x"}],
    )
    await db.execute(
        Elder.__table__.insert(),
        [
            {"elder_id": elder_id, "name": "张桂芬", "status": "active"},
            {"elder_id": other_elder_id, "name": "李振华", "status": "active"},
        ],
    )
    await db.execute(
        ElderStaffAssignment.__table__.insert(),
        [
            {"elder_id": elder_id, "staff_id": care_worker_id, "relation": "primary_care"},
            {"elder_id": other_elder_id, "staff_id": doctor_id, "relation": "doctor"},
        ],
    )

    ids = {
        "care_review": uuid.uuid4(),  # care_worker 负责长者的待处理通知
        "care_delivered": uuid.uuid4(),  # 已投递，供 ack 用
        "doctor_review": uuid.uuid4(),  # 医生负责长者的通知（护工看不见）
        "assigned_to_care": uuid.uuid4(),  # 无长者、直接派给护工
    }
    # 逐条 insert：一次塞**键不一致**的 dict 列表会按 keyset 分组编译，漏掉的列取 NULL
    # 且不报错（实测 `assignee_staff_id` 就这么丢过），见 test_reports_api._seed 的同名注释。
    notes = [
        {
            "notification_id": ids["care_review"],
            "elder_id": elder_id,
            "kind": "health_review",
            "severity": "yellow",
            "status": "generated",
            "payload": {"summary": "19:00 健康采集"},
        },
        {
            "notification_id": ids["care_delivered"],
            "elder_id": elder_id,
            "kind": "collection_missed",
            "severity": "red",
            "status": "delivered",
            "payload": {"summary": "漏采"},
        },
        {
            "notification_id": ids["doctor_review"],
            "elder_id": other_elder_id,
            "kind": "medication_escalation",
            "severity": "red",
            "status": "generated",
            "payload": {"summary": "用药未确认"},
        },
        {
            "notification_id": ids["assigned_to_care"],
            "elder_id": None,
            "kind": "family_report",
            "severity": "green",
            "status": "generated",
            "assignee_staff_id": care_worker_id,
            "payload": {"summary": "家属日报待发"},
        },
    ]
    for row in notes:
        await db.execute(Notification.__table__.insert(), [row])
    await db.flush()
    return Seed(
        admin_id=admin_id,
        doctor_id=doctor_id,
        care_worker_id=care_worker_id,
        guardian_id=guardian_id,
        elder_id=elder_id,
        other_elder_id=other_elder_id,
        own_ids=ids,
    )


@pytest.fixture
async def seeded(db: AsyncSession) -> Seed:
    return await _seed(db)


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


def _as_admin(seed: Seed) -> Identity:
    return Identity(kind="staff", staff_id=str(seed.admin_id), role="admin")


def _as_doctor(seed: Seed) -> Identity:
    return Identity(kind="staff", staff_id=str(seed.doctor_id), role="doctor")


def _as_care_worker(seed: Seed) -> Identity:
    return Identity(kind="staff", staff_id=str(seed.care_worker_id), role="care_worker")


def _as_guardian(seed: Seed) -> Identity:
    return Identity(kind="guardian", guardian_id=str(seed.guardian_id))


def _as_device(elder_id: uuid.UUID) -> Identity:
    """端侧设备会话：kind=device，绑到某一位长者（回执权限只到这一位）。"""
    return Identity(kind="device", device_id=str(uuid.uuid4()), elder_id=str(elder_id))


# ----------------------------------------------------------------- 可见范围


async def test_admin_sees_everything(seeded: Seed, client: Bundle) -> None:
    client.holder.append(_as_admin(seeded))
    resp = await client.client.get("/api/notifications")
    assert resp.status_code == 200, resp.text
    assert resp.json()["total"] == 4


async def test_care_worker_sees_assigned_scope_plus_direct_assignments(
    seeded: Seed, client: Bundle
) -> None:
    client.holder.append(_as_care_worker(seeded))
    resp = await client.client.get("/api/notifications")
    assert resp.status_code == 200, resp.text
    kinds = {item["kind"] for item in resp.json()["items"]}
    # 负责长者的两条（health_review / collection_missed）+ 直接派给本人的一条
    assert kinds == {"health_review", "collection_missed", "family_report"}
    assert resp.json()["total"] == 3


async def test_doctor_only_sees_own_patients(seeded: Seed, client: Bundle) -> None:
    client.holder.append(_as_doctor(seeded))
    resp = await client.client.get("/api/notifications")
    assert resp.status_code == 200
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["kind"] == "medication_escalation"


async def test_guardian_reads_only_bound_elder_notifications(
    db: AsyncSession, seeded: Seed, client: Bundle
) -> None:
    """W4：绑定家属能读**自己长者**的通知；解绑后立刻看不到。

    迭代 8 的口径是「家属一律 403」，W4 按 PRD 177 白名单（健康日报与需关注事件
    摘要）放开**读**，但范围只由 `elder_guardians.status='active'` 决定 ——
    这条用例同时钉住「放开读」与「解绑即失效」两侧。
    """
    await db.execute(
        ElderGuardian.__table__.insert(),
        [
            {
                "elder_id": seeded.elder_id,
                "guardian_id": seeded.guardian_id,
                "relation": "daughter",
                "status": "active",
            }
        ],
    )
    await db.flush()
    client.holder.append(_as_guardian(seeded))

    resp = await client.client.get("/api/notifications")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] >= 1, "绑定长者有通知，家属应当看得见"
    # 只许看到绑定长者那一条（李振华的通知不在范围内）。
    assert {item["elder_id"] for item in body["items"]} == {str(seeded.elder_id)}

    await db.execute(
        update(ElderGuardian)
        .where(ElderGuardian.guardian_id == seeded.guardian_id)
        .values(status="unbound")
    )
    await db.flush()
    after = await client.client.get("/api/notifications")
    assert after.status_code == 200, after.text
    assert after.json()["total"] == 0, "解绑后必须立刻读不到（unbound 是保痕不是可读历史）"


async def test_guardian_cannot_deliver_or_acknowledge(seeded: Seed, client: Bundle) -> None:
    """能读 ≠ 能处置：投递与确认是护工侧动作，家属一律 403。

    否则家属点一下就能把一条红色事件关掉，工单闭环与审计归因都会失真
    （PRD §4.6 / AC-24）。这条与范围过滤分开断言：家属**看得到**这条通知，
    但**动不了**它。
    """
    client.holder.append(_as_guardian(seeded))
    nid = seeded.own_ids["care_review"]
    for path in (f"/api/notifications/{nid}/deliver", f"/api/notifications/{nid}/ack"):
        resp = await client.client.post(path)
        assert resp.status_code == 403, f"{path} 必须拒绝家属：{resp.text}"
        assert resp.json()["code"] == "ROLE_FORBIDDEN"


async def test_status_filter(seeded: Seed, client: Bundle) -> None:
    client.holder.append(_as_admin(seeded))
    resp = await client.client.get("/api/notifications", params={"status": "delivered"})
    assert resp.status_code == 200
    assert resp.json()["total"] == 1

    bad = await client.client.get("/api/notifications", params={"status": "bogus"})
    assert bad.status_code == 422
    assert bad.json()["code"] == "VALIDATION_ERROR"


# ------------------------------------------------------------------- 状态机


async def test_deliver_is_push_attempt_then_device_receipt_then_ack(
    seeded: Seed, client: Bundle
) -> None:
    """FR-ALT-03 主链路：generated →(员工推送) pending →(设备回执) delivered →(员工) acknowledged。

    「送达」必须由端侧回执产生——员工点按钮只算尝试推送（PRD line 398）。
    """
    client.holder.append(_as_care_worker(seeded))
    nid = seeded.own_ids["care_review"]

    pushed = await client.client.post(f"/api/notifications/{nid}/deliver")
    assert pushed.status_code == 200, pushed.text
    assert pushed.json()["status"] == "pending"
    assert pushed.json()["delivered_at"] is None
    assert pushed.json()["payload"]["pushed_at"], "推送尝试必须记 pushed_at"

    client.holder[0] = _as_device(seeded.elder_id)
    receipt = await client.client.post(f"/api/notifications/{nid}/receipt")
    assert receipt.status_code == 200, receipt.text
    body = receipt.json()
    assert body["status"] == "delivered"
    assert body["delivered_at"] is not None
    assert body["acknowledged_at"] is None

    # 重复回执幂等：仍是 delivered，不改成 acknowledged。
    again = await client.client.post(f"/api/notifications/{nid}/receipt")
    assert again.status_code == 200, again.text
    assert again.json()["status"] == "delivered"

    client.holder[0] = _as_care_worker(seeded)
    acked = await client.client.post(f"/api/notifications/{nid}/ack")
    assert acked.status_code == 200, acked.text
    assert acked.json()["status"] == "acknowledged"
    assert acked.json()["acknowledged_at"] is not None
    assert acked.json()["confirmed_at"] == acked.json()["acknowledged_at"]


async def test_receipt_from_other_elder_device_is_403_and_keeps_status(
    db: AsyncSession, seeded: Seed, client: Bundle
) -> None:
    """负例（红线）：别的 elder 的设备令牌回执 → 403，通知状态保持 pending。"""
    client.holder.append(_as_care_worker(seeded))
    nid = seeded.own_ids["care_review"]
    pushed = await client.client.post(f"/api/notifications/{nid}/deliver")
    assert pushed.status_code == 200, pushed.text

    client.holder[0] = _as_device(seeded.other_elder_id)
    resp = await client.client.post(f"/api/notifications/{nid}/receipt")
    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "SCOPE_FORBIDDEN"

    row = (
        await db.execute(select(Notification).where(Notification.notification_id == nid))
    ).scalar_one()
    assert row.status == "pending", "越权回执不得改动状态"
    assert row.delivered_at is None


async def test_receipt_requires_device_identity(seeded: Seed, client: Bundle) -> None:
    """家属/员工凭证不能替代端侧回执（否则又回到「人工声明送达」）。"""
    client.holder.append(_as_care_worker(seeded))
    nid = seeded.own_ids["care_review"]
    await client.client.post(f"/api/notifications/{nid}/deliver")

    for identity in (_as_guardian(seeded), _as_care_worker(seeded)):
        client.holder[0] = identity
        resp = await client.client.post(f"/api/notifications/{nid}/receipt")
        assert resp.status_code == 403, f"{identity.kind} 不应能回执：{resp.text}"
        assert resp.json()["code"] == "ROLE_FORBIDDEN"


async def test_receipt_before_push_is_invalid_transition(seeded: Seed, client: Bundle) -> None:
    """未推送就回执 → 409：状态机不允许 generated 直接跳 delivered。"""
    client.holder.append(_as_device(seeded.elder_id))
    resp = await client.client.post(f"/api/notifications/{seeded.own_ids['care_review']}/receipt")
    assert resp.status_code == 409
    assert resp.json()["code"] == "invalid_transition"


async def test_receipt_on_simulated_terminal_is_rejected(
    db: AsyncSession, seeded: Seed, client: Bundle
) -> None:
    """`simulated` 是演示终态，端侧回执不得改写它（Lead 2026-11-12 裁决）。"""
    client.holder.append(_as_care_worker(seeded))
    nid = seeded.own_ids["care_review"]
    simulated = await client.client.post(
        f"/api/notifications/{nid}/deliver", params={"simulated": "true"}
    )
    assert simulated.status_code == 200, simulated.text
    assert simulated.json()["status"] == "simulated"

    client.holder[0] = _as_device(seeded.elder_id)
    resp = await client.client.post(f"/api/notifications/{nid}/receipt")
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "invalid_transition"
    row = (
        await db.execute(select(Notification).where(Notification.notification_id == nid))
    ).scalar_one()
    assert row.status == "simulated"


async def test_receipt_after_ack_keeps_acknowledged(seeded: Seed, client: Bundle) -> None:
    """`acknowledged` 之后回执是幂等 200，但**不许**把状态退回 delivered。"""
    client.holder.append(_as_care_worker(seeded))
    nid = seeded.own_ids["care_delivered"]
    acked = await client.client.post(f"/api/notifications/{nid}/ack")
    assert acked.status_code == 200, acked.text
    acknowledged_at = acked.json()["acknowledged_at"]

    client.holder[0] = _as_device(seeded.elder_id)
    resp = await client.client.post(f"/api/notifications/{nid}/receipt")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "acknowledged"
    assert body["acknowledged_at"] == acknowledged_at


async def test_ack_requires_delivered(seeded: Seed, client: Bundle) -> None:
    client.holder.append(_as_care_worker(seeded))
    nid = seeded.own_ids["care_review"]

    resp = await client.client.post(f"/api/notifications/{nid}/ack")
    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == "invalid_transition"
    assert "generated" in body["detail"]
    assert len(body["trace_id"]) == 32


async def test_terminal_state_cannot_transition(seeded: Seed, client: Bundle) -> None:
    client.holder.append(_as_care_worker(seeded))
    nid = seeded.own_ids["care_delivered"]

    first = await client.client.post(f"/api/notifications/{nid}/ack")
    assert first.status_code == 200

    again = await client.client.post(f"/api/notifications/{nid}/ack")
    assert again.status_code == 409
    assert again.json()["code"] == "invalid_transition"

    deliver_after = await client.client.post(f"/api/notifications/{nid}/deliver")
    assert deliver_after.status_code == 409


async def test_simulated_branch_is_terminal(seeded: Seed, client: Bundle) -> None:
    client.holder.append(_as_care_worker(seeded))
    nid = seeded.own_ids["care_review"]

    resp = await client.client.post(
        f"/api/notifications/{nid}/deliver", params={"simulated": "true"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "simulated"

    acked = await client.client.post(f"/api/notifications/{nid}/ack")
    assert acked.status_code == 409


async def test_unknown_notification_is_404(seeded: Seed, client: Bundle) -> None:
    client.holder.append(_as_admin(seeded))
    resp = await client.client.post(f"/api/notifications/{uuid.uuid4()}/ack")
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


async def test_out_of_scope_notification_is_404_not_403(seeded: Seed, client: Bundle) -> None:
    """范围外的通知按"不存在"处理：不泄露别人队列里的条目。"""
    client.holder.append(_as_doctor(seeded))
    resp = await client.client.post(f"/api/notifications/{seeded.own_ids['care_review']}/deliver")
    assert resp.status_code == 404


# ------------------------------------------------------------------ 服务层单元


def test_transition_table_shape() -> None:
    """迁移表本身也是契约：终态为空集，且四个入口态都可直达 failed。"""
    for status in ("generated", "pending", "delivered"):
        assert "failed" in notification_service._ALLOWED_TRANSITIONS[status]
    for terminal in ("acknowledged", "failed", "simulated"):
        assert notification_service._ALLOWED_TRANSITIONS[terminal] == frozenset()


async def test_create_validates_enums(db: AsyncSession) -> None:
    with pytest.raises(notification_service.NotificationValidationError):
        await notification_service.create_notification(db, kind="nope", severity="red")
    with pytest.raises(notification_service.NotificationValidationError):
        await notification_service.create_notification(db, kind="health_review", severity="purple")


async def test_list_keeps_payload(db: AsyncSession, seeded: Seed) -> None:
    identity = _as_admin(seeded)
    page = await notification_service.list_for_staff(db, identity, page=1, page_size=10)
    assert page.total == 4
    rows = (await db.execute(select(Notification))).scalars().all()
    assert all(row.status in notification_service.NOTIFICATION_STATUSES for row in rows)


# ------------------------------------------------- severity 下推过滤（W1-A1/A5）
#
# 背景：`list_for_staff` 原先只认 status/kind，而照护台前端一直在传 `severity`
# （`console/pages/NotificationsPage.tsx` 的严重度下拉 → `listNotifications`），
# FastAPI 静默丢弃未知查询参数 → **下拉框是死控件**。家属端则在客户端只筛已加载
# 的那一页，红档落在后续页时显示成「暂时没有需要您关注的事项」而「加载更多」还亮着。
#
# 正确修法是把 severity 放进 `filters`（count 与 rows 共用），即**分页之前**过滤。
# 下面第一条用例就是钉住这一点：total 必须是筛选后的总数，不是未筛总数。


async def test_list_severity_total_is_filtered_before_pagination(
    client: Bundle, seeded: Seed
) -> None:
    """红档共 2 条、每页 1 条：total 必须是 2，且两页合起来正好是那 2 条红档。"""
    client.holder.append(_as_admin(seeded))
    first = await client.client.get(
        "/api/notifications", params={"severity": "red", "page_size": 1}
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["total"] == 2, (
        "total 必须是筛选后总数；未筛总数是 4，会让前端空态与加载更多判据失真"
    )
    assert len(body["items"]) == 1
    assert body["items"][0]["severity"] == "red"

    second = await client.client.get(
        "/api/notifications", params={"severity": "red", "page_size": 1, "page": 2}
    )
    assert second.status_code == 200, second.text
    seen = {body["items"][0]["notification_id"], second.json()["items"][0]["notification_id"]}
    assert seen == {str(seeded.own_ids["care_delivered"]), str(seeded.own_ids["doctor_review"])}


async def test_list_severity_matches_only_that_tier(client: Bundle, seeded: Seed) -> None:
    client.holder.append(_as_admin(seeded))
    for tier, expected in (("yellow", 1), ("green", 1), ("red", 2)):
        resp = await client.client.get("/api/notifications", params={"severity": tier})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == expected, f"{tier} 档应有 {expected} 条"
        assert all(item["severity"] == tier for item in body["items"])


async def test_list_severity_respects_staff_visibility(client: Bundle, seeded: Seed) -> None:
    """护工筛红档只应看到自己负责长者那条，不能因为加了筛选就把医生的漏出来。"""
    client.holder.append(_as_care_worker(seeded))
    resp = await client.client.get("/api/notifications", params={"severity": "red"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["notification_id"] == str(seeded.own_ids["care_delivered"])


async def test_list_severity_respects_guardian_scope(
    client: Bundle, db: AsyncSession, seeded: Seed
) -> None:
    """家属筛红档同样受 active 绑定约束：只看绑定长者的红档。"""
    await db.execute(
        ElderGuardian.__table__.insert(),
        [
            {
                "elder_id": seeded.elder_id,
                "guardian_id": seeded.guardian_id,
                "relation": "daughter",
                "status": "active",
            }
        ],
    )
    await db.flush()
    client.holder.append(_as_guardian(seeded))
    resp = await client.client.get("/api/notifications", params={"severity": "red"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["notification_id"] == str(seeded.own_ids["care_delivered"])


async def test_list_severity_combines_with_kind(client: Bundle, seeded: Seed) -> None:
    """severity 与 kind 是 AND，不是互相覆盖。"""
    client.holder.append(_as_admin(seeded))
    resp = await client.client.get(
        "/api/notifications", params={"severity": "red", "kind": "collection_missed"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["notification_id"] == str(seeded.own_ids["care_delivered"])


async def test_list_rejects_unknown_severity(client: Bundle, seeded: Seed) -> None:
    """非法档位必须 422，不能静默忽略（静默忽略正是原缺陷的成因）。"""
    client.holder.append(_as_admin(seeded))
    resp = await client.client.get("/api/notifications", params={"severity": "purple"})
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "VALIDATION_ERROR"


async def test_list_severity_validation_at_service_layer(db: AsyncSession, seeded: Seed) -> None:
    with pytest.raises(notification_service.NotificationValidationError):
        await notification_service.list_for_staff(
            db, _as_admin(seeded), page=1, page_size=10, severity="purple"
        )


async def test_list_without_severity_keeps_red_first(client: Bundle, seeded: Seed) -> None:
    """不传 severity 时排序口径不变：红档仍置顶（severity_rank → created_at desc → id）。"""
    client.holder.append(_as_admin(seeded))
    resp = await client.client.get("/api/notifications")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 4
    assert body["items"][0]["severity"] == "red"
