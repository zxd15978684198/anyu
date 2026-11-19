"""批 3 · 设备掉线扫描的 Celery 任务体（FR-ADM-10 后半；计划 §2.1 的「可迁的一侧」）。

## 为什么设备扫描可以迁，用药与采集不行

设备掉线是**巡检语义**：判据只有 `devices.last_heartbeat` 这一条时间戳，结果对延迟不
敏感（APScheduler 版本身是 60 秒一跑，最坏晚 1 分钟发现；Celery 版照抄 60 秒，晚几秒
没有任何业务后果）。它对进程内状态零依赖：

- 不碰 `Deliver`（不播语音、不外呼）——那是 audio 侧对象，只在 backend 进程里有；
- 不碰 LangGraph 会话/checkpoint——不需要 `thread_id`，没有中断恢复；
- 幂等闸门在服务层（「该长者名下有没有未关的 `device_fault`」+ 去重键），重投/重跑
  只会被 `find_open_work_order()` 挡住，`task_acks_late=True`（会重）因此是安全的。

反过来，用药四枪与 19:00 采集留在 APScheduler（计划 §2.1）：用药要**复用进程内连接**
（`Deliver` 的 TTS/WebRTC 通道、会话上下文），采集要秒级推进 20:00 窗口——这两类跨进程
只能靠 Redis 事件重建，等于把「播一句提醒」改成一次状态同步，是计划明确不做的形态。

## 任务体只做三件事

建 session → 调 `app/services/device_monitor_service.scan_offline_devices()` → commit。
判断条件、去重键、收件人逻辑全在 service 里（与 APScheduler 薄壳
`app/scheduler/devices.py:run_device_offline_scan()` 共用同一份实现），调度层只负责
「到点把服务叫起来」——所以回滚等于删掉一条 beat entry，不涉及业务改动。

## 与 APScheduler 版的差异（只有这两处）

1. trace_id：worker 侧每轮自造一个（静态 beat 的 signature 固定，beat 无法按次注入
   trace 头，计划 §6 第 1 条）；日志里的 `trace_id` 不再恒为 `-`。
2. 失败处理：`logger.exception(...)` 后 **re-raise**——`acks_late` 靠非零退出码把消息
   回队重跑。但重跑安全的前提是服务层幂等，不是「反正会重」。

## 为什么本模块不 import `app.scheduler` / `app.tasks.celery_app`

- `app.scheduler` 会连锁装配 devices/health/medication/reports/… 全部模块，等于为了
  扫一次心跳把整个 APScheduler 装配（含图驱动、DB 引擎）拖进 worker；
- `app.tasks.celery_app` 会在导入期遍历 `app/tasks/jobs/*.py`，反向 import 就成环。

job 名从 service 常量取（`OFFLINE_JOB_NAME`），保证与 `app/scheduler/__init__.py` 的
`CELERY_OWNED_JOBS` 里那一条字面量一致——两边都是 `"device-offline-scan"`，改名只改
`device_monitor_service` 一处，再跟着改 `CELERY_OWNED_JOBS`。
"""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING, Any, Final

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.core.context import new_trace_id, reset_trace_id, set_trace_id
from app.db import session_factory
from app.services import device_monitor_service
from app.services.device_monitor_service import OFFLINE_JOB_NAME, OFFLINE_SCAN_INTERVAL_SECONDS
from app.tasks.jobs._runtime import sync_task

if TYPE_CHECKING:
    from celery import Celery

logger = logging.getLogger(__name__)

#: 任务短名；与 APScheduler 版的 job 名（`app.scheduler.JOB_PREFIX:device-offline-scan`）
#: 同名，便于日志/看板对照，也是 `CELERY_OWNED_JOBS` 里那一条。
JOB_NAME: Final[str] = OFFLINE_JOB_NAME

#: Celery 任务全路径名（`app.tasks.jobs.<模块>.<函数>` 口径，与骨架约定一致）。
TASK_NAME: Final[str] = "app.tasks.jobs.devices.device_offline_scan"

#: 静态 beat 表条目：由 `app/tasks/celery_app.py` 自动发现并合并。
#: 60 秒 = 与 APScheduler 版 `interval 60s` 逐秒对齐（`OFFLINE_SCAN_INTERVAL_SECONDS`）。
BEAT_ENTRIES: dict[str, dict[str, object]] = {
    JOB_NAME: {
        "task": TASK_NAME,
        "schedule": float(OFFLINE_SCAN_INTERVAL_SECONDS),
    },
}

#: 可恢复错误：网络/连接类。只有这些进 `autoretry_for`，其余错误重试无意义（计划 §5）。
RECOVERABLE_ERRORS: Final[tuple[type[BaseException], ...]] = (
    ConnectionError,
    TimeoutError,
    RedisConnectionError,
    RedisTimeoutError,
)

#: 任务级可靠性参数（计划 §5：soft < hard 留收尾余地；退避 + 抖动避免重试风暴）。
TASK_OPTIONS: Final[dict[str, Any]] = {
    "ignore_result": True,
    "autoretry_for": RECOVERABLE_ERRORS,
    "retry_backoff": True,
    "retry_jitter": True,
    "max_retries": 3,
    "soft_time_limit": 30,
    "time_limit": 60,
}


def _coerce_now(raw: object) -> datetime.datetime | None:
    """把测试/上层注入的 `now` 归一成 datetime：接受 datetime 或 ISO 8601 字符串。

    这是给用例的逃生口：判定时钟必须可注入，否则测「跨过 5 分钟阈值」要真等 5 分钟。
    """
    if raw is None:
        return None
    if isinstance(raw, datetime.datetime):
        return raw
    if isinstance(raw, str):
        return datetime.datetime.fromisoformat(raw)
    raise TypeError(f"payload['now'] 必须是 datetime 或 ISO 8601 字符串：{type(raw)!r}")


async def device_offline_scan(payload: dict[str, Any] | None = None) -> dict[str, int]:
    """异步任务体：跑一轮掉线扫描，返回 `OfflineScanStats` 的 dict 形态。

    `payload` 是可选注入点（全部键都可省略，无参调用等价于「现在跑一轮」）：

    - `"now"`：判定时钟（datetime 或 ISO 字符串），只供用例拨表；
    - `"trace_id"`：外部已有 trace 时沿用，否则本轮自造一个。

    返回字典而不是 dataclass：Celery 任务返回值和日志 `extra` 都只吃 JSON 友好的值，
    而本任务 `ignore_result=True`，这个返回值实际只给「同步调用/用例」用。
    """
    payload = payload or {}
    trace_id = payload.get("trace_id") or new_trace_id()
    moment = _coerce_now(payload.get("now"))
    token = set_trace_id(trace_id)
    try:
        async with session_factory() as session:
            stats = await device_monitor_service.scan_offline_devices(session, now=moment)
            await session.commit()
    except RECOVERABLE_ERRORS:
        logger.warning(
            "device offline scan task recoverable failure",
            extra={"job": JOB_NAME, "trace_id": trace_id},
        )
        raise
    except Exception:
        logger.exception(
            "device offline scan task failed",
            extra={"job": JOB_NAME, "trace_id": trace_id},
        )
        raise
    finally:
        reset_trace_id(token)

    result = {
        # 不叫 `created`：它是 LogRecord 的保留属性，`extra={"created": ...}` 会让
        # logging 抛 KeyError，整个任务每轮崩溃（批A 修）。
        "devices_created": stats.created,
        "device_total": stats.device_total,
        "unbound_device_count": stats.unbound_device_count,
        "locked_offline_count": stats.locked_offline_count,
        "elder_away_count": stats.elder_away_count,
        "open_order_count": stats.open_order_count,
    }
    logger.info(
        "device offline scan task done",
        extra={
            "job": JOB_NAME,
            "trace_id": trace_id,
            "threshold_minutes": device_monitor_service.OFFLINE_THRESHOLD_MINUTES,
            **result,
        },
    )
    return result


def register(celery_app: Celery) -> None:
    """把任务挂到骨架上（装配期出错要早暴露，故不吞异常）。"""
    celery_app.task(name=TASK_NAME, **TASK_OPTIONS)(sync_task(device_offline_scan, TASK_NAME))
    logger.info("celery device scan task registered", extra={"task": TASK_NAME})


__all__ = [
    "BEAT_ENTRIES",
    "JOB_NAME",
    "RECOVERABLE_ERRORS",
    "TASK_NAME",
    "TASK_OPTIONS",
    "device_offline_scan",
    "register",
]
