"""文档 `file:line` 引用漂移校验。

为什么需要它：`docs/` 与 `README.md` 里有数百处 `app/xxx.py:123` 形式的引用，用来说明
「这条口径在哪实现」。代码一改行号就整体失效，但**文档不会报错**——读者按引用去找，
轻则扑空，重则读到另一段无关代码还以为是证据（本项目已发生过：README 的服务端 VAD
引用 `webrtc.py:675` 实际指向音频重采样段，与 VAD 毫无关系）。

判据不是「行号存在」，而是「行号处的内容仍与文档描述一致」。做法：

- 从引用行里提取**期望内容**（反引号正上方／同段内的标识符、字符串字面量）；
- 到 `file:line` 处取上下文窗口（含前 2 行、后 2 行），检查期望内容是否命中；
- 命中即 OK；不命中报 `DRIFT`，并在报告里给出该行的实际内容，便于人工定位。

三种结论：

- `OK`      —— 行号在，且上下文命中期望内容；
- `DRIFT`   —— 行号在，但上下文不含期望内容（多半是行号漂了）；
- `MISSING` —— 文件不存在（拼错路径，或文件被删/移动）。

**不做自动改写**：行号修复需要判断「这个引用现在应该指哪儿」，脚本只负责把可疑项
按文档聚合并给出实际行内容，改由人工确认。自动跳到「下一个包含该符号的行」看似聪明，
但同一符号常在多文件多处出现（如 `verify_session_identity` 在 media_bridge 有两处调用），
猜错会把错误固化进文档。

用法::

    uv run python scripts/check_doc_refs.py                 # 全量校验，只打印问题
    uv run python scripts/check_doc_refs.py --verbose       # 逐条打印 OK
    uv run python scripts/check_doc_refs.py --file docs/SECURITY.md
    uv run python scripts/check_doc_refs.py --json          # 机器可读，给 CI / 后续工具用

退出码：0 全部通过；1 有 DRIFT 或 MISSING。纯标准库，无第三方依赖。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

#: 文档里引用的形式：`path.py:123` / `path.ts:12-34` / 裸 `path.py:123`
_REF_PATTERN = re.compile(
    r"(?P<path>(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.(?:py|ts|tsx|js|mjs|yml|yaml|conf|json|sh|toml|ini|css))"
    r":(?P<start>\d+)(?:-(?P<end>\d+))?"
)

#: 期望内容的候选来源：反引号内的标识符 / 引号字符串 / 常见关键字
_IDENTIFIER = re.compile(r"\b(?:def|class|async def)\s+([A-Za-z_][A-Za-z0-9_]*)")
_IDENT_CALL = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\s*\(")
_QUOTED = re.compile(r"[\"']([^\"']{3,60})[\"']")
_CONST = re.compile(r"\b([A-Z][A-Z0-9_]{3,})\b")

#: 只接受「代码形态」的标识符，用来滤掉文档里的英文单词与文档名。
#: 判据：snake_case（含下划线）或 camelCase/PascalCase（含内部大写）或全大写常量。
_CODE_SHAPE = re.compile(
    r"^(?:[a-z][a-z0-9]*(?:_[a-z0-9]+)+|[a-z]+[A-Z][A-Za-z0-9]*|[A-Z][a-zA-Z0-9]*[a-z][A-Za-z0-9]*|[A-Z][A-Z0-9_]{2,})$"
)

#: 文档结构词，绝不可能是代码符号（大小写不敏感地排除）
_STOPWORDS = {
    "http",
    "https",
    "json",
    "yaml",
    "api",
    "url",
    "uri",
    "html",
    "css",
    "cli",
    "env",
    "sql",
    "utc",
    "utc_z",
    "prd",
    "td",
    "ac",
    "fr",
    "day",
    "readme",
    "agents",
    "security",
    "identity_rules",
    "harness_design",
    "ai_usage",
    "interview_qa",
    "ab",
    "ok",
    "fail",
    "todo",
    # SQL 关键字与常见错误码：文档里常出现在「含 CHECK 约束」「返回 NOT_FOUND」这类行文里，
    # 与引用处的代码符号无关。
    "check",
    "not_found",
    "not_found_error",
    "null",
    "true",
    "false",
    "string",
    "integer",
    "boolean",
    "object",
    "array",
    "select",
    "insert",
    "update",
    "delete",
    "where",
    "index",
    "unique",
    "primary",
    "foreign",
    "references",
    # 文档正文里的缩写与概念词（不是代码符号）
    "rest",
    "fifo",
    "lru",
    "ttl",
    "p95",
    "p50",
    "jwt",
    "rbac",
    "sdk",
    "orm",
    "ddl",
    "mcp",
    "llm",
    "asr",
    "tts",
    "vad",
    "kws",
    "webrtc",
    "vrm",
    "pcm",
    "wasm",
    "cps",
    "hitl",
    "react",
    "otel",
}

#: 引用附近多远内的词才算「这条引用的期望」——一行里常有多个引用，各管各的
_EXPECT_RADIUS = 70

#: 默认扫描面
DEFAULT_TARGETS = (
    "README.md",
    "AGENTS.md",
    "docs/SECURITY.md",
    "docs/HARNESS_DESIGN.md",
    "docs/IDENTITY_RULES.md",
    "docs/INTERVIEW_QA.md",
    "docs/AI_USAGE.md",
    "docs/接口契约_记忆.md",
    "docs/接口契约_身份与权限.md",
    "docs/接口契约_音频与信令.md",
    "docs/安语3D数字人_技术设计文档.md",
    "docs/ER_数据模型设计.md",
)

#: 上下文窗口：行号前后各取几行（行号本身有时指向装饰器、上一行是 docstring 等）
_WINDOW = 2


@dataclass
class Ref:
    doc: str
    doc_line: int
    raw: str
    path: str
    start: int
    end: int
    expectations: list[str]


@dataclass
class Result:
    ref: Ref
    status: str  # OK / DRIFT / MISSING
    actual: str = ""
    detail: str = ""


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _extract_expectations(
    doc_line_text: str, raw: str, ref_span: tuple[int, int] | None = None
) -> list[str]:
    """从引用**附近的文本**里提取「该行号处应该出现什么」。

    为什么只看附近而不是整行：一行里常并列多个引用
    （`signaling.py:826`/`:830` 两个上行分派），各引用该比对的是它自己旁边的词；
    整行取词会让所有引用共享一套期望，命中任何不相干的词就算通过。

    只保留「代码形态」的标识符（snake_case / camelCase / 全大写常量），
    滤掉 `HTTP`、`AGENTS` 这类文档用词——它们不是代码符号，比对必然假阳。
    """
    if ref_span is not None:
        lo = max(0, ref_span[0] - _EXPECT_RADIUS)
        hi = min(len(doc_line_text), ref_span[1] + _EXPECT_RADIUS)
        text = doc_line_text[lo:hi].replace(raw, " ")
    else:
        text = doc_line_text.replace(raw, " ")

    found: list[str] = []
    for m in _IDENTIFIER.finditer(text):
        found.append(m.group(1))
    for m in _QUOTED.finditer(text):
        candidate = m.group(1).strip()
        if candidate and not candidate.startswith(("http", "/", "\\")):
            found.append(candidate)
    for m in _CONST.finditer(text):
        found.append(m.group(1))
    for m in _IDENT_CALL.finditer(text):
        found.append(m.group(1))

    seen: set[str] = set()
    ordered: list[str] = []
    for item in found:
        if len(item) < 4 or item.isdigit():
            continue
        if item.lower() in _STOPWORDS:
            continue
        if not _CODE_SHAPE.match(item):
            continue
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered[:6]


def _collect_refs(doc: Path, root: Path) -> list[Ref]:
    try:
        lines = doc.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []

    refs: list[Ref] = []
    for idx, line in enumerate(lines, start=1):
        for m in _REF_PATTERN.finditer(line):
            raw = m.group(0)
            start = int(m.group("start"))
            end = int(m.group("end")) if m.group("end") else start
            refs.append(
                Ref(
                    doc=str(doc.relative_to(root)).replace("\\", "/"),
                    doc_line=idx,
                    raw=raw,
                    path=m.group("path"),
                    start=start,
                    end=end,
                    expectations=_extract_expectations(line, raw, m.span()),
                )
            )
    return refs


def _resolve(ref: Ref, root: Path) -> tuple[Path | None, str]:
    """把文档里的相对路径解析成真实文件。

    文档里的写法不统一，按可靠性依次尝试：

    1. 仓库根相对路径（`app/xxx.py`、`frontend/src/...`）—— 最可靠；
    2. 前端简写（`console/notifications.ts` → `frontend/src/console/notifications.ts`）——
       文档里常省略 `frontend/src/` 前缀；
    3. 按文件名全仓唯一匹配 —— 兜底，仅接受唯一命中，避免张冠李戴。
    """
    exact = root / ref.path
    if exact.is_file():
        return exact, ""

    # 前端简写补全
    for prefix in ("frontend/src/", "frontend/"):
        candidate = root / prefix / ref.path
        if candidate.is_file():
            return candidate, f"按前端简写补全为 {candidate.relative_to(root)}"

    # 后端裸文件名补全（文档里常写 `config.py:282` 而不是 `app/core/config.py:282`）：
    # 限定在 app/ 与 tests/ 下找，不扫 .venv / .tmp / node_modules，避免误命中第三方同名文件。
    name = Path(ref.path).name
    if "/" not in ref.path:
        for base in ("app", "tests", "mcp-servers", "scripts"):
            hits = [
                p
                for p in (root / base).rglob(name)
                if p.is_file() and ".venv" not in p.parts and "__pycache__" not in p.parts
            ]
            if len(hits) == 1:
                return hits[0], f"按裸文件名在 {base}/ 下唯一匹配到 {hits[0].relative_to(root)}"
            if len(hits) > 1:
                return None, (
                    f"{base}/ 下有 {len(hits)} 个 {name}"
                    f"（{', '.join(str(h.relative_to(root)) for h in hits[:3])}…），无法确定"
                )

    # 退一步：按文件名全仓搜（仅接受唯一命中）
    hits = [
        p
        for p in root.rglob(name)
        if p.is_file()
        and ".venv" not in p.parts
        and "node_modules" not in p.parts
        and ".git" not in p.parts
        and ".tmp" not in p.parts
        and "spikes" not in p.parts
    ]
    if len(hits) == 1:
        return hits[0], f"路径按文件名唯一匹配到 {hits[0].relative_to(root)}"
    if len(hits) > 1:
        return (
            None,
            f"文件名 {name} 在仓库内有 {len(hits)} 处（{', '.join(str(h.relative_to(root)) for h in hits[:3])}…），无法确定",
        )
    return None, f"文件不存在：{ref.path}"


def _check(ref: Ref, root: Path) -> Result:
    target, note = _resolve(ref, root)
    if target is None:
        return Result(ref=ref, status="MISSING", detail=note)

    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return Result(ref=ref, status="MISSING", detail=f"读取失败：{exc}")

    if not lines:
        return Result(ref=ref, status="MISSING", detail="文件为空")

    lo = max(1, ref.start - _WINDOW)
    hi = min(len(lines), ref.end + _WINDOW)
    window = "\n".join(lines[lo - 1 : hi])
    actual_line = lines[ref.start - 1] if ref.start <= len(lines) else ""

    if ref.start > len(lines):
        return Result(
            ref=ref,
            status="DRIFT",
            actual=actual_line,
            detail=f"文件只有 {len(lines)} 行，引用第 {ref.start} 行已越界",
        )

    if not ref.expectations:
        # 提取不到期望内容时不做判定，只提示（避免大量假阳）
        return Result(ref=ref, status="OK", actual=actual_line, detail="无可比对期望，仅验行号存在")

    # 大小写不敏感比对：文档常写环境变量名（VOICEPRINT_FERNET_KEY），
    # 代码里是同名属性（voiceprint_fernet_key）；两者指的是同一件事，
    # 用大小写敏感会把这类正确引用误报成漂移。
    window_folded = window.casefold()
    for expect in ref.expectations:
        if expect.casefold() in window_folded:
            return Result(ref=ref, status="OK", actual=actual_line, detail=f"命中 {expect!r}")

    # 兜底：该行是否在讲「这里定义了什么」——目标行本身非空且是定义/赋值/调用形态时，
    # 说明行号确实落在代码上，只是同一文档行里并列的多个引用让期望词串了位。
    # 这类报 WEAK 而不是 DRIFT：仍需人工扫一眼，但不该和真漂移混在一个计数里。
    stripped = actual_line.strip()
    if stripped and not stripped.startswith("#") and ref.start > 0:
        return Result(
            ref=ref,
            status="WEAK",
            actual=actual_line,
            detail="行号落在代码上但未命中期望（多引用同行或符号改名），建议人工确认",
        )

    return Result(
        ref=ref,
        status="DRIFT",
        actual=actual_line,
        detail="上下文未命中任何期望内容：" + ", ".join(repr(e) for e in ref.expectations[:3]),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验文档里的 file:line 引用是否仍然准确")
    parser.add_argument("--file", action="append", help="只校验指定文档（可重复）")
    parser.add_argument("--verbose", action="store_true", help="逐条打印通过的引用")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument(
        "--max-report",
        type=int,
        default=0,
        help="每份文档最多打印多少条问题（0 = 不限）",
    )
    args = parser.parse_args(argv)

    root = _repo_root()
    docs = args.file or list(DEFAULT_TARGETS)

    results: list[Result] = []
    for doc_name in docs:
        doc = root / doc_name
        if not doc.is_file():
            print(f"[skip] 文档不存在：{doc_name}", file=sys.stderr)
            continue
        for ref in _collect_refs(doc, root):
            results.append(_check(ref, root))

    ok = [r for r in results if r.status == "OK"]
    weak = [r for r in results if r.status == "WEAK"]
    drift = [r for r in results if r.status == "DRIFT"]
    missing = [r for r in results if r.status == "MISSING"]

    if args.json:
        payload = {
            "summary": {
                "total": len(results),
                "ok": len(ok),
                "weak": len(weak),
                "drift": len(drift),
                "missing": len(missing),
            },
            "drift": [
                {
                    "doc": r.ref.doc,
                    "doc_line": r.ref.doc_line,
                    "ref": r.ref.raw,
                    "actual": r.actual.strip()[:160],
                    "detail": r.detail,
                }
                for r in drift
            ],
            "weak": [
                {
                    "doc": r.ref.doc,
                    "doc_line": r.ref.doc_line,
                    "ref": r.ref.raw,
                    "actual": r.actual.strip()[:160],
                    "detail": r.detail,
                }
                for r in weak
            ],
            "missing": [
                {"doc": r.ref.doc, "doc_line": r.ref.doc_line, "ref": r.ref.raw, "detail": r.detail}
                for r in missing
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if not drift and not missing else 1

    if args.verbose:
        for r in ok:
            print(f"[OK]      {r.ref.doc}:{r.ref.doc_line}  {r.ref.raw}  ({r.detail})")

    by_doc: dict[str, list[Result]] = {}
    for r in drift + missing + (weak if args.verbose else []):
        by_doc.setdefault(r.ref.doc, []).append(r)

    for doc_name, items in sorted(by_doc.items()):
        print(f"\n=== {doc_name} · {len(items)} 处可疑 ===")
        shown = items if args.max_report <= 0 else items[: args.max_report]
        for r in shown:
            print(f"  [{r.status}] 第 {r.ref.doc_line} 行引用 {r.ref.raw}")
            if r.actual.strip():
                print(f"           实际： {r.actual.strip()[:150]}")
            print(f"           判定： {r.detail}")
        if args.max_report > 0 and len(items) > args.max_report:
            print(f"  … 另有 {len(items) - args.max_report} 处未显示")

    print(
        f"\n总计 {len(results)} 处引用：OK {len(ok)} / DRIFT {len(drift)} / MISSING {len(missing)}"
    )
    return 0 if not drift and not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
