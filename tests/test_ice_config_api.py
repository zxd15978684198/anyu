"""W2 · `GET /api/audio/ice-config` 的接口级测试。

不连数据库、不起 uvicorn：只把 `app/api/ice.py` 的 router 挂到一个临时 FastAPI 实例上
（**不改** `app/api/__init__.py`，挂载由 Lead 做），用 `dependency_overrides` 顶掉
`get_session` 与 `get_current_principal`。无 token 用例**不覆盖** `get_current_principal`，
走真实鉴权链 —— 401 是它给出的，不是测试假装的。

覆盖卡面要求的六类：设备 token 200 且 host 来自请求头、缺 `turn_credential` 只给 STUN、
全空给 `[]`、家属/员工 403、无 token 401、`generated_at` 是 UTC `Z`。
另外补三条守边界的用例：loopback 不过滤（W2 的核心诉求）、`X-Forwarded-Host` 优先、
端口/ TTL 真的取自配置。
"""

from __future__ import annotations

import datetime
import re
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.ice import router
from app.core.auth import get_current_principal
from app.core.config import get_settings
from app.core.runtime_context import Identity
from app.db import get_session

DEVICE_ID = "a1111111-1111-4111-8111-111111111111"
STAFF_ID = "a2222222-2222-4222-8222-222222222222"
GUARDIAN_ID = "a3333333-3333-4333-8333-333333333333"

DEVICE = Identity(kind="device", device_id=DEVICE_ID)
STAFF = Identity(kind="staff", staff_id=STAFF_ID, role="admin")
GUARDIAN = Identity(kind="guardian", guardian_id=GUARDIAN_ID)

#: 一份「TURN 完整」的配置。`turn_url` 故意用 localhost：服务端自用时会过滤 loopback，
#: 而下发给浏览器时**必须**保留 —— 这正是本卡要守的行为。
FULL_ENV: dict[str, str] = {
    "STUN_URL": "stun:localhost:3478",
    "TURN_URL": "turn:localhost:3478",
    "TURN_USERNAME": "anyu",
    "TURN_CREDENTIAL": "test-turn-secret",
    "TURN_CREDENTIAL_TTL_SECONDS": "3600",
    "TURN_PORT": "3478",
}

_GENERATED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_ERROR_KEYS = {"detail", "code", "trace_id"}


@contextmanager
def _client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    env: dict[str, str] | None = None,
    identity: Identity | None = DEVICE,
) -> Iterator[TestClient]:
    """装配「只有 ice router」的应用。

    `identity=None` 时不覆盖 `get_current_principal`，走真实鉴权链（无 token 用例）。
    `get_session` 一律顶成空对象：本端点不读库，但鉴权依赖把它声明成了子依赖。
    """
    merged = dict(FULL_ENV)
    merged.update(env or {})
    for key, value in merged.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()

    application = FastAPI()
    application.include_router(router)

    async def _no_db_session() -> AsyncIterator[object]:
        yield object()

    application.dependency_overrides[get_session] = _no_db_session
    if identity is not None:
        application.dependency_overrides[get_current_principal] = lambda: identity
    try:
        yield TestClient(application)
    finally:
        get_settings.cache_clear()


def _assert_utc_z(value: str) -> None:
    """`generated_at` 必须是 UTC、秒级、`Z` 结尾，且是「刚刚」。"""
    assert _GENERATED_AT.match(value), value
    parsed = datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.UTC)
    now = datetime.datetime.now(datetime.UTC)
    assert abs((now - parsed).total_seconds()) < 60, value


# ------------------------------------------------------------------ 200 正常路径


def test_device_token_gets_host_from_request_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """设备 token → 200；URL 里的 host 来自**请求头**，不是硬编码的 localhost。"""
    with _client(monkeypatch) as client:
        resp = client.get("/api/audio/ice-config", headers={"host": "pad.room301.example:8443"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ice_servers"] == [
        {
            "urls": [
                "stun:pad.room301.example:3478",
                "turn:pad.room301.example:3478",
                "turns:pad.room301.example:5349?transport=tcp",
            ],
            "username": "anyu",
            "credential": "test-turn-secret",
        }
    ]
    assert body["ttl_seconds"] == 3600
    _assert_utc_z(body["generated_at"])


def test_loopback_host_is_not_filtered_for_the_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """host=localhost 时 TURN **照发**：loopback 过滤只属于服务端自连的 `ice_servers()`。

    删掉 `ice_servers_for_client` 里的「不过滤 loopback」这一决定（例如图省事复用
    `ice_servers()`），本用例立刻变红 —— 本机联调就只剩 STUN。
    """
    with _client(monkeypatch) as client:
        resp = client.get("/api/audio/ice-config", headers={"host": "localhost"})

    assert resp.status_code == 200, resp.text
    server = resp.json()["ice_servers"][0]
    assert server["urls"] == [
        "stun:localhost:3478",
        "turn:localhost:3478",
        # 2026-11-09 起同时下发 TURNS：证书受信走 TLS，不受信自动回落明文 TURN。
        "turns:localhost:5349?transport=tcp",
    ]
    assert server["username"] == "anyu"


def test_forwarded_host_wins_over_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """反代场景：`Host` 是容器名，`X-Forwarded-Host` 才是浏览器认的域名。"""
    with _client(monkeypatch) as client:
        resp = client.get(
            "/api/audio/ice-config",
            headers={
                "host": "backend:8000",
                "x-forwarded-host": "sm.example.com, proxy.internal",
            },
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["ice_servers"][0]["urls"] == [
        "stun:sm.example.com:3478",
        "turn:sm.example.com:3478",
        "turns:sm.example.com:5349?transport=tcp",
    ]


def test_host_port_is_stripped_and_turn_port_comes_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`localhost:5173` → `localhost`；端口取自 `turn_port` 而不是请求头的端口。"""
    with _client(monkeypatch, env={"TURN_PORT": "9443"}) as client:
        resp = client.get("/api/audio/ice-config", headers={"host": "localhost:5173"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["ice_servers"][0]["urls"] == [
        "stun:localhost:9443",
        "turn:localhost:9443",
        # TURNS 端口独立于 turn_port：改 3478→9443 不应连带改 TLS 端口。
        "turns:localhost:5349?transport=tcp",
    ]


def test_ttl_seconds_comes_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    with _client(monkeypatch, env={"TURN_CREDENTIAL_TTL_SECONDS": "120"}) as client:
        resp = client.get("/api/audio/ice-config", headers={"host": "localhost"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["ttl_seconds"] == 120


# ------------------------------------------------------------------ 降级路径


@pytest.mark.parametrize(
    ("missing_key", "empty_value"),
    [
        ("TURN_CREDENTIAL", {"TURN_CREDENTIAL": ""}),
        ("TURN_USERNAME", {"TURN_USERNAME": ""}),
        ("TURN_URL", {"TURN_URL": ""}),
    ],
)
def test_turn_disabled_returns_stun_only(
    monkeypatch: pytest.MonkeyPatch,
    missing_key: str,
    empty_value: dict[str, str],
) -> None:
    """TURN 三项任一为空 → 只给 STUN，且**不带** username/credential 键。"""
    with _client(monkeypatch, env=empty_value) as client:
        resp = client.get("/api/audio/ice-config", headers={"host": "pad.local"})

    assert resp.status_code == 200, f"{missing_key} 为空时应降级而不是报错: {resp.text}"
    body = resp.json()
    assert body["ice_servers"] == [{"urls": ["stun:pad.local:3478"]}], body
    assert "username" not in body["ice_servers"][0]
    assert "credential" not in body["ice_servers"][0]


def test_all_empty_returns_empty_list_and_still_200(monkeypatch: pytest.MonkeyPatch) -> None:
    """全空 → `ice_servers: []`（前端无参建连），**不抛错**。"""
    with _client(
        monkeypatch,
        env={
            "STUN_URL": "",
            "TURN_URL": "",
            "TURN_USERNAME": "",
            "TURN_CREDENTIAL": "",
        },
    ) as client:
        resp = client.get("/api/audio/ice-config", headers={"host": "pad.local"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ice_servers"] == []
    assert body["ttl_seconds"] == 3600
    _assert_utc_z(body["generated_at"])


def test_stun_off_turn_on_returns_turn_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """STUN 关、TURN 全 → 只给 TURN（另一侧的降级不能被写成「一律先 STUN」）。"""
    with _client(monkeypatch, env={"STUN_URL": ""}) as client:
        resp = client.get("/api/audio/ice-config", headers={"host": "pad.local"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["ice_servers"] == [
        {
            "urls": [
                "turn:pad.local:3478",
                "turns:pad.local:5349?transport=tcp",
            ],
            "username": "anyu",
            "credential": "test-turn-secret",
        }
    ]


# ------------------------------------------------------------------ 鉴权拒绝


@pytest.mark.parametrize(
    ("identity", "label"),
    [(GUARDIAN, "家属"), (STAFF, "员工")],
)
def test_non_device_token_is_403(
    monkeypatch: pytest.MonkeyPatch, identity: Identity, label: str
) -> None:
    """家属/员工持有效凭证 → 403 `ROLE_FORBIDDEN`（不是 401：重登也换不到设备权限）。"""
    with _client(monkeypatch, identity=identity) as client:
        resp = client.get("/api/audio/ice-config", headers={"host": "pad.local"})

    assert resp.status_code == 403, f"{label}令牌不应拿到 ICE 配置: {resp.text}"
    body: dict[str, Any] = resp.json()
    assert set(body) == _ERROR_KEYS, body
    assert body["code"] == "ROLE_FORBIDDEN", body
    assert isinstance(body["detail"], str) and body["detail"].strip(), body


def test_missing_token_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """无凭证 → 401 `TOKEN_INVALID`，由真实鉴权链给出（本用例不覆盖 principal 依赖）。"""
    with _client(monkeypatch, identity=None) as client:
        resp = client.get("/api/audio/ice-config", headers={"host": "pad.local"})

    assert resp.status_code == 401, resp.text
    body: dict[str, Any] = resp.json()
    assert set(body) == _ERROR_KEYS, body
    assert body["code"] == "TOKEN_INVALID", body


def test_turns_is_omitted_when_tls_port_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """`TURN_TLS_PORT=0` = 明确不要 turns:（老 coturn 没起 TLS 时用）。"""
    with _client(monkeypatch, env={"TURN_TLS_PORT": "0"}) as client:
        resp = client.get("/api/audio/ice-config", headers={"host": "localhost"})

    assert resp.status_code == 200, resp.text
    urls = resp.json()["ice_servers"][0]["urls"]
    assert urls == ["stun:localhost:3478", "turn:localhost:3478"]
    assert not any(url.startswith("turns:") for url in urls)


def test_turns_port_follows_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """TLS 端口可配：反代只放行某个端口时不必改代码。"""
    with _client(monkeypatch, env={"TURN_TLS_PORT": "5443"}) as client:
        resp = client.get("/api/audio/ice-config", headers={"host": "sm.example.com"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["ice_servers"][0]["urls"] == [
        "stun:sm.example.com:3478",
        "turn:sm.example.com:3478",
        "turns:sm.example.com:5443?transport=tcp",
    ]
