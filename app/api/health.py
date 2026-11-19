"""健康检查 + 健康记录（迭代 8 W2）。

`/health` 是**存活性探针**，刻意不探数据库 / Redis（本课验收要求「没有 `.env`
也能起来并 `curl /health` 返回 200」，探依赖会把它绑死在容器存活上）。

`/api/health/*` 是健康采集的业务端点（PRD §4.6 / FR-HLT-04/06/09）：代填、趋势、
补录、复核。它们必须碰数据库，所以放在同一模块但路径与探针完全分开 ——
`app/api/__init__.py` 以 `(health.router, "")` 挂载本模块，前缀为空，因此业务路径
在装饰器里写全 `/api/health/...`，注册表不必改（该文件归 Lead 独占）。

分层口径：本层只做参数校验 + 调 `app.services.health_service`，不写 ORM、不判分级
（AGENTS §2；分级阈值在服务层，API 只透传）。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.api.elders import ContractAPIRoute, _error_response
from app.core.auth import get_current_principal
from app.core.config import get_settings
from app.core.context import get_trace_id
from app.core.runtime_context import Identity
from app.db import get_session
from app.services import health_service
from app.services.health_service import (
    HealthConflictError,
    HealthNotFoundError,
    HealthRecordView,
    HealthScopeForbiddenError,
    HealthValidationError,
    ReviewTaskView,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str = Field(examples=["ok"], description="进程存活即 ok，不代表依赖就绪")
    service: str = Field(examples=["anyu"])
    version: str = Field(examples=["0.1.0"])
    app_env: str = Field(examples=["local"])
    trace_id: str = Field(description="本次请求的链路 ID，与响应头 X-Trace-Id 相同")
    server_time: str = Field(description="服务端 UTC 时间，ISO 8601")
    health_checkup_driver: str = Field(
        default="unknown",
        examples=["wired"],
        description=(
            "19:00 健康采集驱动是否**真的会跑**：wired=驱动装配成功且调度器已启动、"
            "19:00 job 已注册（唯一「真的会采」）｜driver_ready_but_scheduler_missing="
            "驱动有但调度器没起来（job 不会跑）｜unavailable=驱动装配失败（只落提醒、零会话）｜"
            "disabled=本环境不启动调度器｜unknown=未经过 lifespan 装配（如测试临时装配）"
        ),
    )
    health_checkup_driver_error: str | None = Field(
        default=None,
        examples=[None],
        description="装配失败时的异常类型名（如 ImportError）；其余情况为空",
    )


@router.get("/health", response_model=HealthResponse, summary="健康检查（不探依赖）")
async def health(request: Request) -> HealthResponse:
    settings = get_settings()
    # 采集驱动状态由装配根（`app/main.py` lifespan）写进 `app.state`：探针本身
    # 仍然不连库、不拦存活，只是把「今天到底会不会采」如实说出来。
    driver_state = getattr(request.app.state, "health_checkup_driver_state", "unknown")
    driver_error = getattr(request.app.state, "health_checkup_driver_error", None)
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        version=__version__,
        app_env=settings.app_env,
        trace_id=get_trace_id() or "-",
        server_time=datetime.now(UTC).isoformat(timespec="seconds"),
        health_checkup_driver=driver_state,
        health_checkup_driver_error=driver_error,
    )


# ------------------------------------------------------------------ 契约模型


class HealthRecordResponse(BaseModel):
    """一条健康记录（补录链上的单个 revision）。"""

    record_id: uuid.UUID
    elder_id: uuid.UUID
    record_date: date
    source: str = Field(
        description="voice_scheduled | voice_active | staff_scheduled | staff_active"
    )
    systolic: float | None = None
    diastolic: float | None = None
    heart_rate: float | None = None
    sleep_hours: float | None = Field(default=None, description="Numeric(4,1)，序列化成数字")
    mood_score: int | None = None
    grade: str = Field(description="green | yellow | red | missing")
    abnormal_reasons: list[str] = Field(default_factory=list)
    revision: int
    superseded_by: uuid.UUID | None = None
    recorded_by_staff_id: uuid.UUID | None = None
    collected_at: datetime
    notification_id: uuid.UUID | None = Field(
        default=None, description="黄/红档生成的复核任务；绿档为空"
    )
    complaint: str | None = Field(
        default=None, description="长者主动主诉原文（HLT-07）；非主诉发起为空"
    )
    revoked_at: datetime | None = Field(
        default=None, description="撤销时刻（UTC）；空=未撤销。撤销是软删留痕，不删行（HLT-04）"
    )
    revoked_by: uuid.UUID | None = Field(default=None, description="执行撤销的员工（医生/管理员）")

    @classmethod
    def from_view(cls, view: HealthRecordView) -> HealthRecordResponse:
        return cls(
            record_id=view.record_id,
            elder_id=view.elder_id,
            record_date=view.record_date,
            source=view.source,
            systolic=view.systolic,
            diastolic=view.diastolic,
            heart_rate=view.heart_rate,
            # Numeric 列在 psycopg 里是 Decimal，JSON 不能直出 Decimal。
            sleep_hours=float(view.sleep_hours) if view.sleep_hours is not None else None,
            mood_score=view.mood_score,
            grade=view.grade,
            abnormal_reasons=list(view.abnormal_reasons),
            revision=view.revision,
            superseded_by=view.superseded_by,
            recorded_by_staff_id=view.recorded_by_staff_id,
            collected_at=view.collected_at,
            notification_id=view.notification_id,
            complaint=view.complaint,
            revoked_at=view.revoked_at,
            revoked_by=view.revoked_by,
        )


class ReviewTaskResponse(BaseModel):
    """黄/红档的复核任务（`notifications(kind='health_review')`）。"""

    notification_id: uuid.UUID
    elder_id: uuid.UUID | None = None
    severity: str
    status: str
    payload: dict[str, Any]
    created_at: datetime
    acknowledged_at: datetime | None = None

    @classmethod
    def from_view(cls, view: ReviewTaskView) -> ReviewTaskResponse:
        return cls(
            notification_id=view.notification_id,
            elder_id=view.elder_id,
            severity=view.severity,
            status=view.status,
            payload=dict(view.payload),
            created_at=view.created_at,
            acknowledged_at=view.acknowledged_at,
        )


# 分级引擎只认这五个键；`values()` 必须把它们与请求体里的元数据（elder_id/source）分开。
_VITAL_KEYS = ("systolic", "diastolic", "heart_rate", "sleep_hours", "mood_score")


class _VitalsFields(BaseModel):
    """五字段的入参口径；字段级约束先挡明显越界，服务层再按 PRD 规则复验。"""

    systolic: int | None = Field(default=None, ge=0, le=300, description="收缩压 mmHg")
    diastolic: int | None = Field(default=None, ge=0, le=300, description="舒张压 mmHg")
    heart_rate: int | None = Field(default=None, ge=20, le=250, description="心率 bpm")
    sleep_hours: float | None = Field(default=None, ge=0, le=24, description="昨晚睡眠时长")
    mood_score: int | None = Field(default=None, ge=1, le=5, description="情绪 1–5")

    def values(self) -> dict[str, object]:
        """只交出五字段本身。

        注意：子类（`FillVitalsRequest`）还带 `elder_id`/`source`/`record_date`，
        直接 `model_dump()` 会把它们一起塞进分级输入，服务层会按「未知字段」拒掉。
        """
        return {key: value for key in _VITAL_KEYS if (value := getattr(self, key)) is not None}


class FillVitalsRequest(_VitalsFields):
    """护工/医生代填（FR-HLT-06）。

    `complaint` 是 PRD §4.6 第 5 问（「今天身体有什么不舒服？」）的文本答案，
    它也走代填表单：`values()` 只交出五个数值键（分级引擎只认这五个），
    主诉单独作为参数交给服务层 —— 服务层据此抬黄并落 `health_records.complaint`。
    """

    elder_id: uuid.UUID
    source: Literal["staff_scheduled", "staff_active"] = Field(
        default="staff_active", description="staff_scheduled=按计划采集，staff_active=主动代填"
    )
    record_date: date | None = Field(default=None, description="不传则记北京时间当天")
    complaint: str | None = Field(
        default=None, max_length=500, description="主诉/不适（第 5 问）；「无」等同未填"
    )


class SupersedeRequest(_VitalsFields):
    """补录：只带要补的字段，未带的字段在新版本里为空（原值仍在旧版本行）。"""


class ResolveReviewRequest(BaseModel):
    decision: Literal["acknowledged", "escalated", "ignored"] = Field(
        default="acknowledged", description="复核裁决；escalated=已升级（如联系家属/医生）"
    )
    note: str | None = Field(default=None, max_length=500)


class TrendResponse(BaseModel):
    elder_id: uuid.UUID
    days: int
    records: list[HealthRecordResponse]


class HealthRecordListResponse(BaseModel):
    """列表页形状（前端 `normalizeRecordList` 直接吃这四个键）。"""

    items: list[HealthRecordResponse]
    total: int
    page: int
    page_size: int


class SupersedeResponse(BaseModel):
    record: HealthRecordResponse
    previous: HealthRecordResponse


# ------------------------------------------------------------------ 路由类与错误映射


class HealthAPIRoute(ContractAPIRoute):
    """在统一契约错误映射之上，再补健康领域的四种业务错误。

    复用 `ContractAPIRoute` 的 401/403/404/409/422 基线（与 elders/devices 同一套
    `{detail, code, trace_id}` 结构），只补自家异常 —— 不另起一套响应格式。
    """

    def get_route_handler(self) -> Callable[[Request], Any]:
        parent = super().get_route_handler()

        async def handler(request: Request) -> Any:
            try:
                return await parent(request)
            except HealthScopeForbiddenError:
                return _error_response(
                    status.HTTP_403_FORBIDDEN, "SCOPE_FORBIDDEN", "长者不在当前账号负责范围内"
                )
            except HealthNotFoundError:
                return _error_response(
                    status.HTTP_404_NOT_FOUND, "NOT_FOUND", "健康记录或复核任务不存在"
                )
            except HealthConflictError:
                return _error_response(
                    status.HTTP_409_CONFLICT, "CONFLICT", "该记录已有更新的版本，请刷新后重试"
                )
            except HealthValidationError as exc:
                return _error_response(
                    status.HTTP_422_UNPROCESSABLE_CONTENT, "VALIDATION_ERROR", str(exc)
                )

        return handler


# 说明：现有 router 的 route_class 在构造时定死（且它不是本卡可改的注册入口），
# 故只用 `route_class_override` 给新增的业务路由套错误映射；`/health` 探针保持原样。
def _add_route(path: str, endpoint: Callable[..., Any], **kwargs: Any) -> None:
    router.add_api_route(path, endpoint, route_class_override=HealthAPIRoute, **kwargs)


# ------------------------------------------------------------------ 通用模式守卫（W2）

#: 身份未确认（generic）时的错误码（S-D4-2 裁决）。
#: 用 403 而不是 422：缺的是「你是谁」这个前置，不是请求参数不合法。
IDENTITY_REQUIRED_CODE = "IDENTITY_REQUIRED"


def _is_generic_identity(identity: Identity) -> bool:
    """通用模式在 HTTP 层的唯一可观测形态：**设备会话尚未确认到长者**。

    为什么不用 `identity.is_generic`：`Identity`（`app/core/runtime_context.py`）描述的是
    已验签的**主体**（`staff` / `guardian` / `device` / `staff_card`），它没有 generic 这一
    档；generic 是对话侧的判定（`DialogueIdentity.source`，`app/core/context.py`）。两者在
    API 层的交点是「pad 设备还没绑定/确认到长者」——`identity_service.resolve_on_connect`
    对 `devices.elder_id IS NULL` 正是直接落 generic 线程
    （`identity_service.py:252-258`，reason=`elder_unbound`）。所以这里把
    「device 且 elder_id 为空」当作同一事实的投影，判定口径与服务层同源，不另发明一套。

    收窄到 `device` 的理由：家属没有 `elder_id` 但身份是确定的（403 应走角色/范围语义），
    工牌持有人同样是已确认的员工 —— 把它们算成 generic 会用一个错误码盖掉真正的越权原因。
    """
    return identity.kind == "device" and identity.elder_id is None


# ------------------------------------------------------------------ 代填 / 趋势 / 补录


async def fill_vitals(
    payload: FillVitalsRequest,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HealthRecordResponse | JSONResponse:
    """护工/医生代填一次采集（非负责长者 403；黄/红自动生成复核任务）。

    通用模式（FR-VPR-03 / AC-28）**不写健康记录**：身份未确认时连服务层都不进，
    自然也不会落库、不会生成复核任务；同样不写审计 —— generic 本就不该产生个人数据
    痕迹（计划 §3-W2）。
    """
    if _is_generic_identity(identity):
        return _error_response(
            status.HTTP_403_FORBIDDEN, IDENTITY_REQUIRED_CODE, "请先确认身份，再记录健康数据"
        )

    view = await health_service.fill_vitals(
        session,
        identity,
        elder_id=payload.elder_id,
        source=payload.source,
        values=payload.values(),
        record_date=payload.record_date,
        complaint=payload.complaint,
    )
    return HealthRecordResponse.from_view(view)


async def list_records(
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    elder_id: uuid.UUID | None = None,
    record_date: Annotated[date | None, Query(alias="date")] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> HealthRecordListResponse:
    """健康记录分页列表（照护台列表页；只看当前版本，最近记录在前）。"""
    views, total = await health_service.list_records(
        session,
        identity,
        elder_id=elder_id,
        record_date=record_date,
        page=page,
        page_size=page_size,
    )
    return HealthRecordListResponse(
        items=[HealthRecordResponse.from_view(view) for view in views],
        total=total,
        page=page,
        page_size=page_size,
    )


async def list_trend(
    elder_id: uuid.UUID,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    days: int = 14,
) -> TrendResponse:
    """近 N 天趋势（只看当前版本；补录前的旧值属于审计链，不进折线）。"""
    views = await health_service.list_trend(session, identity, elder_id=elder_id, days=days)
    return TrendResponse(
        elder_id=elder_id,
        days=days,
        records=[HealthRecordResponse.from_view(view) for view in views],
    )


async def supersede_record(
    record_id: uuid.UUID,
    payload: SupersedeRequest,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SupersedeResponse:
    """补录：追加新 revision，原行值一个不改（FR-HLT-04）。"""
    record, previous = await health_service.supersede(
        session, identity, record_id=record_id, values=payload.values()
    )
    return SupersedeResponse(
        record=HealthRecordResponse.from_view(record),
        previous=HealthRecordResponse.from_view(previous),
    )


async def revoke_record(
    record_id: uuid.UUID,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HealthRecordResponse:
    """撤销一条健康记录（医生/管理员；HLT-04）。

    软删：行留在库里（补录链与审计要能查到「曾经有过这条、谁撤的」），只是落
    `revoked_at`/`revoked_by`，日报与趋势从此不再计入它。撤销权只给医生/管理员
    （`health_service.revoke_record` 里校验角色）：护工能补录、不能抹掉自己填错的数。

    已撤销或已被补录版本顶替的记录返回 409：撤销一条「已经不是当前版本」的行没有意义，
    要撤回补录请对当前版本操作（服务层注释同款口径）。
    """
    view = await health_service.revoke_record(session, identity, record_id=record_id)
    return HealthRecordResponse.from_view(view)


# ------------------------------------------------------------------ 复核（HITL）


async def get_review(
    notification_id: uuid.UUID,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ReviewTaskResponse:
    """看一条复核任务（护工/医生按范围，admin 全量）。"""
    view = await health_service.get_review_task(session, identity, notification_id=notification_id)
    return ReviewTaskResponse.from_view(view)


async def resolve_review(
    notification_id: uuid.UUID,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    payload: ResolveReviewRequest | None = None,
) -> ReviewTaskResponse:
    """人工复核完成：置 acknowledged，并把裁决喂回采集子图（FR-HLT-03 的 HITL resume）。

    `payload` 可省略（空 body 也返 200）：照护台「已看过」按钮只需要把任务收掉，
    此时按默认裁决 `acknowledged` 处理。

    子图 resume 失败只记日志、不改请求结果：复核结论已经落库，采集线程的
    checkpoint 恢复属于「尽力而为」——把它做成强一致会让一次 PG checkpoint
    故障连带复核台不可用（长者侧已在等人工处置）。
    """
    decision = payload.decision if payload is not None else "acknowledged"
    note = payload.note if payload is not None else None
    view = await health_service.mark_review_acknowledged(
        session,
        identity,
        notification_id=notification_id,
        decision=decision,
        note=note,
    )
    thread_id = view.payload.get("thread_id")
    if thread_id:
        try:
            from langgraph.types import Command

            from app.graphs.health_checkup_graph import get_health_graph

            graph = await get_health_graph()
            await graph.ainvoke(
                Command(resume={"decision": decision, "note": note}),
                {"configurable": {"thread_id": str(thread_id)}},
            )
            # 复核裁决回来即这条采集线程的终点：会话收成 completed，超时扫描不再捡它。
            await health_service.close_checkup_session_by_thread(
                session, thread_id=str(thread_id), status="completed"
            )
        except Exception as exc:
            logger.warning(
                "health_review_resume_failed",
                extra={
                    "trace_id": get_trace_id() or "-",
                    "notification_id": str(notification_id),
                    "thread_id": str(thread_id),
                    "error": type(exc).__name__,
                },
            )
    return ReviewTaskResponse.from_view(view)


# ------------------------------------------------------------------ 采集会话（FR-HLT-01）


class CheckupSessionResponse(BaseModel):
    """一场采集会话的现况：现在问哪题、等到几点、已经答了什么。"""

    session_id: uuid.UUID
    elder_id: uuid.UUID
    record_date: date
    status: str = Field(description="asking | awaiting_review | completed | ended")
    source: str = Field(description="voice_scheduled（19:00 定时）| voice_active（长者主动）")
    thread_id: str
    current_key: str | None = Field(default=None, description="当前问的字段；复核/终态为空")
    prompt: dict[str, Any] | None = Field(
        default=None, description="当前问句（含 key/label/prompt/attempt）；终态为空"
    )
    expires_at: datetime | None = Field(default=None, description="本题等待截止；终态为空")
    attempt_no: int
    timeouts: int = Field(description="已发生的超时次数")
    answers: dict[str, float | int] = Field(default_factory=dict, description="已采集到的数值")
    missing: list[str] = Field(default_factory=list, description="两次无效/超时后标缺失的字段")
    active: bool = Field(description="是否仍在推进（asking/awaiting_review）")
    closed_at: datetime | None = None

    @classmethod
    def from_view(cls, view: health_service.CheckupSessionView) -> CheckupSessionResponse:
        return cls(
            session_id=view.session_id,
            elder_id=view.elder_id,
            record_date=view.record_date,
            status=view.status,
            source=view.source,
            thread_id=view.thread_id,
            current_key=view.current_key,
            prompt=dict(view.prompt) if view.prompt else None,
            expires_at=view.expires_at,
            attempt_no=view.attempt_no,
            timeouts=view.timeouts,
            answers=dict(view.answers),
            missing=list(view.missing),
            active=view.active,
            closed_at=view.closed_at,
        )


class CheckupAnswerRequest(BaseModel):
    """长者（或照护台代填）对当前一问的回答。"""

    text: str = Field(min_length=1, max_length=200, description="长者原话；由服务端解析成数值")
    record_date: date | None = Field(default=None, description="业务日；不传=今天（上海时区）")


class CheckupTurnResponse(BaseModel):
    """一次推进后的结果：会话现况 + 如果本次收尾则带上落库的记录。"""

    session: CheckupSessionResponse
    record: HealthRecordResponse | None = Field(
        default=None, description="本次收尾时落库的健康记录（绿/黄/红档由分级引擎决定）"
    )


async def get_checkup_session(
    elder_id: uuid.UUID,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    record_date: Annotated[date | None, Query(alias="date")] = None,
) -> CheckupSessionResponse:
    """拉取当天采集会话（pad 轮询问句、照护台看进度、家属看是否在采集）。"""
    if _is_generic_identity(identity):
        return _error_response(  # type: ignore[return-value]
            status.HTTP_403_FORBIDDEN, IDENTITY_REQUIRED_CODE, "请先确认身份，再查看健康采集"
        )
    view = await health_service.get_checkup_session_for(
        session, identity, elder_id=elder_id, record_date=record_date
    )
    return CheckupSessionResponse.from_view(view)


async def submit_checkup_answer(
    elder_id: uuid.UUID,
    payload: CheckupAnswerRequest,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CheckupTurnResponse:
    """回答当前一问，返回下一问或终态（PRD FR-HLT-01）。

    权限先走服务层：范围外主体 403、当天没有会话 404；`generic` 设备连服务层都不进
    （与 `fill_vitals` 同口径：身份未确认时不该产生健康数据）。
    """
    if _is_generic_identity(identity):
        return _error_response(  # type: ignore[return-value]
            status.HTTP_403_FORBIDDEN, IDENTITY_REQUIRED_CODE, "请先确认身份，再回答健康采集"
        )
    # 先按身份校验范围（未绑定/未授权连「有没有这场采集」都不该被告知）。
    await health_service.get_checkup_session_for(
        session, identity, elder_id=elder_id, record_date=payload.record_date
    )
    from app.graphs.health_checkup_graph import submit_checkup_answer as answer_turn

    outcome = await answer_turn(
        session,
        elder_id=elder_id,
        text=payload.text,
        record_date=payload.record_date,
    )
    return CheckupTurnResponse(
        session=CheckupSessionResponse.from_view(outcome.session),
        record=HealthRecordResponse.from_view(outcome.record) if outcome.record else None,
    )


async def finish_checkup(
    elder_id: uuid.UUID,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    record_date: Annotated[date | None, Query(alias="date")] = None,
) -> CheckupTurnResponse:
    """一次性结束当天采集（pad「结束采集」按钮，审计 A2-3）。

    与逐题「标缺失」的区别只在于往返次数：缺失集合由服务端按图的 state 一次算完，
    端侧不必先发 N 次再发「结束」，也就不会出现「标了 3 条、第 4 条网络断了」的半个缺失集。
    已答项照常落库分级（`_end_checkup` 的既有语义，不丢数据）。
    """
    if _is_generic_identity(identity):
        return _error_response(  # type: ignore[return-value]
            status.HTTP_403_FORBIDDEN, IDENTITY_REQUIRED_CODE, "请先确认身份，再结束健康采集"
        )
    # 先按身份校验范围（未绑定/未授权连「有没有这场采集」都不该被告知）
    await health_service.get_checkup_session_for(
        session, identity, elder_id=elder_id, record_date=record_date
    )
    from app.graphs.health_checkup_graph import end_checkup as finish_turn

    outcome = await finish_turn(session, elder_id=elder_id, record_date=record_date)
    return CheckupTurnResponse(
        session=CheckupSessionResponse.from_view(outcome.session),
        record=HealthRecordResponse.from_view(outcome.record) if outcome.record else None,
    )


_add_route(
    "/api/health/records",
    fill_vitals,
    methods=["POST"],
    response_model=HealthRecordResponse,
    status_code=status.HTTP_201_CREATED,
    summary="护工/医生代填健康采集",
)
_add_route(
    "/api/health/records",
    list_records,
    methods=["GET"],
    response_model=HealthRecordListResponse,
    summary="健康记录分页列表（当前版本）",
)
_add_route(
    "/api/health/records/{elder_id}",
    list_trend,
    methods=["GET"],
    response_model=TrendResponse,
    summary="长者健康趋势（近 N 天当前版本）",
)
_add_route(
    "/api/health/records/{record_id}/supersede",
    supersede_record,
    methods=["POST"],
    response_model=SupersedeResponse,
    summary="补录（追加版本，不改原值）",
)
_add_route(
    "/api/health/records/{record_id}/revoke",
    revoke_record,
    methods=["POST"],
    response_model=HealthRecordResponse,
    summary="撤销健康记录（医生/管理员，软删留痕）",
)
_add_route(
    "/api/health/review/{notification_id}",
    get_review,
    methods=["GET"],
    response_model=ReviewTaskResponse,
    summary="查看复核任务",
)
_add_route(
    "/api/health/review/{notification_id}/resolve",
    resolve_review,
    methods=["POST"],
    response_model=ReviewTaskResponse,
    summary="人工复核完成并恢复采集子图",
)
_add_route(
    "/api/health/checkup/{elder_id}",
    get_checkup_session,
    methods=["GET"],
    response_model=CheckupSessionResponse,
    summary="当天采集会话（当前问句 / 截止时间 / 已答项）",
)
_add_route(
    "/api/health/checkup/{elder_id}/answer",
    submit_checkup_answer,
    methods=["POST"],
    response_model=CheckupTurnResponse,
    summary="回答当前一问（推进到下一问或终态）",
)
_add_route(
    "/api/health/checkup/{elder_id}/finish",
    finish_checkup,
    methods=["POST"],
    response_model=CheckupTurnResponse,
    summary="一次性结束当天采集（未答项统一补为缺失）",
)


__all__ = [
    "IDENTITY_REQUIRED_CODE",
    "CheckupAnswerRequest",
    "CheckupSessionResponse",
    "CheckupTurnResponse",
    "FillVitalsRequest",
    "HealthAPIRoute",
    "HealthRecordListResponse",
    "HealthRecordResponse",
    "HealthResponse",
    "ResolveReviewRequest",
    "ReviewTaskResponse",
    "SupersedeRequest",
    "SupersedeResponse",
    "TrendResponse",
    "fill_vitals",
    "finish_checkup",
    "get_checkup_session",
    "get_review",
    "health",
    "list_records",
    "list_trend",
    "resolve_review",
    "revoke_record",
    "router",
    "submit_checkup_answer",
    "supersede_record",
]
