"""建号领域服务（`app/services/staff_service.py`）的单元级测试。

与 `tests/test_staff_api.py` / `tests/test_guardians_api.py` 的分工：
- 那两个文件走 HTTP，验证「契约 → 状态码」；
- 本文件直接调 service 函数，验证**每个领域异常分支**与边界（去重、同值不升版本、只数 active）。

这样做的理由：接口层用例为了覆盖 4 个端点已经很长，再往里塞分支会让失败定位变得困难；
而 service 是唯一写库层，它的每条 `raise` 都对应一个外部可见的错误码，值得逐条钉住。

无 PostgreSQL 时由 conftest 的 `require_db_connection()` 直接 fail（`ANYU_ALLOW_DB_SKIP=1` 才 skip）。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.runtime_context import Identity, identity_scope
from app.core.security import KIND_STAFF, STAFF_ROLES
from app.models.audit import AuditLog
from app.models.guardian import GuardianAccount
from app.models.organization import Elder
from app.models.relation import ElderGuardian, ElderStaffAssignment
from app.models.staff import StaffAccount
from app.services import staff_service
from app.services.staff_service import (
    MAX_ACTIVE_GUARDIANS_PER_ELDER,
    ElderNotFoundError,
    GuardianAlreadyBoundError,
    GuardianLimitExceededError,
    GuardianNotFoundError,
    StaffConflictError,
    StaffNotFoundError,
    StaffRoleConflictError,
    bind_guardian,
    create_guardian,
    create_staff,
    mask_phone,
    update_staff,
)
from conftest import require_db_connection

ELDER_A = uuid.UUID("aaaa1111-1111-4111-8111-111111111101")
ELDER_B = uuid.UUID("aaaa1111-1111-4111-8111-111111111102")
EXISTING_STAFF_ID = uuid.UUID("aaaa1111-1111-4111-8111-111111111201")
EXISTING_GUARDIAN_ID = uuid.UUID("aaaa1111-1111-4111-8111-111111111301")
EXISTING_STAFF_PHONE = "13800000010"
EXISTING_GUARDIAN_PHONE = "13900000010"


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """连不上库按 conftest 的统一策略处置（默认 fail，`ANYU_ALLOW_DB_SKIP=1` 才 skip）。"""
    conn: AsyncConnection = await require_db_connection(engine)

    trans = await conn.begin()
    sess = AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
    try:
        sess.add_all(
            [
                Elder(elder_id=ELDER_A, name="长者甲", status="active"),
                Elder(elder_id=ELDER_B, name="长者乙", status="active"),
                StaffAccount(
                    staff_id=EXISTING_STAFF_ID,
                    name="既有员工",
                    role="care_worker",
                    phone=EXISTING_STAFF_PHONE,
                    password_hash="$2b$12$placeholderplaceholderplaceholderplaceholder",
                    status="active",
                    token_version=0,
                ),
                GuardianAccount(
                    guardian_id=EXISTING_GUARDIAN_ID,
                    name="既有家属",
                    phone=EXISTING_GUARDIAN_PHONE,
                    password_hash="$2b$12$placeholderplaceholderplaceholderplaceholder",
                    status="active",
                ),
            ]
        )
        await sess.commit()
        yield sess
    finally:
        await sess.close()
        await trans.rollback()
        await conn.close()


AUDIT_ACTOR_ID = uuid.UUID("aaaa1111-1111-4111-8111-111111111901")


@pytest.fixture(autouse=True)
def admin_identity() -> Iterator[None]:
    """每条用例都在一个明确的 staff 身份下执行。

    建号与绑定要能答「谁记的账」，服务层从 `runtime_context` 取主体；身份缺失是
    **错误**而不是可容错状态（不许编一个 actor、也不许跳过审计），所以夹具给的是
    真身份，被测函数里没有任何兜底分支。
    """
    with identity_scope(Identity(kind=KIND_STAFF, staff_id=str(AUDIT_ACTOR_ID), role="admin")):
        yield


# ------------------------------------------------------------------ mask_phone


def test_mask_phone_empty_returns_empty_string() -> None:
    assert mask_phone(None) == ""
    assert mask_phone("") == ""


def test_mask_phone_keeps_head_and_tail() -> None:
    assert mask_phone("13800000001") == "138****0001"


# ------------------------------------------------------------------ create_staff


async def test_create_staff_rejects_unknown_role(session: AsyncSession) -> None:
    with pytest.raises(StaffRoleConflictError, match="未知员工角色"):
        await create_staff(
            session, name="张三", role="nurse", phone="13700000001", password="Passw0rd!"
        )


async def test_create_staff_admin_with_elders_is_rejected(session: AsyncSession) -> None:
    """裁决 D-01：管理员不参与长者绑定。"""
    with pytest.raises(StaffRoleConflictError, match="管理员账号不参与长者绑定"):
        await create_staff(
            session,
            name="王管理",
            role="admin",
            phone="13700000002",
            password="Passw0rd!",
            elder_ids=[ELDER_A],
        )


async def test_create_staff_conflicts_with_existing_staff_phone(session: AsyncSession) -> None:
    with pytest.raises(StaffConflictError):
        await create_staff(
            session,
            name="撞号员工",
            role="doctor",
            phone=EXISTING_STAFF_PHONE,
            password="Passw0rd!",
        )


async def test_create_staff_conflicts_with_existing_guardian_phone(session: AsyncSession) -> None:
    """跨表查重：家属占用的号码不能再建员工，否则登录侧会出现二义性。"""
    with pytest.raises(StaffConflictError):
        await create_staff(
            session,
            name="撞号医生",
            role="doctor",
            phone=EXISTING_GUARDIAN_PHONE,
            password="Passw0rd!",
        )


async def test_create_staff_missing_elder_raises_before_writing(session: AsyncSession) -> None:
    ghost = uuid.uuid4()
    with pytest.raises(ElderNotFoundError, match="长者不存在"):
        await create_staff(
            session,
            name="李医生",
            role="doctor",
            phone="13700000003",
            password="Passw0rd!",
            elder_ids=[ELDER_A, ghost],
        )

    # 校验前置于写库：异常路径不应留下任何员工行
    count = (
        await session.execute(
            select(func.count())
            .select_from(StaffAccount)
            .where(StaffAccount.phone == "13700000003")
        )
    ).scalar_one()
    assert count == 0


async def test_create_staff_admin_without_elders_has_no_assignment(session: AsyncSession) -> None:
    staff = await create_staff(
        session, name="王管理", role="admin", phone="13700000004", password="Passw0rd!"
    )
    assert staff.role == "admin"
    assert staff.status == "active"
    assert staff.token_version == 0

    assignments = (
        await session.execute(
            select(func.count())
            .select_from(ElderStaffAssignment)
            .where(ElderStaffAssignment.staff_id == staff.staff_id)
        )
    ).scalar_one()
    assert assignments == 0


async def test_create_staff_doctor_assignments_dedupe(session: AsyncSession) -> None:
    """同一 elder_id 传两遍只落一行，否则会撞 (elder_id, staff_id, relation) 唯一约束。"""
    staff = await create_staff(
        session,
        name="李医生",
        role="doctor",
        phone="13700000005",
        password="Passw0rd!",
        elder_ids=[ELDER_A, ELDER_A, ELDER_B],
    )

    rows = (
        (
            await session.execute(
                select(ElderStaffAssignment).where(ElderStaffAssignment.staff_id == staff.staff_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    assert {row.relation for row in rows} == {"doctor"}
    assert {row.elder_id for row in rows} == {ELDER_A, ELDER_B}


async def test_create_staff_care_worker_maps_to_primary_care(session: AsyncSession) -> None:
    staff = await create_staff(
        session,
        name="赵护工",
        role="care_worker",
        phone="13700000006",
        password="Passw0rd!",
        elder_ids=[ELDER_A],
    )
    row = (
        await session.execute(
            select(ElderStaffAssignment).where(ElderStaffAssignment.staff_id == staff.staff_id)
        )
    ).scalar_one()
    assert row.relation == "primary_care"


async def test_staff_roles_constant_is_source_of_truth() -> None:
    """角色白名单必须来自 `app.core.security.STAFF_ROLES`，不另起一份。"""
    assert set(staff_service._ROLE_TO_RELATION) <= set(STAFF_ROLES)
    assert "admin" not in staff_service._ROLE_TO_RELATION


# ------------------------------------------------------------------ update_staff


async def test_update_staff_not_found(session: AsyncSession) -> None:
    with pytest.raises(StaffNotFoundError):
        await update_staff(session, staff_id=uuid.uuid4(), role="doctor")


async def test_update_staff_same_values_do_not_bump_token_version(session: AsyncSession) -> None:
    """空 PATCH 与同值 PATCH 都不能误伤在线会话。"""
    same = await update_staff(
        session, staff_id=EXISTING_STAFF_ID, role="care_worker", status="active"
    )
    assert same.token_version == 0

    empty = await update_staff(session, staff_id=EXISTING_STAFF_ID)
    assert empty.token_version == 0


async def test_update_staff_role_change_bumps_token_version(session: AsyncSession) -> None:
    updated = await update_staff(session, staff_id=EXISTING_STAFF_ID, role="doctor")
    assert updated.role == "doctor"
    assert updated.token_version == 1


async def test_update_staff_status_change_bumps_token_version(session: AsyncSession) -> None:
    updated = await update_staff(session, staff_id=EXISTING_STAFF_ID, status="disabled")
    assert updated.status == "disabled"
    assert updated.token_version == 1


async def test_update_staff_both_fields_change_bumps_once(session: AsyncSession) -> None:
    """同一次请求改两个字段只加 1，不是加 2。"""
    updated = await update_staff(
        session, staff_id=EXISTING_STAFF_ID, role="doctor", status="disabled"
    )
    assert (updated.role, updated.status, updated.token_version) == ("doctor", "disabled", 1)


async def _assignment_count(session: AsyncSession, staff_id: uuid.UUID) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(ElderStaffAssignment)
                .where(ElderStaffAssignment.staff_id == staff_id)
            )
        ).scalar_one()
    )


async def test_update_staff_to_admin_clears_elder_assignments(session: AsyncSession) -> None:
    """批B-D-01 回归：护工 → 管理员，负责长者被清除，且不波及别人。

    断言落在库里的行数上而不是返回值：这条规则的全部意义是「管理员名下不再挂着
    长老的分工」，返回值看不出这一点。再建一个护工做对照，防止删除条件写宽成
    「删整张表」也能过。
    """
    promoted = await create_staff(
        session,
        name="待升管理员",
        role="care_worker",
        phone="13800000012",
        password="Passw0rd!",
        elder_ids=[ELDER_A, ELDER_B],
    )
    bystander = await create_staff(
        session,
        name="对照护工",
        role="care_worker",
        phone="13800000013",
        password="Passw0rd!",
        elder_ids=[ELDER_A],
    )
    await session.flush()
    assert await _assignment_count(session, promoted.staff_id) == 2
    assert await _assignment_count(session, bystander.staff_id) == 1

    updated = await update_staff(session, staff_id=promoted.staff_id, role="admin")

    assert updated.role == "admin"
    assert updated.token_version == 1, "改档必须同时作废旧 Token（AC-24）"
    assert await _assignment_count(session, promoted.staff_id) == 0
    assert await _assignment_count(session, bystander.staff_id) == 1, "别人的绑定不能被误删"

    row = next(
        r for r in await _audit_rows(session, "staff.update") if r.resource_id == promoted.staff_id
    )
    assert row.detail["assignments_cleared"] == 2, row.detail


async def test_update_staff_not_to_admin_keeps_assignments(session: AsyncSession) -> None:
    """护工 → 医生不清绑定（医生的关系是另一行 relation，由后续分配流程处理）。"""
    moved = await create_staff(
        session,
        name="转医生",
        role="care_worker",
        phone="13800000014",
        password="Passw0rd!",
        elder_ids=[ELDER_A],
    )
    await session.flush()

    await update_staff(session, staff_id=moved.staff_id, role="doctor")

    assert await _assignment_count(session, moved.staff_id) == 1


# ------------------------------------------------------------------ create_guardian


async def test_create_guardian_conflicts_with_staff_phone(session: AsyncSession) -> None:
    with pytest.raises(StaffConflictError):
        await create_guardian(
            session, name="撞号家属", phone=EXISTING_STAFF_PHONE, password="Passw0rd!"
        )


async def test_create_guardian_success(session: AsyncSession) -> None:
    guardian = await create_guardian(
        session, name="张女士", phone="13900000020", password="Passw0rd!"
    )
    assert guardian.status == "active"
    assert guardian.phone == "13900000020"
    assert guardian.password_hash.startswith("$2b$")


# ------------------------------------------------------------------ bind_guardian


async def test_bind_guardian_elder_not_found(session: AsyncSession) -> None:
    with pytest.raises(ElderNotFoundError):
        await bind_guardian(
            session, elder_id=uuid.uuid4(), guardian_id=EXISTING_GUARDIAN_ID, relation="女儿"
        )


async def test_bind_guardian_guardian_not_found(session: AsyncSession) -> None:
    with pytest.raises(GuardianNotFoundError):
        await bind_guardian(session, elder_id=ELDER_A, guardian_id=uuid.uuid4(), relation="女儿")


async def test_bind_guardian_already_bound(session: AsyncSession) -> None:
    await bind_guardian(
        session, elder_id=ELDER_A, guardian_id=EXISTING_GUARDIAN_ID, relation="女儿"
    )
    with pytest.raises(GuardianAlreadyBoundError):
        await bind_guardian(
            session, elder_id=ELDER_A, guardian_id=EXISTING_GUARDIAN_ID, relation="女儿"
        )


async def test_bind_guardian_limit_exceeded(session: AsyncSession) -> None:
    extra = []
    for index in range(MAX_ACTIVE_GUARDIANS_PER_ELDER + 1):
        extra.append(
            GuardianAccount(
                guardian_id=uuid.uuid4(),
                name=f"家属{index}",
                phone=f"1390000003{index}",
                password_hash="$2b$12$placeholderplaceholderplaceholderplaceholder",
                status="active",
            )
        )
    session.add_all(extra)
    await session.flush()

    for index in range(MAX_ACTIVE_GUARDIANS_PER_ELDER):
        await bind_guardian(
            session,
            elder_id=ELDER_B,
            guardian_id=extra[index].guardian_id,
            relation="儿子",
        )

    with pytest.raises(GuardianLimitExceededError):
        await bind_guardian(
            session,
            elder_id=ELDER_B,
            guardian_id=extra[-1].guardian_id,
            relation="儿子",
        )


async def test_bind_guardian_unbound_does_not_consume_quota(session: AsyncSession) -> None:
    """`unbound` 是解绑保痕（AC-24），不该继续占 2 个名额。"""
    first = await bind_guardian(
        session, elder_id=ELDER_A, guardian_id=EXISTING_GUARDIAN_ID, relation="女儿"
    )
    second = GuardianAccount(
        guardian_id=uuid.uuid4(),
        name="家属乙",
        phone="13900000040",
        password_hash="$2b$12$placeholderplaceholderplaceholderplaceholder",
        status="active",
    )
    session.add(second)
    await session.flush()
    await bind_guardian(session, elder_id=ELDER_A, guardian_id=second.guardian_id, relation="儿子")

    # 满员：第三位被拒
    third = GuardianAccount(
        guardian_id=uuid.uuid4(),
        name="家属丙",
        phone="13900000041",
        password_hash="$2b$12$placeholderplaceholderplaceholderplaceholder",
        status="active",
    )
    session.add(third)
    await session.flush()
    with pytest.raises(GuardianLimitExceededError):
        await bind_guardian(
            session, elder_id=ELDER_A, guardian_id=third.guardian_id, relation="侄子"
        )

    # 解绑第一位后名额释放
    first.status = "unbound"
    await session.flush()
    await bind_guardian(session, elder_id=ELDER_A, guardian_id=third.guardian_id, relation="侄子")


async def test_bind_guardian_writes_active_status(session: AsyncSession) -> None:
    binding = await bind_guardian(
        session, elder_id=ELDER_A, guardian_id=EXISTING_GUARDIAN_ID, relation="女儿"
    )
    assert binding.status == "active"

    stored = (
        await session.execute(select(ElderGuardian.status).where(ElderGuardian.id == binding.id))
    ).scalar_one()
    assert stored == "active"


# --------------------------------------------------- 审计入库（FR-ADM-04 / AC-24）


async def _audit_rows(session: AsyncSession, action: str) -> list[Any]:
    """真库读 `audit_logs`：凭证与人员变更必须能在库里追到人，不拿日志当证据。"""
    stmt = select(
        AuditLog.actor_type,
        AuditLog.actor_id,
        AuditLog.action,
        AuditLog.resource_type,
        AuditLog.resource_id,
        AuditLog.detail,
        AuditLog.created_at,
    ).where(AuditLog.action == action)
    return list((await session.execute(stmt)).all())


async def test_create_staff_writes_audit_row(session: AsyncSession) -> None:
    """建员工：一条审计行，答得出谁/对谁/什么角色/绑了哪些长者，且不含手机号原文。"""
    staff = await create_staff(
        session,
        name="新护工",
        role="care_worker",
        phone="13800000011",
        password="Passw0rd!",
        elder_ids=[ELDER_A],
    )

    rows = await _audit_rows(session, "staff.create")
    assert len(rows) == 1, rows
    row = rows[0]
    assert row.actor_type == "staff"
    assert row.actor_id == AUDIT_ACTOR_ID
    assert row.resource_type == "staff_accounts"
    assert row.resource_id == staff.staff_id
    assert row.detail["role"] == "care_worker"
    assert row.detail["phone_masked"] == "138****0011"
    assert row.detail["assignments"] == [str(ELDER_A)]
    assert row.detail["relation"] == "primary_care"
    assert row.created_at is not None
    assert "13800000011" not in str(row.detail)


async def test_update_staff_writes_audit_row(session: AsyncSession) -> None:
    """停用员工 = 旧 Token 立即作废（AC-24），这条账必须留下凭证已吊销的证据。"""
    await update_staff(session, staff_id=EXISTING_STAFF_ID, status="disabled")

    rows = await _audit_rows(session, "staff.update")
    assert len(rows) == 1, rows
    row = rows[0]
    assert row.actor_type == "staff"
    assert row.actor_id == AUDIT_ACTOR_ID
    assert row.resource_type == "staff_accounts"
    assert row.resource_id == EXISTING_STAFF_ID
    assert row.detail["changed"] == ["status"]
    assert row.detail["status"] == "disabled"
    assert row.detail["token_version"] == 1
    assert row.detail["credentials_revoked"] is True


async def test_create_guardian_writes_audit_row(session: AsyncSession) -> None:
    guardian = await create_guardian(
        session, name="新家属", phone="13900000011", password="Passw0rd!"
    )

    rows = await _audit_rows(session, "guardian.create")
    assert len(rows) == 1, rows
    row = rows[0]
    assert row.actor_type == "staff"
    assert row.actor_id == AUDIT_ACTOR_ID
    assert row.resource_type == "guardian_accounts"
    assert row.resource_id == guardian.guardian_id
    assert row.detail["phone_masked"] == "139****0011"
    assert row.detail["status"] == "active"
    assert "13900000011" not in str(row.detail)


async def test_bind_guardian_writes_audit_row(session: AsyncSession) -> None:
    binding = await bind_guardian(
        session, elder_id=ELDER_A, guardian_id=EXISTING_GUARDIAN_ID, relation="child"
    )

    rows = await _audit_rows(session, "guardian.bind")
    assert len(rows) == 1, rows
    row = rows[0]
    assert row.actor_type == "staff"
    assert row.actor_id == AUDIT_ACTOR_ID
    assert row.resource_type == "elder_guardians"
    assert row.resource_id == binding.id
    assert row.detail["elder_id"] == str(ELDER_A)
    assert row.detail["guardian_id"] == str(EXISTING_GUARDIAN_ID)
    assert row.detail["relation"] == "child"
    assert row.detail["status"] == "active"


async def test_missing_identity_blocks_creation_before_writing(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有请求身份时**什么都不写**：连员工行都不该出现，更不能出现无主审计。

    「没有上下文」的真实形态是 `get_identity()` 返回 None（调度/internal 直调服务时就是这样），
    这里按这个形态模拟，而不是把身份换成某个兜底 actor。
    """
    monkeypatch.setattr(staff_service, "get_identity", lambda: None)

    with pytest.raises(staff_service.StaffServiceError, match="审计主体"):
        await create_staff(
            session,
            name="无主体员工",
            role="doctor",
            phone="13700000099",
            password="Passw0rd!",
        )

    count = (
        await session.execute(
            select(func.count())
            .select_from(StaffAccount)
            .where(StaffAccount.phone == "13700000099")
        )
    ).scalar_one()
    assert count == 0
    assert await _audit_rows(session, "staff.create") == []


async def test_audit_failure_aborts_staff_creation(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """负例：审计写不进去时建号必须失败——「建了号但查不到谁建的」是不可接受的。"""

    async def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("audit_logs is unavailable")

    monkeypatch.setattr(staff_service, "write_audit", _boom)

    with pytest.raises(RuntimeError, match="audit_logs is unavailable"):
        await create_staff(
            session,
            name="审计故障员工",
            role="doctor",
            phone="13700000098",
            password="Passw0rd!",
        )

    await session.rollback()

    count = (
        await session.execute(
            select(func.count())
            .select_from(StaffAccount)
            .where(StaffAccount.phone == "13700000098")
        )
    ).scalar_one()
    assert count == 0
    assert await _audit_rows(session, "staff.create") == []
