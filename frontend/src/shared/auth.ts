/**
 * 三端共享的认证请求客户端（前端计划 W2；Q-1 已裁决放开共享）。
 *
 * ## 为什么是工厂而不是模块级单例
 * 收敛前 `console/api.ts` 与 `family/api.ts` 是 **86% 逐字相同**的两份实现，
 * 原裁决（task-05 §0）禁止互相 import，理由是「同源 localStorage 共享，避免双端互踢登录态」。
 * 那个**目的**在这里由**实例隔离**达成，而不是靠复制代码：
 * 每端 `createAuthClient({ storagePrefix })` 得到自己的
 * storage 键（`kb_console_*` / `kb_family_*`）、自己的 refresh 单飞、自己的 401 收口回调。
 * 两端在同一浏览器同时登录互不影响，且改一处两端都受益。
 *
 * ## 收敛掉的四类行为分叉（复制代码长出来的真 bug）
 * 1. **401 收口**：family 有、console 没有 → console 登录失效后卡在失效界面（W1-A3 已补，现统一）；
 * 2. **错误分类**：console 的 `catch {}` 把 refresh 被拒吞成「网络异常」（W1-A4 已修，现统一）；
 * 3. **空响应体**：console 处理 204/205/content-length:0，family 无条件 `res.json()`；
 * 4. **超时**：两端**都没有**，弱网只能等浏览器默认 → 这里补上（默认 30s，可按请求覆盖）。
 *
 * 契约唯一依据：`app/api/auth.py` + `app/api/schemas/auth.py` + `docs/接口契约_身份与权限.md` §E1–E3。
 * E1 实际端点是 `POST /api/sessions`（任务卡写的 `/api/sessions/login` 是笔误）。
 *
 * 本模块是**叶子**：不得 import pad / console / family，也不得 import three。
 */

/** 登录主体信息（E1 principal / E3 身份详情共用的展示字段）。 */
export interface Principal {
  kind: "staff" | "guardian";
  id: string;
  role: string | null;
  name: string;
}

/** E3 当前身份详情。 */
export interface CurrentIdentity extends Principal {
  scope_elder_ids: string[];
}

/** 统一错误文案：用用户语言，禁止回显 trace_id / 内部细节。 */
export type ApiErrorKind = "invalid" | "network";

export class ApiError extends Error {
  constructor(
    public readonly kind: ApiErrorKind,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export const NETWORK_ERROR_TEXT = "网络异常，请稍后重试";
export const INVALID_CREDENTIAL_TEXT = "手机号或密码不正确";

/** 角色显示名（护工/医生/管理员/家属）。 */
export function roleLabel(principal: Principal): string {
  if (principal.kind === "guardian") return "家属";
  const labels: Record<string, string> = {
    care_worker: "护工",
    doctor: "医生",
    admin: "管理员",
  };
  return labels[principal.role ?? ""] ?? principal.role ?? "员工";
}

export interface AuthClientOptions {
  /** localStorage 键前缀，**必须每端不同**（`kb_console` / `kb_family`），否则双端互踢。 */
  storagePrefix: string;
  /** 请求超时（毫秒）。默认 30s；大文件导出等长请求可按调用覆盖。 */
  timeoutMs?: number;
}

/** 单次请求可覆盖超时；`timeoutMs: 0` 表示不超时。 */
export interface AuthedInit extends RequestInit {
  timeoutMs?: number;
}

export interface AuthClient {
  login(phone: string, password: string): Promise<Principal>;
  fetchCurrent(): Promise<CurrentIdentity>;
  logout(): void;
  clearSession(): void;
  hasSession(): boolean;
  cachedProfile(): Principal | null;
  authedFetch(path: string, init?: AuthedInit): Promise<Response>;
  setUnauthorizedHandler(handler: (() => void) | null): void;
  /** 供测试与诊断：本实例用的三个 storage 键。 */
  readonly keys: { access: string; refresh: string; profile: string };
}

const DEFAULT_TIMEOUT_MS = 30_000;

/** 带超时的 fetch。优先用原生 AbortSignal.timeout，不可用时退回 AbortController。 */
async function fetchWithTimeout(
  path: string,
  init: RequestInit,
  timeoutMs: number,
): Promise<Response> {
  if (timeoutMs <= 0) return fetch(path, init);
  const signal =
    typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function"
      ? AbortSignal.timeout(timeoutMs)
      : undefined;
  if (signal) return fetch(path, { ...init, signal });
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(path, { ...init, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

/**
 * 造一个端专属的认证客户端。
 *
 * 每个实例自带闭包状态（refresh 单飞、401 回调），**不共享模块级变量**——
 * 这是「双端不互踢」的实现方式，也是它敢被两端共用的前提。
 */
export function createAuthClient(options: AuthClientOptions): AuthClient {
  const prefix = options.storagePrefix;
  if (!prefix) throw new Error("createAuthClient 必须给 storagePrefix，否则双端会互踢登录态");
  const defaultTimeout = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;

  const keys = {
    access: `${prefix}_access_token`,
    refresh: `${prefix}_refresh_token`,
    profile: `${prefix}_profile`,
  } as const;

  const readAccess = (): string | null => localStorage.getItem(keys.access);
  const readRefresh = (): string | null => localStorage.getItem(keys.refresh);

  const readProfile = (): Principal | null => {
    const raw = localStorage.getItem(keys.profile);
    if (!raw) return null;
    try {
      return JSON.parse(raw) as Principal;
    } catch {
      return null;
    }
  };

  const clearSession = (): void => {
    localStorage.removeItem(keys.access);
    localStorage.removeItem(keys.refresh);
    localStorage.removeItem(keys.profile);
  };

  const saveSession = (access: string, refresh: string, principal: Principal): void => {
    localStorage.setItem(keys.access, access);
    localStorage.setItem(keys.refresh, refresh);
    localStorage.setItem(keys.profile, JSON.stringify(principal));
  };

  let unauthorizedHandler: (() => void) | null = null;
  let refreshInFlight: Promise<string> | null = null;

  const refreshOnce = (): Promise<string> => {
    if (refreshInFlight) return refreshInFlight;
    const refreshToken = readRefresh();
    if (!refreshToken) {
      return Promise.reject(new ApiError("invalid", INVALID_CREDENTIAL_TEXT));
    }
    refreshInFlight = (async () => {
      try {
        const res = await fetchWithTimeout(
          "/api/sessions/refresh",
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ refresh_token: refreshToken }),
          },
          defaultTimeout,
        );
        if (!res.ok) {
          // refresh 也失效（类型不符/过期/token_version 不匹配）→ 清态回登录页
          clearSession();
          throw new ApiError("invalid", INVALID_CREDENTIAL_TEXT);
        }
        const data = (await res.json()) as { access_token: string };
        localStorage.setItem(keys.access, data.access_token);
        return data.access_token;
      } catch (err) {
        if (err instanceof ApiError) throw err;
        clearSession();
        throw new ApiError("network", NETWORK_ERROR_TEXT);
      } finally {
        refreshInFlight = null;
      }
    })();
    return refreshInFlight;
  };

  const authedFetch = async (path: string, init: AuthedInit = {}): Promise<Response> => {
    const { timeoutMs, ...rest } = init;
    const limit = timeoutMs ?? defaultTimeout;
    const access = readAccess();
    let res = await fetchWithTimeout(
      path,
      {
        ...rest,
        headers: {
          ...(rest.headers ?? {}),
          ...(access ? { Authorization: `Bearer ${access}` } : {}),
        },
      },
      limit,
    );
    if (res.status === 401 && readRefresh()) {
      try {
        const newAccess = await refreshOnce();
        res = await fetchWithTimeout(
          path,
          {
            ...rest,
            headers: { ...(rest.headers ?? {}), Authorization: `Bearer ${newAccess}` },
          },
          limit,
        );
      } catch (err) {
        // refresh 也被拒（invalid）：refreshOnce 已清态，这里**必须继续走到下面的 401 收口**。
        // 原实现让异常直接冒出去，导致 unauthorizedHandler 永不触发 ——
        // 冷启动靠 App 自己的 catch 还能回登录页，但**应用内请求失效时用户只看到错误横幅、
        // 停在失效界面**（A3 在这条路径上并未真正修好，由 family-pages 用例抓出）。
        // 网络类失败（network）仍向上抛：断网不该把用户踹回登录页，由 http 层映射成 status=0。
        if (!(err instanceof ApiError && err.kind === "invalid")) throw err;
      }
    }
    if (res.status === 401) {
      clearSession();
      unauthorizedHandler?.();
    }
    return res;
  };

  const login = async (phone: string, password: string): Promise<Principal> => {
    let res: Response;
    try {
      res = await fetchWithTimeout(
        "/api/sessions",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ phone, password }),
        },
        defaultTimeout,
      );
    } catch {
      throw new ApiError("network", NETWORK_ERROR_TEXT);
    }
    if (!res.ok) {
      // 401 密码错/账号 disabled；其余（422/5xx）按固定文案，不回显内部细节
      throw new ApiError(
        res.status === 401 ? "invalid" : "network",
        res.status === 401 ? INVALID_CREDENTIAL_TEXT : NETWORK_ERROR_TEXT,
      );
    }
    const data = (await res.json()) as {
      access_token: string;
      refresh_token: string;
      principal: Principal;
    };
    saveSession(data.access_token, data.refresh_token, data.principal);
    return data.principal;
  };

  const fetchCurrent = async (): Promise<CurrentIdentity> => {
    let res: Response;
    try {
      res = await authedFetch("/api/sessions/current");
    } catch (err) {
      if (err instanceof ApiError) throw err;
      throw new ApiError("network", NETWORK_ERROR_TEXT);
    }
    if (!res.ok) {
      if (res.status === 401) throw new ApiError("invalid", INVALID_CREDENTIAL_TEXT);
      throw new ApiError("network", NETWORK_ERROR_TEXT);
    }
    return (await res.json()) as CurrentIdentity;
  };

  return {
    keys,
    login,
    fetchCurrent,
    logout: clearSession,
    clearSession,
    hasSession: () => Boolean(readAccess() || readRefresh()),
    cachedProfile: readProfile,
    authedFetch,
    setUnauthorizedHandler: (handler) => {
      unauthorizedHandler = handler;
    },
  };
}