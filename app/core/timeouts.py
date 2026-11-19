"""三档首包超时护栏（迭代 5 §4.2 / §4.3）：ASR / LLM / TTS 各一档，超时即降级。

**本模块只做一件事**：把一个 awaitable 包上「首包预算」，正常完成就原样交回结果，
超时就换成一句可读的降级话术 —— **绝不向上抛 `TimeoutError`**。

为什么要统一入口（而不是各处自己写 `asyncio.wait_for`）：

1. WS 下行（`app/audio/signaling.py`）自己还有一层 `_send_error` 兜底。若超时异常穿到
   那一层，长者会先听到一句「超时」再听到一句「出状况」，**双重话术**。所以降级必须在
   本层收口成返回值（计划 §4.2「不向上抛」）；
2. 三档阈值统一从 `Settings` 读，演示「逐个降级」时改 `.env` 即可，不用改代码；
3. 降级话术只认 `app/core/degrade_speech.py` 一个源（AGENTS §4.8 一能力一形态），
   本模块不新增键、不改措辞。

口径（照计划 §4.2 / §4.3，别自己加）：

- 档位 → 键：`asr` → `asr_unavailable`、`llm` → `llm_timeout`、`tts` → `tts_unavailable`；
  未知档位 → `internal`（仍可工作，只是话术退到通用句）；
- 超时打点：`counter("degrade_total", stage=<档位>, reason="timeout")`；
  耗时打点：`observe_ms("<档位>_timeout_ms", elapsed)` —— **只在超时路径打**。
  成功路径的耗时属于 `asr.py` / `tts.py` 各自的 first_packet 打点（计划 §3.2 ②⑤），
  在这里再打一份会让同一口径出现两个 key，读数时互相打架。

取消语义：`asyncio.wait_for` 超时后**取消**被等待的协程，本模块不吞 `CancelledError`。
也就是说「外层 task 被取消」与「本档位超时」被严格区分：前者继续向上抛（`CancelledError`
是 `BaseException`，下面的 `except` 抓不到），后者转成降级结果。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Final

from app.core.config import get_settings
from app.core.context import get_trace_id
from app.core.degrade_speech import key_for_timeout, spoken_speech
from app.core.metrics import counter, observe_ms

logger = logging.getLogger("app.core.timeouts")

#: 三个已知档位（与 `degrade_speech.TIMEOUT_STAGE_TO_KEY` 的键一致）。
STAGE_ASR: Final = "asr"
STAGE_LLM: Final = "llm"
STAGE_TTS: Final = "tts"

#: 档位 → `Settings` 字段名。新增档位必须同时改 `degrade_speech.TIMEOUT_STAGE_TO_KEY`，
#: 否则能跑但没有专属话术（会退到 `internal`）。
_STAGE_SETTING_NAME: Final[dict[str, str]] = {
    STAGE_ASR: "asr_first_packet_timeout_ms",
    STAGE_LLM: "llm_first_token_timeout_ms",
    STAGE_TTS: "tts_first_packet_timeout_ms",
}


@dataclass(frozen=True, slots=True)
class StageResult[T]:
    """一次「带预算的等待」的结果。**成功失败同一个类型**，调用方按 `ok` 分支即可。

    - `ok=True`：`value` 是被等待协程的返回值，`degrade_key` / `spoken` 为 `None`；
    - `ok=False`：只发生在超时。`degrade_key` 可直接喂给 REST 侧的
      `degrade_speech.REST_DEGRADE`，`spoken` 是已经取好的口语话术（WS 侧直接用）。

    `elapsed_ms` 两种路径都有：成功时是真实耗时，超时时是**实际等待时长**
    （≈ 预算，可用来发现「阈值配错了」这种问题）。`timeout_ms` 回填本次生效的预算，
    便于演示脚本与测试断言「档位确实读的是 settings」。
    """

    ok: bool
    stage: str
    value: T | None = None
    degrade_key: str | None = None
    spoken: str | None = None
    elapsed_ms: float = 0.0
    timeout_ms: int = 0


def resolve_timeout_ms(stage: str) -> int:
    """档位 → 预算（毫秒）。未知档位退到**最宽松**的一档（LLM 首 token 3s）并照常工作。

    为什么未知档位不报错：它在生产里对应「新加了一个环节但话术表没跟上」，此时宁可
    「慢一点但还能答」也不要整条链路 500；话术会退到 `internal`，日志里有 warning 可查。

    负数按 0 处理（等价「立刻超时」），不抛：配置写错不该比不降级更糟。
    """
    settings = get_settings()
    field = _STAGE_SETTING_NAME.get(stage)
    if field is None:
        return max(int(settings.llm_first_token_timeout_ms), 0)
    return max(int(getattr(settings, field)), 0)


def _elapsed_ms(started: float) -> float:
    """单调时钟差值 → 毫秒。

    用 `perf_counter` 而不是 `monotonic`：Windows 上 `monotonic` 走 `GetTickCount64`，
    粒度约 15.6ms，会把「阈值调成 1ms 的降级演示」记成 15.6ms（迭代 5 E1 实测踩到，
    `reply_stub` 的 `llm_first_token_ms` 曾因此 P50 一半是 0、一半是 16）。
    `perf_counter` 同样是单调非递减的，只是精度到亚微秒。
    """
    return (time.perf_counter() - started) * 1000.0


async def with_timeout[T](
    awaitable: Awaitable[T],
    *,
    stage: str,
    timeout_ms: int | None = None,
) -> StageResult[T]:
    """给一个 awaitable 套上「首包预算」，超时转降级结果，**不向上抛**。

    参数：
        awaitable: 待等待的协程 / Future。超时会被 `asyncio.wait_for` 取消。
        stage: `asr` / `llm` / `tts`；未知值走 `internal` 且仍可工作。
        timeout_ms: 显式预算（毫秒）。`None` 时按 `stage` 从 settings 取（计划 §4.2 三档）。

    异常口径：
        - 超时（`asyncio.TimeoutError`，3.11+ 即内置 `TimeoutError`）→ 返回 `ok=False`；
          注意这也意味着**被等协程自己抛 `TimeoutError`** 会被同样当成档位超时 —— 两者
          在 3.11+ 是同一个类，分不开；本项目内部不用 `TimeoutError` 表达业务失败，
          故按档位超时处理是正确的取舍；
        - 其他异常（`ASRError` 之类）原样向上抛：那属于「失败了」而不是「太慢了」，
          各有各的上层处置（503 / 错误码），不该在这里被吞成一句「请再说一遍」；
        - 外层取消（`CancelledError`）原样向上抛，保持 `wait_for` 的取消语义。
    """
    budget_ms = resolve_timeout_ms(stage) if timeout_ms is None else max(int(timeout_ms), 0)
    started = time.perf_counter()

    try:
        value = await asyncio.wait_for(awaitable, timeout=budget_ms / 1000.0)
    except TimeoutError:
        elapsed = _elapsed_ms(started)
        degrade_key = key_for_timeout(stage)
        spoken = spoken_speech(degrade_key)
        counter("degrade_total", stage=stage, reason="timeout")
        observe_ms(f"{stage}_timeout_ms", elapsed)
        logger.warning(
            "stage first-packet timeout → degrade (trace_id=%s, stage=%s, timeout_ms=%d, elapsed_ms=%.1f)",
            get_trace_id() or "-",
            stage,
            budget_ms,
            elapsed,
            extra={
                "trace_id": get_trace_id() or "-",
                "stage": stage,
                "degrade_key": degrade_key,
                "timeout_ms": budget_ms,
                "elapsed_ms": round(elapsed, 1),
            },
        )
        return StageResult(
            ok=False,
            stage=stage,
            degrade_key=degrade_key,
            spoken=spoken,
            elapsed_ms=elapsed,
            timeout_ms=budget_ms,
        )

    return StageResult(
        ok=True,
        stage=stage,
        value=value,
        elapsed_ms=_elapsed_ms(started),
        timeout_ms=budget_ms,
    )


__all__ = [
    "STAGE_ASR",
    "STAGE_LLM",
    "STAGE_TTS",
    "StageResult",
    "resolve_timeout_ms",
    "with_timeout",
]
