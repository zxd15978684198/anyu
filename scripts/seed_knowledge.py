"""把 `docs/knowledge_seed/*.md` 灌进机构知识库（DAG N22）。

用法::

    # 直接灌（幂等：已存在的 (title, version) 会 skip，重复执行安全）
    uv run python scripts/seed_knowledge.py

    # 只看会灌什么，不写库
    uv run python scripts/seed_knowledge.py --dry-run

    # 换目录 / 指定写入人（默认取第一个 admin）
    uv run python scripts/seed_knowledge.py --seed-dir docs/knowledge_seed --actor-id <staff uuid>

**为什么需要它**：N21 的检索端上线后，`knowledge_documents` / `knowledge_chunks`
是空表，检索只会稳定返回 `no_match`。机构知识是**平台侧配置**（不是长者记忆），
没有对话写入路径，只能由运维/开发手工灌 —— 这个脚本就是那条唯一入口。

## 口径

- 解析、切块、向量化、审计全部走 `app/services/knowledge_seed.py` 与
  `knowledge_service.ingest_document`，脚本本身不碰 ORM（分层：脚本 → services）。
- 幂等键 `(title, version)`：重复执行只打印 `skip`，不改旧行、不升版本；
  要出新版本请走 `supersede_document`（管理员 API），不要手改库。
- embedding provider 由配置决定：四项（base_url/model/api_key/timeout）配齐用
  HTTP，否则退回 `DeterministicEmbeddingProvider` —— **后者只有哈希噪声、没有语义**，
  脚本会在摘要里明确标注 `semantic=False`，别拿它对外演示「语义检索」。
- 不需要迁移：表与向量列在 `0011_knowledge` 已建好。

退出码：0 成功（含全部 skip）；1 解析/数据库/写入人错误。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlalchemy.exc import SQLAlchemyError  # noqa: E402

from app.db import session_factory  # noqa: E402
from app.services import knowledge_seed  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="把 docs/knowledge_seed/*.md 灌进机构知识库（幂等）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：uv run python scripts/seed_knowledge.py --dry-run",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只解析并列出计划，不写库",
    )
    parser.add_argument(
        "--seed-dir",
        type=Path,
        default=knowledge_seed.DEFAULT_SEED_DIR,
        help=f"种子目录（默认 {knowledge_seed.DEFAULT_SEED_DIR}）",
    )
    parser.add_argument(
        "--actor-id",
        type=uuid.UUID,
        default=None,
        help="写入人（staff_id）；缺省取第一个 role=admin 的账号",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    documents = knowledge_seed.load_seed_documents(args.seed_dir)
    print(f"解析到种子：{len(documents)} 份（{args.seed_dir}）")
    for document in documents:
        print(
            f"  {document.title}  v{document.version}  "
            f"category={document.category}  authority={document.authority_level}  "
            f"effective_from={document.local_effective_date.isoformat()}"
        )

    async with session_factory() as session:
        actor_id = await knowledge_seed.resolve_actor(session, actor_id=args.actor_id)
        print(f"写入人：{actor_id}")

        if args.dry_run:
            await session.rollback()
            print("\nDRY-RUN：未写库。去掉 --dry-run 重跑即入库。")
            return 0

        summary = await knowledge_seed.seed_documents(session, documents, actor_id=actor_id)
        await session.commit()

    print(f"\n新建 {len(summary.created)} 份 / 跳过 {len(summary.skipped)} 份")
    for label in summary.created:
        print(f"  + {label}")
    for label in summary.skipped:
        print(f"  = {label}（已存在，跳过）")
    print(f"新增切块：{summary.chunks}；provider={summary.provider}；semantic={summary.semantic}")
    if not summary.semantic:
        print(
            "注意：当前是确定性哈希 provider（无 embedding key），"
            "只能证明管线通了，不能用来评价召回质量。",
            file=sys.stderr,
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except knowledge_seed.SeedError as exc:  # 解析/写入人问题：给出可读原因
        print(f"种子错误：{exc}", file=sys.stderr)
        return 1
    except SQLAlchemyError as exc:  # 连不上库 / 写入失败
        print(f"数据库错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
