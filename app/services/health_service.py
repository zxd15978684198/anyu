"""健康记录领域服务：分级引擎 + 护工代填 + 补录版本链 + 复核承载（PRD §4.6 / FR-HLT-01/03/04/06）。

## 三条硬口径落在这里

1. **分级阈值只写在代码里**（AGENTS §6 L0：阈值写死在代码而非 prompt）。本模块的
   `grade_vitals()` 是唯一分级入口，语音子图与代填 API 都调它 —— 两个渠道同一套规则，
   不允许模型自报「您这是高血压」这类诊断口径（AGENTS §4.4）。
2. **范围判定在服务层**：代填/补录/查看趋势都先判「员工角色 + `elder_staff_assignments`
   的 relation」再碰资源；范围外一律 403 且先落审计（契约 §3「验签 → 角色 → 范围 → 存在」）。
   护工只认 `relation='primary_care'`、医生只认 `relation='doctor'`，admin 全量。
3. **写库只发生在本文件**（AGENTS §2）：API 只调用这里的函数，不拼查询、不写 ORM。

## 与基座模型的适配（已在任务报告登记）

- 基座（迁移 0007）用 `record_date` / `grade` / `revision` / `collected_at`，技术设计文档
  §3 的 `recorded_at` / `severity` / `version` / `review_status` / `graph_thread_id` 等列
  **未落地**；本文件按基座实现，不复刻文档列名。
- 基座 5 字段是 收缩压/舒张压/心率/睡眠/情绪；PRD 的第 5 问是 `complaint` 文本。
  HLT-06 对齐（按 AGENTS 前置铁律 7「以实现为准」，差异逐条登记在任务报告）：
  - 语音采集的固定五问仍是 `health_checkup_graph.QUESTION_ORDER`（含 `heart_rate`、
    不含 `complaint`）：`heart_rate` 是基座列（迭代 8 任务书的第 3 问），不属 PRD 五问；
    主诉是**独立采集链路**（`record_complaint` / `start_complaint_checkup`），
    原文落 `health_records.complaint` 列（HLT-07 起已有列），不再只塞通知 payload。
  - 主诉判档：只要这一版带着真实主诉（不是「无 / 没有不舒服」），等级不低于黄，
    原因码补 `complaint` —— 与 `record_complaint` 同口径。
  - 来源枚举沿用 PRD §4.6 的四枚举（`staff_*` / `voice_*`，与 TD §3、模型 CHECK 一致），
    FR-HLT-06 行内的 `staff_report` / `resident` 二值只作**展示标签**（`prd_source_label()`），
    不改 CHECK、不新增第二套枚举。
- 分级规则取 PRD §4.6 与 迭代 8 任务书（Lead）的**并集**：PRD 的低值项（收缩<90 / 舒张<60）
  与心率异常（<50 / >110，非 PRD 字段但属「其他异常」）、睡眠 >12h 都判黄。
  每条规则带原因码，便于复核与测试表驱动。
"""

from __future__ import annotations

import datetime
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Literal, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import RoleForbiddenError, check_roles
from app.core.context import get_trace_id
from app.core.runtime_context import Identity
from app.core.security import KIND_DEVICE, KIND_GUARDIAN, KIND_STAFF_CARD
from app.models import Elder, ElderGuardian, ElderStaffAssignment, HealthRecord, Notification
from app.models.health import HealthCheckupSession
from app.services import notification_service

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ 枚举与阈值

Grade = Literal["green", "yellow", "red"]

#: 分级规则版本（PRD §4.6 要求分级可追溯；基座无 `rule_version` 列，先落在常量与日志里）。
RULE_VERSION = "day8-v1"

#: 代填来源（FR-HLT-06）；语音侧来源由子图/对话链路传入，本服务不代填。
STAFF_SOURCES: tuple[str, ...] = ("staff_scheduled", "staff_active")
VOICE_SOURCES: tuple[str, ...] = ("voice_scheduled", "voice_active")
ALL_SOURCES: tuple[str, ...] = STAFF_SOURCES + VOICE_SOURCES

#: PRD 展示口径的来源标签（FR-HLT-06 行内 `staff_report` / `resident` 二值）。
#: 只做**展示映射**：库内与接口仍走上面四个枚举（PRD §4.6 正文、TD §3、模型 CHECK 同源），
#: 因此复核台既能按「护工填报 / 长者自述」看，也不破坏 scheduled/active 的触发维度。
PRD_SOURCE_LABELS: dict[str, str] = {
    "staff_scheduled": "staff_report",
    "staff_active": "staff_report",
    "voice_scheduled": "resident",
    "voice_active": "resident",
}


def prd_source_label(source: str) -> str:
    """把库内来源枚举映射成 PRD 口径的二值标签；未知来源原样回传（不猜、不吞）。"""
    return PRD_SOURCE_LABELS.get(source, source)


# 红档阈值（PRD §7 硬性数值：收缩压 ≥180 或 舒张压 ≥120，单分量即判红）。
SYSTOLIC_RED = 180
DIASTOLIC_RED = 120

# 黄档阈值（PRD §4.6：140–179 / 90–119 / <90 / <60 / 睡眠 <5h / mood ≤2 / 任一缺失）。
SYSTOLIC_HIGH_RANGE = (140, 179)
DIASTOLIC_HIGH_RANGE = (90, 119)
SYSTOLIC_LOW = 90
DIASTOLIC_LOW = 60
SLEEP_LOW_HOURS = 5.0
SLEEP_HIGH_HOURS = 12.0
MOOD_LOW_MAX = 2

# 心率不是 PRD 的五问字段，但基座保留了 `heart_rate_bpm` 列（迭代 8 任务书把心率列为第 3 问）。
# 采集缺失**不判黄**（否则每条 PRD 绿档都会被心率缺失拖黄），仅值异常入黄。
HEART_RATE_LOW = 50
HEART_RATE_HIGH = 110

#: 角色 → `elder_staff_assignments.relation`；admin 不在表内表示不受范围限制。
_RELATION_BY_ROLE: dict[str, str] = {"care_worker": "primary_care", "doctor": "doctor"}

_SHANGHAI = datetime.timezone(datetime.timedelta(hours=8), name="Asia/Shanghai")


def today_in_shanghai() -> datetime.date:
    """健康记录的 `record_date` 是**北京时间自然日**（模型注释口径）。"""
    return datetime.datetime.now(tz=_SHANGHAI).date()


# ------------------------------------------------------------------ 错误类型


class HealthServiceError(Exception):
    """健康领域错误基类：只表达「这次请求被拒」，不构造 HTTP 响应。"""


class HealthScopeForbiddenError(HealthServiceError):
    """员工角色不在健康业务白名单，或不在该长者的负责范围内（403）。"""


class HealthNotFoundError(HealthServiceError):
    """长者 / 记录 / 复核任务不存在，或不可见（404）。"""


class HealthValidationError(HealthServiceError):
    """入参非法（422）。"""


class HealthConflictError(HealthServiceError):
    """状态冲突，如重复补录同一条记录（409）。"""


# ------------------------------------------------------------------ 度量字段


@dataclass(frozen=True, slots=True)
class MeasurementSpec:
    """一个可采集字段的取值口径（列名 + 合法区间 + 是否整数）。"""

    key: str
    column: str
    label: str
    minimum: float
    maximum: float
    integer: bool


MEASUREMENTS: dict[str, MeasurementSpec] = {
    "systolic": MeasurementSpec("systolic", "systolic_mmhg", "收缩压", 0, 300, True),
    "diastolic": MeasurementSpec("diastolic", "diastolic_mmhg", "舒张压", 0, 300, True),
    "heart_rate": MeasurementSpec("heart_rate", "heart_rate_bpm", "心率", 20, 250, True),
    "sleep_hours": MeasurementSpec("sleep_hours", "sleep_hours", "睡眠时长", 0, 24, False),
    "mood_score": MeasurementSpec("mood_score", "mood_score", "情绪评分", 1, 5, True),
}

MEASUREMENT_KEYS: tuple[str, ...] = tuple(MEASUREMENTS)


def _as_number(value: object) -> float | None:
    """把「ASR 文本 / JSON 数字 / Decimal」统一成 float；不是数就返回 None。

    只认十进制数字与可选小数：中文数字（「一百二」）由 ASR 侧负责归一，
    这里再猜会把警告话术说错，宁可让采集流程重问一次。
    """
    if isinstance(value, bool):  # bool 是 int 子类，必须先挡掉
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(" ", "")
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def coerce_measurement(key: str, value: object) -> float | int:
    """按字段口径校验并归一化；非法抛 `HealthValidationError`。

    规则（PRD §4.6）：非数字 / 负值 / 越界无效；情绪分必须是 1–5 的整数。
    """
    spec = MEASUREMENTS.get(key)
    if spec is None:
        raise HealthValidationError(f"unknown measurement: {key}")
    number = _as_number(value)
    if number is None:
        raise HealthValidationError(f"{spec.label} 不是数字")
    if spec.integer and not float(number).is_integer():
        raise HealthValidationError(f"{spec.label} 必须是整数")
    if number < spec.minimum or number > spec.maximum:
        raise HealthValidationError(
            f"{spec.label} 超出可接受范围 {spec.minimum:g}–{spec.maximum:g}"
        )
    return int(number) if spec.integer else number


def blood_pressure_ordered(systolic: object, diastolic: object) -> bool:
    """两分量都有时校验 收缩压 > 舒张压（PRD §4.6 数据校验）。

    缺任一分量不算冲突：单分量判红优先于完整性校验（FR-HLT-02 / AC-21）。
    """
    left = _as_number(systolic)
    right = _as_number(diastolic)
    if left is None or right is None:
        return True
    return left > right


def normalise_values(values: Mapping[str, object]) -> dict[str, float | int]:
    """把请求体里的五字段归一化；未知字段直接 422，不静默丢弃。"""
    unknown = set(values) - set(MEASUREMENTS)
    if unknown:
        raise HealthValidationError(f"unknown fields: {', '.join(sorted(unknown))}")
    cleaned: dict[str, float | int] = {}
    for key, raw in values.items():
        if raw is None or raw == "":
            continue
        cleaned[key] = coerce_measurement(key, raw)
    if not cleaned:
        raise HealthValidationError("at least one health measurement is required")
    if not blood_pressure_ordered(cleaned.get("systolic"), cleaned.get("diastolic")):
        raise HealthValidationError("收缩压必须高于舒张压")
    return cleaned


# ------------------------------------------------------------------ 分级引擎


@dataclass(frozen=True, slots=True)
class GradeResult:
    """分级结果：等级 + 可解释原因码（写库进 `abnormal_reasons`，复核台直接展示）。"""

    grade: Grade
    reasons: tuple[str, ...]
    rule_version: str = RULE_VERSION

    @property
    def needs_review(self) -> bool:
        """黄/红都需要人工复核任务（PRD §4.6 系统行为列）。"""
        return self.grade in ("yellow", "red")


def grade_vitals(
    *,
    systolic: object = None,
    diastolic: object = None,
    heart_rate: object = None,
    sleep_hours: object = None,
    mood_score: object = None,
) -> GradeResult:
    """按 PRD §4.6 分级：红 > 黄 > 绿，多项并存取最高等级。

    红档只由血压触发（急症症状规则属 L0 规则层，不在这里）；**单分量即红**，
    另一分量缺失不阻断（AC-21）。黄档含低值项、睡眠过短/过长、情绪低分、
    心率异常与任何 PRD 五项里的缺失项。
    """
    systolic_n = _as_number(systolic)
    diastolic_n = _as_number(diastolic)
    heart_n = _as_number(heart_rate)
    sleep_n = _as_number(sleep_hours)
    mood_n = _as_number(mood_score)

    red: list[str] = []
    if systolic_n is not None and systolic_n >= SYSTOLIC_RED:
        red.append("systolic_red")
    if diastolic_n is not None and diastolic_n >= DIASTOLIC_RED:
        red.append("diastolic_red")

    yellow: list[str] = []
    if systolic_n is None:
        yellow.append("systolic_missing")
    elif systolic_n < SYSTOLIC_LOW:
        yellow.append("systolic_low")
    elif SYSTOLIC_HIGH_RANGE[0] <= systolic_n <= SYSTOLIC_HIGH_RANGE[1]:
        yellow.append("systolic_high")

    if diastolic_n is None:
        yellow.append("diastolic_missing")
    elif diastolic_n < DIASTOLIC_LOW:
        yellow.append("diastolic_low")
    elif DIASTOLIC_HIGH_RANGE[0] <= diastolic_n <= DIASTOLIC_HIGH_RANGE[1]:
        yellow.append("diastolic_high")

    if sleep_n is None:
        yellow.append("sleep_missing")
    elif sleep_n < SLEEP_LOW_HOURS:
        yellow.append("sleep_low")
    elif sleep_n > SLEEP_HIGH_HOURS:
        yellow.append("sleep_high")

    if mood_n is None:
        yellow.append("mood_missing")
    elif mood_n <= MOOD_LOW_MAX:
        yellow.append("mood_low")

    if heart_n is not None and not (HEART_RATE_LOW <= heart_n <= HEART_RATE_HIGH):
        yellow.append("heart_rate_abnormal")

    if red:
        return GradeResult("red", tuple(red + yellow))
    if yellow:
        return GradeResult("yellow", tuple(yellow))
    return GradeResult("green", ())


#: 长者对第 5 问（「今天身体有什么不舒服？」）的否定回答：这些不算主诉，不判黄。
#: 只认整句就是否定词的短答，宁可漏判也不把「没有不舒服」误记成一条主诉。
_NO_COMPLAINT_TEXTS: frozenset[str] = frozenset(
    {
        "无",
        "没有",
        "没",
        "无不适",
        "没有不适",
        "没有不舒服",
        "没有哪里不舒服",
        "没有哪儿不舒服",
        "没事",
        "都好",
        "挺好",
        "还好",
    }
)


def complaint_is_present(text: str | None) -> bool:
    """第 5 问有没有真实主诉（PRD §4.6 第 5 问的答案是「无 / 文本」二选一）。"""
    normalised = (text or "").strip().strip("。.！!，,、；;~ ")
    return bool(normalised) and normalised not in _NO_COMPLAINT_TEXTS


def grade_with_complaint(grade: GradeResult, complaint: str | None) -> GradeResult:
    """PRD §4.6：**非急症主诉判黄** —— 带着真实主诉的版本等级不低于黄。

    为什么要在服务层补这一刀：`grade_vitals()` 只看得见五个数值，主诉是文本、
    它给不了分级；于是「说了不舒服、五项都正常」会落成绿档且**不建复核任务**，
    与主诉独立链路（`record_complaint` 固定黄档）自相矛盾。这里把两条入口拉齐：
    主诉在场 → 绿抬黄 + 原因码 `complaint`；红档不动（红已是最高档），只补原因码，
    让复核台能一眼看到「这单是主诉起的，还是数值起的」。
    """
    if not complaint_is_present(complaint):
        return grade
    reasons = tuple(dict.fromkeys((*grade.reasons, "complaint")))
    if grade.grade == "green":
        return replace(grade, grade="yellow", reasons=reasons)
    return replace(grade, reasons=reasons)


# ------------------------------------------------------------------ 返回结构


@dataclass(frozen=True, slots=True)
class HealthRecordView:
    """API/子图共用的健康记录读模型（不复用 ORM 行：Decimal 与列名都收在这一层）。"""

    record_id: uuid.UUID
    elder_id: uuid.UUID
    record_date: datetime.date
    source: str
    systolic: float | None
    diastolic: float | None
    heart_rate: float | None
    sleep_hours: Decimal | None
    mood_score: int | None
    grade: str
    abnormal_reasons: list[str]
    revision: int
    superseded_by: uuid.UUID | None
    recorded_by_staff_id: uuid.UUID | None
    collected_at: datetime.datetime
    notification_id: uuid.UUID | None = None
    #: 主诉原文（HLT-07）。只来自长者口述链路；补录/采集记录恒为 None。
    complaint: str | None = None
    #: 撤销痕迹（HLT-04）：撤销是软删留痕——行还在、版本链不断，只是不再进日报与趋势。
    revoked_at: datetime.datetime | None = None
    revoked_by: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class ReviewTaskView:
    """复核任务（`notifications(kind='health_review')` 的读模型）。"""

    notification_id: uuid.UUID
    elder_id: uuid.UUID | None
    severity: str
    status: str
    payload: dict[str, Any]
    created_at: datetime.datetime
    acknowledged_at: datetime.datetime | None


# ------------------------------------------------------------------ 审计与权限


def _audit(
    identity: Identity | None,
    *,
    action: str,
    target: str,
    result: str,
    reason: str | None = None,
    **extra: object,
) -> None:
    """结构化审计日志（A-02）。只放 ID / 字段名 / 判定结果，不放姓名与主诉原文。"""
    fields: dict[str, object] = {
        "trace_id": get_trace_id() or "-",
        "audit_action": action,
        "actor": f"{identity.kind}:{identity.principal_id}" if identity is not None else "system",
        "target": target,
        "result": result,
    }
    if reason is not None:
        fields["reason"] = reason
    fields.update(extra)
    logger.info("audit", extra=fields)


def _staff_uuid(identity: Identity) -> uuid.UUID:
    if identity.staff_id is None:  # pragma: no cover - Identity 构造时已保证
        raise HealthScopeForbiddenError("staff identity is malformed")
    return uuid.UUID(identity.staff_id)


async def _require_scope(
    session: AsyncSession,
    identity: Identity,
    elder_id: uuid.UUID,
    *,
    action: str,
) -> uuid.UUID | None:
    """角色 + 负责范围判定；admin 返回 None 表示不受限。

    顺序固定：先 role（`check_roles`，非员工主体直接 403），再 relation 范围，
    最后才由调用方查资源存在性 —— 范围外主体拿到的永远是 403（契约 §3）。
    """
    target = f"elder:{elder_id}"
    try:
        check_roles(identity, ("admin", "care_worker", "doctor"))
    except RoleForbiddenError:
        _audit(identity, action=action, target=target, result="forbidden", reason="role")
        raise

    staff_id = _staff_uuid(identity)
    if identity.role == "admin":
        return None

    relation = _RELATION_BY_ROLE.get(identity.role or "")
    if relation is None:  # pragma: no cover - 角色枚举由 CHECK 与上一步双重保证
        _audit(identity, action=action, target=target, result="forbidden", reason="role")
        raise HealthScopeForbiddenError("staff role has no health scope")

    assignment = (
        await session.execute(
            select(ElderStaffAssignment.id)
            .where(
                ElderStaffAssignment.elder_id == elder_id,
                ElderStaffAssignment.staff_id == staff_id,
                ElderStaffAssignment.relation == relation,
            )
            .limit(1)
        )
    ).first()
    if assignment is None:
        _audit(identity, action=action, target=target, result="forbidden", reason="scope")
        raise HealthScopeForbiddenError("elder is outside the current staff scope")
    return staff_id


async def _require_elder(session: AsyncSession, elder_id: uuid.UUID, *, action: str) -> Elder:
    elder = await session.get(Elder, elder_id)
    if elder is None:
        _audit(None, action=action, target=f"elder:{elder_id}", result="not_found")
        raise HealthNotFoundError(str(elder_id))
    return elder


# ------------------------------------------------------------------ 视图转换


def _view(record: HealthRecord, *, notification_id: uuid.UUID | None = None) -> HealthRecordView:
    return HealthRecordView(
        record_id=record.record_id,
        elder_id=record.elder_id,
        record_date=record.record_date,
        source=record.source,
        systolic=float(record.systolic_mmhg) if record.systolic_mmhg is not None else None,
        diastolic=float(record.diastolic_mmhg) if record.diastolic_mmhg is not None else None,
        heart_rate=float(record.heart_rate_bpm) if record.heart_rate_bpm is not None else None,
        sleep_hours=Decimal(record.sleep_hours) if record.sleep_hours is not None else None,
        mood_score=record.mood_score,
        grade=record.grade,
        abnormal_reasons=list(record.abnormal_reasons or ()),
        revision=record.revision,
        superseded_by=record.superseded_by,
        recorded_by_staff_id=record.recorded_by_staff_id,
        collected_at=record.collected_at,
        notification_id=notification_id,
        complaint=record.complaint,
        revoked_at=record.revoked_at,
        revoked_by=record.revoked_by,
    )


def _review_view(task: Notification) -> ReviewTaskView:
    return ReviewTaskView(
        notification_id=task.notification_id,
        elder_id=task.elder_id,
        severity=task.severity,
        status=task.status,
        payload=dict(task.payload or {}),
        created_at=task.created_at,
        acknowledged_at=task.acknowledged_at,
    )


# ------------------------------------------------------------------ 写库：版本链


def _columns_for(values: Mapping[str, float | int]) -> dict[str, Any]:
    """API/子图字段名 → ORM 列名（五字段一一对应，不手抄散落各处）。"""
    return {MEASUREMENTS[key].column: value for key, value in values.items() if key in MEASUREMENTS}


async def _append_version(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    record_date: datetime.date,
    source: str,
    values: Mapping[str, float | int],
    grade: GradeResult,
    recorded_by_staff_id: uuid.UUID | None,
    complaint: str | None = None,
) -> HealthRecord:
    """追加一个新 revision；原值一律不改（FR-HLT-04「补录追加版本不覆盖原始值」）。

    当前版本 = 同 (elder_id, record_date) 且 `superseded_by IS NULL` 的那一行；
    新行的 `revision = 当前 + 1`，并把它指向新行 —— 版本链是单向的，可回查。

    `complaint` 只有主诉链路（HLT-07）传值：它是这一版健康记录的发起原因，
    必须落在业务行上（此前只塞进通知 payload，导致「主诉」在库内无处可查）。
    """
    current = (
        await session.execute(
            select(HealthRecord)
            .where(
                HealthRecord.elder_id == elder_id,
                HealthRecord.record_date == record_date,
                HealthRecord.superseded_by.is_(None),
            )
            .order_by(HealthRecord.revision.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    record = HealthRecord(
        elder_id=elder_id,
        record_date=record_date,
        source=source,
        grade=grade.grade,
        abnormal_reasons=list(grade.reasons) or None,
        revision=(current.revision + 1) if current is not None else 1,
        recorded_by_staff_id=recorded_by_staff_id,
        complaint=complaint,
        **_columns_for(values),
    )
    session.add(record)
    try:
        # 先把新行落库（拿到 record_id 与 revision）；旧行回指放在后面单独 flush，
        # 否则 self-FK（superseded_by → record_id）可能先于 INSERT 执行而撞外键。
        await session.flush()
    except IntegrityError as exc:
        # 并发补录撞了 UNIQUE(elder_id, record_date, revision)：恢复事务再映射 409。
        await session.rollback()
        logger.warning("health_record_conflict", extra={"reason": "revision_race"})
        raise HealthConflictError("another revision was created concurrently") from exc
    if current is not None:
        current.superseded_by = record.record_id
        await session.flush()
    await session.refresh(record)
    return record


async def _reuse_review_notification(
    session: AsyncSession,
    *,
    existing: Notification,
    record: HealthRecord,
    grade: GradeResult,
    payload: Mapping[str, Any],
) -> Notification:
    """复用当天已开着的复核单；红值到达时就地升红并留痕（HLT-03）。

    两条分支的共同目标是「同一长者同一天只留一条待办」，区别只在证据：
    - 新值达到红标（`systolic_red` / `diastolic_red`）：`severity -> red`，
      payload 写 `escalated_from / escalated_record_id / escalated_revision`，
      让医生看到「这张单原本是黄，因 185/118 补录升红」。
    - 其余（同档重复补录）：把本次新增的 payload 键补进去（已有键不覆盖），
      原单的 record_id 仍指向首次触发那条记录，不丢发起证据。
    """
    if grade.grade == "red" and existing.severity != "red":
        await notification_service.escalate_severity(
            session,
            existing,
            reason="vitals_red_threshold",
            evidence={
                "grade": "red",
                "abnormal_reasons": list(grade.reasons),
                "requires_doctor_review": True,
                "escalated_record_id": str(record.record_id),
                "escalated_revision": record.revision,
            },
        )
        logger.info(
            "health_review_escalated",
            extra={
                "trace_id": get_trace_id() or "-",
                "notification_id": str(existing.notification_id),
                "record_id": str(record.record_id),
                "elder_id": str(record.elder_id),
                "record_date": record.record_date.isoformat(),
                "reasons": list(grade.reasons),
            },
        )
        return existing

    merged = {
        key: value for key, value in payload.items() if key not in dict(existing.payload or {})
    }
    if merged:
        existing.payload = {**dict(existing.payload or {}), **merged}
        await session.flush()
        await session.refresh(existing)
    logger.info(
        "health_review_reused",
        extra={
            "trace_id": get_trace_id() or "-",
            "notification_id": str(existing.notification_id),
            "record_id": str(record.record_id),
            "elder_id": str(record.elder_id),
            "record_date": record.record_date.isoformat(),
            "grade": grade.grade,
        },
    )
    return existing


async def _create_review_notification(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    record: HealthRecord,
    grade: GradeResult,
    source: str,
    thread_id: str | None = None,
    extra_payload: Mapping[str, Any] | None = None,
) -> Notification | None:
    """黄/红档落一条 `health_review` 复核任务（S-D8-2：不建 work_orders）。

    指派对象按 PRD §4.6「立即通知护工」：优先负责护工（`primary_care`），
    没有负责护工时退回医生；红档在 payload 里标 `requires_doctor_review`。
    """
    if not grade.needs_review:
        return None

    assignee: uuid.UUID | None = None
    for relation in ("primary_care", "doctor"):
        row = (
            await session.execute(
                select(ElderStaffAssignment.staff_id)
                .where(
                    ElderStaffAssignment.elder_id == elder_id,
                    ElderStaffAssignment.relation == relation,
                )
                .limit(1)
            )
        ).first()
        if row is not None:
            assignee = row.staff_id
            break

    payload: dict[str, Any] = {
        "record_id": str(record.record_id),
        "record_date": record.record_date.isoformat(),
        "elder_id": str(elder_id),
        "grade": grade.grade,
        "abnormal_reasons": list(grade.reasons),
        "rule_version": grade.rule_version,
        "source": source,
        # FR-HLT-06 验收句要「来源 staff_report 且含操作者」，而库内是四枚举：
        # 两个字段都给出，复核台不必自己反推（`source` 仍是唯一事实）。
        "source_label": prd_source_label(source),
        "reported_by_staff_id": (
            str(record.recorded_by_staff_id) if record.recorded_by_staff_id is not None else None
        ),
        "revision": record.revision,
        "requires_doctor_review": grade.grade == "red",
        "thread_id": thread_id,
    }
    if extra_payload:
        payload.update(dict(extra_payload))

    # HLT-03：同一长者同一天同一类复核只留一条**未关闭**的单。
    # 补录链路（fill_vitals / supersede / 主诉 / 采集落库）每次都走到这里，
    # 以前每次 `session.add` 一张新待办，护工关掉一张还有一张；黄档还开着时
    # 补录出红值也只是多出一张黄单，红值永远不升红。
    existing = await notification_service.find_open_daily_notification(
        session,
        kind="health_review",
        elder_id=elder_id,
        business_date=record.record_date,
    )
    if existing is not None:
        return await _reuse_review_notification(
            session, existing=existing, record=record, grade=grade, payload=payload
        )

    # 走 `notification_service.create_notification` 这唯一入口：kind/severity 闭集校验、
    # 默认 status、创建日志都在那一处。绕过它自己 `Notification(...)` + `session.add`，
    # 等于这两处校验对健康复核通知不生效（audit-reverse R-D8 抓到的那条）。
    task = await notification_service.create_notification(
        session,
        kind="health_review",
        severity=grade.grade,
        elder_id=elder_id,
        payload=payload,
        assignee_staff_id=assignee,
    )
    logger.info(
        "health_review_created",
        extra={
            "trace_id": get_trace_id() or "-",
            "notification_id": str(task.notification_id),
            "elder_id": str(elder_id),
            "grade": grade.grade,
            "has_assignee": assignee is not None,
        },
    )
    return task


# ------------------------------------------------------------------ 对外：代填


async def fill_vitals(
    session: AsyncSession,
    identity: Identity,
    *,
    elder_id: uuid.UUID,
    source: str,
    values: Mapping[str, object],
    record_date: datetime.date | None = None,
    complaint: str | None = None,
) -> HealthRecordView:
    """护工/医生代填（FR-HLT-06）：同一套分级引擎，写新 revision，黄/红建复核任务。

    - 只接受 `staff_scheduled` / `staff_active`（语音来源由采集子图写入）；
    - 范围外的负责长者一律 403 并审计（AC-32）；
    - 破获规则与语音渠道完全一致（同一个 `grade_vitals`）；
    - `complaint` 对应 PRD 第 5 问（「今天身体有什么不舒服？」的文本）。服务层已收，
      但 `app/api/health.py` 的 `FillVitalsRequest` 与 console 代填表单还没带这个字段，
      所以线上暂时恒为 `None`；口径差异已登记（见模块 docstring），补字段不需要再动这里。
    """
    if source not in STAFF_SOURCES:
        raise HealthValidationError("source must be staff_scheduled or staff_active")
    cleaned = normalise_values(values)
    complaint_text = (complaint or "").strip() or None
    grade = grade_with_complaint(
        grade_vitals(
            systolic=cleaned.get("systolic"),
            diastolic=cleaned.get("diastolic"),
            heart_rate=cleaned.get("heart_rate"),
            sleep_hours=cleaned.get("sleep_hours"),
            mood_score=cleaned.get("mood_score"),
        ),
        complaint_text,
    )

    staff_id = await _require_scope(session, identity, elder_id, action="health.fill")
    await _require_elder(session, elder_id, action="health.fill")

    record = await _append_version(
        session,
        elder_id=elder_id,
        record_date=record_date or today_in_shanghai(),
        source=source,
        values=cleaned,
        grade=grade,
        recorded_by_staff_id=staff_id or _staff_uuid(identity),
        complaint=complaint_text,
    )
    task = await _create_review_notification(
        session,
        elder_id=elder_id,
        record=record,
        grade=grade,
        source=source,
        extra_payload=(
            {"complaint": complaint_text, "complaint_length": len(complaint_text)}
            if complaint_text is not None
            else None
        ),
    )
    _audit(
        identity,
        action="health.fill",
        target=f"elder:{elder_id}",
        result="success",
        grade=grade.grade,
        revision=record.revision,
        notification=str(task.notification_id) if task is not None else None,
    )
    return _view(record, notification_id=task.notification_id if task is not None else None)


async def supersede(
    session: AsyncSession,
    identity: Identity,
    *,
    record_id: uuid.UUID,
    values: Mapping[str, object],
) -> tuple[HealthRecordView, HealthRecordView]:
    """补录：为同一天追加新 revision，原行只写 `superseded_by`，值一个不动（FR-HLT-04）。

    返回 `(新版本, 原版本)`；原版本已被补录过则 409（幂等键是「当前版本」）。
    """
    previous = await session.get(HealthRecord, record_id)
    if previous is None:
        _audit(None, action="health.supersede", target=f"record:{record_id}", result="not_found")
        raise HealthNotFoundError(str(record_id))
    await _require_scope(session, identity, previous.elder_id, action="health.supersede")
    if previous.superseded_by is not None:
        _audit(
            identity,
            action="health.supersede",
            target=f"record:{record_id}",
            result="conflict",
            reason="already_superseded",
        )
        raise HealthConflictError("record has already been superseded")

    cleaned = normalise_values(values)
    grade = grade_vitals(
        systolic=cleaned.get("systolic"),
        diastolic=cleaned.get("diastolic"),
        heart_rate=cleaned.get("heart_rate"),
        sleep_hours=cleaned.get("sleep_hours"),
        mood_score=cleaned.get("mood_score"),
    )
    record = await _append_version(
        session,
        elder_id=previous.elder_id,
        record_date=previous.record_date,
        source="staff_active",  # 补录操作者是员工；原始来源留在 previous.source
        values=cleaned,
        grade=grade,
        recorded_by_staff_id=_staff_uuid(identity),
    )
    task = await _create_review_notification(
        session,
        elder_id=previous.elder_id,
        record=record,
        grade=grade,
        source=record.source,
        extra_payload={"supersedes": str(previous.record_id)},
    )
    _audit(
        identity,
        action="health.supersede",
        target=f"record:{previous.record_id}",
        result="success",
        grade=grade.grade,
        revision=record.revision,
    )
    return (
        _view(record, notification_id=task.notification_id if task is not None else None),
        _view(previous),
    )


async def revoke_record(
    session: AsyncSession,
    identity: Identity,
    *,
    record_id: uuid.UUID,
) -> HealthRecordView:
    """撤销一条健康记录（FR-HLT-04 补撤销口径）：软删留痕，不物理删除。

    为什么是软删而不是 `DELETE`：健康记录是审计链的一环（版本链 `superseded_by`
    还指向它），物理删会把留痕一起抹掉。撤销只表达「这条事实不再作为依据」：
    `revoked_at` + `revoked_by` 记下谁在什么时候撤的，行与版本链原样保留。

    权限：只有医生/管理员能撤销（护工是录入方，自己撤自己的录入等于无痕改数）。
    撤销后：日报聚合与趋势查询不再纳入它（见 `list_trend` 与 report_service）。

    幂等口径与补录一致：已撤销再撤 → 409；被补录过的旧版本不能再撤
    （撤销对象只能是当前版本，旧版本已不在任何对外视图里）。
    """
    try:
        check_roles(identity, ("admin", "doctor"))
    except RoleForbiddenError:
        _audit(
            identity,
            action="health.revoke",
            target=f"record:{record_id}",
            result="forbidden",
            reason="role",
        )
        raise

    record = await session.get(HealthRecord, record_id)
    if record is None:
        _audit(identity, action="health.revoke", target=f"record:{record_id}", result="not_found")
        raise HealthNotFoundError(str(record_id))
    await _require_scope(session, identity, record.elder_id, action="health.revoke")

    if record.revoked_at is not None:
        _audit(
            identity,
            action="health.revoke",
            target=f"record:{record_id}",
            result="conflict",
            reason="already_revoked",
        )
        raise HealthConflictError("record has already been revoked")
    if record.superseded_by is not None:
        _audit(
            identity,
            action="health.revoke",
            target=f"record:{record_id}",
            result="conflict",
            reason="already_superseded",
        )
        raise HealthConflictError("superseded record cannot be revoked")

    record.revoked_at = datetime.datetime.now(datetime.UTC)
    record.revoked_by = _staff_uuid(identity)
    await session.flush()
    await session.refresh(record)
    _audit(
        identity,
        action="health.revoke",
        target=f"record:{record_id}",
        result="success",
        elder_id=str(record.elder_id),
        record_date=record.record_date.isoformat(),
    )
    logger.info(
        "health_record_revoked",
        extra={
            "trace_id": get_trace_id() or "-",
            "record_id": str(record_id),
            "elder_id": str(record.elder_id),
            "revoked_by": str(record.revoked_by) if record.revoked_by else None,
        },
    )
    return _view(record)


async def record_complaint(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    text: str,
    source: str = "voice_active",
    thread_id: str | None = None,
) -> HealthRecordView:
    """长者主动主诉（FR-HLT-07 的内部分支）：非急症主诉按黄档建复核任务。

    HLT-07 起 `complaint` 有独立列：原文同时写进健康记录行与复核通知 payload，
    行内是事实来源（可查、可撤销、进日报），通知 payload 只是展示副本。
    """
    if source not in VOICE_SOURCES:
        raise HealthValidationError("complaint source must be voice_scheduled or voice_active")
    complaint = (text or "").strip()
    if not complaint:
        raise HealthValidationError("complaint text is required")

    await _require_elder(session, elder_id, action="health.complaint")
    grade = GradeResult("yellow", ("complaint",))
    record = await _append_version(
        session,
        elder_id=elder_id,
        record_date=today_in_shanghai(),
        source=source,
        values={},
        grade=grade,
        recorded_by_staff_id=None,
        complaint=complaint,
    )
    task = await _create_review_notification(
        session,
        elder_id=elder_id,
        record=record,
        grade=grade,
        source=source,
        thread_id=thread_id,
        extra_payload={"complaint": complaint, "complaint_length": len(complaint)},
    )
    _audit(
        None,
        action="health.complaint",
        target=f"elder:{elder_id}",
        result="success",
        notification=str(task.notification_id) if task is not None else None,
    )
    return _view(record, notification_id=task.notification_id if task is not None else None)


async def persist_checkup_state(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    state: Mapping[str, Any],
) -> HealthRecordView:
    """把健康子图的终态落库（语音采集 / HITL resume 后的统一写入口）。

    子图只算状态、不碰数据库（graph → services 单向依赖）：`answers` 里的五字段
    在这里过一遍同一套校验与分级，再追加版本。缺项按缺失判黄（PRD §4.6）。
    """
    answers = state.get("answers") or {}
    source = str(state.get("source") or "voice_scheduled")
    if source not in ALL_SOURCES:
        raise HealthValidationError("state carries an unknown source")

    values: dict[str, float | int] = {}
    for key in MEASUREMENT_KEYS:
        raw = answers.get(key)
        if raw is None:
            continue
        try:
            values[key] = coerce_measurement(key, raw)
        except HealthValidationError:
            # 子图已经把非法答重问两次并标缺失；这里再遇到只可能是状态被手改，
            # 按缺失处理而不是让整条采集失败。
            continue

    grade = grade_vitals(
        systolic=values.get("systolic"),
        diastolic=values.get("diastolic"),
        heart_rate=values.get("heart_rate"),
        sleep_hours=values.get("sleep_hours"),
        mood_score=values.get("mood_score"),
    )
    record_date_raw = state.get("record_date")
    try:
        record_date = (
            datetime.date.fromisoformat(str(record_date_raw))
            if record_date_raw
            else today_in_shanghai()
        )
    except ValueError as exc:
        raise HealthValidationError("state carries an invalid record_date") from exc

    # 主诉链路（HLT-07）：图把长者原话放在 state["complaint"]，落库时写进行内，
    # 并作为复核单的「为什么建这一单」写进 payload —— 两处指向同一 record_id。
    # HLT-06：主诉是 PRD 的第 5 问，带着它的这一版等级不低于黄（五项全绿时也一样），
    # 否则「说了不舒服但五项正常」会落成绿档且没有复核任务。
    complaint = str(state.get("complaint") or "").strip() or None
    grade = grade_with_complaint(grade, complaint)
    record = await _append_version(
        session,
        elder_id=elder_id,
        record_date=record_date,
        source=source,
        values=values,
        grade=grade,
        recorded_by_staff_id=None,
        complaint=complaint,
    )
    task = await _create_review_notification(
        session,
        elder_id=elder_id,
        record=record,
        grade=grade,
        source=source,
        thread_id=str(state.get("thread_id") or "") or None,
        extra_payload=(
            {"complaint": complaint, "complaint_length": len(complaint)}
            if complaint is not None
            else None
        ),
    )
    _audit(
        None,
        action="health.checkup.persist",
        target=f"elder:{elder_id}",
        result="success",
        grade=grade.grade,
        revision=record.revision,
    )
    return _view(record, notification_id=task.notification_id if task is not None else None)


# ------------------------------------------------------------------ 采集会话（FR-HLT-01）

#: 每问等待时长（PRD §4.6 FR-HLT-01「每问等 60 秒」，同样是 §7 的硬性数值）。
CHECKUP_ANSWER_TIMEOUT_SECONDS = 60

#: 会话状态四态（模型 CHECK 同源）；前两态是「还活着」，后两态是终态。
CHECKUP_SESSION_STATUSES: tuple[str, ...] = ("asking", "awaiting_review", "completed", "ended")
CHECKUP_OPEN_STATUSES: tuple[str, ...] = ("asking", "awaiting_review")

#: 超时扫描单轮上限：一轮 tick 只推进这么多场，积压时不至于把 30 秒间隔跑穿。
CHECKUP_TIMEOUT_BATCH_LIMIT = 200


@dataclass(frozen=True, slots=True)
class CheckupSessionView:
    """采集会话的对外形状（API 直接序列化；不含 checkpoint 内部状态）。"""

    session_id: uuid.UUID
    elder_id: uuid.UUID
    record_date: datetime.date
    status: str
    source: str
    thread_id: str
    current_key: str | None
    prompt: dict[str, Any] | None
    expires_at: datetime.datetime | None
    attempt_no: int
    timeouts: int
    answers: dict[str, float | int]
    missing: list[str]
    created_at: datetime.datetime
    closed_at: datetime.datetime | None

    @property
    def active(self) -> bool:
        """会话是否仍在推进（`asking` / `awaiting_review`）。"""
        return self.status in CHECKUP_OPEN_STATUSES


def _checkup_now(now: datetime.datetime | None) -> datetime.datetime:
    """会话时刻统一 UTC；naive 按 UTC 解释（与 `inside_collection_window` 同口径）。"""
    moment = now if now is not None else datetime.datetime.now(datetime.UTC)
    return moment.replace(tzinfo=datetime.UTC) if moment.tzinfo is None else moment


def checkup_session_view(row: HealthCheckupSession) -> CheckupSessionView:
    """ORM 行 → 视图（JSONB 列必须是新对象，别把 ORM 里的 dict 直接交出去）。"""
    return CheckupSessionView(
        session_id=row.session_id,
        elder_id=row.elder_id,
        record_date=row.record_date,
        status=row.status,
        source=row.source,
        thread_id=row.thread_id,
        current_key=row.current_key,
        prompt=dict(row.prompt) if row.prompt else None,
        expires_at=row.expires_at,
        attempt_no=row.attempt_no,
        timeouts=row.timeouts,
        answers=dict(row.answers or {}),
        missing=list(row.missing or ()),
        created_at=row.created_at,
        closed_at=row.closed_at,
    )


async def load_checkup_session(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    record_date: datetime.date,
) -> HealthCheckupSession | None:
    """取当天会话行；没有则 None（调用方决定是「起一场」还是 404）。"""
    return (
        await session.execute(
            select(HealthCheckupSession)
            .where(
                HealthCheckupSession.elder_id == elder_id,
                HealthCheckupSession.record_date == record_date,
            )
            .limit(1)
        )
    ).scalar_one_or_none()


async def open_checkup_session(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    record_date: datetime.date,
    thread_id: str,
    source: str = "voice_scheduled",
) -> tuple[HealthCheckupSession, bool]:
    """建当天会话行；已存在就返回原行（返回 `(行, 是否新建)`）。

    幂等是这条链的前提：19:00 任务重跑、`--reload` 抖一下、手工补跑都不允许把
    同一位长者的会话重置回第一问（长者的回答在 checkpoint 里，重置会让他重答）。
    """
    existing = await load_checkup_session(session, elder_id=elder_id, record_date=record_date)
    if existing is not None:
        return existing, False
    row = HealthCheckupSession(
        elder_id=elder_id,
        record_date=record_date,
        thread_id=thread_id,
        status="asking",
        source=source,
        attempt_no=1,
        timeouts=0,
        answers={},
        missing=[],
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row, True


async def claim_checkup_session(
    session: AsyncSession,
    *,
    session_id: uuid.UUID,
    status: str,
    expected: str = "asking",
    now: datetime.datetime | None = None,
    close: bool = False,
) -> bool:
    """条件 UPDATE：仅当状态仍是 `expected` 才迁移到 `status`，返回是否抢到。

    为什么用 `UPDATE ... WHERE status = expected` 而不是「先读再写」：起问、超时扫描、
    长者回答三条路径可能同时指向同一场会话；条件 UPDATE 是不引入额外锁就能互斥的
    唯一手段——抢不到的一方直接跳过，不会把同一场采集落成两条 `health_records`
    版本（AC-11 的「不重复建单」正靠这道闸门）。
    """
    if status not in CHECKUP_SESSION_STATUSES:
        raise HealthValidationError(f"unknown checkup session status {status!r}")
    moment = _checkup_now(now)
    result = await session.execute(
        update(HealthCheckupSession)
        .where(
            HealthCheckupSession.session_id == session_id,
            HealthCheckupSession.status == expected,
        )
        .values(
            status=status,
            closed_at=moment if close else None,
            expires_at=None,
        )
    )
    return bool(cast(CursorResult[Any], result).rowcount)


async def update_checkup_prompt(
    session: AsyncSession,
    row: HealthCheckupSession,
    *,
    prompt: dict[str, Any] | None,
    current_key: str | None,
    attempt_no: int,
    answers: Mapping[str, Any],
    missing: list[str],
    expires_at: datetime.datetime | None,
    timeouts: int | None = None,
) -> None:
    """把「当前问哪题、等到几点、答到哪了」写回会话行。

    JSONB 列一律整体赋值（不能原地改 dict + 靠脏检查）：SQLAlchemy 对 `JSONB` 的
    默认比较是相等判断，原地改同一个对象在部分驱动下不会触发 UPDATE。
    """
    row.prompt = dict(prompt) if prompt else None
    row.current_key = current_key
    row.attempt_no = attempt_no
    row.answers = dict(answers)
    row.missing = list(missing)
    row.expires_at = expires_at
    if timeouts is not None:
        row.timeouts = timeouts
    await session.flush()


async def due_checkup_sessions(
    session: AsyncSession,
    *,
    now: datetime.datetime | None = None,
    limit: int = CHECKUP_TIMEOUT_BATCH_LIMIT,
) -> list[HealthCheckupSession]:
    """到期仍未回答的会话（`asking` 且 `expires_at <= now`），按到期先后。"""
    moment = _checkup_now(now)
    rows = await session.execute(
        select(HealthCheckupSession)
        .where(
            HealthCheckupSession.status == "asking",
            HealthCheckupSession.expires_at.is_not(None),
            HealthCheckupSession.expires_at <= moment,
        )
        .order_by(HealthCheckupSession.expires_at, HealthCheckupSession.session_id)
        .limit(max(limit, 1))
    )
    return list(rows.scalars().all())


async def close_checkup_session_by_thread(
    session: AsyncSession,
    *,
    thread_id: str,
    status: str = "completed",
    now: datetime.datetime | None = None,
) -> bool:
    """按线程键把进行中的会话关成终态（复核 resume 之后收尾用）。

    为什么不复用 `claim_checkup_session`：复核台手里只有通知 payload 里的 `thread_id`，
    拿不到 `session_id`；这里按线程键做同样的条件 UPDATE，语义一致（只关还活着的那行）。
    """
    if status not in CHECKUP_SESSION_STATUSES:
        raise HealthValidationError(f"unknown checkup session status {status!r}")
    moment = _checkup_now(now)
    result = await session.execute(
        update(HealthCheckupSession)
        .where(
            HealthCheckupSession.thread_id == thread_id,
            HealthCheckupSession.status.in_(CHECKUP_OPEN_STATUSES),
        )
        .values(status=status, closed_at=moment, expires_at=None)
    )
    return bool(cast(CursorResult[Any], result).rowcount)


async def _require_checkup_access(
    session: AsyncSession,
    identity: Identity,
    elder_id: uuid.UUID,
    *,
    action: str,
) -> None:
    """采集会话的读/答权限：设备只认自己绑定的长者，家属要 active 绑定，员工按范围。

    与 `_require_scope` 的差别只在「非员工主体」这一支：`_require_scope` 直接用
    `check_roles` 把家属/设备挡在 403，而采集会话恰恰是给 pad 与家属用的读数，
    所以这里先按主体种类分流，员工仍走同一套 assignment 范围判定（不写第二套）。
    """
    target = f"elder:{elder_id}"
    if identity.kind in (KIND_DEVICE, KIND_STAFF_CARD):
        # 设备/工牌的长者绑定来自 `devices.elder_id`（服务端读表，不由客户端指定）。
        if identity.elder_id != str(elder_id):
            _audit(
                identity, action=action, target=target, result="forbidden", reason="device_binding"
            )
            raise HealthScopeForbiddenError("elder is not bound to the current device")
        return
    if identity.kind == KIND_GUARDIAN:
        binding = (
            await session.execute(
                select(ElderGuardian.elder_id)
                .where(
                    ElderGuardian.elder_id == elder_id,
                    ElderGuardian.guardian_id == uuid.UUID(str(identity.guardian_id)),
                    ElderGuardian.status == "active",
                )
                .limit(1)
            )
        ).first()
        if binding is None:
            _audit(identity, action=action, target=target, result="forbidden", reason="guardian")
            raise HealthScopeForbiddenError("elder is not bound to the current guardian")
        return
    await _require_scope(session, identity, elder_id, action=action)


async def get_checkup_session_for(
    session: AsyncSession,
    identity: Identity,
    *,
    elder_id: uuid.UUID,
    record_date: datetime.date | None = None,
) -> CheckupSessionView:
    """按身份取当天采集会话（pad 拉问句、照护台看进度、家属看是否在采集）。

    顺序固定「身份/范围 → 存在性」：范围外的主体拿到的是 403 而不是 404（契约 §3），
    否则「有没有这场采集」本身就成了一条越权情报。
    """
    await _require_checkup_access(session, identity, elder_id, action="health.checkup.read")
    day = record_date if record_date is not None else today_in_shanghai()
    row = await load_checkup_session(session, elder_id=elder_id, record_date=day)
    if row is None:
        _audit(
            identity,
            action="health.checkup.read",
            target=f"elder:{elder_id}",
            result="not_found",
        )
        raise HealthNotFoundError("no health checkup session for today")
    return checkup_session_view(row)


# ------------------------------------------------------------------ 对外：读


async def list_records(
    session: AsyncSession,
    identity: Identity,
    *,
    elder_id: uuid.UUID | None = None,
    record_date: datetime.date | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[HealthRecordView], int]:
    """健康记录分页列表（照护台列表页）。

    过滤：`elder_id`（可选）、`date`（可选，单日）、只看当前版本（`superseded_by IS NULL`）；
    排序 `record_date DESC, revision DESC`，与列表页「最近记录在最上」的阅读顺序一致。

    范围口径与趋势一致：admin 全量；care_worker/doctor 只能看自己负责的长者
    （没传 `elder_id` 时按 `elder_staff_assignments` 收敛，而不是回落成全量）。
    """
    if page < 1:
        raise HealthValidationError("page must be >= 1")
    if page_size < 1 or page_size > 100:
        raise HealthValidationError("page_size must be between 1 and 100")

    conditions: list[Any] = [HealthRecord.superseded_by.is_(None)]

    if elder_id is not None:
        await _require_scope(session, identity, elder_id, action="health.list")
        conditions.append(HealthRecord.elder_id == elder_id)
    else:
        # 没指定长者时仍要收敛范围：admin 全量，其余按负责关系子查询过滤。
        try:
            check_roles(identity, ("admin", "care_worker", "doctor"))
        except RoleForbiddenError:
            _audit(
                identity, action="health.list", target="elder:*", result="forbidden", reason="role"
            )
            raise
        if identity.role != "admin":
            relation = _RELATION_BY_ROLE.get(identity.role or "")
            if relation is None:  # pragma: no cover - 角色枚举由 CHECK 保证
                raise HealthScopeForbiddenError("staff role has no health scope")
            conditions.append(
                HealthRecord.elder_id.in_(
                    select(ElderStaffAssignment.elder_id).where(
                        ElderStaffAssignment.staff_id == _staff_uuid(identity),
                        ElderStaffAssignment.relation == relation,
                    )
                )
            )

    if record_date is not None:
        conditions.append(HealthRecord.record_date == record_date)

    total = int(
        (
            await session.execute(select(func.count()).select_from(HealthRecord).where(*conditions))
        ).scalar_one()
    )
    rows = (
        await session.execute(
            select(HealthRecord)
            .where(*conditions)
            .order_by(HealthRecord.record_date.desc(), HealthRecord.revision.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars()
    views = [_view(row) for row in rows]

    _audit(
        identity,
        action="health.list",
        target=f"elder:{elder_id}" if elder_id is not None else "elder:*",
        result="success",
        total=total,
        page=page,
        page_size=len(views),
    )
    return views, total


async def list_trend(
    session: AsyncSession,
    identity: Identity,
    *,
    elder_id: uuid.UUID,
    days: int = 14,
    today: datetime.date | None = None,
) -> list[HealthRecordView]:
    """医生/护工趋势（FR-HLT-09）：近 N 天**当前版本**，按日期升序。

    只看当前版本：补录过的旧值属于审计链，不该在折线图上出现两次。
    已撤销的记录（HLT-04）同样排除：撤销的含义是「这条事实不作数」，
    留在折线图上等于撤销没生效。列表接口不过滤，以便照护台看到撤销痕迹。
    """
    if days <= 0 or days > 90:
        raise HealthValidationError("days must be between 1 and 90")
    await _require_scope(session, identity, elder_id, action="health.trend")
    await _require_elder(session, elder_id, action="health.trend")

    end = today or today_in_shanghai()
    start = end - datetime.timedelta(days=days - 1)
    rows = (
        await session.execute(
            select(HealthRecord)
            .where(
                HealthRecord.elder_id == elder_id,
                HealthRecord.record_date >= start,
                HealthRecord.record_date <= end,
                HealthRecord.superseded_by.is_(None),
                HealthRecord.revoked_at.is_(None),
            )
            .order_by(HealthRecord.record_date, HealthRecord.revision)
        )
    ).scalars()
    return [_view(row) for row in rows]


async def count_records(session: AsyncSession, elder_id: uuid.UUID) -> int:
    """某长者的记录总数（跨版本）；日报/验收用例用。"""
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(HealthRecord)
                .where(HealthRecord.elder_id == elder_id)
            )
        ).scalar_one()
    )


# ------------------------------------------------------------------ 对外：复核


async def get_review_task(
    session: AsyncSession, identity: Identity, *, notification_id: uuid.UUID
) -> ReviewTaskView:
    """读一条复核任务；非 `health_review` 一律按 404（不泄露其它通知的存在性）。"""
    task = await session.get(Notification, notification_id)
    if task is None or task.kind != "health_review":
        _audit(
            identity,
            action="health.review.read",
            target=f"notification:{notification_id}",
            result="not_found",
        )
        raise HealthNotFoundError(str(notification_id))
    if task.elder_id is not None:
        await _require_scope(session, identity, task.elder_id, action="health.review.read")
    else:
        try:
            check_roles(identity, ("admin",))
        except RoleForbiddenError:
            raise HealthScopeForbiddenError("system notification is admin-only") from None
    return _review_view(task)


async def mark_review_acknowledged(
    session: AsyncSession,
    identity: Identity,
    *,
    notification_id: uuid.UUID,
    decision: str = "acknowledged",
    note: str | None = None,
) -> ReviewTaskView:
    """人工复核完成：状态置 acknowledged，记录复核人、时间与裁决（幂等）。"""
    task = await session.get(Notification, notification_id)
    if task is None or task.kind != "health_review":
        _audit(
            identity,
            action="health.review.resolve",
            target=f"notification:{notification_id}",
            result="not_found",
        )
        raise HealthNotFoundError(str(notification_id))
    if task.elder_id is not None:
        await _require_scope(session, identity, task.elder_id, action="health.review.resolve")
    else:
        try:
            check_roles(identity, ("admin",))
        except RoleForbiddenError:
            raise HealthScopeForbiddenError("system notification is admin-only") from None

    payload = dict(task.payload or {})
    payload["review_decision"] = decision
    payload["reviewed_by_staff_id"] = str(_staff_uuid(identity))
    if note:
        payload["review_note"] = note[:500]
    task.payload = payload
    if task.status != "acknowledged":
        task.status = "acknowledged"
        task.acknowledged_at = datetime.datetime.now(tz=datetime.UTC)
    await session.flush()
    await session.refresh(task)
    _audit(
        identity,
        action="health.review.resolve",
        target=f"notification:{notification_id}",
        result="success",
        decision=decision,
    )
    return _review_view(task)


__all__ = [
    "ALL_SOURCES",
    "CHECKUP_ANSWER_TIMEOUT_SECONDS",
    "CHECKUP_OPEN_STATUSES",
    "CHECKUP_SESSION_STATUSES",
    "CHECKUP_TIMEOUT_BATCH_LIMIT",
    "DIASTOLIC_RED",
    "MEASUREMENTS",
    "MEASUREMENT_KEYS",
    "RULE_VERSION",
    "STAFF_SOURCES",
    "SYSTOLIC_RED",
    "VOICE_SOURCES",
    "CheckupSessionView",
    "Grade",
    "GradeResult",
    "HealthConflictError",
    "HealthNotFoundError",
    "HealthRecordView",
    "HealthScopeForbiddenError",
    "HealthServiceError",
    "HealthValidationError",
    "ReviewTaskView",
    "blood_pressure_ordered",
    "checkup_session_view",
    "claim_checkup_session",
    "close_checkup_session_by_thread",
    "coerce_measurement",
    "count_records",
    "due_checkup_sessions",
    "fill_vitals",
    "get_checkup_session_for",
    "get_review_task",
    "grade_vitals",
    "list_records",
    "list_trend",
    "load_checkup_session",
    "mark_review_acknowledged",
    "normalise_values",
    "open_checkup_session",
    "persist_checkup_state",
    "record_complaint",
    "revoke_record",
    "supersede",
    "today_in_shanghai",
    "update_checkup_prompt",
]
