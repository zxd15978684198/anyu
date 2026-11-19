"""迭代 9 · W4a：审计查询与导出 REST（`app/api/audit_export.py`）。

覆盖的验收条款：

- **仅 admin**：`GET /api/audit/logs` 与 `GET /api/audit/logs/export` 对医生/护工/家属/设备
  一律 `403` + `code=ROLE_FORBIDDEN`，且**先写一条越权审计再抛**（`action` 分别为
  `audit.read` / `audit.export`，`detail.reason='non_admin'`）。
- **分页查询**：`{items, total, limit, offset}` 外壳，`created_at DESC, audit_id DESC` 稳定倒序。
- **导出**：`application/x-ndjson`，一行一条、已脱敏、最多 1000 行；导出本身留痕
  `action='audit.export'`（`detail.rows` 与 `detail.filters` 可回答"导走了多大范围"）。
- **trace_id 透传**：入站 `X-Trace-Id` → 响应头 + 契约错误体 + 审计 detail 三处同源。
- **非法参数 422**：`actor_type` 枚举、UUID 格式、`limit` 越界由 FastAPI 拦；
  时区窗反向这类语义问题由服务层 `AuditQueryError` → `422 VALIDATION_ERROR`。

## 测试怎么不污染库

同 `tests/test_memory_api.py`：请求会话走 `get_session` 的 override（`create_savepoint`，
端点内的 `commit()` 只释放保存点），越权/导出审计的"自己开会话"用 `factory` fixture 钉到
同一条测试连接，整场测试结束后外层事务统一回滚。

## 为什么用例都带时间窗

`audit_logs` 是只增表，开发库里有历史行。种子行放在 **2020-01-01** 这个项目诞生前的死窗口，
每次查询都带 `start/end`，`total` 才是个可复现的数字（7）。
"""

from __future__ import annotations

import datetime
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from tests.conftest import require_db_connection

from app.api import audit_export as audit_api
from app.core.auth import get_current_principal
from app.core.config import get_settings
from app.core.runtime_context import Identity
from app.core.trace import TraceIdMiddleware
from app.db import get_session
from app.models import AuditLog
from app.services import audit_service

BASE = datetime.datetime(2020, 1, 1, 8, 0, tzinfo=datetime.UTC)
WINDOW_START = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)
WINDOW_END = datetime.datetime(2020, 1, 2, tzinfo=datetime.UTC)
IN_WINDOW = 7

NIL = uuid.UUID(int=0)

#: 合法的入站 trace_id（`app/core/context.py` 只认 8~64 位安全字符）。
CLIENT_TRACE = "w4a-trace-0001"


def _iso(value: datetime.datetime) -> str:
    """UTC 时间用 `Z` 收尾：`+00:00` 里的 `+` 进了 query string 会被解成空格。"""
    return value.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


#: 查询串：窗口 + 满页，拿到的就是种子行 r1..r7。
WINDOW_QUERY = f"start={_iso(WINDOW_START)}&end={_iso(WINDOW_END)}&limit=50"


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


@pytest.fixture
async def factory(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> async_sessionmaker[AsyncSession]:
    """把服务层"自己开会话"的留痕钉在同一条测试连接上。"""
    bind = db.bind
    assert bind is not None
    maker = async_sessionmaker(
        bind=bind,  # type: ignore[arg-type]
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    monkeypatch.setattr(audit_service, "session_factory", maker)
    return maker


@dataclass
class Seed:
    admin_id: uuid.UUID
    doctor_id: uuid.UUID
    care_worker_id: uuid.UUID
    guardian_id: uuid.UUID
    device_id: uuid.UUID
    rows: dict[str, uuid.UUID]


async def _seed(db: AsyncSession) -> Seed:
    admin_id, doctor_id, care_worker_id = (uuid.uuid4() for _ in range(3))
    guardian_id, device_id = (uuid.uuid4() for _ in range(2))
    resource = uuid.uuid4()

    specs: list[dict[str, Any]] = [
        {
            "key": "r1",
            "actor_type": "staff",
            "actor_id": admin_id,
            "action": "memory.read",
            "resource_type": "memory_chunks",
            "resource_id": resource,
            # 手机号同时出现在敏感键与自由文本里：导出必须两处都打码
            "detail": {
                "trace_id": "trace-r1",
                "phone": "13800138000",
                "note": "家属号码 13800138000 已登记",
            },
            "created_at": BASE,
        },
        {
            "key": "r2",
            "actor_type": "staff",
            "actor_id": doctor_id,
            "action": "medication.update",
            "resource_type": "medication_plans",
            "resource_id": resource,
            "detail": {"trace_id": "trace-r2"},
            "created_at": BASE + datetime.timedelta(seconds=1),
        },
        {
            "key": "r3",
            "actor_type": "guardian",
            "actor_id": guardian_id,
            "action": "memory.read",
            "resource_type": "memory_chunks",
            "resource_id": resource,
            "detail": {"trace_id": "trace-r3"},
            "created_at": BASE + datetime.timedelta(seconds=2),
        },
        {
            "key": "r4",
            "actor_type": "system",
            "actor_id": NIL,
            "action": "scheduler.tick",
            "resource_type": "scheduler",
            "resource_id": resource,
            "detail": None,
            "created_at": BASE + datetime.timedelta(seconds=3),
        },
        {
            "key": "r5",
            "actor_type": "device",
            "actor_id": device_id,
            "action": "device.heartbeat",
            "resource_type": "devices",
            "resource_id": resource,
            "detail": {"trace_id": "trace-r5"},
            "created_at": BASE + datetime.timedelta(seconds=4),
        },
        {
            "key": "r6",
            "actor_type": "staff",
            "actor_id": admin_id,
            "action": "memory.read",
            "resource_type": "memory_chunks",
            "resource_id": resource,
            "detail": {"trace_id": "trace-r6"},
            "created_at": BASE + datetime.timedelta(seconds=5),
        },
        {
            "key": "r7",
            "actor_type": "staff",
            "actor_id": care_worker_id,
            "action": "memory.read",
            "resource_type": "memory_chunks",
            "resource_id": resource,
            "detail": {"trace_id": "trace-r7"},
            # 与 r6 同毫秒：排序必须由 audit_id 决出
            "created_at": BASE + datetime.timedelta(seconds=5),
        },
        {
            "key": "r8",
            "actor_type": "staff",
            "actor_id": admin_id,
            "action": "dashboard.view",
            "resource_type": "dashboard",
            "resource_id": resource,
            "detail": {"trace_id": "trace-r8"},
            "created_at": datetime.datetime(2019, 12, 31, 23, 59, 59, tzinfo=datetime.UTC),
        },
    ]

    rows: dict[str, uuid.UUID] = {}
    payload: list[dict[str, Any]] = []
    for spec in specs:
        item = dict(spec)
        key = item.pop("key")
        audit_id = uuid.uuid4()
        rows[key] = audit_id
        payload.append({"audit_id": audit_id, **item})

    await db.execute(AuditLog.__table__.insert(), payload)
    await db.flush()

    return Seed(
        admin_id=admin_id,
        doctor_id=doctor_id,
        care_worker_id=care_worker_id,
        guardian_id=guardian_id,
        device_id=device_id,
        rows=rows,
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
    application.include_router(audit_api.router)

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


def _as_guardian(seed: Seed) -> Identity:
    return Identity(kind="guardian", guardian_id=str(seed.guardian_id))


def _as_device(seed: Seed) -> Identity:
    return Identity(kind="device", device_id=str(seed.device_id))


async def _audit_rows(db: AsyncSession, action: str, **filters: Any) -> list[AuditLog]:
    """按动作 + 主体过滤（`audit_logs` 只增，开发库有历史行，不过滤就会算进别人）。"""
    stmt = select(AuditLog).where(AuditLog.action == action)
    for column, value in filters.items():
        stmt = stmt.where(getattr(AuditLog, column) == value)
    return list(await db.scalars(stmt))


# --------------------------------------------------------------------- 查询端点


async def test_admin_lists_logs_with_pagination_envelope(
    client: Bundle, seeded: Seed, factory: async_sessionmaker[AsyncSession]
) -> None:
    client.holder[:] = [_as_admin(seeded)]
    response = await client.client.get(f"/api/audit/logs?{WINDOW_QUERY}")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"items", "total", "limit", "offset"}
    assert body["total"] == IN_WINDOW
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert len(body["items"]) == IN_WINDOW
    assert set(body["items"][0]) == set(audit_service.EXPORT_KEYS)


async def test_admin_list_is_descending_and_tie_broken(
    client: Bundle, seeded: Seed, factory: async_sessionmaker[AsyncSession]
) -> None:
    client.holder[:] = [_as_admin(seeded)]
    response = await client.client.get(f"/api/audit/logs?{WINDOW_QUERY}")

    items = response.json()["items"]
    stamps = [item["created_at"] for item in items]
    assert stamps == sorted(stamps, reverse=True)
    # 同毫秒的 r6/r7 排在最新位置，且两次请求顺序完全一致
    assert {items[0]["audit_id"], items[1]["audit_id"]} == {
        str(seeded.rows["r6"]),
        str(seeded.rows["r7"]),
    }
    again = await client.client.get(f"/api/audit/logs?{WINDOW_QUERY}")
    assert [item["audit_id"] for item in again.json()["items"]] == [
        item["audit_id"] for item in items
    ]


async def test_admin_list_serialises_trace_id_and_detail(
    client: Bundle, seeded: Seed, factory: async_sessionmaker[AsyncSession]
) -> None:
    client.holder[:] = [_as_admin(seeded)]
    response = await client.client.get(
        f"/api/audit/logs?{WINDOW_QUERY}&action=memory.read&actor_id={seeded.admin_id}"
    )

    items = response.json()["items"]
    # admin 的 memory.read 是 r1 与 r6
    assert {item["audit_id"] for item in items} == {
        str(seeded.rows["r1"]),
        str(seeded.rows["r6"]),
    }
    r1 = next(item for item in items if item["audit_id"] == str(seeded.rows["r1"]))
    assert r1["trace_id"] == "trace-r1"
    assert r1["actor_type"] == "staff"
    assert r1["created_at"] == "2020-01-01T08:00:00Z" or r1["created_at"].startswith(
        "2020-01-01T08:00:00"
    )
    # 手机号在列表接口同样不能原样出现
    assert "13800138000" not in json.dumps(r1["detail"], ensure_ascii=False)


async def test_admin_list_filters_by_actor_type(
    client: Bundle, seeded: Seed, factory: async_sessionmaker[AsyncSession]
) -> None:
    client.holder[:] = [_as_admin(seeded)]
    response = await client.client.get(f"/api/audit/logs?{WINDOW_QUERY}&actor_type=guardian")

    body = response.json()
    assert body["total"] == 1
    assert [item["audit_id"] for item in body["items"]] == [str(seeded.rows["r3"])]


async def test_admin_list_pagination_has_no_gaps(
    client: Bundle, seeded: Seed, factory: async_sessionmaker[AsyncSession]
) -> None:
    client.holder[:] = [_as_admin(seeded)]
    seen: list[str] = []
    offset = 0
    while True:
        response = await client.client.get(
            f"/api/audit/logs?start={_iso(WINDOW_START)}"
            f"&end={_iso(WINDOW_END)}&limit=3&offset={offset}"
        )
        assert response.status_code == 200
        page = response.json()["items"]
        if not page:
            break
        seen.extend(item["audit_id"] for item in page)
        offset += 3

    assert len(seen) == len(set(seen)) == IN_WINDOW


async def test_admin_list_null_detail_is_null(
    client: Bundle, seeded: Seed, factory: async_sessionmaker[AsyncSession]
) -> None:
    client.holder[:] = [_as_admin(seeded)]
    response = await client.client.get(f"/api/audit/logs?{WINDOW_QUERY}&action=scheduler.tick")

    item = response.json()["items"][0]
    assert item["detail"] is None
    assert item["trace_id"] is None  # 字段在、值为 None，键集合不随数据变化


# --------------------------------------------------------------------- 参数校验（422）


async def test_invalid_actor_type_is_422(
    client: Bundle, seeded: Seed, factory: async_sessionmaker[AsyncSession]
) -> None:
    client.holder[:] = [_as_admin(seeded)]
    response = await client.client.get(f"/api/audit/logs?{WINDOW_QUERY}&actor_type=nurse")

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert body["trace_id"]


async def test_invalid_uuid_is_422(
    client: Bundle, seeded: Seed, factory: async_sessionmaker[AsyncSession]
) -> None:
    client.holder[:] = [_as_admin(seeded)]
    response = await client.client.get(f"/api/audit/logs?{WINDOW_QUERY}&actor_id=not-a-uuid")

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_limit_over_max_is_422(
    client: Bundle, seeded: Seed, factory: async_sessionmaker[AsyncSession]
) -> None:
    client.holder[:] = [_as_admin(seeded)]
    response = await client.client.get(
        f"/api/audit/logs?limit={audit_service.MAX_LIMIT + 1}",
        headers={"X-Trace-Id": CLIENT_TRACE},
    )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert body["trace_id"] == CLIENT_TRACE
    assert response.headers["X-Trace-Id"] == CLIENT_TRACE


async def test_reversed_window_is_422(
    client: Bundle, seeded: Seed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """语义问题（start > end）由服务层 `AuditQueryError` 兜成 422。"""
    client.holder[:] = [_as_admin(seeded)]
    response = await client.client.get(
        f"/api/audit/logs?start={_iso(WINDOW_END)}&end={_iso(WINDOW_START)}"
    )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert "start" in body["detail"]


# --------------------------------------------------------------------- 越权（403 + 留痕）


async def test_doctor_forbidden_and_denial_audited(
    client: Bundle, seeded: Seed, db: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    client.holder[:] = [_as_doctor(seeded)]
    response = await client.client.get(f"/api/audit/logs?{WINDOW_QUERY}")

    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "ROLE_FORBIDDEN"
    assert body["trace_id"]

    rows = await _audit_rows(db, audit_service.READ_ACTION, actor_id=seeded.doctor_id)
    assert len(rows) == 1
    assert rows[0].actor_type == "staff"
    assert rows[0].resource_type == audit_service.AUDIT_RESOURCE_TYPE
    assert rows[0].detail is not None
    assert rows[0].detail["reason"] == "non_admin"
    assert rows[0].detail["role"] == "doctor"
    assert rows[0].detail["endpoint"] == "audit.logs"
    assert rows[0].detail["trace_id"] == body["trace_id"]


@pytest.mark.parametrize("who", ["guardian", "device"])
async def test_guardian_and_device_get_role_forbidden(
    client: Bundle,
    seeded: Seed,
    db: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    who: str,
) -> None:
    """非 admin 一律 `ROLE_FORBIDDEN`（本卡验收口径，不按 SCOPE_FORBIDDEN 分流）。"""
    identity = _as_guardian(seeded) if who == "guardian" else _as_device(seeded)
    client.holder[:] = [identity]

    response = await client.client.get(f"/api/audit/logs?{WINDOW_QUERY}")
    assert response.status_code == 403
    assert response.json()["code"] == "ROLE_FORBIDDEN"

    expected_actor = seeded.guardian_id if who == "guardian" else seeded.device_id
    rows = await _audit_rows(db, audit_service.READ_ACTION, actor_id=expected_actor)
    assert len(rows) == 1
    assert rows[0].actor_type == who
    assert rows[0].detail is not None
    assert rows[0].detail["reason"] == "non_admin"


async def test_denied_request_still_carries_client_trace_id(
    client: Bundle, seeded: Seed, db: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """trace_id 三处同源：响应头 / 错误体 / 审计 detail。"""
    client.holder[:] = [_as_doctor(seeded)]
    response = await client.client.get(
        f"/api/audit/logs?{WINDOW_QUERY}", headers={"X-Trace-Id": CLIENT_TRACE}
    )

    assert response.status_code == 403
    assert response.headers["X-Trace-Id"] == CLIENT_TRACE
    assert response.json()["trace_id"] == CLIENT_TRACE

    rows = await _audit_rows(db, audit_service.READ_ACTION, actor_id=seeded.doctor_id)
    assert rows[0].detail is not None
    assert rows[0].detail["trace_id"] == CLIENT_TRACE


# --------------------------------------------------------------------- 导出端点


async def test_export_returns_ndjson(
    client: Bundle, seeded: Seed, factory: async_sessionmaker[AsyncSession]
) -> None:
    client.holder[:] = [_as_admin(seeded)]
    response = await client.client.get(f"/api/audit/logs/export?{WINDOW_QUERY}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "no-store"

    lines = response.text.splitlines()
    assert len(lines) == IN_WINDOW
    records = [json.loads(line) for line in lines]
    assert all(tuple(record) == audit_service.EXPORT_KEYS for record in records)
    assert {record["audit_id"] for record in records} == {
        str(seeded.rows[key]) for key in ("r1", "r2", "r3", "r4", "r5", "r6", "r7")
    }


async def test_export_orders_descending(
    client: Bundle, seeded: Seed, factory: async_sessionmaker[AsyncSession]
) -> None:
    client.holder[:] = [_as_admin(seeded)]
    response = await client.client.get(f"/api/audit/logs/export?{WINDOW_QUERY}")

    records = [json.loads(line) for line in response.text.splitlines()]
    stamps = [record["created_at"] for record in records]
    assert stamps == sorted(stamps, reverse=True)
    assert records[0]["audit_id"] in {str(seeded.rows["r6"]), str(seeded.rows["r7"])}


@pytest.mark.skipif(
    not audit_service.REDACT_AVAILABLE,
    reason="app/core/redact.py 未落地，兜底是浅拷贝（不脱敏）",
)
async def test_export_masks_phone(
    client: Bundle, seeded: Seed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """导出内容已脱敏：手机号既不能出现在敏感键里，也不能出现在自由文本里。"""
    client.holder[:] = [_as_admin(seeded)]
    response = await client.client.get(
        f"/api/audit/logs/export?{WINDOW_QUERY}&action=memory.read&actor_id={seeded.admin_id}"
    )

    records = [json.loads(line) for line in response.text.splitlines()]
    # admin 的 memory.read 有 r1 与 r6；只有 r1 带手机号
    assert {record["audit_id"] for record in records} == {
        str(seeded.rows["r1"]),
        str(seeded.rows["r6"]),
    }
    r1 = next(record for record in records if record["audit_id"] == str(seeded.rows["r1"]))
    payload = json.dumps(r1["detail"], ensure_ascii=False)
    assert "13800138000" not in payload
    assert "138****8000" in payload


async def test_export_writes_audit_export_row(
    client: Bundle, seeded: Seed, db: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """导出动作自身留痕：`action='audit.export'` + 行数 + 过滤条件。"""
    client.holder[:] = [_as_admin(seeded)]
    response = await client.client.get(
        f"/api/audit/logs/export?{WINDOW_QUERY}&actor_type=staff",
        headers={"X-Trace-Id": CLIENT_TRACE},
    )
    assert response.status_code == 200

    rows = await _audit_rows(db, audit_service.EXPORT_ACTION, actor_id=seeded.admin_id)
    assert len(rows) == 1
    record = rows[0]
    assert record.actor_type == "staff"
    assert record.resource_type == audit_service.AUDIT_RESOURCE_TYPE
    assert record.resource_id == NIL
    assert record.detail is not None
    assert record.detail["rows"] == len(response.text.splitlines())
    assert record.detail["trace_id"] == CLIENT_TRACE
    assert record.detail["filters"]["actor_type"] == "staff"


async def test_export_respects_max_rows_cap(
    client: Bundle,
    seeded: Seed,
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上限由服务层常量控制（默认 1000）；这里收紧到 2 验证真的被截住了。"""
    monkeypatch.setattr(audit_service, "MAX_EXPORT_ROWS", 2)
    client.holder[:] = [_as_admin(seeded)]
    response = await client.client.get(f"/api/audit/logs/export?{WINDOW_QUERY}")

    assert response.status_code == 200
    assert len(response.text.splitlines()) == 2


async def test_export_empty_result_is_empty_body(
    client: Bundle, seeded: Seed, factory: async_sessionmaker[AsyncSession]
) -> None:
    client.holder[:] = [_as_admin(seeded)]
    response = await client.client.get(
        f"/api/audit/logs/export?start={_iso(WINDOW_START)}"
        f"&end={_iso(WINDOW_END)}&action=no.such.action"
    )

    assert response.status_code == 200
    assert response.content == b""
    # 空结果也要留下"有人导过一次空数据"的痕迹
    assert "attachment" in response.headers["content-disposition"]


async def test_export_doctor_forbidden_and_denial_audited(
    client: Bundle, seeded: Seed, db: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    client.holder[:] = [_as_doctor(seeded)]
    response = await client.client.get(
        f"/api/audit/logs/export?{WINDOW_QUERY}", headers={"X-Trace-Id": CLIENT_TRACE}
    )

    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "ROLE_FORBIDDEN"
    assert body["trace_id"] == CLIENT_TRACE

    rows = await _audit_rows(db, audit_service.EXPORT_ACTION, actor_id=seeded.doctor_id)
    assert len(rows) == 1
    assert rows[0].detail is not None
    assert rows[0].detail["reason"] == "non_admin"
    assert rows[0].detail["endpoint"] == "audit.logs.export"
    assert rows[0].detail["trace_id"] == CLIENT_TRACE


async def test_export_reversed_window_is_422(
    client: Bundle, seeded: Seed, factory: async_sessionmaker[AsyncSession]
) -> None:
    client.holder[:] = [_as_admin(seeded)]
    response = await client.client.get(
        f"/api/audit/logs/export?start={_iso(WINDOW_END)}&end={_iso(WINDOW_START)}"
    )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_export_denied_writes_no_data_but_still_audits(
    client: Bundle, seeded: Seed, db: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """403 分支不能先吐数据：响应体只有契约错误体，不含 ndjson 行。"""
    client.holder[:] = [_as_guardian(seeded)]
    response = await client.client.get(f"/api/audit/logs/export?{WINDOW_QUERY}")

    assert response.status_code == 403
    assert str(seeded.rows["r1"]) not in response.text
    rows = await _audit_rows(db, audit_service.EXPORT_ACTION, actor_id=seeded.guardian_id)
    assert len(rows) == 1
