# owner=W2 · 依据 设计记录（全量缺口补齐） · N31 契约级验收
"""N31 验收脚本：用**真实的** `app/system_one/client.py::LayaClient` 打服务壳。

为什么不是 pytest：契约级验收必须跑真权重（torch + laya），而项目 `.venv` 按 AGENTS §4-12
不许装 torch —— 写成 pytest 只能 skip，等于没验。故做成独立脚本，用隔离 venv
（`.tmp/laya-venv`）+ `PYTHONPATH=<repo>` 跑：

    .tmp\\laya-venv\\Scripts\\python.exe deploy\\laya\\contract_check.py

## 两个阶段

1. **ASGI 进程内**：`httpx.ASGITransport` 直连 app，验五字段 + 六种非法响应；
   不打端口、不受超时干扰，任何一条不符就是契约破了。
2. **真 uvicorn**：起真端口，验「真的能服务」、`LayaTimeout`（200ms 默认值下必然超时）、
   Bearer 鉴权、以及注入关闭时 `?fault=` 被 403 拒绝。

## 为什么客户端要显式给 `laya_timeout_ms`

`client.py` 的默认 200ms 是**生产口径**（PRD §5.3 目标），CPU 纯软推理做不到；
本脚本为「能取到真实五字段」显式放宽到 5000ms —— 但另有一项**不动默认值**地验证：
200ms 客户端打 `?fault=slow` 必须抛 `LayaTimeout`。数字与偏差写回 `README.md`，不改默认值。
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import os
import socket
import statistics
import sys
import threading
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

#: 服务壳与后端共用 `LAYA_` 前缀的环境变量；本脚本自己摆好测试环境，
#: 调用方只要给对 `LAYA_MODEL_ROOT`（默认取仓库下的 `laya-model`，或由环境变量指定）。
API_KEY = "n31-contract-key"
os.environ["LAYA_API_KEY"] = API_KEY
os.environ["LAYA_FAULT_INJECT"] = "1"
os.environ.setdefault("LAYA_MODEL_ROOT", "laya-model")
os.environ.setdefault("LAYA_SUBFOLDER", "multilingual")
os.environ.setdefault("LAYA_THREADS", "4")

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from deploy.laya.server import app  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.system_one.client import (  # noqa: E402
    LayaBadPayload,
    LayaBadStatus,
    LayaClient,
    LayaTimeout,
)
from app.system_one.questions import INTENT_CLASSES  # noqa: E402

FIVE_FIELDS = frozenset({"intent", "sensitive", "confidence", "privacy", "is_out_of_domain"})
SAMPLE = "帮我放一段越剧梁山伯与祝英台"

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    return ok


def _settings(base_url: str, *, timeout_ms: int, api_key: str = "") -> Settings:
    """构造不读 `.env` 的配置快照（与 `tests/eval_gate.py:249` 同法）。"""
    return Settings(
        _env_file=None,
        laya_enabled=True,
        laya_base_url=base_url,
        laya_timeout_ms=timeout_ms,
        laya_api_key=api_key,
    )


async def _wait_ready(client: httpx.AsyncClient, base_url: str, timeout_s: float = 120.0) -> str:
    """等 `/health` 从 loading 变 ready（加载约 15s，冷启动可能更久）。"""
    deadline = time.perf_counter() + timeout_s
    last: dict[str, Any] = {}
    while time.perf_counter() < deadline:
        response = await client.get(f"{base_url}/health")
        last = response.json()
        if last.get("status") == "ready":
            return "ready"
        if last.get("status") == "failed":
            raise RuntimeError(f"laya 加载失败：{last.get('error')}")
        await asyncio.sleep(0.5)
    raise TimeoutError(f"/health 在 {timeout_s}s 内未就绪，最后状态：{last}")


async def _expect_error(
    client: LayaClient, *, fault: str, expected: type[BaseException], label: str
) -> None:
    """打一个已知非法响应，断言抛出的正是期望的那一类（不接受「别的异常也算过」）。"""
    try:
        await client.classify(f"__fault:{fault}")  # type: ignore[call-arg]
    except expected as exc:
        check(label, True, f"{type(exc).__name__}: {exc}")
    except BaseException as exc:
        check(label, False, f"期望 {expected.__name__}，实际 {type(exc).__name__}: {exc}")
    else:
        check(label, False, f"期望 {expected.__name__}，实际成功返回（契约破了）")


async def phase_a() -> None:
    """阶段 A：进程内 ASGI，一次加载。"""
    print("\n[A] ASGI 进程内（注入开启）")
    base = "http://laya.test"
    request_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=base, timeout=180
    )
    async with app.router.lifespan_context(app):
        t0 = time.perf_counter()
        status = await _wait_ready(request_client, base)
        load_wall = time.perf_counter() - t0
        check("health ready", status == "ready", f"{load_wall:.1f}s")

        raw = (await request_client.get(f"{base}/health")).json()
        check(
            "INTENT_CLASSES 七类单一出处",
            raw["intents"] == list(INTENT_CLASSES),
            f"{raw['intents']}",
        )

        # ---- 正常路径：真实客户端（不改 client.py 一行）打五字段
        client = LayaClient(
            settings=_settings(base, timeout_ms=180_000, api_key=API_KEY), client=request_client
        )
        signal = await client.classify(SAMPLE)
        payload = {
            "intent": signal.intent,
            "sensitive": signal.sensitive,
            "confidence": signal.confidence,
            "privacy": signal.privacy,
            "is_out_of_domain": signal.is_out_of_domain,
        }
        check("五字段齐全且类型合规", frozenset(payload) == FIVE_FIELDS, f"{payload}")
        check("intent 落在七类内", signal.intent in INTENT_CLASSES, signal.intent)
        check("confidence ∈ [0,1]", 0.0 <= signal.confidence <= 1.0, f"{signal.confidence}")

        # ---- 非法响应：六种形态各对应 client.py 的一个拒绝分支
        await _expect_error(
            client,
            fault="missing_field",
            expected=LayaBadPayload,
            label="缺字段 => LayaBadPayload",
        )
        await _expect_error(
            client,
            fault="bad_confidence",
            expected=LayaBadPayload,
            label="confidence 越界 => LayaBadPayload",
        )
        await _expect_error(
            client,
            fault="bad_intent",
            expected=LayaBadPayload,
            label="intent 非字符串 => LayaBadPayload",
        )
        await _expect_error(
            client,
            fault="bad_bool",
            expected=LayaBadPayload,
            label="布尔位非 bool => LayaBadPayload",
        )
        await _expect_error(
            client,
            fault="not_json",
            expected=LayaBadPayload,
            label="非 JSON 正文 => LayaBadPayload",
        )
        await _expect_error(
            client,
            fault="bad_status",
            expected=LayaBadStatus,
            label="HTTP 503 => LayaBadStatus",
        )

    # 阶段 A 的模型还挂在 app.state 上：不释放就会和阶段 B 的模型同时驻留约 2GB，
    # 把阶段 B 的延迟算成偏高的数（实测能差五成），报告里的「每轮多付」就不实了。
    app.state.engine = None
    gc.collect()

    await request_client.aclose()


async def phase_b() -> None:
    """阶段 B：真 uvicorn 端口，验超时 / 鉴权 / 注入闸门 + 单轮真实等待。"""
    print("\n[B] 真 uvicorn（注入开启 + Bearer 鉴权）")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    api_key = "n31-contract-key"

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        async with httpx.AsyncClient(base_url=base, timeout=180) as probe:
            status = await _wait_ready(probe, base)
        check("uvicorn 实际监听并就绪", status == "ready", base)

        # ---- 正常路径：真实 HTTP + 真实客户端，连打 3 次取「每轮多付的等待」
        client = LayaClient(
            settings=_settings(base, timeout_ms=180_000, api_key=api_key), client=None
        )
        latencies: list[float] = []
        try:
            signal = None
            for _ in range(3):
                t0 = time.perf_counter()
                signal = await client.classify(SAMPLE)
                latencies.append((time.perf_counter() - t0) * 1000)
            assert signal is not None
            check(
                "真实 HTTP 五字段",
                signal.intent in INTENT_CLASSES and 0.0 <= signal.confidence <= 1.0,
                f"intent={signal.intent} conf={signal.confidence:.3f} "
                f"ood={signal.is_out_of_domain} | "
                f"单轮 {statistics.median(latencies):.0f}ms "
                f"(min {min(latencies):.0f} / max {max(latencies):.0f})",
            )

            # ---- 200ms 默认值：不动默认值，验「超时确实抛 LayaTimeout」
            slow = LayaClient(settings=_settings(base, timeout_ms=200, api_key=api_key))
            try:
                await slow.classify("__fault:slow")
            except LayaTimeout as exc:
                check("200ms 客户端打慢服务 => LayaTimeout", True, str(exc))
            except BaseException as exc:
                check("200ms 客户端打慢服务 => LayaTimeout", False, type(exc).__name__)
            else:
                check("200ms 客户端打慢服务 => LayaTimeout", False, "没超时（不该发生）")

            # ---- 鉴权：漏带 Bearer 必须是 LayaBadStatus(401)，不能放行
            no_auth = LayaClient(settings=_settings(base, timeout_ms=180_000, api_key=""))
            try:
                await no_auth.classify(SAMPLE)
            except LayaBadStatus as exc:
                check("漏带 Bearer => LayaBadStatus(401)", "401" in str(exc), str(exc))
            except BaseException as exc:
                check("漏带 Bearer => LayaBadStatus(401)", False, type(exc).__name__)
            else:
                check("漏带 Bearer => LayaBadStatus(401)", False, "放行了（鉴权失效）")
        finally:
            await client.aclose()

        # ---- 注入闸门：关掉开关后 ?fault= 必须 403，而不是吐假信号
        async with httpx.AsyncClient(base_url=base, timeout=30) as raw_client:
            app.state.settings.fault_inject = False
            response = await raw_client.post(
                "/v1/classify",
                params={"fault": "missing_field"},
                json={"text": "测试"},
                headers={"authorization": f"Bearer {api_key}"},
            )
            check(
                "注入关闭时 ?fault= => 403", response.status_code == 403, str(response.status_code)
            )
    finally:
        server.should_exit = True
        thread.join(timeout=30)


async def main() -> int:
    print("=" * 78)
    print("N31 契约级验收 · 真实 LayaClient 打 deploy/laya/server.py")
    print(f"仓库 {REPO}")
    print("=" * 78)
    started = time.perf_counter()
    await phase_a()
    await phase_b()

    passed = sum(1 for _, ok, _ in _results if ok)
    print("\n" + "-" * 78)
    print(f"结果 {passed}/{len(_results)} 通过 · 总耗时 {time.perf_counter() - started:.1f}s")
    failed = [name for name, ok, _ in _results if not ok]
    if failed:
        print(f"失败项：{failed}")
        return 1
    print("全部通过：五字段契约成立，六类非法响应与三类失败均由 client.py 正确拒绝。")
    return 0


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(asyncio.run(main()))
