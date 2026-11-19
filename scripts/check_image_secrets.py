#!/usr/bin/env python3
"""构建上下文密钥扫描（迭代 10 · W1）。

## 为什么需要它

镜像层是只增不减的：一个 `.env` 被 `COPY` 进镜像之后，`docker history` / `docker save` /
`docker cp` 都能把它原样取出来，事后删文件也删不掉那一层。所以必须在 `docker build`
之前，对**真正会送进 daemon 的那份上下文**做一次静态检查。

「构建上下文」= 仓库根去掉 `.dockerignore` 命中的路径。本脚本自己解析 `.dockerignore`
（纯标准库），不调用 docker CLI：CI 里没有 daemon 也能量，而且它量的正是
「如果 `.dockerignore` 写错了会发生什么」。

## 判据清单（命中任一条 → exit 1，逐条打印 `file:line`）

| # | 规则 | 命中的是什么 | 为什么算违规 |
| --- | --- | --- | --- |
| 1 | `env-file` | 文件名是 `.env` 或 `.env.<后缀>` | 真实密钥的家；`.env.example` 豁免（只放占位符） |
| 2 | `private-key` | 行内出现 PEM 私钥头 `-----BEGIN <算法> PRIVATE KEY-----` | 私钥进层等于把身份交给拿到镜像的人 |
| 3 | `token-shape` | `sk-…` / `ghp_…` / `github_pat_…` / `AKIA…` 形态的串 | 云 API key、GitHub token、AWS access key id 的高置信形态 |
| 4 | `credential-file` | `*.key` `*.p12` `id_rsa` `credentials.json` 等 | 这些文件的存在意义就是存凭据 |

两条刻意的判断：

- **规则 1 只认文件名，不读内容**。命中即返回：`.env` 的字节从头到尾不进内存、不进日志，
  输出里也只有「`file:line` + 规则名」，不会带出任何一行值。
- **`*.pem` 不按后缀判红**。证书链也是 `.pem`（例如 venv 里 certifi 的 `cacert.pem`），
  按后缀一刀切会把公开证书判成私钥；真正的私钥由规则 2 的头行兜住，判断落在内容上。

`.secrets.baseline` 是 detect-secrets 的审后基线（只存 SHA1 摘要、不存明文；计划 §4.1 明写
「为变绿而覆盖基线等于抹掉审核记录」），因此规则 2/3/4 对它豁免 —— 但规则 1 不豁免任何人。

命中后的处置永远是**把文件挡在上下文之外**（改 `.dockerignore`），而不是放宽本脚本。

## 反向验收（计划 §6.5-2）

把 `.dockerignore` 里挡 `.env` 的那行注释掉，本脚本必须 exit 1：上下文里出现 `.env`
这个文件，规则 1 命中，报告 `file:line` = `.env:1`。

## 用法

    python scripts/check_image_secrets.py            # 0 = 干净 / 1 = 命中 / 2 = 配置缺失
    python scripts/check_image_secrets.py --verbose  # 额外打印每个被跳过的二进制/超大文件
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

#: 仓库根 = 本脚本（scripts/check_image_secrets.py）的上一级。`--root` 可覆盖。
PROJECT_ROOT = Path(__file__).resolve().parent.parent

IGNORE_FILE_NAME = ".dockerignore"
BASELINE_FILE_NAME = ".secrets.baseline"
ENV_FILE_NAME = ".env"
ENV_EXAMPLE_PREFIX = ".env.example"

#: 单个文本文件的上限。上下文里的正常源码都在 1MB 以内（最大的 uv.lock 约 400KB）；
#: 真出现超大文本时，跳过并打印名单，比把几个 GB 读进内存现实。
MAX_TEXT_BYTES = 8 * 1024 * 1024

#: 强制不遍历的目录：`.git` 的对象库是 zlib 压缩的二进制，按文本匹配只会造出假阳性
#: 证据（随机字节里撞出 token 形态）。`.git/` 本来也在 `.dockerignore` 里，这层是兜底。
ALWAYS_SKIP_DIRS = frozenset({".git"})

#: 明确按二进制处理的后缀：不解码、不匹配。`.jks` / `.keystore` 不在此列 ——
#: 它们是凭据容器，由规则 4 按后缀直接判红，跳过内容扫描反而会放过它们。
BINARY_SUFFIXES = frozenset(
    {
        ".7z", ".avi", ".bin", ".bmp", ".class", ".db", ".dll", ".eot", ".exe", ".flac",
        ".gif", ".gz", ".ico", ".jpeg", ".jpg", ".mkv", ".mov", ".mp3",
        ".mp4", ".npy", ".npz", ".ogg", ".onnx", ".otf", ".pdf", ".png", ".pt", ".pth",
        ".pyd", ".so", ".sqlite", ".sqlite3", ".tar", ".ttf", ".vrm", ".wav", ".webm",
        ".webp", ".whl", ".woff", ".woff2", ".xlsx", ".zip",
    }
)  # fmt: skip

#: 规则 4：后缀本身就说明「这是私钥容器」。
CREDENTIAL_SUFFIXES = frozenset({".jks", ".keystore", ".key", ".p12", ".pfx", ".ppk"})

#: 规则 4：文件名就是凭据载体（SSH 私钥、认证配置、云厂商服务账号）。
CREDENTIAL_NAMES = frozenset(
    {
        ".netrc",
        ".npmrc",
        ".pgpass",
        ".pypirc",
        "credentials",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "service-account.json",
        "service_account.json",
    }
)

#: 规则 2/3：逐行内容匹配。第三项是 `True` 表示匹配文本本身可以原样打印（头行不含秘密），
#: `False` 表示必须脱敏（打印实际 key 等于把扫描报告变成新的泄露点，AGENTS §8）。
CONTENT_RULES: tuple[tuple[str, re.Pattern[str], bool], ...] = (
    (
        "private-key",
        re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"),
        True,
    ),
    (
        "token-shape",
        re.compile(r"\b(?:sk-|ghp_|github_pat_|AKIA)[A-Za-z0-9_-]{16,}"),
        False,
    ),
)

#: (模式文本, 匹配正则, 是否 `!` 反向规则) —— 模式文本留着给剪枝安全阀用。
IgnoreRule = tuple[str, re.Pattern[str], bool]


@dataclass(frozen=True, slots=True)
class Finding:
    """一条命中。`path` 是相对仓库根的 POSIX 路径，`line` 从 1 开始。"""

    path: str
    line: int
    rule: str
    detail: str

    def render(self) -> str:
        """`file:line: [rule] 说明`（计划 §6.5-2 要求的形式）。"""
        return f"{self.path}:{self.line}: [{self.rule}] {self.detail}"


# ---------------------------------------------------------------------------
# .dockerignore 解析
# ---------------------------------------------------------------------------


def clean_pattern(pattern: str) -> str:
    """去掉 `./` 前缀与首尾 `/`：`./.env`、`.env`、`.env/` 是同一个模式。"""
    cleaned = pattern
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned.strip("/")


def glob_to_regex(glob: str) -> str:
    """把 `.dockerignore` 的通配翻译成正则片段。

    `**` 跨层、`*` 与 `?` 不跨 `/`、`[...]` 字符类原样透传（Docker 用的是 Go 的
    `filepath.Match`，这一层语义与它对齐；`[!abc]` 这种反向字符类不支持，本仓库也没用）。
    """
    out: list[str] = []
    index = 0
    while index < len(glob):
        char = glob[index]
        if char == "*":
            double = glob[index : index + 2] == "**"
            out.append(".*" if double else "[^/]*")
            index += 2 if double else 1
        elif char == "?":
            out.append("[^/]")
            index += 1
        elif char == "[":
            end = glob.find("]", index + 1)
            if end == -1:
                out.append(re.escape(char))
                index += 1
            else:
                out.append(glob[index : end + 1])
                index = end + 1
        else:
            out.append(re.escape(char))
            index += 1
    return "".join(out)


def ignore_regex(cleaned: str) -> str:
    """一条模式对应的完整匹配正则。

    含 `/` 的模式按「路径前缀」匹配（`docs/plans` 命中 `docs/plans/day10/x.md`）；
    不含 `/` 的模式按「任意层级同名」匹配（`.env` 命中 `a/b/.env`）。后者不是本项目
    自创：Docker 对不含分隔符的模式就比对每一级的 basename。
    """
    body = glob_to_regex(cleaned)
    if "/" in cleaned:
        return f"^{body}(?:/.*)?$"
    return f"^(?:.*/)?{body}(?:/.*)?$"


def load_ignore_rules(ignore_file: Path) -> list[IgnoreRule]:
    """读 `.dockerignore`：空行与 `#` 注释跳过，`!` 前缀记为反向规则。"""
    rules: list[IgnoreRule] = []
    for raw in ignore_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        cleaned = clean_pattern(line[1:].strip() if negate else line)
        if not cleaned:
            continue
        rules.append((cleaned, re.compile(ignore_regex(cleaned)), negate))
    return rules


def is_excluded(rel_path: str, rules: Sequence[IgnoreRule]) -> bool:
    """后匹配覆盖先匹配（Docker 口径）：`!` 规则能把前面排掉的东西重新放回来。"""
    excluded = False
    for _, pattern, negate in rules:
        if pattern.match(rel_path):
            excluded = not negate
    return excluded


def may_reinclude(rel_dir: str, rules: Sequence[IgnoreRule]) -> bool:
    """剪枝前的安全阀：若有 `!` 规则指向这个目录内部，就不能整棵剪掉。

    不这么做的话「先排除目录、再用 `!` 放行其中单个文件」的写法会漏扫，
    「扫描通过」就成了假的。本仓库当前没有这种组合，但解析与遍历必须口径一致。
    """
    prefix = f"{rel_dir}/"
    return any(negate and (text == rel_dir or text.startswith(prefix)) for text, _, negate in rules)


def iter_context_files(
    root: Path, rules: Sequence[IgnoreRule], notes: list[str]
) -> Iterator[tuple[Path, str]]:
    """按 `.dockerignore` 过滤后，逐个体面地列出上下文里的文件（`(绝对路径, 相对路径)`）。"""
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        rel_dir = base.relative_to(root).as_posix()
        if rel_dir == ".":
            rel_dir = ""

        kept_dirs: list[str] = []
        for name in sorted(dirnames):
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if name in ALWAYS_SKIP_DIRS:
                notes.append(f"{rel}/: 版本库内部目录，按二进制跳过")
                continue
            if is_excluded(rel, rules) and not may_reinclude(rel, rules):
                continue
            kept_dirs.append(name)
        dirnames[:] = kept_dirs

        for name in sorted(filenames):
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if is_excluded(rel, rules):
                continue
            yield base / name, rel


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------


def is_env_file_name(name: str) -> bool:
    """规则 1 的文件名判据（`name` 需已小写）。`.env.example*` 是唯一豁免：只放占位符。"""
    if name.startswith(ENV_EXAMPLE_PREFIX):
        return False
    return name == ENV_FILE_NAME or name.startswith(f"{ENV_FILE_NAME}.")


def mask(value: str) -> str:
    """脱敏：只留前 4 个字符与长度，够定位、不够复原。"""
    return f"{value[:4]}***（长度 {len(value)}）"


def scan_file(path: Path, rel: str, notes: list[str]) -> Iterator[Finding]:
    """扫一个文件，产出 0..n 条命中。"""
    name = path.name
    lowered = name.lower()

    # 规则 1：文件名即判据。命中后**立即返回**，不读内容。
    if is_env_file_name(lowered):
        yield Finding(rel, 1, "env-file", "文件名就是环境变量文件；Docker 会把它原样送进构建上下文")
        return

    # 规则 4：文件名本身说明它是凭据载体。
    suffix = path.suffix.lower()
    if suffix in CREDENTIAL_SUFFIXES or lowered in CREDENTIAL_NAMES:
        yield Finding(rel, 1, "credential-file", "文件用途就是存凭据（私钥容器或认证配置）")
        return

    if suffix in BINARY_SUFFIXES:
        notes.append(f"{rel}: 二进制后缀，跳过内容扫描")
        return
    try:
        size = path.stat().st_size
    except OSError as exc:
        notes.append(f"{rel}: 读不到大小（{type(exc).__name__}），跳过")
        return
    if size > MAX_TEXT_BYTES:
        notes.append(f"{rel}: {size} 字节，超过上限 {MAX_TEXT_BYTES}，跳过内容扫描")
        return

    try:
        data = path.read_bytes()
    except OSError as exc:
        notes.append(f"{rel}: 读取失败（{type(exc).__name__}），跳过")
        return
    if b"\x00" in data[:8192]:
        notes.append(f"{rel}: 含 NUL 字节，按二进制跳过")
        return

    # `.secrets.baseline` 只放 SHA1 摘要，规则 2/3/4 对它豁免（规则 1 已在上面生效）。
    if lowered == BASELINE_FILE_NAME:
        return

    text = data.decode("utf-8", errors="replace")
    for lineno, line in enumerate(text.splitlines(), start=1):
        for rule_name, pattern, reveal in CONTENT_RULES:
            match = pattern.search(line)
            if match is None:
                continue
            value = match.group(0)
            detail = value if reveal else mask(value)
            yield Finding(rel, lineno, rule_name, detail)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _force_utf8_stdio() -> None:
    """Windows 默认 cp936，中文报告会乱码；统一按 UTF-8 输出。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: Sequence[str] | None = None) -> int:
    """返回 0（干净）/ 1（命中）/ 2（`.dockerignore` 缺失）。"""
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(
        description="扫描 Docker 构建上下文里的密钥（自解析 .dockerignore）"
    )
    parser.add_argument(
        "--root", type=Path, default=PROJECT_ROOT, help="仓库根，默认本脚本的上一级目录"
    )
    parser.add_argument("--verbose", action="store_true", help="打印被跳过的二进制/超大文件")
    args = parser.parse_args(argv)

    root: Path = args.root.resolve()
    ignore_file = root / IGNORE_FILE_NAME
    if not ignore_file.is_file():
        print(
            f"[FAIL] 缺少 {IGNORE_FILE_NAME}：整个仓库都会成为构建上下文"
            f"（含 {ENV_FILE_NAME} 这类文件），拒绝通过"
        )
        return 2

    rules = load_ignore_rules(ignore_file)
    findings: list[Finding] = []
    notes: list[str] = []
    scanned = 0
    for path, rel in iter_context_files(root, rules, notes):
        scanned += 1
        findings.extend(scan_file(path, rel, notes))

    if args.verbose:
        for note in notes:
            print(f"  · {note}")

    if not findings:
        print(
            f"[OK] 构建上下文干净：扫描 {scanned} 个文件，0 命中"
            f"（{IGNORE_FILE_NAME} 规则 {len(rules)} 条）"
        )
        return 0

    print(f"[FAIL] 构建上下文里有 {len(findings)} 处密钥风险：")
    for finding in findings:
        print(f"  {finding.render()}")
    print(f"  上下文文件 {scanned} 个。处置：把它挡在构建上下文之外（改 {IGNORE_FILE_NAME}）。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
