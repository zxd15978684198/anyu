"""批量导入药单 + 多药合并卡片（FR-MED-07，PRD:337）。

三组验收（对应 task-4 的验收口径）：

1. **成功用例**：CSV 与 XLSX 两条读文件路径都能建出药单，落库字段与 `create_plan` 一致
   （`version=1`、`status='active'`、`created_by=导入人`）；API 侧 `dry_run` 只预检不写，
   去掉 `dry_run` 才真写。
2. **失败行回显**：任一行不合法 → `accepted=False` + 每处的「行号 / 字段 / 原因」，
   且**一行都不写**（调 `apply_import` 直接 `MedicationImportRejected`，库里 0 条）。
3. **幂等去重**：文件内重复行与库内重复都只留一条计划，第二条走 `skips`，不报错。

隔离方式与 `tests/test_medication_plan_edit.py` 同款：真库 + `NullPool` 单连接 + 外层
事务，测试结束整体回滚；`python-multipart` 已在依赖里，multipart 上传不需要新依赖。
"""

from __future__ import annotations

import csv
import datetime
import io
import logging
import uuid
import zipfile
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from tests.conftest import require_db_connection

from app.api import medication as medication_api
from app.core.auth import get_current_principal
from app.core.config import get_settings
from app.core.runtime_context import Identity
from app.core.trace import TraceIdMiddleware
from app.db import get_session
from app.models import (
    AuditLog,
    Elder,
    ElderStaffAssignment,
    MedicationPlan,
    StaffAccount,
)
from app.services import audit_service, medication_service, report_service
from app.services.medication_service import (
    IMPORT_MAX_ROWS,
    IMPORT_SKIP_DUPLICATE_EXISTING,
    IMPORT_TEMPLATE_HEADERS,
    IMPORT_TEMPLATE_SAMPLE_ROWS,
    MedicationImportRejected,
    apply_import,
    build_import_template_csv,
    merge_active_plans,
    preview_import,
)

SHANGHAI = datetime.timezone(datetime.timedelta(hours=8))


# ------------------------------------------------------------------ 夹具


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    connection = await require_db_connection(db_engine)
    transaction = await connection.begin()
    session = AsyncSession(bind=connection, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
        await transaction.rollback()
        await connection.close()


@pytest.fixture(autouse=True)
def isolated_session_factory(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> async_sessionmaker[AsyncSession]:
    """审计写入也接到测试连接上，403 的审计行才不会落到真库、回滚不掉。"""
    factory = async_sessionmaker(
        bind=db.bind,  # type: ignore[arg-type]
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    monkeypatch.setattr(report_service, "session_factory", factory)
    monkeypatch.setattr(audit_service, "session_factory", factory)
    return factory


@dataclass
class Seed:
    doctor_id: uuid.UUID
    care_worker_id: uuid.UUID
    elder_id: uuid.UUID
    other_elder_id: uuid.UUID
    #: 姓名带随机后缀：开发库里已有同名演示长者，名字固定会让「同名多人」误判真实触发。
    elder_name: str = "张桂芬"
    other_elder_name: str = "李振华"


async def _seed(db: AsyncSession) -> Seed:
    doctor_id, care_worker_id = uuid.uuid4(), uuid.uuid4()
    elder_id, other_elder_id = uuid.uuid4(), uuid.uuid4()
    suffix = uuid.uuid4().hex[:6]
    elder_name, other_elder_name = f"张桂芬{suffix}", f"李振华{suffix}"
    await db.execute(
        StaffAccount.__table__.insert(),
        [
            {"staff_id": doctor_id, "name": "王医生", "role": "doctor", "password_hash": "x"},
            {
                "staff_id": care_worker_id,
                "name": "李护工",
                "role": "care_worker",
                "password_hash": "x",
            },
        ],
    )
    await db.execute(
        Elder.__table__.insert(),
        [
            {"elder_id": elder_id, "name": elder_name, "status": "active"},
            {"elder_id": other_elder_id, "name": other_elder_name, "status": "active"},
        ],
    )
    await db.execute(
        ElderStaffAssignment.__table__.insert(),
        [
            {"elder_id": elder_id, "staff_id": doctor_id, "relation": "doctor"},
            {"elder_id": elder_id, "staff_id": care_worker_id, "relation": "primary_care"},
        ],
    )
    await db.flush()
    return Seed(
        doctor_id=doctor_id,
        care_worker_id=care_worker_id,
        elder_id=elder_id,
        other_elder_id=other_elder_id,
        elder_name=elder_name,
        other_elder_name=other_elder_name,
    )


@pytest.fixture
async def seeded(db: AsyncSession) -> Seed:
    return await _seed(db)


@dataclass
class Bundle:
    client: AsyncClient
    holder: list[Identity]


@pytest.fixture
async def client(db: AsyncSession) -> AsyncIterator[Bundle]:
    application = FastAPI()
    application.add_middleware(TraceIdMiddleware)
    application.include_router(medication_api.router)

    async def override_session() -> AsyncIterator[AsyncSession]:
        session = AsyncSession(
            bind=db.bind,  # type: ignore[arg-type]
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    holder: list[Identity] = []

    def override_principal() -> Identity:
        return holder[0]

    application.dependency_overrides[get_session] = override_session
    application.dependency_overrides[get_current_principal] = override_principal

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver"
    ) as ac:
        yield Bundle(client=ac, holder=holder)


def _as_doctor(seed: Seed) -> Identity:
    return Identity(kind="staff", staff_id=str(seed.doctor_id), role="doctor")


def _as_care_worker(seed: Seed) -> Identity:
    return Identity(kind="staff", staff_id=str(seed.care_worker_id), role="care_worker")


# ------------------------------------------------------------------ payload 构造


def _csv_bytes(rows: Sequence[Sequence[str]], *, headers: Sequence[str] | None = None) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(list(headers or IMPORT_TEMPLATE_HEADERS))
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def _xlsx_bytes(rows: Sequence[Sequence[str]]) -> bytes:
    """手搓一个最小 XLSX：只放 inlineStr + 一张 sheet，够验证「不引 openpyxl」这条路。"""

    def letter(index: int) -> str:
        letters = ""
        index += 1
        while index:
            index, remainder = divmod(index - 1, 26)
            letters = chr(65 + remainder) + letters
        return letters

    def row_xml(line_no: int, values: Sequence[str]) -> str:
        cells = "".join(
            f'<c r="{letter(index)}{line_no}" t="inlineStr"><is><t>{value}</t></is></c>'
            for index, value in enumerate(values)
            if value
        )
        return f'<row r="{line_no}">{cells}</row>'

    body = "".join(
        row_xml(index, values)
        for index, values in enumerate([list(IMPORT_TEMPLATE_HEADERS), *map(list, rows)], start=1)
    )
    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{body}</sheetData></worksheet>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return buffer.getvalue()


async def _plan_rows(db: AsyncSession, elder_id: uuid.UUID) -> list[MedicationPlan]:
    rows = await db.scalars(
        select(MedicationPlan)
        .where(MedicationPlan.elder_id == elder_id)
        .order_by(MedicationPlan.schedule_cron.asc())
    )
    return list(rows)


async def _plan_count(db: AsyncSession, elder_id: uuid.UUID) -> int:
    total = await db.scalar(
        select(func.count()).select_from(MedicationPlan).where(MedicationPlan.elder_id == elder_id)
    )
    return int(total or 0)


async def _make_plan(
    db: AsyncSession,
    seed: Seed,
    *,
    drug_name: str = "二甲双胍",
    dose: str = "0.5g 1 片",
    schedule_cron: str = "08:00",
    start_date: datetime.date | None = None,
    end_date: datetime.date | None = None,
    status: str = "active",
) -> MedicationPlan:
    plan = MedicationPlan(
        plan_id=uuid.uuid4(),
        elder_id=seed.elder_id,
        drug_name=drug_name,
        dose=dose,
        schedule_cron=schedule_cron,
        start_date=start_date or datetime.date(2026, 11, 1),
        end_date=end_date,
        created_by=seed.doctor_id,
        version=1,
        status=status,
    )
    db.add(plan)
    await db.flush()
    return plan


# ------------------------------------------------------------------ 一、成功用例


async def test_csv_import_creates_plans_with_expected_fields(
    db: AsyncSession, seeded: Seed
) -> None:
    """CSV 两行 → 两条药单；字段与 `create_plan` 同口径（v1 / active / 导入人）。"""
    payload = _csv_bytes(
        [
            (seeded.elder_name, "二甲双胍", "0.5g 1 片", "08:00", "2026-11-10", ""),
            (seeded.elder_name, "阿托伐他汀", "20mg 1 片", "20:00", "2026-11-10", "2026-12-10"),
        ]
    )

    preview = await preview_import(db, payload=payload)
    assert preview.accepted is True
    assert preview.total_rows == 2
    assert preview.created_count == 2
    assert preview.elder_ids == (seeded.elder_id,)

    result = await apply_import(db, preview=preview, created_by=seeded.doctor_id)
    assert result.accepted is True
    assert len(result.created) == 2
    # 行号在结果里保留：失败/成功都以「第几行」为锚回显
    assert [item.line for item in result.created] == [2, 3]
    assert [item.elder_name for item in result.created] == [seeded.elder_name, seeded.elder_name]

    plans = await _plan_rows(db, seeded.elder_id)
    assert len(plans) == 2
    first = next(plan for plan in plans if plan.drug_name == "二甲双胍")
    assert first.dose == "0.5g 1 片"
    assert first.schedule_cron == "08:00"
    assert first.start_date == datetime.date(2026, 11, 10)
    assert first.end_date is None
    assert first.version == 1
    assert first.status == "active"
    assert first.created_by == seeded.doctor_id

    second = next(plan for plan in plans if plan.drug_name == "阿托伐他汀")
    assert second.end_date == datetime.date(2026, 12, 10)


async def test_xlsx_import_parses_without_extra_dependency(db: AsyncSession, seeded: Seed) -> None:
    """XLSX 走标准库 zipfile+ElementTree：不引 pandas/openpyxl 也能读。"""
    payload = _xlsx_bytes([(seeded.elder_name, "氨氯地平", "5mg 1 片", "09:00", "2026-11-10", "")])
    assert payload.startswith(b"PK\x03\x04")

    preview = await preview_import(db, payload=payload)
    assert preview.accepted is True, preview.issues
    assert preview.created_count == 1

    result = await apply_import(db, preview=preview, created_by=seeded.doctor_id)
    plans = await _plan_rows(db, seeded.elder_id)
    assert [plan.drug_name for plan in plans] == ["氨氯地平"]
    assert result.created[0].line == 2


async def test_import_defaults_elder_from_page_selection(db: AsyncSession, seeded: Seed) -> None:
    """行内不写长者时，用页面当前所选长者（`elder_id` 形参）归属。"""
    payload = _csv_bytes([("", "二甲双胍", "0.5g 1 片", "08:00", "2026-11-10", "")])
    preview = await preview_import(db, payload=payload, elder_id=seeded.elder_id)
    assert preview.accepted is True, preview.issues
    assert preview.rows[0].elder_id == seeded.elder_id
    assert preview.rows[0].elder_name == seeded.elder_name


# ------------------------------------------------------------------ 二、失败行回显


async def test_bad_rows_are_reported_with_line_and_field(db: AsyncSession, seeded: Seed) -> None:
    """第 2 行空药名、第 3 行非法时刻 → 两处问题，行号+字段都能定位。"""
    payload = _csv_bytes(
        [
            (seeded.elder_name, "", "0.5g 1 片", "08:00", "2026-11-10", ""),
            (seeded.elder_name, "二甲双胍", "0.5g 1 片", "25:00", "2026-11-10", ""),
        ]
    )

    preview = await preview_import(db, payload=payload)
    assert preview.accepted is False
    assert preview.created_count == 0
    assert preview.total_rows == 2

    issues = {(issue.line, issue.field) for issue in preview.issues}
    assert (2, "drug_name") in issues
    assert (3, "schedule_cron") in issues
    drug_issue = next(issue for issue in preview.issues if issue.field == "drug_name")
    assert "不能为空" in drug_issue.message


async def test_all_or_nothing_blocks_whole_batch(db: AsyncSession, seeded: Seed) -> None:
    """一行不合法 → `apply_import` 整批拒绝，库里一条都不多（不做半截导入）。"""
    await _make_plan(db, seeded, drug_name="已有药", dose="1 片", schedule_cron="07:00")
    before = await _plan_count(db, seeded.elder_id)

    payload = _csv_bytes(
        [
            (seeded.elder_name, "二甲双胍", "0.5g 1 片", "08:00", "2026-11-10", ""),
            (seeded.elder_name, "阿托伐他汀", "20mg 1 片", "8:00", "2026-11-10", ""),
        ]
    )
    preview = await preview_import(db, payload=payload)
    assert preview.accepted is False
    # 合法那一行仍然会被标成「将创建」，但整体不通过 → 不写
    assert preview.created_count == 1

    with pytest.raises(MedicationImportRejected) as excinfo:
        await apply_import(db, preview=preview, created_by=seeded.doctor_id)
    assert "整批未写入" in str(excinfo.value)
    assert excinfo.value.issues == preview.issues

    await db.flush()
    assert await _plan_count(db, seeded.elder_id) == before


async def test_write_failure_rolls_back_whole_batch(
    db: AsyncSession, seeded: Seed, monkeypatch: pytest.MonkeyPatch
) -> None:
    """预检全过、写第 2 条时炸了 → 第 1 条也不能留在库里（不做半截导入）。

    api 层会把服务层异常吞成响应对象（`_service_error`），依赖看到的是正常 return、
    外层事务照样 commit——所以这条不能只靠调用方回滚，`apply_import` 自己套 SAVEPOINT。
    反向验证：把那段 `session.begin_nested()` 摘掉，本用例立刻变红（第 1 条已在库里）。
    """
    payload = _csv_bytes(
        [
            (seeded.elder_name, "二甲双胍", "0.5g 1 片", "08:00", "2026-11-10", ""),
            (seeded.elder_name, "阿托伐他汀", "20mg 1 片", "20:00", "2026-11-10", ""),
        ]
    )
    preview = await preview_import(db, payload=payload)
    assert preview.accepted is True, preview.issues

    real_create_plan = medication_service.create_plan
    calls = {"count": 0}

    async def flaky_create_plan(*args: Any, **kwargs: Any) -> MedicationPlan:
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("写到第二行时炸了")
        return await real_create_plan(*args, **kwargs)

    monkeypatch.setattr(medication_service, "create_plan", flaky_create_plan)

    with pytest.raises(RuntimeError, match="写到第二行时炸了"):
        await apply_import(db, preview=preview, created_by=seeded.doctor_id)

    assert calls["count"] == 2
    assert await _plan_count(db, seeded.elder_id) == 0


async def test_unknown_elder_name_is_row_issue(db: AsyncSession, seeded: Seed) -> None:
    """长者不存在 → 行级问题（不静默丢行），字段是 elder_name。"""
    payload = _csv_bytes([("查无此人", "二甲双胍", "0.5g 1 片", "08:00", "2026-11-10", "")])
    preview = await preview_import(db, payload=payload)
    assert preview.accepted is False
    assert [(issue.line, issue.field) for issue in preview.issues] == [(2, "elder_name")]
    assert "不存在" in preview.issues[0].message


async def test_overflow_rows_are_rejected_at_file_level(db: AsyncSession, seeded: Seed) -> None:
    """超过单次上限 → `line=0` 的文件级问题，而不是逐行报 200 条。"""
    rows = [
        (seeded.elder_name, f"药{i}", "1 片", "08:00", "2026-11-10", "")
        for i in range(IMPORT_MAX_ROWS + 1)
    ]
    preview = await preview_import(db, payload=_csv_bytes(rows))
    assert preview.accepted is False
    assert preview.issues[0].line == 0
    assert f"上限 {IMPORT_MAX_ROWS} 行" in preview.issues[0].message


# ------------------------------------------------------------------ 三、幂等去重


async def test_duplicate_rows_inside_file_are_skipped(db: AsyncSession, seeded: Seed) -> None:
    """同文件重复两行 → 只建一条，第二行进 `skips`（`duplicate_in_file`）。"""
    same = (seeded.elder_name, "二甲双胍", "0.5g 1 片", "08:00", "2026-11-10", "")
    preview = await preview_import(db, payload=_csv_bytes([same, same]))

    assert preview.accepted is True
    assert preview.created_count == 1
    assert len(preview.skips) == 1
    skip = preview.skips[0]
    assert skip.line == 3
    assert skip.reason == medication_service.IMPORT_SKIP_DUPLICATE_IN_FILE
    assert "与第 2 行内容完全相同" in skip.message

    await apply_import(db, preview=preview, created_by=seeded.doctor_id)
    assert await _plan_count(db, seeded.elder_id) == 1


async def test_second_import_of_same_file_creates_nothing(db: AsyncSession, seeded: Seed) -> None:
    """再导一遍同一份文件 → 全部跳过，不产生第二条计划（幂等）。"""
    payload = _csv_bytes([(seeded.elder_name, "二甲双胍", "0.5g 1 片", "08:00", "2026-11-10", "")])

    first = await preview_import(db, payload=payload)
    await apply_import(db, preview=first, created_by=seeded.doctor_id)
    assert await _plan_count(db, seeded.elder_id) == 1

    second = await preview_import(db, payload=payload)
    assert second.accepted is True
    assert second.created_count == 0
    assert len(second.skips) == 1
    assert second.skips[0].reason == medication_service.IMPORT_SKIP_DUPLICATE_EXISTING
    assert "库中已有同内容药单" in second.skips[0].message
    assert second.skips[0].plan_id is not None

    result = await apply_import(db, preview=second, created_by=seeded.doctor_id)
    assert result.created == ()
    assert await _plan_count(db, seeded.elder_id) == 1


async def test_dedupe_counts_stopped_plans_as_occupied(db: AsyncSession, seeded: Seed) -> None:
    """已停用的同内容药单也算占用：宁可让医生手改，也不凭空再开一条一样的。"""
    await _make_plan(
        db,
        seeded,
        drug_name="二甲双胍",
        dose="0.5g 1 片",
        start_date=datetime.date(2026, 11, 10),
        status="stopped",
    )
    payload = _csv_bytes([(seeded.elder_name, "二甲双胍", "0.5g 1 片", "08:00", "2026-11-10", "")])
    preview = await preview_import(db, payload=payload)
    assert preview.created_count == 0
    assert "已停用" in preview.skips[0].message


async def test_skips_plan_created_between_preview_and_apply(db: AsyncSession, seeded: Seed) -> None:
    """预检通过后、写库之前又有人手工加了同内容药单 → 跳过并回显，不产生第二条。

    计划表没有唯一约束，重复只能靠写入前再查一次库。反向验证：把 `apply_import` 里
    `existing.get(key)` 那段跳过逻辑去掉，本用例会写出第二条、计数断言变红。
    """
    payload = _csv_bytes([(seeded.elder_name, "二甲双胍", "0.5g 1 片", "08:00", "2026-11-10", "")])
    preview = await preview_import(db, payload=payload)
    assert preview.accepted is True, preview.issues
    assert preview.skips == ()

    # 构造时序：预检与写入之间，有人手工插了一条内容完全相同的药单
    existing = await _make_plan(
        db,
        seeded,
        drug_name="二甲双胍",
        dose="0.5g 1 片",
        schedule_cron="08:00",
        start_date=datetime.date(2026, 11, 10),
    )

    result = await apply_import(db, preview=preview, created_by=seeded.doctor_id)
    assert result.accepted is True
    assert result.created == ()
    assert len(result.skips) == 1
    skip = result.skips[0]
    assert skip.reason == IMPORT_SKIP_DUPLICATE_EXISTING
    assert skip.plan_id == existing.plan_id
    assert skip.line == 2
    assert "预检之后库中出现了同内容药单" in skip.message
    assert await _plan_count(db, seeded.elder_id) == 1


# ------------------------------------------------------------------ 四、模板下载


def test_template_csv_has_bom_and_sample_rows() -> None:
    """模板：UTF-8 带 BOM（Excel 不乱码）+ 表头 + 两行示例。"""
    payload = build_import_template_csv()
    assert payload.startswith(b"\xef\xbb\xbf")
    text = payload.decode("utf-8-sig")

    lines = text.strip().splitlines()
    assert lines[0].split(",") == list(IMPORT_TEMPLATE_HEADERS)
    assert len(lines) == 1 + len(IMPORT_TEMPLATE_SAMPLE_ROWS)
    assert "二甲双胍" in text


# ------------------------------------------------------------------ 五、API 层


async def test_import_api_dry_run_then_write(
    client: Bundle, seeded: Seed, db: AsyncSession
) -> None:
    """`dry_run=true` 只回方案不落库；去掉后同一份文件真写入。"""
    payload = _csv_bytes([(seeded.elder_name, "二甲双胍", "0.5g 1 片", "08:00", "2026-11-10", "")])
    client.holder[:] = [_as_doctor(seeded)]

    dry = await client.client.post(
        "/api/medication/plans/import",
        params={"elder_id": str(seeded.elder_id), "dry_run": "true"},
        files={"file": ("meds.csv", payload, "text/csv")},
    )
    assert dry.status_code == 200, dry.text
    body: Any = dry.json()
    assert body["accepted"] is True
    assert body["dry_run"] is True
    assert body["all_or_nothing"] is True
    assert body["total_rows"] == 1
    assert body["will_create"] == 1
    assert body["created"][0]["plan_id"] is None
    assert body["created"][0]["line"] == 2
    assert body["errors"] == []
    assert await _plan_count(db, seeded.elder_id) == 0

    written = await client.client.post(
        "/api/medication/plans/import",
        params={"elder_id": str(seeded.elder_id), "dry_run": "false"},
        files={"file": ("meds.csv", payload, "text/csv")},
    )
    assert written.status_code == 200, written.text
    out: Any = written.json()
    assert out["accepted"] is True
    assert out["dry_run"] is False
    assert out["created"][0]["plan_id"] is not None
    assert out["created"][0]["elder_name"] == seeded.elder_name

    await db.flush()
    assert await _plan_count(db, seeded.elder_id) == 1


async def test_import_api_returns_failed_rows_and_writes_nothing(
    client: Bundle, seeded: Seed, db: AsyncSession
) -> None:
    """失败行以 200 + 结构化 `errors` 回显（契约错误体装不下行级信息），库里 0 条。"""
    payload = _csv_bytes(
        [
            (seeded.elder_name, "二甲双胍", "0.5g 1 片", "08:00", "2026-11-10", ""),
            (seeded.elder_name, "", "20mg 1 片", "20:00", "2026-11-10", ""),
        ]
    )
    client.holder[:] = [_as_doctor(seeded)]
    response = await client.client.post(
        "/api/medication/plans/import",
        params={"elder_id": str(seeded.elder_id)},
        files={"file": ("meds.csv", payload, "text/csv")},
    )
    assert response.status_code == 200, response.text
    body: Any = response.json()
    assert body["accepted"] is False
    assert body["will_create"] == 1
    # 合法那行仍列在 `created` 里（前端要显示「哪些行是好的」），但 `plan_id` 为空
    # 表示没有写库——全量不通过即整体不写。
    assert len(body["created"]) == 1
    assert body["created"][0]["plan_id"] is None
    first_error = body["errors"][0]
    assert (first_error["line"], first_error["field"]) == (3, "drug_name")
    assert "不能为空" in first_error["message"]

    await db.flush()
    assert await _plan_count(db, seeded.elder_id) == 0


async def test_import_api_rejects_non_doctor_and_writes_audit(
    client: Bundle, seeded: Seed, db: AsyncSession
) -> None:
    """护工不能导入（WRITING_ROLES={doctor}）：403 + `medication.plan.import` 拒绝审计。"""
    payload = _csv_bytes([(seeded.elder_name, "二甲双胍", "0.5g 1 片", "08:00", "2026-11-10", "")])
    client.holder[:] = [_as_care_worker(seeded)]
    response = await client.client.post(
        "/api/medication/plans/import",
        params={"elder_id": str(seeded.elder_id)},
        files={"file": ("meds.csv", payload, "text/csv")},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "ROLE_FORBIDDEN"

    await db.flush()
    audit = await db.scalars(select(AuditLog).where(AuditLog.action == "medication.plan.import"))
    assert list(audit), "拒绝写入必须留审计"
    assert await _plan_count(db, seeded.elder_id) == 0


async def test_import_template_endpoint_returns_csv_attachment(
    client: Bundle, seeded: Seed
) -> None:
    """模板端点：CSV 附件 + 不缓存 + BOM。"""
    client.holder[:] = [_as_doctor(seeded)]
    response = await client.client.get("/api/medication/plans/import/template")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert "medication-import-template.csv" in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "no-store"
    assert response.content.startswith(b"\xef\xbb\xbf")
    assert "二甲双胍" in response.content.decode("utf-8-sig")


async def test_import_api_scope_blocks_other_elder(
    client: Bundle, seeded: Seed, db: AsyncSession
) -> None:
    """文件里点名的长者不在医生负责范围 → 403，且一行都不写。"""
    payload = _csv_bytes(
        [(seeded.other_elder_name, "二甲双胍", "0.5g 1 片", "08:00", "2026-11-10", "")]
    )
    client.holder[:] = [_as_doctor(seeded)]
    response = await client.client.post(
        "/api/medication/plans/import",
        files={"file": ("meds.csv", payload, "text/csv")},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "SCOPE_FORBIDDEN"
    await db.flush()
    assert await _plan_count(db, seeded.other_elder_id) == 0


# ------------------------------------------------------------------ 六、多药合并卡片


async def test_merge_active_plans_groups_by_schedule() -> None:
    """同一时刻的多味药合成一张卡；停用与不在区间的药单不参与。"""
    elder_id = uuid.uuid4()
    day = datetime.date(2026, 11, 10)

    def plan(drug: str, dose: str, cron: str, **kwargs: Any) -> MedicationPlan:
        return MedicationPlan(
            plan_id=uuid.uuid4(),
            elder_id=elder_id,
            drug_name=drug,
            dose=dose,
            schedule_cron=cron,
            start_date=kwargs.get("start_date", datetime.date(2026, 11, 1)),
            end_date=kwargs.get("end_date"),
            created_by=uuid.uuid4(),
            version=1,
            status=kwargs.get("status", "active"),
        )

    slots = merge_active_plans(
        [
            plan("二甲双胍", "0.5g 1 片", "08:00"),
            plan("阿司匹林", "100mg 1 片", "08:00"),
            plan("阿托伐他汀", "20mg 1 片", "20:00"),
            plan("已停用药", "1 片", "08:00", status="stopped"),
            plan("未来药", "1 片", "08:00", start_date=datetime.date(2026, 12, 1)),
            plan("过期药", "1 片", "08:00", end_date=datetime.date(2026, 11, 1)),
        ],
        on_date=day,
    )

    assert [slot.schedule_cron for slot in slots] == ["08:00", "20:00"]
    morning = slots[0]
    assert morning.drug_count == 2
    assert [drug.label for drug in morning.drugs] == ["二甲双胍 0.5g 1 片", "阿司匹林 100mg 1 片"]
    assert morning.summary == "08:00 共 2 种药：二甲双胍 0.5g 1 片、阿司匹林 100mg 1 片"
    assert slots[1].drug_count == 1
    assert all(slot.on_date == day for slot in slots)


async def test_merged_endpoint_returns_slots(
    client: Bundle, seeded: Seed, db: AsyncSession
) -> None:
    """GET /plans/merged：按药单状态与区间聚合成卡片。"""
    today = medication_service.today_in_shanghai()
    await _make_plan(db, seeded, drug_name="二甲双胍", dose="0.5g 1 片", schedule_cron="08:00")
    await _make_plan(db, seeded, drug_name="阿司匹林", dose="100mg 1 片", schedule_cron="08:00")
    await _make_plan(db, seeded, drug_name="阿托伐他汀", dose="20mg 1 片", schedule_cron="20:00")
    await _make_plan(
        db, seeded, drug_name="停用药", dose="1 片", schedule_cron="12:00", status="stopped"
    )

    client.holder[:] = [_as_doctor(seeded)]
    response = await client.client.get(
        "/api/medication/plans/merged", params={"elder_id": str(seeded.elder_id)}
    )
    assert response.status_code == 200, response.text
    body: Any = response.json()
    assert body["on_date"] == today.isoformat()
    assert body["total_slots"] == 2
    assert [slot["schedule_cron"] for slot in body["slots"]] == ["08:00", "20:00"]
    assert body["slots"][0]["drug_count"] == 2
    assert body["slots"][0]["summary"].startswith("08:00 共 2 种药：")
    assert {drug["drug_name"] for drug in body["slots"][0]["drugs"]} == {"二甲双胍", "阿司匹林"}


async def test_merged_endpoint_respects_explicit_date(
    client: Bundle, seeded: Seed, db: AsyncSession
) -> None:
    """`on_date` 显式传入时按那天算：起始日在未来的药单不会出现在今天。"""
    await _make_plan(
        db,
        seeded,
        drug_name="未来药",
        dose="1 片",
        schedule_cron="08:00",
        start_date=datetime.date(2026, 12, 1),
    )
    client.holder[:] = [_as_doctor(seeded)]
    response = await client.client.get(
        "/api/medication/plans/merged",
        params={"elder_id": str(seeded.elder_id), "on_date": "2026-11-10"},
    )
    assert response.status_code == 200
    body: Any = response.json()
    assert body["on_date"] == "2026-11-10"
    assert body["slots"] == []


# ------------------------------------------------------------------ 七、聚合关注端点
# FR-HLT-10 的服务层用例在 `tests/test_alert_aggregation.py`；这里只验 HTTP 口径。
# 端点与导入同挂在 `medication_api.router` 上，共用本文件的 client/seeded 夹具，
# 比在另一个文件里再抄一遍整套真库夹具更不容易漂。


async def test_attention_endpoint_returns_signals_without_diagnosis(
    client: Bundle, seeded: Seed, db: AsyncSession
) -> None:
    """GET /attention：四信号 + 只要「建议关注」，响应里带免责声明、无诊断字段。"""
    from app.models import HealthRecord

    for offset, score in enumerate((1, 2)):
        db.add(
            HealthRecord(
                record_id=uuid.uuid4(),
                elder_id=seeded.elder_id,
                record_date=datetime.date(2026, 11, 7 + offset),
                source="voice_scheduled",
                mood_score=score,
                grade="green",
            )
        )
    await db.flush()

    client.holder[:] = [_as_doctor(seeded)]
    response = await client.client.get(
        "/api/medication/attention", params={"elder_id": str(seeded.elder_id)}
    )
    assert response.status_code == 200, response.text

    body: Any = response.json()
    assert body["has_attention"] is True
    assert "mood_low_streak" in body["codes"]
    assert "不构成任何医疗诊断" in body["disclaimer"]
    assert body["rule_version"] == "fr-hlt-10-v1"

    signal = next(item for item in body["signals"] if item["code"] == "mood_low_streak")
    assert signal["level"] == "attention"
    assert signal["evidence"]["streak"] == 2
    assert signal["suggestion"]
    # 只出关注建议：响应体里不出现任何诊断字段，也不允许「诊断」出现在建议里
    assert "diagnosis" not in body
    assert "诊断" not in signal["suggestion"].replace("就诊", "")


# ------------------------------------------------------------------ 日志级别回归


async def test_apply_import_logs_at_info_without_reserved_keys(
    db: AsyncSession, seeded: Seed, caplog: pytest.LogCaptureFixture
) -> None:
    """INFO 打开时必须照样能写库：`extra=` 里不许出现 LogRecord 的保留键。

    2026-11-10 发现的真缺陷：`apply_import` 的 `medication_plan_import_applied`
    把创建条数写成 `extra={"created": …}`，而 `created` 是 LogRecord 的保留属性
    （记录创建时间戳）。`logging` 造记录时直接抛
    `KeyError("Attempt to overwrite 'created' in LogRecord")` —— 于是
    **INFO 日志一打开，导入就 500**。单跑本文件时不报错，是因为那时 root 级别还是
    WARNING、记录根本没被创建；全量跑时别的用例把日志配置到 INFO，缺陷才现形。
    所以这个用例显式把日志级别抬到 INFO，用「有没有产出这条日志」把缺陷钉住。
    """
    caplog.set_level(logging.INFO, logger=medication_service.__name__)
    payload = _csv_bytes([(seeded.elder_name, "二甲双胍", "0.5g 1 片", "08:00", "2026-11-10", "")])
    preview = await preview_import(db, payload=payload)
    assert preview.accepted is True, preview.issues

    result = await apply_import(db, preview=preview, created_by=seeded.doctor_id)

    assert len(result.created) == 1
    applied = [r for r in caplog.records if r.message == "medication_plan_import_applied"]
    assert applied, "INFO 级别下没有产出 medication_plan_import_applied 日志"
    assert getattr(applied[0], "created_count", None) == 1
