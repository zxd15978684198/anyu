"""固定话术音频包：manifest 读取、文本指纹与 WAV 解码（P4）。

## 为什么放 `static/voices/` 而不是 `frontend/public/voices/`

后端容器只挂了 `./static`（见 `docker-compose.yml` 的 backend volumes），镜像里没有
`frontend/`。音频包放 `static/voices/` 后，后端读 manifest 与 nginx 直出 `/static/voices/*`
是同一个目录，零额外挂载；`frontend/public/` 方案需要再加一卷，且容器里路径不存在。

## manifest 口径

```json
{"version": 1, "entries": {"<sha256(strip(text))>": {
  "text": "...", "voice": "zh-CN-XiaoxiaoNeural", "file": "wake.mp3.wav", "duration_ms": 1234
}}}
```

指纹只认 `strip()` 后的原文：改一个字就 miss，回落实时合成，**不会播出旧句子**。
文件路径必须是 manifest 同目录下的相对路径，解析后越界即拒绝。
"""

from __future__ import annotations

import hashlib
import json
import wave
from pathlib import Path
from typing import Final

MAX_TEXT_CHARS: Final[int] = 400

#: 首批固定话术。必须与运行时实际播报文本**逐字一致**才有命中的意义；
#: 对不上只是 miss（回落实时合成），不会播错句子。
FIXED_PHRASES: Final[dict[str, str]] = {
    "greeting_generic": "您好，我在呢。有什么想聊的，随时跟我说一声。",
    "medication_taken": "好，已经记下您吃过了。",
    "medication_snooze": "好，我过一会儿再提醒您。",
    "health_systolic": "今天量血压了吗？收缩压（高压）是多少呀？",
    "health_diastolic": "那低压（舒张压）是多少呢？",
    "health_heart_rate": "心率是多少呀？就是心跳每分钟多少下。",
    "health_sleep": "昨晚睡了几个小时呀？",
    "health_mood": "今天心情怎么样？从 1 到 5 打个分，1 是很差，5 是很好。",
}


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def load_manifest(path: Path) -> dict[str, dict[str, object]] | None:
    """读 manifest；不存在/损坏/结构不对都返回 `None`（音频包整层跳过）。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    entries = raw.get("entries")
    if not isinstance(entries, dict):
        return None
    cleaned: dict[str, dict[str, object]] = {}
    for key, value in entries.items():
        if isinstance(key, str) and isinstance(value, dict):
            cleaned[key] = value
    return cleaned or None


def resolve_entry_file(root: Path, entry: dict[str, object]) -> Path | None:
    """manifest 里的相对路径 → 真实文件；越界、非文件、空路径都返回 `None`。"""
    relative = entry.get("file")
    if not isinstance(relative, str) or not relative.strip():
        return None
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def read_wav_pcm(path: Path) -> bytes:
    """读 16k/16bit/单声道 WAV 的 PCM 帧；参数不符抛 `ValueError`（由调用方降级）。"""
    with wave.open(str(path), "rb") as handle:
        if (
            handle.getnchannels() != 1
            or handle.getsampwidth() != 2
            or handle.getframerate() != 16_000
        ):
            raise ValueError("voice pack wav must be 16kHz/16bit/mono")
        return handle.readframes(handle.getnframes())
