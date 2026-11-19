/**
 * 长者档案数据层（W2 · E5–E8）。
 *
 * 契约依据：`docs/接口契约_身份与权限.md` E5–E8。三条硬约束写在这里而不是页面里：
 * 1. 分页在服务端，默认 20、上限 100——页面只传页码；
 * 2. 写请求白名单只有 `name / birth_date / gender / persona_id / status`，且**绝不发送 null**
 *    （`name`/`status` 显式 null 会 422），空字段统一按「未提供」处理；
 * 3. `status` 是闭集，下拉文案只由 `ELDER_STATUS_OPTIONS` 提供，别处不要另造。
 */
import { apiGet, apiSend, query } from "./http";

export type ElderStatus = "active" | "away" | "transferred" | "archived";

export const PAGE_SIZE = 20;

/** 状态下拉：列表筛选与详情改状态共用同一份文案/顺序（与 E8 闭集一致）。 */
export const ELDER_STATUS_OPTIONS: { value: ElderStatus; label: string }[] = [
  { value: "active", label: "在住" },
  { value: "away", label: "外出" },
  { value: "transferred", label: "转出" },
  { value: "archived", label: "归档" },
];

export function isElderStatus(value: string): value is ElderStatus {
  return ELDER_STATUS_OPTIONS.some((option) => option.value === value);
}

/** E5 列表项（后端只回这几个字段，别在列表里猜详情字段）。 */
export interface ElderListItem {
  elder_id: string;
  name: string;
  room_no: string | null;
  bed_no: string | null;
  status: string;
}

export interface ElderList {
  items: ElderListItem[];
  total: number;
  page: number;
  page_size: number;
}

/** E6 详情（比列表多出生日期、性别、人设、会话序号、建档时间）。 */
export interface ElderDetail extends ElderListItem {
  birth_date: string | null;
  gender: string | null;
  persona_id: string | null;
  session_seq: number;
  created_at: string;
  /** 绑定设备数（后端按 `devices.elder_id` 派生；老接口没有这两个字段，故可选）。 */
  device_count?: number;
  /** 已绑定家属数（只数 `elder_guardians.status='active'`）。 */
  guardian_count?: number;
}

export interface ElderCreateInput {
  name: string;
  birth_date?: string;
  gender?: string;
  persona_id?: string;
}

export interface ElderPatchInput {
  name?: string;
  birth_date?: string;
  gender?: string;
  persona_id?: string;
  status?: ElderStatus;
}

export interface ElderCreateResult {
  elder_id: string;
  status: string;
  session_seq: number;
  created_at: string;
}

/** FR-ADM-09 退房：`reason` 原样进审计；`archive=false` 落 away，true 落 archived。 */
export interface ElderCheckoutInput {
  archive?: boolean;
  reason?: string;
}

/**
 * 退房回执（`app/api/elders.py:467`）。
 * 重复退房计数全 0 且 `audit_log_id=null`，页面据此说明「此前已退房」而不是报错。
 */
export interface ElderCheckoutReceipt {
  elder_id: string;
  elder_status: string | null;
  archived: boolean;
  memories_purged: number;
  voiceprints_removed: number;
  sessions_closed: number;
  relations_removed: number;
  devices_unbound: number;
  staff_cards_revoked: number;
  audit_log_id: string | null;
}

/** 退房原因闭集：取值与 `app/api/elders.py:460` 的审计口径一致。 */
export const CHECKOUT_REASON_OPTIONS: { value: string; label: string }[] = [
  { value: "checkout", label: "正常退房" },
  { value: "transfer", label: "转院" },
  { value: "deceased", label: "离世" },
];

/**
 * 写请求统一收口：丢掉 undefined / null / 空串（含纯空白），只留真正要写的字符串。
 * 这样「留空即不传」在数据层就成立，页面不需要为每个字段写一次三元。
 */
function compact(input: Record<string, unknown>): Record<string, string> {
  const body: Record<string, string> = {};
  for (const [key, value] of Object.entries(input)) {
    if (typeof value !== "string") continue;
    const trimmed = value.trim();
    if (trimmed) body[key] = trimmed;
  }
  return body;
}

/** E5：服务端分页列表；`status` 为空表示全部。 */
export function listElders(params: {
  page: number;
  pageSize?: number;
  status?: ElderStatus | "";
}): Promise<ElderList> {
  const search = query({
    page: params.page,
    page_size: params.pageSize ?? PAGE_SIZE,
    status: params.status || undefined,
  });
  return apiGet<ElderList>(`/api/elders${search}`);
}

/** E7：单个长者档案详情。 */
export function getElder(elderId: string): Promise<ElderDetail> {
  return apiGet<ElderDetail>(`/api/elders/${encodeURIComponent(elderId)}`);
}

/** E6：建档（201）。成功只要返回体里的 elder_id，页面随后回第 1 页刷新列表。 */
export function createElder(input: ElderCreateInput): Promise<ElderCreateResult> {
  return apiSend<ElderCreateResult>("/api/elders", "POST", compact({ ...input }));
}

/** E8：改档案/改状态统一入口；只发真正有值的字段。 */
export function patchElder(elderId: string, patch: ElderPatchInput): Promise<void> {
  return apiSend<void>(`/api/elders/${encodeURIComponent(elderId)}`, "PATCH", compact({ ...patch }));
}

/** FR-ADM-09：退房（仅 admin）。整条流水线在服务端同一事务内完成，前端只展示回执。 */
export function checkoutElder(
  elderId: string,
  input: ElderCheckoutInput = {},
): Promise<ElderCheckoutReceipt> {
  return apiSend<ElderCheckoutReceipt>(
    `/api/elders/${encodeURIComponent(elderId)}/checkout`,
    "POST",
    { archive: input.archive === true, reason: input.reason?.trim() || "checkout" },
  );
}

/** 详情里的空值统一显示「—」（后端字段可为 null，别直接渲染 null）。 */
export function textOrDash(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const text = String(value).trim();
  return text ? text : "—";
}
