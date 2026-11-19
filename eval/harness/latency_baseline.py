"""迭代 5 · 端到端延迟基线（20 次实测，plan `设计记录（延迟护栏与口型）` §6）。

v1.1 口径（plan §4.2b + 计划变更记录 v1.4，2026-11-07）：回复管线改为**逐句合成、
逐句入队**（F1，`app/audio/tts_sentences.py`）。打点语义随之变化：

- 每轮 `tts_first_packet_ms` = 循环起点 → **第一句**拿到 pcm（count=1/轮）
  —— **首包 = 第一句完成时刻**，不再与「整段合成」同值；
- 每轮 `tts_total_ms` = 循环起点 → **全部句子入队完成**（count=1/轮）；
- `route_ms` / `llm_first_token_ms` 语义不变。

固定样例「今天天气怎么样？」约 8 字，在 `MIN_SENTENCE_CHARS=6` 合并规则下大概率切为
**单句**：该样本上「首包 == 整轮」属正常现象，不是 bug，判据不要把它当异常。

除 20 次判据外，本版新增一个**补充探针（非判据）**：约 90 字的多句样本连跑 5 次，
逐次数出「首包 ms vs 整轮 ms」，证明逐句流式下首包 ≪ 整轮；探针跑在 `/metrics`
20 次快照**之后**，绝不掺入主测口径。

做一件事：对着**真实的**回复垫层（`app/audio/reply_stub.py`）跑「asr_final → PCM 首句
入队」，把每次的墙钟耗时、以及 `/metrics` 里分段量（route / LLM 首 token / TTS 首包）
落成一张可以直接贴进基线表的 markdown 表。

测量规则（PRD line 550，逐条照做，别自己发明）：

- **试运行 3 次不计入**：冷 import、JIT、Mock TTS 首次合成都在这一轮里消化；
- **第 1 次正式跑标 cold_start 且不进统计**：PRD 明确要求「冷启动单记」；
- **固定样例**：每次都问同一句话 `QUESTION`（20 次同输入才有可比性）；
- **分段耗时均保存**：分段值从 `metrics_snapshot()` 取，与 `/metrics` 端点同一份数据；
- **不外推生产**：Mock TTS 是合成音（`tts.py::mock_duration_seconds`），延迟天然极低，
  这份基线只能证明「链路通了、口径对了」，不能当作生产 P95（plan §8 R3）。

用法（uv 在本机不可用，用 venv 直跑）::

    .venv\\Scripts\\python.exe eval\\harness\\latency_baseline.py
    .venv\\Scripts\\python.exe eval\\harness\\latency_baseline.py --out eval/reports/latency.json

注意：本文件**不是** pytest 用例，不进 `tests/`；它是可复跑的测量脚本，产物进 `eval/reports/`。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

# 直接 `python eval/harness/latency_baseline.py` 时 cwd 不一定是仓库根；在 import app 之前
# 把仓库根补进 sys.path（pytest / 模块导入路径下 `__name__` 不是 "__main__"，此处不动）。
if __name__ == "__main__":
    _REPO_ROOT = Path(__file__).resolve().parents[2]
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))

from app.audio.asr import TTSProvider
from app.audio.reply_stub import ReplyStub
from app.audio.tts import build_tts
from app.audio.tts_sentences import synthesize_by_sentence
from app.core.metrics import metrics_snapshot, reset_metrics

#: 固定样例（§6.1）：「20 次固定问『今天天气』」。
QUESTION: Final = "今天天气怎么样？"

#: v1.1 补充探针（非判据）样本文本：88 字（含标点）/ 3 句，句句都超过
#: `MIN_SENTENCE_CHARS=6` 不会被合并；句内逗号不切（切句口径见
#: `app/audio/tts_sentences.py` 的 docstring），三句按 `。` 独立成句。
PROBE_TEXT: Final[str] = (
    "今天杭州的天气相当不错，晴朗少云，很适合到室外走一走。"
    "下午三点以后云量会慢慢增多，出门记得带把伞备着。"
    "反正闲着也是闲着，晚饭后咱们一起去小区花园散散步，再听一段越剧放松放松吧。"
)

#: 补充探针连跑次数（非判据）。
PROBE_RUNS: Final = 5

#: 试运行次数（不计入统计）。
WARMUP_RUNS: Final = 3

#: 正式测量次数（§6.1）。
MEASURED_RUNS: Final = 20

#: AC-40 判据：端到端 ≤1.5s；20 次里 ≥19 次达标（PRD line 521 / line 572）。
#: v1.1 起「端到端」的口径 = **首包**（第一句 PCM 入队即「开口」，plan §4.2b）；
#: 预算值与达标次数不变。
E2E_BUDGET_MS: Final = 1500.0
E2E_MIN_PASS: Final = 19


def nearest_rank(samples: Sequence[float], percentile: float) -> float | None:
    """与 `app/core/metrics.py::_nearest_rank` 同一算法（独立实现，避免脚本依赖私有函数）。"""
    if not samples:
        return None
    rank = min(max(math.ceil(percentile / 100.0 * len(samples)), 1), len(samples))
    return samples[rank - 1]


class _RecordingSession:
    """`MediaSession` 的最小替身：记录**逐句**入队的「开口」与「全部入队」时刻。

    逐句口径（§4.2b，v1.4）下一轮会入队多个 pcm 块：`first_at` 只捕**第一块**
    （开口/首包时刻），`last_at` 捕最后一块（整轮入队时刻），互不覆盖
    （v1.0 整段口径下一个 turn 只有一块，两个字段自然同值）。
    """

    def __init__(self) -> None:
        self.first_at: float | None = None
        self.last_at: float | None = None
        self.pcm_bytes = 0
        self.pcm_calls = 0
        self.messages: list[dict[str, Any]] = []

    async def enqueue_tts_pcm(self, turn_id: str, pcm: bytes) -> None:
        now = time.perf_counter()
        if self.first_at is None:
            self.first_at = now
        self.last_at = now
        self.pcm_bytes += len(pcm)
        self.pcm_calls += 1

    def deliver(self, message: dict[str, Any]) -> bool:
        """垫层的文字降级下行（生产是 WS `deliver`）。"""
        self.messages.append(dict(message))
        return True


@dataclass(slots=True)
class RunResult:
    index: int
    e2e_ms: float  # 首包口径（§4.2b）：asr_final → **第一句** PCM 入队（「开口」时刻）
    round_total_ms: float  # 补充口径（非判据）：asr_final → 全部句子入队完成
    pcm_bytes: int  # 本轮入队的 PCM 总字节数（逐句口径下为各句之和）
    pcm_calls: int  # 本轮入队次数 = 句数（固定样本按合并规则切为单句时恒为 1）
    cold_start: bool = False
    warmup: bool = False
    degraded: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "e2e_ms": round(self.e2e_ms, 2),
            "round_total_ms": round(self.round_total_ms, 2),
            "pcm_bytes": self.pcm_bytes,
            "pcm_calls": self.pcm_calls,
            "cold_start": self.cold_start,
            "warmup": self.warmup,
            "degraded": self.degraded,
        }


@dataclass(slots=True)
class ProbeResult:
    """补充探针（非判据）单次结果：多句样本上的「首包 vs 整轮」。

    first_ms = **第一句拿到 pcm** 的墙钟（本探针里即第一次 `synthesize` 返回时刻）；
    total_ms = **全部句子入队完成**（生成器循环结束）的墙钟；管线是 F1 的
    `synthesize_by_sentence`，与回复循环同一条逐句路径。
    """

    index: int
    sentences: int
    first_ms: float
    total_ms: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "sentences": self.sentences,
            "first_ms": round(self.first_ms, 2),
            "total_ms": round(self.total_ms, 2),
        }


@dataclass(slots=True)
class Baseline:
    runs: list[RunResult] = field(default_factory=list)
    timings: dict[str, Any] = field(default_factory=dict)
    counters: dict[str, Any] = field(default_factory=dict)
    # v1.1 补充探针（非判据）：跑在 metrics_snapshot() 之后，独立于主测口径。
    probe: list[ProbeResult] = field(default_factory=list)
    probe_provider: str = ""

    @property
    def measured(self) -> list[RunResult]:
        return [r for r in self.runs if not r.warmup and not r.cold_start]

    def summary(self) -> dict[str, Any]:
        values = [r.e2e_ms for r in self.measured]
        ordered = sorted(values)
        passes = sum(1 for v in values if v <= E2E_BUDGET_MS)
        return {
            "runs_measured": len(values),
            "p50_ms": round(nearest_rank(ordered, 50) or 0.0, 2),
            "p95_ms": round(nearest_rank(ordered, 95) or 0.0, 2),
            "min_ms": round(ordered[0], 2) if ordered else None,
            "max_ms": round(ordered[-1], 2) if ordered else None,
            "within_1500ms": passes,
            "pass": passes >= E2E_MIN_PASS,
            "budget_ms": E2E_BUDGET_MS,
            "required": E2E_MIN_PASS,
        }

    def round_summary(self) -> dict[str, Any]:
        """补充口径（非判据）：整轮入队（全部句子）的 P50/P95/min/max。"""
        ordered = sorted(r.round_total_ms for r in self.measured)
        return {
            "p50_ms": round(nearest_rank(ordered, 50) or 0.0, 2),
            "p95_ms": round(nearest_rank(ordered, 95) or 0.0, 2),
            "min_ms": round(ordered[0], 2) if ordered else None,
            "max_ms": round(ordered[-1], 2) if ordered else None,
        }


async def _run_once(stub: ReplyStub, index: int, *, warmup: bool, cold: bool) -> RunResult:
    session = _RecordingSession()
    turn_id = f"baseline-{index:02d}"
    started = time.perf_counter()
    outcome = await stub.reply(turn_id=turn_id, session=session, asr_text=QUESTION)  # type: ignore[arg-type]
    first = session.first_at if session.first_at is not None else time.perf_counter()
    total = session.last_at if session.last_at is not None else first
    return RunResult(
        index=index,
        # 首包口径（§4.2b / v1.4）：第一句拿到并**入队完成**的时刻，即「开口」。
        e2e_ms=(first - started) * 1000.0,
        # 补充口径（非判据）：全部句子入队完成；单句样本上与 e2e 同值。
        round_total_ms=(total - started) * 1000.0,
        pcm_bytes=session.pcm_bytes,
        pcm_calls=session.pcm_calls,
        cold_start=cold,
        warmup=warmup,
        degraded="degraded" in str(outcome),
    )


async def _probe_once(provider: TTSProvider, index: int) -> ProbeResult:
    """单独驱动 F1 的 `synthesize_by_sentence` 管线跑一遍多句样本（非判据）。

    与回复循环同一条逐句路径、同一个 provider 实例；逐句 `synthesize` 的 `_record`
    结构化日志照常触发（轮级打点按 迭代 5 v1.3 §4.2b 归回复管线，探针不进 /metrics），
    探针跑在 `metrics_snapshot()` **之后**，不会污染主测 20 次快照。
    """
    session = _RecordingSession()
    started = time.perf_counter()
    first_ms: float | None = None
    sentences = 0
    async for pcm in synthesize_by_sentence(PROBE_TEXT, provider=provider):
        if first_ms is None:
            # 「第一句拿到 pcm」：捕在入队**之前**，语义对齐逐句打点的 first 字段。
            first_ms = (time.perf_counter() - started) * 1000.0
        sentences += 1
        await session.enqueue_tts_pcm(f"probe-{index:02d}", pcm)
    finished = time.perf_counter()  # 循环结束 = 最后一句已入队（或被空跳）
    return ProbeResult(
        index=index,
        sentences=sentences,
        first_ms=first_ms if first_ms is not None else (finished - started) * 1000.0,
        total_ms=(finished - started) * 1000.0,
    )


async def measure() -> Baseline:
    """3 次试运行 + 1 次冷启动（单记）+ 20 次正式测量 + 5 次补充探针（非判据）。

    为什么在冷启动之后、正式测量之前 `reset_metrics()`：`/metrics` 快照是**进程级累加**，
    不清零的话分段 P95 会把试运行一起算进去，与报告里的 e2e 20 次不同口径。
    快照取在探针**之前**：探针会让 provider 层逐句打点多跑几轮，混进快照会把
    20 次的分段 count/P95 搅乱，所以探针放在快照之后。
    """
    stub = ReplyStub(tts=build_tts())
    baseline = Baseline()

    for i in range(WARMUP_RUNS):
        baseline.runs.append(await _run_once(stub, i, warmup=True, cold=False))

    baseline.runs.append(await _run_once(stub, WARMUP_RUNS, warmup=False, cold=True))

    reset_metrics()
    for i in range(MEASURED_RUNS):
        baseline.runs.append(await _run_once(stub, WARMUP_RUNS + 1 + i, warmup=False, cold=False))

    snapshot = metrics_snapshot()
    baseline.timings = dict(snapshot["timings"])  # type: ignore[arg-type]
    baseline.counters = dict(snapshot["counters"])  # type: ignore[arg-type]

    # v1.1 补充探针：唯一作用是证明「逐句流式下首包 ≪ 整轮」，非判据、不进 20 次口径。
    baseline.probe_provider = stub.tts.name
    for i in range(PROBE_RUNS):
        baseline.probe.append(await _probe_once(stub.tts, i))
    return baseline


def _fmt(value: Any) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


def _timing_count(baseline: Baseline, name: str) -> int:
    stats = baseline.timings.get(name)
    return int(stats["count"]) if stats else 0


def render_markdown(baseline: Baseline) -> str:
    summary = baseline.summary()
    lines: list[str] = []
    lines.append("## 20 次实测（本地，不外推生产）")
    lines.append("")
    lines.append(
        f"- 固定样例：`{QUESTION}`；试运行 {WARMUP_RUNS} 次不计入；"
        f"第 1 次正式跑标 `cold_start` 不计入统计"
    )
    lines.append(
        "- 口径（plan §4.2b / v1.4）：**首包 = 第一句完成时刻** —— 端到端从 `asr_final` "
        "起算到**第一句 PCM 入队**；「整轮入队」= 全部句子入队完成（补充口径，非判据）"
    )
    lines.append(
        f"- 判据：{summary['runs_measured']} 次中 ≥{E2E_MIN_PASS} 次首包 ≤"
        f"{int(E2E_BUDGET_MS)}ms → **{'达标' if summary['pass'] else '未达标'}**"
        f"（本次 {summary['within_1500ms']}/{summary['runs_measured']}）"
    )
    lines.append("")
    lines.append("| # | 冷启动 | 端到端（首包）ms | 整轮入队 ms | 句数 | PCM 字节 | 降级 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    measured_ordinal = 0
    for run in baseline.runs:
        if run.warmup:
            continue
        if run.cold_start:
            label = "冷"
        else:
            measured_ordinal += 1
            label = str(measured_ordinal)
        lines.append(
            f"| {label} | {'是' if run.cold_start else ''} | "
            f"{run.e2e_ms:.1f} | {run.round_total_ms:.1f} | {run.pcm_calls} | "
            f"{run.pcm_bytes} | {'是' if run.degraded else ''} |"
        )
    lines.append("")
    lines.append("### 分段 P50 / P95（`/metrics` 快照，口径 = 本次 20 次正式跑）")
    lines.append("")
    lines.append("| 节点 | 指标名 | count | P50 ms | P95 ms | max ms |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    node_rows = [
        ("③ 路由决策（占位 0ms）", "route_ms"),
        ("④ LLM 首 token（垫层）", "llm_first_token_ms"),
        ("⑤ TTS 首包（第一句完成时刻，§4.2b）", "tts_first_packet_ms"),
        ("⑤ TTS 全句入队", "tts_total_ms"),
        ("② ASR 首包", "asr_first_packet_ms"),
        ("② ASR 整段", "asr_total_ms"),
    ]
    for label, name in node_rows:
        stats = baseline.timings.get(name)
        if stats is None:
            lines.append(f"| {label} | `{name}` | 0 | N/A | N/A | N/A |")
            continue
        lines.append(
            f"| {label} | `{name}` | {int(stats['count'])} | "
            f"{_fmt(stats.get('p50_ms'))} | {_fmt(stats.get('p95_ms'))} | "
            f"{_fmt(stats.get('max_ms'))} |"
        )
    lines.append("")
    lines.append(
        "> ② ASR 两行为 N/A：本脚本只测**回复侧**（asr_final 之后），不接 ASR 引擎；"
        "ASR 分段要真实麦克风/音频流，见 §3.2 ② 的 `record_asr` 打点（已接线，等有音频输入时自然有数）。"
    )
    lines.append(
        "> ⑤ 首包口径（§4.2b / v1.4）：`tts_first_packet_ms` = 逐句循环起点 → **第一句拿到 pcm**"
        f"（本次快照 count={_timing_count(baseline, 'tts_first_packet_ms')}），"
        "`tts_total_ms` = 循环起点 → **全部句子入队完成**"
        f"（count={_timing_count(baseline, 'tts_total_ms')}）；正式轮数 {summary['runs_measured']}，"
        "count 与轮数一致即为「一轮一记」。"
    )
    lines.append(
        "> 固定样本「今天天气怎么样？」约 8 字，按 `MIN_SENTENCE_CHARS=6` 合并规则"
        "**单句**切分开口：该样本上首包 ≈ 整轮属正常，不是 bug；多句表现见下方补充探针。"
    )
    lines.append("")
    lines.append(
        f"- 首包 P50 = {summary['p50_ms']} ms，P95 = {summary['p95_ms']} ms，"
        f"min = {summary['min_ms']} ms，max = {summary['max_ms']} ms"
    )
    round_stats = baseline.round_summary()
    lines.append(
        f"- 补充（非判据）整轮入队：P50 = {round_stats['p50_ms']} ms，"
        f"P95 = {round_stats['p95_ms']} ms，max = {round_stats['max_ms']} ms"
    )
    lines.append("")
    lines.append("### 补充探针：多句样本 首包 vs 整轮（非判据，5 次）")
    lines.append("")
    probe_sentences = baseline.probe[0].sentences if baseline.probe else 0
    lines.append(f"- 样本文本（{len(PROBE_TEXT)} 字含标点 / {probe_sentences} 句）：`{PROBE_TEXT}`")
    lines.append(
        f"- 管线与主测同源：同一 provider（`{baseline.probe_provider}`）、"
        "同一条 `synthesize_by_sentence` 逐句路径；首包 = 第一句完成时刻，"
        "整轮 = 全部句子入队完成；跑在 `/metrics` 20 次快照之后，不掺入主测口径。"
    )
    lines.append("")
    lines.append("| 次数 | 句数 | 首包 ms | 整轮入队 ms | 整轮 / 首包 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for probe in baseline.probe:
        ratio = probe.total_ms / probe.first_ms if probe.first_ms > 0 else 0.0
        lines.append(
            f"| {probe.index + 1} | {probe.sentences} | {probe.first_ms:.1f} | "
            f"{probe.total_ms:.1f} | {ratio:.1f}× |"
        )
    lines.append("")
    lines.append("### 降级 / 占位计数")
    lines.append("")
    lines.append("| 计数键 | 值 |")
    lines.append("| --- | --- |")
    for key in sorted(baseline.counters):
        lines.append(f"| `{key}` | {baseline.counters[key]} |")
    lines.append("")
    # v1.1（E1-r2，2026-11-07）：验收要求两份报告文末都有变更记录行，所以这条也进
    # 生成器（而不是往生成文件上手抄一行）——复跑重生成时记录行不丢。数字全部取自
    # 本次运行的 summary，永不手抄；v1.0 的历史数字在 baseline 报告里，这里只留指引。
    lines.append("### 变更记录")
    lines.append("")
    lines.append("| 版本 | 日期 | 说明 |")
    lines.append("| --- | --- | --- |")
    lines.append(
        "| v1.0 | 2026-11-07 | 整段合成口径首版（历史；完整记录见 `day05-latency-baseline.md` "
        "变更记录——本文件由 harness 整体重生成，不保留历史逐次数字） |"
    )
    lines.append(
        f"| v1.1 | 2026-11-07 | 口径变更（plan v1.3 §4.2b + v1.4 重执行）：TTS **逐句合成、逐句"
        f"入队**，首包 = **第一句完成时刻**；判据与预算不变。本表即 v1.1 口径实测："
        f"{summary['runs_measured']} 次中 {summary['within_1500ms']} 次 ≤1500ms"
        f"（**{'达标' if summary['pass'] else '未达标'}**），P50 {summary['p50_ms']} ms / "
        f"P95 {summary['p95_ms']} ms / max {summary['max_ms']} ms；"
        "三档降级演示（`degrade_demo.py`）未变、零改动。 |"
    )
    lines.append("")
    return "\n".join(lines)


def _write_outputs(baseline: Baseline, *, markdown_path: str, json_path: str) -> None:
    """落盘（同步 I/O，放在 async 之外，免得 ruff ASYNC240 又判「异步里做阻塞文件操作」）。"""
    markdown = render_markdown(baseline)
    print(markdown)
    if markdown_path:
        # 显式 UTF-8 写出：Windows 控制台是 GBK，直接重定向 stdout 会把中文表头写成乱码。
        md_path = Path(markdown_path)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(markdown, encoding="utf-8")
        print(f"\n[markdown] {md_path}")
    if json_path:
        probe = [p.as_dict() for p in baseline.probe]
        payload = {
            "question": QUESTION,
            "mode": "sentence_stream_v1_1",  # v1.4 §4.2b：逐句合成逐句入队，首包=第一句完成时刻
            "warmup_runs": WARMUP_RUNS,
            "measured_runs": MEASURED_RUNS,
            "summary": baseline.summary(),
            "round_summary": baseline.round_summary(),
            "runs": [r.as_dict() for r in baseline.runs],
            "timings": baseline.timings,
            "counters": baseline.counters,
            "probe": {
                "text": PROBE_TEXT,
                "provider": baseline.probe_provider,
                "runs": probe,
            },
        }
        path = Path(json_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[json] {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="迭代 5 端到端延迟基线（20 次实测 + 多句探针）")
    parser.add_argument("--out", default="", help="把原始结果写成 JSON（目录不存在会自动建）")
    parser.add_argument("--markdown", default="", help="把 markdown 表写成 UTF-8 文件")
    args = parser.parse_args()

    baseline = asyncio.run(measure())
    _write_outputs(baseline, markdown_path=args.markdown, json_path=args.out)
    return 0 if baseline.summary()["pass"] else 1


if __name__ == "__main__":
    # 直接 `python eval/harness/latency_baseline.py` 时 cwd 不一定是仓库根，
    # 补一次 sys.path 比要求调用方记住 PYTHONPATH 更不容易出错（pytest 下不走这条分支）。
    import sys

    _ROOT = Path(__file__).resolve().parents[2]
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    raise SystemExit(main())
