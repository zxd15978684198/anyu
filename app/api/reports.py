"""家属日报只读接口（PRD §4.8 FR-RPT-02 / FR-RPT-04）。

两个端点都只做「校验 + 调服务 + 序列化」，范围与隔离判断全在
`app/services/report_service.py`（AGENTS §2.3：api 层不写业务逻辑）。

（FR-RPT-04 落地后多了第三个端点 `GET /{elder_id}/audio`：它同样只做「调服务 + 回
文件」，鉴权与家属范围也是 `report_service.read_audio` 内部复用 `get_report` 的那一道，
本文件**没有**新增任何权限分支。）

## 为什么这里自己接异常

`ContractAPIRoute`（`app/api/elders.py`）只认识长者/设备那套领域异常，
`ReportNotFoundError` 不在映射表里；照搬 eldres 的映射又不许改它（不在本卡写
作用域）。所以两个端点各自 `try/except` 成契约错误体，**复用同一个**
`_error_response`，保证 `{detail, code, trace_id}` 形状与全仓一致。

## 时间口径

`date` 缺省 = `settings.scheduler_timezone` 的今天（与调度器生成日报的口径相同），
不是容器本地时区的今天——容器跑 UTC 时两者会差一天。

## 家属读日报（W3，裁决 A-01 / 契约 O6′）

家属**只**在「读日报 / 读通知」上被放行，范围取自 `elder_guardians` 里
`status='active'` 的绑定；读**长者档案**仍一律 403（`GET /api/elders` 那条闸在
`elder_service`，本文件与它无关）。因此这里**没有角色白名单可加**：本文件既不认
角色也不认绑定，`get_current_principal` 只负责把身份交给
`report_service`；`admin/doctor/guardian` 的白名单、绑定校验、越权审计都在
service 层（AGENTS §2.3）。本文件新增的只有这一段说明与端点 docstring——
响应字段（含家属看到的姓名，来自日报 `payload`，契约 line 453）一个字都没动。

## 语音条为什么走接口而不是静态目录（FR-RPT-04）

`audio_url` 存的是 `/api/reports/{elder_id}/audio?date=…`，不是 `data/` 下的静态路径：
音频是长者的健康数据，静态目录一旦被 `StaticFiles` 挂出去就等于绕开了家属绑定校验。
走接口则复用日报那一道闸（`report_service.read_audio` 内部就是 `get_report`），
`FileResponse` 负责 `Content-Type` / `Content-Length`，前端用带 Bearer 的 fetch 取
blob 再交给 `<audio>`——裸 `<audio src>` 带不上认证头，只会拿到 401。
"""

from __future__ import annotations

import datetime
import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.elders import ContractAPIRoute, _error_response
from app.core.auth import RoleForbiddenError, get_current_principal
from app.core.runtime_context import Identity
from app.db import get_session
from app.services import elder_service, report_service

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/reports",
    tags=["reports"],
    route_class=ContractAPIRoute,
)


class DailyReportOut(BaseModel):
    """一条日报。`payload` 是整块展示数据，结构见 report_service 模块文档。

    `elder_name` 由服务层在读取时从 `elders.name` 取出（契约 A-01 的替代方案：
    「日报接口自带展示名」）。没有它，家属端只能拿 `elder_id` 显示一串 UUID。
    """

    report_id: uuid.UUID
    elder_id: uuid.UUID
    elder_name: str
    report_date: datetime.date
    payload: dict[str, Any]
    audio_url: str | None = None
    generated_at: datetime.datetime


class DailyReportListOut(BaseModel):
    items: list[DailyReportOut]
    total: int
    days: int


def _out(view: report_service.DailyReportView) -> DailyReportOut:
    return DailyReportOut(
        report_id=view.report_id,
        elder_id=view.elder_id,
        elder_name=view.elder_name,
        report_date=view.report_date,
        payload=view.payload,
        audio_url=view.audio_url,
        generated_at=view.generated_at,
    )


def _report_error(exc: Exception) -> JSONResponse:
    """领域异常 → 契约错误体。403/404 的分档与 elders 端点保持一致。"""
    # 语音条缺失也走 404 `NOT_FOUND`，但 detail 单独一句：前端据此区分「日报没生成」
    # 与「日报在、语音暂时播不了」，不必去猜 payload 里有没有音频。
    if isinstance(exc, report_service.ReportAudioNotFoundError):
        return _error_response(404, "NOT_FOUND", "daily report audio not found")
    if isinstance(exc, report_service.ReportNotFoundError):
        return _error_response(404, "NOT_FOUND", "daily report not found")
    if isinstance(exc, elder_service.ElderNotFoundError):
        return _error_response(404, "NOT_FOUND", "elder not found")
    if isinstance(exc, elder_service.ElderScopeForbiddenError):
        return _error_response(403, "SCOPE_FORBIDDEN", "elder is out of your scope")
    if isinstance(exc, RoleForbiddenError):
        return _error_response(403, "ROLE_FORBIDDEN", "role is not allowed to read reports")
    raise exc  # pragma: no cover - 未分类异常交给全局 handler


@router.get("/{elder_id}", response_model=DailyReportOut)
async def get_daily_report(
    elder_id: uuid.UUID,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    report_date: Annotated[
        datetime.date | None,
        Query(alias="date", description="自然日（Asia/Shanghai），缺省为当日"),
    ] = None,
) -> DailyReportOut | JSONResponse:
    """读某长者某日日报（默认当日）。admin/doctor 全量或按负责范围，家属仅限绑定的长者。"""
    try:
        view = await report_service.get_report(session, identity, elder_id, report_date)
    except (report_service.ReportServiceError, elder_service.ElderServiceError) as exc:
        return _report_error(exc)
    except RoleForbiddenError as exc:
        return _report_error(exc)
    return _out(view)


@router.get("/{elder_id}/audio", response_model=None)
async def get_daily_report_audio(
    elder_id: uuid.UUID,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    report_date: Annotated[
        datetime.date | None,
        Query(alias="date", description="自然日（Asia/Shanghai），缺省为当日"),
    ] = None,
) -> FileResponse | JSONResponse:
    """点播某长者某日日报的语音条（FR-RPT-04）。

    范围与「读日报」同一道闸（`report_service.read_audio` 内部调 `get_report`），
    未生成语音 / 文件已清理 → 404 `NOT_FOUND`（detail 为 `daily report audio not found`）。
    `response_model=None`：响应是二进制文件，没有 Pydantic 模型可校验。
    """
    try:
        path = await report_service.read_audio(session, identity, elder_id, report_date)
    except (report_service.ReportServiceError, elder_service.ElderServiceError) as exc:
        return _report_error(exc)
    except RoleForbiddenError as exc:
        return _report_error(exc)
    return FileResponse(
        path,
        media_type="audio/wav",
        # 语音条是私人健康数据：只允许私有缓存，且每次回源校验（配合 ETag 支持 304）。
        headers={"Cache-Control": "private, max-age=300"},
    )


@router.get("/{elder_id}/list", response_model=DailyReportListOut)
async def list_daily_reports(
    elder_id: uuid.UUID,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    days: Annotated[int, Query(ge=1, le=365, description="回溯天数，含当日")] = 30,
) -> DailyReportListOut | JSONResponse:
    """近 `days` 天日报，按日期倒序；未生成的日期不补空行。家属同样只限绑定的长者。"""
    try:
        views = await report_service.list_reports(session, identity, elder_id, days=days)
    except (report_service.ReportServiceError, elder_service.ElderServiceError) as exc:
        return _report_error(exc)
    except RoleForbiddenError as exc:
        return _report_error(exc)
    return DailyReportListOut(items=[_out(view) for view in views], total=len(views), days=days)


__all__ = ["DailyReportListOut", "DailyReportOut", "router"]
