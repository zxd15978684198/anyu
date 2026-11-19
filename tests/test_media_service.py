"""`media_service` 的点播检索与播放历史用例（迭代 7 计划 §4 W5）。

覆盖任务书七条验收：

1. `pick` 按 `kind` / `genre` 过滤；
2. 排除集生效（「换一段」不返回上一段）；
3. 空库返回 `None`（**不伪造素材**）；
4. `play` → `finish` 完播率写入；
5. `switch_next` 打断当前播放并写 `0.0`（偏好降权信号）；
6. `completion_rate` 越界抛 `ValueError`；
7. `control_intent_from_rule` 六映射 + 未知返回 `None`。

额外守住的边界（都是这条链上真会出事的点）：

- `play` 的素材不存在 → `MediaNotFoundError`，不靠外键报错把 404 伪装成 500；
- `finish(completion_rate=None)` 保留 NULL：**「没测到」≠「没看完」**，
  把两者混起来会污染 FR-CON-06 的偏好信号；
- `finish` 的结束时间幂等（同一会话只结束一次），完播率允许纠正。

## 素材红线（PRD FR-CON-03/05）

本文件的 media 行**只是测试数据**，用完随之回滚：

- `copyright_status` 一律写 `public_domain` / `licensed_sample`（样例授权或公有领域），
  另有一条**故意写 `unknown_source`** 的反向用例，证明未授权素材既不会被检索到、
  也播不了（`MediaCopyrightError`）；
- `media_url` 用 `https://example.invalid/...` 这种不可解析的占位地址——
  **不向仓库塞真实媒体文件、不建 seed SQL、不引在线资源**。素材由 迭代 7 的素材卡
  单独录入（D7-Q1 待裁决：来源与版权）。

## 数据库口径

与 `tests/test_persona_greeting.py` 同构：本文件自建 NullPool 引擎 + 每用例一事务、
结束回滚。**不复用 `app.db.engine`**：它按用例换事件循环时会把上一个循环的连接带进
新循环（取连接报 `'NoneType' object has no attribute 'send'`），是个已知坑。
连接失败按 `conftest.require_db_connection()` 的统一策略处置（默认 fail，不静默 skip）。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.models.media import MediaLibraryItem
from app.models.organization import Elder
from app.services.media_service import (
    INTERRUPTED_COMPLETION_RATE,
    MediaCopyrightError,
    MediaNotFoundError,
    control_intent_from_rule,
    finish,
    pick,
    play,
    switch_next,
)
from conftest import require_db_connection

#: 不用模块级 `pytest.mark.asyncio`：pyproject 已开 `asyncio_mode=auto`，
#: 再标一遍会把 `TestControlIntent` 里的同步用例也误标成协程（pytest 会告警）。

#: 测试用的固定时刻（UTC）：显式传时间，断言不受「现在」影响。
T0 = datetime.datetime(2026, 11, 8, 10, 0, tzinfo=datetime.UTC)
T1 = datetime.datetime(2026, 11, 8, 10, 3, tzinfo=datetime.UTC)
T2 = datetime.datetime(2026, 11, 8, 10, 6, tzinfo=datetime.UTC)


# ------------------------------------------------------------------ 夹具


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    """每用例现连现关的 NullPool 引擎（理由见模块 docstring 的数据库口径）。"""
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db_conn(db_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """每用例一个事务、结束回滚；连不上库按 conftest 的统一策略处置。"""
    conn = await require_db_connection(db_engine)

    trans = await conn.begin()
    try:
        yield conn
    finally:
        await trans.rollback()
        await conn.close()


@pytest.fixture
async def session(db_conn: AsyncConnection) -> AsyncIterator[AsyncSession]:
    """把夹具那条未提交事务借给被测代码；残留由夹具回滚。"""
    db_session = AsyncSession(bind=db_conn, expire_on_commit=False)
    try:
        yield db_session
    finally:
        await db_session.close()


# ------------------------------------------------------------------ 测试数据


def _label(prefix: str) -> str:
    """每个用例独有的一串标记：库里将来录入真实素材也不会和用例撞车。"""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def _make_elder(session: AsyncSession, *, name: str = "张桂芬") -> Elder:
    """一条演示长者（只提供姓名/状态；其余列都可空或服务端有默认值）。"""
    elder = Elder(name=name, status="active")
    session.add(elder)
    await session.flush()
    return elder


async def _make_media(
    session: AsyncSession,
    *,
    kind: str = "opera",
    genre: str | None = None,
    title: str = "梁山伯与祝英台·十八相送",
    copyright_status: str = "licensed_sample",
    duration: int | None = 1800,
) -> MediaLibraryItem:
    """一条**授权**测试素材；`media_url` 是不可解析占位地址（不引真实媒体文件）。"""
    item = MediaLibraryItem(
        type=kind,
        title=title,
        genre=genre,
        media_url=f"https://example.invalid/media/{uuid.uuid4().hex[:12]}.mp3",
        duration=duration,
        copyright_status=copyright_status,
    )
    session.add(item)
    await session.flush()
    return item


# ------------------------------------------------------------------ ①③ 检索


class TestPick:
    """按 type / genre 过滤；空库与未授权素材都返 `None`（不伪造）。"""

    async def test_filters_by_kind(self, session: AsyncSession) -> None:
        """`opera` 与 `movie` 各自只返回自己的类型（电影属 P2，检索路径先留着）。"""
        opera_genre = _label("越剧")
        movie_genre = _label("生活片")
        opera = await _make_media(session, kind="opera", genre=opera_genre)
        movie = await _make_media(
            session, kind="movie", genre=movie_genre, title="地道战（授权样例）"
        )

        assert (await pick(session, kind="opera", genre=opera_genre)).media_id == opera.media_id
        assert (await pick(session, kind="movie", genre=movie_genre)).media_id == movie.media_id

    async def test_filters_by_genre(self, session: AsyncSession) -> None:
        """剧种过滤是「越剧 / 京剧」两种点播的落点（`intent=play_yueju/play_jingju`）。"""
        yueju_genre = _label("越剧")
        jingju_genre = _label("京剧")
        yueju = await _make_media(
            session, kind="opera", genre=yueju_genre, title="红楼梦·天上掉下个林妹妹"
        )
        await _make_media(session, kind="opera", genre=jingju_genre, title="锁麟囊·春秋亭")

        picked = await pick(session, kind="opera", genre=yueju_genre)

        assert picked is not None
        assert picked.media_id == yueju.media_id
        assert picked.genre == yueju_genre

    async def test_blank_genre_means_no_genre_filter(self, session: AsyncSession) -> None:
        """ASR 可能吐空串：空白剧种按「不过滤」处理，不把库滤空。"""
        genre = _label("越剧")
        await _make_media(session, kind="opera", genre=genre)

        picked = await pick(session, kind="opera", genre="   ")

        assert picked is not None
        assert picked.type == "opera"

    async def test_unknown_genre_returns_none(self, session: AsyncSession) -> None:
        """库里没有这个剧种 → `None`，调用方按 FR-CON-04 提示，不编一段出来。"""
        await _make_media(session, kind="opera", genre=_label("越剧"))

        assert await pick(session, kind="opera", genre=_label("昆曲")) is None

    async def test_unknown_kind_raises(self, session: AsyncSession) -> None:
        """型别写错是调用方 bug，不能混进「无素材」分支让提示说谎。"""
        with pytest.raises(ValueError, match="未知点播类型"):
            await pick(session, kind="variety")

    async def test_empty_library_returns_none(self, session: AsyncSession) -> None:
        """空库返回 `None` —— **不伪造素材**（FR-CON-04）。

        用例先清空素材表：素材卡日后会往库里录 20–30 条真实素材（D7-Q1），
        这条用例不能靠「库恰好是空的」过关。删除在测试事务里做，回滚即还原。
        """
        await session.execute(delete(MediaLibraryItem).execution_options(synchronize_session=False))
        await session.flush()

        assert await pick(session, kind="opera") is None

    async def test_unlicensed_material_is_not_retrievable(self, session: AsyncSession) -> None:
        """素材红线：未授权素材不进检索结果（PRD FR-CON-03/05）。"""
        genre = _label("越剧")
        await _make_media(session, kind="opera", genre=genre, copyright_status="unknown_source")

        assert await pick(session, kind="opera", genre=genre) is None


class TestPickExclusion:
    """「换一段」的排除集：上一段不再返回；全被排除就 `None`。"""

    async def test_excludes_previous_item(self, session: AsyncSession) -> None:
        genre = _label("越剧")
        first_item = await _make_media(session, kind="opera", genre=genre, title="第一段")
        second_item = await _make_media(session, kind="opera", genre=genre, title="第二段")

        first = await pick(session, kind="opera", genre=genre)
        second = await pick(session, kind="opera", genre=genre, exclude_media_ids=[first.media_id])

        assert [first.media_id, second.media_id] == sorted(
            [first_item.media_id, second_item.media_id]
        )
        assert second.media_id != first.media_id

    async def test_excluding_everything_returns_none(self, session: AsyncSession) -> None:
        genre = _label("越剧")
        first_item = await _make_media(session, kind="opera", genre=genre)
        second_item = await _make_media(session, kind="opera", genre=genre)

        picked = await pick(
            session,
            kind="opera",
            genre=genre,
            exclude_media_ids=[first_item.media_id, second_item.media_id],
        )

        assert picked is None

    async def test_empty_exclusion_set_is_no_condition(self, session: AsyncSession) -> None:
        """空排除集不生成 `NOT IN ()` 条件（SQL 语义在方言间不一致，索性不写）。"""
        genre = _label("越剧")
        await _make_media(session, kind="opera", genre=genre)

        assert await pick(session, kind="opera", genre=genre, exclude_media_ids=()) is not None


# ------------------------------------------------------------------ ④⑥ 播放会话


class TestPlaybackHistory:
    """`play` 写会话、`finish` 写完播率；越界值直接 `ValueError`。"""

    async def test_play_then_finish_writes_completion_rate(self, session: AsyncSession) -> None:
        """一行 = 一次播放会话：开始时刻必填，结束时刻与完播率由 `finish` 回填。"""
        elder = await _make_elder(session)
        item = await _make_media(session)

        history = await play(session, elder_id=elder.elder_id, media_id=item.media_id, now=T0)

        assert history.media_id == item.media_id
        assert history.started_at == T0
        assert history.finished_at is None
        assert history.completion_rate is None

        done = await finish(
            session, history_id=history.history_id, finished_at=T1, completion_rate=1.0
        )

        assert done.history_id == history.history_id
        assert done.finished_at == T1
        assert done.completion_rate == Decimal("1.000")

    @pytest.mark.parametrize("rate", [0.0, 0.5, 1.0, Decimal("0.999")])
    async def test_completion_rate_accepts_zero_to_one(
        self, session: AsyncSession, rate: float | Decimal
    ) -> None:
        """0 与 1 都是合法端点：没看完是 0，听完是 1。"""
        elder = await _make_elder(session)
        item = await _make_media(session)
        history = await play(session, elder_id=elder.elder_id, media_id=item.media_id, now=T0)

        done = await finish(session, history_id=history.history_id, completion_rate=rate)

        assert done.completion_rate == Decimal(str(rate)).quantize(Decimal("0.001"))

    @pytest.mark.parametrize("rate", [1.5, -0.001, 2, Decimal("1.0001"), "0.5"])
    async def test_completion_rate_out_of_range_raises(
        self, session: AsyncSession, rate: Any
    ) -> None:
        """越界与非数值一律 `ValueError`（TD §3.5 是 numeric(4,3)，0..1 之外无意义）。"""
        elder = await _make_elder(session)
        item = await _make_media(session)
        history = await play(session, elder_id=elder.elder_id, media_id=item.media_id, now=T0)

        with pytest.raises(ValueError, match="completion_rate"):
            await finish(session, history_id=history.history_id, completion_rate=rate)

    async def test_finish_without_rate_keeps_null(self, session: AsyncSession) -> None:
        """没测完播率就留 NULL：**「没测到」≠「没看完」**，写 0 会污染偏好信号。"""
        elder = await _make_elder(session)
        item = await _make_media(session)
        history = await play(session, elder_id=elder.elder_id, media_id=item.media_id, now=T0)

        done = await finish(session, history_id=history.history_id, finished_at=T1)

        assert done.finished_at == T1
        assert done.completion_rate is None

    async def test_finish_is_idempotent_on_finished_at(self, session: AsyncSession) -> None:
        """同一会话只结束一次：再次 `finish` 不改首次结束时间，完播率允许纠正。"""
        elder = await _make_elder(session)
        item = await _make_media(session)
        history = await play(session, elder_id=elder.elder_id, media_id=item.media_id, now=T0)
        await finish(session, history_id=history.history_id, finished_at=T1, completion_rate=0.5)

        corrected = await finish(
            session, history_id=history.history_id, finished_at=T2, completion_rate=0.9
        )

        assert corrected.finished_at == T1
        assert corrected.completion_rate == Decimal("0.900")

    async def test_naive_now_is_interpreted_as_utc(self, session: AsyncSession) -> None:
        """naive 时间按 UTC 解释（AGENTS §8：存储一律 UTC，不猜本地时区）。"""
        elder = await _make_elder(session)
        item = await _make_media(session)

        history = await play(
            session,
            elder_id=elder.elder_id,
            media_id=item.media_id,
            now=datetime.datetime(2026, 11, 8, 10, 0),  # 故意传 naive：验证按 UTC 归一化
        )

        assert history.started_at == T0
        assert history.started_at.tzinfo is not None

    async def test_unknown_media_raises(self, session: AsyncSession) -> None:
        """素材不存在 → `MediaNotFoundError`（API 层映射 404，而不是外键 500）。"""
        elder = await _make_elder(session)

        with pytest.raises(MediaNotFoundError):
            await play(session, elder_id=elder.elder_id, media_id=uuid.uuid4())

    async def test_unlicensed_media_is_refused(self, session: AsyncSession) -> None:
        """素材红线：未授权素材直接拒绝播放，不「先放了再说」（PRD FR-CON-03/05）。"""
        elder = await _make_elder(session)
        item = await _make_media(session, copyright_status="unknown_source")

        with pytest.raises(MediaCopyrightError):
            await play(session, elder_id=elder.elder_id, media_id=item.media_id)

    async def test_unknown_history_raises(self, session: AsyncSession) -> None:
        with pytest.raises(MediaNotFoundError):
            await finish(session, history_id=uuid.uuid4(), completion_rate=1.0)


# ------------------------------------------------------------------ ⑤「换一段」


class TestSwitchNext:
    """`switch_next` = 排除最近几段 + 把未结束的会话按打断收尾（0.0 降权）。"""

    async def test_switches_to_another_item_and_marks_interrupt(
        self, session: AsyncSession
    ) -> None:
        genre = _label("越剧")
        elder = await _make_elder(session)
        first_item = await _make_media(session, kind="opera", genre=genre, title="第一段")
        second_item = await _make_media(session, kind="opera", genre=genre, title="第二段")
        history = await play(session, elder_id=elder.elder_id, media_id=first_item.media_id, now=T0)

        next_item = await switch_next(session, elder_id=elder.elder_id, kind="opera", genre=genre)

        assert next_item is not None
        assert next_item.media_id == second_item.media_id
        # 打断记账：当前会话已结束，完播率 0.0 是偏好降权信号，不是错误码。
        await session.refresh(history)
        assert history.finished_at is not None
        assert history.completion_rate == INTERRUPTED_COMPLETION_RATE == Decimal("0.000")

    async def test_excludes_every_recent_item(self, session: AsyncSession) -> None:
        """排除集来自本人历史：连续「换一段」不会在最近几段里打转。"""
        genre = _label("越剧")
        elder = await _make_elder(session)
        items = [
            await _make_media(session, kind="opera", genre=genre, title=f"第 {index} 段")
            for index in range(3)
        ]
        played = await pick(session, kind="opera", genre=genre)
        await play(session, elder_id=elder.elder_id, media_id=played.media_id, now=T0)

        next_item = await switch_next(session, elder_id=elder.elder_id, kind="opera", genre=genre)

        assert next_item is not None
        assert next_item.media_id != played.media_id
        assert next_item.media_id in {item.media_id for item in items}

    async def test_no_material_still_marks_interrupt(self, session: AsyncSession) -> None:
        """没有下一段时仍记账：长者确实说了「换一段」，这段就是没听完。"""
        genre = _label("越剧")
        elder = await _make_elder(session)
        first_item = await _make_media(session, kind="opera", genre=genre)
        second_item = await _make_media(session, kind="opera", genre=genre)
        first = await play(session, elder_id=elder.elder_id, media_id=first_item.media_id, now=T0)
        second = await play(session, elder_id=elder.elder_id, media_id=second_item.media_id, now=T1)

        next_item = await switch_next(session, elder_id=elder.elder_id, kind="opera", genre=genre)

        assert next_item is None
        for history in (first, second):
            await session.refresh(history)
            assert history.finished_at is not None
            assert history.completion_rate == Decimal("0.000")

    async def test_marks_all_open_sessions_of_the_elder(self, session: AsyncSession) -> None:
        """崩溃遗留的未结束会话一并收尾：同一长者同时只允许一条「正在播」。"""
        genre = _label("越剧")
        elder = await _make_elder(session)
        other_elder = await _make_elder(session, name="李秀兰")
        item = await _make_media(session, kind="opera", genre=genre)
        stale = await play(session, elder_id=elder.elder_id, media_id=item.media_id, now=T0)
        current = await play(session, elder_id=elder.elder_id, media_id=item.media_id, now=T1)
        untouched = await play(
            session, elder_id=other_elder.elder_id, media_id=item.media_id, now=T1
        )

        await switch_next(session, elder_id=elder.elder_id, kind="opera", genre=genre)

        for history in (stale, current):
            await session.refresh(history)
            assert history.completion_rate == Decimal("0.000")
        # 别人正在播的那条不受影响（跨长者隔离，模型层注释同口径）。
        await session.refresh(untouched)
        assert untouched.finished_at is None
        assert untouched.completion_rate is None

    async def test_recent_limit_zero_disables_exclusion(self, session: AsyncSession) -> None:
        """`recent_limit=0` = 不排除，回到 `pick` 的确定顺序（同一段可以再放）。

        断言不与「是哪一段」绑死：`ORDER BY media_id` 排的是随机 UUID，
        用例只证明「不带排除集时，switch_next 与 pick 的答案一致」。
        """
        genre = _label("越剧")
        elder = await _make_elder(session)
        first_item = await _make_media(session, kind="opera", genre=genre)
        await _make_media(session, kind="opera", genre=genre)
        await play(session, elder_id=elder.elder_id, media_id=first_item.media_id, now=T0)
        expected = await pick(session, kind="opera", genre=genre)

        next_item = await switch_next(
            session, elder_id=elder.elder_id, kind="opera", genre=genre, recent_limit=0
        )

        assert expected is not None
        assert next_item is not None
        assert next_item.media_id == expected.media_id

    async def test_negative_recent_limit_raises(self, session: AsyncSession) -> None:
        elder_id = uuid.uuid4()
        with pytest.raises(ValueError, match="recent_limit"):
            await switch_next(session, elder_id=elder_id, recent_limit=-1)

    async def test_unknown_kind_raises(self, session: AsyncSession) -> None:
        with pytest.raises(ValueError, match="未知点播类型"):
            await switch_next(session, elder_id=uuid.uuid4(), kind="variety")


# ------------------------------------------------------------------ ⑦ 规则层接缝（纯函数）


class TestControlIntent:
    """迭代 6 规则层 `decision.intent` → 点播动作串；纯函数不查库。"""

    @pytest.mark.parametrize(
        ("rule_id", "action"),
        [
            ("play_next", "next"),
            ("stop", "stop"),
            ("volume_up", "volume_up"),
            ("volume_down", "volume_down"),
            ("play_yueju", "opera:yueju"),
            ("play_jingju", "opera:jingju"),
        ],
    )
    def test_six_mappings(self, rule_id: str, action: str) -> None:
        assert control_intent_from_rule(rule_id) == action

    @pytest.mark.parametrize(
        "unknown",
        [
            "",
            "play_control",  # 迭代 6 的 rule_id 是「规则分组名」，一个分组两个分支 → 有歧义，不接收
            "volume_control",
            "opera",
            "PLAY_NEXT",
            " play_next ",
            "next",
            "restart",
        ],
    )
    def test_unknown_returns_none(self, unknown: str) -> None:
        """未知/分组名一律 `None`：调用方回落 L1/L2，本层不猜（低层只做减法）。"""
        assert control_intent_from_rule(unknown) is None


class TestMediaUrlUniqueness:
    """`media_library.media_url` 唯一（迁移 `0014_media_url_unique`）。

    为什么值得一条用例：`scripts/seed_media.py` 的幂等靠「先 SELECT 再决定 insert」，
    两个进程同时跑时**都会**通过那条 SELECT，只有数据库能拦住第二行。拦不住的症状
    不是报错，而是长者端面板里同一段戏出现两次、完播率分叉到两个 `media_id` 上。
    """

    async def test_same_media_url_is_rejected(self, session: AsyncSession) -> None:
        url = f"https://example.invalid/media/{uuid.uuid4().hex[:12]}.mp3"
        session.add(
            MediaLibraryItem(
                type="opera",
                title="第一行",
                media_url=url,
                copyright_status="public_domain",
            )
        )
        await session.flush()

        # 用 SAVEPOINT 断言：撞约束只回滚这一条，外层夹具事务仍可用来核对「只进了一行」。
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(
                    MediaLibraryItem(
                        type="opera",
                        title="第二行",
                        media_url=url,
                        copyright_status="public_domain",
                    )
                )

        rows = (
            (
                await session.execute(
                    select(MediaLibraryItem).where(MediaLibraryItem.media_url == url)
                )
            )
            .scalars()
            .all()
        )
        assert [row.title for row in rows] == ["第一行"]

    async def test_different_urls_are_allowed(self, session: AsyncSession) -> None:
        """约束只管「同址两行」，不同素材照常共存（别把幂等键写成全表唯一标题）。"""
        await _make_media(session, kind="opera", title="第一段")
        await _make_media(session, kind="opera", title="第二段")
        await session.flush()
