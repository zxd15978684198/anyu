"""用药领域服务（PRD §4.5 / FR-MED-01..09，技术设计文档 §3.4 + §7.1，迭代 7 · C 卡）。

分层位置：`graphs | scheduler | api → services → models`（AGENTS §2.1 第 1 条）。
**唯一写库层**：用药事件、触达尝试、升级工单三张表的写入全部收在这里；
`medication_confirmation_graph` 与后续调度器只调本模块的 async 函数，不直接碰 ORM。

## 幂等三键（PRD FR-MED-04 + AGENTS §8「幂等」条）

| 键 | 落点 | 语义 |
| --- | --- | --- |
| `(plan_id, scheduled_at)` | `uq_medication_events_plan_id_scheduled_at` | 同一计划同一到点时刻只有一条事件 |
| `payload_hash` | `medication_events.payload_hash` | 同键同内容 → 原行 + `False`；同键异内容 → 409 |
| `(event_id, attempt_no)` | `uq_medication_attempts_event_id_attempt_no` | 同一次触达只记一行 |

## 为什么「先查后插」还要接 `IntegrityError`

「先查」覆盖绝大多数重复调用（调度器重放、语音里又说一次「我要吃药」）；
「接 `IntegrityError`」覆盖并发窗口（两个请求同时查不到、同时插）。唯一键冲突
在这里是**预期路径**而不是 bug：回滚 savepoint → 重查 → 按同内容/异内容分流。
用 `begin_nested()` 隔出 savepoint，避免把调用方事务（API 的 `get_session`、
图节点的 `async with session_factory()`）整条炸掉。

## 事务边界

本模块只 `flush` 不 `commit`，事务归调用方——与 `health_service` / `notification_service`
既有口径一致。图节点每次自己开一个 session 并 commit，是为了让「业务事实落库」
不依赖图状态是否还能续跑（TD §7.1：图状态只做流程编排，业务事实在业务表）。

## 终态口径（S-D7-C1 自裁）

计划 §W2 的六终态 vs TD §3.4 的四值：**以 TD 列取值为准**，`TERMINAL_MAP` 是唯一
映射表。`cancelled` / `superseded` 不写 `terminal_state`——它们由
`medication_plans.status='stopped'` 与事件 `status` 表达，硬塞进四值会污染
「这一次到点到底有没有确认」的判断（AC-08 补确认要靠它判定是否覆盖）。

## 偏离登记

**S-D7-C2（工单流转取值）**：任务卡要求升级时同事务写
`work_order_transitions(from_status='new', to_status='escalated')`，但迁移
`alembic/versions/0009_memory_medication.py` 与 ORM（`app/models/work_order.py`
`_STATUS_IN`）把 `from_status` / `to_status` **都**限死在六态
`new / acknowledged / in_progress / resolved / closed / reopened`；`escalated`
只存在于 `medication_events.status` 与 `risk_events.status`。写 `'escalated'`
必撞 `ck_work_order_transitions_to_status`。故本实现：

1. 建单 `status='new'`（任务卡原文，也是 `work_orders.status` 默认值）——红线漏服
   工单必须停在照护台「待接手」队列里，**绝不能**自造成 `acknowledged`，否则四次
   未响应的单子看起来「已被接手」，属于安全回归（AC-06 要的是**人**去处理）；
2. 不写 transition：工单状态没变就没有状态流转可留痕（ORM 契约：「每次改状态
   同事务写 transitions」），升级事实落在 `work_orders.note` + 事件
   `status='escalated'` / `terminal_state`；
3. 待 schema 增加 `'escalated'`（新迁移，不改 0009 历史迁移）后，在此补一行
   `WorkOrderTransition(from_status='new', to_status='escalated')` 即可。
"""

from __future__ import annotations

import csv
import datetime
import hashlib
import io
import json
import logging
import re
import uuid
import xml.etree.ElementTree as ET
import zipfile
import zoneinfo
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, cast

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import (
    Elder,
    ElderStaffAssignment,
    MedicationAttempt,
    MedicationEvent,
    MedicationPlan,
    Notification,
    RiskEvent,
    WorkOrder,
)
from app.services import work_order_service

logger = logging.getLogger(__name__)

SHANGHAI = zoneinfo.ZoneInfo("Asia/Shanghai")
"""展示/单号日期用的时区（存储一律 UTC，AGENTS §8「时间」）。"""

EVENT_STATUSES: Final[tuple[str, ...]] = ("calling", "waiting_confirm", "confirmed", "escalated")
"""`medication_events.status` 四态（TD §3.4，迁移 0009 的 CHECK 同款）。"""

TERMINAL_STATES = (
    "taken_reported",
    "refused",
    "missed_unconfirmed",
    "notification_failed",
    "late_taken_reported",
)
"""`medication_events.terminal_state` 五值闭集（TD §3.4 四值 + 迁移 0019 补的第五值）。

第五值只增不改：存量四值的语义与判定一个不动，`late_taken_reported` 专门表达
「这件事当时已经按未服/未确认收口，后来长者又说吃了」——记成 `taken_reported`
会把「当时没确认、事后追认」这段事实抹掉（MED-03）。
"""

TERMINAL_WRITE_STATUSES = ("confirmed", "escalated")
"""允许同时写 `terminal_state` 的事件状态：终态只有在「已确认」或「已升级」时才成立。"""

REPORT_COUNT_STATES: Final[tuple[str, ...]] = TERMINAL_STATES[:4]
"""日报 `medication.counts` 的四个计数键（PRD FR-RPT-01 的四个终态口径）。

`late_taken_reported` **不单列**：家属日报要回答「今天这几顿吃没吃」，补确认按
`taken_reported` 计入；「当时没确认、事后追认」这层事实留在事件行与
`ai.medication.late_taken` 上，不改变日报块的四个键（口径一改，家属端与
`tests/test_reports_api.py` 的契约都要跟着动，超出 MED-03 的范围）。
"""

FAILURE_REASONS = ("no_answer", "denied", "tts_failed", "offline", "rtc_failed")
"""`medication_events.failure_reason` 五值闭集（迁移 0015 扩了 `rtc_failed`）。

`rtc_failed` 单独成值而不是塞进 `no_answer`：外呼建连失败（ICE 打不通、设备挂着但
SDP 互换没完成）与「响了没人接」是两种排查路径——前者查网络/信令/设备状态，后者查
长者是否在房间；混在一个值里，运维只能靠翻日志区分，PRD FR-MED-06 要的
「建连失败降级」也失去可统计口径。
"""
CONFIRM_CHANNELS = ("voice", "button")
ATTEMPT_RECEIPTS = ("card_rendered", "tts_finished", "none")
TTS_RESULTS = ("ok", "tts_failed")
RESPONSE_CHANNELS = ("voice", "button", "none")
TRIGGERED_BY = ("scheduler", "dialog")
"""双入口（TD §7.1）：定时扫描 vs 语音里主动说「我要吃药」，共用同一张图。"""

TRIGGERED_BY_DIALOG = "dialog"
"""对话入口的来源值：AC-35 要求确认记录能回查「这次是从对话来的」。"""

WORK_ORDER_TYPES = (
    "missed_medication",
    "sensitive_alert",
    "health_alert",
    "device_fault",
    "sos",
    "collection_missed",
    "drug_question",
    "follow_up",
)
"""`work_orders.type` 八值闭集（TD §3.4）。"""

ESCALATION_REASON = "missed_medication"
"""漏服升级固定用这个 type（AC-06/AC-20）。"""

REFUSAL_WORK_ORDER_TYPE = "follow_up"
"""明确拒服后的派单类型：PRD line 321「生成风险事件+护工跟进工单」。"""

REFUSAL_RISK_TYPE = "med_risk"
"""拒服风险事件的 `risk_events.type`（TD §3.4 五值之一，黄色）。"""

ESCALATION_RISK_TYPE = "med_risk"
"""漏服升级风险事件的 `risk_events.type`：与拒服同属用药类，但 severity 取 `red`。

两处各留一个常量而不是共用一个：拒服是「当班跟进」（yellow，AC-07），升级是
「须立即人工」（red，PRD:312 / AC-06）——同类型、不同严重度，用名字把差别写在调用点，
避免以后有人把 severity 也顺手统一掉。
"""

REFUSAL_RECEIPT_CHANNELS = ("voice", "button")
"""拒服回执来源：语音（`wait_confirm` 里说「不吃」）或页面按钮。"""

RETRY_SPACING_MINUTES = 5
"""PRD §4.5 的四次触达节拍：T0 / +5 / +10 / +15 分钟。"""

CONFIRMED_STATUS = "confirmed"
"""确认服药后的事件状态（TD §3.4 四态之一）。"""

CONFIRMED_TERMINAL_STATE = "taken_reported"
"""确认服药的终态：AC-08 的「+5/+10/+15 才确认」同样是 `taken_reported`，另记 `late_taken`。"""

LATE_TAKEN_TERMINAL_STATE = "late_taken_reported"
"""补确认的终态（MED-03）：原终态已是「未服 / 未确认 / 升级」时，事后确认只能追认。

为什么不能覆盖成 `taken_reported`：`refused` / `missed_unconfirmed` /
`notification_failed` 三条都是「当时这件事已经有结论」的事实。补确认把它们改成
「已服自报」，等于把一次漏服洗成按时服药——日报的依从率、拒服跟进工单的起因
全部失去依据。所以补确认只追加「事后又说吃了」这一层信息，不改写当时的结论。
"""

PRIOR_MISSED_TERMINAL_STATES = (
    "refused",
    "missed_unconfirmed",
    "notification_failed",
    "late_taken_reported",
)
"""补确认时会被判为「迟确认」的原终态：拒服、多次触达未确认、触达失败升级、已补确认过。

`taken_reported` 不在里面（重复确认走幂等早退）；`late_taken_reported` 在里面是为了
「第二次补确认」不再退回 `taken_reported`。
"""

REOPENABLE_TERMINAL_STATES: Final[tuple[str, ...]] = (CONFIRMED_TERMINAL_STATE,)
"""可撤回的终态：**只有** `taken_reported`（长者本人点/说的那一次）。

其余四值一律不可撤：`refused`（明确拒服）与 `missed_unconfirmed` /
`notification_failed`（没做到、触达失败）是「当时已经有结论」的事实，
`late_taken_reported`（事后追认）本身就是在为前面那条结论追加信息。
撤回它们等于改写历史——日报依从率、拒服跟进工单的起因都会失去依据。
所以撤回只放开「自己刚点的这次」，别的走人工纠正而不是按钮。
"""

UNDO_WINDOW_SECONDS: Final[int] = 10
"""FR-UI-08「普通确认 10 秒内可撤销」的服务端闸门（秒）。

前端也在倒计时，但**不能只靠前端**：平板断了网、页面挂起再回来，队列里那条撤销
请求可能在几分钟后才到服务器。服务端按 `confirmed_at` 自己算一次，过期就 409，
不让一条迟到请求改写当天已收口的依从记录。
"""

DIALOG_CHANNEL = "voice"
"""对话入口的回应渠道：长者是说出来的，不是点按钮。"""

DIALOG_CONFIRMED = "confirmed"
DIALOG_REFUSED = "refused"
"""对话入口只处理这两种结论（镜像 `medication_confirmation_graph` 的 decision 取值）。

镜像而不是 import：AGENTS §2.1 的依赖方向是 `graphs → services`，服务层反过来 import 图
会把这条边倒过来；两边取值由 `tests/test_medication_failure_reason.py` 里的相等断言钉住。
"""

PENDING_EVENT_STATUSES = ("calling", "waiting_confirm")
"""还等着回应的两种事件状态：`confirmed` / `escalated` 已有结论，不再参与对话确认。"""

DIALOG_NO_PENDING_REPLY = "当前没有待确认的用药提醒，我先把您说的记下来。"
DIALOG_UNHEARD_REPLY = "没听清您是吃过了还是还没吃，也可以点屏幕上的「已服用」。"
DIALOG_CONFIRMED_REPLY = "好的，已经记下您吃过药了。"
DIALOG_REFUSED_REPLY = "好的，已经记下您这会儿不吃，我请护工过来看看。"
"""对话入口的固定回话。不带药名：长者这句话可能在任何药上说出来，措辞跟具体药无关。"""

TERMINAL_MAP: dict[str, str | None] = {
    "confirmed": "taken_reported",
    "refused": "refused",
    "no_response": "missed_unconfirmed",
    "escalated": "missed_unconfirmed",
    "cancelled": None,
    "superseded": None,
}
"""S-D7-C1 终态映射：计划 §W2 的六终态 → TD §3.4 的四值。

| 计划 §W2 | TD `terminal_state` | 备注 |
| --- | --- | --- |
| `confirmed` | `taken_reported` | 长者说吃了/点了「已服用」 |
| `refused` | `refused` | 明确拒服：停止重试，事件置 `escalated` 走人工跟进（AC-07） |
| `no_response` | `missed_unconfirmed` | 至少一次有页面回执但没确认（AC-20） |
| `escalated` | `missed_unconfirmed` | 四次触达用尽，已派单（AC-06） |
| `cancelled` | `None`（不写） | 计划停用等外部原因，由 `plans.status` 表达 |
| `superseded` | `None`（不写） | 计划换版本，由新事件表达 |

`late_taken_reported`（0019 补的第五值）**不在这张表里**：它不是某个业务终态的映射，
而是「已有终态之上再加一层补确认」的追认值，判定在 `_resume_or_write`。
"""


class MedicationServiceError(Exception):
    """用药服务错误基类（与 health/notification 等既有服务的异常口径一致）。"""


class MedicationValidationError(MedicationServiceError, ValueError):
    """参数/枚举取值非法。同时是 `ValueError`，便于按标准异常兜底。"""


class MedicationNotFoundError(MedicationServiceError):
    """按主键找不到用药事件。"""


class MedicationEventConflict(MedicationServiceError):
    """同幂等键、不同内容（PRD FR-MED-04）。API 层映射 409。"""


class MedicationPlanNotFoundError(MedicationNotFoundError):
    """按主键找不到用药计划（`load_plan` / PATCH / stop 用）。API 层映射 404。"""


class MedicationVersionConflict(MedicationServiceError):
    """药单 `expected_version` 与库中版本不符（W1 并发口径）。API 层映射 409。

    错误码 `VERSION_CONFLICT` 与 `MedicationEventConflict` 的 `CONFLICT` 分开：前者是
    「有人先改了这张药单，刷新重来」，后者是「同一到点键写了两份不同内容」，前端处理
    方式不同，混用一个码会让页面只能给一句含糊的「冲突」。
    """


class MedicationPlanAlreadyRenewed(MedicationServiceError):
    """这张药单已经续过方（`renewed_from_plan_id` 已有指向它的子计划）。API 层映射 409。

    续方是一次**开新医嘱**的写操作，重复点一次不该产生第二条药单（那会让同一个
    服药时刻出现两条计划、两套事件键，长者被提醒两次）。幂等键就取
    `renewed_from_plan_id`：一个旧计划最多只有一个直接续方后代。
    """


# ---------------------------------------------------------------------------
# 纯函数：无 I/O，便于单测与图节点复用
# ---------------------------------------------------------------------------


def drug_snapshot_hash(snapshot: Mapping[str, Any]) -> str:
    """药单快照的 sha256：同键（plan_id, scheduled_at）下的「同内容」指纹。

    `sort_keys=True` 让键序无关，`ensure_ascii=False` 让中文按 UTF-8 编码后哈希
    （避免同一份中文药单因转义差异算出两个 hash，误报 409）。
    """
    if not isinstance(snapshot, Mapping):
        raise MedicationValidationError("drug_snapshot 必须是映射（药名/剂量等字段）")
    canonical = json.dumps(dict(snapshot), sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def next_retry_at(
    scheduled_at: datetime.datetime, attempt_no: int, wait_seconds: int
) -> datetime.datetime:
    """第 `attempt_no` 次触达后的下一次触达时刻（UTC）。

    两个下界取较晚者：

    - **节拍档位**：`scheduled_at + 5 × attempt_no` 分钟（PRD §4.5 的 +5/+10/+15）；
    - **本次等待窗**：`scheduled_at + 5 × (attempt_no - 1)` 分钟再等 `wait_seconds` 秒
      （PRD §4.5「每次等 60s」）。等待窗更长时不能被节拍档位提前打断。

    例：`wait_seconds=60` 时 T0 触达 → 下一次 = T0+5min（60s 窗户在 5 分钟档位内结束）。
    """
    if attempt_no < 1:
        raise MedicationValidationError(f"attempt_no 必须 ≥ 1，收到 {attempt_no!r}")
    if wait_seconds < 0:
        raise MedicationValidationError(f"wait_seconds 不能为负，收到 {wait_seconds!r}")
    base = _as_utc(scheduled_at, field="scheduled_at")
    slot = base + datetime.timedelta(minutes=RETRY_SPACING_MINUTES * attempt_no)
    window_end = base + datetime.timedelta(
        minutes=RETRY_SPACING_MINUTES * (attempt_no - 1), seconds=wait_seconds
    )
    return max(slot, window_end)


def terminal_state_for(plan_state: str) -> str | None:
    """计划 §W2 的流程终态 → TD §3.4 的 `terminal_state`（见 `TERMINAL_MAP`）。"""
    key = (plan_state or "").strip().lower()
    if key not in TERMINAL_MAP:
        raise MedicationValidationError(f"未知的流程终态：{plan_state!r}")
    return TERMINAL_MAP[key]


def max_attempts() -> int:
    """触达上限（配置 `medication_reminder_attempts`，默认 4 = T0/+5/+10/+15）。"""
    return int(get_settings().medication_reminder_attempts)


def attempt_wait_seconds() -> int:
    """单次触达的等待秒数（配置 `medication_attempt_wait_seconds`，默认 60）。"""
    return int(get_settings().medication_attempt_wait_seconds)


# 四枪话术的字数上限。TTS 首包预算 300ms（AGENTS §6），一句话说不完就意味着长者要
# 等到第二轮才听见「可以点按钮」；60 字在 1.0x 语速下约 12 秒，刚好压在等待窗内。
REMINDER_SPEECH_MAX_LENGTH: Final[int] = 60

# ⚠️ 与 `app/audio/outbound.RTC_RESULTS` 同一口径的**镜像**：分层约束禁止 services 反向
# import audio（AGENTS §2.1 依赖单向），所以这里只能抄一份字面量，
# `tests/test_medication_failure_reason.py` 断言两边集合相等，防镜像漂移。
OUTBOUND_RTC_RESULTS: Final[tuple[str, ...]] = (
    "ok",
    "rtc_failed",
    "timeout",
    "no_answer",
    "ice_failed",
)

# 四枪渐进：第 1 枪把「吃什么、吃多少、怎么确认」一次说全；后面每枪只留一件事，
# 越晚越短——长者第 4 次听到时还在犹豫的是「按哪里」，不是「吃什么」。
_REMINDER_SPEECH_TEMPLATES: Final[tuple[str, ...]] = (
    "该吃药了。{label}。吃完跟我说一声「吃了」，也可以点屏幕上的「已服用」。",
    "{label}，该吃了。吃完点「已服用」。",
    "该吃{label}了，吃完点「已服用」。",
    "{drug}还没吃？点「已服用」。",
)
"""第 1..4 枪的话术模板（顺序即 `attempt_no`；`{label}` = 药名+剂量，`{drug}` = 药名）。"""

_DIAGNOSIS_TERMS: Final[tuple[str, ...]] = (
    "高血压",
    "低血压",
    "糖尿病",
    "冠心病",
    "心梗",
    "脑梗",
    "中风",
)
"""话术里绝不能出现的诊断词（AGENTS §4 禁区 4：不说医疗诊断口径）。"""


def _snapshot_text(snapshot: Mapping[str, Any], field: str) -> str:
    """取快照里的一个文本字段；缺失/空串一律报校验错（不拿空药名去播报）。"""
    value = snapshot.get(field)
    if not isinstance(value, str) or not value.strip():
        raise MedicationValidationError(f"drug_snapshot.{field} 必须是非空字符串，收到 {value!r}")
    return value.strip()


def reminder_speech_text(drug_snapshot: Mapping[str, Any], attempt_no: int) -> str:
    """第 `attempt_no` 枪的播报话术（PRD §4.5 渐进提醒，FR-MED-06 的 TTS 文案）。

    为什么话术放在服务层而不是调度层：同一句话既是外呼 TTS 的文本，也是降级到 WS 页面
    卡片时显示的正文（`card_rendered`），两路必须逐字一致，否则长者听到的和看到的对不上。
    且话术只用 `drug_name` / `dose` 两个快照字段——它们来自建事件时的药单快照，
    不实时回查药单（PRD FR-MED-04 的不可变口径）。
    """
    upper = max_attempts()
    if isinstance(attempt_no, bool) or not isinstance(attempt_no, int):
        raise MedicationValidationError(f"attempt_no 必须是整数，收到 {attempt_no!r}")
    if not 1 <= attempt_no <= upper:
        raise MedicationValidationError(f"attempt_no 必须在 1..{upper}，收到 {attempt_no!r}")
    if not isinstance(drug_snapshot, Mapping):
        raise MedicationValidationError("drug_snapshot 必须是映射（药名/剂量等字段）")

    drug = _snapshot_text(drug_snapshot, "drug_name")
    dose_raw = drug_snapshot.get("dose")
    dose = dose_raw.strip() if isinstance(dose_raw, str) else ""

    # 模板只有 4 条，而配置允许把触达次数改到 4 以上（`medication_reminder_attempts`）；
    # 多出来的枪沿用最短的第 4 条，重说一遍比抛错更符合「提醒」这个动作。
    template = _REMINDER_SPEECH_TEMPLATES[min(attempt_no, len(_REMINDER_SPEECH_TEMPLATES)) - 1]
    text = _render_speech(template, drug=drug, dose=dose)
    if len(text) <= REMINDER_SPEECH_MAX_LENGTH:
        return text

    # 药名最长 100 字（`MAX_DRUG_NAME_LENGTH`），复方药在导入文件里能写成一整行。
    # 超长时只截药名、保留剂量与「已服用」指引：少几个字远好过丢掉确认入口。
    repetitions = max(text.count(drug), 1)
    room = REMINDER_SPEECH_MAX_LENGTH - (len(text) - repetitions * len(drug))
    if room >= 3:
        shortened = _render_speech(template, drug=f"{drug[: room - 1]}…", dose=dose)
        if len(shortened) <= REMINDER_SPEECH_MAX_LENGTH:
            return shortened
    return text[:REMINDER_SPEECH_MAX_LENGTH]  # 走到这里说明模板自己就超了 60 字


def _render_speech(template: str, *, drug: str, dose: str) -> str:
    """把药名/剂量填进模板（剂量为空时不留悬空的逗号）。"""
    label = f"{drug}，{dose}" if dose else drug
    return template.format(label=label, drug=drug)


def failure_reason_for_outbound(
    rtc_result: str,
    *,
    degraded_text_sent: bool = False,
    device_offline: bool = False,
) -> str | None:
    """一次外呼的 `rtc_result` → `medication_events.failure_reason`（计划 §2.4 归因表）。

    顺序即优先级：设备掉线是**已经知道的原因**，压过任何建连失败码；建连通了就不该有
    失败原因，哪怕降级文本也发过（那是成功路径里的补偿）。
    """
    if rtc_result not in OUTBOUND_RTC_RESULTS:
        raise MedicationValidationError(
            f"rtc_result 只能是 {OUTBOUND_RTC_RESULTS}，收到 {rtc_result!r}"
        )
    if device_offline:
        return "offline"
    if rtc_result == "ok":
        return None
    if degraded_text_sent:
        # 文本卡片送到了、语音这一路没送到：归 TTS/播放侧，而不是「建连失败」。
        return "tts_failed"
    return "rtc_failed"


def escalation_attribution(receipts: Sequence[str | None]) -> tuple[str, str]:
    """升级时的 `(terminal_state, failure_reason)`：只由**已落库的触达回执**决定（PRD:323-324）。

    为什么用回执而不是外呼结果：`medication_attempts` 没有 `rtc_result` 列，一次外呼是
    建连失败、ICE 失败还是没人接，落库时已经压成了一个 `receipt`；升级发生在 60 秒窗
    关闭之后，拿不到当时的外呼对象。回执是此刻唯一可靠、且已经在库里的证据。

    - **四次触达一次回执都没有**（全 `none`/`None`）：PRD:324「四次均无回执」→
      `notification_failed` + `rtc_failed`。绝不能落 `missed_unconfirmed`——那等于
      断言长者「收到了却没确认」，把人没听见说成人没回应，是诬告。
      空列表（调度器停机错过整个窗口、一枪都没打）走同一分支：**没有送达回执**就是
      事实，没打出去的那一枪不该被记成「有人收到」；`failure_reason` 在五值里只能取
      唯一不自称「人没接」/「设备离线」的 `rtc_failed`（`no_answer` 会诬告长者、
      `offline` 需要设备状态证据、`denied` 需要明确拒服）。
    - **至少一次有回执但无人确认**：PRD:323「有回执、无响应」→ `missed_unconfirmed`
      + `no_answer`（与 `medication_confirmation_graph` 的超时口径逐字一致）。

    `offline` **故意不在这里产出**：调度器拿不到「此刻设备是否离线」的可靠信号
    （`devices.status` 只在解绑/重置时写，WS 断连不更新），按它判离线会把外呼失败
    错记成设备故障。设备离线归因只由 `notification_service.recover_after_offline`
    那条**已知离线**的路径写（PRD FR-MED-08）。
    """
    received = ("card_rendered", "tts_finished")
    if any(receipt in received for receipt in receipts):
        return "missed_unconfirmed", "no_answer"
    return "notification_failed", "rtc_failed"


# ---------------------------------------------------------------------------
# 写库：事件
# ---------------------------------------------------------------------------


async def ensure_event(
    session: AsyncSession,
    *,
    plan_id: uuid.UUID,
    elder_id: uuid.UUID,
    scheduled_at: datetime.datetime,
    drug_snapshot: Mapping[str, Any],
    triggered_by: str = "scheduler",
) -> tuple[MedicationEvent, bool]:
    """幂等创建用药事件；返回 `(事件, 是否新建)`。

    - 同键同 `payload_hash` → 返回原事件 + `False`（重放安全，PRD FR-MED-04）；
    - 同键异 `payload_hash` → `MedicationEventConflict`（API 映射 409）；
    - 新键 → 插入并返回 `True`。

    并发安全靠 `begin_nested()` + `IntegrityError`：撞唯一键时回滚 savepoint、
    重查后再走同一条分流逻辑，不会把调用方事务炸掉。
    """
    if triggered_by not in TRIGGERED_BY:
        raise MedicationValidationError(
            f"triggered_by 只能是 {TRIGGERED_BY}，收到 {triggered_by!r}"
        )
    payload_hash = drug_snapshot_hash(drug_snapshot)
    scheduled = _as_utc(scheduled_at, field="scheduled_at")

    existing = await _find_event(session, plan_id=plan_id, scheduled_at=scheduled)
    if existing is not None:
        return _reuse_event(
            existing, elder_id=elder_id, payload_hash=payload_hash, triggered_by=triggered_by
        )

    event = MedicationEvent(
        reminder_id=uuid.uuid4(),
        plan_id=plan_id,
        elder_id=elder_id,
        scheduled_at=scheduled,
        drug_snapshot=dict(drug_snapshot),
        payload_hash=payload_hash,
        triggered_by=triggered_by,
        status="calling",
        terminal_state=None,
        late_taken=False,
        retry_count=0,
    )
    try:
        async with session.begin_nested():
            session.add(event)
    except IntegrityError:
        _expunge_quietly(session, event)
        existing = await _find_event(session, plan_id=plan_id, scheduled_at=scheduled)
        if existing is None:
            raise MedicationServiceError(
                f"用药事件唯一键冲突后重查不到行：plan_id={plan_id} scheduled_at={scheduled.isoformat()}"
            ) from None
        return _reuse_event(
            existing, elder_id=elder_id, payload_hash=payload_hash, triggered_by=triggered_by
        )

    await session.refresh(event)
    logger.info(
        "medication event created reminder_id=%s plan_id=%s scheduled_at=%s triggered_by=%s",
        event.reminder_id,
        plan_id,
        scheduled.isoformat(),
        triggered_by,
    )
    return event, True


async def load_event(session: AsyncSession, reminder_id: uuid.UUID) -> MedicationEvent:
    """按主键取事件（图节点与 API 共用；找不到抛 `MedicationNotFoundError`）。"""
    event = await session.get(MedicationEvent, reminder_id)
    if event is None:
        raise MedicationNotFoundError(f"用药事件不存在：reminder_id={reminder_id}")
    return event


async def update_event_state(
    session: AsyncSession,
    *,
    reminder_id: uuid.UUID,
    status: str | None = None,
    terminal_state: str | None = None,
    failure_reason: str | None = None,
    retry_count: int | None = None,
    confirmed_at: datetime.datetime | None = None,
    confirm_channel: str | None = None,
    graph_thread_id: str | None = None,
    late_taken: bool | None = None,
) -> MedicationEvent:
    """只改传入的字段（`None` 表示「本次不动这一列」）。

    `terminal_state` 必须与 `status ∈ {confirmed, escalated}` 同时成立：
    终态是「这件事结束了」的断言，只允许在确认或升级时落下，否则会出现
    「还在 calling 却已终态」的自相矛盾行（AC-08 补确认要靠它判是否覆盖）。
    """
    event = await load_event(session, reminder_id)

    if status is not None and status not in EVENT_STATUSES:
        raise MedicationValidationError(f"status 只能是 {EVENT_STATUSES}，收到 {status!r}")
    if failure_reason is not None and failure_reason not in FAILURE_REASONS:
        raise MedicationValidationError(
            f"failure_reason 只能是 {FAILURE_REASONS}，收到 {failure_reason!r}"
        )
    if confirm_channel is not None and confirm_channel not in CONFIRM_CHANNELS:
        raise MedicationValidationError(
            f"confirm_channel 只能是 {CONFIRM_CHANNELS}，收到 {confirm_channel!r}"
        )
    if retry_count is not None:
        upper = max_attempts() - 1
        if (
            not isinstance(retry_count, int)
            or isinstance(retry_count, bool)
            or not 0 <= retry_count <= upper
        ):
            raise MedicationValidationError(f"retry_count 必须在 0..{upper}，收到 {retry_count!r}")
    if terminal_state is not None:
        if terminal_state not in TERMINAL_STATES:
            raise MedicationValidationError(
                f"terminal_state 只能是 {TERMINAL_STATES}，收到 {terminal_state!r}"
            )
        effective_status = status if status is not None else event.status
        if effective_status not in TERMINAL_WRITE_STATUSES:
            raise MedicationValidationError(
                f"写 terminal_state 要求 status ∈ {TERMINAL_WRITE_STATUSES}，"
                f"当前事件 status={effective_status!r}（本次传入 {status!r}）"
            )

    if status is not None:
        event.status = status
    if terminal_state is not None:
        event.terminal_state = terminal_state
    if failure_reason is not None:
        event.failure_reason = failure_reason
    if retry_count is not None:
        event.retry_count = retry_count
    if confirmed_at is not None:
        event.confirmed_at = _as_utc(confirmed_at, field="confirmed_at")
    if confirm_channel is not None:
        event.confirm_channel = confirm_channel
    if graph_thread_id is not None:
        event.graph_thread_id = graph_thread_id
    if late_taken is not None:
        event.late_taken = bool(late_taken)

    await session.flush()
    return event


# ---------------------------------------------------------------------------
# 写库：触达尝试
# ---------------------------------------------------------------------------


async def record_attempt(
    session: AsyncSession,
    *,
    event_id: uuid.UUID,
    attempt_no: int,
    sent_at: datetime.datetime,
    receipt: str | None = None,
    tts_result: str | None = None,
    response: str | None = None,
    response_channel: str | None = None,
) -> MedicationAttempt:
    """幂等记录一次触达（键 `(event_id, attempt_no)`，PRD FR-MED-04）。

    重复调用返回已有行，并用本次传入的非空值**补空字段**（`receipt` /
    `tts_result` / `response` / `response_channel`），不覆盖已有内容——出站触达与
    「长者回应」可能分两次到达同一条 attempt 上（图里先 record 后补 response）。
    """
    if not isinstance(attempt_no, int) or isinstance(attempt_no, bool):
        raise MedicationValidationError(f"attempt_no 必须是整数，收到 {attempt_no!r}")
    upper = max_attempts()
    if not 1 <= attempt_no <= upper:
        raise MedicationValidationError(f"attempt_no 必须在 1..{upper}，收到 {attempt_no!r}")
    _check_optional(receipt, ATTEMPT_RECEIPTS, field="receipt")
    _check_optional(tts_result, TTS_RESULTS, field="tts_result")
    _check_optional(response_channel, RESPONSE_CHANNELS, field="response_channel")
    sent = _as_utc(sent_at, field="sent_at")

    existing = await _find_attempt(session, event_id=event_id, attempt_no=attempt_no)
    if existing is not None:
        return await _fill_attempt(
            session,
            existing,
            receipt=receipt,
            tts_result=tts_result,
            response=response,
            response_channel=response_channel,
        )

    attempt = MedicationAttempt(
        attempt_id=uuid.uuid4(),
        event_id=event_id,
        attempt_no=attempt_no,
        sent_at=sent,
        receipt=receipt,
        tts_result=tts_result,
        response=response,
        response_channel=response_channel,
    )
    try:
        async with session.begin_nested():
            session.add(attempt)
    except IntegrityError:
        _expunge_quietly(session, attempt)
        existing = await _find_attempt(session, event_id=event_id, attempt_no=attempt_no)
        if existing is None:
            raise MedicationNotFoundError(f"用药触达外键失败：event_id={event_id} 不存在") from None
        return await _fill_attempt(
            session,
            existing,
            receipt=receipt,
            tts_result=tts_result,
            response=response,
            response_channel=response_channel,
        )

    logger.info("medication attempt recorded event_id=%s attempt_no=%s", event_id, attempt_no)
    return attempt


# ---------------------------------------------------------------------------
# 写库：升级工单（安全网）
# ---------------------------------------------------------------------------


async def escalate_to_work_order(
    session: AsyncSession,
    *,
    event: MedicationEvent,
    reason: str = ESCALATION_REASON,
) -> WorkOrder:
    """四次触达都没拿到有效回应 → 建红线风险事件 + 红线工单，事件进升级终态（AC-06）。

    PRD:312 原文是「T+15 第三次重试 + 终态分流，**生成风险事件和工单**」，AC-06 又要求
    「唯一事件工单」——所以这里与拒服路径（`refuse_event`）同形，落两条人工链路：

    1. `risk_events`（`med_risk` / **red** / `open`），`source_type='medication_events'`、
       `source_id=reminder_id`，可从风险事件反查是哪一次提醒；比拒服多一档 severity，
       因为漏服升级是「须立即人工」（红单），不是「当班跟进」。
    2. `work_orders`（`missed_medication` / `red` / `new`），`event_id` 挂在上面的风险
       事件上——`work_orders.event_id` 在 TD §3.4 指向 `risk_events.event_id`，
       **不是** `medication_events.reminder_id`；挂了外键追溯就不依赖 note 文本。

    为什么必须补风险事件、而不是只建工单：风险事件是「发生了什么」（事实），工单是
    「谁去处理」。没有事实行，照护台的风险面板看不见这条红线事件、24h 未闭环也无从升级。

    - **单号唯一冲突重试一次**：`order_no` 是 `WO-{上海日期}-{当日序号:04d}`，
      并发生成时会撞 `uq_work_orders_order_no`，重算序号再插一次即可（任务卡要求）。
    - **不写 `work_order_transitions`**：见模块文档 S-D7-C2——工单状态没变
      （建出来就是 `new`，等护工接手），且 `'escalated'` 不在该表六态 CHECK 内。
    - **幂等**：同一事件重复升级不重复建——风险事件按 `(source_type, source_id)` 认领，
      工单按 `event_id` 认领；重放时返回已有工单，事件字段照旧重写（终态相同）。
    """
    if reason not in WORK_ORDER_TYPES:
        raise MedicationValidationError(f"工单类型只能是 {WORK_ORDER_TYPES}，收到 {reason!r}")

    # 先写事件字段再找/建下游：重放时也要把终态写回去（与服务层「只改传入字段」同源），
    # 而且 `_escalate_event` 随后会按真实回执覆盖 `terminal_state`，这里只是默认值。
    event.status = "escalated"
    event.terminal_state = "missed_unconfirmed"

    risk = await _find_risk_event(session, event)
    if risk is None:
        risk = RiskEvent(
            event_id=uuid.uuid4(),
            elder_id=event.elder_id,
            type=ESCALATION_RISK_TYPE,
            severity="red",
            source_type="medication_events",
            source_id=event.reminder_id,
            status="open",
        )
        session.add(risk)
        await session.flush()

    existing = await session.scalar(select(WorkOrder).where(WorkOrder.event_id == risk.event_id))
    if existing is not None:
        await session.flush()
        logger.info(
            "medication escalation reused reminder_id=%s order_no=%s risk_event_id=%s",
            event.reminder_id,
            existing.order_no,
            risk.event_id,
        )
        return existing

    note = (
        f"用药提醒 {event.reminder_id} 四次触达（T0/+5/+10/+15）均未获有效确认，"
        f"系统自动升级派护工跟进；计划 {event.plan_id}，应服时刻 {_iso(event.scheduled_at)}"
    )
    order = await work_order_service.create_work_order(
        session,
        elder_id=event.elder_id,
        type=reason,
        severity="red",
        note=note,
        event_id=risk.event_id,
    )

    await session.flush()
    logger.warning(
        "medication escalated reminder_id=%s order_no=%s risk_event_id=%s elder_id=%s",
        event.reminder_id,
        order.order_no,
        risk.event_id,
        event.elder_id,
    )
    return order


async def refuse_event(
    session: AsyncSession,
    *,
    event: MedicationEvent,
    channel: str | None = None,
) -> WorkOrder:
    """长者明确拒服 → 停止重试，建黄色风险事件 + 护工跟进工单（AC-07 / PRD line 321）。

    PRD 原文：「拒服 | `refused` | **立即停止重试，生成风险事件+护工跟进工单**」。
    因此这里**不是**把事件置终态就结束，必须同时落两条人工链路：

    1. `risk_events`（`med_risk` / `yellow` / `open`），`source_type='medication_events'`、
       `source_id=reminder_id` 便于从风险事件反查是哪一次提醒；
    2. `work_orders`（`follow_up` / `yellow` / `new`），挂在上面的风险事件上等护工跟进。

    **不写 `work_order_transitions`**（同 S-D7-C2：工单建出来就是 `new`，中途没有状态变化）。

    幂等：同一事件重复调用**不重复建单**——风险事件按 `(source_type, source_id)` 认领，
    工单按 `event_id` 认领；重放时直接返回已有工单。事件字段每次都重写一遍（终态相同，幂等）。

    `confirmed_at` 显式置 `None`：拒服不是「确认服药」，留时刻会把统计口径污染成已服。
    """
    _check_optional(channel, REFUSAL_RECEIPT_CHANNELS, field="confirm_channel")

    event.status = "escalated"
    event.terminal_state = terminal_state_for("refused")
    event.failure_reason = "denied"
    event.confirm_channel = channel
    event.confirmed_at = None

    risk = await _find_risk_event(session, event)
    if risk is None:
        risk = RiskEvent(
            event_id=uuid.uuid4(),
            elder_id=event.elder_id,
            type=REFUSAL_RISK_TYPE,
            severity="yellow",
            source_type="medication_events",
            source_id=event.reminder_id,
            status="open",
        )
        session.add(risk)
        await session.flush()

    existing = await session.scalar(select(WorkOrder).where(WorkOrder.event_id == risk.event_id))
    if existing is not None:
        await session.flush()
        logger.info(
            "medication refusal follow-up reused reminder_id=%s order_no=%s",
            event.reminder_id,
            existing.order_no,
        )
        return existing

    channel_text = "语音" if channel == "voice" else "页面按钮"
    note = (
        f"长者明确拒服（{channel_text}），已按 AC-07 停止重试并派护工跟进；"
        f"提醒 {event.reminder_id}，计划 {event.plan_id}，应服时刻 {_iso(event.scheduled_at)}"
    )
    order = await work_order_service.create_work_order(
        session,
        elder_id=event.elder_id,
        type=REFUSAL_WORK_ORDER_TYPE,
        severity="yellow",
        note=note,
        event_id=risk.event_id,
    )
    await session.flush()
    logger.warning(
        "medication refused reminder_id=%s order_no=%s risk_event_id=%s elder_id=%s",
        event.reminder_id,
        order.order_no,
        risk.event_id,
        event.elder_id,
    )
    return order


async def _find_risk_event(session: AsyncSession, event: MedicationEvent) -> RiskEvent | None:
    """按 `(source_type, source_id)` 找回该提醒已建的风险事件（拒服与升级共用的幂等认领）。"""
    return await session.scalar(
        select(RiskEvent).where(
            RiskEvent.source_type == "medication_events",
            RiskEvent.source_id == event.reminder_id,
        )
    )


# ---------------------------------------------------------------------------
# 撤回确认（FR-UI-08：普通确认 10 秒内可撤销）
# ---------------------------------------------------------------------------


async def reopen_event(
    session: AsyncSession,
    *,
    event: MedicationEvent,
    reason: str | None = None,
    now: datetime.datetime | None = None,
) -> MedicationEvent:
    """把「本轮刚确认的这一条」退回待确认（`waiting_confirm` + 终态清空）。

    这是 :func:`confirm_event` 的**逆操作**，也是它唯一被允许的逆：长者手一抖点了
    「已服用」，10 秒内能撤回，否则当天日报会记一顿实际没吃的药。

    ## 撤的是哪些列，以及为什么只撤这些

    撤回必须让 `summarize_day` 的计数**真的减少**：`pending` 靠 `terminal_state IS
    NULL` 认定（:func:`summarize_day`），所以只把 `status` 改回 `waiting_confirm` 却
    留着 `taken_reported` 就是假撤销——日报仍算「已服」。因此终态、确认时刻、确认
    渠道、补确认标记四列一起清：

    - `status` → `waiting_confirm`（回到「等长者回应」）
    - `terminal_state` → `None`
    - `confirmed_at` → `None`
    - `confirm_channel` → `None`
    - `late_taken` → `False`

    **不动**的两列，各有理由：

    - `retry_count`：那是触达次数的**事实记录**（已经打过几次电话/催过几次），
      撤一次误点不会让时间倒流，重试节拍仍按已触达次数算。
    - `failure_reason`：那是触达管线的历史（例如 `tts_failed`），与「这次确认」
      无关；清掉会把一次播报失败从排查线索里抹掉。

    **不动告警侧**（口径见 lead 2026-11-12 裁决）：本函数只重置用药侧状态，
    `risk_events` / `work_orders` / 家庭通知一律原样保留。一次误点不该抹掉一条
    已经发出去的安全告警——真要把单关掉，是护工在照护台上的动作，不是长者端的按钮。
    若该提醒已有风险事件，本函数会把它记进日志（`risk_event_id`）便于反查，但不改它。

    ## 拒绝的情形（一律 `MedicationEventConflict`，API 映射 409）

    - 终态不是 `taken_reported`（见 :data:`REOPENABLE_TERMINAL_STATES`）；
    - 已经撤过（终态已被清空）——重复撤销是冲突，不是幂等成功，否则前端会以为
      「第二次也撤掉了什么」；
    - 超过 :data:`UNDO_WINDOW_SECONDS` 秒。

    只 `flush` 不 `commit`：事务归调用方（与 `confirm_event` 同款）。
    """
    # 调用方手里的 `event` 可能是轮询时的旧快照，判之前先与库对齐一次
    # （与 `_resume_or_write` 同一理由：升级任务可能刚把它改过）。
    await session.refresh(event)

    if event.terminal_state != CONFIRMED_TERMINAL_STATE:
        raise MedicationEventConflict(
            f"只有 {CONFIRMED_TERMINAL_STATE} 可以撤回，当前终态是 {event.terminal_state!r}；"
            "拒服/未确认/补确认都是已收口的事实，请让护工在照护台纠正"
        )
    if event.confirmed_at is None:
        # 终态是 taken_reported 却没有确认时刻：数据本身不自洽，不猜，直接冲突。
        raise MedicationEventConflict(
            "该确认没有 recorded confirmed_at，无法判断是否还在撤销窗口内"
        )

    stamp = _as_utc(now, field="now") if now is not None else datetime.datetime.now(datetime.UTC)
    elapsed = (stamp - _as_utc(event.confirmed_at, field="confirmed_at")).total_seconds()
    if elapsed > UNDO_WINDOW_SECONDS:
        raise MedicationEventConflict(
            f"确认已过去 {int(elapsed)} 秒，超过 {UNDO_WINDOW_SECONDS} 秒的撤销窗口；"
            "请让护工在照护台按实际情况纠正"
        )

    previous_terminal = event.terminal_state
    previous_confirmed_at = event.confirmed_at
    previous_channel = event.confirm_channel

    event.status = "waiting_confirm"
    event.terminal_state = None
    event.confirmed_at = None
    event.confirm_channel = None
    event.late_taken = False
    await session.flush()

    risk = await _find_risk_event(session, event)
    logger.info(
        "medication confirmation reopened reminder_id=%s elder_id=%s prior_terminal=%s "
        "prior_channel=%s elapsed_seconds=%s risk_event_id=%s reason=%s",
        event.reminder_id,
        event.elder_id,
        previous_terminal,
        previous_channel,
        round(elapsed, 3),
        risk.event_id if risk is not None else None,
        (reason or "")[:100] or None,
    )
    logger.debug(
        "medication confirmation reopened detail reminder_id=%s prior_confirmed_at=%s",
        event.reminder_id,
        previous_confirmed_at,
    )
    return event


# ---------------------------------------------------------------------------
# 确认入口：先把回应打回 HITL 图，图里没有停点才直写业务表
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DialogConfirmOutcome:
    """对话入口的一次处理结果（AC-35）。

    为什么不返回裸字符串：调用方既要向长者回话，也要判断「到底有没有落库」。
    把两者压进一句话，迟早会被写成「回复了就当成确认了」——所以
    `confirmed`（是否落库为已服药）与 `decision`（实际处理了什么）分开给。
    """

    confirmed: bool
    reply: str
    reminder_id: uuid.UUID | None = None
    decision: str | None = None
    status: str | None = None
    terminal_state: str | None = None
    confirm_channel: str | None = None
    hitl_resumed: bool = False
    reason: str | None = None


def _graph_module() -> Any:
    """惰性取用药图模块。

    为什么不在模块顶部 import：AGENTS §2.1 的依赖方向是 `graphs → services`，
    服务层反过来 import 图就把这条边倒过来了。运行期按需取，让依赖只在调用栈上
    存在一次（`app/api/health.py` 取健康图也是这个手法）。
    """
    from app.graphs import medication_confirmation_graph as module

    return module


async def confirmation_graph() -> Any:
    """取用药确认子图（生产是进程内单例）。

    单独抽一个函数是为了给测试留替换点：图节点按运行期 `session_factory` 各自开
    session 并 commit，若沿用它默认的 `app.db.session_factory`，测试插在未提交事务里
    的事件它根本看不见（先例：`medication_jobs.db_session_factory`）。
    """
    return await _graph_module().get_medication_graph()


def _confirmation_thread_id(event: MedicationEvent) -> str:
    """事件 → 图线程键。必须与调度器起步时算的是同一个键，否则问不到那条线程。"""
    return cast(
        str,
        _graph_module().medication_thread_id(
            elder_id=event.elder_id,
            plan_id=event.plan_id,
            scheduled_at=event.scheduled_at,
        ),
    )


async def _pending_graph(
    event: MedicationEvent, graph: Any | None
) -> tuple[Any | None, Mapping[str, Any] | None]:
    """拿到图，并问一句「这个事件还在等回应吗」。

    取图失败（checkpointer 不可用等）或读状态失败，一律降级成「没有停点」：
    照护台点确认、长者说「吃了」，都不该因为流程引擎的状态而写不进一条业务事实。
    """
    module = _graph_module()
    active = graph
    if active is None:
        try:
            active = await confirmation_graph()
        except Exception as exc:
            logger.warning(
                "medication graph unavailable reminder_id=%s error_type=%s",
                event.reminder_id,
                type(exc).__name__,
            )
            return None, None
    try:
        pending = await module.pending_confirmation(active, _confirmation_thread_id(event))
    except Exception as exc:
        logger.warning(
            "medication graph state unreadable reminder_id=%s error_type=%s",
            event.reminder_id,
            type(exc).__name__,
        )
        return active, None
    return active, pending


async def _refresh_quietly(session: AsyncSession, event: MedicationEvent) -> None:
    """图在自己的会话里写完并提交后，请求会话里这个 ORM 对象是旧的，重读一次拿新值。"""
    try:
        await session.refresh(event)
    except Exception as exc:
        logger.warning(
            "medication event refresh failed reminder_id=%s error_type=%s",
            event.reminder_id,
            type(exc).__name__,
        )


async def pending_event(session: AsyncSession, *, elder_id: uuid.UUID) -> MedicationEvent | None:
    """该长者当前还等着回应的一条用药事件（最近的一次到点）。

    只认 `calling` / `waiting_confirm`：拿已确认/已升级的事件去 resume，
    等于把「幂等重放」和「这一次真的确认」混成一件事。
    """
    result = await session.execute(
        select(MedicationEvent)
        .where(
            MedicationEvent.elder_id == elder_id,
            MedicationEvent.status.in_(PENDING_EVENT_STATUSES),
        )
        .order_by(MedicationEvent.scheduled_at.desc(), MedicationEvent.reminder_id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _resume_or_write(
    session: AsyncSession,
    *,
    event: MedicationEvent,
    decision: str,
    channel: str,
    graph: Any | None = None,
    triggered_by: str | None = None,
    response: str | None = None,
) -> tuple[MedicationEvent, bool]:
    """两条路共用的一段：有停点就 resume 图，没有就按 `decision` 直写业务表。

    返回 `(event, hitl_resumed)`：`False` 表示这次走的是直写，不代表失败。
    """
    _check_optional(channel, CONFIRM_CHANNELS, field="confirm_channel")
    _check_optional(triggered_by, TRIGGERED_BY, field="triggered_by")

    # MED-03：补确认到底写哪个终态，取决于**库里**的原终态。调用方手里的 `event`
    # 可能是轮询时拿到的旧快照（这中间升级任务可能已经把它关成 missed_unconfirmed），
    # 所以判之前先与库对齐一次。
    await session.refresh(event)
    prior_terminal = event.terminal_state
    late_prior = prior_terminal in PRIOR_MISSED_TERMINAL_STATES

    if decision == DIALOG_CONFIRMED and (
        event.status == CONFIRMED_STATUS and event.terminal_state == CONFIRMED_TERMINAL_STATE
    ):
        # 已经确认过：重复点击/重复说话都不该再改一次 confirmed_at（AC-17 幂等口径）。
        return event, False

    active, pending = await _pending_graph(event, graph)
    if active is not None and pending and not late_prior:
        module = _graph_module()
        try:
            await module.resume_confirmation(
                active,
                thread_id=_confirmation_thread_id(event),
                decision=decision,
                channel=channel,
                triggered_by=triggered_by,
                response=response,
            )
        except Exception as exc:
            # 图侧炸了（节点写库失败、checkpointer 掉线）：让这次点击仍然算数，
            # 落一条 WARNING 带异常类型名，不带上长者的原话与药名。
            logger.warning(
                "medication graph resume failed reminder_id=%s decision=%s error_type=%s",
                event.reminder_id,
                decision,
                type(exc).__name__,
            )
        else:
            await _refresh_quietly(session, event)
            return event, True

    if decision == DIALOG_CONFIRMED:
        # MED-03 的核心一句：原终态已是「未服 / 未确认 / 升级」时，写追认值而不是覆盖。
        # `late_taken=True` 也一并记下——这次确认发生在触达结束之后，与 AC-08 的
        # 「+5/+10/+15 才确认」是同一类迟延事实，只是更晚。
        await update_event_state(
            session,
            reminder_id=event.reminder_id,
            status=CONFIRMED_STATUS,
            terminal_state=(LATE_TAKEN_TERMINAL_STATE if late_prior else CONFIRMED_TERMINAL_STATE),
            confirm_channel=channel,
            confirmed_at=datetime.datetime.now(datetime.UTC),
            late_taken=True if late_prior else None,
        )
        if late_prior:
            logger.info(
                "medication_late_confirmation_recorded reminder_id=%s prior_terminal=%s",
                event.reminder_id,
                prior_terminal,
            )
    elif decision == DIALOG_REFUSED:
        await refuse_event(session, event=event, channel=channel)
    else:
        raise MedicationValidationError(f"decision 只能是 confirmed / refused，收到 {decision!r}")
    return event, False


async def confirm_event(
    session: AsyncSession,
    *,
    event: MedicationEvent,
    channel: str,
    graph: Any | None = None,
    triggered_by: str | None = None,
) -> tuple[MedicationEvent, bool]:
    """确认服药（照护台按钮 / 对话说「吃了」）：先 resume 图，图里没停点才直写。

    为什么两条路都留着：生产目前只有测试会启动这张图（调度器注入是后续卡），
    照护台在「图还没跑过」的事件上点确认也必须能落库；而图已经在等回应时绕开它直写，
    那条线程会永远停在 `wait_confirm`，重启后还会被当成待确认再催一次药。
    """
    return await _resume_or_write(
        session,
        event=event,
        decision=DIALOG_CONFIRMED,
        channel=channel,
        graph=graph,
        triggered_by=triggered_by,
    )


async def confirm_from_dialog(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    text: str,
    graph: Any | None = None,
) -> DialogConfirmOutcome:
    """对话入口的用药确认/拒服（AC-35 / FR-MED-05「对话内触发」）。

    流程与照护台按钮**共用** `confirm_event` / `refuse_event`，只在 `channel`、
    `triggered_by` 和「长者说了什么」三点上不同 —— 不另写一套写库逻辑。

    `confirmed=False` 覆盖三种「什么都没落库」：没听懂、当前没有待确认提醒、
    图与直写都失败。调用方不能靠 `reply` 的措辞推断是否确认，只看 `confirmed`。
    """
    raw = str(text or "").strip()
    if not raw:
        return DialogConfirmOutcome(
            confirmed=False, reason="empty_text", reply=DIALOG_UNHEARD_REPLY
        )
    try:
        decision, _channel, _response = _graph_module().parse_decision(raw)
    except ValueError:
        # 长者说了一句不像结论的话（「今天天气不错」）：不猜、不拦，交回主控 Agent 继续聊。
        return DialogConfirmOutcome(
            confirmed=False, reason="unrecognised_text", reply=DIALOG_UNHEARD_REPLY
        )
    if decision not in (DIALOG_CONFIRMED, DIALOG_REFUSED):
        return DialogConfirmOutcome(
            confirmed=False, reason="unrecognised_text", reply=DIALOG_UNHEARD_REPLY
        )

    event = await pending_event(session, elder_id=elder_id)
    if event is None:
        logger.info("medication dialog without pending event elder_id=%s", elder_id)
        return DialogConfirmOutcome(
            confirmed=False, reason="no_pending_event", reply=DIALOG_NO_PENDING_REPLY
        )

    if decision == DIALOG_CONFIRMED:
        event, hitl_resumed = await confirm_event(
            session,
            event=event,
            channel=DIALOG_CHANNEL,
            graph=graph,
            triggered_by=TRIGGERED_BY_DIALOG,
        )
        logger.info(
            "medication confirmed by dialog elder_id=%s reminder_id=%s hitl_resumed=%s",
            elder_id,
            event.reminder_id,
            hitl_resumed,
        )
        return DialogConfirmOutcome(
            confirmed=True,
            reply=DIALOG_CONFIRMED_REPLY,
            reminder_id=event.reminder_id,
            decision=decision,
            status=event.status,
            terminal_state=event.terminal_state,
            confirm_channel=event.confirm_channel,
            hitl_resumed=hitl_resumed,
        )

    event, hitl_resumed = await _resume_or_write(
        session,
        event=event,
        decision=DIALOG_REFUSED,
        channel=DIALOG_CHANNEL,
        graph=graph,
        triggered_by=TRIGGERED_BY_DIALOG,
        response=raw,
    )
    logger.info(
        "medication refused by dialog elder_id=%s reminder_id=%s hitl_resumed=%s",
        elder_id,
        event.reminder_id,
        hitl_resumed,
    )
    return DialogConfirmOutcome(
        confirmed=False,
        reply=DIALOG_REFUSED_REPLY,
        reminder_id=event.reminder_id,
        decision=decision,
        status=event.status,
        terminal_state=event.terminal_state,
        confirm_channel=event.confirm_channel,
        hitl_resumed=hitl_resumed,
    )


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


async def _find_event(
    session: AsyncSession, *, plan_id: uuid.UUID, scheduled_at: datetime.datetime
) -> MedicationEvent | None:
    result = await session.execute(
        select(MedicationEvent).where(
            MedicationEvent.plan_id == plan_id,
            MedicationEvent.scheduled_at == scheduled_at,
        )
    )
    return result.scalar_one_or_none()


async def _find_attempt(
    session: AsyncSession, *, event_id: uuid.UUID, attempt_no: int
) -> MedicationAttempt | None:
    result = await session.execute(
        select(MedicationAttempt).where(
            MedicationAttempt.event_id == event_id,
            MedicationAttempt.attempt_no == attempt_no,
        )
    )
    return result.scalar_one_or_none()


def _reuse_event(
    event: MedicationEvent,
    *,
    elder_id: uuid.UUID,
    payload_hash: str,
    triggered_by: str,
) -> tuple[MedicationEvent, bool]:
    """同键命中的分流：同内容返回原行，异内容（含长者不一致）抛 409。"""
    if event.payload_hash != payload_hash or event.elder_id != elder_id:
        raise MedicationEventConflict(
            "同一 plan_id + scheduled_at 的用药事件已存在且内容不同"
            f"（reminder_id={event.reminder_id}，existing_hash={event.payload_hash[:12]}…，"
            f"incoming_hash={payload_hash[:12]}…）"
        )
    if event.triggered_by != triggered_by:
        # 入口不同但事情相同（调度器已建、长者又在语音里说「我要吃药」）：不改原行，
        # 只留痕，避免把 scheduler 来源洗成 dialog 影响统计口径。
        logger.info(
            "medication event 重复请求（triggered_by %s → %s）reminder_id=%s",
            event.triggered_by,
            triggered_by,
            event.reminder_id,
        )
    return event, False


async def _fill_attempt(
    session: AsyncSession,
    attempt: MedicationAttempt,
    *,
    receipt: str | None,
    tts_result: str | None,
    response: str | None,
    response_channel: str | None,
) -> MedicationAttempt:
    """幂等重复调用时补空字段（不覆盖已有内容），有变化才 flush。"""
    changed = False
    if receipt is not None and attempt.receipt is None:
        attempt.receipt = receipt
        changed = True
    if tts_result is not None and attempt.tts_result is None:
        attempt.tts_result = tts_result
        changed = True
    if response is not None and attempt.response is None:
        attempt.response = response
        changed = True
    if response_channel is not None and attempt.response_channel is None:
        attempt.response_channel = response_channel
        changed = True
    if changed:
        await session.flush()
    return attempt


def _as_utc(value: datetime.datetime, *, field: str) -> datetime.datetime:
    """统一成带时区的 UTC；naive 值按 UTC 解释（库里全是 timestamptz）。"""
    if not isinstance(value, datetime.datetime):
        raise MedicationValidationError(f"{field} 必须是 datetime，收到 {type(value).__name__}")
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.UTC)
    return value.astimezone(datetime.UTC)


def _check_optional(value: str | None, allowed: tuple[str, ...], *, field: str) -> None:
    if value is not None and value not in allowed:
        raise MedicationValidationError(f"{field} 只能是 {allowed} 或 None，收到 {value!r}")


def _expunge_quietly(session: AsyncSession, obj: Any) -> None:
    """savepoint 回滚后把失败的 pending 对象踢出 session，避免污染后续查询。"""
    try:
        if obj in session:
            session.expunge(obj)
    except Exception:  # pragma: no cover - expunge 本身失败不影响主流程
        logger.debug("expunge failed for %r", type(obj).__name__)


def _iso(value: datetime.datetime | None) -> str:
    return value.isoformat() if value is not None else "—"


# ------------------------------------------------------------------ 计划侧读写（W7 集成）
#
# 这三个函数是 `app/api/medication.py` 的 `_svc_*` 适配块所依赖的缺口（W8）。
# 定盘口径：**服务层不认识 identity** —— 角色与范围在 API 层判，服务层只认已经
# 过关的 `elder_id` / `created_by`；服务层若自己读身份，就等于把闸门③（身份只
# 从 runtime_context 注入）复制成两份，迟早不一致。
#
# 全部只 flush 不 commit：事务边界归 `get_session`（与事件侧一致）。

PLAN_STATUSES: Final[tuple[str, ...]] = ("active", "stopped")
"""`medication_plans.status` 两值（TD §3.4 CHECK 同源）。"""

DEFAULT_EVENT_LIST_LIMIT: Final[int] = 100
MAX_EVENT_LIST_LIMIT: Final[int] = 500
"""事件列表默认/上限条数：一次拉全部历史会把响应体撑成整张表。"""

MAX_DRUG_NAME_LENGTH: Final[int] = 100
MAX_DOSE_LENGTH: Final[int] = 50
"""药名/剂量长度上限（暂取 TD `text` 列的可读上限，属工程自裁，非 PRD 数值）。"""

_SCHEDULE_CRON_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
"""`schedule_cron` 本版只支持单值 `HH:MM`（每日一次）。"""


def parse_schedule_cron(value: str) -> datetime.time:
    """把 `HH:MM` 解析成 `datetime.time`；其它形状一律抛 `MedicationValidationError`。

    刻意**收窄**到每日单点：PRD §4.5 的触达节拍是「每日到点一次」，而 cron 表达式
    能表达的「每 2 小时一次」「每周一三五」在数据模型（`schedule_cron` 单列 + 一条
    计划一条事件键）里没有对应语义，放宽解析等于让「计划写了但从不触达」静默成立。
    """
    if not isinstance(value, str) or not _SCHEDULE_CRON_PATTERN.match(value.strip()):
        raise MedicationValidationError(f"schedule_cron 只能是 HH:MM，收到 {value!r}")
    hour, minute = value.strip().split(":")
    return datetime.time(hour=int(hour), minute=int(minute))


def _normalise_text(value: Any, *, field: str, max_length: int) -> str:
    """去首尾空白 + 非空 + 长度上限；不合格就是调用方参数错，不是业务冲突。"""
    if not isinstance(value, str):
        raise MedicationValidationError(f"{field} 必须是字符串，收到 {type(value).__name__}")
    cleaned = value.strip()
    if not cleaned:
        raise MedicationValidationError(f"{field} 不能为空")
    if len(cleaned) > max_length:
        raise MedicationValidationError(f"{field} 不能超过 {max_length} 字，收到 {len(cleaned)} 字")
    return cleaned


async def list_plans(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    status: str | None = None,
) -> Sequence[MedicationPlan]:
    """某位长者的用药计划列表（`start_date` 倒序，同日按 `plan_id` 保证稳定分页）。

    `status` 传非法值是**参数错**（ValueError），不静默返回空列表 —— 否则前端把
    「筛选写错」读成「这位长者没有用药计划」，这是安全相关的误读。
    """
    _check_optional(status, PLAN_STATUSES, field="status")
    conditions = [MedicationPlan.elder_id == elder_id]
    if status is not None:
        conditions.append(MedicationPlan.status == status)

    rows = (
        (
            await session.execute(
                select(MedicationPlan)
                .where(*conditions)
                .order_by(MedicationPlan.start_date.desc(), MedicationPlan.plan_id.desc())
            )
        )
        .scalars()
        .all()
    )
    logger.info(
        "medication_plans_listed",
        extra={"elder_id": str(elder_id), "status": status or "*", "rows": len(rows)},
    )
    return list(rows)


async def create_plan(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    drug_name: str,
    dose: str,
    schedule_cron: str,
    start_date: datetime.date,
    created_by: uuid.UUID,
    end_date: datetime.date | None = None,
) -> MedicationPlan:
    """新建一条用药计划（仅医生，权限在 API 层判过）。

    为什么 `created_by` 是必填参数而不是可空：TD §3.4 该列 NOT NULL，且「谁开的
    药」是审计链的起点。API 层从 `identity.staff_id` 取，服务层不接受默认值 ——
    默认成 `None` 或某个系统账号会让责任归属断掉。
    """
    cleaned_drug = _normalise_text(drug_name, field="drug_name", max_length=MAX_DRUG_NAME_LENGTH)
    cleaned_dose = _normalise_text(dose, field="dose", max_length=MAX_DOSE_LENGTH)
    parse_schedule_cron(schedule_cron)
    if not isinstance(start_date, datetime.date):
        raise MedicationValidationError("start_date 必须是日期")
    if end_date is not None:
        if not isinstance(end_date, datetime.date):
            raise MedicationValidationError("end_date 必须是日期或 None")
        if end_date < start_date:
            raise MedicationValidationError("end_date 不能早于 start_date")

    plan = MedicationPlan(
        elder_id=elder_id,
        drug_name=cleaned_drug,
        dose=cleaned_dose,
        schedule_cron=schedule_cron.strip(),
        start_date=start_date,
        end_date=end_date,
        version=1,
        status="active",
        created_by=created_by,
    )
    session.add(plan)
    await session.flush()
    logger.info(
        "medication_plan_created",
        extra={
            "plan_id": str(plan.plan_id),
            "elder_id": str(elder_id),
            "created_by": str(created_by),
            "schedule_cron": plan.schedule_cron,
            "has_end_date": end_date is not None,
        },
    )
    return plan


async def list_events(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    status: str | None = None,
    limit: int = DEFAULT_EVENT_LIST_LIMIT,
) -> Sequence[MedicationEvent]:
    """某位长者的用药事件列表（`scheduled_at` 倒序，最近的到点在最前）。"""
    _check_optional(status, EVENT_STATUSES, field="status")
    if limit < 1 or limit > MAX_EVENT_LIST_LIMIT:
        raise MedicationValidationError(f"limit 必须在 [1, {MAX_EVENT_LIST_LIMIT}]，收到 {limit}")

    conditions = [MedicationEvent.elder_id == elder_id]
    if status is not None:
        conditions.append(MedicationEvent.status == status)

    rows = (
        (
            await session.execute(
                select(MedicationEvent)
                .where(*conditions)
                # reminder_id 做次序键：批量为同一天生成的事件 scheduled_at 可能相同，
                # 只按时间排序会得到不稳定分页（与 list_chunks 同一考虑）。
                .order_by(MedicationEvent.scheduled_at.desc(), MedicationEvent.reminder_id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    logger.info(
        "medication_events_listed",
        extra={
            "elder_id": str(elder_id),
            "status": status or "*",
            "limit": limit,
            "rows": len(rows),
        },
    )
    return list(rows)


async def summarize_day(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    since: datetime.datetime,
    until: datetime.datetime,
) -> dict[str, Any]:
    """某长者某自然日的用药事件按终态计数（家属日报「用药」块的唯一数据源）。

    `since` / `until` 是**左闭右开**的带时区时刻，由调用方按报告时区的自然日换算：
    `scheduled_at` 是 timestamptz，拿 DATE 直接比较会把零点前后的事件算到错误的
    那一天（上海 08:00 = UTC 00:00，差一分钟就差一天）。

    `terminal_state IS NULL`（`calling` / `waiting_confirm`，到点流程还没结束）单独
    计 `pending`：TD §3.4 只定义四个终态值，把它们并进 `missed_unconfirmed` 属于
    「互相混写」（PRD FR-RPT-01 明令禁止），直接丢掉又会让分项之和不等于 `total`。

    `state='not_configured'` 表示当日**既没有药单也没有事件**——日报该写「暂无用药
    计划」，而不是列一排 0 让人误以为「提醒过但都没吃」。`plans_active` 只回答
    「有没有药单」，因此**不按 `start_date` / `end_date` 过滤**，它不是统计口径。

    第五值 `late_taken_reported`（MED-03 补确认）计入 `taken_reported`，见
    `REPORT_COUNT_STATES`：日报四项之和对得上 `total`，也不动摇家属端契约。
    """
    rows = (
        await session.execute(
            select(MedicationEvent.terminal_state, func.count())
            .where(
                MedicationEvent.elder_id == elder_id,
                MedicationEvent.scheduled_at >= since,
                MedicationEvent.scheduled_at < until,
            )
            .group_by(MedicationEvent.terminal_state)
        )
    ).all()

    counts: dict[str, int] = dict.fromkeys(REPORT_COUNT_STATES, 0)
    counts["pending"] = 0
    for state, total in rows:
        if state is None:
            counts["pending"] += int(total)
        elif state == LATE_TAKEN_TERMINAL_STATE:
            # 补确认（MED-03）：事后确认吃过，日报按「已服」计入；不新增第五个键。
            counts[CONFIRMED_TERMINAL_STATE] += int(total)
        elif state in counts:
            counts[state] += int(total)
        else:  # pragma: no cover - 取值受 ck_medication_events_terminal_state 约束
            logger.warning(
                "medication event with unknown terminal_state",
                extra={"elder_id": str(elder_id), "terminal_state": str(state)},
            )

    plans_active = int(
        await session.scalar(
            select(func.count())
            .select_from(MedicationPlan)
            .where(MedicationPlan.elder_id == elder_id, MedicationPlan.status == "active")
        )
        or 0
    )

    total_events = sum(counts.values())
    return {
        "module": "medication",
        "state": "not_configured" if total_events == 0 and plans_active == 0 else "collected",
        "counts": counts,
        "total": total_events,
        "plans_active": plans_active,
    }


# ------------------------------------------------------------------ 计划侧：改与停（W1）
#
# 定盘口径（产品负责人 2026-11-09 裁决；PRD:331 FR-MED-01「医生增改停查」、PRD:508 AC-27）：
#
# 1. **改** = 对 `medication_plans` 同一行 UPDATE，并且 `version = version + 1`。历史
#    事件的 `drug_snapshot` 是 JSONB 快照，天然不受影响；未来新事件由调度器的
#    `_plan_snapshot()` 读 plan **当前值**生成。
# 2. **停** = `status='stopped'`，**不新增** `cancelled` 事件态（`medication_events`
#    的 CHECK 仍只有 calling/waiting_confirm/confirmed/escalated 四值）。「计划停了、
#    这条到点作废」由「计划已非 active + 事件尚未触达」共同表达，落点在
#    `app/scheduler/medication.py:decide_event_action`。
# 3. **并发** = PATCH 可选 `expected_version`；不符抛 `MedicationVersionConflict`。
#
# 只 `flush` 不 `commit`：事务归调用方（与 `create_plan` 同款）。

PLAN_UPDATABLE_FIELDS: Final[tuple[str, ...]] = (
    "drug_name",
    "dose",
    "schedule_cron",
    "start_date",
    "end_date",
)
"""`update_plan` 接受的白名单列（顺序无关，逐个校验取值）。"""

PLAN_FORBIDDEN_FIELDS: Final[tuple[str, ...]] = (
    "plan_id",
    "elder_id",
    "created_by",
    "version",
    "status",
)
"""明确拒绝的列：归属、版本、生命周期都不由「改」这个动作表达。

`status` 单独拒在 PATCH 之外，是为了不让「改」变成停用/启用的旁路——停用只走
`stop_plan`，那条路有幂等语义与专门的审计 action（`medication.plan.stop`）。
"""

MAX_STOP_REASON_LENGTH: Final[int] = 200
"""停用原因上限（只进审计与日志；`medication_plans` 没有备注列，不落库）。"""


async def load_plan(session: AsyncSession, plan_id: uuid.UUID) -> MedicationPlan:
    """按主键取用药计划；找不到抛 `MedicationPlanNotFoundError`（API 映射 404）。

    PATCH / stop 的路径参数只有 `plan_id`，长者归属必须从库里反查后才能过范围闸
    （AGENTS §4.9：客户端给的标识不能被当成「这条记录属于谁」的证据）。
    """
    plan = await session.get(MedicationPlan, plan_id)
    if plan is None:
        raise MedicationPlanNotFoundError(f"用药计划不存在：plan_id={plan_id}")
    return plan


def _revision_retire_date(*, start_date: datetime.date, today: datetime.date) -> datetime.date:
    """旧版本「失效日」：改单日的前一天，但不得早于它自己的 `start_date`。

    为什么不直接写 `today`：`end_date` 是**含**当日的服药窗口，修订从今天生效，
    旧版本的最后有效日就是昨天。当天开、当天改的药单没有「昨天」，退到 `start_date`
    即可——两行窗口重叠一天不会重复提醒，提醒只看 `status == 'active'` 的那一行。
    """
    return max(start_date, today - datetime.timedelta(days=1))


async def update_plan(
    session: AsyncSession,
    *,
    plan: MedicationPlan,
    changes: Mapping[str, Any],
    expected_version: int | None = None,
    revised_by: uuid.UUID | None = None,
) -> MedicationPlan:
    """医生改药单（FR-MED-01 的「改」）：**事实修订**——旧行失效 + 新行生效，返回新行。

    为什么不是原地 UPDATE（讲义 §10.3）：药单是「某段时间内按某个剂量服药」的事实。
    把旧行改成新剂量，等于抹掉「11 月 10 日之前是 0.5g」这条事实，事后查「那天护士
    按什么剂量发的药」只能靠事件 JSONB 快照碰运气。修订法下每一行都是一段**只增
    不改**的事实：旧行 `status='stopped'` + `end_date` 收口到改单日前一天，新行带着
    修订后的字段与 `version+1` 生效，历史行与历史事件都不动。

    - **返回的是新行**（新 `plan_id`）：调用方拿到的就是「当前生效版本」；旧行按
      `plan_id` 仍查得到，是历史版本。`version` 在同一药单的版本链上单调递增。
    - `version` 由服务端自增；客户端只能通过 `expected_version` 参与**比对**，请求体里
      直接给 `version` 会被 `PLAN_FORBIDDEN_FIELDS` 拒掉（422），不会被采信。
    - `revised_by` 记到新行的 `created_by`：谁下的这一版医嘱。不传则沿用旧行的开单
      医生（API 适配层目前不传，改单人落在 `audit_logs` 的 `medication.plan.update`）。
    - 校验顺序固定为「白名单 → 字段取值 → 日期窗口 → 生命周期 → 乐观锁」：先挡掉借
      PATCH 偷改归属的请求，再给出能指名到字段的参数错，然后才判「这条计划还能不能
      改」，最后判并发——否则一个 409 会把「药名传了 null」这种明显的参数错盖成
      「有人先改了」。
    - `changes` 与当前值完全相同时**不开新版本**（版本号只表示内容版本，空提交不该
      制造一条历史行），直接把原行返回。
    - 已停用 / 已过 `end_date` 的计划不能再改：那是历史事实，要改就新建一条药单。
    """
    if not isinstance(plan, MedicationPlan):
        raise MedicationValidationError(f"plan 必须是 MedicationPlan，收到 {type(plan).__name__}")
    if not isinstance(changes, Mapping):
        raise MedicationValidationError("changes 必须是映射（字段名 → 新值）")

    unknown = sorted({str(key) for key in changes} - set(PLAN_UPDATABLE_FIELDS))
    if unknown:
        raise MedicationValidationError(
            f"药单只允许改 {PLAN_UPDATABLE_FIELDS}，拒绝字段：{unknown}"
            f"（{PLAN_FORBIDDEN_FIELDS} 由服务端或专用端点维护）"
        )

    cleaned: dict[str, Any] = {}
    if "drug_name" in changes:
        cleaned["drug_name"] = _normalise_text(
            changes["drug_name"], field="drug_name", max_length=MAX_DRUG_NAME_LENGTH
        )
    if "dose" in changes:
        cleaned["dose"] = _normalise_text(changes["dose"], field="dose", max_length=MAX_DOSE_LENGTH)
    if "schedule_cron" in changes:
        raw_cron = changes["schedule_cron"]
        parse_schedule_cron(raw_cron)
        cleaned["schedule_cron"] = str(raw_cron).strip()
    if "start_date" in changes:
        cleaned["start_date"] = _as_date(changes["start_date"], field="start_date")
    if "end_date" in changes:
        raw_end = changes["end_date"]
        cleaned["end_date"] = None if raw_end is None else _as_date(raw_end, field="end_date")

    effective_start = cleaned.get("start_date", plan.start_date)
    effective_end = cleaned.get("end_date", plan.end_date)
    if effective_end is not None and effective_end < effective_start:
        raise MedicationValidationError("end_date 不能早于 start_date")

    # 生命周期闸：只有「当前生效中、还没到期」的版本可以被修订。已停用/已过期的行是
    # 历史事实，改它等于伪造医嘱（讲义 §10.3）。
    today = today_in_shanghai()
    if plan.status != "active":
        raise MedicationValidationError(
            f"用药计划已停用（status={plan.status}），不能再改：历史版本只读，请新建一条药单"
        )
    if plan.end_date is not None and plan.end_date < today:
        raise MedicationValidationError(
            f"用药计划已于 {plan.end_date.isoformat()} 结束，不能再改；请新建一条药单"
        )
    if revised_by is not None and not isinstance(revised_by, uuid.UUID):
        raise MedicationValidationError(
            f"revised_by 必须是 UUID 或 None，收到 {type(revised_by).__name__}"
        )

    if expected_version is not None:
        if not isinstance(expected_version, int) or isinstance(expected_version, bool):
            raise MedicationValidationError(
                f"expected_version 必须是整数，收到 {expected_version!r}"
            )
        if plan.version != expected_version:
            raise MedicationVersionConflict(
                f"药单已被修改：expected_version={expected_version}，库中 version={plan.version}"
            )

    # 只把「值真的变了」的列算作 changed（审计要能区分「点了保存但没改」与「改了剂量」）。
    changed = {field: value for field, value in cleaned.items() if getattr(plan, field) != value}
    if not changed:
        logger.info(
            "medication_plan_revision_noop",
            extra={
                "plan_id": str(plan.plan_id),
                "elder_id": str(plan.elder_id),
                "version": int(plan.version),
                "fields": sorted(cleaned),
            },
        )
        return plan

    # 先算校验、再动旧行：end_date/start_date 组合不合法时不能把旧行改成 stopped 却
    # 又抛异常（调用方回滚前那条「已停用」会留在 session 里，下一处 flush 就落地）。
    # 新版本默认「改单日生效」，但不得早于原计划的开始日：还没开始吃的药单不能被一次
    # 改剂量提前到今天。
    new_start = cleaned.get("start_date", max(plan.start_date, today))
    new_end = cleaned.get("end_date", plan.end_date)
    if new_end is not None and new_end < new_start:
        raise MedicationValidationError(
            f"修订后 end_date（{new_end.isoformat()}）不能早于 start_date（{new_start.isoformat()}）"
        )

    previous_end = plan.end_date
    version_before = int(plan.version)

    # 旧行只动「生命周期」两列：药名/剂量/时刻是历史事实，改一个字都会让「那天护士按
    # 什么剂量发的药」失去证据。
    plan.status = "stopped"
    plan.end_date = _revision_retire_date(start_date=plan.start_date, today=today)

    revised = MedicationPlan(
        elder_id=plan.elder_id,
        drug_name=cleaned.get("drug_name", plan.drug_name),
        dose=cleaned.get("dose", plan.dose),
        schedule_cron=cleaned.get("schedule_cron", plan.schedule_cron),
        start_date=new_start,
        end_date=new_end,
        version=version_before + 1,
        status="active",
        created_by=revised_by if revised_by is not None else plan.created_by,
    )
    session.add(revised)
    await session.flush()

    logger.info(
        "medication_plan_revised",
        extra={
            "plan_id": str(revised.plan_id),
            "previous_plan_id": str(plan.plan_id),
            "elder_id": str(plan.elder_id),
            "version_before": version_before,
            "version_after": int(revised.version),
            "fields": sorted(changed),
            "effective_from": new_start.isoformat(),
            "retired_end_date": plan.end_date.isoformat() if plan.end_date else "",
            "previous_end_date": previous_end.isoformat() if previous_end else "",
            "created_by": str(revised.created_by),
        },
    )
    return revised


async def stop_plan(
    session: AsyncSession,
    *,
    plan: MedicationPlan,
    reason: str | None = None,
) -> MedicationPlan:
    """医生停用药单（FR-MED-01 的「停」）：`status='stopped'` + 窗口收口，不新增事件态。

    1. 只改 `medication_plans` 的生命周期两列 —— `status` 与 `end_date`（停用日即最后
       有效日，含）；**不**给 `medication_events.status` 加 `cancelled`（CHECK 仍是四值）
       ——「这次到点作废」由「计划已停用 + 事件尚未触达」表达。药名/剂量/时刻一格不动：
       停用是生命周期动作，不是内容修订。
    2. 停用后 `run_medication_scan` 只对 `status == 'active'` 的计划生成事件（该过滤
       本就在调度器里，本函数是让 `stopped` 真正可达的那一半）。
    3. **幂等**：已经是 `stopped` 时直接返回原行，不重复 flush、不动 `version`——
       版本号只表示「药单内容」的版本，停用是生命周期动作，重复停用必须 200。
    4. `reason` 只进审计与日志：`medication_plans` 没有备注列，也不为它加表加列。
    """
    if not isinstance(plan, MedicationPlan):
        raise MedicationValidationError(f"plan 必须是 MedicationPlan，收到 {type(plan).__name__}")
    if plan.status not in PLAN_STATUSES:
        raise MedicationValidationError(f"药单状态只能是 {PLAN_STATUSES}，收到 {plan.status!r}")
    if reason is not None and not isinstance(reason, str):
        raise MedicationValidationError(f"reason 必须是字符串或 None，收到 {type(reason).__name__}")
    cleaned_reason = reason.strip() if reason is not None else None
    if cleaned_reason is not None and len(cleaned_reason) > MAX_STOP_REASON_LENGTH:
        raise MedicationValidationError(
            f"reason 不能超过 {MAX_STOP_REASON_LENGTH} 字，收到 {len(cleaned_reason)} 字"
        )

    if plan.status == "stopped":
        logger.info(
            "medication_plan_stop_idempotent",
            extra={"plan_id": str(plan.plan_id), "reason": cleaned_reason or ""},
        )
        return plan

    plan.status = "stopped"
    today = today_in_shanghai()
    if plan.end_date is None or plan.end_date > today:
        # 「失效」要能被读出来，而不只是靠 status：停用日即最后有效日（含），与修订
        # 路径的 end_date 收口口径一致（讲义 §10.3）。已过期/已收口的不往回改。
        plan.end_date = today
    await session.flush()
    logger.info(
        "medication_plan_stopped",
        extra={
            "plan_id": str(plan.plan_id),
            "elder_id": str(plan.elder_id),
            "version": plan.version,
            "reason": cleaned_reason or "",
        },
    )
    return plan


# ------------------------------------------------------------------ FR-MED-09 到期复核与续方
#
# 口径三句话（PRD FR-MED-09；设计记录（第三轮缺口修复） §3）：
# 1. **到期是时间事实，不是状态**：`end_date` 一过，调度自然不再生成事件；不给
#    `status` 加 `expired`（那要让每个读 `status` 的地方都改判，历史行语义也会漂移）。
# 2. **复核是提醒，不是自动续方**：每日 08:00 扫 `end_date ∈ [today+1, today+7]` 的
#    active 计划，`review_notified_at IS NULL` 才建一条 `medication_review` 通知给
#    主治医生并回写该列——列即幂等闸门（通知被关掉也不会二次提醒同一药单）。
# 3. **续方 = 新开一条**：`start_date = 旧 end_date + 1`、`end_date = start + 30`、
#    `renewed_from_plan_id = 旧计划`；旧计划保持 `active` 自然到期，提醒链不改——
#    新计划 `start_date` 一到自然开始提醒（调度按日期过滤，不需要额外补偿逻辑）。

REVIEW_WINDOW_DAYS: Final[int] = 7
"""到期前多少天开始复核提醒（FR-MED-09：today+1 ~ today+7）。"""

RENEW_DURATION_DAYS: Final[int] = 30
"""续方默认时长：`end_date = start_date + 30`（FR-MED-09 口径）。"""

MEDICATION_REVIEW_KIND: Final[str] = "medication_review"
"""到期复核通知 kind（0020 扩的第六值，与 `notification_service.NOTIFICATION_KINDS` 同源）。"""

_ATTENDING_DOCTOR_RELATION: Final[str] = "doctor"
"""主治医生在 `elder_staff_assignments.relation` 的取值（与 `_RELATION_BY_ROLE` 同源）。"""


@dataclass(frozen=True, slots=True)
class PlanReviewOutcome:
    """一次到期复核扫描的结果（调度器只记日志/指标，不读库）。"""

    due: int = 0
    """本次新发的「7 天内到期」复核提醒条数。"""
    expired: int = 0
    """本次处理的「已过期未续」计划条数（含已在催的）。"""
    escalated: int = 0
    """本次被升到红档的条数。"""
    created: int = 0
    """本次新建的通知总数（`due` + 新开的红档单）。"""


def _resident_plans(*conditions: Any) -> Any:
    """当前在住长者的 active 用药计划（`away`/`transferred`/`archived` 一律排除）。

    退房后不该再产生用药提醒（FR-ADM-09）：事件扫描与复核扫描共用同一口径，
    两处各写一遍 `Elder.status == 'active'` 迟早漏一处。
    """
    return (
        select(MedicationPlan)
        .where(
            MedicationPlan.status == "active",
            MedicationPlan.end_date.is_not(None),
            MedicationPlan.elder_id.in_(select(Elder.elder_id).where(Elder.status == "active")),
            *conditions,
        )
        .order_by(MedicationPlan.end_date, MedicationPlan.plan_id)
    )


async def _attending_doctor(session: AsyncSession, elder_id: uuid.UUID) -> uuid.UUID | None:
    """该长者的主治医生（`elder_staff_assignments.relation='doctor'`）；未绑定返回 None。

    与 `notification_service._primary_care_staff` 同一种查法，但 relation 不同：
    `primary_care` 是护工，责任医生是 `doctor`。刻意不塞进通知服务——建单的人
    决定派给谁，服务层不该替业务猜收件人。
    """
    result = await session.execute(
        select(ElderStaffAssignment.staff_id)
        .where(
            ElderStaffAssignment.elder_id == elder_id,
            ElderStaffAssignment.relation == _ATTENDING_DOCTOR_RELATION,
        )
        .order_by(ElderStaffAssignment.staff_id)
        .limit(1)
    )
    return result.scalar_one_or_none()


def _review_payload(
    *, plan: MedicationPlan, business_date: datetime.date, expired: bool
) -> dict[str, Any]:
    """复核通知的 payload：与既有通知一致，业务日期放 `record_date`（去重键的一段）。"""
    end_date = plan.end_date
    label = end_date.isoformat() if end_date is not None else "未知"
    days_to_expiry = (end_date - business_date).days if end_date is not None else 0
    return {
        "module": "medication",
        "plan_id": str(plan.plan_id),
        "elder_id": str(plan.elder_id),
        "drug_name": plan.drug_name,
        "dose": plan.dose,
        "schedule_cron": plan.schedule_cron,
        "end_date": label,
        "days_to_expiry": days_to_expiry,
        "expired": expired,
        "record_date": business_date.isoformat(),
        "summary": (
            f"{plan.drug_name} 已于 {label} 到期且未续方，请续方或停用"
            if expired
            else f"{plan.drug_name} 将于 {label} 到期，请确认是否续方"
        ),
    }


async def _find_open_review_notification(
    session: AsyncSession, *, plan: MedicationPlan
) -> Notification | None:
    """同一药单还开着的复核通知（按 `payload.plan_id` 找，不看人类可读摘要）。"""
    from app.services.notification_service import CLOSED_NOTIFICATION_STATUSES

    result = await session.execute(
        select(Notification)
        .where(
            Notification.kind == MEDICATION_REVIEW_KIND,
            Notification.elder_id == plan.elder_id,
            Notification.status.notin_(CLOSED_NOTIFICATION_STATUSES),
            Notification.payload["plan_id"].astext == str(plan.plan_id),
        )
        .order_by(Notification.created_at.desc(), Notification.notification_id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _create_review_notification(
    session: AsyncSession,
    *,
    plan: MedicationPlan,
    business_date: datetime.date,
    severity: str,
    expired: bool = False,
) -> Notification:
    """建一条到期复核通知并派给主治医生（写库的唯一路径仍在本服务）。

    去重键 = `(plan_id, 到期日, 业务日)`（FR-ALT-04）：前两段是业务身份，第三段是
    「今天这份」——已过期未续的药单在被关单后**次日**还要能再催，键里不带日期就会
    被唯一索引永久钉死在第一天的单上。同一天重跑扫描则由这个键直接吃成一条。
    """
    from app.services import notification_service

    doctor = await _attending_doctor(session, plan.elder_id)
    end_date = plan.end_date.isoformat() if plan.end_date is not None else "none"
    return await notification_service.create_notification(
        session,
        kind=MEDICATION_REVIEW_KIND,
        severity=severity,
        elder_id=plan.elder_id,
        payload=_review_payload(plan=plan, business_date=business_date, expired=expired),
        assignee_staff_id=doctor,
        dedupe_key=f"medication_review:{plan.plan_id}:{end_date}:{business_date.isoformat()}",
    )


async def notify_plan_reviews(
    session: AsyncSession,
    *,
    today: datetime.date | None = None,
) -> PlanReviewOutcome:
    """每日 08:00 的到期复核扫描（FR-MED-09）：到期待复核 + 已过期未续催办。

    两批查询各自幂等，但手法不同：

    - **7 天内到期**：只取 `review_notified_at IS NULL` 的计划，建单后立刻回写该列，
      同一条计划因此一生只提醒一次；通知被 acknowledged 关掉也不会重来。
    - **已过期未续**：旧计划保持 `active` 自然到期，所以「过期」不是一次性事实，
      必须能每天催。去重不靠新列而靠**既有未关单**：同一药单还有开着的
      `medication_review` 就往红档升级（`escalate_severity`），只有单被关掉之后
      才会再建一条新的——照护台看到的是「一条还在催的单」，不是每天一叠重复卡片。
      已经是红档就本日跳过（每天最多催一次的上界）。

    续过方的旧计划（有子计划 `renewed_from_plan_id` 指向它）不再催办：处理过的事实
    不该继续报警。退房/归档长者的计划也不扫（FR-ADM-09）。

    只 flush 不 commit：事务归调用方（调度任务或测试）。
    """
    from app.services import notification_service

    day = today if today is not None else today_in_shanghai()
    if isinstance(day, datetime.datetime) or not isinstance(day, datetime.date):
        raise MedicationValidationError(f"today 必须是日期（date），收到 {day!r}")

    window_end = day + datetime.timedelta(days=REVIEW_WINDOW_DAYS)
    now = datetime.datetime.now(tz=datetime.UTC)

    due_plans = (
        (
            await session.execute(
                _resident_plans(
                    MedicationPlan.end_date > day,
                    MedicationPlan.end_date <= window_end,
                    MedicationPlan.review_notified_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    created = 0
    for plan in due_plans:
        await _create_review_notification(session, plan=plan, business_date=day, severity="yellow")
        plan.review_notified_at = now
        created += 1

    renewed_plan_ids = select(MedicationPlan.renewed_from_plan_id).where(
        MedicationPlan.renewed_from_plan_id.is_not(None)
    )
    expired_plans = (
        (
            await session.execute(
                _resident_plans(
                    MedicationPlan.end_date < day,
                    MedicationPlan.plan_id.not_in(renewed_plan_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    escalated = 0
    for plan in expired_plans:
        open_notification = await _find_open_review_notification(session, plan=plan)
        if open_notification is None:
            await _create_review_notification(
                session, plan=plan, business_date=day, severity="red", expired=True
            )
            created += 1
            continue
        if open_notification.severity == "red":
            continue  # 已经在催同一个药单，本日不重复建单
        await notification_service.escalate_severity(
            session,
            open_notification,
            reason="medication_plan_expired_unrenewed",
            evidence={
                "plan_id": str(plan.plan_id),
                "end_date": plan.end_date.isoformat() if plan.end_date else None,
            },
        )
        escalated += 1

    await session.flush()
    outcome = PlanReviewOutcome(
        due=len(due_plans), expired=len(expired_plans), escalated=escalated, created=created
    )
    logger.info(
        "medication_plan_reviews_notified",
        extra={
            "business_date": day.isoformat(),
            "due": outcome.due,
            "expired": outcome.expired,
            "escalated": outcome.escalated,
            # 不能叫 "created"：LogRecord 已占用该属性名，extra 撞名会让整条日志抛 KeyError。
            "created_count": outcome.created,
        },
    )
    return outcome


async def _plan_has_renewal(session: AsyncSession, *, plan_id: uuid.UUID) -> bool:
    """这条计划是否已经被续过方（存在子计划指向它）。"""
    found = (
        await session.execute(
            select(MedicationPlan.plan_id)
            .where(MedicationPlan.renewed_from_plan_id == plan_id)
            .limit(1)
        )
    ).scalar_one_or_none()
    return found is not None


async def renew_plan(
    session: AsyncSession,
    *,
    plan: MedicationPlan,
    renewed_by: uuid.UUID | None = None,
) -> MedicationPlan:
    """一键续方（FR-MED-09）：**新开一条计划**，旧计划保持 `active` 自然到期。

    - `start_date = 旧 end_date + 1`（紧接着，不留空档）、
      `end_date = start_date + 30`（`RENEW_DURATION_DAYS`）。
    - `renewed_from_plan_id` 指回旧计划：续方链可回溯，也是「这条旧计划处理过」的判据。
    - 复制药名/剂量/服药时刻/长者；`created_by` 记续方医生（`renewed_by`），不传则沿用
      旧计划的开单医生——「谁续的方」是审计链的下一段，不能凭空归给系统。
    - **不改旧计划任何一列**：它按自己的 `end_date` 自然失效，提醒链完全不变；新计划
      `start_date` 一到，调度按日期过滤自然开始生成事件。
    - 为什么不开新 `version`：`version` 表达「同一药单的内容修订」，续方是**新医嘱**；
      混用会让版本链跨过到期边界，审计读不懂「这条 v2 是改剂量还是续方」。
    - 幂等：同一旧计划只能续一次（`MedicationPlanAlreadyRenewed` → 409），否则重复点
      按钮会让同一个服药时刻出现两条计划、两套事件键，长者被提醒两次。
    """
    if not isinstance(plan, MedicationPlan):
        raise MedicationValidationError(f"plan 必须是 MedicationPlan，收到 {type(plan).__name__}")
    if plan.end_date is None:
        raise MedicationValidationError("长期医嘱（end_date 为空）没有到期日，无需续方")
    if plan.status != "active":
        raise MedicationValidationError(
            f"用药计划已停用（status={plan.status}），不能续方：请新建一条药单"
        )
    if renewed_by is not None and not isinstance(renewed_by, uuid.UUID):
        raise MedicationValidationError(
            f"renewed_by 必须是 UUID 或 None，收到 {type(renewed_by).__name__}"
        )
    if await _plan_has_renewal(session, plan_id=plan.plan_id):
        raise MedicationPlanAlreadyRenewed(
            f"用药计划已续过方（plan_id={plan.plan_id}），不能重复续方"
        )

    start_date = plan.end_date + datetime.timedelta(days=1)
    end_date = start_date + datetime.timedelta(days=RENEW_DURATION_DAYS)
    renewed = MedicationPlan(
        elder_id=plan.elder_id,
        drug_name=plan.drug_name,
        dose=plan.dose,
        schedule_cron=plan.schedule_cron,
        start_date=start_date,
        end_date=end_date,
        version=1,
        status="active",
        created_by=renewed_by if renewed_by is not None else plan.created_by,
        renewed_from_plan_id=plan.plan_id,
    )
    session.add(renewed)
    await session.flush()
    logger.info(
        "medication_plan_renewed",
        extra={
            "plan_id": str(renewed.plan_id),
            "renewed_from_plan_id": str(plan.plan_id),
            "elder_id": str(plan.elder_id),
            "previous_end_date": plan.end_date.isoformat(),
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "created_by": str(renewed.created_by),
        },
    )
    return renewed


def _as_date(value: Any, *, field: str) -> datetime.date:
    """只收 `datetime.date`（`datetime.datetime` 是它的子类，得单独挡掉）。

    `schedule_cron` 用 `parse_schedule_cron` 管形状；日期用这里管类型：把 datetime
    直接塞进 `Date` 列，asyncpg 在 flush 时才会炸成 500，不如在这里报成 422。
    """
    if isinstance(value, datetime.datetime) or not isinstance(value, datetime.date):
        raise MedicationValidationError(f"{field} 必须是日期（date），收到 {value!r}")
    return value


# ------------------------------------------------------------------ FR-MED-07 批量导入药单
#
# 口径三句话（PRD:337 FR-MED-07；计划 §2 T4）：
#
# 1. **行级校验、全量预检通过才写**：任一行不合法就整体不写，把「行号 + 字段 + 原因」
#    原样回给调用方——做半截导入比不导入更难收拾（护工以为全进去了）。
# 2. **重复行去重**：计划层面按「同长者 + 同药名/剂量/时刻/区间」判重（文件内重复与
#    库里已有都算重复），跳过而不报错；事件层面仍由既有幂等键
#    `(plan_id, scheduled_at)` 保证不产生第二条提醒（AGENTS §8）。
# 3. **不引新依赖**：CSV 走标准库 `csv`，XLSX 走 `zipfile` + `ElementTree` 直读
#    `xl/sharedStrings.xml` / `xl/worksheets/*.xml` 的单元格值；样式、公式求值、
#    合并单元格一律不支持（药单模板用不到，也不值得为它引 openpyxl）。

IMPORT_MAX_ROWS: Final[int] = 200
"""单次导入最多数据行：再多就该拆文件（也避免一个事务里 flush 出上万行）。"""

IMPORT_MAX_FILE_BYTES: Final[int] = 5 * 1024 * 1024
IMPORT_MAX_XLSX_UNCOMPRESSED_BYTES: Final[int] = 8 * 1024 * 1024
"""上传体积与 XLSX 解压后体积双上限：zip bomb 的第一道闸。"""

IMPORT_TEMPLATE_HEADERS: Final[tuple[str, ...]] = (
    "elder_name",
    "drug_name",
    "dose",
    "schedule_cron",
    "start_date",
    "end_date",
)
"""模板列序（ASCII 列名与字段同名，出问题时肉眼对得上）。"""

IMPORT_TEMPLATE_SAMPLE_ROWS: Final[tuple[tuple[str, ...], ...]] = (
    ("张桂芬", "二甲双胍", "0.5g 1 片", "08:00", "2026-11-10", ""),
    ("张桂芬", "阿托伐他汀", "20mg 1 片", "20:00", "2026-11-10", "2026-12-10"),
)
"""两行示例：一行长期服（无停用日），一行带停用日（疗程）。"""

IMPORT_REQUIRED_COLUMNS: Final[tuple[str, ...]] = (
    "drug_name",
    "dose",
    "schedule_cron",
    "start_date",
)
"""缺任一列就不逐行解析了：列号对不上时报出来的「失败行」只会误导人。"""

IMPORT_HEADER_ALIASES: Final[dict[str, tuple[str, ...]]] = {
    "drug_name": ("drug_name", "drug", "medication", "药名", "药品", "药品名"),
    "dose": ("dose", "dosage", "剂量", "用量"),
    "schedule_cron": ("schedule_cron", "schedule", "time", "时刻", "服药时间", "每日时刻"),
    "start_date": ("start_date", "开始日期", "起始日", "起始日期"),
    "end_date": ("end_date", "结束日期", "停用日", "停用日期"),
    "elder_name": ("elder_name", "resident_name", "长者姓名", "长者", "姓名"),
    "elder_id": ("elder_id", "resident_id", "长者编号", "长者id"),
}
"""表头别名：中英文模板都能吃（医生拿 Excel 手搓一张表也能导）。"""

IMPORT_SKIP_DUPLICATE_IN_FILE: Final[str] = "duplicate_in_file"
IMPORT_SKIP_DUPLICATE_EXISTING: Final[str] = "duplicate_existing"
"""跳过原因码（机器可读；前端按码上色，文案在 API 层另给）。"""

_XLSX_NS: Final[str] = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_XLSX_ZIP_MAGIC: Final[bytes] = b"PK\x03\x04"
_XLSX_OLE_MAGIC: Final[bytes] = b"\xd0\xcf\x11\xe0"
_EXCEL_EPOCH: Final[datetime.date] = datetime.date(1899, 12, 30)
"""Excel 序列日期的零点（1900 闰年 bug 之后的实际口径）。"""

_COLUMN_LETTERS: Final[str] = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


class MedicationImportRejected(MedicationValidationError):
    """预检未通过却调了 `apply_import`：整批拒绝，`issues` 就是失败行清单。

    单独一个类是为了让调用方能用 `except MedicationValidationError` 兜底的同时，还能
    `except MedicationImportRejected as exc: exc.issues` 拿到「哪一行、哪个字段」。
    """

    def __init__(self, issues: Sequence[ImportIssue]) -> None:
        self.issues: tuple[ImportIssue, ...] = tuple(issues)
        head = "；".join(
            f"第 {issue.line} 行 {issue.field}：{issue.message}" for issue in self.issues[:3]
        )
        more = f"（共 {len(self.issues)} 处问题）" if len(self.issues) > 3 else ""
        super().__init__(f"导入预检未通过，整批未写入：{head}{more}")


@dataclass(frozen=True, slots=True)
class ImportIssue:
    """一处行级/文件级问题。`line=0` 表示「整个文件」而不是某一行。"""

    line: int
    field: str
    message: str


@dataclass(frozen=True, slots=True)
class ImportRow:
    """一行通过校验、可以去建计划的药单（值已规范化）。"""

    line: int
    elder_id: uuid.UUID
    elder_name: str
    drug_name: str
    dose: str
    schedule_cron: str
    start_date: datetime.date
    end_date: datetime.date | None = None


@dataclass(frozen=True, slots=True)
class ImportSkip:
    """一行合法但重复：不写库，但要告诉调用方「这里被跳过了」。"""

    line: int
    drug_name: str
    reason: str
    message: str
    plan_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class ImportPreview:
    """预检结果：`rows` 是「将写入」的行，`skips` 是重复行，`issues` 是拦下来的行。"""

    rows: tuple[ImportRow, ...]
    skips: tuple[ImportSkip, ...]
    issues: tuple[ImportIssue, ...]
    total_rows: int
    elder_ids: tuple[uuid.UUID, ...] = ()

    @property
    def accepted(self) -> bool:
        """全量预检通过（`issues` 为空）才允许写库。"""
        return not self.issues

    @property
    def created_count(self) -> int:
        return len(self.rows)


@dataclass(frozen=True, slots=True)
class ImportedPlan:
    """一条新建的计划 + 它来自哪一行：失败/成功都以行号为锚回显给调用方。"""

    line: int
    elder_id: uuid.UUID
    elder_name: str
    plan: MedicationPlan


@dataclass(frozen=True, slots=True)
class ImportResult:
    """真正写库之后的结果：`created` 是新建的计划行（带行号）。"""

    total_rows: int
    created: tuple[ImportedPlan, ...]
    skips: tuple[ImportSkip, ...]
    issues: tuple[ImportIssue, ...] = ()

    @property
    def accepted(self) -> bool:
        return not self.issues


# ------------------------------------------------------------------ 表头与单元格


def _normalise_header(value: str) -> str:
    """表头归一：去空白、去下划线、转小写（`Drug_Name` / `药 名` 都能对上）。"""
    return value.strip().lower().replace(" ", "").replace("\u3000", "").replace("_", "")


_IMPORT_HEADER_LOOKUP: Final[dict[str, str]] = {
    _normalise_header(alias): column
    for column, aliases in IMPORT_HEADER_ALIASES.items()
    for alias in aliases
}
"""`归一表头 → 字段名` 反查表（含中英文别名）。"""


def _cell(cells: Sequence[str], mapping: Mapping[str, int], column: str) -> str:
    """按「字段名 → 列号」取单元格；缺列或行太短都当空字符串。"""
    index = mapping.get(column)
    if index is None or index >= len(cells):
        return ""
    return cells[index].strip()


def _map_import_headers(cells: Sequence[str]) -> tuple[dict[str, int], list[ImportIssue]]:
    """表头行 → `字段名 → 列号`；未知列忽略（模板以后加列不炸旧代码）。"""
    mapping: dict[str, int] = {}
    issues: list[ImportIssue] = []
    for index, raw in enumerate(cells):
        column = _IMPORT_HEADER_LOOKUP.get(_normalise_header(raw))
        if column is None:
            continue
        if column in mapping:
            issues.append(
                ImportIssue(line=0, field=column, message=f"表头「{raw.strip()}」重复出现")
            )
            continue
        mapping[column] = index
    for column in IMPORT_REQUIRED_COLUMNS:
        if column not in mapping:
            issues.append(
                ImportIssue(
                    line=0,
                    field=column,
                    message=(
                        f"缺少必需列「{column}」；模板列：{'、'.join(IMPORT_TEMPLATE_HEADERS)}"
                    ),
                )
            )
    return mapping, issues


def _parse_import_date(value: str, *, field: str) -> datetime.date:
    """导入用的日期解析：`YYYY-MM-DD` / `YYYY/M/D` / `YYYY.M.D` / `YYYY年M月D日` / Excel 序列号。"""
    text = value.strip()
    if not text:
        raise MedicationValidationError(f"{field} 不能为空")
    serial = _excel_serial_date(text)
    if serial is not None:
        return serial
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    normalised = text.replace("年", "-").replace("月", "-").replace("日", "")
    try:
        return datetime.datetime.strptime(normalised, "%Y-%m-%d").date()
    except ValueError as exc:
        raise MedicationValidationError(
            f"{field}「{value}」不是合法日期，请用 YYYY-MM-DD（例：2026-11-10）"
        ) from exc


def _excel_serial_date(text: str) -> datetime.date | None:
    """5 位纯数字按 Excel 序列日期解释（`45201` 这类单元格值），其余返回 None。"""
    if not re.fullmatch(r"\d{5}(?:\.\d+)?", text):
        return None
    days = int(float(text))
    if not 20000 <= days <= 80000:  # 1954–2119，超出这个范围不可能是处方日期
        return None
    return _EXCEL_EPOCH + datetime.timedelta(days=days)


# ------------------------------------------------------------------ 文件读取（CSV / XLSX）


def _decode_import_text(payload: bytes) -> str:
    """解码顺序：UTF-16 BOM → UTF-8（含 BOM）→ GB18030（Excel 中文另存的常见编码）。"""
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        return payload.decode("utf-16")
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise MedicationValidationError("CSV 编码无法识别：请另存为 UTF-8（带 BOM）或 GB18030")


def _csv_rows(payload: bytes) -> list[tuple[int, list[str]]]:
    """CSV → `(物理行号, 单元格)`；空行丢弃，行号取该记录最后一行的物理行号。"""
    reader = csv.reader(io.StringIO(_decode_import_text(payload)))
    rows: list[tuple[int, list[str]]] = []
    for cells in reader:
        if not any(cell.strip() for cell in cells):
            continue
        rows.append((reader.line_num, [cell.strip() for cell in cells]))
    return rows


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    """`xl/sharedStrings.xml` → 字符串表（富文本把各 `<t>` 拼起来）。"""
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))  # noqa: S314 - 见下方说明
    return [
        "".join(node.text or "" for node in item.iter(f"{_XLSX_NS}t"))
        for item in root
        if item.tag == f"{_XLSX_NS}si"
    ]


def _xlsx_sheet_path(archive: zipfile.ZipFile) -> str:
    """取第一张工作表：`xl/worksheets/sheet*.xml` 按名字排序后的第一个。"""
    for name in sorted(archive.namelist()):
        if name.startswith("xl/worksheets/") and name.endswith(".xml"):
            return name
    raise MedicationValidationError("XLSX 里找不到工作表（xl/worksheets/*.xml）")


def _column_index(reference: str) -> int | None:
    """`B7` → 1（0 基列号）；引用不合法返回 None。"""
    letters = ""
    for char in reference:
        if not char.isalpha():
            break
        letters += char.upper()
    if not letters:
        return None
    total = 0
    for char in letters:
        if char not in _COLUMN_LETTERS:
            return None
        total = total * 26 + (_COLUMN_LETTERS.index(char) + 1)
    return total - 1


def _xlsx_cell_text(cell: ET.Element, shared: Sequence[str]) -> str:
    """单个单元格取文本：共享串 / 内联串 / 数值（日期序列号在解析日期时再转）。"""
    kind = cell.attrib.get("t", "n")
    if kind == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(f"{_XLSX_NS}t")).strip()
    value_node = cell.find(f"{_XLSX_NS}v")
    raw = (value_node.text or "") if value_node is not None else ""
    if kind == "s":
        try:
            return shared[int(raw)].strip()
        except (ValueError, IndexError):
            return ""
    return raw.strip()


def _xlsx_rows(payload: bytes) -> list[tuple[int, list[str]]]:
    """XLSX → `(Excel 行号, 单元格)`。

    只读单元格值：`t="s"` 查共享串、`t="inlineStr"` 读内联串、其余按数值文本原样带出
    （日期列存的是序列号，交给 `_parse_import_date` 按 5 位数字识别）。
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise MedicationValidationError("XLSX 文件损坏（不是有效的 zip 包）") from exc
    with archive:
        uncompressed = sum(info.file_size for info in archive.infolist())
        if uncompressed > IMPORT_MAX_XLSX_UNCOMPRESSED_BYTES:
            raise MedicationValidationError(
                f"XLSX 解压后 {uncompressed // 1024} KB，超过 "
                f"{IMPORT_MAX_XLSX_UNCOMPRESSED_BYTES // 1024} KB 上限"
            )
        shared = _xlsx_shared_strings(archive)
        root = ET.fromstring(archive.read(_xlsx_sheet_path(archive)))  # noqa: S314 - 见说明

    rows: list[tuple[int, list[str]]] = []
    for position, row in enumerate(root.iter(f"{_XLSX_NS}row"), start=1):
        raw_number = row.attrib.get("r", "")
        line = int(raw_number) if raw_number.isdigit() else position
        cells: dict[int, str] = {}
        for cell in row.iter(f"{_XLSX_NS}c"):
            index = _column_index(cell.attrib.get("r", ""))
            if index is None:
                continue
            cells[index] = _xlsx_cell_text(cell, shared)
        if not any(value.strip() for value in cells.values()):
            continue
        width = max(cells) + 1
        rows.append((line, [cells.get(index, "") for index in range(width)]))
    return rows


def _read_import_rows(payload: bytes) -> list[tuple[int, list[str]]]:
    """按文件头分派：zip → XLSX，OLE（`.xls`）明确拒绝，其余按文本 CSV 读。"""
    if not payload:
        raise MedicationValidationError("导入文件为空（0 字节）")
    if len(payload) > IMPORT_MAX_FILE_BYTES:
        raise MedicationValidationError(
            f"文件 {len(payload) // 1024} KB，超过 {IMPORT_MAX_FILE_BYTES // 1024} KB 上限"
        )
    if payload.startswith(_XLSX_ZIP_MAGIC):
        return _xlsx_rows(payload)
    if payload.startswith(_XLSX_OLE_MAGIC):
        raise MedicationValidationError("不支持 .xls（旧版二进制），请另存为 .xlsx 或 .csv")
    return _csv_rows(payload)


def build_import_template_csv() -> bytes:
    """模板 CSV（UTF-8 带 BOM）：Excel 双击打开中文不乱码，列名就是字段名。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(IMPORT_TEMPLATE_HEADERS)
    writer.writerows(IMPORT_TEMPLATE_SAMPLE_ROWS)
    return ("\ufeff" + buffer.getvalue()).encode("utf-8")


# ------------------------------------------------------------------ 长者解析


@dataclass(frozen=True, slots=True)
class _ElderRef:
    """导入时用到的长者最小画像（只要 id / 姓名 / 状态）。"""

    elder_id: uuid.UUID
    name: str
    status: str


async def _resolve_import_elders(
    session: AsyncSession,
    rows: Sequence[tuple[int, list[str]]],
    mapping: Mapping[str, int],
    *,
    default_elder_id: uuid.UUID | None = None,
) -> tuple[dict[uuid.UUID, _ElderRef], dict[str, _ElderRef], frozenset[str]]:
    """一次查回文件里出现的所有长者：按 id 索引、按姓名索引、重名集合。

    `default_elder_id`（页面当前所选长者）也要查回来：文件里可以整列不写长者，
    这时靠它兜底，不查就会误报「页面所选长者不存在」。
    """
    ids: set[uuid.UUID] = set()
    names: set[str] = set()
    if default_elder_id is not None:
        ids.add(default_elder_id)
    for _line, cells in rows:
        raw_id = _cell(cells, mapping, "elder_id")
        raw_name = _cell(cells, mapping, "elder_name")
        if raw_id:
            try:
                parsed = uuid.UUID(raw_id)
            except ValueError:
                parsed = None
            if parsed is not None:
                ids.add(parsed)
        if raw_name:
            names.add(raw_name)

    conditions = []
    if ids:
        conditions.append(Elder.elder_id.in_(ids))
    if names:
        conditions.append(Elder.name.in_(names))

    by_id: dict[uuid.UUID, _ElderRef] = {}
    by_name: dict[str, _ElderRef] = {}
    ambiguous: set[str] = set()
    if conditions:
        found = (
            await session.execute(
                select(Elder.elder_id, Elder.name, Elder.status).where(or_(*conditions))
            )
        ).all()
        for elder_id, name, status in found:
            ref = _ElderRef(elder_id=elder_id, name=name, status=status)
            by_id[elder_id] = ref
            if name in by_name:
                ambiguous.add(name)  # 同名两位长者：不允许按姓名导入，要求改用 elder_id
            else:
                by_name[name] = ref
    return by_id, by_name, frozenset(ambiguous)


def _resolve_row_elder(
    line: int,
    cells: Sequence[str],
    mapping: Mapping[str, int],
    *,
    elders_by_id: Mapping[uuid.UUID, _ElderRef],
    elders_by_name: Mapping[str, _ElderRef],
    ambiguous_names: frozenset[str],
    default_elder_id: uuid.UUID | None,
) -> tuple[uuid.UUID | None, str, list[ImportIssue]]:
    """定这一行属于哪位长者：优先 `elder_id`，其次姓名，最后用页面所选长者。"""
    raw_id = _cell(cells, mapping, "elder_id")
    raw_name = _cell(cells, mapping, "elder_name")
    issues: list[ImportIssue] = []
    elder_id: uuid.UUID | None = None
    elder_name = ""

    if raw_id:
        try:
            elder_id = uuid.UUID(raw_id)
        except ValueError:
            issues.append(ImportIssue(line, "elder_id", f"elder_id「{raw_id}」不是合法 UUID"))
            elder_id = None
        else:
            ref = elders_by_id.get(elder_id)
            if ref is None:
                issues.append(ImportIssue(line, "elder_id", "长者不存在（elders 表无此前缀）"))
                elder_id = None
            else:
                elder_name = ref.name

    if raw_name:
        if raw_name in ambiguous_names:
            issues.append(
                ImportIssue(
                    line, "elder_name", f"长者姓名「{raw_name}」对应多位长者，请改用 elder_id"
                )
            )
        else:
            ref = elders_by_name.get(raw_name)
            if ref is None:
                issues.append(ImportIssue(line, "elder_name", f"长者「{raw_name}」不存在"))
            elif elder_id is not None and ref.elder_id != elder_id:
                issues.append(
                    ImportIssue(line, "elder_name", "长者姓名与 elder_id 指向的不是同一位长者")
                )
            elif elder_id is None:
                elder_id, elder_name = ref.elder_id, ref.name

    if not raw_id and not raw_name:
        if default_elder_id is None:
            issues.append(
                ImportIssue(
                    line, "elder_name", "缺少长者标识：请填 elder_name / elder_id，或先选长者"
                )
            )
        else:
            ref = elders_by_id.get(default_elder_id)
            if ref is None:
                issues.append(ImportIssue(line, "elder_name", "页面所选长者不存在"))
            else:
                elder_id, elder_name = ref.elder_id, ref.name

    if default_elder_id is not None and elder_id is not None and elder_id != default_elder_id:
        issues.append(
            ImportIssue(
                line,
                "elder_name",
                "行内长者与页面所选长者不是同一位：请在对方页面导入，或清空所选后导入",
            )
        )
        elder_id = None

    if elder_id is not None:
        ref = elders_by_id.get(elder_id)
        if ref is not None and ref.status != "active":
            issues.append(
                ImportIssue(
                    line,
                    "elder_name",
                    f"长者「{ref.name}」状态为 {ref.status}，只有在住（active）长者能开药单",
                )
            )
            elder_id = None
    return elder_id, elder_name, issues


def _parse_import_row(
    line: int,
    cells: Sequence[str],
    mapping: Mapping[str, int],
    *,
    elders_by_id: Mapping[uuid.UUID, _ElderRef],
    elders_by_name: Mapping[str, _ElderRef],
    ambiguous_names: frozenset[str],
    default_elder_id: uuid.UUID | None,
) -> tuple[ImportRow | None, list[ImportIssue]]:
    """逐字段校验一行；字段级问题各报一条（一次改完，不用来回试）。"""
    elder_id, elder_name, issues = _resolve_row_elder(
        line,
        cells,
        mapping,
        elders_by_id=elders_by_id,
        elders_by_name=elders_by_name,
        ambiguous_names=ambiguous_names,
        default_elder_id=default_elder_id,
    )

    drug_name = ""
    try:
        drug_name = _normalise_text(
            _cell(cells, mapping, "drug_name"),
            field="drug_name",
            max_length=MAX_DRUG_NAME_LENGTH,
        )
    except MedicationValidationError as exc:
        issues.append(ImportIssue(line, "drug_name", str(exc)))

    dose = ""
    try:
        dose = _normalise_text(
            _cell(cells, mapping, "dose"), field="dose", max_length=MAX_DOSE_LENGTH
        )
    except MedicationValidationError as exc:
        issues.append(ImportIssue(line, "dose", str(exc)))

    schedule_cron = _cell(cells, mapping, "schedule_cron").strip()
    try:
        parse_schedule_cron(schedule_cron)
    except MedicationValidationError as exc:
        issues.append(ImportIssue(line, "schedule_cron", str(exc)))

    start_date: datetime.date | None = None
    try:
        start_date = _parse_import_date(_cell(cells, mapping, "start_date"), field="start_date")
    except MedicationValidationError as exc:
        issues.append(ImportIssue(line, "start_date", str(exc)))

    end_date: datetime.date | None = None
    raw_end = _cell(cells, mapping, "end_date")
    if raw_end:
        try:
            end_date = _parse_import_date(raw_end, field="end_date")
        except MedicationValidationError as exc:
            issues.append(ImportIssue(line, "end_date", str(exc)))

    if start_date is not None and end_date is not None and end_date < start_date:
        issues.append(ImportIssue(line, "end_date", "end_date 不能早于 start_date"))

    if issues or elder_id is None or start_date is None:
        return None, issues
    return (
        ImportRow(
            line=line,
            elder_id=elder_id,
            elder_name=elder_name,
            drug_name=drug_name,
            dose=dose,
            schedule_cron=schedule_cron,
            start_date=start_date,
            end_date=end_date,
        ),
        issues,
    )


# ------------------------------------------------------------------ 幂等：计划层判重


def _plan_identity_key(
    *,
    elder_id: uuid.UUID,
    drug_name: str,
    dose: str,
    schedule_cron: str,
    start_date: datetime.date,
    end_date: datetime.date | None,
) -> tuple[Any, ...]:
    """计划层「同一条药单」的判重键（与事件层幂等键互补，不替代它）。"""
    return (elder_id, drug_name, dose, schedule_cron, start_date, end_date)


async def _existing_plan_index(
    session: AsyncSession, elder_ids: Sequence[uuid.UUID]
) -> dict[tuple[Any, ...], tuple[uuid.UUID, str]]:
    """把这些长者已有药单按判重键索引：`键 → (plan_id, status)`。停用的也算占用。"""
    unique_ids = list(dict.fromkeys(elder_ids))
    if not unique_ids:
        return {}
    rows = (
        await session.execute(
            select(
                MedicationPlan.plan_id,
                MedicationPlan.elder_id,
                MedicationPlan.drug_name,
                MedicationPlan.dose,
                MedicationPlan.schedule_cron,
                MedicationPlan.start_date,
                MedicationPlan.end_date,
                MedicationPlan.status,
            ).where(MedicationPlan.elder_id.in_(unique_ids))
        )
    ).all()
    index: dict[tuple[Any, ...], tuple[uuid.UUID, str]] = {}
    for plan_id, elder_id, drug, dose, cron, start, end, status in rows:
        key = _plan_identity_key(
            elder_id=elder_id,
            drug_name=drug,
            dose=dose,
            schedule_cron=cron,
            start_date=start,
            end_date=end,
        )
        index[key] = (plan_id, status)
    return index


# ------------------------------------------------------------------ 预检 / 写库


async def preview_import(
    session: AsyncSession,
    *,
    payload: bytes,
    elder_id: uuid.UUID | None = None,
) -> ImportPreview:
    """读文件 + 逐行校验 + 判重，**不写库**；`accepted` 为真才轮到 `apply_import`。

    `elder_id` 是「页面当前所选长者」：行内没写长者时用它，行内写了别的人则报错——
    从某人页面导入别人的药单几乎一定是点错了，静默写进去最难查。
    """
    rows = _read_import_rows(payload)
    if not rows:
        return ImportPreview(
            rows=(),
            skips=(),
            issues=(ImportIssue(0, "<file>", "文件里没有任何内容"),),
            total_rows=0,
        )

    header_line, header = rows[0]
    mapping, issues = _map_import_headers(header)
    data = rows[1:]
    if len(data) > IMPORT_MAX_ROWS:
        issues.append(
            ImportIssue(
                0,
                "<file>",
                f"数据行 {len(data)} 超过单次上限 {IMPORT_MAX_ROWS} 行，请拆分后分批导入",
            )
        )
    elif not data:
        issues.append(ImportIssue(header_line, "<file>", "只有表头，没有数据行"))
    if issues:
        return ImportPreview(rows=(), skips=(), issues=tuple(issues), total_rows=len(data))

    elders_by_id, elders_by_name, ambiguous = await _resolve_import_elders(
        session, data, mapping, default_elder_id=elder_id
    )
    parsed: list[ImportRow] = []
    skips: list[ImportSkip] = []
    issues = []
    seen: dict[tuple[Any, ...], int] = {}

    for line, cells in data:
        row, row_issues = _parse_import_row(
            line,
            cells,
            mapping,
            elders_by_id=elders_by_id,
            elders_by_name=elders_by_name,
            ambiguous_names=ambiguous,
            default_elder_id=elder_id,
        )
        if row_issues:
            issues.extend(row_issues)
        if row is None:
            continue
        key = _plan_identity_key(
            elder_id=row.elder_id,
            drug_name=row.drug_name,
            dose=row.dose,
            schedule_cron=row.schedule_cron,
            start_date=row.start_date,
            end_date=row.end_date,
        )
        first_line = seen.get(key)
        if first_line is not None:
            skips.append(
                ImportSkip(
                    line=line,
                    drug_name=row.drug_name,
                    reason=IMPORT_SKIP_DUPLICATE_IN_FILE,
                    message=f"与第 {first_line} 行内容完全相同，已跳过",
                )
            )
            continue
        seen[key] = line
        parsed.append(row)

    existing = await _existing_plan_index(session, [row.elder_id for row in parsed])
    writable: list[ImportRow] = []
    elder_ids: list[uuid.UUID] = []
    for row in parsed:
        if row.elder_id not in elder_ids:
            elder_ids.append(row.elder_id)
        key = _plan_identity_key(
            elder_id=row.elder_id,
            drug_name=row.drug_name,
            dose=row.dose,
            schedule_cron=row.schedule_cron,
            start_date=row.start_date,
            end_date=row.end_date,
        )
        hit = existing.get(key)
        if hit is not None:
            plan_id, status = hit
            skips.append(
                ImportSkip(
                    line=row.line,
                    drug_name=row.drug_name,
                    reason=IMPORT_SKIP_DUPLICATE_EXISTING,
                    message=(
                        "库中已有同内容药单"
                        f"（{'启用中' if status == 'active' else '已停用'}），未重复创建"
                    ),
                    plan_id=plan_id,
                )
            )
            continue
        writable.append(row)

    logger.info(
        "medication_plan_import_previewed",
        extra={
            "total_rows": len(data),
            "writable": len(writable),
            "skipped": len(skips),
            "issues": len(issues),
            "elder_ids": [str(value) for value in elder_ids],
        },
    )
    return ImportPreview(
        rows=tuple(writable),
        skips=tuple(skips),
        issues=tuple(issues),
        total_rows=len(data),
        elder_ids=tuple(elder_ids),
    )


async def apply_import(
    session: AsyncSession,
    *,
    preview: ImportPreview,
    created_by: uuid.UUID,
) -> ImportResult:
    """把预检通过的行写进库：**全量通过才写**，任一行不合法连一行都不写。

    写之前再查一次库：预检与写入之间若有人手工加了同内容药单，这里也要跳过而不是
    撞 `uq_*` 之外的重复（计划没有唯一约束，重复只能靠查）。

    写库整体套一层 SAVEPOINT：中途任一行抛错就回滚到进入本函数前的状态，绝不留半截
    药单。不能只指望调用方回滚——api 层会把服务层异常吞成响应对象，依赖看到的是正常
    return，外层事务照样 commit。
    """
    if not isinstance(preview, ImportPreview):
        raise MedicationValidationError("preview 必须是 ImportPreview")
    if not preview.accepted:
        raise MedicationImportRejected(preview.issues)
    if not isinstance(created_by, uuid.UUID):
        raise MedicationValidationError("created_by 必须是 UUID（谁导入的）")

    skips = list(preview.skips)
    created: list[ImportedPlan] = []
    if preview.rows:
        existing = await _existing_plan_index(session, [row.elder_id for row in preview.rows])
        # 「全量成功或全量不落」在这里落锁，而不是靠调用方的事务边界：api 层会把服务
        # 异常吞成响应对象（`_service_error`），依赖看到的是正常 return，外层照样 commit。
        try:
            async with session.begin_nested():
                for row in preview.rows:
                    key = _plan_identity_key(
                        elder_id=row.elder_id,
                        drug_name=row.drug_name,
                        dose=row.dose,
                        schedule_cron=row.schedule_cron,
                        start_date=row.start_date,
                        end_date=row.end_date,
                    )
                    hit = existing.get(key)
                    if hit is not None:
                        plan_id, status = hit
                        skips.append(
                            ImportSkip(
                                line=row.line,
                                drug_name=row.drug_name,
                                reason=IMPORT_SKIP_DUPLICATE_EXISTING,
                                message=(
                                    "预检之后库中出现了同内容药单"
                                    f"（{'启用中' if status == 'active' else '已停用'}），未重复创建"
                                ),
                                plan_id=plan_id,
                            )
                        )
                        continue
                    plan = await create_plan(
                        session,
                        elder_id=row.elder_id,
                        drug_name=row.drug_name,
                        dose=row.dose,
                        schedule_cron=row.schedule_cron,
                        start_date=row.start_date,
                        end_date=row.end_date,
                        created_by=created_by,
                    )
                    created.append(
                        ImportedPlan(
                            line=row.line,
                            elder_id=row.elder_id,
                            elder_name=row.elder_name,
                            plan=plan,
                        )
                    )
                    existing[key] = (plan.plan_id, plan.status)
        except Exception:
            logger.warning(
                "medication_plan_import_rolled_back",
                extra={
                    "total_rows": preview.total_rows,
                    "created_before_failure": len(created),
                },
                exc_info=True,
            )
            raise

    logger.info(
        "medication_plan_import_applied",
        extra={
            "total_rows": preview.total_rows,
            # 键名不能叫 `created`：它是 LogRecord 的保留属性（记录创建时间戳），
            # logging.makeRecord 见到同名键会直接抛 KeyError("Attempt to overwrite 'created'")。
            # 用 `created_count` 与 created_before_failure 保持同一套命名。
            "created_count": len(created),
            "skipped": len(skips),
            "created_by": str(created_by),
        },
    )
    return ImportResult(
        total_rows=preview.total_rows,
        created=tuple(created),
        skips=tuple(skips),
        issues=preview.issues,
    )


# ------------------------------------------------------------------ FR-MED-07 多药合并卡片


@dataclass(frozen=True, slots=True)
class MergedDrug:
    """合并卡片里的一味药。"""

    plan_id: uuid.UUID
    drug_name: str
    dose: str

    @property
    def label(self) -> str:
        return f"{self.drug_name} {self.dose}"


@dataclass(frozen=True, slots=True)
class MergedSlot:
    """同一时刻的多种药合并成一张卡片（一次播报说清「这个点吃什么」）。"""

    schedule_cron: str
    on_date: datetime.date
    drugs: tuple[MergedDrug, ...]

    @property
    def drug_count(self) -> int:
        return len(self.drugs)

    @property
    def summary(self) -> str:
        return f"{self.schedule_cron} 共 {self.drug_count} 种药：{'、'.join(drug.label for drug in self.drugs)}"


def today_in_shanghai() -> datetime.date:
    """上海时区的今天（`schedule_cron` 与服药区间都按本地日算）。"""
    return datetime.datetime.now(datetime.UTC).astimezone(SHANGHAI).date()


def merge_active_plans(
    plans: Sequence[MedicationPlan], *, on_date: datetime.date | None = None
) -> list[MergedSlot]:
    """把某长者当日在服的药单按时刻合并成卡片，按时刻升序。

    只用 `active` 且区间覆盖 `on_date` 的计划；合并**只影响展示与播报**，不改事件粒度：
    每味药仍各自建 `medication_events` 行、各自确认（PRD FR-MED-07 只要「合并卡片」）。
    同一次到点里药名重复（同药不同剂量）照原样列出——剂量不同就是不同的医嘱，
    合并展示不等于合并成一条。
    """
    target = on_date or today_in_shanghai()
    grouped: dict[str, list[MergedDrug]] = {}
    for plan in plans:
        if plan.status != "active":
            continue
        if plan.start_date > target:
            continue
        if plan.end_date is not None and target > plan.end_date:
            continue
        grouped.setdefault(plan.schedule_cron, []).append(
            MergedDrug(plan_id=plan.plan_id, drug_name=plan.drug_name, dose=plan.dose)
        )
    slots: list[MergedSlot] = []
    for schedule_cron in sorted(grouped):
        drugs = sorted(grouped[schedule_cron], key=lambda item: (item.drug_name, item.dose))
        slots.append(MergedSlot(schedule_cron=schedule_cron, on_date=target, drugs=tuple(drugs)))
    return slots
