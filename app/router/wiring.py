# owner=W4 · 依据 设计记录（三级路由） §4-W4 · 垫层侧路由装配
"""W4 话音垫层的路由装配（迭代 6）：惰性 LayaClient + 设备身份解析。

两个进程级入口，供 ``app/audio/webrtc.py`` 的 B1 垫层注入 ``ReplyStub``：

- ``wire_route(text, ...)``：``app/router/routing.route`` 的绑参薄壳——
  LayaClient 进程级**懒建**（首次话音才读配置；Laya 不可用时 ``decide_l1``
  按失败四分类降级 ``L2 degraded=laya_unavailable``，不致命）；并负责把
  「低置信只澄清一次」从**单轮**延伸到**跨轮**（按 ``thread_id`` 的 TTL 记忆，
  见 ``_clarify_turn_state`` / ``_settle_clarify``）。
- ``resolve_identity(device_id)``：device → ``(elder_id, thread_id)`` **只读**
  解析。UUID 解析失败 / 设备未绑定 / 库异常一律 ``(None, None)``——身份绝不许
  发明（AGENTS §4.9）；``decision_log_service`` 侧对 None 身份跳过落库。

分层（AGENTS §2 单向依赖）：本模块归 ``app/router/``（编排侧装配），只 import
``services`` / ``system_one``，**不 import** ``app/audio``（webrtc 反向 import
本模块，与 迭代 5 垫层同构，不构成环）。刻意**不进** ``app/main.py`` lifespan：
dialogue 真编排路径（``dialogue_ready``）由主线接管，垫层装配只求惰性可用。
"""

from __future__ import annotations

import logging
import time
import uuid as uuid_lib
from typing import TYPE_CHECKING, Any, Final

from app import db
from app.router.routing import route
from app.services.identity_service import find_open_thread, resolve_device_elder
from app.system_one.client import LayaClient

if TYPE_CHECKING:
    from uuid import UUID

    from app.router.decision import RouteDecision

logger = logging.getLogger("app.router.wiring")

__all__ = ["reset_clarify_ledger", "resolve_identity", "warm_up_laya", "wire_route"]

#: 进程级懒 LayaClient：首次话音才构建（连接期不读配置）。测试可 monkeypatch。
_laya: LayaClient | None = None

#: 「本轮已澄清过」在 `turn_state` 里的键名；与 `app/system_one/questions.py` 的
#: `_CLARIFIED_KEY` 同值（那边是私有常量，这边按 `turn_state` 的**线上契约**引用）。
_CLARIFY_KEY: Final[str] = "clarified"

#: `decide_l1` 在「低置信首次、给出澄清」时写的降级名（questions.py:64）。出现它说明
#: 这一轮**用掉了**那一次澄清，会话记忆记下 180 秒。
_DEGRADED_CLARIFY: Final[str] = "low_confidence_clarify"

#: 澄清记忆的存活时间（秒）。取 3 分钟：长者听到澄清后组织一句话通常几十秒内，
#: 超过 3 分钟当另起一段对话（再模糊就再澄清一次，而不是直接下沉 L2）。
_CLARIFY_TTL_SECONDS: Final[float] = 180.0

#: 记忆条目上限：只是防「thread_id 无限多」把进程内存吃穿，超过就按最早过期清。
_CLARIFY_MAX_ENTRIES: Final[int] = 512

#: `thread_id → 过期时刻（time.monotonic）`。**进程内**存储：本仓后端是单进程
#: uvicorn（docker-compose 一个 backend 容器、无 --workers），与 LayaClient 的
#: 进程级单例同一条前提。多 worker/多副本部署时这份记忆不共享——那时要换成
#: Redis（`app/core/state_store.py` 的 StateStore，client 由 main.py 注入），
#: 但**不许**在 router 层自己 from_url 建第二个连接池（state_store.py 模块
#: docstring 的硬约束）。
_clarify_marks: dict[str, float] = {}


def reset_clarify_ledger() -> None:
    """清空澄清记忆（测试与运维复位口；生产不调用）。"""
    _clarify_marks.clear()


def _prune_clarify(now: float) -> None:
    """把过期的清掉；条目超上限时按最早过期先清，保持 O(n) 一次性回收。"""
    expired = [key for key, deadline in _clarify_marks.items() if deadline <= now]
    for key in expired:
        _clarify_marks.pop(key, None)
    overflow = len(_clarify_marks) - _CLARIFY_MAX_ENTRIES
    if overflow > 0:
        for key in sorted(_clarify_marks, key=_clarify_marks.__getitem__)[:overflow]:
            _clarify_marks.pop(key, None)


def _clarify_turn_state(thread_id: str | None) -> dict[str, Any]:
    """这一轮的 `turn_state` 初值：上一轮澄清过就带上 ``clarified=True``。

    `thread_id` 缺失（身份没解析出来）时返回空 dict——与接线前逐字节一致，
    没有会话就没有跨轮状态可言。
    """
    if thread_id is None:
        return {}
    now = time.monotonic()
    _prune_clarify(now)
    deadline = _clarify_marks.get(thread_id)
    if deadline is None or deadline <= now:
        _clarify_marks.pop(thread_id, None)
        return {}
    return {_CLARIFY_KEY: True, "clarified_at": now}


def _settle_clarify(thread_id: str | None, decision: RouteDecision) -> None:
    """按这一轮的结论更新澄清记忆（三种结论，互斥）。

    判据只看 `degraded`，不看 `turn_state`——初值可能带着上一轮留下的
    `clarified`，用它当写入条件会把「这一轮其实说清楚了」也记成澄清。

    1. `degraded == low_confidence_twice`：那一次澄清已经用掉 → 删记忆，下一句重来；
    2. `degraded == low_confidence_clarify`：这一轮刚澄清过 → 记下（TTL 180s），
       下一轮仍模糊就下沉 L2；
    3. 其余（L0 命中 / L1 高置信 / 安全告警 / Laya 降级）：这段模糊对话结束了 → 删记忆。

    第 3 条是「只澄清一次」的边界：长者被澄清后说清楚了，就别再拿旧标记压他到 L2。
    """
    if thread_id is None:
        return
    degraded = getattr(decision, "degraded", None)
    if degraded == _DEGRADED_CLARIFY:
        now = time.monotonic()
        _clarify_marks[thread_id] = now + _CLARIFY_TTL_SECONDS
        _prune_clarify(now)
        return
    _clarify_marks.pop(thread_id, None)


def _get_laya() -> LayaClient:
    """惰性单例：垫层不抢建连期预算，首句话音才付出 LayaClient 构建成本。"""
    global _laya
    if _laya is None:
        _laya = LayaClient()
    return _laya


async def warm_up_laya() -> None:
    """启动期后台预热 Laya：把 13.7s 冷启动留给进程启动，而不是长者第一句话。

    失败只记 warning：预热是增强动作，Laya 不可用时路由照常按失败四分类降级 L2。
    `laya_enabled=False` 时直接返回，不占一次注定失败的连接尝试。
    """
    client = _get_laya()
    if not client.settings.laya_enabled:
        return
    started = time.perf_counter()
    try:
        await client.warm_up()
    except Exception as exc:
        logger.warning(
            "laya warm_up failed",
            extra={
                "error_type": type(exc).__name__,
                "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 1),
            },
        )
        return
    logger.info(
        "laya warm_up done",
        extra={"elapsed_ms": round((time.perf_counter() - started) * 1000.0, 1)},
    )


async def wire_route(
    text: str,
    *,
    elder_id: UUID | None = None,
    thread_id: str | None = None,
    trace_id: str | None = None,
) -> RouteDecision:
    """垫层绑参薄壳：``route(text)`` + 按 ``thread_id`` 续接的**澄清记忆**。

    2026-11-12（T6 · DLG-03）：这里原先每轮置空 ``turn_state``，注释自述「垫层无多轮
    状态」。后果不是「垫层口径如此」，而是 PRD 的「低置信只澄清一次」在**两条生产路径
    上都不成立**——``DialogueRuntime``（真编排，``app/main.py`` 注入的就是本函数）与
    ``ReplyStub``（垫层）共用这一个入口，每轮空 dict 意味着 ``decide_l1`` 永远读不到
    ``clarified``，同一句模糊话术可以无限澄清、永不下沉 L2。

    现在的口径：按 ``thread_id`` 在进程内记 180 秒「已经澄清过一次」，第二轮仍模糊就
    由 ``decide_l1`` 正常下沉 L2（``low_confidence_twice``）；任何一句说得清楚的回复
    都会让这段模糊对话结束、记忆作废。存储拿不到（``thread_id`` 为 None、或部署改成
    多进程）时退回原行为：每轮空 dict，绝不因为记忆层问题影响路由。
    """
    turn_state = _clarify_turn_state(thread_id)
    decision = await route(
        text,
        laya=_get_laya(),
        turn_state=turn_state,
        now=time.time(),
        # `routing.route` 的 `elder_id` 形参是 `str | None`（它最终落
        # `decision_logs.elder_id`，该列是 Text），而本函数接的是 `resolve_identity`
        # 解析出的 `UUID | None`，所以在边界上换成规范 UUID 文本。
        elder_id=None if elder_id is None else str(elder_id),
        thread_id=thread_id,
        trace_id=trace_id,
    )
    _settle_clarify(thread_id, decision)
    return decision


async def resolve_identity(device_id: str) -> tuple[UUID | None, str | None]:
    """device → ``(elder_id, thread_id)``；解析不出就 ``(None, None)``，永不抛。

    优先级：设备绑定（``Device.elder_id``，裁决 v2.3 的 1v1 口径）为 elder 权威；
    未绑定时回退 open thread 的 ``elder_id``（重连场景，identity_service 注释
    「库里这条 open thread 的 elder_id 就是答案」）。``thread_id`` 取最近未结束
    会话；都没有则 None。库异常只记 warning——评测留痕不值得打断应答。
    """
    try:
        device_uuid = uuid_lib.UUID(device_id)
    except (TypeError, ValueError, AttributeError):
        # device_id 不是合法 UUID（协议层不该发生）：当无身份处理。
        return None, None
    try:
        async with db.session_factory() as session:
            elder_id = await resolve_device_elder(session, device_uuid)
            open_thread = await find_open_thread(session, device_id=device_uuid)
    except Exception:
        logger.warning(
            "identity_resolve_failed",
            extra={"device_id": device_id},
            exc_info=True,
        )
        return None, None
    thread_id: str | None = None
    if open_thread is not None:
        thread_id = open_thread.thread_id
        if elder_id is None:
            elder_id = open_thread.elder_id
    return elder_id, thread_id
