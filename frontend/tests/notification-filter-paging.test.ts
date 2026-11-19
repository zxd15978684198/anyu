/**
 * W1 止血缺陷的回归契约（2026-11-13，前端计划 §1.3 A1–A4 + 新发现 A5）。
 *
 * 五条都是「链路走不通 / 边界被破」，不是审美问题：
 * - A1 家属端筛选只在已加载页内做，红档在后续页时被显示成「暂时没有需要您关注的事项」；
 * - A2 家属端客户端按 created_at 重排，抹掉服务端 severity_rank（红→黄→绿）优先级；
 * - A3 照护台 access+refresh 都失效后不回登录页，视图停在 app 后续请求裸奔 401；
 * - A4 照护台把 refresh 抛的 ApiError("invalid") 吞成 HttpError(0,"NETWORK")；
 * - A5 照护台严重度下拉是死控件：前端传 severity，后端 list_for_staff 根本不认这个参数。
 *
 * A1/A5 的正确修法是**把 severity 下推到服务端、在分页之前过滤**，而不是客户端凑页：
 * 下推后 total 即筛选后总数，「加载更多」与空态判据自然正确。
 *
 * 沿用本仓测试口径：源码契约断言，不引 vitest/jsdom。真行为由
 * `tests/test_notifications_severity_filter.py` 在真库上断言。
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";

const src = (rel: string): string =>
  readFileSync(new URL(`../src/${rel}`, import.meta.url), "utf8");
const repo = (rel: string): string =>
  readFileSync(new URL(`../../${rel}`, import.meta.url), "utf8");

// ------------------------------------------------------- A1/A5 severity 下推服务端

describe("A1/A5 · severity 必须在服务端分页前过滤", () => {
  it("服务层 list_for_staff 接受 severity 形参", () => {
    const text = repo("app/services/notification_service.py");
    const sig = text.slice(
      text.indexOf("async def list_for_staff"),
      text.indexOf(") -> NotificationPage:", text.indexOf("async def list_for_staff")),
    );
    assert.equal(
      /severity:\s*str\s*\|\s*None\s*=\s*None/.test(sig),
      true,
      "期望：list_for_staff 收 severity。现在：照护台传了后端不认，下拉框是死控件",
    );
  });

  it("服务层校验 severity 闭集，非法值抛 NotificationValidationError", () => {
    const text = repo("app/services/notification_service.py");
    assert.equal(
      /if severity is not None and severity not in NOTIFICATION_SEVERITIES:/.test(text),
      true,
      "期望：与 status/kind 同样做闭集校验，不静默忽略非法值",
    );
  });

  it("severity 进 filters（与 count 共用），不是分页后再筛", () => {
    const text = repo("app/services/notification_service.py");
    const body = text.slice(text.indexOf("async def list_for_staff"));
    const filtersAt = body.indexOf("filters.append(Notification.severity == severity)");
    const countAt = body.indexOf("select(func.count())");
    assert.equal(filtersAt > 0, true, "期望：severity 进 filters");
    assert.equal(
      filtersAt < countAt,
      true,
      "期望：severity 必须在 count 之前进 filters，否则 total 是未筛总数，前端判据全部失真",
    );
  });

  it("API 端点声明 severity 查询参数并透传", () => {
    const text = repo("app/api/notifications.py");
    const body = text.slice(
      text.indexOf("async def list_notifications"),
      text.indexOf("async def", text.indexOf("async def list_notifications") + 10),
    );
    assert.equal(/severity:\s*Annotated\[str \| None, Query\(/.test(body), true);
    assert.equal(/severity=severity,/.test(body), true);
  });
});

describe("A1 · 家属端把筛选交给服务端，不再客户端凑页", () => {
  it("数据层 listNotifications 接受并下发 severity", () => {
    const text = src("family/notifications.ts");
    assert.equal(/severity\?:\s*NotificationSeverity \| "";/.test(text), true);
    assert.equal(/severity:\s*params\.severity \|\| undefined,/.test(text), true);
  });

  it("页面把 filter 传给服务端，且 filter 变化时回到第 1 页", () => {
    const text = src("family/pages/NotificationsPage.tsx");
    assert.equal(
      /severity:\s*filter === "all" \? "" : filter,/.test(text),
      true,
      "期望：筛选下推。现在：只筛已加载页会把后续页的红档说成不存在",
    );
    assert.equal(/\}, \[page, attempt, filter\]\);/.test(text), true);
    assert.equal(
      /function changeFilter[\s\S]*?setFilter\(next\);\s*setPage\(1\);/.test(text),
      true,
      "期望：换筛选必须重置页码，否则第 3 页配新筛选会漏掉前两页",
    );
  });

  it("不再保留客户端 severity 过滤", () => {
    const text = src("family/pages/NotificationsPage.tsx");
    assert.equal(
      /items\.filter\(\(entry\) => severityOf\(entry\) === filter\)/.test(text),
      false,
      "期望：删掉客户端过滤，避免与服务端筛选双重口径",
    );
  });
});

// ---------------------------------------------------------------- A2 排序权

describe("A2 · 家属端不得客户端重排（排序权在服务端 severity_rank）", () => {
  it("family/notifications.ts 不再对 created_at 做 sort", () => {
    const text = src("family/notifications.ts");
    assert.equal(
      /sort\(\s*\(a,\s*b\)\s*=>\s*b\.created_at/.test(text),
      false,
      "期望：删掉客户端重排。现在：按 created_at 重排会抹掉红→黄→绿优先级，且与分页口径打架",
    );
  });

  it("数据层注释写明排序权归服务端（与 console 同口径）", () => {
    assert.equal(
      src("family/notifications.ts").includes("severity_rank"),
      true,
      "期望：注释指向服务端 severity_rank，避免后人再加回客户端排序",
    );
  });
});

// ---------------------------------------------------------------- A3 401 收口

describe("A3 · 照护台 401 收口回登录页", () => {
  // 断言随结构更新（前端计划 R-3）：W2 把实现收敛到 shared/auth.ts，
  // 判据跟着搬到真正的实现处，并额外钉住「console 确实委托过去」，不是放宽。
  it("shared/auth.ts 提供 setUnauthorizedHandler（每端实例各自持有）", () => {
    assert.equal(
      /setUnauthorizedHandler:\s*\(handler\)\s*=>\s*\{/.test(src("shared/auth.ts")),
      true,
      "期望：工厂返回的客户端带收口回调注册点",
    );
  });

  it("authedFetch 在 refresh 后仍 401 时清态并触发回调", () => {
    const text = src("shared/auth.ts");
    const body = text.slice(text.indexOf("const authedFetch = async"));
    assert.equal(
      /if \(res\.status === 401\) \{\s*clearSession\(\);\s*unauthorizedHandler\?\.\(\);/.test(body),
      true,
      "期望：401 收口清态 + 回调。没有它视图会停在 app、后续裸奔 401",
    );
  });

  it("console/api.ts 把收口注册点委托给共享实例（前缀 kb_console）", () => {
    const text = src("console/api.ts");
    assert.equal(text.includes("export const setUnauthorizedHandler = client.setUnauthorizedHandler"), true);
    assert.equal(/storagePrefix:\s*"kb_console"/.test(text), true);
  });

  it("console/App.tsx 订阅收口并切回登录视图", () => {
    const text = src("console/App.tsx");
    assert.equal(text.includes("setUnauthorizedHandler"), true);
    assert.equal(
      /setUnauthorizedHandler\(\(\)\s*=>\s*\{[\s\S]*?setView\("login"\)/.test(text),
      true,
      "期望：回调里把视图切回 login",
    );
  });
});

// ---------------------------------------------------------------- A4 错误分类

describe("A4 · 登录失效不得被吞成网络异常", () => {
  it("shared/http.ts 的 send 区分 ApiError invalid 与网络失败", () => {
    const text = src("shared/http.ts");
    assert.equal(text.includes("ApiError"), true);
    assert.equal(
      /err instanceof ApiError && err\.kind === "invalid"/.test(text),
      true,
      "期望：只有 invalid 判登录失效，其余按网络失败（否则断网会把用户踹回登录页）",
    );
  });

  it("不再无条件吞成 NETWORK", () => {
    const text = src("shared/http.ts");
    const send = text.slice(text.indexOf("const send = async"), text.indexOf("const settle = async"));
    assert.equal(
      /catch\s*\{\s*throw new HttpError\(0, "NETWORK", NETWORK_TEXT\);\s*\}/.test(send),
      false,
      "期望：catch 必须分类。原缺陷是把登录失效一律显示成「网络异常」",
    );
  });

  it("console/http.ts 委托共享机制，且保留照护台自己的用语", () => {
    const text = src("console/http.ts");
    assert.equal(text.includes("createHttpClient"), true);
    assert.equal(text.includes("当前角色无权执行该操作"), true, "照护台用语不得被家属端覆盖");
    assert.equal(text.includes("allowEmptyBody: true"), true, "写操作端必须允许 204 空体");
  });
});