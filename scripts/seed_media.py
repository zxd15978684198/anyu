"""把 `media/manifest.json` 的授权素材灌进 `media_library`（幂等种子脚本）。

用法::

    # 只看会做什么，不写库
    uv run python scripts/seed_media.py --dry-run

    # 真写库（重复执行安全：以 media_url 为业务键做 upsert）
    uv run python scripts/seed_media.py

**为什么需要它**：`media_library` 是空表时，`app.services.media_service.pick`
返回 `None`，长者说「听一段戏」只能听到「没找到这段」（FR-CON-04）；而 `seed_demo.py`
不碰这张表（素材不是演示账号数据，是**平台侧授权内容**）。这个脚本是素材入库的唯一入口。

## 口径（三条校验都是 fail closed，不静默跳过）

1. **授权白名单**：`copyright_status` 必须落在
   `media_service.AUTHORIZED_COPYRIGHT_STATUSES`（``public_domain`` / ``licensed_sample``）
   内。白名单外直接报错退出，**不**「先入库再在检索侧过滤」——素材红线
   （PRD FR-CON-03/05）宁可不入库，也不能让一条来历不明的素材先在库里躺着等某天被点开。
   白名单**从服务层 import**，不在本脚本里抄一份常量：两份常量迟早漂移，
   而漂移的那天就是未授权素材被放出去的那天。
2. **音频文件必须真实存在**：`media/<file>` 不存在就报错退出。库里有一条 `media_url`
   指向 404 的文件，界面上「看起来有这段、点了没声」，排查成本远高于在种子里当场失败；
   `media_url` 与磁盘必然同源（同一个 `<file>`），所以这条校验没有误伤空间。
3. **`media_url` 不得重复**：同一地址两条记录，upsert 会随机命中一条，之后「更新了却没变」。

## 幂等键：为什么是 `media_url`

`media_library` 的 `media_id` 是 `gen_random_uuid()` 的代理主键，**每次插入都不同**，
拿它当键等于每次都新增一行，重复执行行数翻倍；而 `media_url` 是这条素材的天然业务标识
（一个媒体文件在站内只有一个地址，`pick` 检索出来的、前端 `<audio src>` 播放的也是它），
所以 `media_url` 天然唯一、天然稳定 —— 用它判断「这条是否已存在」，已存在就更新可变字段，
不存在才 `INSERT`，重复运行行数不变。

> 数据库侧已有 `UNIQUE (media_url)`（迁移 `alembic/versions/0014_media_url_unique.py`），
> 所以除了本脚本的「先 SELECT 再决定」，还有一层由数据库兜底：两条进程同时查到
> 「没有」、各自想去插入时，后到的会被唯一约束拒绝。
> 因此本脚本给**每条插入**单独包一个 `SAVEPOINT`：撞上唯一约束就回滚这一条并按
> 「已存在」改成更新，而不是抛异常炸掉整批（也就不会出现同一段戏在库里两行）。

## 字段名映射（清单 ↔ ORM 不一致，以模型为准）

`manifest.json` 是抓取产物，字段名和 ORM 列名有两处**不同名**，本脚本显式映射，
不做「同名即可直传」的假设：

==================  ==============================  ==========================================
清单字段              ORM 列（`app/models/media.py`）  说明
==================  ==============================  ==========================================
``kind``             ``type``                        `type` 是 SQL 关键字，闭集 opera / movie
``duration_seconds`` ``duration``                    **Integer**，清单是 float（如 39.105）
==================  ==============================  ==========================================

`duration` 列是 `Integer` 且有 `duration > 0` 的 CHECK，故清单的浮点秒数四舍五入取整
（39.105 → 39）；缺字段写 NULL（「未知时长」），不写 0（会被 CHECK 拒绝，且 0 秒是错数据）。

退出码：0 成功（含全部 skip）；1 清单/校验/数据库错误。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.exc import IntegrityError, SQLAlchemyError  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.db import engine, session_factory  # noqa: E402
from app.models.media import MediaLibraryItem  # noqa: E402
from app.services.media_service import (  # noqa: E402
    AUTHORIZED_COPYRIGHT_STATUSES,
    MEDIA_KINDS,
)

#: 清单路径：用本文件位置反推仓库根（不写死盘符，脚本搬到别的机器也能跑）。
MANIFEST_PATH: Final[Path] = _REPO_ROOT / "media" / "manifest.json"
MEDIA_ROOT: Final[Path] = _REPO_ROOT / "media"

#: 允许的处置动作。`skip` = 已存在且可变字段全等，无需写库。
ACTION_INSERT: Final[str] = "insert"
ACTION_UPDATE: Final[str] = "update"
ACTION_SKIP: Final[str] = "skip"

#: 可变字段（`media_url` 是幂等键、`media_id` 是代理主键，都不在可变集合里）。
MUTABLE_FIELDS: Final[tuple[str, ...]] = (
    "type",
    "title",
    "genre",
    "era",
    "artist",
    "duration",
    "copyright_status",
)


class SeedError(Exception):
    """清单或校验问题：给运维看的一行原因，脚本以退出码 1 结束。"""


@dataclass(frozen=True, slots=True)
class MediaSeedItem:
    """一条已校验的素材（清单字段 → ORM 列名后的形态）。"""

    media_url: str
    type: str
    title: str
    genre: str | None
    era: str | None
    artist: str | None
    duration: int | None
    copyright_status: str
    relative_file: str

    def as_columns(self) -> dict[str, Any]:
        """可变字段的列值字典，供插入/更新共用（避免两处各拼一遍）。"""
        return {name: getattr(self, name) for name in MUTABLE_FIELDS}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="把 media/manifest.json 的授权素材灌进 media_library（幂等）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：uv run python scripts/seed_media.py --dry-run",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印计划（insert/update/skip），不写库",
    )
    return parser


def _as_duration(raw: Any, label: str) -> int | None:
    """清单 `duration_seconds`（float）→ `duration` 列（Integer，必须 > 0）。

    四舍五入而不是截断：39.105 截断成 39 与 40 都「差不到一秒」，但截断会系统性偏短，
    累积到多段素材上就是播放条总提前走完。缺字段返回 None（未知），不返回 0。
    """
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise SeedError(f"{label} 的 duration_seconds 不是数字：{raw!r}")
    seconds = round(float(raw))
    if seconds <= 0:
        raise SeedError(f"{label} 的 duration_seconds 必须为正：{raw!r}")
    return int(seconds)


def _optional_text(raw: Any) -> str | None:
    """空白串归一成 None：清单里的空 `genre` 不该在库里变成「有一列但是空的」。"""
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _load_items(path: Path) -> list[MediaSeedItem]:
    """读清单并逐条校验；任何一条不合格都抛 `SeedError`（不跳过、不降级）。"""
    if not path.is_file():
        raise SeedError(f"清单不存在：{path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SeedError(f"清单不是合法 JSON：{path}（{exc}）") from exc
    if not isinstance(raw, dict):
        raise SeedError(f"清单顶层必须是对象：{path}")
    raw_items = raw.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise SeedError(f"清单没有 items 或 items 为空：{path}")

    items: list[MediaSeedItem] = []
    seen_urls: dict[str, int] = {}
    for index, entry in enumerate(raw_items, start=1):
        if not isinstance(entry, dict):
            raise SeedError(f"第 {index} 条不是对象：{entry!r}")
        file = _optional_text(entry.get("file"))
        label = f"第 {index} 条（{file or entry.get('media_url') or '无标识'}）"

        media_url = _optional_text(entry.get("media_url"))
        if media_url is None:
            raise SeedError(f"{label} 缺 media_url（幂等键不能为空）")

        # 校验 1：授权白名单。fail closed —— 白名单外直接退出，不静默跳过。
        status = _optional_text(entry.get("copyright_status"))
        if status not in AUTHORIZED_COPYRIGHT_STATUSES:
            raise SeedError(
                f"{label} 的 copyright_status={status!r} 不在授权白名单内"
                f"（允许：{', '.join(AUTHORIZED_COPYRIGHT_STATUSES)}）；"
                "拒绝入库。素材红线：授权状态不明一律不放行（FR-CON-03/05）"
            )

        # 校验 2：`kind` → ORM 的 `type`，必须是闭集（DB 有 CHECK 约束兜底）。
        kind = _optional_text(entry.get("kind"))
        if kind not in MEDIA_KINDS:
            raise SeedError(f"{label} 的 kind={kind!r} 不是 {' / '.join(MEDIA_KINDS)}")

        # 校验 3：音频文件必须真实存在，否则库里会出现「点了没声」的死记录。
        if file is None:
            raise SeedError(f"{label} 缺 file（无法校验音频是否存在）")
        media_path = MEDIA_ROOT / file
        if not media_path.is_file():
            raise SeedError(f"{label} 的音频文件不存在：{media_path}；拒绝入库（会点了没声）")

        # 校验 4：同一 media_url 不得重复，否则 upsert 命中哪条不确定。
        if media_url in seen_urls:
            raise SeedError(
                f"{label} 的 media_url={media_url!r} 与第 {seen_urls[media_url]} 条重复；"
                "重复地址会让 upsert 命中随机一行"
            )
        seen_urls[media_url] = index

        title = _optional_text(entry.get("title"))
        if title is None:
            raise SeedError(f"{label} 缺 title（节目名是播报用的必填列）")

        items.append(
            MediaSeedItem(
                media_url=media_url,
                type=kind,
                title=title,
                genre=_optional_text(entry.get("genre")),
                era=_optional_text(entry.get("era")),
                artist=_optional_text(entry.get("artist")),
                duration=_as_duration(entry.get("duration_seconds"), label),
                copyright_status=status,
                relative_file=file,
            )
        )
    return items


async def _load_existing(
    session: AsyncSession, media_urls: list[str]
) -> dict[str, MediaLibraryItem]:
    """按 `media_url` 批量取已有行（幂等键 → 行）。

    一次 `IN` 查完，不在循环里逐条查（N 条素材 = N 次往返）。库里同一 `media_url`
    出现多行时报错：那是脏数据，本脚本不猜该改哪一条（改哪条都等于悄悄丢数据），
    交给人清理后再跑。
    """
    rows = (
        (
            await session.execute(
                select(MediaLibraryItem).where(MediaLibraryItem.media_url.in_(media_urls))
            )
        )
        .scalars()
        .all()
    )
    counts = Counter(row.media_url for row in rows)
    duplicated = [url for url, count in counts.items() if count > 1]
    if duplicated:
        raise SeedError(
            f"库里 media_library.media_url 有重复行（{', '.join(duplicated)}）；"
            "请先人工确认保留哪一条，脚本不代为选择"
        )
    return {row.media_url: row for row in rows}


def _plan(
    items: list[MediaSeedItem], existing: dict[str, MediaLibraryItem]
) -> list[tuple[str, MediaSeedItem, list[str]]]:
    """算出每条素材的处置（不写库的纯函数）。

    - 不存在 → `insert`
    - 存在但可变字段有差异 → `update`（列出**哪些**字段变，便于核对清单改动）
    - 存在且全等 → `skip`（这是重复执行的常态，行数因此不变）
    """
    plan: list[tuple[str, MediaSeedItem, list[str]]] = []
    for item in items:
        row = existing.get(item.media_url)
        if row is None:
            plan.append((ACTION_INSERT, item, []))
            continue
        changed = [name for name, value in item.as_columns().items() if getattr(row, name) != value]
        plan.append((ACTION_UPDATE if changed else ACTION_SKIP, item, changed))
    return plan


def _apply_mutable(row: MediaLibraryItem, item: MediaSeedItem) -> None:
    """把清单里的可变字段写到已存在的行上（`media_url` / `media_id` 不动）。"""
    for name, value in item.as_columns().items():
        setattr(row, name, value)


def _print_plan(plan: list[tuple[str, MediaSeedItem, list[str]]]) -> Counter[str]:
    """逐条打印处置，返回动作计数。"""
    actions: Counter[str] = Counter()
    for action, item, changed in plan:
        actions[action] += 1
        seconds = "-" if item.duration is None else f"{item.duration}s"
        detail = f"  变更字段：{', '.join(changed)}" if changed else ""
        print(
            f"  {action:<6} {item.type:<5} {item.media_url:<46} {seconds:>4}  {item.title}{detail}"
        )
    return actions


async def _type_counts(session: AsyncSession) -> dict[str, int]:
    """库里按 `type` 计数（写库后回报「opera/movie 各几条」）。"""
    rows = (
        await session.execute(
            select(MediaLibraryItem.type, func.count()).group_by(MediaLibraryItem.type)
        )
    ).all()
    return {row_type: count for row_type, count in rows}


async def _run(args: argparse.Namespace) -> int:
    items = _load_items(MANIFEST_PATH)
    print(f"清单：{MANIFEST_PATH}（{len(items)} 条，授权状态均已校验）")
    try:
        return await _seed(items, dry_run=args.dry_run)
    finally:
        # 在当前事件循环内释放连接池：放到 `asyncio.run` 之后再 dispose，
        # asyncpg 的连接会绑在已关闭的循环上，冒「Event loop is closed」噪声
        # （Windows 上尤其明显）。
        await engine.dispose()


async def _seed(items: list[MediaSeedItem], *, dry_run: bool) -> int:
    async with session_factory() as session:
        existing = await _load_existing(session, [item.media_url for item in items])
        plan = _plan(items, existing)
        actions = _print_plan(plan)

        if dry_run:
            # dry-run 不提交：上下文退出时会话回滚，SELECT 不留下任何写入。
            print("\nDRY-RUN：以上为计划，未写库。去掉 --dry-run 重跑即入库。")
            return 0

        raced = 0
        for action, item, _changed in plan:
            if action == ACTION_INSERT:
                try:
                    # 每条插入一个 SAVEPOINT：唯一约束撞车时只回滚这一条，
                    # 前面已插好的素材不跟着陪葬（整批回滚会让复跑「时好时坏」）。
                    async with session.begin_nested():
                        session.add(MediaLibraryItem(media_url=item.media_url, **item.as_columns()))
                except IntegrityError:
                    # 数据库说这条已经存在（另一个进程刚插进来）：按更新处理，
                    # 与 ACTION_UPDATE 分支落到同一个结果，不报错也不留两行。
                    raced += 1
                    row = (
                        await session.execute(
                            select(MediaLibraryItem).where(
                                MediaLibraryItem.media_url == item.media_url
                            )
                        )
                    ).scalar_one()
                    _apply_mutable(row, item)
            elif action == ACTION_UPDATE:
                _apply_mutable(existing[item.media_url], item)
        await session.commit()

        counts = await _type_counts(session)

    total = sum(counts.values())
    print(
        f"\n处置汇总：insert {actions[ACTION_INSERT]} / "
        f"update {actions[ACTION_UPDATE]} / skip {actions[ACTION_SKIP]}"
    )
    if raced:
        # 只在真发生过并发插入时出现：说明「先查后插」的窗口这次被数据库兜住了。
        print(f"其中 {raced} 条插入时撞到唯一约束（另一进程已插），已按更新处理")
    print(
        "库里现状："
        + "、".join(f"{kind}={counts.get(kind, 0)}" for kind in MEDIA_KINDS)
        + f"，合计 {total} 条"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except SeedError as exc:  # 清单/校验问题：一行原因 + 退出码 1（fail closed）
        print(f"种子校验失败：{exc}", file=sys.stderr)
        return 1
    except SQLAlchemyError as exc:  # 连不上库 / 写入失败
        print(f"数据库错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
