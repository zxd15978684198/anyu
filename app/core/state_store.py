"""统一运行时状态中心：版本号 + CAS 写入 + 写权限矩阵（迭代 7 · N24 · 讲义 §六）。

## 为什么需要这一层

`cascade`（身份级联）、`turn`（当前对话轮）、`media`（媒体会话）三组状态原先各自散落在
`app/audio/media_bridge.py` 与 `app/dialogue_runtime.py` 的进程内字典里：同一条连接上的
两条协程（例如 30s 超时任务与 `identity_confirm` 回调）能同时改同一份状态，谁最后写谁赢，
而且事后读日志看不出「被谁覆盖过」。§六 要求「冲突则重读后重算，**不静默覆盖**」——
要做到这件事只有一条路：状态**自带版本号**，写入方**必须声明自己读到的是哪一版**，
版本对不上就拒写、把当前版本交给它去重算。

## 本模块提供的三件事

1. **版本单调递增**：每个键一个 `version`，从 1 起，每次成功写入 +1，永不回退、永不重置。
   版本不存在独立的表/键里，而是与值同在一个 Redis HASH（`version` / `kind` / `value`），
   一次 CAS 只碰一个 key —— 版本与值不可能分裂成两种真相。
2. **CAS 写入**：``write(key, expected_version, value)``。``expected_version=0`` 表示
   「我认为这个键还不存在」（首次创建）。版本不一致抛 `StateVersionConflict`，**不写、
   不改值、不建键**，并把 `actual_version` 一并带出，调用方据此重读重算。
3. **写权限矩阵**：见 `WRITE_PERMISSIONS`。越权抛 `StateWriteForbidden`，并且**在碰 Redis
   之前**就抛 —— 越权请求连一次网络往返都不该花掉（也让「越权没副作用」可被用例证明）。

## 为什么用 Lua 而不是 WATCH/MULTI

`WATCH` 的语义是「事务期间有人动过就失败」，调用方要自己包 while 重试、处理 `WatchError`、
在重试前 `reset()`；在 asyncio 里还要额外处理管道对象的复用与关闭。而 Redis 执行 Lua 脚本
是**单线程原子**的：读版本 → 比版本 → 写值，中间不可能被另一条连接插进来，一次往返就拿到
结论。脚本见 `CAS_SCRIPT`，返回值约定：成功 = 新版本（≥1），冲突 = -1。TTL 也在同一个脚本里
续期，不会出现「值写了、过期没续上」的半截状态。

## 为什么 client 必须由外部注入

本项目 Redis 客户端只有**一个**建法：`app/main.py` 启动时建一次（
`app.main._build_dialogue_runtime`，`decode_responses=False`），`RedisTurnStore` 与本模块
共用它。所以本模块**不 import redis、不 from_url、不 ping、不持有连接池**——拿到什么用什么
（鸭子类型，只要求 `hget` / `hgetall` / `eval` 三个方法）。这样「第二套连接池」在结构上就
不可能被建出来，这条约束靠构造签名本身兜住，而不是靠注释提醒。

## 键与值

```
Redis key : {redis_key_prefix}:state:{kind}:{name}
HASH 字段 : version(int) / kind(str) / value(json str)
```

版本粒度 = 键粒度，即「某一类状态下的某一个对象」；`name` 由调用方决定（thread_id /
device_id / 会话 id），冒号之后的字符原样保留（`cascade:a:b` 的 name 就是 `a:b`）。
`kind` 既出现在 key 前缀（用于命名空间与权限判定），也写进 HASH 字段（用于 SCAN 出来的键
自证身份）。

值的形状限定为 JSON 对象（`Mapping[str, Any]`）：状态是「一组字段」而不是裸标量，
裸标量请自己包一层 —— 这样读取端永远不必猜「这个值是 dict 还是 str」。

## 本节点（N24）的边界

本文件**只提供能力，不接线**：把 `app/audio/media_bridge.py`（cascade 段）与
`app/dialogue_runtime.py`（turn 段）改成用本模块，是 N43 由 Lead 独占完成的事。
因此这里不 import 它们中的任何一个 —— 既避免反向依赖，也避免与热点文件的写冲突。
"""

from __future__ import annotations

import enum
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

logger = logging.getLogger(__name__)

#: Redis key 的第二段（`{prefix}:state:...`），与 `dialogue` / `weather` 等段位同一层级。
STATE_SEGMENT: Final[str] = "state"
#: kind 与 name 之间的分隔符，同时用于拼 Redis key。
KEY_SEPARATOR: Final[str] = ":"

#: HASH 字段名（三个都写死在常量里：它们是 Redis 侧的契约，不是实现细节）。
VERSION_FIELD: Final[str] = "version"
KIND_FIELD: Final[str] = "kind"
VALUE_FIELD: Final[str] = "value"

#: Lua 脚本的冲突哨兵值。用 -1 而不是 nil：nil 在 redis-py 里回到 Python 是 None，
#: 与「脚本写错了没返回」无法区分；-1 是合法版本号之外的唯一值，判定不会歧义。
CONFLICT_SENTINEL: Final[int] = -1


class StateKind(enum.StrEnum):
    """三组运行时状态（§六）。**闭集**：加第四类必须先改 `WRITE_PERMISSIONS`。

    取值就是 Redis key 里的段位文本，改它等于改存储格式（线上键会全部对不上），
    所以这里显式写字符串、不用 `auto()`。
    """

    CASCADE = "cascade"
    TURN = "turn"
    MEDIA = "media"


class StateStoreError(Exception):
    """状态中心所有错误的基类（调用方可以只 catch 这一个）。"""


class StateKeyError(StateStoreError, ValueError):
    """键不合法：不是 `<kind>:<name>`、kind 不在闭集、name 为空。

    继承 `ValueError` 是为了与「参数不合法」的常规语义对齐（`app/core/` 里
    `SecurityError` / `RuntimeContextError` 也是这个口径）。
    """

    def __init__(self, *, key: object, detail: str) -> None:
        self.key = key
        self.detail = detail
        super().__init__(f"状态键不合法：{key!r} —— {detail}")


class StateWriteForbidden(StateStoreError):
    """越权写：该组件不在这一类状态的写权限白名单里（见 `WRITE_PERMISSIONS`）。"""

    def __init__(self, *, kind: StateKind, component: str, allowed: frozenset[str]) -> None:
        self.kind = kind
        self.component = component
        self.allowed = allowed
        names = "、".join(sorted(allowed)) if allowed else "（无）"
        super().__init__(
            f"越权写：组件 {component!r} 无权写 {kind.value} 状态（允许写者：{names}）"
        )


class StateVersionConflict(StateStoreError):
    """CAS 失败：`expected_version` 与当前版本不一致。

    `actual_version` 是**冲突时的真实版本**（键不存在则为 0），调用方据此重读重算：

        record = await store.read(key)          # 拿到值 + 真实版本
        new_value = recompute(record.value)     # 基于最新值重算
        await store.write(key, record.version, new_value)   # 再试一次

    没有「自动重试」：重算逻辑属于业务（谁在写、为什么写），本模块替它重试只会把
    一次冲突变成一次静默覆盖。
    """

    def __init__(self, *, key: str, expected: int, actual: int) -> None:
        self.key = key
        self.expected_version = expected
        self.actual_version = actual
        super().__init__(
            f"状态版本冲突：key={key!r} expected={expected} actual={actual}"
            "（应重读后重算，不得静默覆盖）"
        )


class StateCorrupt(StateStoreError):
    """HASH 存在但内容不可信：版本不是正整数、value 不是 JSON 对象。

    为什么不「当作没有状态」继续：CAS 的全部意义是「我读到的那一版」。一旦键里的版本
    读不出来，再把它当成 version=0 重建，等于把一次数据损坏悄悄变成一次覆盖 —— 这正是
    本模块要防的事。宁可抛出来让调用方决定（通常：告警 + 主动重置并留痕）。
    """

    def __init__(self, *, key: str, detail: str) -> None:
        self.key = key
        self.detail = detail
        super().__init__(f"状态数据损坏：key={key!r} —— {detail}")


#: 写权限矩阵：状态类 → 唯一允许写入它的组件（硬编码，**不读配置**）。
#:
#: 为什么硬编码而不是配置项：「谁能改哪类状态」是安全属性，改它必须过代码评审与 diff，
#: 不能靠改 `.env` 或运维侧配置悄悄放宽。加一个写者 = 改这张表 = 一次可审计的提交。
#:
#: 归属依据：
#: - `cascade`：身份级联由 `media_bridge` 独占（A2↔A3 接线层，`_Cascade` 的所有者）；
#: - `turn`：当前轮由 `dialogue_runtime` 独占（它是一轮对话的唯一编排者）；
#: - `media`：媒体会话状态归 `media_bridge` —— 它是对外持有媒体会话引用的接线层，
#:   `app/audio/webrtc.py` 的 `MediaSession` 只是这份状态的**值来源**，不是统一状态
#:   中心的写方（写方一旦下探到 webrtc 层，barge-in 与级联就会出现两个写口）。
WRITE_PERMISSIONS: Final[Mapping[StateKind, frozenset[str]]] = MappingProxyType(
    {
        StateKind.CASCADE: frozenset({"media_bridge"}),
        StateKind.TURN: frozenset({"dialogue_runtime"}),
        StateKind.MEDIA: frozenset({"media_bridge"}),
    }
)

#: 矩阵里登记过的全部组件（构造时校验拼写：`"media-brigde"` 这种错字要立刻炸，
#: 而不是等到某次写入才以「越权」的形式出现）。
KNOWN_COMPONENTS: Final[frozenset[str]] = frozenset(
    component for owners in WRITE_PERMISSIONS.values() for component in owners
)

#: CAS 的原子内核。参数口径：
#:
#: - `KEYS[1]`：完整 Redis key（`{prefix}:state:{kind}:{name}`）
#: - `ARGV[1]`：expected_version（0 = 认为键不存在）
#: - `ARGV[2]`：value 的 JSON 文本
#: - `ARGV[3]`：kind 文本（写进 `kind` 字段，供 SCAN 出来的键自证身份）
#: - `ARGV[4]`：ttl_ms（0 = 不设过期）
#:
#: 返回值：成功 = 新版本（≥1）；冲突 = -1（详见 `CONFLICT_SENTINEL`）。
#:
#: 注意 `tonumber(... or '0')`：键不存在与「版本字段缺失」在这里都视作 0，与
#: `expected_version=0` 的语义一致；而「存在但版本非法」由 Python 侧的 `read()` 报
#: `StateCorrupt`，不交给脚本猜。
CAS_SCRIPT: Final[str] = """
local actual = tonumber(redis.call('HGET', KEYS[1], 'version') or '0')
local expected = tonumber(ARGV[1])
if actual ~= expected then
  return -1
end
local next_version = expected + 1
redis.call('HSET', KEYS[1], 'version', next_version, 'kind', ARGV[3], 'value', ARGV[2])
local ttl_ms = tonumber(ARGV[4])
if ttl_ms > 0 then
  redis.call('PEXPIRE', KEYS[1], ttl_ms)
end
return next_version
""".strip()


@dataclass(frozen=True, slots=True)
class StateRecord:
    """一次 `read` 的快照：属于哪一类、当前版本、值。

    `frozen` 只冻结字段绑定，`value` 这个 dict 本身仍可被调用方改 —— 它是**快照**，
    改它不会回写 Redis；要落回去必须走 `write()` 的 CAS。
    """

    key: str
    kind: StateKind
    version: int
    value: dict[str, Any]


def make_key(kind: StateKind, name: str) -> str:
    """拼逻辑键 `<kind>:<name>`；`name` 里可以有冒号，原样保留。"""
    if not isinstance(kind, StateKind):
        raise StateKeyError(key=kind, detail="kind 必须是 StateKind 成员")
    if not isinstance(name, str) or not name:
        raise StateKeyError(key=name, detail="name 必须是非空字符串")
    return f"{kind.value}{KEY_SEPARATOR}{name}"


def kind_of(key: str) -> StateKind:
    """取出逻辑键属于哪一类状态；非法键抛 `StateKeyError`（不猜、不兜底）。"""
    return _split_key(key)[0]


def _split_key(key: str) -> tuple[StateKind, str]:
    """`<kind>:<name>` → (kind, name)。只切第一刀，name 里的冒号保留。"""
    if not isinstance(key, str):
        raise StateKeyError(key=key, detail="键必须是字符串")
    kind_text, separator, name = key.partition(KEY_SEPARATOR)
    if not separator or not kind_text or not name:
        raise StateKeyError(key=key, detail="键必须形如 `<kind>:<name>`，且两段都非空")
    try:
        kind = StateKind(kind_text)
    except ValueError as exc:
        allowed = "、".join(member.value for member in StateKind)
        raise StateKeyError(key=key, detail=f"未知状态类 {kind_text!r}（只认 {allowed}）") from exc
    return kind, name


def _as_text(raw: object) -> str:
    """Redis 客户端两种口径都要认：`decode_responses=False` 时给 bytes（main.py 就是这种）。"""
    if isinstance(raw, bytes):
        return raw.decode("utf-8")
    if isinstance(raw, str):
        return raw
    return str(raw)  # pragma: no cover - redis-py 只会给 str 或 bytes


def _decode_version(raw: object, *, key: str) -> int:
    """版本字段 → int；缺失按 0（键还不存在）处理，非法值抛 `StateCorrupt`。"""
    if raw is None:
        return 0
    try:
        version = int(_as_text(raw))
    except (TypeError, ValueError) as exc:
        raise StateCorrupt(key=key, detail=f"version 字段不是整数：{raw!r}") from exc
    if version < 0:
        raise StateCorrupt(key=key, detail=f"version 字段为负：{version}")
    return version


def _decode_value(raw: object, *, key: str) -> dict[str, Any]:
    """值字段 → dict；坏 JSON / 非对象一律抛 `StateCorrupt`（见类文档）。"""
    if raw is None:
        raise StateCorrupt(key=key, detail="缺少 value 字段")
    try:
        payload = json.loads(_as_text(raw))
    except (TypeError, ValueError) as exc:
        raise StateCorrupt(key=key, detail="value 字段不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise StateCorrupt(key=key, detail=f"value 不是 JSON 对象，而是 {type(payload).__name__}")
    return {str(field): item for field, item in payload.items()}


class StateStore:
    """某一类状态（更准确地说：**某个组件视角下**的可写状态中心）。

    ```python
    store = StateStore(redis_client, component="media_bridge", key_prefix=settings.redis_key_prefix)
    record = await store.read(make_key(StateKind.CASCADE, device_id))   # None 或版本 + 值
    version = await store.write(make_key(StateKind.CASCADE, device_id), 0, {"resolved": False})
    ```

    一个实例只代表一个组件：同一个组件写两类状态时共用一个实例（矩阵按 kind 判定），
    两个组件各建各的实例。**不要**为了省一个对象而把 component 设成「两者都行」——
    那样矩阵就失去意义了。

    Args:
        client: 现有 Redis 异步客户端（`app/main.py` 那一个）。本类不会自己建客户端。
        component: 写者身份，必须在 `KNOWN_COMPONENTS` 里（拼错立即 `ValueError`）。
        key_prefix: Redis 键前缀；缺省时才惰性读 `Settings.redis_key_prefix`
            （与 `WeatherCache` 同一口径：import 期不碰配置）。
        ttl_seconds: 可选过期时间（每次成功写入续期，滑动 TTL）。缺省 None = 不设过期：
            级联/当前轮都是运行时会话态，接线方（N43）明确要过期时再传值即可。
    """

    def __init__(
        self,
        client: Any,
        *,
        component: str,
        key_prefix: str | None = None,
        ttl_seconds: int | None = None,
    ) -> None:
        if component not in KNOWN_COMPONENTS:
            known = "、".join(sorted(KNOWN_COMPONENTS))
            raise ValueError(f"未知组件 {component!r}：写权限矩阵只登记了 {known}")
        if ttl_seconds is not None and int(ttl_seconds) <= 0:
            raise ValueError("ttl_seconds 必须是正整数；不设过期请传 None")
        self._client = client
        self._component = component
        self._key_prefix_override = key_prefix
        self._resolved_prefix: str | None = None
        self._ttl_ms = 0 if ttl_seconds is None else int(ttl_seconds) * 1000

    # ---------------------------------------------------------------- 只读

    @property
    def component(self) -> str:
        """本实例的写者身份（越权判定用）。"""
        return self._component

    def redis_key(self, key: str) -> str:
        """逻辑键 → 完整 Redis key（`{prefix}:state:{kind}:{name}`）。"""
        kind, name = _split_key(key)
        return KEY_SEPARATOR.join((self._prefix(), STATE_SEGMENT, kind.value, name))

    async def read(self, key: str) -> StateRecord | None:
        """整条读回；键不存在返回 None（**不是**空记录）。"""
        kind, _ = _split_key(key)
        raw = await self._client.hgetall(self.redis_key(key))
        if not raw:
            return None
        fields = {_as_text(field): _as_text(value) for field, value in raw.items()}
        version = _decode_version(fields.get(VERSION_FIELD), key=key)
        if version == 0:
            raise StateCorrupt(key=key, detail="HASH 存在却没有正版本号")
        return StateRecord(
            key=key,
            kind=kind,
            version=version,
            value=_decode_value(fields.get(VALUE_FIELD), key=key),
        )

    async def version(self, key: str) -> int:
        """只读版本（少取一个 value 字段）；键不存在返回 0。"""
        _split_key(key)  # 非法键要在碰 Redis 之前拒绝
        raw = await self._client.hget(self.redis_key(key), VERSION_FIELD)
        return _decode_version(raw, key=key)

    # ---------------------------------------------------------------- 写入

    async def write(self, key: str, expected_version: int, value: Mapping[str, Any]) -> int:
        """CAS 写入，返回新版本；版本不符抛 `StateVersionConflict`（不写任何东西）。

        Args:
            key: 逻辑键（`make_key(...)` 拼出来的）。
            expected_version: 写入方**读到**的版本；0 表示「认为键不存在」。
            value: 新值（JSON 对象）；传进来后立刻序列化，不持有调用方的引用。

        Raises:
            StateKeyError: 键不合法。
            StateWriteForbidden: 本实例的组件无权写这一类状态（碰 Redis 之前）。
            StateVersionConflict: 版本不符。
            StateCorrupt: 冲突后回读时发现 HASH 已损坏。
            TypeError: value 不是 Mapping 或不可 JSON 序列化。
            ValueError: expected_version 为负。
        """
        kind, _ = _split_key(key)
        self._require_writer(kind)
        expected = int(expected_version)
        if expected < 0:
            raise ValueError("expected_version 不能为负：0 表示「该键尚不存在」")
        if not isinstance(value, Mapping):
            raise TypeError(f"value 必须是 Mapping（JSON 对象），收到 {type(value).__name__}")
        payload = json.dumps(dict(value), ensure_ascii=False)
        result = await self._client.eval(
            CAS_SCRIPT,
            1,
            self.redis_key(key),
            expected,
            payload,
            kind.value,
            self._ttl_ms,
        )
        new_version = int(result)
        if new_version < 0:
            actual = await self.version(key)
            raise StateVersionConflict(key=key, expected=expected, actual=actual)
        return new_version

    # ---------------------------------------------------------------- 内部

    def _require_writer(self, kind: StateKind) -> None:
        allowed = WRITE_PERMISSIONS.get(kind, frozenset())
        if self._component not in allowed:
            raise StateWriteForbidden(kind=kind, component=self._component, allowed=allowed)

    def _prefix(self) -> str:
        """键前缀：显式传入优先，否则惰性读配置（import 期不触发 Settings）。"""
        if self._resolved_prefix is None:
            prefix = self._key_prefix_override
            if prefix is None:
                from app.core.config import get_settings

                prefix = get_settings().redis_key_prefix
            self._resolved_prefix = prefix
        return self._resolved_prefix


__all__ = [
    "CAS_SCRIPT",
    "CONFLICT_SENTINEL",
    "KEY_SEPARATOR",
    "KIND_FIELD",
    "KNOWN_COMPONENTS",
    "STATE_SEGMENT",
    "VALUE_FIELD",
    "VERSION_FIELD",
    "WRITE_PERMISSIONS",
    "StateCorrupt",
    "StateKeyError",
    "StateKind",
    "StateRecord",
    "StateStore",
    "StateStoreError",
    "StateVersionConflict",
    "StateWriteForbidden",
    "kind_of",
    "make_key",
]
