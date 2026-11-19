/**
 * 操作日志读取与导出（FR-ADM-05，2026-11-12）。
 *
 * 后端在 `app/api/audit_export.py`：
 * - `GET /api/audit/logs` → `{items, total, limit, offset}`，项字段见 `AuditLogItem`；
 * - `GET /api/audit/logs/export` → NDJSON（`application/x-ndjson`），带
 *   `Content-Disposition: attachment; filename="audit_logs_<UTC>Z.ndjson"`，最多 `MAX_EXPORT_ROWS` 行。
 *
 * 两个端点都是 **admin only**：非管理员拿 403，且服务端会先把这次越权访问写进审计
 * （`_require_admin` 里「先留痕再抛错」，见 `app/api/audit_export.py:140-173`）。
 * 所以本模块不做本地权限兜底——403 原样上抛，由页面解释成一句人话。
 *
 * ## 为什么导出走 `authedFetch` 而不是 `apiGet`
 * `apiGet` 会把响应体当 JSON 解析，而导出是文件流。这里只借 `authedFetch` 的
 * 「带 Bearer + 401 自动刷新重放一次」，自己处理 blob 与文件名。
 */
import { authedFetch } from "./api";
import { HttpError, NETWORK_TEXT, apiGet, query } from "./http";
import { formatDateTimeFull } from "../shared/format";

/** 一条审计记录的公开投影（键集合与后端 `AuditLogOut` 一致）。 */
export interface AuditLogItem {
  audit_id: string;
  actor_type: string;
  actor_id: string;
  action: string;
  resource_type: string;
  resource_id: string;
  /** `trace_id` 在表里没有独立列，由服务层从 `detail` 提升上来；老数据为 null，但键一定存在。 */
  detail: Record<string, unknown> | null;
  trace_id: string | null;
  created_at: string;
}

/** 分页外壳（`items` + 服务端回显的 `limit/offset`）。 */
export interface AuditLogList {
  items: AuditLogItem[];
  total: number;
  /** 本次「服务端实际生效」的条数，不是请求里那个。 */
  limit: number;
  offset: number;
}

/** 页面上的筛选条件（空串一律视为不筛，由 `buildParams` 丢掉）。 */
export interface AuditFilters {
  actorType: string;
  action: string;
  actorId: string;
  /** `datetime-local` 的原值（本地时区无偏移），提交前转 ISO 8601。 */
  start: string;
  end: string;
}

/** `app/services/audit_service.py:81` */
export const AUDIT_DEFAULT_LIMIT = 50;
/** `app/services/audit_service.py:84` —— 请求超过会被 422，故前端下拉不越过它。 */
export const AUDIT_MAX_LIMIT = 200;
/** `app/services/audit_service.py:87` —— 导出上限，页面要提前告诉人「最多这么多行」。 */
export const AUDIT_MAX_EXPORT_ROWS = 1000;

/** 与 `audit_logs.actor_type` 的 CHECK 取值一致（`app/api/audit_export.py:81`）。 */
export const AUDIT_ACTOR_TYPES = [
  { value: "", label: "全部主体" },
  { value: "staff", label: "员工" },
  { value: "guardian", label: "家属" },
  { value: "system", label: "系统" },
  { value: "device", label: "设备" },
] as const;

const EMPTY_FILTERS: AuditFilters = {
  actorType: "",
  action: "",
  actorId: "",
  start: "",
  end: "",
};

export function emptyFilters(): AuditFilters {
  return { ...EMPTY_FILTERS };
}

/**
 * `datetime-local` 的 `2026-11-12T14:05` → ISO 8601。
 *
 * 直接回传本地朴素时间会让服务端按 UTC 解释，边界差 8 小时；统一用 `Date` 转一次，
 * 拿到的就是带 `Z` 的绝对时刻。解析失败则原样回传（服务端 422 会把话说清楚）。
 */
function toIso(value: string): string {
  if (!value) return "";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toISOString();
}

function buildParams(filters: AuditFilters): Record<string, string> {
  return {
    actor_type: filters.actorType,
    action: filters.action.trim(),
    actor_id: filters.actorId.trim(),
    start: toIso(filters.start),
    end: toIso(filters.end),
  };
}

/** 一页审计日志。`limit` 会被服务端夹到 `MAX_LIMIT`，所以回显值以响应为准。 */
export async function listAuditLogs(
  filters: AuditFilters,
  page: { limit?: number; offset?: number } = {},
): Promise<AuditLogList> {
  const params = buildParams(filters);
  return apiGet<AuditLogList>(
    `/api/audit/logs${query({
      ...params,
      limit: page.limit ?? AUDIT_DEFAULT_LIMIT,
      offset: page.offset ?? 0,
    })}`,
  );
}

/** 导出结果：文件名以服务端 `Content-Disposition` 为准，缺了就按同一命名规则兜底。 */
export interface AuditExport {
  blob: Blob;
  filename: string;
}

function filenameFrom(res: Response): string {
  const disposition = res.headers.get("content-disposition") ?? "";
  const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(disposition);
  if (match?.[1]) return match[1];
  const stamp = new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z");
  return `audit_logs_${stamp}.ndjson`;
}

/** 把导出失败翻译成带 `code` 的 `HttpError`（与 `http.ts` 的码表同口径）。 */
function exportFailure(res: Response): HttpError {
  const code =
    res.status === 401
      ? "TOKEN_INVALID"
      : res.status === 403
        ? "ROLE_FORBIDDEN"
        : res.status === 422
          ? "VALIDATION_ERROR"
          : res.status >= 500
            ? "INTERNAL_ERROR"
            : "BAD_REQUEST";
  return new HttpError(res.status, code, res.statusText || NETWORK_TEXT);
}

/**
 * 按当前筛选条件导出 NDJSON 文件。
 *
 * 服务端最多给 `MAX_EXPORT_ROWS` 行——**不是**「这次筛选一共多少行」，
 * 页面必须把这句话说明白，否则人会以为导出完整。
 */
export async function exportAuditLogs(filters: AuditFilters): Promise<AuditExport> {
  let res: Response;
  try {
    res = await authedFetch(`/api/audit/logs/export${query(buildParams(filters))}`, {
      method: "GET",
      headers: { Accept: "application/x-ndjson" },
    });
  } catch {
    throw new HttpError(0, "NETWORK", NETWORK_TEXT);
  }
  if (!res.ok) throw exportFailure(res);
  return { blob: await res.blob(), filename: filenameFrom(res) };
}

/** 触发浏览器下载。放这里而不是页面里，是为了让「导出」只有一条实现路径。 */
export function saveAuditExport({ blob, filename }: AuditExport): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  // 立刻 revoke 在部分浏览器会取消尚未开始的下载，推迟一拍更稳。
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** 主体种类 → 中文（未知取值原样显示，方便发现后端新增了枚举）。 */
export function actorTypeLabel(value: string): string {
  return AUDIT_ACTOR_TYPES.find((item) => item.value === value)?.label ?? value;
}

/** `detail` 压成一行摘要：审计页是扫读场景，不展开 JSON。 */
export function detailSummary(detail: Record<string, unknown> | null): string {
  if (!detail || Object.keys(detail).length === 0) return "—";
  const text = JSON.stringify(detail);
  return text.length > 120 ? `${text.slice(0, 120)}…` : text;
}

/** 时间统一按 **Asia/Shanghai** 展示（存储是 UTC）。收敛到 shared/format，不随浏览器时区漂移。 */
export function formatAuditTime(value: string): string {
  // 收敛到 shared/format（AGENTS §8：展示一律 Asia/Shanghai）。
  // 原实现走 toLocaleString 即浏览器本地时区，护工机器时区一变时间就漂。
  return formatDateTimeFull(value);
}

/** 主键太长，表格里只留前 8 位；完整值放 `title`，悬停可看。 */
export function shortId(value: string): string {
  return value.length > 8 ? value.slice(0, 8) : value;
}
