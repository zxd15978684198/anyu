"""W2 · System One（Laya 客户端 + 五信号 → 路由决策）回归集。

对应 `设计记录（三级路由）` §4-W2 验收与任务卡矩阵：

| 用例组 | 覆盖的验收口径 |
| --- | --- |
| `test_classify_*` | 五字段解析 + 契约路径（POST /v1/classify，入参 {"text": ...}） |
| `test_confidence_bounds_accepted` | confidence 0 / 0.85 / 1 三个边界**收下** |
| `test_bad_payload_*` | 字段缺失、类型不对、confidence ∉ [0,1]（含 NaN）→ `LayaBadPayload` |
| `test_timeout_* / test_connection_error_* / test_non_200_*` | 失败四分类的三个网络侧映射 |
| `test_disabled_* / test_classify_after_aclose_*` | `laya_enabled=False` / 复用已关 client → `LayaUnavailable` |
| `test_singleton_* / test_aclose_* / test_timeout_budget_*` | 单例 AsyncClient、注入不代关、超时预算取 `laya_timeout_ms/1000` |
| `test_high_confidence_* / test_boundary_* / test_low_confidence_*` | 决策矩阵：≥0.85 直路由 / 0.85 边界 / <0.85 首次澄清 / 二次升级 L2 |
| `test_sensitive_*` | 敏感优先于置信度与域外（AGENTS §6），`intent="alert"`，不写澄清标记 |
| `test_out_of_domain_*` | 域外 → L2 且落 `is_out_of_domain` 标记，不澄清 |
| `test_laya_error_*`（四类各一例） | Laya 四类异常 → `layer=L2, degraded=laya_unavailable` |
| `test_privacy_*` | privacy 五字段无分支透传（迭代 7 记忆召回消费） |

**全部走 `httpx.MockTransport`**，不打真网；`Settings` 一律 `_env_file=None`
构造（本任务红线：W2 不读 `.env`）。

**W1 并行说明**：`app/router/decision.py` 由同波 W1 产出。W2 开工时若三次
等待（60s×3）后仍未落盘，`decide_l1` 矩阵用例以 **显式理由 skip**（`-ra`
摘要可见；client 层用例不依赖 W1，照常执行）。W1 合入后本文件自动恢复全绿。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.system_one.client import (
    FiveSignal,
    LayaBadPayload,
    LayaBadStatus,
    LayaClient,
    LayaError,
    LayaTimeout,
    LayaUnavailable,
)
from app.system_one.questions import (
    DEGRADED_LAYA_UNAVAILABLE,
    DEGRADED_LOW_CONFIDENCE_CLARIFY,
    DEGRADED_LOW_CONFIDENCE_TWICE,
    decide_l1,
)

# --------------------------------------------------------------- W1 并行探针

_W1_UNAVAILABLE_REASON = (
    "W1（app/router/decision.py）尚未落盘：W2 的 questions 矩阵 blocked（并行波次）,"
    "W1 合入后复跑本文件即恢复全绿；client 层矩阵不受影响"
)

try:
    # 探针导入：只判定 W1 是否已落盘（RouteDecision 在 skipif 标记与用例里真实使用）。
    from app.router.decision import RouteDecision
except ImportError:  # W1 尚未产出（任务说明里的并行状态，不是测试代码的错）
    RouteDecision = None  # type: ignore[assignment,misc]

requires_w1 = pytest.mark.skipif(RouteDecision is None, reason=_W1_UNAVAILABLE_REASON)


def _degraded_of(decision: Any) -> Any:
    """读决策对象上的 degraded 标记；字段名以 W1 实际产出的 RouteDecision 为准。"""
    return getattr(decision, "degraded", None)


# --------------------------------------------------------------- 测试替身 / 夹具


def _settings(**overrides: Any) -> Settings:
    """构造一份不读 `.env` 的配置（`_env_file=None` 是红线要求，见模块 docstring）。"""
    base: dict[str, Any] = {
        "laya_base_url": "http://laya.test:7001",
        "laya_timeout_ms": 200,
        "laya_confidence_threshold": 0.85,
        "laya_enabled": True,
        "laya_api_key": "",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


class _RawJson(str):
    """标记型字符串：按原文作为响应体发出（用于 `json=` 无法编码的坏样本）。

    httpx 的 `json=` 编码禁 NaN（`ValueError: Out of range float values`），
    而真实 Laya 返回的本来就是字节流——`NaN` 字面量样本用原始文本最贴切，
    `Response.json()` 默认解析器会把 `NaN` 还原成 float NaN，由 app 侧拦下。
    """


#: confidence 写成非法 JSON `NaN` 字面量的样本（app 必须拦在 [0,1] 校验处）。
_RAW_NAN_BODY = _RawJson(
    '{"intent":"chat","sensitive":false,"confidence":NaN,"privacy":false,"is_out_of_domain":false}'
)


def _signal_payload(**overrides: Any) -> dict[str, Any]:
    """一份合法五字段样本；按用例需要覆写单个字段。"""
    payload: dict[str, Any] = {
        "intent": "chat",
        "sensitive": False,
        "confidence": 0.92,
        "privacy": False,
        "is_out_of_domain": False,
    }
    payload.update(overrides)
    return payload


def _respond_json(payload: Any) -> httpx.Response:
    """把样本投回 200 响应：`_RawJson` 走原始文本文替，其余按 JSON 编码。"""
    if isinstance(payload, _RawJson):
        return httpx.Response(200, text=str(payload))
    return httpx.Response(200, json=payload)


def _request_recording(handler: Any, requests: list[httpx.Request]) -> Any:
    """在 handler 外面包一层请求记录（断言契约路径 / 方法 / 入参用）。"""

    def wrapper(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    return wrapper


def _make_client(
    handler: Any,
    requests: list[httpx.Request] | None = None,
    **overrides: Any,
) -> LayaClient:
    """把 handler 包进 MockTransport 注入 LayaClient（生产路径不变，全程无真网）。"""
    transport = httpx.MockTransport(handler)
    if requests is not None:
        transport = httpx.MockTransport(_request_recording(handler, requests))
    injected = httpx.AsyncClient(transport=transport)
    return LayaClient(_settings(**overrides), client=injected)


# =============================================================== client 层


async def test_classify_returns_five_signals_and_hits_contract_path() -> None:
    """成功路径：五信号逐字段还原 + OpenAI 兼容契约(POST /v1/classify)。"""
    requests: list[httpx.Request] = []
    laya = _make_client(
        lambda request: httpx.Response(200, json=_signal_payload()),
        requests=requests,
    )
    signal = await laya.classify("今天天气怎么样")

    assert isinstance(signal, FiveSignal)
    assert signal == FiveSignal(
        intent="chat",
        sensitive=False,
        confidence=0.92,
        privacy=False,
        is_out_of_domain=False,
    )
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "http://laya.test:7001/v1/classify"
    assert json.loads(request.content) == {"text": "今天天气怎么样"}


@pytest.mark.parametrize("value", [0.0, 0.85, 1.0])
async def test_confidence_bounds_accepted(value: float) -> None:
    """confidence 三边界（0 / 0.85 / 1）都在契约内，正常出五信号。"""
    laya = _make_client(lambda request: httpx.Response(200, json=_signal_payload(confidence=value)))
    signal = await laya.classify("今天天气怎么样")
    assert signal.confidence == value


_BAD_PAYLOAD_CASES = [
    pytest.param([1, 2], id="not-an-object"),
    pytest.param("hello", id="not-a-json-object-string"),
    pytest.param({}, id="all-five-fields-missing"),
    pytest.param(
        {"sensitive": False, "confidence": 0.9, "privacy": False, "is_out_of_domain": False},
        id="missing-intent",
    ),
    pytest.param(
        {"intent": "chat", "sensitive": False, "privacy": False, "is_out_of_domain": False},
        id="missing-confidence",
    ),
    pytest.param(
        {"intent": "chat", "confidence": 0.9, "privacy": False, "is_out_of_domain": False},
        id="missing-sensitive",
    ),
    pytest.param(_signal_payload(intent=7), id="intent-not-string"),
    pytest.param(_signal_payload(confidence=1.5), id="confidence-above-one"),
    pytest.param(_signal_payload(confidence=-0.1), id="confidence-below-zero"),
    pytest.param(_signal_payload(confidence="0.9"), id="confidence-string"),
    pytest.param(_signal_payload(confidence=True), id="confidence-bool"),
    pytest.param(_RAW_NAN_BODY, id="confidence-nan"),
    pytest.param(_signal_payload(sensitive=1), id="sensitive-not-bool"),
    pytest.param(_signal_payload(privacy="yes"), id="privacy-not-bool"),
    pytest.param(_signal_payload(is_out_of_domain=None), id="ood-not-bool"),
]


@pytest.mark.parametrize("payload", _BAD_PAYLOAD_CASES)
async def test_bad_payload_rejects_contract_violations(payload: Any) -> None:
    """字段缺失 / 类型不对 / confidence 越界（含 NaN）→ LayaBadPayload，不伪造输出。"""
    laya = _make_client(lambda request: _respond_json(payload))
    with pytest.raises(LayaBadPayload):
        await laya.classify("今天天气怎么样")


async def test_200_with_html_body_is_bad_payload() -> None:
    """200 但响应体不是 JSON（网关 HTML）→ LayaBadPayload，而非解析炸栈。"""
    laya = _make_client(lambda request: httpx.Response(200, text="<html>gateway</html>"))
    with pytest.raises(LayaBadPayload, match=r"not valid json"):
        await laya.classify("今天天气怎么样")


async def test_timeout_maps_to_laya_timeout() -> None:
    """超时 → LayaTimeout（LayaError 子类，供 questions 统一捕获）。"""

    def raise_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    laya = _make_client(raise_timeout)
    with pytest.raises(LayaTimeout) as excinfo:
        await laya.classify("今天天气怎么样")
    assert isinstance(excinfo.value, LayaError)


async def test_connection_error_maps_to_laya_unavailable() -> None:
    """连不上（拒绝/DNS/断连）→ LayaUnavailable。"""

    def raise_connect_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    laya = _make_client(raise_connect_error)
    with pytest.raises(LayaUnavailable):
        await laya.classify("今天天气怎么样")


@pytest.mark.parametrize("status", [500, 503, 404])
async def test_non_200_maps_to_laya_bad_status(status: int) -> None:
    """HTTP 非 200 → LayaBadStatus，状态码进消息；响应体不外泄到日志路径。"""
    laya = _make_client(lambda request: httpx.Response(status, text="oops"))
    with pytest.raises(LayaBadStatus) as excinfo:
        await laya.classify("今天天气怎么样")
    assert str(status) in str(excinfo.value)


async def test_disabled_laya_raises_unavailable_without_any_request() -> None:
    """`laya_enabled=False`（计划 §1-P2 spike 不过口径）→ LayaUnavailable，且零网络请求。"""
    requests: list[httpx.Request] = []
    laya = _make_client(
        lambda request: httpx.Response(200, json=_signal_payload()),
        requests=requests,
        laya_enabled=False,
    )
    with pytest.raises(LayaUnavailable):
        await laya.classify("今天天气怎么样")
    assert requests == []


async def test_classify_after_aclose_raises_unavailable() -> None:
    """aclose 之后再 classify → LayaUnavailable（联网之前就如实失败）。

    生产形态是自建 AsyncClient（owner 口径）：aclose 置空连接池后，
    `_classify_once` 在 `_client is None` 处早退；注入式 client 的
    aclose 不代关（见 test_aclose_closes_owned_not_injected），故该路径
    必须用自建实例重现——`None` 早退先于任何网络请求，零真网风险。
    """
    laya = LayaClient(_settings())
    await laya.aclose()
    assert laya._client is None
    with pytest.raises(LayaUnavailable):
        await laya.classify("今天天气怎么样")


async def test_singleton_http_client_reused_across_calls() -> None:
    """每实例一个 AsyncClient 单例：两次 classify 复用同一连接池对象。"""
    laya = _make_client(lambda request: httpx.Response(200, json=_signal_payload()))
    before = laya._client
    await laya.classify("今天天气怎么样")
    await laya.classify("再来一段")
    assert laya._client is before


async def test_aclose_closes_owned_not_injected() -> None:
    """注入的 client 由注入方负责关闭：LayaClient.aclose 不动它，且二次 aclose 幂等。"""
    injected = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=_signal_payload()))
    )
    laya = LayaClient(_settings(), client=injected)
    await laya.aclose()
    await laya.aclose()  # 幂等
    assert injected.is_closed is False


async def test_timeout_budget_comes_from_laya_timeout_ms() -> None:
    """超时预算 = `laya_timeout_ms / 1000`（750ms → 0.75s），计划 W2 硬口径。"""
    laya = LayaClient(_settings(laya_timeout_ms=750))
    try:
        assert laya._client is not None
        assert laya._client.timeout.read == 0.75
        assert laya._client.timeout.connect == 0.75
    finally:
        await laya.aclose()


async def test_warm_up_uses_long_timeout_not_turn_budget() -> None:
    """启动预热走真实分类，但用 30s 超时：冷启 13.7s 不能撞 200/3000ms 的轮次预算。"""
    requests: list[httpx.Request] = []
    laya = _make_client(
        lambda request: httpx.Response(200, json=_signal_payload()),
        requests=requests,
        laya_timeout_ms=200,
    )
    await laya.warm_up()

    assert len(requests) == 1
    assert json.loads(requests[0].content) == {"text": "你好"}
    assert requests[0].extensions["timeout"]["read"] == 30.0


async def test_warm_up_respects_disabled_switch() -> None:
    """`laya_enabled=false` 时不发预热请求（不占一次注定失败的连接）。"""
    requests: list[httpx.Request] = []
    laya = _make_client(
        lambda request: httpx.Response(200, json=_signal_payload()),
        requests=requests,
        laya_enabled=False,
    )
    with pytest.raises(LayaUnavailable):
        await laya.warm_up()
    assert requests == []


# =============================================================== questions 层（决策矩阵）

_NOW = 1234.0

_IDENTITY = {"elder_id": "elder-1", "thread_id": "thread-1", "trace_id": "trace-w2"}


async def _decide(turn_state: dict[str, Any], laya: LayaClient) -> Any:
    return await decide_l1(laya, "今天天气怎么样", turn_state=turn_state, now=_NOW, **_IDENTITY)


@requires_w1
async def test_high_confidence_routes_l1_directly() -> None:
    """matrix ①：confidence ≥ 0.85 → 直接 layer=L1，五字段透传。"""
    laya = _make_client(lambda request: httpx.Response(200, json=_signal_payload(confidence=0.9)))
    decision = await _decide({}, laya)
    assert decision.layer == "L1"
    assert _degraded_of(decision) is None
    assert decision.intent == "chat"
    assert decision.sensitive is False
    assert decision.confidence == pytest.approx(0.9)
    assert decision.privacy is False
    assert decision.is_out_of_domain is False


@requires_w1
async def test_boundary_confidence_0_85_routes_l1() -> None:
    """matrix ②：0.85 恰在阈值上 → 仍走直路由（≥ 含边界，PRD §2.3）。"""
    laya = _make_client(lambda request: httpx.Response(200, json=_signal_payload(confidence=0.85)))
    decision = await _decide({}, laya)
    assert decision.layer == "L1"
    assert _degraded_of(decision) is None


@requires_w1
async def test_low_confidence_first_time_marks_clarify() -> None:
    """matrix ③：<0.85 第一次 → L1 + low_confidence_clarify，并落 clarified=True。"""
    turn_state: dict[str, Any] = {}
    laya = _make_client(lambda request: httpx.Response(200, json=_signal_payload(confidence=0.4)))
    decision = await _decide(turn_state, laya)
    assert decision.layer == "L1"
    assert _degraded_of(decision) == DEGRADED_LOW_CONFIDENCE_CLARIFY
    assert turn_state.get("clarified") is True
    assert turn_state.get("clarified_at") == _NOW


@requires_w1
async def test_low_confidence_second_time_upgrades_to_l2() -> None:
    """matrix ④：同 turn 已澄清过、二次仍模糊 → L2 + low_confidence_twice。"""
    turn_state: dict[str, Any] = {}
    laya = _make_client(lambda request: httpx.Response(200, json=_signal_payload(confidence=0.4)))
    first = await _decide(turn_state, laya)
    second = await _decide(turn_state, laya)
    assert first.layer == "L1"  # 第一路：澄清
    assert second.layer == "L2"
    assert _degraded_of(second) == DEGRADED_LOW_CONFIDENCE_TWICE


@requires_w1
async def test_sensitive_wins_over_low_confidence_without_clarify() -> None:
    """敏感优先：低置信 + sensitive → 仍 L1 alert，且不写澄清标记（安全先于澄清）。"""
    turn_state: dict[str, Any] = {}
    laya = _make_client(
        lambda request: httpx.Response(200, json=_signal_payload(sensitive=True, confidence=0.4))
    )
    decision = await _decide(turn_state, laya)
    assert decision.layer == "L1"
    assert decision.intent == "alert"
    assert decision.sensitive is True
    assert turn_state == {}  # 敏感路径不应有澄清副作用


@requires_w1
async def test_sensitive_wins_over_out_of_domain() -> None:
    """敏感 + 域外同时命中 → 敏感赢（人工分支高于域外分流）。"""
    laya = _make_client(
        lambda request: httpx.Response(
            200, json=_signal_payload(sensitive=True, is_out_of_domain=True, confidence=0.95)
        )
    )
    decision = await _decide({}, laya)
    assert decision.layer == "L1"
    assert decision.intent == "alert"


@requires_w1
async def test_out_of_domain_routes_l2_with_flag() -> None:
    """matrix ②'：域外 → L2 + is_out_of_domain 透传，且不澄清（高置信也拦下）。"""
    turn_state: dict[str, Any] = {}
    laya = _make_client(
        lambda request: httpx.Response(
            200, json=_signal_payload(is_out_of_domain=True, confidence=0.95)
        )
    )
    decision = await _decide(turn_state, laya)
    assert decision.layer == "L2"
    assert decision.is_out_of_domain is True
    assert turn_state == {}  # 域外不澄清


@requires_w1
async def test_timeout_failure_degrades_to_l2() -> None:
    """LayaError ①/4：超时 → L2 + laya_unavailable（断 Laya 不 500，链路仍能出话）。"""

    def raise_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    turn_state: dict[str, Any] = {}
    laya = _make_client(raise_timeout)
    decision = await _decide(turn_state, laya)
    assert decision.layer == "L2"
    assert _degraded_of(decision) == DEGRADED_LAYA_UNAVAILABLE
    assert turn_state == {}


@requires_w1
async def test_unavailability_failure_degrades_to_l2() -> None:
    """LayaError ②/4：连接失败 → L2 + laya_unavailable。"""

    def raise_connect_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    laya = _make_client(raise_connect_error)
    decision = await _decide({}, laya)
    assert decision.layer == "L2"
    assert _degraded_of(decision) == DEGRADED_LAYA_UNAVAILABLE


@requires_w1
async def test_bad_status_failure_degrades_to_l2() -> None:
    """LayaError ③/4：HTTP 503 → L2 + laya_unavailable。"""
    laya = _make_client(lambda request: httpx.Response(503, text="oops"))
    decision = await _decide({}, laya)
    assert decision.layer == "L2"
    assert _degraded_of(decision) == DEGRADED_LAYA_UNAVAILABLE


@requires_w1
async def test_bad_payload_failure_degrades_to_l2() -> None:
    """LayaError ④/4：200 但字段缺失 → L2 + laya_unavailable（残缺信号不进 L1 路由）。"""
    laya = _make_client(lambda request: httpx.Response(200, json={}))
    decision = await _decide({}, laya)
    assert decision.layer == "L2"
    assert _degraded_of(decision) == DEGRADED_LAYA_UNAVAILABLE


@requires_w1
async def test_privacy_passes_through_without_branching() -> None:
    """privacy 五信号之一：只透传（迭代 7 消费），本日路由不看它。"""
    laya = _make_client(
        lambda request: httpx.Response(200, json=_signal_payload(privacy=True, confidence=0.9))
    )
    decision = await _decide({}, laya)
    assert decision.layer == "L1"
    assert decision.privacy is True
