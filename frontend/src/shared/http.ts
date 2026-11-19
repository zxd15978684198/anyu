/**
 * 三端共享的业务请求层机制（前端计划 W2；Q-1 已裁决放开共享）。
 *
 * ## 收敛什么、不收敛什么
 * **收敛机制**：错误分类（登录失效 vs 断网）、空响应体处理、查询串拼装、错误外壳。
 * **不收敛用语**：按产品负责人 Q-3 裁决「不同业务场景用不同用语，不同表述不是冲突」——
 * 护工看「当前角色无权执行该操作」、家属看「没有权限查看」，是两个角色该听到的不同话。
 * 所以文案由 `messageOf(status, code)` **注入**，各端保留自己那套。
 *
 * ## 收敛前的两类真分叉
 * 1. **空响应体**：console 处理 204/205/`content-length: 0`，family 无条件 `res.json()`
 *    → 后端返回空体时 family 抛 `BAD_BODY`，把「成功但无内容」报成错误。
 *    现由 `allowEmptyBody` 显式表达：写操作端允许，只读端不允许（只读拿到空体确实是异常）。
 * 2. **错误分类**：console 的 `catch {}` 曾把 refresh 被拒吞成「网络异常」（W1-A4 已修）。
 *    现统一为：只有 `ApiError.kind === "invalid"` 判登录失效，其余按网络失败（status=0），
 *    否则断网会把用户踹回登录页。
 *
 * 本模块是**叶子**：只依赖 `shared/auth`，不得 import pad / console / family，也不得 import three。
 */
import { ApiError } from "./auth";

/** 非 2xx 统一错误。`status=0` 表示请求没发出去（网络层失败 / 超时）。 */
export class HttpError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "HttpError";
  }
}

export interface HttpClientOptions {
  /** 该端的带鉴权请求（由 `shared/auth` 的实例提供）。 */
  authedFetch: (path: string, init?: RequestInit) => Promise<Response>;
  /** 把 (状态码, 后端 code) 翻成**该端角色**读得懂的话。用语刻意不跨端统一。 */
  messageOf: (status: number, code: string) => string;
  /** 网络层失败文案（各端用词不同）。 */
  networkText: string;
  /**
   * 成功响应是否允许空体。写操作端（console）有 204/205，必须允许；
   * 只读端（family）拿到空体属异常，置 false 以便报 `BAD_BODY`。默认 true。
   */
  allowEmptyBody?: boolean;
  /**
   * 后端没给 `code` 且状态码不在已知映射里时的兜底 code。
   * console 原用 `BAD_REQUEST`（其文案表有这个键），family 原用 `HTTP_<status>`。
   * 这个差异要保住，否则 console 的「请求格式不正确」会退化成网络文案。
   */
  fallbackCode?: (status: number) => string;
}

export interface HttpClient {
  apiGet<T>(path: string): Promise<T>;
  apiSend<T>(
    path: string,
    method: "POST" | "PATCH" | "PUT" | "DELETE",
    body?: unknown,
  ): Promise<T>;
  errorText(err: unknown): string;
  isNetworkError(err: unknown): boolean;
  isNotFound(err: unknown): boolean;
}

interface ErrorBody {
  code?: unknown;
}

/** 把参数对象拼成查询串，undefined / null / 空串自动跳过。两端原本逐字相同。 */
export function query(
  params: Record<string, string | number | boolean | null | undefined>,
): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

/** 非 2xx 时从响应体取 `code`；取不到按状态码兜底（不回显 detail，那是给排障看的英文）。 */
async function readErrorCode(
  res: Response,
  fallbackCode: (status: number) => string,
): Promise<string> {
  let body: ErrorBody = {};
  try {
    body = (await res.json()) as ErrorBody;
  } catch {
    /* 非 JSON 错误体（网关返回 HTML 等）：按状态码兜底 */
  }
  if (typeof body.code === "string" && body.code) return body.code;
  if (res.status === 401) return "TOKEN_INVALID";
  if (res.status === 403) return "ROLE_FORBIDDEN";
  if (res.status === 404) return "NOT_FOUND";
  if (res.status === 409) return "CONFLICT";
  if (res.status === 422) return "VALIDATION_ERROR";
  if (res.status >= 500) return "INTERNAL_ERROR";
  return fallbackCode(res.status);
}

export function createHttpClient(options: HttpClientOptions): HttpClient {
  const {
    authedFetch,
    messageOf,
    networkText,
    allowEmptyBody = true,
    fallbackCode = () => "BAD_REQUEST",
  } = options;

  const send = async (path: string, init: RequestInit): Promise<Response> => {
    try {
      return await authedFetch(path, init);
    } catch (err) {
      // authedFetch 只抛两类：refresh 链路的 ApiError（invalid = refresh 也被拒），
      // 以及 fetch 自己的 TypeError / AbortError（断网、后端没起、超时）。
      // **只有 invalid 才是登录失效**，其余一律按网络失败（status=0）。
      const tokenInvalid = err instanceof ApiError && err.kind === "invalid";
      const status = tokenInvalid ? 401 : 0;
      const code = tokenInvalid ? "TOKEN_INVALID" : "NETWORK";
      throw new HttpError(status, code, tokenInvalid ? messageOf(401, code) : networkText);
    }
  };

  const settle = async <T>(res: Response): Promise<T> => {
    if (res.status === 204 || res.status === 205) {
      if (allowEmptyBody) return undefined as T;
      throw new HttpError(res.status, "BAD_BODY", messageOf(res.status, "BAD_BODY"));
    }
    if (!res.ok) {
      const code = await readErrorCode(res, fallbackCode);
      throw new HttpError(res.status, code, messageOf(res.status, code));
    }
    if (allowEmptyBody && res.headers.get("content-length") === "0") {
      return undefined as T;
    }
    try {
      return (await res.json()) as T;
    } catch {
      if (allowEmptyBody) return undefined as T;
      throw new HttpError(res.status, "BAD_BODY", messageOf(res.status, "BAD_BODY"));
    }
  };

  const errorText = (err: unknown): string => {
    if (err instanceof HttpError) {
      if (err.status === 0) return networkText;
      return messageOf(err.status, err.code) || err.message || networkText;
    }
    if (err instanceof ApiError) return err.message;
    if (err instanceof Error && err.message) return err.message;
    return networkText;
  };

  return {
    apiGet: async <T>(path: string): Promise<T> => settle<T>(await send(path, { method: "GET" })),
    apiSend: async <T>(
      path: string,
      method: "POST" | "PATCH" | "PUT" | "DELETE",
      body?: unknown,
    ): Promise<T> => {
      const init: RequestInit = { method };
      if (body !== undefined) {
        init.headers = { "Content-Type": "application/json" };
        init.body = JSON.stringify(body);
      }
      return settle<T>(await send(path, init));
    },
    errorText,
    isNetworkError: (err) => err instanceof HttpError && err.status === 0,
    isNotFound: (err) => err instanceof HttpError && err.status === 404,
  };
}