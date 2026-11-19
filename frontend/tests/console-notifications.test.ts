/**
 * console「消息中心」数据层（task-41 / 批A）门禁用例。
 *
 * 三件事各有用例：
 * 1. `canResend` 叠了角色——后端 `notification_service.RESEND_ROLES = {admin, care_worker}`，
 *    医生点重发必 403，所以按钮对医生不该出现；
 * 2. `canResendStatus` 只看状态——页面拿它判「终态」，混用会把「医生看一条可重发的通知」
 *    误标成终态；
 * 3. `CODE_TEXT.invalid_transition`——没有这条，409 会落到 NETWORK 显示「网络异常」，
 *    把「状态不对」说成断网。
 *
 * 删掉什么会变红（反向验收口径）：
 * 1. `canResend` 去掉角色判断 → 医生用例红；
 * 2. 把 `!RESEND_ROLES.includes(role)` 写成 `RESEND_ROLES.includes(role)` → admin 用例红；
 * 3. 删掉 `CODE_TEXT.invalid_transition` → 文案用例红（会取到网络异常兜底）。
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { HttpError, errorText } from "../src/console/http";
import { canResend, canResendStatus } from "../src/console/notifications";

const here = dirname(fileURLToPath(import.meta.url));

type Role = "admin" | "care_worker" | "doctor";

function principal(role: Role) {
  return { kind: "staff" as const, id: `staff-${role}`, role, name: role };
}

/** 把登录态写进最小 localStorage 桩，让 `cachedProfile()` 读到指定角色。 */
function loginAs(role: Role | null) {
  const store = new Map<string, string>();
  if (role) {
    store.set("kb_console_access_token", "test-access-token");
    store.set("kb_console_profile", JSON.stringify(principal(role)));
  }
  const stub = {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => void store.set(key, value),
    removeItem: (key: string) => void store.delete(key),
    clear: () => store.clear(),
    key: (index: number) => Array.from(store.keys())[index] ?? null,
    get length() {
      return store.size;
    },
  };
  Object.defineProperty(globalThis, "localStorage", { value: stub, configurable: true });
}

test("canResend：admin 与 care_worker 对可重发三态放行", () => {
  for (const role of ["admin", "care_worker"] as const) {
    for (const status of ["generated", "pending", "delivered"]) {
      assert.equal(canResend(status, principal(role)), true, `${role} + ${status}`);
    }
  }
});

test("canResend：医生无权重发（后端 RESEND_ROLES 不含 doctor）", () => {
  for (const status of ["generated", "pending", "delivered"]) {
    assert.equal(canResend(status, principal("doctor")), false, `doctor + ${status}`);
  }
});

test("canResend：终态与未知状态对谁都不放行", () => {
  for (const status of ["acknowledged", "failed", "simulated", "", "weird"]) {
    assert.equal(canResend(status, principal("admin")), false, `admin + ${status}`);
  }
});

test("canResend：未登录（principal 为 null）按无权处理", () => {
  assert.equal(canResend("generated", null), false);
});

test("canResend：角色缺省时取登录态 principal（同一份缓存）", () => {
  loginAs("doctor");
  assert.equal(canResend("generated"), false, "医生登录态下不该出现重发按钮");
  loginAs("care_worker");
  assert.equal(canResend("generated"), true, "护工登录态下应出现重发按钮");
  loginAs(null);
  assert.equal(canResend("generated"), false, "无登录态时判不了角色，按无权处理");
});

test("canResendStatus：只看状态，与角色无关（页面用它判终态）", () => {
  assert.equal(canResendStatus("generated"), true);
  assert.equal(canResendStatus("acknowledged"), false);
  assert.equal(canResendStatus("failed"), false);
});

test("invalid_transition 的 409 不再显示成网络异常", () => {
  const text = errorText(new HttpError(409, "invalid_transition", "通知当前状态不允许重发"));
  assert.equal(text, "这条消息已被处理过，刷新看看最新状态");
  // 对照：没有 code 的 409 仍走 CONFLICT 文案（别把通用冲突也改掉）
  assert.equal(errorText(new HttpError(409, "CONFLICT", "conflict")), "与现有数据冲突，请检查后重试");
});

test("源码契约：页面的「终态」判定用 canResendStatus，不用带角色的 canResend", () => {
  const source = readFileSync(resolve(here, "../src/console/pages/NotificationsPage.tsx"), "utf8");
  const terminalLine = source
    .split("\n")
    .find((line) => line.includes("canDeliver(row.status) && !canAck(row.status)"));
  assert.ok(terminalLine, "页面里应保留终态判定那一行");
  assert.match(terminalLine!, /canResendStatus\(row\.status\)/);
});
