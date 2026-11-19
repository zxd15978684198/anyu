/**
 * 长者端记忆（PRD FR-MEM-02）的**唯一出口**：组件里不写裸 `fetch`。
 *
 * 负责什么：
 * - `/api/memory/me/chunks` 三个端点的封装：列表、就地改正文、撤回一条（软删）；
 * - 把 HTTP 结果翻成一句长者看得懂的话（不出现状态码、不出现「接口/参数」这类词）；
 * - 分类枚举的中文标签。
 *
 * 不负责什么：
 * - **不决定 `elder_id`**：三个端点只认设备令牌绑定的那位长者，请求里塞了也不会被采信
 *   （AGENTS §4 第 9 条），所以本模块从头到尾没有 `elder_id` 参数；
 * - **不新建记忆**：写入走对话侧的记忆抽取链路，本人端只能改已有正文、撤回已有的，
 *   没有「新增一条」的入口（后端本人端也没有 POST）；
 * - 不决定入口、不做路由与整页刷新——那属于调用方。
 *
 * 三个端点共用一条口径：**失败不抛异常**，返回 `ok:false` + 一句人话，
 * 界面照旧可用。写法照 `apiSos.ts` / `apiDialogue.ts`：长者端不能因为一次网络抖动就白屏。
 */

import { deviceToken } from "./http";

/** 一条记忆切片（后端 `MemoryChunkOut`；**不返回 embedding**，前端也拿不到向量）。 */
export interface MemoryChunk {
  chunk_id: string;
  elder_id: string;
  content: string;
  /** `private` / `health` / `family` / `preference` …… 取值随后端增长，界面一律走 `memoryCategoryLabel` */
  category: string;
  importance: number;
  status: string;
  created_at: string | null;
  last_accessed: string | null;
}

/**
 * 列表结果。
 *
 * `items` 为空数组有两种可能：**真的一条都没记住**，或者**根本没取回来**。
 * 只有 `ok` 能区分这两件事——把失败画成空态，长者会以为自己的记忆被清空了。
 * `total` / `limit` / `offset` 故意不往上带：后端的 `total` 是**本页条数**不是总数
 * （契约 §3.3），面板一屏只显示一页，带上它只会诱使界面写出「共 N 条」这种错话。
 */
export interface MemoryListResult {
  ok: boolean;
  items: MemoryChunk[];
  /** 可给长者看的一句话；`ok:true` 时为 null */
  message: string | null;
  /** 登录失效 / 设备认不出人：界面该引导「先登录」，而不是「网络不好，再试一次」 */
  needsLogin: boolean;
}

/** 改正文的结果。 */
export interface MemoryCorrectResult {
  ok: boolean;
  /**
   * 服务端落库后的那一条（正文已被后端去掉首尾空白）。
   * `ok:true` 但 `item` 为 null 只发生在一件事上：**写成功了、响应体却读不出来**，
   * 此时调用方应当用自己提交的正文更新本地那一条，而不是当成失败。
   */
  item: MemoryChunk | null;
  message: string | null;
  needsLogin: boolean;
}

/** 撤回一条的结果。 */
export interface MemoryForgetResult {
  ok: boolean;
  /**
   * `true` = 本次真的把这条从「还记着」改成了「已删」；
   * `false` = 这条此前就是这样（重复点按），**不是失败**，界面照样把它移出列表。
   */
  deleted: boolean;
  /** 这次操作该说给长者听的一句话（成功也有，因为行已经从屏幕上消失了） */
  message: string;
  needsLogin: boolean;
}

/** 一次取多少条。后端单页上限 200；长者端一屏看 20 条已经要滚了。 */
export const MEMORY_PAGE_SIZE = 20;

/**
 * 正文长度上限。
 *
 * **跟后端对齐，不跟口头契约对齐**：`app/services/memory_service.py:95`
 * 是 `MAX_CONTENT_LENGTH: Final[int] = 200`，超长在 Pydantic 入口直接 422。
 * 前端若按 500 放行，长者能写完 300 字再被打回——那比一开始就写不下更伤。
 * 后端真放宽时只需改这一个数。
 */
export const MEMORY_CONTENT_MAX = 200;

const NETWORK_TEXT = "刚才没取回来，再说一次就行";
const CORRECT_FAIL_TEXT = "刚才没改成，再说一次就行";
const FORGET_FAIL_TEXT = "刚才没能忘掉，再说一次就行";
const LOGIN_TEXT = "登录已失效，请重新登录";
const NOT_OURS_TEXT = "这台设备认不出您，请先登录";
const EMPTY_TEXT = "这句话是空的，写点什么再改";
const TOO_LONG_TEXT = `一次最多记 ${MEMORY_CONTENT_MAX} 个字，短一点再说一次`;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function readNullableString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

/**
 * 只认「能上屏」的两件事：`chunk_id` 与 `content`。
 *
 * 少这两样就没法渲染也没法操作，宁可当这条没取到（列表少一行），
 * 也不往界面上画 `undefined`。其余字段给安全默认值：它们只用于展示，不参与判定。
 */
function readChunk(value: unknown): MemoryChunk | null {
  if (!isRecord(value)) return null;
  const chunkId = value.chunk_id;
  const content = value.content;
  if (typeof chunkId !== "string" || chunkId === "") return null;
  if (typeof content !== "string") return null;
  return {
    chunk_id: chunkId,
    elder_id: typeof value.elder_id === "string" ? value.elder_id : "",
    content,
    category: typeof value.category === "string" ? value.category : "",
    importance: typeof value.importance === "number" ? value.importance : 0,
    status: typeof value.status === "string" ? value.status : "",
    created_at: readNullableString(value.created_at),
    last_accessed: readNullableString(value.last_accessed),
  };
}

/** 读响应体：读不出来返回 null，由调用方决定这算成功还是失败。 */
async function readJson(res: Response): Promise<unknown> {
  try {
    const raw: unknown = await res.json();
    return raw;
  } catch {
    return null;
  }
}

function authHeaders(token: string): Record<string, string> {
  return { Authorization: `Bearer ${token}` };
}

/**
 * 取本人还记着的记忆。
 *
 * 不传 `status`：后端默认 `active`，长者端只该看见「现在还记着的」；
 * 已删的条目要显式传 `status=deleted` 才看得到——那属于管理员回查，不是本人的日常。
 */
export async function listMyMemories(
  limit: number = MEMORY_PAGE_SIZE,
  offset = 0,
): Promise<MemoryListResult> {
  const token = deviceToken();
  if (!token) {
    return { ok: false, items: [], message: LOGIN_TEXT, needsLogin: true };
  }

  const params = new URLSearchParams();
  params.set("limit", String(limit));
  params.set("offset", String(offset));

  let res: Response;
  try {
    res = await fetch(`/api/memory/me/chunks?${params.toString()}`, {
      headers: authHeaders(token),
    });
  } catch {
    return { ok: false, items: [], message: NETWORK_TEXT, needsLogin: false };
  }

  if (res.status === 401) {
    return { ok: false, items: [], message: LOGIN_TEXT, needsLogin: true };
  }
  // 403 = 不是设备主体，或这台设备还没绑住户（后端两种都压成 403）
  if (res.status === 403) {
    return { ok: false, items: [], message: NOT_OURS_TEXT, needsLogin: true };
  }
  if (!res.ok) {
    return { ok: false, items: [], message: NETWORK_TEXT, needsLogin: false };
  }

  const raw = await readJson(res);
  if (!isRecord(raw) || !Array.isArray(raw.items)) {
    return { ok: false, items: [], message: NETWORK_TEXT, needsLogin: false };
  }

  const items: MemoryChunk[] = [];
  for (const entry of raw.items) {
    const chunk = readChunk(entry);
    if (chunk) items.push(chunk);
  }
  return { ok: true, items, message: null, needsLogin: false };
}

/**
 * 改一条记忆的正文（`PATCH /api/memory/me/chunks/{chunk_id}`）。
 *
 * 只发 `{ content }`：`importance` / `category` 都是写入期的判定结果，
 * 让被记录的人顺手改自己的权重会毁掉衰减与淘汰的可比性（后端也只收这一个字段）。
 */
export async function correctMyMemory(
  chunkId: string,
  content: string,
): Promise<MemoryCorrectResult> {
  const text = content.trim();
  if (text === "") {
    return { ok: false, item: null, message: EMPTY_TEXT, needsLogin: false };
  }
  if (text.length > MEMORY_CONTENT_MAX) {
    return { ok: false, item: null, message: TOO_LONG_TEXT, needsLogin: false };
  }

  const token = deviceToken();
  if (!token) {
    return { ok: false, item: null, message: LOGIN_TEXT, needsLogin: true };
  }

  let res: Response;
  try {
    res = await fetch(`/api/memory/me/chunks/${encodeURIComponent(chunkId)}`, {
      method: "PATCH",
      headers: {
        ...authHeaders(token),
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ content: text }),
    });
  } catch {
    return { ok: false, item: null, message: CORRECT_FAIL_TEXT, needsLogin: false };
  }

  if (res.status === 401) {
    return { ok: false, item: null, message: LOGIN_TEXT, needsLogin: true };
  }
  if (res.status === 403) {
    return { ok: false, item: null, message: NOT_OURS_TEXT, needsLogin: true };
  }
  // 404：跨长者与「不存在」后端压成同一个结果，这里不区分
  if (res.status === 404) {
    return {
      ok: false,
      item: null,
      message: "这条已经不在我记着的里面了，我把它从这一页拿掉",
      needsLogin: false,
    };
  }
  if (res.status === 422) {
    return { ok: false, item: null, message: TOO_LONG_TEXT, needsLogin: false };
  }
  if (!res.ok) {
    return { ok: false, item: null, message: CORRECT_FAIL_TEXT, needsLogin: false };
  }

  // 2xx 已经落库：响应体读不出来也算改成功，调用方用本地草稿兜住正文
  return { ok: true, item: readChunk(await readJson(res)), message: null, needsLogin: false };
}

/**
 * 撤回一条记忆（`DELETE /api/memory/me/chunks/{chunk_id}`，软删）。
 *
 * 两条不能写错的口径：
 * 1. `deleted=false` 表示**这次之前就已经删过了**（重复点按），不是失败——
 *    后端照样 200，而且照样写了一条审计。界面必须把它移出列表并说明「早就忘掉了」，
 *    不能弹红字让人再按一次。
 * 2. 404（别人的 / 不存在的）也按「目的已达成」处理：这条记忆本来就不会再被想起，
 *    给长者看红字只会让人以为「没删掉」。**改动正文的 404 是失败**（那次修改确实没发生），
 *    两者不要照抄。
 */
export async function deleteMyMemory(chunkId: string): Promise<MemoryForgetResult> {
  const token = deviceToken();
  if (!token) {
    return { ok: false, deleted: false, message: LOGIN_TEXT, needsLogin: true };
  }

  let res: Response;
  try {
    res = await fetch(`/api/memory/me/chunks/${encodeURIComponent(chunkId)}`, {
      method: "DELETE",
      headers: authHeaders(token),
    });
  } catch {
    return { ok: false, deleted: false, message: FORGET_FAIL_TEXT, needsLogin: false };
  }

  if (res.status === 401) {
    return { ok: false, deleted: false, message: LOGIN_TEXT, needsLogin: true };
  }
  if (res.status === 403) {
    return { ok: false, deleted: false, message: NOT_OURS_TEXT, needsLogin: true };
  }
  if (res.status === 404) {
    return { ok: true, deleted: false, message: "这条已经不在了", needsLogin: false };
  }
  if (!res.ok) {
    return { ok: false, deleted: false, message: FORGET_FAIL_TEXT, needsLogin: false };
  }

  const raw = await readJson(res);
  // 2xx 却读不出 body：服务端已经收下这次请求，按「删掉了」说，
  // 说成「早就忘掉了」会让长者以为自己之前就删过一次
  const deleted = isRecord(raw) ? raw.deleted === true : true;
  return {
    ok: true,
    deleted,
    message: deleted ? "已经忘掉了" : "这条早就忘掉了",
    needsLogin: false,
  };
}

/**
 * 分类翻成长者听得懂的中文。
 *
 * **未知取值一律落到「其他」，不回显原样**：后端随时可能加新分类，
 * 把 `finance` 这种英文枚举直接摆上长者端的大字区，等于没翻译。
 */
export function memoryCategoryLabel(category: string): string {
  switch (category) {
    case "private":
      return "私事";
    case "health":
      return "身体";
    case "family":
      return "家里人";
    case "preference":
      return "喜好";
    default:
      return "其他";
  }
}
