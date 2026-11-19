/**
 * 设备绑定页的数据层（W3）。
 *
 * 契约唯一依据：`app/api/devices.py`（E9–E11 原有；E13/E14 于 2026-11-11 补齐）
 * 与 `app/services/device_service.py`。
 *
 * ## 为什么绑定要先拉「候选长者」
 * `DeviceBindRequest` 是 `{room_id, elder_id}` 且 `extra="forbid"` —— 缺 `room_id` 直接 422。
 * 但列表项（`DeviceListItem`）**不含 `room_id`、也不含 `device_code`**
 * （`devices` 表压根没有 device_code 列，`设计记录（音频传输与设备链路技术方案）`
 * v1.3 已裁决「device_code 彻底出局」）。E11 的候选接口正好回 `{room_id, mode, candidates}`，
 * 于是绑定弹窗先打 E11 拿到 `room_id` 与本房间的合法候选（单人间 1 位 / 双人间 2 位），
 * 再按 `{room_id, elder_id}` 提交——既绕开缺字段，也顺带拦住「把 302 房的爷爷绑到 401 房的设备」。
 *
 * ## 2026-11-11 补齐设备生命周期
 * 此前只能解绑不能新建、解绑后凭证丢了也没地方重发。现在补上 E13（重发凭证）与
 * E14（新建设备），两者都**只在响应里返回一次** `device_secret`，页面必须当场展示并提醒
 * 「刷新后取不回」。
 */
import { apiGet, apiSend, query } from "./http";
import { asString, readPage, type ListPage } from "./staff";
import { formatInstant } from "../shared/format";

/**
 * 设备列表一行（W4 约定形状里 `device_code` 实际不存在，见文件头）。
 *
 * `device_code` 保留为可选字段仅用于兼容老响应的解析，页面**不再展示它**：
 * 后端从未有过这一列，展示一个恒空的字段只会让人怀疑数据丢了。
 */
export interface DeviceRow {
  device_id: string;
  device_code?: string;
  elder_id: string;
  elder_name: string;
  room_no: string;
  bed_no: string;
  bound: boolean;
  last_seen_at: string;
}

export interface DeviceListQuery {
  page: number;
  pageSize: number;
  /** `""` = 全部；`"true"` / `"false"` 对应服务端 `bound: bool | None`。 */
  bound?: "" | "true" | "false";
}

export function deviceListPath(input: DeviceListQuery): string {
  return `/api/devices${query({
    page: input.page,
    page_size: input.pageSize,
    bound: input.bound ?? "",
  })}`;
}

function toDeviceRow(raw: unknown): DeviceRow {
  const item = (raw ?? {}) as Record<string, unknown>;
  return {
    device_id: asString(item.device_id),
    elder_id: asString(item.elder_id),
    elder_name: asString(item.elder_name),
    room_no: asString(item.room_no),
    bed_no: asString(item.bed_no),
    // `bound` 是服务端派生的布尔值；缺失时按「未绑定」渲染会骗人，故只在严格 true 时为真。
    bound: item.bound === true,
    last_seen_at: asString(item.last_seen_at),
  };
}

/** `GET /api/devices?page&page_size&bound`。 */
export async function listDevices(input: DeviceListQuery): Promise<ListPage<DeviceRow>> {
  const raw = await apiGet<unknown>(deviceListPath(input));
  const page = readPage<unknown>(raw, { page: input.page, pageSize: input.pageSize });
  return { ...page, items: page.items.map(toDeviceRow) };
}

/** E14 新建设备的结果：`device_secret` 只出现这一次。 */
export interface CreatedDevice {
  device_id: string;
  room_id: string;
  elder_id: string;
  device_secret: string;
  status: string;
}

/**
 * `POST /api/devices`（E14）：新设备一律 `offline`，凭证只回显一次。
 *
 * 不在这里做「新建后顺便绑定」：服务端已支持 `elder_id`，但绑定要走 E9 的
 * 房间-床位一致性校验；让新建只管建，绑定复用既有弹窗，责任更清楚。
 */
export async function createDevice(payload: {
  room_id: string;
  elder_id?: string | null;
}): Promise<CreatedDevice> {
  const raw = await apiSend<unknown>("/api/devices", "POST", payload);
  const body = (raw ?? {}) as Record<string, unknown>;
  return {
    device_id: asString(body.device_id),
    room_id: asString(body.room_id),
    elder_id: asString(body.elder_id),
    device_secret: asString(body.device_secret),
    status: asString(body.status),
  };
}

/** E13 重发凭证的结果。 */
export interface IssuedCredential {
  device_id: string;
  device_secret: string;
  rotated_at: string;
}

/**
 * `POST /api/devices/{device_id}/credential`（E13，裁决 D2-A）。
 *
 * 语义是「先解绑再签发」：调用后该设备的长者绑定被清空、状态置 `offline`、
 * 旧凭证明文立即失效。UI 必须先警告再调用，不能当普通「重置密码」按钮。
 */
export async function issueCredential(deviceId: string): Promise<IssuedCredential> {
  const raw = await apiSend<unknown>(
    `/api/devices/${encodeURIComponent(deviceId)}/credential`,
    "POST",
  );
  const body = (raw ?? {}) as Record<string, unknown>;
  return {
    device_id: asString(body.device_id),
    device_secret: asString(body.device_secret),
    rotated_at: asString(body.rotated_at),
  };
}

/** E11 候选长者（姓名已脱敏：`张*芬`）。 */
export interface ElderCandidate {
  elder_id: string;
  bed_no: string;
  display_name: string;
}

export interface ElderCandidateList {
  room_id: string;
  mode: "single" | "double";
  candidates: ElderCandidate[];
}

/**
 * `GET /api/devices/{device_id}/elder-candidates`（E11，已在线）。
 *
 * 返回本房间可绑定的候选长者；`room_id` 正是绑定请求体里缺的那一项。
 */
export async function listElderCandidates(deviceId: string): Promise<ElderCandidateList> {
  const raw = await apiGet<unknown>(
    `/api/devices/${encodeURIComponent(deviceId)}/elder-candidates`,
  );
  const body = (raw ?? {}) as Record<string, unknown>;
  const rawCandidates = Array.isArray(body.candidates) ? body.candidates : [];
  return {
    room_id: asString(body.room_id),
    mode: body.mode === "double" ? "double" : "single",
    candidates: rawCandidates.map((item) => {
      const row = (item ?? {}) as Record<string, unknown>;
      return {
        elder_id: asString(row.elder_id),
        bed_no: asString(row.bed_no),
        display_name: asString(row.display_name),
      };
    }),
  };
}

export interface DeviceBindResult {
  device_id: string;
  room_id: string;
  elder_id: string;
}

/**
 * `POST /api/devices/{device_id}/bind` —— 请求体必须是 `{room_id, elder_id}`（`extra="forbid"`）。
 *
 * `elder_id = null` 走「绑定房间、暂不绑长者」语义（服务端 `_clear_binding`），本页不提供该路径：
 * 照护台上「绑定」就是把设备给某位长者，留空会让人以为没绑上。
 */
export async function bindDevice(
  deviceId: string,
  payload: { room_id: string; elder_id: string },
): Promise<DeviceBindResult> {
  const raw = await apiSend<unknown>(
    `/api/devices/${encodeURIComponent(deviceId)}/bind`,
    "POST",
    payload,
  );
  const body = (raw ?? {}) as Record<string, unknown>;
  return {
    device_id: asString(body.device_id),
    room_id: asString(body.room_id),
    elder_id: asString(body.elder_id),
  };
}

/**
 * `POST /api/devices/{device_id}/unbind`（无请求体）。
 *
 * 服务端语义：清 `elder_id`、状态置 `offline`、**轮换长期令牌哈希**——旧设备凭证立即 401，
 * 现场必须重新下发凭证才能上线。弹窗文案据此写，不弱化成「断开连接」。
 */
export async function unbindDevice(deviceId: string): Promise<DeviceBindResult> {
  const raw = await apiSend<unknown>(
    `/api/devices/${encodeURIComponent(deviceId)}/unbind`,
    "POST",
  );
  const body = (raw ?? {}) as Record<string, unknown>;
  return {
    device_id: asString(body.device_id),
    room_id: asString(body.room_id),
    elder_id: asString(body.elder_id),
  };
}

/** 设备短号：UUID 前 8 位，列表里已经够区分（完整 id 放 title 供核对）。 */
export function shortId(device: DeviceRow): string {
  return device.device_id ? device.device_id.slice(0, 8) : "—";
}

/** 最后心跳：ISO 串 → `MM-DD HH:mm`（**Asia/Shanghai**）；空值显示「从未上线」。 */
export function formatLastSeen(value: string): string {
  if (!value) return "从未上线";
  // 收敛到 shared/format（AGENTS §8）。原实现用 getHours() 即浏览器本地时区。
  return formatInstant(value, { withYear: false, invalid: "raw" });
}

/**
 * 床位显示：后端 `bed_no` 已经是「1 号床」这种带单位的成品文案（见 W4 契约），
 * 页面若再拼一个「床」就会渲染成「1 号床 床」。这里只补裸数字（如「1」→「1 床」），
 * 已含「床」的原样返回，空值返回空串由调用方决定占位。
 */
export function bedLabel(value: string): string {
  const text = value.trim();
  if (!text) return "";
  return text.includes("床") ? text : `${text} 床`;
}
