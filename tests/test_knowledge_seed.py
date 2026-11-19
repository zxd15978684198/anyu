"""N22 种子知识：解析、中文别名、幂等灌库（`app/services/knowledge_seed.py`）。

这些用例钉住三件事：

1. **四份种子的 front-matter 能对上模型 CHECK**：文件里写的是「作息/服务/活动/应急」
   这类中文类别、`应急流程.md` 还把类别填进了 `authority_level`。别名表就是为这两件事
   存在的，改错别名会让 CLI 静默灌错类别，所以每条别名都有断言。
2. **生效日按上海算**：`effective_from: 2026-11-01` 存进 UTC 会变成 10-31T16:00Z，
   直接 `.date()` 就写成 10-31。`local_effective_date` 专门负责这件事。
3. **可重复执行**：第二次跑必须全部 `skipped`、不新增行 —— CLI 的「幂等」不能只是文档
   里的口号（task-39 的验收要求就是「可重复执行」）。

数据库用例沿用 `tests/test_medication_plan_edit.py` 的夹具：单连接 + 外层事务回滚。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from tests.conftest import require_db_connection

from app.core.config import get_settings
from app.models import AuditLog, KnowledgeChunk, KnowledgeDocument, StaffAccount
from app.models.knowledge import KNOWLEDGE_CATEGORIES
from app.services import knowledge_seed
from app.services.embedding import DeterministicEmbeddingProvider
from app.services.knowledge_seed import SeedDocument, SeedError

EXPECTED_TITLES = (
    # 顺序 = 文件名排序（Unicode 码位：作 < 应 < 服 < 活），load_seed_documents 不重排。
    "机构作息安排",
    "身体不适时的应急流程",
    "日常服务与探视流程",
    "本周活动室安排",
)

#: 上海 2026-11-01 00:00 == UTC 2026-10-31 16:00（差一天正是「按 UTC 取 date」的坑）。
SHANGHAI_2026_11_01_UTC = datetime.datetime(2026, 10, 31, 16, 0, tzinfo=datetime.UTC)


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


# --------------------------------------------------------------------- 解析


def test_real_seed_files_parse_into_model_enums() -> None:
    documents = knowledge_seed.load_seed_documents()

    assert [doc.title for doc in documents] == list(EXPECTED_TITLES)
    for doc in documents:
        assert doc.category in KNOWLEDGE_CATEGORIES, doc.title
        assert doc.authority_level == "institution", doc.title
        assert doc.version == 1
        assert doc.effective_from == SHANGHAI_2026_11_01_UTC
        assert doc.local_effective_date == datetime.date(2026, 11, 1)
        assert doc.body.strip()


def test_chinese_category_aliases_cover_all_four_seeds() -> None:
    """「作息/服务/活动/应急」四个中文词都必须落到合法枚举上（别名表的最小验证）。"""
    by_title = {doc.title: doc for doc in knowledge_seed.load_seed_documents()}

    assert by_title["机构作息安排"].category == "schedule"
    assert by_title["日常服务与探视流程"].category == "service"
    assert by_title["本周活动室安排"].category == "activity"
    # `应急流程.md` 的 front-matter 只写了 `category: 应急`，没有 authority 语义 ——
    # 归 policy 是 DAG N22 的裁决；authority 走 `policy -> institution` 别名兜回。
    assert by_title["身体不适时的应急流程"].category == "policy"
    assert by_title["身体不适时的应急流程"].authority_level == "institution"


def test_seed_body_keeps_the_lights_out_time() -> None:
    """种子正文是「知识能不能被检索到」的唯一内容来源，关键条款别在改写中丢。"""
    by_title = {doc.title: doc for doc in knowledge_seed.load_seed_documents()}

    assert "熄灯时间 21:30" in by_title["机构作息安排"].body
    assert "09:00 至 20:00" in by_title["日常服务与探视流程"].body


def _write_seed(tmp_path: Path, *, front: str, body: str = "正文内容。\n") -> Path:
    path = tmp_path / "seed.md"
    path.write_text(f"---\n{front}\n---\n\n{body}", encoding="utf-8")
    return path


def test_unknown_category_is_rejected_with_a_readable_error(tmp_path: Path) -> None:
    path = _write_seed(
        tmp_path,
        front=(
            "title: 天气安排\ncategory: 天气\neffective_from: 2026-11-01\n"
            "authority_level: institution"
        ),
    )

    with pytest.raises(SeedError) as err:
        knowledge_seed.parse_seed_document(path)

    assert "category" in str(err.value)


def test_unknown_authority_level_is_rejected(tmp_path: Path) -> None:
    path = _write_seed(
        tmp_path,
        front=(
            "title: 某制度\ncategory: policy\neffective_from: 2026-11-01\nauthority_level: 民间说法"
        ),
    )

    with pytest.raises(SeedError) as err:
        knowledge_seed.parse_seed_document(path)

    assert "authority" in str(err.value)


def test_missing_required_key_is_rejected(tmp_path: Path) -> None:
    path = _write_seed(
        tmp_path,
        front="category: schedule\neffective_from: 2026-11-01\nauthority_level: institution",
    )

    with pytest.raises(SeedError) as err:
        knowledge_seed.parse_seed_document(path)

    assert "title" in str(err.value)


def test_missing_front_matter_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "seed.md"
    path.write_text("没有 front-matter 的正文。\n", encoding="utf-8")

    with pytest.raises(SeedError):
        knowledge_seed.parse_seed_document(path)


def test_empty_seed_directory_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(SeedError):
        knowledge_seed.load_seed_documents(tmp_path)


def test_missing_seed_directory_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(SeedError):
        knowledge_seed.load_seed_documents(tmp_path / "not-there")


# --------------------------------------------------------------------- 写入人


async def _seed_staff(db: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    admin_id, doctor_id = uuid.uuid4(), uuid.uuid4()
    await db.execute(
        StaffAccount.__table__.insert(),
        [
            {"staff_id": admin_id, "name": "王管理", "role": "admin", "password_hash": "x"},
            {"staff_id": doctor_id, "name": "王医生", "role": "doctor", "password_hash": "x"},
        ],
    )
    await db.flush()
    return admin_id, doctor_id


async def test_resolve_actor_falls_back_to_admin(db: AsyncSession) -> None:
    admin_id, _ = await _seed_staff(db)
    admins = set(
        await db.scalars(select(StaffAccount.staff_id).where(StaffAccount.role == "admin"))
    )

    resolved = await knowledge_seed.resolve_actor(db)

    # 库里可能已有别的 admin（演示数据），所以断言「落到了一个 admin 上」而不是「等于我刚插的那个」。
    assert resolved in admins
    assert admin_id in admins


async def test_resolve_actor_accepts_existing_explicit_id(db: AsyncSession) -> None:
    _, doctor_id = await _seed_staff(db)

    assert await knowledge_seed.resolve_actor(db, actor_id=doctor_id) == doctor_id


async def test_resolve_actor_rejects_unknown_id(db: AsyncSession) -> None:
    await _seed_staff(db)

    with pytest.raises(SeedError):
        await knowledge_seed.resolve_actor(db, actor_id=uuid.uuid4())


# --------------------------------------------------------------------- 幂等灌库


def _document(title: str) -> SeedDocument:
    return SeedDocument(
        title=title,
        category="schedule",
        version=1,
        effective_from=SHANGHAI_2026_11_01_UTC,
        authority_level="institution",
        body="每天早上 06:30 响起床音乐。\n\n熄灯时间 21:30，之后请保持房间安静。\n",
        source=Path("docs/knowledge_seed/作息.md"),
    )


async def _counts(db: AsyncSession, titles: tuple[str, ...]) -> tuple[int, int]:
    documents = await db.scalar(
        select(func.count())
        .select_from(KnowledgeDocument)
        .where(KnowledgeDocument.title.in_(titles))
    )
    chunks = await db.scalar(
        select(func.count())
        .select_from(KnowledgeChunk)
        .join(KnowledgeDocument, KnowledgeDocument.document_id == KnowledgeChunk.document_id)
        .where(KnowledgeDocument.title.in_(titles))
    )
    return int(documents or 0), int(chunks or 0)


async def test_seeding_is_idempotent_and_reports_what_it_did(db: AsyncSession) -> None:
    admin_id, _ = await _seed_staff(db)
    suffix = uuid.uuid4().hex[:8]
    titles = (f"测试作息-{suffix}", f"测试服务-{suffix}")
    documents = [_document(titles[0]), replace(_document(titles[0]), title=titles[1])]
    provider = DeterministicEmbeddingProvider()

    first = await knowledge_seed.seed_documents(
        db, documents, actor_id=admin_id, embedding_provider=provider
    )
    second = await knowledge_seed.seed_documents(
        db, documents, actor_id=admin_id, embedding_provider=provider
    )

    assert len(first.created) == 2
    assert first.skipped == ()
    assert first.chunks == 4  # 每份两段 → 两块
    assert first.provider == "DeterministicEmbeddingProvider"
    assert first.semantic is False

    assert second.created == ()
    assert set(second.skipped) == {f"{title} v1" for title in titles}
    assert second.chunks == 0

    assert await _counts(db, titles) == (2, 4)


async def test_seeding_writes_audit_rows_for_each_document(db: AsyncSession) -> None:
    admin_id, _ = await _seed_staff(db)
    title = f"测试制度-{uuid.uuid4().hex[:8]}"
    document = replace(_document(title), category="policy", body="探视时间 09:00 至 20:00。\n")

    await knowledge_seed.seed_documents(
        db, [document], actor_id=admin_id, embedding_provider=DeterministicEmbeddingProvider()
    )

    actions = list(
        await db.scalars(
            select(AuditLog.action).where(
                AuditLog.resource_type == "knowledge_documents",
                AuditLog.action == "knowledge.document.ingest",
                AuditLog.actor_id == admin_id,
            )
        )
    )
    assert actions == ["knowledge.document.ingest"]
