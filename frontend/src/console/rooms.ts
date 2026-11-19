/**
 * 房间数据层（FR-ADM-01）。
 *
 * 契约唯一依据：`app/api/rooms.py`。
 * - `GET /api/rooms`：admin only，按 `room_no` 排序，不分页；
 * - `PATCH /api/rooms/{room_id}`：admin only，只收 `room_no` / `status`，空补丁 422。
 *
 * 房间同样给设备绑定页选房用，所以列表实现放在这里，`devices.ts` 不再保留第二份。
 */
import { apiGet, apiSend } from "./http";
import { asString } from "./staff";

export type RoomStatus = "available" | "partial" | "full" | "maintenance";

export interface RoomOption {
  room_id: string;
  room_no: string;
  status: string;
  bed_count: number;
}

export interface RoomUpdatePayload {
  room_no?: string;
  status?: RoomStatus;
}

export const ROOM_STATUS_OPTIONS: readonly { value: RoomStatus; label: string }[] = [
  { value: "available", label: "可用" },
  { value: "partial", label: "部分入住" },
  { value: "full", label: "已满" },
  { value: "maintenance", label: "维护中" },
];

const ROOM_STATUS_TEXT: Record<string, string> = Object.fromEntries(
  ROOM_STATUS_OPTIONS.map((item) => [item.value, item.label]),
);

export function roomStatusText(status: string): string {
  return ROOM_STATUS_TEXT[status] ?? status;
}

function toRoomOption(raw: unknown): RoomOption {
  const row = (raw ?? {}) as Record<string, unknown>;
  const bedCount = Number(row.bed_count);
  return {
    room_id: asString(row.room_id),
    room_no: asString(row.room_no),
    status: asString(row.status),
    bed_count: Number.isFinite(bedCount) ? bedCount : 0,
  };
}

/** `GET /api/rooms`（admin）：设备绑定与房间管理共用。 */
export async function listRooms(): Promise<RoomOption[]> {
  const raw = await apiGet<unknown>("/api/rooms");
  const body = (raw ?? {}) as Record<string, unknown>;
  const items = Array.isArray(body.items) ? body.items : [];
  return items.map(toRoomOption);
}

/**
 * `PATCH /api/rooms/{room_id}`（admin）：只提交发生变化的字段。
 *
 * 服务端 `extra="forbid"` 且拒绝空补丁；页面层也应该先比较再提交，避免把
 * 「打开弹窗又原样保存」当成一次成功写操作。
 */
export async function updateRoom(
  roomId: string,
  payload: RoomUpdatePayload,
): Promise<RoomOption> {
  const raw = await apiSend<unknown>(
    `/api/rooms/${encodeURIComponent(roomId)}`,
    "PATCH",
    payload,
  );
  return toRoomOption(raw);
}
