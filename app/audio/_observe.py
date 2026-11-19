"""A1 观测收口：ASR/TTS 的置信度与首包耗时统一从这里进 trace。

迭代 3 的验收口径是「**ASR 置信度进 trace**」（技术设计文档 §8 迭代 3 技术验收）与
「分段耗时（ASR/路由/LLM/TTS）都要保存」（PRD §5.3），不是「现在就把 OTel 接上」——
OTel/Langfuse 排在 迭代 10（技术设计文档 §8 迭代 10 落地清单）。

所以本模块做两件事：
1. 用现成的 `trace_id`（`app/core/context.py` 中间件写入）+ JSON 结构化日志承载观测字段，
   字段名已对齐 `decision_logs.asr_confidence` 与 PRD §5.3 的分段耗时口径；
2. 留一个**单点收口**：迭代 10 接 OTel 时，只改本文件两个 `record_*` 的函数体
   （加 `span.set_attribute(...)`），调用方一行都不用动。

纪律（AGENTS §4 + day-03 计划 §3 裁决）：
- 置信度拿不到就写 ``null``，**禁止伪造 0.0**；
- 只写指标与文本长度，**不写转写全文、不写长者隐私**（日志脱敏硬要求）。
"""

from __future__ import annotations

import logging
import time

from app.core._observe import emit_span
from app.core.metrics import observe_ms

logger = logging.getLogger("app.audio.observe")


class Stopwatch:
    """毫秒计时器；`elapsed_ms()` 可反复调用（拿首包再拿总耗时）。"""

    __slots__ = ("_t0",)

    def __init__(self) -> None:
        self._t0 = time.perf_counter()

    def elapsed_ms(self) -> float:
        return round((time.perf_counter() - self._t0) * 1000.0, 2)


def start() -> Stopwatch:
    """开始计时（一般在收到第一帧音频 / 进入 synthesize 时调用）。"""
    return Stopwatch()


def observe_latency_ms(name: str, elapsed_ms: float | None) -> None:
    """把一档耗时灌进 `/metrics`，并在本层把「没拿到 / 不是数」挡掉。

    为什么不在调用点裸调 `observe_ms`：
    1. `None` 的语义是**这一档没拿到**（典型：FunASR 在首帧前就 ERROR），必须跳过而不是记 0
       —— 记 0 会被平均进基线，把首包 P95 稀释成假达标；
    2. 非数值也不能让异常冒出去：两处调用都在生成器/收尾路径里，抛出去会盖掉真正的 ASR/TTS
       故障。当前 `app/core/metrics.observe_ms` 的非数值分支本身会抛（它的 warn 用
       `extra={"name": ...}`，与 `LogRecord.name` 重名 → KeyError，2026-11-07 实测），
       所以这里先自己转成有限 float，再交给它做聚合。metrics 侧修好后本函数仍然成立，
       只是少了一层防御。
    3. 开关关掉时 `observe_ms` 自己 no-op，这里不做二次判断。
    """
    if elapsed_ms is None:
        return
    try:
        value = float(elapsed_ms)
    except (TypeError, ValueError):
        return
    if value != value or value in (float("inf"), float("-inf")):
        return
    observe_ms(name, value)


def record_asr(
    *,
    provider: str,
    source: str,
    text_len: int,
    is_final: bool,
    confidence: float | None,
    first_packet_ms: float | None,
    total_ms: float | None,
) -> None:
    """记录一次 ASR 观测。

    source 取值：``provider``（引擎直给）/ ``funasr``（协议层拿不到，confidence 恒 None）/ ``mock``。

    迭代 5 W1 ② 起**额外**把两个耗时灌进 `app/core/metrics.observe_ms`（指标名 迭代 6/7 还要复用，
    不要改名）。日志字段一个不动：打点是在日志之后追加的副作用，metrics 关掉时自然 no-op。
    """
    logger.info(
        "asr observation",
        extra={
            "asr_provider": provider,
            "asr_source": source,
            "asr_confidence": confidence,
            "asr_text_len": text_len,
            "asr_is_final": is_final,
            "asr_first_packet_ms": first_packet_ms,
            "asr_total_ms": total_ms,
        },
    )
    # 分段耗时进 /metrics（PRD §5.3「分段耗时均保存」）。None = 「这一档没拿到」→ 跳过不记 0；
    # 非数值同样只跳过（`observe_latency_ms` 的 docstring 说明了为什么要在本层挡）。
    # 指标名 迭代 6/7 还要复用：`asr_first_packet_ms` / `asr_total_ms` 不要改名。
    observe_latency_ms("asr_first_packet_ms", first_packet_ms)
    observe_latency_ms("asr_total_ms", total_ms)
    # 迭代 10 · W4：同一批字段再走一次 trace（span 旁路）。`emit_span` 自己吞异常、
    # 只落本地 span / 后台导出，**不写任何 counter**，所以 metrics 关掉时这里也没有副作用；
    # 本函数签名与上面的日志字段一个不动（day-03 计划 §3 的单点收口约定）。
    emit_span(
        "asr.transcribe",
        duration_ms=total_ms,
        **{
            "asr.provider": provider,
            "asr.source": source,
            "asr.text_length": text_len,
            "asr.is_final": is_final,
            "asr.confidence": confidence,
            "asr.first_packet_ms": first_packet_ms,
            "asr.total_ms": total_ms,
        },
    )


def record_tts(
    *,
    provider: str,
    voice: str,
    text_len: int,
    pcm_bytes: int,
    first_packet_ms: float | None,
    total_ms: float | None,
) -> None:
    """记录一次 TTS 观测（迭代 3 非流式：首包 = 整段可用时刻）。"""
    logger.info(
        "tts observation",
        extra={
            "tts_provider": provider,
            "tts_voice": voice,
            "tts_text_len": text_len,
            "tts_pcm_bytes": pcm_bytes,
            "tts_first_packet_ms": first_packet_ms,
            "tts_total_ms": total_ms,
        },
    )
    # 迭代 10 · W4：TTS 也进 trace（§6.1-4 要求 trace 串到 TTS）。同样只记 span、
    # 不写 counter：迭代 5 的 `tests/test_audio_tts.py` 断言过「无样本时 counters 为空」。
    emit_span(
        "tts.synthesize",
        duration_ms=total_ms,
        **{
            "tts.provider": provider,
            "tts.voice": voice,
            "tts.text_length": text_len,
            "tts.pcm_bytes": pcm_bytes,
            "tts.first_packet_ms": first_packet_ms,
            "tts.total_ms": total_ms,
        },
    )
