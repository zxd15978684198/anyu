/**
 * 照护台「长者记忆」数据层（task-40 / FR-MEM-02）。
 *
 * 契约唯一依据：`docs/接口契约_记忆.md` v1.1 §5（M1/M2/M3，**admin only**）。
 * 本文件只做三件事：① 三个端点的请求封装（复用 `./http`，不另造第二套 fetch 封装）；
 * ② 契约口径的**文案与判定**（total 语义、删除幂等口径、错误文案）；③ 便于 node 用例覆盖的纯函数。
 *
 * 三条不能写错的口径：
 * 1. `total` 是**当前页条数**，不是全量计数（契约 M1 §3.3）。所以本文件不提供「算页数」的辅助函数，
 *    只提供 `mayHaveNextPage`（`本页条数 == limit` 才可能有下一页）；页面也不许出现「共 N 条」。
 * 2. `DELETE` 是**软删除 + 非幂等操作**（契约 M3 §7.3）：第二次调用仍 200 但 `deleted_chunks=0`
 *    且**再写一条审计**。所以 UI 必须把「删掉了 N 条」与「本来就没有」显示成两句不同的话。
 * 3. `source` **不落库**（契约 M2）：`inferred` 按 0.6 折算进 `importance`，页面只能靠 `previewImportance`
 *    把「这次折算后会落成多少」提前告诉操作者，不能事后从接口反推来源。
 */

import { apiGet, apiSend, HttpError, errorText, query } from "./http";
import { formatDateTimeFull } from "../shared/format";

/** 每页条数。契约 M1 的 `limit` 上限是 200，这里固定 20（照护台一屏不看 200 条记忆）。 */
export const MEMORY_PAGE_SIZE = 20;

/** 正文长度上限（契约 M2：1–200 字，超长**拒收**不截断）。前端只做提示，不替用户裁剪。 */
export const MEMORY_CONTENT_MAX = 200;

/** 软删除的默认审计原因（契约 M3 的 `reason` 默认值）。 */
export const MEMORY_DELETE_REASON = "checkout";

/** 来源系数（契约 M2 / 讲义 §8.3）：`inferred` 的权重必须低于长者亲口说的 `explicit`。 */
export const MEMORY_SOURCE_COEFFICIENT: Record<MemorySource, number> = {
  explicit: 1,
  inferred: 0.6,
};

/** 页面顶部/工具栏里对 `total` 的唯一解释，禁止改写成「共 N 条」。 */
export const PAGE_TOTAL_HINT = "本页条数即 total（契约 M1：统计的是当前页，不是全部记忆）";

/** 来源选择旁的说明，必须让人看懂「推断的记忆权重更低」。 */
export const SOURCE_HINT = "推断出来的记忆权重更低：inferred 按 0.6 折算后落库；列表里的重要度是折算后的值。";

export type MemoryStatus = "active" | "superseded" | "deleted";
export type MemoryCategory = "private" | "family";
export type MemorySource = "explicit" | "inferred";

/** M1 单条（契约 M1：**不返回 embedding**）。 */
export interface MemoryChunk {
  chunk_id: string;
  elder_id: string;
  content: string;
  category: MemoryCategory | string;
  importance: number;
  status: MemoryStatus | string;
  created_at: string | null;
  last_accessed: string | null;
}

/** M1 列表响应。`total` 是当前页条数，别拿它算页数。 */
export interface MemoryChunkList {
  items: MemoryChunk[];
  total: number;
  elder_id: string;
  limit: number;
  offset: number;
  status: MemoryStatus | string | null;
}

/** M2 写入响应（比 M1 单条多两个「向量从哪来」的字段）。 */
export interface MemoryChunkWrite extends MemoryChunk {
  embedding_provider: string;
  semantic_embedding: boolean;
}

/** M3 软删除响应。`deleted_chunks=0` 是「本来就没有」，不是失败。 */
export interface MemoryDeleteResult {
  elder_id: string;
  deleted_chunks: number;
  reason: string;
  audit_logged: boolean;
  soft_delete: boolean;
  trace_id: string | null;
}

// ---------------------------------------------------------------------------
// 端点封装（M1 / M2 / M3）
// ---------------------------------------------------------------------------

export interface ListMemoryChunksParams {
  elderId: string;
  status?: MemoryStatus | "";
  limit?: number;
  offset?: number;
}

/** M1：`GET /api/memory/chunks?elder_id=&status=&limit=&offset=`（admin only）。 */
export function listMemoryChunks(params: ListMemoryChunksParams): Promise<MemoryChunkList> {
  return apiGet<MemoryChunkList>(
    `/api/memory/chunks${query({
      elder_id: params.elderId,
      status: params.status,
      limit: params.limit ?? MEMORY_PAGE_SIZE,
      offset: params.offset ?? 0,
    })}`,
  );
}

export interface CreateMemoryChunkParams {
  elderId: string;
  content: string;
  category?: MemoryCategory;
  importance?: number;
  source?: MemorySource;
}

/** M2：`POST /api/memory/chunks?elder_id=`（admin only；201 返回落库后的单条）。 */
export function createMemoryChunk(params: CreateMemoryChunkParams): Promise<MemoryChunkWrite> {
  return apiSend<MemoryChunkWrite>(
    `/api/memory/chunks${query({ elder_id: params.elderId })}`,
    "POST",
    {
      content: params.content,
      category: params.category ?? "private",
      importance: params.importance ?? 1,
      source: params.source ?? "explicit",
    },
  );
}

export interface DeleteElderMemoryParams {
  elderId: string;
  reason?: string;
}

/** M3：`DELETE /api/memory/elder/{elder_id}?reason=`（admin only；软删除，无请求体）。 */
export function deleteElderMemory(params: DeleteElderMemoryParams): Promise<MemoryDeleteResult> {
  return apiSend<MemoryDeleteResult>(
    `/api/memory/elder/${params.elderId}${query({ reason: params.reason ?? MEMORY_DELETE_REASON })}`,
    "DELETE",
  );
}

// ---------------------------------------------------------------------------
// 选项与标签
// ---------------------------------------------------------------------------

export const STATUS_OPTIONS: { value: MemoryStatus | ""; label: string }[] = [
  { value: "", label: "全部状态" },
  { value: "active", label: "生效中" },
  { value: "superseded", label: "已被新记忆覆盖" },
  { value: "deleted", label: "已软删除" },
];

export const CATEGORY_OPTIONS: { value: MemoryCategory; label: string }[] = [
  { value: "private", label: "私人（仅长者本人与照护方）" },
  { value: "family", label: "家庭" },
];

export const SOURCE_OPTIONS: { value: MemorySource; label: string }[] = [
  { value: "explicit", label: "明确陈述（长者亲口说 / 照护者确认）" },
  { value: "inferred", label: "行为推断（折算系数 0.6）" },
];

export function statusLabel(status: MemoryStatus | string): string {
  if (status === "active") return "生效中";
  if (status === "superseded") return "已被覆盖";
  if (status === "deleted") return "已软删除";
  return String(status);
}

export function statusTone(status: MemoryStatus | string): "neutral" | "info" | "success" | "warning" | "danger" {
  if (status === "active") return "success";
  if (status === "superseded") return "warning";
  if (status === "deleted") return "neutral";
  return "neutral";
}

export function categoryLabel(category: MemoryCategory | string): string {
  if (category === "family") return "家庭";
  if (category === "private") return "私人";
  return String(category);
}

export function sourceLabel(source: MemorySource | string): string {
  if (source === "inferred") return "行为推断";
  if (source === "explicit") return "明确陈述";
  return String(source);
}

// ---------------------------------------------------------------------------
// 纯函数：列表口径、删除口径、错误文案、表单校验
// ---------------------------------------------------------------------------

/**
 * 列表此刻该显示什么。**500 / 403 / 404 一律是 `error`，不是 `empty`**——
 * 「把请求失败画成暂无数据」正是本卡要堵的那条坑。
 */
export function listStateOf(state: {
  loading: boolean;
  error: string | null;
  count: number;
}): "loading" | "error" | "empty" | "ready" {
  if (state.loading) return "loading";
  if (state.error) return "error";
  if (state.count === 0) return "empty";
  return "ready";
}

/**
 * 有没有下一页。**唯一依据是「本页条数 == limit」**：
 * `total` 只是当前页条数，拿 `offset/total` 或 `total/limit` 算页数都会错（契约 M1 §3.3）。
 */
export function mayHaveNextPage(count: number, limit: number = MEMORY_PAGE_SIZE): boolean {
  return count > 0 && count === limit;
}

/** 「第 a–b 条」；空页单独一句话，不写「第 1–0 条」。 */
export function pageRangeText(offset: number, count: number): string {
  if (count <= 0) return "本页没有记录";
  return `第 ${offset + 1}–${offset + count} 条`;
}

/** 翻页提示：本页满了才说「可能还有下一页」。 */
export function pageNote(count: number, limit: number = MEMORY_PAGE_SIZE): string {
  if (count <= 0) return "没有符合条件的记忆。";
  if (mayHaveNextPage(count, limit)) return `本页已满 ${limit} 条，可能还有下一页。`;
  return "已经是最后一页。";
}

/**
 * 删除结果文案。`deleted_chunks > 0` 与 `= 0` **必须是两句不同的话**：
 * 后者不代表失败，它代表「本来就没有生效中的记忆」，而且这次调用仍然写了一条审计（契约 M3 §7.3）。
 */
export function deleteOutcomeText(deletedChunks: number): string {
  if (deletedChunks > 0) {
    return `已软删除 ${deletedChunks} 条生效记忆（物理记录保留，可审计追溯）。`;
  }
  return "没有可删除的生效记忆：该长者本来就没有生效中的记忆。本次调用仍写入了一条 deleted_chunks=0 的审计。";
}

/** 错误文案：把契约 §8 的 403/404/422/5xx 分别说清，且明说 5xx 不是「暂无数据」。 */
export function memoryErrorText(err: unknown): string {
  if (err instanceof HttpError) {
    if (err.status === 403) {
      return `${errorText(err)}。长者记忆的查看、纠正与删除仅对管理员开放（接口契约 M1–M3 admin only）。`;
    }
    if (err.status === 404) {
      return `${errorText(err)}。该长者可能已不在档案里，请回到长者档案页确认。`;
    }
    if (err.status === 422) {
      return `${errorText(err)}。请检查正文是否在 1–${MEMORY_CONTENT_MAX} 字、重要度是否在 0–1 之间。`;
    }
    if (err.status >= 500) {
      return `服务端出错了（HTTP ${err.status}）：这不是「暂无记忆」，列表没有加载出来，请稍后重试。`;
    }
  }
  return errorText(err);
}

/** 表单校验：返回第一条错误文案，`null` 表示可以提交。**超长直接拦下，不截断**。 */
export function validateChunkDraft(draft: { content: string; importance: string | number }): string | null {
  const content = draft.content.trim();
  if (!content) return "请填写记忆正文。";
  if (content.length > MEMORY_CONTENT_MAX) {
    return `正文最多 ${MEMORY_CONTENT_MAX} 字（当前 ${content.length} 字），请拆分后分条写入。`;
  }
  const importance = Number(draft.importance);
  if (!Number.isFinite(importance)) return "重要度请填 0 到 1 之间的数字。";
  if (importance < 0 || importance > 1) return "重要度需在 0 到 1 之间（这是折算前的基础值）。";
  return null;
}

/**
 * 折算后的落库重要度（契约 M2：`importance × 系数`，夹到 [0,1]，两位小数）。
 * 只用于「提交前先告诉操作者会落成多少」，不替代服务端计算。
 */
export function previewImportance(base: number, source: MemorySource): string {
  if (!Number.isFinite(base)) return "—";
  const coefficient = MEMORY_SOURCE_COEFFICIENT[source];
  const weighted = Math.min(1, Math.max(0, base * coefficient));
  return weighted.toFixed(2);
}

/** 重要度展示：落库值是两位小数，统一成同样宽度，避免一列参差不齐。 */
export function importanceText(value: number): string {
  if (!Number.isFinite(value)) return "—";
  return value.toFixed(2);
}

/** 表格里的正文预览：多行记忆只显示首段，完整内容放 `title` 悬浮看。 */
export function contentPreview(content: string, max = 48): string {
  const flat = content.replace(/\s+/g, " ").trim();
  if (flat.length <= max) return flat;
  return `${flat.slice(0, max)}…`;
}

/** 时间展示（存储是 UTC，展示固定 **Asia/Shanghai**；空值给破折号而不是空串）。 */
export function formatTimestamp(value: string | null | undefined): string {
  // 收敛到 shared/format（AGENTS §8）。原实现走浏览器本地时区。
  return formatDateTimeFull(value);
}
