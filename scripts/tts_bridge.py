"""本机 TTS 桥：容器出不了网时，由**宿主机**代跑 `edge-tts`。

## 为什么需要这个东西（2026-11-11 实测）

- 宿主机直连 `speech.platform.bing.com:443` 正常，`edge_tts` 出 12096 字节 mp3；
- 容器内 TCP 能连上，但 **TLS 握手读 0 字节就 EOF**（`SSLEOFError`），对
  `www.google.com` 同样失败 → 整个容器的出网 TLS 都被中间设备掐断；
- 于是 `TTS_PROVIDER=edge` 在容器里 100% 抛 `TTSUnavailable`，只能退到
  `espeak-ng` 的机器人声。

这个脚本跑在**宿主机**上（不占容器网络栈），容器通过
`http://host.docker.internal:7811` 调它，拿到的是 edge 的真音色。

## 用法

```bash
uv run --with fastapi --with uvicorn --with edge-tts \
  uvicorn --app-dir scripts tts_bridge:app --host 0.0.0.0 --port 7811
```

容器侧只需在 `.env`（或 compose 环境）里设：

```
TTS_BRIDGE_URL=http://host.docker.internal:7811
```

后端会按 `bridge → edge → espeak` 的顺序取第一个成功的（见
`app/audio/tts.py::ChainTTSProvider`）。

## 安全边界

- 只做「文本 → mp3」，不读库、不落盘、不记文本内容（日志只记字符数）；
- 默认 `0.0.0.0` 是为了让 Docker 网段能访问；这是**本机演示用**的桥，
  不要暴露到公网（如要暴露，请自行加鉴权或绑到具体网卡）。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Final

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("tts_bridge")

DEFAULT_VOICE: Final[str] = "zh-CN-XiaoxiaoNeural"
MAX_TEXT_CHARS: Final[int] = 400

app = FastAPI(title="anyu-tts-bridge", version="1.0.0")


class TtsRequest(BaseModel):
    """一条合成请求。`rate` 用 edge-tts 的 `+20%` / `-15%` 形式。"""

    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    voice: str = Field(default=DEFAULT_VOICE, max_length=64)
    rate: str = Field(default="+0%", max_length=8)


@app.get("/healthz")
async def healthz() -> dict[str, object]:
    """存活探针：容器侧启动时用来判断「桥在不在」，不在就直接走下一档。"""
    return {"ok": True, "service": "tts-bridge"}


@app.post("/tts")
async def synthesize(req: TtsRequest) -> Response:
    """文本 → mp3 字节（`audio/mpeg`）。失败一律 503，让后端切下一档。"""
    import edge_tts  # 延迟导入：只有真被调用时才需要这个依赖

    buffer = bytearray()
    try:
        communicate = edge_tts.Communicate(req.text, req.voice, rate=req.rate)
        async for chunk in communicate.stream():
            # edge-tts 的流里混着 WordBoundary 等元数据帧，只取音频帧。
            if chunk["type"] == "audio":
                buffer.extend(chunk["data"])
    except Exception as exc:  # 第三方异常类型不稳定，统一映射成「可降级」
        logger.warning("tts_bridge_failed chars=%d err=%s", len(req.text), type(exc).__name__)
        raise HTTPException(status_code=503, detail=f"edge-tts failed: {exc}") from exc
    if not buffer:
        logger.warning("tts_bridge_empty chars=%d", len(req.text))
        raise HTTPException(status_code=503, detail="edge-tts returned no audio")
    logger.info("tts_bridge_ok chars=%d bytes=%d voice=%s", len(req.text), len(buffer), req.voice)
    return Response(content=bytes(buffer), media_type="audio/mpeg")


@app.post("/tts/stream")
async def synthesize_stream(req: TtsRequest) -> StreamingResponse:
    """文本 → mp3 **块流**（P2）：来一块转一块，不攒整段再返回。

    首块拿到之前失败仍返回 503，保证后端能在**没出声**时切下一档；首块之后失败只能
    断流（音频已经写进响应，HTTP 状态改不了），后端按「流中断」降级。
    """
    import edge_tts  # 延迟导入：只有真被调用时才需要这个依赖

    async def _audio() -> AsyncIterator[bytes]:
        communicate = edge_tts.Communicate(req.text, req.voice, rate=req.rate)
        async for chunk in communicate.stream():
            if chunk["type"] == "audio" and chunk["data"]:
                yield chunk["data"]

    iterator = _audio()
    try:
        first = await anext(iterator)
    except StopAsyncIteration as exc:
        await iterator.aclose()
        logger.warning("tts_bridge_empty chars=%d", len(req.text))
        raise HTTPException(status_code=503, detail="edge-tts returned no audio") from exc
    except Exception as exc:  # 第三方异常类型不稳定，统一映射成「可降级」
        await iterator.aclose()
        logger.warning("tts_bridge_failed chars=%d err=%s", len(req.text), type(exc).__name__)
        raise HTTPException(status_code=503, detail=f"edge-tts failed: {exc}") from exc

    async def body() -> AsyncIterator[bytes]:
        count = 0
        try:
            count += len(first)
            yield first
            async for chunk in iterator:
                count += len(chunk)
                yield chunk
        finally:
            await iterator.aclose()
            logger.info(
                "tts_bridge_stream_done chars=%d bytes=%d voice=%s",
                len(req.text),
                count,
                req.voice,
            )

    return StreamingResponse(body(), media_type="audio/mpeg")
