"""机构知识种子灌库（DAG N22；讲义 §9.1 建库 → §9.2 检索的前一步）。

## 这个文件解决什么问题

检索端（N21）交付后，`knowledge_documents` / `knowledge_chunks` 两张表是空的：
检索只会稳定返回 `{"hits": [], "reason": "no_match"}`。仓库里已经有 4 份人工写好的
种子（`docs/knowledge_seed/*.md`，作息 / 服务流程 / 活动说明 / 应急流程），本模块把
它们解析、切块、向量化并**走 `knowledge_service.ingest_document` 这一个写入口**入库。

## 三条口径

1. **幂等**：按 `(title, version)` 判重，已存在就 `skip` 而不是报错 —— 重复执行
   （换机器、重灌、CI 冒烟）是常态，报错会让「重跑一次」变成手工删库。
2. **front-matter 不是 YAML**：4 份种子只用得到 `title/category/version/effective_from/
   authority_level` 五个键，所以这里用一个 `key: value` 的极简解析器，**不引入 PyYAML**。
   解析失败（缺分隔线、缺必填键）一律抛 `SeedError`，不做「猜一个默认值」。
3. **类别与权威级别的字面量只在这里翻译一次**：种子里的 `category` 是中文口语
   （「作息」「应急」），入库必须是模型 CHECK 的六值枚举；`应急流程.md` 还把 `policy`
   写进了 `authority_level` 字段。两处都用显式别名表收口 —— 值不认识就报错，不静默映射。

## 与写入口的关系

本模块**不** `session.add(KnowledgeDocument)`：所有插入都走
`knowledge_service.ingest_document`（含审计 `knowledge.document.ingest`），
与 admin API 完全同一条路径（AGENTS §4 第 8 条：一个能力一种形态）。
"""

from __future__ import annotations

import datetime
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import KnowledgeDocument
from app.models.staff import StaffAccount
from app.services import knowledge_service
from app.services.embedding import EmbeddingProvider, build_embedding_provider

logger = logging.getLogger("app.services.knowledge_seed")

#: 种子目录：仓库根的 `docs/knowledge_seed/`（本文件位于 `app/services/`，上溯两级）。
DEFAULT_SEED_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "docs" / "knowledge_seed"

#: 种子写作时使用的本地时区：`effective_from: 2026-11-01` 是「上海时间的 11 月 1 日」。
_LOCAL_TIMEZONE: Final[ZoneInfo] = ZoneInfo("Asia/Shanghai")

#: 中文口语 → 模型 CHECK 的六值枚举。值不在表里就当配置错误（不静默猜）。
CATEGORY_ALIASES: Final[dict[str, str]] = {
    "作息": "schedule",
    "时间": "schedule",
    "schedule": "schedule",
    "餐饮": "meal",
    "用餐": "meal",
    "meal": "meal",
    "活动": "activity",
    "娱乐": "activity",
    "activity": "activity",
    "制度": "policy",
    "政策": "policy",
    "规定": "policy",
    "应急": "policy",
    "policy": "policy",
    "设施": "facility",
    "设备": "facility",
    "facility": "facility",
    "服务": "service",
    "流程": "service",
    "service": "service",
}

#: 种子里的权威级别写法 → 模型 CHECK 的两值。`policy` 是 `应急流程.md` 把类别写错位置，
#: 该文件正文都是机构规定，按 `institution` 收（此处是**唯一**一处容错，别在别处再抄）。
AUTHORITY_ALIASES: Final[dict[str, str]] = {
    "institution": "institution",
    "机构": "institution",
    "policy": "institution",
    "elder_memory": "elder_memory",
    "长者": "elder_memory",
}

#: front-matter 必须提供的键（`version` 缺省 1，其余缺一即错）。
_REQUIRED_KEYS: Final[tuple[str, ...]] = (
    "title",
    "category",
    "effective_from",
    "authority_level",
)


class SeedError(knowledge_service.KnowledgeServiceError):
    """种子文件解析失败 / 目录为空 / 找不到可用的写入人。**不是**「没有数据」。"""


@dataclass(frozen=True, slots=True)
class SeedDocument:
    """一份解析好的种子（还没入库）：最后一项是来源文件，审计里要留痕。"""

    title: str
    category: str
    version: int
    effective_from: datetime.datetime
    authority_level: str
    body: str
    source: Path

    @property
    def local_effective_date(self) -> datetime.date:
        """生效日在上海时区的日期：`effective_from` 存 UTC，直接 `.date()` 会差一天。"""
        return self.effective_from.astimezone(_LOCAL_TIMEZONE).date()


@dataclass(frozen=True, slots=True)
class SeedSummary:
    """一次灌库的结果：新建了哪些、跳过了哪些、落了多少块、用的哪个 provider。"""

    created: tuple[str, ...]
    skipped: tuple[str, ...]
    chunks: int
    provider: str
    semantic: bool


def parse_seed_document(path: Path) -> SeedDocument:
    """解析一份种子文件（front-matter + 正文）。任何不合规都抛 `SeedError`。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SeedError(f"cannot read seed file {path}: {exc}") from exc

    front, body = _split_front_matter(text, path)
    title = _clean_required(front, "title", path)
    category = _normalise_category(_clean_required(front, "category", path), path)
    authority = _normalise_authority(_clean_required(front, "authority_level", path), path)
    version = _parse_version(front.get("version", "1"), path)
    effective_from = _parse_date(_clean_required(front, "effective_from", path), path)

    if not body.strip():
        raise SeedError(f"{path.name}: body is empty after the front-matter")
    return SeedDocument(
        title=title,
        category=category,
        version=version,
        effective_from=effective_from,
        authority_level=authority,
        body=body,
        source=path,
    )


def load_seed_documents(seed_dir: Path | None = None) -> list[SeedDocument]:
    """读目录下全部 `.md`（按文件名排序，保证输出稳定），空目录按错误处理。"""
    directory = seed_dir if seed_dir is not None else DEFAULT_SEED_DIR
    if not directory.is_dir():
        raise SeedError(f"seed directory not found: {directory}")
    paths = sorted(path for path in directory.glob("*.md") if path.is_file())
    if not paths:
        raise SeedError(f"no seed markdown found in {directory}")
    return [parse_seed_document(path) for path in paths]


async def resolve_actor(session: AsyncSession, *, actor_id: uuid.UUID | None = None) -> uuid.UUID:
    """确定写入人（`created_by` 的 FK 指向 `staff_accounts`）。

    显式给了就校验存在；没给就取一个 `admin`（FR-KNW-02：知识只允许 admin 写）。
    都找不到时抛 `SeedError` 并指路 `scripts/create_account.py` —— 不伪造一个
    `uuid(int=0)` 去撞外键。
    """
    if actor_id is not None:
        exists = await session.scalar(
            select(StaffAccount.staff_id).where(StaffAccount.staff_id == actor_id).limit(1)
        )
        if exists is None:
            raise SeedError(f"staff {actor_id} not found; pass an existing --actor-id")
        return actor_id

    resolved = await session.scalar(
        select(StaffAccount.staff_id)
        .where(StaffAccount.role == "admin")
        .order_by(StaffAccount.staff_id)
        .limit(1)
    )
    if resolved is None:
        raise SeedError(
            "no admin staff account found; create one with scripts/create_account.py "
            "or pass --actor-id"
        )
    return resolved


async def seed_documents(
    session: AsyncSession,
    documents: Sequence[SeedDocument],
    *,
    actor_id: uuid.UUID,
    embedding_provider: EmbeddingProvider | None = None,
) -> SeedSummary:
    """把解析好的种子逐份入库（幂等，只 `flush`；commit 归调用方）。

    判重键是 `(title, version)`：已有同标题同版本 → 进 `skipped`，不报错、不改旧行。
    并发下若两个进程同时插同一篇，`ingest_document` 抛的 `KnowledgeConflictError`
    也归到 `skipped`（唯一键保证只有一个赢家）。
    """
    provider = embedding_provider if embedding_provider is not None else build_embedding_provider()
    provider_name = type(provider).__name__
    semantic = provider_name != "DeterministicEmbeddingProvider"

    created: list[str] = []
    skipped: list[str] = []
    chunks = 0

    for document in documents:
        label = f"{document.title} v{document.version}"
        existing = await session.scalar(
            select(KnowledgeDocument.document_id)
            .where(
                KnowledgeDocument.title == document.title,
                KnowledgeDocument.version == document.version,
            )
            .limit(1)
        )
        if existing is not None:
            skipped.append(label)
            logger.info(
                "knowledge_seed_skipped",
                extra={"title": document.title, "version": document.version},
            )
            continue

        try:
            row = await knowledge_service.ingest_document(
                session,
                title=document.title,
                body=document.body,
                category=document.category,
                authority_level=document.authority_level,
                version=document.version,
                effective_from=document.effective_from,
                actor_id=actor_id,
                embedding_provider=provider,
                detail={"source": "knowledge_seed", "file": document.source.name},
            )
        except knowledge_service.KnowledgeConflictError:
            skipped.append(label)
            logger.info(
                "knowledge_seed_skipped_on_conflict",
                extra={"title": document.title, "version": document.version},
            )
            continue

        created.append(label)
        chunks += len(knowledge_service.split_into_chunks(document.body))
        logger.info(
            "knowledge_seed_created",
            extra={
                "document_id": str(row.document_id),
                "title": document.title,
                "version": document.version,
                "chunks": len(knowledge_service.split_into_chunks(document.body)),
            },
        )

    return SeedSummary(
        created=tuple(created),
        skipped=tuple(skipped),
        chunks=chunks,
        provider=provider_name,
        semantic=semantic,
    )


# ------------------------------------------------------------------ front-matter 解析


def _split_front_matter(text: str, path: Path) -> tuple[dict[str, str], str]:
    """切出 `--- ... ---` 头部与正文；没有分隔线就报错（不把整篇当正文吞掉）。"""
    stripped = text.lstrip("\ufeff").replace("\r\n", "\n")
    if not stripped.startswith("---\n"):
        raise SeedError(f"{path.name}: missing front-matter (expected leading '---')")
    end = stripped.find("\n---\n", 3)
    if end == -1:
        raise SeedError(f"{path.name}: front-matter is not closed by a '---' line")

    front: dict[str, str] = {}
    for raw_line in stripped[3:end].split("\n"):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise SeedError(f"{path.name}: front-matter line is not 'key: value' -- {line!r}")
        front[key.strip()] = value.strip().strip('"').strip("'")
    return front, stripped[end + len("\n---\n") :]


def _clean_required(front: dict[str, str], key: str, path: Path) -> str:
    value = front.get(key, "").strip()
    if not value:
        raise SeedError(f"{path.name}: front-matter key {key!r} is missing or empty")
    return value


def _normalise_category(value: str, path: Path) -> str:
    resolved = CATEGORY_ALIASES.get(value.strip())
    if resolved is None:
        raise SeedError(
            f"{path.name}: unknown front-matter category {value!r}; "
            f"known: {', '.join(sorted(CATEGORY_ALIASES))}"
        )
    return resolved


def _normalise_authority(value: str, path: Path) -> str:
    resolved = AUTHORITY_ALIASES.get(value.strip())
    if resolved is None:
        raise SeedError(
            f"{path.name}: unknown front-matter authority_level {value!r}; "
            f"known: {', '.join(sorted(AUTHORITY_ALIASES))}"
        )
    return resolved


def _parse_version(value: str, path: Path) -> int:
    try:
        version = int(value.strip())
    except ValueError as exc:
        raise SeedError(f"{path.name}: version must be an integer, got {value!r}") from exc
    if version <= 0:
        raise SeedError(f"{path.name}: version must be positive, got {version}")
    return version


def _parse_date(value: str, path: Path) -> datetime.datetime:
    """`YYYY-MM-DD` → 该日 00:00（上海时区）的 UTC 时刻；非当日零点会让「含当日」失效。"""
    try:
        day = datetime.date.fromisoformat(value.strip())
    except ValueError as exc:
        raise SeedError(f"{path.name}: effective_from must be YYYY-MM-DD, got {value!r}") from exc
    return datetime.datetime.combine(day, datetime.time(0, 0), tzinfo=_LOCAL_TIMEZONE).astimezone(
        datetime.UTC
    )


__all__ = [
    "AUTHORITY_ALIASES",
    "CATEGORY_ALIASES",
    "DEFAULT_SEED_DIR",
    "SeedDocument",
    "SeedError",
    "SeedSummary",
    "load_seed_documents",
    "parse_seed_document",
    "resolve_actor",
    "seed_documents",
]
