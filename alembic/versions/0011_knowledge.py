"""迭代 10 · 机构知识两表 `knowledge_documents` / `knowledge_chunks`（0011）

新建机构知识两表。列定义逐字对齐技术设计文档 §3.5（v1.11）、ER §4.4.8/§4.4.9
（v0.4），CHECK 取值对齐 ER §5.3。

本迁移由人工逐行写入（未用 autogenerate——autogenerate 会把注释、约束名、索引名
按当前库的差异重排，反而是漂移来源），复查要点：

1. **只新增两张表 + 三个索引**，不改任何既有表、不动历史迁移（AGENTS §4 禁区 6）；
2. 主键 uuid4 + PG `gen_random_uuid()`，同 0006/0007/0008/0009/0010；
3. `knowledge_chunks.embedding` 用 pgvector `VECTOR(1536)` 且 **NOT NULL**——与
   `memory_chunks.embedding` 的可空口径有意不同：记忆允许无向量降级留原文，
   知识分块没有正文可退，没有向量就等于永远召不回，宁可在写入时失败；
4. 约束名与 `app/models/knowledge.py` 经 `app/models/base.py` 的 naming_convention
   渲染结果逐字一致（`ck_knowledge_documents_category` 等）；两个唯一约束写**全名**
   （`uq` 模板不含 `%(constraint_name)s`，短名会落成 `title_version` 之类的裸名）；
5. 引 `staff_accounts.staff_id`（该表主键不是 `id`），**不设 ondelete**：账号停用
   不等于文档作废；`knowledge_chunks.document_id` 则 `ON DELETE CASCADE`；
6. **不建 ivfflat/HNSW 向量索引**（技术文档 §3.6：万行级走精确检索，避免过早优化）；
   检索必须「先过滤生效版本，再在候选集内 cosine」，所以本迁移只建过滤用索引；
7. 列注释与 ORM `comment=` **逐字一致**：`alembic check` 会把注释也算进漂移，
   两边措辞不同就会报一串 `modify_comment`；
8. downgrade 逆序 drop 索引与表（先 chunks 后 documents，避免外键依赖），可回滚。

Revision ID: 0011_knowledge
Revises: 0010_conversation_events
Create Date: 2026-11-10
"""

from __future__ import annotations

from collections.abc import Sequence

import pgvector
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0011_knowledge"
down_revision: str | None = "0010_conversation_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "knowledge_documents",
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            comment="文档编号（uuid4 由 PG gen_random_uuid() 承担，同 0006/0007/0008/0009）",
        ),
        sa.Column(
            "title",
            sa.String(length=200),
            nullable=False,
            comment="标题（如「午餐时间」「探视规定」）；同一标题的版本号唯一",
        ),
        sa.Column(
            "category",
            sa.String(length=32),
            nullable=False,
            comment="类别：schedule/meal/activity/policy/facility/service（CHECK）",
        ),
        sa.Column(
            "authority_level",
            sa.String(length=32),
            nullable=False,
            comment=(
                "权威级别：institution（机构规定）/ elder_memory（长者自述经管理员转录）；"
                "检索排序 institution 优先（FR-KNW-02）"
            ),
        ),
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            comment="版本号，同一标题内递增；UNIQUE(title, version)",
        ),
        sa.Column(
            "effective_from",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="生效起点（UTC 存储）",
        ),
        sa.Column(
            "effective_until",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="生效终点（NULL = 长期有效；非空时必须晚于 effective_from）",
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            comment="draft / active / retired（CHECK）；仅 active 且在生效区间内的版本可被检索",
        ),
        sa.Column(
            "body",
            sa.Text(),
            nullable=False,
            comment="正文；进上下文时按「参考资料」包裹，其中指令性文本不执行（FR-KNW-04）",
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="录入人（仅 admin 可写，FR-KNW-02；写操作同时落 audit_logs）",
        ),
        sa.Column(
            "updated_by",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="最后修改人（仅 admin 可写，FR-KNW-02）",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="创建时刻（UTC 存储）",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="最后修改时刻（UTC 存储；ORM onupdate 维护，不建触发器）",
        ),
        sa.CheckConstraint(
            "category IN ('schedule', 'meal', 'activity', 'policy', 'facility', 'service')",
            name=op.f("ck_knowledge_documents_category"),
        ),
        sa.CheckConstraint(
            "authority_level IN ('institution', 'elder_memory')",
            name=op.f("ck_knowledge_documents_authority_level"),
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'retired')",
            name=op.f("ck_knowledge_documents_status"),
        ),
        sa.CheckConstraint(
            "effective_until IS NULL OR effective_until > effective_from",
            name=op.f("ck_knowledge_documents_effective_range"),
        ),
        sa.CheckConstraint("version > 0", name=op.f("ck_knowledge_documents_version_positive")),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["staff_accounts.staff_id"],
            name=op.f("fk_knowledge_documents_created_by_staff_accounts"),
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["staff_accounts.staff_id"],
            name=op.f("fk_knowledge_documents_updated_by_staff_accounts"),
        ),
        sa.PrimaryKeyConstraint("document_id", name=op.f("pk_knowledge_documents")),
        sa.UniqueConstraint(
            "title",
            "version",
            name=op.f("uq_knowledge_documents_title_version"),
        ),
        comment="机构知识文档（文档级版本 + 生效区间 + 权威级别；平台侧配置，不参与长者记忆衰减）",
    )
    op.create_index(
        "ix_knowledge_documents_status_category",
        "knowledge_documents",
        ["status", "category"],
        unique=False,
    )
    op.create_index(
        "ix_knowledge_documents_effective_from_effective_until",
        "knowledge_documents",
        ["effective_from", "effective_until"],
        unique=False,
    )
    op.create_table(
        "knowledge_chunks",
        sa.Column(
            "chunk_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            comment="分块编号",
        ),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="所属文档（随文档级联删除：退役文档的分块不留孤儿）",
        ),
        sa.Column(
            "chunk_index",
            sa.Integer(),
            nullable=False,
            comment="文档内序号，从 0 起；UNIQUE(document_id, chunk_index)",
        ),
        sa.Column(
            "content",
            sa.Text(),
            nullable=False,
            comment="分块正文（命中后只把这一块放进上下文，不整篇塞入）",
        ),
        sa.Column(
            "embedding",
            pgvector.sqlalchemy.vector.VECTOR(dim=1536),
            nullable=False,
            comment="向量表示（1536 维，与 memory_chunks 同一提供方）；没有向量就不入库",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="写入时刻（UTC 存储）",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["knowledge_documents.document_id"],
            name=op.f("fk_knowledge_chunks_document_id_knowledge_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("chunk_id", name=op.f("pk_knowledge_chunks")),
        sa.UniqueConstraint(
            "document_id",
            "chunk_index",
            name=op.f("uq_knowledge_chunks_document_id_chunk_index"),
        ),
        comment="机构知识分块（pgvector 1536 维；先过滤生效版本，再在候选集内 cosine）",
    )
    op.create_index(
        "ix_knowledge_chunks_document_id",
        "knowledge_chunks",
        ["document_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_chunks_document_id", table_name="knowledge_chunks")
    op.drop_table("knowledge_chunks")
    op.drop_index(
        "ix_knowledge_documents_effective_from_effective_until",
        table_name="knowledge_documents",
    )
    op.drop_index("ix_knowledge_documents_status_category", table_name="knowledge_documents")
    op.drop_table("knowledge_documents")
