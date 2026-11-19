# owner=W4 · 依据 设计记录（三级路由） §4-W4 · RouteDecision → decision_logs
"""路由决策日志服务（迭代 6 W4）：``RouteDecision`` 落 ``decision_logs``。

唯一写库层（AGENTS §2 分层硬约束）：``app/audio/**`` 垫层只 import 本模块，
不直接触碰 ORM。落库**永不抛异常**——话音链路的可用性高于评测留痕：
任何写失败只记 ``decision_log_write_failed`` warning（带 trace_id），
绝不向上传播打断应答。

身份口径（AGENTS §4.9）：``elder_id`` 为 None（设备未绑定/身份未解析）时
**不落行**，改记 ``decision_log_skipped_total{reason=no_identity}``——
``decision_logs.elder_id`` 是 NOT NULL 的服务端身份列，绝不许发明占位值。

privacy 类型收敛（台账 §3-7 漂移的落库侧处置）：``RouteDecision.privacy``
契约是 ``str | None``，而 W2 的 ``FiveSignal.privacy`` 透传进来的实际是 bool。
本服务落库前把 bool 收敛为 ``"true" / "false"`` 字符串（Text 列的安全形态），
``None`` 保持 ``None``——类型契约的最终裁决（bool|None vs str|None）走计划
§3-7 主线外裁决，本层两种都接。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app import db
from app.core._observe import record_decision
from app.core.metrics import counter
from app.models.decision_log import DecisionLog

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.router.decision import RouteDecision

logger = logging.getLogger("app.services.decision_log_service")

__all__ = ["decision_to_log", "log_decision"]


def decision_to_log(decision: RouteDecision) -> DecisionLog:
    """纯映射：``RouteDecision`` → ORM 行（不碰库，单测免 DB）。

    ``elder_id`` 为 None 时调用方必须先拦（见 ``log_decision``）；这里不替
    身份做主。``route_ms`` 直接透传（编排层已保证非负整数）。
    """
    privacy = decision.privacy
    if isinstance(privacy, bool):
        privacy = "true" if privacy else "false"
    assert decision.elder_id is not None  # 调用方契约：身份缺失根本不该走到这
    return DecisionLog(
        elder_id=decision.elder_id,
        thread_id=decision.thread_id,
        layer=decision.layer,
        rule_id=decision.rule_id,
        intent=decision.intent,
        sensitive=decision.sensitive,
        confidence=decision.confidence,
        is_out_of_domain=decision.is_out_of_domain,
        privacy=privacy,
        route_ms=decision.route_ms,
        degraded=decision.degraded,
        trace_id=decision.trace_id,
    )


async def log_decision(decision: RouteDecision) -> DecisionLog | None:
    """把一条决策写进 ``decision_logs``；失败/无身份都不抛，返回落库行或 None。

    每次调用自持一个会话（垫层是后台任务，不在请求依赖里）：
    ``async with db.session_factory()`` 成功即 commit。评测侧按
    ``trace_id`` 对账（Langfuse / 三方一致，计划 §6 验收行）。

    迭代 10 · W4：派生指标（分层命中率 / S1 闭环率）在这里打点，**必须放在无身份早退之前**
    —— 否则「模型把身份编出来了」的回合完全不进分母，命中率会系统性虚高。打点自身永不抛。
    """
    record_decision(decision)
    if not decision.elder_id:
        counter("decision_log_skipped_total", reason="no_identity")
        return None
    row = decision_to_log(decision)
    try:
        session: AsyncSession
        async with db.session_factory() as session:
            session.add(row)
            await session.commit()
    except Exception:
        logger.warning(
            "decision_log_write_failed",
            extra={"trace_id": decision.trace_id or "-"},
            exc_info=True,
        )
        counter("decision_log_write_failed_total")
        return None
    return row
