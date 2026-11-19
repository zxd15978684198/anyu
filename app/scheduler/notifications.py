"""通知到点未确认的自动升级（N30；PRD §4.7）。

## 为什么是 interval 轮询，而不是投递时挂一个定时器

升级的判据是「墙上的时间过了 120 秒还没人确认」，不是「某个回调该到点了」：
进程重启、`--reload`、投递动作本身失败，都不该让升级丢失。所以这里是一条
**无状态**的轮询任务，每次重新问库要候选；幂等闸门在
`notification_service.escalate_unacknowledged`（`payload.escalated_at`）。

## 为什么 `now` 能注入

120 秒边界靠 sleep 去测是不可接受的（用例要么慢两分钟、要么靠抖动）。任务体与
服务层都收 `now`，判定与落库时间戳同源，用例可以把时钟拨到边界两侧各跑一次。

## 写库归服务层

本文件只「开 session → 调服务 → commit」（AGENTS §2：services 是唯一写库层）。
"""

from __future__ import annotations

import datetime
import logging
from typing import Final

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.context import get_trace_id
from app.db import session_factory
from app.services import notification_service

logger = logging.getLogger(__name__)

#: 任务短名；与 `app/scheduler/__init__.py` 拼成 `anyu:notification-escalation`。
JOB_NAME: Final[str] = "notification-escalation"

#: 轮询间隔：60s 足够贴近 120s 的判据（最坏多等一轮），又不会把库点着。
ESCALATION_INTERVAL_SECONDS: Final[int] = 60


async def run_notification_escalation(
    *,
    now: datetime.datetime | None = None,
    delay_seconds: int = notification_service.ESCALATION_DELAY_SECONDS,
) -> list[str]:
    """扫一轮并提交：返回被升级的通知 id（文本），失败返回已处理的部分。

    异常只记日志、不往外抛：调度器里一次失败不该让后续 tick 全停。返回已升级的
    那一批而不是 0，是为了让「升了几条」在日志与用例里都可核对。
    """
    escalated: list[str] = []
    try:
        async with session_factory() as session:
            ids = await notification_service.escalate_unacknowledged(
                session,
                now=now,
                delay_seconds=delay_seconds,
            )
            await session.commit()
            escalated = [str(item) for item in ids]
    except Exception:
        logger.exception(
            "notification escalation job failed",
            extra={
                "job": "notification_escalation",
                "trace_id": get_trace_id() or "-",
            },
        )
        return escalated

    logger.info(
        "notification escalation job done",
        extra={
            "job": "notification_escalation",
            "escalated_count": len(escalated),
            "delay_seconds": delay_seconds,
        },
    )
    return escalated


def register_notification_jobs(
    scheduler: AsyncIOScheduler,
    *,
    interval_seconds: int = ESCALATION_INTERVAL_SECONDS,
) -> None:
    """挂上升级任务；与用药/维护任务同口径（replace_existing / 单实例 / coalesce）。

    `JOB_PREFIX` 在函数内 import：`app/scheduler/__init__.py` 在**定义它之前**就
    已经 import 了本模块，模块级反向 import 会成环（与 `medication.py` 同一条注释）。
    """
    from app.scheduler import JOB_PREFIX

    scheduler.add_job(
        run_notification_escalation,
        trigger="interval",
        seconds=interval_seconds,
        id=f"{JOB_PREFIX}:{JOB_NAME}",
        name=JOB_NAME,
        replace_existing=True,
        coalesce=True,  # 错过多次只补跑一次
        max_instances=1,  # 上一轮没跑完不叠加
        misfire_grace_time=300,
    )
    logger.info(
        "scheduler job registered",
        extra={"job_id": f"{JOB_PREFIX}:{JOB_NAME}", "interval_seconds": interval_seconds},
    )


__all__ = [
    "ESCALATION_INTERVAL_SECONDS",
    "JOB_NAME",
    "register_notification_jobs",
    "run_notification_escalation",
]
