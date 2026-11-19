"""结构化日志：一行一个 JSON，每行必带 `trace_id`。

迭代排期 1.4 验收原文：「日志每行有 trace_id」。因此 trace_id 不是可选字段 ——
没有值时写 `"-"`，保证 grep 与采集侧字段恒定（技术文档 §1.2 全链路统一 trace_id）。

不引入 structlog：AGENTS.md §4 禁区 12「不擅自引入新框架」，标准库足够。
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from app.core.context import get_thread_id, get_trace_id, get_turn_id
from app.core.redact import redact_mapping, redact_text

# LogRecord 自带的属性，打日志时不当成业务字段
_RESERVED_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
        # uvicorn 自带：ANSI 着色的重复消息，进 JSON 只会污染日志
        "color_message",
    }
)

_SERVICE_FIELD = "service"


class JsonFormatter(logging.Formatter):
    """把 LogRecord 渲染成单行 JSON；业务字段用 `logger.info("...", extra={...})` 传入。

    **落盘前脱敏**（迭代 9 · W1，技术设计文档 §1.2 第 8 条 / PRD AC-36）：

    - `msg` 走 `redact_text`：自由文本里嵌了手机号/身份证/密钥也能抓到；
    - `extra` 走 `redact_mapping`：按键名识别，`extra={"user_phone": "N/A"}` 这种
      「值不像手机号」的字段同样会整体打码；
    - 异常栈与 `stack_info` 同样过一遍 `redact_text`（异常消息里带号码是常见泄露路径）。

    既有行为保持不变：字段白名单、`trace_id` 恒有值、`ensure_ascii=False` 逐字节不动，
    只是给每个值多套一层掩码 —— 掩码函数对不含敏感信息的值是恒等变换。
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact_text(record.getMessage()),
            "trace_id": get_trace_id() or "-",
            # 三 ID 同源（S4）：trace 来自请求 / span，thread 与 turn 来自服务端编排。
            # 不设时同样写 "-"，保证采集侧字段恒定（AGENTS §8：每行都带 trace_id 的延伸）。
            "thread_id": get_thread_id() or "-",
            "turn_id": get_turn_id() or "-",
        }

        # extra={...} 带进来的业务字段（不许覆盖保留字段），脱敏后再并入
        extra = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RESERVED_ATTRS and key not in payload and not key.startswith("_")
        }
        payload.update(redact_mapping(extra))

        if record.exc_info:
            payload["exc"] = redact_text(self.formatException(record.exc_info))
        if record.stack_info:
            payload["stack"] = redact_text(self.formatStack(record.stack_info))

        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO", *, service: str = "anyu", force: bool = False) -> None:
    """幂等配置根日志器；重复调用不会叠加 handler（uvicorn --reload 会重复 import）。"""
    root = logging.getLogger()
    if getattr(root, "_anyu_configured", False) and not force:
        return

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter())

    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn 自带 handler 会绕过 JSON 格式，这里收编到根日志器
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True

    root._anyu_configured = True  # type: ignore[attr-defined]

    logging.getLogger(__name__).info(
        "logging configured", extra={_SERVICE_FIELD: service, "log_level": level.upper()}
    )
