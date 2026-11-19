/**
 * 照护台认证 API 封装 —— **薄封装层**（task-05 F3 / 前端计划 W2）。
 *
 * 实现已收敛到 `shared/auth.ts`，本文件只做两件事：
 * ① 用**照护台专属前缀** `kb_console` 造一个客户端实例；
 * ② 保持原有对外 API 不变，业务模块与页面不需要跟着改。
 *
 * 契约唯一依据：app/api/auth.py + app/api/schemas/auth.py + docs/接口契约_身份与权限.md §E1–E3。
 * E1 实际端点是 `POST /api/sessions`（任务卡 §3 写的 `/api/sessions/login` 是笔误）。
 *
 * **口径变更（2026-11-15，产品负责人裁决 Q-1）**：原 task-05 §0「与 family/api.ts 独立同构、
 * 禁止互相 import」已放开。那条裁决的**目的**是「同源 localStorage 下双端不互踢登录态」，
 * 现在由**实例隔离**达成：每端一个 `createAuthClient`，各自持有自己的 storage 键、
 * refresh 单飞与 401 收口回调。两端同浏览器同时登录互不影响，且改一处两端都受益。
 * 收敛前两份是 86% 逐字相同，并已长出四类行为分叉（401 收口 / 错误分类 / 空响应体 / 超时）。
 *
 * **仍然禁止**：import `src/pad/**`（pad 用 sessionStorage + 设备凭证，是另一套范式）。
 */
import {
  ApiError,
  INVALID_CREDENTIAL_TEXT,
  NETWORK_ERROR_TEXT,
  createAuthClient,
  roleLabel,
  type ApiErrorKind,
  type AuthedInit,
  type CurrentIdentity,
  type Principal,
} from "../shared/auth";

export {
  ApiError,
  INVALID_CREDENTIAL_TEXT,
  NETWORK_ERROR_TEXT,
  roleLabel,
  type ApiErrorKind,
  type CurrentIdentity,
  type Principal,
};

/** 照护台专属实例：键前缀 `kb_console`，与家属端 `kb_family` 完全隔离。 */
const client = createAuthClient({ storagePrefix: "kb_console" });

export const clearSession = client.clearSession;
export const hasSession = client.hasSession;
export const cachedProfile = client.cachedProfile;
export const setUnauthorizedHandler = client.setUnauthorizedHandler;
export const logout = client.logout;
export const login = client.login;
export const fetchCurrent = client.fetchCurrent;

/** 带鉴权请求：access 过期自动 refresh 后重放一次；默认 30s 超时，可按请求覆盖。 */
export function authedFetch(path: string, init: AuthedInit = {}): Promise<Response> {
  return client.authedFetch(path, init);
}