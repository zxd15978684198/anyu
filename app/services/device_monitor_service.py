"""设备掉线扫描的编排实现（FR-ADM-10 后半：心跳超时 → 派单 + 通知管理员与护工）。

从 `app/scheduler/devices.py` 原样下沉而来（批 3 前置）：**判断条件、去重键、收件人
逻辑一字未改**，只换了归属层与事务入口——调度器薄壳（将来是 Celery 任务体）负责
建 session、调本函数、commit 与记日志；本模块只 add / flush，**绝不 commit**。

一轮扫描做三件事，缺一件都算没闭环：

1. **判掉线**：`devices.last_heartbeat` 早于 `now - OFFLINE_THRESHOLD_MINUTES`；
2. **落工单**：`type='device_fault'`、`severity='yellow'`，指派主责护工（没有主责就留在
   未指派队列里等管理员认领）；
3. **发通知**：给主责护工一条、给每个在职管理员一条——PRD 的「通知管理员与护工」是
   两种人两种待办，只发一条给护工，管理员那一侧的「设备侧异常」就没人看得见。

## 为什么判据只有一条 `last_heartbeat`，不看 `devices.status`

`devices.status` 是**人为标定**（`online/offline/locked`），心跳是**设备自证**。掉线是
「设备不再自证」这件事，只有时间戳能回答；拿 `status` 判会把「上次被人手动标 online、
其实早就断了」的设备一直当在线——这正是 FR-ADM-10 要抓的那类静默故障。反过来，
`status='locked'` 是管理员主动锁定（停用/维修），锁定期间的心跳缺失是预期内的，跳过。

## 阈值为什么取 5 分钟（PRD 说「超 2 个周期」）

心跳写入的最小间隔是 `device_service.HEARTBEAT_MIN_INTERVAL_SECONDS = 60` 秒，所以
「2 个周期」在最密的情况下是 2 分钟。取 5 分钟 = 5 个周期，是**偏保守**的一侧：
多等 3 分钟只是晚一点发现（扫描间隔 60 秒，最坏 6 分钟内出单），而阈值压到 2 分钟
会把「网络抖一下、两次心跳眨眼间挤在一起」的设备误判成掉线——误判一次就要派人上门，
代价远大于晚 3 分钟。数值写在本模块常量里，改口径只改这一处。

## 幂等：按「该设备未关单的 device_fault」去重，落在长者维度

`work_orders` 没有 `device_id` 列，而设备:长者 1:1（`uq_devices_elder_id` 部分唯一索引），
所以「这台设备还有没有挂着没关的掉线单」等价于「这位长者名下有没有 `device_fault` 且
状态在 `ACTIVE_STATUSES` 里的单」——用 `work_order_service.find_open_work_order()` 查。
这样调度器重启、`--reload` 误触发、手工补跑都不会给同一次掉线刷出第二张单；护工把单
关掉之后设备仍然没心跳，下一轮才会重新派单（这是**新的一次**掉线，不是重复）。

## 长者不在住 / 设备未绑定：跳过，但计数留痕

- `Elder.status != 'active'`（`away/transferred/archived`）：人不在这个房间里（外出/转区/
  已归档），设备掉线不影响任何在住长者的求助通道。口径与 `app/scheduler/health.py`
  的 `_ELDER_IN_HOUSE` 一致——**不派单**，也不留半截数据。
- `devices.elder_id IS NULL`（已入库未绑定）：`work_orders.elder_id` 是 NOT NULL，建不出
  单；这类设备归设备管理，不属于掉线派单。只记数，不进日志噪声。

两种情况都只在 `OfflineScanStats` 的计数里出现，不新建任何行——宁可少派，也不生成
「无主工单」污染照护台队列。
"""

from __future__ import annotations

import dataclasses
import datetime
import logging
import uuid
from typing import Final
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, Elder, ElderStaffAssignment, StaffAccount, WorkOrder
from app.services import notification_service, work_order_service

logger = logging.getLogger(__name__)

#: 任务名（job id 用 `app.scheduler.JOB_PREFIX` 拼；Celery 迁移后沿用同名）。
OFFLINE_JOB_NAME: Final[str] = "device-offline-scan"

#: 扫描间隔：60 秒。心跳最小间隔也是 60 秒，等于「每秒都上一轮」，最坏晚 1 分钟发现。
#: 只被调度器注册函数读取，放这里是为了让「阈值 / 间隔」两个业务口径挨着。
OFFLINE_SCAN_INTERVAL_SECONDS: Final[int] = 60

#: 判掉线的心跳静默阈值（分钟）。理由见模块 docstring「阈值为什么取 5 分钟」。
OFFLINE_THRESHOLD_MINUTES: Final[int] = 5

#: 工单类型：`work_orders.type` 的 CHECK 已含 `device_fault`（无需迁移）。
OFFLINE_ORDER_TYPE: Final[str] = "device_fault"

#: 严重度：掉线不是险情（险情走 `safety_alert` 红单），但必须有人上门看一眼 → 黄档。
OFFLINE_ORDER_SEVERITY: Final[str] = "yellow"

#: 通知复用 `safety_alert` kind：`notifications.kind` 的 CHECK 只有五个值（0012 迁移），
#: 加第六个要新迁移（本次任务不加）。`safety_alert` 在其中语义最贴——「必须人工介入的
#: 设备/安全异常」，读的人靠 `payload.module == "devices"` 区分来源。
OFFLINE_NOTIFICATION_KIND: Final[str] = "safety_alert"

#: `payload.module`：与 `alert_service.PAYLOAD_MODULE="safety"`、
#: `risk_event_service` 的 `"risk_event_escalation"` 同一套区分手法。
OFFLINE_PAYLOAD_MODULE: Final[str] = "devices"

#: 在住状态：只有 `active` 的长者才派单（away/transferred/archived 一律跳过）。
_ELDER_IN_HOUSE: Final[str] = "active"

#: 设备状态：管理员主动锁定期间不判掉线（锁定 = 预期内不心跳）。
_DEVICE_LOCKED: Final[str] = "locked"

#: 业务日口径（与 `app/scheduler/medication.py` 的 `_business_date` 一致）：去重键里的
#: 「业务日」按上海本地日历算，不按 UTC 算——凌晨 1 点的掉线属于前一天是错的。
_BUSINESS_TZ: Final[ZoneInfo] = ZoneInfo("Asia/Shanghai")


@dataclasses.dataclass(slots=True)
class OfflineScanStats:
    """一轮扫描的结果统计：返回值给调用方记日志/计数，不落库。

    字段名与迁移前 `run` 函数里那组日志 `extra` 逐字对应，方便日志看板对比迁移前后。
    """

    created: int = 0
    """本轮新建的掉线工单数（即旧 `scan_offline_devices()` 的返回值）。"""

    device_total: int = 0
    """参与判定的设备数（已 join 上长者，含锁定/未激活/新鲜/不在住的）。"""

    unbound_device_count: int = 0
    """`elder_id IS NULL` 的已入库未绑定设备数（只统计不派单）。"""

    locked_offline_count: int = 0
    """管理员锁定且心跳已过期的设备数（只统计不派单）。"""

    elder_away_count: int = 0
    """心跳过期但长者不在住（away/transferred/archived）的设备数。"""

    open_order_count: int = 0
    """心跳过期但已有未关单的设备数（同一场故障，未重复派单）。"""


def _business_date(moment: datetime.datetime) -> datetime.date:
    """一次掉线归属的业务日（上海本地日历）。"""
    return moment.astimezone(_BUSINESS_TZ).date()


async def _dispatch_sequence(
    session: AsyncSession, *, elder_id: uuid.UUID, order_type: str, business_date: datetime.date
) -> int:
    """该长者当日第几张同类单（含已关闭的），用于让「关单后当天再掉线」拿到新键。

    去重键如果只带 `(device_id, 业务日)`，一次掉线被关单之后当天再掉线的第二张单会和
    第一张撞唯一索引，`create_work_order` 只能把**已关闭的旧单**返回给调用方——那就成了
    「故障复发了却没人看到」。带上当日序号，同一次故障重放仍然是同一个键（并发第二轮
    算出来也是 1，被索引挡住），而真正的新故障拿到 seq=2。
    """
    day_start = datetime.datetime.combine(business_date, datetime.time.min, tzinfo=_BUSINESS_TZ)
    total = (
        await session.execute(
            select(func.count())
            .select_from(WorkOrder)
            .where(
                WorkOrder.elder_id == elder_id,
                WorkOrder.type == order_type,
                WorkOrder.created_at >= day_start,
            )
        )
    ).scalar_one()
    return int(total) + 1


def _as_utc(now: datetime.datetime | None) -> datetime.datetime:
    """把注入的时钟归一到 UTC；不注入就是此刻（naive 按 UTC 解释，与存储口径一致）。"""
    if now is None:
        return datetime.datetime.now(datetime.UTC)
    return now if now.tzinfo is not None else now.replace(tzinfo=datetime.UTC)


async def _primary_care_staff(session: AsyncSession, elder_id: uuid.UUID) -> uuid.UUID | None:
    """该长者的主责护工（`elder_staff_assignments.relation='primary_care'`）。"""
    return (
        await session.execute(
            select(ElderStaffAssignment.staff_id)
            .where(
                ElderStaffAssignment.elder_id == elder_id,
                ElderStaffAssignment.relation == "primary_care",
            )
            .limit(1)
        )
    ).scalar_one_or_none()


async def _active_admin_ids(session: AsyncSession) -> list[uuid.UUID]:
    """全部在职管理员（停用账号不该再被叫起来看设备，`status='disabled'` 排除）。"""
    rows = (
        await session.execute(
            select(StaffAccount.staff_id)
            .where(StaffAccount.role == "admin", StaffAccount.status == "active")
            .order_by(StaffAccount.staff_id)
        )
    ).scalars()
    return list(rows.all())


async def _uncountable_device_ids(
    session: AsyncSession, *, cutoff: datetime.datetime
) -> tuple[int, int]:
    """只读统计：`(未绑定长者的设备数, 已锁定且心跳过期的设备数)`——只进统计，不落库。"""
    unbound = (
        await session.execute(select(Device.device_id).where(Device.elder_id.is_(None)).limit(1000))
    ).scalars()
    locked = (
        await session.execute(
            select(Device.device_id)
            .where(
                Device.status == _DEVICE_LOCKED,
                Device.last_heartbeat.is_not(None),
                Device.last_heartbeat < cutoff,
            )
            .limit(1000)
        )
    ).scalars()
    return len(list(unbound.all())), len(list(locked.all()))


def _notification_payload(
    *,
    device: Device,
    order_no: str,
    offline_minutes: int,
    last_heartbeat: datetime.datetime,
    staff_id: uuid.UUID | None,
) -> dict[str, object]:
    """通知体：给照护台看「哪台设备、停在哪个房间、多久没心跳、派给了哪张单」。

    只放 `device_id` / `room_id` / 时长 / 单号，不放长者姓名、不放设备令牌——通知是给人
    看的摘要，不是把设备档案整份抄进 `payload`（AGENTS §4-10 的脱敏口径）。
    """
    return {
        "module": OFFLINE_PAYLOAD_MODULE,
        "state": "offline",
        "device_id": str(device.device_id),
        "room_id": str(device.room_id),
        "last_heartbeat": last_heartbeat.isoformat(),
        "offline_minutes": offline_minutes,
        "threshold_minutes": OFFLINE_THRESHOLD_MINUTES,
        "order_no": order_no,
        "assignee_staff_id": str(staff_id) if staff_id is not None else None,
        "summary": f"设备心跳已停 {offline_minutes} 分钟，已派 device_fault 工单 {order_no}",
    }


async def scan_offline_devices(
    session: AsyncSession, *, now: datetime.datetime | None = None
) -> OfflineScanStats:
    """扫一轮掉线设备：给每台「心跳过期 + 未派单」的在住设备建单并发通知。

    `now` 可注入是为了让用例把时钟拨过阈值，而不是 `sleep(300)` 等真时间
    （阈值是业务口径，不是让测试跑五分钟）。

    **事务边界**：只 add / flush，不 commit，也不吞异常——提交与失败处理归调用方
    （`app/scheduler/devices.py` 的薄壳，将来是 Celery 任务体）。
    """
    moment = _as_utc(now)
    cutoff = moment - datetime.timedelta(minutes=OFFLINE_THRESHOLD_MINUTES)
    stats = OfflineScanStats()

    stats.unbound_device_count, stats.locked_offline_count = await _uncountable_device_ids(
        session, cutoff=cutoff
    )
    rows = (
        await session.execute(
            select(Device, Elder.status)
            .join(Elder, Elder.elder_id == Device.elder_id)
            .where(Device.elder_id.is_not(None))
            .order_by(Device.device_id)
        )
    ).all()

    for device, elder_status in rows:
        stats.device_total += 1
        if device.status == _DEVICE_LOCKED:
            # 主动锁定期间不判掉线（理由见模块 docstring）。
            continue
        if device.last_heartbeat is None:
            # 从未上报过心跳：这是「未激活」，不是「掉线」，不派单。
            continue
        if device.last_heartbeat >= cutoff:
            continue
        if elder_status != _ELDER_IN_HOUSE:
            # 人不在这个房间（外出/转区/已归档）：掉线不影响在住长者的求助通道。
            stats.elder_away_count += 1
            continue
        if device.elder_id is None:  # pragma: no cover - 上面的 join 已排除
            continue
        existing = await work_order_service.find_open_work_order(
            session, elder_id=device.elder_id, type=OFFLINE_ORDER_TYPE
        )
        if existing is not None:
            # 这台设备的掉线单还挂着没关：同一场故障，不重复派单。
            stats.open_order_count += 1
            continue

        offline_minutes = max(0, int((moment - device.last_heartbeat).total_seconds() // 60))
        staff_id = await _primary_care_staff(session, device.elder_id)
        # 去重键 = (设备, 业务日, 当日序号)（FR-ALT-04 计划 §2.3 的「掉线单」那一档）。
        # 序号只为放行「关单后当天再掉线」，见 `_dispatch_sequence` 的说明。
        business_date = _business_date(moment)
        seq = await _dispatch_sequence(
            session,
            elder_id=device.elder_id,
            order_type=OFFLINE_ORDER_TYPE,
            business_date=business_date,
        )
        dedupe_key = f"device_offline:{device.device_id}:{business_date.isoformat()}:{seq}"
        order = await work_order_service.create_work_order(
            session,
            elder_id=device.elder_id,
            type=OFFLINE_ORDER_TYPE,
            severity=OFFLINE_ORDER_SEVERITY,
            note=(
                f"设备 {device.device_id} 心跳已停 {offline_minutes} 分钟"
                f"（阈值 {OFFLINE_THRESHOLD_MINUTES} 分钟），"
                "长者端语音通道不可用；若与用药/健康采集窗口重叠需立即上门人工介入"
            ),
            assignee_staff_id=staff_id,
            dedupe_key=dedupe_key,
        )
        payload = _notification_payload(
            device=device,
            order_no=order.order_no,
            offline_minutes=offline_minutes,
            last_heartbeat=device.last_heartbeat,
            staff_id=staff_id,
        )
        # 护工那条是「派单」凭据；管理员那条是「设备侧异常」的值班视角，
        # 两者各自一条（定向行在待办池里归属清楚，见 risk_event_service 的同款理由）。
        # 收件人进键：`(elder_id, kind, dedupe_key)` 只差收件人就会把管理员那条
        # 吃成护工那条的重复，反而少一个待办。
        if staff_id is not None:
            await notification_service.create_notification(
                session,
                kind=OFFLINE_NOTIFICATION_KIND,
                severity=OFFLINE_ORDER_SEVERITY,
                elder_id=device.elder_id,
                payload=payload,
                assignee_staff_id=staff_id,
                dedupe_key=f"{dedupe_key}:staff:{staff_id}",
            )
        admin_ids = await _active_admin_ids(session)
        for admin_id in admin_ids:
            await notification_service.create_notification(
                session,
                kind=OFFLINE_NOTIFICATION_KIND,
                severity=OFFLINE_ORDER_SEVERITY,
                elder_id=device.elder_id,
                payload=payload,
                assignee_staff_id=admin_id,
                dedupe_key=f"{dedupe_key}:admin:{admin_id}",
            )
        if staff_id is None:
            logger.warning(
                "device offline without primary care staff",
                extra={
                    "job": OFFLINE_JOB_NAME,
                    "device_id": str(device.device_id),
                    "reason": "no_primary_care",
                },
            )
        if not admin_ids:
            logger.warning(
                "device offline without admin",
                extra={
                    "job": OFFLINE_JOB_NAME,
                    "device_id": str(device.device_id),
                    "reason": "no_active_admin",
                },
            )
        stats.created += 1

    return stats


__all__ = [
    "OFFLINE_JOB_NAME",
    "OFFLINE_NOTIFICATION_KIND",
    "OFFLINE_ORDER_SEVERITY",
    "OFFLINE_ORDER_TYPE",
    "OFFLINE_PAYLOAD_MODULE",
    "OFFLINE_SCAN_INTERVAL_SECONDS",
    "OFFLINE_THRESHOLD_MINUTES",
    "OfflineScanStats",
    "scan_offline_devices",
]
