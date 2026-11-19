"""`work_order_service` 的单号 / 撞号重试 / 并发用例（N21b · task-66）。

覆盖三件事：

1. 单号格式 `WO-YYYYMMDD-NNNN` 与当日自增；
2. 唯一键撞号后 **savepoint 回滚 + 重算重试** 真的生效——删掉 `MAX_ATTEMPTS`
   的第二次尝试，这条立刻红（反向验证口径）；
3. 两个独立事务并发建单，单号不重复、两张单都落库。

前两条沿用 `tests/test_medication_graph.py` 的隔离手法：自建 `NullPool` 引擎 →
外层事务 → 用例结束整体回滚，主库零残留。第 3 条必须真提交（两个连接各自一个
事务），因此只在 `ANYU_TEST_DATABASE_URL` 指向隔离测试库时运行，并在
`finally` 里按 `order_id` / `elder_id` 精确清理。
"""

from __future__ import annotations

import asyncio
import datetime
import os
import re
import uuid
from collections.abc import AsyncIterator, Callable

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from tests.conftest import require_db_connection

from app.core.config import get_settings
from app.models import Elder, WorkOrder
from app.services import work_order_service

ELDER = uuid.UUID("00000000-0000-4000-8000-00000000a066")
"""测试长者固定 ID；跑在测试事务里，回滚即消失。"""

ORDER_NO_PATTERN = re.compile(r"WO-(\d{8})-(\d{4})")


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
def make_session(db: AsyncConnection) -> Callable[[], AsyncSession]:
    """每次返回新 session（同一外层事务内的 savepoint），与用药图用例同构。"""

    def _make() -> AsyncSession:
        return AsyncSession(
            bind=db, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )

    return _make


@pytest.fixture
async def elder(db: AsyncConnection) -> uuid.UUID:
    await db.execute(
        Elder.__table__.insert().values(elder_id=ELDER, name="张桂芬", status="active")
    )
    return ELDER


def _seq(order_no: str) -> int:
    match = ORDER_NO_PATTERN.fullmatch(order_no)
    assert match is not None, f"单号格式不对：{order_no!r}"
    return int(match.group(2))


async def test_create_continues_the_day_sequence(
    make_session: Callable[[], AsyncSession], elder: uuid.UUID
) -> None:
    """同一天连建两张单：格式对、序号恰好 +1（证明 next_order_no 读的是当日最大值）。"""
    async with make_session() as session:
        first = await work_order_service.create_work_order(
            session, elder_id=elder, type="sos", severity="red", note="一号单"
        )
        second = await work_order_service.create_work_order(
            session, elder_id=elder, type="sos", severity="red", note="二号单"
        )

        prefix = f"WO-{work_order_service.shanghai_today():%Y%m%d}-"
        assert first.order_no.startswith(prefix), first.order_no
        assert second.order_no.startswith(prefix), second.order_no
        assert _seq(second.order_no) == _seq(first.order_no) + 1
        assert (first.status, first.false_alarm) == ("new", False)
        assert (second.status, second.false_alarm) == ("new", False)


async def test_order_no_conflict_retries_once_and_recomputes(
    make_session: Callable[[], AsyncSession],
    elder: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """撞 `uq_work_orders_order_no` 后：savepoint 回滚 → 踢出失败实例 → 重算再插。

    用 monkeypatch 把第一次 `next_order_no()` 钉成占位单的单号，模拟「两个事务
    读到同一个最大值」的竞态结果。若删掉 `create_work_order()` 里的第二次尝试，
    这条会以 `IntegrityError` 收场——这就是反向验证。
    """
    async with make_session() as session:
        stale = await work_order_service.next_order_no(session)
        # 测试库是共享的：别的用例/探针可能已经把今天的单号提交进去了。断言不能写成
        # 「全表恰好两行」，否则一条历史遗留的 WO-YYYYMMDD-0001 就能把这里判红；
        # 改成「相对进用例时的基线只多出这两行」，既不受污染影响、又保留了「没多写」。
        baseline = {
            row.order_no for row in (await session.execute(WorkOrder.__table__.select())).all()
        }
        session.add(
            WorkOrder(
                order_id=uuid.uuid4(),
                order_no=stale,
                elder_id=elder,
                type="sos",
                severity="red",
                status="new",
                false_alarm=False,
                note="占位：模拟并发对手先提交的单号",
            )
        )
        await session.flush()

        real_next_order_no = work_order_service.next_order_no
        calls = {"count": 0}

        async def fake_next_order_no(
            inner: AsyncSession, *, day: datetime.date | None = None
        ) -> str:
            calls["count"] += 1
            if calls["count"] == 1:
                return stale
            return await real_next_order_no(inner, day=day)

        monkeypatch.setattr(work_order_service, "next_order_no", fake_next_order_no)

        order = await work_order_service.create_work_order(
            session, elder_id=elder, type="sos", severity="red", note="撞号重试"
        )

        assert calls["count"] == 2, "撞号后必须恰好重算一次，不能更多也不能更少"
        assert order.order_no != stale, "重试成功后单号必须换掉"
        assert _seq(order.order_no) == _seq(stale) + 1, "重算要读到占位单并往后跳一号"
        assert order in session, "重试成功的实例必须留在 session 里，否则后续 commit 会丢单"

        stored = {
            row.order_no for row in (await session.execute(WorkOrder.__table__.select())).all()
        }
        assert stored == baseline | {stale, order.order_no}


@pytest.mark.skipif(
    not os.environ.get("ANYU_TEST_DATABASE_URL", "").strip(),
    reason="并发用例会真实提交，只允许跑在 ANYU_TEST_DATABASE_URL 指向的隔离库",
)
async def test_two_transactions_get_distinct_order_numbers(db_engine: AsyncEngine) -> None:
    """两个连接各自开事务、同时建单：都成功且单号不同（撞号由服务内重试消化）。"""
    elder_id = uuid.uuid4()
    order_ids: list[uuid.UUID] = []
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with factory() as setup:
        setup.add(Elder(elder_id=elder_id, name="并发探针长者", status="active"))
        await setup.commit()

    async def create_one(note: str) -> str:
        async with factory() as session:
            order = await work_order_service.create_work_order(
                session, elder_id=elder_id, type="sos", severity="red", note=note
            )
            order_ids.append(order.order_id)
            await session.commit()
            return order.order_no

    try:
        numbers = await asyncio.gather(create_one("并发探针 A"), create_one("并发探针 B"))
        assert len(set(numbers)) == 2, f"两个事务拿到了同一个单号：{numbers}"
        for number in numbers:
            _seq(number)  # 格式断言

        async with factory() as check:
            rows = (
                await check.execute(
                    WorkOrder.__table__.select().where(WorkOrder.elder_id == elder_id)
                )
            ).all()
        assert {row.order_no for row in rows} >= set(numbers), "并发建的两张单都要真的落库"
    finally:
        async with db_engine.begin() as cleanup:
            if order_ids:
                await cleanup.execute(delete(WorkOrder).where(WorkOrder.order_id.in_(order_ids)))
            await cleanup.execute(delete(Elder).where(Elder.elder_id == elder_id))
