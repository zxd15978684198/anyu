/**
 * 家属日报数据层（迭代 8 · W4）。
 *
 * 契约：`GET /api/reports/{elder_id}?date=`、`GET /api/reports/{elder_id}/list?days=30`。
 *
 * 三条硬约束：
 * 1. **payload 是展示物**，健康块/用药块的结构由服务端给；后端两路并行期间
 *    `health_block`（计划命名）与 `health`（report_service 实际命名）都认，页面只读 `healthBlockOf()`。
 * 2. **用药块在 迭代 7 交付前是占位**（计划裁决 S-D8-3），页面只显示 `summary`，
 *    **不得**自己算用药统计——没有数据就没有数据。
 * 3. 读日报的角色是 admin / doctor（care_worker 403 由服务端兜底），前端只做导航与按钮显隐。
 */
import { apiGet, query } from "./http";
import type { HealthGrade, HealthSource } from "./health";

/** 日报里「最新一条生效版本」的五字段快照。 */
export interface ReportHealthLatest {
  record_id: string;
  source: string;
  grade: string;
  systolic_mmhg: number | null;
  diastolic_mmhg: number | null;
  heart_rate_bpm: number | null;
  sleep_hours: number | null;
  mood_score: number | null;
  abnormal_reasons: string[];
  collected_at: string;
}

/** 健康块：当日绿/黄/红计数 + 来源计数 + 最新五字段（`state=missing` 表示当日一条都没有）。 */
export interface ReportHealthBlock {
  state: "collected" | "missing" | string;
  counts: Partial<Record<HealthGrade, number>>;
  total: number;
  sources: Partial<Record<HealthSource, number>>;
  latest: ReportHealthLatest | null;
}

/**
 * 用药块：四类终态分列（PRD FR-RPT-01 + TD §3.4），`pending` = 到点流程还没结束。
 * `summary` 只为**回填前的历史行**保留；新行带 `counts`，页面按计数显示。
 */
export interface ReportMedicationBlock {
  module?: string;
  state?: string;
  summary?: string;
  counts?: Record<string, number>;
  total?: number;
  plans_active?: number;
}

export interface DailyReportPayload {
  elder_id?: string;
  report_date?: string;
  health?: ReportHealthBlock;
  health_block?: ReportHealthBlock;
  medication?: ReportMedicationBlock;
  medication_block?: ReportMedicationBlock;
  sources?: Record<string, string>;
}

export interface DailyReport {
  report_id: string;
  elder_id: string;
  report_date: string;
  payload: DailyReportPayload | null;
  audio_url?: string | null;
  generated_at?: string;
}

/** 用药块的旧占位文案（只对回填前的历史行兜底，避免页面空白）。 */
export const MEDICATION_PLACEHOLDER_TEXT = "用药模块属 迭代 7 交付，暂未启用";

/** 四类终态的中文口径（与 `medication_events.terminal_state` 一一对应）。 */
export const MEDICATION_COUNT_LABEL: Record<string, string> = {
  taken_reported: "已服（自报）",
  refused: "拒服",
  missed_unconfirmed: "已送达未确认",
  notification_failed: "通知失败",
  pending: "尚未结束",
};

/** 展示顺序固定，避免 JSONB 键序抖动把同一份日报排成两种样子。 */
export const MEDICATION_COUNT_ORDER: readonly string[] = [
  "taken_reported",
  "refused",
  "missed_unconfirmed",
  "notification_failed",
  "pending",
];

/**
 * 用药计数行。**有 `counts` 就显示数字（0 也显示）**——0 次拒服是「数过了没有」，
 * 与「没数」不是一回事；只有缺 `counts`（回填前的历史行）才返回空数组，由页面退回说明。
 */
export function medicationCountRows(
  block: ReportMedicationBlock | null,
): { key: string; label: string; value: number }[] {
  const counts = block?.counts;
  if (!counts) return [];
  return MEDICATION_COUNT_ORDER.filter((key) => key in counts).map((key) => ({
    key,
    label: MEDICATION_COUNT_LABEL[key] ?? key,
    value: typeof counts[key] === "number" ? (counts[key] as number) : 0,
  }));
}

/** 计划命名 `health_block` 与 service 命名 `health` 都认；都没有则返回 null（页面渲染空态）。 */
export function healthBlockOf(payload: DailyReportPayload | null | undefined): ReportHealthBlock | null {
  if (!payload) return null;
  const block = payload.health_block ?? payload.health;
  return block ?? null;
}

export function medicationBlockOf(
  payload: DailyReportPayload | null | undefined,
): ReportMedicationBlock | null {
  if (!payload) return null;
  return payload.medication_block ?? payload.medication ?? null;
}

/** 健康块里某一档的计数（后端只回出现过的档位时，缺省按 0）。 */
export function countOf(block: ReportHealthBlock | null, grade: HealthGrade): number {
  const value = block?.counts?.[grade];
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

/** 来源计数排成「有出现过的」列表，顺序固定，避免 JSONB 键序抖动。 */
export function sourceRows(
  block: ReportHealthBlock | null,
): { source: string; count: number }[] {
  const sources = block?.sources ?? {};
  const order: HealthSource[] = ["voice_scheduled", "voice_active", "staff_scheduled", "staff_active"];
  return order
    .map((source) => ({ source, count: typeof sources[source] === "number" ? sources[source] : 0 }))
    .filter((row) => row.count > 0);
}

/** 单份日报（按长者 + 日期）。未生成当日日报时服务端 404，页面显示「尚未生成」。 */
export function getReport(elderId: string, date: string): Promise<DailyReport> {
  return apiGet<DailyReport>(
    `/api/reports/${encodeURIComponent(elderId)}${query({ date: date || undefined })}`,
  );
}

export interface ReportList {
  items: DailyReport[];
  total: number;
}

/** 近 N 天日报列表（默认 30）：数组或 `{items,total}` 都认。 */
export async function listReports(elderId: string, days = 30): Promise<ReportList> {
  const raw = await apiGet<unknown>(
    `/api/reports/${encodeURIComponent(elderId)}/list${query({ days })}`,
  );
  if (Array.isArray(raw)) return { items: raw as DailyReport[], total: raw.length };
  const body = (raw ?? {}) as { items?: unknown; total?: unknown };
  const items = Array.isArray(body.items) ? (body.items as DailyReport[]) : [];
  return { items, total: typeof body.total === "number" ? body.total : items.length };
}
