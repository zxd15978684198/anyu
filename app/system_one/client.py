# owner=W2 · 依据 设计记录（三级路由） §1-P2 / §4-W2 · Laya HTTP 客户端
"""System One · Laya 端侧分类器客户端（迭代 6 W2）。

**只做一件事**：把一句话发给 OpenAI 兼容的自定契约 `POST {laya_base_url}/v1/classify`，
拿回五个信号（intent / sensitive / confidence / privacy / is_out_of_domain）。
不做路由决策（那是 `questions.py` 的事）、不写库、不发 WebSocket —— 依赖方向见 AGENTS §2。

## 接口口径（计划 §1-P2 自定契约）

- 入参：`{"text": str}`；
- 出参：且仅且是五字段的 JSON 对象，`confidence ∈ [0,1]`；
- `laya_timeout_ms`（默认 200）是**客户端单次请求超时**，不是性能目标
  （性能门槛「单句 P95 ≤ 200ms」由 W5 评测报告核算）。

## 失败四分类（计划 §4-W2）

| 情形 | 异常 | 备注 |
| --- | --- | --- |
| 超过客户端超时 | `LayaTimeout` | 200ms 预算内不许重试，重试会把延迟推过上限 |
| 连接层失败（拒绝 / DNS / 传输中断） | `LayaUnavailable` | 含 `laya_enabled=False`（计划 §1-P2 spike 不过口径） |
| HTTP 非 200 | `LayaBadStatus` | 状态码进异常消息，正文与响应体不落日志 |
| 200 但五字段不全 / 类型不对 / confidence ∉ [0,1] | `LayaBadPayload` | 宁可降级 L2，不许把残缺信号带进路由 |

## 关键口径：不得伪造 Laya 输出

四类失败一律抛异常（不返回编造的信号对象、不返回空对象续跑），
由 `questions.decide_l1` 统一降级为 `layer=L2, degraded=laya_unavailable`
（`laya_fallback_to_llm` 语义，计划 §1-P2「不得为了跑通而伪造 Laya 输出」）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Final

import httpx

from app.core.config import Settings, get_settings
from app.core.metrics import observe_ms

logger = logging.getLogger("app.system_one.client")

#: OpenAI 兼容自定契约的请求路径，只拼在 `settings.laya_base_url` 后面（计划 §1-P2）。
_CLASSIFY_PATH: Final[str] = "/v1/classify"

#: 出参五字段（S-D6-1：五信号口径；AGENTS §6 的「四信号」清单待产品负责人裁决回填）。
_SIGNAL_FIELDS: Final[tuple[str, ...]] = (
    "intent",
    "sensitive",
    "confidence",
    "privacy",
    "is_out_of_domain",
)


# ---------------------------------------------------------------- 异常四分类


class LayaError(Exception):
    """Laya 调用失败的基类；调用方对四类子类统一降级（`questions.decide_l1`）。"""


class LayaTimeout(LayaError):
    """单次请求超过 `laya_timeout_ms` 预算（httpx.TimeoutException）。"""


class LayaUnavailable(LayaError):
    """连接层失败（拒绝 / DNS / 传输中断）或 `laya_enabled=False`：进程不可用口径。"""


class LayaBadStatus(LayaError):
    """HTTP 非 200（异常消息只带状态码，不带响应体）。"""


class LayaBadPayload(LayaError):
    """HTTP 200 但五字段不全 / 字段类型不对 / confidence ∉ [0,1]。"""


# ---------------------------------------------------------------- 五信号


@dataclass(frozen=True, slots=True)
class FiveSignal:
    """Laya 一次前向的五个信号（计划 S-D6-1 五信号口径）。

    `privacy` 是双人间隐私收敛标签（涉及他人/旁人的话题），迭代 6 只在
    `decision_logs` 透传留痕，路由不据此分支；迭代 7 记忆召回按它决定是否入库。
    """

    intent: str
    sensitive: bool
    confidence: float
    privacy: bool
    is_out_of_domain: bool


def _parse_signal(payload: Any) -> FiveSignal:
    """严格校验出参契约；任何残缺/越界都按 `LayaBadPayload` 拒绝，不猜字段。

    为什么严格：路由层（`questions.decide_l1`）直接把 `sensitive` 等当布尔用、
    把 `confidence` 与 0.85 比较，宽接会把「残缺/超界」变成**静默错误路由**
    ——宁可丢给 L2 兜底，也不许 L1 出假信号（计划 §1-P2 第二门槛）。
    """
    if not isinstance(payload, dict):
        raise LayaBadPayload(f"laya payload is not a json object (got {type(payload).__name__})")
    missing = [field for field in _SIGNAL_FIELDS if field not in payload]
    if missing:
        raise LayaBadPayload(f"laya payload missing fields: {','.join(missing)}")

    intent = payload["intent"]
    if not isinstance(intent, str):
        raise LayaBadPayload(f"intent must be string, got {type(intent).__name__}")

    confidence = payload["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise LayaBadPayload(f"confidence must be number, got {type(confidence).__name__}")
    value = float(confidence)
    # `not (lo <= x <= hi)` 把 NaN 也挡在门外（NaN 参与任何比较都是 False）。
    if not 0.0 <= value <= 1.0:
        raise LayaBadPayload(f"confidence out of [0,1]: {value!r}")

    booleans: list[tuple[str, Any]] = [
        ("sensitive", payload["sensitive"]),
        ("privacy", payload["privacy"]),
        ("is_out_of_domain", payload["is_out_of_domain"]),
    ]
    for name, flag in booleans:
        if not isinstance(flag, bool):
            raise LayaBadPayload(f"{name} must be bool, got {type(flag).__name__}")

    return FiveSignal(
        intent=intent,
        sensitive=payload["sensitive"],
        confidence=value,
        privacy=payload["privacy"],
        is_out_of_domain=payload["is_out_of_domain"],
    )


# ---------------------------------------------------------------- 客户端


class LayaClient:
    """OpenAI 兼容 `POST /v1/classify` 的非流式客户端。

    生命周期与 HTTP 连接池绑定：生产由装配方建一个进程级实例，退出时
    `await client.aclose()`；测试注入自带 `httpx.MockTransport` 的
    `httpx.AsyncClient`，走与生产完全相同的请求构造与响应解析路径
    （同 `tests/test_llm_service.py` 的注入模式，全程不打真网）。

    每个实例持有一个 `httpx.AsyncClient` 单例属性（复用连接池，
    不每句重建）；失败**不重试**——L1 的 200ms 预算装不下退避重试，
    失败交给 `questions.decide_l1` 统一降级。
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        if client is not None:
            # 测试/装配方注入的 client：不在这里关别人的东西。
            self._client: httpx.AsyncClient | None = client
            self._owned_client = False
        else:
            self._client = httpx.AsyncClient(
                base_url=self._settings.laya_base_url,
                timeout=self._settings.laya_timeout_ms / 1000,
            )
            self._owned_client = True

    # ---------------------------------------------------------------- 生命周期

    @property
    def settings(self) -> Settings:
        """只读配置快照（调用方断言阈值 / 打日志用）。"""
        return self._settings

    async def aclose(self) -> None:
        """关闭自建连接池；注入的 client 由注入方负责关闭。"""
        if self._owned_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---------------------------------------------------------------- 对外接口

    async def warm_up(self, *, timeout_s: float = 30.0) -> None:
        """启动期预热：用一次真实分类把权重冷启动挪到后台。

        本机 CPU 实测首次 13.7s、热态 p50 838ms，而 `laya_timeout_ms` 的
        演示口径是 3000ms——第一句话若直接撞冷启必然超时并整轮降级到 L2。
        预热请求故意用更长的超时（默认 30s）：它跑在启动期，不占长者等待预算。
        成败都由调用方记日志；这里不吞异常（与 `classify` 同样的失败四分类）。
        """
        await self._classify_once("你好", timeout_s=timeout_s)

    async def classify(self, text: str) -> FiveSignal:
        """分类一句话：成功返回 `FiveSignal`，失败抛 `LayaError` 四类子类之一。

        映射见模块 docstring；**不重试、不降级、不伪造信号**——把超时/宕机
        包装成「正常信号」会污染 0.85 阈值的语义，这里只负责如实失败。
        """
        started = perf_counter()
        try:
            return await self._classify_once(text)
        except LayaError as exc:
            # 日志只带异常类别与含状态码的 message，不带请求正文（AGENTS §4.3）。
            logger.warning(
                "laya classify failed",
                extra={"error_type": type(exc).__name__, "detail": str(exc)},
            )
            raise
        finally:
            observe_ms("laya_classify_ms", (perf_counter() - started) * 1000)

    async def _classify_once(self, text: str, *, timeout_s: float | None = None) -> FiveSignal:
        if self._client is None:
            raise LayaUnavailable("laya client already closed")
        if not self._settings.laya_enabled:
            # 计划 §1-P2：spike 不过 → 全部走 L2 的开关侧实现（不占一次网络尝试）。
            raise LayaUnavailable("laya disabled by settings (laya_enabled=false)")

        effective_timeout_s = (
            self._settings.laya_timeout_ms / 1000 if timeout_s is None else timeout_s
        )
        api_key = self._settings.laya_api_key.strip()
        headers = {"authorization": f"Bearer {api_key}"} if api_key else None
        # 请求 URL 一律从 settings 拼绝对地址：注入式 client（测试/装配方）可能
        # 不带 base_url，httpx 对「相对路径 + 无 base_url」会在内部 cookie 处理处
        # 抛 ValueError；自建 client 虽带 base_url，但绝对 URL 与之等价——
        # 两条路径共用同一 URL 构造，契约主机与路径不依赖 transit 配置。
        url = f"{self._settings.laya_base_url.rstrip('/')}{_CLASSIFY_PATH}"
        try:
            response = await self._client.post(
                url,
                json={"text": text},
                headers=headers,
                timeout=effective_timeout_s,
            )
        except httpx.TimeoutException as exc:
            raise LayaTimeout(f"laya timeout after {effective_timeout_s * 1000:.0f}ms") from exc
        except httpx.HTTPError as exc:
            # ConnectError / 传输中断 / 重定向次数爆掉等一切传输层失败归一类。
            raise LayaUnavailable(f"laya unreachable: {type(exc).__name__}") from exc

        if response.status_code != 200:
            raise LayaBadStatus(f"laya returned http {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise LayaBadPayload("laya payload is not valid json") from exc
        return _parse_signal(payload)
