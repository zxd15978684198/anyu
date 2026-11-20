#!/usr/bin/env python3
"""`scripts/ci.py` 与 CI workflow 的 argv 漂移检查（迭代 10 · 计划 §6.5 反向验收 R4）。

为什么需要它：Gatekeeper 的静态审计只看 workflow 的形状（concurrency / timeout /
permissions / cache / artifact 等），**不读 `scripts/ci.py` 的阶段表**。于是存在一种
两边都绿的坏状态——本地门禁悄悄少跑一个阶段（或被改成更宽松的 argv），CI 那边看起来
一切正常。这个脚本把「本地八阶段」与「workflow 的 run 步骤」做双向集合比对：

  1. `scripts/ci.py --list` 里的每一条 `$ argv` 必须能在 workflow 的 `run:` 里找到；
  2. workflow 里每一条以 `uv ` 开头的命令也必须能在 `ci.py --list` 里找到。

任一侧多出或缺少一条命令即 exit 1，并逐条点名。纯标准库、不联网、不改任何文件。

用法：
    uv run --locked python scripts/check_gate_sync.py            # 检查
    uv run --locked python scripts/check_gate_sync.py --verbose  # 打印比对全量

退出码：0 = 一致；1 = 漂移（有多余或缺项）；2 = 无法完成检查（缺文件/命令失败）。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CI_SCRIPT = REPO_ROOT / "scripts" / "ci.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "anyu.yml"

# workflow 里合法但不属于 ci.py 阶段表的命令（装工具链、上传产物等编排动作）。
# 只允许显式列在这里，避免「随便加一条 workflow 步骤」静默逃过比对。
WORKFLOW_ONLY_PREFIXES = (
    "uv sync",  # 由 ci.py 的 sync 阶段代表，但 workflow 需要它作为独立安装步骤
    # CI 的 PostgreSQL 是 service container 起的空库，表全靠迁移建；本地这一步是开发者
    # 手动做的前置（README §5），没有对应的 ci.py 阶段，故只允许出现在 workflow 侧。
    "uv run --locked alembic upgrade head",
)


def _norm(command: str) -> str:
    """比较用归一化：压空白、去首尾引号差异、统一 `python` 与 `uv run... python` 写法差异。"""
    return " ".join(command.split()).strip()


def ci_argv() -> list[str]:
    """从 `scripts/ci.py --list` 取出全部 `$ argv` 行（按出现顺序）。"""
    proc = subprocess.run(  # noqa: S603 - 固定 argv，无 shell
        [sys.executable, str(CI_SCRIPT), "--list"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        print(f"[FAIL] `scripts/ci.py --list` 退出码 {proc.returncode}")
        print(proc.stderr.strip() or proc.stdout.strip())
        raise SystemExit(2)
    return [
        line.strip()[2:].strip()
        for line in proc.stdout.splitlines()
        if line.strip().startswith("$ ")
    ]


def workflow_commands() -> list[str]:
    """解析 workflow 的 `run:` 步骤（支持 `run: xxx` 与 `run: |` 多行块）。"""
    if not WORKFLOW.is_file():
        print(f"[FAIL] 找不到 workflow：{WORKFLOW.relative_to(REPO_ROOT)}")
        raise SystemExit(2)
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    commands: list[str] = []
    index = 0
    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()
        if blocked := stripped.startswith("run:"):
            inline = stripped[len("run:") :].strip()
            indent = len(raw) - len(raw.lstrip())
            index += 1
            if inline and inline not in {"|", ">", "|-", ">-"}:
                commands.append(inline)
                continue
            # 多行块：吃到缩进回落到 <= 本行缩进为止。
            while index < len(lines):
                body = lines[index]
                if body.strip() and len(body) - len(body.lstrip()) <= indent:
                    break
                if body.strip():
                    commands.append(body.strip())
                index += 1
            continue
        index += 1
        _ = blocked
    return commands


def main() -> int:
    parser = argparse.ArgumentParser(description="ci.py 与 workflow 的 argv 漂移检查")
    parser.add_argument("--verbose", action="store_true", help="打印两侧全量命令")
    args = parser.parse_args()

    local = [_norm(cmd) for cmd in ci_argv()]
    remote = [_norm(cmd) for cmd in workflow_commands()]
    remote_uv = [cmd for cmd in remote if cmd.startswith("uv ")]

    if args.verbose:
        print(f"ci.py 阶段命令 {len(local)} 条：")
        for cmd in local:
            print(f"  $ {cmd}")
        print(f"workflow 的 uv 命令 {len(remote_uv)} 条：")
        for cmd in remote_uv:
            print(f"  $ {cmd}")

    missing_in_ci = [
        cmd for cmd in remote_uv if cmd not in local and not cmd.startswith(WORKFLOW_ONLY_PREFIXES)
    ]
    missing_in_workflow = [cmd for cmd in local if cmd not in remote]

    problems = 0
    if missing_in_workflow:
        problems += len(missing_in_workflow)
        print("[FAIL] ci.py 有、workflow 没有（本地多跑/CI 少跑）：")
        for cmd in missing_in_workflow:
            print(f"  - {cmd}")
    if missing_in_ci:
        problems += len(missing_in_ci)
        print("[FAIL] workflow 有、ci.py 没有（CI 多跑/本地少跑）：")
        for cmd in missing_in_ci:
            print(f"  + {cmd}")

    if problems:
        print(
            f"\n结论：FAIL —— {problems} 条命令两侧不一致。"
            "改一侧 argv 必须同时改另一侧（Gatekeeper IR 只保证顺序与阶段名，不保证命令正文）。"
        )
        return 1

    print(f"[OK] 门禁命令两侧一致：ci.py {len(local)} 条 / workflow uv 命令 {len(remote_uv)} 条。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
