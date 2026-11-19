"""迭代 5 · 三档首包超时「逐个演示」的证据脚本（plan §4.5 / §6.3 第 2:30-3:30 段）。

做法（plan §4.5 手段 1「配置注入」）：把三档阈值压到 1ms，等一个必定超时的协程
（`asyncio.sleep(50ms)`），观察 `with_timeout` 的返回值——**三档必须给出三句不同的、
长者能听懂的话术**，同时 `degrade_total{stage=...,reason=timeout}` 各加 1。

为什么不真起 WS 服务：本脚本要的是「可复跑、秒级出结果」的证据，链路里唯一还没接的是
真 ASR / 真 LLM（迭代 6 才有），拿桩协程演示阈值行为比编一个假链路更诚实。
产物直接贴进 `eval/reports/latency-baseline.md` 的「降级验证」小节。

用法::

    .venv\\Scripts\\python.exe eval\\harness\\degrade_demo.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

if __name__ == "__main__":
    _REPO_ROOT = Path(__file__).resolve().parents[2]
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))

from app.core.degrade_speech import REST_DEGRADE, SPOKEN_DEGRADE
from app.core.metrics import metrics_snapshot, reset_metrics
from app.core.timeouts import STAGE_ASR, STAGE_LLM, STAGE_TTS, with_timeout

#: 演示用的「肯定超预算」的耗时：50ms 远大于演示阈值 1ms，又不至于让脚本变慢。
SLOW_MS: float = 50.0

#: 演示阈值（plan §4.5 手段 1：把对应阈值调到 1ms）。
DEMO_TIMEOUT_MS: int = 1


async def _slow() -> str:
    await asyncio.sleep(SLOW_MS / 1000.0)
    return "太慢了，你不该看到我"


async def main() -> int:
    reset_metrics()
    rows: list[tuple[str, str, str, str]] = []
    for stage in (STAGE_ASR, STAGE_LLM, STAGE_TTS):
        result = await with_timeout(_slow(), stage=stage, timeout_ms=DEMO_TIMEOUT_MS)
        assert result.ok is False, f"{stage} 居然没超时"
        assert result.degrade_key is not None and result.spoken is not None
        rows.append(
            (
                stage,
                result.degrade_key,
                result.spoken,
                REST_DEGRADE[result.degrade_key],
            )
        )
        print(
            f"[{stage}] ok={result.ok} elapsed={result.elapsed_ms:.1f}ms "
            f"key={result.degrade_key} spoken={result.spoken}"
        )

    texts = [spoken for _, _, spoken, _ in rows]
    assert len(set(texts)) == 3, f"三档话术必须互不相同，实际 {texts}"
    assert all(t.strip() for t in texts), "话术不能为空"
    for _, key, spoken, rest in rows:
        assert spoken == SPOKEN_DEGRADE[key]
        assert rest == REST_DEGRADE[key]

    counters = metrics_snapshot()["counters"]
    for stage, _, _, _ in rows:
        key = f"degrade_total{{stage={stage},reason=timeout}}"
        value = counters.get(key)  # type: ignore[union-attr]
        assert value == 1, f"{key} 应为 1，实际 {value}"
        print(f"[metric] {key} = {value}")

    print(
        "\n| 档位 | 阈值 | 触发手段 | degrade_key | 长者听到（WS 口语） | REST 书面 | degrade_total |"
    )
    print("| --- | --- | --- | --- | --- | --- | --- |")
    for stage, key, spoken, rest in rows:
        print(f"| {stage} | 1ms | `asyncio.sleep(50ms)` 桩 | `{key}` | {spoken} | {rest} | 1 |")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
