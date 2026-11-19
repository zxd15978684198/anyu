/**
 * 家属日报数据层（W4）。
 *
 * 契约（只读，不改后端）：
 * - `GET /api/reports/{elder_id}/list?days=N`（N ≤ 365）→ `{items, total, days}`
 * - `GET /api/reports/{elder_id}?date=YYYY-MM-DD` → 单条
 *
 * 三条硬约束：
 * 1. **payload 是展示物**：健康块/用药块的结构由服务端给；两处命名都认
 *    （`health` / `health_block`），页面只读 `healthBlockOf()`。
 * 2. **用药块是占位**，页面只显示服务端给的 `summary`，**不自己算用药统计**——
 *    没有数据就没有数据，家属端尤其不能编。
 * 3. `state === "missing"` 或 `latest === null` 表示当天一条都没采到，
 *    这不是「0 次血压」，摘要与趋势图都要按「缺失」处理。
 */
import { formatShortDate, isToday, shiftDate } from "./format";
import { apiGet, query } from "./http";

/** 家属端一次取多少天日报（首页列表窗口；14 天趋势图从中切片）。 */
export const REPORT_WINDOW_DAYS = 30;
/** 趋势图窗口（PRD FR-HLT-09 的近 14 天）。 */
export const TREND_DAYS = 14;

export type HealthGrade = "red" | "yellow" | "green" | "missing";

export interface ReportHealthLatest {
  record_id?: string;
  source?: string;
  grade?: string;
  systolic_mmhg?: number | null;
  diastolic_mmhg?: number | null;
  heart_rate_bpm?: number | null;
  sleep_hours?: number | null;
  mood_score?: number | null;
  abnormal_reasons?: string[] | null;
  collected_at?: string;
}

export interface ReportHealthBlock {
  state?: string;
  counts?: Partial<Record<HealthGrade, number>>;
  total?: number;
  sources?: Partial<Record<HealthSourceKey, number>>;
  latest?: ReportHealthLatest | null;
}

/** 采集方式键（与 `health_records.source` 的闭集一致）。 */
export type HealthSourceKey =
  | "voice_scheduled"
  | "voice_active"
  | "staff_scheduled"
  | "staff_active";

/**
 * 用药块：四类终态分列（PRD FR-RPT-01），外加「到点流程还没结束」的 `pending`。
 *
 * `summary` 只为**回填前的历史行**保留（那些行只有一句占位说明、没有 `counts`）；
 * 新行不带 `summary`，页面必须按 `counts` 显示，不许再退回那句话。
 */
export interface ReportMedicationBlock {
  module?: string;
  /** `collected` = 当日有数据；`not_configured` = 没有药单也没有到点事件。 */
  state?: string;
  summary?: string;
  counts?: Record<string, number>;
  total?: number;
  plans_active?: number;
}

/** 四类终态的中文口径（与 TD §3.4 `terminal_state` 一一对应，顺序固定）。 */
export const MEDICATION_COUNT_LABEL: Record<string, string> = {
  taken_reported: "已服（自报）",
  refused: "拒服",
  missed_unconfirmed: "已送达未确认",
  notification_failed: "通知失败",
};

/** 展示顺序：四类终态在前，`pending` 只在真的存在时补在最后。 */
export const MEDICATION_COUNT_ORDER: string[] = [
  "taken_reported",
  "refused",
  "missed_unconfirmed",
  "notification_failed",
  "pending",
];

/**
 * 用药块的计数行。**有 `counts` 就按计数显示（0 也显示）**——0 次拒服是「数过了，
 * 没有」，与「没数」不是一回事；只有整块缺 `counts`（回填前的历史行）才返回空数组，
 * 由页面退回 `summary`。
 */
export function medicationCountRows(block: ReportMedicationBlock | null): SourceRow[] {
  if (!block?.counts) return [];
  const counts = block.counts;
  return MEDICATION_COUNT_ORDER.filter((key) => key in counts).map((key) => ({
    label: key === "pending" ? "尚未结束" : (MEDICATION_COUNT_LABEL[key] ?? key),
    text: `${displayCount(counts[key])} 次`,
  }));
}

function displayCount(value: unknown): string {
  const number = numberOrNull(value);
  return number == null ? "—" : String(number);
}

export interface DailyReportPayload {
  elder_id?: string;
  report_date?: string;
  health?: ReportHealthBlock;
  health_block?: ReportHealthBlock;
  medication?: ReportMedicationBlock;
  medication_block?: ReportMedicationBlock;
  /** 数据来源说明（`health` / `medication` / `note`），值是机器码或整句。 */
  sources?: Record<string, string>;
}

export interface DailyReportItem {
  report_id: string;
  elder_id: string;
  elder_name?: string;
  report_date: string;
  payload?: DailyReportPayload | null;
  audio_url?: string | null;
  generated_at?: string;
}

export interface TrendPoint {
  date: string;
  systolic: number | null;
  diastolic: number | null;
}

// ---------------------------------------------------------------------------
// 取值工具
// ---------------------------------------------------------------------------

export function healthBlockOf(payload: DailyReportPayload | null | undefined): ReportHealthBlock | null {
  if (!payload) return null;
  return payload.health ?? payload.health_block ?? null;
}

export function medicationBlockOf(
  payload: DailyReportPayload | null | undefined,
): ReportMedicationBlock | null {
  if (!payload) return null;
  return payload.medication ?? payload.medication_block ?? null;
}

/** JSONB 出来的数值可能是 number / string / null，统一收成 number | null。 */
export function numberOrNull(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

/** 数值展示：整数不带小数点，7.5 就写 7.5。 */
export function displayNumber(value: number | null): string {
  if (value === null) return "—";
  return Number.isInteger(value) ? String(value) : String(Math.round(value * 10) / 10);
}

export function reportDateOf(item: DailyReportItem): string {
  return item.report_date.slice(0, 10);
}

/** 当天有没有采到数据：`latest` 为空即视为缺失（缺失不是 0）。 */
export function hasHealthData(item: DailyReportItem): boolean {
  return healthBlockOf(item.payload)?.latest != null;
}

/** 健康等级：色点、摘要、详情标题共用同一处判定。 */
export function gradeOf(item: DailyReportItem): HealthGrade {
  const grade = healthBlockOf(item.payload)?.latest?.grade;
  if (grade === "red" || grade === "yellow" || grade === "green") return grade;
  return "missing";
}

export const GRADE_LABEL: Record<HealthGrade, string> = {
  red: "红 · 需关注",
  yellow: "黄 · 待关注",
  green: "绿 · 正常",
  missing: "未采集",
};

// ---------------------------------------------------------------------------
// 摘要（列表卡片一句话）
// ---------------------------------------------------------------------------

/**
 * 例：「今日血压 128/78，睡眠 7.5 小时」。
 * 缺项只写有的那几项；一项数值都没有时写清楚「没有采集到」，不留空。
 */
export function summaryOf(item: DailyReportItem): string {
  const latest = healthBlockOf(item.payload)?.latest ?? null;
  if (!latest) return "当天没有采集到健康数据";

  const prefix = isToday(reportDateOf(item)) ? "今日" : "当日";
  const systolic = numberOrNull(latest.systolic_mmhg);
  const diastolic = numberOrNull(latest.diastolic_mmhg);
  const heartRate = numberOrNull(latest.heart_rate_bpm);
  const sleepHours = numberOrNull(latest.sleep_hours);

  const parts: string[] = [];
  if (systolic !== null && diastolic !== null) {
    parts.push(`血压 ${displayNumber(systolic)}/${displayNumber(diastolic)}`);
  } else if (systolic !== null) {
    parts.push(`收缩压 ${displayNumber(systolic)}`);
  } else if (diastolic !== null) {
    parts.push(`舒张压 ${displayNumber(diastolic)}`);
  }
  if (sleepHours !== null) {
    parts.push(`睡眠 ${displayNumber(sleepHours)} 小时`);
  } else if (heartRate !== null) {
    parts.push(`心率 ${displayNumber(heartRate)} 次/分`);
  }

  if (parts.length === 0) {
    const reasons = latest.abnormal_reasons ?? [];
    return reasons.length > 0 ? "当天有健康提醒，建议咨询医生" : "当天有采集记录，暂无具体数值";
  }
  return `${prefix}${parts.join("，")}`;
}

// ---------------------------------------------------------------------------
// 异常原因 / 数据来源（翻成人话，不出现机器码与诊断口径）
// ---------------------------------------------------------------------------

const REASON_LABEL: Record<string, string> = {
  systolic_red: "收缩压读数偏高",
  diastolic_red: "舒张压读数偏高",
  systolic_high: "收缩压略高于常见范围",
  diastolic_high: "舒张压略高于常见范围",
  systolic_low: "收缩压略低于常见范围",
  diastolic_low: "舒张压略低于常见范围",
  heart_rate_abnormal: "心率不在常见范围",
  sleep_low: "睡眠时间偏短",
  sleep_high: "睡眠时间偏长",
  mood_low: "情绪评分偏低",
  systolic_missing: "本次未填收缩压",
  diastolic_missing: "本次未填舒张压",
  sleep_missing: "本次未填睡眠时长",
  mood_missing: "本次未填情绪评分",
  complaint: "长者主动说明身体不适",
};

/** 只翻译已知原因码；未知码给一句中性提示，不把 `xxx_yyy` 甩给家属。 */
export function reasonLabel(code: string): string {
  return REASON_LABEL[code] ?? "本次有一项需要关注";
}

export function reasonLabels(latest: ReportHealthLatest | null): string[] {
  return (latest?.abnormal_reasons ?? []).map((code) => reasonLabel(String(code)));
}

const PAYLOAD_SOURCE_KEY: Record<string, string> = {
  health: "健康数据",
  medication: "用药数据",
  note: "说明",
};

const PAYLOAD_SOURCE_VALUE: Record<string, string> = {
  health_records: "来自每日健康采集记录（语音采集或护工代填）",
  medication_events: "来自用药提醒记录（每次到点一条）",
  // 回填前的历史行仍是这个值；`scripts/backfill_report_medication.py` 跑完后应绝迹。
  not_enabled_day7: "这条日报生成时用药模块尚未启用，已由回填脚本补齐",
};

export const HEALTH_SOURCE_LABEL: Record<HealthSourceKey, string> = {
  voice_scheduled: "语音 · 定时采集",
  voice_active: "语音 · 主动说明",
  staff_scheduled: "护工代填 · 定时采集",
  staff_active: "护工代填 · 补充记录",
};

export interface SourceRow {
  label: string;
  text: string;
}

/**
 * `payload.sources` 翻成人话（key 是数据域，value 是来源码或整句）。
 * `note` 是服务端写的整句说明，原样展示——它属于「数据是这么来的」的权威口径。
 */
export function payloadSourceRows(payload: DailyReportPayload | null | undefined): SourceRow[] {
  const sources = payload?.sources ?? {};
  const rows: SourceRow[] = [];
  for (const [key, value] of Object.entries(sources)) {
    if (typeof value !== "string" || value.trim() === "") continue;
    if (key === "note") continue;
    rows.push({
      label: PAYLOAD_SOURCE_KEY[key] ?? key,
      text: PAYLOAD_SOURCE_VALUE[value] ?? value,
    });
  }
  return rows;
}

export function payloadSourceNote(payload: DailyReportPayload | null | undefined): string | null {
  const note = payload?.sources?.note;
  return typeof note === "string" && note.trim() !== "" ? note.trim() : null;
}

/** 当天各采集方式的次数（只列出现过的，顺序固定，避免 JSONB 键序抖动）。 */
export function healthSourceRows(block: ReportHealthBlock | null): SourceRow[] {
  const counts = block?.sources ?? {};
  const order: HealthSourceKey[] = ["voice_scheduled", "voice_active", "staff_scheduled", "staff_active"];
  return order
    .map((key) => ({ key, count: numberOrNull(counts[key]) ?? 0 }))
    .filter((row) => row.count > 0)
    .map((row) => ({
      label: HEALTH_SOURCE_LABEL[row.key],
      text: `${String(row.count)} 次`,
    }));
}

// ---------------------------------------------------------------------------
// 14 天趋势：补全日历轴（列表不补空行，趋势图必须补，否则缺口会被连成直线）
// ---------------------------------------------------------------------------

/**
 * 从「近 N 天日报列表」切出趋势点。
 *
 * 关键：日报列表**只回存在的日期**，缺失日不补空行。若直接按列表项画线，
 * 中间少了一天就会被两端直接连起来，看着像当天测过 —— 所以这里先按日历
 * 生成 `days` 个连续日期（以 `endDate` 收尾），再逐日回填，缺失日留 null。
 */
export function trendPoints(
  reports: DailyReportItem[],
  endDate: string,
  days: number = TREND_DAYS,
): TrendPoint[] {
  const byDate = new Map<string, DailyReportItem>();
  for (const item of reports) byDate.set(reportDateOf(item), item);

  const points: TrendPoint[] = [];
  for (let offset = days - 1; offset >= 0; offset -= 1) {
    const date = shiftDate(endDate, -offset);
    const latest = healthBlockOf(byDate.get(date)?.payload)?.latest ?? null;
    points.push({
      date,
      systolic: numberOrNull(latest?.systolic_mmhg),
      diastolic: numberOrNull(latest?.diastolic_mmhg),
    });
  }
  return points;
}

/** 趋势图 `<title>`：「11-09 128/78」（缺一半就只写有的那一半）。 */
export function trendPointTitle(point: TrendPoint): string {
  const date = formatShortDate(point.date);
  const systolic = numberOrNull(point.systolic);
  const diastolic = numberOrNull(point.diastolic);
  if (systolic !== null && diastolic !== null) return `${date} ${displayNumber(systolic)}/${displayNumber(diastolic)}`;
  if (systolic !== null) return `${date} 收缩压 ${displayNumber(systolic)}`;
  if (diastolic !== null) return `${date} 舒张压 ${displayNumber(diastolic)}`;
  return `${date} 没有采集`;
}

// ---------------------------------------------------------------------------
// 请求
// ---------------------------------------------------------------------------

interface RawReportList {
  items?: unknown;
  total?: unknown;
  days?: unknown;
}

/** 近 `days` 天日报（服务端已按日期倒序；这里再排一次，防后端换实现）。 */
export async function listReports(
  elderId: string,
  days: number = REPORT_WINDOW_DAYS,
): Promise<DailyReportItem[]> {
  const raw = await apiGet<RawReportList | DailyReportItem[]>(
    `/api/reports/${encodeURIComponent(elderId)}/list${query({ days })}`,
  );
  const items = Array.isArray(raw) ? raw : Array.isArray(raw.items) ? (raw.items as DailyReportItem[]) : [];
  return [...items].sort((a, b) => reportDateOf(b).localeCompare(reportDateOf(a)));
}

/** 单条日报；该日未生成时服务端 404（页面显示「这份日报还没生成」）。 */
export function getReport(elderId: string, date: string): Promise<DailyReportItem> {
  return apiGet<DailyReportItem>(
    `/api/reports/${encodeURIComponent(elderId)}${query({ date: date || undefined })}`,
  );
}
