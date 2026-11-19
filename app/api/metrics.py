"""运维指标端点（`GET /metrics`）。

owner=error-contract · task-20；2026-11-13 迭代 5 · S3 改写为 Prometheus 文本抓取面。

背景：AGENTS.md §3、README、技术设计文档与 PRD FR-TC-08 都把 `/metrics` 当已有能力引用，
而 迭代 5 之前它返回的是 JSON 快照。Prometheus 抓取端不会带 JWT，也不认自定义 JSON，
所以 S3 做了三处口径变更（依据：`设计记录（可观测性接入）` §4.2）：

1. **响应体改 Prometheus 文本**（`text/plain; version=0.0.4`），内容来自
   `app/core/metrics.py::render_metrics()` —— 由 OTel Meter + Prometheus reader 渲染，
   与进程内打点是同一笔（`counter()` / `observe_ms()` 双写，不是第二条打点链路）；
2. **去掉 JWT**：抓取侧无法注入员工 token。安全性改由**回环地址限制**承担 ——
   只有 `127.0.0.1` / `::1` 能读，外网与其它容器一律 403（`B4-04` 的结论随之改为
   「远端读不到」，登记在 `docs/SECURITY.md` 的 R-2）；
3. **出错仍走契约 §7**：`ContractAPIRoute` 保留，403 响应体仍是 `{detail, code, trace_id}`。

开关关掉（`metrics_enabled=False`）时端点**仍返回 200**，但响应体为空：Prometheus 抓空
响应只会「没有样本」，比 500 更容易看出是配置问题（`app/core/metrics.py` 的口径）。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import PlainTextResponse

from app.api.elders import ContractAPIRoute
from app.core.metrics import render_metrics

# 路径不带前缀：`/metrics` 与 `/health` 同级，注册表里写 `(metrics.router, "")`。
router = APIRouter(tags=["metrics"], route_class=ContractAPIRoute)

#: 允许读 `/metrics` 的客户端地址。`testclient` 是 FastAPI TestClient 的默认 host。
_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "testclient"})

#: Prometheus 文本格式的 Content-Type（OpenMetrics 之前的事实标准，抓取端都认）。
_PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


@router.get(
    "/metrics",
    response_class=PlainTextResponse,
    summary="Prometheus 文本指标（仅本机可读）",
    description=(
        "返回 Prometheus exposition 格式的指标文本。只读、无副作用；"
        "非回环地址访问一律 403；`metrics_enabled=False` 时返回空文本（200）。"
    ),
    include_in_schema=False,
)
async def read_metrics(request: Request) -> PlainTextResponse:
    """把 OTel registry 渲染成文本返回。

    为什么先判回环再渲染：渲染本身有成本（Prometheus client 要拼全量文本），
    而外部探测会高频打这个端点；先拒掉能少做一半无用功。
    """
    host = request.client.host if request.client is not None else ""
    if host not in _LOOPBACK_HOSTS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="metrics endpoint is only available on loopback",
        )
    return PlainTextResponse(render_metrics(), media_type=_PROMETHEUS_CONTENT_TYPE)


__all__ = ["router"]
