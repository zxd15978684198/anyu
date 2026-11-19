"""日报任务：07:00 生成（PRD §4.8 FR-RPT-01）+ 家属推送重试轮询（FR-RPT-03 / AC-25）。

## 生成（`run_daily_report_generation`）

本模块只负责「什么时候跑、用哪个 session、什么时候 commit」：选人（`Elder.status ==
"active"`）与逐条兜底都在 `report_service.generate_for_active_elders()` 里（批 0-B
下沉，调度侧不再维护业务口径）。任务体是薄壳——建 session → 调服务 → commit。
服务层保证幂等（UNIQUE(elder_id, report_date, version) + 内容未变则复用），所以本任务
补跑/重复触发都不会产出第二行，这条性质是任务可以不记账就能安全重试的前提。

单个长者失败**不连坐**由服务层逐条 try/except 保证；本层只兜住「开 session / 整批
提交」这类任务级失败，记日志不外抛。

## 推送重试（`run_daily_report_delivery`）

`notification_service.deliver_due_daily_reports` 只在「到点 + 有人来问」时推进，因此
必须有一条轮询任务在墙上时间走过 +1/+5/+15 分钟后把它叫起来——没有这条任务，重试
簿记只是躺在 `payload.delivery` 里，AC-25 的自动重试在**生产上不会真正发生**。这与
`app/scheduler/notifications.py` 的 120s 未确认升级同构：无状态轮询、幂等闸门在服务层、
`now` 可注入（睡 15 分钟去测重试是不可接受的）。
"""

from __future__ import annotations

import datetime
import logging
from typing import Final
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.config import get_settings
from app.core.context import get_trace_id
from app.db import session_factory
from app.services import notification_service, report_service

logger = logging.getLogger(__name__)

#: 推送重试轮询的任务短名（与 `app/scheduler/__init__.py` 拼成
#: `anyu:daily-report-delivery`）。
DELIVERY_JOB_NAME: Final[str] = "daily-report-delivery"

#: 轮询间隔：重试点是 +1/+5/+15 分钟，60s 一轮最坏迟到 1 分钟，足够贴近又不点库。
DELIVERY_INTERVAL_SECONDS: Final[int] = 60


def _today() -> datetime.date:
    return datetime.datetime.now(ZoneInfo(get_settings().scheduler_timezone)).date()


async def run_daily_report_generation() -> int:
    """为在住长者生成当日日报，返回成功条数。

    薄壳：选人、逐条生成、单条失败回滚继续全部在
    `report_service.generate_for_active_elders()`；本函数只开 session、提交、
    把任务级异常记日志（不外抛，一次 tick 失败不该让后续 tick 全停）。
    """
    day = _today()
    generated = 0
    failed = 0
    try:
        async with session_factory() as session:
            generated, failed = await report_service.generate_for_active_elders(session, day)
            await session.commit()
    except Exception:
        logger.exception(
            "daily report job failed",
            extra={"job": "daily_report_generate", "business_date": day.isoformat()},
        )
        return generated
    logger.info(
        "daily report job done",
        extra={
            "job": "daily_report_generate",
            "business_date": day.isoformat(),
            "generated": generated,
            "failed": failed,
        },
    )
    return generated


async def run_daily_report_delivery(
    *,
    now: datetime.datetime | None = None,
    sender: notification_service.DailyReportSender | None = None,
) -> int:
    """推一轮到期的日报通知并提交，返回本轮扫到的条数。

    `sender` 只为测试注入（默认「模拟推送成功」）；异常只记日志不上抛——调度器里
    一次失败不该让后续 tick 全停，失败的通知行仍留在库里等下一轮按 +5/+15 再试。
    """
    processed = 0
    try:
        async with session_factory() as session:
            processed = await notification_service.deliver_due_daily_reports(
                session,
                now=now,
                sender=sender,
            )
            await session.commit()
    except Exception:
        logger.exception(
            "daily report delivery job failed",
            extra={"job": "daily_report_delivery", "trace_id": get_trace_id() or "-"},
        )
        return processed

    logger.info(
        "daily report delivery job done",
        extra={"job": "daily_report_delivery", "scanned": processed},
    )
    return processed


def register_daily_report_delivery_job(
    scheduler: AsyncIOScheduler,
    *,
    interval_seconds: int = DELIVERY_INTERVAL_SECONDS,
) -> None:
    """挂上「日报推送到点扫描」任务；与通知升级任务同口径（单实例 / coalesce）。

    `JOB_PREFIX` 在函数内 import：`app/scheduler/__init__.py` 在定义它之前就已 import
    本模块，模块级反向 import 会成环（与 `app/scheduler/notifications.py` 同一条注释）。
    """
    from app.scheduler import JOB_PREFIX

    scheduler.add_job(
        run_daily_report_delivery,
        trigger="interval",
        seconds=interval_seconds,
        id=f"{JOB_PREFIX}:{DELIVERY_JOB_NAME}",
        name=DELIVERY_JOB_NAME,
        replace_existing=True,
        coalesce=True,  # 错过多次只补跑一次
        max_instances=1,  # 上一轮没跑完不叠加
        misfire_grace_time=300,
    )
    logger.info(
        "scheduler job registered",
        extra={"job_id": f"{JOB_PREFIX}:{DELIVERY_JOB_NAME}", "interval_seconds": interval_seconds},
    )


__all__ = [
    "DELIVERY_INTERVAL_SECONDS",
    "DELIVERY_JOB_NAME",
    "register_daily_report_delivery_job",
    "run_daily_report_delivery",
    "run_daily_report_generation",
]
