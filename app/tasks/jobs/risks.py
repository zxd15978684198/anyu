"""批 2 · 红色事件 120 秒超时升级 Celery 任务体（AC-12）。

## 任务体只做三件事

建 session → 调 `app/services/risk_event_service.escalate_overdue_red_events` → commit。
「只升红档、只升 open、没人确认、过 120 秒」四条判据全在服务层；调度层只负责「到点把
服务叫起来」。所以同一份服务函数既能被 APScheduler（`app/scheduler/risks.py`）触发、
也能被 Celery 触发，迁移等于「换触发者不换逻辑」，回滚就是删掉一条 beat entry。

## 为什么是每分钟一次的轮询，而不是建事件时挂一个定时器

要判的是「墙上的时间过了 120 秒还没人确认」，不是「某个回调该到点了」：进程重启、
建事件那条事务后面又回滚了，都不该让升级丢失或重复。所以这里是一条**无状态**的轮询
任务；幂等闸门在服务层的 `status == 'open'` 条件里（同一事件只会被选中一次）。

## 为什么本模块不 import app.tasks.celery_app

骨架要在导入期遍历 `app/tasks/jobs/*.py` 收走本模块的 `BEAT_ENTRIES`；反向 import 就是
`celery_app → jobs → celery_app` 的循环导入。故本模块只**对外提供** `BEAT_ENTRIES` 与
`register(celery_app)`，由骨架单向拉取。

## 两个容易踩的点（与 `reports.py`、`notifications.py` 同一写法）

1. **Celery 不支持 async 任务体**：`celery_app.task(...)(async_fn)` 拿到的是一个没人
   await 的 coroutine，任务「跑完」却什么都没做。所以 `register()` 注册的是 `_sync`
   适配器，真正干活的是本模块的 `async def` —— 也因此测试可以直接 `await`，不必过 broker。
2. **同步适配用 `asyncio.run`**：已知代价是 asyncpg 连接绑定创建它的 loop，每条任务新起
   loop 时池里的旧连接可能报 attached to a different loop。这是批 1–4 所有 job 模块的
   共同风险，要修就统一修在骨架（共享常驻 loop），**不在单个 job 里各修各的**。

## 异常分类

可恢复（连接类）与不可恢复（逻辑错）分开记日志，**两者都 re-raise**：worker 侧
`autoretry_for` 只对可恢复错误重试（计划 §5），逻辑错重试只是把错误延后。
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Final

from celery.schedules import crontab
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.core.context import new_trace_id, reset_trace_id, set_trace_id
from app.db import session_factory
from app.services import risk_event_service
from app.tasks.jobs._runtime import sync_task

if TYPE_CHECKING:
    from celery import Celery

logger = logging.getLogger(__name__)

#: 任务短名；与 APScheduler 版的 `JOB_NAME` 同名，便于日志/看板对照。
JOB_NAME: Final[str] = "risk-event-escalation"

#: Celery 任务全路径名（`app.tasks.jobs.<模块>.<函数>` 口径，与骨架约定一致）。
TASK_NAME: Final[str] = "app.tasks.jobs.risks.risk_event_escalation"

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

#: 静态 beat 表条目：由骨架自动发现并合并，不在这里改全局配置。
#: 每分钟一次与 APScheduler 的 interval 60s 等价（判据是墙上时间 120s）。
BEAT_ENTRIES: dict[str, dict[str, Any]] = {
    JOB_NAME: {
        "task": TASK_NAME,
        "schedule": crontab(minute="*"),
    },
}


async def risk_event_escalation(
    *,
    trace_id: str | None = None,
    now: datetime.datetime | None = None,
    timeout_seconds: int = risk_event_service.ESCALATION_TIMEOUT_SECONDS,
) -> list[str]:
    """扫一轮并提交：返回被升级的事件 id（文本）；失败记日志后原样 re-raise。

    `now` 可注入：120 秒边界靠 sleep 去测是不可接受的，任务体与服务层都收 `now`，
    判定与落库时间戳同源，用例可以把时钟拨到边界两侧各跑一次。
    """
    token = set_trace_id(trace_id or new_trace_id())
    try:
        async with session_factory() as session:
            ids = await risk_event_service.escalate_overdue_red_events(
                session,
                now=now,
                timeout_seconds=timeout_seconds,
            )
            await session.commit()
    except RECOVERABLE_ERRORS:
        logger.warning(
            "risk event escalation task recoverable failure",
            extra={"job": JOB_NAME, "timeout_seconds": timeout_seconds},
        )
        raise
    except Exception:
        logger.exception(
            "risk event escalation task failed",
            extra={"job": JOB_NAME, "timeout_seconds": timeout_seconds},
        )
        raise
    finally:
        reset_trace_id(token)

    escalated = [str(item) for item in ids]
    logger.info(
        "risk event escalation task done",
        extra={
            "job": JOB_NAME,
            "escalated_count": len(escalated),
            "timeout_seconds": timeout_seconds,
        },
    )
    return escalated


#: 任务名 → 真正干活的 async 函数（`register` 与测试共用同一张表，避免抄两遍）。
TASKS: Final[tuple[tuple[str, Callable[..., Awaitable[list[str]]]], ...]] = (
    (TASK_NAME, risk_event_escalation),
)


def register(celery_app: Celery) -> None:
    """把任务注册进 `celery_app`；失败直接抛（装配期错要早暴露）。"""
    for name, task_fn in TASKS:
        celery_app.task(name=name, **TASK_OPTIONS)(sync_task(task_fn, name))
    logger.info("celery risk task registered", extra={"tasks": [name for name, _ in TASKS]})


__all__ = [
    "BEAT_ENTRIES",
    "JOB_NAME",
    "RECOVERABLE_ERRORS",
    "TASKS",
    "TASK_NAME",
    "TASK_OPTIONS",
    "register",
    "risk_event_escalation",
]
