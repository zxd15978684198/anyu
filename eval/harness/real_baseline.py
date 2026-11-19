"""迭代 5 · P1-6：真实链路 20 次延迟复测（真 LLM + 真 MCP 天气工具 + 真 Edge TTS）。

## 为什么另起一个脚本

`latency_baseline.py` 量的是 `ReplyStub`（mock TTS、无 ASR、无工具），报告里已写明
「不可外推生产」。本脚本走生产组件的真实实现：

```
注入终稿文本 → DialogueService.respond
                 ├─ Agent（ReAct：真 LLM + 真 weather MCP get_weather）
                 └─ 或 llm.chat（真 DeepSeek 兼容端点）
              → synthesize_by_sentence（Edge TTS）→ 首块 PCM
```

度量三段：`reply_ms`（组装 + Agent/LLM 出话术）、`tts_ms`（首句 TTS 首块）、
`total_ms`（前两段之和，即服务端回复侧端到端）。

## 仍然不覆盖的三段（报告里必须继续标注，不得外推）

- **① 唤醒**：端侧 VAD/唤醒词，服务端没有点位；
- **② 真 ASR**：本机 FunASR（`:7002`）未启动，脚本直接注入终稿文本；
- **⑥ 前端播放**：浏览器侧，唯一证据是 `/metrics` 的 `client_playback_ms`（P1-6 已接）。

## 三档预算

`reply_ms` 走的是 `DialogueService` 内的 LLM 档（3s，`with_timeout(stage="llm")`），
`tts_ms` 走的是与 `dialogue_runtime._deliver_audio` 同构的 TTS 档（1s）。所以本表
既给出真实耗时，也给出「按当前预算会不会降级」——两者必须一起读。

用法：

```bash
PYTHONPATH=. uv run python eval/harness/real_baseline.py --runs 20
```
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.agents.runtime_factory import build_agent_runner, build_tool_registry, close_tool_registry
from app.audio.tts import EDGE_DEFAULT_VOICE, EdgeTTSProvider
from app.audio.tts_sentences import synthesize_by_sentence
from app.core.config import get_settings
from app.core.context import DialogueIdentity
from app.core.timeouts import STAGE_TTS, with_timeout
from app.services.dialogue_service import DialogueService
from app.services.llm_service import build_llm

#: 10 个问法轮两遍 = 20 次。混合长句/短句/带地名，避免同一个 prompt 的缓存效应。
QUESTIONS: tuple[str, ...] = (
    "今天天气怎么样？",
    "外面下雨了吗？",
    "今天适合出去散步吗？",
    "杭州今天冷不冷？",
    "明天天气好不好？",
    "现在外面风大不大？",
    "今天要带伞吗？",
    "今天最高气温多少度？",
    "上海今天天气如何？",
    "晚上出门要多穿点吗？",
)

#: 生产预算（PRD:543 1.5s 口径；TD:1035-1041 三档阈值）。
TOTAL_BUDGET_MS = 1500.0


@dataclass(slots=True)
class RunRow:
    index: int
    question: str
    reply_ms: float
    tts_ms: float
    total_ms: float
    degraded: bool
    tool_calls: int
    tts_ok: bool
    tts_timeout_ms: float
    error: str = ""


def _nearest_rank(values: list[float], percentile: float) -> float | None:
    """nearest-rank：与 `app/core/metrics.py` 同一算法，样本少时不造插值假数。"""
    if not values:
        return None
    ordered = sorted(values)
    rank = min(max(math.ceil(percentile / 100.0 * len(ordered)), 1), len(ordered))
    return ordered[rank - 1]


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f}"


async def _run_once(
    *,
    index: int,
    question: str,
    service: DialogueService,
    tts: EdgeTTSProvider,
) -> RunRow:
    # 天气句必须走 Agent（L0 固定意图 + 唯一真实 MCP Tool，见 dialogue_service._AGENT_INTENTS）；
    # 这里注入的 decision 只有 layer/intent 两个字段被 respond 读，和路由层形状一致。
    decision = SimpleNamespace(layer="L0", intent="weather_query")
    identity = DialogueIdentity(source="device", elder_id="baseline-elder", display_name="基线长者")

    started = time.perf_counter()
    reply_text = ""
    degraded = True
    tool_calls = 0
    error = ""
    try:
        reply = await service.respond(
            thread_id=f"baseline{index:04d}",
            identity=identity,
            user_text=question,
            decision=decision,
        )
        reply_text = reply.text
        degraded = reply.degraded
        tool_calls = reply.tool_calls
    except Exception as exc:  # 单次失败不终止整批，但要出现在表里
        error = f"{type(exc).__name__}: {exc}"[:200]
    reply_ms = (time.perf_counter() - started) * 1000.0

    tts_ok = False
    tts_ms = 0.0
    tts_timeout_ms = 0.0
    if reply_text and not error:
        agen = synthesize_by_sentence(reply_text, provider=tts)
        tts_started = time.perf_counter()
        try:
            first = await with_timeout(agen.__anext__(), stage=STAGE_TTS)
            tts_ok = bool(first.ok and first.value)
            tts_timeout_ms = float(first.timeout_ms)
        except StopAsyncIteration:
            error = "empty_reply"
        except Exception as exc:  # 供应商网络错误也要落表，而不是让整批崩掉
            error = f"tts {type(exc).__name__}: {exc}"[:200]
        finally:
            await agen.aclose()
        tts_ms = (time.perf_counter() - tts_started) * 1000.0

    return RunRow(
        index=index,
        question=question,
        reply_ms=round(reply_ms, 1),
        tts_ms=round(tts_ms, 1),
        total_ms=round(reply_ms + tts_ms, 1),
        degraded=degraded,
        tool_calls=tool_calls,
        tts_ok=tts_ok,
        tts_timeout_ms=tts_timeout_ms,
        error=error,
    )


def _report(rows: list[RunRow]) -> dict[str, Any]:
    reply = [r.reply_ms for r in rows]
    tts = [r.tts_ms for r in rows]
    total = [r.total_ms for r in rows]
    within = [r for r in rows if not r.error and not r.degraded and r.tts_ok]
    return {
        "runs": len(rows),
        "reply_ms": {
            "p50": _nearest_rank(reply, 50),
            "p95": _nearest_rank(reply, 95),
            "max": max(reply) if reply else None,
        },
        "tts_ms": {
            "p50": _nearest_rank(tts, 50),
            "p95": _nearest_rank(tts, 95),
            "max": max(tts) if tts else None,
        },
        "total_ms": {
            "p50": _nearest_rank(total, 50),
            "p95": _nearest_rank(total, 95),
            "max": max(total) if total else None,
        },
        "degraded_runs": sum(1 for r in rows if r.degraded),
        "tts_timeout_runs": sum(1 for r in rows if not r.tts_ok),
        "tool_call_runs": sum(1 for r in rows if r.tool_calls > 0),
        "tool_calls_total": sum(r.tool_calls for r in rows),
        "error_runs": sum(1 for r in rows if r.error),
        "within_budget": f"{len(within)}/{len(rows)}",
        "budget_ms": TOTAL_BUDGET_MS,
        "note": ("服务端回复侧（③→⑤）；不含 ① 唤醒、② 真 ASR、⑥ 前端播放，不得外推生产 P95"),
    }


def _markdown(rows: list[RunRow], summary: dict[str, Any]) -> str:
    lines = [
        "# 迭代 5 · 真实链路延迟基线（P1-6 复测）",
        "",
        "> 口径：服务端回复侧 ③ 组装 → ④ LLM/Agent → ⑤ TTS 首块；"
        "不含 ① 唤醒、② 真 ASR、⑥ 前端播放。**不得外推生产 P95**（PRD:550）。",
        f"> 生成：`eval/harness/real_baseline.py --runs {summary['runs']}`，"
        "真 LLM（DeepSeek 兼容端点）+ 真 weather MCP 工具 + 真 Edge TTS。",
        "",
        "## 汇总",
        "",
        "| 指标 | P50 | P95 | max |",
        "| --- | --- | --- | --- |",
    ]
    for name, key in (
        ("reply_ms（Agent/LLM 出话术）", "reply_ms"),
        ("tts_ms（首句首块）", "tts_ms"),
    ):
        block = summary[key]
        lines.append(
            f"| {name} | {_pct(block['p50'])} | {_pct(block['p95'])} | {_pct(block['max'])} |"
        )
    total = summary["total_ms"]
    lines.append(
        f"| **total_ms（服务端回复侧）** | {_pct(total['p50'])} | {_pct(total['p95'])} | "
        f"{_pct(total['max'])} |"
    )
    lines += [
        "",
        f"- 1.5s 内且未降级：**{summary['within_budget']}**（预算 {summary['budget_ms']:.0f}ms）",
        f"- LLM 档降级（degraded）：{summary['degraded_runs']}/{summary['runs']}",
        f"- TTS 档超时（1s 预算）：{summary['tts_timeout_runs']}/{summary['runs']}",
        f"- 真调用工具的轮次：{summary['tool_call_runs']}，工具调用合计 {summary['tool_calls_total']} 次",
        f"- 异常轮次：{summary['error_runs']}",
        "",
        "## 逐次明细",
        "",
        "| # | 问题 | reply_ms | tts_ms | total_ms | degraded | tool_calls | tts_ok | error |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row.index} | {row.question} | {row.reply_ms:.0f} | {row.tts_ms:.0f} | "
            f"{row.total_ms:.0f} | {'是' if row.degraded else '否'} | {row.tool_calls} | "
            f"{'是' if row.tts_ok else '否'} | {row.error} |"
        )
    lines += [
        "",
        "## 读数注意",
        "",
        "- TTS 是 **Edge TTS 非流式实现**：`synthesize()` 收完整句才返回，所以 `tts_ms` 是",
        "  「整句合成就绪」而不是「流式首包」；与 1s 预算比对时按此口径解释。",
        "- 工具调用次数为 0 的轮次说明 Agent 没走到 `get_weather`（可能是模型直接作答），",
        "  这本身是可观测信号，不要在报告里当成成功。",
        "- 超 1.5s 不是测量误差：PRD:543 的口径是「首段回答音频开始播放」，当前真 provider",
        "  组合（非流式 Edge TTS + 2s 量级的兼容端点）在 LLM 3s 与 TTS 1s 两档都撞线。",
        "  结论只能写成「当前 provider 不满足预算」，**不得下调阈值**（AGENTS §7）。",
    ]
    if summary["within_budget"].startswith("0/") and summary["runs"] > 1:
        lines.insert(
            lines.index("## 读数注意"),
            "> **结论**：0 轮在预算内。当前真 provider 组合不满足 PRD:543 的 1.5s；"
            "阈值按 AGENTS §7 不得下调，需换流式 TTS / 更快的 LLM 端点后复测。\n",
        )
    return "\n".join(lines) + "\n"


async def _main(runs: int) -> list[RunRow]:
    settings = get_settings()
    llm = build_llm(settings)
    tool_registry = await build_tool_registry(settings)
    if tool_registry is None:
        print("[baseline] weather MCP 不可达：工具层未装配，Agent 将退回 llm.chat", file=sys.stderr)
    runner = build_agent_runner(settings=settings, llm=llm, tool_registry=tool_registry)
    service = DialogueService(llm=llm, store=None, agent=runner)
    tts = EdgeTTSProvider(voice=EDGE_DEFAULT_VOICE, speed=1.0)

    rows: list[RunRow] = []
    try:
        for index in range(1, runs + 1):
            question = QUESTIONS[(index - 1) % len(QUESTIONS)]
            row = await _run_once(index=index, question=question, service=service, tts=tts)
            rows.append(row)
            print(
                f"[{index:02d}/{runs}] reply={row.reply_ms:.0f}ms tts={row.tts_ms:.0f}ms "
                f"degraded={row.degraded} tools={row.tool_calls} err={row.error or '-'}",
                flush=True,
            )
    finally:
        await close_tool_registry(tool_registry)
    return rows


def _write(json_path: Path, md_path: Path, rows: list[RunRow]) -> dict[str, Any]:
    summary = _report(rows)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(
            {"summary": summary, "rows": [asdict(row) for row in rows]},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    md_path.write_text(_markdown(rows, summary), encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="迭代 5 真实链路延迟基线")
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--json", default="eval/reports/latency-real.json")
    parser.add_argument("--md", default="eval/reports/latency-real.md")
    args = parser.parse_args(argv)
    rows = asyncio.run(_main(args.runs))
    summary = _write(Path(args.json), Path(args.md), rows)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
