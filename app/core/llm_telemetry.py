"""LangGraph / LangChain 自动插桩门面（迭代 5 · plan §4.4 D4、§6 S9）。

## 为什么单独一个模块

`app/core/observability.py` 是**手写打点**的门面（`record()` / `span()` / `agent_span()`），
本模块只管一件事：把 LangGraph/LangChain 执行层**自动**挂到同一个 `TracerProvider` 上。
两者共用 provider、共用导出管道，但装载时机与失败语义完全不同——所以拆开，既不动
trace 门面（S2 刚整文件重写过），也保证「谁掉链子不牵连谁」。

## 三条硬性质（与 `tests/test_llm_telemetry.py` 一一对应）

1. **关掉即零副作用**：`otel_enabled=False` 时**连 `openinference` 都不 import**
   （延迟 import 写在函数体内），返回 `False`。这样测试进程与生产关闭态都不会把
   第三方插桩库拉进 `sys.modules`。
2. **没有 provider 就不装**：`get_tracer().provider is None` 说明 trace 前门已经落到
   NoOp（`observability.py` 的「关掉 = 零开销」），此时插桩没有落点，直接返回 `False`。
3. **幂等且绝不抛**：模块级 `_INSTRUMENTED` 保证 `instrument()` 只调一次；任何异常只
   `logger.warning`，返回 `False`。观测是**增强依赖**，与 Redis / tracer 同一口径——
   它坏了不许拦住后端启动。

## 合规提醒（plan §4.4）

自动插桩会抓 prompt / 检索原文。**库级** `TraceConfig(hide_inputs_outputs=...)` 是软闸、
参数名随版本漂移，本模块**不单独依赖它**；真正的硬闸是 `PiiStrippingSpanProcessor`
（S10）。在 S10 落地前，本模块只在 `otel_enabled=True` 的进程里生效，关掉即彻底不加载。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.core.config import get_settings

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注，运行期不 import opentelemetry
    from opentelemetry.sdk.trace import TracerProvider

logger = logging.getLogger("app.observability.llm")

#: 是否已经装载过插桩。`LangChainInstrumentor` 在进程内是单例语义，重复 instrument
#: 会叠出重复 span，所以用本模块自己的标志挡住（不依赖库内部状态）。
_INSTRUMENTED = False


def _current_provider() -> TracerProvider | None:
    """取当前装配好的 `TracerProvider`；未装配（trace 关闭）时为 `None`。

    延迟 import 是刻意的：本模块被 `app/main.py` 在 lifespan 里导入，若顶层就把
    `observability`（连带 `opentelemetry-sdk`）拉进来，「关掉零开销」那条验收
    就只剩一句口号了。
    """
    from app.core.observability import get_tracer

    return get_tracer().provider


def instrument_llm(settings: object | None = None) -> bool:
    """按配置装载 LangChain/LangGraph 自动插桩；返回**是否已装载**，永不抛。

    参数 `settings` 省略时按 `get_settings()` 解析（与 `configure_observability` 同口径）。
    """
    global _INSTRUMENTED

    resolved = settings if settings is not None else get_settings()
    if not getattr(resolved, "otel_enabled", False):
        # 闸门一：开关关闭。此处之后**不得**出现任何 openinference 的 import。
        return False
    if _INSTRUMENTED:
        return True

    try:
        provider = _current_provider()
        if provider is None:
            # 闸门二：trace 前门是 NoOp，插桩没有落点。
            logger.info("langchain auto-instrumentation skipped: no tracer provider")
            return False

        # 闸门三：延迟 import。只有真的要用时才把第三方库拉进来。
        from openinference.instrumentation.langchain import LangChainInstrumentor

        LangChainInstrumentor().instrument(tracer_provider=provider)
    except Exception as exc:  # 观测是增强依赖：任何失败只降级，不拦启动
        logger.warning(
            "langchain auto-instrumentation unavailable",
            extra={"error_type": type(exc).__name__},
        )
        return False

    _INSTRUMENTED = True
    logger.info("langchain auto-instrumentation enabled")
    return True


def reset_instrumentation() -> None:
    """复位幂等标志（**仅测试与进程内重装配用**）。

    不调用 `LangChainInstrumentor().uninstrument()`：那会把全进程的 LangChain 插桩都摘掉，
    测试里 monkeypatch 掉假库后它并不存在。真实重装配由 `observability.shutdown()`
    带 provider 一起收敛。
    """
    global _INSTRUMENTED
    _INSTRUMENTED = False


__all__ = ["instrument_llm", "reset_instrumentation"]
