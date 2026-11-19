/**
 * pad 请求原语（前端计划 W5）—— 抽出 8 处重复的鉴权头 / 超时 / 错误体解析。
 *
 * ## 为什么不做成一个统一 wrapper
 * pad 各端点的**超时是刻意不同的**：ICE 3s、身份确认 8s、媒体 8s、当日药单 8s、
 * 打字轮 15s、按住说话整段上传 60s（必须覆盖 120 秒音频上传 + 服务端 ASR）。
 * 错误语义也不同：`padRest` 抛 `PadApiError`，`rest-client` 返回判别联合
 * `UtteranceError`（auth/too_long/unavailable/empty），`apiIdentity` 有自己的话术映射。
 * 强行统一会抹掉这些差异，且整段上传与同步核心相连，风险不可接受。
 *
 * 所以这里只抽**三件共用的事**，各调用方保留自己的超时值与错误语义：
 * 1. `deviceAuthHeader()`：从 session 取设备令牌拼 Authorization（原本 8 处各写一遍）；
 * 2. `timeoutSignal(ms)`：AbortSignal + 清理函数（原本两种写法：AbortSignal.timeout 与
 *    AbortController+setTimeout，行为等价但代码两份）；
 * 3. `readErrorBody(res)`：从 C7 错误体抠 `detail` / `code`（原本多处各解析一遍）。
 *
 * 本模块是 pad 内部叶子：只依赖 session，不依赖任何 api* 模块，避免循环引用。
 */
import { loadSession } from "./session";

/** 会话缺失时的统一错误形状；调用方可自行包装成各自的错误类型。 */
export class NoSessionError extends Error {
  readonly status = 401;
  readonly code = "no_session";
  constructor(message = "本次会话已失效，请重新登录") {
    super(message);
    this.name = "NoSessionError";
  }
}

/** 设备令牌鉴权头。无会话时抛 NoSessionError（调用方可转成自己的错误类型）。 */
export function deviceAuthHeader(): Record<string, string> {
  const session = loadSession();
  if (!session) throw new NoSessionError();
  return { Authorization: `Bearer ${session.access_token}` };
}

/** 取设备令牌；无会话返回 null（调用方想自己决定错误形态时用这个）。 */
export function deviceToken(): string | null {
  return loadSession()?.access_token ?? null;
}

export interface TimeoutHandle {
  signal: AbortSignal;
  /** 请求结束后必须调用，否则定时器泄漏。 */
  clear: () => void;
}

/**
 * 超时信号。优先用原生 `AbortSignal.timeout`（无需手动清理），
 * 不可用时退回 `AbortController + setTimeout`。
 * 两种路径对外都给出 `clear()`，调用方写法一致。
 */
export function timeoutSignal(ms: number): TimeoutHandle {
  if (typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function") {
    return { signal: AbortSignal.timeout(ms), clear: () => {} };
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), ms);
  return { signal: controller.signal, clear: () => clearTimeout(timer) };
}

export interface ErrorBodyInfo {
  /** 后端 C7 错误体的 `code`（如 `device_not_bound`）；没有则 `http_error`。 */
  code: string;
  /** 给长者看的那句话：字符串 detail 直接用；FastAPI 校验数组取各项 msg 拼接。 */
  detail: string;
  /** 原始解析结果，调用方需要别的字段时用。 */
  parsed: unknown;
}

/**
 * 解析响应体（成功与失败都可用）。
 *
 * 口径与 `rest-client.ts::detailText` 一致：后端 `app/main.py` 的 handler 把
 * `detail` 包成 `{detail, code, trace_id}`，**响应体里没有 message 字段**，
 * 业务原因全在 detail 里。detail 可能是字符串，也可能是 FastAPI 校验错误数组
 * （每项 `{msg, loc, type}`），这里只做形态归一不做语义判断。
 *
 * 代理返回的 HTML（404/502）解析不了：`parsed` 为 null，`detail` 退到 `HTTP <status>`。
 */
export async function readErrorBody(res: Response): Promise<ErrorBodyInfo> {
  const text = await res.text();
  let parsed: unknown = null;
  try {
    parsed = text ? JSON.parse(text) : null;
  } catch {
    parsed = null;
  }
  const obj = parsed && typeof parsed === "object" ? (parsed as Record<string, unknown>) : null;
  const code = obj && typeof obj.code === "string" && obj.code ? obj.code : "http_error";
  return { code, detail: detailText(obj?.detail) ?? `HTTP ${res.status}`, parsed };
}

/** detail 形态归一：字符串直接用；校验数组取 msg 拼接；其余 null。 */
export function detailText(detail: unknown): string | null {
  if (typeof detail === "string") return detail.trim() || null;
  if (Array.isArray(detail)) {
    const parts = detail
      .map((item: unknown) => {
        if (typeof item === "string") return item.trim();
        if (item !== null && typeof item === "object") {
          const msg = (item as { msg?: unknown }).msg;
          if (typeof msg === "string") return msg.trim();
        }
        return "";
      })
      .filter((t) => t.length > 0);
    return parts.length > 0 ? parts.join("；") : null;
  }
  return null;
}