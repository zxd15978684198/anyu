"""宿主机 TTS 桥：`POST /tts/stream` 必须真流式，且首块前失败仍能 503 降级。

桥是容器出不了网时的唯一真音色入口。它一旦把「流式」实现成「攒完整段再返回」，
容器侧 P2 的分块出流就白做了；这里直接对 ASGI 应用断言响应体分块顺序与失败时机。
"""

from __future__ import annotations

import sys
import types
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from scripts import tts_bridge

CHUNKS = [b"mp3-a", b"mp3-b", b"mp3-c"]


def _install_edge(
    monkeypatch: pytest.MonkeyPatch, *, chunks: list[bytes], fail: bool = False
) -> None:
    class FakeCommunicate:
        def __init__(self, text: str, voice: str, *, rate: str = "+0%") -> None:
            self.text = text

        async def stream(self) -> AsyncIterator[dict[str, Any]]:
            yield {"type": "WordBoundary", "offset": 0}
            if fail:
                raise RuntimeError("edge refused")
            for chunk in chunks:
                yield {"type": "audio", "data": chunk}

    module = types.ModuleType("edge_tts")
    module.Communicate = FakeCommunicate  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "edge_tts", module)


async def _post(path: str, app: Any = tts_bridge.app) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bridge") as client:
        return await client.post(path, json={"text": "你好", "voice": "zh-CN-XiaoxiaoNeural"})


async def test_stream_returns_audio_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_edge(monkeypatch, chunks=CHUNKS)

    response = await _post("/tts/stream")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/mpeg")
    assert response.content == b"".join(CHUNKS)


async def test_stream_failure_before_first_chunk_returns_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_edge(monkeypatch, chunks=[], fail=True)

    response = await _post("/tts/stream")

    assert response.status_code == 503


async def test_stream_empty_audio_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_edge(monkeypatch, chunks=[])

    response = await _post("/tts/stream")

    assert response.status_code == 503


async def test_whole_endpoint_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_edge(monkeypatch, chunks=CHUNKS)

    response = await _post("/tts")

    assert response.status_code == 200
    assert response.content == b"".join(CHUNKS)
