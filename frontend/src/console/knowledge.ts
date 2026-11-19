/**
 * 机构知识库数据层（K3 console 管理页，FR-KNW-02）。
 *
 * 契约：`app/api/knowledge.py` 的五个端点
 * 1. `GET  /api/knowledge/search?query=&category=&limit=`（admin / doctor / care_worker）
 * 2. `POST /api/knowledge/documents`（仅 admin，201）→ `KnowledgeDocumentOut`（含向量来源）
 * 3. `POST /api/knowledge/documents/{id}/supersede`（仅 admin，201）→ 同上
 * 4. `GET  /api/knowledge/documents?status=&category=&q=&limit=&offset=`（仅 admin）
 *    → `KnowledgeDocumentPageOut{items,total,limit,offset}`，**行里没有 `embedding_provider`**
 *    （正文与向量来源只在新建/换代的响应里，列表按需再看，别拿列表行去渲染向量提示）
 * 5. `POST /api/knowledge/documents/{id}/retire`（仅 admin，幂等）
 *    → `KnowledgeRetireOut{…, already_retired}`：重复停用返回 `already_retired=true`，
 *      不是错误，页面上要说成「这一版本来就是停用状态」而不是「停用成功」。
 *
 * 四条硬口径（改之前先读）：
 * 1. **类别/状态/权威级别的取值只有后端一处定义**（`app/models/knowledge.py` 的
 *    `KNOWLEDGE_CATEGORIES` / `KNOWLEDGE_STATUSES` / `KNOWLEDGE_AUTHORITY_LEVELS`），
 *    这里的中文标签只是展示层映射，不得自己发明「审核中」「已发布」这类词。
 * 2. **状态只有三值**：`draft`（草稿）/ `active`（生效）/ `retired`（已退役）；
 *    只有 `active` 且在生效区间内可被检索（FR-KNW-02 / AC-44）。
 * 3. **时间一律按 UTC 收**：服务层 `_as_utc()` 把无时区的时间当 UTC（`knowledge_service.py:202-204`），
 *    所以 `datetime-local` 拿到的手填时间必须先经 `toApiIso()` 折算成带 `Z` 的 ISO，否则
 *    东八区 08:00 会被存成 08:00Z（=本地 16:00），生效窗口整体偏 8 小时。
 * 4. **机构知识是平台侧配置**，不进长者记忆、不参与 90 天衰减、退房清数据不动本表
 *    （TD `:680`）；页面上的正文提示语就是这条口径的可见化。
 */
import { apiGet, apiSend, query } from "./http";
import { formatDateTimeFull } from "../shared/format";

/** `knowledge_documents.category` 六类（顺序与 `KNOWLEDGE_CATEGORIES` 一致）。 */
export type KnowledgeCategory = "schedule" | "meal" | "activity" | "policy" | "facility" | "service";

/** 三值状态，与后端 CHECK 逐字一致。 */
export type KnowledgeStatus = "draft" | "active" | "retired";

/** 权威级别：机构规定优先于「长者自述经管理员转录」（FR-KNW-02）。 */
export type KnowledgeAuthority = "institution" | "elder_memory";

/** 检索返回的 `reason`：区分「查了没有」与「这次查不了」。 */
export type KnowledgeSearchReason = "ok" | "no_match" | "empty_query" | "degraded";

export const CATEGORY_ORDER: readonly KnowledgeCategory[] = [
  "schedule",
  "meal",
  "activity",
  "policy",
  "facility",
  "service",
];

export const CATEGORY_LABEL: Record<KnowledgeCategory, string> = {
  schedule: "作息",
  meal: "餐饮",
  activity: "活动",
  policy: "制度",
  facility: "设施",
  service: "服务流程",
};

export const STATUS_ORDER: readonly KnowledgeStatus[] = ["draft", "active", "retired"];

export const STATUS_LABEL: Record<KnowledgeStatus, string> = {
  draft: "草稿",
  active: "生效",
  retired: "已退役",
};

/** 与 `ui/Feedback.tsx` 的 `Badge` 同一套 tone。 */
/** 语义色单一真源在 shared/tone（W4）。 */
import type { Tone as KnowledgeTone } from "../shared/tone";
export type { KnowledgeTone };

export const STATUS_TONE: Record<KnowledgeStatus, KnowledgeTone> = {
  draft: "neutral",
  active: "success",
  retired: "warning",
};

export const AUTHORITY_LABEL: Record<KnowledgeAuthority, string> = {
  institution: "机构规定",
  elder_memory: "长者自述（转录）",
};

export const REASON_TEXT: Record<KnowledgeSearchReason, string> = {
  ok: "命中",
  no_match: "库里没有匹配的条目",
  empty_query: "检索词为空",
  degraded: "本次无法语义检索（向量服务未配置），不是「库里没有」",
};

/**
 * 正文输入区的常驻提示（卡里要求的那句话）。
 * 放在数据层而不是页面里：测试与页面共用同一份字面量，改口径只改一处。
 */
export const BODY_HINT =
  "机构知识是平台侧配置：不进长者记忆、不参与衰减，退房清理数据时也不会删除。";

/** 换代提示：旧版立刻不可检索，但历史行仍可追溯（不是覆盖）。 */
export const SUPERSEDE_HINT =
  "换代不是覆盖：新版生效后旧版状态置「已退役」、生效终点收到新版起点，旧版立刻不再被检索，但正文仍留在库里可追溯。";

/** 只有管理员能进入本页（服务端 `WRITE_ROLES` 与 `GET /documents` 都只认 admin）。 */
export const ADMIN_ONLY_TITLE = "机构知识库仅供管理员维护";
export const ADMIN_ONLY_DESCRIPTION =
  "知识的录入与版本切换只开放给管理员：它决定机构口径（几点开饭、探视到几点），写错会直接落到长者面前。医生与护工可以在对话里检索知识，但不在本页修改。";

export const INSERT_HINT = "新建默认直接生效（`status=active`），生效起点缺省为当前时刻，留空即长期有效。";

// ------------------------------------------------------------------ 类型

export interface KnowledgeHit {
  chunk_id: string;
  document_id: string;
  chunk_index: number;
  title: string;
  category: string;
  authority_level: string;
  version: number;
  effective_from: string;
  effective_until: string | null;
  content: string;
  distance: number;
  similarity: number;
}

export interface KnowledgeSearchResult {
  hits: KnowledgeHit[];
  reason: string;
  degraded: boolean;
  limit: number;
}

/** 新建/换代/详情返回的完整文档视图（含向量来源与分块数）。 */
export interface KnowledgeDocument {
  document_id: string;
  title: string;
  category: string;
  version: number;
  authority_level: string;
  status: string;
  effective_from: string;
  effective_until: string | null;
  chunks: number;
  embedding_provider: string;
  semantic_embedding: boolean;
}

/**
 * 列表行（`KnowledgeDocumentSummaryOut`）：只有元数据与分块数，**没有**正文与向量来源。
 * 别把它当 `KnowledgeDocument` 用——缺的两个字段会让「语义 / 降级」提示渲染成 `undefined`。
 */
export interface KnowledgeDocumentSummary {
  document_id: string;
  title: string;
  category: string;
  version: number;
  authority_level: string;
  status: string;
  effective_from: string;
  effective_until: string | null;
  chunks: number;
}

/** 一页列表：`total` 由后端按同一组过滤条件单独 count，不是 `items.length`。 */
export interface KnowledgeDocumentPage {
  items: KnowledgeDocumentSummary[];
  total: number;
  limit: number;
  offset: number;
}

/** 停用结果：`already_retired=true` 表示这次是重复请求（幂等，不再写审计）。 */
export interface KnowledgeRetireResult {
  document_id: string;
  title: string;
  version: number;
  status: string;
  effective_until: string | null;
  already_retired: boolean;
}

export interface KnowledgeIngestInput {
  title: string;
  body: string;
  category: string;
  version: number;
  authority_level: string;
  effective_from?: string | null;
  effective_until?: string | null;
}

export interface KnowledgeSupersedeInput {
  body: string;
  version?: number | null;
  effective_from?: string | null;
  effective_until?: string | null;
}

/** 新建表单（页面上手填的原始值；时间还是 `datetime-local` 的本地字符串）。 */
export interface IngestForm {
  title: string;
  body: string;
  category: KnowledgeCategory;
  version: string;
  authority_level: KnowledgeAuthority;
  effective_from: string;
  effective_until: string;
}

export type FormErrors = Partial<Record<keyof IngestForm, string>>;

// ------------------------------------------------------------------ 展示映射

export function categoryLabel(value: string): string {
  return CATEGORY_LABEL[value as KnowledgeCategory] ?? value;
}

export function statusLabel(value: string): string {
  return STATUS_LABEL[value as KnowledgeStatus] ?? value;
}

export function statusTone(value: string): KnowledgeTone {
  return STATUS_TONE[value as KnowledgeStatus] ?? "neutral";
}

export function authorityLabel(value: string): string {
  return AUTHORITY_LABEL[value as KnowledgeAuthority] ?? value;
}

export function reasonText(value: string): string {
  return REASON_TEXT[value as KnowledgeSearchReason] ?? value;
}

/** 生效窗口的展示：`2026-11-01 00:00 起` / `… 至 2026-12-01 00:00`；终点空 = 长期有效。 */
export function effectiveWindowText(from?: string | null, until?: string | null): string {
  const start = formatDateTime(from);
  if (!until) return `${start} 起（长期有效）`;
  return `${start} 至 ${formatDateTime(until)}`;
}

/** 库里的时间（ISO/UTC）→ 东八区展示；解析失败原样返回，不吞掉数据。 */
export function formatDateTime(value?: string | null): string {
  // 收敛到 shared/format。本模块原先是三端里唯一显式指定 Asia/Shanghai 的，
  // 现在其余 11 份也统一到同一实现（AGENTS §8）。
  return formatDateTimeFull(value);
}

/**
 * `datetime-local` 的本地时间 → 带时区的 ISO（发给后端）。
 * 空值返回 `null`（＝后端缺省：起点=当前时刻、终点=长期有效）。
 * 走 `Date` 解析是刻意的：浏览器把无时区的 `YYYY-MM-DDTHH:mm` 按**本地时区**解释，
 * `.toISOString()` 再折算成 UTC，与服务层「naive 当 UTC」的口径正好对齐在「同一个瞬间」。
 */
export function toApiIso(localValue: string): string | null {
  const text = localValue.trim();
  if (!text) return null;
  const date = new Date(text);
  if (Number.isNaN(date.getTime())) return null;
  return date.toISOString();
}

/** ISO → `datetime-local` 的值（只用于把服务端时间回填进表单）。 */
export function toLocalInput(iso?: string | null): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const offset = date.getTimezoneOffset() * 60000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}

/** 表单可以提交吗：标题与正文非空、版本是正整数。 */
export function canSubmitForm(form: IngestForm, supersede = false): boolean {
  if (!form.body.trim()) return false;
  if (!supersede && !form.title.trim()) return false;
  if (!supersede && !/^[1-9]\d*$/.test(form.version.trim())) return false;
  return true;
}

/** 表单级校验：返回字段错误（空对象＝通过）。超长交给后端 422，不在这里猜上限。 */
export function validateForm(form: IngestForm, supersede = false): FormErrors {
  const errors: FormErrors = {};
  if (!supersede && !form.title.trim()) errors.title = "标题不能为空";
  if (!form.body.trim()) errors.body = "正文不能为空";
  if (!supersede && !/^[1-9]\d*$/.test(form.version.trim())) errors.version = "版本号必须是正整数";
  if (form.effective_from && form.effective_until) {
    const start = new Date(form.effective_from).getTime();
    const end = new Date(form.effective_until).getTime();
    if (Number.isFinite(start) && Number.isFinite(end) && end <= start) {
      errors.effective_until = "生效终点必须晚于起点";
    }
  }
  return errors;
}

export function emptyIngestForm(): IngestForm {
  return {
    title: "",
    body: "",
    category: "schedule",
    version: "1",
    authority_level: "institution",
    effective_from: "",
    effective_until: "",
  };
}

// ------------------------------------------------------------------ 端点

/** 语义检索（admin/doctor/care_worker 可读）：只返回 `active` 且生效中的分块。 */
export function searchKnowledge(
  query_text: string,
  options: { category?: string; limit?: number } = {},
): Promise<KnowledgeSearchResult> {
  return apiGet<KnowledgeSearchResult>(
    `/api/knowledge/search${query({
      query: query_text,
      category: options.category || undefined,
      limit: options.limit,
    })}`,
  );
}

/** 新建一篇知识（admin）：文档 + 切块 + 向量 + 审计同一事务，重复 `(title,version)` → 409。 */
export function createDocument(input: KnowledgeIngestInput): Promise<KnowledgeDocument> {
  return apiSend<KnowledgeDocument>("/api/knowledge/documents", "POST", input);
}

/** 换代（admin）：新版生效、旧版退役，旧版立刻不可检索。 */
export function supersedeDocument(
  documentId: string,
  input: KnowledgeSupersedeInput,
): Promise<KnowledgeDocument> {
  return apiSend<KnowledgeDocument>(
    `/api/knowledge/documents/${encodeURIComponent(documentId)}/supersede`,
    "POST",
    input,
  );
}

/**
 * 列表（admin）：含 `draft` / `active` / `retired` 全量（检索侧只取 active，管理页要看得见退役行）。
 * 参数留空＝不过滤；`q` 只按标题子串匹配，不查正文。
 */
export async function listDocuments(
  options: { status?: string; category?: string; q?: string; limit?: number; offset?: number } = {},
): Promise<KnowledgeDocumentPage> {
  const raw = await apiGet<Partial<KnowledgeDocumentPage> & { items?: unknown }>(
    `/api/knowledge/documents${query({
      status: options.status || undefined,
      category: options.category || undefined,
      q: options.q || undefined,
      limit: options.limit,
      offset: options.offset,
    })}`,
  );
  const items = Array.isArray(raw?.items) ? (raw.items as KnowledgeDocumentSummary[]) : [];
  return {
    items,
    total: typeof raw?.total === "number" ? raw.total : items.length,
    limit: typeof raw?.limit === "number" ? raw.limit : items.length,
    offset: typeof raw?.offset === "number" ? raw.offset : 0,
  };
}

/**
 * 停用（admin）：置 `retired` 并把生效终点收到当前时刻；检索立刻不再命中它。
 * **幂等**：重复调用返回 `already_retired=true`，不报错、不再写审计、不推进封口时刻。
 */
export function retireDocument(documentId: string): Promise<KnowledgeRetireResult> {
  return apiSend<KnowledgeRetireResult>(
    `/api/knowledge/documents/${encodeURIComponent(documentId)}/retire`,
    "POST",
  );
}
