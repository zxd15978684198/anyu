"""把固定话术预合成成 16k 单声道 WAV + manifest（P4 音频包）。

## 为什么要在离线时预生成

edge 是微软公开接口，无 SLA；演示机容器又出不了网，只能靠宿主机 TTS 桥。固定话术
（开场白、用药确认、健康五问）是**每条会话都会出现**的句子，提前合成后命中 manifest
即可零合成、零网络直接播放——桥没起、edge 抽风、断网都不影响这些句子有真人声。

## 用法

```bash
uv run python scripts/bake_voice_pack.py --voice zh-CN-XiaoxiaoNeural --speed 0.95
```

产物写到 `static/voices/`：每个 key 一个 WAV + 一份 `manifest.json`。目录与后端
`VOICE_PACK_DIR`（默认 `static/voices`）一致；nginx 已把 `/static/` 直出。

## 命中口径

manifest 的键是 `sha256(text.strip())`，命中要求**音色也一致**。改一个字或换角色音色
都会 miss，回落实时合成——宁可多一次网络往返，也不让长者听到「字幕是新角色、声音是
旧角色」。因此改文案后要重跑本脚本，否则旧句子只会白白留在目录里。

## 边界

- 只本机/CI 生成资产，不进请求链路；运行期只读 manifest 与 WAV。
- 不打印、不落库任何长者数据；话术全部是代码里的固定文案。
- `--force` 才覆盖已有文件；默认增量补齐缺失项。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import wave
from pathlib import Path
from typing import Final

# 仓库根补进 sys.path：scripts/bake_voice_pack.py -> scripts -> 根
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.audio.tts import EdgeTTSProvider, TTSUnavailable  # noqa: E402
from app.audio.voice_pack import FIXED_PHRASES, text_sha256  # noqa: E402

DEFAULT_VOICE: Final[str] = "zh-CN-XiaoxiaoNeural"
DEFAULT_OUT: Final[Path] = Path("static/voices")


def _write_wav(path: Path, pcm: bytes) -> int:
    """PCM（16k/16bit/mono）→ WAV；返回时长毫秒（按采样率换算，不依赖外部探针）。"""
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(pcm)
    return round(len(pcm) / 2 / 16_000 * 1000)


def _load_existing(path: Path) -> dict[str, dict[str, object]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = raw.get("entries") if isinstance(raw, dict) else None
    if not isinstance(entries, dict):
        return {}
    return {
        key: value
        for key, value in entries.items()
        if isinstance(key, str) and isinstance(value, dict)
    }


def _prepare(out_dir: Path) -> tuple[Path, dict[str, dict[str, object]]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.json"
    return manifest_path, _load_existing(manifest_path)


async def bake(out_dir: Path, *, voice: str, speed: float, force: bool) -> int:
    manifest_path, entries = await asyncio.to_thread(_prepare, out_dir)
    provider = EdgeTTSProvider(voice=voice, speed=speed)
    failures = 0
    for key, text in FIXED_PHRASES.items():
        digest = text_sha256(text)
        filename = f"{key}.wav"
        target = out_dir / filename
        if target.is_file() and not force and digest in entries:
            print(f"skip   {key:<20} (exists)")
            continue
        try:
            pcm = await provider.synthesize(text)
        except TTSUnavailable as exc:
            failures += 1
            print(f"FAIL   {key:<20} {exc}", file=sys.stderr)
            continue
        duration_ms = _write_wav(target, pcm)
        entries[digest] = {
            "text": text,
            "voice": voice,
            "file": filename,
            "duration_ms": duration_ms,
        }
        print(f"baked  {key:<20} {duration_ms:>5} ms  {len(pcm):>7} bytes")

    # 只保留当前话术清单的条目，避免改文案后旧 hash 永远留在 manifest 里。
    current = {text_sha256(text) for text in FIXED_PHRASES.values()}
    entries = {key: value for key, value in entries.items() if key in current}
    manifest_path.write_text(
        json.dumps({"version": 1, "voice": voice, "entries": entries}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    print(f"manifest {manifest_path} entries={len(entries)} failures={failures}")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Bake fixed-phrase TTS voice pack")
    parser.add_argument("--voice", default=DEFAULT_VOICE)
    parser.add_argument("--speed", type=float, default=0.95)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--force", action="store_true", help="覆盖已有 WAV 与 manifest 条目")
    args = parser.parse_args()
    return asyncio.run(bake(args.out, voice=args.voice, speed=args.speed, force=args.force))


if __name__ == "__main__":
    raise SystemExit(main())
