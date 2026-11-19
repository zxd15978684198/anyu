"""`app/core/timeouts.py` 的单测（迭代 5 §4.2 三档超时 / §4.5 演示手段第 3 条）。

覆盖四组口径：

1. **快速路径**：正常完成返回 `ok=True` + `value` + `elapsed_ms`，且**不打**降级计数；
2. **超时路径**：阈值 1ms + 被等协程 20ms → `ok=False` + `degrade_key` + 非空话术，
   **不抛异常**（WS 层再兜一次就会造成双重话术，计划 §4.2 明令禁止）；
3. **打点**：`degrade_total{stage=...,reason=timeout}` 增长、`<stage>_timeout_ms` 落一条；
4. **档位与阈值**：省略 `timeout_ms` 时三档各自读 `Settings`，未知档位回落 `internal`
   且仍可工作。

另有两组「不该被吞」的语义：被等协程自己抛的异常原样上抛；外层取消（`CancelledError`）
不算超时。这两条一旦被吞，整条语音链会把「真失败」误报成「请再说一遍」。

阈值用 1ms、慢协程用 20ms（20 倍差），调度抖动不影响判定；`metrics` 是进程级全局，
故有 autouse 夹具在每用例前后各 `reset_metrics()` 一次，避免计数漏进下一个用例。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from app.core import metrics as metrics_core
from app.core.config import Settings, get_settings
from app.core.degrade_speech import SPOKEN_DEGRADE, spoken_speech
from app.core.timeouts import STAGE_ASR, STAGE_LLM, STAGE_TTS, with_timeout

#: 超时用例的阈值（毫秒）：故意贴着 0，保证「慢协程必然超过」。
TIMEOUT_MS = 1
#: 被等协程的睡眠时长（毫秒）：比阈值大一个数量级，不受事件循环抖动影响。
SLOW_MS = 20

#: 三个已知档位。
_ALL_STAGES = (STAGE_ASR, STAGE_LLM, STAGE_TTS)


@pytest.fixture(autouse=True)
def fresh_metrics() -> Iterator[None]:
    """隔离进程级计数器：每用例从空快照开始、结束后也清掉（同 test_metrics_api.py）。"""
    metrics_core.reset_metrics()
    yield
    metrics_core.reset_metrics()


async def _quick(value: str = "pong") -> str:
    """快速成功的被等协程。"""
    return value


async def _slow(value: str = "late") -> str:
    """必然超时的被等协程（睡眠 20ms > 阈值 1ms）。"""
    await asyncio.sleep(SLOW_MS / 1000.0)
    return value


# ---------------------------------------------------------------- 快速路径


async def test_fast_path_returns_value_and_elapsed() -> None:
    result = await with_timeout(_quick(), stage=STAGE_ASR, timeout_ms=1000)

    assert result.ok is True
    assert result.stage == STAGE_ASR
    assert result.value == "pong"
    assert result.degrade_key is None
    assert result.spoken is None
    assert result.elapsed_ms >= 0.0
    assert result.timeout_ms == 1000


async def test_fast_path_does_not_count_degrade() -> None:
    await with_timeout(_quick(), stage=STAGE_LLM, timeout_ms=1000)

    snapshot = metrics_core.metrics_snapshot()
    assert snapshot["counters"] == {}
    assert snapshot["timings"] == {}


# ---------------------------------------------------------------- 超时路径


async def test_timeout_degrades_instead_of_raising() -> None:
    """核心口径：超时**不抛** `TimeoutError`，而是一句能念出来的话术。"""
    result = await with_timeout(_slow(), stage=STAGE_ASR, timeout_ms=TIMEOUT_MS)

    assert result.ok is False
    assert result.stage == STAGE_ASR
    assert result.value is None
    assert result.degrade_key == "asr_unavailable"
    assert result.spoken == spoken_speech("asr_unavailable")
    assert isinstance(result.spoken, str)
    assert result.spoken.strip() != ""
    assert result.timeout_ms == TIMEOUT_MS


async def test_timeout_increments_degrade_total() -> None:
    await with_timeout(_slow(), stage=STAGE_ASR, timeout_ms=TIMEOUT_MS)

    counters = metrics_core.metrics_snapshot()["counters"]
    # 标签顺序固定为调用方传参顺序（metrics.py 模块注释），故 key 是确定的。
    assert counters == {"degrade_total{stage=asr,reason=timeout}": 1}


async def test_timeout_records_elapsed_timing() -> None:
    result = await with_timeout(_slow(), stage=STAGE_TTS, timeout_ms=TIMEOUT_MS)

    timings = metrics_core.metrics_snapshot()["timings"]
    assert set(timings) == {"tts_timeout_ms"}
    slot = timings["tts_timeout_ms"]
    assert slot["count"] == 1
    # 落进 timings 的就是返回值里那一个数，两条路径不许各算一次。
    assert slot["sum_ms"] == pytest.approx(result.elapsed_ms)
    assert slot["max_ms"] == pytest.approx(result.elapsed_ms)


async def test_non_positive_budget_degrades_immediately() -> None:
    """阈值写成 0 / 负数（配置手滑）→ 立刻降级，而不是抛异常或永久挂住。"""
    result = await with_timeout(_slow(), stage=STAGE_LLM, timeout_ms=0)

    assert result.ok is False
    assert result.degrade_key == "llm_timeout"
    assert result.timeout_ms == 0


async def test_timeout_cancels_inner_awaitable() -> None:
    """`asyncio.wait_for` 的取消语义必须保留：被等的协程要真的被 cancel 掉。"""
    finalized = False

    async def _hang() -> None:
        nonlocal finalized
        try:
            await asyncio.sleep(5)
        finally:
            finalized = True

    result = await with_timeout(_hang(), stage=STAGE_TTS, timeout_ms=TIMEOUT_MS)

    assert result.ok is False
    assert finalized is True


# ---------------------------------------------------------------- 档位与阈值


async def test_default_budgets_come_from_settings() -> None:
    """省略 `timeout_ms` 时按档位读 settings，且三个类默认值就是 TD line 1042 的 2s/3s/1s。

    默认值断言读的是 **Settings 类字段**而不是实例：`.env` 里可以按环境调阈值
    （演示降级就要调），但「出厂默认」不许悄悄变。
    """
    assert Settings.model_fields["asr_first_packet_timeout_ms"].default == 2000
    assert Settings.model_fields["llm_first_token_timeout_ms"].default == 3000
    assert Settings.model_fields["tts_first_packet_timeout_ms"].default == 1000

    settings = get_settings()
    expected = {
        STAGE_ASR: settings.asr_first_packet_timeout_ms,
        STAGE_LLM: settings.llm_first_token_timeout_ms,
        STAGE_TTS: settings.tts_first_packet_timeout_ms,
    }
    for stage, budget in expected.items():
        result = await with_timeout(_quick(), stage=stage)
        assert result.ok is True
        assert result.timeout_ms == budget, stage


async def test_unknown_stage_degrades_to_internal() -> None:
    """未知档位（例如未来新增的「唤醒」）不报错：用最宽松的预算，话术退到内部通用句。"""
    result = await with_timeout(_slow(), stage="wake", timeout_ms=TIMEOUT_MS)

    assert result.ok is False
    assert result.degrade_key == "internal"
    assert result.spoken == SPOKEN_DEGRADE["internal"]
    counters = metrics_core.metrics_snapshot()["counters"]
    assert counters == {"degrade_total{stage=wake,reason=timeout}": 1}


async def test_unknown_stage_without_budget_still_works() -> None:
    result = await with_timeout(_quick(), stage="wake")

    assert result.ok is True
    assert result.value == "pong"
    # 未知档位的兜底预算取三档里最宽松的那一档（LLM 首 token）。
    assert result.timeout_ms == get_settings().llm_first_token_timeout_ms


# ---------------------------------------------------------------- 不该被吞的语义


async def test_inner_error_propagates() -> None:
    """「失败了」不是「太慢了」：ASRError 之类必须原样上抛，让上层给 503 / 错误码。"""

    async def _boom() -> None:
        raise ValueError("provider exploded")

    with pytest.raises(ValueError, match="provider exploded"):
        await with_timeout(_boom(), stage=STAGE_ASR, timeout_ms=1000)


async def test_outer_cancellation_propagates() -> None:
    """外层 task 被取消时继续抛 `CancelledError`，不能被误判成本档位超时。"""
    task = asyncio.create_task(with_timeout(_slow(), stage=STAGE_ASR, timeout_ms=10_000))
    await asyncio.sleep(0.01)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    # 取消不是超时：不许给「取消」记一笔降级。
    assert metrics_core.metrics_snapshot()["counters"] == {}


async def test_all_stages_share_one_result_shape() -> None:
    """三档都用同一个返回类型：调用方不必按档位分支处理字段。"""
    for stage in _ALL_STAGES:
        ok_result = await with_timeout(_quick(), stage=stage, timeout_ms=1000)
        timeout_result = await with_timeout(_slow(), stage=stage, timeout_ms=TIMEOUT_MS)

        assert ok_result.ok is True
        assert timeout_result.ok is False
        assert timeout_result.spoken is not None
        assert timeout_result.spoken.strip() != ""
        assert timeout_result.degrade_key is not None
