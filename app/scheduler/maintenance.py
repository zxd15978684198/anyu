"""维护类定时任务：72h 原始音频清理 + 记忆衰减 + 记忆容量淘汰（迭代 7 追加）。

## 三个任务与理由

| job id | cron（`settings.scheduler_timezone`，默认 `Asia/Shanghai`） | 依据 |
| --- | --- | --- |
| `anyu:audio-cleanup` | 每小时 `minute=0` | 技术设计文档 §1.2 第 8 条 / PRD §1.5、FR-TC-07：原始音频最长保留 **72 小时**（`settings.audio_retention_hours`），超时自动删除 |
| `anyu:memory-decay` | 每天 `03:00` | PRD FR-MEM-07：长期记忆 90 天未被访问 → 重要性衰减（`memory_service.DECAY_DAYS` / `DECAY_FACTOR`） |
| `anyu:memory-capacity` | 每天 `03:10` | 技术设计文档记忆容量口径：每长者活跃记忆上限（`memory_service.CAPACITY_PER_ELDER`）超出部分软删 |

为什么错开 10 分钟：衰减会改 `importance`，容量淘汰按「超限条数」软删；两者跑在
同一时刻会互相看到中间状态，错开让每一步的结果可复现、日志可对账。

## 扫哪些目录（2026-11-13 起是两个根）

| 根 | 来源 | 现状 |
| --- | --- | --- |
| `AUDIO_SPOOL_DIR` | 约定 spool 目录（系统临时目录下） | `settings.audio_debug_save` 默认关闭，目前是空的 |
| `report_service.REPORT_AUDIO_ROOT` | 日报语音 `data/report_audio/<elder_id>/<date>.wav` | **真实落盘**，此前从不清理，等于永久留存原始音频 |

第二个根是补漏（攻防检验 §4.6-S4 / B4-07）：日报 WAV 是原始音频，同样受技术设计文档
§1.2 第 8 条的 72h 约束。两个根都按同一条 `cutoff` 与同一份后缀白名单判定，不另造口径。

两者都可能「扫一遍 → 0 个文件」。为了让「任务确实跑过」这条事实可观测，空转也必须打点：

- `audio_cleanup_runs_total`：每轮 +1（证明 cron 在跳）；
- `audio_cleanup_files_total`：本轮删除数，**空转时也要写 0**（没这个点就分不清
  「跑过但没文件」和「压根没跑」）。

## 失败不外抛

调度器里一个任务抛异常会让 APScheduler 记 error 并影响下一次触发的心跳；维护任务
失败**不是**长者侧的紧急事件，因此一律 `logger.exception` / `logger.warning` 后吞掉，
返回 0 或已处理计数。真正需要叫人的是依赖它的业务：漏服、红标、采集中断。

## 分层（AGENTS §2）

只调 `app/services/memory_service.py`；容量淘汰要遍历长者，因此自己发一条
`select(Elder.elder_id)`（读口径，与 `app/scheduler/reports.py` 同款），写库仍只经服务层。
"""

from __future__ import annotations

import datetime
import logging
import time
from pathlib import Path
from typing import Final

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from app.core.config import get_settings
from app.core.metrics import counter, observe_ms
from app.db import session_factory as db_session_factory
from app.models import Elder
from app.services import audio_retention_service, memory_service

logger = logging.getLogger(__name__)

#: 原始音频的约定落盘目录（W4 音频层落地前的临时契约）。
#:
#: 配置里只有 `audio_debug_save`（开关）与 `audio_retention_hours`（保留时长），**没有**
#: 目录项；这里用「系统临时目录 / anyu / audio-spool」这一条约定路径，
#: 等 `app/audio/**` 接入时把同一常量换成配置项即可（`run_audio_cleanup(root=...)`
#: 已经留好注入点，测试与将来的配置都走它）。
#:
#: 常量与实现已搬进 `app/services/audio_retention_service.py`（批 3：Celery worker
#: 也要跑同一套清理，而 worker import `app/scheduler/**` 会把整个调度装配拖进来）；
#: 这里保留同名 import，因为既有用例是 `monkeypatch.setattr(maintenance_jobs,
#: "AUDIO_SPOOL_DIR", ...)` 换掉本模块属性、再由 `_cleanup_dirs` 读自己的名字。
AUDIO_SPOOL_DIR: Final[Path] = audio_retention_service.AUDIO_SPOOL_DIR

#: 只删这些后缀：共享临时目录里可能有别人的文件，白名单比黑名单安全。
AUDIO_SUFFIXES: Final[frozenset[str]] = audio_retention_service.AUDIO_SUFFIXES


def _report_audio_dir() -> Path:
    """日报语音的落盘根（实现见 `audio_retention_service.report_audio_dir`）。"""
    return audio_retention_service.report_audio_dir()


def _cleanup_dirs(root: Path | str | None) -> tuple[Path, ...]:
    """本轮要扫的根。

    `root` 是测试/临时用的单目录注入点：**给了就只扫它**，不叠加默认根——否则测试
    （`root=tmp_path`）会顺带扫到仓库里真实的 `data/report_audio/`。不给才扫两个默认根。

    刻意读本模块的 `AUDIO_SPOOL_DIR`（而不是直接调 service 的 `cleanup_dirs()`）：
    上面那个常量是既有用例 monkeypatch 的落点。
    """
    if root is not None:
        return (Path(root),)
    return (AUDIO_SPOOL_DIR, _report_audio_dir())


_AUDIO_CLEANUP_JOB: Final[str] = "audio-cleanup"
_MEMORY_DECAY_JOB: Final[str] = "memory-decay"
_MEMORY_CAPACITY_JOB: Final[str] = "memory-capacity"

_ELDER_IN_HOUSE: Final[str] = "active"
"""在住长者口径（与 `app/scheduler/health.py` / `reports.py` 一致）。"""


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


def register_maintenance_jobs(scheduler: AsyncIOScheduler) -> None:
    """挂上三个维护任务（`JOB_PREFIX` 函数内 import，理由见 `medication.py` 同名注释）。

    三个任务都带 `coalesce=True, max_instances=1, misfire_grace_time=300`：进程停机后
    只补跑一次，不按错过次数连发；维护任务重复跑也没有副作用（衰减按 `last_accessed`
    判定、淘汰按上限软删、清理按 mtime 删）。
    """
    from app.scheduler import JOB_PREFIX

    jobs: tuple[tuple[str, object, dict[str, int]], ...] = (
        (_AUDIO_CLEANUP_JOB, run_audio_cleanup, {"minute": 0}),
        (_MEMORY_DECAY_JOB, run_memory_decay, {"hour": 3, "minute": 0}),
        (_MEMORY_CAPACITY_JOB, run_memory_capacity_eviction, {"hour": 3, "minute": 10}),
    )
    for name, func, fields in jobs:
        scheduler.add_job(
            func,
            trigger="cron",
            id=f"{JOB_PREFIX}:{name}",
            name=name,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=300,
            **fields,
        )
    logger.info("maintenance jobs registered count=%s", len(jobs))


# ---------------------------------------------------------------------------
# 任务体 1：72h 原始音频清理
# ---------------------------------------------------------------------------


def _purge_stale_audio(directory: Path, cutoff: datetime.datetime) -> int:
    """同步清理：删 `mtime < cutoff` 的白名单音频，返回删除条数。

    实现已搬到 `app/services/audio_retention_service.purge_stale_audio`（批 3：Celery
    worker 要跑同一套清理，而 worker 不能靠 import 本模块把整个调度装配拖进去）；
    这里只留同名转发，避免既有调用点与用例的 import 路径失效。
    """
    return audio_retention_service.purge_stale_audio(directory, cutoff)


async def run_audio_cleanup(
    *, now: datetime.datetime | None = None, root: Path | str | None = None
) -> int:
    """删除超过 `audio_retention_hours`（默认 72h）的原始音频；返回删除条数。

    - 默认扫两个根：spool 目录 + 日报语音目录（见模块文档）。传 `root` 则只扫那一个，
      供测试与临时目录使用；
    - 超时口径以文件 `mtime` 为准（落盘即开始计时，重命名不影响）；
    - 目录不存在/为空 → 空转，仍然打 `audio_cleanup_runs_total` 与
      `audio_cleanup_files_total=0`（见模块文档：区分「跑过但没文件」与「没跑」）；
    - 某个根抛异常只影响该根，其它根继续跑；返回已删总数——维护任务失败不许把调度器带崩。
    """
    current = _as_utc(now)
    settings = get_settings()
    cutoff = current - datetime.timedelta(hours=settings.audio_retention_hours)
    directories = _cleanup_dirs(root)
    return await audio_retention_service.purge_expired_audio(
        directories,
        cutoff=cutoff,
        log=logger,
    )


# ---------------------------------------------------------------------------
# 任务体 2：记忆衰减
# ---------------------------------------------------------------------------


async def run_memory_decay(*, now: datetime.datetime | None = None) -> int:
    """全库跑一遍 90 天记忆衰减（`memory_service.decay`）；返回受影响行数。

    `decay` 的 `elder_id=None` 就是「全库一遍」——维护任务不该按长者循环，
    一条 UPDATE 覆盖全部，省掉 N 次往返。
    """
    current = _as_utc(now)
    started = time.perf_counter()
    affected = 0

    try:
        async with db_session_factory() as session:
            affected = await memory_service.decay(session)
            await session.commit()
    except Exception:
        logger.exception("记忆衰减失败 now=%s", current.isoformat())
        affected = 0
    finally:
        counter("memory_decay_runs_total")
        counter("memory_decay_rows_total", value=affected)
        observe_ms("memory_decay_ms", (time.perf_counter() - started) * 1000.0)

    logger.info("记忆衰减完成 affected=%s now=%s", affected, current.isoformat())
    return affected


# ---------------------------------------------------------------------------
# 任务体 3：记忆容量淘汰
# ---------------------------------------------------------------------------


async def run_memory_capacity_eviction(*, now: datetime.datetime | None = None) -> int:
    """遍历在住长者，把超出 `CAPACITY_PER_ELDER` 的活跃记忆软删；返回淘汰条数。

    单个长者失败（例如并发更新冲突）只警告并回滚到干净事务边界，继续下一位——
    一位长者的库错误不该让其他人的容量治理整晚不执行。
    """
    current = _as_utc(now)
    started = time.perf_counter()
    evicted = 0
    elders_done = 0

    try:
        async with db_session_factory() as session:
            elder_ids = list(
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
            for elder_id in elder_ids:
                try:
                    evicted += await memory_service.enforce_capacity(session, elder_id=elder_id)
                except Exception:
                    logger.warning("记忆容量淘汰失败 elder_id=%s", elder_id, exc_info=True)
                    await session.rollback()
                    continue
                elders_done += 1
            await session.commit()
    except Exception:
        logger.exception("记忆容量淘汰整轮失败 now=%s", current.isoformat())
    finally:
        counter("memory_capacity_runs_total")
        counter("memory_capacity_evicted_total", value=evicted)
        observe_ms("memory_capacity_ms", (time.perf_counter() - started) * 1000.0)

    logger.info(
        "记忆容量淘汰完成 elders=%s evicted=%s now=%s",
        elders_done,
        evicted,
        current.isoformat(),
    )
    return evicted


__all__ = [
    "AUDIO_SPOOL_DIR",
    "AUDIO_SUFFIXES",
    "register_maintenance_jobs",
    "run_audio_cleanup",
    "run_memory_capacity_eviction",
    "run_memory_decay",
]
