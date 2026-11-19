"""用药提醒的调度任务：事件预生成 + 四次触达 + 漏服升级（PRD §4.5 FR-MED-04/06/08）。

## 口径（沿用占位文件，一条不改）

- 触达节拍 **T0 / +5 / +10 / +15 分钟**，每次触达后等 **60 秒**确认窗；四次仍未确认
  → 升级为 `notifications(kind='medication_escalation')` 并派护工（同事务一起落
  红线工单）。
- 幂等键：事件 `(plan_id, scheduled_at)`、触达 `(event_id, attempt_no)`——都靠
  `app/models/medication.py` 里的唯一约束兜底，重复调用返回原行，不重复触达。
- 一个 `plan_id × scheduled_at` 只允许一条提醒（`uq_medication_events_plan_id_scheduled_at`）。

## 为什么是**三个** job 而不是一个

| job id | cron | 职责 | 粒度理由 |
| --- | --- | --- | --- |
| `anyu:medication-scan` | `minute="*/5"` | 提前生成当日用药事件 | 5 分钟粒度足以把「药单时刻 T→事件落库」延迟压到 <5 分钟；若用每小时 `minute=0`，08:05 的药单要等到 09:00 才生成，等于漏服 |
| `anyu:medication-due-scan` | `minute="*"` | 判定下一枪：触达 / 等待 / 升级 | 四档间隔固定 5 分钟、每枪确认窗 60 秒，判定粒度必须 ≤1 分钟；5 分钟粒度会让 60 秒窗口整体失真 |
| `anyu:medication-review-scan` | `hour="8", minute="0"` | 到期复核 + 已过期未续催办（FR-MED-09） | 复核是**人的动作**，不是到点提醒；一天扫一次就够，扫得再勤也只是把同一批药单重复看一遍 |

三个 job 都带 `coalesce=True, max_instances=1, misfire_grace_time=300`：进程重启后
补跑一次而不是堆积成雪崩，且同一个 job 不会并发两份。

## 到期复核与续方（FR-MED-09）

- 每日 08:00 `run_medication_review_scan` → `medication_service.notify_plan_reviews`：
  `end_date ∈ [today+1, today+7]` 且 `review_notified_at IS NULL` 的 active 计划建一条
  `medication_review` 通知给主治医生并回写该列（列即幂等闸门）。
- 已过期未续（`end_date < today` 且没有子计划指着它）**不重复建单**：复用既有未关单
  去重，单还开着且不是红档就升红，已经是红档就本日跳过。
- **续方 = 新开一条计划**（`start_date = 旧 end_date + 1`、`end_date = start + 30`），
  旧计划保持 `active` 自然到期；本模块不需要为续方加任何补偿逻辑——新计划
  `start_date` 一到，`run_medication_scan` 按日期过滤自然开始生成事件。

## 「每次触达等 60s」在调度器里怎么表达

**任务体里不许 `await asyncio.sleep(60)`**（会占住调度器线程、重启即丢、也没法补跑）。
「等待」被表达成**下一次扫描的到点时间**：

- 第 k 次触达的 `sent_at` 落在 `medication_attempts`，本轮扫描据此算出
  `next_retry_at(scheduled_at, k, wait_seconds)` = `max(T+5k, T+5(k-1)+60s)`；
- 扫描到点前一律 `wait`，到点才发下一枪；
- 四枪打完后再等最后一枪的 60 秒窗关闭 → 仍无确认则升级。

时间线（`wait_seconds=60`）：`T0 触达 → T+5 第二枪 → T+10 第三枪 → T+15 第四枪 →
T+16 升级`。整个窗口终点是 `T + 5×(attempts-1) + 60s`；**窗口整体错过（例如调度器
停机 20 分钟）时直接升级、不补发晚到的提醒**（FR-MED-08：超窗建单，不补发）。

## 停用药单（W1，PRD:508 AC-27）

医生的「停」= `medication_plans.status='stopped'`（服务层 `stop_plan`），**不新增**
`cancelled` 事件态（事件的 CHECK 仍只有四值）。停用后的三件事都在本模块落地：

1. `run_medication_scan` 的 `WHERE status == 'active'` 让停用药单**不再生成新事件**；
   同一处还有 `elders.status == 'active'` 的过滤：**退房/转出/归档的长者不再生成
   用药事件**（FR-ADM-09，W3 核对时补的缺口）；
2. 已经到点但**一次都没触达**的事件，`decide_event_action` 判 `skip`（不触达、不升级），
   并记 `medication_skipped_total{reason=plan_stopped}`；
3. 已经发出去、还在等确认的事件**照旧走完**：长者已经被提醒过一次，静默丢弃会让
   「提醒过但没人跟进」这件事从库里消失——那比多打一通电话更糟（安全回归）。

## 分层（AGENTS §2）

任务体全是纯编排：**只调 `app/services/medication_service.py` 与
`app/services/notification_service.py`**，不出现 `session.add/commit(Model)` 之外的
写库动作；唯一自己发起的 `select(...)` 是「读事件 / 聚合触达次数」这类读口径
（与 `app/scheduler/health.py` 同款）。时间一律 UTC 存储，业务自然日按
`settings.scheduler_timezone`（默认 `Asia/Shanghai`）切。

## 触达通道（`deliver`）是注入点

出站通道**由装配根注入**：生产环境在 `app/main.py` 的 lifespan 里用
`app.audio.medication_delivery.build_medication_deliver(...)` 构造，再经
`build_scheduler(medication_deliver=...)` → `register_medication_jobs(deliver=...)`
一路传到 due-scan 的 `functools.partial`（注册时绑定，运行期每轮吃同一个通道对象）。

本模块**不 import `app/audio`**（AGENTS §2.1 禁止横向 import），两侧只通过
`Deliver` 这一**函数签名**耦合。默认实现 `_default_deliver` **不伪造送达**，回执落
`receipt='none'`——它只在「通道没装配」（装配失败、测试、`scheduler_enabled=False`）
时生效，日志口径 `no outbound channel wired` 是告警信号而不是正常态。
回执取值闭集见 `medication_service.ATTEMPT_RECEIPTS`，非法值一律降级为 `none` 并记
warning——宁可记「没送到」，也不能让库里出现「假装送到了」。

## 升级时的终态归因（FR-MED-06 / FR-MED-08）

`_escalate_event` 建单后按该事件的触达回执补写终态（纯函数口径见
`medication_service.escalation_attribution`）：

| 回执情况 | `terminal_state` | `failure_reason` | 依据 |
| --- | --- | --- | --- |
| 四次触达**一次回执都没有** | `notification_failed` | `rtc_failed` | PRD:324「四次均无回执」+ FR-MED-06 建连失败归因 |
| 有回执但无人确认 | `missed_unconfirmed` | `no_answer` | PRD:323「有回执、无响应」 |
| 窗口整体错过、一枪都没打 | `notification_failed` | `rtc_failed` | 同「没送到」：没有送达回执就不许写成「收到了」 |

`offline` 不从这里产出：设备离线归 `notification_service.recover_after_offline`
那条**已知离线**的路径（PRD FR-MED-08）。调度器侧 `devices.status` 只在解绑/重置时
写、WS 断连不更新，拿它判离线属于猜——错记成设备故障比记「外呼没送到」更难排查。

## 服务层对齐

`medication_service` 由 迭代 7 · C 卡并行落地，因此本模块对它的 import 放在**函数内**
（模块级 import 会让 `app.scheduler` / `app.main` 在服务层写盘中途整体崩掉）。所有调用
集中在文件尾 `_svc_*` 适配块，签名若变只改那一块。
"""

from __future__ import annotations

import datetime
import logging
import re
import time
import uuid
import zoneinfo
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Final, Literal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.metrics import counter, observe_ms
from app.db import session_factory as db_session_factory
from app.models import (
    Elder,
    ElderStaffAssignment,
    MedicationAttempt,
    MedicationEvent,
    MedicationPlan,
)
from app.services import notification_service

logger = logging.getLogger(__name__)

#: `schedule_cron` 的受限形态：本仓只支持 `HH:MM`（模型注释与 API 边界同款正则）。
#: 不复用 `app/api/medication.py` 的私有常量——`scheduler → api` 是横向 import（AGENTS §2.1）。
SCHEDULE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

#: 事件预生成的扫描周期（分钟）。见模块文档的粒度理由。
SCAN_INTERVAL_MINUTES: Final[int] = 5

#: 提前生成的天花板：到点时刻在 `now + SCAN_INTERVAL_MINUTES` 内就先落库，
#: 让「到点」的那一轮扫描用真实到点时刻触达，而不是晚一个扫描周期。
SCAN_LOOKAHEAD_MINUTES: Final[int] = SCAN_INTERVAL_MINUTES

#: 单轮 due-scan 最多处理的事件数：调度器不许被一次积压拖成雪球（下一分钟接着做）。
DUE_SCAN_BATCH_LIMIT: Final[int] = 200

_SCAN_JOB: Final[str] = "medication-scan"
_DUE_SCAN_JOB: Final[str] = "medication-due-scan"
_REVIEW_SCAN_JOB: Final[str] = "medication-review-scan"

#: 还在等确认的事件状态：只有这两态会被 due-scan 继续推进。
_OPEN_EVENT_STATUSES: Final[tuple[str, ...]] = ("calling", "waiting_confirm")

#: 已经闭环的事件状态：再触达就是打扰。
_CLOSED_EVENT_STATUSES: Final[tuple[str, ...]] = ("confirmed", "escalated")

#: 回执闭集（与 `medication_service.ATTEMPT_RECEIPTS` 对齐；非法值降级为 `none`）。
_RECEIPT_VALUES: Final[tuple[str, ...]] = ("card_rendered", "tts_finished", "none")

Action = Literal["wait", "touch", "escalate", "skip"]
"""一轮扫描可以做出的判定。

`skip` = 所属药单已停用、且这条事件一次都没触达（W1 停用口径）：不触达、不升级、
不计入「推进」，只在指标里留一笔 `medication_skipped_total{reason=plan_stopped}`。
"""


# ---------------------------------------------------------------------------
# 纯函数 / 小工具
# ---------------------------------------------------------------------------


def _as_utc(value: datetime.datetime | None) -> datetime.datetime:
    """统一成 UTC 的 aware 时间。`None` → 现在；naive 按 UTC 解释（存储口径）。"""
    if value is None:
        return datetime.datetime.now(datetime.UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.UTC)
    return value.astimezone(datetime.UTC)


def _business_date(moment: datetime.datetime) -> datetime.date:
    """UTC 时刻对应的业务自然日（展示/排期用 `Asia/Shanghai`，AGENTS §8）。"""
    return moment.astimezone(zoneinfo.ZoneInfo(get_settings().scheduler_timezone)).date()


def parse_schedule_cron(value: str | None) -> tuple[int, int]:
    """`"HH:MM"` → `(hour, minute)`；其它形态抛 `ValueError`。

    数据库列**没有** CHECK 约束（迁移 0009 只写了 text），所以调度器必须容忍坏值：
    调用方接住 `ValueError`、记 warning、跳过该计划，不能让整轮扫描崩掉。
    """
    if not isinstance(value, str):
        raise ValueError(f"schedule_cron 必须是字符串，收到 {value!r}")
    text = value.strip()
    if not SCHEDULE_PATTERN.match(text):
        raise ValueError(f"schedule_cron 只支持 HH:MM（本项目不支持 cron 表达式）：{value!r}")
    hour, minute = text.split(":")
    return int(hour), int(minute)


def scheduled_at_for(
    day: datetime.date, hour: int, minute: int, tz: zoneinfo.ZoneInfo
) -> datetime.datetime:
    """业务自然日 + 药单时刻 → UTC 的到点时间。"""
    local = datetime.datetime.combine(day, datetime.time(hour, minute), tzinfo=tz)
    return local.astimezone(datetime.UTC)


@dataclass(frozen=True, slots=True)
class EventDecision:
    """一条到点事件的下一步动作（`wait` / `touch` / `escalate` / `skip`）。"""

    action: Action
    attempt_no: int | None = None
    reason: str = ""


def decide_event_action(
    *,
    scheduled_at: datetime.datetime,
    attempts_sent: int,
    last_sent_at: datetime.datetime | None,
    now: datetime.datetime,
    attempts: int,
    wait_seconds: int,
    interval_minutes: int,
    plan_status: str | None = None,
) -> EventDecision:
    """判定一条事件在这一轮扫描里的动作（纯函数：只读入参、不碰库，便于单测）。

    优先级（顺序即语义，不可重排）：

    0. **计划已非 active 且一次都没触达**：`skip`（W1 停用口径）。停用的药单不该把人
       从床上叫起来，也不该为它派单；`plan_status=None`（查不到计划行，FK 理论上挡得住）
       按未知处理、不跳过，让它照常走安全网。
    1. **四枪打完**（`attempts_sent >= attempts`）：等最后一枪的 60 秒窗关闭；关了仍
       没确认 → `escalate`。窗未关 → `wait`。
    2. **下一枪未到点**：`wait`。到点时间取 `medication_service.next_retry_at` 的口径
       （节拍档位与本次 60 秒窗取晚者）。
    3. **窗口整体错过**（`now >= 时间线终点`）：`escalate`，**不补发**晚到的提醒
       （FR-MED-08）。
    4. 其余 → `touch(attempt_no = attempts_sent + 1)`。

    `attempts_sent` 与 `last_sent_at` 由 `medication_attempts` 聚合得到，不按事件 N+1 查。

    **已触达过的事件不因计划停用而 skip**：长者已经被提醒过一次，静默丢弃会让「提醒过
    但没人跟进」这件事从库里消失——那比多打一通电话更糟（安全回归）。停用只截断
    「还没开始」的那一条。

    指标：命中 skip 时记 `medication_skipped_total{reason=plan_stopped}`。事件行保持开态
    直到人工收尾，每轮扫描都会重新命中一次，所以这个计数按「判定次数」读，不是「事件条数」。
    """
    from app.services import medication_service

    scheduled = _as_utc(scheduled_at)
    current = _as_utc(now)
    last_sent = _as_utc(last_sent_at) if last_sent_at is not None else None
    max_attempts = max(1, attempts)
    spacing = datetime.timedelta(minutes=max(0, interval_minutes))
    window = datetime.timedelta(seconds=max(0, wait_seconds))
    # 整个提醒窗口的终点：最后一枪的节拍档位 + 它自己的确认窗。
    timeline_end = scheduled + spacing * (max_attempts - 1) + window

    if plan_status is not None and plan_status != "active" and attempts_sent <= 0:
        counter("medication_skipped_total", reason="plan_stopped")
        return EventDecision(action="skip", reason="plan_stopped")

    if attempts_sent >= max_attempts:
        deadline = (last_sent + window) if last_sent is not None else timeline_end
        if current >= deadline:
            return EventDecision(action="escalate", reason="attempts_exhausted")
        return EventDecision(action="wait", reason="last_window_open")

    if attempts_sent <= 0:
        due_at = scheduled
    else:
        due_at = medication_service.next_retry_at(scheduled, attempts_sent, wait_seconds)

    if current < due_at:
        return EventDecision(action="wait", reason="slot_not_due")
    if current >= timeline_end:
        return EventDecision(action="escalate", reason="window_missed")
    return EventDecision(action="touch", attempt_no=attempts_sent + 1, reason="slot_due")


@dataclass(frozen=True, slots=True)
class MedicationTouchTarget:
    """交给出站通道（TTS / 卡片 / 外呼）的一次触达目标。

    `drug_snapshot` 是事件里冻结的药单快照（药单改了不追溯历史事件的展示口径）。
    长者姓名等展示字段由外呼层按 `elder_id` 自行解析，本模块不重复查。
    """

    reminder_id: uuid.UUID
    plan_id: uuid.UUID
    elder_id: uuid.UUID
    scheduled_at: datetime.datetime
    attempt_no: int
    drug_snapshot: Mapping[str, object]


Deliver = Callable[[MedicationTouchTarget], Awaitable[str | None]]
"""出站通道签名：返回 `medication_attempts.receipt` 取值（未知值降级为 `none`）。"""


async def _default_deliver(target: MedicationTouchTarget) -> str:
    """默认出站通道：**空实现**，不伪造送达。

    生产链路由 `app/main.py` 注入 `build_medication_deliver()` 的真通道（见模块文档
    「触达通道」）；走到这里的只有「通道没装配」——测试、`scheduler_enabled=False`、
    或启动期构造失败。此时如实记 `none`（`receipt` 闭集里的「没送到」），而不是假装
    `tts_finished`；升级时它会让事件归到 `notification_failed`，运维能看见。
    """
    logger.info(
        "medication touch not delivered (no outbound channel wired) reminder_id=%s attempt_no=%s",
        target.reminder_id,
        target.attempt_no,
    )
    return "none"


def _normalise_receipt(raw: object) -> str:
    """回执白名单化：非法/未知值一律 `none`，绝不让「假送达」进库。"""
    if isinstance(raw, str) and raw in _RECEIPT_VALUES:
        return raw
    logger.warning("未知的触达回执 %r，按 none 记录", raw)
    return "none"


def _tts_result_for(receipt: str) -> str | None:
    """回执 → `medication_attempts.tts_result`（TD §3.4：tts 失败但卡片渲染成功算已送达）。"""
    if receipt == "tts_finished":
        return "ok"
    if receipt == "card_rendered":
        return "tts_failed"
    return None


# ---------------------------------------------------------------------------
# 用药确认图：触达之后把事件挂到 HITL 停点上（FR-MED-05 / AC-17）
# ---------------------------------------------------------------------------
#
# 分工：**触达仍由本模块发**（通道在这里、节拍也在这里），图只负责「等长者回应」
# 这一段的停点与终态。为什么不让图自己发第一枪：`MedicationRuntime.deliver` 的形状是
# `(ReminderEvent, text) -> DeliveryResult`，而本模块的通道是
# `MedicationTouchTarget -> receipt`；两个签名各有各的调用方（图给对话/测试用、
# 本模块给调度用），硬转一层等于把同一个通道实现两遍（AGENTS §4.8）。
#
# 因此这里用 `aupdate_state(as_node="remind")` 把线程直接推到 `wait_confirm`：
# 「提醒已经发出去了」是既成事实，图只需要接着等回应。停点一挂上，
# `medication_service.confirm_event` / `confirm_from_dialog` 那两条入口就能
# `Command(resume=...)` 把它推完，`hitl_resumed` 也才真正为 True。
#
# 判停点必须读 checkpoint（`aget_state().next`），不能看事件行：事件行只说明
# 「有一枪发出去了」，说明不了「有没有人在等回应」，而后者才是重放时要不要再发的判据。

#: 图里 `wait_confirm` 的前一个节点。`as_node` 指它 = 「提醒这一步已经做完了」。
_GRAPH_REMIND_NODE: Final[str] = "remind"


async def _confirmation_thread(target: MedicationTouchTarget) -> str:
    """这条事件在图里的线程键（与图节点用同一个纯函数，重启后照样算得出来）。"""
    from app.graphs import medication_confirmation_graph as graph_module

    return graph_module.medication_thread_id(
        elder_id=target.elder_id,
        plan_id=target.plan_id,
        scheduled_at=target.scheduled_at,
    )


def _payload_attempt(payload: Mapping[str, object] | None) -> int | None:
    """停点载荷里的触达序号；取不到就当「不知道」（比猜一个更安全）。"""
    if payload is None:
        return None
    raw = payload.get("attempt_no")
    return raw if isinstance(raw, int) else None


async def _pending_confirmation(
    target: MedicationTouchTarget,
) -> Mapping[str, object] | None:
    """这条事件是否已经停在图的确认停点上；图不可用一律当「没有停点」。

    图不可用（checkpointer 掉线、图装配失败）只记 warning：调度器的主职责是**把提醒
    发出去**，流程引擎是增强项，不能反过来把触达卡死。
    """
    try:
        from app.graphs import medication_confirmation_graph as graph_module
        from app.services import medication_service

        graph = await medication_service.confirmation_graph()
        return await graph_module.pending_confirmation(graph, await _confirmation_thread(target))
    except Exception as exc:
        logger.warning(
            "用药确认图不可用，按直写继续 reminder_id=%s error_type=%s",
            target.reminder_id,
            type(exc).__name__,
        )
        return None


async def _attach_confirmation_wait(
    target: MedicationTouchTarget,
    *,
    attempt_no: int,
    delivered_at: datetime.datetime,
) -> bool:
    """把已发出的这一枪挂到图的确认停点上；返回是否挂上（挂不上不影响触达）。

    调用时机固定在**写库之后**：先把 attempts/事件落定，再挂停点。反过来（先挂图后
    写库）一旦图抛异常，已经喊出去的提醒就没有账，等于凭空消失。
    """
    try:
        from app.graphs import medication_confirmation_graph as graph_module
        from app.services import medication_service

        graph = await medication_service.confirmation_graph()
        thread = await _confirmation_thread(target)
        if await graph_module.pending_confirmation(graph, thread) is not None:
            # 图上已经有一个更早的停点在等（T+5 这一枪发出去时，T0 的确认还没回应）。
            # 不再叠加第二个停点：一次确认只能回答「这条提醒」，叠上去会让 resume
            # 打回哪一枪变得不可预测。
            return True

        seed = graph_module.initial_state(
            thread_id=thread,
            elder_id=target.elder_id,
            reminder_id=target.reminder_id,
            plan_id=target.plan_id,
            scheduled_at=target.scheduled_at,
            drug_snapshot=dict(target.drug_snapshot),
            triggered_by="scheduler",
            attempt_no=attempt_no,
        )
        seed["status"] = "waiting_confirm"
        seed["next_action"] = graph_module.CONFIRM_STOP_NODE
        seed["delivered_at"] = delivered_at.isoformat()

        config = graph_module.state_config(thread)
        await graph.aupdate_state(config, seed, as_node=_GRAPH_REMIND_NODE)
        await graph.ainvoke(None, config)
    except Exception as exc:
        logger.warning(
            "用药确认停点挂载失败（提醒已发出，事件按直写继续）reminder_id=%s "
            "attempt_no=%s error_type=%s",
            target.reminder_id,
            attempt_no,
            type(exc).__name__,
        )
        return False

    logger.info(
        "medication confirmation stop opened reminder_id=%s attempt_no=%s thread_id=%s",
        target.reminder_id,
        attempt_no,
        thread,
    )
    return True


def _plan_snapshot(plan: MedicationPlan) -> dict[str, object]:
    """药单 → 事件快照（`payload_hash` 的输入，键序无关）。

    带上 `plan_version` / 起止日：药单被医生改过（version 自增）后再扫描同一到点
    时刻，快照内容不同 → 服务层按 FR-MED-04 判「同键异内容」并拦下来，而不是
    悄悄沿用旧快照。
    """
    return {
        "drug_name": plan.drug_name,
        "dose": plan.dose,
        "schedule_cron": plan.schedule_cron,
        "plan_version": plan.version,
        "start_date": plan.start_date.isoformat() if plan.start_date else None,
        "end_date": plan.end_date.isoformat() if plan.end_date else None,
    }


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------


def register_medication_jobs(
    scheduler: AsyncIOScheduler, *, deliver: Deliver | None = None
) -> None:
    """把用药相关的两个 cron 任务挂到调度器上（job id 前缀与既有任务同源）。

    `deliver` 是外呼通道（TTS/卡片/外呼），由装配根 `app/main.py` 构造后传进来；
    `None` 时任务体退回 `_default_deliver`（空实现、不伪造送达），只有测试和
    「通道装配失败」两种情况该走这条路径。

    为什么要 `functools.partial` 而不是给 `add_job` 传 `kwargs`：APScheduler 的
    `add_job(**kwargs)` 是本函数自己的参数，任务实参要走 `add_job(..., kwargs={...})`；
    但用 partial 把通道**绑在注册时**，预检（`check_callable_args`）看到的就是绑定后的
    签名，执行时也不会因为谁改了 `kwargs` 字典而在两轮之间换通道。

    `JOB_PREFIX` 在函数内 import：`app/scheduler/__init__.py` 在**定义它之前**就
    import 本模块，模块级 import 会成环（ImportError）。
    """
    from app.scheduler import JOB_PREFIX

    # 通道在注册时绑定：due-scan 每一轮都吃同一个 `deliver` 对象，而不是每轮重读配置。
    due_scan_job: Callable[..., Awaitable[int | None]] = partial(
        run_medication_due_scan, deliver=deliver
    )

    jobs: tuple[tuple[str, Callable[..., Awaitable[int | None]], dict[str, str]], ...] = (
        (_SCAN_JOB, run_medication_scan, {"minute": f"*/{SCAN_INTERVAL_MINUTES}"}),
        (_DUE_SCAN_JOB, due_scan_job, {"minute": "*"}),
        # FR-MED-09：每日 08:00 扫「7 天内到期 + 已过期未续」，一天一次就够——复核是
        # 人的动作，不是到点提醒，扫得再勤也只是把同一批单重复看一遍。
        (_REVIEW_SCAN_JOB, run_medication_review_scan, {"hour": "8", "minute": "0"}),
    )
    for name, job_func, fields in jobs:
        scheduler.add_job(
            job_func,
            trigger="cron",
            id=f"{JOB_PREFIX}:{name}",
            name=name,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=300,
            **fields,
        )
    logger.info("medication jobs registered count=%s", len(jobs))


# ---------------------------------------------------------------------------
# 任务体 1：提前生成当日用药事件
# ---------------------------------------------------------------------------


async def run_medication_scan(
    *,
    now: datetime.datetime | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> int:
    """扫 `medication_plans`，把「今天到点（或即将到点）」的计划落成用药事件。

    只做四件事：查 active 且日期区间覆盖今日的计划 → 解析 `HH:MM` → 到点时间在
    `now + 5min` 内则 `medication_service.ensure_event`（幂等）→ 返回新建条数。

    **`status='stopped'` 的计划一条新事件都不生成**（W1 停用口径）：医生的「停」只改
    `medication_plans.status`，不新增事件态，这里就是「停用生效」的落点。

    单条计划失败（非法 cron、同键异内容冲突、库错误）只记日志不中断整轮：一条坏药单
    不能让其它长者的提醒一起消失。
    """
    from app.services import medication_service

    current = _as_utc(now)
    settings = get_settings()
    tz = zoneinfo.ZoneInfo(settings.scheduler_timezone)
    day = _business_date(current)
    horizon = current + datetime.timedelta(minutes=SCAN_LOOKAHEAD_MINUTES)
    factory = session_factory or db_session_factory
    started = time.perf_counter()
    created = 0
    skipped = 0

    try:
        # ① 只取主键。列表拿到就关 session：后面不再持有任何 ORM 实例，
        # 也就不存在「某条 rollback 把别的对象标记 expired」的连锁。
        async with factory() as session:
            plan_ids = list(
                (
                    await session.execute(
                        select(MedicationPlan.plan_id)
                        .where(
                            MedicationPlan.status == "active",
                            MedicationPlan.start_date <= day,
                            (MedicationPlan.end_date.is_(None)) | (MedicationPlan.end_date >= day),
                            # FR-ADM-09：退房/转出/归档的长者不再生成用药提醒。判定与
                            # `medication_service._resident_plans` 同口径（那边用 in_ 子查询，
                            # 这里同样是子查询，不 join 以免改坏下面按 plan 的循环语义）。
                            MedicationPlan.elder_id.in_(
                                select(Elder.elder_id).where(Elder.status == "active")
                            ),
                        )
                        .order_by(MedicationPlan.plan_id)
                    )
                )
                .scalars()
                .all()
            )

        # ② 每条计划独占一个 session / 事务。
        #
        # 为什么不能全部共用一个 session：`session.rollback()` 会把该 session 里已加载的
        # 全部 ORM 实例标记为 expired —— `expire_on_commit=False`（app/db.py:33）只管
        # commit，**管不住 rollback**。一旦某条计划抛 `MedicationEventConflict`（同键异
        # 内容），后面的 plan 读 `schedule_cron` 就会触发懒加载；而属性访问是同步上下文，
        # 懒加载要发 SQL，于是抛 `MissingGreenlet`，整轮连同其它长者的提醒一起丢失。
        # 隔离到单条后，一条失败只记一条日志 —— 正是本函数 docstring 承诺的行为。
        for plan_id in plan_ids:
            async with factory() as session:
                plan = await session.get(MedicationPlan, plan_id)
                if plan is None:
                    continue
                try:
                    hour, minute = parse_schedule_cron(plan.schedule_cron)
                except ValueError as exc:
                    skipped += 1
                    logger.warning(
                        "跳过 schedule_cron 非法的用药计划 plan_id=%s schedule_cron=%r：%s",
                        plan.plan_id,
                        plan.schedule_cron,
                        exc,
                    )
                    continue
                scheduled_at = scheduled_at_for(day, hour, minute, tz)
                if scheduled_at > horizon:
                    continue
                try:
                    _, is_new = await _svc_ensure_event(
                        session, plan=plan, scheduled_at=scheduled_at
                    )
                    # 每条独立提交：粒度变细不会引入重复，`ensure_event` 自身幂等。
                    await session.commit()
                except medication_service.MedicationEventConflict as exc:
                    # 同键异内容：药单被改过，这条到点事件保留旧快照，等人看（不刷 error）。
                    skipped += 1
                    logger.warning(
                        "用药事件同键异内容，保留原事件 plan_id=%s scheduled_at=%s：%s",
                        plan.plan_id,
                        scheduled_at.isoformat(),
                        exc,
                    )
                    await session.rollback()
                    continue
                except Exception:
                    skipped += 1
                    logger.exception("生成用药事件失败 plan_id=%s", plan.plan_id)
                    await session.rollback()
                    continue
                if is_new:
                    created += 1
    except Exception:
        logger.exception("medication scan 整轮失败 date=%s", day.isoformat())
    finally:
        counter("medication_scan_runs_total")
        counter("medication_events_created_total", value=created)
        observe_ms("medication_scan_ms", (time.perf_counter() - started) * 1000.0)

    logger.info(
        "medication scan done business_date=%s created=%s skipped=%s",
        day.isoformat(),
        created,
        skipped,
    )
    return created


# ---------------------------------------------------------------------------
# 任务体 1b：到期复核（FR-MED-09，每日 08:00）
# ---------------------------------------------------------------------------


async def run_medication_review_scan(
    *,
    now: datetime.datetime | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> int:
    """扫「7 天内到期」与「已过期未续」的药单，给主治医生建复核提醒。

    判定、去重、建单、回写 `review_notified_at` 全在
    `medication_service.notify_plan_reviews`（写库只能走服务层，AGENTS §2）；这里
    只负责切业务日期、开事务、记指标与日志。

    返回本轮**新建**的通知条数（升级既有单不算新建），供测试与指标断言。
    整轮失败不抛：调度器的任务体不许把异常留给 APScheduler 的日志黑洞。
    """
    from app.services import medication_service

    current = _as_utc(now)
    day = _business_date(current)
    factory = session_factory or db_session_factory
    started = time.perf_counter()
    created = 0

    try:
        async with factory() as session:
            outcome = await medication_service.notify_plan_reviews(session, today=day)
            await session.commit()
            created = outcome.created
            logger.info(
                "medication review scan done business_date=%s due=%s expired=%s "
                "escalated=%s created=%s",
                day.isoformat(),
                outcome.due,
                outcome.expired,
                outcome.escalated,
                outcome.created,
            )
    except Exception:
        logger.exception("medication review scan 整轮失败 date=%s", day.isoformat())
    finally:
        counter("medication_review_scan_runs_total")
        counter("medication_review_notifications_total", value=created)
        observe_ms("medication_review_scan_ms", (time.perf_counter() - started) * 1000.0)
    return created


# ---------------------------------------------------------------------------
# 任务体 2：单次触达
# ---------------------------------------------------------------------------


async def run_medication_touch(
    *,
    reminder_id: uuid.UUID,
    attempt_no: int,
    now: datetime.datetime | None = None,
    deliver: Deliver | None = None,
) -> None:
    """一次触达：出站送达（`deliver`）+ 记 `medication_attempts` + 推进事件状态。

    顺序固定为「先出站、后落库」：出站可能耗时（TTS/外呼），不能占着数据库事务；
    运行库连接与会话在出站前就已释放。

    幂等：同 `(event_id, attempt_no)` 重复调用由服务层返回原行（只是补空字段），
    因此本函数可以安全重放。

    与用药确认图的关系（FR-MED-05 / AC-17）：触达照旧由本函数发，发完再把这条事件
    挂到图的 `wait_confirm` 停点上（`_attach_confirmation_wait`）。三件事按顺序：
    ① 图上已有「等第 `attempt_no` 枪回应」的停点 → 这一枪其实早发过了（后端重启后
    重放、或同一轮被驱动两次），**直接返回**，不重复催药；
    ② 停点在更早一枪上 → 照旧发，节拍归本模块，不能因为图先挂上就吞掉 T+5/T+10/T+15；
    ③ 发完且图上没有停点 → 挂停点；挂不上只 warning（图是增强项，不能拦触达）。
    """
    from app.services import medication_service

    current = _as_utc(now)
    deliverer: Deliver = deliver or _default_deliver
    factory = db_session_factory

    async with factory() as session:
        event = await medication_service.load_event(session, reminder_id)
        if event.status in _CLOSED_EVENT_STATUSES:
            logger.info(
                "用药事件已闭环，跳过触达 reminder_id=%s status=%s", reminder_id, event.status
            )
            return
        target = MedicationTouchTarget(
            reminder_id=event.reminder_id,
            plan_id=event.plan_id,
            elder_id=event.elder_id,
            scheduled_at=event.scheduled_at,
            attempt_no=attempt_no,
            drug_snapshot=dict(event.drug_snapshot or {}),
        )

    pending = await _pending_confirmation(target)
    pending_attempt = _payload_attempt(pending)
    if pending_attempt is not None and pending_attempt >= attempt_no:
        logger.info(
            "用药提醒已在等确认，跳过重复触达 reminder_id=%s attempt_no=%s pending_attempt=%s",
            reminder_id,
            attempt_no,
            pending_attempt,
        )
        return

    receipt = _normalise_receipt(await deliverer(target))

    async with factory() as session:
        await _svc_record_attempt(
            session,
            reminder_id=reminder_id,
            attempt_no=attempt_no,
            sent_at=current,
            receipt=receipt,
        )
        await _svc_update_state(
            session,
            reminder_id=reminder_id,
            status="waiting_confirm",
            retry_count=max(0, attempt_no - 1),
        )
        await session.commit()

    counter("medication_touch_total")
    logger.info(
        "medication touch sent reminder_id=%s attempt_no=%s receipt=%s",
        reminder_id,
        attempt_no,
        receipt,
    )

    if pending is None:
        await _attach_confirmation_wait(target, attempt_no=attempt_no, delivered_at=current)


# ---------------------------------------------------------------------------
# 任务体 3：到点扫描（触达 / 等待 / 升级）
# ---------------------------------------------------------------------------


async def run_medication_due_scan(
    *,
    now: datetime.datetime | None = None,
    deliver: Deliver | None = None,
) -> int:
    """扫到点未确认事件，按 60 秒窗推进到下一枪或升级；返回本轮实际推进的条数。

    - 只推进 `triggered_by='scheduler'` 的事件：语音里主动说「我要吃药」
      （`triggered_by='dialog'`）由 `medication_confirmation_graph` 的 Interrupt 驱动，
      调度器再插一枪会变成对长者的重复打扰；
    - 每条事件**一个** `select` 聚合出已发次数与最后一枪时刻（`GROUP BY event_id`），
      不做 N+1；
    - 单条失败只记日志（`logger.exception`），不连坐整轮。
    """
    current = _as_utc(now)
    started = time.perf_counter()
    handled = 0

    try:
        decisions = await _collect_due_decisions(current)
    except Exception:
        logger.exception("medication due-scan 取事件失败 now=%s", current.isoformat())
        decisions = []

    for reminder_id, decision in decisions:
        try:
            if decision.action == "touch" and decision.attempt_no is not None:
                await run_medication_touch(
                    reminder_id=reminder_id,
                    attempt_no=decision.attempt_no,
                    now=current,
                    deliver=deliver,
                )
            elif decision.action == "escalate":
                await _escalate_event(reminder_id=reminder_id, now=current)
            else:
                # `wait` 不会进这个列表；`skip` = 计划已停用且事件一次都没触达，
                # 不触达、不升级，也不计入 handled（指标已在 decide_event_action 记过）。
                continue
        except Exception:
            logger.exception(
                "用药事件推进失败 reminder_id=%s action=%s", reminder_id, decision.action
            )
            continue
        handled += 1

    counter("medication_due_scan_runs_total")
    observe_ms("medication_due_scan_ms", (time.perf_counter() - started) * 1000.0)
    if handled:
        logger.info("medication due-scan handled=%s now=%s", handled, current.isoformat())
    return handled


async def _collect_due_decisions(
    current: datetime.datetime,
) -> list[tuple[uuid.UUID, EventDecision]]:
    """一轮扫描的判定结果：只返回 `touch` / `escalate` / `skip`，`wait` 不进列表。

    药单状态用一次 `outerjoin` 带出来（不是按事件 N+1 查）：`decide_event_action` 要靠
    它判「计划停了、这条还没触达 → skip」。用 outer 而不是 inner join：计划行缺失时
    宁可当作「未知、照常走安全网」，也不能让事件从扫描里静默消失。
    """
    from app.services import medication_service

    attempts = medication_service.max_attempts()
    wait_seconds = medication_service.attempt_wait_seconds()
    interval_minutes = medication_service.RETRY_SPACING_MINUTES

    async with db_session_factory() as session:
        rows = (
            await session.execute(
                select(
                    MedicationEvent.reminder_id,
                    MedicationEvent.scheduled_at,
                    MedicationPlan.status,
                )
                .select_from(MedicationEvent)
                .outerjoin(MedicationPlan, MedicationPlan.plan_id == MedicationEvent.plan_id)
                .where(
                    MedicationEvent.status.in_(_OPEN_EVENT_STATUSES),
                    MedicationEvent.triggered_by == "scheduler",
                    MedicationEvent.scheduled_at <= current,
                )
                .order_by(MedicationEvent.scheduled_at, MedicationEvent.reminder_id)
                .limit(DUE_SCAN_BATCH_LIMIT)
            )
        ).all()
        if not rows:
            return []
        stats = await _attempt_stats(session, [row[0] for row in rows])

    decisions: list[tuple[uuid.UUID, EventDecision]] = []
    for reminder_id, scheduled_at, plan_status in rows:
        attempts_sent, last_sent_at = stats.get(reminder_id, (0, None))
        decision = decide_event_action(
            scheduled_at=scheduled_at,
            attempts_sent=attempts_sent,
            last_sent_at=last_sent_at,
            now=current,
            attempts=attempts,
            wait_seconds=wait_seconds,
            interval_minutes=interval_minutes,
            plan_status=plan_status,
        )
        if decision.action == "wait":
            continue
        decisions.append((reminder_id, decision))
    return decisions


async def _attempt_stats(
    session: AsyncSession, reminder_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, tuple[int, datetime.datetime | None]]:
    """一次分组查询拿到「已发几枪 + 最后一枪时刻」，避免按事件 N+1。"""
    if not reminder_ids:
        return {}
    rows = (
        await session.execute(
            select(
                MedicationAttempt.event_id,
                func.count(),
                func.max(MedicationAttempt.sent_at),
            )
            .where(MedicationAttempt.event_id.in_(list(reminder_ids)))
            .group_by(MedicationAttempt.event_id)
        )
    ).all()
    return {row[0]: (int(row[1]), row[2]) for row in rows}


async def _escalate_event(*, reminder_id: uuid.UUID, now: datetime.datetime) -> None:
    """四次无响应 → 红线工单 + 派护工通知 + 终态归因（同事务，要么都成、要么都不成）。"""
    from app.services import medication_service

    async with db_session_factory() as session:
        event = await medication_service.load_event(session, reminder_id)
        if event.status in _CLOSED_EVENT_STATUSES:
            logger.info(
                "用药事件已闭环，跳过升级 reminder_id=%s status=%s", reminder_id, event.status
            )
            return
        order = await _svc_escalate(session, reminder_id=reminder_id)
        assignee = await _primary_care_staff(session, event.elder_id)
        # 归因与建单同事务：`escalate_to_work_order` 先落默认的
        # `escalated + missed_unconfirmed`（图与离线恢复两条路径共用的默认值），
        # 这里再按**真实回执**覆盖成 PRD:323/324 的口径——四次一次回执都没有时，
        # 它就是 `notification_failed`，而不是「有人收到却没确认」。
        terminal_state, failure_reason = medication_service.escalation_attribution(
            await _attempt_receipts(session, reminder_id)
        )
        await _svc_update_state(
            session,
            reminder_id=reminder_id,
            terminal_state=terminal_state,
            failure_reason=failure_reason,
        )
        await _svc_notify_escalation(
            session,
            elder_id=event.elder_id,
            plan_id=event.plan_id,
            reminder_id=event.reminder_id,
            scheduled_at=event.scheduled_at,
            order_no=getattr(order, "order_no", None),
            assignee_staff_id=assignee,
        )
        await session.commit()

    counter("medication_escalation_total")
    logger.warning(
        "medication escalated to care staff reminder_id=%s elder_id=%s assignee=%s "
        "terminal_state=%s failure_reason=%s now=%s",
        reminder_id,
        event.elder_id,
        assignee,
        terminal_state,
        failure_reason,
        _as_utc(now).isoformat(),
    )


async def _attempt_receipts(session: AsyncSession, reminder_id: uuid.UUID) -> list[str | None]:
    """该事件已落库的触达回执（按触达序号，升级归因的唯一可靠依据）。

    与 `_attempt_stats` 分开查而不是扩成一个查询：归因发生在升级那一刻、每个事件一次，
    而 `_attempt_stats` 是每轮扫描对整批事件跑的热路径，不该为一次归因多带一列。
    """
    rows = (
        await session.execute(
            select(MedicationAttempt.receipt)
            .where(MedicationAttempt.event_id == reminder_id)
            .order_by(MedicationAttempt.attempt_no)
        )
    ).all()
    return [row[0] for row in rows]


async def _primary_care_staff(session: AsyncSession, elder_id: uuid.UUID) -> uuid.UUID | None:
    """该长者的主责护工（与 `app/scheduler/health.py` 同一读口径）。"""
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


# ---------------------------------------------------------------------------
# 服务层适配块（服务签名若变，只改这里）
# ---------------------------------------------------------------------------


async def _svc_ensure_event(
    session: AsyncSession, *, plan: MedicationPlan, scheduled_at: datetime.datetime
) -> tuple[MedicationEvent, bool]:
    """`medication_service.ensure_event(session, *, plan_id, elder_id, scheduled_at,
    drug_snapshot, triggered_by='scheduler') -> (event, created)`。只 flush，不 commit。"""
    from app.services import medication_service

    return await medication_service.ensure_event(
        session,
        plan_id=plan.plan_id,
        elder_id=plan.elder_id,
        scheduled_at=scheduled_at,
        drug_snapshot=_plan_snapshot(plan),
        triggered_by="scheduler",
    )


async def _svc_record_attempt(
    session: AsyncSession,
    *,
    reminder_id: uuid.UUID,
    attempt_no: int,
    sent_at: datetime.datetime,
    receipt: str,
) -> MedicationAttempt:
    """`record_attempt(session, *, event_id, attempt_no, sent_at, receipt, tts_result, ...)`。"""
    from app.services import medication_service

    return await medication_service.record_attempt(
        session,
        event_id=reminder_id,
        attempt_no=attempt_no,
        sent_at=sent_at,
        receipt=receipt,
        tts_result=_tts_result_for(receipt),
    )


async def _svc_update_state(
    session: AsyncSession,
    *,
    reminder_id: uuid.UUID,
    status: str | None = None,
    retry_count: int | None = None,
    terminal_state: str | None = None,
    failure_reason: str | None = None,
) -> MedicationEvent:
    """`update_event_state(session, *, reminder_id, status, terminal_state, failure_reason,
    retry_count)`：只改传入的非 `None` 字段（服务层把 `None` 当「本次不动这一列」）。"""
    from app.services import medication_service

    return await medication_service.update_event_state(
        session,
        reminder_id=reminder_id,
        status=status,
        retry_count=retry_count,
        terminal_state=terminal_state,
        failure_reason=failure_reason,
    )


async def _svc_escalate(session: AsyncSession, *, reminder_id: uuid.UUID) -> object:
    """`escalate_to_work_order(session, *, event, reason)`：建红线工单 + 事件置 escalated。"""
    from app.services import medication_service

    event = await medication_service.load_event(session, reminder_id)
    return await medication_service.escalate_to_work_order(session, event=event)


async def _svc_notify_escalation(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    plan_id: uuid.UUID,
    reminder_id: uuid.UUID,
    scheduled_at: datetime.datetime,
    order_no: str | None,
    assignee_staff_id: uuid.UUID | None,
) -> None:
    """`notification_service.create_notification(session, *, kind, severity, elder_id,
    payload, assignee_staff_id)`：kind 固定 `medication_escalation`（该 kind 已存在）。

    去重键取 `(plan_id, scheduled_at)`（FR-ALT-04 计划 §2.3）：同一张药单的同一次
    提醒再怎么重放（任务重入、恢复分支重跑），也只该有一条升级通知。人工重发走
    `resend` 链，不复用这个入口，所以不会被拦住。
    """
    await notification_service.create_notification(
        session,
        kind="medication_escalation",
        severity="red",
        elder_id=elder_id,
        assignee_staff_id=assignee_staff_id,
        dedupe_key=f"medication_reminder:{plan_id}:{_as_utc(scheduled_at).isoformat()}",
        payload={
            "module": "medication",
            "state": "escalated",
            "plan_id": str(plan_id),
            "reminder_id": str(reminder_id),
            "work_order_no": order_no,
            "scheduled_at": _as_utc(scheduled_at).isoformat(),
            "summary": "用药提醒四次触达（T0/+5/+10/+15）均未确认，已派护工跟进",
        },
    )


__all__ = [
    "DUE_SCAN_BATCH_LIMIT",
    "SCAN_INTERVAL_MINUTES",
    "SCHEDULE_PATTERN",
    "Deliver",
    "EventDecision",
    "MedicationTouchTarget",
    "decide_event_action",
    "parse_schedule_cron",
    "register_medication_jobs",
    "run_medication_due_scan",
    "run_medication_review_scan",
    "run_medication_scan",
    "run_medication_touch",
    "scheduled_at_for",
]
