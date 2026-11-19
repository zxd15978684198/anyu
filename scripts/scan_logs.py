"""日志脱敏验收脚本：扫日志目录，抓未脱敏的手机号 / 身份证 / 密钥 / 完整地址。

## 用法

```
.\\.venv\\Scripts\\python.exe scripts/scan_logs.py [--root logs] [--glob "*.log"]
```

退出码：`0` = 扫完零命中；`1` = 有命中（先把命中行打出来再结束）；`2` = 参数错（argparse）。

## 三条口径

1. **纯标准库**，规则全部来自 `app.core.redact.REDACTION_RULES` —— 与 `logging.py` 用的是
   同一张表。脚本自己另写一套正则，就会出现「日志脱了、脚本却说没脱」的假绿（假阴性）。
2. **命中片段本身也打码**（`mask_*` 再套一次），因为命中行的原文已经泄露过一次，
   验收报告和 CI 日志不该再泄露第二次。
3. **目录不存在不算命中**：打印警告 + 退出码 0，但一定把「扫描了 0 个文件」写出来 ——
   假绿的风险靠这行数字暴露，不靠退出码撒谎。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

#: 脚本以 `python scripts/scan_logs.py` 运行时，`sys.path[0]` 是 `scripts/`，
#: 仓库根不在导入路径上，先把根插进去再 import（否则 `from app.core.redact` 直接 ModuleNotFoundError）。
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.core.redact import REDACTION_RULES  # noqa: E402  （见上面的 sys.path 引导）

#: 默认扫描目录（相对仓库根；不强制存在）。
_DEFAULT_ROOT = "logs"

#: 默认文件名模式。
_DEFAULT_PATTERN = "*.log"


@dataclass(frozen=True, slots=True)
class Finding:
    """一段文本里的一处命中（片段已打码，可安全打印）。"""

    kind: str
    fragment: str
    position: int

    def format(self) -> str:
        return f"{self.kind}: {self.fragment}"


@dataclass(frozen=True, slots=True)
class LogFinding:
    """带文件与行号的命中，`format()` 就是验收报告里那一行。"""

    path: str
    line: int
    kind: str
    fragment: str

    def format(self) -> str:
        """`file:line: 类型: 部分打码的片段` —— grep 友好，可直接贴进验收报告。"""
        return f"{self.path}:{self.line}: {self.kind}: {self.fragment}"


def scan_text(text: str) -> list[Finding]:
    """扫描一段文本（通常是一行日志），按出现位置升序返回命中。

    规则表里有几条就扫几条（手机号 / 身份证 / 密钥 / 地址），一处命中算一条；
    同一位置的重复命中不再去重 —— 验收要的是「有没有泄露」，不是「泄露了几类」。
    """
    if not text:
        return []
    findings: list[Finding] = []
    for rule in REDACTION_RULES:
        for match in rule.pattern.finditer(text):
            findings.append(
                Finding(kind=rule.kind, fragment=rule.masker(match.group()), position=match.start())
            )
    findings.sort(key=lambda item: item.position)
    return findings


def scan_file(path: Path) -> list[LogFinding]:
    """逐行扫一个文件；行号 1 起。非 UTF-8 字节按替换字符处理，不抛异常。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    found: list[LogFinding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        found.extend(
            LogFinding(path=str(path), line=lineno, kind=item.kind, fragment=item.fragment)
            for item in scan_text(line)
        )
    return found


def iter_log_files(root: Path, pattern: str = _DEFAULT_PATTERN) -> list[Path]:
    """列出待扫文件；`root` 是单文件时直接用它，是目录时递归匹配 `pattern`。不存在返回空列表。"""
    if root.is_file():
        return [root]
    if not root.is_dir():
        return []
    return sorted(item for item in root.rglob(pattern) if item.is_file())


def scan_paths(root: Path, pattern: str = _DEFAULT_PATTERN) -> tuple[list[LogFinding], int]:
    """扫一个目录（或单文件），返回 `(命中列表, 实际扫描的文件数)`。"""
    files = iter_log_files(root, pattern)
    findings = [item for path in files for item in scan_file(path)]
    return findings, len(files)


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口，返回退出码（不自带 `sys.exit`，方便单测直接调）。"""
    parser = argparse.ArgumentParser(
        description="扫描日志目录，抓未脱敏的个人敏感信息（有命中则退出码 1）"
    )
    parser.add_argument("--root", default=_DEFAULT_ROOT, help="日志目录或单个文件（默认 logs）")
    parser.add_argument("--glob", dest="pattern", default=_DEFAULT_PATTERN, help="文件名模式")
    args = parser.parse_args(argv)

    root = Path(args.root)
    findings, scanned = scan_paths(root, args.pattern)

    for item in findings:
        print(item.format())

    if findings:
        print(
            f"[FAIL] {len(findings)} 处未脱敏敏感信息（扫描 {scanned} 个文件）",
            file=sys.stderr,
        )
        return 1

    if scanned == 0:
        print(f"[WARN] 未扫到任何文件：{root}（--glob {args.pattern}）", file=sys.stderr)
        return 0

    print(f"[OK] 未发现未脱敏敏感信息（扫描 {scanned} 个文件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
