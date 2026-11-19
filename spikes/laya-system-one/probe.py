#!/usr/bin/env python
"""Laya System One · CPU spike（DAG 节点 N30）——只回答三个数，不接生产。

要回答的三个数
--------------
1. **中文七类意图准确率**：20 条自造中文长者句（七类各若干）逐条问模型，算 top-1 准确率。
2. **CPU 单句延迟**：p50 / p95（毫秒），口径是「一句长者话 → 五字段」的一次完整调用。
3. **进程 RSS**：加载后与跑完后的常驻内存峰值（MB）。

口径与作弊边界（先说清，避免把数字读成它没有的意思）
--------------------------------------------------
- 权重只用 ``$LAYA_MODEL_ROOT/multilingual``（mmBERT-base，根目录由 ``LAYA_MODEL_ROOT`` 指定）。根目录与 ``typed-decisions/``
  是英文 ModernBERT-large 面，中文上接近随机，**不是**本 spike 的评测对象。
- 样本是**为本 spike 手写的 20 条**，不是 ``eval/fixtures/routing.jsonl`` 的官方固定集，
  也不是 PRD 验收集。20 条只够看「量级」，置信区间很宽；准确率数字不得当作验收结论。
- 七类枚举：``app/system_one/questions.py`` 里**没有**七类常量（该文件只有 ``alert``
  与五信号透传），所以这里按 ``AGENTS.md`` §6 的七类中文名 + ``app/core/injection.py``
  ``INTENT_WHITELIST`` 的既有生产词对齐成 ``chitchat/medicine/opera/movie/weather/health``，
  第七类「其他」用 ``other``。**没有自创新词，但确实不是从 questions.py 抄来的**——
  这个出处缺口已写进 README「已知不一致」。
- 不问模型「五字段」端到端，而是把五字段拆成 1 个 choice（intent）+ 3 个 noul
  （sensitive / privacy / is_out_of_domain），一次 ``system_one`` 批量问完；
  ``confidence`` 用 intent 那一问自带的 ``confidence``（1 - 归一化熵）。这是**拼装**，
  与 ``app/system_one/client.py`` 的五字段契约形似而神不同：真实服务壳应由 GBNF/正则
  强约束产出，本文件只做 spike，不冒充服务端。
- 延迟含 tokenize + 前向 + 解码（即 ``RLAgent.system_one`` 整体），**不含** HTTP 往返。
  另单独跑一遍「只问 intent」的单问题延迟，用来对齐官方 193–464ms 的口径。
- 不做任何降级/重试/超时伪装：跑不快就是跑不快，数字原样打印。

用法
----
    # 用 .tmp/laya-venv 里那份（torch CPU + transformers + psutil）
    .tmp\\laya-venv\\Scripts\\python.exe spikes\\laya-system-one\\probe.py
    # 顺带落一份机器可读结果
    .tmp\\laya-venv\\Scripts\\python.exe spikes\\laya-system-one\\probe.py --json results.json

内存告急时（宿主可用内存 < 1GB）会先打印告警再继续——**不允许**停 funasr 容器腾地方。
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

#: 权重根目录。`rl_agent_api.py` / `rl_common.py` 就在这一层，必须先上 `sys.path`。
MODEL_ROOT: Final[Path] = Path(os.environ.get("LAYA_MODEL_ROOT", "laya-model"))

#: 中文只能走 multilingual（mmBERT-base）；根目录与 typed-decisions 是英文面。
MODEL_DIR: Final[Path] = Path(
    os.environ.get("LAYA_MODEL_DIR", str(MODEL_ROOT / "multilingual"))
)

#: 七类 intent。键刻意对齐 `app/core/injection.py::INTENT_WHITELIST` 的既有生产词，
#: 值用 `AGENTS.md` §6 的七类中文名；`other` 是第七类「其他」。
INTENTS: Final[dict[str, str]] = {
    "chitchat": "闲聊",
    "medicine": "用药查询",
    "opera": "戏曲点播",
    "movie": "电影点播",
    "weather": "天气查询",
    "health": "健康采集",
    "other": "其他",
}

#: 一次 `system_one` 问完四问 = 一次「单句 → 五字段」的服务级调用。
QUESTIONS: Final[dict[str, dict[str, Any]]] = {
    "intent": {
        "type": "choice",
        "instructions": "这位老人刚说的这句话，属于下面哪一类意图？只选一类。",
        "criteria": INTENTS,
    },
    "sensitive": {
        "type": "noul",
        "instructions": (
            "这句话是否涉及需要立刻人工介入的安全风险："
            "急症（胸痛、喘不上气、晕倒）、摔倒、自伤、走失、受虐待？"
        ),
        "criteria": {
            "false": "没有安全风险，只是日常说话",
            "true": "存在上述安全风险，必须马上叫人或告警",
        },
    },
    "privacy": {
        "type": "noul",
        "instructions": (
            "这句话是否涉及他人的隐私，或同住另一半/旁人的事务"
            "（打听别人病情、翻别人东西、替别人做主）？"
        ),
        "criteria": {
            "false": "只关乎说话人自己，或纯公共信息",
            "true": "涉及他人隐私或同住旁人的事务",
        },
    },
    "is_out_of_domain": {
        "type": "noul",
        "instructions": (
            "这句话是否超出养老陪伴助手的职责范围"
            "（法律纠纷、股票理财、写代码、政治时事、医疗诊断结论）？"
        ),
        "criteria": {
            "false": "在陪伴、提醒、点播、天气、健康采集的职责内",
            "true": "超出职责范围",
        },
    },
}

#: 20 条自造中文样本。`expected` 是七类之一；文本刻意贴近长者口语。
SAMPLES: Final[tuple[tuple[str, str, str], ...]] = (
    ("c1", "chitchat", "我孙子昨天来看我了，还带了点水果"),
    ("c2", "chitchat", "一个人在家有点闷，你陪我聊会儿天吧"),
    ("c3", "chitchat", "你说人老了是不是就爱回想以前的事"),
    ("m1", "medicine", "我那个降压药是饭前吃还是饭后吃"),
    ("m2", "medicine", "今天的药吃了没有，我记不清了"),
    ("m3", "medicine", "阿司匹林一天吃几片"),
    ("o1", "opera", "给我放一段越剧梁山伯与祝英台"),
    ("o2", "opera", "我想听京剧，来一段贵妃醉酒"),
    ("o3", "opera", "放个戏曲听听，要黄梅戏"),
    ("f1", "movie", "有没有老电影，地道战那种"),
    ("f2", "movie", "我想看电影，放个喜剧片"),
    ("f3", "movie", "上次那个电影没看完，接着放"),
    ("w1", "weather", "今天外面冷不冷，要不要加件衣服"),
    ("w2", "weather", "明天会不会下雨"),
    ("h1", "health", "我今天量了血压，高压一百五"),
    ("h2", "health", "我这两天头有点晕，浑身没力气"),
    ("h3", "health", "帮我把今天的血糖记一下"),
    ("t1", "other", "帮我查查股票涨了多少"),
    ("t2", "other", "你能帮我写一段代码吗"),
    ("t3", "other", "我那个房产证的事该怎么打官司"),
)


@dataclass
class Row:
    """一条样本的实测结果（五字段 + 延迟 + 是否命中）。"""

    sid: str
    expected: str
    text: str
    predicted: str
    hit: bool
    confidence: float
    sensitive: bool
    sensitive_p: float
    privacy: bool
    privacy_p: float
    is_out_of_domain: bool
    ood_p: float
    latency_ms: float
    act: float


# --------------------------------------------------------------------------- RSS


def _rss_mb() -> float | None:
    """当前进程 RSS（MB）。先 psutil，退 `ctypes` PSAPI，都不行返回 None。

    口径说明：RSS 含 torch 的 arena 与已释放但未还给系统的页，因此是**上界**而不是
    工作集；Windows 上也没有 `/proc`，`tracemalloc` 只统计 Python 对象、会把
    torch 的 C++ 内存全漏掉，所以只在前两条路都失败时才退它并显式标注。
    """
    try:
        import psutil  # type: ignore[import-not-found]

        return psutil.Process().memory_info().rss / (1024 * 1024)
    except Exception:  # noqa: BLE001 - 缺包/权限失败都退下一档，不中断 spike
        pass
    try:
        import ctypes
        from ctypes import wintypes

        class _Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = _Counters()
        counters.cb = ctypes.sizeof(_Counters)
        handle = ctypes.windll.kernel32.GetCurrentProcess()  # type: ignore[attr-defined]
        ok = ctypes.windll.psapi.GetProcessMemoryInfo(  # type: ignore[attr-defined]
            handle, ctypes.byref(counters), counters.cb
        )
        if ok:
            return counters.WorkingSetSize / (1024 * 1024)
    except Exception:  # noqa: BLE001 - 非 Windows / 受限环境
        pass
    return None


# --------------------------------------------------------------------------- 统计


def _percentile(values: list[float], q: float) -> float:
    """线性插值分位数（小样本上比 `median_high` 更接近「p95」的通常口径）。"""
    if not values:
        return float("nan")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def _flag(answer: dict[str, Any]) -> float:
    """`noul` 问的 P(true)；RLAgent 把 p[1] 放在 `noul` 键上。"""
    return float(answer["noul"])


# --------------------------------------------------------------------------- 主流程


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Laya System One CPU spike（N30）")
    parser.add_argument("--threads", type=int, default=4, help="torch CPU 线程数（默认 4，不抢满 8 核）")
    parser.add_argument("--json", type=Path, default=None, help="把结果写成 JSON 到这个路径")
    args = parser.parse_args(argv)

    # 分词器多进程会额外开进程抢内存，spike 一律串行。
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")

    print("=" * 78)
    print("Laya System One · CPU spike（N30）")
    print("=" * 78)
    print(f"model_dir   {MODEL_DIR}")
    print(f"model_root  {MODEL_ROOT}")

    if not MODEL_DIR.is_dir():
        print(f"[FAIL] 权重目录不存在：{MODEL_DIR}", file=sys.stderr)
        return 2

    # `rl_agent_api` 与 `rl_common` 是扁平导入，必须把权重根目录放进 sys.path。
    sys.path.insert(0, str(MODEL_ROOT))

    import torch  # noqa: PLC0415 - 故意延后：先打印路径与告警，导入 torch 很慢
    import transformers  # noqa: PLC0415

    torch.set_num_threads(args.threads)
    print(f"torch       {torch.__version__}")
    print(f"transformers {transformers.__version__}")
    print(f"threads     {torch.get_num_threads()}  device=cpu")

    rss_before = _rss_mb()

    from rl_agent_api import RLAgent  # noqa: PLC0415 - 依赖上面的 sys.path

    started = time.perf_counter()
    agent = RLAgent(str(MODEL_DIR), device="cpu")
    load_ms = (time.perf_counter() - started) * 1000.0
    rss_loaded = _rss_mb()
    print(f"加载完成    {load_ms:.0f} ms")

    # 预热：前 3 条只为把分配器/线程池热起来，不计入延迟统计（结果也不计准确率）。
    for _sid, _expected, warm_text in SAMPLES[:3]:
        agent.system_one(warm_text, QUESTIONS)
    rss_warm = _rss_mb()

    rows: list[Row] = []
    intent_only_ms: list[float] = []
    peak_rss = rss_warm or 0.0

    print()
    print("-" * 78)
    print(f"{'id':<4} {'期望':<5} {'预测':<8} {'conf':>5} {'sens':>5} {'priv':>5} {'ood':>5} {'ms':>7}  句子")
    print("-" * 78)

    for sid, expected, text in SAMPLES:
        t0 = time.perf_counter()
        out = agent.system_one(text, QUESTIONS)
        latency = (time.perf_counter() - t0) * 1000.0

        # 只问 intent 的对照延迟：官方 193–464ms 是「一次前向」，不该混进四问开销。
        t1 = time.perf_counter()
        agent.system_one(text, {"intent": QUESTIONS["intent"]})
        intent_only_ms.append((time.perf_counter() - t1) * 1000.0)

        answers = out["answers"]
        predicted = str(answers["intent"]["choice"])
        row = Row(
            sid=sid,
            expected=expected,
            text=text,
            predicted=predicted,
            hit=predicted == expected,
            confidence=float(answers["intent"]["confidence"]),
            sensitive=_flag(answers["sensitive"]) >= 0.5,
            sensitive_p=_flag(answers["sensitive"]),
            privacy=_flag(answers["privacy"]) >= 0.5,
            privacy_p=_flag(answers["privacy"]),
            is_out_of_domain=_flag(answers["is_out_of_domain"]) >= 0.5,
            ood_p=_flag(answers["is_out_of_domain"]),
            latency_ms=latency,
            act=float(answers["intent"].get("rl_agent", {}).get("act_probability", 0.0)),
        )
        rows.append(row)

        current_rss = _rss_mb()
        if current_rss is not None:
            peak_rss = max(peak_rss, current_rss)

        print(
            f"{sid:<4} {INTENTS[row.expected]:<5} {predicted:<8} {row.confidence:>5.2f} "
            f"{row.sensitive_p:>5.2f} {row.privacy_p:>5.2f} {row.ood_p:>5.2f} "
            f"{latency:>7.1f}  {text}"
        )

    rss_after = _rss_mb()

    # ---------------------------------------------------------------- 汇总
    correct = sum(row.hit for row in rows)
    total = len(rows)
    accuracy = correct / total if total else float("nan")
    latencies = [row.latency_ms for row in rows]
    confidences = [row.confidence for row in rows]
    high_conf = sum(1 for value in confidences if value >= 0.85)

    per_class: dict[str, list[int]] = {key: [0, 0] for key in INTENTS}
    for row in rows:
        per_class[row.expected][1] += 1
        if row.hit:
            per_class[row.expected][0] += 1

    five_fields_ok = sum(
        1
        for row in rows
        if isinstance(row.predicted, str)
        and isinstance(row.confidence, float)
        and isinstance(row.sensitive, bool)
        and isinstance(row.privacy, bool)
        and isinstance(row.is_out_of_domain, bool)
    )

    print()
    print("=" * 78)
    print("结果")
    print("=" * 78)
    print(f"七类准确率      {correct}/{total} = {accuracy:.1%}（20 条自造样本，量级参考，非验收）")
    print(
        "  分类召回      "
        + "  ".join(
            f"{INTENTS[key]}={hits}/{n}" for key, (hits, n) in per_class.items() if n
        )
    )
    print(
        f"置信度         中位={statistics.median(confidences):.3f}  "
        f"min={min(confidences):.3f} max={max(confidences):.3f}  "
        f">=0.85 的 {high_conf}/{total} 条"
    )
    print(
        f"单句延迟(四问) p50={_percentile(latencies, 0.50):.0f}ms  "
        f"p95={_percentile(latencies, 0.95):.0f}ms  "
        f"min={min(latencies):.0f}ms max={max(latencies):.0f}ms"
    )
    print(
        f"单句延迟(仅 intent) p50={_percentile(intent_only_ms, 0.50):.0f}ms  "
        f"p95={_percentile(intent_only_ms, 0.95):.0f}ms  "
        f"min={min(intent_only_ms):.0f}ms max={max(intent_only_ms):.0f}ms"
    )
    print(f"模型加载        {load_ms:.0f} ms")
    print(f"五字段齐全      {five_fields_ok}/{total}（探针拼装口径，见模块 docstring）")
    if rss_before is not None and rss_loaded is not None and rss_after is not None:
        print(
            f"进程 RSS        导入后={rss_before:.0f}MB  加载后={rss_loaded:.0f}MB  "
            f"预热后={rss_warm:.0f}MB  跑完={rss_after:.0f}MB  峰值={peak_rss:.0f}MB"
        )
    else:
        print("进程 RSS        不可用（psutil 与 PSAPI 都失败；未用 tracemalloc 替代）")

    missed = [row for row in rows if not row.hit]
    if missed:
        print()
        print("未命中样例")
        for row in missed:
            print(
                f"  {row.sid}  期望={INTENTS[row.expected]}  预测={row.predicted}  "
                f"conf={row.confidence:.2f}  {row.text}"
            )

    if args.json is not None:
        payload = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "model_dir": str(MODEL_DIR),
            "torch": torch.__version__,
            "transformers": transformers.__version__,
            "threads": torch.get_num_threads(),
            "load_ms": round(load_ms, 1),
            "accuracy": round(accuracy, 4),
            "correct": correct,
            "total": total,
            "confidence_median": round(statistics.median(confidences), 4),
            "confidence_ge_0_85": high_conf,
            "latency_ms": {
                "p50": round(_percentile(latencies, 0.50), 1),
                "p95": round(_percentile(latencies, 0.95), 1),
                "min": round(min(latencies), 1),
                "max": round(max(latencies), 1),
            },
            "latency_intent_only_ms": {
                "p50": round(_percentile(intent_only_ms, 0.50), 1),
                "p95": round(_percentile(intent_only_ms, 0.95), 1),
                "min": round(min(intent_only_ms), 1),
                "max": round(max(intent_only_ms), 1),
            },
            "rss_mb": {
                "after_import": None if rss_before is None else round(rss_before, 1),
                "after_load": None if rss_loaded is None else round(rss_loaded, 1),
                "after_warmup": None if rss_warm is None else round(rss_warm, 1),
                "after_run": None if rss_after is None else round(rss_after, 1),
                "peak": round(peak_rss, 1) if peak_rss else None,
            },
            "rows": [asdict(row) for row in rows],
        }
        args.json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果已写入 {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
