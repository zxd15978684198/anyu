/**
 * 身份确认的 REST 出口：`POST /api/identity/confirm`（降级链路专用）。
 *
 * ## 为什么还要一条 REST
 *
 * 正常链路里长者点「是我」，前端发 WS 帧 `identity_confirm`，由
 * `app/audio/media_bridge.py` 落确认态。但按住说话降级时**没有 WS**（信令断了、
 * 或用的是降级入口），此时 WS 发送函数返回 false，按钮会被永久锁成
 * 「网络还没连好，请稍等一下再点一次」——长者只能反复点，或在受挫后放弃确认。
 * 后端早就备好了等价的 `POST /api/identity/confirm`（`app/api/identity.py`，8 例测试绿），
 * 但前端一次都没调用过，这个出口一直空着。本模块把它接上。
 *
 * ## 契约（与后端逐字对齐）
 *
 * - `Authorization: Bearer <设备令牌>`——从 `session.ts` 取，不自建第二套鉴权。
 * - 请求体 `{elder_id, ask_id?}`；`ask_id` 只用于排障对照，服务端幂等键是
 *   「未结束的 confirm thread」，不是 ask_id（`conversation_threads` 没有该列）。
 * - 200 → `{thread_id, identity_source: "confirm", elder_id, created}`；
 *   `created=false` 表示复用了已有确认态（重复点按 / 重试），**不是失败**。
 * - 403 `elder_mismatch` / 409 `device_unbound` / 503 `identity_unavailable`。
 *
 * ## 永不抛
 *
 * 身份确认失败要能把话讲给长者听（「没确认上，您再试一次」），不能变成一个未捕获的
 * Promise 拒绝——那会让 React 层冒出白屏，长者连「稍后再说」都点不到。
 * 所以所有失败都折算成 `{ok: false, errorCode}`。
 */
import { deviceToken } from "./http";

/** 后端 `app/api/identity.py` 的路由，一个常量免得两处拼错。 */
export const IDENTITY_CONFIRM_PATH = "/api/identity/confirm";

/**
 * 请求超时。取 8s：服务端 `open_thread_from_confirm` 只做一次 INSERT + commit
 * （实测毫秒级），8s 足够覆盖弱网；再长会让长者盯着「已确认，正在进入…」发呆。
 */
export const IDENTITY_CONFIRM_TIMEOUT_MS = 8_000;

export interface IdentityConfirmOutcome {
  ok: boolean;
  /** 成功时的会话线程号；失败为 null */
  threadId: string | null;
  /** true = 复用了已有确认态（重复点按），仍是成功 */
  created?: boolean;
  /** 失败码：device_unbound / elder_mismatch / identity_unavailable / no_session / http_5xx / network … */
  errorCode?: string;
}

/**
 * 把后端错误码翻成给长者看的一句话。
 *
 * 不显示 HTTP 状态码、不显示 detail 原文：那是排障信息，长者看不懂也帮不上忙；
 * 真正的现场证据留在后端日志与 Network 面板里。
 */
export function identityErrorText(code: string | undefined): string {
  switch (code) {
    case "no_session":
      return "登录已过期，请重新登录后再确认。";
    case "device_unbound":
      return "这台设备还没绑定长者，请先让护工配对。";
    case "elder_mismatch":
      return "确认的人和这台设备绑定的不是同一位，请让护工检查配对。";
    case "identity_unavailable":
      return "身份服务暂时不可用，请稍后再试。";
    case "network":
      return "网络没连上，请稍等一下再点一次。";
    default:
      return "这次没确认上，请再点一次「是我」。";
  }
}

/** 从响应体里取错误码：`code` 字段优先，否则按 HTTP 状态兜底。 */
function errorCodeFromResponse(res: Response, body: unknown): string {
  if (body !== null && typeof body === "object") {
    const code = (body as { code?: unknown }).code;
    if (typeof code === "string" && code !== "") return code;
  }
  return `http_${res.status}`;
}

/**
 * 用 REST 落一次「是我」。**永不抛**。
 *
 * @param elderId 必须是本设备绑定的长者（服务端会再校验一次，不匹配即 403）
 * @param askId   `identity_ask.ask_id`，仅作排障对照，可缺
 */
export async function confirmIdentity(
  elderId: string,
  askId?: string,
): Promise<IdentityConfirmOutcome> {
  const token = deviceToken();
  if (!token) {
    return { ok: false, threadId: null, errorCode: "no_session" };
  }

  const body: Record<string, string> = { elder_id: elderId };
  if (askId !== undefined && askId !== "") body.ask_id = askId;

  let res: Response;
  try {
    res = await fetch(IDENTITY_CONFIRM_PATH, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "content-type": "application/json",
        accept: "application/json",
      },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(IDENTITY_CONFIRM_TIMEOUT_MS),
    });
  } catch {
    // 网络断 / DNS / 超时（AbortSignal.timeout 抛的也是 AbortError）
    return { ok: false, threadId: null, errorCode: "network" };
  }

  let parsed: unknown = null;
  try {
    parsed = await res.json();
  } catch {
    // 204/空 body/非 JSON：状态码仍然可信，错误码走 http_${status}
    parsed = null;
  }

  if (!res.ok) {
    return { ok: false, threadId: null, errorCode: errorCodeFromResponse(res, parsed) };
  }

  const threadId =
    parsed !== null && typeof parsed === "object"
      ? (parsed as { thread_id?: unknown }).thread_id
      : undefined;
  const created =
    parsed !== null && typeof parsed === "object"
      ? (parsed as { created?: unknown }).created
      : undefined;

  return {
    ok: true,
    threadId: typeof threadId === "string" && threadId !== "" ? threadId : null,
    created: created === true,
  };
}
