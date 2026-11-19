"""Embedding 维度 1536 → 1024（0013）

背景：`.env` 配的是 `BAAI/bge-m3`（SiliconFlow），它的 dense 输出是 **1024** 维，而
`memory_chunks.embedding` / `knowledge_chunks.embedding` 是 `vector(1536)`。结果不是报错
中断，而是**静默降级**：`HttpEmbeddingProvider._parse` 判 `bad_dimension` →
`memory_service._embed_text` 接住 → `embedding=None` 照常写行。表现就是「记忆写进去了、
一条都召回不出来」，且日志里没有维度数字，查了很久。

为什么选 1024 而不是换一个 1536 维的模型：bge-m3 是本项目已配、已验过连通的模型；
「定死维度」是 2026-11-11 的裁决（不做运行时维度标签、不做无维度 `vector`），理由见
`设计记录（embedding维度对齐）`：「为什么不搞弹性」一节。

## 两条硬约束

1. **不用 `USING embedding::vector(1024)`**。pgvector 的跨维度 cast 是**截断**
   （1536 → 1024 直接砍掉后 512 维），不报错、不可逆、肉眼不可见——迁移里写这一句
   等于把「静默毁数据」固化进 schema 历史。
2. **先清空 `knowledge_chunks` 再改列宽**。库里的 9 行是 `knowledge_seed` 灌的种子，
   向量是 `DeterministicEmbeddingProvider` 用 SHA-256 派生的噪声（S-D7-1 明说不能当
   召回证据），本来就没有语义、也无法从 1536 换算到 1024。删掉后由种子脚本按新维度重灌
   （`app/services/knowledge_seed.py`）；`knowledge_documents` 不动，文档元数据保住。

`memory_chunks` 实测 0 行（本迁移当天），所以只改列宽。若某个开发库有真实 1536 维行，
本迁移会在 `ALTER` 处**大声失败**而不是截断——这是有意的 fail closed：那种行的向量
必须由新 provider 重算，迁移不敢替你做这个决定。

## 复查要点

1. 只动两张表的 `embedding` 一列 + 两处表注释 + 一处列注释，不碰其他表，不改历史迁移
   （AGENTS §4 禁区 6）；
2. 注释与 ORM `comment=` **逐字一致**（`alembic check` 把表/列注释也算漂移）；
3. downgrade 同样先清 `knowledge_chunks`：1024 维向量无法还原成 1536 维，回滚丢向量是
   必然的，与其假装能保住不如写明。

Revision ID: 0013_embedding_dim_1024
Revises: 0012_safety_alert
Create Date: 2026-11-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0013_embedding_dim_1024"
down_revision = "0012_safety_alert"
branch_labels = None
depends_on = None

_OLD_DIM = 1536
_NEW_DIM = 1024

_MEMORY_TABLE_COMMENT = "长期记忆切片（pgvector 1024 维，跨长者强隔离）"
_KNOWLEDGE_TABLE_COMMENT = "机构知识分块（pgvector 1024 维；先过滤生效版本，再在候选集内 cosine）"
_KNOWLEDGE_COL_COMMENT = "向量表示（1024 维，与 memory_chunks 同一提供方）；没有向量就不入库"

_OLD_MEMORY_TABLE_COMMENT = "长期记忆切片（pgvector 1536 维，跨长者强隔离）"
_OLD_KNOWLEDGE_TABLE_COMMENT = (
    "机构知识分块（pgvector 1536 维；先过滤生效版本，再在候选集内 cosine）"
)
_OLD_KNOWLEDGE_COL_COMMENT = "向量表示（1536 维，与 memory_chunks 同一提供方）；没有向量就不入库"


def upgrade() -> None:
    # 1. 清空知识分块：旧向量是确定性噪声，且无法从 1536 维换算到 1024 维。
    #    种子由 `app/services/knowledge_seed.py` 按新维度重灌，文档元数据不动。
    op.execute("DELETE FROM knowledge_chunks")

    # 2. 改列宽。不带 USING：由 provider 重算，绝不截断。
    op.execute(f"ALTER TABLE memory_chunks ALTER COLUMN embedding TYPE vector({_NEW_DIM})")
    op.execute(f"ALTER TABLE knowledge_chunks ALTER COLUMN embedding TYPE vector({_NEW_DIM})")

    # 3. 注释与 ORM 对齐（否则 `alembic check` 报 modify_comment 漂移）。
    op.execute(f"COMMENT ON TABLE memory_chunks IS '{_MEMORY_TABLE_COMMENT}'")
    op.execute(f"COMMENT ON TABLE knowledge_chunks IS '{_KNOWLEDGE_TABLE_COMMENT}'")
    op.execute(
        f"COMMENT ON COLUMN knowledge_chunks.embedding IS '{_KNOWLEDGE_COL_COMMENT}'"
    )
    _fail_if_dimension_wrong(_NEW_DIM)


def downgrade() -> None:
    # 1024 → 1536 同样无法换算；回滚丢向量是必然的，先清空让 ALTER 有定义。
    op.execute("DELETE FROM knowledge_chunks")

    op.execute(f"ALTER TABLE memory_chunks ALTER COLUMN embedding TYPE vector({_OLD_DIM})")
    op.execute(f"ALTER TABLE knowledge_chunks ALTER COLUMN embedding TYPE vector({_OLD_DIM})")

    op.execute(f"COMMENT ON TABLE memory_chunks IS '{_OLD_MEMORY_TABLE_COMMENT}'")
    op.execute(f"COMMENT ON TABLE knowledge_chunks IS '{_OLD_KNOWLEDGE_TABLE_COMMENT}'")
    op.execute(
        f"COMMENT ON COLUMN knowledge_chunks.embedding IS '{_OLD_KNOWLEDGE_COL_COMMENT}'"
    )
    _fail_if_dimension_wrong(_OLD_DIM)


def _fail_if_dimension_wrong(expected: int) -> None:
    """迁移末尾自检：两列 atttypmod 必须是 expected，错则整条迁移回滚。

    `ALTER ... TYPE vector(N)` 在列上没装 pgvector 的 typmod 时不会报错，只是默默
    变成一个无维度的 `vector`——那会让后续所有 `<=>` 比较在运行时炸，且报错信息完全
    指不到这里。所以宁可在这里失败。
    """
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            """
            SELECT c.relname, a.atttypmod
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            WHERE c.relname IN ('memory_chunks', 'knowledge_chunks')
              AND a.attname = 'embedding'
            """
        )
    ).all()
    expected_rows = 2
    if len(rows) != expected_rows:  # pragma: no cover - 表不存在时 alembic 早已失败
        raise RuntimeError(f"expected {expected_rows} embedding columns, found {len(rows)}")
    for table_name, typmod in rows:
        # pgvector 把维度直接存在 atttypmod 里（不加 VARHDRSZ 偏移），
        # 与 `tests/test_memory_medication_models.py::test_pgvector_extension_and_dimension` 同一口径。
        actual = -1 if typmod is None else int(typmod)
        if actual != expected:
            raise RuntimeError(
                f"{table_name}.embedding dimension is {actual}, expected {expected}"
            )


__all__: Sequence[str] = ["downgrade", "upgrade"]
