"""照护台读接口的响应模型（W4）。

owner=照护台 W4 后端读接口 · 依据 设计记录（照护台前端） §3.3 / §5-W4

四条读端点（E6/E7 的照护台视图）共用本文件的 DTO，敏感字段口径统一在此声明：

- 手机号只出脱敏形式 `phone_masked`，任何响应都不回传完整号码；
- 员工 / 家属视图不含 `password_hash`、`token_version`（不是「默认不序列化」，是压根没这字段）；
- 设备视图不含 `token_hash` / `secret`；`bound` 是 `elder_id` 是否存在的派生值。

**两处与计划文档的偏差（不改 schema 硬凑）**：

1. 设备项没有 `device_code` —— `devices` 表无此列，且
   `设计记录（音频传输与设备链路技术方案）` 的 v1.3 裁决已明确
   「device_code 彻底出局，不新增任何字段/迁移」，加列即违约。
2. 家属项没有 `created_at` —— `guardian_accounts` 建表时就没有该列
   （`alembic/versions/0002_identity.py`），同样不允许新增迁移。
"""

from __future__ import annotations

import datetime
import uuid

from pydantic import BaseModel


class StaffListItemResponse(BaseModel):
    """员工列表项（`GET /api/staff`）。"""

    staff_id: uuid.UUID
    name: str
    role: str
    phone_masked: str
    status: str
    elder_ids: list[uuid.UUID]


class StaffListResponse(BaseModel):
    items: list[StaffListItemResponse]
    total: int
    page: int
    page_size: int


class GuardianListItemResponse(BaseModel):
    """家属列表项（`GET /api/guardians`）。"""

    guardian_id: uuid.UUID
    name: str
    phone_masked: str
    status: str


class GuardianListResponse(BaseModel):
    items: list[GuardianListItemResponse]
    total: int
    page: int
    page_size: int


class DeviceListItemResponse(BaseModel):
    """设备列表项（`GET /api/devices`）。`last_seen_at` 来自 `devices.last_heartbeat`。

    `status` 是设备自身状态（`online` / `offline` / `locked`，见 `devices.status`），
    与派生的 `bound` 分开：解绑会把状态置 `offline`，但离线设备也可能仍在绑定中。
    """

    device_id: uuid.UUID
    elder_id: uuid.UUID | None
    elder_name: str | None
    room_no: str | None
    bed_no: str | None
    bound: bool
    status: str | None = None
    last_seen_at: datetime.datetime | None


class DeviceListResponse(BaseModel):
    items: list[DeviceListItemResponse]
    total: int
    page: int
    page_size: int


class ElderGuardianItemResponse(BaseModel):
    """某长者已绑家属项（`GET /api/elders/{elder_id}/guardians`）。

    `binding_id` 是绑定行 `elder_guardians.id`，**不是**家属账号 ID：解绑端点
    `DELETE /api/bindings/{binding_id}` 要的就是它，`guardian_id` 仍是账号 ID。
    """

    guardian_id: uuid.UUID
    binding_id: uuid.UUID
    name: str
    phone_masked: str
    relation: str
    bound_at: datetime.datetime


class ElderGuardianListResponse(BaseModel):
    items: list[ElderGuardianItemResponse]
    total: int


__all__ = [
    "DeviceListItemResponse",
    "DeviceListResponse",
    "ElderGuardianItemResponse",
    "ElderGuardianListResponse",
    "GuardianListItemResponse",
    "GuardianListResponse",
    "StaffListItemResponse",
    "StaffListResponse",
]
