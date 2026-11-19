"""身份确认 REST 端点：`POST /api/identity/confirm`（按住说话降级链路的「是我」）。

## 它是 WS 帧的等价物，不是第二套身份体系

WS 链路里，长者点 pad 上的「是我」大按钮，前端发 `sendIdentityConfirm`（C→S
`identity_confirm` 帧），由 `app/audio/media_bridge.py` 调
`identity_service.open_thread_from_confirm(...)` 落 thread。按住说话走 REST 降级时
没有 WS 连接，本端点把同一件事用 HTTP 表达一次：

    设备令牌 + body.elder_id  ──校验──▶  identity_service.open_thread_from_confirm()

写库仍然只在 service（AGENTS §2：api 层不碰 ORM），本模块只做三件事：认设备、
比对长者、把结果序列化。服务层的候选集与降级逻辑一概照旧，不在这里复制一份。

## 校验顺序（顺序本身就是安全属性）

1. **401**：没有设备令牌（`current_device` 依赖拦下，早于任何业务判断）。
2. **409 `device_unbound`**：设备当前没绑长者 —— 不知道「是谁」就不该落确认态。
3. **403 `elder_mismatch`**：body 的 `elder_id` 与设备绑定的长者不一致。
   这条是 AGENTS §4-9 的硬要求：**不许借一次确认把身份绑到别人身上**。若把不匹配
   交给服务层，它会按 C5 裁决降级 generic（安静地吞掉），REST 调用方却会以为成功；
   所以 REST 层显式 403，让错误可见。
4. 通过之后才落库：已有未结束的 `confirm` thread 直接复用（幂等），否则新建。

## 幂等

同一台设备重复点按（或前端重试）在同一段会话里只该有一个确认态 thread。
判定用 `identity_service.find_open_thread()`：有未结束、`identity_source="confirm"`、
并且 `elder_id` 就是本次确认的这位 → 复用并返回 `created=false`。不拿 `ask_id`
做幂等键：`conversation_threads` 没有该列，而「未结束的确认态」本身就是会话的生命
周期边界（会话结束由 `ended_at` 标记，重连复用另有一套带有效期的判定）。

并发窗口（读-写非原子）诚实写在 README 式的注释里：两个请求同时读到「没有」时会
各插一条 thread。这与 WS 链路一致（bridge 用进程内 lock 串行化，HTTP 层没有等价物），
真要消除得给 `conversation_threads` 加「设备维度唯一未结束」约束——属于 schema 变更，
不在本卡范围。
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas.identity import IdentityConfirmRequest, IdentityConfirmResponse
from app.core.auth import current_device
from app.core.context import get_trace_id
from app.core.runtime_context import Identity
from app.db import get_session
from app.services import identity_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["identity"])


def _trace_id() -> str:
    """当前请求的 trace_id；中间件没装时退回 "-"（与 voiceprints/sos 同口径）。"""
    return get_trace_id() or "-"


def _error(status_code: int, *, code: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"detail": detail, "code": code, "trace_id": _trace_id()},
    )


@router.post(
    "/api/identity/confirm",
    response_model=IdentityConfirmResponse,
    responses={
        401: {"description": "缺少或无效的设备令牌"},
        403: {"description": "elder_mismatch：body 的 elder_id 不是本设备绑定的长者"},
        409: {"description": "device_unbound：设备尚未绑定长者"},
        503: {"description": "identity_unavailable：身份服务暂时不可用"},
    },
)
async def confirm_identity(
    identity: Annotated[Identity, Depends(current_device)],
    session: Annotated[AsyncSession, Depends(get_session)],
    payload: IdentityConfirmRequest,
) -> IdentityConfirmResponse | JSONResponse:
    """把「这台设备当前会话」落成 `identity_source=confirm` 的确认态。

    成功返回 `{thread_id, identity_source: "confirm", elder_id, created}`；
    `created=false` 表示复用了已有的未结束确认态 thread（重复点按 / 重试）。
    """
    raw_elder_id = identity.elder_id
    if raw_elder_id is None:
        # 设备被解绑（换床、退住、维护中）：不知道是谁就不落确认态。
        return _error(
            409,
            code="device_unbound",
            detail="设备尚未绑定长者，请先完成配对",
        )

    bound_elder_id = uuid.UUID(raw_elder_id)
    if payload.elder_id != bound_elder_id:
        # 不交给服务层降级：那边会安静地按 generic 处理，REST 调用方看不出被拒。
        logger.warning(
            "identity_confirm elder_mismatch",
            extra={
                "trace_id": _trace_id(),
                "device_id": identity.device_id,
                "elder_id": str(payload.elder_id),
                "bound_elder_id": str(bound_elder_id),
            },
        )
        return _error(
            403,
            code="elder_mismatch",
            detail="确认的长者与设备绑定的长者不一致",
        )

    device_uuid = uuid.UUID(str(identity.device_id))

    # 幂等：先看这台设备有没有一段**未结束的确认态**会话，有就直接复用。
    # 只认 confirm：generic/voice 的 open thread 不代表「长者点过是我」，
    # 复用它等于凭空替长者确认，必须新建一条 confirm thread。
    existing = await identity_service.find_open_thread(session, device_id=device_uuid)
    if (
        existing is not None
        and existing.identity_source == "confirm"
        and existing.elder_id == bound_elder_id
    ):
        logger.info(
            "identity_confirm reused",
            extra={
                "trace_id": _trace_id(),
                "device_id": str(device_uuid),
                "elder_id": str(bound_elder_id),
                "confirm_created": False,
            },
        )
        return IdentityConfirmResponse(
            thread_id=existing.thread_id,
            identity_source="confirm",
            elder_id=bound_elder_id,
            created=False,
        )

    try:
        decision = await identity_service.open_thread_from_confirm(
            session,
            device_id=device_uuid,
            confirmed_elder_id=bound_elder_id,
            # 候选集恒为 1 位（该设备绑定的长者）：REST 没有 identity_ask 的多候选，
            # 传同一个值让服务层既有的「confirm 必须等于候选」防线照样生效。
            candidate_elder_id=bound_elder_id,
        )
    except Exception:
        # 先 rollback 再返回：依赖会为正常返回的请求 commit，
        # 不回滚的话一次失败的 flush 会以「提交」的名义被吞掉。
        await session.rollback()
        logger.exception(
            "identity confirm failed",
            extra={
                "trace_id": _trace_id(),
                "device_id": str(device_uuid),
                "elder_id": str(bound_elder_id),
            },
        )
        return _error(
            503,
            code="identity_unavailable",
            detail="身份服务暂时不可用，请稍后再试",
        )

    if decision.elder_id is None or decision.source != "confirm":
        # 校验与写库之间设备被解绑等：服务层降级成 generic（C5 裁决 ③）。
        # 这里不假装成功——前端要看到「这次没确认上」，而不是拿着 generic 会话继续。
        logger.warning(
            "identity confirm degraded to generic",
            extra={
                "trace_id": _trace_id(),
                "device_id": str(device_uuid),
                "elder_id": str(bound_elder_id),
            },
        )
        return _error(
            409,
            code="device_unbound",
            detail="设备在确认过程中被解绑，请重新配对后再试",
        )

    logger.info(
        "identity confirmed",
        extra={
            "trace_id": _trace_id(),
            "device_id": str(device_uuid),
            "elder_id": str(bound_elder_id),
            "confirm_created": True,
        },
    )
    return IdentityConfirmResponse(
        thread_id=decision.thread_id,
        identity_source="confirm",
        elder_id=bound_elder_id,
        created=True,
    )


__all__ = ["router"]
