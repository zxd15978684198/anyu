/**
 * 声纹免登录「可达性」的守卫（P0-2，2026-11-11 审计）。
 *
 * 问题：`clearSession()` 原来把设备编号（`kb_device`）一起删了，而声纹登录又要求先填
 * 36 位设备编号 → 退出/过期后老人想「说一句话就进去」，必须先打一串 UUID，等于不可用。
 * 修法：设备编号单独持久化在 `localStorage`（`sm:lastDeviceId`），清会话不清它，
 * 登录页在它存在时给一个「上次用的设备：…106（点这里直接用）」的一键入口。
 *
 * Node 里没有 `localStorage`（本仓库也不许引 jsdom），所以这里装一个最小 Storage 假实现；
 * session.ts 对「存储不可用」是 try/catch 兜底的，最后一条用例就把全局删掉验证不崩。
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { after, test } from "node:test";

/** 最小 Storage 实现：够 session.ts 用（get/set/remove + length/key）。 */
class MemoryStorage implements Storage {
  private data = new Map<string, string>();

  get length(): number {
    return this.data.size;
  }

  clear(): void {
    this.data.clear();
  }

  getItem(key: string): string | null {
    return this.data.has(key) ? (this.data.get(key) as string) : null;
  }

  key(index: number): string | null {
    return Array.from(this.data.keys())[index] ?? null;
  }

  removeItem(key: string): void {
    this.data.delete(key);
  }

  setItem(key: string, value: string): void {
    this.data.set(key, String(value));
  }
}

const local = new MemoryStorage();
const sessioned = new MemoryStorage();
(globalThis as { localStorage?: Storage }).localStorage = local;
(globalThis as { sessionStorage?: Storage }).sessionStorage = sessioned;

const store = await import("../src/pad/session");

const DEVICE_ID = "11111111-1111-4111-8111-111111111106";

after(() => {
  delete (globalThis as { localStorage?: Storage }).localStorage;
  delete (globalThis as { sessionStorage?: Storage }).sessionStorage;
});

test("设备编号能存能读，且两端都去掉首尾空白", () => {
  local.clear();
  store.saveLastDeviceId(`  ${DEVICE_ID}  `);
  assert.equal(store.readLastDeviceId(), DEVICE_ID);
});

test("空串不覆盖已有值（误触保存不该把好编号抹掉）", () => {
  local.clear();
  store.saveLastDeviceId(DEVICE_ID);
  store.saveLastDeviceId("   ");
  assert.equal(store.readLastDeviceId(), DEVICE_ID);
});

test("saveSession 顺手记住设备编号，clearSession 之后它还在（P0-2 的核心）", () => {
  local.clear();
  sessioned.clear();
  store.saveSession({
    access_token: "token-1",
    expires_at: Date.now() + 86_400_000,
    device: { id: DEVICE_ID, room_id: "room-1", elder_id: null },
  });

  assert.equal(store.readLastDeviceId(), DEVICE_ID, "登录成功时没把设备编号留下");
  assert.equal(store.loadSession()?.device.id, DEVICE_ID);

  store.clearSession();

  assert.equal(store.loadSession(), null, "退出后令牌必须清掉");
  assert.equal(store.readLastDeviceId(), DEVICE_ID, "退出把设备编号也删了：声纹登录又要先打 36 位 UUID");
});

test("forgetLastDeviceId 能显式忘掉（换机器/设备报废）", () => {
  local.clear();
  store.saveLastDeviceId(DEVICE_ID);
  store.forgetLastDeviceId();
  assert.equal(store.readLastDeviceId(), null);
});

test("存储不可用时不抛异常（隐私模式/禁用 localStorage 也要能登录）", () => {
  delete (globalThis as { localStorage?: Storage }).localStorage;
  try {
    assert.doesNotThrow(() => store.saveLastDeviceId(DEVICE_ID));
    assert.equal(store.readLastDeviceId(), null);
    assert.doesNotThrow(() => store.forgetLastDeviceId());
  } finally {
    (globalThis as { localStorage?: Storage }).localStorage = local;
  }
});

const SESSION_SRC = readFileSync(
  fileURLToPath(new URL("../src/pad/session.ts", import.meta.url)),
  "utf8",
);
const LOGIN_SRC = readFileSync(
  fileURLToPath(new URL("../src/pad/pages/LoginPage.tsx", import.meta.url)),
  "utf8",
);
const stripComments = (source: string): string =>
  source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");

test("源码契约：独立持久键在 localStorage，且 clearSession 里不许出现它", () => {
  assert.ok(SESSION_SRC.includes('"sm:lastDeviceId"'), "持久键不是 sm:lastDeviceId");
  const clearBody = SESSION_SRC.slice(SESSION_SRC.indexOf("export function clearSession"));
  assert.equal(
    clearBody.includes("LAST_DEVICE_KEY"),
    false,
    "clearSession 里又动设备编号了：退出后声纹登录会重新要求手输 UUID",
  );
});

test("源码契约：登录页有一键带回入口，并把焦点送到「登录」", () => {
  const code = stripComments(LOGIN_SRC);
  assert.ok(code.includes("readLastDeviceId"), "登录页没读上次的设备编号");
  assert.ok(code.includes("上次用的设备："), "缺「上次用的设备」提示");
  assert.ok(code.includes("点这里直接用"), "缺一键带回的指引");
  // 2026-11-13 取消声纹免登录：焦点目标从声纹按钮改成「登录」按钮
  assert.ok(code.includes("submitRef.current?.focus()"), "带回之后没有把焦点送到「登录」");
  assert.ok(code.includes('data-testid="use-last-device"'), "一键入口要有可测锚点");
});

test("源码契约：只显示后 3 位（公共平板上不摊开完整设备号）", () => {
  assert.ok(LOGIN_SRC.includes("lastDeviceId.slice(-3)"), "把完整设备编号打在屏幕上了");
});

test("源码契约：声纹免登录已取消，且没有新增网络请求", () => {
  const code = stripComments(LOGIN_SRC);
  // 2026-11-13 产品负责人裁决：登录页只留「设备编号 + 密钥」一条路
  assert.equal(code.includes("声纹登录"), false, "声纹登录入口又回来了");
  assert.equal(code.includes("voiceprintLogin"), false, "登录页又接了声纹登录的 API");
  assert.equal(/\bfetch\s*\(/.test(code), false, "登录页里出现了 fetch：网络出口应当只在 api/");
});
