#!/usr/bin/env python
"""演示数据一键种入（四端账号 + 关联数据），**全固定 UUID、全幂等**。

一条命令把「四端都能登录、都能看到内容」的演示数据写进库：

| 角色 | 入口 | 账号 |
| --- | --- | --- |
| 长者端（平板） | `/` | 设备号 `11111111-1111-4111-8111-111111111106` + 设备密钥 |
| 管理员 | `/console` | `13700000000` / `Admin@2026` |
| 护工 | `/console` | `13800000002` / `Care@2026` |
| 医生 | `/console` | `13600000001` / `Doc@2026` |
| 家属 | `/family` | `13900000002` / `Fam@2026` |

为什么所有主键都写死：产品负责人反复遇到「账号又变了」。`create_account.py` 每次都
`gen_random_uuid()` 生成新主键，数据一被清空（见 AGENTS §9「跑测试清空开发库」），
重种出来的 account_id 就全变。本脚本把 12 个演示实体全部钉死成 `1111…1101`
之后的小号段，**重跑只补不换**：口令没变就不重写哈希，`token_version` 不动
（动了会把已登录的会话踢下线）。

与 `seed_personas.py` 的分工：那个脚本是 迭代 4 的 3D 链路种子（只到 device 为止），
提供 `--force-device` 之类轮换开关；本脚本是**演示用的总入口**，把账号、绑定、
药单、健康记录、日报、通知一次补齐，不改 `token_hash` 的既定值。

安全边界：只允许连本机库（`localhost` / `127.0.0.1` / compose 里的 `postgres`），
其余地址直接拒绝——这脚本写的是演示口令，不能碰任何真实环境。
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import hashlib
import sys
import uuid
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# `scripts/` 是 sys.path[0]，仓库根不在其中，直接 `import app...` 会 ModuleNotFoundError。
# 引导必须排在所有 app 导入之前（与 seed_personas.py / create_account.py 同款写法）。
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.security import PasswordHashInvalidError, hash_password, verify_password  # noqa: E402
from app.db import engine, session_factory  # noqa: E402
from app.models.guardian import GuardianAccount  # noqa: E402
from app.models.health import HealthRecord  # noqa: E402
from app.models.medication import MedicationPlan  # noqa: E402
from app.models.notification import Notification  # noqa: E402
from app.models.organization import Bed, Device, Elder, Persona, Room  # noqa: E402
from app.models.relation import ElderGuardian, ElderStaffAssignment  # noqa: E402
from app.models.report import DailyReport  # noqa: E402
from app.models.staff import StaffAccount  # noqa: E402
from app.services import report_service  # noqa: E402

SHANGHAI = ZoneInfo("Asia/Shanghai")

# ---- 固定主键（改动即等于换账号，产品负责人的浏览器就又要重新登录） ----------------
PERSONA_ID = uuid.UUID("11111111-1111-4111-8111-111111111201")
ROOM_ID = uuid.UUID("11111111-1111-4111-8111-111111111101")
ELDER_ID = uuid.UUID("11111111-1111-4111-8111-111111111102")
BED_ID = uuid.UUID("11111111-1111-4111-8111-111111111105")
DEVICE_ID = uuid.UUID("11111111-1111-4111-8111-111111111106")

ADMIN_ID = uuid.UUID("11111111-1111-4111-8111-111111111301")
CARE_WORKER_ID = uuid.UUID("11111111-1111-4111-8111-111111111302")
DOCTOR_ID = uuid.UUID("11111111-1111-4111-8111-111111111303")
GUARDIAN_ID = uuid.UUID("11111111-1111-4111-8111-111111111304")

CARE_ASSIGNMENT_ID = uuid.UUID("11111111-1111-4111-8111-111111111401")
DOCTOR_ASSIGNMENT_ID = uuid.UUID("11111111-1111-4111-8111-111111111402")
GUARDIAN_LINK_ID = uuid.UUID("11111111-1111-4111-8111-111111111403")

PLAN_AMLODIPINE_ID = uuid.UUID("11111111-1111-4111-8111-111111111501")
PLAN_METFORMIN_ID = uuid.UUID("11111111-1111-4111-8111-111111111502")

NOTIF_HEALTH_ID = uuid.UUID("11111111-1111-4111-8111-111111111601")
NOTIF_FAMILY_ID = uuid.UUID("11111111-1111-4111-8111-111111111602")

# ---- 固定凭证（README §5.2-A 与本文件必须一致） ------------------------------
DEVICE_SECRET = "demo-device-secret-0001"  # noqa: S105 — 演示用固定密钥，非真实凭据  # pragma: allowlist secret

STAFF_ACCOUNTS: tuple[tuple[uuid.UUID, str, str, str, str], ...] = (
    (ADMIN_ID, "王管理", "admin", "13700000000", "Admin@2026"),
    (CARE_WORKER_ID, "李护工", "care_worker", "13800000002", "Care@2026"),
    (DOCTOR_ID, "王医生", "doctor", "13600000001", "Doc@2026"),
)
GUARDIAN = (GUARDIAN_ID, "张女士", "13900000002", "Fam@2026")

_ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "postgres"})


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _today() -> datetime.date:
    return datetime.datetime.now(tz=SHANGHAI).date()


def _at(day: datetime.date, hour: int, minute: int) -> datetime.datetime:
    return datetime.datetime.combine(day, datetime.time(hour, minute), tzinfo=SHANGHAI)


def _ensure_local_database() -> None:
    """拒绝连非本机库：这里写的是演示口令，误跑到真实环境是事故。"""
    url = get_settings().database_url
    host = url.split("@")[-1].split("/")[0].rsplit(":", 1)[0].strip("[]") if "@" in url else ""
    if host not in _ALLOWED_HOSTS:
        raise SystemExit(f"拒绝执行：DATABASE_URL 指向 {host!r}，本脚本只允许本机演示库")


def _password_matches(password: str, stored: str) -> bool:
    try:
        return verify_password(password, stored)
    except PasswordHashInvalidError:
        return False


async def _adopt(session: AsyncSession, model: type[Any], *criteria: Any) -> Any | None:
    """按业务唯一键认领既有行。

    为什么不直接按固定主键插入：`login_with_phone` 用 `scalar_one_or_none()` 查手机号，
    **同号两行会直接 500**；药单同理（同一长者同一药名两行，照护台就出现重复条目）。
    早先脚本用随机主键种下的历史行必须被本脚本接管，不许并存。
    """
    return await session.scalar(select(model).where(*criteria))


# ---- 各实体的「补不换」逻辑 ----------------------------------------------------


async def _upsert_staff(
    session: AsyncSession, staff_id: uuid.UUID, name: str, role: str, phone: str, password: str
) -> tuple[str, uuid.UUID]:
    row = await session.get(StaffAccount, staff_id)
    if row is None:
        # 历史遗留行（早先用随机主键种下）必须先认领：同号两行会让 login 的
        # scalar_one_or_none() 直接 500，而换主键又会打断 plans.created_by 的外键。
        row = await _adopt(session, StaffAccount, StaffAccount.phone == phone)
    if row is None:
        session.add(
            StaffAccount(
                staff_id=staff_id,
                name=name,
                role=role,
                phone=phone,
                password_hash=hash_password(password),
                status="active",
            )
        )
        return "created", staff_id
    changed: list[str] = []
    if row.name != name:
        row.name = name
        changed.append("name")
    if row.role != role:
        row.role = role
        changed.append("role")
    if row.phone != phone:
        row.phone = phone
        changed.append("phone")
    if row.status != "active":
        row.status = "active"
        changed.append("status")
    if not _password_matches(password, row.password_hash):
        row.password_hash = hash_password(password)
        changed.append("password")
    note = "updated:" + ",".join(changed) if changed else "unchanged"
    return note, row.staff_id


async def _upsert_guardian(
    session: AsyncSession, guardian_id: uuid.UUID, name: str, phone: str, password: str
) -> tuple[str, uuid.UUID]:
    row = await session.get(GuardianAccount, guardian_id)
    if row is None:
        row = await _adopt(session, GuardianAccount, GuardianAccount.phone == phone)
    if row is None:
        session.add(
            GuardianAccount(
                guardian_id=guardian_id,
                name=name,
                phone=phone,
                password_hash=hash_password(password),
                status="active",
            )
        )
        return "created", guardian_id
    changed: list[str] = []
    if row.name != name:
        row.name = name
        changed.append("name")
    if row.phone != phone:
        row.phone = phone
        changed.append("phone")
    if row.status != "active":
        row.status = "active"
        changed.append("status")
    if not _password_matches(password, row.password_hash):
        row.password_hash = hash_password(password)
        changed.append("password")
    note = "updated:" + ",".join(changed) if changed else "unchanged"
    return note, row.guardian_id


async def _upsert_persona(session: AsyncSession) -> str:
    row = await session.get(Persona, PERSONA_ID)
    if row is None:
        session.add(
            Persona(
                persona_id=PERSONA_ID,
                name="小满",
                personality="温和、耐心、话短句、多鼓励，不主动提医疗判断",
                greeting="您好呀，我是小满，今天感觉怎么样？",
                voice_id="zh-CN-XiaoxiaoNeural",
                avatar_url="/avatars/vrm1_sample.vrm",
            )
        )
        return "created"
    changed: list[str] = []
    if row.avatar_url != "/avatars/vrm1_sample.vrm":
        row.avatar_url = "/avatars/vrm1_sample.vrm"
        changed.append("avatar_url")
    if row.voice_id != "zh-CN-XiaoxiaoNeural":
        row.voice_id = "zh-CN-XiaoxiaoNeural"
        changed.append("voice_id")
    return "updated:" + ",".join(changed) if changed else "unchanged"


async def _upsert_room(session: AsyncSession) -> str:
    row = await session.get(Room, ROOM_ID)
    if row is None:
        session.add(Room(room_id=ROOM_ID, room_no="A-301", status="full"))
        return "created"
    changed: list[str] = []
    if row.room_no != "A-301":
        row.room_no = "A-301"
        changed.append("room_no")
    if row.status != "full":
        row.status = "full"
        changed.append("status")
    return "updated:" + ",".join(changed) if changed else "unchanged"


async def _upsert_elder(session: AsyncSession) -> str:
    row = await session.get(Elder, ELDER_ID)
    if row is None:
        session.add(
            Elder(
                elder_id=ELDER_ID,
                name="张桂芬",
                gender="female",
                persona_id=PERSONA_ID,
                status="active",
            )
        )
        return "created"
    changed: list[str] = []
    if row.name != "张桂芬":
        row.name = "张桂芬"
        changed.append("name")
    if row.persona_id != PERSONA_ID:
        row.persona_id = PERSONA_ID
        changed.append("persona_id")
    if row.status != "active":
        row.status = "active"
        changed.append("status")
    return "updated:" + ",".join(changed) if changed else "unchanged"


async def _upsert_bed(session: AsyncSession) -> str:
    row = await session.get(Bed, BED_ID)
    if row is None:
        session.add(
            Bed(
                bed_id=BED_ID,
                room_id=ROOM_ID,
                bed_no="1 号床",
                status="occupied",
                elder_id=ELDER_ID,
            )
        )
        return "created"
    changed: list[str] = []
    if row.elder_id != ELDER_ID:
        row.elder_id = ELDER_ID
        changed.append("elder_id")
    if row.status != "occupied":
        row.status = "occupied"
        changed.append("status")
    return "updated:" + ",".join(changed) if changed else "unchanged"


async def _upsert_device(session: AsyncSession) -> str:
    row = await session.get(Device, DEVICE_ID)
    token_hash = _sha256_hex(DEVICE_SECRET)
    if row is None:
        session.add(
            Device(
                device_id=DEVICE_ID,
                room_id=ROOM_ID,
                elder_id=ELDER_ID,
                token_hash=token_hash,
                status="online",
            )
        )
        return "created"
    changed: list[str] = []
    # token_hash 由固定明文派生，所以「重算同一个值」是幂等的；不是轮换。
    if row.token_hash != token_hash:
        row.token_hash = token_hash
        changed.append("token_hash")
    if row.elder_id != ELDER_ID:
        row.elder_id = ELDER_ID
        changed.append("elder_id")
    if row.room_id != ROOM_ID:
        row.room_id = ROOM_ID
        changed.append("room_id")
    return "updated:" + ",".join(changed) if changed else "unchanged"


async def _upsert_assignment(
    session: AsyncSession, row_id: uuid.UUID, *, staff_id: uuid.UUID, relation: str
) -> str:
    row = await session.get(ElderStaffAssignment, row_id)
    if row is None:
        row = await _adopt(
            session,
            ElderStaffAssignment,
            ElderStaffAssignment.elder_id == ELDER_ID,
            ElderStaffAssignment.staff_id == staff_id,
        )
    if row is None:
        session.add(
            ElderStaffAssignment(id=row_id, elder_id=ELDER_ID, staff_id=staff_id, relation=relation)
        )
        return "created"
    if row.relation != relation or row.staff_id != staff_id:
        row.relation = relation
        row.staff_id = staff_id
        return "updated:relation"
    return "unchanged"


async def _upsert_guardian_link(session: AsyncSession, guardian_id: uuid.UUID) -> str:
    row = await session.get(ElderGuardian, GUARDIAN_LINK_ID)
    if row is None:
        row = await _adopt(
            session,
            ElderGuardian,
            ElderGuardian.elder_id == ELDER_ID,
            ElderGuardian.guardian_id == guardian_id,
        )
    if row is None:
        session.add(
            ElderGuardian(
                id=GUARDIAN_LINK_ID,
                elder_id=ELDER_ID,
                guardian_id=guardian_id,
                relation="daughter",
                status="active",
            )
        )
        return "created"
    changed: list[str] = []
    if row.status != "active":
        row.status = "active"
        changed.append("status")
    if row.relation != "daughter":
        row.relation = "daughter"
        changed.append("relation")
    return "updated:" + ",".join(changed) if changed else "unchanged"


async def _upsert_plan(
    session: AsyncSession,
    plan_id: uuid.UUID,
    *,
    drug_name: str,
    dose: str,
    schedule_cron: str,
    start_date: datetime.date,
    created_by: uuid.UUID,
) -> str:
    row = await session.get(MedicationPlan, plan_id)
    if row is None:
        row = await _adopt(
            session,
            MedicationPlan,
            MedicationPlan.elder_id == ELDER_ID,
            MedicationPlan.drug_name == drug_name,
        )
    if row is None:
        session.add(
            MedicationPlan(
                plan_id=plan_id,
                elder_id=ELDER_ID,
                drug_name=drug_name,
                dose=dose,
                schedule_cron=schedule_cron,
                start_date=start_date,
                version=1,
                created_by=created_by,
                status="active",
            )
        )
        return "created"
    changed: list[str] = []
    for field, value in (
        ("drug_name", drug_name),
        ("dose", dose),
        ("schedule_cron", schedule_cron),
        ("start_date", start_date),
    ):
        if getattr(row, field) != value:
            setattr(row, field, value)
            changed.append(field)
    if changed:
        row.version = row.version + 1  # 药单内容变了才加版本（与 service 同口径）
        changed.append("version")
    return "updated:" + ",".join(changed) if changed else "unchanged"


async def _seed_health_records(session: AsyncSession, days: int = 13) -> int:
    """13 天健康记录，故意缺第 4/9 天（家属端趋势图要能演示「断线」）。"""
    today = _today()
    existing_days = set(
        (
            await session.scalars(
                select(HealthRecord.record_date).where(HealthRecord.elder_id == ELDER_ID)
            )
        ).all()
    )
    created = 0
    for offset in range(days):
        if offset in (4, 9):
            continue
        day = today - datetime.timedelta(days=offset)
        if day in existing_days:
            # 唯一键是 (elder_id, record_date, revision)，同一天已有记录就不再插，
            # 否则撞 uq_health_version（历史遗留记录也要认）。
            continue
        record_id = uuid.uuid5(uuid.NAMESPACE_URL, f"anyu-demo:health:{ELDER_ID}:{day}")
        if await session.get(HealthRecord, record_id) is not None:
            continue
        systolic = 118 + (offset * 3) % 22
        diastolic = 74 + (offset * 2) % 12
        grade = "green"
        abnormal: list[str] | None = None
        if systolic >= 140 or diastolic >= 90:
            grade = "yellow"
            abnormal = ["blood_pressure_above_normal"]
        session.add(
            HealthRecord(
                record_id=record_id,
                elder_id=ELDER_ID,
                record_date=day,
                source="voice_scheduled",
                systolic_mmhg=systolic,
                diastolic_mmhg=diastolic,
                heart_rate_bpm=68 + offset % 13,
                sleep_hours=round(6.5 + (offset % 5) * 0.4, 1),
                mood_score=3 + offset % 3,
                grade=grade,
                abnormal_reasons=abnormal,
                revision=1,
                collected_at=_at(day, 19, 5),
            )
        )
        created += 1
    return created


async def _seed_notifications(session: AsyncSession) -> int:
    today = _today()
    plan: tuple[tuple[uuid.UUID, str, str, dict[str, Any]], ...] = (
        (
            NOTIF_HEALTH_ID,
            "health_review",
            "yellow",
            {"title": "血压偏高需关注", "body": "昨日收缩压 145，已按规则提示问医生，请复核。"},
        ),
        (
            NOTIF_FAMILY_ID,
            "family_report",
            "green",
            {"title": "今日日报已生成", "body": "张桂芬今日日报已生成，可在日报页查看。"},
        ),
    )
    created = 0
    for notification_id, kind, severity, payload in plan:
        if await session.get(Notification, notification_id) is not None:
            continue
        adopted = await _adopt(
            session,
            Notification,
            Notification.elder_id == ELDER_ID,
            Notification.kind == kind,
        )
        if adopted is not None:
            # 同一种通知只保留一条：历史遗留行直接更新，免得通知中心出现两条同标题。
            adopted.severity = severity
            adopted.payload = payload
            continue
        session.add(
            Notification(
                notification_id=notification_id,
                elder_id=ELDER_ID,
                kind=kind,
                severity=severity,
                status="generated",
                payload=payload,
                created_at=_at(today, 8, 0),
            )
        )
        created += 1
    return created


async def _seed_reports(session: AsyncSession, days: int = 4) -> int:
    """`report_service.generate` 本身按 (elder_id, report_date) 幂等，重复调用返回已有行。"""
    today = _today()
    targets = [today - datetime.timedelta(days=offset) for offset in range(days)]
    before = await session.scalar(
        select(func.count())
        .select_from(DailyReport)
        .where(DailyReport.elder_id == ELDER_ID, DailyReport.report_date.in_(targets))
    )
    for day in targets:
        await report_service.generate(session, ELDER_ID, day)
    return days - int(before or 0)


async def _run(force_device: bool) -> int:
    _ensure_local_database()
    summary: list[tuple[str, str]] = []
    async with session_factory() as session:
        summary.append(("persona  小满", await _upsert_persona(session)))
        summary.append(("room     A-301", await _upsert_room(session)))
        summary.append(("elder    张桂芬", await _upsert_elder(session)))
        summary.append(("bed      1 号床", await _upsert_bed(session)))
        summary.append(("device   平板", await _upsert_device(session)))
        effective: dict[str, uuid.UUID] = {}
        for staff_id, name, role, phone, password in STAFF_ACCOUNTS:
            note, effective[role] = await _upsert_staff(
                session, staff_id, name, role, phone, password
            )
            summary.append((f"staff    {role}", note))
        guardian_id, guardian_name, guardian_phone, guardian_password = GUARDIAN
        guardian_note, guardian_effective = await _upsert_guardian(
            session, guardian_id, guardian_name, guardian_phone, guardian_password
        )
        summary.append(("guardian 张女士", guardian_note))
        summary.append(
            (
                "assignment 护工",
                await _upsert_assignment(
                    session,
                    CARE_ASSIGNMENT_ID,
                    staff_id=effective["care_worker"],
                    relation="primary_care",
                ),
            )
        )
        summary.append(
            (
                "assignment 医生",
                await _upsert_assignment(
                    session, DOCTOR_ASSIGNMENT_ID, staff_id=effective["doctor"], relation="doctor"
                ),
            )
        )
        summary.append(("binding  女儿", await _upsert_guardian_link(session, guardian_effective)))
        summary.append(
            (
                "plan     氨氯地平",
                await _upsert_plan(
                    session,
                    PLAN_AMLODIPINE_ID,
                    drug_name="氨氯地平",
                    dose="5mg",
                    schedule_cron="08:00",
                    start_date=today_or_default(),
                    created_by=effective["doctor"],
                ),
            )
        )
        summary.append(
            (
                "plan     二甲双胍",
                await _upsert_plan(
                    session,
                    PLAN_METFORMIN_ID,
                    drug_name="二甲双胍",
                    dose="0.5g",
                    schedule_cron="12:30",
                    start_date=today_or_default(),
                    created_by=effective["doctor"],
                ),
            )
        )
        summary.append(("health   13 天", f"created:{await _seed_health_records(session)}"))
        summary.append(("notif    2 条", f"created:{await _seed_notifications(session)}"))
        summary.append(("report   4 天", f"created:{await _seed_reports(session)}"))
        await session.commit()

    print("\n==== 演示数据 ====")
    for label, result in summary:
        print(f"  {label:<24} {result}")
    print("\n==== 四端登录 ====")
    print("  长者端 /        设备号 11111111-1111-4111-8111-111111111106")
    print(f"                  设备密钥 {DEVICE_SECRET}")
    for _, name, role, phone, password in STAFF_ACCOUNTS:
        print(f"  /console        {role:<11} {phone} / {password}    ({name})")
    print(f"  /family         guardian    {GUARDIAN[2]} / {GUARDIAN[3]}    ({GUARDIAN[1]})")
    print("\n重复执行不会换账号：口令没变就不重写哈希，token_version 不动。")
    # 与 seed_media.py 同口径：脚本结束后释放连接池。Windows 上 asyncpg 连接绑在
    # 即将关闭的事件循环上，不 dispose 会在收尾时冒 "Event loop is closed" 噪声；
    # seed_acceptance.py 还会在本脚本之后用同一 engine 再起一轮事件循环。
    await engine.dispose()
    return 0


def today_or_default() -> datetime.date:
    """药单开始日期取「今天」，重跑同一天内不产生 update。"""
    return _today()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="种入四端演示数据（固定 UUID，幂等）")
    parser.add_argument(
        "--force-device",
        action="store_true",
        help="保留开关：当前 token_hash 永远由固定明文派生，无需轮换",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_run(args.force_device))


if __name__ == "__main__":
    sys.exit(main())
