"""FR-MEM-02 · 长者本人端三个端点（`/api/memory/me/*`）。

覆盖的口径（契约 `docs/接口契约_记忆.md` §6）：

- **本人只看自己**：`GET /api/memory/me/chunks` 只返回设备令牌绑定长者的条目，默认只列
  `active`；别人的条目一条都看不到。
- **`elder_id` 只从服务端身份上下文取**（AGENTS §4 第 9 条）：三个端点没有 `elder_id`
  查询参数，请求体里塞同名值被忽略（`MemoryCorrectIn` 都没有这个字段）。
- **跨长者一律 404**：不是 403——403 会泄露「这条记录存在但不属于你」，是可探测面。
- **纠正只改正文**：`content` 变了、`importance` 不变；撞长度上限在 Pydantic 入口 422。
- **删除是软删且有审计**：`status='deleted'`、行还在；重复删除返回 `deleted=false`
  但**照样写第二条审计**（G4 口径：重复操作是事实）。

## 测试怎么不污染库

同 `tests/test_memory_api.py`：业务读写走 `db` fixture 的外层事务（请求内 `commit()`
只释放保存点），越权审计用 `isolated_session_factory` 钉到同一条测试连接。
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

MY_CONTENT = "我年轻时在杭州当过小学教员。"
OTHER_CONTENT = "另一位长者的私房话，不该被看到。"


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
    """越权审计用「自己开会话」写，把它钉在同一条测试连接上才看得到。"""
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
    device_id: uuid.UUID
    doctor_id: uuid.UUID
    guardian_id: uuid.UUID
    elder_id: uuid.UUID
    other_elder_id: uuid.UUID


async def _seed(db: AsyncSession) -> Seed:
    device_id, doctor_id, guardian_id = (uuid.uuid4() for _ in range(3))
    elder_id, other_elder_id = (uuid.uuid4() for _ in range(2))
    # 长者姓名带随机后缀：开发库里已有同名演示数据，写死名字会让「同名多人」这类
    # 与本次无关的分支进入断言（test_medication_import 踩过）。
    suffix = uuid.uuid4().hex[:6]

    await db.execute(
        StaffAccount.__table__.insert(),
        [
            {"staff_id": doctor_id, "name": "医生李", "role": "doctor", "password_hash": "x"},
        ],
    )
    await db.execute(
        Elder.__table__.insert(),
        [
            {"elder_id": elder_id, "name": f"张桂芬{suffix}", "status": "active"},
            {"elder_id": other_elder_id, "name": f"李振华{suffix}", "status": "active"},
        ],
    )
    await db.flush()
    return Seed(
        device_id=device_id,
        doctor_id=doctor_id,
        guardian_id=guardian_id,
        elder_id=elder_id,
        other_elder_id=other_elder_id,
    )


async def _add_chunk(
    db: AsyncSession,
    elder_id: uuid.UUID,
    content: str,
    *,
    status: str = "active",
    importance: float = 1.0,
) -> MemoryChunk:
    chunk = MemoryChunk(
        chunk_id=uuid.uuid4(),
        elder_id=elder_id,
        content=content,
        category="private",
        importance=importance,
        status=status,
    )
    db.add(chunk)
    await db.flush()
    return chunk


@pytest.fixture
async def seeded(db: AsyncSession) -> Seed:
    seed = await _seed(db)
    await _add_chunk(db, seed.elder_id, MY_CONTENT)
    await _add_chunk(db, seed.elder_id, "我女儿上周日来看过我。", importance=0.4)
    await _add_chunk(db, seed.elder_id, "我已经搬去杭州住了。", status="superseded")
    await _add_chunk(db, seed.elder_id, "这句已经删掉了。", status="deleted")
    await _add_chunk(db, seed.other_elder_id, OTHER_CONTENT)
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


def _as_elder(seed: Seed) -> Identity:
    return Identity(kind="device", device_id=str(seed.device_id), elder_id=str(seed.elder_id))


def _as_unbound_device(seed: Seed) -> Identity:
    return Identity(kind="device", device_id=str(seed.device_id))


def _as_doctor(seed: Seed) -> Identity:
    return Identity(kind="staff", staff_id=str(seed.doctor_id), role="doctor")


def _as_guardian(seed: Seed) -> Identity:
    return Identity(kind="guardian", guardian_id=str(seed.guardian_id))


async def _audit_rows(db: AsyncSession, action: str, **filters: Any) -> list[AuditLog]:
    """按动作 + 任意列过滤（`audit_logs` 只增，开发库里有历史行，必须过滤）。"""
    stmt = select(AuditLog).where(AuditLog.action == action)
    for column, value in filters.items():
        stmt = stmt.where(getattr(AuditLog, column) == value)
    return list(await db.scalars(stmt))


async def _chunk(db: AsyncSession, chunk_id: uuid.UUID) -> MemoryChunk:
    """按主键取回**最新**行。

    `populate_existing=True` 不是保险：外层 `db` 会话的 identity map 里已有这条对象，
    请求内 upsert 走的是同一条连接的保存点，直接 `db.get()` 会拿回旧属性
    （实测：软删后仍读到 `status='active'`），必须强制重新 SELECT。
    """
    row = await db.get(MemoryChunk, chunk_id, populate_existing=True)
    assert row is not None
    return row


# --------------------------------------------------------------- 本人查看（GET）


async def test_list_my_chunks_only_mine_and_active_by_default(client: Bundle, seeded: Seed) -> None:
    """默认只列自己的 active 条目：superseded / deleted / 别人的都不出现。"""
    client.holder[:] = [_as_elder(seeded)]

    resp = await client.client.get("/api/memory/me/chunks")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["elder_id"] == str(seeded.elder_id)
    assert body["total"] == len(body["items"]) == 2
    assert {item["content"] for item in body["items"]} == {MY_CONTENT, "我女儿上周日来看过我。"}
    assert all(item["status"] == "active" for item in body["items"])
    assert all(item["elder_id"] == str(seeded.elder_id) for item in body["items"])


async def test_list_my_chunks_can_include_deleted_explicitly(client: Bundle, seeded: Seed) -> None:
    """显式传 `status=deleted` 才看得到已撤回的条目（默认看不见）。"""
    client.holder[:] = [_as_elder(seeded)]

    resp = await client.client.get("/api/memory/me/chunks", params={"status": "deleted"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [item["content"] for item in body["items"]] == ["这句已经删掉了。"]


# --------------------------------------------------------------- 本人纠正（PATCH）


async def test_correct_my_chunk_keeps_importance_and_audits(
    client: Bundle, seeded: Seed, db: AsyncSession
) -> None:
    """纠正只改正文：`importance` 不被重置，且同事务写 `memory.chunk.correct` 审计。"""
    client.holder[:] = [_as_elder(seeded)]
    chunks = list(
        await db.scalars(select(MemoryChunk).where(MemoryChunk.elder_id == seeded.elder_id))
    )
    target = next(row for row in chunks if float(row.importance) == pytest.approx(0.4))

    resp = await client.client.patch(
        f"/api/memory/me/chunks/{target.chunk_id}",
        json={"content": "  我女儿上周日来看过我，带了桂花糕。  "},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["chunk_id"] == str(target.chunk_id)
    assert body["content"] == "我女儿上周日来看过我，带了桂花糕。"  # 首尾空白被剥掉
    assert body["importance"] == pytest.approx(0.4), "正文纠正不得重置权重"

    refreshed = await _chunk(db, target.chunk_id)
    assert refreshed.content == "我女儿上周日来看过我，带了桂花糕。"
    assert float(refreshed.importance) == pytest.approx(0.4)

    audits = await _audit_rows(db, memory_service.ACTION_CHUNK_CORRECT, resource_id=target.chunk_id)
    assert len(audits) == 1
    assert audits[0].actor_type == "device"
    assert audits[0].actor_id == seeded.device_id
    # 正文不落审计：只留长度（AGENTS §8 不落原文）。
    assert "content" not in audits[0].detail
    assert audits[0].detail["content_length"] == len("我女儿上周日来看过我，带了桂花糕。")


async def test_correct_other_elders_chunk_is_404(
    client: Bundle, seeded: Seed, db: AsyncSession
) -> None:
    """跨长者纠正一律 404（不是 403，避免「存在但不属于你」的探测面），且原样不动。"""
    client.holder[:] = [_as_elder(seeded)]
    other = next(
        iter(
            await db.scalars(
                select(MemoryChunk).where(MemoryChunk.elder_id == seeded.other_elder_id)
            )
        )
    )

    resp = await client.client.patch(
        f"/api/memory/me/chunks/{other.chunk_id}", json={"content": "越权改一条。"}
    )

    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "NOT_FOUND"
    assert (await _chunk(db, other.chunk_id)).content == OTHER_CONTENT
    assert await _audit_rows(db, memory_service.ACTION_CHUNK_CORRECT) == []


async def test_correct_too_long_content_is_422(
    client: Bundle, seeded: Seed, db: AsyncSession
) -> None:
    """201 字在 Pydantic 入口就被拒（422 `VALIDATION_ERROR`），不进服务层、不改库。"""
    client.holder[:] = [_as_elder(seeded)]
    mine = next(
        iter(await db.scalars(select(MemoryChunk).where(MemoryChunk.elder_id == seeded.elder_id)))
    )

    resp = await client.client.patch(
        f"/api/memory/me/chunks/{mine.chunk_id}", json={"content": "字" * 201}
    )

    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert set(body) == {"detail", "code", "trace_id"}
    assert (await _chunk(db, mine.chunk_id)).content == mine.content


async def test_correct_blank_content_is_422(client: Bundle, seeded: Seed, db: AsyncSession) -> None:
    """只有空白通过 `min_length=1`，由服务层 `_normalise_content` 拒掉并映射 422。"""
    client.holder[:] = [_as_elder(seeded)]
    mine = next(
        iter(await db.scalars(select(MemoryChunk).where(MemoryChunk.elder_id == seeded.elder_id)))
    )

    resp = await client.client.patch(
        f"/api/memory/me/chunks/{mine.chunk_id}", json={"content": "   "}
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "VALIDATION_ERROR"
    assert (await _chunk(db, mine.chunk_id)).content == mine.content


async def test_correct_deleted_chunk_is_404(client: Bundle, seeded: Seed, db: AsyncSession) -> None:
    """已撤回的条目不能再改：对本人端而言它已经不存在（404）。"""
    client.holder[:] = [_as_elder(seeded)]
    deleted = next(
        iter(
            await db.scalars(
                select(MemoryChunk).where(
                    MemoryChunk.elder_id == seeded.elder_id, MemoryChunk.status == "deleted"
                )
            )
        )
    )

    resp = await client.client.patch(
        f"/api/memory/me/chunks/{deleted.chunk_id}", json={"content": "复活一下。"}
    )

    assert resp.status_code == 404, resp.text


async def test_body_elder_id_is_ignored(client: Bundle, seeded: Seed, db: AsyncSession) -> None:
    """AGENTS §4 第 9 条：body 里的 `elder_id` 一律不采信（模型里就没这个字段）。"""
    client.holder[:] = [_as_elder(seeded)]
    mine = next(
        iter(await db.scalars(select(MemoryChunk).where(MemoryChunk.elder_id == seeded.elder_id)))
    )
    other = next(
        iter(
            await db.scalars(
                select(MemoryChunk).where(MemoryChunk.elder_id == seeded.other_elder_id)
            )
        )
    )

    resp = await client.client.patch(
        f"/api/memory/me/chunks/{mine.chunk_id}",
        json={"content": "只改我自己的。", "elder_id": str(seeded.other_elder_id)},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["elder_id"] == str(seeded.elder_id)
    assert (await _chunk(db, mine.chunk_id)).content == "只改我自己的。"
    assert (await _chunk(db, other.chunk_id)).content == OTHER_CONTENT


# --------------------------------------------------------------- 本人删除（DELETE）


async def test_delete_my_chunk_soft_deletes_and_audits(
    client: Bundle, seeded: Seed, db: AsyncSession
) -> None:
    """软删：`status='deleted'`、行还在，并写一条 `memory.chunk.delete` 审计。"""
    client.holder[:] = [_as_elder(seeded)]
    mine = next(
        iter(await db.scalars(select(MemoryChunk).where(MemoryChunk.elder_id == seeded.elder_id)))
    )

    resp = await client.client.delete(f"/api/memory/me/chunks/{mine.chunk_id}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted"] is True
    assert body["status"] == "deleted"
    assert body["soft_delete"] is True

    row = await _chunk(db, mine.chunk_id)  # 行还在 = 软删
    assert row.status == "deleted"

    audits = await _audit_rows(db, memory_service.ACTION_CHUNK_DELETE, resource_id=mine.chunk_id)
    assert len(audits) == 1
    assert audits[0].actor_type == "device"
    assert audits[0].detail["already_deleted"] is False


async def test_delete_other_elders_chunk_is_404(
    client: Bundle, seeded: Seed, db: AsyncSession
) -> None:
    """跨长者删除一律 404，且对方条目原样 active。"""
    client.holder[:] = [_as_elder(seeded)]
    other = next(
        iter(
            await db.scalars(
                select(MemoryChunk).where(MemoryChunk.elder_id == seeded.other_elder_id)
            )
        )
    )

    resp = await client.client.delete(f"/api/memory/me/chunks/{other.chunk_id}")

    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "NOT_FOUND"
    assert (await _chunk(db, other.chunk_id)).status == "active"
    assert await _audit_rows(db, memory_service.ACTION_CHUNK_DELETE) == []


async def test_delete_twice_returns_false_and_writes_second_audit(
    client: Bundle, seeded: Seed, db: AsyncSession
) -> None:
    """重复删除：200 + `deleted=false`，但**仍写第二条审计**（`already_deleted=true`）。

    G4 口径：删除是破坏性操作，重复点击本身是事实，吞掉审计等于丢痕迹。
    """
    client.holder[:] = [_as_elder(seeded)]
    mine = next(
        iter(await db.scalars(select(MemoryChunk).where(MemoryChunk.elder_id == seeded.elder_id)))
    )

    first = await client.client.delete(f"/api/memory/me/chunks/{mine.chunk_id}")
    second = await client.client.delete(f"/api/memory/me/chunks/{mine.chunk_id}")

    assert first.json()["deleted"] is True
    assert second.status_code == 200, second.text
    assert second.json()["deleted"] is False

    audits = await _audit_rows(db, memory_service.ACTION_CHUNK_DELETE, resource_id=mine.chunk_id)
    assert [row.detail["already_deleted"] for row in audits] == [False, True]


# ------------------------------------------------------- 非长者主体 / 未绑定设备


async def test_staff_and_guardian_are_forbidden(
    client: Bundle, seeded: Seed, db: AsyncSession
) -> None:
    """员工与家属令牌 403 `SCOPE_FORBIDDEN`：本人端只认设备主体（不是 401）。"""
    for identity in (_as_doctor(seeded), _as_guardian(seeded)):
        client.holder[:] = [identity]
        resp = await client.client.get("/api/memory/me/chunks")
        assert resp.status_code == 403, resp.text
        assert resp.json()["code"] == "SCOPE_FORBIDDEN"

    denied = await _audit_rows(db, "memory.read", actor_type="staff")
    assert len(denied) == 1
    assert denied[0].detail["reason"] == "not_elder_device"


async def test_unbound_device_is_forbidden(client: Bundle, seeded: Seed, db: AsyncSession) -> None:
    """未绑定长者的设备没有「自己」可看：403，且**不能**退回成按参数指定长者。"""
    client.holder[:] = [_as_unbound_device(seeded)]

    resp = await client.client.get(
        "/api/memory/me/chunks", params={"elder_id": str(seeded.elder_id)}
    )

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "SCOPE_FORBIDDEN"
    denied = await _audit_rows(db, "memory.read")
    assert any(row.detail["reason"] == "device_unbound" for row in denied)


async def test_unknown_chunk_id_is_404(client: Bundle, seeded: Seed) -> None:
    """不存在的 chunk_id 与跨长者同样 404（不区分，避免遍历探测）。"""
    client.holder[:] = [_as_elder(seeded)]

    resp = await client.client.delete(f"/api/memory/me/chunks/{uuid.uuid4()}")

    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "NOT_FOUND"
