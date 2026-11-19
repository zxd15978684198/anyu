/**
 * 照护台用药模块 API 封装（迭代 11 补：医生开药单的前端入口）。
 *
 * 契约唯一依据：`app/api/medication.py`（端点与响应模型）+ `docs/接口契约_身份与权限.md` §6 权限矩阵。
 *
 * 端点与角色（服务端为准，前端只做显隐）：
 * - `GET   /api/medication/plans?elder_id=`   → doctor / care_worker / admin
 * - `POST  /api/medication/plans`             → **仅 doctor**（护工与管理员 403）
 * - `PATCH /api/medication/plans/{id}`        → **仅 doctor**（改药名/剂量/时刻/日期）
 * - `POST  /api/medication/plans/{id}/stop`   → **仅 doctor**（停用，幂等）
 * - `GET   /api/medication/events?elder_id=`  → doctor / care_worker / admin
 * - `POST  /api/medication/events/{id}/confirm` → doctor / care_worker / admin
 * - `GET   /api/medication/plans/import/template` → doctor / care_worker / admin（CSV 模板）
 * - `POST  /api/medication/plans/import?dry_run=` → **仅 doctor**（true=只预检不写库，false=落库）
 * - `GET   /api/medication/plans/merged?elder_id=` → doctor / care_worker / admin（多药合并卡片）
 *
 * 「改」与「停」的语义（与 `app/api/medication.py` 同款，W1 补齐 PRD:331 FR-MED-01）：
 * - **改** = 对同一行药单 UPDATE 且 `version + 1`；历史事件的 `drug_snapshot` 是 JSONB
 *   快照，**天然不受影响**，未来新事件读药单当前值。提交时带 `expected_version`，
 *   与库中不符则服务端 409 `VERSION_CONFLICT`（有人先改了，需要刷新后重来）。
 * - **停** = `status='stopped'`，**不新增事件态**；停用后不再生成新提醒，历史事件保留。
 * - 两个按钮只给医生（服务端 `WRITING_ROLES = {doctor}` 独立兜底，前端显隐只是体贴）。
 */
import { authedFetch } from "./api";
import { HttpError, NETWORK_TEXT, apiGet, apiSend, query } from "./http";
import { formatInstant } from "../shared/format";

/** 药单状态（`medication_plans.status` 的 CHECK 只允许这两值）。 */
export type PlanStatus = "active" | "stopped";

/** 事件流转态（`medication_events.status` 的四值）。 */
export type EventStatus = "calling" | "waiting_confirm" | "confirmed" | "escalated";

/** 事件终态（`terminal_state`，可空：还在流转中没有终态）。 */
export type TerminalState = "taken_reported" | "refused" | "missed_unconfirmed" | "notification_failed";

/** 语义色单一真源在 shared/tone（W4 收敛前本类型在照护台写了 5 份）。 */
import type { Tone as BadgeTone } from "../shared/tone";
export type { BadgeTone };

/** 一条药单（响应模型 `MedicationPlanOut`）。 */
export interface MedicationPlanItem {
  plan_id: string;
  elder_id: string;
  drug_name: string;
  dose: string;
  schedule_cron: string;
  start_date: string;
  end_date?: string | null;
  status: string;
  version: number;
  created_by?: string | null;
}

export interface MedicationPlanList {
  items: MedicationPlanItem[];
  total: number;
  elder_id: string;
}

/** 一条用药事件（响应模型 `MedicationEventOut`）。 */
export interface MedicationEventItem {
  reminder_id: string;
  plan_id: string;
  elder_id: string;
  scheduled_at: string;
  status: string;
  terminal_state?: string | null;
  late_taken: boolean;
  retry_count: number;
  confirm_channel?: string | null;
  confirmed_at?: string | null;
  triggered_by?: string | null;
}

export interface MedicationEventList {
  items: MedicationEventItem[];
  total: number;
  elder_id: string;
  status?: string | null;
}

export interface MedicationEventConfirmResult {
  reminder_id: string;
  status: string;
  terminal_state: string;
  confirm_channel: string;
  note?: string | null;
  late_taken: boolean;
}

/** 新建药单入参（`MedicationPlanCreateRequest`，`extra="forbid"`：多传字段直接 422）。 */
export interface MedicationPlanCreateInput {
  elder_id: string;
  drug_name: string;
  dose: string;
  /** 只支持 `HH:MM`，服务端正则校验。 */
  schedule_cron: string;
  start_date: string;
  end_date?: string | null;
}

/** 修改药单入参（`MedicationPlanPatchRequest`，`extra="forbid"`：只认这些字段）。 */
export interface MedicationPlanPatchInput {
  drug_name?: string;
  dose?: string;
  /** 只支持 `HH:MM`，服务端正则校验。 */
  schedule_cron?: string;
  start_date?: string;
  end_date?: string | null;
  /** 乐观锁：传了且与库中版本不符 → 409 `VERSION_CONFLICT`（本次未保存）。 */
  expected_version?: number;
}

/** 停用结果：首次停用返回药单对象；已停用时返回 `{ already_stopped: true }`。 */
export type MedicationPlanStopResult = MedicationPlanItem | { already_stopped: true };

/** 药单被并发修改时的提示（`VERSION_CONFLICT` 不在 `http.ts` 的中文错误码表里）。 */
export const VERSION_CONFLICT_TEXT = "药单已被他人修改，请刷新后重新编辑（本次未保存）";

export const PLAN_STATUS_LABEL: Record<PlanStatus, string> = {
  active: "启用中",
  stopped: "已停用",
};

export const EVENT_STATUS_LABEL: Record<EventStatus, string> = {
  calling: "呼叫中",
  waiting_confirm: "等待确认",
  confirmed: "已确认",
  escalated: "已升级",
};

export const TERMINAL_STATE_LABEL: Record<TerminalState, string> = {
  taken_reported: "长者自报已服",
  refused: "长者拒服",
  missed_unconfirmed: "四次未确认",
  notification_failed: "通知未送达",
};

export const CHANNEL_LABEL: Record<string, string> = {
  voice: "语音",
  button: "照护台代确认",
};

/** 事件状态筛选（含「全部」）。 */
export const EVENT_STATUS_OPTIONS: { value: string; label: string }[] = [
  { value: "", label: "全部状态" },
  { value: "calling", label: EVENT_STATUS_LABEL.calling },
  { value: "waiting_confirm", label: EVENT_STATUS_LABEL.waiting_confirm },
  { value: "confirmed", label: EVENT_STATUS_LABEL.confirmed },
  { value: "escalated", label: EVENT_STATUS_LABEL.escalated },
];

/** 还能被代确认的事件（终态一旦落库，服务端 409，页面就不该给按钮）。 */
export function canConfirm(event: MedicationEventItem): boolean {
  if (event.terminal_state) return false;
  return event.status === "calling" || event.status === "waiting_confirm";
}

export function planStatusLabel(status: string): string {
  return PLAN_STATUS_LABEL[status as PlanStatus] ?? status;
}

export function planStatusTone(status: string): BadgeTone {
  return status === "active" ? "success" : "neutral";
}

export function eventStatusLabel(status: string): string {
  return EVENT_STATUS_LABEL[status as EventStatus] ?? status;
}

export function eventStatusTone(status: string): BadgeTone {
  if (status === "escalated") return "danger";
  if (status === "confirmed") return "success";
  if (status === "waiting_confirm") return "warning";
  return "info";
}

export function terminalStateLabel(value?: string | null): string {
  if (!value) return "—";
  return TERMINAL_STATE_LABEL[value as TerminalState] ?? value;
}

export function terminalStateTone(value?: string | null): BadgeTone {
  if (value === "refused" || value === "missed_unconfirmed") return "danger";
  if (value === "notification_failed") return "warning";
  if (value === "taken_reported") return "success";
  return "neutral";
}

export function channelLabel(value?: string | null): string {
  if (!value) return "—";
  return CHANNEL_LABEL[value] ?? value;
}

/** `HH:MM` 已经是给人看的格式，原样显示即可（不要 new Date 解析出时区偏移）。 */
export function scheduleText(value: string): string {
  return value || "—";
}

/** 日期区间文案：`2026-11-01 起` / `2026-11-01 ~ 2026-11-30`。 */
export function windowText(start: string, end?: string | null): string {
  if (!start) return "—";
  return end ? `${start} ~ ${end}` : `${start} 起，长期`;
}

/** ISO 时间 → `MM-DD HH:MM`（**Asia/Shanghai**，护理人员要看的是「哪班」）。 */
export function timeText(value?: string | null): string {
  if (!value) return "—";
  // 收敛到 shared/format（AGENTS §8）。原实现走 toLocaleString 即浏览器本地时区，
  // 换一台机器「哪班」就会漂，这对用药是实质风险。
  return formatInstant(value, { withYear: false, invalid: "raw" });
}

export function listPlans(elderId: string): Promise<MedicationPlanList> {
  return apiGet<MedicationPlanList>(`/api/medication/plans${query({ elder_id: elderId })}`);
}

export function createPlan(input: MedicationPlanCreateInput): Promise<MedicationPlanItem> {
  return apiSend<MedicationPlanItem>("/api/medication/plans", "POST", input);
}

/** 医生改药单：`PATCH /api/medication/plans/{id}`，返回**更新后**的药单（含新 version）。 */
export function updatePlan(
  planId: string,
  input: MedicationPlanPatchInput,
): Promise<MedicationPlanItem> {
  return apiSend<MedicationPlanItem>(`/api/medication/plans/${planId}`, "PATCH", input);
}

/** 医生停用药单：幂等——已停用时服务端返回 200 与 `{ already_stopped: true }`。 */
export function stopPlan(
  planId: string,
  reason?: string | null,
): Promise<MedicationPlanStopResult> {
  return apiSend<MedicationPlanStopResult>(
    `/api/medication/plans/${planId}/stop`,
    "POST",
    reason ? { reason } : {},
  );
}

/** 停用响应是否「本来就已停用」（幂等分支），页面据此给不同的提示文案。 */
export function isAlreadyStopped(
  result: MedicationPlanStopResult,
): result is { already_stopped: true } {
  return (result as { already_stopped?: boolean }).already_stopped === true;
}

export function listEvents(elderId: string, status = ""): Promise<MedicationEventList> {
  return apiGet<MedicationEventList>(
    `/api/medication/events${query({ elder_id: elderId, status })}`,
  );
}

export function confirmEvent(
  reminderId: string,
  note?: string | null,
): Promise<MedicationEventConfirmResult> {
  return apiSend<MedicationEventConfirmResult>(
    `/api/medication/events/${reminderId}/confirm`,
    "POST",
    note ? { note } : {},
  );
}

// ---------------------------------------------------------------------------
// FR-MED-07 批量导入与多药合并
//
// 契约唯一依据：`app/api/medication.py` 的 `download_import_template` / `import_plans` /
// `merged_plan_cards`，以及响应模型 `MedicationImportOut` / `MedicationMergedOut`。
// 三条端点的请求层集中在本模块，页面只负责选文件与展示逐行结果（口径只有一处）。
// ---------------------------------------------------------------------------

/** 模板存盘用的文件名（服务端 `Content-Disposition` 也是这个）。 */
export const IMPORT_TEMPLATE_FILENAME = "medication-import-template.csv";

/** 失败行（`MedicationImportIssueOut`）：`line=0` 表示整文件级问题（空文件/超行数/表头缺列）。 */
export interface MedicationImportIssue {
  line: number;
  field: string;
  message: string;
}

/** 合法但重复、被跳过的行（`MedicationImportSkippedOut`）：不写库，但不算失败。 */
export interface MedicationImportSkip {
  line: number;
  drug_name: string;
  reason: string;
  message: string;
  plan_id?: string | null;
}

/** 一行将被创建（预检态）或已创建（应用态）的药单（`MedicationImportPlanOut`）。 */
export interface MedicationImportRow {
  line: number;
  /** 预检态为 null：还没落库，自然没有 plan_id。 */
  plan_id?: string | null;
  elder_id: string;
  elder_name: string;
  drug_name: string;
  dose: string;
  schedule_cron: string;
  start_date: string;
  end_date?: string | null;
}

/**
 * 导入结果（`MedicationImportOut`）：**预览与应用是同一个模型**，靠 `dry_run` 区分。
 *
 * 服务端刻意用 200 而不是 422 表达「行级校验没过」，因为契约错误体装不下
 * 「第几行、哪个字段、为什么」；页面要的正是这张能逐行修的失败表。
 */
export interface MedicationImportResult {
  /** 全量预检通过才为 true；false 时一行都没写（不做半截导入）。 */
  accepted: boolean;
  /** true=只预检（未写库），false=真的写库了。 */
  dry_run: boolean;
  all_or_nothing: boolean;
  total_rows: number;
  /** 预检态=将新建条数；应用态=实际新建条数。 */
  will_create: number;
  created: MedicationImportRow[];
  skipped: MedicationImportSkip[];
  /** 逐行/整文件问题：行号 + 字段 + 原因，页面必须原样展示。 */
  errors: MedicationImportIssue[];
}

/** 合并卡片里的一味药（`MedicationMergedDrugOut`）。 */
export interface MedicationMergedDrug {
  plan_id: string;
  drug_name: string;
  dose: string;
}

/** 同一时刻的多味药合成一张卡片（`MedicationMergedSlotOut`）。 */
export interface MedicationMergedSlot {
  schedule_cron: string;
  on_date: string;
  drug_count: number;
  summary: string;
  drugs: MedicationMergedDrug[];
}

/** 多药合并卡片（`MedicationMergedOut`）：只读，不含任何写操作。 */
export interface MedicationMergedCards {
  elder_id: string;
  on_date: string;
  total_slots: number;
  slots: MedicationMergedSlot[];
}

/**
 * 下载导入模板（`GET /api/medication/plans/import/template`，直接回 CSV 文件流）。
 *
 * 返回 Blob 而不是在这里点 `<a download>`：存盘是浏览器行为，交给页面；
 * 本层只管把带鉴权的字节流拿到手（裸开链接带不上 Bearer，只会 401）。
 */
export async function downloadImportTemplate(): Promise<Blob> {
  let response: Response;
  try {
    response = await authedFetch("/api/medication/plans/import/template");
  } catch {
    throw new HttpError(0, "NETWORK", NETWORK_TEXT);
  }
  if (!response.ok) {
    throw new HttpError(response.status, "BAD_REQUEST", "模板下载失败，请稍后重试");
  }
  return response.blob();
}

/**
 * 上传导入文件（两段式共用的底层实现）：`dry_run=true` 只预检，`false` 才落库。
 *
 * 不能复用 `apiSend`：它固定 JSON body，而这里必须是 multipart（字段名 `file`，
 * 与后端 `UploadFile = File(...)` 对齐）。错误体仍是契约的 `{detail, code, trace_id}`，
 * 故只把 `code` / `detail` 摘出来交给 `HttpError`，页面再按 code 分支出中文。
 */
async function sendImportFile(
  file: File,
  elderId: string,
  dryRun: boolean,
): Promise<MedicationImportResult> {
  const form = new FormData();
  form.append("file", file);
  const path = `/api/medication/plans/import${query({
    elder_id: elderId || undefined,
    dry_run: dryRun,
  })}`;

  let response: Response;
  try {
    response = await authedFetch(path, { method: "POST", body: form });
  } catch {
    throw new HttpError(0, "NETWORK", NETWORK_TEXT);
  }

  if (!response.ok) {
    let code = "";
    let message = "导入请求被拒绝，请检查文件与权限后重试";
    try {
      const body = (await response.json()) as { code?: unknown; detail?: unknown };
      if (typeof body.code === "string" && body.code) code = body.code;
      if (typeof body.detail === "string" && body.detail) message = body.detail;
    } catch {
      /* 非 JSON 错误体：按状态码兜底 */
    }
    if (code === "PAYLOAD_TOO_LARGE") message = "文件超过 5MB 上限，请拆分后再导入";
    throw new HttpError(response.status, code || "BAD_REQUEST", message);
  }
  return (await response.json()) as MedicationImportResult;
}

/**
 * 第一段 · 预览（`POST /api/medication/plans/import?dry_run=true`）。
 *
 * 服务端口径是「全量预检不过就整批不写」，所以预览不会改任何数据；
 * 页面必须先把 `errors`（行号 + 字段 + 原因）原样给医生看，再允许点「确认导入」。
 *
 * 注意：后端的预检开关是 `dry_run`，**不是** `apply`——写错参数名会被 FastAPI 当成
 * 未知查询串直接忽略，`apply=false` 会变成真的落库。这里只认 `dry_run`。
 */
export function previewImport(file: File, elderId = ""): Promise<MedicationImportResult> {
  return sendImportFile(file, elderId, true);
}

/**
 * 第二段 · 应用（`POST /api/medication/plans/import?dry_run=false`），真正写库。
 *
 * 只在前一次预览通过（`accepted === true`）之后调用；即便有人绕过页面直接调，
 * 服务端会再跑一遍预检，不过就整批不写并把 `errors` 原样返回，不存在半截导入。
 * `dry_run` 缺省是 false，所以这里**始终显式传 false**，不依赖缺省值。
 */
export function applyImport(file: File, elderId = ""): Promise<MedicationImportResult> {
  return sendImportFile(file, elderId, false);
}

/**
 * 多药合并卡片（`GET /api/medication/plans/merged`）：同一时刻的多味药并成一张卡。
 *
 * `on_date` 缺省时服务端按「今天（上海）」算；传了则按那一天在服的药单算。
 * 只统计启用中且区间覆盖该日的药单，按每日时刻升序——只读，不带任何写操作。
 */
export function fetchMergedCards(elderId: string, onDate?: string): Promise<MedicationMergedCards> {
  return apiGet<MedicationMergedCards>(
    `/api/medication/plans/merged${query({ elder_id: elderId, on_date: onDate })}`,
  );
}
