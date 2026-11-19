"""TTS 适配层回归测试：Mock 音频格式、时长规则、工厂分派、ffmpeg 兜底与降级路径。

钉住四条验收口径（迭代 3 任务卡 A1 · 计划 §T1.3/§T1.4）：

1. `MockTTSProvider` 输出 **16kHz / 16bit / 单声道** s16le PCM —— 采样率与声道靠「字节数 ↔ 时长」
   互换校验（立体声会正好翻倍，8bit 会正好减半），位深靠整块解成 int16 后看峰值量级；
2. 时长 `max(0.4s, 0.06s × 字数)`，随字数**单调不减**，且 0.4s 地板之后严格线性增长；
3. 工厂 `build_tts()`：空值/未知值归一到 mock，`edge`/`cloud` 正确分派，settings 能透传；
4. ffmpeg 缺失（monkeypatch PATH）→ `TTSUnavailable("ffmpeg not found")`，**不联网、不依赖 ffmpeg**。

网络用例（真 edge-tts 合成）标 `@pytest.mark.slow`，`-m "not slow"` 下不跑；真实 ffmpeg 转码用例
只依赖本机二进制、不依赖网络，因此留在默认集合里，缺 ffmpeg 时由 fixture skip 而不是静默通过。
"""

from __future__ import annotations

import array
import asyncio
import itertools
import json
import logging
import shutil
import subprocess
import sys
import types
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import pytest

from app.audio import tts as tts_mod
from app.audio.tts import (
    PCM_CHANNELS,
    PCM_FRAME_BYTES,
    PCM_SAMPLE_RATE,
    PCM_SAMPLE_WIDTH,
    BridgeTTSProvider,
    ChainTTSProvider,
    CloudTTSProvider,
    EdgeTTSProvider,
    FallbackTTSProvider,
    MockTTSProvider,
    TTSUnavailable,
    build_tts,
    mock_duration_seconds,
    mp3_to_pcm,
)
from app.core import metrics as metrics_core
from app.core.config import Settings

requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None,
    reason="本机没有 ffmpeg：跳过真实转码用例（ffmpeg 缺失路径另有专门用例覆盖）",
)


@pytest.fixture(autouse=True)
def _disable_voice_pack(monkeypatch: pytest.MonkeyPatch) -> None:
    """本模块钉的是取音链内部结构；音频包命中/回落由 test_voice_pack.py 覆盖。

    仓库里 `static/voices/manifest.json` 是否存在不应改变这里的断言，否则 CI 与
    本地会因为「有没有跑过 bake 脚本」得到两套结果。
    """
    monkeypatch.setattr(
        tts_mod,
        "_wrap_voice_pack",
        lambda provider, config, *, requested_voice: provider,
    )


def _edge_from(provider: Any) -> EdgeTTSProvider:
    """从 `build_tts("edge")` 的**兜底链**里取回真正配好的 edge provider。

    `build_tts()` 有意不返回裸 `EdgeTTSProvider`：edge 之后再挂 espeak 兜底
    （`app/audio/tts.py:502-519`）—— 容器里 edge-tts 常被 TLS 拦，没有兜底就是
    「一点声音都没有」。所以配置 `edge` 得到的外层恒是：
      - `tts_bridge_url` 非空 → `ChainTTSProvider([bridge, edge, espeak])`
      - 出厂 `tts_bridge_url=""` → `FallbackTTSProvider(edge, espeak)`
    断言因此必须落到 `providers[1]` / `primary` 那个实例上，而不是外层。
    """
    if isinstance(provider, ChainTTSProvider):
        inner = provider.providers[1]
    elif isinstance(provider, FallbackTTSProvider):
        inner = provider.primary
    else:  # pragma: no cover - 兜底链结构变更时这里先炸，比断言静默失效好
        raise AssertionError(f"build_tts('edge') 返回了预期之外的 {type(provider).__name__}")
    assert isinstance(inner, EdgeTTSProvider), (
        f"兜底链里的主档不是 edge，而是 {type(inner).__name__}"
    )
    return inner


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _samples(pcm: bytes) -> list[int]:
    """把 s16le PCM 整块解成 int16 采样序列（解不开就说明位深/对齐不对）。"""
    decoded = array.array("h")
    decoded.frombytes(pcm)
    return decoded.tolist()


def _duration_s(pcm: bytes) -> float:
    """按 16kHz/16bit/单声道换算 PCM 时长。"""
    return len(pcm) / (PCM_SAMPLE_RATE * PCM_FRAME_BYTES)


def _zero_crossings(samples: list[int]) -> int:
    """统计过零次数，用于估算一段波形的平均频率（越高频过零越多）。"""
    return sum(1 for left, right in itertools.pairwise(samples) if (left < 0) != (right < 0))


def _install_fake_edge_tts(monkeypatch: pytest.MonkeyPatch, mp3: bytes) -> list[dict[str, str]]:
    """把 `edge_tts` 换成只吐内存 mp3 的假模块，返回记录到的调用参数（不联网）。"""
    calls: list[dict[str, str]] = []

    class FakeCommunicate:
        """假 edge-tts 的 Communicate：按 chunk 吐出预先准备好的真 mp3 字节。"""

        def __init__(self, text: str, voice: str, *, rate: str = "+0%") -> None:
            calls.append({"text": text, "voice": voice, "rate": rate})

        async def stream(self) -> AsyncIterator[dict[str, Any]]:
            # 真实 edge-tts 的流里混着 WordBoundary 元数据帧，provider 必须只挑 audio 帧。
            yield {"type": "WordBoundary", "offset": 0, "duration": 0}
            for offset in range(0, len(mp3), 4096):
                yield {"type": "audio", "data": mp3[offset : offset + 4096]}

    module = types.ModuleType("edge_tts")
    module.Communicate = FakeCommunicate  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "edge_tts", module)
    return calls


@pytest.fixture(scope="session")
def sample_mp3(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    """用本机 ffmpeg 现造一段 0.5s 440Hz mp3，充当「edge-tts 的输出」。"""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("本机没有 ffmpeg：无法生成 mp3 夹具")
    out = tmp_path_factory.mktemp("tts") / "sine.mp3"
    subprocess.run(  # noqa: S603 - argv 全为常量，输入只有 labfi 生成的测试音频
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.5",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "64k",
            str(out),
        ],
        capture_output=True,
        check=True,
    )
    return out.read_bytes()


# ---------------------------------------------------------------------------
# 1. Mock 输出格式：16kHz / 16bit / 单声道
# ---------------------------------------------------------------------------


def test_pcm_format_constants_are_16k_16bit_mono() -> None:
    assert PCM_SAMPLE_RATE == 16_000
    assert PCM_SAMPLE_WIDTH == 2
    assert PCM_CHANNELS == 1
    assert PCM_FRAME_BYTES == PCM_SAMPLE_WIDTH * PCM_CHANNELS


async def test_mock_pcm_is_16k_16bit_mono() -> None:
    text = "今天天气不错，我们出去走走吧"  # 15 字 → 0.9s，高于 0.4s 地板
    pcm = await MockTTSProvider().synthesize(text)

    assert pcm, "Mock TTS 不得返回空字节（A3 写轨拿到空 buffer 会写坏轨）"
    # 位深：s16le 必须能整块解成 int16，且峰值达到 16bit 量级（8bit 数据凑长度会在这里露馅）
    assert len(pcm) % PCM_FRAME_BYTES == 0
    samples = _samples(pcm)
    assert max(samples) > 0.25 * 32767
    assert min(samples) < -0.25 * 32767
    # 采样率 + 单声道：字节数 = 时长 × 16000 × 2；若是立体声会正好翻倍，8bit 会正好减半
    expected_s = mock_duration_seconds(text)
    assert len(pcm) == round(expected_s * PCM_SAMPLE_RATE) * PCM_FRAME_BYTES
    assert _duration_s(pcm) == pytest.approx(expected_s, abs=1 / PCM_SAMPLE_RATE)


async def test_mock_pcm_is_sweep_then_silence() -> None:
    pcm = await MockTTSProvider().synthesize("扫频加静音")
    samples = _samples(pcm)
    tone_part = samples[: int(len(samples) * tts_mod.MOCK_TONE_RATIO)]

    # 尾段必须是全零静音（后 30%）
    tail = samples[int(len(samples) * 0.8) :]
    assert tail
    assert not any(tail)

    # 前段是扫频：后 10% 的过零次数应明显多于前 10%（220Hz → 880Hz，理论 4 倍）
    head_window = tone_part[: len(tone_part) // 10]
    tail_window = tone_part[-len(tone_part) // 10 :]
    assert _zero_crossings(tail_window) > _zero_crossings(head_window)


async def test_mock_works_without_ffmpeg_or_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # PATH 只剩一个空目录 → ffmpeg / edge-tts 全不可见，Mock 仍须可用（断网联调前提）
    monkeypatch.setenv("PATH", str(tmp_path))

    pcm = await MockTTSProvider().synthesize("断网也能跑")

    assert pcm
    assert _duration_s(pcm) == pytest.approx(mock_duration_seconds("断网也能跑"), abs=0.01)


# ---------------------------------------------------------------------------
# 2. 时长规则：max(0.4s, 0.06s × 字数)，随字数单调
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected_s"),
    [
        ("", 0.4),  # 空串仍给地板时长，保证不返回空字节
        ("你好", 0.4),  # 2 字 = 0.12s，被 0.4s 地板抬起
        ("一二三四五六", 0.4),  # 6 字 = 0.36s，仍在地板下
        ("一二三四五六七", 0.42),  # 7 字 = 0.42s，刚过地板
        ("好" * 20, 1.2),
        ("  ", 0.4),  # 纯空白按 0 字算
    ],
)
def test_mock_duration_rule(text: str, expected_s: float) -> None:
    assert mock_duration_seconds(text) == pytest.approx(expected_s)


def test_mock_duration_monotonic_in_text_length() -> None:
    lengths = (0, 1, 5, 6, 7, 10, 20, 40)
    durations = [mock_duration_seconds("好" * n) for n in lengths]

    assert durations == sorted(durations), "时长必须随字数单调不减"
    assert durations[-1] > durations[0]
    # 过 0.4s 地板后按 0.06s/字 严格线性增长
    beyond_floor = [mock_duration_seconds("好" * n) for n in (7, 10, 20, 40)]
    assert all(later > earlier for earlier, later in itertools.pairwise(beyond_floor))


async def test_mock_pcm_length_tracks_text_length() -> None:
    provider = MockTTSProvider()
    byte_lengths = [len(await provider.synthesize("好" * n)) for n in (7, 10, 20)]

    assert byte_lengths == sorted(byte_lengths)
    assert len(set(byte_lengths)) == len(byte_lengths), (
        "字数不同必须出不同长度，否则 A5 无法按时长排帧"
    )


# ---------------------------------------------------------------------------
# 3. 观测：start() / record_tts() 真的被调用
# ---------------------------------------------------------------------------


async def test_mock_records_observation(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="app.audio.observe")
    text = "记录一条观测"

    pcm = await MockTTSProvider().synthesize(text)

    records = [record for record in caplog.records if record.getMessage() == "tts observation"]
    assert len(records) == 1
    record = records[0]
    assert record.tts_provider == "mock"
    assert record.tts_voice == tts_mod.MOCK_VOICE
    assert record.tts_text_len == len(text)
    assert record.tts_pcm_bytes == len(pcm)
    assert record.tts_total_ms >= 0.0
    assert record.tts_first_packet_ms == record.tts_total_ms  # 非流式：首包 = 整段


# ---------------------------------------------------------------------------
# 4. 工厂分派
# ---------------------------------------------------------------------------


def test_build_tts_empty_provider_falls_back_to_mock() -> None:
    # 空值是 迭代 3 的默认环境（.env 里 TTS_PROVIDER 未配置），必须落到 mock 而不是抛错
    assert isinstance(build_tts(Settings(tts_provider="")), MockTTSProvider)


@pytest.mark.parametrize(
    ("raw", "outer_types"),
    [
        ("", (MockTTSProvider,)),
        ("mock", (MockTTSProvider,)),
        ("MOCK", (MockTTSProvider,)),
        # `edge` 恒是兜底链：出厂 `tts_bridge_url=""` → Fallback，配了桥 → Chain。
        # 两档都算对（本机 .env 里有没有 TTS_BRIDGE_URL 都能跑），
        # 真正要钉的是「链里那个 edge 在、且配置落在它身上」——由 `_edge_from` 负责。
        ("  edge  ", (FallbackTTSProvider, ChainTTSProvider)),
        ("edge", (FallbackTTSProvider, ChainTTSProvider)),
        ("cloud", (CloudTTSProvider,)),
        ("Cloud", (CloudTTSProvider,)),
    ],
)
def test_build_tts_dispatch(raw: str, outer_types: tuple[type, ...]) -> None:
    provider = build_tts(Settings(tts_provider=raw, tts_bridge_url=""))

    assert isinstance(provider, outer_types)
    if raw.strip().lower() == "edge":
        _edge_from(provider)


def test_build_tts_unknown_provider_warns_and_falls_back(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="app.audio.tts")

    provider = build_tts(Settings(tts_provider="azure"))

    assert isinstance(provider, MockTTSProvider)
    assert "unknown TTS_PROVIDER" in caplog.text


def test_build_tts_passes_settings_to_edge() -> None:
    provider = build_tts(
        Settings(tts_provider="edge", tts_voice_id="zh-CN-YunxiNeural", tts_speed=1.2)
    )

    edge = _edge_from(provider)
    # voice / speed 必须落在链里的 edge 实例上，而不是被外层兜底壳吞掉
    assert edge.voice == "zh-CN-YunxiNeural"
    assert edge.speed == 1.2


def test_build_tts_edge_falls_back_to_default_voice() -> None:
    provider = build_tts(Settings(tts_provider="edge", tts_voice_id=""))

    edge = _edge_from(provider)
    assert edge.voice == tts_mod.EDGE_DEFAULT_VOICE


def test_build_tts_without_argument_reads_global_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tts_mod, "get_settings", lambda: Settings(tts_provider="edge"))

    _edge_from(build_tts())


def test_build_tts_persona_voice_overrides_settings() -> None:
    provider = build_tts(
        Settings(tts_provider="edge", tts_voice_id="zh-CN-YunxiNeural", tts_bridge_url=""),
        voice="zh-CN-XiaoyiNeural",
    )

    assert _edge_from(provider).voice == "zh-CN-XiaoyiNeural"


def test_build_tts_placeholder_voice_does_not_override() -> None:
    for placeholder in ("", "mock", "bridge", "MOCK"):
        provider = build_tts(
            Settings(tts_provider="edge", tts_voice_id="zh-CN-YunxiNeural", tts_bridge_url=""),
            voice=placeholder,
        )
        assert _edge_from(provider).voice == "zh-CN-YunxiNeural"


def test_build_tts_bridge_receives_persona_voice() -> None:
    provider = build_tts(
        Settings(
            tts_provider="edge",
            tts_voice_id="zh-CN-YunxiNeural",
            tts_bridge_url="http://bridge.local",
        ),
        voice="zh-CN-XiaoyiNeural",
    )

    assert isinstance(provider, ChainTTSProvider)
    bridge = provider.providers[0]
    assert bridge.voice == "zh-CN-XiaoyiNeural"
    assert _edge_from(provider).voice == "zh-CN-XiaoyiNeural"


def test_bridge_posts_voice_with_text(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class _Response:
        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"fake-mp3"

    def fake_urlopen(request: Any, timeout: float) -> _Response:
        captured["payload"] = json.loads(request.data)
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(tts_mod, "mp3_to_pcm", lambda mp3: b"\x00" * 3200)

    provider = BridgeTTSProvider(url="http://bridge.local", voice="zh-CN-XiaoyiNeural")
    asyncio.run(provider.synthesize("你好"))

    assert captured["payload"] == {"text": "你好", "voice": "zh-CN-XiaoyiNeural"}


@pytest.mark.parametrize(
    ("speed", "expected_rate"),
    [(1.0, "+0%"), (1.25, "+25%"), (0.8, "-20%"), (2.5, "+100%"), (0.1, "-50%")],
)
def test_speed_to_rate_clamps_and_formats(speed: float, expected_rate: str) -> None:
    # settings.tts_speed 是浮点倍率，edge-tts 要的是 "+20%" 这种百分号串；越界夹到 ±100%/50%
    assert tts_mod._speed_to_rate(speed) == expected_rate


# ---------------------------------------------------------------------------
# 5. ffmpeg 缺失 → TTSUnavailable（不联网、不需要真 ffmpeg）
# ---------------------------------------------------------------------------


async def test_edge_without_ffmpeg_raises_tts_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))

    with pytest.raises(TTSUnavailable, match="ffmpeg not found"):
        await EdgeTTSProvider().synthesize("没有 ffmpeg 就别联网了")


def test_mp3_to_pcm_without_ffmpeg_raises_tts_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))

    with pytest.raises(TTSUnavailable, match="ffmpeg not found"):
        mp3_to_pcm(b"\x00\x01")


def test_mp3_to_pcm_rejects_empty_payload() -> None:
    with pytest.raises(TTSUnavailable, match="empty mp3 payload"):
        mp3_to_pcm(b"")


@requires_ffmpeg
async def test_edge_without_package_raises_tts_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    # sys.modules 里放 None 会让 `import edge_tts` 抛 ImportError（无论本机装没装）
    monkeypatch.setitem(sys.modules, "edge_tts", None)  # type: ignore[arg-type]

    with pytest.raises(TTSUnavailable, match="edge-tts not installed"):
        await EdgeTTSProvider().synthesize("没装 edge-tts")


# ---------------------------------------------------------------------------
# 6. 真实 ffmpeg 转码链路（本地二进制，不走网络）
# ---------------------------------------------------------------------------


@requires_ffmpeg
def test_mp3_to_pcm_with_real_ffmpeg(sample_mp3: bytes) -> None:
    pcm = mp3_to_pcm(sample_mp3)

    assert pcm
    assert len(pcm) % PCM_FRAME_BYTES == 0
    # 0.5s 源音频经 mp3 编解码会多一点帧对齐填充，容差放到 ±0.15s
    assert _duration_s(pcm) == pytest.approx(0.5, abs=0.15)
    assert max(abs(sample) for sample in _samples(pcm)) > 1000  # 440Hz 正弦确实被解出来了


@requires_ffmpeg
async def test_edge_provider_end_to_end_with_fake_package(
    monkeypatch: pytest.MonkeyPatch,
    sample_mp3: bytes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # 假 edge-tts + 真 ffmpeg：钉住「惰性 import → 挑 audio 帧 → 子进程转码 → 观测」整条链
    calls = _install_fake_edge_tts(monkeypatch, sample_mp3)
    caplog.set_level(logging.INFO, logger="app.audio.observe")
    provider = EdgeTTSProvider(voice="zh-CN-XiaoxiaoNeural", speed=1.2)

    pcm = await provider.synthesize("你好，我是安语")

    assert pcm
    assert len(pcm) % PCM_FRAME_BYTES == 0
    assert _duration_s(pcm) == pytest.approx(0.5, abs=0.15)
    assert calls == [{"text": "你好，我是安语", "voice": "zh-CN-XiaoxiaoNeural", "rate": "+20%"}]
    observations = [record for record in caplog.records if record.getMessage() == "tts observation"]
    assert [record.tts_provider for record in observations] == ["edge"]


@requires_ffmpeg
async def test_edge_provider_maps_broken_audio_to_tts_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_edge_tts(monkeypatch, b"this is not an mp3")

    with pytest.raises(TTSUnavailable, match="ffmpeg convert failed"):
        await EdgeTTSProvider().synthesize("坏音频")


# ---------------------------------------------------------------------------
# 6b. P2 分块出流：增量解码与整段解码逐字节一致
# ---------------------------------------------------------------------------


@requires_ffmpeg
@pytest.mark.parametrize("cut", [1024, 7168, 333, 1])
async def test_stream_mp3_to_pcm_matches_whole_decode(sample_mp3: bytes, cut: int) -> None:
    """块边界落在任意位置都不能改变 PCM：增量解码必须等价于整段解码。"""

    async def chunks() -> AsyncIterator[bytes]:
        for offset in range(0, len(sample_mp3), cut):
            yield sample_mp3[offset : offset + cut]

    streamed = b"".join([piece async for piece in tts_mod.stream_mp3_to_pcm(chunks())])
    whole = mp3_to_pcm(sample_mp3)

    assert streamed == whole


@requires_ffmpeg
async def test_edge_stream_matches_whole_synthesis(
    monkeypatch: pytest.MonkeyPatch, sample_mp3: bytes
) -> None:
    _install_fake_edge_tts(monkeypatch, sample_mp3)
    provider = EdgeTTSProvider()

    streamed = b"".join([piece async for piece in provider.synthesize_stream("你好")])
    whole = await provider.synthesize("你好")

    assert streamed == whole


class _FakeBridgeResponse:
    def __init__(self, status_code: int, chunks: list[bytes]) -> None:
        self.status_code = status_code
        self._chunks = chunks

    async def __aenter__(self) -> _FakeBridgeResponse:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def aread(self) -> bytes:
        return b"bridge error"

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


class _FakeBridgeClient:
    status_code = 200
    chunks: ClassVar[list[bytes]] = []

    def __init__(self, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _FakeBridgeClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def stream(self, method: str, url: str, **kwargs: Any) -> _FakeBridgeResponse:
        return _FakeBridgeResponse(self.status_code, self.chunks)


@pytest.mark.parametrize("chunks", [[b"aa", b"bb"], [b"aa", b"", b"bb"]])
async def test_bridge_stream_uses_stream_endpoint(
    monkeypatch: pytest.MonkeyPatch, chunks: list[bytes]
) -> None:
    import httpx

    _FakeBridgeClient.status_code = 200
    _FakeBridgeClient.chunks = chunks
    monkeypatch.setattr(httpx, "AsyncClient", _FakeBridgeClient)
    provider = BridgeTTSProvider(url="http://bridge.local", voice="zh-CN-XiaoxiaoNeural")

    assert b"".join([piece async for piece in provider._iter_mp3("你好")]) == b"aabb"


async def test_bridge_stream_404_falls_back_to_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    _FakeBridgeClient.status_code = 404
    monkeypatch.setattr(httpx, "AsyncClient", _FakeBridgeClient)
    provider = BridgeTTSProvider(url="http://bridge.local", voice="zh-CN-XiaoxiaoNeural")

    async def fake_whole(text: str) -> bytes:
        return b"whole-mp3"

    monkeypatch.setattr(provider, "_post_whole", fake_whole)

    assert b"".join([piece async for piece in provider._iter_mp3("你好")]) == b"whole-mp3"


# ---------------------------------------------------------------------------
# 7. 云骨架 + 真实 edge-tts 网络用例（slow）
# ---------------------------------------------------------------------------


async def test_cloud_provider_is_skeleton() -> None:
    with pytest.raises(NotImplementedError):
        await CloudTTSProvider(voice="", timeout_ms=300).synthesize("云端待接入")


@pytest.mark.slow
async def test_edge_live_synthesis_over_network() -> None:
    """真 edge-tts 合成（要外网 + 装了 edge-tts）；`-m "not slow"` 下不跑。"""
    pytest.importorskip("edge_tts", reason="未安装 edge-tts：跳过真实合成用例")

    pcm = await EdgeTTSProvider().synthesize("你好")

    assert pcm
    assert _duration_s(pcm) > 0.3


# ---------------------------------------------------------------------------
# 8. 观测收口只写日志；轮级打点归属回复管线（迭代 5 v1.3 §4.2b）
# ---------------------------------------------------------------------------
# 打点口径变更：`tts_first_packet_ms` / `tts_total_ms` 作为跨天契约的 key（迭代 6/7
# 复用）不变，但**观测点已从 `_record`（句级：provider.synthesize 每句调用一次，
# 句级观测会让 count=句数、破坏 P50/P95 分位语义）搬到回复管线
# `app/audio/reply_stub.py`**（轮级：首包=第一句完成时刻、总时长=逐句循环自然走完；
# 轮级 key 的正向断言收口在 tests/test_audio_reply_stub.py）。本节反向钉住：
# `_record` 只写日志、不再产生 /metrics 观测——谁把打点加回这里，这里先红。


@pytest.fixture()
def metrics_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """打开指标开关并清空累计：每条用例的断言只属于它自己打的点。"""
    monkeypatch.setattr(metrics_core, "get_settings", lambda: SimpleNamespace(metrics_enabled=True))
    metrics_core.reset_metrics()
    try:
        yield
    finally:
        metrics_core.reset_metrics()


def _timings() -> dict[str, dict[str, float]]:
    return cast(dict[str, dict[str, float]], metrics_core.metrics_snapshot()["timings"])


def test_record_writes_log_but_no_tts_latency_metrics(
    metrics_on: None, caplog: pytest.LogCaptureFixture
) -> None:
    """`_record` 只写结构化日志：不再产生 `tts_first_packet_ms` / `tts_total_ms` 观测。

    逐句形态下 provider.synthesize 每句调用一次 `_record`，句级观测会让 count=句数、
    破坏 P50/P95 分位语义（迭代 5 v1.3 §4.2b）；轮级打点由 reply_stub 按轮观测。
    """
    caplog.set_level(logging.INFO, logger="app.audio.observe")
    tts_mod._record(provider="mock", voice="mock-sine", text="打点", pcm=b"\x00\x00", total_ms=42.5)

    records = [record for record in caplog.records if record.getMessage() == "tts observation"]
    assert len(records) == 1  # 日志仍在：观测收口（record_tts）不受打点搬迁影响
    assert records[0].tts_total_ms == pytest.approx(42.5)

    timings = _timings()
    assert "tts_first_packet_ms" not in timings
    assert "tts_total_ms" not in timings


async def test_mock_synthesize_writes_no_tts_latency_metrics(
    metrics_on: None, caplog: pytest.LogCaptureFixture
) -> None:
    """真实链路：synthesize → _record → 只落「tts observation」日志，不再 feed /metrics。"""
    caplog.set_level(logging.INFO, logger="app.audio.observe")
    await MockTTSProvider().synthesize("逐句后不打点")

    records = [record for record in caplog.records if record.getMessage() == "tts observation"]
    assert len(records) == 1

    timings = _timings()
    assert "tts_first_packet_ms" not in timings
    assert "tts_total_ms" not in timings
    assert metrics_core.metrics_snapshot()["counters"] == {}


def test_record_latency_metrics_disabled_writes_log_and_no_metrics(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """开关关掉时同样只写日志（log 与 metrics 解耦），且不产生任何观测。"""
    monkeypatch.setattr(
        metrics_core, "get_settings", lambda: SimpleNamespace(metrics_enabled=False)
    )
    metrics_core.reset_metrics()
    caplog.set_level(logging.INFO, logger="app.audio.observe")

    tts_mod._record(provider="mock", voice="mock-sine", text="x", pcm=b"\x00\x00", total_ms=42.5)

    assert any(record.getMessage() == "tts observation" for record in caplog.records)
    snapshot = metrics_core.metrics_snapshot()
    assert snapshot["enabled"] is False
    assert snapshot["timings"] == {}
    assert snapshot["counters"] == {}
