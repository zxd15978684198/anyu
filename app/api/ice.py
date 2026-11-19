"""W2 · `GET /api/audio/ice-config` —— 下发给浏览器的 STUN/TURN 配置。

## 为什么需要这个端点

浏览器的 `RTCPeerConnection` 默认只带厂商 STUN，拿不到任何 TURN。跨容器 / 跨网段时
对称 NAT 两侧都只报内网地址，建连必失败，于是整条 WebRTC 链路降级成「按住说话」。
本端点把 coturn 的 STUN+TURN 配置下发给**设备端浏览器**，让 ICE 能走 relay。

## 口径（产品负责人 2026-11-09 裁决，契约见 `docs/接口契约_音频与信令.md`）

- 路径：`GET /api/audio/ice-config`；
- 鉴权：**设备令牌**（`typ=device`，即 pad 登录换到的 `access_token`）；
  家属 / 员工令牌是 403 `ROLE_FORBIDDEN`（持有效凭证但走错入口，不是 401），
  无凭证 / 凭证失效才是 401（由 `get_current_principal` 的异常体系给出）；
- `<host>` 取请求的 `X-Forwarded-Host`（优先）或 `Host` 头，**不硬编码**：
  本机浏览器访问 `https://localhost` 就得到 `turn:localhost:<turn_port>`，
  服务器上访问 `https://example.com` 就得到 `turn:example.com:<turn_port>`；
- 端口取 `settings.turn_port`（coturn 的 `--listening-port`，STUN 与 TURN 共用）；
- 降级：TURN 三项任一为空 → 只返回 STUN；全空 → `ice_servers: []`，
  前端无参建连（**不许抛错**）。

## 分层（AGENTS §2）

本模块只做三件事：判身份种类、从请求头取 host、把 `MediaConfig` 的结果序列化。
**不写库、不碰 ORM、不读 `.env`**（配置只经 `get_settings()`）。
只读配置端点不承载任何跨长者数据，故 403 只拒绝、不落 `audit_logs`
（与 `app/api/pad.py` 的 `avatar-config` 同一口径：那里越权也不写审计）。
"""

from __future__ import annotations

import datetime
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.api.elders import ContractAPIRoute
from app.audio.webrtc import MediaConfig
from app.core.auth import RoleForbiddenError, get_current_principal
from app.core.config import get_settings
from app.core.context import get_trace_id
from app.core.runtime_context import Identity

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/audio", tags=["audio"], route_class=ContractAPIRoute)

#: 设备身份在 `Identity.kind` 里的取值（与契约 §5.1 的 `typ` / `kind` 同名同义）。
_DEVICE_KIND = "device"

#: `_request_host` 的头部优先级：反代场景 `Host` 是容器名（`backend:8000`），
#: 只有 `X-Forwarded-Host` 才是长者浏览器实际访问的域名。
_HOST_HEADERS: tuple[str, ...] = ("x-forwarded-host", "host")


class IceConfigResponse(BaseModel):
    """200 响应体（三个键，形状与契约逐字一致）。

    `ice_servers` 用 `list[dict]` 而不是再建一层模型：每项要么 `{"urls": [...]}`，
    要么 `{"urls": [...], "username": ..., "credential": ...}`，用 `Optional` 字段建模
    会让「只给 STUN」的那一项多出两个 `null` 键，与契约示例不逐字相等。
    """

    ice_servers: list[dict[str, Any]]
    ttl_seconds: int
    generated_at: str


def _request_host(request: Request) -> str:
    """取请求的主机名（**去掉端口**），用于现拼 `stun:` / `turn:` URL。

    - 优先 `X-Forwarded-Host`：nginx 转发后 `Host` 是上游服务名，浏览器认的域名只在
      这个头里；不读它，服务器上会下发 `turn:backend:3478` —— 一个永远连不上的地址。
    - 多级反代按惯例取第一段（`a.example.com, b.internal` → `a.example.com`）。
    - 去掉 `host:port` 里的端口：ICE URL 自己带端口，`turn:localhost:5173:3478` 是坏 URI。
    - IPv6 字面量保留方括号：RFC 7065 的 TURN URI 形态是 `turn:[::1]:3478`。
    """
    raw = ""
    for name in _HOST_HEADERS:
        value = request.headers.get(name)
        if value and value.strip():
            raw = value
            break

    candidate = raw.split(",")[0].strip()
    if "://" in candidate:  # 某些反代会把 scheme 一起塞进来
        candidate = candidate.split("://", 1)[1]
    candidate = candidate.split("/", 1)[0]  # 误带路径时只取 authority
    if candidate.startswith("["):
        end = candidate.find("]")
        return candidate[: end + 1] if end != -1 else candidate
    return candidate.split(":", 1)[0]


def _utc_now_z() -> str:
    """`generated_at`：UTC、秒级、以 `Z` 结尾（契约示例形态，不用 `+00:00`）。"""
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@router.get(
    "/ice-config",
    response_model=IceConfigResponse,
    summary="浏览器建立 WebRTC 所需的 STUN/TURN 配置（设备令牌）",
)
async def read_ice_config(
    request: Request,
    identity: Annotated[Identity, Depends(get_current_principal)],
) -> IceConfigResponse:
    """返回本环境可用的 ICE 配置；配置不全是**降级**而不是错误。"""

    # 主体种类判断放在依赖之后、业务之前：拿家属/员工令牌打这个端点说明用错了凭证体系，
    # 403 才符合契约 §7 的四类拒绝（401 会误导成「重新登录就好」）。
    # 故意不用 `Depends(current_device)`：它在种类不符时给 401，与 W2 已定口径相反。
    if identity.kind != _DEVICE_KIND:
        raise RoleForbiddenError("ice-config is only reachable with a device token")

    media = MediaConfig.from_settings(get_settings())
    host = _request_host(request)
    if not host:
        # 没有 Host 就没有可信主机名：宁可下发空配置（前端无参建连），
        # 也不拼一个 `turn::3478` 的坏地址让浏览器静默失败。
        logger.warning(
            "ice-config request has no usable host header",
            extra={"trace_id": get_trace_id() or "-"},
        )
    return IceConfigResponse(
        ice_servers=media.ice_servers_for_client(host),
        ttl_seconds=media.turn_credential_ttl_seconds,
        generated_at=_utc_now_z(),
    )
