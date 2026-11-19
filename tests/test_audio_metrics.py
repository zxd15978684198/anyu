"""S7-3/4 + S8 音频链路指标（`error_frame_total` / `ws_connections` / `tts_queue_depth`）。

覆盖口径（设计记录（可观测性接入））：

1. `error_frame_total{code}`——下行 error 帧每发一帧记一笔，code 是**闭集**键
   （`app/core/degrade_speech.py::SPOKEN_DEGRADE`），野 code 归 `internal`；
   计数走进程内镜像（`metrics_snapshot()["counters"]`），与其它 counter 用例同一种读法；
2. `ws_connections`——accept 后 +1、连接收尾 -1；握手前就失败的连接**不计不扣**
   （否则进程级的连接数会慢慢减成负数）；
3. `tts_queue_depth`——逐句合成队列的入队/消费净额，打断（生成器提前关闭）时必须回落到 0。

为什么 gauge 用 `render_metrics()` 读：`gauge()` 只写 OTel meter（`UpDownCounter`），
不写进程内 `_counters` 镜像，所以 `metrics_snapshot()` 里看不到它 —— 这是
`app/core/metrics.py` 的既定分工，不是本文件绕路。

本文件**不需要数据库**：WS 用例只驱动「握手 → 首帧非 auth → 4401 关闭」这条
在 `resolve_principal` 之前就结束的路径，靠一个进程内假 WebSocket 完成；
真实 DB + 真实 app 的端到端帧序语义由 `tests/test_audio_ws_auth.py` 继续覆盖。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any, cast

import pytest

from app.audio import signaling, tts_sentences
from app.audio.asr import ASREvent, ASREventKind, ASRUnavailable
from app.core import metrics as metrics_core
from app.core.metrics import render_metrics


@pytest.fixture(autouse=True)
def _clean_metrics() -> Iterator[None]:
    """指标是进程级全局（含 OTel meter），每用例前后各清一次，免得上一个用例漏进来。"""
    metrics_core.reset_metrics()
    try:
        yield
    finally:
        metrics_core.reset_metrics()


# ---------------------------------------------------------------------------
# 读法：counter 用快照，gauge 用 Prometheus 文本
# ---------------------------------------------------------------------------


def _counters() -> dict[str, int]:
    return cast("dict[str, int]", metrics_core.metrics_snapshot()["counters"])


def _gauge_value(name: str) -> float | None:
    """从 `render_metrics()` 文本里取某个 gauge 的当前值；没有样本时返回 None。

    只认 `<name>{` 或 `<name> ` 起始的行（避开 `# HELP` / `# TYPE`），多标签样本时
    取第一条 —— 这三类 gauge 目前都不带标签，不存在歧义。
    """
    for line in render_metrics().splitlines():
        if not line.startswith(name):
            continue
        rest = line[len(name) :]
        if not rest or rest[0] not in "{ ":
            continue
        return float(rest.rsplit(" ", 1)[1])
    return None


# ---------------------------------------------------------------------------
# 1. error_frame_total{code}
# ---------------------------------------------------------------------------


def _conn() -> signaling.ConnectionContext:
    """只带下行队列的连接上下文：`_send_error` 不碰 websocket / 身份。"""
    return signaling.ConnectionContext(trace_id="t" * 32, websocket=cast("Any", None))


async def test_send_error_counts_per_code() -> None:
    conn = _conn()
    await signaling._send_error(conn, "internal")
    await signaling._send_error(conn, "internal")
    await signaling._send_error(conn, "tool_budget")

    counters = _counters()
    assert counters["error_frame_total{code=internal}"] == 2
    assert counters["error_frame_total{code=tool_budget}"] == 1
    # 帧本身照旧入队（打点是副作用，不改协议行为）
    first = conn.downstream.get_nowait()
    assert first["type"] == "error"
    assert first["message"] == signaling._ERROR_TALKBACK["internal"]


async def test_send_error_folds_unknown_code_into_internal() -> None:
    """闭集之外的自定义 code 计到 `internal`：标签基数必须可控，话术也是这么取的。"""
    conn = _conn()
    await signaling._send_error(conn, "no_such_degrade_key")

    counters = _counters()
    assert counters["error_frame_total{code=internal}"] == 1
    assert "error_frame_total{code=no_such_degrade_key}" not in counters
    # 下行帧仍保留原始 code（客户端按 code 分支，不能被我改掉）
    assert conn.downstream.get_nowait()["code"] == "no_such_degrade_key"


async def test_asr_error_frame_counted_only_when_delivered() -> None:
    """ASR ERROR 走的不是 `_send_error`（那是连接级下行），投递成功才算发了一帧。"""
    event = ASREvent(kind=ASREventKind.ERROR, result=None, error=ASRUnavailable("boom"))

    # 设备不在线：deliver 返回 False，不计数
    assert signaling.deliver_asr_event("dev-absent", event) is False
    assert "error_frame_total{code=asr_unavailable}" not in _counters()

    conn = _conn()
    conn.device_id = "dev-1"
    conn.authed = True
    signaling._registry["dev-1"] = conn
    try:
        assert signaling.deliver_asr_event("dev-1", event) is True
        assert conn.downstream.get_nowait()["code"] == "asr_unavailable"
    finally:
        signaling._registry.pop("dev-1", None)

    assert _counters()["error_frame_total{code=asr_unavailable}"] == 1


# ---------------------------------------------------------------------------
# 2. ws_connections（UpDownCounter）
# ---------------------------------------------------------------------------


class _FakeWebSocket:
    """信号端点用得到的最小 WebSocket 面：accept / receive_text / send_text / close。

    `depth_when_replying` 在端点把 `auth_fail` 直发的**那一刻**读一次 gauge：
    这是「连接已 accept、finally 还没跑」之间唯一稳定的观测窗口。
    """

    def __init__(self, frames: list[str], *, fail_accept: bool = False) -> None:
        self._frames = list(frames)
        self._fail_accept = fail_accept
        self.sent: list[dict[str, Any]] = []
        self.closed_with: int | None = None
        self.depth_when_replying: float | None = None

    async def accept(self) -> None:
        if self._fail_accept:
            raise RuntimeError("handshake rejected by server")

    async def receive_text(self) -> str:
        return self._frames.pop(0)

    async def send_text(self, data: str) -> None:
        self.sent.append(json.loads(data))
        self.depth_when_replying = _gauge_value(signaling.WS_CONNECTIONS_GAUGE)

    async def close(self, code: int = 1000) -> None:
        self.closed_with = code


async def test_ws_connections_rises_on_accept_and_falls_on_close() -> None:
    # 首帧不是 auth → 走 `_reject("invalid")`：在 resolve_principal 之前结束，不需要 DB。
    ws = _FakeWebSocket([json.dumps({"type": "offer", "sdp": "v=0 fake"})])

    await signaling.signal_endpoint(cast("Any", ws), cast("Any", None))

    assert ws.closed_with == signaling.WS_CLOSE_AUTH_FAILED
    assert ws.sent and ws.sent[0]["code"] == "invalid"
    # accept 之后、收尾之前：连接计数是 1
    assert ws.depth_when_replying == 1.0
    # finally 之后回到 0（不是负数、也不是残留 1）
    assert _gauge_value(signaling.WS_CONNECTIONS_GAUGE) == 0.0


async def test_ws_connections_not_decremented_when_accept_failed() -> None:
    """握手就没成功的连接从未计入，收尾里不能扣 —— 否则计数会一路减成负数。"""
    ws = _FakeWebSocket([], fail_accept=True)

    with pytest.raises(RuntimeError, match="handshake rejected"):
        await signaling.signal_endpoint(cast("Any", ws), cast("Any", None))

    # 一次也没 add 过 → 没有该指标样本（出现过 -1 的话这里会是 -1.0）
    assert _gauge_value(signaling.WS_CONNECTIONS_GAUGE) is None


# ---------------------------------------------------------------------------
# 3. tts_queue_depth（逐句合成队列）
# ---------------------------------------------------------------------------


class _FakeTTS:
    """冻结契约的最小实现：整段入参 → 一段 PCM；记录被合成了哪几句。"""

    def __init__(self) -> None:
        self.texts: list[str] = []

    async def synthesize(self, text: str) -> bytes:
        self.texts.append(text)
        return b"\x00\x00" * 8


async def test_tts_queue_depth_rises_then_drains() -> None:
    provider = _FakeTTS()
    depths: list[float | None] = []
    chunks: list[bytes] = []

    async for chunk in tts_sentences.synthesize_by_sentence(
        "今天天气不错。我们出去走走吧。", provider=cast("Any", provider)
    ):
        chunks.append(chunk)
        depths.append(_gauge_value(tts_sentences.TTS_QUEUE_DEPTH_GAUGE))

    assert len(chunks) == 2
    assert provider.texts == ["今天天气不错。", "我们出去走走吧。"]
    # 第一句出队时两句都还在队列里；第二句出队时只剩 1
    assert depths == [2.0, 1.0]
    # 全部消费完 → 归零，不留残值
    assert _gauge_value(tts_sentences.TTS_QUEUE_DEPTH_GAUGE) == 0.0


async def test_tts_queue_depth_drains_when_stream_is_cancelled() -> None:
    """barge-in：生成器被提前关闭时，未消费的句子必须在 finally 里补减，否则深度永久虚高。"""
    provider = _FakeTTS()
    stream = tts_sentences.synthesize_by_sentence(
        "第一句话在这里。第二句话在这里。第三句话在这里。", provider=cast("Any", provider)
    )

    await stream.__anext__()
    assert _gauge_value(tts_sentences.TTS_QUEUE_DEPTH_GAUGE) == 3.0

    await stream.aclose()
    assert _gauge_value(tts_sentences.TTS_QUEUE_DEPTH_GAUGE) == 0.0


async def test_tts_queue_depth_untouched_for_unspeakable_text() -> None:
    """切不出句子 = 一句都没入队，不能留下 0 这个「有样本但没发生」的假象。"""
    provider = _FakeTTS()

    chunks = [
        chunk
        async for chunk in tts_sentences.synthesize_by_sentence(
            " 。。。 ", provider=cast("Any", provider)
        )
    ]

    assert chunks == []
    assert provider.texts == []
    assert _gauge_value(tts_sentences.TTS_QUEUE_DEPTH_GAUGE) is None
