"""TTS 兜底链单测（edge → espeak-ng）：只测行为，全部替身，不真跑子进程/网络。

## 为什么单独一组

容器到 ``speech.platform.bing.com:443`` 的 TLS 握手被掐断，edge-tts 100% 抛
``TTSUnavailable``（2026-11-11 实测，宿主机正常、容器内失败）。当前
``TTS_PROVIDER=edge`` 返回的是
``FallbackTTSProvider(EdgeTTSProvider, EspeakTTSProvider)``，由镜像内的
``espeak-ng``（普通话音色 ``cmn``）兜底。这组用例钉三件事：

1. 主 provider 成功时**不许**碰兜底（否则每次合成白跑一次 espeak）；
2. 主 provider 抛 ``TTSUnavailable`` 才切兜底，并落 ``tts_fallback`` 日志；
3. 两条都断时向上抛的必须还是 ``TTSUnavailable``（调用方只认这一种降级信号）。

另钉 ``EspeakTTSProvider`` 的两个边界：二进制缺失、空 stdout —— 都必须是
``TTSUnavailable``，不能让 ``FileNotFoundError`` / ``CalledProcessError`` 漏给调用方
（那会把「可降级」变成「整条链路炸」）。
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from app.audio import tts as tts_module
from app.audio.tts import (
    EDGE_DEFAULT_VOICE,
    ESPEAK_BIN,
    ESPEAK_VOICE,
    DisabledTTSProvider,
    EdgeTTSProvider,
    EspeakTTSProvider,
    FallbackTTSProvider,
    MockTTSProvider,
    TTSUnavailable,
    build_tts,
)
from app.core.config import Settings

TEXT = "今天天气怎么样"
PRIMARY_PCM = b"\x01" * 640
FALLBACK_PCM = b"\x02" * 640


class _Provider:
    """`TTSProvider` 替身：记录调用、返回固定 PCM 或抛指定异常。"""

    def __init__(
        self, name: str, *, pcm: bytes = PRIMARY_PCM, error: BaseException | None = None
    ) -> None:
        self.name = name
        self.pcm = pcm
        self.error = error
        self.texts: list[str] = []

    async def synthesize(self, text: str) -> bytes:
        self.texts.append(text)
        if self.error is not None:
            raise self.error
        return self.pcm


class _Completed:
    """`subprocess.run` 返回值的最小替身（本层只读 stdout）。"""

    def __init__(self, stdout: bytes) -> None:
        self.stdout = stdout
        self.stderr = b""


def _settings(**overrides: Any) -> Settings:
    """测试专用 Settings：断开本机 `.env`（`_env_file=None`），只钉 TTS 相关字段。"""
    base: dict[str, Any] = {
        "tts_provider": "edge",
        "tts_speed": 1.0,
        "voice_pack_enabled": False,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


# ===========================================================================
# 一、FallbackTTSProvider：只在主链路「不可用」时切兜底
# ===========================================================================


class TestFallbackTTSProvider:
    @pytest.mark.asyncio
    async def test_primary_success_never_touches_fallback(self) -> None:
        primary = _Provider("edge")
        fallback = _Provider("espeak", pcm=FALLBACK_PCM)
        provider = FallbackTTSProvider(primary, fallback)

        result = await provider.synthesize(TEXT)

        assert result == PRIMARY_PCM
        assert primary.texts == [TEXT]
        assert fallback.texts == [], "主 provider 成功还调了兜底：每次合成白跑一次 espeak"
        assert provider.name == "edge", (
            "name 必须反映主 provider（观测口径里「配的是 edge」不该因降级而变）"
        )

    @pytest.mark.asyncio
    async def test_switches_to_fallback_and_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING, logger="app.audio.tts")
        primary = _Provider("edge", error=TTSUnavailable("edge-tts failed: TLS handshake blocked"))
        fallback = _Provider("espeak", pcm=FALLBACK_PCM)

        result = await FallbackTTSProvider(primary, fallback).synthesize(TEXT)

        assert result == FALLBACK_PCM
        assert fallback.texts == [TEXT]

        records = [record for record in caplog.records if record.getMessage() == "tts_fallback"]
        assert records, (
            "降级了却没有任何 tts_fallback 日志：线上分不清「配的就是 espeak」与「edge 挂了在兜底」"
        )
        record = records[0]
        assert getattr(record, "primary", None) == "edge"
        assert getattr(record, "fallback", None) == "espeak"
        assert getattr(record, "text_len", None) == len(TEXT)

    @pytest.mark.asyncio
    async def test_both_fail_raises_tts_unavailable(self) -> None:
        fallback_error = TTSUnavailable("espeak-ng not installed")
        primary = _Provider("edge", error=TTSUnavailable("edge-tts failed"))
        fallback = _Provider("espeak", error=fallback_error)

        with pytest.raises(TTSUnavailable) as info:
            await FallbackTTSProvider(primary, fallback).synthesize(TEXT)

        assert info.value is fallback_error, (
            "两条都断时把主链路的异常吞掉、换成兜底的异常才对得上现场：现场断的是兜底这条腿"
        )
        assert type(info.value) is TTSUnavailable, "调用方只认 TTSUnavailable 这一种降级信号"
        assert fallback.texts == [TEXT]


# ===========================================================================
# 二、EspeakTTSProvider：二进制缺失 / 空 stdout 都必须是 TTSUnavailable
# ===========================================================================


class TestEspeakTTSProvider:
    @pytest.mark.asyncio
    async def test_missing_binary_maps_to_tts_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*args: Any, **kwargs: Any) -> _Completed:
            raise FileNotFoundError("espeak-ng")

        monkeypatch.setattr(tts_module.subprocess, "run", boom)

        with pytest.raises(TTSUnavailable) as info:
            await EspeakTTSProvider().synthesize(TEXT)

        assert "espeak-ng not installed" in str(info.value), (
            "镜像里是我们自己装的 espeak-ng：缺失要报成可降级的 TTSUnavailable，"
            "不能让 FileNotFoundError 漏给调用方"
        )

    @pytest.mark.asyncio
    async def test_empty_stdout_maps_to_tts_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tts_module.subprocess, "run", lambda *a, **k: _Completed(b""))

        with pytest.raises(TTSUnavailable) as info:
            await EspeakTTSProvider().synthesize(TEXT)

        assert "no audio" in str(info.value), (
            "空 WAV 是 espeak 静音的真实形态：报 TTSUnavailable 才能让上层切字幕兜底，"
            "否则会拿到 0 字节 PCM 直接把下行轨写坏"
        )

    @pytest.mark.asyncio
    async def test_uses_cmn_voice_and_transcodes_wav(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}

        def fake_run(command: list[str], **kwargs: Any) -> _Completed:
            seen["command"] = command
            seen["kwargs"] = kwargs
            return _Completed(b"RIFF\x00\x00\x00\x00WAVEfmt ")

        def fake_convert(wav: bytes) -> bytes:
            seen["wav"] = wav
            return b"\x03" * 320

        monkeypatch.setattr(tts_module.subprocess, "run", fake_run)
        monkeypatch.setattr(tts_module, "mp3_to_pcm", fake_convert)

        result = await EspeakTTSProvider().synthesize(TEXT)

        assert result == b"\x03" * 320
        command = seen["command"]
        assert command[0] == ESPEAK_BIN
        assert command[command.index("-v") + 1] == ESPEAK_VOICE == "cmn", (
            "普通话音色不能被改掉：英语音色读汉字是一串拼音"
        )
        assert command[-1] == TEXT
        assert "--stdout" in command, "必须走 stdout 出 WAV，别落临时文件（容器只读层/清理成本）"
        assert seen["kwargs"].get("check") is True
        assert seen["wav"].startswith(b"RIFF"), "转码入口必须拿到 WAV 原文"


# ===========================================================================
# 三、build_tts：tts_provider=edge 时必须把兜底链装好
# ===========================================================================


class TestBuildTts:
    def test_edge_wires_primary_and_fallback_when_robot_voice_allowed(self) -> None:
        provider = build_tts(
            _settings(
                tts_provider="edge",
                tts_voice_id="",
                tts_speed=1.25,
                allow_robot_voice=True,
            )
        )

        assert isinstance(provider, FallbackTTSProvider), (
            "edge 必须自带兜底：容器到 speech.platform.bing.com 的 TLS 被掐断是常态，"
            "裸 EdgeTTSProvider 等于「网络一断就没有声音」"
        )
        assert isinstance(provider.primary, EdgeTTSProvider)
        assert isinstance(provider.fallback, EspeakTTSProvider)
        assert provider.name == provider.primary.name == "edge"
        assert provider.primary.voice == EDGE_DEFAULT_VOICE, (
            "tts_voice_id 为空时应回落到默认普通话音色，而不是空字符串"
        )
        assert provider.primary.speed == 1.25
        assert provider.fallback.speed == 1.25, "语速要一路传到兜底，否则降级后语速突变"

    def test_edge_disables_robot_voice_by_default(self) -> None:
        """P4：默认 ALLOW_ROBOT_VOICE=false，espeak 不能出现在用户可播路径。"""
        provider = build_tts(_settings(tts_provider="edge"))

        assert isinstance(provider, FallbackTTSProvider)
        assert isinstance(provider.fallback, DisabledTTSProvider)
        assert not isinstance(provider.fallback, EspeakTTSProvider)

    @pytest.mark.asyncio
    async def test_disabled_robot_voice_raises_tts_unavailable(self) -> None:
        provider = build_tts(_settings(tts_provider="edge", allow_robot_voice=False))

        with pytest.raises(TTSUnavailable):
            await provider.fallback.synthesize("你好")

    def test_unknown_provider_falls_back_to_mock(self) -> None:
        provider = build_tts(_settings(tts_provider="edge-typo"))

        assert isinstance(provider, MockTTSProvider), (
            "未知值必须归一到 mock：拼错配置不应让整条链路跑不通"
        )
