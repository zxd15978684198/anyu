/**
 * 戏曲 / 电影点播（FR-CON-03 / FR-CON-04）的**唯一** HTTP 出口。
 *
 * ## 本模块负责
 *
 * 五个端点的地址、鉴权头、错误归一、给长者看的中文话术、响应形状校验：
 *
 * | 函数 | 端点 |
 * | --- | --- |
 * | `fetchMediaLibrary(kind)` | `GET  /api/media/library?kind=` |
 * | `pickMedia({kind, genre?})` | `POST /api/media/pick` |
 * | `playMedia(mediaId)` | `POST /api/media/{media_id}/play`（无 body） |
 * | `finishMedia(historyId, progress)` | `POST /api/media/finish` |
 * | `switchNextMedia({kind, genre?})` | `POST /api/media/switch-next` |
 *
 * ## 本模块不负责
 *
 * - **播放**：`<audio>` 与「播完了 / 不听了」的收尾时序在 `components/MediaPlayer.tsx`。
 * - **TTS 互斥与音频焦点**：让数字人闭嘴再放戏是父组件的事，这里只发请求。
 * - **控制意图分发**：「换一段 / 停」从规则层经 App 分发到哪个面板，是 App 的活；
 *   本模块不订阅 WS、不读路由决策。
 * - **选哪一段的业务口径**（去重最近 5 条、版权白名单、被打断记 0.0）全在
 *   `app/services/media_service.py`，前端一条都不复制。
 *
 * ## 三条口径
 *
 * 1. **不传 `elder_id`**：五个端点一律靠设备令牌，服务端从 `devices.elder_id` 取；
 *    请求体里带 `elder_id` 会被 `extra="forbid"` 判 422（AGENTS §4.9 红线）。
 * 2. **永不抛**：失败折算成 `{ok:false, errorCode, message}`。长者端任何一个漏到
 *    React 的未捕获拒绝都是白屏，而白屏对长者等于「平板坏了」。
 * 3. **不暴露内部错误码**：HTTP 状态、`code`、`detail` 只用来归类，界面只看到
 *    `message` 这句大白话（排障证据留在后端日志与 Network 面板）。
 */

import { deviceToken } from "./http";

/**
 * 单次请求上限 8s：与 `padRest.ts` / `apiIdentity.ts` 同一口径。
 * 卧室平板是弱网常态，再长长者就会以为「按了没反应」开始连点。
 */
const REQUEST_TIMEOUT_MS = 8_000;

/** 点播类型，与后端 `MediaKind = Literal["opera", "movie"]` 同源。 */
export type MediaKind = "opera" | "movie";

/**
 * 一条可播节目。字段与 `app/api/media.py::MediaItemResponse` **逐字对齐**（蛇形），
 * 前端不另起一套驼峰名：这样 Lead 接线时把响应直接喂给组件，不需要中间映射层，
 * 也就少一处「改了后端忘了改映射」的静默失配。
 */
export interface MediaItem {
  media_id: string;
  kind: MediaKind;
  title: string;
  genre: string | null;
  era: string | null;
  artist: string | null;
  /** nginx 托管的同源路径（`/media/...`），可直接喂给 `<audio src>` */
  url: string;
  /** 素材录入时登记的时长；缺失为 null（不是 0） */
  duration_seconds: number | null;
}

/** `GET /library`：只含版权可播的素材 */
export interface MediaLibrary {
  kind: MediaKind;
  items: MediaItem[];
}

/** `pick` / `switch-next` 的响应 */
export interface MediaPick {
  kind: MediaKind;
  item: MediaItem;
}

/** `play` 的响应：`history_id` 是后续 `finish` 的唯一凭据 */
export interface MediaPlay {
  history_id: string;
  elder_id: string;
  started_at: string;
  item: MediaItem;
}

/** `finish` 的响应；`completion_rate` 为 null 表示没测到，**不是 0** */
export interface MediaFinish {
  history_id: string;
  elder_id: string;
  media_id: string;
  started_at: string;
  finished_at: string;
  completion_rate: number | null;
}

/**
 * 错误分类。取值刻意按「长者能听懂的失败原因」分，不按 HTTP 状态分：
 * 同一个 404 在 `library` 是「库里没有」，在 `finish` 是「记录没了」，
 * 界面话术却可以统一成「这段找不着了」。
 */
export type MediaErrorCode =
  | "no_session"
  | "device_unbound"
  | "not_found"
  | "copyright"
  | "forbidden"
  | "offline"
  | "server"
  | "bad_payload";

export interface MediaFailure {
  ok: false;
  errorCode: MediaErrorCode;
  /** 给长者看的一句话（不含状态码、不含英文错误码） */
  message: string;
}

export interface MediaSuccess<T> {
  ok: true;
  data: T;
}

export type MediaOutcome<T> = MediaSuccess<T> | MediaFailure;

/**
 * 错误分类 → 长者话术。**唯一**一张翻译表：调用方别再各写一句。
 *
 * 措辞口径：说「接下来怎么办」，不说「哪里坏了」。长者也帮不上排障，
 * 与其让人盯着「网络错误」发愁，不如直接告诉他会发生什么。
 */
export function mediaErrorText(code: MediaErrorCode): string {
  switch (code) {
    case "no_session":
      return "登录已过期，请重新登录";
    case "device_unbound":
      return "这台设备还没配对，请叫工作人员看一下";
    case "not_found":
      return "这段没找着，我给您换一段";
    case "copyright":
      return "这段暂时不能放，我给您换一段";
    case "forbidden":
      return "这条播放记录对不上这台设备，请叫工作人员看一下";
    case "offline":
      return "网断了，等一会儿再试";
    case "server":
      return "后台这会儿有点忙，等一会儿再试";
    case "bad_payload":
      return "这段放不出来，我给您换一段";
    default:
      return "没能放出来，请再按一次";
  }
}

function fail(errorCode: MediaErrorCode): MediaFailure {
  return { ok: false, errorCode, message: mediaErrorText(errorCode) };
}

/** 从任意响应体里取一个非空字符串字段（不信任后端字段类型，先判再取）。 */
function readString(raw: unknown, key: string): string | null {
  if (raw === null || typeof raw !== "object") return null;
  const value = (raw as Record<string, unknown>)[key];
  return typeof value === "string" && value !== "" ? value : null;
}

/**
 * 状态码 + 错误体 → 分类。
 *
 * 只有契约里的 `code` 才是**语义**，状态码只用来兜「契约之外那一层」（鉴权、参数校验）。
 *
 * 为什么不再按 422 猜「版权」：服务端的授权失败已经从普通校验里拆出来了
 * （`app/api/media.py` 的媒体路由类返回独立 `code = COPYRIGHT_UNAUTHORIZED`，
 * 状态仍是 422）。继续按状态猜，长者会听到「这段没授权」——而他其实什么也没做错，
 * 只是请求形状不对（那种情况界面该说「出了点问题」，不是「没版权」）。
 */
function failureFromStatus(status: number, body: unknown): MediaFailure {
  const code = readString(body, "code");
  if (code === "device_unbound") return fail("device_unbound");
  if (code === "NOT_FOUND") return fail("not_found");
  if (code === "COPYRIGHT_UNAUTHORIZED") return fail("copyright");
  if (code === "VALIDATION_ERROR") return fail("bad_payload");
  if (code === "SCOPE_FORBIDDEN") return fail("forbidden");
  if (status === 401) return fail("no_session");
  if (status === 403) return fail("forbidden");
  if (status === 404) return fail("not_found");
  // 400 只可能来自路径参数（media_id 不是 UUID）：那是前端的形状错，不是服务故障
  if (status === 400) return fail("bad_payload");
  return fail("server");
}

interface RequestInitLite {
  method: "GET" | "POST";
  body?: unknown;
}

/**
 * 统一请求信封：鉴权头、超时、JSON 解析、状态归一。
 *
 * 成功时返回**未校验**的 `unknown`，由各导出函数自己收窄——列表和单条节目的
 * 形状不同，信封层不该假装知道。
 */
async function requestJson(path: string, init: RequestInitLite): Promise<MediaOutcome<unknown>> {
  const token = deviceToken();
  if (!token) return fail("no_session");

  let res: Response;
  try {
    res = await fetch(path, {
      method: init.method,
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
      headers: {
        accept: "application/json",
        ...(init.body === undefined ? {} : { "content-type": "application/json" }),
        Authorization: `Bearer ${token}`,
      },
      ...(init.body === undefined ? {} : { body: JSON.stringify(init.body) }),
    });
  } catch {
    // 断网 / DNS / 超时（AbortSignal.timeout 抛的 AbortTimeoutError 也走这里）
    return fail("offline");
  }

  let body: unknown = null;
  try {
    body = await res.json();
  } catch {
    // 代理回的 HTML 错误页、空 body：状态码仍可信，形状校验交给调用方
    body = null;
  }

  if (!res.ok) return failureFromStatus(res.status, body);
  return { ok: true, data: body };
}

function optString(raw: unknown): string | null {
  return typeof raw === "string" && raw.trim() !== "" ? raw : null;
}

/**
 * 校验一条节目。**必须**有 `media_id` / `title` / `url`，否则视为不可播。
 *
 * 为什么在这一层挡：`url` 直接被 `<audio src>` 消费，一个 `undefined` 进去就是
 * 静默无声（浏览器不报错、界面不闪），长者只会觉得「平板又坏了」。
 * 宁可在这里判成 `bad_payload`，让界面立刻说「这段放不出来，我给您换一段」。
 */
function readItem(raw: unknown): MediaItem | null {
  if (raw === null || typeof raw !== "object") return null;
  const row = raw as Record<string, unknown>;
  const mediaId = row.media_id;
  const kind = row.kind;
  const title = row.title;
  const url = row.url;
  if (typeof mediaId !== "string" || mediaId === "") return null;
  if (kind !== "opera" && kind !== "movie") return null;
  if (typeof title !== "string" || title === "") return null;
  if (typeof url !== "string" || url === "") return null;

  const duration = row.duration_seconds;
  return {
    media_id: mediaId,
    kind,
    title,
    genre: optString(row.genre),
    era: optString(row.era),
    artist: optString(row.artist),
    url,
    duration_seconds:
      typeof duration === "number" && Number.isFinite(duration) && duration > 0 ? duration : null,
  };
}

/** 列可播节目。空库返回 `items: []`（不是失败）：界面要说「库里还没录」，不是「坏了」。 */
export async function fetchMediaLibrary(
  kind: MediaKind,
): Promise<MediaOutcome<MediaLibrary>> {
  const result = await requestJson(`/api/media/library?kind=${encodeURIComponent(kind)}`, {
    method: "GET",
  });
  if (!result.ok) return result;

  const body = result.data;
  const rows =
    body !== null && typeof body === "object"
      ? (body as Record<string, unknown>).items
      : null;
  if (!Array.isArray(rows)) return fail("bad_payload");

  const items: MediaItem[] = [];
  for (const row of rows) {
    const item = readItem(row);
    // 单条脏数据只丢这一条：一个坏 url 不该让整张戏曲列表变成「点不了」
    if (item) items.push(item);
  }
  return { ok: true, data: { kind, items } };
}

/** 读单条节目 + 类型；`kind` 缺省时认请求里的那个。 */
function readPick(raw: unknown, fallbackKind: MediaKind): MediaPick | null {
  if (raw === null || typeof raw !== "object") return null;
  const row = raw as Record<string, unknown>;
  const item = readItem(row.item);
  if (!item) return null;
  const kind = row.kind === "opera" || row.kind === "movie" ? row.kind : fallbackKind;
  return { kind, item };
}

/**
 * 挑一段。不传参时后端默认 `kind="opera"`（`media_service.DEFAULT_KIND`）。
 *
 * 挑不到就是失败（`not_found`），不拿别的节目顶上——空库时说「没这段」比
 * 放一段不相干的相声诚实。
 */
export async function pickMedia(options: {
  kind?: MediaKind;
  genre?: string;
} = {}): Promise<MediaOutcome<MediaPick>> {
  const body: { kind?: MediaKind; genre?: string } = {};
  if (options.kind !== undefined) body.kind = options.kind;
  if (options.genre !== undefined && options.genre !== "") body.genre = options.genre;

  const result = await requestJson("/api/media/pick", { method: "POST", body });
  if (!result.ok) return result;
  const pick = readPick(result.data, options.kind ?? "opera");
  return pick ? { ok: true, data: pick } : fail("bad_payload");
}

/**
 * 开始播放：服务端写一条 `media_history` 并回 `history_id`。
 *
 * 调用方**必须**把 `history_id` 留住并交给 `finishMedia`：没有它就没法收尾，
 * 那条会话会一直挂在「未结束」上，直到下一次 `switch_next` 把它按打断收掉。
 */
export async function playMedia(mediaId: string): Promise<MediaOutcome<MediaPlay>> {
  if (mediaId === "") return fail("bad_payload");

  const result = await requestJson(`/api/media/${encodeURIComponent(mediaId)}/play`, {
    method: "POST", // 无 body：后端不解析请求体，塞什么都进不了代码路径
  });
  if (!result.ok) return result;

  const body = result.data;
  const historyId = readString(body, "history_id");
  const row = body !== null && typeof body === "object" ? (body as Record<string, unknown>) : null;
  const item = readItem(row?.item);
  if (!historyId || !item) return fail("bad_payload");

  return {
    ok: true,
    data: {
      history_id: historyId,
      elder_id: readString(body, "elder_id") ?? "",
      started_at: readString(body, "started_at") ?? "",
      item,
    },
  };
}

/**
 * 收尾一次播放，带上「播到哪 / 总长」两段毫秒。
 *
 * 两个值缺一个就是「没测到」：服务端落 NULL。**不要**为了凑数补 0——
 * 0 的含义是「被打断」，与「不知道」在偏好排序里不是一回事。
 *
 * `progress_ms` / `duration_ms` 的校验（`ge=0` / `gt=0`）在前端也做一遍：
 * 平板休眠后 `currentTime` 可能是 `NaN`，直接 JSON 序列化成 `null` 更糟。
 */
export async function finishMedia(
  historyId: string,
  progress: { progress_ms?: number; duration_ms?: number } = {},
): Promise<MediaOutcome<MediaFinish>> {
  if (historyId === "") return fail("bad_payload");

  const body: { history_id: string; progress_ms?: number; duration_ms?: number } = {
    history_id: historyId,
  };
  const played = progress.progress_ms;
  if (typeof played === "number" && Number.isFinite(played) && played >= 0) {
    body.progress_ms = Math.round(played);
  }
  const total = progress.duration_ms;
  if (typeof total === "number" && Number.isFinite(total) && total > 0) {
    body.duration_ms = Math.round(total);
  }

  const result = await requestJson("/api/media/finish", { method: "POST", body });
  if (!result.ok) return result;

  const data = result.data;
  const history = readString(data, "history_id");
  const mediaId = readString(data, "media_id");
  if (!history || !mediaId) return fail("bad_payload");

  const rawRate =
    data !== null && typeof data === "object"
      ? (data as Record<string, unknown>).completion_rate
      : null;
  return {
    ok: true,
    data: {
      history_id: history,
      elder_id: readString(data, "elder_id") ?? "",
      media_id: mediaId,
      started_at: readString(data, "started_at") ?? "",
      finished_at: readString(data, "finished_at") ?? "",
      // null = 没测到；不要 `?? 0`，那会把「不知道」写成「没听完」
      completion_rate: typeof rawRate === "number" && Number.isFinite(rawRate) ? rawRate : null,
    },
  };
}

/**
 * 「换一段」：排除最近 5 条后再挑一段，并把当前未结束的会话按「被打断」收尾。
 *
 * 注意响应里**没有** `history_id`：这个端点只挑节目、不收尾新会话。要真正播起来，
 * 调用方拿到 `item` 后还得再 `playMedia(item.media_id)` 拿新的 `history_id`
 * （`MediaPlayer` 就是这么做的）。
 */
export async function switchNextMedia(options: {
  kind?: MediaKind;
  genre?: string;
} = {}): Promise<MediaOutcome<MediaPick>> {
  const body: { kind?: MediaKind; genre?: string } = {};
  if (options.kind !== undefined) body.kind = options.kind;
  if (options.genre !== undefined && options.genre !== "") body.genre = options.genre;

  const result = await requestJson("/api/media/switch-next", { method: "POST", body });
  if (!result.ok) return result;
  const pick = readPick(result.data, options.kind ?? "opera");
  return pick ? { ok: true, data: pick } : fail("bad_payload");
}
