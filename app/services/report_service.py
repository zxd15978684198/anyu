"""家属日报领域服务：健康统计聚合 + 用药占位 + 幂等生成（PRD §4.8 FR-RPT-01..04）。

## 为什么 payload 是整块 JSONB

日报是**展示物**，不是业务主数据：健康块的字段会随 迭代 9（趋势/趋势图）与
迭代 7（用药事件）迭代，拆成列就要反复改迁移。所以这里只承诺「payload 的结构
由本文件产出」，读取方按块取值。

## 用药块的口径（原占位已回填）

迭代 8 落地时 `medication_events` 还不存在（该表属 迭代 7 批次，S-D8-2 / S-D8-3），
当时只能输出占位块 `{"state": "pending_day7", ...}`。迭代 7 的表与服务落地后，
本文件按当时留的承诺**只改聚合与本函数**（不动表结构、不动 API）接上真实统计：

```json
{"module": "medication", "state": "collected",
 "counts": {"taken_reported": 2, "refused": 0, "missed_unconfirmed": 1,
            "notification_failed": 0, "pending": 0},
 "total": 3, "plans_active": 2}
```

四类终态取自 TD §3.4 的 `terminal_state` 取值，**分列不合并**（PRD FR-RPT-01
明令不许合并成「实际漏服」）；`pending` 是「到点流程还没结束」的计数，来自
`terminal_state IS NULL`，同样不并入任何一类。统计窗口按 `scheduler_timezone`
的自然日折算成左闭右开的 UTC 时刻（见 `_day_window`）。

`MEDICATION_LEGACY_STATE` 保留了旧占位值，只给回填脚本 `scripts/backfill_report_medication.py`
识别「还没回填的历史行」用；**新日报不再产出这个值**。

## 幂等与补录版本（FR-RPT-01 幂等 + FR-RPT-03 版本更新）

`daily_reports` 有 UNIQUE (elder_id, report_date, version)，`version` 默认 1。
`generate()` 先算一遍**这次会写进 payload 的内容**再决定：

- 与最新版逐字段相同 → 原样返回那一行（不产第二版）。「重复生成返回已有行」是
  FR-RPT-01 的验收项，不能变成 500，也不能产出第二行；
- 内容变了（补录了健康数据、用药事件补齐）→ `version = 最新版 + 1` 插新行，旧版本
  保留（AC-25「保留历史版本」）。「有效补录」= 聚合结果真的变化，**不是**每次调用都
  加一版：没有新数据的重试必须是幂等的。

并发下两个请求可能同时算到同一版号，`begin_nested()` 兜住唯一键冲突后按「返回已有行」
收口；列表与读取一律取**该日最新版本**（`_find` 里 `order_by version desc`）。

## 推送（FR-RPT-03 / AC-25）

生成即建 `family_report` 通知（`notification_service.notify_daily_report`，同事务）。
幂等键 `(elder_id, report_id, version)`：同一版重跑不会重复建；补录的新版各发一次
「已更新」。收件人 = 该长者 `elder_guardians.status='active'` 的家属，一个都没有就
不建（没人能读的通知等于假装送达）。1/5/15 三次重试与失败落 `failed` 全在通知服务里，
本文件不重复实现。

## 权限

- admin：全量；
- doctor：只看**自己负责**的长者（复用 `elder_service` 的 assignment 口径）；
- care_worker：日报读数**不在**其能力面（照护台 W4 口径：admin/doctor）；
- 家属：只读**自己绑定**的长者（`elder_guardians.status='active'`，W3 裁决），
  越权 403 `SCOPE_FORBIDDEN`、成功 200，两条都**落一条 audit_logs**（FR-RPT-02），
  `actor_type='guardian'`。范围判定只有一份实现
  （`elder_service.require_report_read_scope`），本文件不重复它，只负责把结论翻译成审计；
- 家属读**档案**仍一律 403（裁决 A-01 / 契约 O6′）：那道闸在 `elder_service`，
  `action='archive_read'` 至今不给家属放行。

## 语音条（FR-RPT-04，迭代 8 预留的 `audio_url` 在这一版落地）

生成日报时**同步**合成语音摘要：播报文本由 `_summary_text()` 从**刚落库的
`payload["health"]` / `payload["medication"]`** 现推，与家属端看到的文本块是同一份
快照、同一套措辞——PRD:412 那句「内容与文本摘要一致」不靠人肉对齐，也不靠再多存一份
迟早分叉的字符串（`payload` 的键集是家属端契约，见 `_build_payload`）。PCM 由
`app.audio.tts.build_tts()` 产出（AGENTS §4.8：语音合成只有这一套实现，这里不另造
provider），封装成 16kHz/16bit/单声道 WAV 落盘，`audio_url` 写**接口地址**而不是静态
目录——音频是长者的健康数据，读之前必须过与 `get_report` 同一道闸（`read_audio` 直接
复用 `get_report`，不新增第二种范围判定）。

**音频失败不得连坐日报**：TTS 不可用、写盘失败都只记日志并留 `audio_url=NULL`，日报照
常生成。语音条是增强项，不是生成日报的前置条件。

**复用路径同样补齐**：`generate()` 命中已有日报时走「复用即返回」，早返回前会调一次
`_ensure_report_audio()`——迭代 8 之前生成的历史行因此第一次被取回时补上语音。判据是
「地址在**且**文件也在」：两者都满足就不重复合成（幂等），文件被清理过则重建。
"""

from __future__ import annotations

import asyncio
import datetime
import io
import logging
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import RoleForbiddenError
from app.core.config import get_settings
from app.core.context import get_trace_id
from app.core.runtime_context import Identity
from app.core.security import KIND_GUARDIAN, KIND_STAFF
from app.db import session_factory
from app.models import AuditLog, DailyReport, Elder, HealthRecord
from app.services import elder_service, medication_service, notification_service
from app.services.elder_service import ElderScopeForbiddenError

logger = logging.getLogger(__name__)

#: 旧占位块的状态值（S-D8-3 的历史物）。只用于识别**回填前**生成的历史日报，
#: 新日报一律输出真实统计；`scripts/backfill_report_medication.py` 靠它筛选目标行。
MEDICATION_LEGACY_STATE = "pending_day7"

#: `payload.sources.note`：数据来源的权威口径，前端原样展示（不翻译）。
SOURCES_NOTE = "健康统计仅含当前生效版本；用药统计只数当日到点事件，四类终态分列、不合并"

#: 读日报允许的角色（照护台口径）。care_worker 不在其中，见模块 docstring。
_REPORT_READING_ROLES: frozenset[str] = frozenset({"admin", "doctor"})

#: PRD §4.8 的固定说明。日报、文本摘要、语音摘要三处都带这一句，一个字都不许改。
REPORT_DISCLAIMER = "本报告为演示模拟信息，不构成医疗诊断。"

#: 语音条落盘根目录。`data/` 在 `.gitignore` 里，音频不进版本库。
#: 用模块常量而不是新增 settings 键（本卡不动 `app/core/config.py`）；测试通过
#: monkeypatch 指到 `tmp_path`，因此运行时一律经 `_audio_path()` 读它，不缓存副本。
REPORT_AUDIO_ROOT: Path = Path(__file__).resolve().parents[2] / "data" / "report_audio"

#: 点播地址：相对路径 + 查询参数日期。家属端用带 Bearer 的 fetch 取 blob 再播放，
#: 不用 `<audio src>` 裸开——裸开带不上 `Authorization`，只会拿到 401。
REPORT_AUDIO_URL_TEMPLATE = "/api/reports/{elder_id}/audio?date={report_date}"


class ReportServiceError(Exception):
    """日报领域错误基类。"""


class ReportNotFoundError(ReportServiceError):
    """日报不存在（或该日尚未生成）。"""


class ReportAudioNotFoundError(ReportServiceError):
    """该日日报没有可点播的语音条：未生成、生成失败，或文件已被清理。

    与 `ReportNotFoundError` 分开：日报存在而语音条缺失是**降级状态**，前端要区分
    「这份日报还没有」和「语音暂时播不了」，否则家属会以为整份日报丢了。
    """


@dataclass(frozen=True, slots=True)
class DailyReportView:
    """日报展示对象（API 层直接序列化，不暴露 ORM）。

    `elder_name` 是**读取时**从 `elders` 取的展示名，不写进 `payload`：
    - 契约《身份与权限》A-01 的替代方案明写「日报接口自带展示名」，家属据此
      在页面上显示「张桂芬的日报」而不是一串 UUID；
    - 不写进 payload 是为了让改名即时生效（payload 是生成时快照，历史客户端的
      旧日报不该停在改名前），也避免改动已落库的 payload 形状。
    """

    report_id: uuid.UUID
    elder_id: uuid.UUID
    elder_name: str
    report_date: datetime.date
    payload: dict[str, Any]
    audio_url: str | None
    generated_at: datetime.datetime


def _view(report: DailyReport, elder_name: str) -> DailyReportView:
    return DailyReportView(
        report_id=report.report_id,
        elder_id=report.elder_id,
        elder_name=elder_name,
        report_date=report.report_date,
        payload=report.payload,
        audio_url=report.audio_url,
        generated_at=report.generated_at,
    )


async def _elder_name(session: AsyncSession, elder_id: uuid.UUID) -> str:
    """取展示名（只读一列，不碰档案字段）。

    只取 `name`：裁决 A-01 的替代方案允许「日报接口自带展示名」，但没把年龄、
    病史、床位一起放开；要加字段先改契约，不要在这里顺手 SELECT *。
    行不存在时返回空串而不是抛错——日报已经生成过说明长者当时存在，
    展示名缺失不该让一份历史日报变成 500。
    """
    stmt = select(Elder.name).where(Elder.elder_id == elder_id)
    name = (await session.execute(stmt)).scalar_one_or_none()
    return name or ""


def _today() -> datetime.date:
    """调度与默认查询口径都按 `settings.scheduler_timezone` 的自然日。

    `health_records.record_date` 的注释明写「北京时间对应的自然日」，
    日报的 `report_date` 必须与它同一口径，否则跨零点会统计到错误的一天。
    """
    return datetime.datetime.now(ZoneInfo(get_settings().scheduler_timezone)).date()


# ------------------------------------------------------------------ 审计


async def _write_audit(
    *,
    actor_type: str,
    actor_id: uuid.UUID,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID,
    detail: dict[str, Any],
    result: str = "forbidden",
) -> None:
    """留痕：独立会话 + 独立事务写入。

    为什么不用请求会话：`get_session` 在异常路径上会 rollback，而越权拒绝**必然**
    抛异常——同事务写审计等于自己把证据抹掉。审计是「只增不改」的旁路记录
    （PRD AC-14），因此这里单开一个会话提交；失败只记日志，不能反向阻断 403。
    家属**成功**读取也走这里（W3）：成功的证据与越权同等重要，只是 `result` 不同。
    """
    try:
        async with session_factory() as audit_session:
            audit_session.add(
                AuditLog(
                    actor_type=actor_type,
                    actor_id=actor_id,
                    action=action,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    detail=detail,
                )
            )
            await audit_session.commit()
    except Exception:  # pragma: no cover - 审计写失败不能改变业务结论
        logger.exception("report audit write failed", extra={"audit_action": action})
    logger.info(
        "audit",
        extra={
            "trace_id": get_trace_id() or "-",
            "audit_action": action,
            "actor": f"{actor_type}:{actor_id}",
            "target": f"{resource_type}:{resource_id}",
            "result": result,
        },
    )


async def _require_report_scope(
    session: AsyncSession, identity: Identity, elder_id: uuid.UUID, *, action: str
) -> None:
    """日报读权闸门：先判主体，再复用 `elder_service` 的范围口径。

    家属的绑定判定**只有一份实现**（`elder_service.require_report_read_scope`）。
    这里若再写一遍 `elder_guardians` 查询，两处口径迟早分叉 —— 一处改了 `status`
    过滤、另一处没改，表现就是「解绑了还能读」。本函数只把结论翻译成审计：
    越权一条、成功一条，都在独立事务里落 `audit_logs`。
    """
    if identity.kind == KIND_GUARDIAN:
        guardian_id = uuid.UUID(identity.guardian_id or "")
        try:
            await elder_service.require_report_read_scope(
                session, identity, elder_id, audit_action=action
            )
        except ElderScopeForbiddenError:
            await _write_audit(
                actor_type="guardian",
                actor_id=guardian_id,
                action=action,
                resource_type="daily_report",
                resource_id=elder_id,
                detail={"trace_id": get_trace_id() or "-", "reason": "scope", "bound": False},
            )
            raise
        await _write_audit(
            actor_type="guardian",
            actor_id=guardian_id,
            action=action,
            resource_type="daily_report",
            resource_id=elder_id,
            detail={
                "trace_id": get_trace_id() or "-",
                "reason": "guardian_binding",
                "bound": True,
            },
            result="success",
        )
        return

    if identity.kind != KIND_STAFF:
        raise ElderScopeForbiddenError("daily report is only readable by staff sessions")

    if (identity.role or "") not in _REPORT_READING_ROLES:
        raise RoleForbiddenError("staff role is not allowed to read daily reports")

    # doctor 的 assignment 范围与存在性判定完全复用长者服务的口径：
    # 范围外 403（不泄露存在性）、admin 查不到才是 404。
    await elder_service.get_elder(session, identity, elder_id)


# ------------------------------------------------------------------ 聚合


async def _aggregate_health(
    session: AsyncSession, elder_id: uuid.UUID, report_date: datetime.date
) -> dict[str, Any]:
    """当日健康块：只统计**当前生效版本**（`superseded_by IS NULL`）。

    补录是追加版本行（FR-HLT-04），旧版本仍留在表里但已失效；把它算进
    「当日采集次数」会让补录一次就凭空多一条记录。已撤销（HLT-04）同理不计入。
    """
    records = (
        (
            await session.execute(
                select(HealthRecord)
                .where(
                    HealthRecord.elder_id == elder_id,
                    HealthRecord.record_date == report_date,
                    HealthRecord.superseded_by.is_(None),
                    HealthRecord.revoked_at.is_(None),
                )
                .order_by(HealthRecord.collected_at.desc(), HealthRecord.record_id)
            )
        )
        .scalars()
        .all()
    )

    counts = {"green": 0, "yellow": 0, "red": 0, "missing": 0}
    sources = {
        "voice_scheduled": 0,
        "voice_active": 0,
        "staff_scheduled": 0,
        "staff_active": 0,
    }
    for record in records:
        if record.grade in counts:
            counts[record.grade] += 1
        if record.source in sources:
            sources[record.source] += 1

    latest: dict[str, Any] | None = None
    if records:
        head = records[0]
        latest = {
            "record_id": str(head.record_id),
            "source": head.source,
            "grade": head.grade,
            "systolic_mmhg": head.systolic_mmhg,
            "diastolic_mmhg": head.diastolic_mmhg,
            "heart_rate_bpm": head.heart_rate_bpm,
            # Numeric(4,1) 取出来是 Decimal，JSONB 编码器不吃，统一转 float。
            "sleep_hours": float(head.sleep_hours) if head.sleep_hours is not None else None,
            "mood_score": head.mood_score,
            "abnormal_reasons": list(head.abnormal_reasons or []),
            "collected_at": head.collected_at.isoformat(),
        }

    return {
        # `missing` 没有记录可数：本日**一条都没有**时用 state 表达，不伪造一行统计。
        "state": "collected" if records else "missing",
        "counts": counts,
        "total": len(records),
        "sources": sources,
        "latest": latest,
    }


def _day_window(report_date: datetime.date) -> tuple[datetime.datetime, datetime.datetime]:
    """日报自然日 → `[起, 止)` 的 UTC 时刻，时区与 `_today()` 同一份配置。

    `medication_events.scheduled_at` 是 timestamptz（UTC 存储），而 `report_date` 是
    `scheduler_timezone` 下的自然日：直接拿日期比会漏掉「北京时间今天 00:30」这类
    事件（它在 UTC 还是昨天）。所以先在上海时区取当日零点，再换回 UTC。
    """
    tz = ZoneInfo(get_settings().scheduler_timezone)
    start = datetime.datetime.combine(report_date, datetime.time.min, tzinfo=tz)
    end = start + datetime.timedelta(days=1)
    return start.astimezone(datetime.UTC), end.astimezone(datetime.UTC)


async def _aggregate_medication(
    session: AsyncSession, elder_id: uuid.UUID, report_date: datetime.date
) -> dict[str, Any]:
    """当日用药块：口径与聚合全在 `medication_service.summarize_day`（不在本文件重写）。"""
    since, until = _day_window(report_date)
    return await medication_service.summarize_day(
        session, elder_id=elder_id, since=since, until=until
    )


# ------------------------------------------------------------------ 语音条（FR-RPT-04）


def _vitals_text(latest: dict[str, Any] | None) -> str:
    """把「最近一次采集」翻译成一句口播文本；缺项**整项跳过**，不念「未记录」。

    血压只念双侧齐全的值：单边缺失念成「128/未记录」既不像话，也容易让家属误以为
    另一侧是 0。零散的缺项由页面上的「—」承担，语音只负责念清楚的那几项。
    """
    if not latest:
        return ""
    pieces: list[str] = []
    systolic = latest.get("systolic_mmhg")
    diastolic = latest.get("diastolic_mmhg")
    if systolic is not None and diastolic is not None:
        pieces.append(f"血压 {systolic}/{diastolic} 毫米汞柱")
    heart_rate = latest.get("heart_rate_bpm")
    if heart_rate is not None:
        pieces.append(f"心率 {heart_rate} 次每分钟")
    sleep_hours = latest.get("sleep_hours")
    if sleep_hours is not None:
        # `:g` 去掉 "7.0" 的小数尾巴，口播与页面的 `displayNumber` 口径一致。
        pieces.append(f"睡眠 {float(sleep_hours):g} 小时")
    return f"最近一次采集：{'，'.join(pieces)}。" if pieces else ""


def _summary_text(
    report_date: datetime.date,
    health: dict[str, Any],
    medication: dict[str, Any],
) -> str:
    """日报的**文本摘要**，同时是语音条的唯一音源（PRD:412「内容与文本摘要一致」）。

    一份数据两处消费：`payload["summary"]` 落库，`_generate_report_audio` 直接读它合成
    语音。**不许**在这里之外再拼一遍播报文本——两份文本迟早会分叉。

    口径与页面保持一致：
    - 没采到就直说「没有采集到记录」，不念一排 0（页面的「缺项写 —」同理）；
    - 用药四类终态**分列念**，不合并成「漏服」（PRD FR-RPT-01 明令禁止合并）；
    - 结尾带 PRD §4.8 的固定说明，原样不改。

    不写长者姓名：`payload` 是生成时快照，姓名由 `_view` 在读取时取（改名要即时生效）。
    """
    counts = health.get("counts") or {}
    parts = [f"{report_date.year}年{report_date.month}月{report_date.day}日的健康日报。"]
    if health.get("state") == "missing":
        parts.append("健康数据：当天没有采集到记录。")
    else:
        parts.append(
            "健康数据：当天共采集 {total} 次，绿色 {green} 次、"
            "黄色 {yellow} 次、红色 {red} 次。".format(
                total=int(health.get("total") or 0),
                green=int(counts.get("green") or 0),
                yellow=int(counts.get("yellow") or 0),
                red=int(counts.get("red") or 0),
            )
        )
        vitals = _vitals_text(health.get("latest"))
        if vitals:
            parts.append(vitals)

    med_counts = medication.get("counts") or {}
    if medication.get("state") == "not_configured":
        parts.append("用药提醒：当天没有用药计划。")
    else:
        sentence = (
            "用药提醒：当天共 {total} 次到点提醒，已服自报 {taken} 次、"
            "拒服 {refused} 次、已送达未确认 {missed} 次、通知失败 {failed} 次。"
        ).format(
            total=int(medication.get("total") or 0),
            taken=int(med_counts.get("taken_reported") or 0),
            refused=int(med_counts.get("refused") or 0),
            missed=int(med_counts.get("missed_unconfirmed") or 0),
            failed=int(med_counts.get("notification_failed") or 0),
        )
        pending = int(med_counts.get("pending") or 0)
        if pending:
            sentence += f"另有 {pending} 次提醒还在进行中。"
        parts.append(sentence)

    parts.append(REPORT_DISCLAIMER)
    return "".join(parts)


def _audio_path(elder_id: uuid.UUID, report_date: datetime.date) -> Path:
    """落盘路径：`<root>/<elder_id>/<date>.wav`（一天一个文件，重生成即覆盖）。"""
    return REPORT_AUDIO_ROOT / str(elder_id) / f"{report_date.isoformat()}.wav"


def _audio_url(elder_id: uuid.UUID, report_date: datetime.date) -> str:
    return REPORT_AUDIO_URL_TEMPLATE.format(elder_id=elder_id, report_date=report_date.isoformat())


def _pcm_to_wav(pcm: bytes, *, sample_rate: int, sample_width: int, channels: int) -> bytes:
    """裸 PCM → WAV 容器：浏览器 `<audio>` 不认 s16le 裸流，必须带 RIFF 头。"""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(sample_width)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return buffer.getvalue()


def _write_audio_file(path: Path, data: bytes) -> None:
    """原子落盘：先写临时文件再替换，避免并发读取到半截 WAV（播放器会直接报错）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.tmp")
    temp.write_bytes(data)
    temp.replace(path)


async def _generate_report_audio(report: DailyReport) -> str | None:
    """把日报的**已落库**内容合成语音条，返回点播地址；失败返回 `None`。

    三条不可动摇的口径：

    1. **文本只有一份**：用 `_summary_text()` 从**已落库的** `payload["health"]` /
       `payload["medication"]` 现推，与家属端消费的是同一份快照、同一套措辞；本函数
       不重查数据库，也不自造另一套播报话术；
    2. **失败不连坐**：TTS 不可用 / 写盘失败一律吞掉并记日志，`audio_url` 留 NULL，
       日报照常生成（FR-RPT-04 只要求生成时同步产出语音，不允许反过来卡住日报）；
    3. **复用唯一实现**：`app.audio.tts.build_tts()`（AGENTS §4.8）。这里用**惰性
       import**而不是模块级 import，是因为 `services` 不该在模块加载期就绑死到
       `audio` 适配层（AGENTS §2.2 依赖方向单向）；只有真正要合成时才取这个能力。
    """
    payload = report.payload or {}
    health = payload.get("health")
    medication = payload.get("medication")
    if not isinstance(health, dict) or not isinstance(medication, dict):
        logger.warning(
            "daily report payload incomplete, skip audio",
            extra={
                "elder_id": str(report.elder_id),
                "report_date": report.report_date.isoformat(),
                "report_id": str(report.report_id),
            },
        )
        return None

    # 播报文本从落库快照现推：这正是页面渲染的那两块，因此「语音与文本一致」是结构性的，
    # 不需要再存一份字符串去和它对齐。
    summary = _summary_text(report.report_date, health, medication)

    try:
        from app.audio.tts import (
            PCM_CHANNELS,
            PCM_SAMPLE_RATE,
            PCM_SAMPLE_WIDTH,
            build_tts,
        )

        pcm = await build_tts().synthesize(summary)
        wav = _pcm_to_wav(
            pcm,
            sample_rate=PCM_SAMPLE_RATE,
            sample_width=PCM_SAMPLE_WIDTH,
            channels=PCM_CHANNELS,
        )
        await asyncio.to_thread(
            _write_audio_file, _audio_path(report.elder_id, report.report_date), wav
        )
    except Exception:
        # 注意：这里**故意**吞掉所有异常（含 TTSUnavailable / OSError / ffmpeg 缺失）。
        # 语音条是增强项，日报本身已经落库，不能因为念不出来就判整份日报失败。
        logger.exception(
            "daily report audio generation failed",
            extra={
                "elder_id": str(report.elder_id),
                "report_date": report.report_date.isoformat(),
                "report_id": str(report.report_id),
            },
        )
        return None

    logger.info(
        "daily report audio generated",
        extra={
            "report_id": str(report.report_id),
            "elder_id": str(report.elder_id),
            "report_date": report.report_date.isoformat(),
            "audio_bytes": len(wav),
            "audio_sample_rate": PCM_SAMPLE_RATE,
        },
    )
    return _audio_url(report.elder_id, report.report_date)


def _audio_file_present(elder_id: uuid.UUID, report_date: datetime.date) -> bool:
    """同步判断语音文件是否还在。

    单独抽一个同步函数（而不是在 async 里直接 `Path.is_file()`）有两个原因：一次 `stat`
    不值得为它开线程，而 ASYNC240 会把异步函数里的阻塞路径调用判成缺陷。
    """
    return _audio_path(elder_id, report_date).is_file()


async def _ensure_report_audio(report: DailyReport) -> None:
    """复用路径补齐语音条（FR-RPT-04）：老日报不该永远是灰的播放器。

    判据只有一条——**地址在、文件也在才算就绪**：

    * `audio_url` 非空且文件存在 → 直接返回，**不重复合成**（幂等：同一天重复打开
      日报页不该每次都调一次 TTS）；
    * `audio_url` 为空（迭代 8 之前生成的历史行）→ 补生成；
    * 地址在但文件被清掉（有人清过 `data/` 目录、换了机器）→ 视为需重建。地址是由
      长者与日期推导出来的，重建后地址不变，所以库里不会出现第二条地址。

    与 `generate()` 那条路径共用同一个 `_generate_report_audio()`：失败已在该函数内
    被吞掉并记日志，这里拿到 `None` 就保持 `audio_url` 不变（复用语义照旧，只是没语音）。
    """
    if report.audio_url and _audio_file_present(report.elder_id, report.report_date):
        return
    audio_url = await _generate_report_audio(report)
    if audio_url is not None:
        report.audio_url = audio_url


def _build_payload(
    elder_id: uuid.UUID,
    report_date: datetime.date,
    health: dict[str, Any],
    medication: dict[str, Any],
) -> dict[str, Any]:
    """拼 payload。用药块由 `_aggregate_medication` 产出，这里只负责装进去。

    **不在这里塞播报文本**：`payload` 的键集是家属端契约（`tests/test_family_reports.py`
    逐键校验），凭空加一个字段就是改契约。语音条改为合成时用 `_summary_text()` 从**这两个
    已落库的块**里现推（见 `_generate_report_audio`）——同一份快照、同一套措辞，两处消费
    不会分叉，也不必为一句文案再做一次迁移。
    """
    return {
        "elder_id": str(elder_id),
        "report_date": report_date.isoformat(),
        "health": health,
        "medication": medication,
        "sources": {
            "health": "health_records",
            "medication": "medication_events",
            "note": SOURCES_NOTE,
        },
    }


async def _find(
    session: AsyncSession, elder_id: uuid.UUID, report_date: datetime.date
) -> DailyReport | None:
    """取某长者某日**最新版本**的日报（同一天可能有多个补录版本并存）。

    为什么按 version 倒序取第一行而不是 `scalar_one_or_none()`：FR-RPT-03 允许补录
    追加版本（AC-25 保留历史版本），一天可能有多行，`scalar_one_or_none` 会直接抛
    `MultipleResultsFound`。旧版行仍然在表里，只是所有「读当前日报」的入口都走这里。
    """
    return (
        await session.execute(
            select(DailyReport)
            .where(
                DailyReport.elder_id == elder_id,
                DailyReport.report_date == report_date,
            )
            .order_by(DailyReport.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _publish_report(
    session: AsyncSession,
    report: DailyReport,
    *,
    previous_version: int | None,
) -> None:
    """为一份日报建 `family_report` 通知（委托 `notification_service`，幂等）。

    为什么写在这里而不是 API/调度器：生成即建通知是**同一个业务动作**（FR-RPT-03），
    分成两步就必然有人只调 `generate()` 而漏掉推送。通知的收件人解析、重试链、失败
    落 `failed` 全在通知服务里，本文件只递「哪份日报、第几版」。

    为什么不 catch 异常：通知行与日报行在**同一个事务**里（调用方 commit），这里吞掉
    异常会留下「日报已生成、推送簿记没有」的半截状态——那比重试一次更难查。通道本身
    的失败不在这一步发生（那在 `deliver_due_daily_reports` 里如实落 `failed`）。
    """
    await notification_service.notify_daily_report(
        session,
        elder_id=report.elder_id,
        report_id=report.report_id,
        report_date=report.report_date,
        version=report.version,
        previous_version=previous_version,
    )


async def generate(
    session: AsyncSession, elder_id: uuid.UUID, report_date: datetime.date
) -> DailyReport:
    """生成（或取回）某长者某日的日报；**内容未变则幂等复用，变了就追加一版**。

    判定顺序（FR-RPT-01 幂等 + FR-RPT-03 补录版本更新）：

    1. 算一遍当前聚合（健康块 + 用药块），拼出**这次会写进 payload 的内容**；
    2. 已存在最新版且 payload **逐字段相等** → 原样复用（返回同一行、不产第二版、
       也不会重复发通知）——`test_generate_is_idempotent` 依赖这条；
    3. 内容变了（补录了健康数据 / 用药事件补齐）→ `version = latest.version + 1`
       插一行新版本，**旧版本行保留**（AC-25「保留历史版本」）；
    4. 两种路径都会调 `_publish_report`：新版本发「已更新」通知，复用路径命中已建
       通知就直接返回（幂等键 `(elder_id, report_id, version)`）。

    取回已有行时会把语音条补齐（`_ensure_report_audio`）：日报内容仍原样复用，只是
    允许顺手产出那天欠下的音频。调用方负责 commit。
    """
    latest = await _find(session, elder_id, report_date)
    health = await _aggregate_health(session, elder_id, report_date)
    medication = await _aggregate_medication(session, elder_id, report_date)
    payload = _build_payload(elder_id, report_date, health, medication)

    if latest is not None and latest.payload == payload:
        logger.info(
            "daily report reused",
            extra={
                "elder_id": str(elder_id),
                "report_date": report_date.isoformat(),
                "report_id": str(latest.report_id),
                "version": latest.version,
            },
        )
        # FR-RPT-04：复用路径同样要保证「日报可播」。迭代 8 之前生成的日报只有文本、
        # 没有语音条，家属点进详情页会一直看到空播放器；这里**只补音频**，不重算
        # payload、不改复用语义、不动上面那段判定与返回时机。
        await _ensure_report_audio(latest)
        # 复用路径也调一次：首次生成时没有家属、之后补绑的场景靠这条补发。
        await _publish_report(session, latest, previous_version=None)
        return latest

    previous_version = latest.version if latest is not None else None
    report = DailyReport(
        elder_id=elder_id,
        report_date=report_date,
        version=1 if previous_version is None else previous_version + 1,
        payload=payload,
    )
    try:
        # 并发下两个请求可能同时通过 `_find`；savepoint 让唯一键冲突只回滚这一次
        # INSERT，外层事务（请求会话）仍然可用，随后按「返回已有行」收口。
        async with session.begin_nested():
            session.add(report)
            await session.flush()
    except IntegrityError:
        reused = await _find(session, elder_id, report_date)
        if reused is None:  # pragma: no cover - 唯一键冲突必然有对手行
            raise
        logger.info(
            "daily report reused after race",
            extra={"elder_id": str(elder_id), "report_date": report_date.isoformat()},
        )
        # 竞态下拿到的对手行同样补一次语音（已就绪时 `_ensure_report_audio` 直接返回）。
        await _ensure_report_audio(reused)
        await _publish_report(session, reused, previous_version=None)
        return reused

    await session.refresh(report)

    # FR-RPT-04：日报先落库，再同步产语音条。失败只留 `audio_url=NULL`（函数内部已记
    # 日志并吞掉异常），因此这里的返回值只决定写不写这一列，不影响本函数的成功语义。
    # 赋值即脏标记：UPDATE 由调用方那次 commit 一起发出（与既有「事务归调用方」一致）。
    audio_url = await _generate_report_audio(report)
    if audio_url is not None:
        report.audio_url = audio_url

    # 音频失败**不得连坐推送**：`audio_url` 为空也照样建 family_report 通知——家属先
    # 看到文字日报，语音条是增强项（与 _generate_report_audio 同一口径）。
    await _publish_report(session, report, previous_version=previous_version)

    logger.info(
        "daily report generated",
        extra={
            "elder_id": str(elder_id),
            "report_date": report_date.isoformat(),
            "report_id": str(report.report_id),
            "version": report.version,
            "previous_version": previous_version,
            "health_total": health["total"],
            "counts": health["counts"],
            "medication_total": medication["total"],
        },
    )
    return report


# ------------------------------------------------------------------ 批量生成（调度侧）


async def generate_for_active_elders(session: AsyncSession, day: datetime.date) -> tuple[int, int]:
    """为**全部在住长者**生成当日日报，返回 `(generated, failed)`。

    这是「07:00 批量出日报」的选人与逐条兜底，从 `app/scheduler/reports.py` 原样
    下沉到服务层（批 0-B：先下沉、不改触发）：调度器只该负责「什么时候跑、用哪个
    session、什么时候 commit」，不该在这里维护一份 `Elder.status == "active"` 的业务
    口径——那份口径一变，就会有人去调度器里改查询。

    两条性质与下沉前**逐字等价**：

    1. 幂等靠 `generate()`：`UNIQUE(elder_id, report_date, version)` + 内容未变则复用，
       所以补跑/重复触发都不会产出第二行，任务不记账也能安全重试；
    2. 单个长者失败**不连坐**：逐个 try/except，某一位的数据异常不该让整轮日报停在
       半路；失败后 `rollback()` 回到干净的事务边界，避免 aborted 事务把后续 flush
       全带死（代价是本次循环里尚未提交的成果一起回滚，与下沉前一致）。

    **调用方负责 commit**（与 `generate()` 同一约定）：本函数不提交，调度器拿到
    返回值后统一提交，任务体的事务边界仍只有一处。
    """
    elder_ids = list(
        (
            await session.execute(
                select(Elder.elder_id).where(Elder.status == "active").order_by(Elder.elder_id)
            )
        )
        .scalars()
        .all()
    )

    generated = 0
    failed = 0
    for elder_id in elder_ids:
        try:
            await generate(session, elder_id, day)
            generated += 1
        except Exception:
            failed += 1
            logger.exception(
                "daily report generate failed for elder",
                extra={"job": "daily_report_generate", "elder_id": str(elder_id)},
            )
            # 回到干净的事务边界，避免一条失败把后续 flush 全带成 aborted。
            await session.rollback()
    return generated, failed


async def refresh_medication_block(session: AsyncSession, report: DailyReport) -> bool:
    """把一条**仍带旧占位**的日报重算成当前口径；已回填过的返回 `False`。

    为什么只认旧占位、不做「无条件重算」：`payload` 是**生成时快照**，日报的意义
    就是「那天看到了什么」。允许事后跟着今天的用药事件滚动重算，等于让历史日报
    随数据变化，家属再也对不上账。所以只有 `pending_day7` 这一种「从来就没数据」
    的行才补算，跑第二遍不会改动任何已回填的行（幂等）。

    只改 `payload` 内的两块（`medication` 与 `sources`），不碰健康块、不碰表结构。
    调用方负责 commit。
    """
    current = (report.payload or {}).get("medication") or {}
    if current.get("state") != MEDICATION_LEGACY_STATE:
        return False

    medication = await _aggregate_medication(session, report.elder_id, report.report_date)
    payload = dict(report.payload)
    payload["medication"] = medication
    sources = dict(payload.get("sources") or {})
    sources["medication"] = "medication_events"
    sources["note"] = SOURCES_NOTE
    payload["sources"] = sources
    # 整体换 dict 而不是原地改：JSONB 列的变更检测看的是属性赋值，原地 mutate 不触发 UPDATE。
    report.payload = payload
    logger.info(
        "daily report medication backfilled",
        extra={
            "report_id": str(report.report_id),
            "elder_id": str(report.elder_id),
            "report_date": report.report_date.isoformat(),
            "medication_total": medication["total"],
            "medication_state": medication["state"],
        },
    )
    return True


async def get_report(
    session: AsyncSession,
    identity: Identity,
    elder_id: uuid.UUID,
    report_date: datetime.date | None = None,
) -> DailyReportView:
    """读某长者某日日报（默认当日）。权限见模块 docstring。"""
    target_date = report_date or _today()
    await _require_report_scope(session, identity, elder_id, action="report.read")

    report = await _find(session, elder_id, target_date)
    if report is None:
        raise ReportNotFoundError(f"{elder_id}@{target_date.isoformat()}")
    return _view(report, await _elder_name(session, elder_id))


async def read_audio(
    session: AsyncSession,
    identity: Identity,
    elder_id: uuid.UUID,
    report_date: datetime.date | None = None,
) -> Path:
    """点播某长者某日日报的语音条，返回落盘文件路径（FR-RPT-04）。

    鉴权与范围**完全复用** `get_report`：音频是日报的一部分，读音频就等于读日报。
    这里刻意不新开一道权限判断——家属绑定口径只有 `elder_service` 一份实现，多写一条
    闸门就多一个分叉点，迟早出现「日报读得了、语音读不了」或更糟的反向漏洞。

    文件缺失（清理/换机/手工删）时抛 `ReportAudioNotFoundError` 而不是 500：数据库里
    有 `audio_url` 只说明「当时生成过」，磁盘才是事实来源。
    """
    view = await get_report(session, identity, elder_id, report_date)
    if not view.audio_url:
        raise ReportAudioNotFoundError(f"{elder_id}@{view.report_date.isoformat()}")

    path = _audio_path(view.elder_id, view.report_date)
    if not path.is_file():
        logger.warning(
            "daily report audio file missing",
            extra={
                "report_id": str(view.report_id),
                "elder_id": str(view.elder_id),
                "report_date": view.report_date.isoformat(),
                "audio_url": view.audio_url,
            },
        )
        raise ReportAudioNotFoundError(f"{elder_id}@{view.report_date.isoformat()}")
    return path


async def list_reports(
    session: AsyncSession,
    identity: Identity,
    elder_id: uuid.UUID,
    *,
    days: int = 30,
    today: datetime.date | None = None,
) -> list[DailyReportView]:
    """近 `days` 天日报（按日期倒序，一天只出**最新版**）；未生成的日期不补空行。

    同一天可能有多个补录版本并存（FR-RPT-03）。列表是「时间线」，家属扫一眼就该看到
    那天**当前**的结论，所以按 `(report_date desc, version desc)` 取，同日期只留第一行；
    历史版本仍在 `daily_reports` 里可查（按 `report_id` 可定位），不在列表里重复展示。
    """
    anchor = today or _today()
    since = anchor - datetime.timedelta(days=max(days, 1) - 1)
    await _require_report_scope(session, identity, elder_id, action="report.list")

    rows = (
        (
            await session.execute(
                select(DailyReport)
                .where(
                    DailyReport.elder_id == elder_id,
                    DailyReport.report_date >= since,
                    DailyReport.report_date <= anchor,
                )
                .order_by(DailyReport.report_date.desc(), DailyReport.version.desc())
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return []
    seen: set[datetime.date] = set()
    current: list[DailyReport] = []
    for row in rows:
        if row.report_date in seen:
            continue
        seen.add(row.report_date)
        current.append(row)
    # 同一 endpoint 只查一个长者，名字取一次即可；空列表提前返回省掉这次查询。
    name = await _elder_name(session, elder_id)
    return [_view(row, name) for row in current]


__all__ = [
    "MEDICATION_LEGACY_STATE",
    "REPORT_AUDIO_ROOT",
    "REPORT_AUDIO_URL_TEMPLATE",
    "REPORT_DISCLAIMER",
    "SOURCES_NOTE",
    "DailyReportView",
    "ReportAudioNotFoundError",
    "ReportNotFoundError",
    "ReportServiceError",
    "generate",
    "generate_for_active_elders",
    "get_report",
    "list_reports",
    "read_audio",
    "refresh_medication_block",
]
