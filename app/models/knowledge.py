"""机构知识两表 ORM（`knowledge_documents` / `knowledge_chunks`，DAG N11）。

口径来源：DAG 节点 N11 任务卡 + 技术设计文档 §3.5（v1.11 新增）、ER §4.4.8/§4.4.9
（v0.4）、ER §5.3 的 CHECK 清单。与「长者记忆」是两套东西，别混：

1. **机构知识是平台侧配置**（作息/餐饮/活动/政策/设施/服务），由 admin 录入并维护；
   不参与长者记忆的 90 天衰减、归并、容量淘汰，退房清数据也不动本表（PRD §4.12 边界）。
2. **版本在文档粒度**：同一 `title` 用递增 `version` 表达修订，`UNIQUE(title, version)`
   保证同一版本只存一份；生效与否由 `status` + `effective_from`/`effective_until` 决定。
   DDL 只保证「区间合法」，**「怎么选出生效版本」是服务层口径**（`status='active'`
   且 `now()` 落在区间内），不写成 `WHERE` 常量或视图，避免把检索策略钉死在 schema 上。
3. **枚举一律 CHECK，不用 PG ENUM**（ER §5.3 / 技术文档裁决 C6）：`category` /
   `authority_level` / `status` 三处都是 `IN (...)`。加值只能改 CHECK（要迁移），
   但避免了 `ALTER TYPE` 在事务与回滚上的麻烦。
4. **`embedding` 非空**（与 `memory_chunks.embedding` 有意不同）：记忆允许无向量降级
   （供应商挂了也要留下原文），而知识分块没有正文可退——没有向量就等于永远召不回，
   所以宁可让写入失败，也不要入库一条「占位分块」。
5. **检索口径**：先按「生效版本 + category + 权限」过滤出候选集，再在候选集内做
   cosine 精确检索 top-k；不得先全库向量扫描再过滤（否则退役版本会混进答案，
   违反 FR-KNW-02）。因此本表**不建 ivfflat/HNSW 索引**（技术文档 §3.6：万行级
   演示数据走精确检索，避免过早优化）。
6. **正文进上下文时按「参考资料」包裹**，其中指令性文本不执行（FR-KNW-04，与
   FR-TC-07 同族防护）；本文件只落库，包裹话术在服务层。
7. **`created_by` / `updated_by` 引 `staff_accounts.staff_id`**（该表主键不是 `id`），
   不设 ondelete：账号停用不等于文档作废，物理删账号应先处置文档；写操作同时落
   `audit_logs`（FR-KNW-02，仅 admin 可写）。
8. `updated_at` 由 **ORM `onupdate`** 维护（不建触发器：AGENTS 与既有迁移都没用过
   触发器，跨行/跨表逻辑一律放服务层）。它只影响 UPDATE 语句，不参与 DDL，
   所以 `alembic check` 不会因此报漂移。
"""

from __future__ import annotations

import datetime
import uuid
from typing import Final

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

EMBEDDING_DIM = 1024
"""与 `memory_chunks.embedding` 同一 embedding 提供方（技术设计文档 §3.6）。

维度口径 2026-11-11 由 1536 改为 1024（bge-m3 dense），来源 `settings.embedding_dimension`。

这里**不复用** `app/models/memory.py` 的常量：两个模型文件之间不互相 import，
否则单独导入其中一个会连带拉起另一个（alembic autogenerate 与测试夹具都受影响）。
维度只在这两处 + 迁移里改，三处写的是同一数字。
"""

#: `knowledge_documents.category` 的合法取值（CHECK 的唯一出处）。
#:
#: 服务层（`app/services/knowledge_service.py`）**从这里 import，不另抄一份字面量**：
#: 两处字面量早晚会漂移 —— 模型加了值而服务层没加，用户按新类别检索会被服务层判成
#: 非法值；反过来服务层认了模型没有的值，SQL 会撞 CHECK 变成 500。所以「有哪些类别」
#: 只有这一个定义，CHECK 约束也由它拼出来（见下表参数）。
KNOWLEDGE_CATEGORIES: Final[tuple[str, ...]] = (
    "schedule",
    "meal",
    "activity",
    "policy",
    "facility",
    "service",
)

#: `authority_level`：机构规定优先于「长者自述经管理员转录」（FR-KNW-02）。
KNOWLEDGE_AUTHORITY_LEVELS: Final[tuple[str, ...]] = ("institution", "elder_memory")

#: `status`：只有 `active` 且在生效区间内的版本可被检索（`draft` 未发布、`retired` 已退役）。
KNOWLEDGE_STATUSES: Final[tuple[str, ...]] = ("draft", "active", "retired")


def _check_in(column: str, values: tuple[str, ...]) -> str:
    """把枚举常量拼成 `column IN ('a', 'b')`，供 `CheckConstraint` 使用。

    与迁移里的字面量**逐字同形**（单引号、逗号+空格、顺序一致），否则 `alembic check`
    的 DDL 指纹会与 0011 不一致。
    """
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


class KnowledgeDocument(Base):
    """一条机构知识文档；版本在文档粒度递增，检索只取「生效版本」。"""

    __tablename__ = "knowledge_documents"
    __table_args__ = (
        # 同一标题的同一版本只存一份。名字写全（不走 naming_convention 的 uq 模板）：
        # `uq` 模板不含 `%(constraint_name)s`，只传短名会原样落成 `title_version`，
        # 与库里的 `uq_knowledge_documents_title_version` 对不上，alembic check 会报漂移。
        UniqueConstraint(
            "title",
            "version",
            name="uq_knowledge_documents_title_version",
        ),
        CheckConstraint(
            _check_in("category", KNOWLEDGE_CATEGORIES),
            name="category",
        ),
        CheckConstraint(
            _check_in("authority_level", KNOWLEDGE_AUTHORITY_LEVELS),
            name="authority_level",
        ),
        CheckConstraint(_check_in("status", KNOWLEDGE_STATUSES), name="status"),
        CheckConstraint(
            "effective_until IS NULL OR effective_until > effective_from",
            name="effective_range",
        ),
        CheckConstraint("version > 0", name="version_positive"),
        # 检索前过滤：先按状态 + 类别圈候选集（技术设计文档 §3.6 索引清单）。
        Index("ix_knowledge_documents_status_category", "status", "category"),
        # 按生效区间过滤「此刻有效」的版本。
        Index(
            "ix_knowledge_documents_effective_from_effective_until",
            "effective_from",
            "effective_until",
        ),
        {
            "comment": "机构知识文档（文档级版本 + 生效区间 + 权威级别；平台侧配置，不参与长者记忆衰减）"
        },
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        comment="文档编号（uuid4 由 PG gen_random_uuid() 承担，同 0006/0007/0008/0009）",
    )
    title: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        comment="标题（如「午餐时间」「探视规定」）；同一标题的版本号唯一",
    )
    category: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="类别：schedule/meal/activity/policy/facility/service（CHECK）",
    )
    authority_level: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="权威级别：institution（机构规定）/ elder_memory（长者自述经管理员转录）；检索排序 institution 优先（FR-KNW-02）",
    )
    version: Mapped[int] = mapped_column(
        Integer(),
        nullable=False,
        comment="版本号，同一标题内递增；UNIQUE(title, version)",
    )
    effective_from: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="生效起点（UTC 存储）",
    )
    effective_until: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="生效终点（NULL = 长期有效；非空时必须晚于 effective_from）",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="draft / active / retired（CHECK）；仅 active 且在生效区间内的版本可被检索",
    )
    body: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="正文；进上下文时按「参考资料」包裹，其中指令性文本不执行（FR-KNW-04）",
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("staff_accounts.staff_id"),
        nullable=False,
        comment="录入人（仅 admin 可写，FR-KNW-02；写操作同时落 audit_logs）",
    )
    updated_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("staff_accounts.staff_id"),
        nullable=False,
        comment="最后修改人（仅 admin 可写，FR-KNW-02）",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        comment="创建时刻（UTC 存储）",
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
        comment="最后修改时刻（UTC 存储；ORM onupdate 维护，不建触发器）",
    )


class KnowledgeChunk(Base):
    """一条机构知识分块（pgvector 1024 维）；命中后只把这一块放进上下文。"""

    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        # 文档内序号唯一；冲突由服务层捕获后跳过或重排，不吐 500。
        UniqueConstraint(
            "document_id",
            "chunk_index",
            name="uq_knowledge_chunks_document_id_chunk_index",
        ),
        # 按文档取全部分块（重建上下文、换 embedding 模型时重算）。
        Index("ix_knowledge_chunks_document_id", "document_id"),
        {"comment": "机构知识分块（pgvector 1024 维；先过滤生效版本，再在候选集内 cosine）"},
    )

    chunk_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        comment="分块编号",
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_documents.document_id", ondelete="CASCADE"),
        nullable=False,
        comment="所属文档（随文档级联删除：退役文档的分块不留孤儿）",
    )
    chunk_index: Mapped[int] = mapped_column(
        Integer(),
        nullable=False,
        comment="文档内序号，从 0 起；UNIQUE(document_id, chunk_index)",
    )
    content: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="分块正文（命中后只把这一块放进上下文，不整篇塞入）",
    )
    embedding: Mapped[list[float]] = mapped_column(
        Vector(EMBEDDING_DIM),
        nullable=False,
        comment="向量表示（1024 维，与 memory_chunks 同一提供方）；没有向量就不入库",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        comment="写入时刻（UTC 存储）",
    )
