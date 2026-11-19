/**
 * 照护台角色能力判定（W1-3 / 迭代 8 扩展）——**全站唯一的角色判断处**。
 *
 * 依据：docs/接口契约_身份与权限.md §6 权限矩阵 + PRD §4.7（通知与日报的操作角色）。
 * 铁律：前端判定只影响「按钮显不显示、导航项在不在」，**不构成权限**——
 * 服务端每个写端点仍然独立校验（PRD FR-ADM-03 / AC-18）。
 *
 * 迭代 8 新增三能力（产品负责人 2026-11-07 裁决）：
 * - `health_write`：admin + care_worker + doctor（护工代填负责长者的五字段，范围由服务端 403 兜底）；
 * - `reports_view`：admin + doctor（家属预留：guardian 在这里拿不到任何能力，家庭端走 family 入口）；
 * - `notifications_ack`：三角色都能收/确认自己可见范围内的通知。
 */
import type { Principal } from "./api";

/** 能力名按「资源:动作」命名，与契约章节一一对应，新增能力必须同时改本表与服务端。 */
export type Capability =
  | "elder:read"
  | "elder:write"
  // 长者退房（FR-ADM-09）：服务端 `check_roles(identity, ("admin",))`，只给管理员。
  | "elder:checkout"
  // 数字人角色列表（FR-3D-03）：服务端 `GET /api/elders/personas` 只放 admin / doctor。
  | "persona:read"
  | "staff:write"
  | "guardian:write"
  | "device:write"
  | "health_write"
  | "health:revoke"
  | "room:write"
  | "media:write"
  | "reports_view"
  | "notifications_ack"
  | "medication:read"
  | "medication:create"
  | "medication:confirm"
  | "memory:read"
  | "knowledge:write"
  // 工单看板（FR-ALT-02）：服务端 `_WORK_ORDER_ROLES` 是 admin / care_worker / doctor，
  // 故这三个角色都给，护士不给。read 与「能否流转」分开看：流转另由状态机与 isAdmin 决定。
  | "workorder:read"
  | "audit:read";

const ADMIN_CAPS: readonly Capability[] = [
  "elder:read",
  "elder:write",
  "elder:checkout",
  "persona:read",
  "staff:write",
  "guardian:write",
  "device:write",
  "health_write",
  // 撤销健康记录（FR-HLT-04）：服务端只给医生/管理员，护工能补录但不能抹掉自己填错的数。
  "health:revoke",
  // 房间维护（FR-ADM-01）与媒体库维护（FR-ADM-02）都只给管理员。
  "room:write",
  "media:write",
  "reports_view",
  "notifications_ack",
  "medication:read",
  "medication:create",
  "medication:confirm",
  // 长者记忆（契约 M1–M3 全部 admin only，FR-MEM-02 的查看/纠正/删除都走设备端或管理端）
  "memory:read",
  // 机构知识库（FR-KNW-02）：录入与更新仅限管理员，检索也只在 admin 侧开放
  "knowledge:write",
  // 工单看板（FR-ALT-02）：管理员可读可流转，重开另有 isAdmin 门槛
  "workorder:read",
  "audit:read",
];

/** 护工：读负责范围的长者 + 代填健康 + 复核自己可见的消息；**不给**日报（care_worker 403）。 */
const CARE_WORKER_CAPS: readonly Capability[] = [
  "elder:read",
  "health_write",
  "notifications_ack",
  // 护工能读药单、也能代确认服药（服务端 `READING_ROLES` / `CONFIRMING_ROLES` 都含 care_worker），
  // 但**不能开药单**（`WRITING_ROLES = {doctor}`）——这条差异是本页最容易被写错的地方。
  "medication:read",
  "medication:confirm",
  // 工单看板（FR-ALT-02）：护工是工单的主要承接人，可读可流转；重开仍只给管理员
  "workorder:read",
];

/** 医生：护工的能力 + 查阅日报（PRD §4.8 家属日报的照护台侧是医生读）+ 开具药单（FR-MED-01）。 */
const DOCTOR_CAPS: readonly Capability[] = [
  "elder:read",
  "persona:read",
  "health_write",
  // 医生可撤销错误的健康记录（服务端 health.revoke 白名单含 doctor）。
  "health:revoke",
  "notifications_ack",
  "reports_view",
  "medication:read",
  "medication:create",
  "medication:confirm",
  // 工单看板（FR-ALT-02）：医生可查健康类告警工单并流转；重开仍只给管理员
  "workorder:read",
];

export function isAdmin(principal: Principal | null): boolean {
  return principal?.kind === "staff" && principal.role === "admin";
}

export function capabilitiesOf(principal: Principal | null): readonly Capability[] {
  if (!principal || principal.kind !== "staff") return [];
  if (principal.role === "admin") return ADMIN_CAPS;
  if (principal.role === "care_worker") return CARE_WORKER_CAPS;
  if (principal.role === "doctor") return DOCTOR_CAPS;
  return [];
}

export function can(principal: Principal | null, capability: Capability): boolean {
  return capabilitiesOf(principal).includes(capability);
}

/** 长者列表顶部提示：admin 看全量，护工/医生只看负责范围（产品负责人 2026-11-07 裁决）。 */
export function elderScopeNote(principal: Principal | null): string | null {
  if (isAdmin(principal)) return null;
  if (principal?.kind !== "staff") return null;
  if (principal.role === "care_worker" || principal.role === "doctor") {
    return "仅显示您负责的长者";
  }
  return null;
}
