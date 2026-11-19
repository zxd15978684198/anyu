"""迭代 7 · 九张表模型 + 迁移 0009 一致性测试。

三天前的 `tests/test_decision_log_model.py` 只能做「静态对照」替代 alembic check，因为当时
本机无库；**现在库可用**，所以这次两面都测：

1. **ORM metadata 面**（纯内存，无库也能跑）：表名、列集合、可空性、唯一约束/CHECK
   约束名（经 `app/models/base.py` 的 naming_convention 渲染后的全名）、pgvector 维度；
2. **真库面**（`db_conn` 夹具，事务回滚）：`information_schema` 里的列名/可空性与
   `Base.metadata` 逐表对齐，`pg_constraint` 里的唯一约束与 CHECK 名确实存在
   —— 这才是「迁移真的按模型建了表」的证据，防止模型改了而迁移没跟上（或反之）。

迁移文件本身也做静态校验：revision 链指向 0008、九次 `create_table`、九次 `drop_table`
（迁移可回滚），以及 `import pgvector`（缺了它 `VECTOR(N)` 在运行期 NameError）。
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.models.media import MediaHistory, MediaLibraryItem
from app.models.medication import MedicationAttempt, MedicationEvent, MedicationPlan
from app.models.memory import EMBEDDING_DIM, MemoryChunk
from app.models.work_order import RiskEvent, WorkOrder, WorkOrderTransition
from conftest import require_db_connection

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MIGRATION = _REPO_ROOT / "alembic" / "versions" / "0009_memory_medication.py"

_MODELS = (
    MemoryChunk,
    MedicationPlan,
    MedicationEvent,
    MedicationAttempt,
    RiskEvent,
    WorkOrder,
    WorkOrderTransition,
    MediaLibraryItem,
    MediaHistory,
)

_EXPECTED_COLUMNS: dict[str, set[str]] = {
    "memory_chunks": {
        "chunk_id",
        "elder_id",
        "content",
        "embedding",
        "category",
        "importance",
        "status",
        "source_thread_id",
        "created_at",
        "last_accessed",
    },
    "medication_plans": {
        "plan_id",
        "elder_id",
        "drug_name",
        "dose",
        "schedule_cron",
        "start_date",
        "end_date",
        "version",
        "created_by",
        "status",
        # 0020 增列（FR-MED-09 到期复核 + 一键续方）：续方血缘与复核闸门。
        "renewed_from_plan_id",
        "review_notified_at",
    },
    "medication_events": {
        "reminder_id",
        "plan_id",
        "elder_id",
        "scheduled_at",
        "drug_snapshot",
        "payload_hash",
        "created_at",
        "triggered_by",
        "status",
        "terminal_state",
        "late_taken",
        "failure_reason",
        "retry_count",
        "confirmed_at",
        "confirm_channel",
        "graph_thread_id",
    },
    "medication_attempts": {
        "attempt_id",
        "event_id",
        "attempt_no",
        "sent_at",
        "receipt",
        "tts_result",
        "response",
        "response_channel",
    },
    "risk_events": {
        "event_id",
        "elder_id",
        "type",
        "severity",
        "source_type",
        "source_id",
        "status",
        "created_at",
        "acknowledged_at",
        "closed_at",
        # 0012（迭代 8 安全告警链路）新增：命中证据的脱敏快照。
        # 明确登记在此而不是让 test 放宽成「至少包含」，否则以后真丢了列也没人发现。
        "detail",
    },
    "work_orders": {
        "order_id",
        "order_no",
        "elder_id",
        "event_id",
        "type",
        "severity",
        "status",
        "assignee_id",
        "false_alarm",
        "false_alarm_reason",
        "actual_handled_at",
        "note",
        # 0022（第五轮 FR-ALT-04）新增：建单幂等键，
        # 配合 (elder_id, type, dedupe_key) 的部分唯一索引，NULL 不参与去重。
        "dedupe_key",
        "created_at",
        "closed_at",
    },
    "work_order_transitions": {
        "transition_id",
        "order_id",
        "from_status",
        "to_status",
        "actor_id",
        "at",
        "note",
    },
    "media_library": {
        "media_id",
        "type",
        "title",
        "genre",
        "era",
        "artist",
        "media_url",
        "duration",
        "copyright_status",
    },
    "media_history": {
        "history_id",
        "elder_id",
        "media_id",
        "started_at",
        "finished_at",
        "completion_rate",
    },
}

# 三个业务幂等键 + 一个人类可读单号唯一键
_EXPECTED_UNIQUE = {
    "uq_medication_events_plan_id_scheduled_at",
    "uq_medication_attempts_event_id_attempt_no",
    "uq_work_orders_order_no",
}

_EXPECTED_CHECKS = {
    "ck_memory_chunks_status",
    "ck_memory_chunks_importance",
    "ck_medication_plans_status",
    "ck_medication_plans_version_positive",
    "ck_medication_events_status",
    "ck_medication_events_terminal_state",
    "ck_medication_events_triggered_by",
    "ck_medication_events_failure_reason",
    "ck_medication_events_confirm_channel",
    "ck_medication_events_retry_count",
    "ck_medication_attempts_attempt_no",
    "ck_medication_attempts_receipt",
    "ck_medication_attempts_tts_result",
    "ck_medication_attempts_response_channel",
    "ck_risk_events_type",
    "ck_risk_events_severity",
    "ck_risk_events_status",
    "ck_work_orders_type",
    "ck_work_orders_severity",
    "ck_work_orders_status",
    "ck_work_order_transitions_from_status",
    "ck_work_order_transitions_to_status",
    "ck_media_library_type",
    "ck_media_library_duration_positive",
    "ck_media_history_completion_rate",
}


def _migration_text() -> str:
    return _MIGRATION.read_text(encoding="utf-8")


# 自建 NullPool 引擎（与 `tests/test_health_api.py` 同因）：模块级连接池会把上一个事件循环的
# asyncpg 连接留给下一个用例，第二个数据库用例就报 "Event loop is closed"。
@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db(db_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """每测试一个事务，yield 后回滚；连不上库时按 conftest 统一口径 fail。"""
    conn = await require_db_connection(db_engine)
    trans = await conn.begin()
    try:
        yield conn
    finally:
        await trans.rollback()
        await conn.close()


# ---------------------------------------------------------------------------
# 1. ORM metadata 面（不需要数据库）
# ---------------------------------------------------------------------------
def test_nine_tables_registered() -> None:
    assert {model.__tablename__ for model in _MODELS} == set(_EXPECTED_COLUMNS)


@pytest.mark.parametrize("model", _MODELS, ids=lambda m: m.__tablename__)
def test_column_set_matches_technical_design(model: type) -> None:
    table = model.__table__

    assert set(table.columns.keys()) == _EXPECTED_COLUMNS[table.name]


def test_unique_constraints_rendered_by_naming_convention() -> None:
    names = {
        constraint.name
        for model in _MODELS
        for constraint in model.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }

    assert {str(name) for name in names} >= _EXPECTED_UNIQUE


def test_check_constraints_rendered_by_naming_convention() -> None:
    names = {
        str(constraint.name)
        for model in _MODELS
        for constraint in model.__table__.constraints
        if constraint.__class__.__name__ == "CheckConstraint"
    }

    assert names >= _EXPECTED_CHECKS


def test_embedding_is_nullable_vector() -> None:
    column = MemoryChunk.__table__.columns["embedding"]

    assert column.nullable is True, "供应商不可用时为 NULL —— 降级不丢原文"
    assert column.type.dim == EMBEDDING_DIM


def test_medication_event_columns_are_the_idempotency_contract() -> None:
    columns = MedicationEvent.__table__.columns

    # 幂等键落成唯一约束（不是应用层「先查后插」的君子协定）
    assert columns["scheduled_at"].nullable is False
    assert columns["payload_hash"].nullable is False
    # 触达次数与事件状态的口径互锁
    assert columns["retry_count"].nullable is False
    assert columns["status"].nullable is False


# ---------------------------------------------------------------------------
# 2. 迁移文件静态面
# ---------------------------------------------------------------------------
def test_migration_revision_chain() -> None:
    source = _migration_text()

    # autogenerate 用单引号，人工改过之后可能变双引号 —— 两种都要认
    assert re.search(
        r"^revision(?:: str)?\s*=\s*['\"]0009_memory_medication['\"]", source, re.MULTILINE
    )
    assert re.search(
        r"^down_revision(?:: str \| None)?\s*=\s*['\"]0008_decision_logs['\"]",
        source,
        re.MULTILINE,
    )


def test_migration_creates_and_drops_the_same_nine_tables() -> None:
    source = _migration_text()
    created = set(re.findall(r"op\.create_table\(\s*['\"](\w+)['\"]", source))
    dropped = set(re.findall(r"op\.drop_table\(\s*['\"](\w+)['\"]", source))

    assert created == set(_EXPECTED_COLUMNS)
    assert dropped == created, "downgrade 必须逆序 drop 同一批表，否则不可回滚"


def test_migration_imports_pgvector() -> None:
    # autogenerate 渲染出 pgvector.sqlalchemy.vector.VECTOR(...)；缺 import 则运行期 NameError
    assert re.search(r"^import pgvector$", _migration_text(), flags=re.MULTILINE)


# ---------------------------------------------------------------------------
# 3. 真库面：迁移真的按模型建了表
# ---------------------------------------------------------------------------
async def test_database_schema_matches_models(db: AsyncConnection) -> None:
    expected = {
        table: {(column.name, column.nullable) for column in model.__table__.columns}
        for table, model in ((model.__tablename__, model) for model in _MODELS)
    }

    for table_name, model_columns in expected.items():
        rows = (
            await db.execute(
                text(
                    "SELECT column_name, is_nullable FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = :table"
                ),
                {"table": table_name},
            )
        ).all()
        actual = {(name, flag == "YES") for name, flag in rows}

        assert actual == model_columns, f"{table_name} 的库结构与 ORM 模型不一致"


async def test_database_constraints_exist(db: AsyncConnection) -> None:
    rows = (
        await db.execute(
            text("SELECT conname FROM pg_constraint WHERE connamespace = 'public'::regnamespace")
        )
    ).all()
    names = {name for (name,) in rows}

    assert names >= _EXPECTED_UNIQUE
    assert names >= _EXPECTED_CHECKS


async def test_pgvector_extension_and_dimension(db: AsyncConnection) -> None:
    dim = (
        await db.execute(
            text(
                "SELECT atttypmod FROM pg_attribute "
                "WHERE attrelid = 'memory_chunks'::regclass AND attname = 'embedding'"
            )
        )
    ).scalar_one()

    assert dim == EMBEDDING_DIM, "列维度必须与 EMBEDDING_DIM 一致（pgvector atttypmod 即维度）"
