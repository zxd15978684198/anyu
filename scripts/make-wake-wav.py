"""合成一段可喂给浏览器「假麦克风」的唤醒词 WAV。

背景：开发机常年没有麦克风，而唤醒词（FR-UI-11 / AC-42）只有在真实采集链路上
才谈得上验证。Chrome 的 `--use-file-for-fake-audio-capture` 能把一个 16kHz
单声道 WAV 伪装成麦克风输入，于是 `getUserMedia` 会正常返回 MediaStream，
pad 的整条链路（KWS → 建连 → VAD → 上行）都能真跑。

本脚本只负责造那段音频：TTS 合成「小伴」→ 转 16k/mono/s16 → 加静音拼成
可循环的时间轴（静音 + 唤醒词 + 静音），保证 Chrome 循环播放时周期性触发。

用法：
    uv run python scripts/make-wake-wav.py
    uv run python scripts/make-wake-wav.py --out .tmp/voice/wake.wav --repeats 4

依赖：edge_tts（已在项目 venv 中）、ffmpeg（在 PATH 上）。
产物落在 .tmp/ 下，不入库——仓库禁区第 11 条不允许提交媒体大文件。
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

TARGET_RATE = 16_000
TARGET_WIDTH = 2  # s16le
TARGET_CHANNELS = 1


def _synth_mp3(text: str, voice: str, dest: Path) -> None:
    """用 edge-tts 把文本合成成 mp3。网络不可用时抛 RuntimeError。"""
    try:
        import edge_tts  # 延迟导入：只在真正合成时要求依赖存在
        from edge_tts.exceptions import EdgeTTSException
    except ImportError as exc:  # 依赖缺失给可读指引，而不是把导入栈甩给使用者
        raise RuntimeError(
            "缺少依赖 edge-tts：`uv add edge-tts`；或改用 --wav 复用一段现成 WAV。"
        ) from exc

    async def _run() -> None:
        await edge_tts.Communicate(text, voice).save(str(dest))

    # 只兜 edge-tts 自家异常基类（网络/鉴权/无音频）与本地 I/O、参数类错误。
    # 其它异常（例如事件循环已在跑时的 RuntimeError）原样上抛，不伪装成「合成失败」。
    try:
        asyncio.run(_run())
    except (EdgeTTSException, OSError, ValueError) as exc:
        raise RuntimeError(
            f"edge-tts 合成失败（{type(exc).__name__}: {exc}）。"
            "该步骤需要联网访问 Microsoft 的 TTS 服务；离线时可自行准备一段"
            "包含「小伴」的 16kHz 单声道 WAV 并用 --wav 直接传入。"
        ) from exc

    if not dest.exists() or dest.stat().st_size == 0:
        raise RuntimeError("edge-tts 未产出音频文件")


def _to_pcm(mp3: Path, dest: Path) -> None:
    """mp3 → 16kHz / mono / s16le 的裸 PCM。"""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("找不到 ffmpeg，请先安装并确保在 PATH 上")

    cmd = [
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(mp3),
        "-ar",
        str(TARGET_RATE),
        "-ac",
        str(TARGET_CHANNELS),
        "-f",
        "s16le",
        str(dest),
    ]
    # 安全：argv 是列表、未传 shell=True（默认 False），ffmpeg 路径来自 shutil.which()，
    # 其余元素是脚本常量或用户显式指定的路径，都不经过 shell 解析 → 无命令注入面。
    # S603 只在本文件放开（见 pyproject.toml 的 per-file-ignores）。
    subprocess.run(cmd, check=True)


def _read_wav_pcm(path: Path) -> bytes:
    """读入一个 WAV，返回其 16k/mono/s16 的裸 PCM（已由调用方保证格式）。"""
    with wave.open(str(path), "rb") as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (
            TARGET_RATE,
            TARGET_CHANNELS,
            TARGET_WIDTH,
        ):
            raise RuntimeError(
                f"{path} 不是 16kHz/单声道/16bit（实际 "
                f"{wav.getframerate()}Hz/{wav.getnchannels()}ch/{wav.getsampwidth() * 8}bit）"
            )
        return wav.readframes(wav.getnframes())


def _silence(seconds: float) -> bytes:
    frames = int(seconds * TARGET_RATE)
    return b"\x00" * (frames * TARGET_WIDTH * TARGET_CHANNELS)


def _write_wav(pcm: bytes, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(dest), "wb") as wav:
        wav.setnchannels(TARGET_CHANNELS)
        wav.setsampwidth(TARGET_WIDTH)
        wav.setframerate(TARGET_RATE)
        wav.writeframes(pcm)


def _trim_silence(pcm: bytes, threshold: int = 320) -> bytes:
    """掐掉首尾静音，避免 TTS 自带留白把时间轴撑长。threshold 约为 -41dBFS。"""
    step = TARGET_WIDTH * TARGET_CHANNELS
    frames = [pcm[i : i + step] for i in range(0, len(pcm), step)]
    loud = [
        i for i, f in enumerate(frames) if abs(int.from_bytes(f, "little", signed=True)) > threshold
    ]
    if not loud:
        return pcm
    return pcm[loud[0] * step : (loud[-1] + 1) * step]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="合成唤醒词假麦克风音频")
    parser.add_argument("--text", default="小伴", help="唤醒词文本，默认「小伴」")
    parser.add_argument("--voice", default="zh-CN-XiaoxiaoNeural", help="edge-tts 音色")
    parser.add_argument("--out", default=".tmp/voice/wake-xiaoban.wav", help="输出路径")
    parser.add_argument("--wav", default=None, help="复用一段已有 WAV（跳过 TTS）")
    parser.add_argument("--lead", type=float, default=1.5, help="每轮开头静音秒数")
    parser.add_argument("--gap", type=float, default=2.5, help="每轮结尾静音秒数")
    parser.add_argument("--repeats", type=int, default=3, help="唤醒词重复次数")
    args = parser.parse_args(argv)

    out = Path(args.out)
    with tempfile.TemporaryDirectory(prefix="wake-") as tmp:
        tmpdir = Path(tmp)
        if args.wav:
            src_pcm_path = Path(args.wav)
            phrase = _read_wav_pcm(src_pcm_path)
        else:
            mp3 = tmpdir / "phrase.mp3"
            pcm = tmpdir / "phrase.pcm"
            _synth_mp3(args.text, args.voice, mp3)
            _to_pcm(mp3, pcm)
            phrase = _trim_silence(pcm.read_bytes())

    if len(phrase) < TARGET_RATE * TARGET_WIDTH * 0.25:
        raise RuntimeError("唤醒词音频过短（<0.25s），TTS 可能没读出东西")

    timeline = (_silence(args.lead) + phrase + _silence(args.gap)) * args.repeats
    _write_wav(timeline, out)

    seconds = len(timeline) / (TARGET_RATE * TARGET_WIDTH * TARGET_CHANNELS)
    print(f"[ok] {out}  {out.stat().st_size} bytes  {seconds:.2f}s")
    print(
        f"     {TARGET_RATE}Hz / mono / 16bit / 唤醒词×{args.repeats}（每轮 {args.lead}s 静音 + 词 + {args.gap}s 静音）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
