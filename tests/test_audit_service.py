"""迭代 9 · W4a：审计查询与导出服务层（`app/services/audit_service.py`）。

覆盖的验收条款：

- **稳定排序**：`created_at DESC, audit_id DESC`。`created_at` 默认 `now()`，一次请求写两三条
  审计很容易同毫秒 —— 只按时间排会得到不确定顺序，翻页就重复/漏行，所以要主键做 tiebreak。
- **过滤组合**：`actor_type / action / actor_id / start / end` 任意组合取交集。
- **分页不重不漏**：按 offset 逐页取，拼接后与全量一致且无重复。
- **导出脱敏**：`detail` 里的手机号在导出结果里必须已被打码（`app/core/redact.py` 未落地时
  该用例 skip —— 兜底是浅拷贝，不假装脱敏了）。
- **导出留痕**：`record_export` 用**独立会话**写 `action='audit.export'`，行数与过滤条件进 detail。
- **服务层只 flush 不 commit**（写入口 `write_audit`）：事务归调用方，否则「业务失败要回滚」
  会把审计一起带走。

## 测试怎么不污染库

同 `tests/test_memory_api.py`：所有读写走 `db` fixture 的外层事务，结束整体回滚；
`write_audit_isolated` / `record_export` 的「自己开会话」用 `isolated_session_factory` 钉到
同一条测试连接（`join_transaction_mode="create_savepoint"`，它们的 `commit()` 只释放保存点）。

## 为什么用例都带时间窗

`audit_logs` 是只增表，开发库里留着历史行。断言 `total == 7` 这种数字必须先圈定范围，
否则跑第二次就红。这里把种子行放在 **2020-01-01** 这个项目诞生前的死窗口里，
每条查询都带 `start/end`，数字才可复现；唯一的「不带过滤」用例只断言我的行都在。
"""

from __future__ import annotations

import datetime
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from tests.conftest import require_db_connection

from app.core.config import get_settings
from app.models import AuditLog
from app.services import audit_service

#: 种子行的时间基准：2020-01-01 是项目诞生前的死窗口，开发库的残留行不会落在这里。
BASE = datetime.datetime(2020, 1, 1, 8, 0, tzinfo=datetime.UTC)

#: 默认查询窗（含两端），种子行 r1..r7 落在这里，r8/r9 刻意落在窗外。
WINDOW_START = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)
WINDOW_END = datetime.datetime(2020, 1, 2, tzinfo=datetime.UTC)

#: 窗口内的行数（r1..r7）。
IN_WINDOW = 7

NIL = uuid.UUID(int=0)


# --------------------------------------------------------------------- fixtures


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """外层事务：测试里所有写入最后整体回滚。"""
    conn = await require_db_connection(db_engine)
    trans = await conn.begin()
    session = AsyncSession(bind=conn, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.rollback()
        await trans.rollback()
        await session.close()
        await conn.close()


@pytest.fixture
async def factory(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> async_sessionmaker[AsyncSession]:
    """把服务层「自己开会话」钉在同一条测试连接上。"""
    bind = db.bind
    assert bind is not None
    maker = async_sessionmaker(
        bind=bind,  # type: ignore[arg-type]
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    monkeypatch.setattr(audit_service, "session_factory", maker)
    return maker


@dataclass
class Seed:
    """种子行的主键，方便按名字断言顺序。"""

    staff_id: uuid.UUID
    other_staff_id: uuid.UUID
    guardian_id: uuid.UUID
    device_id: uuid.UUID
    rows: dict[str, uuid.UUID]


async def _seed(db: AsyncSession) -> Seed:
    staff_id, other_staff_id, guardian_id, device_id = (uuid.uuid4() for _ in range(4))
    resource = uuid.uuid4()

    # 顺序即 created_at 顺序；r6/r7 同毫秒，用来验证主键 tiebreak。
    specs: list[dict[str, Any]] = [
        {
            "key": "r1",
            "actor_type": "staff",
            "actor_id": staff_id,
            "action": "memory.read",
            "resource_type": "memory_chunks",
            "resource_id": resource,
            # 键名命中敏感规则（phone）与自由文本里的手机号（note）各一条。
            "detail": {
                "trace_id": "trace-r1",
                "phone": "13800138000",
                "note": "家属号码 13800138000 已登记",
            },
            "created_at": BASE,
        },
        {
            "key": "r2",
            "actor_type": "staff",
            "actor_id": staff_id,
            "action": "medication.update",
            "resource_type": "medication_plans",
            "resource_id": resource,
            "detail": {"trace_id": "trace-r2"},
            "created_at": BASE + datetime.timedelta(seconds=1),
        },
        {
            "key": "r3",
            "actor_type": "guardian",
            "actor_id": guardian_id,
            "action": "memory.read",
            "resource_type": "memory_chunks",
            "resource_id": resource,
            "detail": {"trace_id": "trace-r3"},
            "created_at": BASE + datetime.timedelta(seconds=2),
        },
        {
            "key": "r4",
            "actor_type": "system",
            "actor_id": NIL,
            "action": "scheduler.tick",
            "resource_type": "scheduler",
            "resource_id": resource,
            "detail": None,  # detail 可空：序列化后必须是 None 而不是 {} 或缺键
            "created_at": BASE + datetime.timedelta(seconds=3),
        },
        {
            "key": "r5",
            "actor_type": "device",
            "actor_id": device_id,
            "action": "device.heartbeat",
            "resource_type": "devices",
            "resource_id": resource,
            "detail": {"trace_id": "trace-r5"},
            "created_at": BASE + datetime.timedelta(seconds=4),
        },
        {
            "key": "r6",
            "actor_type": "staff",
            "actor_id": other_staff_id,
            "action": "memory.read",
            "resource_type": "memory_chunks",
            "resource_id": resource,
            "detail": {"trace_id": "trace-r6"},
            "created_at": BASE + datetime.timedelta(seconds=5),
        },
        {
            "key": "r7",
            "actor_type": "staff",
            "actor_id": other_staff_id,
            "action": "memory.read",
            "resource_type": "memory_chunks",
            "resource_id": resource,
            "detail": {"trace_id": "trace-r7"},
            # 与 r6 同毫秒：排序必须由 audit_id 决出，不能靠运气
            "created_at": BASE + datetime.timedelta(seconds=5),
        },
        {
            "key": "r8",
            "actor_type": "staff",
            "actor_id": staff_id,
            "action": "dashboard.view",
            "resource_type": "dashboard",
            "resource_id": resource,
            "detail": {"trace_id": "trace-r8"},
            "created_at": datetime.datetime(2019, 12, 31, 23, 59, 59, tzinfo=datetime.UTC),
        },
        {
            "key": "r9",
            "actor_type": "guardian",
            "actor_id": guardian_id,
            "action": "safety.alert",
            "resource_type": "alerts",
            "resource_id": resource,
            "detail": {"trace_id": "trace-r9"},
            "created_at": datetime.datetime(2020, 1, 2, 0, 0, 1, tzinfo=datetime.UTC),
        },
    ]

    rows: dict[str, uuid.UUID] = {}
    payload: list[dict[str, Any]] = []
    for spec in specs:
        item = dict(spec)
        key = item.pop("key")
        audit_id = uuid.uuid4()
        rows[key] = audit_id
        payload.append({"audit_id": audit_id, **item})

    await db.execute(AuditLog.__table__.insert(), payload)
    await db.flush()

    return Seed(
        staff_id=staff_id,
        other_staff_id=other_staff_id,
        guardian_id=guardian_id,
        device_id=device_id,
        rows=rows,
    )


@pytest.fixture
async def seeded(db: AsyncSession) -> Seed:
    return await _seed(db)


async def _in_window(db: AsyncSession, **kwargs: Any) -> list[AuditLog]:
    """默认窗口内查询（数字可复现）。"""
    kwargs.setdefault("start", WINDOW_START)
    kwargs.setdefault("end", WINDOW_END)
    kwargs.setdefault("limit", audit_service.MAX_LIMIT)
    return list(await audit_service.list_audits(db, **kwargs))


def _ids(rows: list[AuditLog]) -> list[uuid.UUID]:
    return [row.audit_id for row in rows]


def _within(seed: Seed, keys: str) -> set[uuid.UUID]:
    return {seed.rows[key] for key in keys.split()}


# --------------------------------------------------------------------- 查询与排序


async def test_list_without_filters_returns_seeded_rows(
    db: AsyncSession, seeded: Seed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """不带过滤时至少要包含全部种子行（库里有历史行，不能断言精确条数）。"""
    rows = await audit_service.list_audits(db, limit=audit_service.MAX_LIMIT)
    returned = set(_ids(rows))
    assert set(seeded.rows.values()) <= returned


async def test_list_time_window_is_inclusive_on_both_ends(
    db: AsyncSession, seeded: Seed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """`start` / `end` 都是闭区间：端点上的行必须算进来。"""
    rows = await _in_window(db, start=BASE, end=BASE + datetime.timedelta(seconds=5))
    assert set(_ids(rows)) == _within(seeded, "r1 r2 r3 r4 r5 r6 r7")

    rows = await _in_window(db, start=BASE, end=BASE)
    assert _ids(rows) == [seeded.rows["r1"]]


async def test_list_excludes_rows_outside_window(
    db: AsyncSession, seeded: Seed, factory: async_sessionmaker[AsyncSession]
) -> None:
    rows = await _in_window(db)
    assert len(rows) == IN_WINDOW
    assert seeded.rows["r8"] not in _ids(rows)
    assert seeded.rows["r9"] not in _ids(rows)


async def test_list_orders_by_created_at_desc_then_audit_id(
    db: AsyncSession, seeded: Seed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """倒序 + 同毫秒按 `audit_id` 倒序；两次调用必须完全一致。"""
    rows = await _in_window(db)
    expected = sorted(rows, key=lambda row: (row.created_at, row.audit_id), reverse=True)
    assert _ids(rows) == _ids(expected)
    assert _ids(await _in_window(db)) == _ids(rows)


async def test_list_filter_by_actor_type(
    db: AsyncSession, seeded: Seed, factory: async_sessionmaker[AsyncSession]
) -> None:
    rows = await _in_window(db, actor_type="guardian")
    assert _ids(rows) == [seeded.rows["r3"]]

    staff = await _in_window(db, actor_type="staff")
    assert set(_ids(staff)) == _within(seeded, "r1 r2 r6 r7")


async def test_list_filter_by_action(
    db: AsyncSession, seeded: Seed, factory: async_sessionmaker[AsyncSession]
) -> None:
    rows = await _in_window(db, action="memory.read")
    assert set(_ids(rows)) == _within(seeded, "r1 r3 r6 r7")


async def test_list_filter_by_actor_id(
    db: AsyncSession, seeded: Seed, factory: async_sessionmaker[AsyncSession]
) -> None:
    rows = await _in_window(db, actor_id=seeded.guardian_id)
    assert _ids(rows) == [seeded.rows["r3"]]

    rows = await _in_window(db, actor_id=seeded.other_staff_id)
    assert set(_ids(rows)) == _within(seeded, "r6 r7")


async def test_list_filter_combination_intersects(
    db: AsyncSession, seeded: Seed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """三个过滤条件是 AND：staff + memory.read + 某个 actor_id。"""
    rows = await _in_window(
        db, actor_type="staff", action="memory.read", actor_id=seeded.other_staff_id
    )
    assert set(_ids(rows)) == _within(seeded, "r6 r7")

    rows = await _in_window(
        db, actor_type="guardian", action="memory.read", actor_id=seeded.other_staff_id
    )
    assert rows == []


async def test_list_pagination_has_no_gaps_or_duplicates(
    db: AsyncSession, seeded: Seed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """按 3 条一页翻到底：拼接结果必须等于全量，且没有重复。"""
    full = _ids(await _in_window(db))

    pages: list[uuid.UUID] = []
    offset = 0
    while True:
        page = await _in_window(db, limit=3, offset=offset)
        if not page:
            break
        pages.extend(_ids(page))
        offset += 3

    assert pages == full
    assert len(pages) == len(set(pages)) == IN_WINDOW


async def test_count_matches_list_filters(
    db: AsyncSession, seeded: Seed, factory: async_sessionmaker[AsyncSession]
) -> None:
    filters = {"start": WINDOW_START, "end": WINDOW_END}
    assert await audit_service.count_audits(db, **filters) == IN_WINDOW
    assert await audit_service.count_audits(db, **filters, actor_type="staff") == 4
    assert await audit_service.count_audits(db, **filters, action="memory.read") == 4
    assert await audit_service.count_audits(db, **filters, action="no.such.action") == 0


async def test_count_is_independent_of_pagination(
    db: AsyncSession, seeded: Seed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """`total` 是过滤后的总条数，不该被 `limit` 影响。"""
    filters = {"start": WINDOW_START, "end": WINDOW_END}
    page = await audit_service.list_audits(db, limit=2, **filters)
    assert len(page) == 2
    assert await audit_service.count_audits(db, **filters) == IN_WINDOW


# --------------------------------------------------------------------- 参数校验


async def test_invalid_actor_type_raises(db: AsyncSession, seeded: Seed) -> None:
    with pytest.raises(audit_service.AuditQueryError, match="actor_type"):
        await audit_service.list_audits(db, actor_type="nurse")


@pytest.mark.parametrize("limit", [0, -1, audit_service.MAX_LIMIT + 1])
async def test_invalid_limit_raises(db: AsyncSession, seeded: Seed, limit: int) -> None:
    with pytest.raises(audit_service.AuditQueryError, match="limit"):
        await audit_service.list_audits(db, limit=limit)


async def test_invalid_offset_raises(db: AsyncSession, seeded: Seed) -> None:
    with pytest.raises(audit_service.AuditQueryError, match="offset"):
        await audit_service.list_audits(db, offset=-1)


async def test_reversed_window_raises(db: AsyncSession, seeded: Seed) -> None:
    with pytest.raises(audit_service.AuditQueryError, match="start"):
        await audit_service.list_audits(db, start=WINDOW_END, end=WINDOW_START)


async def test_blank_action_raises(db: AsyncSession, seeded: Seed) -> None:
    with pytest.raises(audit_service.AuditQueryError, match="action"):
        await audit_service.list_audits(db, action="   ")


async def test_naive_datetime_is_treated_as_utc(
    db: AsyncSession, seeded: Seed, factory: async_sessionmaker[AsyncSession]
) -> None:
    # 无时区的时间按 UTC 解释（AGENTS §8 存储一律 UTC），而不是被拒绝
    naive = datetime.datetime(2020, 1, 1, 8, 0, 0)
    rows = await audit_service.list_audits(db, start=naive, end=naive)
    assert _ids(rows) == [seeded.rows["r1"]]


# --------------------------------------------------------------------- 序列化


async def test_serialize_has_stable_keys_and_iso_utc(db: AsyncSession, seeded: Seed) -> None:
    row = (
        await db.execute(select(AuditLog).where(AuditLog.audit_id == seeded.rows["r1"]))
    ).scalar_one()
    payload = audit_service.serialize_audit(row)

    assert tuple(payload) == audit_service.EXPORT_KEYS
    assert payload["audit_id"] == str(seeded.rows["r1"])
    assert payload["actor_type"] == "staff"
    assert payload["actor_id"] == str(seeded.staff_id)
    assert payload["action"] == "memory.read"
    # 时间是带 UTC 偏移的 ISO 8601，不是 naive 字符串
    assert payload["created_at"] == "2020-01-01T08:00:00+00:00"
    assert payload["trace_id"] == "trace-r1"


async def test_serialize_null_detail_becomes_none_and_trace_is_none(
    db: AsyncSession, seeded: Seed
) -> None:
    row = (
        await db.execute(select(AuditLog).where(AuditLog.audit_id == seeded.rows["r4"]))
    ).scalar_one()
    payload = audit_service.serialize_audit(row)

    assert payload["detail"] is None
    assert payload["trace_id"] is None  # 键仍在，值为 None（键集合不随数据变化）
    assert tuple(payload) == audit_service.EXPORT_KEYS


# --------------------------------------------------------------------- 导出


async def test_export_returns_filtered_rows_with_stable_keys(
    db: AsyncSession, seeded: Seed
) -> None:
    records = await audit_service.export_audits(
        db, start=WINDOW_START, end=WINDOW_END, action="memory.read"
    )
    assert len(records) == 4
    for record in records:
        assert tuple(record) == audit_service.EXPORT_KEYS
        # 必须能直接 json 序列化（导出就是 json.dumps 出去的）
        json.dumps(record, ensure_ascii=False)


@pytest.mark.skipif(
    not audit_service.REDACT_AVAILABLE,
    reason="app/core/redact.py 未落地，兜底是浅拷贝（不脱敏）",
)
async def test_export_masks_phone_in_detail(db: AsyncSession, seeded: Seed) -> None:
    """手机号必须被打码：键名命中（phone）与自由文本（note）两条路都要挡住。"""
    records = await audit_service.export_audits(
        db, start=WINDOW_START, end=WINDOW_END, actor_id=seeded.staff_id, action="memory.read"
    )
    assert [record["audit_id"] for record in records] == [str(seeded.rows["r1"])]

    detail = records[0]["detail"]
    assert detail is not None
    dumped = json.dumps(detail, ensure_ascii=False)
    assert "13800138000" not in dumped
    assert "138****8000" in dumped  # 打了码但保留可辨识片段
    assert detail["trace_id"] == "trace-r1"  # 非敏感键照旧透出


async def test_export_respects_max_rows(db: AsyncSession, seeded: Seed) -> None:
    records = await audit_service.export_audits(db, start=WINDOW_START, end=WINDOW_END, max_rows=3)
    assert len(records) == 3


@pytest.mark.parametrize("max_rows", [0, -5, audit_service.MAX_EXPORT_ROWS + 1])
async def test_export_rejects_invalid_max_rows(
    db: AsyncSession, seeded: Seed, max_rows: int
) -> None:
    """超上限直接抛错而不是静默截断：静默截断会让导出方以为拿到了全量。"""
    with pytest.raises(audit_service.AuditQueryError, match="max_rows"):
        await audit_service.export_audits(db, start=WINDOW_START, end=WINDOW_END, max_rows=max_rows)


async def test_export_audit_jsonl_writes_one_line_per_row(
    db: AsyncSession, seeded: Seed, tmp_path: Path
) -> None:
    target = tmp_path / "audit.ndjson"
    written = await audit_service.export_audit_jsonl(
        db, path=target, start=WINDOW_START, end=WINDOW_END
    )

    assert written == IN_WINDOW
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == IN_WINDOW
    parsed = [json.loads(line) for line in lines]
    assert all(tuple(item) == audit_service.EXPORT_KEYS for item in parsed)
    assert {item["audit_id"] for item in parsed} == {
        str(value) for value in _within(seeded, "r1 r2 r3 r4 r5 r6 r7")
    }
    # 倒序落盘：created_at 序列单调不增
    stamps = [item["created_at"] for item in parsed]
    assert stamps == sorted(stamps, reverse=True)


async def test_export_audit_jsonl_empty_writes_empty_file(
    db: AsyncSession, seeded: Seed, tmp_path: Path
) -> None:
    """空结果写 0 字节文件，而不是一个空行（空行会被下游当坏行）。"""
    target = tmp_path / "empty.ndjson"
    empty_window = (
        datetime.datetime(2019, 12, 30, tzinfo=datetime.UTC),
        datetime.datetime(2019, 12, 31, tzinfo=datetime.UTC),
    )
    written = await audit_service.export_audit_jsonl(
        db, path=target, start=empty_window[0], end=empty_window[1]
    )
    assert written == 0
    assert target.read_text(encoding="utf-8") == ""


async def test_export_audit_jsonl_accepts_str_path(
    db: AsyncSession, seeded: Seed, tmp_path: Path
) -> None:
    target = tmp_path / "as-str.ndjson"
    written = await audit_service.export_audit_jsonl(
        db, path=str(target), start=WINDOW_START, end=WINDOW_END, max_rows=2
    )
    assert written == 2
    assert len(target.read_text(encoding="utf-8").splitlines()) == 2


# --------------------------------------------------------------------- 写入口


async def test_write_audit_flushes_without_committing(
    db: AsyncSession, seeded: Seed, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`write_audit` 只 flush：事务归调用方，否则业务回滚会把审计一起带走。"""
    commits: list[int] = []

    async def fake_commit() -> None:  # pragma: no cover - 被调用即失败
        commits.append(1)

    monkeypatch.setattr(db, "commit", fake_commit)

    row = await audit_service.write_audit(
        db,
        actor_type="staff",
        actor_id=seeded.staff_id,
        action="audit.read",
        resource_type=audit_service.AUDIT_RESOURCE_TYPE,
        resource_id=NIL,
        detail={"reason": "manual"},
        trace_id="trace-write",
    )

    assert commits == []
    assert row.audit_id is not None
    # 已 flush：同一会话里能查回来
    found = (
        await db.execute(select(AuditLog).where(AuditLog.audit_id == row.audit_id))
    ).scalar_one()
    assert found.action == "audit.read"
    assert found.detail == {"reason": "manual", "trace_id": "trace-write"}


async def test_write_audit_masks_detail_on_write(db: AsyncSession, seeded: Seed) -> None:
    """列注释要求「脱敏后」：写入口就是第一道闸，手机号不落库。"""
    row = await audit_service.write_audit(
        db,
        actor_type="staff",
        actor_id=seeded.staff_id,
        action="audit.read",
        resource_type=audit_service.AUDIT_RESOURCE_TYPE,
        resource_id=NIL,
        detail={"phone": "13800138000"},
    )
    assert row.detail is not None
    if audit_service.REDACT_AVAILABLE:
        assert row.detail["phone"] != "13800138000"
    else:  # 兜底：浅拷贝，值原样保留
        assert row.detail["phone"] == "13800138000"


async def test_write_audit_rejects_invalid_actor_type(db: AsyncSession) -> None:
    """CHECK 只有四个值：非法种类当场炸，别等 INSERT 撞库。"""
    with pytest.raises(audit_service.AuditQueryError, match="actor_type"):
        await audit_service.write_audit(
            db,
            actor_type="nurse",
            actor_id=NIL,
            action="x",
            resource_type="y",
            resource_id=NIL,
        )


async def test_write_audit_isolated_survives_caller_rollback(
    db: AsyncSession, seeded: Seed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """独立会话立即提交：调用方的 403 回滚不会抹掉这条留痕。"""
    ok = await audit_service.write_audit_isolated(
        actor_type="staff",
        actor_id=seeded.staff_id,
        action="audit.read",
        resource_type=audit_service.AUDIT_RESOURCE_TYPE,
        resource_id=NIL,
        detail={"reason": "non_admin"},
        trace_id="trace-denied",
    )
    assert ok is True

    denied = list(
        await db.scalars(
            select(AuditLog)
            .where(AuditLog.action == "audit.read")
            .where(AuditLog.actor_id == seeded.staff_id)
        )
    )
    assert len(denied) == 1
    assert denied[0].detail == {"reason": "non_admin", "trace_id": "trace-denied"}
    # 调用方事务没有被提交：外层回滚依然有效（这正是"独立会话"的意义）
    assert db.in_transaction() is True


async def test_write_audit_isolated_never_raises_on_broken_factory(
    db: AsyncSession, seeded: Seed, monkeypatch: pytest.MonkeyPatch
) -> None:
    """审计是旁路：写失败返回 False + 日志，不能把 403 变成 500。"""

    def boom() -> Any:
        raise RuntimeError("db down")

    monkeypatch.setattr(audit_service, "session_factory", boom)
    ok = await audit_service.write_audit_isolated(
        actor_type="staff",
        actor_id=seeded.staff_id,
        action="audit.read",
        resource_type=audit_service.AUDIT_RESOURCE_TYPE,
        resource_id=NIL,
    )
    assert ok is False


# --------------------------------------------------------------------- 导出留痕


async def test_record_export_writes_audit_export_row(
    db: AsyncSession, seeded: Seed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """默认走独立会话：`action='audit.export'`，过滤条件与行数进 detail。"""
    filters = {"actor_type": "staff", "actor_id": seeded.staff_id, "start": WINDOW_START}
    await audit_service.record_export(
        actor_type="staff",
        actor_id=seeded.staff_id,
        trace_id="trace-export",
        filters=filters,
        rows=IN_WINDOW,
    )

    row = (
        (await db.execute(select(AuditLog).where(AuditLog.action == audit_service.EXPORT_ACTION)))
        .scalars()
        .all()
    )
    assert len(row) == 1
    record = row[0]
    assert record.actor_type == "staff"
    assert record.actor_id == seeded.staff_id
    assert record.resource_type == audit_service.AUDIT_RESOURCE_TYPE
    assert record.resource_id == NIL
    assert record.detail is not None
    assert record.detail["rows"] == IN_WINDOW
    assert record.detail["trace_id"] == "trace-export"
    # 过滤条件里的 UUID / datetime 必须已转成可进 JSONB 的原生值
    assert record.detail["filters"] == {
        "actor_type": "staff",
        "actor_id": str(seeded.staff_id),
        "start": "2020-01-01T00:00:00+00:00",
    }


async def test_record_export_selects_commit_behaviour_by_argument(
    db: AsyncSession, seeded: Seed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """传会话 → 只在调用方事务里 flush；不传 → 自己开会话并提交。"""
    await audit_service.record_export(
        db,
        actor_type="guardian",
        actor_id=seeded.guardian_id,
        trace_id="trace-export-2",
        filters={},
        rows=0,
    )
    rows = list(
        await db.scalars(select(AuditLog).where(AuditLog.action == audit_service.EXPORT_ACTION))
    )
    assert len(rows) == 1
    assert rows[0].detail == {
        "filters": {},
        "rows": 0,
        "trace_id": "trace-export-2",
    }


async def test_record_export_via_factory_isolation(
    db: AsyncSession, seeded: Seed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """显式传 factory 时用它（而不是模块级全局），便于调度/脚本复用。"""
    await audit_service.record_export(
        factory,
        actor_type="system",
        actor_id=NIL,
        trace_id=None,
        filters={"action": "memory.read"},
        rows=4,
    )
    rows = list(
        await db.scalars(select(AuditLog).where(AuditLog.action == audit_service.EXPORT_ACTION))
    )
    assert len(rows) == 1
    assert rows[0].detail is not None
    assert rows[0].detail["rows"] == 4
    # trace_id 缺省时不该出现 None 值键（宁缺勿脏）
    assert "trace_id" not in rows[0].detail


# --------------------------------------------------------------------- actor_type 映射


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("staff", "staff"),
        ("staff_card", "staff"),
        ("guardian", "guardian"),
        ("device", "device"),
        ("system", "system"),
    ],
)
def test_actor_type_of_maps_identity_kind(kind: str, expected: str) -> None:
    assert audit_service.actor_type_of(kind) == expected


def test_actor_type_of_rejects_unknown_kind() -> None:
    with pytest.raises(audit_service.AuditQueryError):
        audit_service.actor_type_of("robot")
