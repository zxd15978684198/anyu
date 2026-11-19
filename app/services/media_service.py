"""娱乐点播领域服务：素材检索 + 点播历史（PRD FR-CON-03/05/06；迭代 7 计划 §4 W5）。

## 这个模块只做两件事

1. **检索**：从 `media_library` 按 `type`（opera / movie）+ 可选 `genre` 取一条素材；
   「换一段」= 拿该长者最近点播记录做排除集再取（:func:`switch_next`）。
2. **留痕**：一次播放会话写一行 `media_history`（:func:`play` / :func:`finish`）；
   被打断的会话写低完播率 `completion_rate=0.0`，作为**偏好降权信号**
   （FR-CON-06「播放时长/完播率作为下次推荐依据」）——是信号，不是惩罚：
   频繁「换一段」只说明这段不合口味，下次推荐排后，不弹提示、不评价长者。

## 素材红线（PRD FR-CON-03/05、技术设计文档 §1「不做什么」第 4/6 条）

`media_library` 只收**样例授权**或**公有领域**的戏曲/老电影样例（`copyright_status`
是强制列，见 TD §3.5）；本模块**不抓取在线资源、不做 AI 生成、不做 TTS 代唱**，
`media_url` 一律由 nginx 静态托管、浏览器直连（PRD §2.1 媒体分发）。库里没有素材时
:func:`pick` 返回 `None`，调用方按 FR-CON-04 提示「没找到这段」——**不伪造曲目**，
也不允许本服务内联任何 URL 常量兜底。

授权判定采用 **fail closed 白名单**（:data:`AUTHORIZED_COPYRIGHT_STATUSES` =
``public_domain`` / ``licensed_sample``）：白名单外的任何取值（含 ``unknown_source``、
空串、脏数据）一律视为未授权——:func:`pick` 检索阶段就过滤掉，:func:`play` 再拦一道
（防绕过检索的直调）、抛 :class:`MediaCopyrightError`。素材红线宁可「这段放不了」，
也不能「先放了再说」。

老电影属 **P2**（PRD §1.6：迭代排期验收项的「老电影」被收敛为可选项，FR-CON-05）；
`kind="movie"` 的检索路径先留着，是否有素材取决于 `media_library` 录入
（D7-Q1 待裁决：素材来源与版权）。戏曲是 P0 闭环，本模块的测试只用戏曲数据。

## 播放控制不在本模块（迭代 6 已冻结）

「换一段 / 停 / 暂停 / 继续 / 大声点 / 小声点 / 越剧 / 京剧」由 L0 规则层
（`app/router/rules.py`）直路由、≤200ms、**不进 LLM**（FR-CON-03 / AC-37）。
本模块只提供纯函数 :func:`control_intent_from_rule` 把规则层的动作级标识翻成点播
动作串，自身不查库、不执行播放、不新增第二种控制形态（AGENTS §4.8）。

> 2026-11-12（W6）：补 `pause` / `resume` 两条。此前规则层已经产出这两个 intent
> （`app/router/rules.py:105/112`），但本表没有映射 → :func:`control_intent_from_rule`
> 返回 `None` → 编排层不下发、pad 收不到，链路在中间断掉（同时 `rule_replies.py`
> 还回着「这台只能整个关掉、不能暂停」的假话）。pause/resume 是**播放器本地动作**
> （pad 记 `currentTime`、resume 时 seek 回去），不经 HTTP、不挑素材、不动
> `media_history`——暂停不是一次收尾，继续也不新建历史。

## 接口约定（与 迭代 6 规则层的唯一接缝）

迭代 6 的 :class:`app.router.decision.RouteDecision` 有两个字段，容易混淆，这里明确：

| 迭代 6 代码 | 取值 | 本模块怎么用 |
| --- | --- | --- |
| ``decision.rule_id`` | 规则**分组**名：``play_control`` / ``volume_control`` / ``opera`` … | **不接收**：一个分组有 2 个分支，映射到动作不唯一 |
| ``decision.intent`` | 动作级标识：``play_next`` / ``stop`` / ``pause`` / ``resume`` / ``volume_up`` / ``volume_down`` / ``play_yueju`` / ``play_jingju`` | 传给 :func:`control_intent_from_rule` |

即：调用方应当传 ``decision.intent``。本函数的入参按任务书命名为 ``rule_id``（保持
对外签名稳定），但**语义是 迭代 6 的动作级 intent**；分组名与未知值一律返回 `None`，
由调用方回落到 L1（System One），不在这里猜。
"""

from __future__ import annotations

import datetime
import logging
import uuid
from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Final, cast

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.context import get_trace_id
from app.models.media import MediaHistory, MediaLibraryItem

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ 常量

#: `media_library.type` 的合法取值（TD §3.5 / ER §4.4.1）。movie 属 P2（PRD §1.6）。
MEDIA_KINDS: Final[tuple[str, ...]] = ("opera", "movie")

#: 授权状态白名单（`media_library.copyright_status` 的两种文档取值）。
#: **fail closed**：白名单外一律视为未授权 —— 素材红线宁可「这段放不了」，
#: 也不能「先放了再说」（PRD FR-CON-03/05、TD §1「不做什么」第 4/6 条）。
AUTHORIZED_COPYRIGHT_STATUSES: Final[tuple[str, ...]] = ("public_domain", "licensed_sample")

#: 默认点播类型：戏曲是 P0 闭环，老电影是 P2 可选项。
DEFAULT_KIND: Final[str] = "opera"

#: 「换一段」的排除窗口：最近 N 条点播记录不再返回（避免连着重复同一段）。
RECENT_HISTORY_LIMIT: Final[int] = 5

#: 被打断（「换一段」切走）的完播率：0.0 是**偏好降权信号**，不是错误码。
#: 用 Decimal 而不是 float：`completion_rate` 是 numeric(4,3)，asyncpg 的 numeric
#: 编码要 Decimal，交给驱动隐式转换会在不同驱动/版本上表现不一致。
INTERRUPTED_COMPLETION_RATE: Final[Decimal] = Decimal("0.000")

#: 规则层动作级标识 → 点播动作串（契约见模块 docstring 的表）。纯映射，不查库。
CONTROL_INTENTS: Final[dict[str, str]] = {
    "play_next": "next",
    "stop": "stop",
    # 暂停/继续是**播放器本地动作**：pad 记 currentTime，resume 时 seek 回去；
    # 不经过 HTTP、不挑素材、不动 `media_history`（暂停不算收尾，继续不新建历史）。
    "pause": "pause",
    "resume": "resume",
    "volume_up": "volume_up",
    "volume_down": "volume_down",
    "play_yueju": "opera:yueju",
    "play_jingju": "opera:jingju",
}


# ------------------------------------------------------------------ 错误类型


class MediaServiceError(Exception):
    """点播领域错误基类：只表达「这次请求被拒」，不构造 HTTP 响应。"""


class MediaNotFoundError(MediaServiceError):
    """素材或播放会话不存在（API 层映射 404）；**不静默造一条历史行**。"""


class MediaCopyrightError(MediaServiceError):
    """素材未授权（PRD FR-CON-03/05 红线）：拒绝播放，不「先放了再说」。"""


class MediaConflictError(MediaServiceError):
    """素材维护冲突（API 层映射 409）：`media_url` 撞唯一键，或素材已被点播过。

    两种冲突都必须拒绝而不是「顺手覆盖」：前者会让同一地址出现两条互相矛盾的
    素材，后者会经外键 CASCADE 把 `media_history` 里的播放记录一起抹掉。
    """


# ------------------------------------------------------------------ 内部工具


def _utc_now() -> datetime.datetime:
    """本服务所有时间戳一律 UTC 存储（AGENTS §8「存储 UTC，展示转 Asia/Shanghai」）。"""
    return datetime.datetime.now(tz=datetime.UTC)


def _as_utc(value: datetime.datetime | None) -> datetime.datetime:
    """归一化到 UTC：`None` → 现在；naive → 按 UTC 解释（不猜本地时区）。"""
    if value is None:
        return _utc_now()
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.UTC)
    return value.astimezone(datetime.UTC)


def _as_completion_rate(value: object) -> Decimal:
    """校验完播率并转 Decimal：只接受 0..1 的实数，越界或非数抛 `ValueError`。"""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"completion_rate 必须是 0..1 的实数，收到 {value!r}")
    rate = Decimal(str(value))
    if rate != rate:  # NaN：与自身不相等；先挡掉，避免下面的比较抛 InvalidOperation
        raise ValueError("completion_rate 不能是 NaN")
    if rate < Decimal(0) or rate > Decimal(1):
        raise ValueError(f"completion_rate 必须在 0..1 之间，收到 {value!r}")
    return rate


def _stored_rate(value: Decimal) -> float:
    """把校验后的 `Decimal` 完播率交给 ORM 属性。

    `media_history.completion_rate` 列是 `Numeric(4, 3)`，Python 侧规范类型就是
    `Decimal`（读写都是它，测试也按 `Decimal` 断言）；ORM 却把它标注成
    `Mapped[float | None]`——这是列类型与注解不一致的既有事实，模型层不在本次
    写作用域内。`cast` 只把静态类型对齐到列的真实取值类型：运行期仍写 `Decimal`，
    与改动前的行为逐位相同（改成 `float` 会改写入类型，那才是行为变化）。
    """
    return cast(float, value)


def _check_kind(kind: str) -> str:
    """`kind` 必须是 TD 的 `media_library.type` 闭集；未知值直接 ValueError。

    不返回 `None` 假装「库里没这段」：型别写错是调用方 bug，混进「无素材」分支
    会让 FR-CON-04 的提示说谎，也会掩盖上一层的拼写错误。
    """
    if kind not in MEDIA_KINDS:
        raise ValueError(f"未知点播类型 {kind!r}，允许：{', '.join(MEDIA_KINDS)}")
    return kind


def _log(event: str, **fields: object) -> None:
    """结构化日志：每行都带 trace_id（AGENTS §8）；只放 ID，不放标题/媒体地址。"""
    logger.info(event, extra={"trace_id": get_trace_id() or "-", **fields})


# ------------------------------------------------------------------ 检索


async def pick(
    session: AsyncSession,
    *,
    kind: str = DEFAULT_KIND,
    genre: str | None = None,
    exclude_media_ids: Iterable[uuid.UUID] = (),
) -> MediaLibraryItem | None:
    """按类型 / 剧种取一条素材；排除集给「换一段」用，空库返回 `None`。

    - `kind`：`media_library.type`（`opera` / `movie`，闭集）；未知值 `ValueError`。
    - `genre`：可选的剧种过滤（「越剧」来自 `decision.intent=play_yueju`）；
      空白串按「不过滤」处理（ASR 侧可能吐出空串，不该把库滤空）。
    - `exclude_media_ids`：不再返回的素材 ID（「换一段」= 最近点播过的几段）；
      空集不生成 SQL 条件（`NOT IN ()` 在部分方言下语义不一致）。
    - **只返回授权素材**：`copyright_status` 必须在
      :data:`AUTHORIZED_COPYRIGHT_STATUSES` 白名单内（fail closed）。
      未授权素材连检索都进不去，从源头堵住「先放了再说」。
    - **确定顺序**：`ORDER BY media_id`。不按播放量/偏好排序，是为了让本层可测、
      可复现；偏好排序属推荐策略（FR-CON-06 的信号已写进 `media_history`），
      不在这里偷偷实现第二套规则。
    - 空库 / 全被排除 → `None`。**不伪造素材**（FR-CON-04：未找到就如实提示）。
    """
    _check_kind(kind)
    conditions = [
        MediaLibraryItem.type == kind,
        MediaLibraryItem.copyright_status.in_(AUTHORIZED_COPYRIGHT_STATUSES),
    ]
    if genre is not None and genre.strip():
        conditions.append(MediaLibraryItem.genre == genre.strip())
    excluded = list(exclude_media_ids)
    if excluded:
        conditions.append(MediaLibraryItem.media_id.notin_(excluded))

    item = (
        await session.execute(
            select(MediaLibraryItem).where(*conditions).order_by(MediaLibraryItem.media_id).limit(1)
        )
    ).scalar_one_or_none()
    _log(
        "media.pick",
        kind=kind,
        genre=genre or None,
        excluded=len(excluded),
        hit=item is not None,
    )
    return item


# ------------------------------------------------------------------ 写库：播放会话


async def play(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    media_id: uuid.UUID,
    now: datetime.datetime | None = None,
) -> MediaHistory:
    """开一次播放会话：写一行 `media_history`，`finished_at` 留空表示「正在播」。

    一行 = 一次播放会话（FR-CON-06 的「播放两段戏曲至不同进度后可见时长与完播率」）。
    `now` 缺省取当前 UTC 时间；naive 时间按 UTC 解释。

    素材必须先存在：`media_id` 不在 `media_library` 时抛 :class:`MediaNotFoundError`，
    不靠外键报错——那会把「素材不存在」伪装成 500，也让调用方无法按 FR-CON-04 提示。
    素材还必须是**授权素材**：`copyright_status` 不在白名单时抛
    :class:`MediaCopyrightError`（:func:`pick` 已经过滤过一遍，这里是第二道闸，
    挡住绕过检索直接 `play` 的调用方）。
    本函数**不**顺手关掉上一条未结束会话：「停/暂停」是播放器语义，由调用方决定是否
    :func:`finish`；只有「换一段」的打断由 :func:`switch_next` 自己记账。
    """
    media = await session.get(MediaLibraryItem, media_id)
    if media is None:
        _log("media.play", elder_id=str(elder_id), media_id=str(media_id), result="not_found")
        raise MediaNotFoundError(f"media_library 没有这条素材：{media_id}")
    if media.copyright_status not in AUTHORIZED_COPYRIGHT_STATUSES:
        _log(
            "media.play",
            elder_id=str(elder_id),
            media_id=str(media_id),
            result="copyright_forbidden",
            copyright_status=media.copyright_status,
        )
        raise MediaCopyrightError(
            f"素材 {media_id} 的授权状态 {media.copyright_status!r} 不在白名单内"
            f"（允许：{', '.join(AUTHORIZED_COPYRIGHT_STATUSES)}）"
        )

    history = MediaHistory(
        elder_id=elder_id,
        media_id=media_id,
        started_at=_as_utc(now),
    )
    session.add(history)
    await session.flush()
    await session.refresh(history)
    _log(
        "media.play",
        elder_id=str(elder_id),
        media_id=str(media_id),
        history_id=str(history.history_id),
        result="success",
    )
    return history


async def finish(
    session: AsyncSession,
    *,
    history_id: uuid.UUID,
    finished_at: datetime.datetime | None = None,
    completion_rate: float | Decimal | None = None,
) -> MediaHistory:
    """收尾一次播放会话：写 `finished_at`（缺省 now）与可选完播率。

    - `completion_rate` 合法区间 **0..1**（含端点），越界或非数抛 `ValueError`
      （TD §3.5 是 numeric(4,3)，不会接受负数或 1.5）；
    - 传 `None` 表示「没测完播率」，保留 NULL，不写 0——**没测到 ≠ 没看完**，
      把两者混在一起会污染 FR-CON-06 的偏好信号；
    - `finished_at` 幂等：已结束的会话保留**第一次**的结束时间（同一会话只结束一次），
      完播率允许后续纠正。

    会话不存在抛 :class:`MediaNotFoundError`。
    """
    history = await session.get(MediaHistory, history_id)
    if history is None:
        _log("media.finish", history_id=str(history_id), result="not_found")
        raise MediaNotFoundError(f"media_history 没有这条播放记录：{history_id}")

    if completion_rate is not None:
        history.completion_rate = _stored_rate(_as_completion_rate(completion_rate))
    if history.finished_at is None:
        history.finished_at = _as_utc(finished_at)
    await session.flush()
    await session.refresh(history)
    _log(
        "media.finish",
        history_id=str(history_id),
        elder_id=str(history.elder_id),
        completion_rate=str(history.completion_rate),
        result="success",
    )
    return history


# ------------------------------------------------------------------ 素材维护
#
# FR-ADM-02 要求管理员能维护「本地素材」（素材入库/改错/下架）。这里只做**写**：
# 长者端点播走 pick/play/finish，素材维护是照护台的动作，两者共用 `media_library`。

#: 维护时允许改的列。`media_id` 是代理主键，永不改；`media_url` 可改，但它是素材的
#: 业务身份（DB 上有 `uq_media_library_media_url`），改了就等于换了一条素材的内容源。
LIBRARY_MUTABLE_FIELDS: Final[tuple[str, ...]] = (
    "type",
    "title",
    "genre",
    "era",
    "artist",
    "media_url",
    "duration",
    "copyright_status",
)


def _library_text(value: object, *, field: str) -> str:
    """必填文本列：去首尾空白后不得为空（空标题会在播报里念成一串空白）。"""
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是字符串，收到 {type(value).__name__}")
    text = value.strip()
    if not text:
        raise ValueError(f"{field} 不能为空")
    return text


def _library_optional_text(value: object, *, field: str) -> str | None:
    """可选文本列：`None` 与空白串都归一成 NULL（维护界面清空字段 = 恢复「未填」）。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是字符串或 null，收到 {type(value).__name__}")
    return value.strip() or None


def _library_duration(value: object) -> int | None:
    """时长（秒）：正整数或 NULL。0/负数会被 DB 的 `duration_positive` CHECK 拒绝，
    这里提前给出人话原因；未知时长请传 `None`，不要填 0 假装知道。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"duration 必须是整数或 null，收到 {value!r}")
    if value <= 0:
        raise ValueError(f"duration 必须为正整数（未知时长传 null），收到 {value}")
    return value


def _library_copyright(value: object) -> str:
    """授权状态：**fail closed**，白名单外一律拒绝写入。

    `media_library.copyright_status` 在 DB 侧没有 CHECK（只有 `AUTHORIZED_COPYRIGHT_STATUSES`
    这个服务层常量），所以这里是唯一一道闸；绕过它等于把未授权素材放进点播池。
    """
    if not isinstance(value, str) or value not in AUTHORIZED_COPYRIGHT_STATUSES:
        raise MediaCopyrightError(
            f"copyright_status={value!r} 不在授权白名单内"
            f"（允许：{', '.join(AUTHORIZED_COPYRIGHT_STATUSES)}）"
        )
    return value


async def _load_library_item(session: AsyncSession, media_id: uuid.UUID) -> MediaLibraryItem:
    item = await session.get(MediaLibraryItem, media_id)
    if item is None:
        _log("media.library.load", media_id=str(media_id), result="not_found")
        raise MediaNotFoundError(f"media_library 没有这条素材：{media_id}")
    return item


async def _media_url_taken(
    session: AsyncSession, media_url: str, *, exclude: uuid.UUID | None = None
) -> bool:
    """`media_url` 是否已被别的素材占用（`exclude` 排除自己，供 PATCH 用）。"""
    stmt = select(MediaLibraryItem.media_id).where(MediaLibraryItem.media_url == media_url)
    if exclude is not None:
        stmt = stmt.where(MediaLibraryItem.media_id != exclude)
    return await session.scalar(stmt.limit(1)) is not None


async def create_library_item(
    session: AsyncSession,
    *,
    kind: str,
    title: str,
    media_url: str,
    copyright_status: str,
    genre: str | None = None,
    era: str | None = None,
    artist: str | None = None,
    duration: int | None = None,
) -> MediaLibraryItem:
    """新增一条素材（管理员）。校验顺序：类型闭集 → 授权白名单 → 文本/时长 → 唯一键。

    先校验再查库：参数本身不合法时不该先打一次数据库，也不该把「拼错 kind」
    和「地址已存在」混成同一个 409。
    """
    _check_kind(kind)
    item = MediaLibraryItem(
        type=kind,
        title=_library_text(title, field="title"),
        media_url=_library_text(media_url, field="media_url"),
        copyright_status=_library_copyright(copyright_status),
        genre=_library_optional_text(genre, field="genre"),
        era=_library_optional_text(era, field="era"),
        artist=_library_optional_text(artist, field="artist"),
        duration=_library_duration(duration),
    )
    if await _media_url_taken(session, item.media_url):
        _log("media.library.create", media_url=item.media_url, result="duplicate")
        raise MediaConflictError(f"media_url 已被其它素材占用：{item.media_url}")

    try:
        # SAVEPOINT：唯一键在并发下仍可能被抢先，让这次失败只回滚这一条插入。
        async with session.begin_nested():
            session.add(item)
    except IntegrityError as exc:
        _log("media.library.create", media_url=item.media_url, result="duplicate")
        raise MediaConflictError(f"media_url 已被其它素材占用：{item.media_url}") from exc

    await session.refresh(item)
    _log(
        "media.library.create",
        media_id=str(item.media_id),
        kind=item.type,
        result="success",
    )
    return item


async def update_library_item(
    session: AsyncSession,
    *,
    media_id: uuid.UUID,
    changes: Mapping[str, object],
) -> MediaLibraryItem:
    """按字段补丁修改一条素材；`changes` 里没出现的列一律不动。

    **为什么收 `Mapping` 而不是一串 `xxx=None` 形参**：`None` 在这里有两种含义——
    「不动这一列」（没传）和「清空这一列」（显式传 null）。HTTP 层用 Pydantic 的
    `exclude_unset` 已经区分好了，服务层再压成同名关键字参数就会把两种语义压成一种，
    `genre` 从此清不空。调用方传 `{"genre": None}` 就是清空。

    校验全部通过后才落列：任何一项不合法都不允许让前几项先生效。
    """
    item = await _load_library_item(session, media_id)
    if not changes:
        raise ValueError("没有任何要修改的字段")
    unknown = sorted(set(changes) - set(LIBRARY_MUTABLE_FIELDS))
    if unknown:
        raise ValueError(
            f"不可修改的字段：{', '.join(unknown)}；允许：{', '.join(LIBRARY_MUTABLE_FIELDS)}"
        )

    prepared: dict[str, object] = {}
    for name, raw in changes.items():
        if name == "type":
            if not isinstance(raw, str):
                raise ValueError(f"type 必须是字符串，收到 {type(raw).__name__}")
            prepared[name] = _check_kind(raw)
        elif name == "copyright_status":
            prepared[name] = _library_copyright(raw)
        elif name == "duration":
            prepared[name] = _library_duration(raw)
        elif name in ("genre", "era", "artist"):
            prepared[name] = _library_optional_text(raw, field=name)
        else:  # title / media_url
            prepared[name] = _library_text(raw, field=name)

    new_url = prepared.get("media_url")
    if isinstance(new_url, str) and await _media_url_taken(session, new_url, exclude=media_id):
        _log("media.library.update", media_id=str(media_id), result="duplicate")
        raise MediaConflictError(f"media_url 已被其它素材占用：{new_url}")

    before_copyright = item.copyright_status
    for name, value in prepared.items():
        setattr(item, name, value)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError as exc:
        _log("media.library.update", media_id=str(media_id), result="duplicate")
        raise MediaConflictError(f"media_url 已被其它素材占用：{item.media_url}") from exc

    await session.refresh(item)
    _log(
        "media.library.update",
        media_id=str(item.media_id),
        fields=",".join(sorted(prepared)),
        copyright_before=before_copyright,
        copyright_after=item.copyright_status,
        result="success",
    )
    return item


async def delete_library_item(session: AsyncSession, *, media_id: uuid.UUID) -> MediaLibraryItem:
    """下架并删除一条素材；**已被点播过就拒绝**（409），不做静默级联。

    `media_history.media_id` 是 `ON DELETE CASCADE`：直接删会把这位长者的播放记录
    一起抹掉，日报与偏好信号凭空少几天数据且无法追溯。要真正下架，正确做法是
    改 `copyright_status`（PATCH）让它从点播池里掉出去，历史留着。
    """
    item = await _load_library_item(session, media_id)
    played = await session.scalar(
        select(func.count()).select_from(MediaHistory).where(MediaHistory.media_id == media_id)
    )
    if played:
        _log(
            "media.library.delete",
            media_id=str(media_id),
            history_rows=int(played),
            result="has_history",
        )
        raise MediaConflictError(
            f"该素材已有 {int(played)} 条播放记录，删除会连带清掉播放历史；"
            "请改用 PATCH 撤下授权（copyright_status）"
        )

    await session.delete(item)
    await session.flush()
    _log("media.library.delete", media_id=str(media_id), result="success")
    return item


#: 管理员素材列表的默认页大小与硬上限。上限 200 与 `app/api/media.py` 的
#: `LIBRARY_LIMIT` 同一口径：素材库再大，一次翻页也不该把整库拉进进程。
DEFAULT_LIBRARY_PAGE_SIZE: Final[int] = 50
MAX_LIBRARY_PAGE_SIZE: Final[int] = 200


async def list_library_items(
    session: AsyncSession,
    *,
    kind: str | None = None,
    genre: str | None = None,
    limit: int = DEFAULT_LIBRARY_PAGE_SIZE,
    offset: int = 0,
) -> tuple[list[MediaLibraryItem], int]:
    """管理员视角列素材（FR-ADM-02）：返回 ``(当页行, 过滤后总数)``。

    **不按授权白名单过滤**——这一点与长者端 `pick` 正好相反，也是本函数存在的理由：
    维护界面上要能看见「这条为什么播不了」，否则一条被撤授权的素材在列表里凭空
    消失，运营只会以为是没保存成功。

    过滤只有 `type` / `genre` 两个等值条件（模糊搜索等库里真有 200 条以上再谈）。
    排序固定 `type, title, media_id`：`media_id` 兜底保证同一标题的多条之间也有
    全序，翻页不会跳行或重复。

    `limit` 越界（`<=0` 或 `> MAX_LIBRARY_PAGE_SIZE`）、`offset < 0`、`kind` 未知一律
    `ValueError`——这三个都是客户端参数，不能让「给个巨大 limit」静默变成「返回全库」。
    只读：不写库、不写审计（读列表不产生副作用，写审计会把库冲成噪音）。
    """
    if limit <= 0 or limit > MAX_LIBRARY_PAGE_SIZE:
        raise ValueError(f"limit 必须在 1..{MAX_LIBRARY_PAGE_SIZE} 之间：{limit}")
    if offset < 0:
        raise ValueError(f"offset 不能为负数：{offset}")
    if kind is not None:
        _check_kind(kind)

    filters: list[ColumnElement[bool]] = []
    if kind is not None:
        filters.append(MediaLibraryItem.type == kind)
    if genre is not None:
        filters.append(MediaLibraryItem.genre == genre)

    total_statement = select(func.count()).select_from(MediaLibraryItem)
    rows_statement = select(MediaLibraryItem)
    for condition in filters:
        total_statement = total_statement.where(condition)
        rows_statement = rows_statement.where(condition)

    total = int(await session.scalar(total_statement) or 0)
    rows = (
        await session.scalars(
            rows_statement.order_by(
                MediaLibraryItem.type,
                MediaLibraryItem.title,
                MediaLibraryItem.media_id,
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    _log(
        "media.library.list",
        kind=kind,
        genre=genre,
        limit=limit,
        offset=offset,
        total=total,
        returned=len(rows),
    )
    return list(rows), total


async def _mark_interrupted(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    now: datetime.datetime | None = None,
) -> list[MediaHistory]:
    """把该长者所有未结束的播放会话按「被打断」收尾，完播率写 0.0。

    **为什么是 0.0**：长者说「换一段」时当前这段没看完就切走了，这既不等于听完
    （1.0），也不等于「没测到」（NULL）。完播率是 FR-CON-06 的**偏好降权信号**，
    0.0 让下次推荐把这段排后；它不是错误码、不触发任何告警、不对长者评价。

    收尾的是**全部** `finished_at IS NULL` 的行而不是仅最新一条：同一个长者只有
    一台平板、一路播放，正常情况下最多一条未结束；多出来的只可能是进程崩溃遗留。
    一次性收干净，才能守住「同一长者同时只有一条未结束会话」的不变量。
    """
    rows = list(
        (
            await session.execute(
                select(MediaHistory).where(
                    MediaHistory.elder_id == elder_id,
                    MediaHistory.finished_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    stamp = _as_utc(now)
    for row in rows:
        row.finished_at = stamp
        row.completion_rate = _stored_rate(INTERRUPTED_COMPLETION_RATE)
    if rows:
        await session.flush()
        _log(
            "media.interrupt",
            elder_id=str(elder_id),
            count=len(rows),
            media_ids=[str(row.media_id) for row in rows],
        )
    return rows


async def recent_media_ids(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    limit: int = RECENT_HISTORY_LIMIT,
) -> list[uuid.UUID]:
    """该长者最近点播过的素材 ID（新的在前），给「不许重复挑同一段」用。

    只读、不写库。`limit <= 0` 返回空列表（调用方表达「不排除」不必自己判空集）。
    排序 `started_at DESC, history_id DESC`：同一时刻的并列用 ID 兜底，用例可复现。
    """
    if limit <= 0:
        return []
    return list(
        (
            await session.execute(
                select(MediaHistory.media_id)
                .where(MediaHistory.elder_id == elder_id)
                .order_by(MediaHistory.started_at.desc(), MediaHistory.history_id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


async def pick_for_elder(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    kind: str = DEFAULT_KIND,
    genre: str | None = None,
    recent_limit: int = RECENT_HISTORY_LIMIT,
) -> MediaLibraryItem | None:
    """`pick` + 「排除这位长者最近听过的几段」。

    为什么端点要用这个而不是直接 `pick`：`pick` 按 `media_id` 升序取第一条，
    库里只有三段时，长者每次点「听一段戏」听到的都是同一段，而他刚说过「换一段」。
    排除最近 N 条的规则与 `switch_next` 完全一致（同一个 `recent_media_ids`），
    避免「首次点播」和「换一段」两套不同的去重口径。

    **不记账**：选段不等于开始播，`media_history` 只在 :func:`play` 里落。
    """
    recent_ids = await recent_media_ids(session, elder_id=elder_id, limit=recent_limit)
    return await pick(session, kind=kind, genre=genre, exclude_media_ids=recent_ids)


async def switch_next(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    kind: str = DEFAULT_KIND,
    genre: str | None = None,
    recent_limit: int = RECENT_HISTORY_LIMIT,
) -> MediaLibraryItem | None:
    """「换一段」：排除最近 `recent_limit` 条点播记录后再取一段，并记一次打断。

    两步（顺序即契约）：

    1. 取该长者最近 `recent_limit` 条 `media_history` 的 `media_id` 作排除集
       （`ORDER BY started_at DESC, history_id DESC`，同一时刻的并列用 ID 兜底，
       保证用例可复现）；
    2. 把**当前未结束的播放会话**按打断收尾：`completion_rate = 0.0`（偏好降权信号，
       见 :func:`_mark_interrupted`）。

    返回下一条素材；库空或全被排除时返回 `None`（调用方提示「没别的了」）。
    此时打断仍然记账——长者确实说了「换一段」，当前这段就是没听完；
    不因为「库里没下一段」就把这次行为从偏好信号里抹掉。

    `recent_limit=0` 表示不排除；负数 `ValueError`。`kind` 未知 `ValueError`。
    """
    if recent_limit < 0:
        raise ValueError(f"recent_limit 不能为负数：{recent_limit}")
    _check_kind(kind)

    recent_ids = await recent_media_ids(session, elder_id=elder_id, limit=recent_limit)

    await _mark_interrupted(session, elder_id=elder_id)
    item = await pick(session, kind=kind, genre=genre, exclude_media_ids=recent_ids)
    _log(
        "media.switch_next",
        elder_id=str(elder_id),
        kind=kind,
        recent=len(recent_ids),
        hit=item is not None,
    )
    return item


# ------------------------------------------------------------------ 规则层接缝（纯函数）


def control_intent_from_rule(rule_id: str) -> str | None:
    """把 迭代 6 规则层的动作级标识翻成点播动作串；未知值返回 `None`。

    八条映射（模块 docstring 的表是契约）：

    ================================  ==============  ==============================
    入参（迭代 6 ``decision.intent``）  返回动作串       调用方动作
    ================================  ==============  ==============================
    ``play_next``                     ``next``        ``media_service.switch_next()``
    ``stop``                          ``stop``        播放器停止当前音轨
    ``pause``                         ``pause``       播放器就地暂停（记 currentTime）
    ``resume``                        ``resume``      播放器 seek 回原位继续
    ``volume_up``                     ``volume_up``   播放器 +10% 音量
    ``volume_down``                   ``volume_down`` 播放器 −10% 音量
    ``play_yueju``                    ``opera:yueju`` ``pick(kind='opera', genre='越剧')``
    ``play_jingju``                   ``opera:jingju`` ``pick(kind='opera', genre='京剧')``
    ================================  ==============  ==============================

    命名口径：入参名沿用任务书的 ``rule_id``，但**语义是动作级标识**——在 迭代 6 的
    ``RouteDecision`` 里，这六个值放在 ``intent`` 字段，``rule_id`` 反而是规则分组名
    （``play_control`` / ``volume_control`` / ``opera``）。分组名有歧义（一组两个分支），
    因此这里**不接收**分组名，传给本函数的是 ``decision.intent``。

    纯函数：不查库、不读配置、不判身份；`None` 由调用方回落到 L1/L2，本层不猜
    （AGENTS §6「低层不得被高层覆盖」，规则层只做减法）。
    """
    return CONTROL_INTENTS.get(rule_id)


__all__ = [
    "AUTHORIZED_COPYRIGHT_STATUSES",
    "CONTROL_INTENTS",
    "DEFAULT_KIND",
    "DEFAULT_LIBRARY_PAGE_SIZE",
    "INTERRUPTED_COMPLETION_RATE",
    "LIBRARY_MUTABLE_FIELDS",
    "MAX_LIBRARY_PAGE_SIZE",
    "MEDIA_KINDS",
    "RECENT_HISTORY_LIMIT",
    "MediaConflictError",
    "MediaCopyrightError",
    "MediaNotFoundError",
    "MediaServiceError",
    "control_intent_from_rule",
    "create_library_item",
    "delete_library_item",
    "finish",
    "list_library_items",
    "pick",
    "pick_for_elder",
    "play",
    "recent_media_ids",
    "switch_next",
    "update_library_item",
]
