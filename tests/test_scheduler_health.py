"""19:00 采集驱动 + 每题 60 秒超时的**调度接线与推进**（FR-HLT-01/03，AC-11）。

本文件钉的是「链路真的会动」这一层，而不是分级口径：

1. `build_scheduler` 把 20 秒超时扫描注册成了一条 interval 任务——没有这条 job，
   `expires_at` 只是躺在会话行上，「每问等 60 秒」在生产上永远不会发生；
2. `run_health_checkup_start` 除了照旧落 `health_review` 提醒，还**真的把图跑到第一问**
   并把问句、截止时间写进 `health_checkup_sessions`；
3. `run_health_checkup_timeout` 用驱动推进到期会话：第一次超时重问一次，
   第二次超时标缺失继续下一题（PRD 原文的两级语义，由图里的 `MAX_ATTEMPTS` 承担）；
4. 全部问完 / 长者主动结束 → 会话终态 + 走 `persist_checkup_state` 落库分级。

**驱动是注入的**：`app/scheduler/health.py` 不 import `app/graphs`（AGENTS §2.1 禁止横向
import），只认 `HealthCheckupDriver` 这个 Protocol；本文件用装配根那个实现
（`app.main._GraphCheckupDriver`）包住内存图，顺带把注入这条接缝也钉住。
**不注入**（`driver=None`）时 19:00 只落提醒通知、零会话行，日志写 `driver_unavailable`。

**不起调度器时钟**：只构造 `AsyncIOScheduler`、读 job 定义、直接 `await` 任务体。
**不 sleep 60 秒**：`now` 全部注入，60 秒边界用假时钟跨过去。
图用 `MemorySaver` 单跑（每个用例一张新图），不写 langgraph checkpoint 库。
"""

from __future__ import annotations

import datetime
import functools
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from tests.conftest import require_db_connection

from app.api import health as health_api
from app.core.config import get_settings
from app.graphs import health_checkup_graph as health_checkup
from app.models import Elder, Notification
from app.models.health import HealthCheckupSession, HealthRecord
from app.scheduler import JOB_PREFIX, build_scheduler
from app.scheduler import health as health_jobs
from app.services import health_checkup_service, health_service

#: 固定时间零点：所有超时推进都相对它算，不碰真实时钟。
ANCHOR = datetime.datetime(2026, 11, 12, 11, 0, tzinfo=datetime.UTC)

#: 一问的等待时长（与 `health_service.CHECKUP_ANSWER_TIMEOUT_SECONDS` 同源）。
WAIT = datetime.timedelta(seconds=60)


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
def isolated_factory(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> async_sessionmaker[AsyncSession]:
    """把任务体读的 `session_factory` 换进外层事务：任务体的 commit 只放 savepoint。

    2026-11-13 起「到期未答扫描」的实现搬进了 `app.services.health_checkup_service`
    （Celery worker 也要用，不能 import `app.scheduler.**`），调度模块只剩再导出；
    因此两处都要 patch，否则服务层仍会去拿真实的进程级连接池。
    """
    factory = async_sessionmaker(
        bind=db.bind,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    monkeypatch.setattr(health_jobs, "session_factory", factory)
    monkeypatch.setattr(health_checkup_service, "session_factory", factory)
    return factory


@pytest.fixture
def memory_graph(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """给调度任务一条内存图：不依赖 checkpointer 库，也不跨用例串状态。"""
    graph = health_checkup.build_health_graph(MemorySaver())

    async def _graph() -> Any:
        return graph

    monkeypatch.setattr(health_checkup, "get_health_graph", _graph)
    yield graph


@pytest.fixture
def driver(memory_graph: Any) -> Any:
    """走**装配根那一条**注入路径：`_GraphCheckupDriver` 包住图模块。

    不用假驱动跑既有行为用例，是为了把「调度器只认 Protocol、实现由 main 拼」
    这条接缝也一起钉住——假驱动只用在专门验证注入参数的那几条用例里。
    """
    from app.main import _GraphCheckupDriver

    return _GraphCheckupDriver(health_checkup)


async def _seed_elder(db: AsyncSession, *, checkup_start: str | None = None) -> uuid.UUID:
    elder_id = uuid.uuid4()
    await db.execute(
        Elder.__table__.insert(),
        [
            {
                "elder_id": elder_id,
                "name": "张桂芬",
                "status": "active",
                "checkup_start": checkup_start,
            }
        ],
    )
    await db.flush()
    return elder_id


async def _sessions(db: AsyncSession, elder_id: uuid.UUID) -> list[HealthCheckupSession]:
    # 调度任务走的是另一个 session（savepoint 版），本 session 的 identity map 里
    # 还留着上一轮的旧值；不 expire 就会读到「上一轮」的 current_key / timeouts。
    db.expire_all()
    rows = await db.execute(
        select(HealthCheckupSession)
        .where(HealthCheckupSession.elder_id == elder_id)
        .order_by(HealthCheckupSession.created_at)
    )
    return list(rows.scalars().all())


async def _records(db: AsyncSession, elder_id: uuid.UUID) -> list[HealthRecord]:
    db.expire_all()
    rows = await db.execute(
        select(HealthRecord)
        .where(HealthRecord.elder_id == elder_id)
        .order_by(HealthRecord.revision)
    )
    return list(rows.scalars().all())


async def _review_notifications(db: AsyncSession, elder_id: uuid.UUID) -> list[Notification]:
    rows = await db.execute(
        select(Notification).where(
            Notification.elder_id == elder_id,
            Notification.kind == "health_review",
        )
    )
    return list(rows.scalars().all())


# ----------------------------------------------------------------- 注册与任务体


async def test_build_scheduler_registers_checkup_timeout_job() -> None:
    """装配根必须挂上 20 秒扫描：没有 job，「每问 60 秒」在生产上不会发生。"""
    scheduler = build_scheduler()
    job = scheduler.get_job(f"{JOB_PREFIX}:{health_jobs.CHECKUP_TIMEOUT_JOB_NAME}")
    assert job is not None, "build_scheduler 必须注册采集超时扫描"
    # 驱动在注册时用 `partial` 绑进任务体（调度器只会无实参调用），所以比的是内层函数。
    assert isinstance(job.func, functools.partial)
    assert job.func.func is health_jobs.run_health_checkup_timeout
    assert job.func.keywords == {"driver": None}
    assert job.trigger.interval.total_seconds() == health_jobs.CHECKUP_TIMEOUT_INTERVAL_SECONDS
    assert job.coalesce is True
    assert job.max_instances == 1


@dataclass
class _RecordingDriver:
    """假驱动：只记调度器递进来的参数，用来钉「注入真的被调用、参数没走样」。"""

    starts: list[dict[str, Any]] = field(default_factory=list)
    advances: list[datetime.datetime | None] = field(default_factory=list)

    async def start_due(
        self,
        session: AsyncSession,
        *,
        elder_id: uuid.UUID,
        record_date: datetime.date,
        now: datetime.datetime | None,
    ) -> bool:
        self.starts.append(
            {"elder_id": elder_id, "record_date": record_date, "now": now, "session": session}
        )
        return True

    async def advance_due(self, session: AsyncSession, *, now: datetime.datetime | None) -> int:
        self.advances.append(now)
        return 3


async def test_build_scheduler_passes_health_checkup_driver_to_jobs(
    db: AsyncSession, isolated_factory: async_sessionmaker[AsyncSession]
) -> None:
    """注入的驱动要同时落到采集时刻扫描 job 与 20 秒 job 上（两条都不能丢驱动）。

    FR-HLT-05 之后单点 19:00 cron 不再注册（否则默认群体一天采两场），
    原来挂驱动的那条「日任务」换成 5 分钟窗口扫描 `health-checkup-due`。
    """
    fake = _RecordingDriver()
    scheduler = build_scheduler(health_checkup_driver=fake)
    try:
        start_job = scheduler.get_job(f"{JOB_PREFIX}:{health_jobs.CHECKUP_DUE_JOB_NAME}")
        timeout_job = scheduler.get_job(f"{JOB_PREFIX}:{health_jobs.CHECKUP_TIMEOUT_JOB_NAME}")
        assert start_job is not None and timeout_job is not None
        assert start_job.func.keywords == {"driver": fake}
        assert timeout_job.func.keywords == {"driver": fake}
        # 旧的 19:00 单点任务必须消失：两套并存 = 默认群体被采两次。
        assert scheduler.get_job(f"{JOB_PREFIX}:health-checkup-start") is None
        assert (
            start_job.trigger.interval.total_seconds() == health_jobs.CHECKUP_DUE_INTERVAL_SECONDS
        )
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)

    elder_id = await _seed_elder(db)
    created = await health_jobs.run_health_checkup_start(now=ANCHOR, driver=fake)

    assert created == 1  # 提醒通知照旧
    assert len(fake.starts) == 1
    entry = fake.starts[0]
    assert entry["now"] == ANCHOR  # `now` 透传给驱动，不吞成真实时钟
    assert entry["record_date"] == health_jobs._today()  # 业务日按调度时区算
    assert entry["elder_id"] == elder_id
    assert await _sessions(db, elder_id) == []  # 假驱动不落会话：责任边界清楚


async def test_start_job_without_driver_only_raises_the_reminder(
    db: AsyncSession, isolated_factory: async_sessionmaker[AsyncSession], caplog: Any
) -> None:
    """驱动没装配：只落提醒通知、零会话行，并且在日志里明说 `driver_unavailable`。"""
    elder_id = await _seed_elder(db)

    with caplog.at_level("WARNING", logger="app.scheduler.health"):
        created = await health_jobs.run_health_checkup_start(now=ANCHOR, driver=None)

    assert created == 1
    assert await _sessions(db, elder_id) == []
    notifications = await _review_notifications(db, elder_id)
    assert [n.status for n in notifications] == ["generated"]
    # W1：提醒通知必须自曝「采不了」，不能让照护台把排队当成已安排采集。
    payload = notifications[0].payload
    assert payload["driver_state"] == "driver_unavailable"
    assert payload["collectable"] is False
    assert "无法采集" in payload["summary"]
    assert any("driver_unavailable" in record.getMessage() for record in caplog.records)
    # 没有驱动时超时扫描也空转：返回 0，不去碰没有的会话。
    assert await health_jobs.run_health_checkup_timeout(now=ANCHOR, driver=None) == 0


async def test_start_job_marks_reminder_collectable_when_driver_wired(
    db: AsyncSession, isolated_factory: async_sessionmaker[AsyncSession]
) -> None:
    """驱动在：同一条提醒通知要说「已启动」，并且标 collectable=True。"""
    elder_id = await _seed_elder(db)

    created = await health_jobs.run_health_checkup_start(now=ANCHOR, driver=_RecordingDriver())

    assert created == 1
    payload = (await _review_notifications(db, elder_id))[0].payload
    assert payload["driver_state"] == "wired"
    assert payload["collectable"] is True
    assert payload["summary"] == "19:00 健康采集已启动，本日尚无健康记录"


# --------------------------------------------------- FR-HLT-05 采集时刻窗口扫描


def _local(hour: int, minute: int) -> datetime.datetime:
    """调度时区（Asia/Shanghai）下的某一刻：窗口判定按本地时刻算，不按 UTC。"""
    return datetime.datetime(
        2026, 11, 12, hour, minute, tzinfo=ZoneInfo(get_settings().scheduler_timezone)
    )


def test_is_due_window_is_left_closed_and_right_open() -> None:
    """窗口左闭右开：`now-5min` 命中，`now` 本身不命中（相邻两轮不会各命中一次）。"""
    clock = datetime.time(19, 0)
    assert health_jobs._is_due(clock, _local(19, 0), window_minutes=5) is True
    assert health_jobs._is_due(clock, _local(19, 4), window_minutes=5) is True
    assert health_jobs._is_due(clock, _local(19, 5), window_minutes=5) is False
    assert health_jobs._is_due(clock, _local(18, 59), window_minutes=5) is False
    # 跨零点：23:58 的窗口在 00:02 这一轮里命中，不能算成「差 23 小时」。
    assert health_jobs._is_due(datetime.time(23, 58), _local(0, 2), window_minutes=5) is True


async def test_due_scan_starts_at_custom_clock_and_never_twice(
    db: AsyncSession, isolated_factory: async_sessionmaker[AsyncSession]
) -> None:
    """长者自己设 18:30：18:32 这一轮就起采集，19:00 再来一轮不许重复（验收①）。"""
    elder_id = await _seed_elder(db, checkup_start="18:30")
    driver = _RecordingDriver()

    created = await health_jobs.run_health_checkup_due(now=_local(18, 32), driver=driver)

    assert created == 1
    assert len(driver.starts) == 1
    entry = driver.starts[0]
    assert entry["elder_id"] == elder_id
    # 业务日按调度时区算：18:32 本地仍是 11-12，不能被 UTC 拖成 11-11。
    assert entry["record_date"] == datetime.date(2026, 11, 12)
    payload = (await _review_notifications(db, elder_id))[0].payload
    assert payload["scheduled_clock"] == "18:30"
    assert payload["source"] == "elder"
    assert payload["driver_state"] == "wired"

    # 19:00 那轮：自定义时刻 18:30 早已出窗，全局 19:00 也不该对他生效 → 0 条。
    again = await health_jobs.run_health_checkup_due(now=_local(19, 0), driver=driver)
    assert again == 0
    assert len(driver.starts) == 1


async def test_due_scan_falls_back_to_global_clock(
    db: AsyncSession, isolated_factory: async_sessionmaker[AsyncSession]
) -> None:
    """没设自定义时刻 → 回落到全局默认（19:00），payload 标明来源是 global（验收②）。"""
    elder_id = await _seed_elder(db)
    driver = _RecordingDriver()

    created = await health_jobs.run_health_checkup_due(now=_local(19, 1), driver=driver)

    assert created == 1
    assert len(driver.starts) == 1
    payload = (await _review_notifications(db, elder_id))[0].payload
    assert payload["scheduled_clock"] == get_settings().health_checkup_start
    assert payload["source"] == "global"


async def test_due_scan_ignores_minutes_outside_the_window(
    db: AsyncSession, isolated_factory: async_sessionmaker[AsyncSession]
) -> None:
    """窗口外的分钟一律不采：18:32 对 19:00 的长者不是「提前一点」，是「还没到」。"""
    elder_id = await _seed_elder(db)
    driver = _RecordingDriver()

    created = await health_jobs.run_health_checkup_due(now=_local(18, 32), driver=driver)

    assert created == 0
    assert driver.starts == []
    assert await _review_notifications(db, elder_id) == []


async def test_due_scan_skips_when_today_already_has_session(
    db: AsyncSession, isolated_factory: async_sessionmaker[AsyncSession], caplog: Any
) -> None:
    """今日已有会话（任何状态）→ 跳过，不重复起第二场（验收④）。"""
    elder_id = await _seed_elder(db)
    await health_service.open_checkup_session(
        db,
        elder_id=elder_id,
        record_date=datetime.date(2026, 11, 12),
        thread_id=health_checkup.health_thread_id(elder_id, datetime.date(2026, 11, 12)),
    )
    driver = _RecordingDriver()

    with caplog.at_level("INFO", logger="app.scheduler.health"):
        created = await health_jobs.run_health_checkup_due(now=_local(19, 1), driver=driver)

    assert created == 0
    assert driver.starts == []
    done = [r for r in caplog.records if r.getMessage() == "health checkup due job done"]
    assert done and done[-1].__dict__["skipped_count"] == 1


async def test_due_scan_without_driver_only_raises_the_reminder(
    db: AsyncSession, isolated_factory: async_sessionmaker[AsyncSession]
) -> None:
    """驱动没装配：窗口照样点名，但会话一场都不起，通知自曝「采不了」。"""
    elder_id = await _seed_elder(db)

    created = await health_jobs.run_health_checkup_due(now=_local(19, 1), driver=None)

    assert created == 1
    assert await _sessions(db, elder_id) == []
    payload = (await _review_notifications(db, elder_id))[0].payload
    assert payload["driver_state"] == "driver_unavailable"
    assert payload["collectable"] is False
    assert "无法采集" in payload["summary"]


async def test_health_probe_reports_checkup_driver_state() -> None:
    """`/health` 如实暴露采集驱动状态：unavailable 时带异常类型名，wired 时不带。"""
    application = FastAPI()
    application.include_router(health_api.router)
    transport = ASGITransport(app=application)

    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        # 没走过 lifespan 装配（例如单文件临时装配）：不许瞎猜成 wired。
        body = (await http.get("/health")).json()
        assert body["health_checkup_driver"] == "unknown"
        assert body["health_checkup_driver_error"] is None

        application.state.health_checkup_driver_state = "unavailable"
        application.state.health_checkup_driver_error = "ImportError"
        body = (await http.get("/health")).json()
        assert body["health_checkup_driver"] == "unavailable"
        assert body["health_checkup_driver_error"] == "ImportError"

        application.state.health_checkup_driver_state = "wired"
        application.state.health_checkup_driver_error = None
        body = (await http.get("/health")).json()
        assert body["health_checkup_driver"] == "wired"
        assert body["health_checkup_driver_error"] is None


async def test_graph_driver_wraps_graph_outcome_into_bool() -> None:
    """装配根那个驱动只回答「这场会话起没起」：None/created=False 都是 False。"""

    @dataclass
    class _Outcome:
        created: bool

    class _FakeGraphs:
        def __init__(self, outcome: Any) -> None:
            self.outcome = outcome
            self.calls: list[dict[str, Any]] = []

        async def start_checkup(self, session: Any, **kwargs: Any) -> Any:
            self.calls.append({"session": session, **kwargs})
            return self.outcome

    from app.main import _GraphCheckupDriver

    day = datetime.date(2026, 11, 12)
    for outcome, expected in (
        (_Outcome(created=True), True),
        (_Outcome(created=False), False),
        (None, False),
    ):
        graphs = _FakeGraphs(outcome)
        driver = _GraphCheckupDriver(graphs)
        assert (
            await driver.start_due(None, elder_id=uuid.uuid4(), record_date=day, now=ANCHOR)
            is expected
        )
        assert graphs.calls[0]["record_date"] == day
        assert graphs.calls[0]["now"] == ANCHOR


async def test_checkup_driver_state_requires_a_running_scheduler() -> None:
    """`wired` 的语义是「19:00 真的会采」：驱动装上了但调度器没起来，不许报 wired。

    复核方 2026-11-12 指出：`build_scheduler` 抛了之后 state 仍是 wired，等于骗运维。
    """
    from app.main import _checkup_driver_state

    assert _checkup_driver_state(assembled=True, scheduler_running=True) == "wired"
    assert (
        _checkup_driver_state(assembled=True, scheduler_running=False)
        == "driver_ready_but_scheduler_missing"
    )
    # 驱动都没装上：不管调度器在不在，都只能是 unavailable。
    assert _checkup_driver_state(assembled=False, scheduler_running=True) == "unavailable"
    assert _checkup_driver_state(assembled=False, scheduler_running=False) == "unavailable"


async def test_timeout_job_passes_now_to_the_driver() -> None:
    """20 秒 job 把注入的驱动与 `now` 原样递进去：漏了 `now`，超时就会按真实时钟乱推。"""
    fake = _RecordingDriver()

    advanced = await health_jobs.run_health_checkup_timeout(now=ANCHOR, driver=fake)

    assert advanced == 3
    assert fake.advances == [ANCHOR]
    assert fake.starts == []


async def test_start_job_opens_session_and_asks_first_question(
    db: AsyncSession,
    isolated_factory: async_sessionmaker[AsyncSession],
    memory_graph: Any,
    driver: Any,
) -> None:
    """19:00 不只是落提醒：会话行要有第一问、截止时间、`voice_scheduled` 口径。"""
    elder_id = await _seed_elder(db)

    created = await health_jobs.run_health_checkup_start(now=ANCHOR, driver=driver)

    assert created == 1  # 提醒通知照旧，既有照护台口径不变
    rows = await _sessions(db, elder_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "asking"
    assert row.source == "voice_scheduled"
    assert row.current_key == "systolic"
    assert row.prompt is not None and row.prompt["type"] == "question"
    assert row.prompt["attempt"] == 1
    assert row.expires_at == ANCHOR + WAIT
    # 同一事务里既建提醒又起会话：提醒只说「已启动」，不伪造「已采集」。
    assert [n.status for n in await _review_notifications(db, elder_id)] == ["generated"]


async def test_start_job_does_not_restart_an_in_flight_session(
    db: AsyncSession,
    isolated_factory: async_sessionmaker[AsyncSession],
    memory_graph: Any,
    driver: Any,
) -> None:
    """任务重跑（reload / 手工补跑）不能把已经答了两题的长者重置回第一题。"""
    elder_id = await _seed_elder(db)
    await health_jobs.run_health_checkup_start(now=ANCHOR, driver=driver)
    await health_checkup.submit_checkup_answer(
        db, elder_id=elder_id, text="128", now=ANCHOR + datetime.timedelta(seconds=5)
    )

    await health_jobs.run_health_checkup_start(
        now=ANCHOR + datetime.timedelta(seconds=10), driver=driver
    )

    rows = await _sessions(db, elder_id)
    assert len(rows) == 1
    assert rows[0].current_key == "diastolic"
    assert rows[0].answers == {"systolic": 128}
    # 提醒通知同样幂等：一条。
    assert len(await _review_notifications(db, elder_id)) == 1


async def test_timeout_retries_once_then_marks_missing(
    db: AsyncSession,
    isolated_factory: async_sessionmaker[AsyncSession],
    memory_graph: Any,
    driver: Any,
) -> None:
    """第一次超时重问同一题（attempt=2），第二次超时标缺失并问下一题。"""
    elder_id = await _seed_elder(db)
    await health_jobs.run_health_checkup_start(now=ANCHOR, driver=driver)

    advanced = await health_jobs.run_health_checkup_timeout(now=ANCHOR + WAIT, driver=driver)
    assert advanced == 1
    row = (await _sessions(db, elder_id))[0]
    assert row.status == "asking"
    assert row.current_key == "systolic"  # 重问，不跳题
    assert row.prompt is not None and row.prompt["attempt"] == 2
    assert row.timeouts == 1
    assert row.expires_at == ANCHOR + WAIT + WAIT
    assert row.missing == []

    advanced = await health_jobs.run_health_checkup_timeout(now=ANCHOR + WAIT + WAIT, driver=driver)
    assert advanced == 1
    row = (await _sessions(db, elder_id))[0]
    assert row.current_key == "diastolic"  # 标缺失后继续下一题，不阻断
    assert row.missing == ["systolic"]
    assert row.answers == {}
    assert row.timeouts == 2


async def test_timeout_before_due_does_nothing(
    db: AsyncSession,
    isolated_factory: async_sessionmaker[AsyncSession],
    memory_graph: Any,
    driver: Any,
) -> None:
    """没到 60 秒不动：扫描是「到点才推进」，提前推进等于把长者的思考时间吞掉。"""
    elder_id = await _seed_elder(db)
    await health_jobs.run_health_checkup_start(now=ANCHOR, driver=driver)

    assert await health_jobs.run_health_checkup_timeout(now=ANCHOR, driver=driver) == 0

    row = (await _sessions(db, elder_id))[0]
    assert row.current_key == "systolic"
    assert row.prompt is not None and row.prompt["attempt"] == 1


async def test_all_timeouts_close_without_fabricating_a_record(
    db: AsyncSession,
    isolated_factory: async_sessionmaker[AsyncSession],
    memory_graph: Any,
    driver: Any,
) -> None:
    """五题全超时（一个数都没采到）→ 会话 completed、**不落库**，缺失留给 20:00 漏采扫描。

    全缺项交给 `grade_vitals` 会是黄档：那样每场「长者没接话」都会凭空长出一条黄色记录
    与一条复核任务，20:00 的 `run_missed_scan` 还会因为「当天已有记录」而不再报漏采——
    等于把漏采洗成已采集。所以这里宁可不落，让缺失如实暴露。
    """
    elder_id = await _seed_elder(db)
    await health_jobs.run_health_checkup_start(now=ANCHOR, driver=driver)

    # 每问两次超时：第一次重问、第二次标缺失。十轮之后整场采集结束。
    clock = ANCHOR
    for _ in range(10):
        clock = clock + WAIT
        assert await health_jobs.run_health_checkup_timeout(now=clock, driver=driver) == 1

    row = (await _sessions(db, elder_id))[0]
    assert row.status == "completed"
    assert row.expires_at is None
    assert row.prompt is None
    assert sorted(row.missing) == sorted(health_checkup.QUESTION_ORDER)
    assert await _records(db, elder_id) == []
    # 只剩 19:00 那条提醒：没有记录就不该有复核任务（FR-HLT-03 的复核对象是记录）。
    reviews = await _review_notifications(db, elder_id)
    assert len(reviews) == 1
    assert reviews[0].payload.get("state") == "pending_collection"


async def test_all_timeouts_stop_the_timeout_scan(
    db: AsyncSession,
    isolated_factory: async_sessionmaker[AsyncSession],
    memory_graph: Any,
    driver: Any,
) -> None:
    """会话关掉之后超时扫描不再捡它，长者补一句也不会把已关的场次重新打开。"""
    elder_id = await _seed_elder(db)
    await health_jobs.run_health_checkup_start(now=ANCHOR, driver=driver)
    clock = ANCHOR
    for _ in range(10):
        clock = clock + WAIT
        await health_jobs.run_health_checkup_timeout(now=clock, driver=driver)

    assert await health_jobs.run_health_checkup_timeout(now=clock + WAIT, driver=driver) == 0
    outcome = await health_checkup.submit_checkup_answer(
        db, elder_id=elder_id, text="120", now=clock + WAIT
    )

    assert outcome.session.status == "completed"
    assert outcome.prompt is None
    assert len(await _records(db, elder_id)) == 0
    count = await db.scalar(
        select(func.count())
        .select_from(HealthCheckupSession)
        .where(HealthCheckupSession.elder_id == elder_id)
    )
    assert count == 1


async def test_answered_items_then_timeouts_land_yellow_review_task(
    db: AsyncSession,
    isolated_factory: async_sessionmaker[AsyncSession],
    memory_graph: Any,
    driver: Any,
) -> None:
    """答了血压、其余全超时 → 黄档记录 + 复核任务（AC-11 的「标缺失继续」）。"""
    elder_id = await _seed_elder(db)
    await health_jobs.run_health_checkup_start(now=ANCHOR, driver=driver)
    clock = ANCHOR
    # 收缩压与舒张压各答一次，后面三题（各两次超时）全部记缺失。
    for text in ("128", "78"):
        clock = clock + datetime.timedelta(seconds=5)
        await health_checkup.submit_checkup_answer(db, elder_id=elder_id, text=text, now=clock)

    for _ in range(6):
        clock = clock + WAIT
        assert await health_jobs.run_health_checkup_timeout(now=clock, driver=driver) == 1

    row = (await _sessions(db, elder_id))[0]
    # 有数据 + 缺失 → 判黄 → 停在复核闸门（FR-HLT-03 的 HITL）。
    assert row.status == "awaiting_review"
    assert row.expires_at is None
    assert row.prompt is not None and row.prompt["type"] == "review"
    assert sorted(row.missing) == ["heart_rate", "mood_score", "sleep_hours"]

    records = await _records(db, elder_id)
    assert len(records) == 1
    record = records[0]
    assert record.grade == "yellow"
    assert record.systolic_mmhg == 128
    assert record.diastolic_mmhg == 78
    # 缺失项的告警词是短名（sleep_missing / mood_missing）；心率缺失按既有口径不判黄。
    assert "sleep_missing" in record.abnormal_reasons
    assert "mood_missing" in record.abnormal_reasons
    assert record.source == "voice_scheduled"
    reviews = await _review_notifications(db, elder_id)
    # 提醒一条 + 复核任务一条：黄档必须进人工复核（FR-HLT-03）。
    assert len(reviews) == 2
    assert any(n.payload.get("record_id") == str(record.record_id) for n in reviews)

    # 进了复核闸门之后再扫、再补一句都不会落第二个版本（AC-22 幂等由既有逻辑承担）。
    assert await health_jobs.run_health_checkup_timeout(now=clock + WAIT, driver=driver) == 0
    again = await health_checkup.submit_checkup_answer(
        db, elder_id=elder_id, text="120", now=clock + WAIT
    )
    assert again.session.status == "awaiting_review"
    assert len(await _records(db, elder_id)) == 1


async def test_end_reply_stops_and_persists_answered_items(
    db: AsyncSession,
    isolated_factory: async_sessionmaker[AsyncSession],
    memory_graph: Any,
    driver: Any,
) -> None:
    """长者说「不测了」：会话置 ended，已答的收缩压照常落库（不丢已采集数据）。"""
    elder_id = await _seed_elder(db)
    await health_jobs.run_health_checkup_start(now=ANCHOR, driver=driver)
    await health_checkup.submit_checkup_answer(
        db, elder_id=elder_id, text="128", now=ANCHOR + datetime.timedelta(seconds=5)
    )

    outcome = await health_checkup.submit_checkup_answer(
        db, elder_id=elder_id, text="不测了，我不想测了", now=ANCHOR + datetime.timedelta(seconds=8)
    )

    assert outcome.session.status == "ended"
    assert outcome.session.prompt is None
    assert outcome.session.closed_at is not None
    records = await _records(db, elder_id)
    assert len(records) == 1
    assert records[0].systolic_mmhg == 128
    assert records[0].grade == "yellow"  # 其余项缺失判黄，不是绿
    assert outcome.record is not None and outcome.record.record_id == records[0].record_id


async def test_end_reply_before_any_answer_persists_nothing(
    db: AsyncSession,
    isolated_factory: async_sessionmaker[AsyncSession],
    memory_graph: Any,
    driver: Any,
) -> None:
    """一个数都没采到就不落库：否则 20:00 的漏采扫描会以为当天已经采过。"""
    elder_id = await _seed_elder(db)
    await health_jobs.run_health_checkup_start(now=ANCHOR, driver=driver)

    outcome = await health_checkup.submit_checkup_answer(
        db, elder_id=elder_id, text="结束", now=ANCHOR + datetime.timedelta(seconds=3)
    )

    assert outcome.session.status == "ended"
    assert outcome.record is None
    assert await _records(db, elder_id) == []


async def test_answer_after_answer_advances_question_by_question(
    db: AsyncSession,
    isolated_factory: async_sessionmaker[AsyncSession],
    memory_graph: Any,
    driver: Any,
) -> None:
    """正常作答一路走到绿档：五问答完 → completed + 绿色记录 + 无复核任务。"""
    elder_id = await _seed_elder(db)
    await health_jobs.run_health_checkup_start(now=ANCHOR, driver=driver)

    clock = ANCHOR
    for index, text in enumerate(("128", "78", "72", "7.5", "4")):
        clock = clock + datetime.timedelta(seconds=5)
        outcome = await health_checkup.submit_checkup_answer(
            db, elder_id=elder_id, text=text, now=clock
        )
        if index < 4:
            assert outcome.session.status == "asking"
            assert outcome.record is None

    assert outcome.session.status == "completed"
    assert outcome.session.answers == {
        "systolic": 128,
        "diastolic": 78,
        "heart_rate": 72,
        "sleep_hours": 7.5,
        "mood_score": 4,
    }
    records = await _records(db, elder_id)
    assert len(records) == 1
    assert records[0].grade == "green"
    assert records[0].source == "voice_scheduled"
    # 绿档不进复核：当天只有 19:00 那条提醒。
    assert len(await _review_notifications(db, elder_id)) == 1
