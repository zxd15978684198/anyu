/**
 * W2 · 浏览器侧的 ICE 配置（STUN/TURN）拉取。
 *
 * ## 为什么要有这个模块
 *
 * 旧代码 `new RTCPeerConnection()` 不带任何参数：浏览器只拿到厂商 STUN，没有 TURN。
 * 跨容器 / 跨网段时两侧都只能播内网地址，ICE 永远走不通，整条 WebRTC 降级成「按住说话」。
 * 服务端 `GET /api/audio/ice-config` 用设备令牌下发本环境可用的 STUN+TURN，
 * 这里负责取回来喂给 `RTCPeerConnection`。
 *
 * ## 契约（`docs/接口契约_音频与信令.md`）
 *
 * - 鉴权：设备令牌（`Authorization: Bearer <access_token>`，与 pad 登录同一个）；
 * - 响应：`{ ice_servers: [{ urls: [...], username?, credential? }], ttl_seconds, generated_at }`；
 * - **降级不是错误**：服务端 TURN 未配置时给 `ice_servers: []`；本模块同样把
 *   超时 / 断网 / 401 / 500 / 响应体异常一律收敛成 `[]`，**绝不 reject**——
 *   调用方拿到的永远是「可以用的数组」，空数组等价于旧的无参建连。
 *
 * 因此这里的失败处理只有一条：`console.warn` **一次**（不是每次建链都刷屏），
 * 然后静默降级。真正的可观测交给服务端日志与 `chrome://webrtc-internals`。
 */

import { loadSession } from "./session";

const ENDPOINT = "/api/audio/ice-config";
/** 3s 是「建链前可以多等的上限」：超过就宁可无 TURN 建连，也不卡住通话 */
const TIMEOUT_MS = 3000;

interface IceConfigBody {
  ice_servers?: unknown;
}

/** 只在页面生命周期里 warn 一次：WS 重连会反复建链，逐次刷屏反而淹没真正的报错 */
let warned = false;

function warnOnce(message: string): void {
  if (warned) return;
  warned = true;
  console.warn(`[ice] ${message}（本轮降级为无参建连，跨网段可能不通）`);
}

/**
 * 单项白名单收窄：`urls` 必须是非空字符串（或字符串数组），其余键按类型取。
 * 响应体来自网络，即便服务端可靠也不把 `unknown` 直接塞进 `RTCIceServer`。
 */
function toIceServer(item: unknown): RTCIceServer | null {
  if (typeof item !== "object" || item === null) return null;
  const raw = item as { urls?: unknown; username?: unknown; credential?: unknown };

  const urls: string[] = Array.isArray(raw.urls)
    ? raw.urls.filter((u): u is string => typeof u === "string" && u.length > 0)
    : typeof raw.urls === "string" && raw.urls.length > 0
      ? [raw.urls]
      : [];
  if (urls.length === 0) return null;

  const server: RTCIceServer = { urls };
  if (typeof raw.username === "string") server.username = raw.username;
  if (typeof raw.credential === "string") server.credential = raw.credential;
  return server;
}

/**
 * 取本环境可用的 ICE 配置；任何失败都返回 `[]`（调用方无须 try/catch）。
 *
 * 返回值直接可填 `new RTCPeerConnection({ iceServers })`；空数组与不传参数等价。
 */
export async function loadIceServers(): Promise<RTCIceServer[]> {
  const token = loadSession()?.access_token;
  if (!token) {
    // 未登录（或会话已被清）时不该发请求：这一条属预期分支，不算失败告警
    return [];
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const resp = await fetch(ENDPOINT, {
      headers: { Authorization: `Bearer ${token}` },
      signal: controller.signal,
    });
    if (!resp.ok) {
      warnOnce(`ICE 配置接口返回 ${resp.status}`);
      return [];
    }
    const body = (await resp.json()) as IceConfigBody;
    const list = Array.isArray(body.ice_servers) ? body.ice_servers : [];
    const servers = list
      .map(toIceServer)
      .filter((s): s is RTCIceServer => s !== null);
    if (servers.length === 0) {
      // 服务端已定口径：TURN 未配置就只给 STUN，全空给 []。这里不是故障，但值得留痕。
      warnOnce("服务端下发的 ice_servers 为空");
    }
    return servers;
  } catch (err) {
    // AbortError（3s 超时）/ 网络错误 / JSON 解析失败都走这里
    warnOnce(`ICE 配置获取失败：${err instanceof Error ? err.message : String(err)}`);
    return [];
  } finally {
    clearTimeout(timer);
  }
}
