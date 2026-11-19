"""W3 · 工牌签发 / 换会话 / 动作级 scope 门禁（FR-ADM-08）集成测试.

真实 DB + httpx.AsyncClient，口径沿用 `tests/test_auth_api.py`：
NullPool 引擎（绕开跨事件循环复用）、每用例一个事务最后 rollback、
seed 只 flush 不 commit，请求与 seed 共享同一个 session。

**只连独立测试库**：本文件夹具会 `TRUNCATE`，跑之前必须给
`ANYU_TEST_DATABASE_URL`（见 AGENTS §9 与 tests/conftest.py:59）。没给就会被
conftest 的 `require_db_*` 在连接阶段拦下（默认 fail，不静默 skip）。

覆盖（对应 task-3 验收 1–3）：
1. 管理员签发 → 201 + `staff_tokens` 一行 + `audit_logs` 一条 `staff_card.issue`；
2. 表外 scope / 非管理员 → 422 / 403，且**不写**令牌行；
3. 工牌换会话 → 仍是 `kind=staff_card`（不升格），`expires_in` 不超过卡剩余寿命；
4. 超 scope 调工单流转 → 403 `SCOPE_FORBIDDEN` + `audit_logs` 一条 `scope_denied`。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.security import decode_token, hash_password, sha256_hex
from app.db import get_session as _app_get_session
from app.models import ElderStaffAssignment, StaffAccount
from app.models.organization import Bed, Device, Elder, Room

_ADMIN_PHONE = "13800000011"
_CARE_PHONE = "13800000012"
_PASSWORD = "test123"
_DEVICE_SECRET = "w3-device-secret-plain"

_TRUNCATE = (
    "TRUNCATE audit_logs, staff_tokens, elder_staff_assignments, staff_accounts, "
    "devices, elders, beds, personas, rooms CASCADE"
)


@pytest.fixture
async def test_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(_TRUNCATE))
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
async def seeded(test_session: AsyncSession) -> dict[str, Any]:
    room = Room(room_no="B-201")
    test_session.add(room)
    await test_session.flush()

    bed = Bed(room_id=room.room_id, bed_no="1 号床")
    device = Device(
        room_id=room.room_id,
        token_hash=sha256_hex(_DEVICE_SECRET),
        status="online",
    )
    elder = Elder(name="李国强", status="active")
    admin = StaffAccount(
        name="王管理",
        role="admin",
        phone=_ADMIN_PHONE,
        password_hash=hash_password(_PASSWORD),
        token_version=0,
        status="active",
    )
    care = StaffAccount(
        name="赵护工",
        role="care_worker",
        phone=_CARE_PHONE,
        password_hash=hash_password(_PASSWORD),
        token_version=0,
        status="active",
    )
    test_session.add_all([bed, device, elder, admin, care])
    await test_session.flush()

    bed.elder_id = elder.elder_id
    device.elder_id = elder.elder_id
    test_session.add(
        ElderStaffAssignment(
            elder_id=elder.elder_id, staff_id=care.staff_id, relation="primary_care"
        )
    )
    await test_session.flush()

    return {
        "elder_id": elder.elder_id,
        "device_id": device.device_id,
        "admin_id": admin.staff_id,
        "care_id": care.staff_id,
    }


@pytest.fixture
async def app_for_test(test_session: AsyncSession, seeded: dict[str, Any]) -> FastAPI:
    from fastapi.responses import JSONResponse

    from app.api.auth import router as auth_router
    from app.api.work_orders import router as work_order_router
    from app.core.auth import (
        CredentialExpiredError,
        CredentialInvalidError,
        CredentialRevokedError,
    )
    from app.core.security import TokenExpiredError

    app = FastAPI()
    app.include_router(auth_router, prefix="/api")
    app.include_router(work_order_router)

    async def _override_get_session():
        yield test_session

    app.dependency_overrides[_app_get_session] = _override_get_session

    @app.exception_handler(CredentialInvalidError)
    async def _invalid(_req, exc):
        return JSONResponse(
            status_code=401,
            content={"detail": str(exc), "code": "TOKEN_INVALID", "trace_id": "-"},
        )

    @app.exception_handler(CredentialExpiredError)
    async def _expired(_req, exc):
        return JSONResponse(
            status_code=401,
            content={"detail": str(exc), "code": "TOKEN_EXPIRED", "trace_id": "-"},
        )

    @app.exception_handler(CredentialRevokedError)
    async def _revoked(_req, exc):
        return JSONResponse(
            status_code=401,
            content={"detail": str(exc), "code": "TOKEN_REVOKED", "trace_id": "-"},
        )

    @app.exception_handler(TokenExpiredError)
    async def _token_expired(_req, exc):
        return JSONResponse(
            status_code=401,
            content={"detail": "token has expired", "code": "TOKEN_EXPIRED", "trace_id": "-"},
        )

    return app


@pytest.fixture
async def ac(app_for_test: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app_for_test)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


async def _login(ac: httpx.AsyncClient, phone: str) -> str:
    resp = await ac.post("/api/sessions", json={"phone": phone, "password": _PASSWORD})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def _issue(
    ac: httpx.AsyncClient, *, admin_token: str, staff_id: uuid.UUID, seeded, scope: list[str]
) -> httpx.Response:
    return await ac.post(
        f"/api/staff/{staff_id}/cards",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"device_id": str(seeded["device_id"]), "scope": scope},
    )


# ---------------------------------------------------------------------------
# 验收 1：签发 201 + 落库 + 审计
# ---------------------------------------------------------------------------


async def test_admin_issues_card_201_and_writes_row(
    ac: httpx.AsyncClient, test_session: AsyncSession, seeded: dict[str, Any]
) -> None:
    admin_token = await _login(ac, _ADMIN_PHONE)
    resp = await _issue(
        ac,
        admin_token=admin_token,
        staff_id=seeded["care_id"],
        seeded=seeded,
        scope=["workorder:handle"],
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["scope"] == ["workorder:handle"]
    assert uuid.UUID(body["staff_id"]) == seeded["care_id"]
    assert uuid.UUID(body["device_id"]) == seeded["device_id"]
    assert body["token"]

    claims = decode_token(body["token"], expected_typ="staff_card")
    assert claims["kind"] == "staff_card"
    assert claims["scope"] == ["workorder:handle"]

    # 回查 DB（契约 §10：写操作必须回查，不只看 200/201）
    row = (
        await test_session.execute(
            text("SELECT staff_id, device_id, scope FROM staff_tokens WHERE token_hash = :h"),
            {"h": sha256_hex(body["token"])},
        )
    ).first()
    assert row is not None
    assert row[0] == seeded["care_id"]
    assert row[1] == seeded["device_id"]
    assert row[2] == "workorder:handle"

    audit = (
        await test_session.execute(
            text(
                "SELECT actor_type, action, resource_type FROM audit_logs "
                "WHERE action = 'staff_card.issue'"
            )
        )
    ).all()
    assert len(audit) == 1
    assert audit[0][0] == "staff"  # 工牌没有独立 actor_type（audit_logs 的 CHECK 只有四个）
    assert audit[0][2] == "staff_tokens"


async def test_scope_outside_allowlist_returns_422_without_row(
    ac: httpx.AsyncClient, test_session: AsyncSession, seeded: dict[str, Any]
) -> None:
    admin_token = await _login(ac, _ADMIN_PHONE)
    resp = await _issue(
        ac,
        admin_token=admin_token,
        staff_id=seeded["care_id"],
        seeded=seeded,
        scope=["workorder:hadle"],  # 拼错一个字符——白名单必须在签发时就拒
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "VALIDATION_ERROR"

    count = (await test_session.execute(text("SELECT count(*) FROM staff_tokens"))).scalar_one()
    assert count == 0


async def test_non_admin_cannot_issue_card(ac: httpx.AsyncClient, seeded: dict[str, Any]) -> None:
    care_token = await _login(ac, _CARE_PHONE)
    resp = await _issue(
        ac,
        admin_token=care_token,
        staff_id=seeded["care_id"],
        seeded=seeded,
        scope=["workorder:handle"],
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "ROLE_FORBIDDEN"


async def test_issue_for_unknown_staff_returns_404(
    ac: httpx.AsyncClient, seeded: dict[str, Any]
) -> None:
    admin_token = await _login(ac, _ADMIN_PHONE)
    resp = await _issue(
        ac,
        admin_token=admin_token,
        staff_id=uuid.uuid4(),
        seeded=seeded,
        scope=["workorder:handle"],
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# 验收 2：工牌换会话 —— 仍是 staff_card，不升格
# ---------------------------------------------------------------------------


async def test_exchange_card_keeps_staff_card_kind(
    ac: httpx.AsyncClient, seeded: dict[str, Any]
) -> None:
    admin_token = await _login(ac, _ADMIN_PHONE)
    issued = await _issue(
        ac,
        admin_token=admin_token,
        staff_id=seeded["care_id"],
        seeded=seeded,
        scope=["workorder:handle"],
    )
    assert issued.status_code == 201, issued.text
    card_token = issued.json()["token"]

    resp = await ac.post("/api/staff-card-sessions", json={"card_token": card_token})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["scope"] == ["workorder:handle"]
    assert uuid.UUID(body["device_id"]) == seeded["device_id"]
    assert uuid.UUID(body["elder_id"]) == seeded["elder_id"]

    claims = decode_token(body["access_token"], expected_typ="staff_card")
    assert claims["kind"] == "staff_card"  # 不升格为 kind=staff
    assert body["expires_in"] <= 7200  # min(卡剩余寿命, access TTL=2h)
    assert body["expires_in"] > 0


async def test_exchange_rejects_garbage_token(ac: httpx.AsyncClient) -> None:
    resp = await ac.post("/api/staff-card-sessions", json={"card_token": "not-a-jwt"})
    assert resp.status_code == 401, resp.text
    assert resp.json()["code"] == "TOKEN_INVALID"


# ---------------------------------------------------------------------------
# 验收 3：动作级 scope 门禁 —— 超 scope → 403 + 越权审计
# ---------------------------------------------------------------------------


async def test_card_without_scope_gets_403_and_audit(
    ac: httpx.AsyncClient, test_session: AsyncSession, seeded: dict[str, Any]
) -> None:
    admin_token = await _login(ac, _ADMIN_PHONE)
    issued = await _issue(
        ac,
        admin_token=admin_token,
        staff_id=seeded["care_id"],
        seeded=seeded,
        scope=["health:staff_form"],  # 合法 scope，但不覆盖 workorder:handle
    )
    assert issued.status_code == 201, issued.text
    # 注意：换会话这一步只是把卡再签一次；直接用卡令牌调流转端点即可
    card_token = issued.json()["token"]

    resp = await ac.post(
        f"/api/work-orders/{uuid.uuid4()}/transitions",
        headers={"Authorization": f"Bearer {card_token}"},
        json={"to_status": "handling"},
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "SCOPE_FORBIDDEN"

    # 越权审计走**独立会话**（write_audit_isolated），能在这里查到就说明真的提交了
    rows = (
        await test_session.execute(
            text(
                "SELECT actor_type, actor_id, action, resource_type, detail "
                "FROM audit_logs WHERE action = 'scope_denied'"
            )
        )
    ).all()
    assert len(rows) == 1
    actor_type, actor_id, _action, resource_type, detail = rows[0]
    assert actor_type == "staff"
    assert actor_id == seeded["care_id"]
    assert resource_type == "staff_tokens"
    assert detail["kind"] == "staff_card"
    assert detail["required"] == ["workorder:handle"]
    assert detail["granted"] == ["health:staff_form"]


async def _seed_order(session: AsyncSession, *, elder_id: uuid.UUID, order_no: str) -> uuid.UUID:
    """给某位长者建一张 `new` 工单（流转端点只认六态的合法迁移）。"""
    from app.models.work_order import WorkOrder

    order = WorkOrder(
        order_no=order_no,
        elder_id=elder_id,
        type="follow_up",
        severity="yellow",
        status="new",
    )
    session.add(order)
    await session.flush()
    return order.order_id


async def test_card_with_scope_completes_transition(
    ac: httpx.AsyncClient, test_session: AsyncSession, seeded: dict[str, Any]
) -> None:
    """ADM-08 闭环：持 `workorder:handle` 的工牌能真正走完状态流转（200）。

    工牌是同一名员工的第二套凭证，所以服务层的范围判定必须与普通 access **同解**：
    `care_worker` 在 primary_care 关系上的单子，工牌照样能处理。审计里工牌照旧
    记 `actor_type='staff'`（表 CHECK 只有四值），但 `detail.principal_kind` 能区分
    这次是工牌还是密码会话。
    """
    order_id = await _seed_order(
        test_session, elder_id=seeded["elder_id"], order_no="WO-W3CARD-0001"
    )
    admin_token = await _login(ac, _ADMIN_PHONE)
    issued = await _issue(
        ac,
        admin_token=admin_token,
        staff_id=seeded["care_id"],
        seeded=seeded,
        scope=["workorder:handle"],
    )
    assert issued.status_code == 201, issued.text

    resp = await ac.post(
        f"/api/work-orders/{order_id}/transitions",
        headers={"Authorization": f"Bearer {issued.json()['token']}"},
        json={"to_status": "acknowledged", "note": "已到床边"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "acknowledged"

    # 审计只回答「谁做了什么」：actor_type 是 staff（CHECK 无 staff_card），
    # 身份种类落在 detail.principal_kind 上，工牌动作因此可追溯。
    detail = (
        await test_session.execute(
            text(
                "SELECT detail FROM audit_logs WHERE action = 'work_order.transition' "
                "AND resource_id = :o"
            ),
            {"o": order_id},
        )
    ).scalar_one()
    assert detail["principal_kind"] == "staff_card"
    assert detail["to_status"] == "acknowledged"


async def test_card_out_of_staff_scope_is_rejected(
    ac: httpx.AsyncClient, test_session: AsyncSession, seeded: dict[str, Any], caplog
) -> None:
    """同一张工牌打**不属于该员工**的单子仍 403：范围过滤没被放宽。

    工牌接受的是「凭证种类」，不是「跳过 assignment 校验」——这条用例与
    `test_card_with_scope_completes_transition` 成对，一起锁住「同口径」。
    """
    other_elder = Elder(name="钱秀英", status="active")
    test_session.add(other_elder)
    await test_session.flush()
    other_order = await _seed_order(
        test_session, elder_id=other_elder.elder_id, order_no="WO-W3CARD-0002"
    )

    admin_token = await _login(ac, _ADMIN_PHONE)
    issued = await _issue(
        ac,
        admin_token=admin_token,
        staff_id=seeded["care_id"],
        seeded=seeded,
        scope=["workorder:handle"],
    )
    assert issued.status_code == 201, issued.text

    caplog.set_level(logging.INFO, logger="app.services.work_order_service")
    resp = await ac.post(
        f"/api/work-orders/{other_order}/transitions",
        headers={"Authorization": f"Bearer {issued.json()['token']}"},
        json={"to_status": "acknowledged"},
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "SCOPE_FORBIDDEN"

    # 结构化审计日志的主体是 `kind:principal_id`；工牌必须显示成 staff_card！
    actors = [rec.__dict__.get("actor") for rec in caplog.records]
    assert f"staff_card:{seeded['care_id']}" in actors


async def test_exchanged_session_token_works_on_next_request(
    ac: httpx.AsyncClient, test_session: AsyncSession, seeded: dict[str, Any]
) -> None:
    """换出来的短期令牌必须**真能用**：`resolve_principal` 会按 hash 回查 `staff_tokens`。

    回归用例（W7 服务级回归抓到）：`exchange_staff_card` 早先只签 JWT 不落行，
    换出来的令牌下一次请求就 `401 TOKEN_REVOKED`——「换会话」形同虚设。
    """
    order_id = await _seed_order(
        test_session, elder_id=seeded["elder_id"], order_no="WO-W3CARD-0003"
    )
    admin_token = await _login(ac, _ADMIN_PHONE)
    issued = await _issue(
        ac,
        admin_token=admin_token,
        staff_id=seeded["care_id"],
        seeded=seeded,
        scope=["workorder:handle"],
    )
    assert issued.status_code == 201, issued.text

    exchanged = await ac.post(
        "/api/staff-card-sessions", json={"card_token": issued.json()["token"]}
    )
    assert exchanged.status_code == 200, exchanged.text
    access_token = exchanged.json()["access_token"]

    # 新令牌在库里有行（可被退房按 device_id 吊销）；原卡那一行仍在。
    hashes = (
        (
            await test_session.execute(
                text("SELECT token_hash FROM staff_tokens WHERE staff_id = :s"),
                {"s": seeded["care_id"]},
            )
        )
        .scalars()
        .all()
    )
    assert sha256_hex(access_token) in hashes
    assert sha256_hex(issued.json()["token"]) in hashes

    resp = await ac.post(
        f"/api/work-orders/{order_id}/transitions",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"to_status": "acknowledged"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "acknowledged"

    # 原卡不被换取覆盖：同一张卡还能再换一次
    again = await ac.post("/api/staff-card-sessions", json={"card_token": issued.json()["token"]})
    assert again.status_code == 200, again.text
