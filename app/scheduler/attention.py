"""四信号「建议关注」每日扫描（FR-HLT-10）：评估 → 落通知 → 派单。

一轮扫描做三件事，缺一件都算没闭环：

1. **评估**：对每位在住长者调 `alert_aggregation.evaluate_attention_signals`
   （情绪持续低落 / 异常沉默 / 深夜交互 / 同一类问题反复出现四条，阈值与算法不在本模块）；
2. **落库**：每个**命中信号**建一条 `kind='attention_signal'` 的通知，`severity='yellow'`
   （「建议关注」不是险情，红档留给安全告警与漏服升级）；
3. **派单**：同一位长者的主责护工（`elder_staff_assignment.relation='primary_care'`）
   拿一条 `type='follow_up'` 的黄档工单——「有人去看一眼」比「通知躺在列表里」更强。

## 为什么用 `notifications` + `work_orders`，不新建表

技术设计文档的 30 张业务表里没有「关注信号」表，本轮口径（计划 §3 W4）明确不新建表：
四信号是**结论**，不是新实体，落成「一条通知 + 一张跟进单」正好复用既有的待办链路与
状态机；新表只会多出一套没人看的生命周期。

## 幂等：按「长者 + 信号码 + 业务日」去重，`created=0` 表示今天已经报过

判据是通知行的 `(kind, elder_id, payload.record_date, payload.code)`：同一个信号在同一个
业务日只建一条，重跑（手工补跑、`--reload` 误触发、扫描重试）拿到 `created=0`。
业务日按 **Asia/Shanghai** 切（人理解的「今天」），落库仍是 UTC（AGENTS §8）。

这里**不**复用 `notification_service.find_open_daily_notification`：那个闸门按
`(kind, elder_id, record_date)` 粒度去重，一位长者一天可能有 2~3 条不同信号，
用它会把第二条起全部误杀；本模块按 `payload.code` 再细分一格。

## 文案红线

`evaluate_attention_signals` 建信号时已逐条过 `assert_attention_wording`
（`app/services/alert_aggregation.py`），本模块拿到的 `title/detail/suggestion` 是
**已经过闸门**的文本；工单 `note` 是这三段拼起来的，写入前**再过一次**闸门——拼装本身
可能引入新的组合（例如把「情绪」与「反复问」拼在一句里），闸门放在出口最保险。
任何诊断口径都会当场抛 `AttentionWordingError`，不写库、不派单。

## 没有主责护工：落通知 + `logger.warning`，不静默丢

主责护工是派单的受理人；没有它，工单就没有 `assignee_id`，会掉进「未指派队列」——
这与「设备掉线」那条链路的口径一致（见 `app/scheduler/devices.py`）：**通知照建**
（照护台待办池还能看到），只是不派单，并打一条 `attention_signal_without_primary_care`
warning 让值班人知道这条没人接。

## 长者不在住：跳过，计数留痕

`Elder.status != 'active'`（`away/transferred/archived`）的长者不评估、不建行——
与 `app/scheduler/health.py`、`app/scheduler/devices.py` 的 `_ELDER_IN_HOUSE` 同口径
（FR-ADM-09：退房后该长者的提醒不应再生成）。

## 任务体不抛

外层 `except Exception` 记满上下文并返回已建条数，等下一轮补跑：一条长者的信号查询
失败不该让整轮扫描停摆（`evaluate_attention_signals` 内部已对单信号兜底，这里兜的是
更强的那层）。
"""

from __future__ import annotations

import datetime
import logging
import uuid
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import session_factory
from app.models import Elder, ElderStaffAssignment, Notification
from app.services import alert_aggregation, notification_service, work_order_service

logger = logging.getLogger(__name__)

#: 任务名（job id 用 `app.scheduler.JOB_PREFIX` 拼，函数内 import 防循环）。
ATTENTION_JOB_NAME = "attention-signal-scan"

#: 每日 09:00（与 19:00 健康采集、08:00 用药复核错开）：护工上班后先看关注列表。
ATTENTION_SCAN_HOUR = 9
ATTENTION_SCAN_MINUTE = 0

#: 通知 kind：0021 迁移放开的第七档（表级 CHECK 是闭集，必须与迁移逐字一致）。
ATTENTION_NOTIFICATION_KIND = "attention_signal"

#: 工单类型 `follow_up` 已在 `work_orders.type` 的 CHECK 里（0010 起），无需迁移。
ATTENTION_ORDER_TYPE = "follow_up"

#: 严重度：建议关注是黄档（红档留给安全告警与漏服升级）。
ATTENTION_ORDER_SEVERITY = "yellow"

#: `payload.module`：与 `devices` / `safety` / `risk_event_escalation` 同一套区分手法。
ATTENTION_PAYLOAD_MODULE = "attention"

#: 在住状态：只有 `active` 的长者才评估（away/transferred/archived 一律跳过）。
_ELDER_IN_HOUSE = "active"

#: 业务日按上海时区切（人理解的「今天」）；存储仍走 UTC。
_BUSINESS_TZ = ZoneInfo("Asia/Shanghai")


def _as_utc(now: datetime.datetime | None) -> datetime.datetime:
    """把注入的时钟归一到 UTC；不注入就是此刻（naive 按 UTC 解释，与存储口径一致）。"""
    if now is None:
        return datetime.datetime.now(datetime.UTC)
    return now if now.tzinfo is not None else now.replace(tzinfo=datetime.UTC)


def _business_date(moment: datetime.datetime) -> datetime.date:
    """业务日：UTC 时刻换算到 Asia/Shanghai 后的日期。"""
    return moment.astimezone(_BUSINESS_TZ).date()


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


async def _notified_codes(
    session: AsyncSession, *, elder_id: uuid.UUID, business_date: datetime.date
) -> set[str]:
    """该长者本业务日已经报过的信号码（幂等判据，见模块文档）。"""
    rows = await session.execute(
        select(Notification.payload["code"].astext).where(
            Notification.kind == ATTENTION_NOTIFICATION_KIND,
            Notification.elder_id == elder_id,
            Notification.payload["record_date"].astext == business_date.isoformat(),
        )
    )
    return {str(code) for code in rows.scalars().all() if code}


def _notification_payload(
    *,
    signal: alert_aggregation.AttentionSignal,
    elder_id: uuid.UUID,
    business_date: datetime.date,
    order_no: str | None,
    staff_id: uuid.UUID | None,
) -> dict[str, object]:
    """通知体：给照护台看「哪条信号、看到了什么、建议做什么、派给了哪张单」。

    只放结构化事实与已经过闸门的文案，不放长者姓名、不放原始对话片段
    （通知是给人看的摘要，不是把证据整份抄进 `payload`；AGENTS §4-10）。
    """
    return {
        "module": ATTENTION_PAYLOAD_MODULE,
        "code": signal.code,
        "level": signal.level,
        "title": signal.title,
        "detail": signal.detail,
        "suggestion": signal.suggestion,
        "evidence": dict(signal.evidence),
        "elder_id": str(elder_id),
        "record_date": business_date.isoformat(),
        "order_no": order_no,
        "assignee_staff_id": str(staff_id) if staff_id is not None else None,
        "summary": f"{signal.title}：{signal.suggestion}",
    }


def _order_note(signal: alert_aggregation.AttentionSignal, *, business_date: datetime.date) -> str:
    """工单备注：把「看到了什么 + 建议做什么」拼成人话，出口再过一次措辞闸门。"""
    note = (
        f"【{signal.title}】{signal.detail} 建议：{signal.suggestion}"
        f"（{business_date.isoformat()} 由每日关注扫描自动派单）"
    )
    return alert_aggregation.assert_attention_wording(note, field_name="order note")


async def _scan_elder(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    business_date: datetime.date,
    moment: datetime.datetime,
) -> int:
    """评估一位长者并按信号落通知 / 派单，返回新建的通知条数。"""
    report = await alert_aggregation.evaluate_attention_signals(
        session, elder_id=elder_id, now=moment
    )
    if not report.has_attention:
        return 0

    existing = await _notified_codes(session, elder_id=elder_id, business_date=business_date)
    staff_id = await _primary_care_staff(session, elder_id)
    created = 0
    for signal in report.signals:
        if signal.code in existing:
            continue
        order_no: str | None = None
        # 去重键 = (信号 code, 业务日)（FR-ALT-04 计划 §2.3）：同一位长者同一信号
        # 一天只该有一张单 / 一条通知，任务重入不会多出第二份。
        dedupe_key = f"attention:{signal.code}:{business_date.isoformat()}"
        if staff_id is not None:
            order = await work_order_service.create_work_order(
                session,
                elder_id=elder_id,
                type=ATTENTION_ORDER_TYPE,
                severity=ATTENTION_ORDER_SEVERITY,
                note=_order_note(signal, business_date=business_date),
                assignee_staff_id=staff_id,
                dedupe_key=dedupe_key,
            )
            order_no = str(order.order_no)
        payload = _notification_payload(
            signal=signal,
            elder_id=elder_id,
            business_date=business_date,
            order_no=order_no,
            staff_id=staff_id,
        )
        await notification_service.create_notification(
            session,
            kind=ATTENTION_NOTIFICATION_KIND,
            severity=ATTENTION_ORDER_SEVERITY,
            elder_id=elder_id,
            payload=payload,
            assignee_staff_id=staff_id,
            dedupe_key=dedupe_key,
        )
        created += 1
        if staff_id is None:
            logger.warning(
                "attention_signal_without_primary_care",
                extra={
                    "job": ATTENTION_JOB_NAME,
                    "elder_id": str(elder_id),
                    "code": signal.code,
                    "reason": "no_primary_care",
                    "record_date": business_date.isoformat(),
                },
            )
    return created


async def scan_attention_signals(*, now: datetime.datetime | None = None) -> int:
    """跑一轮关注扫描：给每位在住长者评估四信号，命中的落通知并派单，返回新订单数。

    `now` 可注入是为了让用例钉死业务日与四信号的时间窗口，而不是 `sleep` 等真实时间。

    任务体**不抛**：外层 `except Exception` 记满上下文后返回已建条数，等下一轮补跑。
    """
    moment = _as_utc(now)
    business_date = _business_date(moment)
    created = 0
    try:
        async with session_factory() as session:
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
            skipped = 0
            for elder_id in elder_ids:
                try:
                    # 单长者一段 SAVEPOINT（复核 2026-11-12 指出）：`_scan_elder` 内是
                    # 「先建单、后建通知」两步，若通知一步抛错又被下面 `continue` 吞掉，
                    # 半成品工单会留在本事务里、随末尾统一 commit 落库，形成「有单无通知」。
                    # 套一层 savepoint 后，该长者的部分写入整体回滚，其余长者不受影响。
                    async with session.begin_nested():
                        new_rows = await _scan_elder(
                            session,
                            elder_id=elder_id,
                            business_date=business_date,
                            moment=moment,
                        )
                except alert_aggregation.AlertAggregationError:
                    # 红线（措辞 / 入参）必须让整轮失败：宁可今天不提醒，也不送诊断口径出去。
                    raise
                except Exception:
                    # 单条长者失败不拖垮整轮：记满上下文，等下一轮补跑。
                    logger.warning(
                        "attention_scan_elder_failed",
                        extra={
                            "job": ATTENTION_JOB_NAME,
                            "elder_id": str(elder_id),
                            "record_date": business_date.isoformat(),
                        },
                        exc_info=True,
                    )
                    skipped += 1
                    continue
                created += new_rows
            await session.commit()
    except alert_aggregation.AlertAggregationError:
        # 红线（措辞 / 入参）必须让整轮失败：宁可今天不提醒，也不送诊断口径出去。
        raise
    except Exception:
        logger.exception(
            "attention signal scan job failed",
            extra={"job": ATTENTION_JOB_NAME, "record_date": business_date.isoformat()},
        )
        return created
    logger.info(
        "attention signal scan job done",
        extra={
            "job": ATTENTION_JOB_NAME,
            "created_count": created,
            "elder_total": len(elder_ids),
            "elder_failed": skipped,
            "record_date": business_date.isoformat(),
        },
    )
    return created


def register_attention_jobs(scheduler: AsyncIOScheduler) -> None:
    """把关注扫描挂到调度器上（cron，每日 09:00）。

    函数内 import `JOB_PREFIX`：`app/scheduler/__init__.py` 会 import 本模块，模块级再
    反向 import 就成环了（与 `app/scheduler/devices.py`、`medication.py` 同一手法）。
    """
    from app.scheduler import JOB_PREFIX

    scheduler.add_job(
        scan_attention_signals,
        trigger="cron",
        hour=ATTENTION_SCAN_HOUR,
        minute=ATTENTION_SCAN_MINUTE,
        id=f"{JOB_PREFIX}:{ATTENTION_JOB_NAME}",
        name=ATTENTION_JOB_NAME,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=300,
    )


__all__ = [
    "ATTENTION_JOB_NAME",
    "ATTENTION_NOTIFICATION_KIND",
    "ATTENTION_ORDER_SEVERITY",
    "ATTENTION_ORDER_TYPE",
    "ATTENTION_PAYLOAD_MODULE",
    "ATTENTION_SCAN_HOUR",
    "ATTENTION_SCAN_MINUTE",
    "register_attention_jobs",
    "scan_attention_signals",
]
