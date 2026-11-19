"""mock-operations 的落库适配层：参数校验 → 复用 app/services 的唯一写实现 → 审计（N20）。

**一套写库实现的证据**（本模块自身不构造工单/通知行，也不写任何 INSERT）：

- 建单：`app.services.work_order_service.create_work_order` 是仓里唯一的工单插入点，
  漏服升级 `escalate_to_work_order`、拒服跟进 `refuse_event` 与安全告警都走它（含
  `WO-YYYYMMDD-NNNN` 单号与唯一冲突重算）；
- 通知：`app.services.notification_service.create_notification` 是通知写入口
  （kind/severity 闭集校验、`generated` 初始态、payload 落 JSONB）；
- 审计：`app.services.audit_service.write_audit`，与业务写**同事务**（要么都有、要么都没有）。

**为什么是「延迟导入 backend service」而不是「MCP 内部另写 INSERT」**：N20 卡明文禁止
第二套落库逻辑；本服务与 backend 同镜像、同源码、同库（compose 里同 `BACKEND_IMAGE`，
只换 entrypoint/command），复用 service 层即是共享同一实现。

**为什么不走「MCP 通过内部 HTTP 调 backend」**：`app/api/**` 里没有内部建单/通知端点，
新增端点要过三套鉴权与 `runtime_context` 注入（AGENTS §4.9 身份只从服务端注入），
属 N21 之后的改造面；而直接复用 service 层既不新增 API 面，也不产生第二份写库代码。
选择与理由同步写在 `README.md` §1。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import session_factory
from app.models.organization import Elder
from app.services import medication_service, notification_service, work_order_service
from app.services.audit_service import NIL_RESOURCE_ID, write_audit, write_audit_isolated

logger = logging.getLogger(__name__)

SessionFactory = async_sessionmaker[AsyncSession]

#: 工具层整段预算（开连接 + 存在性校验 + 写入 + 审计 + commit）。
TOOL_TIMEOUT_SECONDS: Final[float] = 10.0

#: `work_orders.note` 是可读备注（TD 里是 text 列），只挡「把整篇文档塞进来」。
MAX_NOTE_LENGTH: Final[int] = 1000

#: `notifications.payload.summary` 会被家属端直接当摘要渲染，限制成一句话长度。
MAX_MESSAGE_LENGTH: Final[int] = 500

#: `work_orders.severity` 的库内闭集（模型 CHECK 只有红/黄，没有 green）。
WORK_ORDER_SEVERITIES: Final[tuple[str, ...]] = ("red", "yellow")

#: 工具入参不带 severity，由 kind 推导：通知的严重度跟事件性质走，不由调用方随意拔高。
SEVERITY_BY_KIND: Final[dict[str, str]] = {
    "medication_escalation": "red",
    "health_review": "yellow",
    "collection_missed": "yellow",
    "family_report": "green",
}

#: 审计里不落原文的字段（消息内容属长者/家属隐私，只留长度与种类）。
_AUDIT_MESSAGE_PREVIEW_MAX: Final[int] = 40


#: 身份随请求头走（FR-TC-03）：header 由 `app/mcp_client/client.py` 在派发处按
#: runtime_context 注入，模型改不到传输层；body 里的 `elder_id` 是模型可写的入参，
#: 两者不一致就是「拿别人的长者身份调工具」的信号，必须拒绝而不是静默取一个。
ELDER_HEADER: Final[str] = "X-Anyu-Elder"

#: trace 头与 app 侧同名，让被拒调用也能在 audit_logs 里和请求链路对上。
TRACE_HEADER: Final[str] = "X-Trace-Id"


class OpsError(Exception):
    """结构化工具错误：`error_code` 对齐 mcp_client 的四分类（param_error/not_found/transient）。

    只带客户端可安全展示的字段；内部异常栈进日志，不进信封（AGENTS §5 MCP server 口径）。

    `audit_error_code` / `audit_detail` 是**审计专用**的旁路字段：信封里仍回四分类码
    （越权对外一律 `forbidden`），审计里则落稳定细分码（`elder_mismatch`）与脱敏证据。
    """

    def __init__(
        self,
        error_code: str,
        detail: str,
        *,
        retryable: bool = False,
        audit_error_code: str | None = None,
        audit_detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{error_code}: {detail}")
        self.error_code = error_code
        self.detail = detail
        self.retryable = retryable
        self.audit_error_code = audit_error_code
        self.audit_detail = audit_detail or {}


def _mask_elder(value: str) -> str:
    """审计里只留「可辨认但不可复用」的长者标识：前 8 位 + 后 4 位。"""
    text = value.strip()
    if len(text) <= 12:
        return f"{text[:4]}…{text[-2:]}" if len(text) > 6 else "…"
    return f"{text[:8]}…{text[-4:]}"


def resolve_elder_authority(body_elder_id: object, header_elder_id: str | None) -> str:
    """把「body 里的 elder_id」与「传输层 header」对齐成唯一权威值（FR-TC-03）。

    返回**拿去落库的** elder_id 字符串；任何不一致都在这里断掉，不往下走业务写入。

    规则（顺序即优先级）：

    1. header 缺失 → ``param_error/elder_id_missing``：body 的值来自模型，不能当身份用。
       保留这条旧码是为了让「绕过客户端直连」的调用方仍拿到结构化参数错。
    2. header 有值、body 也带值且二者不同 → ``forbidden/elder_mismatch``：这是越权信号，
       审计里落 ``requested_elder``（脱敏），业务行一行都不写。
    3. 其余（只有 header / 两者相同）→ 以 header 为准。
    """
    authority = (header_elder_id or "").strip()
    requested = body_elder_id if isinstance(body_elder_id, str) else None
    requested = requested.strip() if requested else None

    if not authority:
        raise OpsError(
            "param_error",
            "elder_id_missing",
            audit_detail={"requested_elder": _mask_elder(requested) if requested else None},
        )
    if requested and requested != authority:
        raise OpsError(
            "forbidden",
            "elder_mismatch",
            audit_error_code="elder_mismatch",
            audit_detail={"requested_elder": _mask_elder(requested)},
        )
    return authority


def _parse_elder_id(raw: object) -> uuid.UUID:
    """elder_id 必须是 UUID；字符串形态由工具协议传入。

    `None`/空串单独判 `elder_id_missing`：正常调用方由 `mcp_client` 从 runtime_context
    注入，走到这里为空说明上游没注入或有人绕过客户端直连 —— 这是参数错，不是内部错。
    服务端无法验证"这个调用方是谁"（MCP 无传输层鉴权），只能守住"值必须存在且合法"，
    身份覆盖的责任在 `app/mcp_client/client.py` 的派发处。
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise OpsError("param_error", "elder_id_missing")
    try:
        return uuid.UUID(str(raw))
    except (AttributeError, TypeError, ValueError) as exc:
        raise OpsError("param_error", "invalid_elder_id") from exc


async def _ensure_elder(session: AsyncSession, elder_id: uuid.UUID) -> None:
    """先验存在性：FK 违规会以 IntegrityError 冒出内部细节，不如提前判「目标不存在」。"""
    found = await session.scalar(select(Elder.elder_id).where(Elder.elder_id == elder_id))
    if found is None:
        raise OpsError("not_found", "elder_not_found")


def _masked_message(text: str) -> dict[str, Any]:
    """审计里的消息摘要：只留长度与前若干字，避免把整条家属通知写进审计。"""
    return {"length": len(text), "preview": text[:_AUDIT_MESSAGE_PREVIEW_MAX]}


async def create_work_order(
    elder_id: str | None,
    order_type: str,
    severity: str,
    note: str | None = None,
    *,
    factory: SessionFactory | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """建一张 `new` 工单，并在同一事务里写 `tool.call` 审计。

    入参校验全部在开事务前完成（非法调用不该占用数据库连接）；`order_type` 取
    `medication_service.WORK_ORDER_TYPES`（TD §3.4 的八值闭集），`severity` 只允许
    `red`/`yellow`（模型 CHECK 同源）。
    """
    elder = _parse_elder_id(elder_id)
    if order_type not in medication_service.WORK_ORDER_TYPES:
        raise OpsError("param_error", "unknown_type")
    if severity not in WORK_ORDER_SEVERITIES:
        raise OpsError("param_error", "invalid_severity")
    note_text = (note or "").strip()
    if len(note_text) > MAX_NOTE_LENGTH:
        raise OpsError("param_error", "note_too_long")
    if not note_text:
        note_text = f"MCP create_work_order（{order_type}）"

    try:
        async with asyncio.timeout(TOOL_TIMEOUT_SECONDS):
            async with (factory or session_factory)() as session:
                await _ensure_elder(session, elder)
                order = await work_order_service.create_work_order(
                    session,
                    elder_id=elder,
                    type=order_type,
                    severity=severity,
                    note=note_text,
                )
                # 同事务写审计：提交成功才有「单 + 账」，回滚则两者都不留。
                await write_audit(
                    session,
                    actor_type="system",
                    actor_id=elder,
                    action="tool.call",
                    resource_type="work_order",
                    resource_id=order.order_id,
                    detail={
                        "tool": "create_work_order",
                        "ok": True,
                        "order_no": order.order_no,
                        "type": order_type,
                        "severity": severity,
                    },
                    trace_id=trace_id,
                )
                await session.commit()
                return {
                    "order_id": str(order.order_id),
                    "order_no": order.order_no,
                    "status": order.status,
                    "type": order.type,
                    "severity": order.severity,
                    "elder_id": str(order.elder_id),
                }
    except OpsError:
        raise
    except TimeoutError as exc:
        logger.warning("create_work_order: tool budget %ss exceeded", TOOL_TIMEOUT_SECONDS)
        raise OpsError("transient", "timeout", retryable=True) from exc
    except SQLAlchemyError as exc:
        logger.exception("create_work_order: database failure")
        raise OpsError("transient", "db_unavailable", retryable=True) from exc


async def send_family_notification(
    elder_id: str | None,
    kind: str,
    message: str,
    *,
    factory: SessionFactory | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """生成一条家属通知（`generated` 初始态），并写 `tool.call` 审计。

    `message` 写进 `payload.summary`（家属端摘要优先取这个键，见
    `frontend/src/family/notifications.ts` 的契约注释），同时保留 `payload.message`
    供详情页渲染；`kind` 只接受 `NOTIFICATION_KINDS` 四值。
    """
    elder = _parse_elder_id(elder_id)
    if kind not in notification_service.NOTIFICATION_KINDS:
        raise OpsError("param_error", "unknown_kind")
    severity = SEVERITY_BY_KIND.get(kind)
    if severity is None:  # 新增 kind 忘了补映射时，宁可拒绝也不要瞎猜严重度
        raise OpsError("param_error", "unsupported_kind")
    text = (message or "").strip()
    if not text:
        raise OpsError("param_error", "message_required")
    if len(text) > MAX_MESSAGE_LENGTH:
        raise OpsError("param_error", "message_too_long")

    try:
        async with asyncio.timeout(TOOL_TIMEOUT_SECONDS):
            async with (factory or session_factory)() as session:
                await _ensure_elder(session, elder)
                notification = await notification_service.create_notification(
                    session,
                    kind=kind,
                    severity=severity,
                    elder_id=elder,
                    payload={
                        "summary": text,
                        "message": text,
                        "source": "mock-operations-mcp",
                    },
                )
                await write_audit(
                    session,
                    actor_type="system",
                    actor_id=elder,
                    action="tool.call",
                    resource_type="notification",
                    resource_id=notification.notification_id,
                    detail={
                        "tool": "send_family_notification",
                        "ok": True,
                        "kind": kind,
                        "severity": severity,
                        "message": _masked_message(text),
                    },
                    trace_id=trace_id,
                )
                await session.commit()
                return {
                    "notification_id": str(notification.notification_id),
                    "kind": notification.kind,
                    "severity": notification.severity,
                    "status": notification.status,
                    "elder_id": str(notification.elder_id),
                }
    except OpsError:
        raise
    except TimeoutError as exc:
        logger.warning("send_family_notification: tool budget %ss exceeded", TOOL_TIMEOUT_SECONDS)
        raise OpsError("transient", "timeout", retryable=True) from exc
    except SQLAlchemyError as exc:
        logger.exception("send_family_notification: database failure")
        raise OpsError("transient", "db_unavailable", retryable=True) from exc


async def audit_rejection(
    tool: str, elder_id: str | None, err: OpsError, *, trace_id: str | None = None
) -> None:
    """被拒调用也留痕（`tool.call` + `detail.ok=False`）。

    用**独立会话**写（`write_audit_isolated`）：业务事务已经不存在或已回滚，同事务写
    等于把证据一起抹掉。写失败不改变工具结论 —— 审计是旁路。

    越权类拒绝（`elder_mismatch`）与普通参数错的区别落在 `detail.error_code`：
    前者是稳定细分码，后者与信封同码；`trace_id` 透传后能与 app 侧日志对上。
    """
    try:
        try:
            resource_id = uuid.UUID(str(elder_id)) if elder_id else NIL_RESOURCE_ID
        except (TypeError, ValueError):
            resource_id = NIL_RESOURCE_ID
        detail: dict[str, Any] = {
            "tool": tool,
            "ok": False,
            "error_code": err.audit_error_code or err.error_code,
            "detail": err.detail,
        }
        detail.update(err.audit_detail)
        await write_audit_isolated(
            actor_type="system",
            actor_id=resource_id,
            action="tool.call",
            resource_type="tool",
            resource_id=resource_id,
            detail=detail,
            trace_id=trace_id,
        )
    except Exception:  # pragma: no cover - 审计旁路不能反过来打断工具调用
        logger.exception("audit rejection failed tool=%s", tool)
