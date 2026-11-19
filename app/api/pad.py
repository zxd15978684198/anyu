# owner=A3 · 依据 C7 v1.0 · PRD FR-3D-01/02（前端渲染所需的前置数据）
"""长者端（pad）渲染前置数据 REST 薄层 —— C7 `GET /api/pad/avatar-config`。

本文件只做四件事：取设备主体、调 `app/services/persona_service.py`、按 C7 序列化四键、
把「设备没绑长者」映射成 403。**不拼查询、不读配置、不读 persona 表**（AGENTS §2）。

三条口径：

1. **只认设备凭证**：`current_device` 会先把非 `kind=device` 的主体判成 401 —— 拿错了凭证
   体系（员工令牌 / 家属令牌）时，403 会误导客户端「换个更高级的账号就能过」，
   而真实动作是「用平板自己的设备令牌重登」（契约 §7 的四类拒绝）。
2. **未绑定 → 403 `device_not_bound`**：C7 把 403 的错误体定为 `{detail, code, trace_id}`。
   这里不能返回 200 + `avatar_url=None` 糊过去 —— 那个 200 的语义是「后端没配形象，
   前端用内置兜底」，而「设备没绑长者」需要人工去照护台重新绑定，是两种不同的处置。
3. **不挂载**：路由注册留在 `app/main.py`（T6 负责），本卡不碰装配文件，避免与其他人冲突。

`_error_response` 与 `ContractAPIRoute` 复用 `app/api/elders.py`：契约 §7 的错误体目前只有
那一处实现（`app/core/errors.py` 尚未建立，也不在本卡写作用域内）。复制一份错误体形状
迟早与 elders/devices 分叉，而这正是「同一个错误有两种 body」的典型来源；等统一错误处理器
落地后，三个 API 模块一起撤掉即可。
"""

from __future__ import annotations

import datetime
import logging
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dialogue import get_tts
from app.api.elders import ContractAPIRoute, _error_response
from app.audio.asr import TTSProvider
from app.core.auth import current_device
from app.core.config import get_settings
from app.core.context import get_trace_id
from app.core.runtime_context import Identity
from app.db import get_session
from app.services import (
    audit_service,
    device_service,
    medication_service,
    notification_service,
    persona_service,
)
from app.services.audit_service import NIL_RESOURCE_ID
from app.services.medication_service import MedicationServiceError
from app.services.tts_audio_service import announce_medication

logger = logging.getLogger(__name__)


async def _touch_device_heartbeat(
    identity: Annotated[Identity, Depends(current_device)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """FR-ADM-10：平板每次访问长者端接口都刷新一次心跳（服务层内按 60s 节流）。

    为什么挂成**各设备端点**的依赖、而不是路由级依赖：路由级会连 `/demo-login`
    一起罩住，而那个端点是**无鉴权的**（登录页在拿到设备凭证之前就要调它），
    一旦依赖里要 `current_device`，它就会先 401——演示登录直接不可用。
    所以挂法是「只挂在需要设备凭证的端点上」，漏挂一个端点，那类请求就在在线
    判定里变成幽灵设备，改端点时记得补。

    写库失败只降级成一条告警日志——心跳丢一次不影响业务，让整个请求 500 才是真故障。
    """
    if identity.device_id is None:
        return
    try:
        if await device_service.touch_heartbeat(session, identity.device_id):
            await session.commit()
    except Exception:
        logger.warning("pad heartbeat touch failed device_id=%s", identity.device_id, exc_info=True)


router = APIRouter(
    prefix="/api/pad",
    tags=["pad"],
    route_class=ContractAPIRoute,
)


class AvatarConfigResponse(BaseModel):
    """C7 的 200 响应体：四个键**严格**为这四项。

    显式声明响应模型而不是直接返回服务层的 TypedDict，是为了让「契约形状」在 FastAPI 的
    响应校验里被强制一次：服务层将来多出一个字段，也不会悄悄漏给前端。
    """

    avatar_url: str | None
    fallback_image_url: str | None
    rotation_y_offset: float
    version: int


@router.get(
    "/avatar-config",
    response_model=AvatarConfigResponse,
    summary="长者端数字人形象配置（C7；设备未绑定长者返回 403）",
    dependencies=[Depends(_touch_device_heartbeat)],
)
async def read_avatar_config(
    identity: Annotated[Identity, Depends(current_device)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AvatarConfigResponse | JSONResponse:
    """返回平板当前绑定长者该渲染的数字人配置（只读，无写库、无副作用）。"""

    if identity.elder_id is None:
        # 403 而不是空配置，理由见模块 docstring 第 2 条；code 按 C7 用下划线命名。
        return _error_response(403, "device_not_bound", "device is not bound to an elder")

    # `Identity.__post_init__` 已把 elder_id 规范成小写连字 UUID 文本，这次转换不可能失败；
    # 真失败说明身份被手改坏了，让它冒成 500 比悄悄按「没绑定」降级更容易被发现。
    elder_id = uuid.UUID(identity.elder_id)
    config = await persona_service.get_avatar_config(session, elder_id=elder_id)
    return AvatarConfigResponse.model_validate(config)


class DemoLoginResponse(BaseModel):
    """演示凭证：只有两项，前端只用来「填入」，不参与签发令牌。"""

    device_id: str
    device_secret: str


@router.get(
    "/demo-login",
    response_model=DemoLoginResponse,
    summary="本机演示：长者端一键填入的设备凭证（生产环境或未配置时 404）",
)
async def read_demo_login() -> DemoLoginResponse | JSONResponse:
    """把演示设备凭证交给登录页的「填入演示账号」按钮。

    为什么需要它：`devices` 表只存 `token_hash`（sha256），**设备密钥的明文在库里查不到**，
    演示时只能靠 `scripts/seed_demo.py` 的控制台输出或 README 手抄 40 位 UUID + 20 位字符串。

    三条边界（改之前先读）：

    1. **只服务本机演示**：`app_env` 是 prod/production 时一律 404 —— 不因为「配了变量」就放行；
    2. **不碰业务数据**：只回两个字符串，不查库、不签发令牌；换证仍走 `/api/device-sessions`；
    3. **留空即不可用**：`DEMO_DEVICE_ID` / `DEMO_DEVICE_SECRET` 任一为空就 404，
       所以 `.env.example` 里留空，真机部署不会意外带上演示凭证。
    """

    settings = get_settings()
    if settings.is_production or not (settings.demo_device_id and settings.demo_device_secret):
        return _error_response(404, "demo_login_unavailable", "demo login is not available")
    return DemoLoginResponse(
        device_id=settings.demo_device_id,
        device_secret=settings.demo_device_secret,
    )


# ---------------------------------------------------------------------------
# 断网兜底（FR-MED-08）：端侧缓存的「当日药单」与掉线/恢复上报
# ---------------------------------------------------------------------------
#
# 为什么不复用 `/api/medication/**`：那两个端点走 `get_current_principal`，
# 只对员工/家属开放（`_require_elder_scope`）；平板手里只有设备凭证，拿不到
# 「我自己这位长者今天的药单」。而 FR-MED-08 第①层恰恰要求平板在断网**前**
# 就把当日药单存进 IndexedDB——没有这个读接口，端侧缓存只能靠员工账号预热，
# 真断网时它就是空的。写侧同理：只有平板自己知道「我掉线了」。


class PadReminderOut(BaseModel):
    """端侧缓存的一行药单；字段口径与 `frontend/src/pad/offline/types.ts` 对齐。"""

    reminderId: str
    planId: str
    drugName: str
    dose: str
    usage: str
    scheduledAt: str
    windowSeconds: int
    attemptsSent: int
    snapshotAt: str


class PadRemindersOut(BaseModel):
    reminders: list[PadReminderOut]


class PadOfflineEventIn(BaseModel):
    """端侧上报的一条提醒定位键（只带时刻与计数，不带任何健康推断）。"""

    reminderId: uuid.UUID
    scheduledAt: datetime.datetime | None = None
    windowEnd: datetime.datetime | None = None
    attemptsSent: int = 0
    announced: bool = False


class PadOfflineReportIn(BaseModel):
    """一次联网状态变化：`offline` 走第③层，`online` 走第②层。"""

    kind: Literal["offline", "online"]
    at: datetime.datetime | None = None
    events: list[PadOfflineEventIn]


class PadOfflineEventOut(BaseModel):
    reminderId: str
    action: str
    reason: str
    attemptNo: int | None
    windowEnd: str | None
    workOrderNo: str | None


class PadOfflineReportOut(BaseModel):
    results: list[PadOfflineEventOut]


def _iso(value: datetime.datetime | None) -> str | None:
    """统一成「带时区的 ISO」：端侧拿它跟本地钟比，无时区的串会算错。"""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.UTC)
    return value.isoformat()


@router.get(
    "/medications/today",
    response_model=PadRemindersOut,
    summary="平板缓存的当日药单（FR-MED-08 第①层）",
    dependencies=[Depends(_touch_device_heartbeat)],
)
async def read_today_reminders(
    identity: Annotated[Identity, Depends(current_device)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PadRemindersOut | JSONResponse:
    """返回本机绑定长者**今天（上海时区）**的用药事件，供端侧写进 IndexedDB。

    只读、不写库。窗长与已触达次数必须由服务端给：端侧断网后没有别的来源能问
    「这一枪该等多久」「已经催过几次」。
    """

    if identity.elder_id is None:
        return _error_response(403, "device_not_bound", "device is not bound to an elder")

    elder_id = uuid.UUID(identity.elder_id)
    # 到点时刻倒序 → 今天的事件排在最前，默认上限足够覆盖单日。
    events = await medication_service.list_events(session, elder_id=elder_id)
    today = medication_service.today_in_shanghai()
    window_seconds = medication_service.attempt_wait_seconds()

    reminders: list[PadReminderOut] = []
    for event in events:
        scheduled = event.scheduled_at
        if scheduled.tzinfo is None:
            scheduled = scheduled.replace(tzinfo=datetime.UTC)
        # 「今天」按展示时区判（AGENTS §8：存储 UTC、展示 Asia/Shanghai）。
        if scheduled.astimezone(medication_service.SHANGHAI).date() != today:
            continue
        snapshot = event.drug_snapshot or {}
        reminders.append(
            PadReminderOut(
                reminderId=str(event.reminder_id),
                planId=str(event.plan_id),
                drugName=str(snapshot.get("drug_name") or ""),
                dose=str(snapshot.get("dose") or ""),
                usage=str(snapshot.get("usage") or ""),
                scheduledAt=_iso(scheduled) or "",
                windowSeconds=window_seconds,
                attemptsSent=int(event.retry_count or 0),
                snapshotAt=_iso(event.created_at) or "",
            )
        )
    logger.info("pad today reminders elder_id=%s count=%s", elder_id, len(reminders))
    return PadRemindersOut(reminders=reminders)


@router.post(
    "/offline/report",
    response_model=PadOfflineReportOut,
    summary="掉线/恢复上报（FR-MED-08 第②③层）",
    dependencies=[Depends(_touch_device_heartbeat)],
)
async def report_offline(
    body: PadOfflineReportIn,
    identity: Annotated[Identity, Depends(current_device)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PadOfflineReportOut | JSONResponse:
    """把平板观察到的掉线/恢复翻译成服务端动作（PRD:338 第②③层）。

    - `offline` → 第③层：事件仍在本轮窗口内就**立即通知护工**；已闭环/已超窗
      由服务层返回 `None`，这里如实回 `skipped`，不重复打扰（幂等由服务层保证）。
    - `online` → 第②层：按「次数用尽 > 超窗 > 窗口内」判补呼 / 建单 / 等等。
      其中 `callback` 分支**不在这里发外呼**——出站只有一处实现
      （`app/scheduler/medication.run_medication_touch`），恢复后那一枪由既有到点
      扫描按 `(event_id, attempt_no)` 幂等补发。本端点只负责判决与归因落库，
      不从 API 层另开一条外呼路径（AGENTS §2 依赖方向）。

    单条事件失败只记日志、不连坐整份上报——一块平板掉线时可能同时带着好几条提醒。
    """

    if identity.elder_id is None:
        return _error_response(403, "device_not_bound", "device is not bound to an elder")

    results: list[PadOfflineEventOut] = []
    for item in body.events:
        try:
            if body.kind == "offline":
                notification = await notification_service.notify_offline_overlap(
                    session, reminder_id=item.reminderId
                )
                results.append(
                    PadOfflineEventOut(
                        reminderId=str(item.reminderId),
                        action="notified" if notification is not None else "skipped",
                        reason="offline_overlap" if notification is not None else "no_overlap",
                        attemptNo=None,
                        windowEnd=_iso(item.windowEnd),
                        workOrderNo=None,
                    )
                )
            else:
                outcome = await notification_service.recover_after_offline(
                    session, reminder_id=item.reminderId
                )
                results.append(
                    PadOfflineEventOut(
                        reminderId=str(item.reminderId),
                        action=outcome.action,
                        reason=outcome.reason,
                        attemptNo=outcome.attempt_no,
                        windowEnd=_iso(outcome.window_end),
                        workOrderNo=outcome.work_order_no,
                    )
                )
        except MedicationServiceError:
            # 事件不存在/已删：如实告诉端侧「这条不用再等了」，不回显内部细节。
            logger.warning(
                "pad offline report skipped reminder_id=%s kind=%s",
                item.reminderId,
                body.kind,
            )
            results.append(
                PadOfflineEventOut(
                    reminderId=str(item.reminderId),
                    action="missing",
                    reason="event_not_found",
                    attemptNo=None,
                    windowEnd=_iso(item.windowEnd),
                    workOrderNo=None,
                )
            )
        except Exception:
            logger.exception(
                "pad offline report failed reminder_id=%s kind=%s", item.reminderId, body.kind
            )
            results.append(
                PadOfflineEventOut(
                    reminderId=str(item.reminderId),
                    action="error",
                    reason="internal_error",
                    attemptNo=None,
                    windowEnd=_iso(item.windowEnd),
                    workOrderNo=None,
                )
            )

    # commit 留在 API 层（服务层不 commit，AGENTS §2）：整份上报一次提交即可，
    # 失败的那几条已各自降级成 error 行，不影响其余。
    await session.commit()
    logger.info("pad offline report kind=%s events=%s", body.kind, len(body.events))
    return PadOfflineReportOut(results=results)


class PadMedicationAnnouncementOut(BaseModel):
    """CON-04 播报载荷：一句确认话术 + 可选音频地址。

    `degraded=True`（等价于 `audio_url is None`）就是「TTS 这次没合成出来」的完整表达：
    前端据此只上 `subtitle` 大字，**不影响**确认本身已经落库——播报是增强项。
    """

    action: str
    text: str
    subtitle: str
    audio_url: str | None
    degraded: bool


class PadMedicationConfirmOut(BaseModel):
    """长者点「已服用」后的回执：只回状态，不回处方细节。"""

    reminder_id: str
    status: str
    terminal_state: str | None
    #: CON-04 确认播报；TTS 不可用时 `degraded=True` 且 `audio_url=None`，确认照样成功。
    announcement: PadMedicationAnnouncementOut | None = None


@router.post(
    "/medications/{reminder_id}/confirm",
    response_model=PadMedicationConfirmOut,
    summary="长者点「已服用」（FR-UI-03；终态 taken_reported = 自报，非核实服药）",
    dependencies=[Depends(_touch_device_heartbeat)],
)
async def confirm_medication(
    reminder_id: uuid.UUID,
    identity: Annotated[Identity, Depends(current_device)],
    session: Annotated[AsyncSession, Depends(get_session)],
    tts: Annotated[TTSProvider, Depends(get_tts)],
) -> PadMedicationConfirmOut | JSONResponse:
    """长者端确认「已服用」。

    为什么要有这个端点，而不是复用 `/api/medication/events/{id}/confirm`：
    那个入口的角色门禁是 `doctor / care_worker / admin`（照护台代确认），长者设备
    拿的是 `kind=device` 凭证，调它必然 403。**自报与代确认是两种口径**，不能靠放开
    角色混成一条：`triggered_by` / `confirm_channel` 要能区分「长者自己点的」与
    「护工打电话确认的」，审计与 AC-08 的迟到判定都依赖这个区别。

    四条边界：

    1. **只认设备凭证**：`current_device` 先把非 device 主体判 401，避免拿员工令牌
       在这里冒充长者自报。
    2. **未绑定 → 403**：没有绑定长者就没有「我的用药」，不能返回空 200。
    3. **跨长者 → 403**：`reminder_id` 是客户端传的，必须与设备绑定的长者比对，
       否则改个 UUID 就能替别人确认。
    4. **状态机冲突 → 409**：已终态的事件（如已升级 `missed_unconfirmed`）不能再被
       自报覆盖，冲突由服务层抛出后如实回 409，不吞成 200。

    CON-04 播报：确认**先落库再合成语音**——长者点下去已经是既成事实，语音只是把
    「已记下」说给他听。所以合成放在 `commit()` 之后，且整段再兜一层 `try`：TTS 任何
    意外都只把响应降级成 `announcement.degraded=True`（前端上大字字幕），绝不让
    「长者的确认因为机器没合成出声音而失败」。
    """
    if identity.elder_id is None:
        return _error_response(403, "device_not_bound", "device is not bound to an elder")

    try:
        event = await medication_service.load_event(session, reminder_id)
    except MedicationServiceError:
        return _error_response(404, "reminder_not_found", "reminder not found")

    # `Identity.elder_id` 是规范化的 UUID **文本**，`event.elder_id` 是 `uuid.UUID`；
    # 直接比会恒不相等（最后一次运行期验证就踩了这个坑：长者自己的提醒被判成
    # 「别人的」）。统一转文本再比。
    if str(event.elder_id) != identity.elder_id:
        return _error_response(403, "forbidden", "reminder belongs to another elder")

    try:
        # `triggered_by` 的服务层白名单只有 ('scheduler', 'dialog')：容器里实测传
        # `elder_pad` 会 409。渠道已由 `channel='button'` 记下「谁点的、从哪种媒介」，
        # 再争一个 triggered_by 值就要动服务层契约与库表 CHECK，不值当。
        updated, _hitl_resumed = await medication_service.confirm_event(
            session,
            event=event,
            channel="button",
            triggered_by=None,
        )
    except MedicationServiceError as exc:
        return _error_response(409, "state_conflict", str(exc))

    await session.commit()
    logger.info("pad medication confirmed reminder_id=%s status=%s", reminder_id, updated.status)

    snapshot = event.drug_snapshot or {}
    announcement: PadMedicationAnnouncementOut | None = None
    try:
        spoken = await announce_medication(
            tts,
            "taken",
            med_name=str(snapshot.get("drug_name") or "") or None,
            trace_id=get_trace_id() or "-",
        )
        announcement = PadMedicationAnnouncementOut(
            action=spoken.action,
            text=spoken.text,
            subtitle=spoken.subtitle,
            audio_url=spoken.audio_url,
            degraded=spoken.degraded,
        )
    except Exception:  # 播报是增强项：连「话术表写错」都不许回滚已经成功的确认
        logger.warning(
            "pad medication announcement skipped reminder_id=%s", reminder_id, exc_info=True
        )

    return PadMedicationConfirmOut(
        reminder_id=str(reminder_id),
        status=updated.status,
        terminal_state=updated.terminal_state,
        announcement=announcement,
    )


class PadMedicationUndoOut(BaseModel):
    """撤回「已服用」后的回执。`terminal_state` 回 `None` 就是本次撤销的证明。"""

    reminder_id: str
    status: str
    terminal_state: str | None
    undone_at: str


#: 撤销确认的审计动作（与照护台的 `medication.event.confirm` 同族，便于一条查询里连起来看）。
ACTION_MEDICATION_UNDO = "medication.event.undo"

#: 审计里的资源类型按表名写（与 `app/api/media.py` 的 `media_library` 同一口径）。
MEDICATION_EVENT_RESOURCE = "medication_events"


def _device_actor_id(identity: Identity) -> uuid.UUID:
    """审计里的 `actor_id`：设备主体取 `device_id`；坏值退成全零占位，不让 409 变 500。"""
    raw = identity.staff_id or identity.device_id
    if not raw:
        return NIL_RESOURCE_ID
    try:
        return uuid.UUID(raw)
    except ValueError:  # 身份行里的 ID 坏了：留占位，比因为审计失败而 500 要好
        return NIL_RESOURCE_ID


@router.post(
    "/medications/{reminder_id}/undo",
    response_model=PadMedicationUndoOut,
    summary="撤回刚刚的「已服用」（FR-UI-08；10 秒内、仅 taken_reported）",
    dependencies=[Depends(_touch_device_heartbeat)],
)
async def undo_medication(
    reminder_id: uuid.UUID,
    identity: Annotated[Identity, Depends(current_device)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PadMedicationUndoOut | JSONResponse:
    """长者点「撤销」把刚才那次自报撤回，事件退回「等回应」。

    与 `/confirm` 是逆操作，四条边界同源，多两条：

    1. **只认设备凭证**（`current_device` 先把非 device 主体判 401）；
    2. **未绑定 → 403**；
    3. **跨长者 → 403**（`reminder_id` 是客户端传的，必须与设备绑定的长者比对）；
    4. **可撤性 → 409**：只有终态还是 `taken_reported`、且距 `confirmed_at` 不超过
       10 秒的才放行。拒服 / 未确认 / 触达失败 / 补确认都是「已收口的事实」，
       撤回它们等于改写历史，一律 409 让护工在照护台处理（口径见
       `medication_service.REOPENABLE_TERMINAL_STATES`）。

    **不撤回告警侧**：`risk_events` / `work_orders` / 家庭通知原样保留。长者一次
    误点不该抹掉一条已经发出去的安全告警；本端点只重置用药侧状态并留审计。

    审计：`medication.event.undo`（同事务写入，与业务改动一起提交或一起回滚），
    detail 里带撤销前的终态 / 确认渠道 / 确认时刻，事后能回答「谁在什么时候撤了哪次」。
    """
    if identity.elder_id is None:
        return _error_response(403, "device_not_bound", "device is not bound to an elder")

    try:
        event = await medication_service.load_event(session, reminder_id)
    except MedicationServiceError:
        return _error_response(404, "reminder_not_found", "reminder not found")

    # 与 `/confirm` 同一个坑：`Identity.elder_id` 是文本、`event.elder_id` 是 UUID，
    # 统一转文本再比，否则长者自己的提醒会被判成「别人的」。
    if str(event.elder_id) != identity.elder_id:
        return _error_response(403, "forbidden", "reminder belongs to another elder")

    prior_terminal = event.terminal_state
    prior_channel = event.confirm_channel
    prior_confirmed_at = event.confirmed_at

    try:
        reopened = await medication_service.reopen_event(session, event=event, reason="pad_undo")
    except MedicationServiceError as exc:
        return _error_response(409, "state_conflict", str(exc))

    await audit_service.write_audit(
        session,
        actor_type=identity.kind,
        actor_id=_device_actor_id(identity),
        action=ACTION_MEDICATION_UNDO,
        resource_type=MEDICATION_EVENT_RESOURCE,
        resource_id=reminder_id,
        detail={
            "prior_terminal_state": prior_terminal,
            "prior_confirm_channel": prior_channel,
            "prior_confirmed_at": prior_confirmed_at.isoformat() if prior_confirmed_at else None,
            "window_seconds": medication_service.UNDO_WINDOW_SECONDS,
            "elder_id": identity.elder_id,
        },
    )

    await session.commit()
    logger.info(
        "pad medication confirmation undone reminder_id=%s prior_terminal=%s",
        reminder_id,
        prior_terminal,
    )
    return PadMedicationUndoOut(
        reminder_id=str(reminder_id),
        status=reopened.status,
        terminal_state=reopened.terminal_state,
        undone_at=datetime.datetime.now(datetime.UTC).isoformat(),
    )


class PadMedicationRenderedOut(BaseModel):
    """卡片渲染回执（FR-UI-10）。

    `receipt` 是这一枪触达在库里**最终**的回执值：本次调用成功登记时是
    `card_rendered`；若该枪此前已被服务端记为 `tts_finished`，则保留更强的既有证据
    （见 `medication_service._fill_attempt` 的「只补空、不覆盖」），这里如实回显。
    """

    reminder_id: str
    attempt_no: int
    receipt: str


#: 客户端回执的取值（PRD line 314 / `medication_service.ATTEMPT_RECEIPTS`）。
RECEIPT_CARD_RENDERED = "card_rendered"


@router.post(
    "/medications/{reminder_id}/rendered",
    response_model=PadMedicationRenderedOut,
    summary="卡片已在平板渲染完成（FR-UI-10；card_rendered 回执）",
    dependencies=[Depends(_touch_device_heartbeat)],
)
async def rendered_medication_card(
    reminder_id: uuid.UUID,
    identity: Annotated[Identity, Depends(current_device)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PadMedicationRenderedOut | JSONResponse:
    """长者端「这张提醒卡真的画到他眼前了」的客户端回执。

    为什么必须由客户端发这一枪（PRD FR-ALT-03「只有客户端回执算送达」）：
    服务端记的 `tts_finished` 只证明音频播完，证明不了长者眼前有这张卡——TTS 走
    WebRTC 时，页面白屏/被切走、JS 崩了，声音照样能播完。所以「卡片已渲染」这条
    证据只能由渲染方给出，不能由发送方推断。

    四条边界与 `/confirm` 同源：

    1. **只认设备凭证**：`current_device` 先把非 device 主体判 401；
    2. **未绑定 → 403**：没有绑定长者就没有「我的用药」，不回空 200；
    3. **跨长者 → 403**：`reminder_id` 是客户端传的，必须与设备绑定的长者比对；
    4. **幂等**：回执按 `(event_id, attempt_no)` 记，`attempt_no` 由服务端的
       `retry_count + 1` 推出（**不信任客户端传第几枪**）。同一枪重复上报只补空字段，
       不会多出一行；已记为 `tts_finished` 的那一枪保留原值。

    `sent_at` 只在「这一枪还没有 attempt 行」时使用（例如卡片先渲染、调度写库稍后到）；
    已有行时服务层忽略它，不会把既有的发出时刻改掉。
    """
    if identity.elder_id is None:
        return _error_response(403, "device_not_bound", "device is not bound to an elder")

    try:
        event = await medication_service.load_event(session, reminder_id)
    except MedicationServiceError:
        return _error_response(404, "reminder_not_found", "reminder not found")

    # 与 `/confirm` / `/undo` 同一个坑：`Identity.elder_id` 是文本、`event.elder_id` 是 UUID。
    if str(event.elder_id) != identity.elder_id:
        return _error_response(403, "forbidden", "reminder belongs to another elder")

    # 第几枪由服务端状态推：`retry_count` 在每次触达后 +1（调度侧 attempt_no = retry_count + 1），
    # 客户端只说「卡片画到了」，说不了也不该说「这是第几次」。
    attempt_no = event.retry_count + 1
    try:
        attempt = await medication_service.record_attempt(
            session,
            event_id=event.reminder_id,
            attempt_no=attempt_no,
            sent_at=datetime.datetime.now(datetime.UTC),
            receipt=RECEIPT_CARD_RENDERED,
        )
    except MedicationServiceError as exc:
        # attempt_no 越界（>4）等：如实回 409，不静默吞成 200 —— 否则「已送达」
        # 会记到一个不存在的枪上，事后对不上 AC-20 的送达判定。
        return _error_response(409, "state_conflict", str(exc))

    await session.commit()
    logger.info(
        "pad medication card rendered reminder_id=%s attempt_no=%s receipt=%s",
        reminder_id,
        attempt.attempt_no,
        attempt.receipt,
    )
    return PadMedicationRenderedOut(
        reminder_id=str(reminder_id),
        attempt_no=attempt.attempt_no,
        receipt=attempt.receipt or "none",
    )


__all__ = [
    "router",
]
