"""记忆来源权重（讲义 §8.3）：**明确陈述**必须比**行为推断**重。

## 这条闸门防的是什么

「他说他腿疼」和「他今天只走了两百步」如果等权写进记忆，模型下次就会拿后者当长者
自己说过的话来复述——**推断被洗成陈述**，而且一旦落库就再也分不出原始出处。所以
`remember(..., source=...)` 按来源给 `importance` 打折：`explicit` 1.0 / `inferred` 0.6。

## 三层断言，各自堵一个洞

1. **等值冻结**（纯函数）：系数就是 1.0 / 0.6，且默认来源是 `explicit`——把 0.6 改成 0.9
   或者把默认值改成 `inferred`（等于让全仓历史写入集体降权）都要红；
2. **行为层**（真库）：同一段内容、同一 base，`inferred` 的落库值严格小于 `explicit`；
   兜底 provider 不可用时也照写，权重折算与向量是否拿到无关；
3. **边界与拒绝**：乘积夹在 [0,1]、两位小数；来源非法 / base 越界都 `ValueError` 且
   **一行都不落库**（静默夹掉会让调用方以为写进去了）。

真库用例一律用 `db_conn` 夹具（每用例一个事务、结束回滚），不建独立测试库，口径见
`tests/conftest.py`。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

import app.services.memory_service as memory_service_module
from app.db import engine as app_engine
from app.models import Elder, MemoryChunk
from app.models.memory import EMBEDDING_DIM
from app.services.embedding import DeterministicEmbeddingProvider
from app.services.memory_service import (
    DEFAULT_MEMORY_SOURCE,
    MEMORY_SOURCE_COEFFICIENTS,
    remember,
)

# ============================================================
# 夹具与小工具
# ============================================================


@pytest.fixture(autouse=True)
async def _release_pool() -> AsyncIterator[None]:
    """每个用例结束把 `app.db.engine` 池里的连接还掉（仓库已登记的 P-03 坑）。

    `pytest-asyncio` 每用例一个新事件循环，而 `app.db.engine` 是模块级连接池：池里留着
    上一个循环里建的 asyncpg 连接，下一个用例取到它就会报「Event loop is closed」。
    `db_conn` 夹具用的是这个共享引擎，所以必须在用例结束时清空池（`tests/test_memory_service.py`
    的 `_release_pool` 是同一条口径，直接照抄，不另造第二种做法）。
    """
    yield
    await app_engine.dispose()


def _session(conn: AsyncConnection) -> AsyncSession:
    """把夹具那条未提交的事务借给被测代码；残留由夹具回滚。"""
    return AsyncSession(bind=conn, expire_on_commit=False)


async def _seed_elder(conn: AsyncConnection) -> uuid.UUID:
    """`memory_chunks.elder_id` 的外键目标；占位姓名，不写真实长者数据（AGENTS §4.3）。"""
    elder_id = uuid.uuid4()
    await conn.execute(Elder.__table__.insert().values(elder_id=elder_id, name="测试长者"))
    return elder_id


async def _count_of(session: AsyncSession, elder_id: uuid.UUID) -> int:
    """该长者落了几行（含 superseded / deleted）：用来证明「拒绝了就一行都没写」。"""
    stmt = select(func.count()).select_from(MemoryChunk).where(MemoryChunk.elder_id == elder_id)
    return int((await session.execute(stmt)).scalar_one())


class _NoVectorProvider:
    """`available=False` 的兜底 provider。

    为什么要它：同一段内容两次写入时，确定性 provider 会给出同一个向量，第二条会把第一条
    supersede 掉（`SUPERSEDE_SIMILARITY` 的既有行为）。本文件只关心 `importance`，所以用
    「拿不到向量」这条路径绕开冲突判定——`remember` 在 provider 不可用时照写不误。
    """

    available = False
    dimension = EMBEDDING_DIM

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise AssertionError("available=False 时不该被调用")


# ============================================================
# A. 等值冻结：系数与默认来源
# ============================================================


def test_source_coefficients_are_frozen() -> None:
    """系数就是讲义 §8.3 的数字：0.6 改成 0.9、或默认值改成 inferred 都要红。"""
    assert MEMORY_SOURCE_COEFFICIENTS == {"explicit": 1.0, "inferred": 0.6}
    assert DEFAULT_MEMORY_SOURCE == "explicit"


# ============================================================
# B. 行为层：同一段内容，推断必须更轻
# ============================================================


async def test_inferred_weighs_less_than_explicit_for_the_same_content(
    db_conn: AsyncConnection,
) -> None:
    """同一段内容、同一 base：`inferred` 的落库权重严格小于 `explicit`。"""
    elder_id = await _seed_elder(db_conn)
    session = _session(db_conn)
    provider = _NoVectorProvider()

    stated = await remember(
        session,
        elder_id=elder_id,
        content="最近膝盖疼，晚上不敢多走路",
        embedding_provider=provider,  # type: ignore[arg-type]
        importance=0.8,
        source="explicit",
    )
    guessed = await remember(
        session,
        elder_id=elder_id,
        content="最近膝盖疼，晚上不敢多走路",
        embedding_provider=provider,  # type: ignore[arg-type]
        importance=0.8,
        source="inferred",
    )

    assert float(stated.importance) == pytest.approx(0.80)
    assert float(guessed.importance) == pytest.approx(0.48)  # 0.80 × 0.6
    assert float(stated.importance) > float(guessed.importance)
    # 正文一个字都不许被权重改写：折算只动 importance
    assert stated.content == guessed.content


async def test_default_source_keeps_legacy_writes_unchanged(db_conn: AsyncConnection) -> None:
    """不传 `source` 的老调用方落库值不变（默认 explicit × 默认 importance=1.0）。"""
    elder_id = await _seed_elder(db_conn)
    session = _session(db_conn)

    row = await remember(
        session,
        elder_id=elder_id,
        content="女儿每周六上午来",
        embedding_provider=DeterministicEmbeddingProvider(),
    )

    assert float(row.importance) == pytest.approx(1.0)


async def test_explicit_weight_does_not_depend_on_embedding(db_conn: AsyncConnection) -> None:
    """权重折算与「向量拿到没」无关：provider 不可用时 inferred 仍然是 0.6 倍。"""
    elder_id = await _seed_elder(db_conn)
    session = _session(db_conn)

    row = await remember(
        session,
        elder_id=elder_id,
        content="今天午睡超过了两个小时",
        embedding_provider=_NoVectorProvider(),  # type: ignore[arg-type]
        importance=0.5,
        source="inferred",
    )

    assert row.embedding is None
    assert float(row.importance) == pytest.approx(0.30)


# ============================================================
# C. 边界：夹回 [0,1]，两位小数
# ============================================================


def test_helper_clamps_the_product_into_zero_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """乘积越界要夹回边界，而不是让 `CheckConstraint` 在 flush 时才炸。

    真实系数都 ≤1、base 又被 `_normalise_importance` 限在 [0,1]，乘积本来就出不了界；
    所以这里临时把系数改成 3.0 / -1.0，直接盯 `min/max` 那一行是不是活的
    （删掉 clamp 的变异会让本用例红）。
    """
    apply = memory_service_module._apply_source_weight

    assert apply(1.0, "explicit") == Decimal("1.00")
    assert apply(0.0, "inferred") == Decimal("0.00")

    monkeypatch.setitem(MEMORY_SOURCE_COEFFICIENTS, "inferred", 3.0)
    assert apply(0.9, "inferred") == Decimal("1.00")

    monkeypatch.setitem(MEMORY_SOURCE_COEFFICIENTS, "inferred", -1.0)
    assert apply(0.9, "inferred") == Decimal("0.00")


async def test_rounded_weight_is_representable_in_numeric_3_2(db_conn: AsyncConnection) -> None:
    """折算结果先定死两位小数再落库：`0.99 × 0.6 = 0.594` → 0.59。"""
    elder_id = await _seed_elder(db_conn)
    session = _session(db_conn)

    row = await remember(
        session,
        elder_id=elder_id,
        content="喜欢在下午听越剧",
        embedding_provider=_NoVectorProvider(),  # type: ignore[arg-type]
        importance=0.99,
        source="inferred",
    )

    value = Decimal(row.importance)
    assert value == Decimal("0.59")
    assert value.as_tuple().exponent == -2
    assert Decimal("0.00") <= value <= Decimal("1.00")


# ============================================================
# D. 拒绝：来源非法 / base 越界都不许静默写进去
# ============================================================


async def test_unknown_source_is_rejected_without_writing(db_conn: AsyncConnection) -> None:
    elder_id = await _seed_elder(db_conn)
    session = _session(db_conn)

    with pytest.raises(ValueError, match="source must be one of"):
        await remember(
            session,
            elder_id=elder_id,
            content="系统猜他心情不好",
            embedding_provider=DeterministicEmbeddingProvider(),
            source="guessed",
        )

    assert await _count_of(session, elder_id) == 0


async def test_out_of_range_base_is_rejected_not_clamped(db_conn: AsyncConnection) -> None:
    """base 越界（1.5）仍旧 `ValueError`：clamp 只夹**乘积**，不替调用方掩盖输入错误。"""
    elder_id = await _seed_elder(db_conn)
    session = _session(db_conn)

    with pytest.raises(ValueError, match="importance must be within"):
        await remember(
            session,
            elder_id=elder_id,
            content="权重越界的记忆",
            embedding_provider=DeterministicEmbeddingProvider(),
            importance=1.5,
            source="inferred",
        )

    assert await _count_of(session, elder_id) == 0
