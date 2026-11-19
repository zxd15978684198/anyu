"""通知领域服务：四类通知 × 六状态状态机（PRD §4.7 / 技术设计文档 §3.6）。

本文件是 `notifications` 表**唯一**的写库入口（AGENTS §2 分层硬约束），也承载
状态机的全部裁决——API 层不许自己判 `status` 能不能跳。

## 状态机（口径来源：PRD §4.7 正文「generated → pending → delivered → acknowledged / failed」）

```
generated ─┬─> pending ─┬─> delivered ─┬─> acknowledged  (终态)
           │             │              ├─> failed        (终态)
           ├─> delivered ─┘              └─> simulated     (终态)
           ├─> simulated (终态)
           └─> failed    (终态)
```

- `delivered` 从 `generated` 直达是**正当**的：站内通知没有「排队中」这一步时，
  生成即投递（迭代 8 的 health_review / collection_missed 都走这条）。
- `simulated` 是「模拟推送」分支的终态：本仓不接微信/短信（AGENTS §4.5），
  演示环境的「推送」只落状态，不再往下走 ack。
- 终态（acknowledged / failed / simulated）**不得再跳**；任何表外跳转一律
  `InvalidTransitionError`，由 API 路由映射成 409 `invalid_transition`。

## 可见范围（PRD FR-ALT-02「按角色可见范围」）

admin 全量；`care_worker` 只认 `relation='primary_care'`、`doctor` 只认
`relation='doctor'`（与 `elder_service` 同源同一张 `elder_staff_assignments`），
另外**派给本人**的通知恒可见（工单式定向投递，`elder_id` 为空也成立）。
非员工主体（家属 / 设备 / 工牌）读通知一律 403：通知中心是照护台能力。

## FR-MED-08 断网三层兜底的**服务端段**（②窗口处置 / ③掉线通知 / `offline` 归因）

PRD:338 的三层里，端侧（IndexedDB + Service Worker + `speechSynthesis`）在
`frontend/public/sw.js` 与 `frontend/src/pad/offline/`；这里只做「网络恢复后」与
「掉线期间」两件事：

- `decide_offline_recovery`（纯函数）：按 PRD §4.5 的 T0/+5/+10/+15 四次触达节拍判
  窗口内是否还允许补一枪。窗口内 → `callback`（补呼**一**次，不集中补播，PRD:326）；
  超窗 → `work_order`（PRD:338「超窗直接生成工单」）；最后一枪还在等 60s → `wait`
  （不许抢跑升级）。
- `recover_after_offline`：把判决落库。**写库一律委托 `medication_service`**——
  建单走 `escalate_to_work_order`（唯一工单构造器），归因走 `update_event_state`，
  本文件不直接碰用药三张表的 ORM，也不新增事件态/表/迁移。
- `notify_offline_overlap`：掉线与用药窗口重叠时立刻给主责护工建红档
  `medication_escalation` 通知（PRD:338 第③层），同一 `reminder_id` 同状态只建一次。
- `OFFLINE_FAILURE_REASON`：`notification_failed` 的归因值，与 `denied` 区分
  「设备没播出来」和「长者说不吃」（PRD:338 末尾）。`failure_reason` 的 CHECK 早在
  0009 迁移里放行该值，**不需要迁移**。

补呼为什么不在本文件里发：出站只有一处实现（`app/scheduler/medication.run_medication_touch`），
再写一条外呼路径就是 AGENTS §4.8 的「第二种同类实现」；而在这里记一条
`receipt='none'` 的假触达会污染 PRD:324 的回执口径。所以 `callback` 只回判决，发送由
接线层复用既有任务体完成（幂等键 `(event_id, attempt_no)` 保证重放安全）。

## FR-RPT-03 日报推送（`family_report` + 1/5/15 重试，服务端段）

`family_report` 是 0012 迁移就放行的 `kind`，但直到本卡才第一次真的有人建它。
三条纪律：

- **收件人只按绑定关系算**：一次推送 = 一条 `family_report` 通知，挂在 `elder_id` 上，
  由 `_visibility_filters` 把「这个长者的所有 `elder_guardians.status='active'`」放行
  读它。所以收件人列表是**读**出来的（`notify_daily_report` 里查一次
  `elder_guardians`），也不写第二张表、也不逐家属复制 N 条（PRD:520 的「只发一次」
  按长者算：三个家属看的是同一条）。一个 active 家属都没有就不建——建一条没人能读的
  通知等于假装送达。
- **重试簿记只在 `payload.delivery` 里**（`attempts` / `anchor_at` / `next_retry_at` /
  `last_error`），照 `escalate_unacknowledged` 与 `resend` 的既有手法：`notifications`
  没有重试列，为一个 P0 缺口加迁移不值当，而 JSONB 里放投递簿记本仓已有两处先例。
  1/5/15 三枪的偏移相对**创建时刻**（`anchor_at`）算，不相对上一枪算——重试链的
  时间点因此可预测，也不会因为一次调度延迟把整条链往后推。
- **失败不伪造送达**：三枪都没成功 → `status='failed'` + 一条 `audit_logs`
  （`actor_type='system'`），绝不写 `delivered`。`simulated` 也不写：那是「本来就
  只模拟」的终态，而这里的失败是真的没送出去，混用会让照护台以为家属看过了。

推送动作本身是**可注入**的 `DailyReportSender`：本仓不接微信/短信（AGENTS §4.5），
默认实现立刻成功（即「模拟推送成功」），失败路径靠注入抛异常的 sender 才有测试价值。
与 `medication_delivery` 同一个思路：通道在装配根注入，服务层只认协议。
"""

from __future__ import annotations

import datetime
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import DateTime, case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import RoleForbiddenError
from app.core.context import get_trace_id
from app.core.runtime_context import Identity
from app.core.security import KIND_DEVICE, KIND_GUARDIAN, KIND_STAFF
from app.models import (
    AuditLog,
    Elder,
    ElderGuardian,
    ElderStaffAssignment,
    MedicationAttempt,
    MedicationEvent,
    Notification,
)

logger = logging.getLogger(__name__)

#: `kind` 枚举（与 0020 迁移的 CHECK 逐字一致，别在别处再抄一遍）。
#: `safety_alert` 是 迭代 8 加的：安全规则命中后由 alert_service 走这一个入口建单，
#: 不复用 `health_review`（那是采集流程的）或 `medication_escalation`（那是漏服的）。
#: `medication_review` 是 0020 加的：药单到期前 7 天复核 / 已过期未续催办，
#: 由 `medication_service.notify_plan_reviews` 建单（也不复用上面两个）。
#: `attention_signal` 是 0021 加的：四信号「建议关注」每日扫描命中后建单，
#: 由 `app/scheduler/attention.py` 调本入口（与上面几档都不同源，别复用）。
NOTIFICATION_KINDS: tuple[str, ...] = (
    "health_review",
    "medication_escalation",
    "medication_review",
    "collection_missed",
    "family_report",
    "safety_alert",
    "attention_signal",
    "work_order",
)

#: `severity` 三枚举（PRD §4.6 绿/黄/红）。
NOTIFICATION_SEVERITIES: tuple[str, ...] = ("red", "yellow", "green")

#: `status` 六枚举。
NOTIFICATION_STATUSES: tuple[str, ...] = (
    "generated",
    "pending",
    "delivered",
    "acknowledged",
    "failed",
    "simulated",
)

#: 已「关单」的状态：acknowledged = 人处理过，failed = 投递失败已归因。
#: 去重只看**还开着**的单（HLT-03）：已关掉的历史单不该拦住同一天新的复核。
CLOSED_NOTIFICATION_STATUSES: tuple[str, ...] = ("acknowledged", "failed")

#: 幂等去重键长度上限（FR-ALT-04）：与 0022 迁移的 `varchar(120)` 逐字一致。
#: 超长不截断（截断会把两条不同业务键压成同一条），直接拒绝。
MAX_DEDUPE_KEY_LENGTH: Final[int] = 120

#: 去重键的部分唯一索引名（并发撞键时靠它区分「是去重命中」还是「别的约束冲突」）。
DEDUPE_NOTIFICATION_INDEX: Final[str] = "uq_notifications_dedupe_key"

#: 状态迁移表：**唯一**的合法性定义。空集合 = 终态。
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "generated": frozenset({"pending", "delivered", "simulated", "failed"}),
    "pending": frozenset({"delivered", "simulated", "failed"}),
    "delivered": frozenset({"acknowledged", "simulated", "failed"}),
    "acknowledged": frozenset(),
    "failed": frozenset(),
    "simulated": frozenset(),
}

#: 角色 → assignment relation（与 `elder_service._RELATION_BY_ROLE` 同源；
#: 两处都只是这张表的投影，改口径必须同时改）。
_RELATION_BY_ROLE: dict[str, str] = {"care_worker": "primary_care", "doctor": "doctor"}


class NotificationServiceError(Exception):
    """通知领域错误基类：只表达「这次请求被拒」，不构造 HTTP 响应。"""


class NotificationNotFoundError(NotificationServiceError):
    """通知不存在，或不在当前主体的可见范围内（不区分，避免探测存在性）。"""


class InvalidTransitionError(NotificationServiceError):
    """状态机非法跳转（含对终态的任何再迁移）。"""

    def __init__(self, from_status: str, to_status: str) -> None:
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(f"status transition {from_status} -> {to_status} is not allowed")


class NotificationValidationError(NotificationServiceError):
    """入参枚举越界（kind / severity / status）。"""


class NotificationScopeForbiddenError(NotificationServiceError):
    """端侧回执的目标通知不属于该设备绑定的长者（越权，不泄露存在性）。"""


@dataclass(frozen=True, slots=True)
class NotificationItem:
    """列表项：通知中心一行。`payload` 是展示内容，脱敏由调用方负责。"""

    notification_id: uuid.UUID
    elder_id: uuid.UUID | None
    kind: str
    severity: str
    status: str
    payload: dict[str, Any] | None
    assignee_staff_id: uuid.UUID | None
    delivered_at: datetime.datetime | None
    acknowledged_at: datetime.datetime | None
    created_at: datetime.datetime


@dataclass(frozen=True, slots=True)
class NotificationPage:
    items: list[NotificationItem]
    total: int


def _item(notification: Notification) -> NotificationItem:
    return NotificationItem(
        notification_id=notification.notification_id,
        elder_id=notification.elder_id,
        kind=notification.kind,
        severity=notification.severity,
        status=notification.status,
        payload=notification.payload,
        assignee_staff_id=notification.assignee_staff_id,
        delivered_at=notification.delivered_at,
        acknowledged_at=notification.acknowledged_at,
        created_at=notification.created_at,
    )


def _staff_uuid(identity: Identity) -> uuid.UUID:
    """员工身份里的 staff_id 已被 `Identity` 保证为规范 UUID 文本。"""
    if identity.staff_id is None:  # pragma: no cover - Identity 构造时已保证
        raise RoleForbiddenError("staff identity is malformed")
    return uuid.UUID(identity.staff_id)


def _scope_relation(identity: Identity) -> str | None:
    """返回 None = admin 全量；否则是要匹配的 assignment relation。

    非员工主体一律拒绝：通知中心只对 `staff` 会话开放（工牌是单会话凭证，
    不等于照护台登录态，S-24）。
    """
    if identity.kind != KIND_STAFF:
        raise RoleForbiddenError("notifications are only readable by staff sessions")
    if identity.role == "admin":
        return None
    relation = _RELATION_BY_ROLE.get(identity.role or "")
    if relation is None:
        # 角色枚举已由 0002 迁移的 CHECK 与 task-4 `_require_role` 双重保证。
        raise RoleForbiddenError("staff role has no notification scope")
    return relation


async def _visibility_filters(session: AsyncSession, identity: Identity) -> list[Any]:
    """列表与详情共用的可见范围谓词；admin 返回空列表（不过滤）。

    三类主体各有各的范围来源，**都不要在这里合并成一个条件表**：

    - staff：`elder_staff_assignments` 按角色 relation + 定向派给自己的；
    - guardian（W4）：`elder_guardians.status='active'` 绑定的长者；
      家属白名单只有「健康日报与需关注事件摘要」（PRD 177），所以这里只放宽
      **读**通知，`deliver`/`acknowledge` 仍由 `_require_staff` 挡住——
      能看见不等于能替护工确认；
    - device / staff_card：一律 403（工牌是单会话凭证，S-24）。
    """
    if identity.kind == KIND_GUARDIAN:
        guardian_id = uuid.UUID(identity.guardian_id or "")
        return [
            Notification.elder_id.in_(
                select(ElderGuardian.elder_id).where(
                    ElderGuardian.guardian_id == guardian_id,
                    ElderGuardian.status == "active",
                )
            )
        ]

    relation = _scope_relation(identity)
    if relation is None:
        return []
    staff_id = _staff_uuid(identity)
    return [
        or_(
            Notification.elder_id.in_(
                select(ElderStaffAssignment.elder_id).where(
                    ElderStaffAssignment.staff_id == staff_id,
                    ElderStaffAssignment.relation == relation,
                )
            ),
            Notification.assignee_staff_id == staff_id,
        )
    ]


def _require_staff(identity: Identity, *, action: str) -> None:
    """投递与确认是**护工侧动作**，家属与设备一律 403。

    这条与范围过滤是两件事：家属能读到自己长者的通知（`_visibility_filters`），
    但「替这户人家确认已处理」必须由照护台的人来做——否则家属点一下就能把
    一条红色事件关掉，工单闭环与审计归因都会失真（PRD §4.6 / AC-24）。
    """
    if identity.kind != KIND_STAFF:
        raise RoleForbiddenError(f"notification {action} is only available to staff sessions")


async def create_notification(
    session: AsyncSession,
    *,
    kind: str,
    severity: str,
    elder_id: uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
    assignee_staff_id: uuid.UUID | None = None,
    dedupe_key: str | None = None,
) -> Notification:
    """生成一条通知（初始状态 `generated`），由调用方决定是否立刻投递。

    `dedupe_key`（FR-ALT-04）：调用方给一个**业务摘要**（如
    `medication_reminder:<plan_id>:<scheduled_at>`），同 `elder_id + kind + dedupe_key`
    下只允许一行（库级部分唯一索引 `uq_notifications_dedupe_key` 兜底）。

    - 命中已有行：**返回原行**、不新建、不报 409，返回对象上带 `deduplicated=True`；
    - 并发：两个请求同时查不到就同时插入，后到的那个被唯一索引挡住（`IntegrityError`），
      这里 rollback 掉这一行再把原行查回来返回——调用方无感；
    - 不传 `dedupe_key`（或 `elder_id` 为 None）：行为与改造前完全一致（不去重）。
      系统级通知没有长者维度，`(elder_id, kind, dedupe_key)` 对它不成立，所以不参与去重。

    `deduplicated` 是**返回对象上的临时属性**，不是表里的列：ORM 实例允许挂任意属性，
    而改成返回包装类会打断全部现有调用方（12 处把它们当 ORM 用）。
    """
    if kind not in NOTIFICATION_KINDS:
        raise NotificationValidationError(f"unknown notification kind {kind!r}")
    if severity not in NOTIFICATION_SEVERITIES:
        raise NotificationValidationError(f"unknown notification severity {severity!r}")

    key = _normalise_dedupe_key(dedupe_key)
    if key is not None and elder_id is not None:
        existing = await _find_by_dedupe_key(session, elder_id=elder_id, kind=kind, dedupe_key=key)
        if existing is not None:
            return _mark_deduplicated(existing, kind=kind, dedupe_key=key)

    notification = Notification(
        elder_id=elder_id,
        kind=kind,
        severity=severity,
        payload=payload,
        assignee_staff_id=assignee_staff_id,
        dedupe_key=key,
    )
    try:
        async with session.begin_nested():
            session.add(notification)
    except IntegrityError as exc:
        # 唯一索引兜底并发：可能是 dedupe 撞键，也可能是别的约束（比如未来的 CHECK）。
        # 只处理去重键，其余原样上抛，不做「悄悄吞掉」。
        if key is None or elder_id is None or not _is_dedupe_conflict(exc):
            raise
        _expunge_quietly(session, notification)
        existing = await _find_by_dedupe_key(session, elder_id=elder_id, kind=kind, dedupe_key=key)
        if existing is None:  # pragma: no cover - 唯一索引报错却查不到原行，说明库里被删了
            raise
        return _mark_deduplicated(existing, kind=kind, dedupe_key=key)

    # status / created_at 都是 server_default，必须 refresh 才读得到。
    await session.refresh(notification)
    notification.deduplicated = False
    logger.info(
        "notification created",
        extra={
            "notification_id": str(notification.notification_id),
            "kind": kind,
            "severity": severity,
            "elder_id": str(elder_id) if elder_id is not None else None,
            "dedupe_key": key,
        },
    )
    return notification


def _normalise_dedupe_key(dedupe_key: str | None) -> str | None:
    """去除首尾空白；空串视作没传。超长直接拒绝（列是 varchar(120)，静默截断会撞键）。"""
    if dedupe_key is None:
        return None
    key = str(dedupe_key).strip()
    if not key:
        return None
    if len(key) > MAX_DEDUPE_KEY_LENGTH:
        raise NotificationValidationError(
            f"dedupe_key too long ({len(key)} > {MAX_DEDUPE_KEY_LENGTH})"
        )
    return key


async def _find_by_dedupe_key(
    session: AsyncSession, *, elder_id: uuid.UUID, kind: str, dedupe_key: str
) -> Notification | None:
    result = await session.execute(
        select(Notification)
        .where(
            Notification.elder_id == elder_id,
            Notification.kind == kind,
            Notification.dedupe_key == dedupe_key,
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


def _mark_deduplicated(notification: Notification, *, kind: str, dedupe_key: str) -> Notification:
    notification.deduplicated = True
    logger.info(
        "notification deduplicated",
        extra={
            "notification_id": str(notification.notification_id),
            "kind": kind,
            "dedupe_key": dedupe_key,
        },
    )
    return notification


def _is_dedupe_conflict(exc: IntegrityError) -> bool:
    """只认本表的去重索引；`order_no` 那类冲突交给调用方自己的重试口径。"""
    return DEDUPE_NOTIFICATION_INDEX in str(exc.orig)


def _expunge_quietly(session: AsyncSession, obj: Any) -> None:
    """savepoint 回滚后把失败的 pending 对象踢出 session，避免污染后续 flush。"""
    try:
        if obj in session:
            session.expunge(obj)
    except Exception:  # pragma: no cover - expunge 本身失败不影响主流程
        logger.debug("expunge failed for %r", type(obj).__name__)


async def find_open_daily_notification(
    session: AsyncSession,
    *,
    kind: str,
    elder_id: uuid.UUID,
    business_date: datetime.date,
) -> Notification | None:
    """同一天同一类通知里**还开着**的那一条（HLT-03 的去重键）。

    去重键是 `(kind, elder_id, payload.record_date)`：`record_date` 是业务日期（长者
    哪一天的健康事实），而 `created_at` 是入库时刻——用后者会在跨零点补录时把同一天的
    事实拆成两条单。业务日期本仓一直放在 `payload.record_date`（`_create_review_notification`
    与日报都这么读），这里直接按同一个位置查，不新增列。

    只认未关闭的状态：`acknowledged` / `failed` 是关单，历史单不该拦住新的复核
    （否则同一长者当天第二次异常会永远静默）。
    """
    if kind not in NOTIFICATION_KINDS:
        raise NotificationValidationError(f"unknown notification kind {kind!r}")
    result = await session.execute(
        select(Notification)
        .where(
            Notification.kind == kind,
            Notification.elder_id == elder_id,
            Notification.status.notin_(CLOSED_NOTIFICATION_STATUSES),
            Notification.payload["record_date"].astext == business_date.isoformat(),
        )
        .order_by(Notification.created_at.asc(), Notification.notification_id.asc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def escalate_severity(
    session: AsyncSession,
    notification: Notification,
    *,
    reason: str,
    evidence: Mapping[str, Any] | None = None,
) -> Notification:
    """把一条**未关闭**的通知升到红档，并在 payload 里留升级痕迹（HLT-03）。

    为什么「升」而不是「新建一条红的」：同一天同一类复核只该有**一条**待办。
    新建一条会留下两张待办，护工关掉旧的仍看到新的；而只改 severity 又不留痕，
    事后回查就分不清「一开始就是红」还是「黄档补录后升红」。所以两个动作一起做：
    `severity -> red` + `payload.escalated_from / escalated_to / escalated_at /
    escalation_reason`，原始证据（新记录的 record_id / revision）也一并写进去。

    幂等：已经是红档就原样返回（重复补录红值不该反复改写 payload 时间戳）。
    """
    if notification.severity == "red":
        return notification
    if notification.status in CLOSED_NOTIFICATION_STATUSES:
        raise InvalidTransitionError(notification.status, notification.status)

    moment = datetime.datetime.now(datetime.UTC)
    payload: dict[str, Any] = dict(notification.payload or {})
    payload.update(
        {
            "escalated_from": notification.severity,
            "escalated_to": "red",
            "escalated_at": moment.isoformat(),
            "escalation_reason": reason,
        }
    )
    if evidence:
        payload.update(dict(evidence))
    notification.severity = "red"
    notification.payload = payload
    await session.flush()
    await session.refresh(notification)
    logger.info(
        "notification escalated",
        extra={
            "notification_id": str(notification.notification_id),
            "kind": notification.kind,
            "escalated_from": payload["escalated_from"],
            "reason": reason,
            "elder_id": str(notification.elder_id) if notification.elder_id else None,
        },
    )
    return notification


def _transition(
    notification: Notification,
    target: str,
    *,
    now: datetime.datetime | None = None,
) -> None:
    """按迁移表校验并落状态；时间戳与状态在同一处更新，避免只改一半。

    `now` 只给测试与到点任务用：调度器判定用的是它自己注入的时刻，落库时间戳
    必须与之同源，否则 120s / 10s 这两条边界只能靠 sleep 去测。
    """
    if target not in _ALLOWED_TRANSITIONS.get(notification.status, frozenset()):
        raise InvalidTransitionError(notification.status, target)
    notification.status = target
    moment = _moment(now)
    if target in ("delivered", "simulated"):
        notification.delivered_at = moment
    elif target == "acknowledged":
        notification.acknowledged_at = moment


async def _load_visible(
    session: AsyncSession, identity: Identity, notification_id: uuid.UUID
) -> Notification:
    """按可见范围取一条通知；取不到即 404（范围外不泄露存在性）。"""
    notification = (
        await session.execute(
            select(Notification).where(
                Notification.notification_id == notification_id,
                *(await _visibility_filters(session, identity)),
            )
        )
    ).scalar_one_or_none()
    if notification is None:
        raise NotificationNotFoundError(str(notification_id))
    return notification


async def deliver(
    session: AsyncSession,
    identity: Identity,
    notification_id: uuid.UUID,
    *,
    simulated: bool = False,
) -> NotificationItem:
    """推送尝试：generated/pending → pending，并记 `payload.pushed_at`。

    FR-ALT-03 之后「送达」不再由服务端宣布：本仓不接任何真实推送通道
    （AGENTS §4.5），这里能证明的只有「已尝试推送」。真正落到端上要等设备
    回执（`receipt`）把状态推到 `delivered`。`simulated=True` 是演示终态分支，
    保持旧语义不动（终态不再接受回执）。
    """
    _require_staff(identity, action="delivery")
    notification = await _load_visible(session, identity, notification_id)
    if simulated:
        _transition(notification, "simulated")
    else:
        moment = _moment(None)
        if notification.status == "generated":
            _transition(notification, "pending", now=moment)
        elif notification.status != "pending":
            # delivered/acknowledged/failed/simulated 都是「已经过了推送点」的状态，
            # 再点一次推送不会把它们拉回 pending——那样会把已回执的通知重开。
            raise InvalidTransitionError(notification.status, "pending")
        payload = dict(notification.payload or {})
        payload["pushed_at"] = moment.isoformat()
        notification.payload = payload
    await session.flush()
    await session.refresh(notification)
    logger.info(
        "notification push attempted",
        extra={
            "notification_id": str(notification.notification_id),
            "status": notification.status,
            "simulated": simulated,
        },
    )
    return _item(notification)


async def acknowledge(
    session: AsyncSession, identity: Identity, notification_id: uuid.UUID
) -> NotificationItem:
    """护工/医生确认：delivered → acknowledged。其他来源态一律 409。

    「确认」是人工闭环，只能由照护台员工产生；端侧回执只到 `delivered` 为止。
    """
    _require_staff(identity, action="acknowledgement")
    notification = await _load_visible(session, identity, notification_id)
    _transition(notification, "acknowledged")
    await session.flush()
    await session.refresh(notification)
    logger.info(
        "notification acknowledged",
        extra={"notification_id": str(notification.notification_id)},
    )
    return _item(notification)


async def receipt(
    session: AsyncSession,
    identity: Identity,
    notification_id: uuid.UUID,
) -> NotificationItem:
    """端侧回执：pending → delivered，`delivered_at` 以此为送达时刻（FR-ALT-03）。

    三条边界都是刻意的：

    - **只认设备会话**：回执是「这台绑在该长者房间的设备真的渲染了」，员工代点
      与家属代点都不算（`kind=device`，且设备必须绑着同一个 elder）；
    - **不为越权泄露存在性**：通知存在但不属于该设备的长者 → 403，与「不存在」
      的 404 分开，但不回显通知内容；
    - **幂等**：已 delivered / acknowledged 再回执返回 200 原状态，绝不把
      acknowledged 退回 delivered（人工确认不能被端侧重放打回）。
    """
    if identity.kind != KIND_DEVICE:
        raise RoleForbiddenError("notification receipts are only accepted from device sessions")
    notification = (
        await session.execute(
            select(Notification).where(Notification.notification_id == notification_id)
        )
    ).scalar_one_or_none()
    if notification is None:
        raise NotificationNotFoundError(str(notification_id))
    if notification.elder_id is None or identity.elder_id != str(notification.elder_id):
        raise NotificationScopeForbiddenError(str(notification_id))

    if notification.status in ("delivered", "acknowledged"):
        return _item(notification)
    if notification.status != "pending":
        # generated（还没推）/ simulated（演示终态）/ failed 都不接受回执。
        raise InvalidTransitionError(notification.status, "delivered")

    _transition(notification, "delivered")
    await session.flush()
    await session.refresh(notification)
    logger.info(
        "notification receipt accepted",
        extra={
            "notification_id": str(notification.notification_id),
            "elder_id": str(notification.elder_id),
        },
    )
    return _item(notification)


async def list_for_staff(
    session: AsyncSession,
    identity: Identity,
    *,
    page: int,
    page_size: int,
    status: str | None = None,
    kind: str | None = None,
    severity: str | None = None,
) -> NotificationPage:
    """通知列表：按角色可见范围过滤，**红档置顶**、同档按创建时间倒序。

    排序是「档位 → 时间倒序 → notification_id」，三者缺一不可：

    - 档位在前：红档是待处置事件（PRD §4.7「红色优先」），不能因为一条绿色通知
      比它晚 3 秒生成就压到第二页去；
    - 时间倒序：同档位内新的先看；
    - `notification_id` 兜底：同一秒内多条 `created_at` 完全相同时，没有稳定键
      两次查询的顺序会抖，评测会把它误读成「排序没生效」。

    分页仍是「先 count 再 limit/offset」，且 count 用同一组 `filters`（不含排序）
    ——**先分页后排序**会把置顶做废，这两步的先后顺序也是口径的一部分。

    名字里的 `staff` 是历史口径（迭代 8 只有照护台会用）；W4 起绑定家属也能读，
    范围换成 `elder_guardians` 的 active 绑定。**改名会牵动 API 与用例**，
    收益只是好看，等有第二个非员工主体再统一改。
    """
    if status is not None and status not in NOTIFICATION_STATUSES:
        raise NotificationValidationError(f"unknown notification status {status!r}")
    if kind is not None and kind not in NOTIFICATION_KINDS:
        raise NotificationValidationError(f"unknown notification kind {kind!r}")
    if severity is not None and severity not in NOTIFICATION_SEVERITIES:
        raise NotificationValidationError(f"unknown notification severity {severity!r}")

    filters = await _visibility_filters(session, identity)
    if status is not None:
        filters.append(Notification.status == status)
    if kind is not None:
        filters.append(Notification.kind == kind)
    # severity 必须在**分页之前**进 filters：count 与 rows 共用同一组 filters，
    # 否则 total 会是未筛总数，前端「加载更多」与空态判据全部失真
    # （家属端曾因此在红档位于后续页时显示「暂时没有需要您关注的事项」）。
    if severity is not None:
        filters.append(Notification.severity == severity)

    # 档位常量：red=0 / yellow=1 / 其余（green）=2。写在函数内而不是模块级，
    # 避免 import 期就构造 SQL 表达式（本模块被 scheduler / api 一起 import）。
    severity_rank = case(
        (Notification.severity == "red", 0),
        (Notification.severity == "yellow", 1),
        else_=2,
    )

    total = int(
        (
            await session.execute(select(func.count()).select_from(Notification).where(*filters))
        ).scalar_one()
    )
    rows = (
        (
            await session.execute(
                select(Notification)
                .where(*filters)
                .order_by(
                    severity_rank,
                    Notification.created_at.desc(),
                    Notification.notification_id,
                )
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return NotificationPage(items=[_item(row) for row in rows], total=total)


# ---------------------------------------------------------------------------
# 通知运维三件事：到点未确认升级（N30） · 重发（N31） · 10 秒内撤销（N32）
# ---------------------------------------------------------------------------
#
# 三件事都**不改表**：升级与重发的证据写进 `payload`（JSONB），撤销复用既有终态
# `failed` —— 不需要第五个状态，也不需要迁移。

ESCALATION_DELAY_SECONDS: Final[int] = 120
"""到点未确认多久升级（PRD §4.7：通知发出后 120 秒内未确认即升级）。"""

ESCALATION_BATCH_LIMIT: Final[int] = 200
"""单轮最多升级多少条：积压时一轮只吃一批，别让一次 tick 变成全表扫描。"""

_ESCALATION_CANDIDATE_STATUSES: Final[tuple[str, ...]] = ("pending", "delivered")
"""还能升级的两态：`generated` 还没发出去、终态已经闭环，都不该再升。"""

_ELDER_IN_HOUSE: Final[str] = "active"
"""房态判据（与 `app/scheduler/health.py:63` / `maintenance.py:74` 同一取值）。

调度侧的在场判据只有 `elders.status` 一个：人退房（away/archived）之后，房间里
的提醒、日报、漏采派单全都停下。**通知升级原来漏了这一条**——已经入队的通知在
长者离开后仍会往家属手机上抬档。收口放在派发侧（升级扫描每轮重问一次库，天然
覆盖「退房瞬间已入队的存量」），而不是退房时去作废 pending 通知：撤单要动退房
事务与通知状态机，而这里只是一条 WHERE。
"""

_SEVERITY_LADDER: Final[dict[str, str]] = {"green": "yellow", "yellow": "red", "red": "red"}
"""只抬一档；`red` 已在顶上，停在原档但仍记 `escalated_at`（幂等闸门照旧生效）。"""

RESEND_ROLES: Final[frozenset[str]] = frozenset({"admin", "care_worker"})
"""重发只给照护侧两个角色：医生能看能确认，重发是「再推一次」的运营动作。"""

_RESENDABLE_STATUSES: Final[tuple[str, ...]] = ("generated", "pending", "delivered")
"""还能重发的三态；`acknowledged` / `failed` / `simulated` 一律 409。"""

REVOKE_WINDOW_SECONDS: Final[int] = 10
"""撤销窗口（N32）：从「送达时间」起算 10 秒；没送达过则从创建时间起算。"""


class RevokeWindowExpiredError(NotificationServiceError):
    """过了撤销窗口。

    单独成类是为了让 API 层给出比 `invalid_transition` 更准的码：窗口过了是
    「晚了」，状态不对是「本来就不该撤」。两者塞进同一个码，照护台只能显示一句
    含糊的「操作不被允许」，护工也没法判断该重发还是该找管理员。
    """

    def __init__(
        self,
        *,
        notification_id: uuid.UUID,
        sent_at: datetime.datetime,
        now: datetime.datetime,
        window_seconds: int,
    ) -> None:
        self.notification_id = notification_id
        self.sent_at = sent_at
        self.now = now
        self.window_seconds = window_seconds
        super().__init__(
            f"notification {notification_id} can no longer be revoked: "
            f"{window_seconds}s window expired"
        )


def _moment(now: datetime.datetime | None) -> datetime.datetime:
    """把注入的时钟归一到 UTC；不注入就是此刻（naive 按 UTC 解释，与 `_utc` 同口径）。"""
    return datetime.datetime.now(datetime.UTC) if now is None else _utc(now)


def _payload_copy(notification: Notification) -> dict[str, Any]:
    """JSONB 列取出来的是**原对象**：必须 copy 后整体赋值，原地改 flush 检测不到。"""
    return dict(notification.payload or {})


async def escalate_unacknowledged(
    session: AsyncSession,
    *,
    now: datetime.datetime | None = None,
    delay_seconds: int = ESCALATION_DELAY_SECONDS,
    limit: int = ESCALATION_BATCH_LIMIT,
) -> list[uuid.UUID]:
    """把「发出 120s 还没人确认」的通知就地抬一档，返回被升级的通知 id。

    ## 为什么抬原行的 severity，而不是新建一条升级通知

    一行通知代表「这件事」。升级只是同一件事变得更紧急，不是第二件事：新建会让
    列表里出现两条一模一样的通知；而且护工确认了升级条之后原条仍停在 `delivered`
    ——同一条事件两个状态，列表与统计都会分叉。

    ## 幂等

    闸门是 `payload.escalated_at`（JSONB 键，不是表列，不需要迁移）：查重条件里带
    `payload->>'escalated_at' IS NULL`，写回与判定在同一事务。所以调度器重启、手工
    补跑、`--reload` 误触发都只升一次。

    ## `red` 怎么办

    `red` 已在顶上：不升档（`severity` 不变），但仍写 `escalated_at` 并记
    `escalation_count`。这样「红色也没人管」在审计里看得见，且下一轮不再重复处理。
    """
    moment = _moment(now)
    effective_delay = max(0, delay_seconds)
    cutoff = moment - datetime.timedelta(seconds=effective_delay)
    # 派单队列的取数顺序（每轮 `limit` 条）：红档先、同档新的先、`notification_id` 兜底。
    # 与 `list_for_staff` 的档位口径一致，但**故意不抽公共 helper**：这里是批处理取数，
    # 「先处理哪一批」将来可能因子表含义变化而单独调整（例如改成先清积压最久的），
    # 抽成参数化 helper 会把差异藏进开关里，改一处顺序时看不出另一处也被改。
    escalation_rank = case(
        (Notification.severity == "red", 0),
        (Notification.severity == "yellow", 1),
        else_=2,
    )
    candidate_conditions = (
        Notification.status.in_(_ESCALATION_CANDIDATE_STATUSES),
        Notification.created_at <= cutoff,
        Notification.payload["escalated_at"].astext.is_(None),
    )
    # 房态闸门：长者不在屋里（away/transferred/archived）就不升级、也不打
    # `escalated_at` 标记——「人不在」不是「已升级」，下次回来该升的还要能升。
    # `elder_id IS NULL` 的通知是定向派给某个人的，没有房间归属，照常升级。
    in_house = or_(
        Notification.elder_id.is_(None),
        Notification.elder_id.in_(select(Elder.elder_id).where(Elder.status == _ELDER_IN_HOUSE)),
    )
    rows = (
        (
            await session.execute(
                select(Notification)
                .where(*candidate_conditions, in_house)
                .order_by(
                    escalation_rank,
                    Notification.created_at.desc(),
                    Notification.notification_id,
                )
                .limit(max(1, limit))
            )
        )
        .scalars()
        .all()
    )
    # 被房态闸门挡下的条数：不打标记就不该悄悄消失，记进日志供运营核对
    # （「今天有 3 条没升」比「什么都没发生」更难误读）。多一条 count 查询，
    # 换来「跳过」在日志里可数，值得。
    skipped_away = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Notification)
                .where(*candidate_conditions, ~in_house)
            )
        ).scalar_one()
    )
    if skipped_away:
        logger.info(
            "notification escalation skipped away residents",
            extra={
                "skipped_away": skipped_away,
                "delay_seconds": effective_delay,
                "elder_status_not_in_house": _ELDER_IN_HOUSE,
            },
        )
    escalated: list[uuid.UUID] = []
    for notification in rows:
        previous = notification.severity
        payload = _payload_copy(notification)
        payload["escalated_at"] = moment.isoformat()
        payload["escalated_from"] = previous
        payload["escalation_reason"] = f"unacknowledged_{effective_delay}s"
        payload["escalation_count"] = int(payload.get("escalation_count") or 0) + 1
        notification.severity = _SEVERITY_LADDER.get(previous, "red")
        notification.payload = payload
        escalated.append(notification.notification_id)

    if escalated:
        await session.flush()
        logger.warning(
            "notification escalated",
            extra={
                "escalated_count": len(escalated),
                "delay_seconds": effective_delay,
                "notification_ids": [str(item) for item in escalated],
            },
        )
    return escalated


async def resend(
    session: AsyncSession,
    identity: Identity,
    notification_id: uuid.UUID,
    *,
    now: datetime.datetime | None = None,
) -> NotificationItem:
    """重发一条通知：**还是那一行**，只是再投一次。

    不许产生重复 notification 行（N31 的硬约束）：`payload.resend_count` 记重发
    次数，`delivered_at` 刷新成最近一次投递时间。重发是「没收到，再发一次」，
    不是「把已经结束的事重新打开」，所以三条终态一律 409。

    角色只放 admin / care_worker：医生能看、能确认，但重发属于照护排班侧的运营动作。
    """
    _require_staff(identity, action="resend")
    if (identity.role or "") not in RESEND_ROLES:
        raise RoleForbiddenError("notification resend is limited to admin and care_worker")
    moment = _moment(now)
    notification = await _load_visible(session, identity, notification_id)
    if notification.status not in _RESENDABLE_STATUSES:
        raise InvalidTransitionError(notification.status, "delivered")
    if notification.status == "delivered":
        # 已投递过：重发不换状态，只把「最近一次投递时间」推到现在。
        notification.delivered_at = moment
    else:
        _transition(notification, "delivered", now=moment)
    payload = _payload_copy(notification)
    payload["resend_count"] = int(payload.get("resend_count") or 0) + 1
    payload["last_resent_at"] = moment.isoformat()
    payload["last_resent_by"] = str(_staff_uuid(identity))
    notification.payload = payload
    await session.flush()
    await session.refresh(notification)
    logger.info(
        "notification resent",
        extra={
            "notification_id": str(notification.notification_id),
            "status": notification.status,
            "resend_count": payload["resend_count"],
        },
    )
    return _item(notification)


async def revoke(
    session: AsyncSession,
    identity: Identity,
    notification_id: uuid.UUID,
    *,
    reason: str | None = None,
    now: datetime.datetime | None = None,
) -> NotificationItem:
    """10 秒内撤销刚发出的通知：置 `failed`，同事务写一条审计。

    三条拒绝各自可辨：

    - 已确认（`acknowledged`）、已失败、已模拟 → `InvalidTransitionError`
      → 409 `invalid_transition`；
    - 超过 10 秒 → `RevokeWindowExpiredError` → 409 `revoke_window_expired`。

    撤销用既有终态 `failed` 而不是第五个状态：通知的结局只有确认、失败、模拟三种，
    撤销是「发错了，当作没发出去」，语义落在 `failed` 上，由 `payload.revoked` 区分
    「投递失败」与「人工撤回」。

    审计**同事务**写（`session.add` + flush，与 `report_service` / `memory_service`
    同口径）：撤销是业务写入，凭据必须和状态一起提交或一起回滚。只有越权拒绝才用
    `audit_service.write_audit_isolated`（独立会话）——那条路径必然抛异常回滚，
    同事务写等于自己把证据抹掉。
    """
    _require_staff(identity, action="revocation")
    if reason is not None and len(reason) > 100:
        raise NotificationValidationError("revoke reason must be at most 100 characters")
    moment = _moment(now)
    notification = await _load_visible(session, identity, notification_id)
    if notification.status == "acknowledged":
        raise InvalidTransitionError(notification.status, "failed")
    sent_at = _utc(notification.delivered_at or notification.created_at)
    if moment - sent_at > datetime.timedelta(seconds=REVOKE_WINDOW_SECONDS):
        raise RevokeWindowExpiredError(
            notification_id=notification.notification_id,
            sent_at=sent_at,
            now=moment,
            window_seconds=REVOKE_WINDOW_SECONDS,
        )
    previous = notification.status
    _transition(notification, "failed", now=moment)
    payload = _payload_copy(notification)
    payload["revoked"] = True
    payload["revoked_at"] = moment.isoformat()
    payload["revoked_by"] = str(_staff_uuid(identity))
    if reason:
        payload["revoke_reason"] = reason
    notification.payload = payload
    session.add(
        AuditLog(
            actor_type=identity.kind,
            actor_id=_staff_uuid(identity),
            action="notification.revoke",
            resource_type="notifications",
            resource_id=notification.notification_id,
            detail={
                "from_status": previous,
                "to_status": "failed",
                "elder_id": str(notification.elder_id) if notification.elder_id else None,
                "reason": reason,
                "trace_id": get_trace_id(),
            },
        )
    )
    await session.flush()
    await session.refresh(notification)
    logger.info(
        "notification revoked",
        extra={
            "notification_id": str(notification.notification_id),
            "from_status": previous,
            "elapsed_seconds": (moment - sent_at).total_seconds(),
        },
    )
    return _item(notification)


# ---------------------------------------------------------------------------
# FR-RPT-03 日报推送（family_report + 1/5/15 重试）
# ---------------------------------------------------------------------------

DAILY_REPORT_KIND: Final[str] = "family_report"
"""日报推送用的 `kind`（0012 迁移 CHECK 已放行，本卡只是第一个真的用它的调用方）。"""

DAILY_REPORT_SEVERITY: Final[str] = "green"
"""日报是日常提醒，不是告警：铺红档会让照护台的红色待办池天天被日报刷屏。"""

DAILY_REPORT_RETRY_MINUTES: Final[tuple[int, ...]] = (1, 5, 15)
"""三次推送尝试相对**创建时刻**的偏移（分钟）：+1 / +5 / +15（PRD FR-RPT-03）。

为什么是元组而不是三个常量：`advance_daily_report_push` 要用「第 n 枪的偏移」和
「一共几枪」两个信息，分开写必然有一天只改了一处。
"""

DAILY_REPORT_RETRY_BATCH_LIMIT: Final[int] = 200
"""单次扫描最多处理多少条到期日报（与 `ESCALATION_BATCH_LIMIT` 同量级，防长尾）。"""

DAILY_REPORT_FAILED_ACTION: Final[str] = "notification.family_report_failed"
"""三次都没送出去时的审计 action：照护台据此捞「家属没收到日报」的现场。"""

_DELIVERY_KEY: Final[str] = "delivery"
_ATTEMPTS_KEY: Final[str] = "attempts"
_ANCHOR_KEY: Final[str] = "anchor_at"
_NEXT_RETRY_KEY: Final[str] = "next_retry_at"
_LAST_ERROR_KEY: Final[str] = "last_error"
_REPORT_ID_KEY: Final[str] = "report_id"
_REPORT_VERSION_KEY: Final[str] = "version"

DailyReportSender = Callable[[Notification], Awaitable[None]]
"""一次真实的推送动作（发给家属的通道）。

本仓没有真实通道（AGENTS §4.5 不接微信/短信），默认实现是「立刻成功」。留这个
协议是为了让**失败路径**可测：注入一个抛异常的 sender，1/5/15 与 `failed` 才有断言
价值——否则重试链在测试里永远走不到 `except` 分支。
"""


async def _active_guardians(session: AsyncSession, elder_id: uuid.UUID) -> list[uuid.UUID]:
    """这个长者当前有效的家属（`status='active'`），按 id 排序保证可复现。"""
    rows = await session.execute(
        select(ElderGuardian.guardian_id)
        .where(ElderGuardian.elder_id == elder_id, ElderGuardian.status == "active")
        .order_by(ElderGuardian.guardian_id)
    )
    return list(rows.scalars().all())


async def _find_daily_report_notification(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    report_id: uuid.UUID,
    version: int,
) -> Notification | None:
    """按幂等键 `(elder_id, report_id, version)` 找已有的日报通知。

    为什么不用 `(elder_id, report_date)`：补录会产出第二版，AC-25 要求新版**各自**
    只发一次通知；把日期当键会让第二版的推送被第一版拦掉（家属永远收不到「已更新」）。
    """
    return (
        await session.execute(
            select(Notification).where(
                Notification.kind == DAILY_REPORT_KIND,
                Notification.elder_id == elder_id,
                Notification.payload[_REPORT_ID_KEY].astext == str(report_id),
                Notification.payload[_REPORT_VERSION_KEY].astext == str(version),
            )
        )
    ).scalar_one_or_none()


def _delivery_state(payload: dict[str, Any]) -> dict[str, Any]:
    """取出 `payload.delivery` 的副本（JSONB 里嵌套 dict，必须复制后再整体回写）。"""
    state = payload.get(_DELIVERY_KEY)
    return dict(state) if isinstance(state, dict) else {}


def _anchor_moment(notification: Notification, state: dict[str, Any]) -> datetime.datetime:
    """重试链的零点：`payload.delivery.anchor_at`，缺失时退回 `created_at`。

    为什么不直接用 `created_at`：1/5/15 是「相对创建时刻」的绝对时刻，落库一份
    ISO 串让它与 `created_at` 解耦——通知行被外部脚本改写时间戳时重试链不会跟着漂。
    """
    raw = state.get(_ANCHOR_KEY)
    if isinstance(raw, str):
        try:
            return _utc(datetime.datetime.fromisoformat(raw))
        except ValueError:
            logger.warning(
                "daily report anchor unparsable",
                extra={"notification_id": str(notification.notification_id)},
            )
    return _utc(notification.created_at)


async def notify_daily_report(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    report_id: uuid.UUID,
    report_date: datetime.date,
    version: int,
    previous_version: int | None = None,
    now: datetime.datetime | None = None,
) -> Notification | None:
    """为一份日报建 `family_report` 通知（幂等）。返回 `None` 表示没有可推送的家属。

    三件事固定下来：

    1. **收件人 = 该长者 `elder_guardians.status='active'` 的家属**，写进
       `payload.recipients` 供展示/审计；读通知走 `_visibility_filters` 的 guardian
       分支（按 `elder_id`），所以不需要 `assignee_staff_id`（那一列只能指员工）。
    2. **幂等键 `(elder_id, report_id, version)`**：重跑 `generate()` 或调度重放都
       只会命中已有行并原样返回（AC-25「失败重试不重复通知」）。
    3. `previous_version` 非空表示这是补录版本：标题与 `updated` 标记换成「已更新」，
       家属才不会以为这是今天的第一份日报。

    `session` 的事务由调用方掌握（与 `create_notification` 同口径）：日报与通知要么
    一起提交，要么一起回滚——分两次提交会出现「日报生成了但推送记录没落」的孤儿。
    """
    moment = _moment(now)
    existing = await _find_daily_report_notification(
        session, elder_id=elder_id, report_id=report_id, version=version
    )
    if existing is not None:
        logger.info(
            "daily report notification reused",
            extra={
                "notification_id": str(existing.notification_id),
                "report_id": str(report_id),
                "version": version,
            },
        )
        return existing

    recipients = await _active_guardians(session, elder_id)
    if not recipients:
        # 没有家属就等于没有收件人：建一条谁都读不到的通知只会让「推送状态」说谎。
        logger.info(
            "daily report notification skipped",
            extra={"elder_id": str(elder_id), "report_id": str(report_id), "reason": "no_guardian"},
        )
        return None

    updated = previous_version is not None
    first_due = moment + datetime.timedelta(minutes=DAILY_REPORT_RETRY_MINUTES[0])
    payload: dict[str, Any] = {
        "report_id": str(report_id),
        "elder_id": str(elder_id),
        "report_date": report_date.isoformat(),
        "version": version,
        "updated": updated,
        "previous_version": previous_version,
        "title": "健康日报已更新" if updated else "今日健康日报已生成",
        "recipients": [str(guardian_id) for guardian_id in recipients],
        _DELIVERY_KEY: {
            _ATTEMPTS_KEY: 0,
            _ANCHOR_KEY: moment.isoformat(),
            _NEXT_RETRY_KEY: first_due.isoformat(),
            _LAST_ERROR_KEY: None,
        },
    }
    notification = await create_notification(
        session,
        kind=DAILY_REPORT_KIND,
        severity=DAILY_REPORT_SEVERITY,
        elder_id=elder_id,
        payload=payload,
        # 上面的查询式判定负责「返回已有那条并解释」，这里的键负责并发/重试兜底：
        # 同一天同一版日报只允许一行（2026-11-13 真机库里同一天见过 5 条）。
        dedupe_key=f"daily_report:{elder_id}:{report_id}:{version}",
    )
    logger.info(
        "daily report notification created",
        extra={
            "notification_id": str(notification.notification_id),
            "elder_id": str(elder_id),
            "report_id": str(report_id),
            "version": version,
            "updated": updated,
            "recipients": len(recipients),
            "next_retry_at": first_due.isoformat(),
        },
    )
    return notification


async def _simulate_daily_report_push(notification: Notification) -> None:
    """默认推送器：立刻成功（模拟推送，AGENTS §4.5 不接微信/短信）。

    名字里的 simulate 是**诚实标注**：这条通知的 `delivered` 只代表「站内通道没报错」，
    不代表团属手机真的收到了外部推送。真实通道接进来时换掉这个 sender，服务的重试
    与失败语义一行都不用改。
    """
    logger.debug(
        "daily report push simulated",
        extra={"notification_id": str(notification.notification_id)},
    )


async def deliver_due_daily_reports(
    session: AsyncSession,
    *,
    now: datetime.datetime | None = None,
    limit: int = DAILY_REPORT_RETRY_BATCH_LIMIT,
    sender: DailyReportSender | None = None,
) -> int:
    """扫描到期的日报通知并推一枪；返回**扫到并处理**的行数。

    时间与次数（PRD FR-RPT-03）：

    - 到点判据 `payload.delivery.next_retry_at <= now`，由 `notify_daily_report` 写入
      （+1/+5/+15，相对创建时刻）；
    - 失败且还没到第 3 枪 → `pending` + 写下一枪的 `next_retry_at`；
    - 第 3 枪仍失败 → `failed` + 一条 `audit_logs`（`actor_type='system'`），
      **不写 `delivered`**——「模拟推送状态记录」里最不能错的就是这一位；
    - 成功 → `delivered`（`delivered_at` 由 `_transition` 填），`next_retry_at` 清空。

    为什么不在通知创建时就把三枪都排成行：那会污染 `notifications` 的语义（一次推送
    一条记录 vs 一次尝试一条记录），也会让「同一 `(elder_id, report_id, version)` 只有
    一条通知」的幂等口径失效。簿记放 `payload` 是既有做法（`resend` / `escalate`）。
    """
    moment = _moment(now)
    # ISO 串在 JSONB 里是 text，必须 CAST 成 timestamptz 才能和 now() 比大小；
    # 直接按字符串比较的话，带不同偏移量（+08:00 / +00:00）的两个时刻会排错。
    due_at = Notification.payload[_DELIVERY_KEY][_NEXT_RETRY_KEY].astext.cast(
        DateTime(timezone=True)
    )
    rows = (
        (
            await session.execute(
                select(Notification)
                .where(
                    Notification.kind == DAILY_REPORT_KIND,
                    Notification.status.in_(("generated", "pending")),
                    Notification.payload[_DELIVERY_KEY][_NEXT_RETRY_KEY].astext.is_not(None),
                    due_at <= moment,
                )
                .order_by(due_at)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    pusher: DailyReportSender = sender or _simulate_daily_report_push
    delivered = retried = failed = 0
    for notification in rows:
        payload = _payload_copy(notification)
        state = _delivery_state(payload)
        anchor = _anchor_moment(notification, state)
        attempts = int(state.get(_ATTEMPTS_KEY) or 0) + 1
        state[_ATTEMPTS_KEY] = attempts
        try:
            await pusher(notification)
        except Exception as exc:  # 通道异常一律归为一次失败尝试（不向上抛，单条失败不拖垮整批）
            state[_LAST_ERROR_KEY] = type(exc).__name__
            if attempts >= len(DAILY_REPORT_RETRY_MINUTES):
                # 三枪打完：如实落 failed + 审计，绝不伪装成 delivered。
                state[_NEXT_RETRY_KEY] = None
                payload[_DELIVERY_KEY] = state
                notification.payload = payload
                _transition(notification, "failed", now=moment)
                session.add(
                    AuditLog(
                        actor_type="system",
                        actor_id=notification.elder_id or notification.notification_id,
                        action=DAILY_REPORT_FAILED_ACTION,
                        resource_type="notifications",
                        resource_id=notification.notification_id,
                        detail={
                            "report_id": payload.get(_REPORT_ID_KEY),
                            "version": payload.get(_REPORT_VERSION_KEY),
                            "report_date": payload.get("report_date"),
                            "attempts": attempts,
                            "last_error": state[_LAST_ERROR_KEY],
                            "trace_id": get_trace_id(),
                        },
                    )
                )
                failed += 1
            else:
                if notification.status == "generated":
                    _transition(notification, "pending", now=moment)
                offset = DAILY_REPORT_RETRY_MINUTES[attempts]
                state[_NEXT_RETRY_KEY] = (anchor + datetime.timedelta(minutes=offset)).isoformat()
                payload[_DELIVERY_KEY] = state
                notification.payload = payload
                retried += 1
            logger.warning(
                "daily report push failed",
                extra={
                    "notification_id": str(notification.notification_id),
                    "attempts": attempts,
                    "last_error": state[_LAST_ERROR_KEY],
                    "final": attempts >= len(DAILY_REPORT_RETRY_MINUTES),
                },
            )
            continue
        state[_LAST_ERROR_KEY] = None
        state[_NEXT_RETRY_KEY] = None
        payload[_DELIVERY_KEY] = state
        notification.payload = payload
        _transition(notification, "delivered", now=moment)
        delivered += 1

    await session.flush()
    logger.info(
        "daily report deliveries processed",
        extra={
            "scanned": len(rows),
            "delivered": delivered,
            "retried": retried,
            "failed": failed,
        },
    )
    return len(rows)


# ---------------------------------------------------------------------------
# FR-ALT-01 红色事件通知监护人（safety_alert + payload.recipients）
# ---------------------------------------------------------------------------

GUARDIAN_ALERT_KIND: Final[str] = "safety_alert"
"""家属告警复用既有 `safety_alert` kind。

为什么不新开一个 kind：`notifications.kind` 的 CHECK 只有五个值，加第六个就要迁移，
而 FR-ALT-01 要的是「同一场事件也告诉家属」，不是新的事件类型。这里复述字面量而不是
`import alert_service.SAFETY_ALERT_TYPE`：`alert_service` 已经 import 本模块，反向 import
会成环；两处取值由 `NOTIFICATION_KINDS` 与迁移 CHECK 双重钉死，写错会当场被校验拦下。
"""

GUARDIAN_ALERT_AUDIENCE: Final[str] = "guardian"
"""家属告警行在 payload 里的受众标记。

为什么必须有：同一场红色事件本来就有**一条护工通知**（`alert_service` 建的，payload
里也带 `risk_event_id`）。没有受众标记，`(elder_id, risk_event_id)` 这个幂等键会把护工
那条当成「已经通知过家属」，家属永远收不到——两行必须能被区分开。
"""

GUARDIAN_ALERT_TITLE: Final[str] = "家人触发安全告警"
"""家属端只看到这句话 + 时间 + 事件类型（PRD 177 白名单：健康日报与需关注事件摘要）。"""

_AUDIENCE_KEY: Final[str] = "audience"
_EVENT_ID_KEY: Final[str] = "risk_event_id"
_RECIPIENTS_KEY: Final[str] = "recipients"


async def _find_guardian_alert_notification(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    risk_event_id: uuid.UUID,
) -> Notification | None:
    """按幂等键 `(elder_id, risk_event_id)` 找已建的家属告警通知。

    查询带 `audience='guardian'`：只带 `risk_event_id` 会同时命中护工那条（同一事件），
    于是第二次调用会以为「家属已通知」而直接返回护工通知，家属的通知永远建不出来。
    """
    return (
        await session.execute(
            select(Notification).where(
                Notification.kind == GUARDIAN_ALERT_KIND,
                Notification.elder_id == elder_id,
                Notification.payload[_EVENT_ID_KEY].astext == str(risk_event_id),
                Notification.payload[_AUDIENCE_KEY].astext == GUARDIAN_ALERT_AUDIENCE,
            )
        )
    ).scalar_one_or_none()


async def notify_safety_alert_guardians(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    risk_event_id: uuid.UUID,
    category: str,
    severity: str,
    occurred_at: datetime.datetime,
    order_no: str | None = None,
    trace_id: str | None = None,
) -> Notification | None:
    """红色安全事件给家属建**一条**通知；没有在职家属时返回 ``None`` 且不建行。

    口径（PRD §4.7 FR-ALT-01 / AC-12）：

    - **一条收多人**：收件人清单写在 `payload.recipients`，不按家属逐个复制通知。
      家属读通知走 `_visibility_filters` 的 guardian 分支（按 `elder_id` 放行），
      一条行天然对所有绑定家属可见；逐个复制只会让家属端出现 N 条同样的告警。
    - **幂等键 `(elder_id, risk_event_id)`**：同一次事件重试不重复建；一次新的红色
      事件是新 `risk_event_id`，家属必须再收到一条（这与按分钟去重的 `source_id` 是
      两层：`source_id` 管「同一分钟同一规则只立一次案」，这里管「这一案只通知一次」）。
    - **不伪造送达**：本函数只建 `generated` 行，投递与重试仍归照护台/通道；
      没有家属就不建行——一条家属永远看不见的通知只会污染照护台列表。
    """
    existing = await _find_guardian_alert_notification(
        session, elder_id=elder_id, risk_event_id=risk_event_id
    )
    if existing is not None:
        logger.info(
            "guardian alert notification reused",
            extra={
                "notification_id": str(existing.notification_id),
                "risk_event_id": str(risk_event_id),
            },
        )
        return existing

    guardian_ids = await _active_guardians(session, elder_id)
    if not guardian_ids:
        # 不建行也不报错：没有绑定家属是合法的（老人独居、家属未注册）。
        logger.info(
            "guardian alert notification skipped",
            extra={"reason": "no_guardian", "risk_event_id": str(risk_event_id)},
        )
        return None

    payload: dict[str, Any] = {
        "module": "safety",
        "audience": GUARDIAN_ALERT_AUDIENCE,
        "title": GUARDIAN_ALERT_TITLE,
        "risk_event_id": str(risk_event_id),
        "category": category,
        "severity": severity,
        "elder_id": str(elder_id),
        "occurred_at": occurred_at.isoformat(),
        # JSONB 里统一成字符串：家属端与查询都用文本比较，UUID 对象进去会被序列化成
        # 带引号的形态，`payload["recipients"].astext` 上做 `IN` 就对不上。
        _RECIPIENTS_KEY: [str(guardian_id) for guardian_id in guardian_ids],
    }
    if order_no:
        payload["order_no"] = order_no
    if trace_id:
        payload["trace_id"] = trace_id

    notification = await create_notification(
        session,
        kind=GUARDIAN_ALERT_KIND,
        severity=severity,
        elder_id=elder_id,
        # 通知正文由模板拼装（title + 事件类型 + 时间），**不夹带长者原话**：
        # `alert_service` 那边已经把命中切片脱敏进 risk_events.detail，家属侧
        # 只给「需关注事件摘要」（PRD 177），越界的话术等于把隐私再扩散一次。
        payload=payload,
        # assignee_staff_id 只能指员工（FK staff_accounts），家属一律走 recipients。
        assignee_staff_id=None,
    )
    logger.info(
        "guardian alert notification created",
        extra={
            "notification_id": str(notification.notification_id),
            "risk_event_id": str(risk_event_id),
            "recipient_count": len(guardian_ids),
            "severity": severity,
        },
    )
    return notification


# ---------------------------------------------------------------------------
# FR-MED-08 断网三层兜底（服务端段）
# ---------------------------------------------------------------------------

OFFLINE_FAILURE_REASON: Final[str] = "offline"
"""断网归因值（`medication_events.failure_reason` 四值之一，PRD:338）。

它存在的唯一理由是**区分设备故障与拒服**：两者都是「没吃药」，但 `denied` 要跟长者
谈服药依从性，`offline` 要查平板与网络——写混了照护台就会用错处置。0009 迁移的
CHECK 已放行该值，本卡不动 schema。
"""

OFFLINE_RECOVERY_ACTIONS: Final[tuple[str, ...]] = (
    "callback",
    "work_order",
    "wait",
    "already_closed",
)
"""`decide_offline_recovery` / `recover_after_offline` 的四类判决。

- `callback`：窗口内，可补呼第 `attempts_sent + 1` 枪（**只补一枪**，不集中补播）；
- `work_order`：超窗或四次用尽且最后一枪窗已关，直接建单；
- `wait`：四次已发、最后一枪的 60s 还没等完，**不许抢跑升级**（PRD §4.5）；
- `already_closed`：事件已确认/已升级，重连不动它（幂等，不重复建单）。
"""

OFFLINE_NOTIFICATION_STATES: Final[tuple[str, ...]] = ("offline_overlap", "notification_failed")
"""断网通知的 `payload.state` 两态：掉线与窗口重叠 / 断网导致无回执终态。"""

OFFLINE_RETRY_SPACING_MINUTES: Final[int] = 5
"""四次触达节拍 5 分钟（PRD §4.5；与 `medication_service.RETRY_SPACING_MINUTES` 同源）。"""

_OFFLINE_CLOSED_EVENT_STATUSES: Final[tuple[str, ...]] = ("confirmed", "escalated")
"""事件已闭环的两态：重连路径遇到就什么都不做（`escalate_to_work_order` 不幂等）。"""

_OFFLINE_SUMMARIES: Final[dict[str, str]] = {
    "offline_overlap": "长者端掉线与用药提醒窗口重叠，设备未能播报，已立即通知护工核实",
    "notification_failed": "长者端断网致四次触达均无回执，已按设备故障归因并派工单跟进",
}
"""两种断网通知的长者可读摘要（不含药名之外的健康细节，展示前仍由前端脱敏）。"""


@dataclass(frozen=True, slots=True)
class OfflineRecoveryPlan:
    """一次「网络恢复后该怎么办」的纯判决（不碰库，可单测）。"""

    action: str
    reason: str
    attempt_no: int | None
    window_end: datetime.datetime
    attempts_sent: int


@dataclass(frozen=True, slots=True)
class OfflineRecoveryOutcome:
    """判决 + 已落库的结果：`work_order` 分支带回单号与通知编号。"""

    reminder_id: uuid.UUID
    action: str
    reason: str
    attempt_no: int | None
    window_end: datetime.datetime
    attempts_sent: int
    work_order_no: str | None = None
    notification_id: uuid.UUID | None = None


def _utc(value: datetime.datetime) -> datetime.datetime:
    """naive 一律按 UTC 解释（存储口径 AGENTS §8；只有展示层转上海）。"""
    return value if value.tzinfo is not None else value.replace(tzinfo=datetime.UTC)


def _resolve_attempts() -> int:
    from app.services import medication_service

    return medication_service.max_attempts()


def _resolve_wait_seconds() -> int:
    from app.services import medication_service

    return medication_service.attempt_wait_seconds()


def _offline_window_end(
    scheduled_at: datetime.datetime,
    *,
    attempts: int,
    wait_seconds: int,
    interval_minutes: int = OFFLINE_RETRY_SPACING_MINUTES,
) -> datetime.datetime:
    """提醒窗口的关闭时刻 = 最后一枪（T+15）后再等满 `wait_seconds`。

    与 `app/scheduler/medication.decide_event_action` 的 `timeline_end` 同一算法：
    过了这一刻还发提醒就是「集中补播」，而 PRD:326 明确禁止。
    """
    return _utc(scheduled_at) + datetime.timedelta(
        minutes=interval_minutes * (attempts - 1), seconds=wait_seconds
    )


def decide_offline_recovery(
    *,
    scheduled_at: datetime.datetime,
    attempts_sent: int,
    now: datetime.datetime,
    last_sent_at: datetime.datetime | None = None,
    attempts: int | None = None,
    wait_seconds: int | None = None,
    interval_minutes: int = OFFLINE_RETRY_SPACING_MINUTES,
) -> OfflineRecoveryPlan:
    """按窗口规则判「补呼 / 建单 / 等等」（纯函数，PRD:338 第②层）。

    判决顺序即 PRD 的优先级：**次数用尽 > 超窗 > 窗口内**。

    1. 四次都发过了：最后一枪的 60s 等满 → `work_order`（四次无回执 = 无回执终态）；
       没等满 → `wait`，因为「等了 60s」是 PRD §4.5 的硬约束，抢跑会少等一次。
    2. 窗口已关（`now >= window_end`）→ `work_order`：不补发晚到的提醒（FR-MED-08）。
    3. 其他 → `callback`，`attempt_no = attempts_sent + 1`，**只补这一枪**。
    """
    total = attempts if attempts is not None else _resolve_attempts()
    wait = wait_seconds if wait_seconds is not None else _resolve_wait_seconds()
    if total < 1:
        raise NotificationValidationError(f"attempts 必须 ≥1，收到 {total!r}")
    if attempts_sent < 0:
        raise NotificationValidationError(f"attempts_sent 不能为负，收到 {attempts_sent!r}")

    current = _utc(now)
    window_end = _offline_window_end(
        scheduled_at,
        attempts=total,
        wait_seconds=wait,
        interval_minutes=interval_minutes,
    )

    if attempts_sent >= total:
        if last_sent_at is None or current >= _utc(last_sent_at) + datetime.timedelta(seconds=wait):
            return OfflineRecoveryPlan(
                action="work_order",
                reason="attempts_exhausted",
                attempt_no=None,
                window_end=window_end,
                attempts_sent=attempts_sent,
            )
        return OfflineRecoveryPlan(
            action="wait",
            reason="last_attempt_pending",
            attempt_no=None,
            window_end=window_end,
            attempts_sent=attempts_sent,
        )

    if current >= window_end:
        return OfflineRecoveryPlan(
            action="work_order",
            reason="window_missed",
            attempt_no=None,
            window_end=window_end,
            attempts_sent=attempts_sent,
        )

    return OfflineRecoveryPlan(
        action="callback",
        reason="window_open",
        attempt_no=attempts_sent + 1,
        window_end=window_end,
        attempts_sent=attempts_sent,
    )


async def _attempt_stats(
    session: AsyncSession, reminder_id: uuid.UUID
) -> tuple[int, datetime.datetime | None]:
    """已触达次数与最后一枪时刻（读 `medication_attempts`，幂等键保证不重复）。"""
    row = (
        await session.execute(
            select(func.count(), func.max(MedicationAttempt.sent_at)).where(
                MedicationAttempt.event_id == reminder_id
            )
        )
    ).one()
    return int(row[0] or 0), row[1]


async def _primary_care_staff(session: AsyncSession, elder_id: uuid.UUID) -> uuid.UUID | None:
    """该长者的主责护工（与 `app/scheduler/medication.py:_primary_care_staff` 同一读口径）。"""
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


async def _find_offline_notification(
    session: AsyncSession, *, reminder_id: uuid.UUID, state: str
) -> Notification | None:
    """同一次提醒的同一种断网通知只建一条（平板抖动会反复上报离线）。

    复用查询取「最该被复用的那一条」：红档优先（已经压到红档说明事态更重）、同档取
    最新的、`notification_id` 兜底。排序**必须在 `limit(1)` 之前**——先截断再排序
    等于没排。这一处与 `list_for_staff` 同口径但独立写：它的用途是「挑一条来复用」，
    不是给人看的列表，以后要换判据也只该动这里。
    """
    reuse_rank = case(
        (Notification.severity == "red", 0),
        (Notification.severity == "yellow", 1),
        else_=2,
    )
    return (
        (
            await session.execute(
                select(Notification)
                .where(
                    Notification.kind == "medication_escalation",
                    Notification.payload["failure_reason"].astext == OFFLINE_FAILURE_REASON,
                    Notification.payload["reminder_id"].astext == str(reminder_id),
                    Notification.payload["state"].astext == state,
                )
                .order_by(
                    reuse_rank,
                    Notification.created_at.desc(),
                    Notification.notification_id,
                )
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


async def _create_offline_notification(
    session: AsyncSession,
    *,
    event: MedicationEvent,
    state: str,
    window_end: datetime.datetime,
    order_no: str | None,
    attempts_sent: int,
) -> Notification:
    """建（或复用）一条断网红档通知：`kind/severity` 与调度器升级通知同口径。"""
    if state not in OFFLINE_NOTIFICATION_STATES:
        raise NotificationValidationError(f"unknown offline notification state {state!r}")
    existing = await _find_offline_notification(session, reminder_id=event.reminder_id, state=state)
    if existing is not None:
        return existing

    return await create_notification(
        session,
        kind="medication_escalation",
        severity="red",
        elder_id=event.elder_id,
        assignee_staff_id=await _primary_care_staff(session, event.elder_id),
        # 与调度器升级通知同一套键口径（FR-ALT-04）：同一张药单的同一次提醒、同一个
        # 断网状态只留一条。`state` 进键是因为「断网补呼」与「窗口处置」是两种处置。
        dedupe_key=(
            f"medication_reminder:{event.plan_id}:{_utc(event.scheduled_at).isoformat()}:{state}"
        ),
        payload={
            "module": "medication",
            "state": state,
            "failure_reason": OFFLINE_FAILURE_REASON,
            "reminder_id": str(event.reminder_id),
            "work_order_no": order_no,
            "scheduled_at": _utc(event.scheduled_at).isoformat(),
            "window_end": _utc(window_end).isoformat(),
            "attempts_sent": attempts_sent,
            "summary": _OFFLINE_SUMMARIES[state],
        },
    )


async def notify_offline_overlap(
    session: AsyncSession,
    *,
    reminder_id: uuid.UUID,
    now: datetime.datetime | None = None,
) -> Notification | None:
    """PRD:338 第③层：掉线与用药窗口重叠 → 立即通知护工（幂等）。

    返回 `None` 的两种情形都不是错误：事件已闭环（没啥可做），或窗口已经关了
    （那属于第②层的「超窗建单」，由 `recover_after_offline` 归因，不在这里发）。
    """
    from app.services import medication_service

    current = _utc(now) if now is not None else datetime.datetime.now(datetime.UTC)
    event = await medication_service.load_event(session, reminder_id)
    if event.status in _OFFLINE_CLOSED_EVENT_STATUSES or event.terminal_state is not None:
        logger.info(
            "offline overlap skipped: event closed reminder_id=%s status=%s",
            reminder_id,
            event.status,
        )
        return None

    window_end = _offline_window_end(
        event.scheduled_at,
        attempts=_resolve_attempts(),
        wait_seconds=_resolve_wait_seconds(),
    )
    if current > window_end:
        return None

    attempts_sent, _ = await _attempt_stats(session, reminder_id)
    return await _create_offline_notification(
        session,
        event=event,
        state="offline_overlap",
        window_end=window_end,
        order_no=None,
        attempts_sent=attempts_sent,
    )


async def recover_after_offline(
    session: AsyncSession,
    *,
    reminder_id: uuid.UUID,
    now: datetime.datetime | None = None,
    attempts: int | None = None,
    wait_seconds: int | None = None,
) -> OfflineRecoveryOutcome:
    """网络恢复后按窗口规则处置一次提醒（PRD:338 第②层）。

    - `callback` / `wait`：**只回判决，不落库**。补呼由接线层复用
      `app/scheduler/medication.run_medication_touch` 发（幂等键
      `(event_id, attempt_no)` 保证重放安全），本文件不造第二条外呼路径。
    - `work_order`：同事务做三件事——建红线工单（`escalate_to_work_order`，
      唯一工单构造器）、把终态改成 `notification_failed` 并写
      `failure_reason='offline'`、给主责护工建红档通知。
    - `already_closed`：事件已确认/已升级，直接返回（`escalate_to_work_order`
      不幂等，重复调会多建一张单）。

    调用方负责 `commit`（与其它服务函数一致：只 flush）。
    """
    from app.services import medication_service

    current = _utc(now) if now is not None else datetime.datetime.now(datetime.UTC)
    total = attempts if attempts is not None else _resolve_attempts()
    wait = wait_seconds if wait_seconds is not None else _resolve_wait_seconds()

    event = await medication_service.load_event(session, reminder_id)
    attempts_sent, last_sent_at = await _attempt_stats(session, reminder_id)
    window_end = _offline_window_end(event.scheduled_at, attempts=total, wait_seconds=wait)

    if event.status in _OFFLINE_CLOSED_EVENT_STATUSES or event.terminal_state is not None:
        return OfflineRecoveryOutcome(
            reminder_id=reminder_id,
            action="already_closed",
            reason="already_closed",
            attempt_no=None,
            window_end=window_end,
            attempts_sent=attempts_sent,
        )

    plan = decide_offline_recovery(
        scheduled_at=event.scheduled_at,
        attempts_sent=attempts_sent,
        now=current,
        last_sent_at=last_sent_at,
        attempts=total,
        wait_seconds=wait,
    )

    if plan.action != "work_order":
        logger.info(
            "offline recovery %s reminder_id=%s attempts_sent=%s attempt_no=%s reason=%s",
            plan.action,
            reminder_id,
            attempts_sent,
            plan.attempt_no,
            plan.reason,
        )
        return OfflineRecoveryOutcome(
            reminder_id=reminder_id,
            action=plan.action,
            reason=plan.reason,
            attempt_no=plan.attempt_no,
            window_end=plan.window_end,
            attempts_sent=attempts_sent,
        )

    order = await medication_service.escalate_to_work_order(session, event=event)
    await medication_service.update_event_state(
        session,
        reminder_id=reminder_id,
        terminal_state="notification_failed",
        failure_reason=OFFLINE_FAILURE_REASON,
    )
    notification = await _create_offline_notification(
        session,
        event=event,
        state="notification_failed",
        window_end=plan.window_end,
        order_no=getattr(order, "order_no", None),
        attempts_sent=attempts_sent,
    )
    logger.warning(
        "offline recovery escalated reminder_id=%s elder_id=%s reason=%s order_no=%s",
        reminder_id,
        event.elder_id,
        plan.reason,
        getattr(order, "order_no", None),
    )
    return OfflineRecoveryOutcome(
        reminder_id=reminder_id,
        action="work_order",
        reason=plan.reason,
        attempt_no=None,
        window_end=plan.window_end,
        attempts_sent=attempts_sent,
        work_order_no=getattr(order, "order_no", None),
        notification_id=notification.notification_id,
    )


__all__ = [
    "ESCALATION_BATCH_LIMIT",
    "ESCALATION_DELAY_SECONDS",
    "GUARDIAN_ALERT_AUDIENCE",
    "GUARDIAN_ALERT_KIND",
    "GUARDIAN_ALERT_TITLE",
    "NOTIFICATION_KINDS",
    "NOTIFICATION_SEVERITIES",
    "NOTIFICATION_STATUSES",
    "OFFLINE_FAILURE_REASON",
    "OFFLINE_NOTIFICATION_STATES",
    "OFFLINE_RECOVERY_ACTIONS",
    "OFFLINE_RETRY_SPACING_MINUTES",
    "RESEND_ROLES",
    "REVOKE_WINDOW_SECONDS",
    "InvalidTransitionError",
    "NotificationItem",
    "NotificationNotFoundError",
    "NotificationPage",
    "NotificationScopeForbiddenError",
    "NotificationServiceError",
    "NotificationValidationError",
    "OfflineRecoveryOutcome",
    "OfflineRecoveryPlan",
    "RevokeWindowExpiredError",
    "acknowledge",
    "create_notification",
    "decide_offline_recovery",
    "deliver",
    "deliver_due_daily_reports",
    "escalate_unacknowledged",
    "list_for_staff",
    "notify_daily_report",
    "notify_offline_overlap",
    "notify_safety_alert_guardians",
    "receipt",
    "recover_after_offline",
    "resend",
    "revoke",
]
