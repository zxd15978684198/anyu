"""迭代 8 红档置顶（N34）：通知列表的严重度排序证据。

为什么单为「一条 `ORDER BY`」开一个用例文件：

- 验收口径（PRD:397 FR-ALT-02、TD:1129）写的是「护工看板按严重度排序、红档置顶」，
  但 迭代 8 收口时 `notification_service.list_for_staff` 只有
  `ORDER BY created_at DESC`，severity 从未参与——功能看着在（列表能出来），
  「置顶」只留在文档里，没有任何一条用例能证明它在。
- 排序**必须在 SQL 里**。如果实现是「先 `limit/offset` 取一页、再在 Python 里排」，
  第一页看着像对的，第二页就会混进黄/绿档。所以本文件的核心是
  `test_second_page_keeps_red_first` 与 `test_pages_cover_list_without_overlap`，
  不是补充用例。
- 同 severity 内必须 `created_at DESC`；同一时刻的行必须有确定的 tie-break
  （否则同一页两次查询顺序可能不同，前端会闪）。

本文件只验证 `GET /api/notifications`（`app/api/notifications.py:103`）的读路径，
不验证写路径、不造工单——工单列表端点目前并不存在，那属于新增对外能力，不在本卡范围。

库与事务处理沿用 `tests/test_notifications_api.py`：所有写入落在外层事务里，
`get_session` 被覆盖成 savepoint 模式，teardown 整体回滚，不污染种子数据。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from tests.conftest import require_db_connection

from app.api import notifications as notifications_api
from app.core.auth import get_current_principal
from app.core.config import get_settings
from app.core.runtime_context import Identity
from app.core.trace import TraceIdMiddleware
from app.db import get_session
from app.models import Elder, Notification, StaffAccount

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
    elder_id: uuid.UUID


async def _seed(db: AsyncSession) -> Seed:
    admin_id, elder_id = uuid.uuid4(), uuid.uuid4()
    await db.execute(
        StaffAccount.__table__.insert(),
        [{"staff_id": admin_id, "name": "管理员", "role": "admin", "password_hash": "x"}],
    )
    await db.execute(
        Elder.__table__.insert(),
        [{"elder_id": elder_id, "name": "张桂芬", "status": "active"}],
    )
    await db.flush()
    return Seed(admin_id=admin_id, elder_id=elder_id)


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


# ----------------------------------------------------------------------- helpers

_BASE = datetime.datetime(2026, 11, 10, 12, 0, tzinfo=datetime.UTC)
"""所有用例的基准时刻。写死时间而不是 `now()`：排序证据必须可复现，不能靠跑得快。"""


def _minutes_before(minutes: int) -> datetime.datetime:
    return _BASE - datetime.timedelta(minutes=minutes)


async def _insert(
    db: AsyncSession,
    *,
    elder_id: uuid.UUID,
    severity: str,
    created_at: datetime.datetime,
    notification_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """插一条通知；`created_at` 显式给值，才能构造「红档更旧却仍排前面」的场景。"""
    nid = notification_id or uuid.uuid4()
    await db.execute(
        Notification.__table__.insert(),
        [
            {
                "notification_id": nid,
                "elder_id": elder_id,
                "kind": "health_review",
                "severity": severity,
                "status": "generated",
                "payload": {"summary": f"{severity} @ {created_at.isoformat()}"},
                "created_at": created_at,
            }
        ],
    )
    await db.flush()
    return nid


async def _list(bundle: Bundle, seed: Seed, *, page: int, page_size: int) -> dict[str, object]:
    bundle.holder[:] = [Identity(kind="staff", staff_id=str(seed.admin_id), role="admin")]
    response = await bundle.client.get(
        "/api/notifications", params={"page": page, "page_size": page_size}
    )
    assert response.status_code == 200, response.text
    return response.json()


# ------------------------------------------------------------------ 红档置顶

# 这四条数据是故意配成「按时间倒序会把绿排最前」的形状：
# 绿(10 分钟前) < 黄(30 分钟前) < 红新(1 分钟前) < 红旧(5 分钟前)——如果只按 created_at 排，
# 结果是 green, yellow, red_new, red_old；红档置顶要求 red_new, red_old, yellow, green。
_MIXED = (
    ("red", _minutes_before(5)),
    ("red", _minutes_before(1)),
    ("yellow", _minutes_before(30)),
    ("green", _minutes_before(10)),
)


async def test_red_first_in_mixed_list(db: AsyncSession, seeded: Seed, client: Bundle) -> None:
    ids = []
    for severity, created_at in _MIXED:
        ids.append(
            await _insert(db, elder_id=seeded.elder_id, severity=severity, created_at=created_at)
        )

    body = await _list(client, seeded, page=1, page_size=10)
    items = body["items"]
    assert [item["severity"] for item in items] == ["red", "red", "yellow", "green"], (
        "红档置顶失效：列表按时间倒序，红档被更新的黄/绿挤到后面"
    )
    # 红档内部仍按 created_at DESC：1 分钟前的那条应排在 5 分钟前的前面。
    assert [item["notification_id"] for item in items[:2]] == [str(ids[1]), str(ids[0])]


async def test_second_page_keeps_red_first(db: AsyncSession, seeded: Seed, client: Bundle) -> None:
    """第二页仍是红档：这一条能抓住「先分页、再在 Python 里排」的假实现。

    5 条红 + 2 条黄 + 1 条绿，page_size=2：
    第 1 页应是红[最新两条]，第 2 页仍是红（第 3、4 条）。
    如果实现先取第 2 页（第 3、4 行按时间）再排序，第 2 页会混进黄/绿。
    """
    red_ids = [
        await _insert(
            db,
            elder_id=seeded.elder_id,
            severity="red",
            created_at=_minutes_before(minutes),
        )
        for minutes in (50, 40, 30, 20, 10)
    ]
    await _insert(db, elder_id=seeded.elder_id, severity="yellow", created_at=_minutes_before(5))
    await _insert(db, elder_id=seeded.elder_id, severity="yellow", created_at=_minutes_before(3))
    await _insert(db, elder_id=seeded.elder_id, severity="green", created_at=_minutes_before(1))

    page1 = await _list(client, seeded, page=1, page_size=2)
    page2 = await _list(client, seeded, page=2, page_size=2)

    assert [item["severity"] for item in page1["items"]] == ["red", "red"]
    assert [item["severity"] for item in page2["items"]] == ["red", "red"], (
        "分页破坏了置顶：第 2 页出现了非红档，说明排序发生在分页之后"
    )
    # 红档内部按时间倒序：10 分钟前 → 50 分钟前（id 顺序与插入顺序相反）。
    assert [item["notification_id"] for item in page1["items"]] == [
        str(red_ids[4]),
        str(red_ids[3]),
    ]
    assert [item["notification_id"] for item in page2["items"]] == [
        str(red_ids[2]),
        str(red_ids[1]),
    ]


async def test_same_severity_created_at_desc(
    db: AsyncSession, seeded: Seed, client: Bundle
) -> None:
    """同档内按 created_at DESC；红档即便更旧也压在最前。"""
    oldest_red = await _insert(
        db, elder_id=seeded.elder_id, severity="red", created_at=_minutes_before(60)
    )
    yellow_newest = await _insert(
        db, elder_id=seeded.elder_id, severity="yellow", created_at=_minutes_before(1)
    )
    yellow_middle = await _insert(
        db, elder_id=seeded.elder_id, severity="yellow", created_at=_minutes_before(20)
    )
    yellow_oldest = await _insert(
        db, elder_id=seeded.elder_id, severity="yellow", created_at=_minutes_before(40)
    )

    body = await _list(client, seeded, page=1, page_size=10)
    assert [item["notification_id"] for item in body["items"]] == [
        str(oldest_red),
        str(yellow_newest),
        str(yellow_middle),
        str(yellow_oldest),
    ], "同 severity 内没有按 created_at 倒序（或红档没有压过更旧的黄档）"


async def test_pages_cover_list_without_overlap(
    db: AsyncSession, seeded: Seed, client: Bundle
) -> None:
    """分页拼接 == 整表一次取：不重不漏，否则「置顶」在翻页时会漂移。"""
    for minutes, severity in ((10, "red"), (9, "yellow"), (8, "red"), (7, "green"), (6, "red")):
        await _insert(
            db,
            elder_id=seeded.elder_id,
            severity=severity,
            created_at=_minutes_before(minutes),
        )

    full = await _list(client, seeded, page=1, page_size=10)
    paged: list[str] = []
    for page in (1, 2, 3):
        body = await _list(client, seeded, page=page, page_size=2)
        paged.extend(item["notification_id"] for item in body["items"])

    assert paged == [item["notification_id"] for item in full["items"]], (
        "分页结果与整表顺序不一致：置顶在翻页时会漂移"
    )
    assert full["total"] == 5


async def test_tie_break_is_stable(db: AsyncSession, seeded: Seed, client: Bundle) -> None:
    """同一时刻 + 同一 severity 的两行，两次查询顺序必须一致（前端不会闪）。"""
    same_moment = _minutes_before(5)
    first = await _insert(db, elder_id=seeded.elder_id, severity="red", created_at=same_moment)
    second = await _insert(db, elder_id=seeded.elder_id, severity="red", created_at=same_moment)

    order_a = [
        item["notification_id"]
        for item in (await _list(client, seeded, page=1, page_size=10))["items"]
    ]
    order_b = [
        item["notification_id"]
        for item in (await _list(client, seeded, page=1, page_size=10))["items"]
    ]

    assert order_a == order_b, "同一批数据两次查询顺序不同：缺少确定的 tie-break"
    assert sorted(order_a) == sorted([str(first), str(second)])
