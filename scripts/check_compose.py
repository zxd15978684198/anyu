"""docker-compose.yml 不变量校验（迭代 10 · W2）。

为什么需要它：`docker compose config` 只判语法，判不出「端口裸奔公网」「服务没健康检查」
「backend 没等数据库」这类**编排语义**问题；而这几条正是 §6.5 反向验收第 3 条的靶子。

五条判据（任一违反即 exit 1，并逐条打印具体条目）：

- **C1 服务齐全**：候选服务数 ≥ 7，且七服务名齐（postgres / redis / backend / frontend /
  weather-mcp / mock-operations-mcp / coturn）。`laya` 挂在 `profiles: [laya]` 下（缺省不起、
  不阻塞主栈），有则一并纳入 C2 的健康检查校验，不列为硬门槛。
- **C2 每服务有 healthcheck**：`healthcheck.test` 必须非空，且不能 `disable: true`。
- **C3 backend 依赖健康**：`depends_on` 里必须同时有 `postgres` 与 `redis`，且两者
  `condition: service_healthy`（短语法 `/` 无 `condition` 一律算违反）。
- **C4 端口不裸奔**（S-D10-4）：除 80 / 443 / 3478 / 5349 外，每条 `ports` 的宿主侧都必须绑
  `127.0.0.1`（或 `::1`）；短语法只写 `宿主:容器` 等于绑 `0.0.0.0`，判违反。
- **C5 无明文密钥**：文件正文不得出现 `password=` / `secret=` / `api_key=` 式的字面值
  （`${VAR}` 占位与空值放行）。

实现约定：

- **不读 `.env`**。只解析 compose 文件本身，`${VAR}` 保持原样；C5 也只在原文上扫。
- 优先用 `.venv` 里的 `pyyaml`；没有则退化到 `docker compose config --format json`
  （注意该路径会按 `.env` 展开变量，只用于 C1–C4；C5 始终扫原文）。
- 纯标准库 + 可选 pyyaml，无第三方必需依赖。

用法::

    .\\.venv\\Scripts\\python.exe scripts\\check_compose.py
    .\\.venv\\Scripts\\python.exe scripts\\check_compose.py --file <临时副本>

退出码：0 全过 / 1 有违反 / 2 用法或解析错误。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_COMPOSE = REPO_ROOT / "docker-compose.yml"

#: 七服务名（README §6.2 端口表 + 计划 §4-W2）。laya 因 profiles 不在此列，见模块文档。
REQUIRED_SERVICES: tuple[str, ...] = (
    "postgres",
    "redis",
    "backend",
    "frontend",
    "weather-mcp",
    "mock-operations-mcp",
    "coturn",
)

#: 显式挂 profiles、缺省不启动的服务；出现则校验，不出现也不判失败。
PROFILE_SERVICES: tuple[str, ...] = ("laya",)

MIN_SERVICES = 7

#: 允许对外绑定的端口：前端 HTTP/HTTPS 与 coturn 的 STUN/TURN。
PUBLIC_EXEMPT_PORTS = frozenset({80, 443, 3478, 5349})

#: 视为「环回」的宿主地址。
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

#: backend 必须等这两个服务 healthy。
REQUIRED_HEALTHY_DEPS: tuple[str, ...] = ("postgres", "redis")

#: `${...}` 占位符整体替换掉：避免把 `${POSTGRES_PASSWORD:-xx}` 里的 `PASSWORD:` 误判成明文。
PLACEHOLDER_RE = re.compile(r"\$\{[^}]*\}")

#: `password=xxx` / `secret: xxx` / `api_key=xxx` 形态；`${` 前导已排除。
SECRET_KV_RE = re.compile(
    r"(?i)(?<![{$])(?P<key>password|passwd|secret|api[_-]?key|credential)\s*[=:]\s*(?P<value>\S+)"
)


def _as_int(value: Any) -> int | None:
    """把端口值转 int；不可转（变量、区间、None）返回 None。"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def _load_compose(path: Path) -> tuple[dict[str, Any], str]:
    """返回 (解析后的文档, 文件原文)。优先 pyyaml，缺失时退回 docker compose JSON。"""
    text = path.read_text(encoding="utf-8")
    try:
        import yaml
    except ModuleNotFoundError:
        return _load_via_docker(path), text
    document = yaml.safe_load(text)
    if not isinstance(document, dict):
        raise SystemExit(f"check_compose: {path} 顶层不是映射，无法解析")
    return document, text


def _load_via_docker(path: Path) -> dict[str, Any]:
    """yaml 不可用时的兜底：让 compose 自己静态展开成 JSON。"""
    argv = ["docker", "compose", "-f", str(path), "config", "--format", "json"]
    try:
        proc = subprocess.run(  # noqa: S603
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
            cwd=str(path.parent),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"check_compose: 解析 YAML 失败，且 docker 不可用：{exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise SystemExit(f"check_compose: `docker compose config` 失败：\n{detail}")
    document = json.loads(proc.stdout)
    if not isinstance(document, dict):
        raise SystemExit("check_compose: `docker compose config` 输出不是对象")
    return document


def _services(document: dict[str, Any]) -> dict[str, Any]:
    services = document.get("services")
    if not isinstance(services, dict) or not services:
        raise SystemExit("check_compose: 文件里没有 services 段")
    return services


def _check_services(services: dict[str, Any]) -> tuple[list[str], str]:
    problems: list[str] = []
    missing = [name for name in REQUIRED_SERVICES if name not in services]
    if missing:
        problems.append(f"缺服务：{', '.join(missing)}")
    if len(services) < MIN_SERVICES:
        problems.append(f"服务数 {len(services)} < {MIN_SERVICES}")
    profile = [name for name in PROFILE_SERVICES if name in services]
    detail = f"{len(services)} 个服务；必须项 {len(REQUIRED_SERVICES) - len(missing)}/{len(REQUIRED_SERVICES)}"
    detail += f"；profile 内：{', '.join(profile) if profile else '无'}"
    return problems, detail


def _check_healthchecks(services: dict[str, Any]) -> tuple[list[str], str]:
    problems: list[str] = []
    for name in sorted(services):
        healthcheck = services[name].get("healthcheck")
        if not isinstance(healthcheck, dict) or not healthcheck.get("test"):
            problems.append(f"{name}: 缺 healthcheck.test")
        elif healthcheck.get("disable") is True:
            problems.append(f"{name}: healthcheck 被 disable，等于没有")
    return problems, f"{len(services)}/{len(services)} 服务已声明 healthcheck"


def _check_backend_deps(services: dict[str, Any]) -> tuple[list[str], str]:
    problems: list[str] = []
    backend = services.get("backend")
    if not isinstance(backend, dict):
        return ["没有 backend 服务，无法校验依赖"], "n/a"
    depends_on = backend.get("depends_on")
    if not isinstance(depends_on, dict):
        return [f"backend.depends_on 不是带 condition 的映射（当前：{depends_on!r}）"], "n/a"
    summary: list[str] = []
    for dep in REQUIRED_HEALTHY_DEPS:
        config = depends_on.get(dep)
        if config is None:
            problems.append(f"backend 未 depends_on {dep}")
        elif not isinstance(config, dict):
            problems.append(f"backend.depends_on.{dep} 用了短语法，没有 condition")
        elif config.get("condition") != "service_healthy":
            problems.append(
                f"backend.depends_on.{dep}.condition = {config.get('condition')!r}，应为 service_healthy"
            )
        else:
            summary.append(f"{dep}=service_healthy")
    return problems, "，".join(summary) if summary else "n/a"


def _port_triple(entry: Any) -> tuple[str | None, str | None, str | None]:
    """把 ports 条目归一成 (host_ip, published, target)。兼容短/长两种语法。"""
    if isinstance(entry, dict):
        return (
            _text(entry.get("host_ip")),
            _text(entry.get("published")),
            _text(entry.get("target")),
        )
    spec = str(entry).split("/", 1)[0]
    parts = spec.split(":")
    if len(parts) >= 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return None, parts[0], parts[1]
    return None, None, parts[0]


def _check_ports(services: dict[str, Any]) -> tuple[list[str], str]:
    problems: list[str] = []
    total = 0
    exempt: list[str] = []
    for name in sorted(services):
        entries = services[name].get("ports") or []
        if not isinstance(entries, list):
            problems.append(f"{name}: ports 不是列表")
            continue
        for entry in entries:
            total += 1
            host_ip, published, target = _port_triple(entry)
            port = _as_int(published)
            if port is None:
                port = _as_int(target)
            if port is not None and port in PUBLIC_EXEMPT_PORTS:
                exempt.append(f"{name}:{port}")
                continue
            if host_ip is None:
                problems.append(f"{name}: 端口 {entry!r} 没写 host_ip（等于绑 0.0.0.0）")
            elif host_ip.lower() not in LOOPBACK_HOSTS:
                problems.append(f"{name}: 端口 {entry!r} 绑在 {host_ip}（非环回）")
    detail = f"{total} 条映射，环回 {total - len(exempt)} 条；对外豁免：{', '.join(exempt) or '无'}"
    return problems, detail


def _check_plaintext_secrets(text: str) -> tuple[list[str], str]:
    """扫正文里的字面密钥。`${VAR}` 占位放行，空值放行。"""
    problems: list[str] = []
    scanned = PLACEHOLDER_RE.sub("${VAR}", text)
    for lineno, line in enumerate(scanned.splitlines(), start=1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        for match in SECRET_KV_RE.finditer(line):
            value = match.group("value").strip("\"'")
            if value.startswith("$"):
                continue  # 变量注入（含 ${VAR} / $VAR），不算明文
            problems.append(
                f"第 {lineno} 行：{match.group('key')} 出现字面值 {value!r}（应改用 ${{VAR}}）"
            )
    return problems, "0 处明文" if not problems else f"{len(problems)} 处"


def _force_utf8_stdio() -> None:
    """Windows 默认 cp936，中文报告会乱码；统一按 UTF-8 输出。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


CHECKS: tuple[tuple[str, str, Any], ...] = (
    ("C1", "七服务齐全", _check_services),
    ("C2", "每服务有 healthcheck", _check_healthchecks),
    ("C3", "backend 等 postgres/redis healthy", _check_backend_deps),
    ("C4", "端口只绑环回（豁免 80/443/3478/5349）", _check_ports),
)


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(
        description="校验 docker-compose.yml 的编排不变量（迭代 10 · W2）"
    )
    parser.add_argument(
        "--file",
        type=Path,
        default=DEFAULT_COMPOSE,
        help=f"要校验的编排文件（默认 {DEFAULT_COMPOSE.relative_to(REPO_ROOT)}）",
    )
    args = parser.parse_args(argv)
    path: Path = args.file
    if not path.is_file():
        print(f"check_compose: 找不到文件 {path}", file=sys.stderr)
        return 2

    document, text = _load_compose(path)
    services = _services(document)

    print(f"check_compose: {path}")
    print(f"  services ({len(services)}): {', '.join(sorted(services))}")

    failures = 0
    for code, title, check in CHECKS:
        problems, detail = check(services)
        status = "OK  " if not problems else "FAIL"
        print(f"  [{status}] {code} {title} —— {detail}")
        for problem in problems:
            print(f"         · {problem}")
        failures += len(problems)

    secret_problems, secret_detail = _check_plaintext_secrets(text)
    status = "OK  " if not secret_problems else "FAIL"
    print(f"  [{status}] C5 无明文密钥 —— {secret_detail}")
    for problem in secret_problems:
        print(f"         · {problem}")
    failures += len(secret_problems)

    if failures:
        print(f"FAIL: {failures} 项违反")
        return 1
    print("PASS: 5/5 判据通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
