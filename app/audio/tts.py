"""TTS 适配层：复用 C1 契约的 `TTSProvider`，提供 Mock / edge-tts+ffmpeg / 云骨架。

迭代 3 任务卡 A1（DAG 节点 N4+N5）· 计划 `设计记录（ASR与TTS适配层）` §T1.3/§T1.4 ·
`设计记录（DAG）` §3。

三条设计约束（改动前先问产品负责人）：

1. **不复制契约**：`TTSProvider` 的唯一定义在 `app/audio/asr.py` 的 C1 契约区（已冻结，消费方
   A2/A3/A5），本文件只 import 并继承。再造一份协议就会出现两个会漂移的「事实源」。
2. **零新增 Python 依赖**：edge-tts 出的 mp3 用 **ffmpeg 子进程** 解成 16k/16bit/单声道 PCM。
   不用 pydub —— 它内部同样调 ffmpeg，只会多一层依赖面而没有收益。edge-tts 本身也不进
   `pyproject.toml`，只在 `synthesize()` 里惰性 import：没装时按「环境不可用」降级
   （抛 `TTSUnavailable`），而不是 import 期炸掉整个 app。
3. **C1 口径**：`synthesize()` 返回**整段** 16kHz/16bit/单声道 s16le PCM（迭代 3 非流式）。
   首包 ≈ 整段可用时刻；edge-tts 实测首包 >1s、远超 300ms 预算，已在计划 §8 P-A1-4 登记为
   「断网兜底而非主实现」，不计入 迭代 5 基线。

降级链（`TTSUnavailable` 的两条出路）：调用方捕获后切「大字幕 + 预置音频」，万不得已才用浏览器
`speechSynthesis`（技术设计文档 §6 语音合成行）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import shutil
import struct
import subprocess
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Final

from app.audio._observe import record_tts, start
from app.audio.asr import TTSProvider
from app.audio.voice_pack import load_manifest, read_wav_pcm, resolve_entry_file, text_sha256
from app.core.config import Settings, get_settings

logger = logging.getLogger("app.audio.tts")

# ---------------------------------------------------------------------------
# PCM 口径（C1 契约钉死，不要按调用方心情改）
# ---------------------------------------------------------------------------
PCM_SAMPLE_RATE: Final[int] = 16_000
PCM_SAMPLE_WIDTH: Final[int] = 2  # 16bit = 2 byte
PCM_CHANNELS: Final[int] = 1
PCM_FRAME_BYTES: Final[int] = PCM_SAMPLE_WIDTH * PCM_CHANNELS  # 单采样点 2 byte

# ---------------------------------------------------------------------------
# Mock 参数（计划 T1.3：正弦扫频 + 静音，时长 max(0.4s, 0.06s × 字数)）
# ---------------------------------------------------------------------------
MOCK_MIN_DURATION_S: Final[float] = 0.4
MOCK_SECONDS_PER_CHAR: Final[float] = 0.06
MOCK_VOICE: Final[str] = "mock-sine"
MOCK_TONE_RATIO: Final[float] = 0.7  # 前 70% 扫频、后 30% 静音，两者都能被下游用例观测到
MOCK_SWEEP_START_HZ: Final[float] = 220.0
MOCK_SWEEP_END_HZ: Final[float] = 880.0
MOCK_PEAK_RATIO: Final[float] = 0.30  # 峰值只占满量程 30%，避免削波/刺耳
MOCK_FADE_S: Final[float] = 0.005  # 5ms 线性淡入淡出，消除起播与结尾爆音

# ---------------------------------------------------------------------------
# edge-tts / ffmpeg 参数
# ---------------------------------------------------------------------------
EDGE_DEFAULT_VOICE: Final[str] = "zh-CN-XiaoxiaoNeural"
# `edge_tts.list_voices()` 实测的 zh-CN 全集（2026-11-16）。人设表里曾写入
# Xiaomeng/Xiaohan/Xiaoyan/Xiaorui 这些不存在的 ID，合成阶段才失败、表现为整段静音；
# 这里在工厂层挡一次，坏值只降级到默认音色，不让整条对话断在最后一米。
EDGE_SUPPORTED_VOICES: Final[frozenset[str]] = frozenset(
    {
        "zh-CN-XiaoxiaoNeural",
        "zh-CN-XiaoyiNeural",
        "zh-CN-YunjianNeural",
        "zh-CN-YunxiNeural",
        "zh-CN-YunxiaNeural",
        "zh-CN-YunyangNeural",
        "zh-CN-liaoning-XiaobeiNeural",
        "zh-CN-shaanxi-XiaoniNeural",
    }
)
EDGE_MIN_SPEED: Final[float] = 0.5
EDGE_MAX_SPEED: Final[float] = 2.0
# 离线兜底 TTS（见 `EspeakTTSProvider`）：镜像里装了 espeak-ng，普通话音色 `cmn`，
# 基准语速 150 词/分（espeak 的 `-s` 单位），再按 `TTS_SPEED` 缩放。
ESPEAK_BIN: Final[str] = "espeak-ng"
ESPEAK_VOICE: Final[str] = "cmn"
ESPEAK_BASE_WPM: Final[int] = 150
FFMPEG_BIN: Final[str] = "ffmpeg"
# 计划 §T1.4 钉死的转码参数：stdin 吃 mp3，stdout 吐 s16le / 16kHz / 单声道。
FFMPEG_CONVERT_ARGS: Final[tuple[str, ...]] = (
    "-i",
    "pipe:0",
    "-f",
    "s16le",
    "-ar",
    str(PCM_SAMPLE_RATE),
    "-ac",
    str(PCM_CHANNELS),
    "pipe:1",
)


class TTSUnavailable(Exception):
    """TTS 不可用（ffmpeg 缺失 / edge-tts 未装 / 合成或转码失败）。

    与 ASR 侧的二分类同理：本异常只表示「这一条链路走不通、需要降级」，不带重试语义；
    非流式整段合成没有「部分成功」这种中间态，所以不设 TTSProtocolError。
    """


# ---------------------------------------------------------------------------
# 纯函数：时长规则 / Mock 波形 / mp3→PCM
# ---------------------------------------------------------------------------


def mock_duration_seconds(text: str) -> float:
    """Mock 时长规则：``max(0.4s, 0.06s × 字数)``（计划 T1.3 钉死，不要自己改系数）。

    字数按 `strip()` 后的字符数算：只剔除**首尾**空白；句子中间的标点（`，` `。` 等）
    **仍然计入**，这是刻意的（标点本来就占语气停顿）。别按「标点不计」去推算时长。
    空串仍然给 0.4s，保证 `synthesize()` **永不返回空字节** —— A3 的下行写轨拿到空 buffer
    会直接把轨写坏。
    """
    return max(MOCK_MIN_DURATION_S, MOCK_SECONDS_PER_CHAR * len(text.strip()))


def _render_mock_pcm(duration_s: float) -> bytes:
    """生成「正弦扫频 + 静音尾」的 s16le PCM（纯 CPU、同步；异步侧用 to_thread 包）。

    扫频用**相位累加**而不是 ``sin(2πft)``：变频时后者会在频率切换点产生相位跳变，
    听起来是「咔哒」声；相位累加保证波形连续。
    """
    total_samples = max(1, round(duration_s * PCM_SAMPLE_RATE))
    tone_samples = min(total_samples, round(total_samples * MOCK_TONE_RATIO))
    fade_samples = max(1, round(MOCK_FADE_S * PCM_SAMPLE_RATE))
    peak = MOCK_PEAK_RATIO * 32767.0

    phase = 0.0
    values: list[int] = []
    for index in range(tone_samples):
        progress = index / max(1, tone_samples - 1)
        freq = MOCK_SWEEP_START_HZ + (MOCK_SWEEP_END_HZ - MOCK_SWEEP_START_HZ) * progress
        phase += math.tau * freq / PCM_SAMPLE_RATE
        # 两端各 5ms 线性淡入淡出：min(距头, 距尾) 落在淡入区就按比例衰减。
        edge_distance = min(index, tone_samples - 1 - index) + 1
        gain = min(1.0, edge_distance / fade_samples)
        values.append(round(peak * gain * math.sin(phase)))

    tone = struct.pack(f"<{len(values)}h", *values)
    return tone + bytes(PCM_FRAME_BYTES * (total_samples - tone_samples))


def _require_ffmpeg(ffmpeg_bin: str) -> None:
    """ffmpeg 不在 PATH 上就早失败：降级链路据此切「大字幕 + 预置音频」。"""
    if shutil.which(ffmpeg_bin) is None:
        raise TTSUnavailable("ffmpeg not found")


def _stderr_tail(exc: subprocess.CalledProcessError) -> str:
    """只取 stderr 末尾 200 字符：ffmpeg 的 stderr 很长，且开头往往只是 banner。"""
    raw = exc.stderr if isinstance(exc.stderr, bytes) else b""
    return raw[-200:].decode("utf-8", "replace").strip()


def mp3_to_pcm(mp3: bytes, *, ffmpeg_bin: str = FFMPEG_BIN) -> bytes:
    """把 mp3 字节解成 16k/16bit/单声道 s16le PCM（**阻塞**，异步侧请用 `asyncio.to_thread`）。

    ffmpeg 参数由计划 §T1.4 钉死；用子进程而不是 pydub 的理由见模块 docstring 第 2 条。
    """
    if not mp3:
        raise TTSUnavailable("empty mp3 payload")
    _require_ffmpeg(ffmpeg_bin)
    completed = subprocess.run(  # noqa: S603 - argv 全是常量、shell=False、音频只走 stdin
        [ffmpeg_bin, *FFMPEG_CONVERT_ARGS],
        input=mp3,
        capture_output=True,
        check=True,
    )
    pcm = completed.stdout
    if not pcm:
        raise TTSUnavailable("ffmpeg produced no pcm")
    if len(pcm) % PCM_FRAME_BYTES:
        # 半个采样点说明转码参数或 ffmpeg 版本不对，别把错位字节喂给 A3 的写轨。
        raise TTSUnavailable("ffmpeg returned unaligned pcm")
    return pcm


async def stream_mp3_to_pcm(
    mp3_chunks: AsyncIterator[bytes],
    *,
    ffmpeg_bin: str = FFMPEG_BIN,
    read_size: int = 4096,
) -> AsyncIterator[bytes]:
    """mp3 块流 → PCM 块流：ffmpeg 管道边写边读，不攒整句。

    块边界由 ffmpeg 输出决定，**不保证**与采样点/20ms 帧对齐；这里只保证每次 yield
    的字节数是偶数（16bit 单声道），奇数字节留到下一块。**不保证**首块够一帧——
    下游 `enqueue_tts_chunk_placed_for_device` 会用 carry 接住残样。

    失败语义：源流（edge/bridge）或 ffmpeg 任一失败都抛 `TTSUnavailable`；已经 yield
    出去的音频不回收（调用方已经写进播放轨，不能假装没发生过）。
    """
    _require_ffmpeg(ffmpeg_bin)
    proc = await asyncio.create_subprocess_exec(
        ffmpeg_bin,
        *FFMPEG_CONVERT_ARGS,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    stdin, stdout, stderr = proc.stdin, proc.stdout, proc.stderr
    stderr_tail = bytearray()

    async def _drain_stderr() -> None:
        while True:
            data = await stderr.read(1024)
            if not data:
                return
            stderr_tail.extend(data)
            if len(stderr_tail) > 2000:
                del stderr_tail[:-1000]

    async def _feed() -> None:
        try:
            async for chunk in mp3_chunks:
                if not chunk:
                    continue
                stdin.write(chunk)
                await stdin.drain()
        finally:
            stdin.close()

    stderr_task = asyncio.create_task(_drain_stderr())
    feed_task = asyncio.create_task(_feed())
    pending = b""
    produced = 0
    try:
        while True:
            data = await stdout.read(read_size)
            if not data:
                break
            pending += data
            usable = len(pending) - len(pending) % PCM_FRAME_BYTES
            if not usable:
                continue
            out, pending = pending[:usable], pending[usable:]
            produced += len(out)
            yield out

        feed_error: Exception | None = None
        with contextlib.suppress(asyncio.CancelledError):
            try:
                await feed_task
            except Exception as exc:  # 源流异常：统一映射成可降级
                feed_error = exc
        await proc.wait()
        await stderr_task
        if feed_error is not None:
            if isinstance(feed_error, TTSUnavailable):
                raise feed_error
            raise TTSUnavailable(f"tts stream failed: {feed_error}") from feed_error
        if proc.returncode:
            tail = stderr_tail[-200:].decode("utf-8", "replace").strip()
            raise TTSUnavailable(f"ffmpeg stream failed: {tail}")
        if not produced:
            raise TTSUnavailable("ffmpeg produced no pcm")
    finally:
        for task in (feed_task, stderr_task):
            if not task.done():
                task.cancel()
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()


def _chunk_bytes(pcm: bytes, size: int = 4096) -> list[bytes]:
    """把整段 PCM 切成偶数长度的块（Mock / 音频包用）。"""
    usable = len(pcm) - len(pcm) % PCM_FRAME_BYTES
    return [pcm[offset : offset + size] for offset in range(0, usable, size)]


def _record_stream(
    *,
    provider: str,
    voice: str,
    text: str,
    pcm_bytes: int,
    first_packet_ms: float | None,
    total_ms: float,
) -> None:
    """流式 TTS 观测：首包是**第一块 PCM 到手**，总耗时是流结束。"""
    record_tts(
        provider=provider,
        voice=voice,
        text_len=len(text),
        pcm_bytes=pcm_bytes,
        first_packet_ms=first_packet_ms,
        total_ms=total_ms,
    )


def _speed_to_rate(speed: float) -> str:
    """settings.tts_speed（1.0 = 原速）→ edge-tts 的 ``+20%`` / ``-15%`` 语速串。

    夹到 [0.5, 2.0]：edge-tts 只接受 ±50%/±100% 量级，配置写错时宁可按边界读，也别整段失败。
    """
    clamped = min(max(speed, EDGE_MIN_SPEED), EDGE_MAX_SPEED)
    return f"{round((clamped - 1.0) * 100):+d}%"


def _record(*, provider: str, voice: str, text: str, pcm: bytes, total_ms: float) -> None:
    """TTS 观测单点收口。非流式下首包 = 整段可用时刻，两个耗时字段同值。"""
    record_tts(
        provider=provider,
        voice=voice,
        text_len=len(text),
        pcm_bytes=len(pcm),
        first_packet_ms=total_ms,
        total_ms=total_ms,
    )
    # 轮级打点（tts_first_packet_ms / tts_total_ms）归属回复管线（逐句按轮取首包/总
    # 时长），单次调用观测 count=句数会破坏分位数语义；REST 直调链无轮级打点，
    # 已知取舍（迭代 5 v1.3 §4.2b；正向断言收口在 tests/test_audio_reply_stub.py）。


# ---------------------------------------------------------------------------
# 实现：Mock
# ---------------------------------------------------------------------------


class MockTTSProvider(TTSProvider):
    """断网可用的 Mock TTS：正弦扫频 + 静音尾，供 A3 写轨 / A5 组装在无网络无 key 时联调。

    不依赖网络、不依赖 ffmpeg、不依赖 edge-tts —— 只做纯 CPU 波形合成，因此可以在
    断网机器与 CI 上稳定复现（A3/A5 的用例全部默认走它）。
    """

    name = "mock"

    def __init__(self, *, voice: str = MOCK_VOICE) -> None:
        self.voice = voice

    async def synthesize(self, text: str) -> bytes:
        stopwatch = start()
        duration_s = mock_duration_seconds(text)
        # 6s 音频约 96000 个采样点，纯 Python 循环要十几毫秒 —— 丢线程里算，别卡住事件循环。
        pcm = await asyncio.to_thread(_render_mock_pcm, duration_s)
        total_ms = stopwatch.elapsed_ms()
        _record(provider=self.name, voice=self.voice, text=text, pcm=pcm, total_ms=total_ms)
        return pcm

    async def synthesize_stream(self, text: str) -> AsyncIterator[bytes]:
        stopwatch = start()
        pcm = await asyncio.to_thread(_render_mock_pcm, mock_duration_seconds(text))
        first_ms: float | None = None
        for chunk in _chunk_bytes(pcm):
            if first_ms is None:
                first_ms = stopwatch.elapsed_ms()
            yield chunk
        _record_stream(
            provider=self.name,
            voice=self.voice,
            text=text,
            pcm_bytes=len(pcm),
            first_packet_ms=first_ms,
            total_ms=stopwatch.elapsed_ms(),
        )


# ---------------------------------------------------------------------------
# 实现：edge-tts + ffmpeg 子进程（断网/无云 key 的兜底）
# ---------------------------------------------------------------------------


class EdgeTTSProvider(TTSProvider):
    """edge-tts 合成 mp3 → ffmpeg 子进程转 16k/16bit/单声道 PCM。

    定位是**兜底**而非主实现：整段合成、非流式，首包远超 300ms 预算（计划 §8 P-A1-4）。
    """

    name = "edge"

    def __init__(
        self,
        *,
        voice: str = EDGE_DEFAULT_VOICE,
        speed: float = 1.0,
        ffmpeg_bin: str = FFMPEG_BIN,
    ) -> None:
        self.voice = voice
        self.speed = speed
        self.ffmpeg_bin = ffmpeg_bin

    async def synthesize(self, text: str) -> bytes:
        stopwatch = start()
        # 先查 ffmpeg 再联网：ffmpeg 缺失时合成出来的 mp3 也用不上，白花一次网络往返。
        _require_ffmpeg(self.ffmpeg_bin)
        mp3 = await self._synthesize_mp3(text)
        try:
            pcm = await asyncio.to_thread(mp3_to_pcm, mp3, ffmpeg_bin=self.ffmpeg_bin)
        except subprocess.CalledProcessError as exc:
            raise TTSUnavailable(f"ffmpeg convert failed: {_stderr_tail(exc)}") from exc
        total_ms = stopwatch.elapsed_ms()
        _record(provider=self.name, voice=self.voice, text=text, pcm=pcm, total_ms=total_ms)
        return pcm

    async def _iter_mp3(self, text: str) -> AsyncIterator[bytes]:
        """文本 → mp3 字节块；只做协议过滤，不攒整段（P2 分块出流的源头）。"""
        try:
            import edge_tts  # edge-tts 在 pyproject/uv.lock 里是硬依赖，且自带 py.typed
        except ImportError as exc:
            raise TTSUnavailable("edge-tts not installed") from exc

        produced = False
        try:
            communicate = edge_tts.Communicate(text, self.voice, rate=_speed_to_rate(self.speed))
            async for chunk in communicate.stream():
                # edge-tts 的流里混着 WordBoundary 等元数据帧，只取音频帧。
                if chunk["type"] == "audio" and chunk["data"]:
                    produced = True
                    yield chunk["data"]
        except Exception as exc:  # 第三方异常类型不稳定，统一映射成「可降级」
            raise TTSUnavailable(f"edge-tts failed: {exc}") from exc
        if not produced:
            raise TTSUnavailable("edge-tts returned no audio")

    async def _synthesize_mp3(self, text: str) -> bytes:
        """只负责「文本 → mp3 字节」，转码在 `mp3_to_pcm()`。惰性 import 的理由见模块 docstring。"""
        buffer = bytearray()
        async for chunk in self._iter_mp3(text):
            buffer.extend(chunk)
        return bytes(buffer)

    async def synthesize_stream(self, text: str) -> AsyncIterator[bytes]:
        stopwatch = start()
        first_ms: float | None = None
        produced = 0
        try:
            async for pcm in stream_mp3_to_pcm(self._iter_mp3(text), ffmpeg_bin=self.ffmpeg_bin):
                if first_ms is None:
                    first_ms = stopwatch.elapsed_ms()
                produced += len(pcm)
                yield pcm
        finally:
            _record_stream(
                provider=self.name,
                voice=self.voice,
                text=text,
                pcm_bytes=produced,
                first_packet_ms=first_ms,
                total_ms=stopwatch.elapsed_ms(),
            )


# ---------------------------------------------------------------------------
# 实现：离线兜底（espeak-ng，容器内、不依赖外网）
# ---------------------------------------------------------------------------


class EspeakTTSProvider(TTSProvider):
    """离线兜底 TTS：espeak-ng 的普通话音色（`cmn`）。

    ## 为什么要有这一层

    2026-11-11 实测：宿主机直连 `speech.platform.bing.com:443` 正常（edge-tts 出
    12096 字节 mp3），但**容器内 TLS 握手被掐断**（`SSLEOFError`，握手读 0 字节），
    edge-tts 100% 抛 `TTSUnavailable`。后果不是「少点声音」而是整条体验链断掉：
    开场白没有音频 → 没有口型能量 → 字幕只能在 8s 兜底后整段砸出来，长者看到的就是
    「口型不动、打字机没做」。

    espeak-ng 装在镜像里（`Dockerfile` 的 `espeak-ng`），不联网、不花 key，
    音色是机器人声——但**有声音就能带口型**，比「有字没声」强得多。
    真机需要高质量音色时把 `TTS_PROVIDER=edge` 且容器能出网即可，这层不会抢戏。
    """

    name = "espeak"

    def __init__(self, *, voice: str = ESPEAK_VOICE, speed: float = 1.0) -> None:
        self.voice = voice
        self.speed = speed

    async def synthesize(self, text: str) -> bytes:
        stopwatch = start()
        clamped = min(max(self.speed, 0.5), 2.0)
        wpm = str(round(ESPEAK_BASE_WPM * clamped))
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                [ESPEAK_BIN, "-v", self.voice, "-s", wpm, "--stdout", text],
                capture_output=True,
                check=True,
            )
        except FileNotFoundError as exc:
            raise TTSUnavailable("espeak-ng not installed") from exc
        except subprocess.CalledProcessError as exc:
            raise TTSUnavailable(f"espeak-ng failed: {_stderr_tail(exc)}") from exc
        if not completed.stdout:
            raise TTSUnavailable("espeak-ng produced no audio")
        # espeak-ng 吐 WAV；`FFMPEG_CONVERT_ARGS` 没钉 `-f`，ffmpeg 按容器自动识别，
        # 所以直接复用 mp3→pcm 的转码函数（对 wav 同样成立）。
        pcm = await asyncio.to_thread(mp3_to_pcm, completed.stdout)
        _record(
            provider=self.name,
            voice=self.voice,
            text=text,
            pcm=pcm,
            total_ms=stopwatch.elapsed_ms(),
        )
        return pcm


class BridgeTTSProvider(TTSProvider):
    """宿主机 TTS 桥：容器出不了网时，由**宿主机**代跑 edge-tts。

    ## 为什么需要（2026-11-11 实测）

    - 宿主机直连 `speech.platform.bing.com:443` 正常，`edge_tts` 出 12096 字节 mp3；
    - 容器内 TCP 能连上，但 TLS 握手读 0 字节即 `SSLEOFError`（`www.google.com` 同样），
      → 整个容器出网 TLS 不可用，`EdgeTTSProvider` 在容器里 100% `TTSUnavailable`；
    - 于是退到 `espeak-ng` 的机器人声。音色掉档不是「配置没配」，是网络拓扑问题，
      `TTS_PROVIDER=edge` 在这台机器上怎么写都好不了。

    桥（`scripts/tts_bridge.py`）跑在宿主机上，容器经 `host.docker.internal` 调它，
    拿到的就是 edge 的真音色；桥挂了/没起，本 provider 抛 `TTSUnavailable`，
    由 `ChainTTSProvider` 继续往下一档退（edge 直连、最后 espeak）。
    """

    name = "edge-bridge"

    def __init__(
        self, *, url: str, timeout_ms: int = 15_000, voice: str = EDGE_DEFAULT_VOICE
    ) -> None:
        self.url = url.rstrip("/")
        self.timeout_ms = timeout_ms
        self.voice = voice or EDGE_DEFAULT_VOICE

    def _payload(self, text: str) -> bytes:
        return json.dumps({"text": text, "voice": self.voice}).encode("utf-8")

    async def _post_whole(self, text: str) -> bytes:
        payload = self._payload(text)

        def _post() -> bytes:
            # 用标准库而不是 httpx：这条路径只在「本机演示 + 容器出不了网」时启用。
            import urllib.request

            # 不是用户输入；scheme 由我们在 Settings 里给死成 http(s)。
            request = urllib.request.Request(  # noqa: S310
                f"{self.url}/tts",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(  # noqa: S310
                request, timeout=self.timeout_ms / 1000
            ) as response:
                return bytes(response.read())

        try:
            mp3 = await asyncio.to_thread(_post)
        except Exception as exc:  # urllib 的异常族不稳定，统一映射成「可降级」
            raise TTSUnavailable(f"tts bridge failed: {type(exc).__name__}: {exc}") from exc
        if not mp3:
            raise TTSUnavailable("tts bridge returned no audio")
        return mp3

    async def synthesize(self, text: str) -> bytes:
        stopwatch = start()
        mp3 = await self._post_whole(text)
        pcm = await asyncio.to_thread(mp3_to_pcm, mp3)
        _record(
            provider=self.name,
            voice=self.voice,
            text=text,
            pcm=pcm,
            total_ms=stopwatch.elapsed_ms(),
        )
        return pcm

    async def _iter_mp3(self, text: str) -> AsyncIterator[bytes]:
        """优先走桥的 `POST /tts/stream`；桥还是旧版（404）就退回整段接口。"""
        try:
            import httpx
        except ImportError as exc:  # httpx 是运行时依赖；缺失按不可用降级
            raise TTSUnavailable("httpx not installed") from exc

        endpoint = f"{self.url}/tts/stream"
        timeout = httpx.Timeout(self.timeout_ms / 1000, read=self.timeout_ms / 1000)
        try:
            async with (
                httpx.AsyncClient(timeout=timeout) as client,
                client.stream("POST", endpoint, json=json.loads(self._payload(text))) as resp,
            ):
                if resp.status_code == 404:
                    # 旧桥没有流式端点：整段拿回来，至少不把真音色降级掉。
                    mp3 = await self._post_whole(text)
                    yield mp3
                    return
                if resp.status_code >= 400:
                    detail = (await resp.aread())[:160].decode("utf-8", "replace")
                    raise TTSUnavailable(f"tts bridge stream HTTP {resp.status_code}: {detail}")
                produced = False
                async for chunk in resp.aiter_bytes():
                    if chunk:
                        produced = True
                        yield chunk
                if not produced:
                    raise TTSUnavailable("tts bridge stream returned no audio")
        except TTSUnavailable:
            raise
        except Exception as exc:  # 网络/协议异常统一映射成「可降级」
            raise TTSUnavailable(f"tts bridge stream failed: {type(exc).__name__}: {exc}") from exc

    async def synthesize_stream(self, text: str) -> AsyncIterator[bytes]:
        stopwatch = start()
        first_ms: float | None = None
        produced = 0
        async for pcm in stream_mp3_to_pcm(self._iter_mp3(text)):
            if first_ms is None:
                first_ms = stopwatch.elapsed_ms()
            produced += len(pcm)
            yield pcm
        _record_stream(
            provider=self.name,
            voice=self.voice,
            text=text,
            pcm_bytes=produced,
            first_packet_ms=first_ms,
            total_ms=stopwatch.elapsed_ms(),
        )


def _provider_name(provider: TTSProvider) -> str:
    """provider 的观测名：C1 契约没声明 `name`，但所有实现都带；缺省退类名。"""
    name = getattr(provider, "name", None)
    return name if isinstance(name, str) else type(provider).__name__


async def _close_stream(stream: AsyncIterator[bytes]) -> None:
    """尽力关闭上游流：契约返回 `AsyncIterator`，只有异步生成器带 `aclose`。"""
    aclose = getattr(stream, "aclose", None)
    if aclose is not None:
        await aclose()


class ChainTTSProvider(TTSProvider):
    """按顺序取第一个成功的 provider（全部失败才抛最后一个 `TTSUnavailable`）。

    与 `FallbackTTSProvider` 的区别：那个只两档、且 `name` 固定为主 provider 名；
    这里要表达「bridge → edge → espeak」三档，`name` 取**第一档**（配置意图），
    真降级由每档一条 `tts_fallback` 日志体现。
    """

    def __init__(self, providers: list[TTSProvider]) -> None:
        if not providers:
            raise ValueError("ChainTTSProvider needs at least one provider")
        self.providers = providers
        self.name = _provider_name(providers[0])

    def _log_fallback(self, index: int, provider: TTSProvider, exc: Exception, text: str) -> None:
        next_provider = self.providers[index + 1] if index + 1 < len(self.providers) else None
        logger.warning(
            "tts_fallback",
            extra={
                "primary": _provider_name(provider),
                "fallback": _provider_name(next_provider) if next_provider else None,
                "reason": str(exc)[:120],
                "text_len": len(text),
            },
        )

    async def synthesize(self, text: str) -> bytes:
        last_error: TTSUnavailable | None = None
        for index, provider in enumerate(self.providers):
            try:
                return await provider.synthesize(text)
            except TTSUnavailable as exc:
                last_error = exc
                self._log_fallback(index, provider, exc, text)
        if last_error is not None:
            raise last_error
        raise TTSUnavailable("no tts provider available")

    async def synthesize_stream(self, text: str) -> AsyncIterator[bytes]:
        """按顺序取第一条**能产出首块**的链路；首块之后失败不再切换（已出声，换档会串音）。"""
        last_error: TTSUnavailable | None = None
        for index, provider in enumerate(self.providers):
            iterator = provider.synthesize_stream(text)
            try:
                first = await anext(iterator)
            except StopAsyncIteration:
                await _close_stream(iterator)
                last_error = TTSUnavailable(f"{_provider_name(provider)} returned no audio")
                self._log_fallback(index, provider, last_error, text)
                continue
            except TTSUnavailable as exc:
                await _close_stream(iterator)
                last_error = exc
                self._log_fallback(index, provider, exc, text)
                continue
            try:
                yield first
                async for chunk in iterator:
                    yield chunk
            finally:
                await _close_stream(iterator)
            return
        if last_error is not None:
            raise last_error
        raise TTSUnavailable("no tts provider available")


class DisabledTTSProvider(TTSProvider):
    """显式「这条链禁用」占位：`ALLOW_ROBOT_VOICE=false` 时顶掉 espeak 档。

    用抛错占位而不是从链里删掉，是为了保持 `Fallback(edge, ...)` 的既有结构（观测与
    测试都按「主档 + 兜底档」读）；兜底档永远失败，等于 espeak 不在用户可播路径上。
    """

    name = "disabled"

    def __init__(self, reason: str) -> None:
        self.reason = reason

    async def synthesize(self, text: str) -> bytes:
        raise TTSUnavailable(self.reason)

    async def synthesize_stream(self, text: str) -> AsyncIterator[bytes]:
        raise TTSUnavailable(self.reason)
        yield b""  # pragma: no cover - 让类型检查知道这是异步生成器


class FallbackTTSProvider(TTSProvider):
    """先试主 provider，抛 `TTSUnavailable` 再试兜底（两者都失败才向上抛）。

    `name` 反映**主** provider：观测口径里「配的是 edge」这事不该因为降级而变，
    真降级了由下面那条 `tts_fallback` 日志和 `provider=` 字段区分。
    """

    def __init__(self, primary: TTSProvider, fallback: TTSProvider) -> None:
        self.primary = primary
        self.fallback = fallback
        self.name = _provider_name(primary)

    async def synthesize(self, text: str) -> bytes:
        try:
            return await self.primary.synthesize(text)
        except TTSUnavailable as exc:
            logger.warning(
                "tts_fallback",
                extra={
                    "primary": _provider_name(self.primary),
                    "fallback": _provider_name(self.fallback),
                    "reason": str(exc)[:120],
                    "text_len": len(text),
                },
            )
        return await self.fallback.synthesize(text)

    async def synthesize_stream(self, text: str) -> AsyncIterator[bytes]:
        primary = self.primary.synthesize_stream(text)
        try:
            first = await anext(primary)
        except (TTSUnavailable, StopAsyncIteration) as exc:
            await _close_stream(primary)
            reason = exc if isinstance(exc, TTSUnavailable) else TTSUnavailable("primary empty")
            logger.warning(
                "tts_fallback",
                extra={
                    "primary": _provider_name(self.primary),
                    "fallback": _provider_name(self.fallback),
                    "reason": str(reason)[:120],
                    "text_len": len(text),
                },
            )
            async for chunk in self.fallback.synthesize_stream(text):
                yield chunk
            return
        try:
            yield first
            async for chunk in primary:
                yield chunk
        finally:
            await _close_stream(primary)


# ---------------------------------------------------------------------------
# 实现：云 API 骨架（等统一 key）
# ---------------------------------------------------------------------------


class CloudTTSProvider(TTSProvider):
    """云端 TTS 骨架（等统一 key）。接口形状已按 C1 固定：返回整段 PCM。

    TODO(迭代 5+，拿到 key 后按此顺序补，不要改签名)：
    1. `settings.tts_base_url / tts_app_id / tts_api_key / tts_voice_id` 全部只从 Settings 读，
       禁止硬编码，也禁止落到日志；
    2. 用 httpx.AsyncClient 建长连接（首包预算 300ms），超时取 `settings.tts_timeout_ms`；
    3. 若云端直接给 PCM，就跳过 ffmpeg 分支；若给 mp3/opus，仍复用 `mp3_to_pcm()`；
    4. 云端失败统一抛 `TTSUnavailable`，由调用方切「大字幕 + 预置音频」。
    """

    name = "cloud"

    def __init__(self, *, voice: str = "", timeout_ms: int = 300) -> None:
        self.voice = voice
        self.timeout_ms = timeout_ms

    async def synthesize(self, text: str) -> bytes:
        raise NotImplementedError("CloudTTSProvider 待统一 key（见类 docstring 的 TODO）")


class VoicePackTTSProvider(TTSProvider):
    """固定话术音频包：manifest 命中直接读 WAV，零合成、零网络；未命中透传内层。

    包在整条取音链**最外层**：bridge/edge/espeak 谁能出声不影响命中顺序——长者听到
    的第一句问候、用药提示都先问音频包。音色不一致（人设换了 voice）时不命中，
    否则会出现「字幕是新角色、声音是旧角色」。
    """

    name = "voice-pack"

    def __init__(self, inner: TTSProvider, manifest_path: Path, *, voice: str = "") -> None:
        self.inner = inner
        self.manifest_path = manifest_path
        self.root = manifest_path.parent
        self.voice = (voice or "").strip()
        self.entries = load_manifest(manifest_path)

    def _lookup(self, text: str) -> bytes | None:
        if not self.entries:
            return None
        entry = self.entries.get(text_sha256(text))
        if entry is None:
            return None
        entry_voice = str(entry.get("voice") or "").strip()
        if self.voice and entry_voice and entry_voice != self.voice:
            return None
        path = resolve_entry_file(self.root, entry)
        if path is None:
            logger.warning("voice_pack_file_missing", extra={"text_len": len(text)})
            return None
        try:
            return read_wav_pcm(path)
        except (OSError, ValueError, wave.Error) as exc:
            logger.warning(
                "voice_pack_read_failed",
                extra={"error_type": type(exc).__name__, "text_len": len(text)},
            )
            return None

    def _record_hit(self, text: str, pcm: bytes) -> None:
        record_tts(
            provider=self.name,
            voice=self.voice or "pack",
            text_len=len(text),
            pcm_bytes=len(pcm),
            first_packet_ms=0.0,
            total_ms=0.0,
        )

    async def synthesize(self, text: str) -> bytes:
        pcm = await asyncio.to_thread(self._lookup, text)
        if pcm is None:
            return await self.inner.synthesize(text)
        self._record_hit(text, pcm)
        return pcm

    async def synthesize_stream(self, text: str) -> AsyncIterator[bytes]:
        pcm = await asyncio.to_thread(self._lookup, text)
        if pcm is None:
            async for chunk in self.inner.synthesize_stream(text):
                yield chunk
            return
        self._record_hit(text, pcm)
        for chunk in _chunk_bytes(pcm):
            yield chunk


def _wrap_voice_pack(
    provider: TTSProvider,
    config: Settings,
    *,
    requested_voice: str | None,
) -> TTSProvider:
    """manifest 存在且开关打开才包一层；目录/文件缺失不优化也不报错。"""
    if not config.voice_pack_enabled:
        return provider
    manifest = Path(config.voice_pack_dir or "static/voices") / "manifest.json"
    if not manifest.is_file():
        return provider
    return VoicePackTTSProvider(provider, manifest, voice=requested_voice or "")


# ---------------------------------------------------------------------------
# 工厂：按 Settings 分派（TTS_PROVIDER 空值/未知值一律归一到 mock）
# ---------------------------------------------------------------------------


def _voice_override(voice: str | None) -> str | None:
    """人设音色只在提供 TTS 音色时生效；`mock`/`bridge` 是历史占位值，不当真音色。"""
    normalized = (voice or "").strip()
    if normalized.lower() in {"", "mock", "bridge"}:
        return None
    return normalized


def build_tts(settings: Settings | None = None, *, voice: str | None = None) -> TTSProvider:
    """按 `TTS_PROVIDER` 分派：``mock`` / ``edge`` / ``cloud``。

    `voice` 是人设音色（`personas.voice_id`）的请求级覆盖：给了有效值就盖过
    `TTS_VOICE_ID`；空串与 `mock`/`bridge` 占位值视为不覆盖。这样同一进程可以按
    当前角色选音色，而不必为每个角色重建一套 Settings。

    空值与未知 provider 都归一到 ``mock``：迭代 3 的默认环境没有云端 key、也不保证装了
    ffmpeg，让「没配置」变成「跑不通」会把 A3/A5 一起拖下水。未知值额外打 warning。
    """
    config = settings or get_settings()
    kind = (config.tts_provider or "").strip().lower()
    requested_voice = _voice_override(voice) or (config.tts_voice_id or "").strip() or None

    if kind == "edge":
        resolved_voice = requested_voice or EDGE_DEFAULT_VOICE
        if resolved_voice not in EDGE_SUPPORTED_VOICES:
            logger.warning(
                "unsupported edge voice %r, fallback to %s",
                resolved_voice,
                EDGE_DEFAULT_VOICE,
            )
            resolved_voice = EDGE_DEFAULT_VOICE
        edge = EdgeTTSProvider(voice=resolved_voice, speed=config.tts_speed)
        if config.allow_robot_voice:
            espeak: TTSProvider = EspeakTTSProvider(speed=config.tts_speed)
        else:
            espeak = DisabledTTSProvider("robot voice disabled (ALLOW_ROBOT_VOICE=false)")
        # 本机演示：容器出不了网（见 config.tts_bridge_url 注释），先问宿主机上的桥。
        # 桥没起/超时都只是抛 TTSUnavailable，自动落回 edge 直连、再落最后一档。
        bridge_url = (config.tts_bridge_url or "").strip()
        if bridge_url:
            provider: TTSProvider = ChainTTSProvider(
                [
                    BridgeTTSProvider(url=bridge_url, voice=resolved_voice),
                    edge,
                    espeak,
                ]
            )
        else:
            provider = FallbackTTSProvider(edge, espeak)
        return _wrap_voice_pack(provider, config, requested_voice=resolved_voice)
    if kind == "cloud":
        cloud = CloudTTSProvider(voice=requested_voice or "", timeout_ms=config.tts_timeout_ms)
        return _wrap_voice_pack(cloud, config, requested_voice=requested_voice or "")
    if kind not in {"", "mock"}:
        logger.warning("unknown TTS_PROVIDER=%r, fallback to mock", kind)
    return MockTTSProvider(voice=requested_voice or MOCK_VOICE)
