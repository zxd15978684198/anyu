/**
 * 工单看板的数据层（迭代 8 · task-11）。
 *
 * 契约唯一依据：`app/api/work_orders.py`（E15 列表 / E16 详情 / E17 流转）与
 * `app/services/work_order_service.py` 的 `ALLOWED_TRANSITIONS` / `ACTIVE_STATUSES`。
 *
 * ## 三条必须记住的口径
 * 1. **排序在服务端**：`work_order_service.list_work_orders` 用
 *    `case((WorkOrder.severity == "red", 0), else_=1)` 再 `created_at ASC` 排。
 *    分页时前端只能排当前页，第 2 页的红档永远上不来——所以本文件一行排序代码都不写。
 * 2. **默认视图 = 未处理四态**：`status` 留空时服务端只回
 *    `new / acknowledged / in_progress / reopened`。页面把「未处理」当默认筛选项，
 *    而不是自己拼一个 `status=new`（那会把已确认、处理中的单子全部藏起来）。
 * 3. **误报不是状态**：误报关单就是 `to_status="closed"` + `false_alarm=true` +
 *    必填 `false_alarm_reason`，此时 `note` 可以空。别在这里造第七个状态，
 *    服务端的 `WORK_ORDER_STATUSES` 与库上 CHECK 会同时失效。
 *
 * ## 为什么把 `ALLOWED_TRANSITIONS` 抄一份到前端
 * 后端对非法迁移回 409，但「点了才报错」是烂体验。这里按**同一份表**决定按钮渲不渲染，
 * 让用户点不到错的东西。代价是两张表必须同步改：动 `work_order_service.ALLOWED_TRANSITIONS`
 * 时也要改本文件，否则前端会渲染出一个必然 409 的按钮。
 */
import { apiGet, apiSend, query } from "./http";
import { asString, readPage, type ListPage } from "./staff";
import type { Tone } from "./ui/Feedback";
import { formatDateTimeFull } from "../shared/format";

/** 工单六态闭集（`app/models/work_order.py::WORK_ORDER_STATUSES`，顺序即页面展示顺序）。 */
export type WorkOrderStatus =
  | "new"
  | "acknowledged"
  | "in_progress"
  | "resolved"
  | "closed"
  | "reopened";

export const WORK_ORDER_STATUSES: readonly WorkOrderStatus[] = [
  "new",
  "acknowledged",
  "in_progress",
  "resolved",
  "closed",
  "reopened",
];

/** 「未处理」四态 = 看板默认视图（PRD:397「未处理列表即天然交接清单」）。 */
export const ACTIVE_STATUSES: readonly WorkOrderStatus[] = [
  "new",
  "acknowledged",
  "in_progress",
  "reopened",
];

/**
 * 合法迁移表 —— 逐条照抄 `work_order_service.ALLOWED_TRANSITIONS`。
 *
 * `closed → reopened` 是唯一出边：关单可撤销，但重开不等于「回到 new」，
 * 它带着上一次处置的历史，所以落到 `reopened`。
 */
export const ALLOWED_TRANSITIONS: Readonly<Record<WorkOrderStatus, readonly WorkOrderStatus[]>> = {
  new: ["acknowledged", "closed"],
  acknowledged: ["in_progress", "closed"],
  in_progress: ["resolved", "closed"],
  resolved: ["closed", "reopened"],
  closed: ["reopened"],
  reopened: ["acknowledged", "in_progress", "closed"],
};

/** 按钮展示顺序（与 `WORK_ORDER_STATUSES` 一致，避免同一状态在不同源状态下换位置）。 */
const TRANSITION_ORDER: readonly WorkOrderStatus[] = [
  "acknowledged",
  "in_progress",
  "resolved",
  "closed",
  "reopened",
];

/** 列表一行：`app/api/work_orders.py::WorkOrderItemResponse` 逐字对应。 */
export interface WorkOrderRow {
  order_id: string;
  order_no: string;
  elder_id: string;
  /** 已脱敏（姓 + `*` + 末字），前端不要再加工。 */
  elder_name: string;
  type: string;
  severity: string;
  status: string;
  assignee_id: string | null;
  assignee_name: string | null;
  false_alarm: boolean;
  note: string | null;
  created_at: string;
  closed_at: string | null;
}

/** 一条状态变更历史：`TransitionResponse` 逐字对应。 */
export interface TransitionRow {
  transition_id: string;
  from_status: string;
  to_status: string;
  /** 系统自动流转时为空，页面显示「系统」。 */
  actor_id: string | null;
  actor_name: string | null;
  at: string;
  note: string | null;
}

/** 详情 = 列表项 + 完整流转历史 + 关联风险事件（`WorkOrderDetailResponse`）。 */
export interface WorkOrderDetail extends WorkOrderRow {
  transitions: TransitionRow[];
  event_id: string | null;
  event_status: string | null;
  false_alarm_reason: string | null;
  actual_handled_at: string | null;
}

export interface WorkOrderListQuery {
  page: number;
  pageSize: number;
  /** `""` = 未处理四态（服务端默认）；其余为精确状态。 */
  status?: WorkOrderStatus | "";
  /** `""` = 全部；`red` 须立即人工，`yellow` 当班跟进。 */
  severity?: "red" | "yellow" | "";
}

export function workOrderListPath(input: WorkOrderListQuery): string {
  return `/api/work-orders${query({
    page: input.page,
    page_size: input.pageSize,
    status: input.status ?? "",
    severity: input.severity ?? "",
  })}`;
}

// ---------------------------------------------------------------- 收窄工具

/** `asString` 把 null 与缺失都折成 `""`，但本接口里有真 null 字段（`assignee_name` 等），需分开。 */
function asNullableString(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function toWorkOrderRow(raw: unknown): WorkOrderRow {
  const item = (raw ?? {}) as Record<string, unknown>;
  return {
    order_id: asString(item.order_id),
    order_no: asString(item.order_no),
    elder_id: asString(item.elder_id),
    elder_name: asString(item.elder_name),
    type: asString(item.type),
    severity: asString(item.severity),
    status: asString(item.status),
    assignee_id: asNullableString(item.assignee_id),
    assignee_name: asNullableString(item.assignee_name),
    // 布尔只认严格 true：缺失时按「非误报」会把误报单显示成真实警情。
    false_alarm: item.false_alarm === true,
    note: asNullableString(item.note),
    created_at: asString(item.created_at),
    closed_at: asNullableString(item.closed_at),
  };
}

function toTransitionRow(raw: unknown): TransitionRow {
  const item = (raw ?? {}) as Record<string, unknown>;
  return {
    transition_id: asString(item.transition_id),
    from_status: asString(item.from_status),
    to_status: asString(item.to_status),
    actor_id: asNullableString(item.actor_id),
    actor_name: asNullableString(item.actor_name),
    at: asString(item.at),
    note: asNullableString(item.note),
  };
}

function toWorkOrderDetail(raw: unknown): WorkOrderDetail {
  const item = (raw ?? {}) as Record<string, unknown>;
  const rawTransitions = Array.isArray(item.transitions) ? item.transitions : [];
  return {
    ...toWorkOrderRow(item),
    transitions: rawTransitions.map(toTransitionRow),
    event_id: asNullableString(item.event_id),
    event_status: asNullableString(item.event_status),
    false_alarm_reason: asNullableString(item.false_alarm_reason),
    actual_handled_at: asNullableString(item.actual_handled_at),
  };
}

// ---------------------------------------------------------------- 端点

/** `GET /api/work-orders?page&page_size&status&severity`（E15）。 */
export async function listWorkOrders(input: WorkOrderListQuery): Promise<ListPage<WorkOrderRow>> {
  const raw = await apiGet<unknown>(workOrderListPath(input));
  const page = readPage<unknown>(raw, { page: input.page, pageSize: input.pageSize });
  return { ...page, items: page.items.map(toWorkOrderRow) };
}

/** `GET /api/work-orders/{order_id}`（E16）：带完整流转历史。 */
export async function getWorkOrder(orderId: string): Promise<WorkOrderDetail> {
  const raw = await apiGet<unknown>(`/api/work-orders/${encodeURIComponent(orderId)}`);
  return toWorkOrderDetail(raw);
}

/**
 * E17 请求体 —— 与 `TransitionRequest` 逐字一致，且后端 `extra="forbid"`。
 *
 * 因此调用方**只能**传这五个键：多传一个（比如顺手带上 `order_id`）就是 422，
 * 不是「多余字段被忽略」。可选键在页面上按需拼，不要无脑传全量。
 */
export interface TransitionPayload {
  to_status: WorkOrderStatus;
  note?: string | null;
  false_alarm?: boolean;
  false_alarm_reason?: string | null;
  /** ISO 8601 串；红档关单必填。 */
  actual_handled_at?: string | null;
  /**
   * 关单时顺带关闭的风险事件（第五轮 FR-ALT-02）。
   *
   * 服务端在同一事务里先关单、再关事件，事件与工单任何一边校验不过就整笔回滚，
   * 所以这里**只发一次请求**——不要在前端拆成「先关事件、后关单」两步：
   * 独立 close-event 端点要求工单已关闭，反序必然 409。
   */
  event_id?: string | null;
}

/** `POST /api/work-orders/{order_id}/transitions`（E17）：返回流转后的完整详情。 */
export async function transitionWorkOrder(
  orderId: string,
  payload: TransitionPayload,
): Promise<WorkOrderDetail> {
  const raw = await apiSend<unknown>(
    `/api/work-orders/${encodeURIComponent(orderId)}/transitions`,
    "POST",
    payload,
  );
  return toWorkOrderDetail(raw);
}

/** `POST /api/risk-events/{event_id}/ack` 的响应（`RiskEventAckResponse`）。 */
export interface RiskEventAck {
  event_id: string;
  elder_id: string;
  type: string;
  severity: string;
  /** 确认只写 `acknowledged_at`，`status` 仍是 `open`，不要据此判断失败。 */
  status: string;
  acknowledged_at: string;
}

function toRiskEventAck(raw: unknown): RiskEventAck {
  const item = (raw ?? {}) as Record<string, unknown>;
  return {
    event_id: asString(item.event_id),
    elder_id: asString(item.elder_id),
    type: asString(item.type),
    severity: asString(item.severity),
    status: asString(item.status),
    acknowledged_at: asString(item.acknowledged_at),
  };
}

/**
 * `POST /api/risk-events/{event_id}/ack`（ALT-01）：确认接手一条风险事件。
 *
 * 幂等：重复确认返回原 `acknowledged_at`。工单详情目前不回这个字段，所以刷新后
 * 按钮会再次出现——页面必须按「重复确认无害」解释，而不是按「上次没成功」。
 */
export async function ackRiskEvent(eventId: string): Promise<RiskEventAck> {
  const raw = await apiSend<unknown>(
    `/api/risk-events/${encodeURIComponent(eventId)}/ack`,
    "POST",
  );
  return toRiskEventAck(raw);
}

// ---------------------------------------------------------------- 文案与判定

const STATUS_TEXT: Record<WorkOrderStatus, string> = {
  new: "待接单",
  acknowledged: "已接单",
  in_progress: "处理中",
  resolved: "已解决",
  closed: "已关闭",
  reopened: "已重开",
};

const STATUS_TONE: Record<WorkOrderStatus, Tone> = {
  new: "danger",
  acknowledged: "warning",
  in_progress: "info",
  resolved: "success",
  closed: "neutral",
  reopened: "warning",
};

const SEVERITY_TEXT: Record<string, string> = {
  red: "红档",
  yellow: "黄档",
};

const SEVERITY_TONE: Record<string, Tone> = {
  red: "danger",
  yellow: "warning",
};

/** `work_orders.type` 八值闭集（`medication_service.WORK_ORDER_TYPES`）。 */
const TYPE_TEXT: Record<string, string> = {
  missed_medication: "漏服用药",
  sensitive_alert: "敏感告警",
  health_alert: "健康告警",
  device_fault: "设备故障",
  sos: "紧急求助",
  collection_missed: "漏采健康数据",
  drug_question: "用药咨询",
  follow_up: "护工跟进",
};

const TRANSITION_TEXT: Record<WorkOrderStatus, string> = {
  new: "退回待接单",
  acknowledged: "确认接单",
  in_progress: "开始处理",
  resolved: "标记已解决",
  closed: "关闭工单",
  reopened: "重新打开",
};

/** 未知取值原样显示：显示成「未知」会让人以为数据坏了。 */
export function statusText(status: string): string {
  return STATUS_TEXT[status as WorkOrderStatus] ?? status;
}

export function statusTone(status: string): Tone {
  return STATUS_TONE[status as WorkOrderStatus] ?? "neutral";
}

export function severityText(severity: string): string {
  return SEVERITY_TEXT[severity] ?? severity;
}

export function severityTone(severity: string): Tone {
  return SEVERITY_TONE[severity] ?? "neutral";
}

export function typeText(type: string): string {
  return TYPE_TEXT[type] ?? type;
}

/** 流转按钮文案（按目标状态取名，而不是按源状态）。 */
export function transitionText(to: WorkOrderStatus): string {
  return TRANSITION_TEXT[to] ?? to;
}

/**
 * 当前状态可去的下一状态（按固定顺序）。非法状态返回空数组——
 * 页面据此**不渲染**按钮，而不是渲染一个点了必然 409 的死按钮。
 */
export function nextStatuses(status: string): readonly WorkOrderStatus[] {
  const allowed = ALLOWED_TRANSITIONS[status as WorkOrderStatus];
  if (!allowed) return [];
  return TRANSITION_ORDER.filter((to) => allowed.includes(to));
}

export function canTransition(from: string, to: WorkOrderStatus): boolean {
  return nextStatuses(from).includes(to);
}

/**
 * 关单是否需要「实际处置时间」：只有红档要（服务端
 * `severity == "red" and actual_handled_at is None` → 422）。
 */
export function requiresHandledAt(severity: string): boolean {
  return severity === "red";
}

/** 时间展示：ISO 串 → `YYYY-MM-DD HH:mm`（**Asia/Shanghai**）；空值 → `—`，非法值原样返回。
 *  收敛到 shared/format（AGENTS §8）。原实现用 `date.getHours()` 即浏览器本地时区。 */
export function formatDateTime(value: string): string {
  return formatDateTimeFull(value);
}
