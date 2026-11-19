"""健康采集任务（PRD §4.6 / FR-HLT-01、FR-HLT-03、FR-HLT-05）。

| 时刻 | 任务 | 做什么 |
| --- | --- | --- |
| `health_checkup_start`（19:00） | `run_health_checkup_start` | 对在住、当日**尚无**健康记录的长者：① 落一条 `health_review` 提醒；② **真正起一场采集**（跑到第一问的 `interrupt`） |
| 每 20 秒 | `run_health_checkup_timeout` | 扫到期未答的会话，交给注入的驱动推进（重问一次 → 标缺失继续） |
| `health_checkup_end`（20:00） | `run_missed_scan` | 对当日仍**一条记录都没有**的在住长者生成 `collection_missed`，派给 `primary_care` 护工 |

## 会话驱动为什么从装配根递进来

「起一场采集 / 推进到期的一场」都要拉起 LangGraph 图，而图在 `app/graphs/`。
`app/scheduler` 直接 import `app/graphs` 就是横向 import（AGENTS §2.1 禁止），
所以本模块只声明 `HealthCheckupDriver` 这个 Protocol，实现在装配根
`app/main.py`（由 `app/graphs/health_checkup_graph` 拼出）注入，与用药外呼通道
`app.scheduler.medication.Deliver` 同一手法。

`driver=None`（没装配）时：19:00 **只落提醒通知、一场会话都不起**，并在日志里
写 `driver_unavailable`；20 秒扫描直接返回 0。宁可少跑，也不伪造「采集中」。

## 19:00 为什么「通知 + 会话」两件事都做

通知是照护台那一侧的可见留痕（复核页按 `business_date` 找它），会话是长者那一侧
真正在进行的问答：`health_checkup_sessions` 记着「现在问的是哪一题、等到几点」，
pad 端拉它、照护台代填也走它。两者缺一：只有通知就是「提醒了但没人问」，
只有会话则照护台看不见今天该采谁。**注意**：通知只说「已启动、尚无记录」，
不把「已通知」写成「已采集」（不伪造）。

## 超时为什么交给一个 20 秒的 interval 任务

PRD 要求「每问等 60 秒」。用 `sleep(60)` 轮询会占住调度线程且长者可答时也无法提前推进；
把「到期」写成会话行上的 `expires_at`，再用一个短间隔任务扫到期行，就能做到
「到点才推进、提前答完就撤掉截止时间」。间隔取 20 秒：60/20=3 次机会，误差不超过 20 秒，
对「等 60 秒」这种口径足够；再密就是空转。

## 幂等

日任务按 `(kind, elder_id, payload.business_date)` 查重通知；采集会话按
`(elder_id, record_date)` 唯一。调度器重启、`--reload` 误触发、手工补跑都不会给
同一位长者刷出第二条当日通知，也不会把已经问了两题的长者重置回第一题。
"""

from __future__ import annotations

import datetime
import logging
import uuid
from functools import partial
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db import session_factory
from app.models import (
    Elder,
    ElderStaffAssignment,
    HealthCheckupSession,
    HealthRecord,
    Notification,
)
from app.services import notification_service, work_order_service
from app.services.health_checkup_service import (
    CHECKUP_TIMEOUT_INTERVAL_SECONDS,
    CHECKUP_TIMEOUT_JOB_NAME,
    HealthCheckupDriver,
    run_health_checkup_timeout,
)

logger = logging.getLogger(__name__)

#: 在住状态：`active` 才算「在住长者」（away/transferred/archived 都不派单）。
_ELDER_IN_HOUSE = "active"


#: 采集时刻扫描任务名（FR-HLT-05）：每 5 分钟一轮，覆盖「长者自定义时刻」。
CHECKUP_DUE_JOB_NAME = "health-checkup-due"

#: 窗口扫描间隔 = 窗口宽度：每轮只看 `[now-5min, now)` 这个窗口，保证「一天只命中一次」。
CHECKUP_DUE_INTERVAL_SECONDS = 300
CHECKUP_DUE_WINDOW_MINUTES = 5


def _today() -> datetime.date:
    """业务自然日 = `settings.scheduler_timezone` 的今天（与 `health_records.record_date` 同口径）。"""
    return datetime.datetime.now(ZoneInfo(get_settings().scheduler_timezone)).date()


async def _active_elder_ids(session: AsyncSession) -> list[uuid.UUID]:
    return list(
        (
            await session.execute(
                select(Elder.elder_id)
                .where(Elder.status == _ELDER_IN_HOUSE)
                .order_by(Elder.elder_id)
            )
        )
        .scalars()
        .all()
    )


async def _has_health_record(
    session: AsyncSession, elder_id: uuid.UUID, day: datetime.date
) -> bool:
    """当日是否有**当前生效版本**的健康记录（补录的旧版本不计）。"""
    return (
        await session.execute(
            select(HealthRecord.record_id)
            .where(
                HealthRecord.elder_id == elder_id,
                HealthRecord.record_date == day,
                HealthRecord.superseded_by.is_(None),
            )
            .limit(1)
        )
    ).first() is not None


async def _notification_exists(
    session: AsyncSession, kind: str, elder_id: uuid.UUID, day: datetime.date
) -> bool:
    return (
        await session.execute(
            select(Notification.notification_id)
            .where(
                Notification.kind == kind,
                Notification.elder_id == elder_id,
                Notification.payload["business_date"].astext == day.isoformat(),
            )
            .limit(1)
        )
    ).first() is not None


async def _primary_care_staff(session: AsyncSession, elder_id: uuid.UUID) -> uuid.UUID | None:
    return (
        await session.execute(
            select(ElderStaffAssignment.staff_id)
            .where(
                ElderStaffAssignment.elder_id == elder_id,
                ElderStaffAssignment.relation == "primary_care",
            )
            .limit(1)
        )
    ).scalar_one_or_none()


async def run_health_checkup_start(
    *, now: datetime.datetime | None = None, driver: HealthCheckupDriver | None = None
) -> int:
    """19:00：为当日尚未采集的在住长者落提醒**并起一场采集**，返回新通知条数。

    返回值语义保持不变（新建的 `health_review` 条数）——照护台与既有用例都按它判断
    「今天该采的人有没有被点过名」；起了几场采集单独记进日志的 `started_count`。

    `driver` 由装配根注入（见 `HealthCheckupDriver`）。为 `None` 时**只落提醒通知、
    一场会话都不起**：宁可在日志里明说「驱动没装配」，也不能让照护台看到
    「19:00 已启动采集」却其实没人问——那等于伪造进度。
    """
    day = _today()
    created = 0
    started = 0
    try:
        async with session_factory() as session:
            if driver is None:
                logger.warning(
                    "health checkup sessions skipped: driver_unavailable",
                    extra={"job": "health_checkup_start", "business_date": day.isoformat()},
                )
            for elder_id in await _active_elder_ids(session):
                if await _has_health_record(session, elder_id, day):
                    continue
                if not await _notification_exists(session, "health_review", elder_id, day):
                    await notification_service.create_notification(
                        session,
                        kind="health_review",
                        severity="yellow",
                        elder_id=elder_id,
                        payload={
                            "module": "health_checkup",
                            "state": "pending_collection",
                            "business_date": day.isoformat(),
                            # W1：提醒通知也带上真实驱动状态 —— 照护台看到的那条
                            # 「今天该采谁」必须能分辨「排上队了」和「根本采不了」，
                            # 否则通知本身就是伪造进度。
                            "driver_state": "wired" if driver is not None else "driver_unavailable",
                            "collectable": driver is not None,
                            "summary": (
                                "19:00 健康采集已启动，本日尚无健康记录"
                                if driver is not None
                                else "19:00 健康采集未启动：采集驱动未装配，本日无法采集，请联系管理员"
                            ),
                        },
                        # FR-ALT-04：一位长者一天只该有一条「今日待采集」提醒。
                        # 上面的 `_notification_exists` 负责可读的「已有就跳过」，
                        # 这里的键负责并发/重试时的库级兜底（2026-11-13 审计发现
                        # health_review 两条路径都只查不锁）。
                        dedupe_key=f"health_review:checkup:{elder_id}:{day.isoformat()}",
                    )
                    created += 1
                if driver is None:
                    continue
                # 起采集与建通知放在同一事务：只建通知不起会话等于「点了名没人问」，
                # 只起会话不建通知则照护台看不见今天该采谁。
                try:
                    started_now = await driver.start_due(
                        session,
                        elder_id=elder_id,
                        record_date=day,
                        now=now,
                    )
                except Exception as exc:
                    # 单场起不来（图/检查点异常）不能连累其他长者：记下后继续下一位。
                    logger.warning(
                        "health checkup start failed for elder",
                        extra={
                            "job": "health_checkup_start",
                            "business_date": day.isoformat(),
                            "error_type": type(exc).__name__,
                        },
                    )
                    continue
                if started_now:
                    started += 1
            await session.commit()
    except Exception:
        # 任务体不崩：记满上下文，等下一轮/人工补跑。
        logger.exception(
            "health checkup start job failed",
            extra={"job": "health_checkup_start", "business_date": day.isoformat()},
        )
        return created
    logger.info(
        "health checkup start job done",
        # 键名不能用 `created`：那是 LogRecord 保留属性，logging 会抛 KeyError。
        extra={
            "job": "health_checkup_start",
            "business_date": day.isoformat(),
            "created_count": created,
            "started_count": started,
            "driver_state": "wired" if driver is not None else "driver_unavailable",
        },
    )
    return created


async def _has_checkup_session(
    session: AsyncSession, elder_id: uuid.UUID, day: datetime.date
) -> bool:
    """当日是否**已经起过**采集会话（任何状态都算：completed/ended 也不许再来一场）。

    这是 FR-HLT-05 窗口扫描的幂等闸：19:00 采过的长者，18:30 设成自定义时刻再被扫到，
    只要当天已有会话就跳过——否则「改个时刻」会把人一天采两遍。
    """
    return (
        await session.execute(
            select(HealthCheckupSession.session_id)
            .where(
                HealthCheckupSession.elder_id == elder_id,
                HealthCheckupSession.record_date == day,
            )
            .limit(1)
        )
    ).first() is not None


def _parse_hhmm(value: str | None) -> datetime.time | None:
    """把 `"18:30"` 解析成 `time`；空值/脏值返回 None（脏值由配置或 DB CHECK 先挡一层）。"""
    if not value:
        return None
    hour_text, sep, minute_text = value.partition(":")
    if not sep or len(hour_text) != 2 or len(minute_text) != 2:
        return None
    try:
        return datetime.time(int(hour_text), int(minute_text))
    except ValueError:
        return None


def _is_due(clock: datetime.time, now_local: datetime.datetime, *, window_minutes: int) -> bool:
    """`clock` 是否落在 `[now-window, now)` 这个窗口里（窗口左闭右开，跨零点安全）。

    左闭右开是刻意的：窗口宽度 = 扫描间隔（都 5 分钟），闭合区间会让整点恰好被
    相邻两轮各命中一次；右开保证「同一时刻一天只命中一次」。
    """
    now_minutes = now_local.hour * 60 + now_local.minute
    clock_minutes = clock.hour * 60 + clock.minute
    delta = (now_minutes - clock_minutes) % (24 * 60)
    return 0 <= delta < window_minutes


async def _active_elders_with_clock(
    session: AsyncSession,
) -> list[tuple[uuid.UUID, str | None]]:
    """在住长者的 `(elder_id, 自定义采集时刻)`；NULL = 用全局默认。"""
    rows = await session.execute(
        select(Elder.elder_id, Elder.checkup_start)
        .where(Elder.status == _ELDER_IN_HOUSE)
        .order_by(Elder.elder_id)
    )
    return [(row[0], row[1]) for row in rows.all()]


async def run_health_checkup_due(
    *, now: datetime.datetime | None = None, driver: HealthCheckupDriver | None = None
) -> int:
    """每 5 分钟：把「生效采集时刻刚过」的在住长者起一场采集，返回新通知条数（FR-HLT-05）。

    生效时刻 = `COALESCE(elders.checkup_start, settings.health_checkup_start)`：
    长者自己设过就按他的，没设就回落到全局默认（19:00）——所以「不改任何配置」的
    群体行为与旧的 19:00 定时任务完全一致，只是命中方式从单点 cron 换成了窗口扫描。

    幂等（三重闸，缺一层都会重复采集）：
    ① 今日**已有会话**（任何状态）→ 跳过，`skipped_count` 计一笔；
    ② 今日已有当前生效版本的**健康记录** → 跳过；
    ③ 通知按 `(kind, elder_id, business_date)` 查重（与旧任务同口径）。

    `driver=None` 时仍落提醒通知（照护台看得见今天该采谁），但一场会话都不起，
    并在日志里如实写 `driver_unavailable`——与 `run_health_checkup_start` 同一口径。
    """
    settings = get_settings()
    now_local = (now or datetime.datetime.now(ZoneInfo(settings.scheduler_timezone))).astimezone(
        ZoneInfo(settings.scheduler_timezone)
    )
    day = now_local.date()
    global_clock = _parse_hhmm(settings.health_checkup_start)
    if global_clock is None:  # 配置本身非法：早炸在 build_scheduler，这里只防御
        logger.warning(
            "health checkup due skipped: invalid global clock",
            extra={"job": CHECKUP_DUE_JOB_NAME, "clock": settings.health_checkup_start},
        )
        return 0

    created = 0
    started = 0
    skipped = 0
    try:
        async with session_factory() as session:
            if driver is None:
                logger.warning(
                    "health checkup sessions skipped: driver_unavailable",
                    extra={"job": CHECKUP_DUE_JOB_NAME, "business_date": day.isoformat()},
                )
            for elder_id, custom_clock in await _active_elders_with_clock(session):
                clock_text = custom_clock or settings.health_checkup_start
                clock = _parse_hhmm(clock_text)
                if clock is None:
                    logger.warning(
                        "health checkup due skipped: invalid elder clock",
                        extra={
                            "job": CHECKUP_DUE_JOB_NAME,
                            "elder_id": str(elder_id),
                            "clock": clock_text,
                        },
                    )
                    continue
                if not _is_due(clock, now_local, window_minutes=CHECKUP_DUE_WINDOW_MINUTES):
                    continue
                if await _has_checkup_session(session, elder_id, day) or await _has_health_record(
                    session, elder_id, day
                ):
                    skipped += 1
                    continue
                if not await _notification_exists(session, "health_review", elder_id, day):
                    await notification_service.create_notification(
                        session,
                        kind="health_review",
                        severity="yellow",
                        elder_id=elder_id,
                        payload={
                            "module": "health_checkup",
                            "state": "pending_collection",
                            "business_date": day.isoformat(),
                            "scheduled_clock": clock_text,
                            "source": "elder" if custom_clock else "global",
                            "driver_state": "wired" if driver is not None else "driver_unavailable",
                            "collectable": driver is not None,
                            "summary": (
                                f"{clock_text} 健康采集已启动，本日尚无健康记录"
                                if driver is not None
                                else f"{clock_text} 健康采集未启动：采集驱动未装配，本日无法采集，请联系管理员"
                            ),
                        },
                        # 与上一条路径同键：同一长者同一天只有一条「待采集」提醒，
                        # 自定义采集时刻与全局 19:00 撞到同一天也只留一条。
                        dedupe_key=f"health_review:checkup:{elder_id}:{day.isoformat()}",
                    )
                    created += 1
                if driver is None:
                    continue
                try:
                    started_now = await driver.start_due(
                        session, elder_id=elder_id, record_date=day, now=now
                    )
                except Exception as exc:
                    logger.warning(
                        "health checkup start failed for elder",
                        extra={
                            "job": CHECKUP_DUE_JOB_NAME,
                            "business_date": day.isoformat(),
                            "elder_id": str(elder_id),
                            "error_type": type(exc).__name__,
                        },
                    )
                    continue
                if started_now:
                    started += 1
            await session.commit()
    except Exception:
        logger.exception(
            "health checkup due job failed",
            extra={"job": CHECKUP_DUE_JOB_NAME, "business_date": day.isoformat()},
        )
        return created
    logger.info(
        "health checkup due job done",
        extra={
            "job": CHECKUP_DUE_JOB_NAME,
            "business_date": day.isoformat(),
            "created_count": created,
            "started_count": started,
            "skipped_count": skipped,
            "driver_state": "wired" if driver is not None else "driver_unavailable",
        },
    )
    return created


def register_health_checkup_jobs(
    scheduler: AsyncIOScheduler,
    *,
    driver: HealthCheckupDriver | None = None,
    interval_seconds: int = CHECKUP_TIMEOUT_INTERVAL_SECONDS,
    due_interval_seconds: int = CHECKUP_DUE_INTERVAL_SECONDS,
) -> None:
    """把「60 秒超时扫描」与「5 分钟采集时刻窗口扫描」两条 interval job 挂上调度器。

    驱动在注册时用 `partial` 绑进任务体：调度器只会以「无实参」调用任务，
    绑定后任务跑起来拿到的就是装配根递进来的那个驱动（与用药触达同一手法）。

    FR-HLT-05 之后**不再有 19:00 的单点 cron**：采集时刻按长者自定义值
    （`elders.checkup_start`，NULL 回落全局默认）由窗口扫描命中，见
    `run_health_checkup_due`。旧的 `run_health_checkup_start` 函数保留，但不再注册——
    两套都注册会让「没改过时刻」的群体在默认时刻起两场采集。

    函数内 import `JOB_PREFIX`：`app/scheduler/__init__.py` 会 import 本模块，
    模块级再反向 import 就成环了（与 `app/scheduler/medication.py` 同一手法）。
    """
    from app.scheduler import JOB_PREFIX

    scheduler.add_job(
        partial(run_health_checkup_timeout, driver=driver),
        trigger="interval",
        seconds=max(int(interval_seconds), 1),
        id=f"{JOB_PREFIX}:{CHECKUP_TIMEOUT_JOB_NAME}",
        name=CHECKUP_TIMEOUT_JOB_NAME,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=300,
    )

    # 窗口宽度与扫描间隔一致（都 5 分钟）：命中区间左闭右开，一天只会命中一次。
    scheduler.add_job(
        partial(run_health_checkup_due, driver=driver),
        trigger="interval",
        seconds=max(int(due_interval_seconds), 1),
        id=f"{JOB_PREFIX}:{CHECKUP_DUE_JOB_NAME}",
        name=CHECKUP_DUE_JOB_NAME,
        replace_existing=True,
        coalesce=True,  # 错过多次只补跑一次：补跑也只命中当前窗口，不会一天采两场
        max_instances=1,
        misfire_grace_time=300,
    )


async def run_missed_scan() -> int:
    """20:00：当日仍无健康记录的在住长者 → `collection_missed`，派负责护工。"""
    day = _today()
    created = 0
    try:
        async with session_factory() as session:
            for elder_id in await _active_elder_ids(session):
                if await _has_health_record(session, elder_id, day):
                    continue
                if await _notification_exists(session, "collection_missed", elder_id, day):
                    continue
                staff_id = await _primary_care_staff(session, elder_id)
                await notification_service.create_notification(
                    session,
                    kind="collection_missed",
                    severity="red",
                    elder_id=elder_id,
                    payload={
                        "module": "health_checkup",
                        "state": "missing",
                        "business_date": day.isoformat(),
                        "summary": "20:00 采集窗口已结束，本日无健康记录",
                    },
                    assignee_staff_id=staff_id,
                )
                # FR-HLT-08 是「漏采**派单**」：只发通知不算交付——通知是提醒，工单才是
                # 派给护工的动作。幂等由上面 `_notification_exists(..., day)` 那把 continue
                # 兜住（同一长者每天只走一次），所以这里不会重复建单。
                await work_order_service.create_work_order(
                    session,
                    elder_id=elder_id,
                    type="collection_missed",
                    severity="red",
                    note=(
                        f"20:00 健康采集窗口结束，{day.isoformat()} 仍无任何健康记录"
                        "（系统未完成采集，需护工上门补采）"
                    ),
                    assignee_staff_id=staff_id,
                )
                created += 1
            await session.commit()
    except Exception:
        logger.exception(
            "health missed scan job failed",
            extra={"job": "health_missed_scan", "business_date": day.isoformat()},
        )
        return created
    logger.info(
        "health missed scan job done",
        extra={
            "job": "health_missed_scan",
            "business_date": day.isoformat(),
            "created_count": created,
        },
    )
    return created


__all__ = [
    "CHECKUP_DUE_INTERVAL_SECONDS",
    "CHECKUP_DUE_JOB_NAME",
    "CHECKUP_DUE_WINDOW_MINUTES",
    "CHECKUP_TIMEOUT_INTERVAL_SECONDS",
    "CHECKUP_TIMEOUT_JOB_NAME",
    "HealthCheckupDriver",
    "register_health_checkup_jobs",
    "run_health_checkup_due",
    "run_health_checkup_start",
    "run_health_checkup_timeout",
    "run_missed_scan",
]
