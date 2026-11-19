/**
 * 员工账号页的数据层（W3）。
 *
 * 契约唯一依据：
 * - 写：`app/api/staff.py` + `app/api/schemas/staff.py`（`POST /api/staff`、`PATCH /api/staff/{id}`）
 * - 读：`app/services/staff_service.py::list_staff`（W4 的 `GET /api/staff` 由 error-contract 并行实现）
 * - 长者下拉：`app/api/elders.py::list_elders`（W2 已上线）
 *
 * 本文件只做「拼路径 / 取数 / 收窄形状 / 枚举文案」，不含 JSX——分页参数、角色闭集、
 * 脱敏字段都集中在这里，页面只管渲染。
 *
 * ## 两个必须守住的边界
 * 1. **读接口没上线 ≠ 没数据**：W4 还没挂载时 FastAPI 回 404/405。判定统一走
 *    `isEndpointMissing()`，页面据此显示「读接口未就绪」，不许退化成空态。
 * 2. **手机号只认 `phone_masked`**：服务层返回的是 `phone` 原文，由 API 层统一脱敏。
 *    若某次响应里只剩 `phone`，这里**故意不回退**——宁可显示「—」，也不把完整号码
 *    渲染到管理员屏幕上（AGENTS §4 / 契约 S-06）。
 *
 * ## 三页共用的小工具为什么放在这里
 * 写作用域只有 `staff.ts` / `guardians.ts` / `devices.ts` 三个模块，不新建第四个公共文件；
 * 于是「长者下拉」「读接口未就绪判定」「分页信封收窄」这三段三页都要用的代码落在本文件，
 * 由另外两个模块直接 import（本文件不反向 import 它们，避免环）。
 */
import { HttpError, apiGet, apiSend, query } from "./http";

// --------------------------------------------------------------- 三页共用

/** 列表端点尚未挂载：404（没有这条路由）/ 405（路径在但方法不对）。 */
export function isEndpointMissing(err: unknown): boolean {
  return err instanceof HttpError && (err.status === 404 || err.status === 405);
}

/** 响应与约定形状不符（例如缺 `items`）。与「空列表」是两件事：页面要如实报错，不许显示 0 条。 */
export class ShapeError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ShapeError";
  }
}

/** 服务端分页信封：W4 三个列表接口约定同构（`{items,total,page,page_size}`）。 */
export interface ListPage<T> {
  items: T[];
  total: number;
  page: number;
  pageSize: number;
}

export function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

export function asStringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

/**
 * 把 `{items,total,page,page_size}` 收窄成 `ListPage`。
 *
 * `items` 不是数组 → 抛 `ShapeError`：这说明后端形状和约定对不上，页面必须显式报错，
 * 而不是把「读不到」显示成「没有」。`total` / `page` / `page_size` 缺失时才用兜底值。
 */
export function readPage<T>(
  raw: unknown,
  fallback: { page: number; pageSize: number },
): ListPage<T> {
  const body = (raw ?? {}) as Record<string, unknown>;
  if (!Array.isArray(body.items)) {
    throw new ShapeError("列表响应缺少 items 数组，与 W4 约定形状不符");
  }
  const total =
    typeof body.total === "number" && Number.isFinite(body.total) ? body.total : body.items.length;
  const page = typeof body.page === "number" && body.page >= 1 ? body.page : fallback.page;
  const pageSize =
    typeof body.page_size === "number" && body.page_size >= 1 ? body.page_size : fallback.pageSize;
  return { items: body.items as T[], total, page, pageSize };
}

/** 长者下拉的一行（`app/api/elders.py::ElderListItemResponse`）。 */
export interface ElderOption {
  elder_id: string;
  name: string;
  room_no: string;
  bed_no: string;
  status: string;
}

/**
 * 长者下拉（三页共用）：`GET /api/elders?page_size=100`。
 *
 * 只取第一页 100 条——单个 CCRC 社区的在住长者规模够用；不做全量翻页，避免把下拉变成
 * 一次几十个请求的慢操作。超出时下拉底部会提示「仅列出前 100 位」。
 */
export async function listElderOptions(): Promise<ElderOption[]> {
  const raw = await apiGet<unknown>(`/api/elders${query({ page: 1, page_size: 100 })}`);
  const page = readPage<unknown>(raw, { page: 1, pageSize: 100 });
  return page.items.map((item) => {
    const row = (item ?? {}) as Record<string, unknown>;
    return {
      elder_id: asString(row.elder_id),
      name: asString(row.name),
      room_no: asString(row.room_no),
      bed_no: asString(row.bed_no),
      status: asString(row.status),
    };
  });
}

/** 长者下拉的显示文案：`张桂芬 · 302 房 1 床`（缺房间床位时不留空括号）。 */
export function elderOptionLabel(elder: ElderOption): string {
  const place = [elder.room_no ? `${elder.room_no} 房` : "", elder.bed_no ? `${elder.bed_no} 床` : ""]
    .filter(Boolean)
    .join(" ");
  return place ? `${elder.name} · ${place}` : elder.name;
}

// --------------------------------------------------------------- 枚举

export type StaffRole = "admin" | "care_worker" | "doctor";
export type StaffStatus = "active" | "disabled";

export const STAFF_ROLES: readonly StaffRole[] = ["admin", "care_worker", "doctor"];

const ROLE_LABEL: Record<string, string> = {
  admin: "管理员",
  care_worker: "护工",
  doctor: "医生",
};

export function staffRoleLabel(role: string): string {
  return ROLE_LABEL[role] ?? role;
}

export function staffStatusLabel(status: string): string {
  return status === "active" ? "启用" : status === "disabled" ? "停用" : status;
}

/** 下拉选项（`ui/Form.tsx::Select` 的入参形状）。 */
export const STAFF_ROLE_OPTIONS: { value: string; label: string }[] = STAFF_ROLES.map((role) => ({
  value: role,
  label: ROLE_LABEL[role] ?? role,
}));

export const STAFF_STATUS_OPTIONS: { value: string; label: string }[] = [
  { value: "active", label: "启用" },
  { value: "disabled", label: "停用" },
];

// --------------------------------------------------------------- 读：列表

/** 列表一行（W4 约定形状：`{staff_id,name,role,phone_masked,status,elder_ids}`）。 */
export interface StaffRow {
  staff_id: string;
  name: string;
  role: string;
  phone_masked: string;
  status: string;
  elder_ids: string[];
}

export interface StaffListQuery {
  page: number;
  pageSize: number;
  /** 空串 = 不筛选。 */
  role?: StaffRole | "";
}

/** 列表路径（单独导出便于页面/探针核对实际打出的 URL）。 */
export function staffListPath(input: StaffListQuery): string {
  return `/api/staff${query({
    page: input.page,
    page_size: input.pageSize,
    role: input.role ?? "",
  })}`;
}

function toStaffRow(raw: unknown): StaffRow {
  const item = (raw ?? {}) as Record<string, unknown>;
  return {
    staff_id: asString(item.staff_id),
    name: asString(item.name),
    role: asString(item.role),
    // 只认脱敏字段；后端若只给 phone 原文，这里故意留空由页面显示「—」。
    phone_masked: asString(item.phone_masked),
    status: asString(item.status),
    elder_ids: asStringList(item.elder_ids),
  };
}

/** `GET /api/staff?page&page_size&role` —— W4 并行实现中，未上线时抛 404，由页面判定。 */
export async function listStaff(input: StaffListQuery): Promise<ListPage<StaffRow>> {
  const raw = await apiGet<unknown>(staffListPath(input));
  const page = readPage<unknown>(raw, { page: input.page, pageSize: input.pageSize });
  return { ...page, items: page.items.map(toStaffRow) };
}

// --------------------------------------------------------------- 写：建号 / 改档

export interface StaffCreatePayload {
  name: string;
  role: StaffRole;
  phone: string;
  password: string;
  /** 管理员不绑长者，页面已保证这里为空数组。 */
  elder_ids: string[];
}

/** `POST /api/staff` 的 201 响应（`StaffResponse`）。 */
export interface StaffCreated {
  staff_id: string;
  name: string;
  role: string;
  phone_masked: string;
  status: string;
}

export async function createStaff(payload: StaffCreatePayload): Promise<StaffCreated> {
  const raw = await apiSend<unknown>("/api/staff", "POST", payload);
  const body = (raw ?? {}) as Record<string, unknown>;
  return {
    staff_id: asString(body.staff_id),
    name: asString(body.name),
    role: asString(body.role),
    phone_masked: asString(body.phone_masked),
    status: asString(body.status),
  };
}

/**
 * `PATCH /api/staff/{staff_id}` 的请求体。
 *
 * **只有这两个字段**：`app/api/schemas/staff.py::StaffUpdateRequest` 就只定义了
 * `role` / `status`（`extra` 未放开，多送字段会被 422）。任务卡里写的
 * `name?/phone?/password?/elder_ids?` 后端当前**不接受**——差异已报产品负责人，
 * 页面据此把姓名/手机号/负责长者做成只读展示，不制造「改了但没保存」的假象。
 */
export interface StaffPatch {
  role?: StaffRole;
  status?: StaffStatus;
}

/** 改档成功会立即让该账号旧 Token 失效（AC-24），页面要提示本人重新登录。 */
export async function updateStaff(staffId: string, patch: StaffPatch): Promise<void> {
  await apiSend<unknown>(`/api/staff/${encodeURIComponent(staffId)}`, "PATCH", patch);
}
