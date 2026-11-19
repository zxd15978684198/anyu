/**
 * 照护台业务请求层 —— **薄封装层**（W1-5 / 前端计划 W2）。
 *
 * 机制已收敛到 `shared/http.ts`（错误分类、空响应体、查询串、错误外壳），
 * 本文件只保留**照护台角色该听到的那套用语**与对外 API。
 *
 * 用语刻意不与家属端统一（产品负责人 Q-3 裁决：不同业务场景用不同用语，不同表述不是冲突）：
 * 护工看「当前角色无权执行该操作」，家属看「没有权限查看」——两个角色该听到不同的话。
 *
 * 契约依据：docs/接口契约_身份与权限.md §7「错误码与响应体」（{detail, code, trace_id}）。
 * 按 code 分支出中文，不解析 detail（那是给排障看的英文短句）。
 */
import { authedFetch } from "./api";
import { HttpError, createHttpClient, query } from "../shared/http";

export { HttpError, query };

/** 照护台文案表：按后端 code 取词，未知 code 退到网络文案。 */
const CODE_TEXT: Record<string, string> = {
  ROLE_FORBIDDEN: "当前角色无权执行该操作",
  SCOPE_FORBIDDEN: "该长者不在您的负责范围内",
  NOT_FOUND: "没有找到对应记录，可能已被删除",
  CONFLICT: "与现有数据冲突，请检查后重试",
  // 状态机拒绝（409）：通知重发/撤销打在不允许的源状态上。没有这一条会落到 NETWORK，
  // 把「状态不对」显示成「网络异常」。
  invalid_transition: "这条消息已被处理过，刷新看看最新状态",
  VALIDATION_ERROR: "填写内容不合法，请检查必填项与格式",
  BAD_REQUEST: "请求格式不正确",
  TOKEN_INVALID: "登录已失效，请重新登录",
  TOKEN_EXPIRED: "登录已失效，请重新登录",
  TOKEN_REVOKED: "登录已失效，请重新登录",
  INTERNAL_ERROR: "服务出错了，请稍后重试",
  NETWORK: "网络异常，请稍后重试",
};

export const NETWORK_TEXT = CODE_TEXT.NETWORK;

const client = createHttpClient({
  authedFetch,
  networkText: NETWORK_TEXT,
  messageOf: (_status, code) => CODE_TEXT[code] ?? NETWORK_TEXT,
  // 照护台有写操作，后端会返回 204/205 与 content-length:0，必须当成功。
  allowEmptyBody: true,
  fallbackCode: () => "BAD_REQUEST",
});

export const apiGet = client.apiGet;
export const apiSend = client.apiSend;
export const errorText = client.errorText;
export const isNetworkError = client.isNetworkError;
export const isNotFound = client.isNotFound;