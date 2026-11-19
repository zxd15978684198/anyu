#!/usr/bin/env python
"""P0-1 黑盒反证：按住说话命中急症后，安全链是否真的走通。

审计出处：`设计记录（长者端功能级联审计）` v1.0 §1-6 / §2-1（P0-1）。
缺点形态（改前）：`app/api/audio.py` 里 `reply_text = f"您说的是『{asr_text}』，我记下了。"`
—— 整条 L0 安全规则被绕开，急症句不落 `risk_events`、不建红单、不通知。

本脚本**只做黑盒**（真 HTTP + 真库），不 import 被审计实现、不进 pytest（它要真服务）：
  1. 用设备令牌登录，取 `elder_id`；
  2. 在临时目录生成一个 3s 单声道 16k WAV（正弦 + 淡入淡出，**不是真人音**）；
  3. 把 Mock ASR 夹具临时改成「终稿 = 我胸口疼」（跑完按原字节还原并核 SHA256）；
  4. `POST /api/audio/utterance`，断言两件事：
       A. 该长者 `risk_events` 多了一条（安全链落库）；
       B. 响应 `reply.text` 不再是「您说的是『…』，我记下了。」写死话术。

用法（改前跑必红、改后跑必绿）：

    # 1) 起一个 Mock ASR 的后端（不要打带 funasr 的常驻栈，否则 ASR 不是脚本）
    uv run uvicorn app.main:app --port 8010   # 另开终端，环境里 ASR_PROVIDER=mock

    # 2) 跑本脚本（默认打 8010；设备号/密钥默认取 seed_demo 的演示设备）
    uv run python scripts/audit-p0-1.py --base-url http://127.0.0.1:8010

    # 3) 退出码：0 = 绿（安全链已接上）；1 = 红（P0-1 仍在）；2 = 前置不满足

前置与边界：
- `ASR_PROVIDER` 必须是 mock（脚本用响应里的 `text` 是否等于注入句来验证，不等就判前置失败）；
- 需要能连上服务端所用的同一个库（读 `app.core.config` 的 DSN，只用于**数行数**，不写库）；
- 会临时改写 `app/audio/fixtures/asr_script.py`（Mock 夹具，不是产品逻辑）：若该文件已有
  未提交改动，脚本拒绝开跑（否则还原会覆盖别人的编辑）；跑完必还原并核 SHA256。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import struct
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import wave
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # 直接 python scripts/... 时 sys.path[0] 是 scripts/
    sys.path.insert(0, str(REPO_ROOT))
FIXTURE_PATH = REPO_ROOT / "app" / "audio" / "fixtures" / "asr_script.py"

# seed_demo.py 的演示设备（README §演示账号）；不是真实凭据。
DEFAULT_DEVICE_ID = "11111111-1111-4111-8111-111111111106"
DEFAULT_DEVICE_KEY = "demo-device-secret-0001"  # pragma: allowlist secret

DEFAULT_TEXT = "我胸口疼"
HARDCODED_TEMPLATE_MARK = "您说的是『"

EXIT_GREEN = 0
EXIT_RED = 1
EXIT_PRECONDITION = 2

_FIXTURE_TEMPLATE = '''"""临时夹具：由 scripts/audit-p0-1.py 改写，跑完会按原字节还原。"""

ASR_MOCK_SCRIPT: list[dict[str, object]] = [
    {{"text": "{partial}", "is_final": False}},
    {{"text": "{final}", "is_final": True}},
]

ASR_EMPTY_SCRIPT: list[dict[str, object]] = [
    {{"text": "", "is_final": False}},
    {{"text": "   ", "is_final": True}},
]
'''


class PreconditionError(RuntimeError):
    """环境/前置不满足：不是 P0-1 的红，而是「这次跑不能算数」。"""


def log(step: str, message: str) -> None:
    print(f"[{step}] {message}", flush=True)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- HTTP 客户端


def _post_json(
    base_url: str, path: str, payload: dict[str, Any], token: str | None = None
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(  # noqa: S310 - 地址来自 --base-url
        f"{base_url.rstrip('/')}{path}", data=body, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # 4xx/5xx 也把响应体带出来，便于判读
        detail = exc.read().decode("utf-8", errors="replace")
        raise PreconditionError(f"{path} 返回 {exc.code}: {detail[:400]}") from exc
    except urllib.error.URLError as exc:
        raise PreconditionError(f"连不上 {base_url}（{exc.reason}）——服务起了吗？") from exc


def _post_wav(base_url: str, token: str, wav: bytes, duration_ms: int) -> dict[str, Any]:
    boundary = f"----audit{int(time.time() * 1000)}"
    parts: list[bytes] = [
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="duration_ms"\r\n\r\n',
        str(duration_ms).encode(),
        b"\r\n",
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="audio"; filename="audit-p0-1.wav"\r\n',
        b"Content-Type: audio/wav\r\n\r\n",
        wav,
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    request = urllib.request.Request(  # noqa: S310 - 同上
        f"{base_url.rstrip('/')}/api/audio/utterance",
        data=b"".join(parts),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise PreconditionError(f"/api/audio/utterance 返回 {exc.code}: {detail[:400]}") from exc
    except urllib.error.URLError as exc:
        raise PreconditionError(f"/api/audio/utterance 不可达：{exc.reason}") from exc


# ---------------------------------------------------------------- 音频与夹具


def build_wav(seconds: float = 3.0, sample_rate: int = 16000) -> bytes:
    """3s 单声道 16k 正弦（220Hz，首尾各 100ms 淡入淡出），不掺任何真人音。"""
    frames = int(seconds * sample_rate)
    fade = int(0.1 * sample_rate)
    pcm = bytearray()
    for index in range(frames):
        gain = 1.0
        if index < fade:
            gain = index / fade
        elif index >= frames - fade:
            gain = (frames - index) / fade
        value = int(0.2 * gain * 32767 * math.sin(2 * math.pi * 220 * index / sample_rate))
        pcm += struct.pack("<h", value)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
        temp_path = Path(handle.name)
    try:
        with wave.open(str(temp_path), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(sample_rate)
            writer.writeframes(bytes(pcm))
        return temp_path.read_bytes()
    finally:
        temp_path.unlink(missing_ok=True)


def _git_dirty(path: Path) -> bool:
    import subprocess

    result = subprocess.run(  # noqa: S603 - 固定参数、无 shell
        ["git", "status", "--porcelain", "--", str(path.relative_to(REPO_ROOT))],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return bool(result.stdout.strip())


class FixtureInjector:
    """临时把 Mock ASR 的终稿改成指定句子；退出时按原始字节还原并核 SHA256。"""

    def __init__(self, text: str) -> None:
        self._text = text
        self._original: bytes | None = None
        self._original_sha = ""

    def __enter__(self) -> FixtureInjector:
        if not FIXTURE_PATH.is_file():
            raise PreconditionError(f"找不到 Mock 夹具：{FIXTURE_PATH}")
        if _git_dirty(FIXTURE_PATH):
            raise PreconditionError(
                "app/audio/fixtures/asr_script.py 有未提交改动——先提交/暂存再跑，"
                "否则还原会覆盖别人的编辑"
            )
        self._original = FIXTURE_PATH.read_bytes()
        self._original_sha = sha256(self._original)
        partial = self._text[: max(2, len(self._text) // 2)]
        FIXTURE_PATH.write_text(
            _FIXTURE_TEMPLATE.format(partial=partial, final=self._text), encoding="utf-8"
        )
        log(
            "fixture",
            f"已把 ASR 终稿临时改为「{self._text}」（跑完还原，原 SHA {self._original_sha[:12]}…）",
        )
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._original is None:
            return
        FIXTURE_PATH.write_bytes(self._original)
        restored = sha256(FIXTURE_PATH.read_bytes())
        status = "一致" if restored == self._original_sha else "不一致！"
        log("fixture", f"已按原字节还原，SHA256 {status}（{restored[:12]}…）")
        if restored != self._original_sha:
            print("!! 夹具还原校验失败，请手动 git checkout 该文件", file=sys.stderr)


# ---------------------------------------------------------------- 库：只数行数


async def count_risk_events(elder_id: str) -> tuple[int, list[str]]:
    import asyncpg

    from app.core.config import get_settings

    dsn = str(get_settings().database_url).replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn=dsn, timeout=10)
    try:
        total = await conn.fetchval(
            "SELECT count(*) FROM risk_events WHERE elder_id = $1::uuid", elder_id
        )
        rows = await conn.fetch(
            "SELECT type, severity, created_at FROM risk_events "
            "WHERE elder_id = $1::uuid ORDER BY created_at DESC LIMIT 3",
            elder_id,
        )
    finally:
        await conn.close()
    described = [f"{row['type']}/{row['severity']}@{row['created_at']}" for row in rows]
    return int(total or 0), described


# ---------------------------------------------------------------- 主流程


def main() -> int:
    # Windows 控制台默认 GBK：中文断言原文会被读成乱码（与 N50 的反证脚本同一个坑）
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="P0-1 黑盒反证（按住说话 → 安全链）")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("ANYU_AUDIT_BASE_URL", "http://127.0.0.1:8010"),
        help="后端地址；默认 http://127.0.0.1:8010（Mock ASR 专用端口，别打常驻栈）",
    )
    parser.add_argument(
        "--device-id", default=os.environ.get("ANYU_AUDIT_DEVICE_ID", DEFAULT_DEVICE_ID)
    )
    parser.add_argument(
        "--device-key", default=os.environ.get("ANYU_AUDIT_DEVICE_KEY", DEFAULT_DEVICE_KEY)
    )
    parser.add_argument("--text", default=DEFAULT_TEXT, help="注入给 Mock ASR 的终稿")
    args = parser.parse_args()

    try:
        return _run(args)
    except PreconditionError as exc:
        print(f"[前置失败] {exc}", file=sys.stderr)
        return EXIT_PRECONDITION


def _run(args: argparse.Namespace) -> int:
    base_url = args.base_url.rstrip("/")

    log("1/6", f"设备令牌登录：{base_url}/api/device-sessions")
    session = _post_json(
        base_url,
        "/api/device-sessions",
        {"device_id": args.device_id, "device_secret": args.device_key},
    )
    token = session.get("access_token")
    elder_id = (session.get("device") or {}).get("elder_id")
    if not token:
        raise PreconditionError(f"登录响应里没有 access_token：{session}")
    if not elder_id:
        raise PreconditionError("演示设备未绑定长者（elder_id 为空），无法验证 risk_events")
    uuid.UUID(str(elder_id))
    log("1/6", f"OK：elder_id={elder_id}")

    log("2/6", "生成 3s 16k 单声道 WAV（正弦，非真人音）")
    wav = build_wav()
    duration_ms = 3_000
    log("2/6", f"OK：{len(wav)} 字节")

    with FixtureInjector(args.text):
        log("3/6", "改动前数一次 risk_events")
        before_total, before_rows = _count_risk_events_sync(elder_id)
        log("3/6", f"OK：改前 {before_total} 条（最近：{before_rows or '无'}）")

        log("4/6", "POST /api/audio/utterance")
        response = _post_wav(base_url, token, wav, duration_ms)
        text = str(response.get("text") or "")
        reply = response.get("reply") or {}
        reply_text = str(reply.get("text") or "")
        log("4/6", f"OK：text=「{text}」 reply.text=「{reply_text}」")

    if text.strip() != args.text:
        raise PreconditionError(
            f"ASR 没走 Mock 脚本（收到「{text}」，期望「{args.text}」）。"
            "请用 ASR_PROVIDER=mock 起后端（uvicorn --port 8010），别打带 funasr 的常驻栈"
        )

    log("5/6", "断言 A：risk_events 必须多一条")
    after_total, after_rows = _count_risk_events_sync(elder_id)
    log("5/6", f"改后 {after_total} 条（最近：{after_rows or '无'}）")

    log("6/6", "断言 B：reply.text 不得是写死话术")
    failures: list[str] = []
    if after_total <= before_total:
        failures.append(
            f"A 失败：risk_events 没有新增（{before_total} → {after_total}）"
            "——安全链没走到，急症句只被当成普通聊天"
        )
    if HARDCODED_TEMPLATE_MARK in reply_text:
        failures.append(
            f"B 失败：reply.text 仍是写死话术「{reply_text}」"
            '（app/api/audio.py 里 f"您说的是『{asr_text}』，我记下了。"）'
        )
    if not reply_text.strip():
        failures.append("B 失败：reply.text 为空，前端无话可播")

    print()
    if failures:
        print("结论：红（P0-1 未闭环）", file=sys.stderr)
        for item in failures:
            print(f"  - {item}", file=sys.stderr)
        return EXIT_RED
    print(
        f"结论：绿（P0-1 已闭环）——risk_events {before_total} → {after_total}，reply.text=「{reply_text}」"
    )
    return EXIT_GREEN


def _count_risk_events_sync(elder_id: str) -> tuple[int, list[str]]:
    import asyncio

    return asyncio.run(count_risk_events(elder_id))


if __name__ == "__main__":
    sys.exit(main())
