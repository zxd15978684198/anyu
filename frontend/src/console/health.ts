/**
 * 健康采集数据层（迭代 8 · W4）。
 *
 * 契约：`GET/POST /api/health/records`、`GET /api/health/records/{elder_id}?days=`、
 * `POST /api/health/records/{record_id}/supersede`、`POST /api/health/review/{notification_id}/resolve`。
 *
 * 三条硬约束写在这里而不是页面里：
 * 1. **分级阈值只在服务端**（PRD §4.6：单分量判红 ≥180/≥120；黄 140–179 / 90–119；缺失判黄）。
 *    前端只做「是不是数字」与范围提示，绝不自己判 grade，否则补录一次就会和引擎口径打架。
 * 2. **写短名、读兼容长名**：提交体按契约用 `systolic / diastolic / heart_rate / sleep_hours / mood_score`，
 *    读回时 ORM 直出的 `systolic_mmhg / diastolic_mmhg / heart_rate_bpm` 也要能吃（后端两路并行，别让页面挑字段名）。
 * 3. `grade` 是闭集 `green|yellow|red|missing`，文案与徽标色只由本文件的 `GRADE_OPTIONS` 提供。
 */
import { apiGet, apiSend, query } from "./http";

export type HealthGrade = "green" | "yellow" | "red" | "missing";
export type HealthSource = "voice_scheduled" | "voice_active" | "staff_scheduled" | "staff_active";
/** 照护台能写入的两种来源（语音两源属 pad / 子图，不由照护台代填）。 */
export type StaffHealthSource = "staff_scheduled" | "staff_active";

/** 语义色单一真源在 shared/tone（W4）。 */
import type { Tone as BadgeTone } from "../shared/tone";
export type { BadgeTone };

/** 服务端分页默认 20，与长者档案页保持一致，避免两页分页手感不同。 */
export const HEALTH_PAGE_SIZE = 20;
/** 趋势窗口（PRD 要求近 14 天）。 */
export const TREND_DAYS = 14;

export const GRADE_OPTIONS: { value: HealthGrade; label: string; tone: BadgeTone }[] = [
  { value: "green", label: "绿 · 正常", tone: "success" },
  { value: "yellow", label: "黄 · 待关注", tone: "warning" },
  { value: "red", label: "红 · 需复核", tone: "danger" },
  { value: "missing", label: "缺失", tone: "neutral" },
];

export const SOURCE_LABEL: Record<HealthSource, string> = {
  voice_scheduled: "语音 · 定时采集",
  voice_active: "语音 · 主动主诉",
  staff_scheduled: "代填 · 定时采集",
  staff_active: "代填 · 主动补录",
};

export const STAFF_SOURCE_OPTIONS: { value: StaffHealthSource; label: string }[] = [
  { value: "staff_scheduled", label: "代填 · 定时采集" },
  { value: "staff_active", label: "代填 · 主动补录" },
];

export function isHealthGrade(value: string): value is HealthGrade {
  return GRADE_OPTIONS.some((option) => option.value === value);
}

/** 未知 grade 一律按「缺失」显示，不把后端新枚举渲染成空白。 */
export function gradeOption(grade: string): { value: HealthGrade; label: string; tone: BadgeTone } {
  return GRADE_OPTIONS.find((option) => option.value === grade) ?? GRADE_OPTIONS[3];
}

export function gradeLabel(grade: string): string {
  return gradeOption(grade).label;
}

export function gradeTone(grade: string): BadgeTone {
  return gradeOption(grade).tone;
}

export function sourceLabel(source: string): string {
  return SOURCE_LABEL[source as HealthSource] ?? source;
}

/** 一条采集记录：长名（ORM 直出）与短名（契约示例）都声明成可选，页面统一走下面的取值函数。 */
export interface HealthRecordItem {
  record_id: string;
  elder_id: string;
  record_date: string;
  source: string;
  grade: string;
  revision?: number;
  superseded_by?: string | null;
  recorded_by_staff_id?: string | null;
  /** 软删留痕：非空表示这条记录已被医生/管理员撤销，不再进入日报与趋势。 */
  revoked_at?: string | null;
  revoked_by?: string | null;
  collected_at?: string;
  abnormal_reasons?: string[] | null;
  systolic_mmhg?: number | null;
  diastolic_mmhg?: number | null;
  heart_rate_bpm?: number | null;
  systolic?: number | null;
  diastolic?: number | null;
  heart_rate?: number | null;
  sleep_hours?: number | null;
  mood_score?: number | null;
}

function finite(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function pickNumber(...values: unknown[]): number | null {
  for (const value of values) {
    const num = finite(value);
    if (num !== null) return num;
  }
  return null;
}

export function systolicOf(record: HealthRecordItem): number | null {
  return pickNumber(record.systolic_mmhg, record.systolic);
}

export function diastolicOf(record: HealthRecordItem): number | null {
  return pickNumber(record.diastolic_mmhg, record.diastolic);
}

export function heartRateOf(record: HealthRecordItem): number | null {
  return pickNumber(record.heart_rate_bpm, record.heart_rate);
}

export function sleepHoursOf(record: HealthRecordItem): number | null {
  return finite(record.sleep_hours);
}

export function moodScoreOf(record: HealthRecordItem): number | null {
  return finite(record.mood_score);
}

export function recordGradeOf(record: HealthRecordItem): HealthGrade | null {
  return isHealthGrade(record.grade) ? record.grade : null;
}

export interface HealthRecordList {
  items: HealthRecordItem[];
  total: number;
  page: number;
  page_size: number;
}

function normalizeRecordList(raw: unknown): HealthRecordList {
  const body = (raw ?? {}) as { items?: unknown; total?: unknown; page?: unknown; page_size?: unknown };
  const items = Array.isArray(body.items) ? (body.items as HealthRecordItem[]) : [];
  return {
    items,
    total: finite(body.total) ?? items.length,
    page: finite(body.page) ?? 1,
    page_size: finite(body.page_size) ?? HEALTH_PAGE_SIZE,
  };
}

/** 列表：`date` 为空表示不限日期（服务端默认按天倒序）。 */
export async function listHealthRecords(params: {
  elderId: string;
  date?: string;
  page?: number;
  pageSize?: number;
}): Promise<HealthRecordList> {
  const search = query({
    elder_id: params.elderId,
    date: params.date || undefined,
    page: params.page ?? 1,
    page_size: params.pageSize ?? HEALTH_PAGE_SIZE,
  });
  return normalizeRecordList(await apiGet<unknown>(`/api/health/records${search}`));
}

/** 代填提交体：五个字段**始终出现**，跳过的那一问发 null（服务端据此判 missing 而不是「没收到」）。
 *
 * `complaint` 是 PRD §4.6 第 5 问（「今天身体有什么不舒服？」）的文本答案，随代填一起提交：
 * 它走单独的参数而不是五字段表（分级引擎只认五个数值键），服务层据它把等级抬到不低于黄。
 */
export interface HealthRecordInput {
  elder_id: string;
  systolic: number | null;
  diastolic: number | null;
  heart_rate: number | null;
  sleep_hours: number | null;
  mood_score: number | null;
  source: StaffHealthSource;
  note?: string;
  complaint?: string;
}

/**
 * 代填响应就是记录体本身（backend-health 已锁定口径）：`record_id / grade / abnormal_reasons`
 * 直接平铺在顶层，**没有** `payload` 包装层。用 Partial 兜住「只回核心列」的返回。
 */
export type HealthSubmitResult = Pick<HealthRecordItem, "record_id" | "grade"> &
  Partial<HealthRecordItem>;

/** 代填（FR-HLT-06）：grade 由服务端分级引擎给出，页面只负责展示徽标。 */
export function submitHealthRecord(input: HealthRecordInput): Promise<HealthSubmitResult> {
  const body: Record<string, unknown> = {
    elder_id: input.elder_id,
    systolic: input.systolic,
    diastolic: input.diastolic,
    heart_rate: input.heart_rate,
    sleep_hours: input.sleep_hours,
    mood_score: input.mood_score,
    source: input.source,
  };
  const note = input.note?.trim();
  if (note) body.note = note;
  const complaint = input.complaint?.trim();
  if (complaint) body.complaint = complaint;
  return apiSend<HealthSubmitResult>("/api/health/records", "POST", body);
}

/** 补录（FR-HLT-04）：追加新版本行，只把「要改的字段」放进去，原值不动。 */
export interface HealthSupplementInput {
  systolic?: number | null;
  diastolic?: number | null;
  heart_rate?: number | null;
  sleep_hours?: number | null;
  mood_score?: number | null;
  note?: string;
}

export function supersedeHealthRecord(
  recordId: string,
  input: HealthSupplementInput,
): Promise<HealthRecordItem | void> {
  const body: Record<string, unknown> = {};
  for (const key of ["systolic", "diastolic", "heart_rate", "sleep_hours", "mood_score"] as const) {
    const value = input[key];
    if (value !== undefined) body[key] = value;
  }
  const note = input.note?.trim();
  if (note) body.note = note;
  return apiSend<HealthRecordItem | void>(
    `/api/health/records/${encodeURIComponent(recordId)}/supersede`,
    "POST",
    body,
  );
}

/**
 * 撤销一条健康记录（FR-HLT-04，医生/管理员）：服务端软删，只写 `revoked_at` /
 * `revoked_by`，行与审计都保留；日报与趋势从此不再计入。已撤销或已被补录顶替回 409。
 */
export function revokeHealthRecord(recordId: string): Promise<HealthRecordItem> {
  return apiSend<HealthRecordItem>(
    `/api/health/records/${encodeURIComponent(recordId)}/revoke`,
    "POST",
  );
}

/** 趋势点：契约给短名，后端若回长名也在 `normalizeTrend` 里兜住。 */
export interface HealthTrendPoint {
  date: string;
  systolic: number | null;
  diastolic: number | null;
  sleep_hours: number | null;
  grade: string;
}

function normalizeTrend(raw: unknown): HealthTrendPoint[] {
  const list = Array.isArray(raw)
    ? raw
    : Array.isArray((raw as { items?: unknown } | null)?.items)
      ? ((raw as { items: unknown[] }).items)
      : [];
  return list
    .map((item) => {
      const row = (item ?? {}) as Record<string, unknown>;
      const date = typeof row.date === "string" ? row.date : typeof row.record_date === "string" ? row.record_date : "";
      return {
        date,
        systolic: pickNumber(row.systolic, row.systolic_mmhg),
        diastolic: pickNumber(row.diastolic, row.diastolic_mmhg),
        sleep_hours: finite(row.sleep_hours),
        grade: typeof row.grade === "string" ? row.grade : "",
      };
    })
    .filter((point) => point.date !== "");
}

/** 近 N 天趋势（默认 14）。空数组由页面渲染空态，不在这里造数据。 */
export async function fetchHealthTrend(elderId: string, days = TREND_DAYS): Promise<HealthTrendPoint[]> {
  const raw = await apiGet<unknown>(
    `/api/health/records/${encodeURIComponent(elderId)}${query({ days })}`,
  );
  return normalizeTrend(raw);
}

/** 复核闭环：把 `health_review` 通知置为已处理（服务端负责状态机与审计）。 */
export function resolveHealthReview(notificationId: string): Promise<void> {
  // body 必须显式给：服务端 `resolve_review` 的 payload 参数是必填（app/api/health.py:432），
  // 空 body 会被 FastAPI 直接判 422（`loc: ["body"]`）——「标记已处理」此前从未成功过。
  // `decision` 省略即服务端默认 acknowledged，这里写明是为了让请求体自解释。
  return apiSend<void>(`/api/health/review/${encodeURIComponent(notificationId)}/resolve`, "POST", {
    decision: "acknowledged",
  });
}
