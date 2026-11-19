"""状态中心的版本号 / CAS / 写权限矩阵用例（迭代 7 · N24 · 讲义 §六）。

不打真 Redis：`StateStore` 只依赖 `hget` / `hgetall` / `eval` 三个方法（鸭子类型），
`FakeRedis` 给出这三个方法的最小实现。其中 `eval` 是**唯一必须与真实脚本同步**的地方，
所以假客户端先把 `script` 与 `state_store.CAS_SCRIPT` 比一次：脚本改了而假客户端没跟，
用例立刻红，而不是悄悄在「假语义」上通过。

真实原子性由 Redis 保证（Lua 单线程执行），本文件能证明的是**调用方看得见的契约**：
版本从 1 起单调递增、版本不符不写不建键、越权在碰 Redis 之前被拒、坏状态不静默放行。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app.core import state_store
from app.core.state_store import (
    KNOWN_COMPONENTS,
    WRITE_PERMISSIONS,
    StateCorrupt,
    StateKeyError,
    StateKind,
    StateStore,
    StateVersionConflict,
    StateWriteForbidden,
    kind_of,
    make_key,
)

PREFIX = "anyu"
CASCADE_KEY = make_key(StateKind.CASCADE, "dev-0001")
TURN_KEY = make_key(StateKind.TURN, "thread-0001")
MEDIA_KEY = make_key(StateKind.MEDIA, "session-0001")


class FakeRedis:
    """`StateStore` 需要的三个方法的最小实现（HASH + EVAL）。

    - `bytes_mode=True` 复刻生产口径：`app/main.py` 用 `decode_responses=False` 建客户端，
      所以 `hget` / `hgetall` 给的是 bytes，读取端必须能认。
    - `eval` 的语义与 `CAS_SCRIPT` 逐行对齐：比版本 → 写 `version`/`kind`/`value` →
      `ttl_ms > 0` 时记录续期。失败路径**不建键**（真脚本在 return -1 之前也没有 HSET）。
    """

    def __init__(self, *, bytes_mode: bool = False) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.bytes_mode = bytes_mode
        self.eval_calls: list[tuple[Any, ...]] = []
        self.expires: dict[str, int] = {}

    async def hget(self, key: str, field: str) -> str | bytes | None:
        value = self.hashes.get(key, {}).get(field)
        if value is None:
            return None
        return value.encode("utf-8") if self.bytes_mode else value

    async def hgetall(self, key: str) -> dict[str, str] | dict[bytes, bytes]:
        fields = self.hashes.get(key, {})
        if self.bytes_mode:
            return {name.encode("utf-8"): value.encode("utf-8") for name, value in fields.items()}
        return dict(fields)

    async def eval(self, script: str, numkeys: int, *args: Any) -> int:
        assert script == state_store.CAS_SCRIPT, "CAS 脚本变了：假客户端必须同步改"
        assert numkeys == 1
        key, expected, payload, kind, ttl_ms = args
        self.eval_calls.append((key, expected, payload, kind, ttl_ms))
        fields = self.hashes.get(key)
        actual = int(fields["version"]) if fields is not None else 0
        if actual != int(expected):
            return -1
        next_version = actual + 1
        self.hashes.setdefault(key, {}).update(
            {
                state_store.VERSION_FIELD: str(next_version),
                state_store.KIND_FIELD: str(kind),
                state_store.VALUE_FIELD: str(payload),
            }
        )
        if int(ttl_ms) > 0:
            self.expires[key] = int(ttl_ms)
        return next_version


def _media_bridge(client: FakeRedis, **kwargs: Any) -> StateStore:
    return StateStore(client, component="media_bridge", key_prefix=PREFIX, **kwargs)


def _dialogue_runtime(client: FakeRedis, **kwargs: Any) -> StateStore:
    return StateStore(client, component="dialogue_runtime", key_prefix=PREFIX, **kwargs)


class TestCasWrites:
    """版本从 0（不存在）起步、每次成功写入 +1，冲突不写不建键。"""

    async def test_create_starts_at_version_one(self) -> None:
        client = FakeRedis()
        store = _media_bridge(client)

        assert await store.write(CASCADE_KEY, 0, {"resolved": False}) == 1

        record = await store.read(CASCADE_KEY)
        assert record is not None
        assert record.kind is StateKind.CASCADE
        assert record.version == 1
        assert record.value == {"resolved": False}
        # 存储契约（SCAN 出来的键要能自证身份）：三个字段都落盘
        assert client.hashes[store.redis_key(CASCADE_KEY)] == {
            "version": "1",
            "kind": "cascade",
            "value": '{"resolved": false}',
        }

    async def test_version_is_monotonic_across_writes(self) -> None:
        store = _media_bridge(FakeRedis())

        versions = [
            await store.write(CASCADE_KEY, expected, {"n": expected}) for expected in range(4)
        ]

        assert versions == [1, 2, 3, 4]
        record = await store.read(CASCADE_KEY)
        assert record is not None
        assert record.version == 4
        assert record.value == {"n": 3}

    async def test_stale_expected_version_conflicts_without_overwrite(self) -> None:
        store = _media_bridge(FakeRedis())
        await store.write(CASCADE_KEY, 0, {"stage": "voice"})

        with pytest.raises(StateVersionConflict) as excinfo:
            await store.write(CASCADE_KEY, 0, {"stage": "confirm"})

        assert excinfo.value.expected_version == 0
        assert excinfo.value.actual_version == 1
        assert excinfo.value.key == CASCADE_KEY
        # 冲突 = 什么都没写：值、版本都不动
        record = await store.read(CASCADE_KEY)
        assert record is not None
        assert record.value == {"stage": "voice"}
        assert await store.version(CASCADE_KEY) == 1

    async def test_missing_key_with_nonzero_expected_conflicts(self) -> None:
        store = _media_bridge(FakeRedis())

        with pytest.raises(StateVersionConflict) as excinfo:
            await store.write(CASCADE_KEY, 3, {"whatever": True})

        assert excinfo.value.actual_version == 0
        assert await store.read(CASCADE_KEY) is None  # 失败路径不建键

    async def test_only_one_of_two_concurrent_writes_wins(self) -> None:
        store = _media_bridge(FakeRedis())
        await store.write(CASCADE_KEY, 0, {"who": "seed"})

        results = await asyncio.gather(
            store.write(CASCADE_KEY, 1, {"who": "a"}),
            store.write(CASCADE_KEY, 1, {"who": "b"}),
            return_exceptions=True,
        )

        succeeded = [item for item in results if isinstance(item, int)]
        conflicts = [item for item in results if isinstance(item, StateVersionConflict)]
        assert succeeded == [2]
        assert len(conflicts) == 1
        assert conflicts[0].actual_version == 2
        assert await store.version(CASCADE_KEY) == 2

    async def test_negative_expected_version_rejected(self) -> None:
        store = _media_bridge(FakeRedis())

        with pytest.raises(ValueError, match="expected_version"):
            await store.write(CASCADE_KEY, -1, {})


class TestWritePermissions:
    """矩阵：cascade 仅 media_bridge、turn 仅 dialogue_runtime、media 归 media_bridge。"""

    def test_matrix_covers_every_kind(self) -> None:
        assert set(WRITE_PERMISSIONS) == set(StateKind)

    async def test_cascade_owned_by_media_bridge(self) -> None:
        client = FakeRedis()
        assert await _media_bridge(client).write(CASCADE_KEY, 0, {"ok": True}) == 1
        with pytest.raises(StateWriteForbidden):
            await _dialogue_runtime(client).write(CASCADE_KEY, 1, {"ok": False})

    async def test_turn_owned_by_dialogue_runtime(self) -> None:
        client = FakeRedis()
        assert await _dialogue_runtime(client).write(TURN_KEY, 0, {"clarified": False}) == 1
        with pytest.raises(StateWriteForbidden):
            await _media_bridge(client).write(TURN_KEY, 1, {"clarified": True})

    async def test_media_owned_by_media_bridge(self) -> None:
        client = FakeRedis()
        assert await _media_bridge(client).write(MEDIA_KEY, 0, {"state": "connected"}) == 1
        with pytest.raises(StateWriteForbidden):
            await _dialogue_runtime(client).write(MEDIA_KEY, 1, {"state": "closed"})

    async def test_forbidden_error_carries_kind_component_and_allowed(self) -> None:
        store = _dialogue_runtime(FakeRedis())

        with pytest.raises(StateWriteForbidden) as excinfo:
            await store.write(CASCADE_KEY, 0, {})

        assert excinfo.value.kind is StateKind.CASCADE
        assert excinfo.value.component == "dialogue_runtime"
        assert excinfo.value.allowed == frozenset({"media_bridge"})
        assert "media_bridge" in str(excinfo.value)

    async def test_forbidden_write_never_touches_redis(self) -> None:
        client = FakeRedis()
        store = _dialogue_runtime(client)

        with pytest.raises(StateWriteForbidden):
            await store.write(CASCADE_KEY, 0, {"x": 1})

        assert client.eval_calls == []  # 越权连一次往返都不花
        assert await store.read(CASCADE_KEY) is None

    def test_unknown_component_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="未知组件"):
            StateStore(FakeRedis(), component="media-brigde", key_prefix=PREFIX)

    def test_known_components_is_matrix_union(self) -> None:
        assert frozenset({"media_bridge", "dialogue_runtime"}) == KNOWN_COMPONENTS


class TestKeys:
    def test_make_key_roundtrip(self) -> None:
        assert make_key(StateKind.TURN, "thread-1") == "turn:thread-1"
        assert kind_of("turn:thread-1") is StateKind.TURN

    @pytest.mark.parametrize(
        "bad",
        [
            pytest.param("cascade", id="no-separator"),
            pytest.param("", id="empty"),
            pytest.param(":name", id="empty-kind"),
            pytest.param("cascade:", id="empty-name"),
            pytest.param("unknown:name", id="unknown-kind"),
            pytest.param("CASCADE:name", id="case-sensitive"),
            pytest.param(None, id="not-a-string"),
        ],
    )
    def test_bad_key_rejected(self, bad: Any) -> None:
        with pytest.raises(StateKeyError):
            kind_of(bad)

    def test_redis_key_layout(self) -> None:
        store = _media_bridge(FakeRedis())

        assert store.redis_key(CASCADE_KEY) == "anyu:state:cascade:dev-0001"

    def test_name_keeps_extra_colons(self) -> None:
        store = _media_bridge(FakeRedis())

        assert store.redis_key("cascade:dev:001") == "anyu:state:cascade:dev:001"

    def test_prefix_falls_back_to_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "app.core.config.get_settings",
            lambda: SimpleNamespace(redis_key_prefix="cfg-prefix"),
        )
        store = StateStore(FakeRedis(), component="media_bridge")

        assert store.redis_key(TURN_KEY) == "cfg-prefix:state:turn:thread-0001"


class TestPayloadAndIntegrity:
    async def test_value_roundtrip_keeps_nested_and_chinese(self) -> None:
        store = _media_bridge(FakeRedis())
        payload = {"name": "张桂芬", "nested": {"candidates": [1, 2]}}

        await store.write(CASCADE_KEY, 0, payload)

        record = await store.read(CASCADE_KEY)
        assert record is not None
        assert record.value == payload

    async def test_value_must_be_mapping(self) -> None:
        store = _media_bridge(FakeRedis())

        with pytest.raises(TypeError, match="Mapping"):
            await store.write(CASCADE_KEY, 0, ["not", "a", "map"])  # type: ignore[arg-type]

    async def test_bytes_client_is_decoded(self) -> None:
        store = _dialogue_runtime(FakeRedis(bytes_mode=True))

        await store.write(TURN_KEY, 0, {"clarified": True})

        record = await store.read(TURN_KEY)
        assert record is not None
        assert record.version == 1
        assert record.value == {"clarified": True}
        assert await store.version(TURN_KEY) == 1

    async def test_missing_key_reads_none_and_version_zero(self) -> None:
        store = _media_bridge(FakeRedis())

        assert await store.read(CASCADE_KEY) is None
        assert await store.version(CASCADE_KEY) == 0

    async def test_corrupt_version_is_reported_not_ignored(self) -> None:
        client = FakeRedis()
        store = _media_bridge(client)
        client.hashes[store.redis_key(CASCADE_KEY)] = {"version": "abc", "value": "{}"}

        with pytest.raises(StateCorrupt):
            await store.read(CASCADE_KEY)

    async def test_hash_without_version_is_reported(self) -> None:
        client = FakeRedis()
        store = _media_bridge(client)
        client.hashes[store.redis_key(CASCADE_KEY)] = {"value": "{}"}

        with pytest.raises(StateCorrupt):
            await store.read(CASCADE_KEY)

    async def test_corrupt_value_is_reported(self) -> None:
        client = FakeRedis()
        store = _media_bridge(client)
        client.hashes[store.redis_key(CASCADE_KEY)] = {"version": "1", "value": "{not-json"}

        with pytest.raises(StateCorrupt):
            await store.read(CASCADE_KEY)

    async def test_ttl_slides_only_when_configured(self) -> None:
        client = FakeRedis()
        await _media_bridge(client).write(CASCADE_KEY, 0, {})
        assert client.expires == {}

        with_ttl = _media_bridge(client, ttl_seconds=90)
        await with_ttl.write(CASCADE_KEY, 1, {})

        assert client.expires == {with_ttl.redis_key(CASCADE_KEY): 90_000}

    def test_non_positive_ttl_rejected(self) -> None:
        with pytest.raises(ValueError, match="ttl_seconds"):
            _media_bridge(FakeRedis(), ttl_seconds=0)
