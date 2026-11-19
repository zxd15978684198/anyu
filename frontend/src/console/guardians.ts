/**
 * 家属与绑定页的数据层（W3）。
 *
 * 契约唯一依据：
 * - 写：`app/api/guardians.py`（`POST /api/guardians`、`POST /api/elders/{elder_id}/guardians`）
 * - 读：`app/services/staff_service.py::list_guardians`、`app/services/elder_service.py::list_elder_guardians`
 *   （W4 的 `GET /api/guardians` 与 `GET /api/elders/{elder_id}/guardians` 由 error-contract 并行实现）
 *
 * 与 `staff.ts` 同构：请求层只做「拼路径 / 取数 / 收窄形状」，页面只管渲染；
 * 「长者下拉」「读接口未就绪」「分页信封」三段共用逻辑在 `staff.ts`（原因见该文件头）。
 *
 * ## 两个形状差异（已在页面上做了容忍，不改后端）
 * 1. 任务卡约定 `GET /api/guardians` 返回 `created_at`，但 `guardian_accounts` 表**没有这一列**
 *    （`staff_service.py::GuardianListItem` 注释写明），因此 `created_at` 按可选处理：
 *    有就显示，没有就显示「—」，不伪造创建时间。
 * 2. `GET /api/elders/{elder_id}/guardians` 信封只有 `{items,total}`，没有 `page/page_size`
 *    （不分页），收窄时按单页处理。
 */
import { HttpError, apiGet, apiSend, errorText, query } from "./http";
import { asString, readPage, type ListPage } from "./staff";

/** 家属列表一行（W4 约定形状；`created_at` 可能不存在，见文件头）。 */
export interface GuardianRow {
  guardian_id: string;
  name: string;
  phone_masked: string;
  status: string;
  created_at: string;
}

export interface GuardianListQuery {
  page: number;
  pageSize: number;
  /** 精确查询：给了就只返回手机号完全一致的那一条（服务端 `GuardianAccount.phone == phone`）。 */
  phone?: string;
}

export function guardianListPath(input: GuardianListQuery): string {
  return `/api/guardians${query({
    page: input.page,
    page_size: input.pageSize,
    phone: input.phone ?? "",
  })}`;
}

function toGuardianRow(raw: unknown): GuardianRow {
  const item = (raw ?? {}) as Record<string, unknown>;
  return {
    guardian_id: asString(item.guardian_id),
    name: asString(item.name),
    // 只认脱敏字段：后端若给 `phone` 原文，这里故意留空，由页面显示「—」。
    phone_masked: asString(item.phone_masked),
    status: asString(item.status),
    created_at: asString(item.created_at),
  };
}

/** `GET /api/guardians?page&page_size&phone` —— W4 并行实现中。 */
export async function listGuardians(input: GuardianListQuery): Promise<ListPage<GuardianRow>> {
  const raw = await apiGet<unknown>(guardianListPath(input));
  const page = readPage<unknown>(raw, { page: input.page, pageSize: input.pageSize });
  return { ...page, items: page.items.map(toGuardianRow) };
}

/** `POST /api/guardians` 的 201 响应（`GuardianCreateResponse`）。 */
export interface GuardianCreated {
  guardian_id: string;
  name: string;
  phone_masked: string;
  status: string;
}

export interface GuardianCreatePayload {
  name: string;
  phone: string;
  password: string;
}

/**
 * 建家属账号（契约缺口 G1 补齐的端点）。
 *
 * 手机号跨表唯一（与员工共用一张手机号空间）：重复时服务端 409 `CONFLICT`，
 * 页面把 `errorText` 落成「与现有数据冲突，请检查后重试」并**保留已填内容**。
 */
export async function createGuardian(payload: GuardianCreatePayload): Promise<GuardianCreated> {
  const raw = await apiSend<unknown>("/api/guardians", "POST", payload);
  const body = (raw ?? {}) as Record<string, unknown>;
  return {
    guardian_id: asString(body.guardian_id),
    name: asString(body.name),
    phone_masked: asString(body.phone_masked),
    status: asString(body.status),
  };
}

export interface GuardianBindPayload {
  guardian_id: string;
  /** 与长者关系：子 / 女 / 孙辈 / 配偶……（自由文本，服务端限 1–32 字）。 */
  relation: string;
}

export interface GuardianBindResult {
  elder_id: string;
  guardian_id: string;
  relation: string;
  status: string;
}

/** `POST /api/elders/{elder_id}/guardians`（E13）；每位长者 active 监护人上限 2 位，超限 409。 */
export async function bindGuardian(
  elderId: string,
  payload: GuardianBindPayload,
): Promise<GuardianBindResult> {
  const raw = await apiSend<unknown>(
    `/api/elders/${encodeURIComponent(elderId)}/guardians`,
    "POST",
    payload,
  );
  const body = (raw ?? {}) as Record<string, unknown>;
  return {
    elder_id: asString(body.elder_id),
    guardian_id: asString(body.guardian_id),
    relation: asString(body.relation),
    status: asString(body.status),
  };
}

/**
 * 某长者已绑家属的一行（`ElderGuardianItem`，只含 `status='active'` 的绑定）。
 *
 * `binding_id` 是绑定行主键（`elder_guardians.id`），`guardian_id` 是家属账号 ID：
 * 解绑要传的是前者，两者混用会删错对象，所以这里两个都留着且名字对齐后端字段。
 */
export interface ElderGuardianRow {
  guardian_id: string;
  binding_id: string;
  name: string;
  phone_masked: string;
  relation: string;
  bound_at: string;
}

/** `GET /api/elders/{elder_id}/guardians` —— 不分页，`total` 恒等于条目数。 */
export async function listElderGuardians(elderId: string): Promise<ListPage<ElderGuardianRow>> {
  const raw = await apiGet<unknown>(
    `/api/elders/${encodeURIComponent(elderId)}/guardians`,
  );
  const page = readPage<unknown>(raw, { page: 1, pageSize: 100 });
  return {
    ...page,
    items: page.items.map((item) => {
      const row = (item ?? {}) as Record<string, unknown>;
      return {
        guardian_id: asString(row.guardian_id),
        binding_id: asString(row.binding_id),
        name: asString(row.name),
        phone_masked: asString(row.phone_masked),
        relation: asString(row.relation),
        bound_at: asString(row.bound_at),
      };
    }),
  };
}

/**
 * 解绑某条家属绑定（`DELETE /api/bindings/{binding_id}`，admin）。
 *
 * 后端口径（契约 E17）：204 无响应体；绑定行置 `status='unbound'` 保痕，**不删家属账号**。
 * `reason` 可选、≤200 字，进审计；不传就不带 query，而不是传空串。
 */
export async function unbindGuardian(bindingId: string, reason?: string): Promise<void> {
  const trimmed = (reason ?? "").trim();
  await apiSend<void>(
    `/api/bindings/${encodeURIComponent(bindingId)}${query({ reason: trimmed })}`,
    "DELETE",
  );
}

/**
 * 解绑失败的文案。
 *
 * 不复用 `CODE_TEXT.CONFLICT`（那句「与现有数据冲突」在这里指向不明）：409 在本端点
 * 只有一个含义——这条绑定已经是 `unbound`（重复解绑），要点是「刷新看最新状态」。
 * 其余错误码回落到通用 `errorText`，不另造一套。
 */
export function unbindErrorText(err: unknown): string {
  if (err instanceof HttpError) {
    if (err.status === 403) return "当前角色无权解绑家属（仅管理员可操作）";
    if (err.status === 404) return "没有找到这条绑定记录，可能已被删除或链接已过期，请刷新列表";
    if (err.status === 409) return "这条绑定已经解绑过了，刷新列表即可看到最新状态";
  }
  return errorText(err);
}

/** 绑定关系常用取值（下拉快捷项，仍允许自由填写其它关系）。 */
export const RELATION_SUGGESTIONS: readonly string[] = [
  "儿子",
  "女儿",
  "儿媳",
  "女婿",
  "孙辈",
  "配偶",
  "兄弟姐妹",
  "其他亲属",
];

/** 家属账号状态文案（与员工共用 active / disabled 闭集）。 */
export function guardianStatusLabel(status: string): string {
  return status === "active" ? "启用" : status === "disabled" ? "停用" : status;
}
