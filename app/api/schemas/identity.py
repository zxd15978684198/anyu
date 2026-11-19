"""`POST /api/identity/confirm` 的请求/响应模型（按住说话降级链路的身份确认）。

## 为什么要有这一组 DTO

WS 链路里，长者点「是我」大按钮走的是 `sendIdentityConfirm`（C→S `identity_confirm`
帧）；按住说话的 REST 降级路径没有 WS 连接，只能用一个 HTTP 端点等价表达同一件事。
契约字段刻意与 WS 帧对齐（`elder_id` + `ask_id`），前端两条链路共用一份确认逻辑。

## 两个安全属性（写在模型里，而不是靠端点自觉）

- `elder_id` **必须显式给出**：`create` 场景下没有默认值，缺字段就是 422，而不是
  「默认按设备绑定的长者确认」——沉默的默认值会掩盖前端把会话对象串了。
- `extra="ignore"`：客户端多塞的 `thread_id` / `device_id` / `identity_source`
  一律丢弃，服务端没有任何地方会读到它们（AGENTS §4-9：身份只由服务端注入）。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

#: ask_id 的长度上限。它是服务端下发的确认单号，超长一定不是正常客户端。
MAX_ASK_ID_CHARS: Final[int] = 64

AskId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_ASK_ID_CHARS),
]


class IdentityConfirmRequest(BaseModel):
    """`{"elder_id": "<uuid>", "ask_id": "<string>"}`（`ask_id` 可省略）。

    `ask_id` 目前只做接口对齐（`conversation_threads` 没有该列），幂等判定走
    「该设备是否已有未结束的 confirm thread」，不依赖它。
    """

    model_config = ConfigDict(extra="ignore")

    elder_id: uuid.UUID = Field(description="长者 UUID；必须等于该设备绑定的长者")
    ask_id: AskId | None = Field(
        default=None,
        description="服务端确认单号（可选）；当前只做契约对齐，不落库",
    )


class IdentityConfirmResponse(BaseModel):
    """确认成功的返回值：WS 链路 `identity_result{source=confirm}` 的 REST 等价物。

    `created=false` 表示这台设备本来就有未结束的确认态会话，本次复用而没有新建
    ——前端据此避免重复播报开场白。
    """

    thread_id: str = Field(description="服务端生成的会话 thread_id（32 位 hex）")
    identity_source: Literal["confirm"] = Field(description="恒为 confirm")
    elder_id: uuid.UUID = Field(description="确认到的长者 UUID")
    created: bool = Field(description="true=本次新建；false=复用了未结束的确认态会话")
