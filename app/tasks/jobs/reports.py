"""批 1 · 日报生成 / 日报推送的 Celery 任务体（FR-RPT-01 / FR-RPT-03 / AC-25）。

## 任务体只做三件事

建 session → 调 `app/services` 里现成函数 → commit。**业务规则一行都不在这里**：
「谁该生成、重试到第几分钟、推送怎么算送达」都在 `report_service` /
`notification_service`；调度层只负责「到点把服务叫起来」。这条纪律的直接收益是
同一个服务函数既能被 APScheduler 触发、也能被 Celery 触发，批 1 的迁移因此是
「换触发者不换逻辑」，回滚等于删掉一条 beat entry。

## 为什么这里不 import app.tasks.celery_app

骨架（`app/tasks/celery_app.py`）要在导入期把本模块的 `BEAT_ENTRIES` 收进静态
`beat_schedule`；本模块若反向 import 骨架就是循环导入。故本模块只**对外提供**
`BEAT_ENTRIES` 与 `register(celery_app)`，由骨架/装配方单向拉取。

## 三个容易踩的点（实测结论，别改回去）

1. **`BEAT_ENTRIES` 走模块级 `__getattr__` 懒展开**：crontab 的小时来自
   `settings.daily_report_generate_hour`，必须**读到配置时**才展开。写成模块级常量
   会在导入期把小时冻死——测试 `get_settings.cache_clear()` 或换环境后，beat 表还是旧小时。
2. **Celery 不支持 async 任务体**：`celery_app.task(...)(async_fn)` 拿到的是一个没人
   await 的 coroutine，任务「跑完」却什么都没做（只留一条 RuntimeWarning）。所以
   `register()` 注册的是 `_sync` 适配器，真正干活的是本模块的 `async def` —— 也因此
   这两个函数可以被测试直接 `await`，不必经过 broker。
3. **同步适配走 `_runtime.sync_task`**：全仓 job 模块共用同一份适配器（共享常驻事件
   循环 + `task.<短名>` span），不在单个模块里各写一份。`asyncio.run` 会让 asyncpg
   连接跨 loop（实测 `'NoneType' object has no attribute 'send'`），已统一弃用。

## 异常分类

可恢复（连接类）与不可恢复（逻辑错）分开记日志，**两者都 re-raise**：worker 侧靠
`autoretry_for` 只对可恢复错误重试（§5 可靠性清单），逻辑错重试只是把错误延后。
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Final
from zoneinfo import ZoneInfo

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.core.config import get_settings
from app.core.context import new_trace_id, reset_trace_id, set_trace_id
from app.db import session_factory
from app.services import notification_service, report_service
from app.tasks.jobs._runtime import sync_task

if TYPE_CHECKING:
    from celery import Celery

logger = logging.getLogger(__name__)

#: 任务短名；与 APScheduler 版的 job 名同名，便于日志/看板对照。
JOB_NAME_GENERATE: Final[str] = "daily-report-generate"
JOB_NAME_DELIVERY: Final[str] = "daily-report-delivery"

#: Celery 任务全路径名（`app.tasks.jobs.<模块>.<函数>` 口径，与骨架约定一致）。
TASK_GENERATE: Final[str] = "app.tasks.jobs.reports.daily_report_generate"
TASK_DELIVERY: Final[str] = "app.tasks.jobs.reports.daily_report_delivery"

#: 可恢复错误：网络/连接类。只有这些进 `autoretry_for`，其余错误重试无意义（§5）。
RECOVERABLE_ERRORS: Final[tuple[type[BaseException], ...]] = (
    ConnectionError,
    TimeoutError,
    RedisConnectionError,
    RedisTimeoutError,
)

#: 任务级可靠性参数（§5：soft < hard 留收尾余地；退避 + 抖动避免重试风暴）。
TASK_OPTIONS: Final[dict[str, Any]] = {
    "autoretry_for": RECOVERABLE_ERRORS,
    "retry_backoff": True,
    "retry_jitter": True,
    "max_retries": 3,
    "soft_time_limit": 30,
    "time_limit": 60,
    "ignore_result": True,
}


def _business_date() -> datetime.date:
    """业务日期按 `scheduler_timezone`（Asia/Shanghai）算，不用服务器本地时区。

    07:00 生成的是「今天」的日报；容器默认 UTC，直接 `date.today()` 在东八区
    07:00 会落成前一天（与 `app/scheduler/reports.py:_today` 同一口径）。
    """
    tz = ZoneInfo(get_settings().scheduler_timezone)
    return datetime.datetime.now(tz).date()


def _parse_hour(raw: str) -> int:
    """`daily_report_generate_hour` 形如 `"07:00"` → `7`；坏值回落 7 点并告警。"""
    head = raw.strip().split(":", 1)[0]
    try:
        return int(head)
    except (TypeError, ValueError):
        logger.warning(
            "daily_report_generate_hour 不是 HH:MM，回落到 07:00",
            extra={"job": JOB_NAME_GENERATE, "daily_report_generate_hour": raw},
        )
        return 7


def _beat_entries() -> dict[str, dict[str, Any]]:
    """构造静态 beat 表；**在调用时**读配置（见模块文档第 1 点）。"""
    # 延迟 import：celery 依赖随批 1 落地，导入期缺依赖不该让整个 app/tasks 炸掉。
    from celery.schedules import crontab

    settings = get_settings()
    hour = _parse_hour(settings.daily_report_generate_hour)
    return {
        JOB_NAME_GENERATE: {
            "task": TASK_GENERATE,
            "schedule": crontab(hour=hour, minute=0),
        },
        # 每 60s 推一轮到期的日报通知：重试点是 +1/+5/+15 分钟，靠轮询推进，
        # 不把 60 秒等进 worker（§5 最后两行）。
        JOB_NAME_DELIVERY: {
            "task": TASK_DELIVERY,
            "schedule": crontab(minute="*"),
        },
    }


# 只有注解、没有赋值：读取走下面的模块级 `__getattr__`，保证「每次访问都读当前配置」。
BEAT_ENTRIES: dict[str, dict[str, Any]]


def __getattr__(name: str) -> Any:
    """PEP 562：`reports.BEAT_ENTRIES` 每次访问时按当前配置现算一份。"""
    if name == "BEAT_ENTRIES":
        return _beat_entries()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


async def daily_report_generate(*, trace_id: str | None = None) -> dict[str, int]:
    """为在住长者生成当日日报，返回 `{"generated": n, "failed": m}`。

    服务层保证幂等（UNIQUE(elder_id, report_date, version) + 内容未变复用），所以
    补跑/重复投递不会产出第二行——这正是 `task_acks_late=True`（会重）能安全落地的前提。
    """
    token = set_trace_id(trace_id or new_trace_id())
    day = _business_date()
    try:
        async with session_factory() as session:
            generated, failed = await report_service.generate_for_active_elders(session, day)
            await session.commit()
    except RECOVERABLE_ERRORS:
        logger.warning(
            "daily report generate task recoverable failure",
            extra={"job": JOB_NAME_GENERATE, "business_date": day.isoformat()},
        )
        raise
    except Exception:
        logger.exception(
            "daily report generate task failed",
            extra={"job": JOB_NAME_GENERATE, "business_date": day.isoformat()},
        )
        raise
    finally:
        reset_trace_id(token)

    logger.info(
        "daily report generate task done",
        extra={
            "job": JOB_NAME_GENERATE,
            "business_date": day.isoformat(),
            "generated": generated,
            "failed": failed,
        },
    )
    return {"generated": generated, "failed": failed}


async def daily_report_delivery(*, trace_id: str | None = None) -> dict[str, int]:
    """推一轮到期的日报通知，返回 `{"scanned": n}`。

    与 `app/scheduler/reports.py:run_daily_report_delivery` 行为等价：无状态轮询、
    幂等闸门在服务层（`payload.delivery` 的 1/5/15 分钟 + `notifications` 去重）。
    """
    token = set_trace_id(trace_id or new_trace_id())
    try:
        async with session_factory() as session:
            scanned = await notification_service.deliver_due_daily_reports(session)
            await session.commit()
    except RECOVERABLE_ERRORS:
        logger.warning(
            "daily report delivery task recoverable failure",
            extra={"job": JOB_NAME_DELIVERY},
        )
        raise
    except Exception:
        logger.exception(
            "daily report delivery task failed",
            extra={"job": JOB_NAME_DELIVERY},
        )
        raise
    finally:
        reset_trace_id(token)

    logger.info(
        "daily report delivery task done",
        extra={"job": JOB_NAME_DELIVERY, "scanned": scanned},
    )
    return {"scanned": scanned}


#: 任务名 → 真正干活的 async 函数（`register` 与测试共用同一张表，避免抄两遍）。
TASKS: Final[tuple[tuple[str, Callable[..., Awaitable[dict[str, int]]]], ...]] = (
    (TASK_GENERATE, daily_report_generate),
    (TASK_DELIVERY, daily_report_delivery),
)


def register(celery_app: Celery) -> None:
    """把两个任务注册进 `celery_app`；失败直接抛（装配期错要早暴露）。"""
    for name, task_fn in TASKS:
        celery_app.task(name=name, **TASK_OPTIONS)(sync_task(task_fn, name))
    logger.info("celery report tasks registered", extra={"tasks": [name for name, _ in TASKS]})


__all__ = [
    "BEAT_ENTRIES",
    "JOB_NAME_DELIVERY",
    "JOB_NAME_GENERATE",
    "RECOVERABLE_ERRORS",
    "TASKS",
    "TASK_DELIVERY",
    "TASK_GENERATE",
    "TASK_OPTIONS",
    "daily_report_delivery",
    "daily_report_generate",
    "register",
]
