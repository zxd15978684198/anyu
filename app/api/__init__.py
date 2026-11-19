"""REST 路由层：只做参数校验 + 权限范围过滤 + 调 `app.services`，不写业务逻辑、不碰 ORM 写库。

## 路由注册表（T0 · 2026-11-06）

`app/main.py` 不再逐条手写 `include_router`，而是遍历本文件的 `ROUTERS`。
这样每个新路由卡只需在自己的分支里**末尾追加一行**，不必改 `main.py` 那块公共代码，
多人并行时不会互相冲突（详见 `设计记录（建号接口-DAG）` §6）。

约定：
- 元组顺序即注册顺序，`.` 前缀写 `""` 表示不加前缀（路由内部自带）。
- **只放 REST 路由**。`app/audio/signaling.py` 的 WS 路由留在 `main.py`，
  因为它是 `api → audio` 的反向依赖，放进来会破坏 AGENTS §2 的单向依赖约束。
- 新增路由必须保持「模块自带 prefix 就少加、不自带就补 `/api`」的既有口径。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api import (
    admin_reset,
    audio,
    audit_export,
    auth,
    devices,
    dialogue,
    elders,
    guardians,
    health,
    ice,
    identity,
    knowledge,
    media,
    medication,
    memory,
    metrics,
    notifications,
    pad,
    reports,
    risk_events,
    rooms,
    sos,
    staff,
    stats,
    voiceprints,
    work_orders,
)

#: (路由器, 挂载前缀) —— 顺序与 `main.py` 原先手写的 6 条保持一致
ROUTERS: tuple[tuple[APIRouter, str], ...] = (
    (health.router, ""),  # /health
    (auth.router, "/api"),  # 路由内只写 /sessions 等，需补 /api
    (elders.router, ""),  # 自带 /api/elders 前缀
    (devices.router, ""),  # 自带 /api/devices 前缀
    (audio.router, ""),  # 自带 /api/audio 前缀（C3）
    (pad.router, ""),  # 自带 /api/pad 前缀（C7）
    # ↓ 2026-11-06 建号接口：以下只增不改，追加新行即可
    (staff.router, ""),  # 自带 /api/staff 前缀（E12）
    (guardians.router, ""),  # 自带 /api/guardians 与 /api/elders/{id}/guardians（G1/E13）
    # ↓ 2026-11-06 迭代 4 收口：声纹三端点（C4），此前模块已写但未挂载
    (voiceprints.router, "/api"),  # 路由内写 /voiceprints/*，需补 /api（C4）
    # ↓ 2026-11-07 观测：/metrics 与 /health 同级（PRD FR-TC-08）
    (metrics.router, ""),  # 路由内写 /metrics，不加前缀
    # ↓ 2026-11-08 迭代 8 · W3：日报与通知（task-33，audio-vad 交付）
    (reports.router, ""),  # 自带 /api/reports 前缀
    (notifications.router, ""),  # 自带 /api/notifications 前缀
    # ↓ 2026-11-08 迭代 7 · W1/W2：用药计划/事件（console）与记忆查询/删除（admin）
    (medication.router, ""),  # 自带 /api/medication 前缀（G）
    (memory.router, ""),  # 自带 /api/memory 前缀（G）
    # ↓ 2026-11-08 迭代 9 · W4a：审计查询与导出（admin 只读面，W4a）
    (audit_export.router, ""),  # 自带 /api/audit 前缀
    # ↓ 2026-11-08 迭代 9 · W4c：运维统计（admin 只读面，W4c）
    (stats.router, ""),  # 自带 /api/stats 前缀
    # ↓ 2026-11-09 迭代 11 · W2：浏览器 iceServers 下发（契约《音频与信令》§1）
    (ice.router, ""),  # 自带 /api/audio 前缀，与 audio.router 并存不冲突（路径不同）
    # ↓ 2026-11-10 DAG N22：机构知识检索与写入（FR-KNW-01/03）
    (knowledge.router, ""),  # 自带 /api/knowledge 前缀
    # ↓ 2026-11-11 打字输入：与话音同一条对话链（长者端 TextFallback）
    (dialogue.router, ""),  # 自带 /api/dialogue 前缀
    # ↓ 2026-11-11 一键求助：长者端常显按钮的后端入口（FR-ALT-06 / FR-UI-08）
    (sos.router, ""),  # 自带 /api/sos 前缀
    # ↓ 2026-11-11 身份确认：按住说话（REST 降级）链路的「是我」，WS 帧的 HTTP 等价物
    (identity.router, ""),  # 自带 /api/identity 前缀
    # ↓ 2026-11-11 设备生命周期：房间清单（新建设备要 room_id，此前无接口可取）
    (rooms.router, ""),  # 自带 /api/rooms 前缀
    # ↓ 2026-11-12 工单看板：状态流转与历史（FR-ALT-02 P0，此前工单能建不能动）
    (work_orders.router, ""),  # 自带 /api/work-orders 前缀
    # ↓ 2026-11-12 戏曲点播：把已实现但不可达的 media_service 接出 HTTP（FR-CON-03 P0）
    (media.router, ""),  # 自带 /api/media 前缀
    # ↓ 2026-11-12 演示数据一键重置（FR-ADM-02 P0；端点内部对 production 直接 404）
    (admin_reset.router, ""),  # 自带 /api/admin/teaching-reset 前缀
    # ↓ 2026-11-12 风险事件确认（ALT-01 P0：红色事件「已确认仍升级」的写入点）
    (risk_events.router, ""),  # 自带 /api/risk-events 前缀
)

__all__ = ["ROUTERS"]
