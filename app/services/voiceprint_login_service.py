"""声纹免登录：一次声纹比对换一个设备会话（task-71 · 后端一半）。

与 E4 的关系
------------
E4 `POST /api/device-sessions` 用**长期令牌**（device_secret）换 24h JWT；本模块用
**声纹**换**同一形状**的会话：同一签发原语 `sign_device_session`、同一 tv 口径
（`token_hash` 前 16 位，契约 S-03）、同一 `device_ttl_seconds()`。前端拿到的
`DeviceLoginResponse` 与 E4 逐字段一致，登录后的后续行为不分叉。

为什么单列一个 services 模块
---------------------------
声纹推理/加解密/余弦/阈值全部留在 `app/services/voiceprint_service.py`，本模块只做
编排：设备 → 唯一候选长者 → 声纹判定 → 签发。不塞进 `voiceprint_service` 的原因是
依赖方向：`identity_service` 已经 import `voiceprint_service` 做已登录会话的首轮比对，
反过来让 `voiceprint_service` import `Device` 并签令牌，等于把「纯声纹能力」升级成
「登录编排」，两条依赖会绕成环（AGENTS §2 单向依赖）。登录编排与 `auth_service`
同层，故放这里。

阈值从哪里来
------------
本模块**不读、也不复制**阈值：判定一律走 `voiceprint_service.cosine_compare_against_elder`，
它内部的 `threshold = settings.voiceprint_similarity_threshold`（0.55，AGENTS §7）。
`CosineMatch.matched` 是唯一判定口径，本模块只消费结果。

安全口径（都是「不泄露」）
------------------------
1. 失败响应**不带相似度**：分数会让人按 0.01 试探阈值。
2. **设备不存在 / 未绑长者 / 没有声纹样本**三种情况返回同一个 409 码，不给
   device_id 做存在性枚举的入口；`device_locked` 单列 403 是因为锁定是管理员动作、
   不指向「绑了谁」。
3. 日志只记 device_id、相似度两位小数、outcome；**不记音频、不记长者姓名**。
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import (
    _DEVICE_TOKEN_HASH_PREFIX_LENGTH,
    device_ttl_seconds,
    sign_device_session,
)
from app.models.organization import Device
from app.services import voiceprint_service as vs

# 复用 E4 的返回 DTO：声纹登录与设备令牌登录**必须是同一响应形状**，
# 否则前端要写两套「登录成功后怎么办」。私名导入是为避免两个 dataclass 漂移。
from app.services.auth_service import DeviceLoginResult, _DeviceBrief

logger = logging.getLogger(__name__)

#: 响应/日志里的错误码（前端按 code 分支，不解析 detail）
VOICEPRINT_NOT_ENROLLED = "voiceprint_not_enrolled"
VOICEPRINT_MISMATCH = "voiceprint_mismatch"
VOICEPRINT_UNAVAILABLE = "voiceprint_unavailable"
DEVICE_LOCKED = "device_locked"


class VoiceprintLoginError(Exception):
    """声纹登录的业务异常基类：路由层只认 `status_code` / `code` / `detail`。

    子类把三个属性写成类属性；`detail` 是**可以直接回给客户端**的话术，任何内部
    信息（相似度、长者姓名、样本条数）都不许进这里 —— 要进日志。
    """

    status_code: int = 500
    code: str = "INTERNAL_ERROR"
    detail: str = "内部错误"

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(self.detail if detail is None else detail)
        if detail is not None:
            self.detail = detail


class VoiceprintNotEnrolled(VoiceprintLoginError):
    """设备未知 / 未绑长者 / 该长者没有 active 声纹样本 —— 三者同一码，防枚举。"""

    status_code = 409
    code = VOICEPRINT_NOT_ENROLLED
    detail = "该设备暂无可用声纹档案"


class VoiceprintMismatch(VoiceprintLoginError):
    """声纹比对未达阈值。响应不带分数。"""

    status_code = 401
    code = VOICEPRINT_MISMATCH
    detail = "声纹不匹配，未能登录"


class VoiceprintLoginUnavailable(VoiceprintLoginError):
    """推理容器不可用或本进程声纹配置缺失（Fernet 密钥等）。"""

    status_code = 503
    code = VOICEPRINT_UNAVAILABLE
    detail = "声纹识别暂不可用，请使用设备令牌登录"


class VoiceprintDeviceLocked(VoiceprintLoginError):
    """设备被管理员锁定：换任何凭证都不签会话。"""

    status_code = 403
    code = DEVICE_LOCKED
    detail = "设备已锁定，请联系管理员"


def _log(
    outcome: str,
    *,
    device_id: uuid.UUID,
    similarity: float | None = None,
) -> None:
    """结构化日志：只有 device_id / 相似度两位小数 / outcome。

    相似度只在**算出分数之后**才有（无样本、设备未知时为 None）——不编一个 0.0 出来，
    否则日志里分不清「没比」和「比了 0 分」。
    """
    logger.info(
        "voiceprint_login",
        extra={
            "device_id": str(device_id),
            "similarity": None if similarity is None else round(similarity, 2),
            "outcome": outcome,
        },
    )


async def login_with_voiceprint(
    session: AsyncSession,
    *,
    device_id: uuid.UUID,
    audio_bytes: bytes,
) -> DeviceLoginResult:
    """未登录设备用一段声纹换会话（前端无需先拿 device_secret）。

    步骤与每一步的失败口径：

    1. 设备不存在 → `VoiceprintNotEnrolled`（409，与「没绑长者」同码，防枚举）；
    2. `status == "locked"` → `VoiceprintDeviceLocked`（403）；
    3. `elder_id is None` → `VoiceprintNotEnrolled`（409）；
    4. 该长者没有 active 样本 → `VoiceprintNotEnrolled`（409）——**在调容器之前**拒，
       不为没档案的设备白跑一次推理；
    5. 容器不可用 / Fernet 配置缺失 → `VoiceprintLoginUnavailable`（503）；
    6. 余弦 < 阈值 → `VoiceprintMismatch`（401）；
    7. 命中 → 按 E4 同一口径签 24h 设备会话。

    `audio_bytes` 用完即弃，不落库、不进日志（AC-36）。
    """
    device_row = (
        await session.execute(select(Device).where(Device.device_id == device_id))
    ).scalar_one_or_none()

    if device_row is None:
        _log("not_enrolled", device_id=device_id)
        # 设备不存在也报「没有声纹档案」：不让调用方靠 404/409 的差异枚举设备号。
        raise VoiceprintNotEnrolled()

    if device_row.status == "locked":
        _log("device_locked", device_id=device_id)
        raise VoiceprintDeviceLocked()

    elder_id = device_row.elder_id
    if elder_id is None:
        _log("not_enrolled", device_id=device_id)
        raise VoiceprintNotEnrolled()

    status = await vs.get_status(session, elder_id=elder_id)
    if not status.registered:
        # 先看档案再调容器：无样本的设备不该产生一次推理开销（也少一次外部依赖波动）。
        _log("not_enrolled", device_id=device_id)
        raise VoiceprintNotEnrolled()

    try:
        match = await vs.cosine_compare_against_elder(
            session=session,
            elder_id=elder_id,
            audio_bytes=audio_bytes,
        )
    except (vs.VoiceprintExtractUnavailable, vs.VoiceprintUnavailable) as exc:
        # 推理容器 503/超时/格式异常，或本进程 Fernet 配置缺失：都按「声纹暂不可用」，
        # 不让调用方从 500 里猜是我们配置错了还是容器挂了。
        logger.warning(
            "voiceprint_login unavailable: device_id=%s error_type=%s",
            device_id,
            type(exc).__name__,
        )
        _log("unavailable", device_id=device_id)
        raise VoiceprintLoginUnavailable() from exc

    if not match.matched:
        # 相似度只进日志（两位小数），绝不出现在异常 detail / 响应体里。
        _log("mismatch", device_id=device_id, similarity=match.score)
        raise VoiceprintMismatch()

    token_hash_prefix = device_row.token_hash[:_DEVICE_TOKEN_HASH_PREFIX_LENGTH]
    access = sign_device_session(
        device_id=device_row.device_id,
        token_hash_prefix=token_hash_prefix,
    )
    _log("granted", device_id=device_id, similarity=match.score)

    return DeviceLoginResult(
        access_token=access,
        expires_in=device_ttl_seconds(),
        device=_DeviceBrief(
            id=str(device_row.device_id),
            room_id=str(device_row.room_id),
            elder_id=str(device_row.elder_id) if device_row.elder_id else None,
        ),
    )


__all__ = [
    "DEVICE_LOCKED",
    "VOICEPRINT_MISMATCH",
    "VOICEPRINT_NOT_ENROLLED",
    "VOICEPRINT_UNAVAILABLE",
    "VoiceprintDeviceLocked",
    "VoiceprintLoginError",
    "VoiceprintLoginUnavailable",
    "VoiceprintMismatch",
    "VoiceprintNotEnrolled",
    "login_with_voiceprint",
]
