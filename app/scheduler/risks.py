"""红色事件 120 秒超时升级的轮询任务（AC-12）。

## 为什么是 interval 轮询，而不是建事件时挂一个定时器

要判的是「墙上的时间过了 120 秒还没人确认」，不是「某个回调该到点了」：进程重启、
`--reload`、建事件那条事务后面又回滚了，都不该让升级丢失或重复。所以这里是一条
**无状态**的轮询任务，每次重新问库要候选；幂等闸门在
`risk_event_service.escalate_overdue_red_events` 的 `status == 'open'` 条件里。

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
from app.services import risk_event_service

logger = logging.getLogger(__name__)

#: 任务短名；与 `app/scheduler/__init__.py` 拼成 `anyu:risk-event-escalation`。
JOB_NAME: Final[str] = "risk-event-escalation"

#: 轮询间隔：60s 足够贴近 120s 的判据（最坏多等一轮），又不会把库点着。
ESCALATION_INTERVAL_SECONDS: Final[int] = 60


async def run_red_event_escalation(
    *,
    now: datetime.datetime | None = None,
    timeout_seconds: int = risk_event_service.ESCALATION_TIMEOUT_SECONDS,
) -> list[str]:
    """扫一轮并提交：返回被升级的事件 id（文本），失败返回已处理的部分。

    异常只记日志、不往外抛：调度器里一次失败不该让后续 tick 全停。返回已升级的
    那一批而不是空表，是为了让「升了几条」在日志与用例里都可核对。
    """
    escalated: list[str] = []
    try:
        async with session_factory() as session:
            ids = await risk_event_service.escalate_overdue_red_events(
                session,
                now=now,
                timeout_seconds=timeout_seconds,
            )
            await session.commit()
            escalated = [str(item) for item in ids]
    except Exception:
        logger.exception(
            "risk event escalation job failed",
            extra={
                "job": "risk_event_escalation",
                "trace_id": get_trace_id() or "-",
            },
        )
        return escalated

    logger.info(
        "risk event escalation job done",
        extra={
            "job": "risk_event_escalation",
            "escalated_count": len(escalated),
            "timeout_seconds": timeout_seconds,
        },
    )
    return escalated


def register_risk_event_jobs(
    scheduler: AsyncIOScheduler,
    *,
    interval_seconds: int = ESCALATION_INTERVAL_SECONDS,
) -> None:
    """挂上升级任务；与用药/维护/通知任务同口径（replace_existing / 单实例 / coalesce）。

    `JOB_PREFIX` 在函数内 import：`app/scheduler/__init__.py` 在**定义它之前**就
    已经 import 了本模块，模块级反向 import 会成环（与 `medication.py`、
    `notifications.py` 同一条注释）。
    """
    from app.scheduler import JOB_PREFIX

    scheduler.add_job(
        run_red_event_escalation,
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
    "register_risk_event_jobs",
    "run_red_event_escalation",
]
