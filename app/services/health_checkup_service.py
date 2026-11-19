"""健康采集会话的推进逻辑（scheduler 与 Celery worker 共用）。

放在 services 而不是 `app/scheduler/health.py`：Celery worker 也要扫「到期未答」，
而 worker import `app.scheduler.**` 会把整个 APScheduler 装配（devices/health/
medication/reports/notifications/risks/attention/memory + DB 引擎）拖进进程。
scheduler 侧改为反向 import 本模块，行为不变。

分层的口径没变：**写库仍只经 models 与既有 service**，这里没有新增业务规则，
只是把「谁在跑」与「怎么推进」分开。
"""

from __future__ import annotations

import datetime
import logging
import uuid
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import session_factory

logger = logging.getLogger(__name__)

#: 超时扫描任务名（job id 前缀由 `app.scheduler.JOB_PREFIX` 拼，防循环 import）。
CHECKUP_TIMEOUT_JOB_NAME = "health-checkup-timeout"

#: 扫描间隔：60 秒的等待被切成 20 秒一轮，最坏晚 20 秒推进（理由见模块 docstring）。
CHECKUP_TIMEOUT_INTERVAL_SECONDS = 20


class HealthCheckupDriver(Protocol):
    """采集会话的驱动（起一场 / 推进到期的一场）。

    为什么是 Protocol 而不是直接 import 图：AGENTS §2.1 禁止横向 import，
    `app/scheduler` 与 `app/graphs` 之间只能靠**签名**耦合——具体实现由装配根
    `app/main.py` 用 `app/graphs/health_checkup_graph` 拼出来再递进来，
    与用药的 `app.scheduler.medication.Deliver` 同一手法。
    """

    async def start_due(
        self,
        session: AsyncSession,
        *,
        elder_id: uuid.UUID,
        record_date: datetime.date,
        now: datetime.datetime | None,
    ) -> bool:
        """起一场采集问出第一题；返回「这场是本次新建的」与否。"""
        ...

    async def advance_due(self, session: AsyncSession, *, now: datetime.datetime | None) -> int:
        """把到期未答的会话推进一轮，返回推进了几场。"""
        ...


async def run_health_checkup_timeout(
    *, now: datetime.datetime | None = None, driver: HealthCheckupDriver | None = None
) -> int:
    """每 20 秒：把到期未答的采集会话推进一轮，返回推进了几场。

    `now` 可注入是为了让用例把时钟拨过 60 秒边界，而不是 `sleep(60)` 等真时间
    （PRD 的「每问等 60 秒」是业务口径，不是让测试跑一分钟）。

    `driver` 为 `None` 时直接返回 0：没有驱动就没有会话在跑，扫描无事可做；
    这里只用 debug 记一笔（这条 job 每 20 秒跑一次，warning 会刷屏）。
    """
    if driver is None:
        logger.debug(
            "health checkup timeout skipped: driver_unavailable",
            extra={"job": CHECKUP_TIMEOUT_JOB_NAME},
        )
        return 0
    advanced = 0
    try:
        async with session_factory() as session:
            advanced = await driver.advance_due(session, now=now)
            await session.commit()
    except Exception:
        logger.exception(
            "health checkup timeout job failed",
            extra={"job": CHECKUP_TIMEOUT_JOB_NAME},
        )
        return advanced
    if advanced:
        logger.info(
            "health checkup timeout job done",
            extra={"job": CHECKUP_TIMEOUT_JOB_NAME, "advanced_count": advanced},
        )
    return advanced


__all__ = [
    "CHECKUP_TIMEOUT_INTERVAL_SECONDS",
    "CHECKUP_TIMEOUT_JOB_NAME",
    "HealthCheckupDriver",
    "run_health_checkup_timeout",
]
