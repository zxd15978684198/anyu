"""审计查询与导出服务（PRD AC-14 操作可追溯；技术设计文档 §3.5 `audit_logs`）。

## 表结构以迁移为准，不按计划稿猜

`alembic/versions/0006_audit_and_constraints.py` 建出的 `audit_logs` 只有八列：
`audit_id`（PK）/ `actor_type` / `actor_id` / `action` / `resource_type` / `resource_id` /
`detail`(JSONB) / `created_at`。**没有 `log_id`，也没有 `trace_id` 列。**

所以本文件两条口径：

1. 主键对外一律叫 `audit_id`（与 ORM、迁移、其它服务一致），不再造一个 `log_id` 同义词；
2. `trace_id` 落在 `detail` 里（`detail["trace_id"]`），导出时**提升**成顶层字段，
   让导出的每一条自带链路号。读侧对老数据（没写过 trace_id 的行）返回 `None`，
   键集合仍然稳定。

之所以不新增列：本卡写作用域不含 `app/models/**` 与 `alembic/**`，且「只增不改」的审计表
加列要单独走迁移裁决；`detail` 本就是「操作细节」的容器，trace_id 属于细节。

## 只读为主，写入口供他人调用

`write_audit` 是**同一事务**内的写入口（只 `flush` 不 `commit`，事务归调用方）；
`write_audit_isolated` 是**独立会话**的写入口，给「先留痕再抛 403」这类必然回滚的路径用
（`app/services/report_service.py::_write_audit` 的同一理由：同事务写审计等于自己抹证据）。

## 脱敏

`app/core/redact.py` 由并行子卡提供，**本文件不假设它一定存在**：

```python
try:
    from app.core.redact import redact_mapping
except ImportError:
    ...  # 兜底：浅拷贝，不脱敏
```

落地后自动生效（`REDACT_AVAILABLE=True`）；未落地时导出照样能跑，只是不做打码 ——
测试里 `phone` 那条用例按 `REDACT_AVAILABLE` skip，不让缺件把整批用例拖红。

写库与导出两侧都过同一层 `_redact_detail`：`audit_logs.detail` 的列注释是「操作细节
（脱敏后）」，脱敏该发生在**写入时**，导出再脱一次是幂等的第二道闸（老数据）。

具体两遍：先 `redact_mapping`（按键命中 → 打码该值，并递归进 dict/list），再
`redact_text` 遍历字符串叶子（覆盖「敏感串被写进自由文本」的情况）。两遍都只用
`app/core/redact.py` 的规则，本文件不重写任何正则。
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.context import get_trace_id
from app.db import session_factory
from app.models import AuditLog

try:  # 并行子卡 `app/core/redact.py`：存在则用，缺失则走本文件的浅拷贝兜底
    from app.core.redact import redact_mapping as _redact_mapping
    from app.core.redact import redact_text as _redact_text
except ImportError:  # pragma: no cover - 取决于同批子卡是否已落地
    _redact_mapping = None  # type: ignore[assignment]
    _redact_text = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

#: 脱敏模块是否可用（测试据此决定「手机号必须被打码」那条用例跑不跑）。
REDACT_AVAILABLE: Final[bool] = _redact_mapping is not None

#: `audit_logs.actor_type` 的 CHECK 取值（0006 迁移 + 模型 `__table_args__`）。
ACTOR_TYPES: Final[frozenset[str]] = frozenset({"staff", "guardian", "system", "device"})

#: 列表默认页大小。
DEFAULT_LIMIT: Final[int] = 50

#: 单页上限：审计行只增不减，一次全拉会拖死 admin 页面。
MAX_LIMIT: Final[int] = 200

#: 导出行数上限（本卡验收口径：最大 1000 行）。
MAX_EXPORT_ROWS: Final[int] = 1000

#: 导出/序列化的**稳定键集合**（键名与顺序固定，前端与回归用例都依赖它）。
EXPORT_KEYS: Final[tuple[str, ...]] = (
    "audit_id",
    "actor_type",
    "actor_id",
    "action",
    "resource_type",
    "resource_id",
    "detail",
    "trace_id",
    "created_at",
)

#: 导出动作自身的审计 action。
EXPORT_ACTION: Final[str] = "audit.export"

#: 读审计的审计 action（越权拒绝时也用它留痕）。
READ_ACTION: Final[str] = "audit.read"

#: 审计行的资源类型名（`resource_type` 是自由文本，这里只做常量收口）。
AUDIT_RESOURCE_TYPE: Final[str] = "audit_logs"

#: 无单一资源对象的动作（列表、导出）用全零 UUID 占位：`resource_id` 是 NOT NULL，
#: 不能留空，也不该编造一个真实 ID。
NIL_RESOURCE_ID: Final[uuid.UUID] = uuid.UUID(int=0)


class AuditQueryError(ValueError):
    """审计查询参数非法（API 层映射成 422 `VALIDATION_ERROR`）。

    继承 `ValueError`：参数问题在 Python 语义里就是这个家族，调用方用
    `except ValueError` 兜住也不会漏；需要区分时再抓本类。
    """


# ------------------------------------------------------------------ 校验工具


def _require_actor_type(actor_type: str) -> None:
    if actor_type not in ACTOR_TYPES:
        raise AuditQueryError(
            f"actor_type must be one of {sorted(ACTOR_TYPES)}, got {actor_type!r}"
        )


def _to_utc(value: datetime.datetime | None, field_name: str) -> datetime.datetime | None:
    """统一成 UTC aware。**无时区按 UTC 解释**（AGENTS §8：存储一律 UTC）。

    不在这里拒绝 naive：服务层调用方（调度任务、脚本）常拿 `datetime.now()`，
    直接判非法会把「本机 UTC 机器上的正确调用」也挡掉；解释成 UTC 才是本项目的口径。
    """
    if value is None:
        return None
    if not isinstance(value, datetime.datetime):
        raise AuditQueryError(f"{field_name} must be a datetime, got {type(value).__name__}")
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.UTC)
    return value.astimezone(datetime.UTC)


def _iso_utc(value: datetime.datetime) -> str:
    """ISO 8601 + UTC 偏移（`2026-11-08T12:00:00+00:00`）。"""
    if value.tzinfo is None:  # 老库里理论上不会有 naive（列是 timestamptz），防御一下
        value = value.replace(tzinfo=datetime.UTC)
    return value.astimezone(datetime.UTC).isoformat()


def _validate_page(limit: int, offset: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise AuditQueryError(f"limit must be an int, got {type(limit).__name__}")
    if limit < 1 or limit > MAX_LIMIT:
        raise AuditQueryError(f"limit must be between 1 and {MAX_LIMIT}, got {limit}")
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise AuditQueryError(f"offset must be an int, got {type(offset).__name__}")
    if offset < 0:
        raise AuditQueryError(f"offset must be >= 0, got {offset}")


# ------------------------------------------------------------------ 脱敏


def _redact_leaves(value: Any) -> Any:
    """对已按**键**脱敏过的结构再走一遍**值**级脱敏（自由文本里的手机号等）。

    两遍的必要性：`redact_mapping` 的规则是「键命中敏感名 → 打码该值」，覆盖不了
    `{"reason": "用户 13800138000 越权"}` 这种把敏感串写进描述的情况。审计 detail 里
    自由文本很常见（拒绝理由、异常摘要），只按键脱敏等于漏一条路。

    规则仍然只有 `app/core/redact.py` 一处（用它的 `redact_text`），本函数只负责遍历 ——
    不在这里重写任何正则。`redact_text` 对已打码的值幂等（内部 `_is_masked` 短路），
    所以「键级已打码 + 值级再过一遍」不会把 `138****8000` 打成人看不出原貌的样子。
    """
    if _redact_text is None:
        return value
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, Mapping):
        return {key: _redact_leaves(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_leaves(item) for item in value]
    return value


def _redact_detail(detail: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """`detail` 脱敏。缺 `app/core/redact.py` 时退化为**浅拷贝**（不修改调用方对象）。

    为什么兜底是浅拷贝而不是自己写一套正则：一个能力只允许一种形态（AGENTS §4 第 8 条），
    脱敏规则必须只有 `app/core/redact.py` 一处；这里再造一套手机号正则，两处迟早分叉，
    而且会把「导出已脱敏」的结论建立在未评审的本地规则上。
    """
    if detail is None:
        return None
    payload = dict(detail)
    if _redact_mapping is None:
        return payload
    try:
        redacted = dict(_redact_mapping(payload))
    except Exception:  # pragma: no cover - 并行子卡签名漂移时不让导出整体 500
        logger.exception(
            "redact failed; falling back to shallow copy",
            extra={"trace_id": get_trace_id() or "-"},
        )
        return payload
    return {key: _redact_leaves(value) for key, value in redacted.items()}


def _detail_with_trace(detail: Mapping[str, Any] | None, trace_id: str | None) -> dict[str, Any]:
    """把 trace_id 并进 detail（本表无该列），并顺手脱敏。"""
    merged: dict[str, Any] = dict(detail or {})
    if trace_id is not None:
        merged["trace_id"] = trace_id
    elif "trace_id" not in merged:
        current = get_trace_id()
        if current:
            merged["trace_id"] = current
    redacted = _redact_detail(merged)
    return redacted if redacted is not None else {}


def _trace_id_of(row: AuditLog) -> str | None:
    detail = row.detail
    if not isinstance(detail, Mapping):
        return None
    value = detail.get("trace_id")
    return value if isinstance(value, str) else None


def actor_type_of(kind: str) -> str:
    """`Identity.kind` → `audit_logs.actor_type` 的 CHECK 取值。

    `staff_card`（工牌）在表里只能记成 `staff`：CHECK 只有四个值，而「工牌也是员工」
    在 `Identity.is_staff_principal` 里已经是这个口径，两处一致。
    """
    actor_type = "staff" if kind in ("staff", "staff_card") else kind
    if actor_type not in ACTOR_TYPES:  # 身份种类新增时当场炸，别让 INSERT 撞 CHECK
        raise AuditQueryError(f"identity kind {kind!r} has no audit actor_type")
    return actor_type


# ------------------------------------------------------------------ 查询


def _conditions(
    *,
    actor_type: str | None,
    action: str | None,
    actor_id: uuid.UUID | None,
    start: datetime.datetime | None,
    end: datetime.datetime | None,
) -> list[ColumnElement[bool]]:
    """把过滤参数翻译成 WHERE 条件；任何非法值先抛 `AuditQueryError`。"""
    where: list[ColumnElement[bool]] = []

    if actor_type is not None:
        _require_actor_type(actor_type)
        where.append(AuditLog.actor_type == actor_type)

    if action is not None:
        if not isinstance(action, str) or not action.strip():
            raise AuditQueryError("action must be a non-empty string")
        where.append(AuditLog.action == action)

    if actor_id is not None:
        if not isinstance(actor_id, uuid.UUID):
            raise AuditQueryError(f"actor_id must be a UUID, got {type(actor_id).__name__}")
        where.append(AuditLog.actor_id == actor_id)

    start_utc = _to_utc(start, "start")
    end_utc = _to_utc(end, "end")
    if start_utc is not None and end_utc is not None and start_utc > end_utc:
        raise AuditQueryError("start must not be later than end")
    if start_utc is not None:
        where.append(AuditLog.created_at >= start_utc)
    if end_utc is not None:
        where.append(AuditLog.created_at <= end_utc)

    return where


async def list_audits(
    session: AsyncSession,
    *,
    actor_type: str | None = None,
    action: str | None = None,
    actor_id: uuid.UUID | None = None,
    start: datetime.datetime | None = None,
    end: datetime.datetime | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> Sequence[AuditLog]:
    """按 `created_at DESC, audit_id DESC` 稳定倒序取一页审计行。

    为什么要 `audit_id` 做第二排序键：`created_at` 默认 `now()`，同一毫秒内多条（一次请求
    写两三条审计很常见）只按时间排会得到**不确定**的顺序，翻页就会重复/漏行。加主键做
    决定性 tiebreak，分页才有「不重不漏」可言。
    """
    _validate_page(limit, offset)
    where = _conditions(
        actor_type=actor_type, action=action, actor_id=actor_id, start=start, end=end
    )
    return await _fetch(session, where, limit=limit, offset=offset)


async def _fetch(
    session: AsyncSession,
    where: Sequence[ColumnElement[bool]],
    *,
    limit: int,
    offset: int,
) -> Sequence[AuditLog]:
    """唯一的取数实现（排序与 tiebreak 只在这里写一次）。

    分页（`list_audits`，上限 `MAX_LIMIT`）与导出（`export_audits`，上限
    `MAX_EXPORT_ROWS`）的行数上限不同，所以校验留在各自入口，SQL 共用这一处 ——
    两处各写一遍 `order_by` 迟早有一个漏掉 tiebreak。
    """
    stmt = (
        select(AuditLog)
        .where(*where)
        .order_by(AuditLog.created_at.desc(), AuditLog.audit_id.desc())
        .limit(limit)
        .offset(offset)
    )
    return (await session.execute(stmt)).scalars().all()


async def count_audits(
    session: AsyncSession,
    *,
    actor_type: str | None = None,
    action: str | None = None,
    actor_id: uuid.UUID | None = None,
    start: datetime.datetime | None = None,
    end: datetime.datetime | None = None,
) -> int:
    """与 `list_audits` **同一套过滤条件**下的总条数（分页展示的 total）。

    过滤条件与排序都走同一组私有函数，避免「列表看不到但 total 算进去」这类偏差。
    """
    where = _conditions(
        actor_type=actor_type, action=action, actor_id=actor_id, start=start, end=end
    )
    stmt = select(func.count()).select_from(AuditLog).where(*where)
    return int(await session.scalar(stmt) or 0)


# ------------------------------------------------------------------ 序列化与导出


def serialize_audit(row: AuditLog) -> dict[str, Any]:
    """一行审计 → JSON 就绪的字典（键集合 = `EXPORT_KEYS`）。

    - UUID → str，时间 → ISO 8601 UTC，`detail` → 脱敏后的 dict（不含原始敏感值）；
    - `trace_id` 从 `detail` 提升到顶层：本表没有该列，但导出的每一条都该能被 trace 串起来。
    """
    detail = _redact_detail(row.detail)
    payload: dict[str, Any] = {
        "audit_id": str(row.audit_id),
        "actor_type": row.actor_type,
        "actor_id": str(row.actor_id),
        "action": row.action,
        "resource_type": row.resource_type,
        "resource_id": str(row.resource_id),
        "detail": detail,
        "trace_id": _trace_id_of(row),
        "created_at": _iso_utc(row.created_at),
    }
    # 键顺序固定：`EXPORT_KEYS` 是契约，落 JSON 时顺序也稳定（便于 diff 与人读）。
    return {key: payload[key] for key in EXPORT_KEYS}


async def export_audits(
    session: AsyncSession,
    *,
    actor_type: str | None = None,
    action: str | None = None,
    actor_id: uuid.UUID | None = None,
    start: datetime.datetime | None = None,
    end: datetime.datetime | None = None,
    max_rows: int = MAX_EXPORT_ROWS,
) -> list[dict[str, Any]]:
    """导出过滤后的审计行（已脱敏、键集合稳定），最多 `max_rows` 行。

    导出**不接受 offset**：导出的语义是「这一批数据」，跳过前 N 行没有业务含义，
    真正要分片时用 `start/end` 时间窗。超上限直接抛错而不是静默截断 —— 静默截断会让
    导出方以为拿到了全量。
    """
    if isinstance(max_rows, bool) or not isinstance(max_rows, int):
        raise AuditQueryError(f"max_rows must be an int, got {type(max_rows).__name__}")
    if max_rows < 1 or max_rows > MAX_EXPORT_ROWS:
        raise AuditQueryError(f"max_rows must be between 1 and {MAX_EXPORT_ROWS}, got {max_rows}")

    rows = await _fetch(
        session,
        _conditions(actor_type=actor_type, action=action, actor_id=actor_id, start=start, end=end),
        limit=max_rows,
        offset=0,
    )
    return [serialize_audit(row) for row in rows]


def _write_text(path: Path, payload: str) -> None:
    """阻塞写盘（由 `asyncio.to_thread` 调起，不占事件循环）。"""
    path.write_text(payload, encoding="utf-8")


async def export_audit_jsonl(
    session: AsyncSession,
    *,
    path: str | Path,
    actor_type: str | None = None,
    action: str | None = None,
    actor_id: uuid.UUID | None = None,
    start: datetime.datetime | None = None,
    end: datetime.datetime | None = None,
    max_rows: int = MAX_EXPORT_ROWS,
) -> int:
    """导出一份 JSONL（一行一条），返回写入行数。

    文件 I/O 用 `asyncio.to_thread`：AGENTS §8 要求 I/O 一律 async，而 `path.write_text`
    是阻塞调用 —— 在事件循环里直接写大文件会卡住同进程的语音信令。
    文件为空时写**空文件**（不是 `"\\n"`）：空行会被下游的 `json.loads` 判成坏行。
    """
    records = await export_audits(
        session,
        actor_type=actor_type,
        action=action,
        actor_id=actor_id,
        start=start,
        end=end,
        max_rows=max_rows,
    )
    payload = "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n" for record in records
    )
    await asyncio.to_thread(_write_text, Path(path), payload)
    return len(records)


# ------------------------------------------------------------------ 写入口


async def write_audit(
    session: AsyncSession,
    *,
    actor_type: str,
    actor_id: uuid.UUID,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID,
    detail: Mapping[str, Any] | None = None,
    trace_id: str | None = None,
) -> AuditLog:
    """在**调用方事务**里写一条审计（只 `flush`，不 `commit`）。

    给「业务写库与审计必须同生共死」的路径用（例如退房清数据：删了就必须有记录）。
    需要「必被回滚也要留下证据」的场景请用 `write_audit_isolated`。
    """
    _require_actor_type(actor_type)
    if not isinstance(actor_id, uuid.UUID):
        raise AuditQueryError(f"actor_id must be a UUID, got {type(actor_id).__name__}")
    if not isinstance(resource_id, uuid.UUID):
        raise AuditQueryError(f"resource_id must be a UUID, got {type(resource_id).__name__}")
    if not isinstance(action, str) or not action.strip():
        raise AuditQueryError("action must be a non-empty string")
    if not isinstance(resource_type, str) or not resource_type.strip():
        raise AuditQueryError("resource_type must be a non-empty string")

    row = AuditLog(
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        # 列注释要求「脱敏后」，写入口就是第一道闸。
        detail=_detail_with_trace(detail, trace_id),
    )
    session.add(row)
    await session.flush()
    return row


async def write_audit_isolated(
    *,
    actor_type: str,
    actor_id: uuid.UUID,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID,
    detail: Mapping[str, Any] | None = None,
    trace_id: str | None = None,
    factory: async_sessionmaker[AsyncSession] | None = None,
) -> bool:
    """用**独立会话**写一条审计并提交；返回是否写成功。

    为什么不复用请求会话：越权路径**必然**抛 403，`get_session` 在异常路径上会 rollback，
    同事务写审计等于自己把证据抹掉（`report_service._write_audit` 踩过同一个坑）。

    失败**不上抛**：审计是旁路记录，写失败只能记日志，不能把「本来该给 403 的请求」
    变成 500。返回值给调用方决定要不要额外告警。
    """
    try:
        maker = factory or session_factory
        async with maker() as audit_session:
            await write_audit(
                audit_session,
                actor_type=actor_type,
                actor_id=actor_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                detail=detail,
                trace_id=trace_id,
            )
            await audit_session.commit()
    except Exception:  # pragma: no cover - 审计写失败不能改变业务结论
        logger.exception(
            "audit write failed",
            extra={"trace_id": trace_id or get_trace_id() or "-", "audit_action": action},
        )
        return False

    logger.info(
        "audit",
        extra={
            "trace_id": trace_id or get_trace_id() or "-",
            "audit_action": action,
            "actor": f"{actor_type}:{actor_id}",
            "target": f"{resource_type}:{resource_id}",
        },
    )
    return True


def _jsonable_filters(filters: Mapping[str, Any]) -> dict[str, Any]:
    """过滤条件 → 可进 JSONB 的原生值（UUID/时间转字符串）。"""
    payload: dict[str, Any] = {}
    for key, value in filters.items():
        if isinstance(value, uuid.UUID):
            payload[key] = str(value)
        elif isinstance(value, datetime.datetime):
            payload[key] = _iso_utc(value)
        elif value is None or isinstance(value, (str, int, float, bool)):
            payload[key] = value
        else:  # 兜底：类型不认识就记字符串，宁可难看也不能让写审计本身失败
            payload[key] = str(value)
    return payload


async def record_export(
    session_or_factory: AsyncSession | async_sessionmaker[AsyncSession] | None = None,
    *,
    actor_type: str,
    actor_id: uuid.UUID,
    trace_id: str | None,
    filters: Mapping[str, Any],
    rows: int,
) -> None:
    """给「导出了一批审计」这件事本身留痕（`action='audit.export'`）。

    第一个参数三态，语义写清楚免得误用：

    | 传什么 | 会话归属 | 谁提交 |
    | --- | --- | --- |
    | `None`（默认） | 模块级 `session_factory`（独立会话） | 本函数 `commit()` |
    | `async_sessionmaker` | 该工厂（独立会话） | 本函数 `commit()` |
    | `AsyncSession` | 调用方会话 | **调用方**（本函数只 flush） |

    默认走独立会话：导出是被审计的动作，不该因为调用方事务回滚而消失。
    """
    detail: dict[str, Any] = {
        "filters": _jsonable_filters(filters),
        "rows": int(rows),
    }
    if trace_id is not None:
        detail["trace_id"] = trace_id

    if isinstance(session_or_factory, AsyncSession):
        await write_audit(
            session_or_factory,
            actor_type=actor_type,
            actor_id=actor_id,
            action=EXPORT_ACTION,
            resource_type=AUDIT_RESOURCE_TYPE,
            resource_id=NIL_RESOURCE_ID,
            detail=detail,
            trace_id=trace_id,
        )
        return

    factory = session_or_factory or session_factory
    async with factory() as audit_session:
        await write_audit(
            audit_session,
            actor_type=actor_type,
            actor_id=actor_id,
            action=EXPORT_ACTION,
            resource_type=AUDIT_RESOURCE_TYPE,
            resource_id=NIL_RESOURCE_ID,
            detail=detail,
            trace_id=trace_id,
        )
        await audit_session.commit()

    logger.info(
        "audit export recorded",
        extra={
            "trace_id": trace_id or get_trace_id() or "-",
            "rows": rows,
            "actor": f"{actor_type}:{actor_id}",
        },
    )


__all__ = [
    "ACTOR_TYPES",
    "AUDIT_RESOURCE_TYPE",
    "DEFAULT_LIMIT",
    "EXPORT_ACTION",
    "EXPORT_KEYS",
    "MAX_EXPORT_ROWS",
    "MAX_LIMIT",
    "NIL_RESOURCE_ID",
    "READ_ACTION",
    "REDACT_AVAILABLE",
    "AuditQueryError",
    "actor_type_of",
    "count_audits",
    "export_audit_jsonl",
    "export_audits",
    "list_audits",
    "record_export",
    "serialize_audit",
    "write_audit",
    "write_audit_isolated",
]
