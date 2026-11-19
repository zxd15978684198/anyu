"""72h 原始音频清理的 Celery 任务（批 3；技术设计文档 §1.2 第 8 条 / PRD FR-TC-07）。

## 为什么这个任务可以先迁、而且适合当第一块试验田

清理判据是文件 `mtime`，**不读库、不开 session、没有事务语义**：同一轮重跑只是再扫一遍
目录，删不掉的（权限/占用）留到下一轮，天然幂等。所以「换成 Celery 触发」不会引入新的
一致性风险，回滚也只是把 beat 条目摘掉、让 APScheduler 那条继续跑。

## 为什么任务体不 import `app/scheduler/maintenance.py`

那条路会执行 `app/scheduler/__init__.py`，它 import 了 devices / health / medication /
reports / notifications / risks / attention / memory 全部模块——等于为了删几个文件，把
整个 APScheduler 装配连同 DB 引擎、models、图驱动拖进 worker。实现因此下沉到
`app/services/audio_retention_service.py`（依赖方向 `tasks → services`，AGENTS §2.1），
调度器侧的 `run_audio_cleanup()` 反过来调同一份实现，两条触发路径行为一致。

## 为什么本模块不 import `app.tasks.celery_app`

`app/tasks/celery_app.py` 会遍历 `app/tasks/jobs/*.py` 调各自的 `register()` 并合并
`BEAT_ENTRIES`；本模块反向 import 它就成环。实例由 `register(app)` 的参数传入。

## 为什么任务体收 `now` / `root`

72 小时边界靠等是测不出来的（要等三天）。`now` 让判定时钟可注入，`root` 让扫描目录指到
`tmp_path`——两者都只是把参数透传给 service，任务体自己不做判定。

## 与 APScheduler 版的差别

`counter()` / `observe_ms()` 打点位置与名称完全不变（在 service 里，两条路径共用）：
`audio_cleanup_runs_total`、`audio_cleanup_files_total`、`audio_cleanup_ms`。worker 侧
另按计划 §6 自造一个 trace_id，保证任务日志能串起来（静态 beat 的 signature 固定，
beat 无法按次注入 trace 头）。
"""

from __future__ import annotations

import datetime
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Final

from celery.schedules import crontab

from app.core.context import get_trace_id, new_trace_id, reset_trace_id, set_trace_id
from app.services import audio_retention_service
from app.tasks.jobs._runtime import sync_task

if TYPE_CHECKING:
    from celery import Celery

logger = logging.getLogger(__name__)

#: 任务短名；与 APScheduler 版的 `_AUDIO_CLEANUP_JOB` 同名，便于日志/看板对照。
JOB_NAME: Final[str] = "audio-cleanup"

#: Celery 任务全路径名（`app.tasks.jobs.<module>.<fn>` 口径，与骨架约定一致）。
TASK_NAME: Final[str] = "app.tasks.jobs.maintenance.audio_cleanup"

#: 静态 beat 表条目：由 `app/tasks/celery_app.py` 自动发现并合并。
#: 每小时整点一次，与 APScheduler 版 `cron minute=0` 等价（PRD/技术文档只说 72h 内必须
#: 删掉，没有更细的时效要求；一小时一轮足够把误差压在保留期上界附近）。
BEAT_ENTRIES: dict[str, dict[str, object]] = {
    JOB_NAME: {
        "task": TASK_NAME,
        "schedule": crontab(minute=0),
    },
}


async def audio_cleanup(
    *,
    now: datetime.datetime | None = None,
    root: Path | str | None = None,
) -> int:
    """异步任务体：跑一轮清理并返回删除条数。

    真正的删除、打点、日志都在 `audio_retention_service`；这里只负责两件任务侧的事：
    绑定一个 trace_id（worker 每条日志才有稳定 trace，计划 §6 第 1 条），以及把异常留给
    Celery 的 `autoretry_for` —— 文件系统抖一下（`OSError`）重试一轮是安全的，因为
    清理本身幂等。
    """
    trace_id = new_trace_id()
    token = set_trace_id(trace_id)
    try:
        removed = await audio_retention_service.run_audio_cleanup(now=now, root=root)
    except OSError:
        # 文件占用/权限这类瞬时错误：与 `autoretry_for=(OSError,)` 对齐，记 warning 而不是
        # exception——重试一轮多半就好了，别在告警里制造噪音。
        logger.warning(
            "audio cleanup task recoverable failure",
            extra={"job": JOB_NAME, "trace_id": trace_id},
        )
        raise
    except Exception:
        logger.exception(
            "audio cleanup task failed",
            extra={"job": JOB_NAME, "trace_id": trace_id},
        )
        raise
    finally:
        reset_trace_id(token)

    logger.info(
        "audio cleanup task done",
        extra={
            "job": JOB_NAME,
            "trace_id": get_trace_id() or trace_id,
            "removed_count": removed,
        },
    )
    return removed


def register(celery_app: Celery) -> None:
    """把任务挂到骨架上；只对 `OSError` 自动重试。

    文件占用/权限这类瞬时错误重试一轮有意义（下一轮多半就删掉了）；逻辑错
    （`ValueError` 之类）重试只是把错误延后，不列进来。
    """
    celery_app.task(
        name=TASK_NAME,
        autoretry_for=(OSError,),
        retry_backoff=True,
        retry_jitter=True,
        max_retries=3,
        # 纯文件清理：比 DB 任务宽（计划 §5「纯文件清理可放宽到 120」），
        # 但仍要 soft < hard，给收尾留余地。
        soft_time_limit=120,
        time_limit=180,
    )(sync_task(audio_cleanup, TASK_NAME))


__all__ = [
    "BEAT_ENTRIES",
    "JOB_NAME",
    "TASK_NAME",
    "audio_cleanup",
    "register",
]
