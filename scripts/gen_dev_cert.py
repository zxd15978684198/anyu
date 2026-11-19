"""本地开发自签证书生成（迭代 10 · W3 接入层）。

## 用途

`frontend/nginx.https.conf` 需要 `certs/dev.crt` + `certs/dev.key`。getUserMedia
（喊「小伴」→ 建连 → 全双工语音）只在**安全上下文**里可用：`localhost` 被浏览器豁免，
但用 127.0.0.1 之外的主机名或局域网 IP 打开 pad 时，没有 HTTPS 连麦克风权限都拿不到
（计划 S-D10-5：自签证书即可，不强制 mkcert）。

## 为什么调系统 openssl 而不是 import cryptography

`cryptography` 不在本项目依赖里（`uv.lock` 里没有它）。为一个只跑一次的证书去加一条
主力依赖，违反 AGENTS §12「不擅自引入新框架」；而往 `.venv` 里临时 `pip install`
又会污染 uv 管理的环境（AGENTS §3：包管理器固定 uv，不混用 pip）。
系统 openssl 在 Git for Windows、macOS、主流 Linux 上都有，是这里唯一干净的路子。

## 行为

1. `dev.crt` 与 `dev.key` **都在** → 幂等跳过，只打印现有证书的主体与有效期（退出码 0）；
2. 只缺一个（半残状态）→ 两个一起重生成：证书与私钥不配对时 nginx 起不来，
   而「补一个」在密码学上不成立，只能重签；
3. 找不到 openssl → 打印分平台安装指引并 exit 1（绝不静默跳过，否则后面是
   nginx `cannot load certificate` 的玄学故障）；
4. `--force` → 无视已有文件强制重签。

生成物落在仓库根的 `certs/`，`.gitignore` 第 1 段（密钥与环境变量）已经忽略 `certs/`
与 `*.key`，不会误入库；compose 侧把它挂到 `/etc/nginx/certs/`。

## 用法

    uv run python scripts/gen_dev_cert.py          # 首次生成 / 幂等跳过
    uv run python scripts/gen_dev_cert.py --force  # 换 CN/SAN 后重签
"""

from __future__ import annotations

import argparse
import contextlib
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

#: 仓库根（本文件在 scripts/ 下）
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CERT_DIR = REPO_ROOT / "certs"

CERT_NAME = "dev.crt"
KEY_NAME = "dev.key"

#: CN 给老客户端看，现代浏览器只看 SAN —— 真正的可达性来自下面这串 SAN
CERT_SUBJECT = "/CN=localhost"
DEFAULT_HOST_NAMES: tuple[str, ...] = ("localhost",)
DEFAULT_HOST_IPS: tuple[str, ...] = ("127.0.0.1",)
CERT_DAYS = "365"
RSA_BITS = "2048"

#: openssl < 1.1.1 没有 `-addext`，退回用配置文件写扩展（下面这个模板）
#: `CA:TRUE` 与 `req -x509` 的默认一致：自签证书本身即自签根，手机端
#: 也正是按「CA 证书」这一档来安装它（安卓只允许安装 CA 证书到用户存储）。
FALLBACK_CNF = """\
[req]
prompt             = no
distinguished_name = dn
x509_extensions    = v3_req

[dn]
CN = localhost

[v3_req]
subjectAltName   = {san}
basicConstraints = critical, CA:TRUE
keyUsage         = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
"""


def build_san(host_names: Sequence[str], host_ips: Sequence[str]) -> str:
    """把主机名与 IP 拼成 openssl 的 SAN 串（顺序稳定，去重保序）。"""
    parts: list[str] = []
    for name in _dedupe(host_names):
        parts.append(f"DNS:{name}")
    for ip in _dedupe(host_ips):
        parts.append(f"IP:{ip}")
    return ",".join(parts)


def _dedupe(values: Sequence[str]) -> list[str]:
    """去空白、去重、保序——`--host-ip` 传重复值时 SAN 不会重复出现。"""
    seen: dict[str, None] = {}
    for raw in values:
        item = (raw or "").strip()
        if item:
            seen.setdefault(item, None)
    return list(seen)


INSTALL_HINTS = """\
找不到 openssl，无法生成自签证书。请先安装（任选其一）：

  Windows  · Git for Windows 自带（`<Git 安装目录>\\usr\\bin\\openssl.exe`），
             装完 Git 后确认 `where openssl` 能找到；或在 PowerShell 里
             `winget install ShiningLight.OpenSSL.Light`
  macOS    · 系统自带 /usr/bin/openssl（LibreSSL），或 `brew install openssl@3`
  Debian/Ubuntu · `sudo apt-get install -y openssl`
  Alpine   · `apk add --no-cache openssl`
  RHEL/CentOS   · `sudo dnf install -y openssl`

装完重开终端（PATH 才会生效）再跑一次本脚本。"""


def find_openssl() -> str | None:
    """在 PATH 里找 openssl 可执行文件；找不到返回 None。"""
    return shutil.which("openssl")


def _supports_addext(openssl: str) -> bool:
    """探测 openssl 是否支持 `req -addext`（1.1.1+ 才有）。

    `openssl req -help` 的用法文本会带上 `-addext`，据此判断比「先跑一次、
    失败再猜错误信息」可靠：版本过旧时不会在磁盘上留下半张证书。
    """
    try:
        proc = subprocess.run(  # noqa: S603 - 参数是固定列表，openssl 来自 which()
            [openssl, "req", "-help"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    return "-addext" in (proc.stdout + proc.stderr)


def _openssl_req(
    openssl: str, out_dir: Path, cert: Path, key: Path, *, addext: bool, san: str
) -> int:
    """执行一次证书签发；返回 openssl 的退出码（0 为成功）。"""
    cmd = [
        openssl,
        "req",
        "-x509",
        "-newkey",
        f"rsa:{RSA_BITS}",
        "-nodes",
        "-sha256",  # 显式指定摘要：老版本 openssl 默认是 sha1，现代浏览器会拒收
        "-days",
        CERT_DAYS,
        "-keyout",
        str(key),
        "-out",
        str(cert),
    ]

    cnf_path: Path | None = None
    if addext:
        cmd += [
            "-subj",
            CERT_SUBJECT,
            "-addext",
            f"subjectAltName={san}",
            # coturn 的 TLS 监听与 nginx 共用这张证书，两个扩展都显式写，
            # 免得将来换 TLS 栈时被「缺 EKU」卡住。
            "-addext",
            "keyUsage=digitalSignature,keyEncipherment",
            "-addext",
            "extendedKeyUsage=serverAuth",
        ]
    else:
        # 老版本 openssl：扩展只能写在配置文件里，用完即删
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".cnf", dir=out_dir, delete=False
        ) as handle:
            handle.write(FALLBACK_CNF.format(san=san))
            cnf_path = Path(handle.name)
        cmd += ["-config", str(cnf_path)]

    print("  执行: " + " ".join(cmd))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603
    except OSError as exc:  # openssl 在 PATH 里但不可执行（权限/损坏）
        print(f"[FAIL] 调用 openssl 失败: {exc}", file=sys.stderr)
        return 1
    finally:
        if cnf_path is not None:
            with contextlib.suppress(OSError):
                cnf_path.unlink()

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        print(f"[FAIL] openssl 退出码 {proc.returncode}:\n{detail}", file=sys.stderr)
    return proc.returncode


def describe(openssl: str, cert: Path) -> None:
    """打印证书主体、SAN 与有效期（失败不影响主流程）。"""
    try:
        proc = subprocess.run(  # noqa: S603
            [
                openssl,
                "x509",
                "-in",
                str(cert),
                "-noout",
                "-subject",
                "-dates",
                "-ext",
                "subjectAltName",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return
    for line in (proc.stdout or "").splitlines():
        if line.strip():
            print(f"  {line.strip()}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="生成本地开发用自签证书（certs/dev.crt + certs/dev.key）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="无视已有证书强制重签（换 CN/SAN，或浏览器已记住旧指纹时用）",
    )
    parser.add_argument(
        "--cert-dir",
        type=Path,
        default=DEFAULT_CERT_DIR,
        help=f"证书输出目录（默认 {DEFAULT_CERT_DIR}）",
    )
    parser.add_argument(
        "--host-ip",
        action="append",
        default=None,
        metavar="IP",
        help=(
            "额外写进 SAN 的 IP，可重复（如 --host-ip 192.168.1.10）。"
            "手机/平板用局域网 IP 访问时必须带，否则浏览器报「证书名不匹配」并"
            "拒绝摄像头麦克风权限"
        ),
    )
    parser.add_argument(
        "--host-name",
        action="append",
        default=None,
        metavar="NAME",
        help="额外写进 SAN 的主机名，可重复（默认只签 localhost）",
    )
    args = parser.parse_args(argv)

    host_names = list(DEFAULT_HOST_NAMES) + list(args.host_name or [])
    host_ips = list(DEFAULT_HOST_IPS) + list(args.host_ip or [])
    san = build_san(host_names, host_ips)

    cert_dir: Path = args.cert_dir
    cert = cert_dir / CERT_NAME
    key = cert_dir / KEY_NAME

    print(f"证书目录: {cert_dir}")

    has_cert, has_key = cert.is_file(), key.is_file()
    if has_cert and has_key and not args.force:
        print("[SKIP] 证书与私钥都已存在，幂等跳过（要重签加 --force）")
        openssl = find_openssl()
        if openssl:
            describe(openssl, cert)
        return 0

    if args.force and (has_cert or has_key):
        print("[INFO] --force：覆盖已有证书")
    elif has_cert != has_key:
        missing = f"{KEY_NAME}（私钥）" if has_cert else f"{CERT_NAME}（证书）"
        print(f"[WARN] 只找到其中一个文件，缺 {missing}；证书与私钥必须配对，改为两个一起重签")

    openssl = find_openssl()
    if openssl is None:
        print(INSTALL_HINTS, file=sys.stderr)
        return 1
    print(f"openssl: {openssl}")

    cert_dir.mkdir(parents=True, exist_ok=True)

    addext = _supports_addext(openssl)
    print(f"签发方式: {'-addext' if addext else '配置文件扩展（openssl 版本较旧）'}")
    print(f"有效期: {CERT_DAYS} 天；SAN: {san}")

    rc = _openssl_req(openssl, cert_dir, cert, key, addext=addext, san=san)
    if rc != 0:
        return rc

    # 私钥只给自己读（Windows 上 chmod 基本是空操作，失败不算错）
    if sys.platform != "win32":
        with contextlib.suppress(OSError):
            key.chmod(0o600)

    if not (cert.is_file() and key.is_file()):
        print("[FAIL] openssl 返回 0 但没有产物，请检查目录权限", file=sys.stderr)
        return 1

    print(f"[OK] 已生成 {cert}")
    print(f"[OK] 已生成 {key}")
    describe(openssl, cert)
    print("提示: certs/ 已被 .gitignore 忽略；compose 里挂到 /etc/nginx/certs/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
