/**
 * 站内通知数据层（迭代 8 · W4 消息中心）。
 *
 * 契约：`GET /api/notifications?kind=&severity=&status=&page=`、`POST /api/notifications/{id}/ack`、
 * `POST /api/notifications/{id}/deliver`、`POST /api/notifications/{id}/resend`、
 * `POST /api/notifications/{id}/revoke?reason=`。
 *
 * 四条硬约束（与 `app/services/notification_service.py` 逐字对齐）：
 * 1. `kind` / `severity` / `status` 都是闭集，文案只由本文件的 OPTIONS 提供；
 * 2. 状态机 generated→pending→delivered→acknowledged（failed / simulated 为终态），
 *    前端只按状态**显隐按钮**，合法性由服务端 409 判定；
 * 3. 本仓不接真实推送：`deliver` 只是状态位模拟，界面文案写「模拟送达」，不要说已推送；
 * 4. 重发 / 撤销是同一行上的两个**运营动作**，判据（可重发三态、撤销 10 秒窗口）只在本文件写一次，
 *    页面不许另抄一份取值，否则与后端常量漂移时看不出来。
 */
import { authedFetch, cachedProfile } from "./api";
import type { Principal } from "./api";
import { HttpError, NETWORK_TEXT, apiGet, apiSend, query } from "./http";

export type NotificationKind =
  | "health_review"
  | "medication_escalation"
  | "medication_review"
  | "collection_missed"
  | "family_report"
  | "safety_alert"
  | "attention_signal";

export type NotificationSeverity = "red" | "yellow" | "green";

export type NotificationStatus =
  | "generated"
  | "pending"
  | "delivered"
  | "acknowledged"
  | "failed"
  | "simulated";

/** 语义色单一真源在 shared/tone（W4）。 */
import type { Tone as BadgeTone } from "../shared/tone";
export type { BadgeTone };

export const NOTIFICATION_PAGE_SIZE = 20;

export const KIND_OPTIONS: { value: NotificationKind; label: string }[] = [
  { value: "health_review", label: "健康复核" },
  { value: "medication_escalation", label: "用药升级" },
  { value: "medication_review", label: "到期复核" },
  { value: "collection_missed", label: "漏采派单" },
  { value: "family_report", label: "家属日报" },
  { value: "safety_alert", label: "安全提醒" },
  { value: "attention_signal", label: "建议关注" },
];

export const SEVERITY_OPTIONS: { value: NotificationSeverity; label: string; tone: BadgeTone }[] = [
  { value: "red", label: "红 · 紧急", tone: "danger" },
  { value: "yellow", label: "黄 · 关注", tone: "warning" },
  { value: "green", label: "绿 · 常规", tone: "success" },
];

export const STATUS_OPTIONS: { value: NotificationStatus; label: string; tone: BadgeTone }[] = [
  { value: "generated", label: "已生成", tone: "neutral" },
  { value: "pending", label: "待送达", tone: "info" },
  { value: "delivered", label: "已送达", tone: "info" },
  { value: "acknowledged", label: "已确认", tone: "success" },
  { value: "failed", label: "送达失败", tone: "danger" },
  { value: "simulated", label: "模拟送达", tone: "neutral" },
];

export function kindLabel(kind: string): string {
  return KIND_OPTIONS.find((option) => option.value === kind)?.label ?? kind;
}

export function severityTone(severity: string): BadgeTone {
  return SEVERITY_OPTIONS.find((option) => option.value === severity)?.tone ?? "neutral";
}

export function severityLabel(severity: string): string {
  return SEVERITY_OPTIONS.find((option) => option.value === severity)?.label ?? severity;
}

export function statusLabel(status: string): string {
  return STATUS_OPTIONS.find((option) => option.value === status)?.label ?? status;
}

export function statusTone(status: string): BadgeTone {
  return STATUS_OPTIONS.find((option) => option.value === status)?.tone ?? "neutral";
}

/** 只有 generated / pending 能「送达」；delivered 能「确认」；其余是终态，不给按钮。 */
export function canDeliver(status: string): boolean {
  return status === "generated" || status === "pending";
}

export function canAck(status: string): boolean {
  return status === "delivered";
}

export interface NotificationItem {
  notification_id: string;
  elder_id: string | null;
  kind: string;
  severity: string;
  status: string;
  payload?: unknown;
  assignee_staff_id?: string | null;
  delivered_at?: string | null;
  acknowledged_at?: string | null;
  created_at: string;
}

export interface NotificationPage {
  items: NotificationItem[];
  total: number;
}

function normalizePage(raw: unknown): NotificationPage {
  const body = (raw ?? {}) as { items?: unknown; total?: unknown };
  const items = Array.isArray(body.items) ? (body.items as NotificationItem[]) : [];
  return {
    items,
    total: typeof body.total === "number" && Number.isFinite(body.total) ? body.total : items.length,
  };
}

/** 列表：**按 severity 优先（红→黄→绿）、同级再按创建时间倒序**，由服务端保证——
 * 排序键在 `notification_service` 的 `severity_rank`，前端不得再排序，否则会与分页口径打架。
 * `kind` / `severity` / `status` 为空表示不筛。 */
export async function listNotifications(params: {
  page: number;
  pageSize?: number;
  kind?: NotificationKind | "";
  severity?: NotificationSeverity | "";
  status?: NotificationStatus | "";
}): Promise<NotificationPage> {
  const search = query({
    page: params.page,
    page_size: params.pageSize ?? NOTIFICATION_PAGE_SIZE,
    kind: params.kind || undefined,
    severity: params.severity || undefined,
    status: params.status || undefined,
  });
  return normalizePage(await apiGet<unknown>(`/api/notifications${search}`));
}

/** 模拟送达（delivered）：本仓不外联任何推送通道，`simulated` 为真时走终态 simulated。 */
export function deliverNotification(
  notificationId: string,
  simulated = false,
): Promise<NotificationItem> {
  const body = simulated ? { simulated: true } : undefined;
  return apiSend<NotificationItem>(
    `/api/notifications/${encodeURIComponent(notificationId)}/deliver${query({ simulated: simulated ? true : undefined })}`,
    "POST",
    body,
  );
}

/** 确认（acknowledged）：delivered → acknowledged，其他来源态服务端 409。 */
export function ackNotification(notificationId: string): Promise<NotificationItem> {
  return apiSend<NotificationItem>(`/api/notifications/${encodeURIComponent(notificationId)}/ack`, "POST");
}

// ---------------------------------------------------------------- 重发 / 撤销（N31 / N32）

/** 撤销窗口（秒）：与后端 `notification_service.REVOKE_WINDOW_SECONDS` **同源同值**，
 *  改一处必须同改——前端判据与后端常量一旦漂移，界面会开始放行注定 409 的请求。 */
export const REVOKE_WINDOW_SECONDS = 10;

/** 还能重发的源状态：逐字对齐后端 `_RESENDABLE_STATUSES`（generated / pending / delivered）。
 *  acknowledged / failed / simulated 三条终态服务端一律 409 `invalid_transition`。 */
const RESENDABLE_STATUSES: readonly string[] = ["generated", "pending", "delivered"];

/** 还能撤销的源状态：`revoke` 先显式拒掉 acknowledged，再由迁移表
 *  （generated / pending / delivered → failed）放行；failed / simulated 是终态，撤销同样 409。 */
const REVOCABLE_STATUSES: readonly string[] = ["generated", "pending", "delivered"];

/** 后端 `notification_service.RESEND_ROLES` 的**同源同值**副本：admin / care_worker。
 *  医生点重发必吃 403，所以按钮对医生不显示——判据只在本文件写一次，页面不许另抄。 */
const RESEND_ROLES: readonly string[] = ["admin", "care_worker"];

/** 状态上允不允许重发（与角色无关，用来区分「终态」和「你无权」两种情况）。 */
export function canResendStatus(status: string): boolean {
  return RESENDABLE_STATUSES.includes(status);
}

/** 能否重发：状态在可重发三态内，**且**当前登录角色在 `RESEND_ROLES` 内。
 *
 * 角色默认取登录态里的 principal（`cachedProfile()`），与页面读的是同一份；
 * 显式传 `null` 表示「未登录 / 判不了」，按无权处理（宁可不显示，也不放行注定 403 的按钮）。 */
export function canResend(status: string, principal: Principal | null = cachedProfile()): boolean {
  if (!principal || !principal.role || !RESEND_ROLES.includes(principal.role)) return false;
  return canResendStatus(status);
}

/**
 * 撤销窗口还剩几秒（向上取整；窗口已关返回 0）。
 *
 * **基准是服务端 `created_at`**，不是组件首次渲染的时刻：拿挂载时的 `Date.now()` 起算，
 * 刷新一次页面就能把 10 秒窗口续上，等于绕过服务端的窗口判定。
 *
 * 后端 `revoke` 的起算点是 `delivered_at or created_at`，前端统一用 `created_at` 只会
 * **更早**收起按钮（宁可少给一秒，也不放行一条超窗请求）；判据宁紧勿松。
 * `created_at` 解析不出来时返回 0：宁可不出按钮，也不让一条判不了龄的通知被撤。
 */
export function revokeRemainingSeconds(createdAt: string, now: number = Date.now()): number {
  const sentAt = Date.parse(createdAt);
  if (Number.isNaN(sentAt)) return 0;
  const remainingMs = REVOKE_WINDOW_SECONDS * 1000 - (now - sentAt);
  if (remainingMs <= 0) return 0;
  return Math.ceil(remainingMs / 1000);
}

/** 能否撤销：状态在可撤销集合内，且以 `created_at` 起算的 10 秒窗口未关。 */
export function canRevoke(status: string, createdAt: string, now: number = Date.now()): boolean {
  return REVOCABLE_STATUSES.includes(status) && revokeRemainingSeconds(createdAt, now) > 0;
}

/**
 * 两个运营动作共用的 POST：**保留后端 `detail`**。
 *
 * 不能走 `apiSend`——它把非 2xx 一律翻成 `CODE_TEXT[code] ?? NETWORK_TEXT`，而
 * `invalid_transition` / `revoke_window_expired` 不在那张表里，会被兜成「网络异常」：
 * 「窗口过了」被吞成网络问题，护工就分不清该重发还是该找管理员。
 * 与 `medications.ts` 上传接口同款处理：只摘 `code` / `detail`，交给 `HttpError`。
 */
async function postAction(path: string, fallback: string): Promise<NotificationItem> {
  let response: Response;
  try {
    response = await authedFetch(path, { method: "POST" });
  } catch {
    throw new HttpError(0, "NETWORK", NETWORK_TEXT);
  }
  if (!response.ok) {
    let code = "";
    let detail = "";
    try {
      const body = (await response.json()) as { code?: unknown; detail?: unknown };
      if (typeof body.code === "string") code = body.code;
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      /* 非 JSON 错误体：按状态码兜底 */
    }
    throw new HttpError(response.status, code || "BAD_REQUEST", detail || fallback);
  }
  return (await response.json()) as NotificationItem;
}

/**
 * 重发（N31）：还是那一行，只再投一次——后端 `payload.resend_count` 自增、`delivered_at`
 * 刷新成最近一次投递时间，**不新建通知行**。
 *
 * 角色限 admin / care_worker（`RESEND_ROLES`）；响应是完整的 `NotificationOut`，
 * 调用方直接拿它替换表格里那一行，不必整页重取。
 */
export function resendNotification(notificationId: string): Promise<NotificationItem> {
  return postAction(
    `/api/notifications/${encodeURIComponent(notificationId)}/resend`,
    "重发失败，请稍后重试",
  );
}

/**
 * 撤销（N32）：置 `failed` + 同事务写审计，**送达后 10 秒**内有效，超窗 409
 * `revoke_window_expired`、状态不对 409 `invalid_transition`。
 *
 * `reason` 走**查询串**而不是 body（后端签名是 `reason: Query(max_length=100)`）；
 * 空串不发参数，后端只在有值时写 `payload.revoke_reason` 与审计的 `reason`。
 */
export function revokeNotification(
  notificationId: string,
  reason: string,
): Promise<NotificationItem> {
  const text = reason.trim();
  return postAction(
    `/api/notifications/${encodeURIComponent(notificationId)}/revoke${query({
      reason: text || undefined,
    })}`,
    "撤销失败，请稍后重试",
  );
}

const SUMMARY_KEYS = [
  "summary",
  "message",
  "detail",
  "note",
  "reason",
  "reasons",
  "title",
  // health_review 的主诉走 record_complaint（基座无 complaint 列），复核队列要读得到。
  "record_complaint",
];

/** 工单六态 → 一句人话（与 `frontend/src/family/notifications.ts` 同一口径，两端不许说两套）。 */
const WORK_ORDER_STATUS_LABEL: Record<string, string> = {
  new: "待处理",
  acknowledged: "已接单",
  processing: "处理中",
  closed: "已关闭",
  reopened: "已重开",
  escalated: "已升级",
};

/** 工单类通知摘要：`payload.order_no` + `to_status` 拼成「工单 WO-… 已接单」。 */
function workOrderSummary(payload: unknown, kind: string | undefined): string | null {
  if (kind !== "work_order") return null;
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
  const record = payload as Record<string, unknown>;
  const orderNo = typeof record.order_no === "string" ? record.order_no.trim() : "";
  if (!orderNo) return null;
  const toStatus = typeof record.to_status === "string" ? record.to_status.trim() : "";
  const label = WORK_ORDER_STATUS_LABEL[toStatus];
  return label ? `工单 ${orderNo} ${label}` : `工单 ${orderNo}`;
}

/** payload 摘要：只挑已知的展示字段，绝不整段 JSON 怼到表格里（用户读不懂也不安全）。 */
export function payloadSummary(payload: unknown, kind?: string): string {
  const orderSummary = workOrderSummary(payload, kind);
  if (orderSummary !== null) return orderSummary;
  if (payload === null || payload === undefined) return "—";
  if (typeof payload === "string") return payload;
  if (typeof payload === "number" || typeof payload === "boolean") return String(payload);
  if (Array.isArray(payload)) return payload.length === 0 ? "—" : `${payload.length} 项`;
  const record = payload as Record<string, unknown>;
  for (const key of SUMMARY_KEYS) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) return value.trim();
    if (Array.isArray(value) && value.length > 0) {
      return value.filter((item) => typeof item === "string").join("、");
    }
  }
  const keys = Object.keys(record);
  return keys.length === 0 ? "—" : `${keys.length} 个字段（展开见详情）`;
}
