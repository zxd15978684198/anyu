"""FastAPI 装配层（技术文档 §2.1：`app/main.py` 承担 FastAPI 装配、`/health`、trace_id 中间件）。

分层约束（AGENTS.md §2）：本文件只做装配，不写业务逻辑；路由在 `app/api/`，
领域逻辑与写库在 `app/services/`。

启动：`uvicorn app.main:app --reload --port 8000`
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from functools import partial
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.api import ROUTERS
from app.api.schemas.auth import ErrorResponse
from app.audio import signaling  # 迭代 3 T2：WS 信令路由（/ws/signal）
from app.audio.webrtc import install_signaling_handler  # 迭代 3 T3：A3 媒体层接线
from app.core.auth import (
    CredentialExpiredError,
    CredentialInvalidError,
    CredentialMissingError,
    CredentialRevokedError,
)
from app.core.config import Settings, get_settings
from app.core.context import TRACE_HEADER, get_trace_id
from app.core.logging import configure_logging
from app.core.rate_limit import RateLimitMiddleware
from app.core.security import TokenExpiredError
from app.core.trace import TraceIdMiddleware
from app.dialogue_runtime import DialogueRuntime
from app.services.auth_service import (
    AuthConflictError,
    AuthExpiredError,
    AuthInvalidError,
    AuthRevokedError,
)

if TYPE_CHECKING:  # pragma: no cover - 只给类型检查器看：运行期不在模块级拉 apscheduler
    from app.scheduler.health import HealthCheckupDriver
    from app.scheduler.medication import Deliver


class _GraphCheckupDriver:
    """把 `app/graphs/health_checkup_graph` 的编排入口包成调度器认的采集驱动。

    为什么由本文件（装配根）拼：`app/scheduler` 与 `app/graphs` 之间不许横向 import
    （AGENTS §2.1），调度器只认 `app.scheduler.health.HealthCheckupDriver` 这个 Protocol，
    具体实现只能由同时看得到两侧的装配根拼起来——与用药的 `medication_deliver` 同一手法。

    图模块以参数形式传进来（而不是在模块级 import）：test 环境不该为 langgraph 与
    检查点连接付导入代价，import 因此留在 lifespan 的 scheduler 分支里。
    """

    def __init__(self, graph_module: Any) -> None:
        self._graphs = graph_module

    async def start_due(
        self,
        session: Any,
        *,
        elder_id: Any,
        record_date: Any,
        now: Any,
    ) -> bool:
        """起一场采集；`created=False`（当天已开过或已走完）不算本次新起。"""
        outcome = await self._graphs.start_checkup(
            session, elder_id=elder_id, record_date=record_date, now=now
        )
        return bool(outcome is not None and outcome.created)

    async def advance_due(self, session: Any, *, now: Any) -> int:
        """把到期未答的会话推进一轮，返回推进了几场。"""
        return int(await self._graphs.advance_due_checkups(session, now=now))


def _checkup_driver_state(*, assembled: bool, scheduler_running: bool) -> str:
    """把「驱动装没装上」「调度器起没起来」折成 `/health` 那个取值。

    `wired` 的语义是「19:00 真的会去起会话」，所以必须**两件事都成**才敢给：
    驱动对象构造成功但 `build_scheduler` 抛了的话，19:00 的 job 根本不存在，
    这时候报 wired 就是骗运维（复核方 2026-11-12 指出）。
    """
    if not assembled:
        return "unavailable"
    return "wired" if scheduler_running else "driver_ready_but_scheduler_missing"


def _check_voiceprint_key(settings: Settings) -> None:
    """声纹加密密钥自检（2026-11-07 P0）。

    `voiceprint_fernet_key` 的默认值曾 base64 解码后只有 39 字节（Fernet 要 32），
    结果是注册 500、比对异常被身份级联吞成 generic ——「语音身份永不命中且静默」。
    非 test 环境 fail-fast；test 环境只记 error（单测需要能构造非法配置）。
    """
    if settings.voiceprint_fernet_ready:
        return
    logging.getLogger("app.main").error(
        "voiceprint_fernet_key_invalid", extra={"app_env": settings.app_env}
    )
    if settings.app_env.lower() == "test":
        return
    raise RuntimeError(
        "voiceprint_fernet_key 不可用（需 32 字节 url-safe base64）；"
        "声纹注册与比对都会失败。请检查 .env 或改用配置默认值。"
    )


async def _probe_embedding(settings: Settings, logger: logging.Logger) -> None:
    """启动自检：真打一次 embedding，把「配了但不通」从静默变成一行日志。

    2026-11-11 的事故：`.env` 配的是 `BAAI/bge-m3`（输出 1024 维），列是
    `vector(1536)`。`_parse` 判 `bad_dimension` → `memory_service._embed_text` 接住 →
    `embedding=None` **照常写行**。链路不报错，表现是「记忆写进去了、一条都召回不出来」，
    而日志里既没有 provider、也没有实际维度、也没有耗时，只能靠读代码推断。

    探活补上这三样，失败只 warning：embedding 是增强依赖，它挂了不该让后端起不来
    （与 Redis / 观测同一口径）。**注意它不是熔断**——运行时仍会每次真调，
    这里只是把配置错误在启动时就喊出来。
    """
    from app.services.embedding import build_embedding_provider

    provider = None
    try:
        provider = build_embedding_provider(settings)
        started = time.perf_counter()
        await provider.embed(["ping"])
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        logger.info(
            "embedding probe ok",
            extra={
                "provider": type(provider).__name__,
                "dimension": provider.dimension,
                "configured_model": getattr(settings, "embedding_model", "") or None,
                "elapsed_ms": elapsed_ms,
            },
        )
    except Exception as exc:  # 增强依赖：任何失败都只降级，不拦启动
        logger.warning(
            "embedding probe failed",
            extra={
                "provider": type(provider).__name__ if provider is not None else None,
                "dimension": getattr(provider, "dimension", None),
                "expected_dimension": getattr(settings, "embedding_dimension", None),
                "error_type": type(exc).__name__,
                "error": str(exc)[:200],
            },
        )
    finally:
        # Http provider 的池只在探活里用一次，不关就是常驻泄漏（它自己建的才关）。
        close = getattr(provider, "aclose", None) if provider is not None else None
        if close is not None:
            # 关连接失败不影响已完成的探活结论，也不许盖住上面的 warning。
            with suppress(Exception):
                await close()


def _probe_report_audio_root(logger: logging.Logger) -> None:
    """启动自检：把「日报语音条目录在不在」从静默 404 变成一行日志。

    2026-11-11 的事故：`REPORT_AUDIO_ROOT` 按
    `Path(__file__).resolve().parents[2] / "data" / "report_audio"` 计算，容器里解析成
    `/app/data/report_audio`，而 backend 的 volumes 当时只挂了 `./static:/app/static`。
    宿主机上 wav 都在（6 个、322KB），接口却对 4 天日报全部返回 404
    `daily report audio not found`——页面表现是「语音加载失败」，日志里一个字都没有。

    修法是补挂 `./data:/app/data`（见 `docker-compose.yml` 的 backend.volumes），
    **不改这个路径算法**：改成别的绝对路径只会让「宿主机直跑」和「容器跑」两套
    口径分叉，而分叉正是这类 bug 的成因。

    这里只 warning 不 raise：目录不存在时其余功能（对话、工单、健康）完全正常，
    为了一个音频目录拒绝启动不合算。真实修复动作写进 extra，便于直接照着做。
    """
    from app.services.report_service import REPORT_AUDIO_ROOT

    exists = REPORT_AUDIO_ROOT.is_dir()
    if exists:
        logger.info(
            "report audio root ok",
            extra={"path": str(REPORT_AUDIO_ROOT)},
        )
        return
    logger.warning(
        "report audio root missing",
        extra={
            "path": str(REPORT_AUDIO_ROOT),
            "hint": "容器内请确认 docker-compose.yml 的 backend.volumes 含 ./data:/app/data",
        },
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """进程级生命周期：装配对话链路（迭代 4 收口 W4）。

    ## 装了什么

    | 依��� | 用途 | 连不上时 |
    | --- | --- | --- |
    | Redis 客户端 | 短期记忆（最近 5 轮） | 只 warn，`store=None`，对话照常但无记忆 |
    | `LLMService` | 调模型 | 无网络也能构造（惰性建池），失败自带降级话术 |
    | `TTSProvider` | 把回复变成音频 | `build_tts()` 不连网（Mock 系），真 provider 在合成时才连 |
    | 工具层 `ToolRegistry` | 天气查询（`get_weather`）走 MCP | 只 warn，`tool_registry=None`，L2 退回模板话术 |
    | `AgentRunner` | L2 开放域交给 ReAct 循环 | 工具层装不上就是 `None`（没有工具不假装有 Agent） |
    | `DialogueService` | 编排 | —— |
    | `DialogueRuntime` | 注册终稿回调 | —— |

    ## 为什么 Redis 连不上不 raise

    与 `_check_voiceprint_key`（配置错必须 fail-fast）不同：Redis 是**增强**依赖，
    它挂了只该失去「记得上几句」，不该让整个后端起不来（计划 S-D9 同一口径）。
    真正需要 Redis 的地方（迭代 6 的路由缓存、限流）到那时再讨论 fail-fast。

    ## 为什么 lifespan 里装配而不是 `create_app` 里

    `create_app()` 在导入期被调用（`app.main:app`），而连接池必须在**事件循环里**建。
    放进 lifespan 也顺带解决了测试问题：不跑 lifespan 的 `TestClient(app)` 拿不到
    编排依赖，此时 `webrtc` 会退回 `reply_stub` 垫层（`has_dialogue_final_handler()`
    为 False），旧测试的语义一行都不用改。
    """
    logger = logging.getLogger("app.main")
    settings: Settings = app.state.settings
    logger.info("application starting", extra={"app_env": settings.app_env})

    # 迭代 10 · W4：可观测性装配。**放在最前面**——否则「启动慢在哪」这类 span 自己也抓不到。
    # 无 key / 开关关掉时只装一个本地 tracer（deque + ContextVar），不建队列、不起后台任务；
    # 任何异常都不许拦住启动（观测是增强依赖，与 Redis 同一口径）。
    tracer = None
    try:
        from app.core.observability import configure_observability

        tracer = configure_observability(settings)

        from app.core.llm_telemetry import instrument_llm

        instrument_llm(settings)
    except Exception as exc:
        logger.warning("observability unavailable", extra={"error_type": type(exc).__name__})
    app.state.tracer = tracer

    await _probe_embedding(settings, logger)
    _probe_report_audio_root(logger)

    # Laya 是 L1 端侧分类器的本地权重：首次请求要付冷启动（本机 CPU 实测 13.7s），
    # 而演示口径只给 3000ms——不预热的话长者第一句话必然超时并整轮降级 L2。
    # FunASR 同理：首次推理实测首包 31s、终稿 58s，不撞穿 6s 首包预算就谢天谢地。
    # 两者**串行**跑：并行预热会互相抢 CPU，实测 Laya 被 FunASR 推理拖过 30s 而失败。
    # 整体放后台、不阻塞启动；失败只 warn。
    model_warmup_task: asyncio.Task[None] | None = None
    if settings.app_env != "test":
        from app.audio.asr import warm_up_asr
        from app.router import wiring

        async def _warm_up_models() -> None:
            await wiring.warm_up_laya()
            await warm_up_asr(settings=settings)

        model_warmup_task = asyncio.create_task(_warm_up_models(), name="model-warm-up")

    runtime, redis_client, llm_service = await _build_dialogue_runtime(app, settings)
    if runtime is not None:
        runtime.register()
    app.state.dialogue_runtime = runtime

    # task-11（P0）：checkpoint schema/表/索引必须**在启动期**建好，绝不能留到请求路径。
    # 旧行为是首次用药确认时在请求事务里跑 `CREATE INDEX CONCURRENTLY`，而 CIC 要等
    # 所有更早开始的事务结束 —— 请求自己那条未提交事务把它挡住了，请求又在等 setup
    # 返回，于是自互等、永久挂起（长者是「点了已服用，页面一直转圈」）。
    # 这里没有业务事务，CIC 拿得到快照，建完就是只读；失败只 warn（增强能力），
    # 运行时的兜底见 `app/graphs/checkpointer.py` 的 `_setup_saver_sync`（抢锁 + 5s 超时）。
    # 放在 scheduler 之前：健康采集驱动一启动就可能用到图。
    try:
        from app.graphs.checkpointer import init_checkpointer

        await init_checkpointer()
    except Exception as exc:  # 连 import 都失败也不许拦启动
        logger.warning("checkpointer init failed", extra={"error_type": type(exc).__name__})

    # 迭代 8 · W3（S-D8-6）：调度器也只在 lifespan 启停（`app/scheduler/__init__.py`
    # 只提供 build_scheduler() 工厂）。test 环境与 scheduler_enabled=False 都不启动，
    # reloader 主进程不会 import 本模块，因此不会双进程各跑一遍。
    #
    # 迭代 10 · 用药触达（FR-MED-06）：外呼通道在**装配根**构造，再递给调度器——
    # `app/scheduler` 不许 import `app/audio`（AGENTS §2.1 禁止横向 import），
    # 两侧只通过 `app.scheduler.medication.Deliver` 这一函数签名耦合。
    #
    # 迭代 10 · 健康采集（FR-HLT-01）：采集驱动同理——`app/scheduler` 不许 import
    # `app/graphs`，图由本文件拼成 `HealthCheckupDriver` 再递进去。
    # 采集驱动装配状态（迭代 10 · 第四轮 W1）：失败不再是「一行 warning 了事」——
    # 状态挂到 `app.state`，由 `/health` 暴露成 `health_checkup_driver`，运维不用翻日志
    # 就知道今天到底有没有在采。**`wired` 必须等 job 真注册进调度器之后才置**：
    # 驱动对象构造成功、但 `build_scheduler` 抛了的话，19:00 的 job 根本不存在，
    # 这时候说 wired 就是骗运维（复核方 2026-11-12 指出）。取值固定四个，别再加同义词：
    #   wired    = 驱动装配成功**且**调度器已启动、19:00 job 真注册（唯一「真的会采」）
    #   driver_ready_but_scheduler_missing = 驱动有、调度器没起来（job 不会跑）
    #   unavailable = 驱动装配抛异常（19:00 只落提醒、零会话；带异常类型名）
    #   disabled = 本环境不启动调度器（`scheduler_enabled=False` 或 `APP_ENV=test`）
    checkup_driver_state = "disabled"
    checkup_driver_error: str | None = None
    scheduler = None
    if settings.scheduler_enabled and settings.app_env != "test":
        # import 必须放在分支里：`app/audio/medication_delivery.py` 会模块级拉起
        # `app/audio/webrtc.py`（aiortc），test 环境不该为它付导入代价。
        medication_deliver: Deliver | None = None
        try:
            from app.audio.medication_delivery import build_medication_deliver
            from app.audio.outbound_speak import build_outbound_speaker

            # speaker 不传就只剩「建连」：外呼接通了却没人说话，回执却写 tts_finished
            # （2026-11-17 实测）。生产实现负责 TTS → PCM → 该设备播放轨。
            medication_deliver = build_medication_deliver(speaker=build_outbound_speaker())
            logger.info("medication deliver wired", extra={"channel": "audio"})
        except Exception as exc:
            # 通道装配失败**只降级、不拦启动**：调度器还要跑日报/漏采/健康采集，
            # 此时用药触达如实记 `receipt='none'`，升级时归 `notification_failed`，
            # 运维在库和日志里都看得见「没送到」，不会被静默当成已提醒。
            medication_deliver = None
            logger.warning(
                "medication deliver unavailable", extra={"error_type": type(exc).__name__}
            )
        # 采集驱动：import 同样放分支里（langgraph + 检查点库的导入代价不该由 test 环境承担）。
        health_checkup_driver: HealthCheckupDriver | None = None
        checkup_driver_assembled = False
        try:
            from app.graphs import health_checkup_graph

            health_checkup_driver = _GraphCheckupDriver(health_checkup_graph)
            checkup_driver_assembled = True
            logger.info("health checkup driver wired", extra={"source": "graphs"})
        except Exception as exc:
            # 驱动装配失败只降级、不拦启动：19:00 仍照常点名叫人（提醒通知），
            # 但一场会话都不会起。日志这里连栈一起打（W1：先前只有 error_type，
            # 排查时根本看不出是哪一环炸的），`/health` 同步暴露 `unavailable`。
            health_checkup_driver = None
            checkup_driver_error = type(exc).__name__
            logger.warning(
                "health checkup driver unavailable",
                extra={"error_type": type(exc).__name__},
                exc_info=True,
            )
        try:
            from app.scheduler import build_scheduler

            scheduler = build_scheduler(
                medication_deliver=medication_deliver,
                health_checkup_driver=health_checkup_driver,
            )
            scheduler.start()
            logger.info("scheduler started", extra={"timezone": settings.scheduler_timezone})
        except Exception as exc:  # 调度起不来只降级：日报/漏采提醒不发，不影响请求链路
            scheduler = None
            # 驱动装上了、调度器却没起来：`/health` 不许继续显示 wired。驱动那侧的错误
            # 优先（它更接近根因），调度器错误只在没有驱动错误时补记。
            checkup_driver_error = checkup_driver_error or type(exc).__name__
            logger.warning("scheduler unavailable", extra={"error_type": type(exc).__name__})
        # 状态只在这里定一次：`wired` 要求驱动装上了**且**调度器真的起来了（job 已注册）。
        checkup_driver_state = _checkup_driver_state(
            assembled=checkup_driver_assembled,
            scheduler_running=scheduler is not None,
        )
    app.state.scheduler = scheduler
    app.state.health_checkup_driver_state = checkup_driver_state
    app.state.health_checkup_driver_error = checkup_driver_error

    try:
        yield
    finally:
        if model_warmup_task is not None and not model_warmup_task.done():
            model_warmup_task.cancel()
            with suppress(asyncio.CancelledError):
                await model_warmup_task
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        if runtime is not None:
            await runtime.shutdown()
        # 迭代 10 · W3：工具层随 runtime 一起撤（runtime 撤完回调就不会再有新工具调用）。
        await _shutdown_tool_layer(app, logger)
        # 关停顺序：先撤回调（见 `app/dialogue_runtime.py` 模块 docstring），
        # 再关 LLM，最后关 Redis —— 任何一个在关的过程中报错都不许拦住后面的。
        if llm_service is not None:
            await llm_service.aclose()
        if redis_client is not None:
            try:
                await redis_client.aclose()
            except Exception as exc:  # 关停期的连接错误只记日志
                logger.warning("redis close failed", extra={"error_type": type(exc).__name__})
        # 迭代 10 · W4：先把队列里剩下的 span 排空再回收 tracer —— 顺序反了会丢掉进程退出前的
        # 最后一批（关停流程自己的 span 恰好是排查「为什么起不来」最关键的证据）。
        if tracer is not None:
            try:
                from app.core.observability import flush, shutdown

                await flush()
                await shutdown()
            except Exception as exc:  # 观测收尾失败不许影响退出码
                logger.warning(
                    "observability shutdown failed", extra={"error_type": type(exc).__name__}
                )
        # task-11：checkpoint 连接放在**最后**关（scheduler/runtime/观测都撤完之后）——
        # 前面任何一步的收尾都可能还在写 checkpoint，先关它就是把链路提前掐断。
        # 关它失败只 warn：退出码不该被一条连接的收尾决定。
        try:
            from app.graphs.checkpointer import shutdown_checkpointer

            await shutdown_checkpointer()
        except Exception as exc:
            logger.warning("checkpointer shutdown failed", extra={"error_type": type(exc).__name__})
        logger.info("application stopped")


async def _build_dialogue_runtime(
    app: FastAPI, settings: Settings
) -> tuple[DialogueRuntime | None, Any, Any]:
    """构造编排依赖。返回 `(runtime, redis_client, llm_service)`。

    **任何一步失败都返回 `(None, ...)` 而不是抛**：起不来对话编排等于退回
    迭代 3 的能力（只有字幕没回复），那是可接受的降级；后端起不来才是 P0。
    """
    logger = logging.getLogger("app.main")

    redis_client: Any = None
    if settings.redis_url.strip():
        try:
            import redis.asyncio as redis_asyncio

            redis_client = redis_asyncio.from_url(
                settings.redis_url, encoding="utf-8", decode_responses=False
            )
            # 真正验证连通性：`from_url` 是惰性的，不 ping 的话第一次对话才发现连不上。
            await redis_client.ping()
        except Exception as exc:
            logger.warning(
                "redis unavailable, dialogue memory disabled",
                extra={"error_type": type(exc).__name__},
            )
            redis_client = None

    try:
        from app.dialogue_runtime import DialogueRuntime
        from app.services.dialogue_service import DialogueService, RedisTurnStore
        from app.services.llm_service import build_llm

        llm_service = build_llm(settings)
        store = (
            RedisTurnStore(redis_client, key_prefix=settings.redis_key_prefix)
            if redis_client is not None
            else None
        )
        # 迭代 10 · W3：工具层 + Agent runner。**单独兜一层异常**——这两件装不上只该少一个
        # 能力（L2 退回模板话术），不该把整条对话链路拖成 None（计划 §4-W3）。
        agent: Any = None
        app.state.mcp_registry = None
        app.state.mcp_client = None
        try:
            from app.agents.runtime_factory import (
                build_agent_runner,
                build_tool_registry,
                tool_client_of,
            )
            from app.db import session_factory as _session_factory
            from app.services.knowledge_lookup import build_knowledge_lookup
            from app.services.memory_recall import build_memory_recaller
            from app.turn_wiring import (
                build_health_checkup_turn,
                build_medication_confirm_turn,
            )

            tool_registry = await build_tool_registry(settings)
            app.state.mcp_registry = tool_registry
            app.state.mcp_client = tool_client_of(tool_registry)  # 排障/关停核对用
            # 机构知识预检索（FR-KNW-01~05）：规章制度全机构共用、不按长者切片，启动期
            # 造一次即可（与记忆召回「按轮现造」相反）。阈值用模块常量
            # `DEFAULT_MAX_DISTANCE`，不新增配置项；闭包内部永不抛，检索挂了只降级。
            knowledge_lookup = build_knowledge_lookup(session_factory=_session_factory)

            # FR-HLT-07 / AC-34、AC-35：把「主动主诉」与「对话内用药确认」接进同一套图。
            # 闭包在 `app/turn_wiring.py`：装配层允许 import graphs，agents 层不允许横向
            # 依赖；抽出去也让 AC-34/35 能用真库 + 真图直接验（tests/test_prd_closeout_e2e.py）。
            health_checkup_turn = build_health_checkup_turn(_session_factory)
            medication_confirm_turn = build_medication_confirm_turn(_session_factory)

            agent = build_agent_runner(
                settings=settings,
                llm=llm_service,
                tool_registry=tool_registry,
                # 长期记忆召回（N27b 的读侧）：每轮由本轮 `identity` 现造召回器。
                # 不能在这里造死一个 —— 召回强制 `WHERE elder_id`，启动期还没有任何身份，
                # 静态 recaller 只可能越界召回别人的记忆。`partial` 只是把会话工厂钉住，
                # 真正的 `identity` 由 `DialogueService.respond` 逐轮传进 runner。
                recall_factory=partial(build_memory_recaller, session_factory=_session_factory),
                knowledge_lookup=knowledge_lookup,
                # 主诉与用药确认的前置路由：两个闭包都是「结构等价的 Callable」，
                # companion 只负责判断该不该走、什么时候走。
                health_checkup=health_checkup_turn,
                medication_confirm=medication_confirm_turn,
            )
        except Exception as exc:
            logger.warning("tool layer unavailable", extra={"error_type": type(exc).__name__})

        # 路由挂主路径：缺省 None 时 DialogueRuntime 行为与接线前逐字一致；
        # 失败只在 _route() 里 warning，不会把整条链路拖成 None。
        from app.router import wiring

        service = DialogueService(llm=llm_service, store=store, agent=agent)
        # 打字入口（`POST /api/dialogue/text`）要复用**同一个** DialogueService，
        # 否则同一位长者的「说话」与「打字」各有一份短期历史（记忆/Agent 上下文断裂）。
        # 放在这里是因为 dlg 在 lifespan 内构造，路由层拿不到闭包变量。
        app.state.dialogue_service = service
        # 会话事件流（N40）：旁路写入器。**构造本身可能失败**（如异步会话工厂导入异常），
        # 单独兜一层 —— 事件流装不上只该少一条回放链路，不该让整轮对话变成 None。
        recorder: Any = None
        try:
            from app.db import session_factory as _session_factory
            from app.services.conversation_event_recorder import ConversationEventRecorder

            recorder = ConversationEventRecorder(_session_factory)
        except Exception as exc:
            logger.warning(
                "conversation event recorder unavailable",
                extra={"error_type": type(exc).__name__},
            )
        # 打字入口（`POST /api/dialogue/text`）也要写事件流：与话音链路用**同一个**
        # 写入器实例。装不上时写 None（不是不写属性）——取的一方要能区分「没装」
        # 与「属性不存在」，两种都得能取。
        app.state.conversation_recorder = recorder

        # 长期记忆写回（N27b）：把「提取 → 写回」串成一个旁路钩子，回复发出之后在后台跑。
        # 与事件流同样单独兜一层：装不上只该少一条记忆，不该让整条对话链路变 None。
        memory_hook: Any = None
        try:
            from app.db import session_factory as _memory_session_factory
            from app.services.memory_turn_hook import MemoryTurnHook

            memory_hook = MemoryTurnHook(llm=llm_service, session_factory=_memory_session_factory)
        except Exception as exc:
            logger.warning(
                "memory turn hook unavailable",
                extra={"error_type": type(exc).__name__},
            )
        # 同上：打字链路每轮也要能写回长期记忆，否则「打字聊十句，一个字都没记住」。
        app.state.memory_hook = memory_hook

        # 统一状态中心（N43）：复用同一个 Redis 客户端（本模块不建第二个连接池）。
        # component 必须是 `dialogue_runtime` —— `WRITE_PERMISSIONS` 里 TURN 段只许它写，
        # 写错组件名会在第一次写时抛 `StateWriteForbidden`，那是最难查的一类错。
        state_store: Any = None
        try:
            from app.core.state_store import StateStore

            if redis_client is not None:
                state_store = StateStore(redis_client, component="dialogue_runtime")
        except Exception as exc:
            logger.warning("state store unavailable", extra={"error_type": type(exc).__name__})

        # 人设读口（迭代 10 · T4 / ADM-07）：`_load_persona` 每轮按当轮识别出的长者取
        # `personas` 行，拼进 system prompt 的固定前缀。与事件流/记忆钩子同样单独兜一层：
        # 装不上只该少一段人设前缀（退回组装层默认名），不许把整条对话链路拖成 None。
        persona_loader: Any = None
        try:
            from app.services.persona_service import load_persona_context

            persona_loader = load_persona_context
        except Exception as exc:
            logger.warning(
                "persona loader unavailable",
                extra={"error_type": type(exc).__name__},
            )

        runtime = DialogueRuntime(
            service=service,
            persona_loader=persona_loader,
            tts_factory=lambda voice: _build_tts_or_none(settings, logger, voice=voice),
            router=wiring.wire_route,
            recorder=recorder,
            memory_hook=memory_hook,
            state=state_store,
        )
        return runtime, redis_client, llm_service
    except Exception as exc:
        logger.warning(
            "dialogue runtime unavailable",
            extra={"error_type": type(exc).__name__},
        )
        return None, redis_client, None


async def _shutdown_tool_layer(app: FastAPI, logger: logging.Logger) -> None:
    """关停 MCP 工具层（迭代 10 · W3）：幂等，失败只 warning（不影响退出码）。

    装配期的探活连接当时就断开了，这里通常只是确认一个空连接；真出错也只记一笔 ——
    关停期的连接错误不许拦住后面的 LLM/Redis 收尾（与 Redis 分支同一口径）。
    """
    try:
        from app.agents.runtime_factory import close_tool_registry

        await close_tool_registry(getattr(app.state, "mcp_registry", None))
    except Exception as exc:
        logger.warning("mcp tool layer shutdown failed", extra={"error_type": type(exc).__name__})


def _build_tts_or_none(
    settings: Settings, logger: logging.Logger, *, voice: str | None = None
) -> Any:
    """构造 TTS provider；失败返回 `None`（字幕仍然可用）。"""
    try:
        from app.audio.tts import build_tts

        return build_tts(settings, voice=voice)
    except Exception as exc:
        logger.warning("tts provider unavailable", extra={"error_type": type(exc).__name__})
        return None


def create_app(settings: Settings | None = None) -> FastAPI:
    """应用工厂：便于测试注入不同配置（`create_app(Settings(app_env="test"))`）。"""
    settings = settings or get_settings()
    configure_logging(settings.log_level, service=settings.otel_service_name)
    _check_voiceprint_key(settings)

    application = FastAPI(
        title=f"{settings.app_name} backend",
        version=__version__,
        description="安语（Anyu）3D 数字人陪伴系统后端",
        lifespan=lifespan,
    )
    application.state.settings = settings

    # 中间件：后添加者在外层，因此 CORS 包住 TraceId，异常响应也带链路头。
    # 限流写在 TraceId **之前** = 运行时在 TraceId **内层**：外层先把 trace_id 注入
    # contextvar 与响应头，内层 429 才能带上同一条 trace_id（失败请求也要可追溯）。
    application.add_middleware(RateLimitMiddleware, settings=settings)
    application.add_middleware(TraceIdMiddleware)
    if settings.cors_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=[TRACE_HEADER],
        )

    # REST 路由统一从 `app/api/__init__.py` 的注册表挂载（T0）：
    # 新增路由只需在那张表里追加一行，不再改动本文件的公共代码块。
    for _router, _prefix in ROUTERS:
        application.include_router(_router, prefix=_prefix)
    application.include_router(signaling.router)  # /ws/signal 无 /api 前缀（契约裁决 A-03）
    install_signaling_handler()  # A3 注册 offer/ice/asr_start/asr_end 上行处理器

    # ---------- 全局 exception handler（契约 §7） ----------
    # auth_service 层异常（双重保险：auth.py 路由已捕获，这里兜底）
    @application.exception_handler(AuthInvalidError)
    async def _handle_auth_invalid(req: Request, exc: AuthInvalidError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(
                detail=str(exc.detail),
                code=exc.kind or "TOKEN_INVALID",
                trace_id=get_trace_id() or "-",
            ).model_dump(),
        )

    @application.exception_handler(AuthExpiredError)
    async def _handle_auth_expired(req: Request, exc: AuthExpiredError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(
                detail=str(exc.detail), code="TOKEN_EXPIRED", trace_id=get_trace_id() or "-"
            ).model_dump(),
        )

    @application.exception_handler(AuthRevokedError)
    async def _handle_auth_revoked(req: Request, exc: AuthRevokedError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(
                detail=str(exc.detail), code="TOKEN_REVOKED", trace_id=get_trace_id() or "-"
            ).model_dump(),
        )

    @application.exception_handler(AuthConflictError)
    async def _handle_auth_conflict(req: Request, exc: AuthConflictError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content=ErrorResponse(
                detail=str(exc.detail), code="CONFLICT", trace_id=get_trace_id() or "-"
            ).model_dump(),
        )

    # task-4 auth 层异常（依赖注入阶段抛出，auth_service 还没机会参与）
    @application.exception_handler(CredentialInvalidError)
    async def _handle_cred_invalid(req: Request, exc: CredentialInvalidError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(
                detail=str(exc), code="TOKEN_INVALID", trace_id=get_trace_id() or "-"
            ).model_dump(),
        )

    @application.exception_handler(CredentialMissingError)
    async def _handle_cred_missing(req: Request, exc: CredentialMissingError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(
                detail=str(exc), code="TOKEN_INVALID", trace_id=get_trace_id() or "-"
            ).model_dump(),
        )

    @application.exception_handler(CredentialExpiredError)
    async def _handle_cred_expired(req: Request, exc: CredentialExpiredError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(
                detail=str(exc), code="TOKEN_EXPIRED", trace_id=get_trace_id() or "-"
            ).model_dump(),
        )

    @application.exception_handler(CredentialRevokedError)
    async def _handle_cred_revoked(req: Request, exc: CredentialRevokedError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(
                detail=str(exc), code="TOKEN_REVOKED", trace_id=get_trace_id() or "-"
            ).model_dump(),
        )

    @application.exception_handler(TokenExpiredError)
    async def _handle_token_expired(req: Request, exc: TokenExpiredError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(
                detail="token has expired", code="TOKEN_EXPIRED", trace_id=get_trace_id() or "-"
            ).model_dump(),
        )

    # 请求校验统一错误体（a0-F01，2026-11-06 迭代 4 收口）：
    # `ContractAPIRoute` 已在路由内把 400/422 包成契约体，但 auth/audio/health
    # 三条普通 APIRouter 仍会落到 FastAPI 默认的 `{"detail": [...]}`，缺 code 与 trace_id。
    # 这里按契约 §7 的同一口径兜底：路径参数格式错 / 请求体非 JSON → 400，其余 → 422。
    def _is_request_format_error(exc: RequestValidationError) -> bool:
        for error in exc.errors():
            if error.get("type") == "json_invalid":
                return True
            if (error.get("loc") or ())[:1] == ("path",):
                return True
        return False

    @application.exception_handler(RequestValidationError)
    async def _handle_validation_error(req: Request, exc: RequestValidationError) -> JSONResponse:
        malformed = _is_request_format_error(exc)
        return JSONResponse(
            status_code=400 if malformed else 422,
            content=ErrorResponse(
                detail="malformed request" if malformed else "request validation failed",
                code="BAD_REQUEST" if malformed else "VALIDATION_ERROR",
                trace_id=get_trace_id() or "-",
            ).model_dump(),
        )

    # 兜底：业务代码与 Starlette 路由层（如 404 未知路径）抛的 HTTPException 都包成 ErrorResponse（契约 §7）。
    # 注意必须注册 starlette 的基类：FastAPI 的 HTTPException 继承自它，反向不成立；
    # 只注册 fastapi.HTTPException 时，「路径不存在」走 Starlette 默认处理器 → 缺 code/trace_id。
    @application.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(req: Request, exc: StarletteHTTPException) -> JSONResponse:
        code_map = {
            400: "BAD_REQUEST",
            401: "TOKEN_INVALID",
            403: "ROLE_FORBIDDEN",
            404: "NOT_FOUND",
            413: "PAYLOAD_TOO_LARGE",
            422: "VALIDATION_ERROR",
            503: "SERVICE_UNAVAILABLE",
        }
        code = code_map.get(exc.status_code, f"HTTP_{exc.status_code}")
        return JSONResponse(
            status_code=exc.status_code,
            content=ErrorResponse(
                detail=str(exc.detail),
                code=code,
                trace_id=get_trace_id() or "-",
            ).model_dump(),
        )

    # 未预期异常 → 500，不回显内部细节（AGENTS §8 失败分类）
    @application.exception_handler(Exception)
    async def _handle_unknown(req: Request, exc: Exception) -> JSONResponse:
        logging.getLogger("app.main").exception("unhandled exception")
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                detail="内部错误", code="INTERNAL_ERROR", trace_id=get_trace_id() or "-"
            ).model_dump(),
        )

    return application


app = create_app()
