"""契约 §7 错误响应体回归测试（a0-F01 全局 422/400 收口，task-13）。

**被钉住的契约**（`docs/接口契约_身份与权限.md` §7）：

- 错误响应体恒为 `{"detail": str, "code": str, "trace_id": str}` —— `detail` 是**字符串**
  （FastAPI 默认的 `{"detail": [{...}]}` 列表形状不合契约）；
- 400 `BAD_REQUEST`：请求体不是合法 JSON、路径参数格式错；
- 422 `VALIDATION_ERROR`：字段缺失、字段类型错、枚举越界等 Pydantic 校验失败；
- `trace_id` 必带回，且与响应头 `X-Trace-Id` 同源（AGENTS §8）。

**为什么要单独一个文件**：这套口径有两份实现 —— `app/api/elders.py` 的
`ContractAPIRoute` 在**路由内**映射，`app/main.py` 的 `RequestValidationError` 处理器在
**应用级**兜底。两份实现一旦漂移，就会表现成「auth/audio 返 422 带 code、elders 不带了」
或者 `detail` 文案分叉，所以用同一组断言同时压住两条链路（见
`test_error_detail_consistent_between_global_and_route_mapping`）。

**为什么不连数据库**：用 `app.dependency_overrides` 顶掉 `get_session`、鉴权依赖与
ASR/TTS 工厂，所有请求都在「取 DB 连接」之前就因参数校验失败返回，无 PG 也能全绿。

**鉴权与校验的先后**（实测，FastAPI 先解依赖再校验 body）：`POST /api/audio/utterance`
挂 `Depends(current_device)`、`GET /api/elders/{elder_id}` 挂 `Depends(get_current_principal)`，
不带凭证时拿到的是 401 而不是 422/400。要断言的是**校验失败**的形状，所以这两个依赖
在 fixture 里被替换成固定身份；这不是绕过鉴权测试，而是把被测对象从「鉴权」切到「错误体」，
鉴权本身的用例在 `tests/test_auth_api.py` / `tests/test_elders_api.py`。

运行：`uv run pytest tests/test_error_contract.py -q`
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.api import audio as audio_api
from app.audio.asr import MockASRProvider
from app.audio.tts import MockTTSProvider
from app.core.auth import current_device, get_current_principal
from app.core.config import get_settings
from app.core.context import TRACE_HEADER
from app.core.runtime_context import Identity
from app.db import get_session
from app.main import create_app

# 契约 §7 的三个键，多一个少一个都算漂移
_ERROR_KEYS = {"detail", "code", "trace_id"}

# `Identity` 构造即校验 UUID 格式（`app/core/runtime_context.py`），必须给真 UUID
_DEVICE_ID = "11111111-1111-4111-8111-111111111111"
_STAFF_ID = "22222222-2222-4222-8222-222222222222"

# 合法的透传 trace_id（8~64 位安全字符，`app/core/context.py` 的口径）
_SUPPLIED_TRACE_ID = "abcd1234efgh5678"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def contract_app() -> FastAPI:
    """真实 `create_app()`（不是裸 FastAPI）：全局 handler 只有在真实装配里才存在。

    只替换「会碰外部资源」的依赖，路由、中间件、异常处理器全部走生产同一套装配，
    否则测到的就不是用户实际拿到的那条链路。
    """
    application = create_app(get_settings())

    async def _no_db_session() -> AsyncIterator[object]:
        """顶掉 `get_session`：本文件的请求都在校验阶段失败，不需要真会话。"""
        yield object()

    application.dependency_overrides[get_session] = _no_db_session
    # 设备端点（audio）与员工端点（elders/devices）各需要一个固定身份；
    # 校验失败发生在依赖之后，因此身份只要合法即可，不需要库里真有这条记录。
    application.dependency_overrides[current_device] = lambda: Identity(
        kind="device", device_id=_DEVICE_ID
    )
    application.dependency_overrides[get_current_principal] = lambda: Identity(
        kind="staff", staff_id=_STAFF_ID, role="admin"
    )
    # ASR/TTS 工厂也是依赖：不顶掉的话 `.env` 指到 funasr 时会被外部服务状态影响
    application.dependency_overrides[audio_api._get_default_asr] = lambda: MockASRProvider()
    application.dependency_overrides[audio_api._get_default_tts] = lambda: MockTTSProvider()
    return application


@pytest.fixture
async def client(contract_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """`ASGITransport` 直连应用：不起 uvicorn、不占端口。"""
    transport = httpx.ASGITransport(app=contract_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


# ---------------------------------------------------------------------------
# 断言助手
# ---------------------------------------------------------------------------


def _assert_contract_error(response: httpx.Response, *, status: int, code: str) -> dict[str, str]:
    """断言响应体符合契约 §7，返回 body 供调用方继续断言。"""
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == _ERROR_KEYS, (
        f"契约 §7 要求恰好 {sorted(_ERROR_KEYS)}，实际 {sorted(body)}；响应体={body}"
    )
    assert body["code"] == code, body
    # 这一条是 a0-F01 的根因：FastAPI 默认把 detail 写成校验错误列表
    assert isinstance(body["detail"], str) and body["detail"].strip(), body
    assert isinstance(body["trace_id"], str) and len(body["trace_id"]) == 32, body
    # 响应头与响应体必须同源，否则前端上报的问题对不上服务端日志
    assert body["trace_id"] == response.headers[TRACE_HEADER], (
        f"body.trace_id={body['trace_id']} 与响应头 {TRACE_HEADER}="
        f"{response.headers.get(TRACE_HEADER)} 不一致"
    )
    return body


# ---------------------------------------------------------------------------
# 422 VALIDATION_ERROR：字段校验失败（普通 APIRouter 分支）
# ---------------------------------------------------------------------------


async def test_audio_missing_required_field_returns_422(client: httpx.AsyncClient) -> None:
    """`POST /api/audio/utterance` 缺必填的 `audio` 文件 → 422。

    audio 路由是普通 `APIRouter`：修复前这里落 FastAPI 默认的 `{"detail": [...]}`。
    """
    response = await client.post("/api/audio/utterance", data={"duration_ms": "1000"})

    _assert_contract_error(response, status=422, code="VALIDATION_ERROR")


async def test_auth_missing_fields_returns_422(client: httpx.AsyncClient) -> None:
    """`POST /api/sessions` 只有 phone、缺 password → 422。

    auth 路由同样是普通 `APIRouter`，这条补上「auth 类」的覆盖（task-13 要求
    auth/audio/health 三条都验）。
    """
    response = await client.post("/api/sessions", json={"phone": "13800000000"})

    _assert_contract_error(response, status=422, code="VALIDATION_ERROR")


async def test_audio_wrong_field_type_returns_422(client: httpx.AsyncClient) -> None:
    """字段齐但类型错（`duration_ms: "abc"` 不是整数）→ 仍是 422，不是 400。

    区分点：400 只留给「请求体不是合法 JSON / 路径参数格式错」，
    字段值不合法属于 Pydantic 校验失败。
    """
    response = await client.post(
        "/api/audio/utterance",
        files={"audio": ("a.wav", b"RIFF....WAVE", "audio/wav")},
        data={"duration_ms": "abc"},
    )

    _assert_contract_error(response, status=422, code="VALIDATION_ERROR")


async def test_audio_empty_body_returns_422(client: httpx.AsyncClient) -> None:
    """完全不带请求体 → 422（不是 400：空体不算「非法 JSON」）。"""
    response = await client.post("/api/audio/utterance")

    _assert_contract_error(response, status=422, code="VALIDATION_ERROR")


# ---------------------------------------------------------------------------
# 400 BAD_REQUEST：请求体不是合法 JSON
# ---------------------------------------------------------------------------


async def test_auth_non_json_body_returns_400(client: httpx.AsyncClient) -> None:
    """`POST /api/sessions` 请求体是 `{oops` → 400 `BAD_REQUEST`（普通 APIRouter 分支）。"""
    response = await client.post(
        "/api/sessions",
        content=b"{oops",
        headers={"Content-Type": "application/json"},
    )

    _assert_contract_error(response, status=400, code="BAD_REQUEST")


async def test_elders_non_json_body_returns_400(client: httpx.AsyncClient) -> None:
    """`POST /api/elders` 请求体是 `{oops` → 400 `BAD_REQUEST`（ContractAPIRoute 分支）。

    与上一条成对存在：两条实现必须给出同一个状态码与同一个错误码。
    """
    response = await client.post(
        "/api/elders",
        content=b"{oops",
        headers={"Content-Type": "application/json"},
    )

    _assert_contract_error(response, status=400, code="BAD_REQUEST")


# ---------------------------------------------------------------------------
# 400 BAD_REQUEST：路径参数格式错
# ---------------------------------------------------------------------------


async def test_elders_path_param_bad_uuid_returns_400(client: httpx.AsyncClient) -> None:
    """`GET /api/elders/not-a-uuid` → 400（`ContractAPIRoute` 的 `loc[0] == "path"` 分支）。"""
    response = await client.get("/api/elders/not-a-uuid")

    _assert_contract_error(response, status=400, code="BAD_REQUEST")


async def test_devices_path_param_bad_uuid_returns_400(client: httpx.AsyncClient) -> None:
    """`POST /api/devices/not-a-uuid/bind` → 400：另一条 ContractAPIRoute 路由的同类分支。"""
    response = await client.post("/api/devices/not-a-uuid/bind", json={})

    _assert_contract_error(response, status=400, code="BAD_REQUEST")


# ---------------------------------------------------------------------------
# 不误伤：合法请求仍走原路径
# ---------------------------------------------------------------------------


async def test_health_still_returns_200(client: httpx.AsyncClient) -> None:
    """`GET /health` 仍是 200：新加的处理器不许把正常响应也包成错误体。"""
    response = await client.get("/health")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert len(body["trace_id"]) == 32
    assert body["trace_id"] == response.headers[TRACE_HEADER]


# ---------------------------------------------------------------------------
# detail 必须是字符串（契约 §7 的核心回归点）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "kwargs"),
    [
        ("/api/audio/utterance", {"data": {"duration_ms": "1000"}}),
        ("/api/sessions", {"json": {"phone": "13800000000"}}),
    ],
    ids=["plain-router-audio", "plain-router-auth"],
)
async def test_422_detail_is_plain_non_empty_string(
    client: httpx.AsyncClient, path: str, kwargs: dict[str, Any]
) -> None:
    """422 的 `detail` 必须是**非空字符串**，不能是 FastAPI 默认的错误列表。"""
    response = await client.post(path, **kwargs)

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert not isinstance(detail, list), f"detail 落回了 FastAPI 默认列表形状：{detail}"
    assert isinstance(detail, str) and detail.strip(), detail


# ---------------------------------------------------------------------------
# trace_id：body / 响应头 / 客户端透传 三处一致
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("GET", "/health", {}),
        ("GET", "/api/elders/not-a-uuid", {}),
        (
            "POST",
            "/api/sessions",
            {"content": b"{oops", "headers": {"Content-Type": "application/json"}},
        ),
        ("POST", "/api/audio/utterance", {"data": {"duration_ms": "1000"}}),
    ],
    ids=["200-health", "400-bad-path", "400-bad-json", "422-field"],
)
async def test_trace_id_echoed_and_consistent(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    kwargs: dict[str, Any],
) -> None:
    """带合法 `X-Trace-Id` 时，响应体 `trace_id` 与响应头都回显同一个值且不为 `-`。

    覆盖 200 / 400 / 422 三种响应：错误分支最容易漏掉 trace_id
    （修复前 auth/audio 的 422 连字段都没有）。
    """
    kwargs = dict(kwargs)
    headers = dict(kwargs.pop("headers", {}))
    headers[TRACE_HEADER] = _SUPPLIED_TRACE_ID

    response = await client.request(method, path, headers=headers, **kwargs)

    assert response.headers[TRACE_HEADER] == _SUPPLIED_TRACE_ID
    body = response.json()
    assert body["trace_id"] == _SUPPLIED_TRACE_ID, body
    assert body["trace_id"] != "-", "trace_id 占位符 '-' 不许出现在响应体里"


# ---------------------------------------------------------------------------
# 两条实现（全局 handler / ContractAPIRoute）的文案与码必须一致
# ---------------------------------------------------------------------------


async def test_error_detail_consistent_between_global_and_route_mapping(
    client: httpx.AsyncClient,
) -> None:
    """同一类错误在「普通 APIRouter」与「ContractAPIRoute」上必须给出同一个错误体。

    两份实现分叉时的典型症状：auth 返 400 `malformed request`、elders 返
    `invalid json`，前端按 code 分支就会漏一条。这里比对 `code` 与 `detail` 两项。
    """
    # 必须显式声明 `Content-Type: application/json`：不带这个头时 FastAPI 不把请求体
    # 当 JSON 解析，坏字节会落成 422（字段类型不符）而不是 400 —— 两条实现一致，
    # 但本用例比的是「同一种输入」下的文案，所以两个请求用同一组头。
    json_headers = {"Content-Type": "application/json"}
    global_400 = (await client.post("/api/sessions", content=b"{oops", headers=json_headers)).json()
    route_400 = (await client.post("/api/elders", content=b"{oops", headers=json_headers)).json()
    assert global_400["code"] == route_400["code"] == "BAD_REQUEST"
    assert global_400["detail"] == route_400["detail"]

    global_422 = (await client.post("/api/sessions", json={})).json()
    route_422 = (await client.post("/api/elders", json={})).json()
    assert global_422["code"] == route_422["code"] == "VALIDATION_ERROR"
    assert global_422["detail"] == route_422["detail"]


# ---------------------------------------------------------------------------
# 404：全局兜底（Lead 已修：`app/main.py` 改注册 `starlette.exceptions.HTTPException`）
# ---------------------------------------------------------------------------


async def test_unknown_route_404_returns_contract_body(client: httpx.AsyncClient) -> None:
    """不存在的路径 → 404，且响应体也应符合契约 §7（`code == "NOT_FOUND"`）。"""
    response = await client.get("/api/definitely-not-a-route")

    assert response.status_code == 404, response.text
    body = response.json()
    assert set(body) == _ERROR_KEYS, body
    assert body["code"] == "NOT_FOUND", body
    assert len(body["trace_id"]) == 32, body
