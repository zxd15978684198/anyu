"""一键求助（SOS）REST 端点：PRD FR-ALT-06 的后端入口。

## 为什么需要这个模块

FR-ALT-06 要求长者端**常显大按钮，点击即生成敏感事件 + 护工工单，不经过语音路由**。
前端 `frontend/src/pad/components/SosButton.tsx` / `pages/SosResult.tsx` 早已就绪，但仓库
里没有任何后端入口——按了只会 404。本模块补的就是这一层：一个设备令牌可达的
`POST /api/sos`，把「按键」翻译成一条安全告警。

## 口径（逐条对齐 PRD 与既有服务）

- **鉴权用设备令牌**：按钮长在 pad 上，那台设备绑定了哪位长者，求助就属于哪位。
  `elder_id` 只从设备绑定拿（`Identity.elder_id`），请求体**不接收** elder_id
  ——AGENTS §4-9：身份只能由服务端注入，客户端传的 elder_id 一律丢弃。
- **落库只走 `alert_service.handle_safety_alert`**：风险事件、红单、通知三张表的口径
  （脱敏、幂等键、单号、主责护工）已收在一个函数里。这里再写一份就是 AGENTS §4-8 的
  「同一个能力造第二套写入口」，所以本模块只负责：鉴权 → 拼 `SafetyHit` → 调服务 →
  序列化结果。
- **`occurred_at` 用服务端当前时刻**：这是唯一允许服务端填 now 的场景。语音命中必须由
  编排层带「命中时刻」进来（否则不同机器算出不同幂等键）；而按键没有语音流，此刻就是
  按下那一刻。
- **幂等**：复用 `alert_service` 的分钟桶幂等键（`rule_id + thread_id + 分钟`）。
  `thread_id` 取 `sos-{device_id}`，同一台设备在同一自然分钟内连点只会有一张单。
  注意这是**自然分钟**而非滚动 60 秒：跨分钟边界（如 08:00:59 与 08:01:01）会各建一单
  ——长者连点两下通常远快于一分钟，真要滚动窗得在服务层加状态，不在这张卡的范围内。
- **不提供长者侧撤销**：FR-UI-08 明确不给长者撤销入口，取消只能由护工在照护台做。
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_device
from app.core.context import get_trace_id
from app.core.runtime_context import Identity
from app.db import get_session
from app.router.decision import LAYER_L0, RouteDecision, SafetyHit
from app.services import alert_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sos"])

#: 规则 ID：稳定值，进风险事件 / 工单备注 / 通知 payload，事后按它筛「按键求助」。
#: 与语音命中的 `safety_emergency` 等规则区分开——同一个告警面板里要能看出触发源。
SOS_RULE_ID = "sos_button"

#: 分类取 `request_human`：按键本身**不是**急症判定，只表达「我需要人」。
#: 把它写成 emergency 会让所有按键都顶着「急症」进入护理口径，属于过度解读。
SOS_CATEGORY = alert_service.CATEGORY_REQUEST_HUMAN

#: 严重度：迭代 8 的红色事件只有一档；求助必须进红档，否则会被排到队列后面。
SOS_SEVERITY = "red"

#: 两张表共用的类型值（`risk_events.type` / `work_orders.type` 的 CHECK 都含 `sos`）。
SOS_WORK_ORDER_TYPE = "sos"

#: 命中片段（进风险事件 detail，人工复核时能看出是按钮而不是一句话）。
SOS_MATCHED = "一键求助按钮"

#: 备注上限：长者只可能说一句「我不舒服」，100 字已经远超需要；再长就该走语音。
MAX_NOTE_LENGTH = 100


class SosRequest(BaseModel):
    """求助请求体。**刻意不含 elder_id**：身份只从设备令牌来。

    整体可为空：前端第一版是「按了就发」，连 body 都不带；`note` 是给后续版本留的
    可选说明（例如长者选了「头晕」标签）。
    """

    note: str | None = Field(default=None, max_length=MAX_NOTE_LENGTH)


def _trace_id() -> str:
    """当前请求的 trace_id；中间件没装时退回 "-"（与 voiceprints 同口径）。"""
    return get_trace_id() or "-"


def _unbound_device_response() -> JSONResponse:
    """设备未绑定长者：409 `device_unbound`（与 voiceprints 的 C4 口径一致）。"""
    return JSONResponse(
        status_code=409,
        content={
            "detail": "设备尚未绑定长者，请先完成配对",
            "code": "device_unbound",
            "trace_id": _trace_id(),
        },
    )


def _sos_failed_response(*, code: str, detail: str) -> JSONResponse:
    """落库失败：500 + 结构化 code（前端按状态码分流，不看 detail 文案）。"""
    return JSONResponse(
        status_code=500,
        content={"detail": detail, "code": code, "trace_id": _trace_id()},
    )


def _bound_elder_id(identity: Identity) -> uuid.UUID | None:
    """设备绑定的长者（未绑定返回 None）。

    `Identity.elder_id` 在 `runtime_context._canon` 里已校验成规范 UUID 文本，
    所以这里的 `uuid.UUID(...)` 不会抛；还原成 UUID 是因为
    `handle_safety_alert(elder_id=...)` 的形参类型是 `uuid.UUID`。
    """
    raw = identity.elder_id
    return None if raw is None else uuid.UUID(raw)


def _build_decision(
    *, elder_id: uuid.UUID, device_id: str, trace_id: str, note: str
) -> RouteDecision:
    """把「按键」翻译成路由层认得的 `RouteDecision(alert=SafetyHit)`。

    关于 `layer=LAYER_L0`：按键不经过 ASR、不经过 Laya、不经过 LLM，是**确定性触发**，
    归到规则层最贴近事实（`decision_logs` 的枚举只有 L0/L1/L2，没有第四档可写）。
    `route_ms=0` 是同一个原因：这一层没有「路由耗时」可言，真实耗时记在下面的日志里。
    """
    hit = SafetyHit(
        rule_id=SOS_RULE_ID,
        category=SOS_CATEGORY,
        severity=SOS_SEVERITY,
        matched=SOS_MATCHED,
        transcript_excerpt=note,
        occurred_at=datetime.now(UTC),
    )
    return RouteDecision(
        elder_id=str(elder_id),
        thread_id=f"sos-{device_id}",
        layer=LAYER_L0,
        rule_id=SOS_RULE_ID,
        intent=alert_service.ALERT_INTENT,
        sensitive=True,
        route_ms=0,
        trace_id=trace_id,
        alert=hit,
    )


@router.post("/api/sos")
async def create_sos(
    identity: Annotated[Identity, Depends(current_device)],
    session: Annotated[AsyncSession, Depends(get_session)],
    payload: Annotated[SosRequest | None, Body()] = None,
) -> JSONResponse:
    """长者一键求助：生成敏感事件 + 护工红单（同一自然分钟内重复按不会重复建单）。

    返回 `{sos_id, created, work_order_no, notified, trace_id}`：
    `created=false` 表示这一分钟内已经建过单，`sos_id` / `work_order_no` 是**原来那张**，
    前端据此显示「护工已经在路上了」而不是再弹一次「已呼叫」。
    """
    elder_id = _bound_elder_id(identity)
    if elder_id is None:
        return _unbound_device_response()

    device_id = identity.device_id or "-"
    trace_id = _trace_id()
    note = (payload.note if payload is not None else None) or ""
    note = note.strip()

    decision = _build_decision(
        elder_id=elder_id,
        device_id=device_id,
        trace_id=trace_id,
        note=note,
    )

    started = time.perf_counter()
    try:
        outcome = await alert_service.handle_safety_alert(
            session,
            decision=decision,
            elder_id=elder_id,
            thread_id=decision.thread_id or f"sos-{device_id}",
            trace_id=trace_id,
            work_order_type=SOS_WORK_ORDER_TYPE,
        )
    except alert_service.AlertContractError:
        # 契约破损是**我们的** bug（少填字段/类型越界），不是长者操作错；
        # 单独一个 code，方便把「部署漏了迁移」和「数据库真挂了」分开查。
        await session.rollback()
        logger.exception(
            "sos contract error: 决策/类型不符合 alert_service 契约",
            extra={"trace_id": trace_id, "device_id": device_id, "elder_id": str(elder_id)},
        )
        return _sos_failed_response(
            code="sos_contract_error",
            detail="求助服务暂时不可用，请稍后再试",
        )
    except Exception:
        # 先 rollback 再返回：`get_session` 依赖会为正常返回的请求 commit，
        # 若不回滚，一次失败的 flush 会以「提交」的名义被吞掉。
        await session.rollback()
        logger.exception(
            "sos persist failed",
            extra={"trace_id": trace_id, "device_id": device_id, "elder_id": str(elder_id)},
        )
        return _sos_failed_response(
            code="sos_failed",
            detail="求助没能送达，请再按一次或直接呼救",
        )

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "sos handled device_id=%s elder_id=%s created=%s order_no=%s elapsed_ms=%d trace_id=%s",
        device_id,
        elder_id,
        outcome.created if outcome is not None else False,
        outcome.order_no if outcome is not None else None,
        elapsed_ms,
        trace_id,
        extra={
            "trace_id": trace_id,
            "device_id": device_id,
            "elder_id": str(elder_id),
            # 键名不能叫 created：LogRecord 自带 created（时间戳），
            # 覆盖它会让 logging 直接抛 KeyError，把一次成功的求助变成 500。
            "sos_created": outcome.created if outcome is not None else False,
            "elapsed_ms": elapsed_ms,
        },
    )

    if outcome is None:  # 防御：带 alert 的决策不该走到这里
        return _sos_failed_response(code="sos_contract_error", detail="求助服务暂时不可用")

    return JSONResponse(
        status_code=200,
        content={
            "sos_id": str(outcome.risk_event_id),
            "created": outcome.created,
            "work_order_no": outcome.order_no,
            "notified": outcome.notification_id is not None,
            "trace_id": trace_id,
        },
    )


__all__ = ["MAX_NOTE_LENGTH", "SOS_RULE_ID", "SOS_WORK_ORDER_TYPE", "router"]
