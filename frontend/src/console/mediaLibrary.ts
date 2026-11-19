/**
 * 管理端媒体库数据层（FR-ADM-02）。
 *
 * 与 pad 的 `GET /api/media/library` 不是同一个口径：
 * - pad 只看授权素材，用于点播；
 * - 管理端 `GET /api/media/library/items` 返回未授权素材、`media_id`、`copyright_status`
 *   与 `playable`，维护页靠它解释“这条为什么播不了”。
 *
 * 写端点只允许 admin；`copyright_status` 服务端 fail-closed 白名单，只接受
 * `public_domain` / `licensed_sample` 两个值。
 */
import { apiGet, apiSend, query } from "./http";
import { asString } from "./staff";

export type MediaKind = "opera" | "movie";

export const MEDIA_KIND_OPTIONS: readonly { value: MediaKind; label: string }[] = [
  { value: "opera", label: "戏曲" },
  { value: "movie", label: "电影" },
];

export type CopyrightStatus = "public_domain" | "licensed_sample";

export const COPYRIGHT_OPTIONS: readonly { value: CopyrightStatus; label: string }[] = [
  { value: "public_domain", label: "公版" },
  { value: "licensed_sample", label: "样例授权" },
];

const COPYRIGHT_TEXT: Record<string, string> = Object.fromEntries(
  COPYRIGHT_OPTIONS.map((item) => [item.value, item.label]),
);

export function copyrightText(value: string): string {
  return COPYRIGHT_TEXT[value] ?? value;
}

export function mediaKindText(value: string): string {
  return MEDIA_KIND_OPTIONS.find((item) => item.value === value)?.label ?? value;
}

export interface MediaLibraryItem {
  media_id: string;
  kind: string;
  title: string;
  genre: string | null;
  era: string | null;
  artist: string | null;
  /** 库里原样存的地址（可能是外链）。 */
  media_url: string;
  /** 归一化后的 /media/ 路径，与 pad 同口径。 */
  url: string;
  duration_seconds: number | null;
  copyright_status: string;
  playable: boolean;
}

export interface MediaLibraryList {
  items: MediaLibraryItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface MediaLibraryQuery {
  kind?: MediaKind | "";
  genre?: string;
  limit: number;
  offset: number;
}

function nullableString(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function nullableNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function toLibraryItem(raw: unknown): MediaLibraryItem {
  const item = (raw ?? {}) as Record<string, unknown>;
  return {
    media_id: asString(item.media_id),
    kind: asString(item.kind),
    title: asString(item.title),
    genre: nullableString(item.genre),
    era: nullableString(item.era),
    artist: nullableString(item.artist),
    media_url: asString(item.media_url),
    url: asString(item.url),
    duration_seconds: nullableNumber(item.duration_seconds),
    copyright_status: asString(item.copyright_status),
    playable: item.playable === true,
  };
}

export function mediaLibraryPath(input: MediaLibraryQuery): string {
  return `/api/media/library/items${query({
    kind: input.kind ?? "",
    genre: input.genre ?? "",
    limit: input.limit,
    offset: input.offset,
  })}`;
}

/** `GET /api/media/library/items`（admin）：`total` 是过滤后的总数。 */
export async function listLibraryItems(input: MediaLibraryQuery): Promise<MediaLibraryList> {
  const raw = await apiGet<unknown>(mediaLibraryPath(input));
  const body = (raw ?? {}) as Record<string, unknown>;
  const items = Array.isArray(body.items) ? body.items.map(toLibraryItem) : [];
  return {
    items,
    total: nullableNumber(body.total) ?? items.length,
    limit: nullableNumber(body.limit) ?? input.limit,
    offset: nullableNumber(body.offset) ?? input.offset,
  };
}

/**
 * `PATCH /api/media/library/{media_id}` 的请求体。
 *
 * 只放出现变化的字段；`null` 表示清空可空列，不传表示不动。`duration` 是请求字段，
 * 响应字段叫 `duration_seconds`，映射只在本层发生。
 */
export interface MediaLibraryPatch {
  title?: string;
  media_url?: string;
  copyright_status?: string;
  genre?: string | null;
  era?: string | null;
  artist?: string | null;
  duration?: number | null;
}

export async function updateLibraryItem(
  mediaId: string,
  payload: MediaLibraryPatch,
): Promise<MediaLibraryItem> {
  const raw = await apiSend<unknown>(
    `/api/media/library/${encodeURIComponent(mediaId)}`,
    "PATCH",
    payload,
  );
  return toLibraryItem(raw);
}

/** `DELETE /api/media/library/{media_id}`（admin）：204 成功；有播放历史时 409。 */
export function deleteLibraryItem(mediaId: string): Promise<void> {
  return apiSend<void>(`/api/media/library/${encodeURIComponent(mediaId)}`, "DELETE");
}
