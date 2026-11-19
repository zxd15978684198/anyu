"""APScheduler 装配：**只构造，不启动**。

启动/关闭属于进程生命周期，统一在 `app/main.py` 的 lifespan 里做（S-D8-6）；
本模块只提供 `build_scheduler()` 工厂，让装配点保持可测：

```python
scheduler = build_scheduler()
scheduler.start()      # main.py 的 lifespan
...
scheduler.shutdown(wait=False)
```

## 为什么三个时刻都从 Settings 取

19:00 / 20:00 / 07:00 是 PRD 硬性数值（PRD §4.5-4.6、FR-RPT-01），但联调时常要
手动触发，所以口径留在 `app/core/config.py`（`health_checkup_start`、
`health_checkup_end`、`daily_report_generate_hour`），代码里不再出现字面量时间。

## `scheduler_enabled=False` 时返回什么

返回一个**没有任何任务**的 scheduler：调用方即使误 start 也不会双进程各跑一遍
（`uvicorn --reload` 的 reloader 与 worker 都会 import 本模块）。

## 用药触达通道为什么从外面递进来

用药触达要真的开口（TTS/外呼），通道在 `app/audio/medication_delivery.py`；而
`scheduler → audio` 是横向 import（AGENTS §2.1 禁止）。因此本模块只声明参数
`medication_deliver: Deliver | None`，由装配根 `app/main.py` 构造并递入——通道的
**生命周期**归进程装配（lifespan），本模块只负责把它送进任务。

## 采集会话驱动为什么也从外面递进来

同一件事的另一侧：起一场五问采集要拉起 `app/graphs/health_checkup_graph.py` 的图，
`scheduler → graphs` 同样是横向 import。本模块声明 `health_checkup_driver`，
由装配根用图拼出实现后递入；`None` 时只落提醒通知、不起会话，扫描空转。

`health_checkup_start`（默认 19:00）自 FR-HLT-05 起**不再是单点 cron**：长者可自定义
采集时刻（`elders.checkup_start`，NULL 回落该配置），由 `app/scheduler/health.py`
的 5 分钟窗口扫描 `run_health_checkup_due` 命中。旧的 `run_health_checkup_start`
保留为「立即起一场」的实现，但**不再注册**——两套并存会让默认群体一天采两遍。

用药与维护任务由 迭代 7 追加（`app/scheduler/medication.py`、
`app/scheduler/maintenance.py`），通知升级任务由 N30 追加
（`app/scheduler/notifications.py`），红色事件超时升级由 AC-12 追加
（`app/scheduler/risks.py`），与上面三个日任务共用同一个 `JOB_PREFIX` 与
`coalesce / max_instances / misfire_grace_time` 口径；`scheduler_enabled=False`
时同样一个都不注册。

## 迁到 Celery 的 job 怎么避免双触发

`CELERY_ENABLED=true` 时，本模块在注册完成后把 `CELERY_OWNED_JOBS` 里的 job 摘掉
（按 `job.name` 匹配、`remove_job`）。同一份代码因此在两种形状下都能跑：

| `celery_enabled` | 那 6 条离线批处理 | 用药 / 采集 / 记忆 / 关注 |
| --- | --- | --- |
| `false`（默认） | APScheduler | APScheduler |
| `true` | celery beat | APScheduler（本模块保留） |

不把 `if` 散布到六个 `register_*` 里：那是六处分支、六次改测试；集中在装配末尾摘除，
开关只有一处，回滚也只是改一个环境变量。
"""

from __future__ import annotations

import logging
from typing import Final
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.config import get_settings
from app.scheduler.attention import register_attention_jobs
from app.scheduler.devices import register_device_jobs
from app.scheduler.health import (
    HealthCheckupDriver,
    register_health_checkup_jobs,
    run_missed_scan,
)
from app.scheduler.maintenance import register_maintenance_jobs
from app.scheduler.medication import Deliver, register_medication_jobs
from app.scheduler.memory import register_memory_jobs
from app.scheduler.notifications import register_notification_jobs
from app.scheduler.reports import (
    register_daily_report_delivery_job,
    run_daily_report_generation,
)
from app.scheduler.risks import register_risk_event_jobs

logger = logging.getLogger(__name__)

#: 任务 id 前缀：日志/排障时能一眼看出是哪个任务触发的。
JOB_PREFIX = "anyu"

#: 已迁到 Celery 的 job 名（与 `app/tasks/jobs/*.py` 的 BEAT_ENTRIES 键一致）。
#: `CELERY_ENABLED=true` 时不再注册，避免 APScheduler 与 beat 双触发。
#: 用药四枪、19:00 采集驱动、**20 秒采集超时扫描**、记忆三件套、09:00 关注扫描
#: **不在此列**——它们要复用本进程的 WS/WebRTC 连接、采集驱动对象与巡检语义（计划 §2.1）。
#: 采集超时扫描尤其如此：驱动是 lifespan 里的内存对象，worker 里拿不到，迁过去只会
#: 变成一个每 20 秒空转、永不推进的假任务。
CELERY_OWNED_JOBS: Final[frozenset[str]] = frozenset(
    {
        "daily-report-generate",
        "daily-report-delivery",
        "notification-escalation",
        "risk-event-escalation",
        "device-offline-scan",
        "audio-cleanup",
    }
)


def _drop_celery_owned_jobs(scheduler: AsyncIOScheduler, *, enabled: bool) -> list[str]:
    """把已交给 Celery 的 job 从本调度器摘掉，返回被摘掉的 job 名；`enabled=False` 时不动。

    抽成函数是为了可测：`build_scheduler()` 要读 Settings，这里只依赖入参。
    """
    if not enabled:
        return []
    removed: list[str] = []
    for job in list(scheduler.get_jobs()):
        if job.name in CELERY_OWNED_JOBS:
            scheduler.remove_job(job.id)
            removed.append(job.name)
    if removed:
        logger.info(
            "scheduler jobs handed over to celery",
            extra={"removed": sorted(removed), "count": len(removed)},
        )
    return removed


def _clock_parts(clock: str) -> tuple[int, int]:
    """解析 `"19:00"` 形态的时刻；解析不了就抛 ValueError（配置错误要早炸）。"""
    hour_text, _, minute_text = clock.partition(":")
    hour = int(hour_text)
    minute = int(minute_text or "0")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"invalid clock value {clock!r}, expect HH:MM")
    return hour, minute


def build_scheduler(
    *,
    medication_deliver: Deliver | None = None,
    health_checkup_driver: HealthCheckupDriver | None = None,
) -> AsyncIOScheduler:
    """构造（未启动）调度器并按配置注册三个日任务。

    `medication_deliver` 是用药触达的外呼通道（`app.scheduler.medication.Deliver`），
    由装配根 `app/main.py` 构造后传进来：`app/scheduler` 不许 import `app/audio`
    （AGENTS §2.1 禁止横向 import），所以通道只能从外面递进来，本模块只负责透传给
    `register_medication_jobs(deliver=...)`。`None` = 通道没装配，任务体退回
    `_default_deliver`（空实现，不伪造送达）。

    `health_checkup_driver` 是采集会话的驱动（`app.scheduler.health.HealthCheckupDriver`），
    同样由装配根注入——`app/scheduler` 不许 import `app/graphs`。`None` = 驱动没装配，
    19:00 只落提醒通知、不起会话，20 秒扫描空转（都不会假装采集在跑）。
    """
    settings = get_settings()
    scheduler = AsyncIOScheduler(timezone=ZoneInfo(settings.scheduler_timezone))

    if not settings.scheduler_enabled:
        # 只记一条，不注册任务：reload 场景下宁可没有任务，也不要双触发。
        logger.info(
            "scheduler disabled by settings",
            extra={"reason": "scheduler_enabled_false"},
        )
        return scheduler

    jobs = (
        ("health-missed-scan", settings.health_checkup_end, run_missed_scan),
        ("daily-report-generate", settings.daily_report_generate_hour, run_daily_report_generation),
    )
    for name, clock, func in jobs:
        hour, minute = _clock_parts(clock)
        scheduler.add_job(
            func,
            trigger="cron",
            hour=hour,
            minute=minute,
            id=f"{JOB_PREFIX}:{name}",
            name=name,
            replace_existing=True,
            coalesce=True,  # 错过多次只补跑一次
            max_instances=1,  # 上一轮没跑完不叠加
            misfire_grace_time=300,
        )
        logger.info(
            "scheduler job registered",
            extra={"job_id": f"{JOB_PREFIX}:{name}", "clock": f"{hour:02d}:{minute:02d}"},
        )

    # 迭代 7 追加：用药（事件预生成 + 四次触达）与维护（音频清理/记忆衰减/容量淘汰）。
    # 放在既有三个日任务之后注册，不动它们的任何参数（job id 与触发时刻保持原样）。
    # 用药触达通道从装配根递进来（本模块不 import `app/audio`）。
    register_medication_jobs(scheduler, deliver=medication_deliver)
    register_maintenance_jobs(scheduler)
    # N30 追加：通知 120s 未确认自动升级（interval 60s，无状态轮询）。
    register_notification_jobs(scheduler)
    # FR-RPT-03 追加：日报推送到点扫描（interval 60s）——重试簿记只有被轮询才会推进，
    # 没有这条 job，AC-25 的 1/5/15 自动重试在生产上不会发生。
    register_daily_report_delivery_job(scheduler)
    # AC-12 追加：红色 `risk_events` 120s 无人确认就地升 `escalated`（interval 60s）。
    register_risk_event_jobs(scheduler)
    # FR-HLT-01 追加：采集会话每问 60 秒的超时推进（interval 20s）。
    # FR-HLT-05 追加：采集时刻 5 分钟窗口扫描（interval 300s，按长者自定义时刻命中）。
    # 上面 `jobs` 里已没有 `health-checkup-start`：单点 19:00 cron 会让默认群体与窗口扫描各起一场。
    register_health_checkup_jobs(scheduler, driver=health_checkup_driver)
    # FR-ADM-10 追加：设备掉线扫描（interval 60s）——心跳过期 → device_fault 工单 + 通知。
    # 只能靠轮询发现：设备断了就再也不会把「我断了」告诉我，没有人主动推这条 job，
    # 「心跳超 2 个周期判掉线」在生产上不会发生。
    register_device_jobs(scheduler)
    # FR-HLT-10 追加：四信号「建议关注」每日 09:00 扫描（估 → 通知 → 跟进单）。
    # 放在 19:00 采集、08:00 用药复核之外：护工上班后先看关注列表，不与采集窗口抢库。
    register_attention_jobs(scheduler)
    # FR-MEM-07 追加：记忆每周归并（周一 03:30，`app/scheduler/memory.py`）。
    # 排在每日 03:00 衰减 / 03:10 容量淘汰之后：先降权、先淘汰，归并的保留者才选得准。
    register_memory_jobs(scheduler)

    # 最后一步：把已交给 Celery 的 job 摘掉（开关只在这里读一次）。
    _drop_celery_owned_jobs(scheduler, enabled=bool(getattr(settings, "celery_enabled", False)))

    return scheduler


__all__ = ["CELERY_OWNED_JOBS", "JOB_PREFIX", "build_scheduler"]
