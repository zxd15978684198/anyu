"""记忆每周归并任务（FR-MEM-07）。

| job id | cron（`settings.scheduler_timezone`，默认 `Asia/Shanghai`） | 依据 |
| --- | --- | --- |
| `anyu:memory-merge` | 每周一 03:30 | FR-MEM-07「每周归并相似记忆」 |

## 为什么单独一个模块，而不是塞进 `maintenance.py`

`app/scheduler/maintenance.py` 里的三个任务都是**日任务**（音频清理每小时、记忆衰减
每日 03:00、容量淘汰每日 03:10）。周任务混进那个 `register_maintenance_jobs` 里会让
「维护都是一天一次」这个阅读预期失效；而它又确实属于维护面，所以本模块只做一件事、
由 `app/scheduler/__init__.py` 与其余 `register_*_jobs` 并列注册。

## 为什么是 03:30

记忆衰减固定在每日 03:00、容量淘汰 03:10（`maintenance.py`）。归并要在两者之后：

- 衰减先把「90 天没露面」的条目降权，归并的保留者才会按降权后的重要性选；
- 容量淘汰可能把低价值条目软删成 `deleted`，归并只看 `active`，先淘汰能少比几对；
- 错峰半小时，避免三个任务在同一分钟抢连接池。

## 为什么逐长者跑、单长者失败只 continue

归并是**不可逆**的状态翻转（`superseded` 之后没有任何入口能恢复）。因此失败的处理原则
与容量淘汰一致：一位长者的库错误不该让其他人的归并整晚不执行。逐长者的事务边界
（SAVEPOINT）与计数在 `memory_service.merge_all_active_elders` 里实现——手动入口
`POST /api/memory/merge` 也要同一份循环，口径只能有一处。

## 分层（AGENTS §2）

只调 `app/services/memory_service.py`；本模块不发任何 SQL、不 import `app/models`。
"""

from __future__ import annotations

import datetime
import logging
import time
from typing import Final

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.metrics import counter, observe_ms
from app.db import session_factory as db_session_factory
from app.services import memory_service

logger = logging.getLogger(__name__)

#: 任务名（job id 的 `anyu:` 之后那一段，也是日志里的名字）。
MEMORY_MERGE_JOB_NAME: Final[str] = "memory-merge"

#: 每周一。APScheduler 的 `day_of_week` 用英文缩写（`mon`），时区取 scheduler 的
#: `settings.scheduler_timezone`（默认 `Asia/Shanghai`，见 `app/scheduler/__init__.py`），
#: 因此这里不需要写时区，也不会在夏令时机器上漂移。
MEMORY_MERGE_DAY_OF_WEEK: Final[str] = "mon"
MEMORY_MERGE_HOUR: Final[int] = 3
MEMORY_MERGE_MINUTE: Final[int] = 30


def _as_utc(value: datetime.datetime | None) -> datetime.datetime:
    """统一成 UTC aware；`None` → 现在；naive 按 UTC 解释（存储口径，AGENTS §8）。"""
    if value is None:
        return datetime.datetime.now(datetime.UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.UTC)
    return value.astimezone(datetime.UTC)


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------


def register_memory_jobs(scheduler: AsyncIOScheduler) -> None:
    """挂上记忆周归并任务（`JOB_PREFIX` 函数内 import，理由见 `medication.py` 同名注释）。

    `coalesce=True` + `max_instances=1` + `misfire_grace_time=300` 与其他任务同一口径：
    进程停机后只补跑一次、上一轮没跑完不叠加；重复跑本身没有副作用（第二次 `merged=0`，
    见 `merge_duplicates` 的幂等说明）。
    """
    from app.scheduler import JOB_PREFIX

    scheduler.add_job(
        run_memory_merge,
        trigger="cron",
        day_of_week=MEMORY_MERGE_DAY_OF_WEEK,
        hour=MEMORY_MERGE_HOUR,
        minute=MEMORY_MERGE_MINUTE,
        id=f"{JOB_PREFIX}:{MEMORY_MERGE_JOB_NAME}",
        name=MEMORY_MERGE_JOB_NAME,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=300,
    )
    logger.info(
        "memory merge job registered",
        extra={
            "job_id": f"{JOB_PREFIX}:{MEMORY_MERGE_JOB_NAME}",
            "clock": (
                f"{MEMORY_MERGE_DAY_OF_WEEK} {MEMORY_MERGE_HOUR:02d}:{MEMORY_MERGE_MINUTE:02d}"
            ),
        },
    )


# ---------------------------------------------------------------------------
# 任务体：每周归并
# ---------------------------------------------------------------------------


async def run_memory_merge(*, now: datetime.datetime | None = None) -> int:
    """全库跑一遍记忆周归并（`memory_service.merge_all_active_elders`）；返回被并掉的条数。

    失败**不抛给调度器**：一位长者的库错误已经在服务层 continue 并计数，整轮失败
    （例如连接池拿不到连接）只 `logger.exception` 后返回 0 —— 周任务不是长者侧的紧急事件，
    真正要叫人的是依赖它的业务（见 `maintenance.py` 模块 docstring 同一段口径）。
    """
    current = _as_utc(now)
    started = time.perf_counter()
    merged = 0
    failed = 0

    try:
        async with db_session_factory() as session:
            report = await memory_service.merge_all_active_elders(session)
            await session.commit()
            merged = report.merged
            failed = report.failed
    except Exception:
        logger.exception("记忆周归并失败 now=%s", current.isoformat())
        merged = 0
    finally:
        counter("memory_merge_runs_total")
        counter("memory_merge_merged_total", value=merged)
        if failed:
            counter("memory_merge_failed_elders_total", value=failed)
        observe_ms("memory_merge_ms", (time.perf_counter() - started) * 1000.0)

    logger.info(
        "记忆周归并完成 merged=%s failed_elders=%s now=%s",
        merged,
        failed,
        current.isoformat(),
    )
    return merged


__all__ = [
    "MEMORY_MERGE_DAY_OF_WEEK",
    "MEMORY_MERGE_HOUR",
    "MEMORY_MERGE_JOB_NAME",
    "MEMORY_MERGE_MINUTE",
    "register_memory_jobs",
    "run_memory_merge",
]
