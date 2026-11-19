/**
 * 家属端业务请求层 —— **薄封装层**（W4 / 前端计划 W2）。
 *
 * 机制已收敛到 `shared/http.ts`，本文件只保留**家属该听到的那套用语**与对外 API。
 *
 * 用语刻意不与照护台统一（产品负责人 Q-3 裁决：不同业务场景用不同用语，不同表述不是冲突）。
 * 三条口径不变：
 * 1. 401 → 登录态已由 authedFetch 清掉，这里只给「登录已失效」；
 * 2. 403 → 固定「没有权限查看」，不能说「未找到」——后者会误导家属以为数据丢了；
 * 3. 网络层失败 → status=0，页面据此渲染「网络不太好，点这里重试」。
 *
 * **只读端**：`allowEmptyBody: false`。家属端只有 GET，成功响应拿到空体确实是异常，
 * 要报 BAD_BODY 而不是静默当成功（照护台有写操作会返回 204，那边必须允许）。
 */
import { authedFetch } from "./api";
import { HttpError, createHttpClient, query } from "../shared/http";

export { HttpError, query };

export const NETWORK_TEXT = "网络不太好，请稍后重试";
export const FORBIDDEN_TEXT = "没有权限查看";
export const EXPIRED_TEXT = "登录已失效，请重新登录";
export const SERVER_TEXT = "服务暂时不可用，请稍后重试";

function messageOfStatus(status: number): string {
  if (status === 401) return EXPIRED_TEXT;
  if (status === 403) return FORBIDDEN_TEXT;
  if (status === 404) return "没有找到对应内容";
  if (status >= 500) return SERVER_TEXT;
  return NETWORK_TEXT;
}

const client = createHttpClient({
  authedFetch,
  networkText: NETWORK_TEXT,
  messageOf: (status, code) => (code === "BAD_BODY" ? SERVER_TEXT : messageOfStatus(status)),
  allowEmptyBody: false,
  fallbackCode: (status) => `HTTP_${String(status)}`,
});

export const apiGet = client.apiGet;
export const errorText = client.errorText;
export const isNetworkError = client.isNetworkError;
export const isNotFound = client.isNotFound;