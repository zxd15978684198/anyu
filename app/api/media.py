"""戏曲 / 电影点播端点（FR-CON-03 / FR-CON-04，P0）。

## 为什么要有这个模块

`app/services/media_service.py` 在 迭代 7 就已写完并有用例覆盖（`tests/test_media_service.py`），
但全仓**没有任何 HTTP 入口**——`pick` / `play` / `finish` / `switch_next` 只有自己和测试在调用，
pad 上的长者点「越剧」时后端根本没有可打的地址。本模块就是把这块能力接出来的唯一一层：
**参数校验 → 身份注入 → 调服务层 → 序列化**，一条写语句都没有（写库全部在
`media_service` 里，`get_session` 在请求结束时统一提交）。

## 身份只从服务端来（AGENTS §4.9 红线）

五个端点一律 `Depends(current_device)`，`elder_id` 只从 `Identity.elder_id`（即
`devices.elder_id`）取。请求体里**没有** `elder_id` 字段：

- `play` / `switch-next` / `pick` 的请求体模型都设 `extra="forbid"`，客户端自带
  `elder_id` 会被 422 拒掉——比静默丢弃更早暴露「前端在自报身份」这个错误；
- `play` 端点干脆不解析请求体，body 里塞什么都进不了代码路径。

未绑定长者的设备（`Identity.elder_id is None`）不能点播：没有长者就没有 `media_history`
的归属，回 409 `device_unbound`，与 `POST /api/sos` 同一口径。

## 播放地址为什么必须归一化到 `/media/`

`media_library.media_url` 是素材录入时的地址（可能是 `https://…` 或裸相对路径），而前端
`<audio src>` 只能打同源静态路径：`frontend/nginx.conf:188` 的 `location /media/` 带
`try_files $uri =404`。**这条 `=404` 是安全阀**——MPA 的兜底 location 会把未命中路径喂回
`index.html`，那样 `<audio>` 会拿到一坨 HTML 当音频解码，页面表现成「点了没反应」且没有
任何报错。所以这里一律把地址折叠成 `/media/<相对路径>`，让缺素材稳定落成 404 而不是 HTML。

## 错误码（沿用契约既有 code，不新造）

| 场景 | HTTP | code |
| --- | --- | --- |
| 素材/会话不存在、没有下一段 | 404 | `NOT_FOUND` |
| 版权未授权（`MediaCopyrightError`） | 422 | `COPYRIGHT_UNAUTHORIZED` |
| 请求体/参数不合法 | 422 | `VALIDATION_ERROR` |
| 路径参数不是 UUID | 400 | `BAD_REQUEST` |
| 设备未绑定长者 | 409 | `device_unbound` |
| 素材地址撞唯一键 / 有播放历史不能删（`MediaConflictError`） | 409 | `CONFLICT` |
| 动别人的播放历史 | 403 | `SCOPE_FORBIDDEN` |
| 员工角色不是 admin | 403 | 全局处理器给（`FORBIDDEN` 等） |
| 非设备令牌（员工/家属/无令牌） | 401 | 全局处理器给（`TOKEN_INVALID` 等） |

## 素材维护与长者端点播是两条路

长者端五个端点一律 `Depends(current_device)`；`GET`/`POST`/`PATCH`/`DELETE
`/api/media/library…` 是 FR-ADM-02 的**管理员**动作，一律 `Depends(current_staff)` +
角色必须是 `admin`（拒绝时先写审计再 403，见 `_require_admin`）。两边共用
`media_library`，但长者端永远只读，且只看得到授权白名单内的素材——管理员能不能改库，
与「这台平板听什么」是两件事，不能因为加了写端点就把 `/library` 的过滤放开。

管理员的四个端点各自解决一件事：

| 端点 | 回答的问题 |
| --- | --- |
| `GET /api/media/library/items` | 「库里现在有什么、哪条不能播、它的 `media_id` 是多少」 |
| `POST /api/media/library` | 「把这段新录的素材登记进来」 |
| `PATCH /api/media/library/{media_id}` | 「改错字 / 补时长 / 撤下授权」 |
| `DELETE /api/media/library/{media_id}` | 「彻底删掉」（有播放历史则 409，见下） |

## 控制指令不在这里实现

「换一段 / 大声点 / 停」是规则层的活：`media_service.control_intent_from_rule()` 已把
规则 id 映射成 `next` / `stop` / `volume_up` / `volume_down`，`play_next → /switch-next`、
`stop → /finish`、音量两条留在 pad 本地。**规则层已经 10 条封顶，本模块一个规则都不加**，
也不把 media 做成第二个 MCP Tool（AGENTS §4.8）。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Coroutine
from datetime import datetime
from typing import Annotated, Any, Literal, cast
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request
from starlette.responses import Response

from app.api.elders import ContractAPIRoute, _error_response
from app.core.auth import RoleForbiddenError, current_device, current_staff
from app.core.runtime_context import Identity
from app.db import get_session
from app.models.media import MediaHistory, MediaLibraryItem
from app.services import audit_service, media_service
from app.services.audit_service import NIL_RESOURCE_ID
from app.services.media_service import (
    AUTHORIZED_COPYRIGHT_STATUSES,
    DEFAULT_KIND,
    DEFAULT_LIBRARY_PAGE_SIZE,
    MAX_LIBRARY_PAGE_SIZE,
    MediaConflictError,
    MediaCopyrightError,
    MediaNotFoundError,
)

#: 点播类型。与 `media_library.type` 的 CHECK 约束、`MEDIA_KINDS` 同一口径；
#: 写成 Literal 让 FastAPI 在参数阶段就把 `kind=news` 判成 422，不用服务层抛 ValueError。
MediaKind = Literal["opera", "movie"]

#: `GET /library` 单次返回上限。pad 的「选一段」列表一屏放不下 200 条，
#: 但也不能无上限——素材表是运营录进来的，没有分页就先给个硬顶。
LIBRARY_LIMIT = 200


# ---------------------------------------------------------------- 领域异常


class MediaDeviceUnboundError(Exception):
    """设备尚未绑定长者：不知道替谁点播，也不知道 `media_history` 该挂谁。"""


class MediaScopeForbiddenError(Exception):
    """跨长者访问：A 的设备去动 B 的播放历史。"""


# ---------------------------------------------------------------- 路由类


class MediaAPIRoute(ContractAPIRoute):
    """在契约错误体（`detail` / `code` / `trace_id`）上补媒体域的四类失败。

    鉴权、参数校验、`RequestValidationError` 仍由 `ContractAPIRoute` 兜
    （401 由 `app/main.py` 的全局处理器给），这里只加服务层异常与两个薄层异常。
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def media_handler(request: Request) -> Response:
            try:
                return await original(request)
            except MediaNotFoundError:
                # FR-CON-04：没找到就如实说「没这段」，不许拿别的节目凑数。
                return _error_response(404, "NOT_FOUND", "没找到这段节目，换一段试试")
            except MediaCopyrightError:
                # 版权未授权是「这段不能播」，不是「服务器坏了」：422 让前端换段并提示。
                # `code` 必须是**独立**的：与普通参数校验共用 `VALIDATION_ERROR` 时，
                # 长者端只能把它读成「我发错了请求」，会把「这段没授权」说成界面故障。
                return _error_response(
                    422, "COPYRIGHT_UNAUTHORIZED", "这段节目暂无播放授权，换一段试试"
                )
            except MediaScopeForbiddenError:
                return _error_response(403, "SCOPE_FORBIDDEN", "无权操作其他长者的播放记录")
            except MediaDeviceUnboundError:
                return _error_response(409, "device_unbound", "设备尚未绑定长者，请先完成配对")
            except MediaConflictError as exc:
                # 素材维护的两种冲突：地址撞唯一键 / 已被点播过不能删。
                # 409 而不是 422：请求本身合法，是「当前状态不允许」，重发前得先改数据。
                return _error_response(409, "CONFLICT", str(exc))

        return media_handler


# 端点全部挂 `MediaAPIRoute`：错误体三字段由路由类统一产出，端点内只管抛异常。
router = APIRouter(tags=["media"], route_class=MediaAPIRoute)


# ---------------------------------------------------------------- 响应 / 请求模型


class MediaItemResponse(BaseModel):
    """一条可播节目。字段名即前端契约，改名会让 pad 静默拿不到 url。"""

    media_id: uuid.UUID
    kind: MediaKind
    title: str
    genre: str | None = None
    era: str | None = None
    artist: str | None = None
    url: str = Field(description="nginx 托管的 /media/ 路径，可直接喂给 <audio src>")
    duration_seconds: int | None = Field(
        default=None, description="素材时长（秒），录入缺失为 null"
    )


class MediaLibraryResponse(BaseModel):
    kind: MediaKind
    items: list[MediaItemResponse]


class MediaPickRequest(BaseModel):
    """`pick` 的入参。**没有** `elder_id`：身份只能来自设备令牌（AGENTS §4.9）。"""

    model_config = ConfigDict(extra="forbid")

    kind: MediaKind = cast(MediaKind, DEFAULT_KIND)
    genre: str | None = Field(default=None, max_length=50, examples=["越剧"])


class MediaPickResponse(BaseModel):
    kind: MediaKind
    item: MediaItemResponse


class MediaPlayResponse(BaseModel):
    history_id: uuid.UUID
    elder_id: uuid.UUID
    started_at: datetime
    item: MediaItemResponse


class MediaFinishRequest(BaseModel):
    """结束一次播放。

    完成度用「播到哪 / 总长」两段毫秒表达——pad 的 `<audio>` 手上就是
    `currentTime` 与 `duration`，让前端自己算比率反而容易算出 130%。
    两个值缺一个就是「没测到」，落 NULL（`media_service` 里 NULL 表示不知道，
    0 表示被打断，两者不能混）。
    """

    model_config = ConfigDict(extra="forbid")

    history_id: uuid.UUID
    progress_ms: int | None = Field(default=None, ge=0)
    duration_ms: int | None = Field(default=None, gt=0)


class MediaFinishResponse(BaseModel):
    history_id: uuid.UUID
    elder_id: uuid.UUID
    media_id: uuid.UUID
    started_at: datetime
    finished_at: datetime
    completion_rate: float | None = Field(
        default=None, description="0..1；null 表示没测到完成度（不是 0）"
    )


class MediaSwitchNextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: MediaKind = cast(MediaKind, DEFAULT_KIND)
    genre: str | None = Field(default=None, max_length=50)


class MediaSwitchNextResponse(BaseModel):
    kind: MediaKind
    item: MediaItemResponse


class MediaLibraryItemIn(BaseModel):
    """管理员新增一条素材（FR-ADM-02「本地素材维护」）。

    **没有** `media_id`（主键由库生成）也**没有**任何操作者字段：操作者只从服务端
    身份取（AGENTS §4.9）。`kind` 复用长者端的 `MediaKind`，`kind=news` 仍按 422 拒。
    """

    model_config = ConfigDict(extra="forbid")

    kind: MediaKind = cast(MediaKind, DEFAULT_KIND)
    title: str = Field(min_length=1, max_length=120)
    media_url: str = Field(
        min_length=1,
        max_length=512,
        description="站内相对路径或授权外链；本端会归一化成 /media/… 给前端",
    )
    copyright_status: str = Field(
        min_length=1,
        max_length=64,
        description=f"授权状态，必须落在 {AUTHORIZED_COPYRIGHT_STATUSES} 白名单内",
    )
    genre: str | None = Field(default=None, max_length=50, examples=["越剧"])
    era: str | None = Field(default=None, max_length=50, examples=["民国"])
    artist: str | None = Field(default=None, max_length=120)
    duration: int | None = Field(default=None, gt=0, description="秒；未知留 null，不要填 0")


class MediaLibraryItemPatch(BaseModel):
    """管理员改一条素材：**只改出现的字段**（Pydantic `exclude_unset` 口径）。

    显式传 `null` 表示「清空这一列」（`genre` / `era` / `artist` / `duration` 可空），
    与「不传=不动」严格区分；必填列传 `null` 由服务层判成 422，而不是静默写成 NULL。
    """

    model_config = ConfigDict(extra="forbid")

    kind: MediaKind | None = None
    title: str | None = Field(default=None, min_length=1, max_length=120)
    media_url: str | None = Field(default=None, min_length=1, max_length=512)
    copyright_status: str | None = Field(default=None, min_length=1, max_length=64)
    genre: str | None = Field(default=None, max_length=50)
    era: str | None = Field(default=None, max_length=50)
    artist: str | None = Field(default=None, max_length=120)
    duration: int | None = Field(default=None, gt=0)


class MediaLibraryItemOut(BaseModel):
    """素材的**管理视角**：比长者端多 `media_url` 原文与 `copyright_status`。

    长者端只看得到授权素材的 `/media/` 路径，运营需要看到「这条为什么不可播」，
    所以这里同时给原值与归一化值，不把 `copyright_status` 藏在列表筛选条件里。
    """

    media_id: uuid.UUID
    kind: MediaKind
    title: str
    genre: str | None = None
    era: str | None = None
    artist: str | None = None
    media_url: str = Field(description="库里原样存的地址（可能是外链）")
    url: str = Field(description="归一化后的 /media/ 路径，与长者端同一口径")
    duration_seconds: int | None = None
    copyright_status: str
    playable: bool = Field(
        description=(
            "这条现在能不能被长者端点播：`copyright_status` 落在 "
            f"{AUTHORIZED_COPYRIGHT_STATUSES} 内才为 true。"
            "列表里 `playable=false` 的行**不是脏数据**，是已撤授权或待补授权。"
        )
    )


class MediaLibraryItemsResponse(BaseModel):
    """管理员素材列表。`total` 是**过滤后的**总数，不是全库行数。

    维护界面靠 `items[].media_id` 才能改/删（长者端的 `/api/media/library` 只给
    授权素材、且不带这些信息），所以这个端点是 ADM-02 端到端可用的前置条件。
    """

    items: list[MediaLibraryItemOut]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------- 薄层工具


def _require_elder_id(identity: Identity) -> uuid.UUID:
    """从服务端身份取长者 ID；取不到就是设备没配对，409 而不是 500。"""
    raw = identity.elder_id
    if not raw:
        raise MediaDeviceUnboundError
    try:
        return uuid.UUID(raw)
    except ValueError as exc:  # 设备行里的 elder_id 坏了，等价于「这台设备还没配对」
        raise MediaDeviceUnboundError from exc


def _media_path(media_url: str) -> str:
    """把 `media_url` 折叠成 nginx 的 `/media/…` 路径。

    只取 path 段（丢掉 scheme / host / query），剥掉可能重复的 `media` 前缀，
    含 `..` 时只保留末段文件名。**永远**返回 `/media/` 开头：这样缺素材由
    `try_files $uri =404` 落 404，而不是被 MPA 兜底 location 喂回 `index.html`。
    """
    segments = [part for part in urlsplit(media_url).path.split("/") if part]
    if ".." in segments:
        segments = segments[-1:]
    if segments and segments[0] == "media":
        segments = segments[1:]
    return "/media/" + "/".join(segments)


def _item(item: MediaLibraryItem) -> MediaItemResponse:
    return MediaItemResponse(
        media_id=item.media_id,
        kind=cast(MediaKind, item.type),
        title=item.title,
        genre=item.genre,
        era=item.era,
        artist=item.artist,
        url=_media_path(item.media_url),
        duration_seconds=item.duration,
    )


#: 素材维护的审计资源类型（自由文本，按表名写，便于按 `resource_type` 过滤）。
MEDIA_LIBRARY_RESOURCE = "media_library"

#: 四个动作分开记：一条素材的「新建/改/删」在审计里必须能分开查。
ACTION_LIBRARY_CREATE = "media.library.create"
ACTION_LIBRARY_UPDATE = "media.library.update"
ACTION_LIBRARY_DELETE = "media.library.delete"
#: 列表是只读的：**成功的 list 不写审计**（会把审计表冲成噪音）；
#: 只有越权读被拒时按这个 action 留痕，和写动作一样能查出「谁试过」。
ACTION_LIBRARY_LIST = "media.library.list"


def _actor_id(identity: Identity) -> uuid.UUID:
    """审计里的 `actor_id`：员工/家属/设备三选一；都没有（理论上不会）用全零占位。"""
    raw = identity.staff_id or identity.guardian_id or identity.device_id
    if not raw:
        return NIL_RESOURCE_ID
    try:
        return uuid.UUID(raw)
    except ValueError:  # 身份行里的 ID 坏了：宁可留占位，也不要让 403 变成 500
        return NIL_RESOURCE_ID


async def _audit_denied(
    identity: Identity,
    *,
    action: str,
    resource_id: uuid.UUID,
    reason: str,
) -> None:
    """越权留痕：**独立会话**写。

    403 抛出后 `get_session` 会回滚本次请求，同事务写的审计会跟着消失——那正是
    「越权尝试查无实据」的老坑（`report_service` 踩过）。所以这条路必须用独立会话。
    """
    await audit_service.write_audit_isolated(
        actor_type=identity.kind,
        actor_id=_actor_id(identity),
        action=action,
        resource_type=MEDIA_LIBRARY_RESOURCE,
        resource_id=resource_id,
        detail={"result": "denied", "reason": reason, "role": identity.role},
    )


async def _require_admin(
    identity: Identity,
    *,
    action: str,
    resource_id: uuid.UUID = NIL_RESOURCE_ID,
) -> None:
    """素材维护只给管理员（FR-ADM-02）。

    主体**种类**由外层 `Depends(current_staff)` 守：非员工令牌一律 401（契约 §7「拿错
    凭证体系」＝401，不是 403）。到这里的只剩「是员工但角色不够」，这条路拒绝时必须
    留痕——素材库是素材池，谁尝试改过比「改没改成」更该被记住。
    """
    if identity.role == "admin":
        return
    await _audit_denied(identity, action=action, resource_id=resource_id, reason="role_not_allowed")
    raise RoleForbiddenError(f"角色 {identity.role!r} 不能维护素材库，需要 admin")


def _library_out(item: MediaLibraryItem) -> MediaLibraryItemOut:
    return MediaLibraryItemOut(
        media_id=item.media_id,
        kind=cast(MediaKind, item.type),
        title=item.title,
        genre=item.genre,
        era=item.era,
        artist=item.artist,
        media_url=item.media_url,
        url=_media_path(item.media_url),
        duration_seconds=item.duration,
        copyright_status=item.copyright_status,
        playable=item.copyright_status in AUTHORIZED_COPYRIGHT_STATUSES,
    )


def _completion_rate(progress_ms: int | None, duration_ms: int | None) -> float | None:
    """`progress_ms / duration_ms`，截断到 0..1；缺一个就返回 None（NULL，不是 0）。"""
    if progress_ms is None or duration_ms is None or duration_ms <= 0:
        return None
    return min(max(progress_ms / duration_ms, 0.0), 1.0)


def _ensure_finished_at(history: MediaHistory) -> datetime:
    """`media_service.finish()` 保证写回 `finished_at`；缺了说明服务层语义被破坏。

    宁可在响应组装处 500，也不要拿 `started_at` 顶上假装播放已结束。
    """
    if history.finished_at is None:  # pragma: no cover - 服务层已保证非空
        raise RuntimeError(f"media_history {history.history_id} 的 finished_at 未写回")
    return history.finished_at


# ---------------------------------------------------------------- 端点


@router.get(
    "/api/media/library",
    response_model=MediaLibraryResponse,
    summary="按类型列出可点播节目",
)
async def list_library(
    kind: Annotated[MediaKind, Query(description="opera=戏曲；movie=电影")],
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(current_device)],
) -> MediaLibraryResponse:
    """只列**可播**的素材：版权状态不在授权白名单里的节目不进列表。

    白名单直接取服务层的 `AUTHORIZED_COPYRIGHT_STATUSES`，不在 api 层复制一份——
    复制出来的第二份白名单迟早和 `pick` 的口径对不上。
    """
    stmt = (
        select(MediaLibraryItem)
        .where(
            MediaLibraryItem.type == kind,
            MediaLibraryItem.copyright_status.in_(AUTHORIZED_COPYRIGHT_STATUSES),
        )
        .order_by(MediaLibraryItem.media_id)
        .limit(LIBRARY_LIMIT)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return MediaLibraryResponse(kind=kind, items=[_item(row) for row in rows])


@router.post("/api/media/pick", response_model=MediaPickResponse, summary="挑一段可播节目")
async def pick_media(
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(current_device)],
    payload: Annotated[MediaPickRequest | None, Body()] = None,
) -> MediaPickResponse:
    """从库里挑一段（默认戏曲）。挑不到就是 404——空库时不许拿「随便什么」顶上。

    走 `pick_for_elder` 而不是裸 `pick`：要排除这位长者最近听过的几段。否则库里只有
    三段时，每次点「听一段戏」都是同一段（`pick` 按 `media_id` 升序取第一条），
    他刚说过「换一段」也照样重复。
    """
    elder_id = _require_elder_id(identity)
    request = payload or MediaPickRequest()
    item = await media_service.pick_for_elder(
        session, elder_id=elder_id, kind=request.kind, genre=request.genre
    )
    if item is None:
        raise MediaNotFoundError
    return MediaPickResponse(kind=request.kind, item=_item(item))


@router.post(
    "/api/media/{media_id}/play",
    response_model=MediaPlayResponse,
    summary="开始播放（落 media_history）",
)
async def play_media(
    media_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(current_device)],
) -> MediaPlayResponse:
    """`media_id` 来自路径，长者来自设备令牌，本端点**不读请求体**。

    `media_history` 的写入（`started_at` 取服务端 UTC）全部在 `media_service.play` 里，
    本函数只把结果翻译成响应；不存在 → 404、未授权 → 422 都由路由类统一成错误体。
    """
    elder_id = _require_elder_id(identity)
    history = await media_service.play(session, elder_id=elder_id, media_id=media_id)
    item = await session.get(MediaLibraryItem, media_id)
    if item is None:  # pragma: no cover - play 已校验存在，这里只为收窄类型
        raise MediaNotFoundError
    return MediaPlayResponse(
        history_id=history.history_id,
        elder_id=history.elder_id,
        started_at=history.started_at,
        item=_item(item),
    )


@router.post("/api/media/finish", response_model=MediaFinishResponse, summary="结束播放并记完成度")
async def finish_media(
    payload: MediaFinishRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(current_device)],
) -> MediaFinishResponse:
    """把一次播放收尾。

    跨长者隔离在这里做一次**只读**范围过滤：`media_service.finish` 只按 `history_id`
    找会话，没有「这条会话是不是我的」这个入参，而本次写作用域不允许改服务层。所以先
    `session.get` 校验归属（AGENTS §2.3 允许 api 做范围过滤），再交给服务层写。
    """
    elder_id = _require_elder_id(identity)
    history = await session.get(MediaHistory, payload.history_id)
    if history is None:
        raise MediaNotFoundError
    if history.elder_id != elder_id:
        raise MediaScopeForbiddenError

    finished = await media_service.finish(
        session,
        history_id=payload.history_id,
        completion_rate=_completion_rate(payload.progress_ms, payload.duration_ms),
    )
    rate = finished.completion_rate
    return MediaFinishResponse(
        history_id=finished.history_id,
        elder_id=finished.elder_id,
        media_id=finished.media_id,
        started_at=finished.started_at,
        finished_at=_ensure_finished_at(finished),
        completion_rate=None if rate is None else float(rate),
    )


@router.post(
    "/api/media/switch-next",
    response_model=MediaSwitchNextResponse,
    summary="换一段（并把上一段记成被打断）",
)
async def switch_next_media(
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(current_device)],
    payload: Annotated[MediaSwitchNextRequest | None, Body()] = None,
) -> MediaSwitchNextResponse:
    """规则层的 `play_next`（换一段）落到这里。

    去重（最近 5 条不再给）与「被打断记 0.0」都在 `media_service.switch_next` 里，
    本端点不重复实现；没有下一段时回 404 而不是原样重播当前段。
    """
    elder_id = _require_elder_id(identity)
    request = payload or MediaSwitchNextRequest()
    item = await media_service.switch_next(
        session, elder_id=elder_id, kind=request.kind, genre=request.genre
    )
    if item is None:
        raise MediaNotFoundError
    return MediaSwitchNextResponse(kind=request.kind, item=_item(item))


# ---------------------------------------------------------------- 素材维护（管理员）
#
# FR-ADM-02「本地素材维护」：演示周期开始时把本地的戏曲/电影录进来、录错了改、
# 下架不要的。三个动词都是**管理员**动作，长者端的 `/library` 只读且只列授权素材，
# 两条路共用 `media_library`，互不替代——这台平板上能听到什么，仍然由 `pick` 决定。


@router.get(
    "/api/media/library/items",
    response_model=MediaLibraryItemsResponse,
    summary="列出全部素材（管理员，含未授权）",
)
async def list_library_items(
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(current_staff)],
    kind: Annotated[MediaKind | None, Query(description="只列一类：opera / movie")] = None,
    genre: Annotated[str | None, Query(max_length=50, description="按流派等值过滤")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIBRARY_PAGE_SIZE)] = DEFAULT_LIBRARY_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> MediaLibraryItemsResponse | Response:
    """管理员素材列表：**含未授权素材**，带 `media_id` / `copyright_status` / `playable`。

    为什么不能直接用长者端那个 `GET /api/media/library`：它挂 `current_device`、只列
    授权白名单内的素材、且不带 `media_id` 之外的运营信息——维护界面拿不到 ID 就没法
    改、也没法解释「这条为什么播不了」（一条被撤授权的素材在那边会凭空消失）。两个
    端点读同一张表，用途不同，**不是重复实现**：长者端永不可写，管理员端点全部 admin。

    读列表不写审计（读不产生副作用，写反而会把审计表冲成噪音）；只有**越权读被拒**
    时留痕（`media.library.list` + `role_not_allowed`），与三个写动作同口径。
    """
    await _require_admin(identity, action=ACTION_LIBRARY_LIST)
    try:
        rows, total = await media_service.list_library_items(
            session,
            kind=kind,
            genre=genre,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        return _error_response(422, "VALIDATION_ERROR", str(exc))
    return MediaLibraryItemsResponse(
        items=[_library_out(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/api/media/library",
    response_model=MediaLibraryItemOut,
    status_code=201,
    summary="新增一条素材（管理员）",
)
async def create_library_item(
    payload: MediaLibraryItemIn,
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(current_staff)],
) -> MediaLibraryItemOut | Response:
    """新增素材。`copyright_status` 不在授权白名单内直接 422，**不**先入库再拦。

    201 而不是 200：这条请求真的产生了新行，前端据此刷新列表。
    """
    await _require_admin(identity, action=ACTION_LIBRARY_CREATE)
    try:
        item = await media_service.create_library_item(
            session,
            kind=payload.kind,
            title=payload.title,
            media_url=payload.media_url,
            copyright_status=payload.copyright_status,
            genre=payload.genre,
            era=payload.era,
            artist=payload.artist,
            duration=payload.duration,
        )
    except ValueError as exc:
        # 服务层的 `ValueError` 是「参数不合法」（空标题、负数时长、未知 kind）。
        # 不并进 409：那不是状态冲突，重发同样的请求会一直失败。
        return _error_response(422, "VALIDATION_ERROR", str(exc))

    await audit_service.write_audit(
        session,
        actor_type=identity.kind,
        actor_id=_actor_id(identity),
        action=ACTION_LIBRARY_CREATE,
        resource_type=MEDIA_LIBRARY_RESOURCE,
        resource_id=item.media_id,
        detail={
            "kind": item.type,
            "title": item.title,
            "copyright_status": item.copyright_status,
        },
    )
    return _library_out(item)


@router.patch(
    "/api/media/library/{media_id}",
    response_model=MediaLibraryItemOut,
    summary="修改一条素材（管理员）",
)
async def update_library_item(
    media_id: uuid.UUID,
    payload: MediaLibraryItemPatch,
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(current_staff)],
) -> MediaLibraryItemOut | Response:
    """按字段补丁改素材。

    只送 `exclude_unset` 的字段：不传＝不动，显式 `null`＝清空（`genre`/`era`/`artist`）。
    空补丁回 422 而不是 200：什么都没改却回成功，会让维护界面以为「保存了」。
    """
    await _require_admin(identity, action=ACTION_LIBRARY_UPDATE, resource_id=media_id)
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        return _error_response(422, "VALIDATION_ERROR", "请求体里没有任何要修改的字段")
    if "kind" in changes:
        # 对外字段名是 `kind`（与长者端一致），库里列名是 `type`（TD §3.5）。
        # 只在这一层换名，服务层不认识 `kind`。
        changes["type"] = changes.pop("kind")

    try:
        item = await media_service.update_library_item(session, media_id=media_id, changes=changes)
    except ValueError as exc:
        return _error_response(422, "VALIDATION_ERROR", str(exc))

    await audit_service.write_audit(
        session,
        actor_type=identity.kind,
        actor_id=_actor_id(identity),
        action=ACTION_LIBRARY_UPDATE,
        resource_type=MEDIA_LIBRARY_RESOURCE,
        resource_id=item.media_id,
        detail={"fields": sorted(changes), "copyright_status": item.copyright_status},
    )
    return _library_out(item)


@router.delete(
    "/api/media/library/{media_id}",
    status_code=204,
    summary="删除一条素材（管理员）",
)
async def delete_library_item(
    media_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(current_staff)],
) -> Response:
    """删除素材；**有播放历史就 409**（见 `media_service.delete_library_item`）。

    204 无响应体：删掉的行没有内容可回。真正要「下架但留历史」请用 PATCH 改
    `copyright_status`——那才是运营日常该做的动作。
    """
    await _require_admin(identity, action=ACTION_LIBRARY_DELETE, resource_id=media_id)
    item = await media_service.delete_library_item(session, media_id=media_id)
    await audit_service.write_audit(
        session,
        actor_type=identity.kind,
        actor_id=_actor_id(identity),
        action=ACTION_LIBRARY_DELETE,
        resource_type=MEDIA_LIBRARY_RESOURCE,
        resource_id=media_id,
        detail={"title": item.title, "media_url": item.media_url},
    )
    return Response(status_code=204)
