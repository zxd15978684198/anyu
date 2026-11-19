"""迭代 7 · G 号卡：记忆 REST API（admin 独占 / 退房清数据留痕 / 标识不来自请求体）。

覆盖的验收条款：

- **仅管理员可读**（PRD §4.9 FR-MEM-02「管理员可协助查看」）：医生与护工
  `GET /memory/chunks` 必须 403 `ROLE_FORBIDDEN` 且落 `audit_logs`；家属 403
  `SCOPE_FORBIDDEN`。
- **退房清数据**（FR-ADM-09）：`DELETE /memory/elder/{elder_id}` 返回删除条数，
  且 `audit_logs` 里有一行 `delete_memory`（服务层 `delete_by_elder` 与业务同事务）。
- **软删而非物理删**：`memory_chunks.status` 落到 `deleted`，行还在——长期记忆的删除
  要可回查，不是 `DELETE FROM`。
- **标识不信任请求体**（AGENTS §4 第 9 条）：`elder_id` 只能来自路径/查询参数；
  DELETE 端点**没有**请求体，塞 body 里的 elder_id 不会被采信。

## 测试怎么不污染库

同 `tests/test_medication_api.py`：业务读写走 `db` fixture 的外层事务（请求内
`commit()` 只释放保存点），越权审计用 `isolated_session_factory` 钉到同一条测试连接。

## 服务层缺函数时

`memory_service.list_chunks` 由 C 并行补（本卡报告里已列期望签名）。缺它就返回
`503 SERVICE_NOT_READY`，对应用例按函数是否存在 skip；同时有一条**打桩**用例把
admin 200 这条路径（分页序列化）锁死在 API 层，不依赖服务层进度。
"""

from __future__ import annotations

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

from app.api import memory as memory_api
from app.core.auth import get_current_principal
from app.core.config import get_settings
from app.core.runtime_context import Identity
from app.core.trace import TraceIdMiddleware
from app.db import get_session
from app.models import AuditLog, Elder, MemoryChunk, StaffAccount
from app.services import memory_service, report_service


def _svc(name: str) -> object | None:
    """取 `memory_service` 的公开函数；未落地或未实现返回 None。"""
    candidate = getattr(memory_service, name, None)
    return candidate if callable(candidate) else None


needs_list_chunks = pytest.mark.skipif(
    _svc("list_chunks") is None,
    reason="memory_service.list_chunks 尚未落地（C 卡并行开发中）",
)

CONTENT_A = "女儿上周日来看过我，带了桂花糕。"
CONTENT_B = "我年轻时在杭州当过小学教员。"


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
    """外层事务：测试里所有写入最后整体回滚。"""
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
def isolated_session_factory(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> async_sessionmaker[AsyncSession]:
    """把越权审计的"自己开会话"钉在同一条测试连接上。"""
    bind = db.bind
    assert bind is not None
    factory = async_sessionmaker(
        bind=bind,  # type: ignore[arg-type]
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    monkeypatch.setattr(report_service, "session_factory", factory)
    return factory


@dataclass
class Seed:
    admin_id: uuid.UUID
    doctor_id: uuid.UUID
    care_worker_id: uuid.UUID
    guardian_id: uuid.UUID
    elder_id: uuid.UUID
    other_elder_id: uuid.UUID


async def _seed(db: AsyncSession) -> Seed:
    admin_id, doctor_id, care_worker_id = (uuid.uuid4() for _ in range(3))
    guardian_id, elder_id, other_elder_id = (uuid.uuid4() for _ in range(3))

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
        Elder.__table__.insert(),
        [
            {"elder_id": elder_id, "name": "张桂芬", "status": "active"},
            {"elder_id": other_elder_id, "name": "李振华", "status": "active"},
        ],
    )
    await db.flush()
    return Seed(
        admin_id=admin_id,
        doctor_id=doctor_id,
        care_worker_id=care_worker_id,
        guardian_id=guardian_id,
        elder_id=elder_id,
        other_elder_id=other_elder_id,
    )


async def _add_chunk(
    db: AsyncSession, elder_id: uuid.UUID, content: str, *, status: str = "active"
) -> MemoryChunk:
    chunk = MemoryChunk(
        chunk_id=uuid.uuid4(),
        elder_id=elder_id,
        content=content,
        category="private",
        importance=1.0,
        status=status,
    )
    db.add(chunk)
    await db.flush()
    return chunk


@pytest.fixture
async def seeded(db: AsyncSession) -> Seed:
    seed = await _seed(db)
    await _add_chunk(db, seed.elder_id, CONTENT_A)
    await _add_chunk(db, seed.elder_id, CONTENT_B)
    await _add_chunk(db, seed.other_elder_id, "另一个长者的记忆，不应跨人可见。")
    return seed


@dataclass
class Bundle:
    client: AsyncClient
    holder: list[Identity]


@pytest.fixture
async def client(db: AsyncSession) -> AsyncIterator[Bundle]:
    application = FastAPI()
    application.add_middleware(TraceIdMiddleware)
    application.include_router(memory_api.router)

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


async def _audit_rows(db: AsyncSession, action: str, **filters: Any) -> list[AuditLog]:
    """按动作 + 主体过滤。

    过滤是必须的，不是保险：`audit_logs` 是只增表，开发库里可能残留上一次跑测的
    同名 action 行；不断言 actor 就会把历史行算进本次结果（实测踩过：5 ≠ 1）。
    """
    stmt = select(AuditLog).where(AuditLog.action == action)
    for column, value in filters.items():
        stmt = stmt.where(getattr(AuditLog, column) == value)
    rows = await db.scalars(stmt)
    return list(rows)


# ------------------------------------------------------------- 只读端点（仅 admin）


async def test_doctor_cannot_list_chunks(client: Bundle, seeded: Seed, db: AsyncSession) -> None:
    """医生能看药单，但**不能**看长期记忆：403 且留痕。"""
    client.holder[:] = [_as_doctor(seeded)]

    resp = await client.client.get("/api/memory/chunks", params={"elder_id": str(seeded.elder_id)})

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "ROLE_FORBIDDEN"
    rows = await _audit_rows(db, "memory.read", actor_id=seeded.doctor_id)
    assert len(rows) == 1
    assert rows[0].actor_id == seeded.doctor_id
    assert rows[0].detail["reason"] == "non_admin"


async def test_care_worker_cannot_list_chunks(client: Bundle, seeded: Seed) -> None:
    client.holder[:] = [_as_care_worker(seeded)]

    resp = await client.client.get("/api/memory/chunks", params={"elder_id": str(seeded.elder_id)})

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "ROLE_FORBIDDEN"


async def test_guardian_cannot_list_chunks(client: Bundle, seeded: Seed) -> None:
    client.holder[:] = [_as_guardian(seeded)]

    resp = await client.client.get("/api/memory/chunks", params={"elder_id": str(seeded.elder_id)})

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "SCOPE_FORBIDDEN"


@needs_list_chunks
async def test_admin_lists_chunks_for_one_elder(client: Bundle, seeded: Seed) -> None:
    client.holder[:] = [_as_admin(seeded)]

    resp = await client.client.get("/api/memory/chunks", params={"elder_id": str(seeded.elder_id)})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    contents = {item["content"] for item in body["items"]}
    assert contents == {CONTENT_A, CONTENT_B}, "跨长者不可见"
    assert body["total"] == 2
    assert all(item["elder_id"] == str(seeded.elder_id) for item in body["items"])


async def test_invalid_status_is_rejected_at_the_edge(client: Bundle, seeded: Seed) -> None:
    """G3：`status` 只接三个合法值，非法值在 Pydantic 入口 422，不再漏成 500。

    修法刻意选「入口枚举」而不是「把 ValueError 笼统映射成 422」：后者会把服务层
    真正的编程错误也伪装成客户端参数问题。
    """
    client.holder[:] = [_as_admin(seeded)]

    resp = await client.client.get(
        "/api/memory/chunks",
        params={"elder_id": str(seeded.elder_id), "status": "archived"},
    )

    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert set(body) == {"detail", "code", "trace_id"}
    assert body["trace_id"] == resp.headers["x-trace-id"]


@needs_list_chunks
async def test_admin_list_excludes_deleted(client: Bundle, seeded: Seed, db: AsyncSession) -> None:
    await _add_chunk(db, seeded.elder_id, "已被取代的旧记忆", status="superseded")
    client.holder[:] = [_as_admin(seeded)]

    resp = await client.client.get(
        "/api/memory/chunks", params={"elder_id": str(seeded.elder_id), "status": "active"}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    assert all(item["status"] == "active" for item in body["items"])


async def test_admin_list_unknown_elder_returns_404(client: Bundle, seeded: Seed) -> None:
    client.holder[:] = [_as_admin(seeded)]

    resp = await client.client.get("/api/memory/chunks", params={"elder_id": str(uuid.uuid4())})

    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "NOT_FOUND"


async def test_admin_list_without_list_chunks_is_503(
    client: Bundle, seeded: Seed, monkeypatch: pytest.MonkeyPatch
) -> None:
    """服务层缺读函数时必须是**显式 503**，而不是 500 或偷偷在 API 层写 SQL。

    `list_chunks` 现在已落地，这条分支靠 `monkeypatch.delattr` 把函数摘掉来复现 ——
    否则 503 这条错误契约就再也没人守了（一旦有人把 `getattr` 兜底改成直接调用，
    线上表现会从「503 明说缺件」退化成「500 堆栈」）。
    """
    monkeypatch.delattr(memory_service, "list_chunks", raising=False)

    client.holder[:] = [_as_admin(seeded)]

    resp = await client.client.get("/api/memory/chunks", params={"elder_id": str(seeded.elder_id)})

    assert resp.status_code == 503, resp.text
    assert resp.json()["code"] == "SERVICE_NOT_READY"
    assert set(resp.json()) == {"detail", "code", "trace_id"}


async def test_admin_list_serialises_stubbed_chunks(
    client: Bundle, seeded: Seed, monkeypatch: pytest.MonkeyPatch
) -> None:
    """打桩服务层：admin 200 这条路径（分页 + 序列化）不依赖 C 的进度即可锁死。"""

    class _Row:
        def __init__(self, chunk_id: uuid.UUID, content: str) -> None:
            self.chunk_id = chunk_id
            self.elder_id = seeded.elder_id
            self.content = content
            self.category = "private"
            self.importance = 1.0
            self.status = "active"
            self.created_at = None
            self.last_accessed = None

    stub_id = uuid.uuid4()

    async def _stub(*args: Any, **kwargs: Any) -> list[Any]:
        return [_Row(stub_id, CONTENT_A)]

    monkeypatch.setattr(memory_api, "_svc_list_chunks", _stub)
    client.holder[:] = [_as_admin(seeded)]

    resp = await client.client.get(
        "/api/memory/chunks",
        params={"elder_id": str(seeded.elder_id), "limit": 1, "offset": 0},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["chunk_id"] == str(stub_id)
    assert body["items"][0]["content"] == CONTENT_A
    assert body["limit"] == 1


async def test_admin_list_rejects_oversized_page(client: Bundle, seeded: Seed) -> None:
    client.holder[:] = [_as_admin(seeded)]

    resp = await client.client.get(
        "/api/memory/chunks", params={"elder_id": str(seeded.elder_id), "limit": 10_000}
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "VALIDATION_ERROR"


# --------------------------------------------------------- 退房清数据（FR-ADM-09）


async def test_admin_delete_returns_count_and_is_audited(
    client: Bundle, seeded: Seed, db: AsyncSession
) -> None:
    client.holder[:] = [_as_admin(seeded)]

    resp = await client.client.delete(f"/api/memory/elder/{seeded.elder_id}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["elder_id"] == str(seeded.elder_id)
    assert body["deleted_chunks"] == 2
    assert body["audit_logged"] is True
    assert body["soft_delete"] is True
    assert len(body["trace_id"]) == 32

    # 软删：行还在，status 落到 deleted，其它长者的记忆不受影响。
    rows = list(
        await db.scalars(select(MemoryChunk).where(MemoryChunk.elder_id == seeded.elder_id))
    )
    assert len(rows) == 2
    assert {row.status for row in rows} == {"deleted"}

    audits = await _audit_rows(db, "delete_memory", resource_id=seeded.elder_id)
    assert len(audits) == 1
    assert audits[0].resource_id == seeded.elder_id
    assert audits[0].detail["deleted_chunks"] == 2
    assert audits[0].detail["reason"] == "checkout"
    assert audits[0].actor_id == seeded.admin_id

    # 别的长者一条都没删。
    other = list(
        await db.scalars(select(MemoryChunk).where(MemoryChunk.elder_id == seeded.other_elder_id))
    )
    assert [row.status for row in other] == ["active"]


async def test_delete_twice_is_idempotent(client: Bundle, seeded: Seed, db: AsyncSession) -> None:
    """第二次退房已无 active 行：返回 0 条、不报错，但**仍记一条 `deleted_chunks=0` 的审计**。

    G4 口径（2026-11-10 裁决）：删除是破坏性操作，重复调用本身就是「有人又按了一次」的
    证据，吞掉它等于丢掉重试痕迹；因此代码保留每条调用都写审计，本文档字符串与原实现
    相反的说法一并改正（契约 `docs/接口契约_记忆.md` §7.3）。
    """
    client.holder[:] = [_as_admin(seeded)]

    first = await client.client.delete(f"/api/memory/elder/{seeded.elder_id}")
    second = await client.client.delete(f"/api/memory/elder/{seeded.elder_id}")

    assert first.status_code == 200, first.text
    assert first.json()["deleted_chunks"] == 2
    assert second.status_code == 200, second.text
    assert second.json()["deleted_chunks"] == 0

    audits = await _audit_rows(db, "delete_memory", resource_id=seeded.elder_id)
    assert [row.detail["deleted_chunks"] for row in audits] == [2, 0]


async def test_doctor_cannot_delete_memory(client: Bundle, seeded: Seed, db: AsyncSession) -> None:
    client.holder[:] = [_as_doctor(seeded)]

    resp = await client.client.delete(f"/api/memory/elder/{seeded.elder_id}")

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "ROLE_FORBIDDEN"
    rows = await _audit_rows(db, "memory.delete", resource_id=seeded.elder_id)
    assert len(rows) == 1, "越权删除必须留痕（FR-ADM-09 全流程审计）"
    assert rows[0].resource_id == seeded.elder_id
    # 数据没被删。
    alive = list(
        await db.scalars(
            select(MemoryChunk).where(
                MemoryChunk.elder_id == seeded.elder_id, MemoryChunk.status == "active"
            )
        )
    )
    assert len(alive) == 2


async def test_elder_id_in_body_is_ignored(client: Bundle, seeded: Seed, db: AsyncSession) -> None:
    """AGENTS §4 第 9 条：目标长者只认路径参数，请求体里塞 elder_id 一律不采信。"""
    client.holder[:] = [_as_admin(seeded)]

    resp = await client.client.request(
        "DELETE",
        f"/api/memory/elder/{seeded.elder_id}",
        json={"elder_id": str(seeded.other_elder_id)},
    )

    assert resp.status_code in {200, 422}, resp.text
    assert resp.json().get("elder_id", str(seeded.elder_id)) == str(seeded.elder_id)
    other = list(
        await db.scalars(select(MemoryChunk).where(MemoryChunk.elder_id == seeded.other_elder_id))
    )
    assert [row.status for row in other] == ["active"], "请求体里的 elder_id 绝不能生效"


# -------------------------------------------------------------------- 错误契约


async def test_error_body_matches_contract(client: Bundle, seeded: Seed) -> None:
    client.holder[:] = [_as_doctor(seeded)]

    resp = await client.client.get("/api/memory/chunks", params={"elder_id": str(seeded.elder_id)})

    body = resp.json()
    assert resp.status_code == 403
    assert set(body) == {"detail", "code", "trace_id"}
    assert body["detail"]
    assert body["trace_id"] == resp.headers["x-trace-id"]
