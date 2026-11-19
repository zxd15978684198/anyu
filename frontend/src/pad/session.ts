/**
 * 设备会话存取（E4）：token 只存 sessionStorage，不进 localStorage（防其他页面读取）。
 *
 * 2026-11-11 P0-2：**设备编号单独持久化到 localStorage**（`sm:lastDeviceId`）。
 * 原来 `clearSession()` 把 `kb_device` 一起删掉，退出/过期后老人想走「声纹登录：说一句话就进去」，
 * 必须先手打一串 36 位 UUID —— 这条路等于不可用。设备编号不是凭证（换证还要设备密钥或声纹），
 * 留在本机只是让下一次少打字；令牌与长者信息仍然只活到会话结束。
 */

const TOKEN_KEY = "kb_access_token";
const EXPIRES_KEY = "kb_expires_at";
const DEVICE_KEY = "kb_device";
/** 上次登录用过的设备编号（跨会话保留；清会话不清它） */
const LAST_DEVICE_KEY = "sm:lastDeviceId";

export interface DeviceSession {
  access_token: string;
  /** 毫秒时间戳 */
  expires_at: number;
  device: { id: string; room_id: string; elder_id: string | null };
}

/**
 * 隐私模式 / 老浏览器 / 被策略禁用时 `localStorage` 取不到。
 * 这些环境里「记住设备编号」只是少一个便捷入口，绝不能因此把登录页弄崩。
 */
function localStore(): Storage | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}

/** 记住这台平板上次登录用的设备编号（登录成功时自动调用；也可手动补）。 */
export function saveLastDeviceId(deviceId: string): void {
  const value = deviceId.trim();
  if (value === "") return;
  try {
    localStore()?.setItem(LAST_DEVICE_KEY, value);
  } catch {
    /* 写不进去就算了：下次手动输 */
  }
}

/** 读上次的设备编号；没有（或存储不可用/被清）返回 null，调用方据此保持原样。 */
export function readLastDeviceId(): string | null {
  try {
    const value = localStore()?.getItem(LAST_DEVICE_KEY);
    return value !== null && value !== undefined && value.trim() !== "" ? value.trim() : null;
  } catch {
    return null;
  }
}

/** 显式忘掉设备编号（换机器、设备报废时用；登录流程本身不清它）。 */
export function forgetLastDeviceId(): void {
  try {
    localStore()?.removeItem(LAST_DEVICE_KEY);
  } catch {
    /* 同上 */
  }
}

export function saveSession(s: DeviceSession): void {
  sessionStorage.setItem(TOKEN_KEY, s.access_token);
  sessionStorage.setItem(EXPIRES_KEY, String(s.expires_at));
  sessionStorage.setItem(DEVICE_KEY, JSON.stringify(s.device));
  // 顺手记住设备编号：声纹登录下次就不必再打一遍 UUID
  saveLastDeviceId(s.device.id);
}

export function loadSession(): DeviceSession | null {
  const token = sessionStorage.getItem(TOKEN_KEY);
  const expiresAt = sessionStorage.getItem(EXPIRES_KEY);
  const device = sessionStorage.getItem(DEVICE_KEY);
  if (!token || !expiresAt || !device) return null;
  return {
    access_token: token,
    expires_at: Number(expiresAt),
    device: JSON.parse(device),
  };
}

/** 会话有效 = 有 token 且未过期（24h 设备会话）；无 refresh 通道则过期回登录页 */
export function sessionValid(): boolean {
  const s = loadSession();
  return s !== null && s.expires_at > Date.now();
}

/**
 * 退出登录：只清令牌与长者信息，**保留设备编号**。
 * 这条是 P0-2 的关键——清掉设备编号，声纹登录就要先打 UUID，长者根本走不到那一步。
 */
export function clearSession(): void {
  sessionStorage.removeItem(TOKEN_KEY);
  sessionStorage.removeItem(EXPIRES_KEY);
  sessionStorage.removeItem(DEVICE_KEY);
}
