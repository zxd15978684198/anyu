#!/usr/bin/env python
"""验收数据包：把三端每个页面要看的场景一次种齐（幂等、固定 UUID、仅本机库）。

与 `scripts/seed_demo.py` 的分工：

- `seed_demo.py` 是**演示账号**的唯一来源（四端登录、1 位长者、2 张药单）；
- 本脚本是**验收数据集**的唯一来源：先跑一遍 `seed_demo`，再复用
  `scripts/seed_media.py` / `app/services/knowledge_seed.py` 两份既有灌库实现，
  最后补齐「每个页面都能看到至少一个非空场景」的业务数据。

一条命令：

    uv run python scripts/seed_acceptance.py

只看覆盖是否齐全（不写库）：

    uv run python scripts/seed_acceptance.py --check

## 覆盖什么

| 页面/链路 | 数据 |
| --- | --- |
| 照护台首页 | 多状态长者/房间/设备、未结红黄工单、今日用药、红标复核、记忆切片 |
| 长者档案 | active / away / transferred / archived 四种状态，含无床位长者 |
| 床位/设备 | 双人间满房、部分入住、维修中、空房；online/offline/locked、已绑/未绑 |
| 员工/家属 | 三角色 + 停用护工；两位家属账号、active/unbound 两种绑定 |
| 用药管理 | active/stopped、续方链、7 天内到期、今日 calling/waiting_confirm/confirmed、
  历史 missed/refused/notification_failed/late_taken，T0→+15 四次触达 |
| 健康采集 | green/yellow/red/missing 四档、补录 revision=2、撤销留痕、四种来源、趋势断线 |
| 作业看板 | 工单六态全覆盖、红黄分级、误报留痕、风险事件六类、状态流转链 |
| 消息中心 | 七类通知 × 三档严重度 × 六种状态，总数 > 20（可翻页） |
| 每日日报 | 3 位长者多天日报；同一自然日存在 v1 + v2 补录版本 |
| 长期记忆 | active/superseded/deleted、private/family、embedding NULL 降级样例 |
| 机构知识 | 4 份种子文档、retired 旧版、draft 草稿、分块检索可用 |
| 操作日志 | 四种 actor_type、越权拒绝、注入拦截、两种导出、今日与历史 |
| 家属端 | 张桂芬 / 李秀兰各一位绑定家属，各自看到日报与通知 |
| 长者端 | 张桂芬平板：今日待确认用药、健康/记忆/媒体素材可演示 |

## 幂等口径

- 固定 UUID + 固定业务键（房间号、床号、手机号、工单号、通知 dedupe_key、
  健康 `(elder_id, record_date, revision)`、用药 `(plan_id, scheduled_at)`）；
- 「补不换」：已存在的行只更新可变字段，不改主键；口令没变不重写哈希；
- 日期相对「今天」的流水（健康/用药/日报/工单/通知/审计）以日期为键：
  同一天重跑全 `unchanged`；换一天重跑只补新一天，不删旧数据；
- 先跑 `seed_demo` 再跑本脚本，重复执行不会产生第二套账号。

## 安全边界

只允许连本机演示库（`localhost` / `127.0.0.1` / compose 的 `postgres`）。
所有姓名、手机号、病情描述均为虚构演示值；不写真实长者数据、不写任何真实密钥。
异常值只作为「需复核」样例，不产生医疗诊断结论。
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import sys
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

_REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (_REPO_ROOT, _REPO_ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import seed_demo  # noqa: E402
import seed_media  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.core.runtime_context import Identity  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db import engine, session_factory  # noqa: E402
from app.models import (  # noqa: E402
    AuditLog,
    Bed,
    DailyReport,
    Device,
    Elder,
    ElderGuardian,
    ElderStaffAssignment,
    GuardianAccount,
    HealthRecord,
    KnowledgeChunk,
    KnowledgeDocument,
    MediaHistory,
    MediaLibraryItem,
    MedicationAttempt,
    MedicationEvent,
    MedicationPlan,
    MemoryChunk,
    Notification,
    RiskEvent,
    Room,
    StaffAccount,
    WorkOrder,
    WorkOrderTransition,
)
from app.services import health_service, knowledge_seed, knowledge_service, report_service  # noqa: E402
from app.services.embedding import DeterministicEmbeddingProvider  # noqa: E402
from app.services.medication_service import drug_snapshot_hash  # noqa: E402

SHANGHAI = seed_demo.SHANGHAI
TODAY = seed_demo._today()
NIL = uuid.UUID(int=0)

# ---- 验收实体固定主键（2222 号段，与 seed_demo 的 1111 演示账号并行） ----------
ROOM_B302 = uuid.UUID("22222222-2222-4222-8222-222222222201")
ROOM_C303 = uuid.UUID("22222222-2222-4222-8222-222222222202")
ROOM_D101 = uuid.UUID("22222222-2222-4222-8222-222222222203")
ROOM_E201 = uuid.UUID("22222222-2222-4222-8222-222222222204")

BED_B302_1 = uuid.UUID("22222222-2222-4222-8222-222222222301")
BED_B302_2 = uuid.UUID("22222222-2222-4222-8222-222222222302")
BED_C303_1 = uuid.UUID("22222222-2222-4222-8222-222222222303")
BED_D101_1 = uuid.UUID("22222222-2222-4222-8222-222222222304")
BED_D101_2 = uuid.UUID("22222222-2222-4222-8222-222222222305")
BED_E201_1 = uuid.UUID("22222222-2222-4222-8222-222222222306")

ELDER_LI = uuid.UUID("22222222-2222-4222-8222-222222222401")
ELDER_CHEN = uuid.UUID("22222222-2222-4222-8222-222222222402")
ELDER_ZHAO = uuid.UUID("22222222-2222-4222-8222-222222222403")
ELDER_SUN = uuid.UUID("22222222-2222-4222-8222-222222222404")
ELDER_ZHOU = uuid.UUID("22222222-2222-4222-8222-222222222405")

DEVICE_B302 = uuid.UUID("22222222-2222-4222-8222-222222222501")
DEVICE_C303 = uuid.UUID("22222222-2222-4222-8222-222222222502")
DEVICE_ZHOU = uuid.UUID("22222222-2222-4222-8222-222222222503")
DEVICE_E201 = uuid.UUID("22222222-2222-4222-8222-222222222504")

GUARDIAN_LIU = uuid.UUID("22222222-2222-4222-8222-222222222601")
GUARDIAN_WANG = uuid.UUID("22222222-2222-4222-8222-222222222602")

STAFF_DISABLED = uuid.UUID("22222222-2222-4222-8222-222222222701")

ASSIGN_LI_CARE = uuid.UUID("22222222-2222-4222-8222-222222222801")
ASSIGN_LI_DOCTOR = uuid.UUID("22222222-2222-4222-8222-222222222802")
ASSIGN_CHEN_CARE = uuid.UUID("22222222-2222-4222-8222-222222222803")
ASSIGN_CHEN_DOCTOR = uuid.UUID("22222222-2222-4222-8222-222222222804")
ASSIGN_ZHOU_CARE = uuid.UUID("22222222-2222-4222-8222-222222222805")
ASSIGN_ZHOU_DOCTOR = uuid.UUID("22222222-2222-4222-8222-222222222806")

LINK_LI_LIU = uuid.UUID("22222222-2222-4222-8222-222222222811")
LINK_CHEN_LIU = uuid.UUID("22222222-2222-4222-8222-222222222812")

PLAN_LI_NIFEDIPINE = uuid.UUID("22222222-2222-4222-8222-222222222901")
PLAN_LI_ASPIRIN_OLD = uuid.UUID("22222222-2222-4222-8222-222222222902")
PLAN_LI_ASPIRIN_NEW = uuid.UUID("22222222-2222-4222-8222-222222222903")
PLAN_CHEN_STOPPED = uuid.UUID("22222222-2222-4222-8222-222222222904")
PLAN_ZHOU_VALSARTAN = uuid.UUID("22222222-2222-4222-8222-222222222905")

GUARDIAN_LIU_PASSWORD = "Fam2@2026"  # noqa: S105 — 演示弱口令，非真实凭据  # pragma: allowlist secret
GUARDIAN_WANG_PASSWORD = "Fam3@2026"  # noqa: S105 — 演示弱口令，非真实凭据  # pragma: allowlist secret
DISABLED_STAFF_PASSWORD = "Care2@2026"  # noqa: S105 — 演示弱口令，非真实凭据  # pragma: allowlist secret

#: 验收设备只用于照护台列表展示，不走设备登录；token_hash 由固定串派生，不暴露可用明文。
_DEVICE_TOKEN_SEED: Final[str] = "anyu-acceptance-device-token"  # noqa: S105 — 派生哈希的固定串，非凭据

NEW_ELDERS: tuple[tuple[uuid.UUID, str, str | None, str, str | None], ...] = (
    (ELDER_LI, "李秀兰", "female", "active", None),
    (ELDER_CHEN, "陈国强", "male", "away", None),
    (ELDER_ZHAO, "赵德海", "male", "transferred", None),
    (ELDER_SUN, "孙玉梅", "female", "archived", None),
    (ELDER_ZHOU, "周文华", "female", "active", "20:30"),
)


# ------------------------------------------------------------------ 幂等底座


def _uid(label: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"anyu-acceptance:{label}")


def _at(day: datetime.date, hour: int, minute: int = 0) -> datetime.datetime:
    return datetime.datetime.combine(day, datetime.time(hour, minute), tzinfo=SHANGHAI)


def _days_ago(days: int) -> datetime.date:
    return TODAY - datetime.timedelta(days=days)


async def _ensure(
    session: AsyncSession,
    model: type[Any],
    pk_name: str,
    row_id: uuid.UUID,
    values: dict[str, Any],
    *,
    adopt_where: tuple[Any, ...] | None = None,
    immutable: frozenset[str] = frozenset(),
) -> tuple[str, Any]:
    """按主键或业务键认领一行；已存在只更新可变字段。"""
    row = await session.get(model, row_id)
    if row is None and adopt_where is not None:
        row = await session.scalar(select(model).where(*adopt_where))
    if row is None:
        session.add(model(**{pk_name: row_id, **values}))
        return "created", None
    changed: list[str] = []
    for field, value in values.items():
        if field in immutable:
            continue
        if getattr(row, field) != value:
            setattr(row, field, value)
            changed.append(field)
    return ("updated:" + ",".join(changed) if changed else "unchanged"), row


def _device_hash(device_id: uuid.UUID) -> str:
    return seed_demo._sha256_hex(f"{_DEVICE_TOKEN_SEED}:{device_id}")


# ------------------------------------------------------------------ 房间/长者/设备


async def _seed_rooms(session: AsyncSession, summary: list[tuple[str, str]]) -> None:
    rooms = (
        (ROOM_B302, "B-302", "full"),
        (ROOM_C303, "C-303", "maintenance"),
        (ROOM_D101, "D-101", "partial"),
        (ROOM_E201, "E-201", "available"),
    )
    for room_id, room_no, status in rooms:
        note, _ = await _ensure(
            session,
            Room,
            "room_id",
            room_id,
            {"room_no": room_no, "status": status},
        )
        summary.append((f"room     {room_no}", note))


async def _seed_elders(session: AsyncSession, summary: list[tuple[str, str]]) -> None:
    for elder_id, name, gender, status, checkup_start in NEW_ELDERS:
        values: dict[str, Any] = {
            "name": name,
            "gender": gender,
            "persona_id": seed_demo.PERSONA_ID,
            "status": status,
            "checkup_start": checkup_start,
        }
        note, _ = await _ensure(
            session,
            Elder,
            "elder_id",
            elder_id,
            values,
        )
        summary.append((f"elder    {name}({status})", note))


async def _seed_beds(session: AsyncSession, summary: list[tuple[str, str]]) -> None:
    beds = (
        (BED_B302_1, ROOM_B302, "1 号床", "occupied", ELDER_LI),
        (BED_B302_2, ROOM_B302, "2 号床", "occupied", ELDER_CHEN),
        (BED_C303_1, ROOM_C303, "1 号床", "maintenance", None),
        (BED_D101_1, ROOM_D101, "1 号床", "occupied", ELDER_ZHOU),
        (BED_D101_2, ROOM_D101, "2 号床", "available", None),
        (BED_E201_1, ROOM_E201, "1 号床", "available", None),
    )
    for bed_id, room_id, bed_no, status, elder_id in beds:
        note, _ = await _ensure(
            session,
            Bed,
            "bed_id",
            bed_id,
            {"room_id": room_id, "bed_no": bed_no, "status": status, "elder_id": elder_id},
            adopt_where=(Bed.room_id == room_id, Bed.bed_no == bed_no),
        )
        summary.append((f"bed      {bed_no}@{room_id.int % 1000}", note))


async def _seed_devices(session: AsyncSession, summary: list[tuple[str, str]]) -> None:
    now = datetime.datetime.now(tz=datetime.UTC)
    devices = (
        (DEVICE_B302, ROOM_B302, ELDER_LI, "online", now - datetime.timedelta(minutes=2)),
        (DEVICE_C303, ROOM_C303, None, "offline", now - datetime.timedelta(hours=3)),
        (DEVICE_ZHOU, ROOM_D101, ELDER_ZHOU, "locked", now - datetime.timedelta(days=1)),
        (DEVICE_E201, ROOM_E201, None, "offline", None),
    )
    for device_id, room_id, elder_id, status, beat in devices:
        note, _ = await _ensure(
            session,
            Device,
            "device_id",
            device_id,
            {
                "room_id": room_id,
                "elder_id": elder_id,
                "token_hash": _device_hash(device_id),
                "status": status,
                "last_heartbeat": beat,
            },
            # 心跳每跑一次都会变；只在新建设备时写，重跑不留无意义的 updated 噪声。
            immutable=frozenset({"last_heartbeat"}),
        )
        summary.append((f"device   {str(device_id)[-4:]}", note))


# ------------------------------------------------------------------ 员工/家属/绑定


async def _seed_people(session: AsyncSession, summary: list[tuple[str, str]]) -> None:
    note, _ = await _ensure(
        session,
        StaffAccount,
        "staff_id",
        STAFF_DISABLED,
        {
            "name": "赵护工",
            "role": "care_worker",
            "phone": "13800000003",
            "password_hash": hash_password(DISABLED_STAFF_PASSWORD),
            "status": "disabled",
            "token_version": 1,
        },
        adopt_where=(StaffAccount.phone == "13800000003",),
    )
    summary.append(("staff    赵护工(disabled)", note))

    guardians = (
        (GUARDIAN_LIU, "刘建国", "13500000003", GUARDIAN_LIU_PASSWORD, "active"),
        (GUARDIAN_WANG, "王丽", "13500000004", GUARDIAN_WANG_PASSWORD, "disabled"),
    )
    for guardian_id, name, phone, password, status in guardians:
        # 只按固定主键认：账号会被 elder_guardians.guardian_id 引用。
        row = await session.get(GuardianAccount, guardian_id)
        if row is None:
            session.add(
                GuardianAccount(
                    guardian_id=guardian_id,
                    name=name,
                    phone=phone,
                    password_hash=hash_password(password),
                    status=status,
                )
            )
            summary.append((f"guardian {name}", "created"))
            continue
        changed: list[str] = []
        if row.name != name:
            row.name = name
            changed.append("name")
        if row.status != status:
            row.status = status
            changed.append("status")
        if not seed_demo._password_matches(password, row.password_hash):
            row.password_hash = hash_password(password)
            changed.append("password")
        summary.append(
            (f"guardian {name}", "updated:" + ",".join(changed) if changed else "unchanged")
        )

    assignments = (
        (ASSIGN_LI_CARE, ELDER_LI, seed_demo.CARE_WORKER_ID, "primary_care"),
        (ASSIGN_LI_DOCTOR, ELDER_LI, seed_demo.DOCTOR_ID, "doctor"),
        (ASSIGN_CHEN_CARE, ELDER_CHEN, seed_demo.CARE_WORKER_ID, "primary_care"),
        (ASSIGN_CHEN_DOCTOR, ELDER_CHEN, seed_demo.DOCTOR_ID, "doctor"),
        (ASSIGN_ZHOU_CARE, ELDER_ZHOU, seed_demo.CARE_WORKER_ID, "primary_care"),
        (ASSIGN_ZHOU_DOCTOR, ELDER_ZHOU, seed_demo.DOCTOR_ID, "doctor"),
    )
    for row_id, elder_id, staff_id, relation in assignments:
        note, _ = await _ensure(
            session,
            ElderStaffAssignment,
            "id",
            row_id,
            {"elder_id": elder_id, "staff_id": staff_id, "relation": relation},
            adopt_where=(
                ElderStaffAssignment.elder_id == elder_id,
                ElderStaffAssignment.staff_id == staff_id,
                ElderStaffAssignment.relation == relation,
            ),
        )
        summary.append((f"assign   {relation}@{str(elder_id)[-4:]}", note))

    links = (
        (LINK_LI_LIU, ELDER_LI, GUARDIAN_LIU, "daughter", "active", _at(_days_ago(90), 10, 0)),
        (
            LINK_CHEN_LIU,
            ELDER_CHEN,
            GUARDIAN_LIU,
            "daughter",
            "unbound",
            _at(_days_ago(120), 10, 0),
        ),
    )
    for row_id, elder_id, guardian_id, relation, status, assigned_at in links:
        note, _ = await _ensure(
            session,
            ElderGuardian,
            "id",
            row_id,
            {
                "elder_id": elder_id,
                "guardian_id": guardian_id,
                "relation": relation,
                "status": status,
                "assigned_at": assigned_at,
            },
            adopt_where=(
                ElderGuardian.elder_id == elder_id,
                ElderGuardian.guardian_id == guardian_id,
            ),
            immutable=frozenset({"assigned_at"}),
        )
        summary.append((f"binding  {relation}@{str(elder_id)[-4:]}", note))


# ------------------------------------------------------------------ 用药计划


async def _seed_plans(session: AsyncSession, summary: list[tuple[str, str]]) -> None:
    plans = (
        (
            PLAN_LI_NIFEDIPINE,
            ELDER_LI,
            "硝苯地平",
            "10mg",
            "08:00",
            _days_ago(20),
            _days_ago(-5),
            "active",
            1,
            None,
            _at(_days_ago(1), 8, 5),
        ),
        (
            PLAN_LI_ASPIRIN_OLD,
            ELDER_LI,
            "阿司匹林",
            "100mg",
            "09:00",
            _days_ago(90),
            _days_ago(-2),
            "active",
            1,
            None,
            _at(_days_ago(2), 8, 5),
        ),
        (
            PLAN_LI_ASPIRIN_NEW,
            ELDER_LI,
            "阿司匹林",
            "100mg",
            "09:00",
            _days_ago(-3),
            None,
            "active",
            1,
            PLAN_LI_ASPIRIN_OLD,
            None,
        ),
        (
            PLAN_CHEN_STOPPED,
            ELDER_CHEN,
            "美托洛尔",
            "25mg",
            "07:30",
            _days_ago(120),
            _days_ago(1),
            "stopped",
            2,
            None,
            None,
        ),
        (
            PLAN_ZHOU_VALSARTAN,
            ELDER_ZHOU,
            "缬沙坦",
            "80mg",
            "20:00",
            _days_ago(30),
            None,
            "active",
            1,
            None,
            None,
        ),
    )
    for (
        plan_id,
        elder_id,
        drug_name,
        dose,
        schedule_cron,
        start_date,
        end_date,
        status,
        version,
        renewed_from,
        review_notified_at,
    ) in plans:
        # 只按固定主键认：药单会被用药事件的 plan_id 引用，认领到另一条主键会让
        # 事件 FK 指向错行；阿司匹林还有「新老两张同药同点」的续方链，业务键也认不出。
        note, _ = await _ensure(
            session,
            MedicationPlan,
            "plan_id",
            plan_id,
            {
                "elder_id": elder_id,
                "drug_name": drug_name,
                "dose": dose,
                "schedule_cron": schedule_cron,
                "start_date": start_date,
                "end_date": end_date,
                "version": version,
                "created_by": seed_demo.DOCTOR_ID,
                "status": status,
                "renewed_from_plan_id": renewed_from,
                "review_notified_at": review_notified_at,
            },
        )
        summary.append((f"plan     {drug_name}({status})", note))


# ------------------------------------------------------------------ 用药事件/触达


async def _upsert_event(
    session: AsyncSession,
    label: str,
    plan_id: uuid.UUID,
    elder_id: uuid.UUID,
    day: datetime.date,
    hour: int,
    minute: int,
    *,
    status: str,
    terminal_state: str | None,
    retry_count: int,
    drug_name: str,
    dose: str,
    triggered_by: str = "scheduler",
    failure_reason: str | None = None,
    late_taken: bool = False,
    confirmed_at: datetime.datetime | None = None,
    confirm_channel: str | None = None,
) -> uuid.UUID:
    event_id = _uid(f"med-event:{label}")
    scheduled_at = _at(day, hour, minute)
    snapshot = {"drug_name": drug_name, "dose": dose, "scheduled_at": scheduled_at.isoformat()}
    note, row = await _ensure(
        session,
        MedicationEvent,
        "reminder_id",
        event_id,
        {
            "plan_id": plan_id,
            "elder_id": elder_id,
            "scheduled_at": scheduled_at,
            "drug_snapshot": snapshot,
            "payload_hash": drug_snapshot_hash(snapshot),
            "triggered_by": triggered_by,
            "status": status,
            "terminal_state": terminal_state,
            "late_taken": late_taken,
            "failure_reason": failure_reason,
            "retry_count": retry_count,
            "confirmed_at": confirmed_at,
            "confirm_channel": confirm_channel,
        },
        adopt_where=(
            MedicationEvent.plan_id == plan_id,
            MedicationEvent.scheduled_at == scheduled_at,
        ),
        immutable=frozenset({"scheduled_at"}),
    )
    del note
    return getattr(row, "reminder_id", event_id) if row is not None else event_id


async def _upsert_attempts(
    session: AsyncSession,
    event_label: str,
    event_id: uuid.UUID,
    base_at: datetime.datetime,
    attempts: tuple[dict[str, Any], ...],
) -> None:
    for index, attempt in enumerate(attempts, start=1):
        await _ensure(
            session,
            MedicationAttempt,
            "attempt_id",
            _uid(f"med-attempt:{event_label}:{index}"),
            {
                "event_id": event_id,
                "attempt_no": index,
                "sent_at": base_at + datetime.timedelta(minutes=5 * (index - 1)),
                "receipt": attempt.get("receipt"),
                "tts_result": attempt.get("tts_result"),
                "response": attempt.get("response"),
                "response_channel": attempt.get("response_channel"),
            },
            adopt_where=(
                MedicationAttempt.event_id == event_id,
                MedicationAttempt.attempt_no == index,
            ),
            immutable=frozenset({"sent_at"}),
        )


async def _seed_medication_events(session: AsyncSession, summary: list[tuple[str, str]]) -> None:
    amlo_today = await _upsert_event(
        session,
        "zhang-amlo-today",
        seed_demo.PLAN_AMLODIPINE_ID,
        seed_demo.ELDER_ID,
        TODAY,
        8,
        0,
        status="confirmed",
        terminal_state="taken_reported",
        retry_count=0,
        drug_name="氨氯地平",
        dose="5mg",
        confirmed_at=_at(TODAY, 8, 1),
        confirm_channel="voice",
    )
    await _upsert_attempts(
        session,
        "zhang-amlo-today",
        amlo_today,
        _at(TODAY, 8, 0),
        (
            {
                "receipt": "card_rendered",
                "tts_result": "ok",
                "response": "吃了",
                "response_channel": "voice",
            },
        ),
    )

    metf_today = await _upsert_event(
        session,
        "zhang-metf-today",
        seed_demo.PLAN_METFORMIN_ID,
        seed_demo.ELDER_ID,
        TODAY,
        12,
        30,
        status="waiting_confirm",
        terminal_state=None,
        retry_count=1,
        drug_name="二甲双胍",
        dose="0.5g",
    )
    await _upsert_attempts(
        session,
        "zhang-metf-today",
        metf_today,
        _at(TODAY, 12, 30),
        (
            {
                "receipt": "card_rendered",
                "tts_result": "ok",
                "response": None,
                "response_channel": "none",
            },
            {
                "receipt": "tts_finished",
                "tts_result": "ok",
                "response": None,
                "response_channel": "none",
            },
        ),
    )

    amlo_yesterday = await _upsert_event(
        session,
        "zhang-amlo-yesterday",
        seed_demo.PLAN_AMLODIPINE_ID,
        seed_demo.ELDER_ID,
        _days_ago(1),
        8,
        0,
        status="escalated",
        terminal_state="missed_unconfirmed",
        retry_count=3,
        drug_name="氨氯地平",
        dose="5mg",
        failure_reason="no_answer",
    )
    await _upsert_attempts(
        session,
        "zhang-amlo-yesterday",
        amlo_yesterday,
        _at(_days_ago(1), 8, 0),
        tuple(
            {
                "receipt": "card_rendered",
                "tts_result": "ok",
                "response": None,
                "response_channel": "none",
            }
            for _ in range(4)
        ),
    )

    metf_yesterday = await _upsert_event(
        session,
        "zhang-metf-yesterday",
        seed_demo.PLAN_METFORMIN_ID,
        seed_demo.ELDER_ID,
        _days_ago(1),
        12,
        30,
        status="confirmed",
        terminal_state="refused",
        retry_count=1,
        drug_name="二甲双胍",
        dose="0.5g",
        confirmed_at=_at(_days_ago(1), 12, 36),
        confirm_channel="button",
    )
    await _upsert_attempts(
        session,
        "zhang-metf-yesterday",
        metf_yesterday,
        _at(_days_ago(1), 12, 30),
        (
            {
                "receipt": "card_rendered",
                "tts_result": "ok",
                "response": "不想吃",
                "response_channel": "voice",
            },
            {
                "receipt": "tts_finished",
                "tts_result": "ok",
                "response": "不想吃",
                "response_channel": "button",
            },
        ),
    )

    amlo_failed = await _upsert_event(
        session,
        "zhang-amlo-2d",
        seed_demo.PLAN_AMLODIPINE_ID,
        seed_demo.ELDER_ID,
        _days_ago(2),
        8,
        0,
        status="escalated",
        terminal_state="notification_failed",
        retry_count=1,
        drug_name="氨氯地平",
        dose="5mg",
        failure_reason="tts_failed",
    )
    await _upsert_attempts(
        session,
        "zhang-amlo-2d",
        amlo_failed,
        _at(_days_ago(2), 8, 0),
        (
            {
                "receipt": "none",
                "tts_result": "tts_failed",
                "response": None,
                "response_channel": "none",
            },
            {
                "receipt": "none",
                "tts_result": "tts_failed",
                "response": None,
                "response_channel": "none",
            },
        ),
    )

    amlo_late = await _upsert_event(
        session,
        "zhang-amlo-3d",
        seed_demo.PLAN_AMLODIPINE_ID,
        seed_demo.ELDER_ID,
        _days_ago(3),
        8,
        0,
        status="confirmed",
        terminal_state="late_taken_reported",
        retry_count=2,
        drug_name="氨氯地平",
        dose="5mg",
        late_taken=True,
        confirmed_at=_at(_days_ago(3), 8, 26),
        confirm_channel="button",
    )
    await _upsert_attempts(
        session,
        "zhang-amlo-3d",
        amlo_late,
        _at(_days_ago(3), 8, 0),
        (
            {
                "receipt": "card_rendered",
                "tts_result": "ok",
                "response": None,
                "response_channel": "none",
            },
            {
                "receipt": "tts_finished",
                "tts_result": "ok",
                "response": None,
                "response_channel": "none",
            },
            {
                "receipt": "tts_finished",
                "tts_result": "ok",
                "response": "已经吃了",
                "response_channel": "button",
            },
        ),
    )

    li_nif_today = await _upsert_event(
        session,
        "li-nif-today",
        PLAN_LI_NIFEDIPINE,
        ELDER_LI,
        TODAY,
        8,
        0,
        status="calling",
        terminal_state=None,
        retry_count=0,
        drug_name="硝苯地平",
        dose="10mg",
    )
    await _upsert_attempts(
        session,
        "li-nif-today",
        li_nif_today,
        _at(TODAY, 8, 0),
        (
            {
                "receipt": "card_rendered",
                "tts_result": "ok",
                "response": None,
                "response_channel": "none",
            },
        ),
    )

    li_asp_yesterday = await _upsert_event(
        session,
        "li-asp-yesterday",
        PLAN_LI_ASPIRIN_OLD,
        ELDER_LI,
        _days_ago(1),
        9,
        0,
        status="confirmed",
        terminal_state="taken_reported",
        retry_count=0,
        drug_name="阿司匹林",
        dose="100mg",
        confirmed_at=_at(_days_ago(1), 9, 2),
        confirm_channel="voice",
    )
    await _upsert_attempts(
        session,
        "li-asp-yesterday",
        li_asp_yesterday,
        _at(_days_ago(1), 9, 0),
        (
            {
                "receipt": "card_rendered",
                "tts_result": "ok",
                "response": "吃了",
                "response_channel": "voice",
            },
        ),
    )

    zhou_yesterday = await _upsert_event(
        session,
        "zhou-val-yesterday",
        PLAN_ZHOU_VALSARTAN,
        ELDER_ZHOU,
        _days_ago(1),
        20,
        0,
        status="confirmed",
        terminal_state="taken_reported",
        retry_count=0,
        drug_name="缬沙坦",
        dose="80mg",
        confirmed_at=_at(_days_ago(1), 20, 3),
        confirm_channel="button",
    )
    await _upsert_attempts(
        session,
        "zhou-val-yesterday",
        zhou_yesterday,
        _at(_days_ago(1), 20, 0),
        (
            {
                "receipt": "card_rendered",
                "tts_result": "ok",
                "response": "吃了",
                "response_channel": "button",
            },
        ),
    )

    chen_past = await _upsert_event(
        session,
        "chen-met-5d",
        PLAN_CHEN_STOPPED,
        ELDER_CHEN,
        _days_ago(5),
        7,
        30,
        status="confirmed",
        terminal_state="taken_reported",
        retry_count=0,
        drug_name="美托洛尔",
        dose="25mg",
        confirmed_at=_at(_days_ago(5), 7, 32),
        confirm_channel="button",
    )
    await _upsert_attempts(
        session,
        "chen-met-5d",
        chen_past,
        _at(_days_ago(5), 7, 30),
        (
            {
                "receipt": "card_rendered",
                "tts_result": "ok",
                "response": "吃了",
                "response_channel": "button",
            },
        ),
    )

    summary.append(("med events", "10 个场景事件 + 触达链"))


# ------------------------------------------------------------------ 健康记录


def _grade_of(systolic: int | None, diastolic: int | None) -> tuple[str, list[str] | None]:
    if systolic is None and diastolic is None:
        return "yellow", ["vitals_missing"]
    reasons: list[str] = []
    if systolic is not None and systolic >= 180:
        reasons.append("systolic>=180")
    if diastolic is not None and diastolic >= 120:
        reasons.append("diastolic>=120")
    if reasons:
        return "red", reasons
    if systolic is not None and systolic >= 140:
        reasons.append("systolic>=140")
    if diastolic is not None and diastolic >= 90:
        reasons.append("diastolic>=90")
    if reasons:
        return "yellow", reasons
    return "green", None


async def _upsert_health(
    session: AsyncSession,
    label: str,
    elder_id: uuid.UUID,
    day: datetime.date,
    *,
    source: str,
    systolic: int | None,
    diastolic: int | None,
    heart_rate: int | None,
    sleep_hours: Decimal | None,
    mood: int | None,
    grade: str,
    reasons: list[str] | None,
    revision: int = 1,
    staff_id: uuid.UUID | None = None,
    complaint: str | None = None,
    superseded_by: uuid.UUID | None = None,
    revoked_at: datetime.datetime | None = None,
    revoked_by: uuid.UUID | None = None,
    hour: int = 19,
) -> uuid.UUID:
    record_id = _uid(f"health:{label}")
    note, row = await _ensure(
        session,
        HealthRecord,
        "record_id",
        record_id,
        {
            "elder_id": elder_id,
            "record_date": day,
            "source": source,
            "systolic_mmhg": systolic,
            "diastolic_mmhg": diastolic,
            "heart_rate_bpm": heart_rate,
            "sleep_hours": sleep_hours,
            "mood_score": mood,
            "grade": grade,
            "abnormal_reasons": reasons,
            "revision": revision,
            "superseded_by": superseded_by,
            "recorded_by_staff_id": staff_id,
            "complaint": complaint,
            "revoked_at": revoked_at,
            "revoked_by": revoked_by,
            "collected_at": _at(day, hour, 5),
        },
        adopt_where=(
            HealthRecord.elder_id == elder_id,
            HealthRecord.record_date == day,
            HealthRecord.revision == revision,
        ),
        immutable=frozenset({"collected_at"}),
    )
    del note
    return getattr(row, "record_id", record_id) if row is not None else record_id


async def _seed_health(session: AsyncSession, summary: list[tuple[str, str]]) -> None:
    # 李秀兰：14 天，红黄绿缺失各一份，来源覆盖语音/代填，留一条 mood=2 供「建议关注」。
    for offset in range(14):
        day = _days_ago(offset)
        if offset == 6:
            await _upsert_health(
                session,
                f"li-missing-{day}",
                ELDER_LI,
                day,
                source="voice_scheduled",
                systolic=None,
                diastolic=None,
                heart_rate=None,
                sleep_hours=None,
                mood=None,
                grade="missing",
                reasons=["question_timeout"],
            )
            continue
        systolic = 132 + (offset * 5) % 26
        diastolic = 78 + (offset * 3) % 14
        if offset == 3:
            systolic, diastolic = 152, 96
        grade, reasons = _grade_of(systolic, diastolic)
        source = "staff_scheduled" if offset == 5 else "voice_scheduled"
        complaint = "昨晚睡得不太好" if offset == 1 else None
        # 最近两次（今天、昨天）都低自评 → 触发「建议关注」尾部连续信号（FR-HLT-10）。
        mood = 2 if offset in (0, 1) else 3 + offset % 3
        await _upsert_health(
            session,
            f"li-{day}",
            ELDER_LI,
            day,
            source=source,
            systolic=systolic,
            diastolic=diastolic,
            heart_rate=70 + offset % 9,
            sleep_hours=Decimal("6.8") if offset % 2 else Decimal("5.5"),
            mood=mood,
            grade=grade,
            reasons=reasons,
            staff_id=seed_demo.CARE_WORKER_ID if source == "staff_scheduled" else None,
            complaint=complaint,
            hour=19 if source == "voice_scheduled" else 10,
        )

    # 陈国强：away 前 3 天没有记录，形成「外出期间断采」。
    for offset in range(3, 10):
        day = _days_ago(offset)
        systolic = 128 + (offset * 4) % 20
        diastolic = 76 + (offset * 2) % 10
        grade, reasons = _grade_of(systolic, diastolic)
        await _upsert_health(
            session,
            f"chen-{day}",
            ELDER_CHEN,
            day,
            source="voice_scheduled",
            systolic=systolic,
            diastolic=diastolic,
            heart_rate=72,
            sleep_hours=Decimal("7.0"),
            mood=3 + offset % 2,
            grade=grade,
            reasons=reasons,
        )

    # 周文华：自定义 20:30 采集时段（FR-HLT-05）。
    for offset in range(8):
        day = _days_ago(offset)
        systolic = 136 + (offset * 6) % 18
        diastolic = 82 + (offset * 2) % 8
        grade, reasons = _grade_of(systolic, diastolic)
        await _upsert_health(
            session,
            f"zhou-{day}",
            ELDER_ZHOU,
            day,
            source="voice_scheduled" if offset % 2 else "voice_active",
            systolic=systolic,
            diastolic=diastolic,
            heart_rate=68 + offset % 6,
            sleep_hours=Decimal("6.5"),
            mood=3,
            grade=grade,
            reasons=reasons,
            hour=20,
            complaint="晚上有点心慌，想测一下" if offset == 1 else None,
        )

    # 赵德海：转出前的历史记录，用于「转出长者仍有历史」。
    for offset in (20, 21, 22):
        day = _days_ago(offset)
        await _upsert_health(
            session,
            f"zhao-{day}",
            ELDER_ZHAO,
            day,
            source="staff_active",
            systolic=130,
            diastolic=80,
            heart_rate=70,
            sleep_hours=Decimal("7.2"),
            mood=4,
            grade="green",
            reasons=None,
            staff_id=seed_demo.CARE_WORKER_ID,
            hour=9,
        )

    # 张桂芬：一条已撤销记录（15 天前，客服误重复录入），演示「软删留痕」。
    await _upsert_health(
        session,
        f"zhang-revoked-{_days_ago(15)}",
        seed_demo.ELDER_ID,
        _days_ago(15),
        source="staff_active",
        systolic=126,
        diastolic=78,
        heart_rate=69,
        sleep_hours=Decimal("7.0"),
        mood=4,
        grade="green",
        reasons=None,
        staff_id=seed_demo.DOCTOR_ID,
        complaint="有点头晕，量一下",
        revoked_at=_at(_days_ago(14), 9, 30),
        revoked_by=seed_demo.DOCTOR_ID,
        hour=9,
    )

    summary.append(("health", "4 位长者 / 四档分级 / 撤销样例"))


async def _supersede_red_and_revoke(session: AsyncSession, summary: list[tuple[str, str]]) -> None:
    """把张桂芬 2 天前补录成红档（revision=2），并让日报出现 v1→v2 两版。"""
    target_day = _days_ago(2)
    current = await session.scalar(
        select(HealthRecord).where(
            HealthRecord.elder_id == seed_demo.ELDER_ID,
            HealthRecord.record_date == target_day,
            HealthRecord.revision == 1,
            HealthRecord.superseded_by.is_(None),
            HealthRecord.revoked_at.is_(None),
        )
    )
    if current is not None:
        doctor = Identity(kind="staff", staff_id=str(seed_demo.DOCTOR_ID), role="doctor")
        await health_service.supersede(
            session,
            doctor,
            record_id=current.record_id,
            values={
                "systolic": 182,
                "diastolic": 118,
                "heart_rate": 92,
                "sleep_hours": 5.0,
                "mood_score": 2,
            },
        )
        summary.append(("health   2 天前补录红档", "created revision=2"))
    else:
        summary.append(("health   2 天前补录红档", "unchanged"))


# ------------------------------------------------------------------ 风险事件/工单


RISK_EVENTS: tuple[dict[str, Any], ...] = (
    {
        "label": "re-sos",
        "elder": seed_demo.ELDER_ID,
        "type": "sos",
        "severity": "red",
        "status": "escalated",
        "source_type": "decision_logs",
        "detail": {"rule_id": "sos_button", "room_no": "A-301", "matched": "床头呼叫铃"},
        "created": (1, 21, 10),
        "acknowledged": (1, 21, 12),
    },
    {
        "label": "re-health",
        "elder": seed_demo.ELDER_ID,
        "type": "health_alert",
        "severity": "red",
        "status": "escalated",
        "source_type": "health_records",
        "detail": {"grade": "red", "abnormal_reasons": ["systolic>=180"]},
        "created": (2, 10, 0),
        "acknowledged": (2, 10, 5),
    },
    {
        "label": "re-med",
        "elder": seed_demo.ELDER_ID,
        "type": "med_risk",
        "severity": "red",
        "status": "closed",
        "source_type": "medication_events",
        "detail": {"attempts": 4, "terminal_state": "missed_unconfirmed"},
        "created": (1, 8, 20),
        "acknowledged": (1, 8, 22),
        "closed": (1, 9, 0),
    },
    {
        "label": "re-sensitive",
        "elder": seed_demo.ELDER_ID,
        "type": "sensitive",
        "severity": "yellow",
        "status": "closed",
        "source_type": "decision_logs",
        "detail": {"category": "privacy_double_room", "matched": "另一位长者的姓名"},
        "created": (4, 15, 30),
        "acknowledged": (4, 15, 35),
        "closed": (4, 16, 0),
    },
    {
        "label": "re-emergency",
        "elder": ELDER_ZHOU,
        "type": "red_emergency",
        "severity": "red",
        "status": "closed",
        "source_type": "decision_logs",
        "detail": {"rule_id": "chest_pain", "matched": "胸口不舒服"},
        "created": (6, 2, 40),
        "acknowledged": (6, 2, 42),
        "closed": (6, 3, 30),
    },
    {
        "label": "re-safety",
        "elder": ELDER_ZHOU,
        "type": "safety_alert",
        "severity": "yellow",
        "status": "open",
        "source_type": "devices",
        "detail": {"fault": "屏幕触控间歇失灵"},
        "created": (4, 9, 15),
    },
    {
        "label": "re-collection",
        "elder": ELDER_ZHOU,
        "type": "health_alert",
        "severity": "yellow",
        "status": "escalated",
        "source_type": "health_records",
        "detail": {
            "missing_fields": ["systolic", "diastolic"],
            "record_date": _days_ago(1).isoformat(),
        },
        "created": (1, 20, 5),
        "acknowledged": (1, 20, 10),
    },
)


WORK_ORDERS: tuple[dict[str, Any], ...] = (
    {
        "label": "wo-sos",
        "seq": 9001,
        "elder": seed_demo.ELDER_ID,
        "event": "re-sos",
        "type": "sos",
        "severity": "red",
        "status": "new",
        "assignee": None,
        "created": (1, 21, 11),
        "note": "床头呼叫铃触发，待当班护工接单。",
    },
    {
        "label": "wo-health",
        "seq": 9002,
        "elder": seed_demo.ELDER_ID,
        "event": "re-health",
        "type": "health_alert",
        "severity": "red",
        "status": "in_progress",
        "assignee": seed_demo.DOCTOR_ID,
        "created": (2, 10, 2),
        "actual_handled": (2, 10, 20),
        "note": "已联系医生复核，安排复测。",
    },
    {
        "label": "wo-med",
        "seq": 9003,
        "elder": seed_demo.ELDER_ID,
        "event": "re-med",
        "type": "missed_medication",
        "severity": "red",
        "status": "resolved",
        "assignee": seed_demo.CARE_WORKER_ID,
        "created": (1, 8, 21),
        "actual_handled": (1, 9, 0),
        "note": "护工上门确认，长者已补服并记录。",
    },
    {
        "label": "wo-sensitive",
        "seq": 9004,
        "elder": seed_demo.ELDER_ID,
        "event": "re-sensitive",
        "type": "sensitive_alert",
        "severity": "yellow",
        "status": "acknowledged",
        "assignee": seed_demo.CARE_WORKER_ID,
        "created": (4, 15, 31),
        "note": "双人间隐私收敛触发，待当面说明。",
    },
    {
        "label": "wo-device",
        "seq": 9005,
        "elder": ELDER_ZHOU,
        "event": "re-safety",
        "type": "device_fault",
        "severity": "red",
        "status": "reopened",
        "assignee": seed_demo.CARE_WORKER_ID,
        "created": (4, 9, 16),
        "actual_handled": (4, 11, 0),
        "note": "触控问题复现，重新开单。",
    },
    {
        "label": "wo-collection",
        "seq": 9006,
        "elder": ELDER_ZHOU,
        "event": "re-collection",
        "type": "collection_missed",
        "severity": "yellow",
        "status": "new",
        "assignee": None,
        "created": (1, 20, 6),
        "note": "昨晚健康采集缺血压两项，待补采。",
    },
    {
        "label": "wo-followup",
        "seq": 9007,
        "elder": ELDER_LI,
        "event": None,
        "type": "follow_up",
        "severity": "yellow",
        "status": "closed",
        "assignee": seed_demo.CARE_WORKER_ID,
        "created": (5, 14, 0),
        "closed": (5, 16, 0),
        "actual_handled": (5, 15, 0),
        "false_alarm": True,
        "false_alarm_reason": "家属回访，非异常事件",
        "note": "家属想确认本周活动安排，已答复。",
    },
    {
        "label": "wo-drug",
        "seq": 9008,
        "elder": ELDER_LI,
        "event": None,
        "type": "drug_question",
        "severity": "yellow",
        "status": "resolved",
        "assignee": seed_demo.DOCTOR_ID,
        "created": (3, 10, 30),
        "actual_handled": (3, 11, 0),
        "note": "长者询问阿司匹林能否掰开服，医生已答复整片吞服。",
    },
    {
        "label": "wo-emergency",
        "seq": 9009,
        "elder": ELDER_ZHOU,
        "event": "re-emergency",
        "type": "safety_alert",
        "severity": "red",
        "status": "closed",
        "assignee": seed_demo.DOCTOR_ID,
        "created": (6, 2, 41),
        "closed": (6, 3, 30),
        "actual_handled": (6, 2, 50),
        "note": "医生到场评估，家属已同步，转院内观察。",
    },
    {
        "label": "wo-missed",
        "seq": 9010,
        "elder": seed_demo.ELDER_ID,
        "event": "re-med",
        "type": "missed_medication",
        "severity": "yellow",
        "status": "acknowledged",
        "assignee": seed_demo.CARE_WORKER_ID,
        "created": (3, 8, 25),
        "note": "补服完成，待记录。",
    },
)


async def _seed_risk_and_orders(session: AsyncSession, summary: list[tuple[str, str]]) -> None:
    event_ids: dict[str, uuid.UUID] = {}
    for spec in RISK_EVENTS:
        label = spec["label"]
        event_id = _uid(f"risk:{label}")
        day_offset, hour, minute = spec["created"]
        values: dict[str, Any] = {
            "elder_id": spec["elder"],
            "type": spec["type"],
            "severity": spec["severity"],
            "source_type": spec["source_type"],
            "source_id": None,
            "detail": spec["detail"],
            "status": spec["status"],
            "created_at": _at(_days_ago(day_offset), hour, minute),
            "acknowledged_at": (
                _at(_days_ago(*spec["acknowledged"][:1]), *spec["acknowledged"][1:])
                if "acknowledged" in spec
                else None
            ),
            "closed_at": (
                _at(_days_ago(*spec["closed"][:1]), *spec["closed"][1:])
                if "closed" in spec
                else None
            ),
        }
        await _ensure(
            session,
            RiskEvent,
            "event_id",
            event_id,
            values,
            immutable=frozenset({"created_at"}),
        )
        event_ids[label] = event_id

    for spec in WORK_ORDERS:
        label = spec["label"]
        order_id = _uid(f"work-order:{label}")
        day_offset, hour, minute = spec["created"]
        created_at = _at(_days_ago(day_offset), hour, minute)
        closed_at = (
            _at(_days_ago(*spec["closed"][:1]), *spec["closed"][1:]) if "closed" in spec else None
        )
        await _ensure(
            session,
            WorkOrder,
            "order_id",
            order_id,
            {
                "order_no": f"WO-{created_at:%Y%m%d}-{spec['seq']:04d}",
                "elder_id": spec["elder"],
                "event_id": event_ids.get(spec["event"]) if spec.get("event") else None,
                "type": spec["type"],
                "severity": spec["severity"],
                "status": spec["status"],
                "assignee_id": spec["assignee"],
                "false_alarm": spec.get("false_alarm", False),
                "false_alarm_reason": spec.get("false_alarm_reason"),
                "actual_handled_at": (
                    _at(_days_ago(*spec["actual_handled"][:1]), *spec["actual_handled"][1:])
                    if "actual_handled" in spec
                    else None
                ),
                "note": spec.get("note"),
                "dedupe_key": f"accept:{label}",
                "created_at": created_at,
                "closed_at": closed_at,
            },
            # 工单主键会被 work_order_transitions.order_id 引用，只按固定主键认。
            immutable=frozenset({"created_at", "dedupe_key"}),
        )
        await _seed_transitions(session, label, order_id, spec["status"], created_at, spec)

    summary.append(("workorders", f"{len(WORK_ORDERS)} 单 / 风险事件 {len(RISK_EVENTS)} 条"))


async def _seed_transitions(
    session: AsyncSession,
    label: str,
    order_id: uuid.UUID,
    final_status: str,
    created_at: datetime.datetime,
    spec: dict[str, Any],
) -> None:
    chains = {
        "new": (),
        "acknowledged": ("acknowledged",),
        "in_progress": ("acknowledged", "in_progress"),
        "resolved": ("acknowledged", "in_progress", "resolved"),
        "closed": ("acknowledged", "in_progress", "resolved", "closed"),
        "reopened": ("acknowledged", "in_progress", "resolved", "closed", "reopened"),
    }
    chain = chains[final_status]
    previous = "new"
    for index, to_status in enumerate(chain, start=1):
        actor = (
            None
            if (label == "wo-sos" and to_status == "acknowledged")
            else (spec.get("assignee") or seed_demo.ADMIN_ID)
        )
        await _ensure(
            session,
            WorkOrderTransition,
            "transition_id",
            _uid(f"work-order:{label}:transition:{index}"),
            {
                "order_id": order_id,
                "from_status": previous,
                "to_status": to_status,
                "actor_id": actor,
                "at": created_at + datetime.timedelta(minutes=8 * index),
                "note": f"验收数据：{previous} → {to_status}",
            },
            immutable=frozenset({"at"}),
        )
        previous = to_status


# ------------------------------------------------------------------ 通知


def _notification(
    label: str,
    elder: uuid.UUID | None,
    kind: str,
    severity: str,
    status: str,
    day_offset: int,
    hour: int,
    minute: int,
    payload: dict[str, Any],
    *,
    assignee: uuid.UUID | None = None,
    delivered: bool = False,
    acknowledged: bool = False,
) -> dict[str, Any]:
    created = _at(_days_ago(day_offset), hour, minute)
    return {
        "label": label,
        "elder": elder,
        "kind": kind,
        "severity": severity,
        "status": status,
        "created_at": created,
        "payload": payload,
        "assignee": assignee,
        "delivered_at": created + datetime.timedelta(minutes=1) if delivered else None,
        "acknowledged_at": created + datetime.timedelta(minutes=5) if acknowledged else None,
        "dedupe_key": f"accept:{label}",
    }


async def _seed_notifications(session: AsyncSession, summary: list[tuple[str, str]]) -> None:
    work_order_no = f"WO-{_days_ago(4):%Y%m%d}-9004"
    notifications = [
        _notification(
            "n-med-escalation",
            seed_demo.ELDER_ID,
            "medication_escalation",
            "red",
            "delivered",
            1,
            8,
            20,
            {
                "title": "用药升级：氨氯地平四次未确认",
                "summary": "氨氯地平 08:00 提醒四次无回应，已升级人工。",
                "body": "已按 T0/+5/+10/+15 触达，未收到确认，请护工上门核实。",
                "drug_name": "氨氯地平",
                "scheduled_at": _at(_days_ago(1), 8, 0).isoformat(),
                "attempts": 4,
                "order_no": f"WO-{_days_ago(1):%Y%m%d}-9003",
            },
            assignee=seed_demo.CARE_WORKER_ID,
            delivered=True,
        ),
        _notification(
            "n-med-review",
            ELDER_LI,
            "medication_review",
            "yellow",
            "acknowledged",
            1,
            8,
            5,
            {
                "title": "药单即将到期：阿司匹林",
                "summary": "阿司匹林 100mg 将于 2 天后到期，请医生复核是否续方。",
                "plan_id": str(PLAN_LI_ASPIRIN_OLD),
                "drug_name": "阿司匹林",
                "end_date": _days_ago(-2).isoformat(),
                "days_left": 2,
            },
            assignee=seed_demo.DOCTOR_ID,
            delivered=True,
            acknowledged=True,
        ),
        _notification(
            "n-collection",
            ELDER_ZHOU,
            "collection_missed",
            "yellow",
            "pending",
            1,
            20,
            10,
            {
                "title": "昨晚健康采集缺两项",
                "summary": "周文华昨晚 20:30 采集未完成，血压两项缺失。",
                "record_date": _days_ago(1).isoformat(),
                "missing_fields": ["systolic", "diastolic"],
                "thread_id": f"health:{ELDER_ZHOU}:{_days_ago(1).isoformat()}",
            },
            assignee=seed_demo.CARE_WORKER_ID,
        ),
        _notification(
            "n-safety",
            seed_demo.ELDER_ID,
            "safety_alert",
            "red",
            "acknowledged",
            1,
            21,
            12,
            {
                "title": "床头呼叫铃已响应",
                "summary": "A-301 床头呼叫铃触发，当班护工已到场。",
                "event_id": str(_uid("risk:re-sos")),
                "room_no": "A-301",
            },
            assignee=seed_demo.CARE_WORKER_ID,
            delivered=True,
            acknowledged=True,
        ),
        _notification(
            "n-attention",
            ELDER_LI,
            "attention_signal",
            "yellow",
            "simulated",
            1,
            19,
            30,
            {
                "title": "建议关注：情绪自评偏低",
                "summary": "李秀兰连续两天情绪自评 2 分，可安排护工多聊几句。",
                "signal": "mood_low",
                "days": 2,
                "suggestion": "安排护工在活动时间多陪伴，不谈医疗判断。",
            },
        ),
        _notification(
            "n-workorder",
            seed_demo.ELDER_ID,
            "work_order",
            "yellow",
            "delivered",
            4,
            15,
            40,
            {
                "title": "工单已接单",
                "summary": f"工单 {work_order_no} 已接单",
                "order_no": work_order_no,
                "to_status": "acknowledged",
            },
            assignee=seed_demo.CARE_WORKER_ID,
            delivered=True,
        ),
        _notification(
            "n-failed",
            seed_demo.ELDER_ID,
            "medication_escalation",
            "red",
            "failed",
            2,
            8,
            20,
            {
                "title": "用药提醒语音播报失败",
                "summary": "氨氯地平 08:00 语音播报失败，已转人工。",
                "drug_name": "氨氯地平",
                "failure_reason": "tts_failed",
                "attempts": 2,
            },
            assignee=seed_demo.CARE_WORKER_ID,
            delivered=True,
        ),
        _notification(
            "n-family-pending",
            ELDER_LI,
            "family_report",
            "green",
            "pending",
            0,
            7,
            10,
            {
                "title": "今日日报待推送",
                "summary": "李秀兰今日日报已生成，待推送家属。",
                "report_date": TODAY.isoformat(),
                "version": 1,
            },
        ),
    ]

    for index in range(8):
        day_offset = 7 + index
        notifications.append(
            _notification(
                f"n-history-{index}",
                ELDER_LI if index % 2 else seed_demo.ELDER_ID,
                "attention_signal" if index % 3 else "safety_alert",
                "yellow" if index % 4 else "red",
                "acknowledged" if index % 2 else "delivered",
                day_offset,
                9 + index % 8,
                15,
                {
                    "title": f"历史关注样例 {index + 1}",
                    "summary": "历史通知样例，用于消息中心翻页与筛选演示。",
                    "history": True,
                },
                assignee=seed_demo.CARE_WORKER_ID,
                delivered=True,
                acknowledged=index % 2 == 0,
            )
        )

    for spec in notifications:
        await _ensure(
            session,
            Notification,
            "notification_id",
            _uid(f"notification:{spec['label']}"),
            {
                "elder_id": spec["elder"],
                "kind": spec["kind"],
                "severity": spec["severity"],
                "status": spec["status"],
                "payload": spec["payload"],
                "assignee_staff_id": spec["assignee"],
                "delivered_at": spec["delivered_at"],
                "acknowledged_at": spec["acknowledged_at"],
                "dedupe_key": spec["dedupe_key"],
                "created_at": spec["created_at"],
            },
            adopt_where=(
                Notification.dedupe_key == spec["dedupe_key"],
                Notification.kind == spec["kind"],
            ),
            immutable=frozenset({"created_at", "dedupe_key"}),
        )
    summary.append(("notifications", f"{len(notifications)} 条显式场景 + 日报生成的通知"))


# ------------------------------------------------------------------ 记忆/媒体/知识/审计


async def _seed_memory(session: AsyncSession, summary: list[tuple[str, str]]) -> None:
    chunks = (
        (
            "zhang-opera",
            seed_demo.ELDER_ID,
            "private",
            0.95,
            "active",
            "张桂芬喜欢越剧《梁山伯与祝英台》，每周三下午会去活动室听戏。",
            40,
            False,
        ),
        (
            "zhang-daughter",
            seed_demo.ELDER_ID,
            "family",
            0.90,
            "active",
            "女儿张女士每周六上午来看望，喜欢带桂花糕。",
            30,
            False,
        ),
        (
            "zhang-old-address",
            seed_demo.ELDER_ID,
            "private",
            0.60,
            "superseded",
            "旧住址在城西老街，现已搬入康养社区。",
            120,
            False,
        ),
        (
            "zhang-temp-pref",
            seed_demo.ELDER_ID,
            "private",
            0.30,
            "deleted",
            "临时偏好：睡前想听广播，已改为听戏曲。",
            90,
            False,
        ),
        (
            "zhang-degraded",
            seed_demo.ELDER_ID,
            "private",
            0.70,
            "active",
            "最近提到晚上睡得浅，希望白天多走动。",
            3,
            True,
        ),
        (
            "zhang-tea",
            seed_demo.ELDER_ID,
            "private",
            0.80,
            "active",
            "喜欢喝温热的淡茶，不爱甜饮料。",
            15,
            False,
        ),
        (
            "li-grandson",
            ELDER_LI,
            "family",
            0.92,
            "active",
            "孙子在读大学，周末会视频通话。",
            20,
            False,
        ),
        (
            "li-calligraphy",
            ELDER_LI,
            "private",
            0.75,
            "active",
            "喜欢写毛笔字，偏爱行书。",
            25,
            False,
        ),
        (
            "li-old-room",
            ELDER_LI,
            "private",
            0.40,
            "superseded",
            "原先住 B-201，后调整到 B-302。",
            80,
            False,
        ),
        (
            "zhou-movie",
            ELDER_ZHOU,
            "private",
            0.85,
            "active",
            "喜欢看老电影，尤其是《我们村里的年轻人》。",
            35,
            False,
        ),
        (
            "zhou-walk",
            ELDER_ZHOU,
            "private",
            0.65,
            "active",
            "傍晚喜欢在花园慢走，需要有人陪同时会按铃。",
            12,
            True,
        ),
    )
    for label, elder_id, category, importance, status, content, days, no_embedding in chunks:
        await _ensure(
            session,
            MemoryChunk,
            "chunk_id",
            _uid(f"memory:{label}"),
            {
                "elder_id": elder_id,
                "content": content,
                "embedding": None if no_embedding else [0.01] * 1024,
                "category": category,
                "importance": Decimal(str(importance)),
                "status": status,
                "source_thread_id": f"seed:{label}",
                "created_at": _at(_days_ago(days), 10, 0),
                "last_accessed": None
                if status != "active"
                else _at(_days_ago(max(days - 1, 0)), 11, 0),
            },
            immutable=frozenset({"created_at"}),
        )
    summary.append(("memory", f"{len(chunks)} 条 / 三态 / 两类"))


async def _seed_media_history(session: AsyncSession, summary: list[tuple[str, str]]) -> None:
    items = (
        await session.scalars(select(MediaLibraryItem).order_by(MediaLibraryItem.title).limit(3))
    ).all()
    if not items:
        summary.append(("media history", "skip：media_library 为空，先跑 seed_media.py"))
        return
    created = 0
    for index, item in enumerate(items):
        for elder_id, label in ((seed_demo.ELDER_ID, "zhang"), (ELDER_LI, "li")):
            existing = await session.get(MediaHistory, _uid(f"media-history:{label}:{index}"))
            if existing is not None:
                continue
            session.add(
                MediaHistory(
                    history_id=_uid(f"media-history:{label}:{index}"),
                    elder_id=elder_id,
                    media_id=item.media_id,
                    started_at=_at(_days_ago(index + 1), 15, 30),
                    finished_at=_at(_days_ago(index + 1), 15, 31),
                    completion_rate=Decimal("0.85") if index % 2 == 0 else Decimal("0.40"),
                )
            )
            created += 1
    summary.append(("media history", f"created:{created}"))


async def _seed_knowledge(session: AsyncSession, summary: list[tuple[str, str]]) -> None:
    # 验收只验「链路是否走通」，不验召回质量；显式用确定性 provider，避免本机
    # 未配 embedding key（或代理变量写坏）时整包数据装到一半失败。语义质量另测。
    provider = DeterministicEmbeddingProvider()
    documents = knowledge_seed.load_seed_documents()
    actor_id = await knowledge_seed.resolve_actor(session, actor_id=seed_demo.ADMIN_ID)
    result = await knowledge_seed.seed_documents(
        session, documents, actor_id=actor_id, embedding_provider=provider
    )
    summary.append(
        (
            "knowledge seed",
            f"created:{len(result.created)} skip:{len(result.skipped)}",
        )
    )

    # retired 样例：活动说明 v1 → v2，v1 自动退役（只补一次）。
    v1 = await session.scalar(
        select(KnowledgeDocument).where(
            KnowledgeDocument.title == "本周活动室安排",
            KnowledgeDocument.version == 1,
        )
    )
    v2_exists = await session.scalar(
        select(func.count())
        .select_from(KnowledgeDocument)
        .where(
            KnowledgeDocument.title == "本周活动室安排",
            KnowledgeDocument.version == 2,
        )
    )
    if v1 is not None and v1.status == "active" and not v2_exists:
        await knowledge_service.supersede_document(
            session,
            document_id=v1.document_id,
            body=v1.body + "\n\n补充：本周日电影放映结束后，活动室延长开放到 20:30。",
            actor_id=actor_id,
            embedding_provider=provider,
        )
        summary.append(("knowledge retired", "活动说明 v1 → v2"))
    else:
        summary.append(("knowledge retired", "unchanged"))

    # draft 样例：管理端能按状态筛出未发布稿。
    draft_title = "康复活动室借用说明（草稿）"
    draft = await session.scalar(
        select(KnowledgeDocument).where(KnowledgeDocument.title == draft_title)
    )
    if draft is None:
        draft_id = _uid("knowledge:draft:rehab-room")
        body = (
            "康复活动室目前仅在工作日 09:00–11:00 开放借用，需提前一天到楼层服务台登记。"
            "借用期间请勿移动固定器械，使用完毕把靠垫放回原位。"
        )
        session.add(
            KnowledgeDocument(
                document_id=draft_id,
                title=draft_title,
                category="facility",
                authority_level="institution",
                version=1,
                effective_from=_at(TODAY, 0, 0),
                effective_until=None,
                status="draft",
                body=body,
                created_by=actor_id,
                updated_by=actor_id,
            )
        )
        # 文档与分块之间没有 relationship，UOW 排不出先后；先 flush 再插分块。
        await session.flush()
        session.add(
            KnowledgeChunk(
                chunk_id=_uid("knowledge:draft:chunk:0"),
                document_id=draft_id,
                chunk_index=0,
                content=body,
                embedding=[0.001] * 1024,
            )
        )
        summary.append(("knowledge draft", "created"))
    else:
        summary.append(("knowledge draft", "unchanged"))


def _audit_row(
    label: str,
    actor_type: str,
    actor_id: uuid.UUID,
    action: str,
    resource_type: str,
    detail: dict[str, Any],
    *,
    day_offset: int = 0,
    hour: int = 9,
    minute: int = 0,
) -> dict[str, Any]:
    return {
        "label": label,
        "audit_id": _uid(f"audit:{label}"),
        "actor_type": actor_type,
        "actor_id": actor_id,
        "action": action,
        "resource_type": resource_type,
        "resource_id": NIL,
        "detail": {"trace_id": f"seed-{label}", **detail},
        "created_at": _at(_days_ago(day_offset), hour, minute),
    }


async def _seed_audit(session: AsyncSession, summary: list[tuple[str, str]]) -> None:
    rows = (
        _audit_row(
            "tool-denied-whitelist",
            "system",
            NIL,
            "tool.denied",
            "tool",
            {"gate": "whitelist", "tool": "shell.exec"},
            day_offset=3,
        ),
        _audit_row(
            "tool-denied-budget",
            "staff",
            seed_demo.CARE_WORKER_ID,
            "tool.denied",
            "tool",
            {"gate": "reject_budget", "tool": "get_weather", "max_calls": 8},
            day_offset=2,
        ),
        _audit_row(
            "tool-denied-breaker",
            "staff",
            seed_demo.DOCTOR_ID,
            "tool.denied",
            "tool",
            {"gate": "reject_breaker", "tool": "get_weather"},
            day_offset=1,
        ),
        _audit_row(
            "injection-1",
            "system",
            NIL,
            "injection_blocked",
            "prompt",
            {"pattern": "ignore previous instructions", "source": "asr"},
            day_offset=0,
            hour=8,
            minute=12,
        ),
        _audit_row(
            "injection-2",
            "system",
            NIL,
            "injection_blocked",
            "prompt",
            {"pattern": "system prompt", "source": "knowledge_chunk"},
            day_offset=4,
        ),
        _audit_row(
            "audit-export",
            "staff",
            seed_demo.ADMIN_ID,
            "audit.export",
            "audit_logs",
            {"format": "csv", "rows": 42},
            day_offset=0,
            hour=10,
            minute=5,
        ),
        _audit_row(
            "memory-export",
            "staff",
            seed_demo.ADMIN_ID,
            "memory.export",
            "memory_chunks",
            {"elder_id": str(seed_demo.ELDER_ID), "chunks": 6},
            day_offset=0,
            hour=10,
            minute=20,
        ),
        _audit_row(
            "stats-denied",
            "guardian",
            GUARDIAN_LIU,
            "stats.overview.read",
            "stats",
            {"reason": "non_staff_channel", "role": None},
            day_offset=0,
            hour=9,
            minute=40,
        ),
        _audit_row(
            "report-scope-denied",
            "guardian",
            GUARDIAN_LIU,
            "reports.read",
            "daily_report",
            {"result": "forbidden", "reason": "scope", "elder_id": str(seed_demo.ELDER_ID)},
            day_offset=1,
        ),
        _audit_row(
            "health-role-denied",
            "guardian",
            GUARDIAN_LIU,
            "health.review.read",
            "notification",
            {"result": "forbidden", "reason": "role"},
            day_offset=2,
        ),
        _audit_row(
            "med-plan-update",
            "staff",
            seed_demo.DOCTOR_ID,
            "medication.plan.update",
            "medication_plan",
            {"version_before": 1, "version_after": 2, "changed": ["dose"]},
            day_offset=5,
        ),
        _audit_row(
            "voiceprint-delete",
            "device",
            seed_demo.DEVICE_ID,
            "voiceprints.delete",
            "voiceprints",
            {"samples": 3},
            day_offset=6,
        ),
        _audit_row(
            "scheduler-reminder",
            "system",
            NIL,
            "scheduler.medication_reminder",
            "medication_event",
            {"attempt_no": 1, "sent": True},
            day_offset=0,
            hour=8,
            minute=0,
        ),
        _audit_row(
            "workorder-transition",
            "staff",
            seed_demo.CARE_WORKER_ID,
            "workorder.transition",
            "work_order",
            {"from_status": "new", "to_status": "acknowledged"},
            day_offset=1,
        ),
    )
    for row in rows:
        await _ensure(
            session,
            AuditLog,
            "audit_id",
            row["audit_id"],
            {
                "actor_type": row["actor_type"],
                "actor_id": row["actor_id"],
                "action": row["action"],
                "resource_type": row["resource_type"],
                "resource_id": row["resource_id"],
                "detail": row["detail"],
                "created_at": row["created_at"],
            },
            immutable=frozenset({"created_at"}),
        )
    summary.append(("audit", f"{len(rows)} 条 / 四类主体"))


# ------------------------------------------------------------------ 日报


async def _seed_reports(session: AsyncSession, summary: list[tuple[str, str]]) -> None:
    targets = (
        (seed_demo.ELDER_ID, 7),
        (ELDER_LI, 5),
        (ELDER_ZHOU, 3),
    )
    for elder_id, days in targets:
        for offset in range(days):
            await report_service.generate(session, elder_id, _days_ago(offset))
    summary.append(("reports", "3 位长者多天（v1）"))


async def _regenerate_superseded_report(
    session: AsyncSession, summary: list[tuple[str, str]]
) -> None:
    """2 天前健康补录成红档后再跑一次日报：内容变了 → 追加新版本（FR-RPT-03）。"""
    report = await report_service.generate(session, seed_demo.ELDER_ID, _days_ago(2))
    summary.append(("reports   2 天前补录版", f"version={report.version}"))


# ------------------------------------------------------------------ 覆盖自检


CHECK_SQL: tuple[tuple[str, Any, int], ...] = ()


def _check_specs() -> tuple[tuple[str, Any, int], ...]:
    """(说明, 计数语句, 至少几条)。"""
    return (
        (
            "长者四状态",
            select(func.count(func.distinct(Elder.status))).where(
                Elder.status.in_(("active", "away", "transferred", "archived"))
            ),
            4,
        ),
        (
            "房间四状态",
            select(func.count(func.distinct(Room.status))).where(
                Room.status.in_(("available", "partial", "full", "maintenance"))
            ),
            4,
        ),
        (
            "床位三状态",
            select(func.count(func.distinct(Bed.status))).where(
                Bed.status.in_(("available", "occupied", "maintenance"))
            ),
            3,
        ),
        (
            "设备三状态",
            select(func.count(func.distinct(Device.status))).where(
                Device.status.in_(("online", "offline", "locked"))
            ),
            3,
        ),
        (
            "设备已绑/未绑",
            select(func.count(func.distinct(Device.elder_id.is_(None)))).select_from(Device),
            2,
        ),
        (
            "停用员工",
            select(func.count()).select_from(StaffAccount).where(StaffAccount.status == "disabled"),
            1,
        ),
        (
            "家属绑定 active/unbound",
            select(func.count(func.distinct(ElderGuardian.status))).where(
                ElderGuardian.status.in_(("active", "unbound"))
            ),
            2,
        ),
        (
            "药单 active/stopped",
            select(func.count(func.distinct(MedicationPlan.status))).where(
                MedicationPlan.status.in_(("active", "stopped"))
            ),
            2,
        ),
        (
            "药单续方链",
            select(func.count())
            .select_from(MedicationPlan)
            .where(MedicationPlan.renewed_from_plan_id.is_not(None)),
            1,
        ),
        (
            "药单 7 天内到期",
            select(func.count())
            .select_from(MedicationPlan)
            .where(
                MedicationPlan.status == "active",
                MedicationPlan.end_date.is_not(None),
                MedicationPlan.end_date <= TODAY + datetime.timedelta(days=7),
            ),
            1,
        ),
        (
            # 种入时是 calling/waiting/confirmed/escalated 四态；backend 在跑时调度器会把
            # 已到点的 calling 推进成 waiting_confirm（正常业务行为），所以这里只要求
            # 仍有 3 种进行态可看，四态齐全在「种完立刻 check」时自然满足。
            "用药事件进行态（≥3）",
            select(func.count(func.distinct(MedicationEvent.status))).where(
                MedicationEvent.status.in_(("calling", "waiting_confirm", "confirmed", "escalated"))
            ),
            3,
        ),
        (
            "用药五终态",
            select(func.count(func.distinct(MedicationEvent.terminal_state))).where(
                MedicationEvent.terminal_state.is_not(None)
            ),
            5,
        ),
        (
            "四次触达 attempt_no 1..4",
            select(func.count(func.distinct(MedicationAttempt.attempt_no))),
            4,
        ),
        (
            "语音播报失败样例",
            select(func.count())
            .select_from(MedicationAttempt)
            .where(MedicationAttempt.tts_result == "tts_failed"),
            1,
        ),
        (
            "健康四档",
            select(func.count(func.distinct(HealthRecord.grade))).where(
                HealthRecord.grade.in_(("green", "yellow", "red", "missing"))
            ),
            4,
        ),
        (
            "健康四种来源",
            select(func.count(func.distinct(HealthRecord.source))).where(
                HealthRecord.source.in_(
                    ("voice_scheduled", "voice_active", "staff_scheduled", "staff_active")
                )
            ),
            4,
        ),
        (
            "健康补录 revision>1",
            select(func.count()).select_from(HealthRecord).where(HealthRecord.revision > 1),
            1,
        ),
        (
            "健康撤销留痕",
            select(func.count())
            .select_from(HealthRecord)
            .where(HealthRecord.revoked_at.is_not(None)),
            1,
        ),
        (
            "连续低自评（建议关注）",
            select(func.count())
            .select_from(HealthRecord)
            .where(HealthRecord.mood_score <= 2, HealthRecord.revoked_at.is_(None)),
            2,
        ),
        (
            "工单六态",
            select(func.count(func.distinct(WorkOrder.status))).where(
                WorkOrder.status.in_(
                    ("new", "acknowledged", "in_progress", "resolved", "closed", "reopened")
                )
            ),
            6,
        ),
        (
            "工单风险类型 ≥6",
            select(func.count(func.distinct(WorkOrder.type))),
            6,
        ),
        (
            "工单误报留痕",
            select(func.count()).select_from(WorkOrder).where(WorkOrder.false_alarm.is_(True)),
            1,
        ),
        (
            "工单流转链",
            select(func.count()).select_from(WorkOrderTransition),
            10,
        ),
        (
            "风险事件六类型",
            select(func.count(func.distinct(RiskEvent.type))),
            6,
        ),
        (
            "风险事件三状态",
            select(func.count(func.distinct(RiskEvent.status))).where(
                RiskEvent.status.in_(("open", "escalated", "closed"))
            ),
            3,
        ),
        (
            "通知七类",
            select(func.count(func.distinct(Notification.kind))).where(
                Notification.kind.in_(
                    (
                        "health_review",
                        "medication_escalation",
                        "medication_review",
                        "collection_missed",
                        "family_report",
                        "safety_alert",
                        "attention_signal",
                    )
                )
            ),
            7,
        ),
        (
            "通知三档严重度",
            select(func.count(func.distinct(Notification.severity))).where(
                Notification.severity.in_(("red", "yellow", "green"))
            ),
            3,
        ),
        (
            "通知六状态",
            select(func.count(func.distinct(Notification.status))).where(
                Notification.status.in_(
                    ("generated", "pending", "delivered", "acknowledged", "failed", "simulated")
                )
            ),
            6,
        ),
        ("通知可翻页（>20）", select(func.count()).select_from(Notification), 21),
        (
            "日报覆盖长者 ≥3",
            select(func.count(func.distinct(DailyReport.elder_id))),
            3,
        ),
        (
            "日报存在 v2",
            select(func.count()).select_from(DailyReport).where(DailyReport.version > 1),
            1,
        ),
        (
            "记忆三状态",
            select(func.count(func.distinct(MemoryChunk.status))).where(
                MemoryChunk.status.in_(("active", "superseded", "deleted"))
            ),
            3,
        ),
        (
            "记忆 embedding 降级",
            select(func.count()).select_from(MemoryChunk).where(MemoryChunk.embedding.is_(None)),
            1,
        ),
        (
            "知识 active/retired/draft",
            select(func.count(func.distinct(KnowledgeDocument.status))).where(
                KnowledgeDocument.status.in_(("active", "retired", "draft"))
            ),
            3,
        ),
        (
            "知识分块可检索",
            select(func.count()).select_from(KnowledgeChunk),
            4,
        ),
        (
            "媒体戏曲/电影",
            select(func.count(func.distinct(MediaLibraryItem.type))).where(
                MediaLibraryItem.type.in_(("opera", "movie"))
            ),
            2,
        ),
        (
            "媒体播放历史",
            select(func.count()).select_from(MediaHistory),
            1,
        ),
        (
            "审计四类主体",
            select(func.count(func.distinct(AuditLog.actor_type))).where(
                AuditLog.actor_type.in_(("staff", "guardian", "system", "device"))
            ),
            4,
        ),
        (
            "审计越权拒绝样例",
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "tool.denied"),
            1,
        ),
        (
            "审计注入拦截样例",
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "injection_blocked"),
            1,
        ),
        (
            "今日审计（首页安全卡片）",
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.created_at >= _at(TODAY, 0, 0)),
            1,
        ),
    )


async def _check(session: AsyncSession) -> bool:
    print("\n==== 覆盖自检 ====")
    ok = True
    for label, stmt, expected in _check_specs():
        actual = int(await session.scalar(stmt) or 0)
        passed = actual >= expected
        ok = ok and passed
        mark = "PASS" if passed else "FAIL"
        print(f"  [{mark}] {label:<28} {actual} / 需要 ≥{expected}")
    return ok


# ------------------------------------------------------------------ 主流程


async def _run(*, check_only: bool) -> int:
    seed_demo._ensure_local_database()
    if check_only:
        async with session_factory() as session:
            ok = await _check(session)
        await engine.dispose()
        return 0 if ok else 1

    # 复用既有入口而不是复制一份：演示账号、媒体清单、知识种子都只有一套实现。
    # 必须 await 内部的 `_run`，不能调 `seed_demo.main()` —— 后者会再起一个
    # `asyncio.run()`，在已运行的事件循环里直接 RuntimeError。
    seed_rc = await seed_demo._run(force_device=False)
    if seed_rc != 0:
        return seed_rc

    media_items = seed_media._load_items(seed_media.MANIFEST_PATH)
    media_rc = await seed_media._seed(media_items, dry_run=False)
    if media_rc != 0:
        return media_rc

    summary: list[tuple[str, str]] = []
    async with session_factory() as session:
        # 顺序即外键顺序；各表之间没有 relationship，必须在组间显式 flush，
        # 否则 UOW 可能先插子表，撞外键。
        for seeder in (
            _seed_rooms,
            _seed_elders,
            _seed_beds,
            _seed_people,
            _seed_devices,
            _seed_plans,
            _seed_medication_events,
            _seed_health,
            _seed_risk_and_orders,
            _seed_memory,
            _seed_media_history,
            _seed_audit,
            _seed_knowledge,
        ):
            await seeder(session, summary)
            await session.flush()

        await _seed_reports(session, summary)
        await session.flush()
        await _supersede_red_and_revoke(session, summary)
        await session.flush()
        await _regenerate_superseded_report(session, summary)
        await _seed_notifications(session, summary)
        await session.commit()

        print("\n==== 验收数据包 ====")
        for label, result in summary:
            print(f"  {label:<30} {result}")
        ok = await _check(session)

    await engine.dispose()
    print("\n==== 验收入口 ====")
    print("  照护台 /console  管理员 13700000000 / Admin@2026")
    print("  家属端 /family   张女士 13900000002 / Fam@2026（张桂芬）")
    print("  家属端 /family   刘建国 13500000003 / Fam2@2026（李秀兰）")
    print("  长者端 /         设备号 11111111-1111-4111-8111-111111111106")
    print("  （设备密钥见 docs/demo-accounts.md；本脚本不改既有设备凭据）")
    return 0 if ok else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="种入三端验收数据包（幂等，仅本机库）")
    parser.add_argument(
        "--check",
        action="store_true",
        help="只做覆盖自检，不写库（返回码 1 表示有缺口）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(check_only=args.check))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
