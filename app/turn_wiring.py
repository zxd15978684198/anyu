"""装配根的两个「本轮入口」闭包（FR-HLT-07 / AC-34、AC-35）。

## 为什么单独一个模块，而不是内联在 `main.py` 的 lifespan 里

- `companion`（agents 层）不许 import `graphs`：AGENTS §2.1 禁止横向依赖，所以
  「健康图 / 用药图」的调用只能由**装配层**拿闭包塞进去（与 `knowledge_lookup`
  同一条口径：结构等价的 Callable，而不是 import 一个类型）。
- lifespan 里内联的闭包测试拿不到，AC-34/AC-35 就只能靠起整服务来验；抽成本模块后
  `tests/test_prd_closeout_e2e.py` 可以用真库 + 真图直接跑，回归成本低一个数量级。

## 本模块的口径边界

允许 import `graphs` 与 `services`（它就是装配层），但**不写业务规则**：分数字段、
状态机、阈值、落库口径全在 service / graph 里；这里只做「本轮该推进哪一张图、
拿哪个 session、什么时候 commit/rollback」这三件事。

## 身份从哪来

`elder_id` 由 `companion.run_turn` 从服务端 `identity` 取（AGENTS §4.9），本模块
只做格式校验；解析不出 UUID 就返回 `None`（等价「本轮不推进」），绝不按文本猜。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app.graphs import health_checkup_graph as health_graph
from app.graphs.health_checkup_graph import CheckupTurnOutcome, ComplaintCheckupOutcome
from app.services import health_service, medication_service

logger = logging.getLogger(__name__)

__all__ = ["build_health_checkup_turn", "build_medication_confirm_turn"]


def build_health_checkup_turn(session_factory: Any) -> Any:
    """造「主诉 → 健康决策图」入口：当天已有会话就续问，没有就把主诉推进图。

    - `status == "asking"`：长者正在回答，这一句就是答案（`submit_checkup_answer`）；
    - `status in CHECKUP_OPEN_STATUSES`（含 `awaiting_review`）：已在等人工复核，
      不再起第二场、也不把复核中的话当成新主诉 —— 返回 `None` 让上层说收口话术；
    - 其余（没有行 / 已结束）：把**这一句主诉**交给
      `health_checkup_graph.start_complaint_checkup`（HLT-07）。它负责判「窗口内
      交回 REST」与「真的起一场」，并把主诉原文放进图状态 —— 原文最终随终态落库
      写进 `health_records.complaint`。传 `session` 是为了让它**建会话行**：
      不建行，下一句就会被当成「当天没有会话」重新起场、冲掉 checkpoint。
    """

    async def turn(text: str, elder_id: str) -> Any:
        try:
            eid = uuid.UUID(elder_id)
        except ValueError:
            logger.warning("health checkup skipped: bad elder id")
            return None
        async with session_factory() as session:
            try:
                day = health_service.today_in_shanghai()
                row = await health_service.load_checkup_session(
                    session, elder_id=eid, record_date=day
                )
                outcome: CheckupTurnOutcome | ComplaintCheckupOutcome
                if row is not None and row.status == "asking":
                    outcome = await health_graph.submit_checkup_answer(
                        session, elder_id=eid, text=text, record_date=day
                    )
                elif row is not None and row.status in health_service.CHECKUP_OPEN_STATUSES:
                    return None
                else:
                    outcome = await health_graph.start_complaint_checkup(
                        elder_id=eid,
                        complaint=text,
                        record_date=day,
                        session=session,
                    )
                await session.commit()
                return outcome
            except Exception:
                await session.rollback()
                raise

    return turn


def build_medication_confirm_turn(session_factory: Any) -> Any:
    """造「对话内用药确认」入口（AC-35）：统一走 `medication_service.confirm_from_dialog`。

    为什么不在本层判「有没有待确认提醒」：那是服务层的口径（要按 `elder_id` 查最近
    一条 calling/waiting_confirm），在本层再判一次就是第二个真源。
    """

    async def turn(text: str, elder_id: str) -> Any:
        try:
            eid = uuid.UUID(elder_id)
        except ValueError:
            logger.warning("medication confirm skipped: bad elder id")
            return None
        async with session_factory() as session:
            try:
                outcome = await medication_service.confirm_from_dialog(
                    session, elder_id=eid, text=text
                )
                await session.commit()
                return outcome
            except Exception:
                await session.rollback()
                raise

    return turn
