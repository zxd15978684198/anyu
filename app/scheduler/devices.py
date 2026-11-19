"""设备掉线扫描的调度入口（FR-ADM-10 后半：心跳超时 → 派单 + 通知管理员与护工）。

**编排逻辑已下沉**到 `app/services/device_monitor_service.py`（批 3 前置，Celery 迁移的
第一步：先把业务搬进唯一写库层，再换触发者）。本模块只剩三件事：

1. 薄壳 `run_device_offline_scan()`：建 session → 调 service → commit，并记原样日志；
2. `register_device_jobs()`：把薄壳挂到 APScheduler（job id 与间隔保持不变）；
3. 常量/入口的**再导出**：既有调用方与用例仍按 `app.scheduler.devices.<NAME>` 取用，
   Celery 切流时两条路径读同一份口径。

判据、阈值、幂等键、收件人逻辑与 `OfflineScanStats` 的字段说明，见
`app/services/device_monitor_service.py` 的模块 docstring——**别再抄一份到这里**。
"""

from __future__ import annotations

import datetime
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.db import session_factory
from app.services import device_monitor_service
from app.services.device_monitor_service import (
    OFFLINE_JOB_NAME,
    OFFLINE_NOTIFICATION_KIND,
    OFFLINE_ORDER_SEVERITY,
    OFFLINE_ORDER_TYPE,
    OFFLINE_SCAN_INTERVAL_SECONDS,
    OFFLINE_THRESHOLD_MINUTES,
)

logger = logging.getLogger(__name__)


async def run_device_offline_scan(*, now: datetime.datetime | None = None) -> int:
    """扫一轮掉线设备，返回本轮新单数（调用方拿它记日志/计数）。

    `now` 可注入是为了让用例把时钟拨过阈值，而不是 `sleep(300)` 等真时间。
    失败不抛：记满上下文后返回已统计到的条数，等下一轮补跑——与下沉前一致。
    """
    stats: device_monitor_service.OfflineScanStats | None = None
    try:
        async with session_factory() as session:
            stats = await device_monitor_service.scan_offline_devices(session, now=now)
            await session.commit()
    except Exception:
        logger.exception(
            "device offline scan job failed",
            extra={"job": OFFLINE_JOB_NAME, "threshold_minutes": OFFLINE_THRESHOLD_MINUTES},
        )
        return stats.created if stats is not None else 0
    logger.info(
        "device offline scan job done",
        extra={
            "job": OFFLINE_JOB_NAME,
            "created_count": stats.created,
            "device_total": stats.device_total,
            "unbound_device_count": stats.unbound_device_count,
            "locked_offline_count": stats.locked_offline_count,
            "elder_away_count": stats.elder_away_count,
            "open_order_count": stats.open_order_count,
            "threshold_minutes": OFFLINE_THRESHOLD_MINUTES,
        },
    )
    return stats.created


#: 旧名（= `run_device_offline_scan`）：job 注册与既有用例仍按这个名字取函数，
#: 换名只发生在「哪个模块持有实现」，不发生在「入口叫什么」。
scan_offline_devices = run_device_offline_scan


def register_device_jobs(
    scheduler: AsyncIOScheduler,
    *,
    interval_seconds: int = OFFLINE_SCAN_INTERVAL_SECONDS,
) -> None:
    """把掉线扫描挂到调度器上（interval，默认 60 秒）。

    函数内 import `JOB_PREFIX`：`app/scheduler/__init__.py` 会 import 本模块，模块级再
    反向 import 就成环了（与 `app/scheduler/medication.py`、`health.py` 同一手法）。
    """
    from app.scheduler import JOB_PREFIX

    scheduler.add_job(
        run_device_offline_scan,
        trigger="interval",
        seconds=max(int(interval_seconds), 1),
        id=f"{JOB_PREFIX}:{OFFLINE_JOB_NAME}",
        name=OFFLINE_JOB_NAME,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=300,
    )


__all__ = [
    "OFFLINE_JOB_NAME",
    "OFFLINE_NOTIFICATION_KIND",
    "OFFLINE_ORDER_SEVERITY",
    "OFFLINE_ORDER_TYPE",
    "OFFLINE_SCAN_INTERVAL_SECONDS",
    "OFFLINE_THRESHOLD_MINUTES",
    "register_device_jobs",
    "run_device_offline_scan",
    "scan_offline_devices",
]
