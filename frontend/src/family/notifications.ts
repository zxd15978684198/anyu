/**
 * 家属端通知中心数据层（W4）。
 *
 * 契约：`GET /api/notifications?page=1&page_size=20&status=&kind=`
 * → `{items, total, page, page_size}`。家属对通知**只读**：
 * `POST /{id}/deliver`、`/{id}/ack` 对家属是 403（`_require_staff`），本端不做这两个动作。
 *
 * 两个口径：
 * - `kind` / `severity` 都是闭集，但服务端可能新增取值，所以按 `Record<string, …>` 兜底
 *   而不是穷举 union —— 新类型退化成原文，好过整页渲染出空白。
 * - 摘要优先取 `payload.summary`（服务端已写成整句），没有就退回类型名，
 *   绝不自己拼医疗结论。
 */
import { formatDateTime } from "./format";
import { apiGet, query } from "./http";

export type NotificationSeverity = "red" | "yellow" | "green";
export type SeverityFilter = "all" | NotificationSeverity;

export interface NotificationItem {
  notification_id: string;
  elder_id?: string | null;
  kind: string;
  severity: string;
  status?: string;
  payload?: Record<string, unknown> | null;
  assignee_staff_id?: string | null;
  delivered_at?: string | null;
  acknowledged_at?: string | null;
  created_at: string;
}

// 七类通知 + 工单，一个都不许漏：漏了的前端只能把 kind 原文（如 `safety_alert`）怼给家属。
// 口径与 `frontend/src/console/notifications.ts::KIND_OPTIONS` 对齐，家属端用更短的说法。
const KIND_LABEL: Record<string, string> = {
  health_review: "健康复核",
  medication_escalation: "用药升级",
  medication_review: "到期复核",
  collection_missed: "漏采",
  family_report: "日报",
  safety_alert: "安全提醒",
  attention_signal: "建议关注",
  work_order: "工单",
};

export const SEVERITY_LABEL: Record<NotificationSeverity, string> = {
  red: "需尽快关注",
  yellow: "待关注",
  green: "正常同步",
};

export const SEVERITY_FILTERS: { value: SeverityFilter; label: string }[] = [
  { value: "all", label: "全部" },
  { value: "red", label: "需尽快关注" },
  { value: "yellow", label: "待关注" },
  { value: "green", label: "正常同步" },
];

/** 类型中文名；未知类型显示原文，不显示成空白。 */
export function kindLabel(kind: string): string {
  return KIND_LABEL[kind] ?? kind;
}

/** 严重度归一：只有绿/黄/红三种点，未知值按黄处理（宁可多提示，不要静默吞掉）。 */
export function severityOf(item: NotificationItem): NotificationSeverity {
  return item.severity === "red" || item.severity === "green" || item.severity === "yellow"
    ? item.severity
    : "yellow";
}

/** 工单六态 → 家属能读懂的一句话（`app/models/work_order.py::WORK_ORDER_STATUSES` 的中文对照）。 */
const WORK_ORDER_STATUS_LABEL: Record<string, string> = {
  new: "待处理",
  acknowledged: "已接单",
  processing: "处理中",
  closed: "已关闭",
  reopened: "已重开",
  escalated: "已升级",
};

/**
 * 工单类通知的人话摘要：`payload.order_no` + `to_status` → 「工单 WO-… 已接单」。
 *
 * 2026-11-13 真机验证发现：服务端只存了 `order_no`/`to_status`，家属端因此只显示类型名「工单」，
 * 看不到是哪一单、现在什么状态。这里只做「把已有的结构化字段拼成人话」，
 * 不引入新的后端字段，也不做任何医学/严重度判断（严重度由 `severityOf` 单独给）。
 */
function workOrderSummary(item: NotificationItem): string | null {
  if (item.kind !== "work_order") return null;
  const payload = item.payload ?? {};
  const orderNo = typeof payload.order_no === "string" ? payload.order_no.trim() : "";
  if (!orderNo) return null;
  const toStatus = typeof payload.to_status === "string" ? payload.to_status.trim() : "";
  const label = WORK_ORDER_STATUS_LABEL[toStatus];
  return label ? `工单 ${orderNo} ${label}` : `工单 ${orderNo}`;
}

function firstText(payload: Record<string, unknown> | null | undefined, keys: string[]): string | null {
  if (!payload) return null;
  for (const key of keys) {
    const value = payload[key];
    if (typeof value === "string" && value.trim() !== "") return value.trim();
  }
  return null;
}

/**
 * 一句话摘要：优先 `payload.summary`，其次几个常见字段，最后退回类型名。
 * 只做「取服务端写好的话」，不在前端拼医学判断。
 */
export function payloadSummary(item: NotificationItem): string {
  return (
    workOrderSummary(item) ??
    firstText(item.payload, ["summary", "message", "title", "note"]) ??
    kindLabel(item.kind)
  );
}

export function timeOf(item: NotificationItem): string {
  return formatDateTime(item.created_at);
}

interface NotificationListResponse {
  items?: unknown;
  total?: unknown;
  page?: unknown;
}

export interface NotificationPage {
  items: NotificationItem[];
  total: number;
  page: number;
}

/** 拉一页通知。**排序权在服务端**：`notification_service.list_for_staff` 按
 *  `severity_rank`（红→黄→绿）→ `created_at` 倒序 → `notification_id` 排序，
 *  前端不得再排序——否则会抹掉红档置顶，且与分页口径打架（与 `console/notifications.ts` 同口径）。
 *  `severity` 同样由服务端在**分页之前**过滤，所以返回的 `total` 就是筛选后的总数，
 *  前端据此判「加载更多」与空态才准确。 */
export async function listNotifications(params: {
  page?: number;
  pageSize?: number;
  kind?: string;
  severity?: NotificationSeverity | "";
} = {}): Promise<NotificationPage> {
  const page = params.page ?? 1;
  const pageSize = params.pageSize ?? 20;
  const raw = await apiGet<NotificationListResponse>(
    `/api/notifications${query({
      page,
      page_size: pageSize,
      kind: params.kind,
      severity: params.severity || undefined,
    })}`,
  );
  const items = Array.isArray(raw.items) ? (raw.items as NotificationItem[]) : [];
  const total = typeof raw.total === "number" ? raw.total : items.length;
  return {
    items,
    total,
    page: typeof raw.page === "number" ? raw.page : page,
  };
}
