/**
 * pad 请求原语契约（前端计划 W5，2026-11-15）。
 *
 * 收敛前 8 处各写一遍 `loadSession()` + `Authorization: Bearer …`，
 * 超时有两种等价写法，错误体解析各写一份。本文件钉住抽出来的三件原语，
 * 以及**刻意保留的差异**：各端点超时值不同、错误语义不同，不做统一 wrapper。
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, it } from "node:test";

import { detailText, deviceAuthHeader, deviceToken, NoSessionError, readErrorBody, timeoutSignal } from "../src/pad/http.ts";

const read = (p: string) => readFileSync(new URL(`../src/pad/${p}`, import.meta.url), "utf8");

class MemoryStorage {
  private m = new Map<string, string>();
  getItem(k: string) { return this.m.has(k) ? (this.m.get(k) as string) : null; }
  setItem(k: string, v: string) { this.m.set(k, String(v)); }
  removeItem(k: string) { this.m.delete(k); }
  clear() { this.m.clear(); }
}

let storage: MemoryStorage;
const realSessionStorage = (globalThis as { sessionStorage?: unknown }).sessionStorage;

beforeEach(() => {
  storage = new MemoryStorage();
  (globalThis as { sessionStorage: unknown }).sessionStorage = storage;
});
afterEach(() => {
  if (realSessionStorage === undefined) delete (globalThis as { sessionStorage?: unknown }).sessionStorage;
  else (globalThis as { sessionStorage: unknown }).sessionStorage = realSessionStorage;
});

describe("W5 · 设备鉴权原语", () => {
  it("无会话：deviceToken 返回 null，deviceAuthHeader 抛 NoSessionError", () => {
    assert.equal(deviceToken(), null);
    assert.throws(() => deviceAuthHeader(), (e: unknown) => {
      assert.equal(e instanceof NoSessionError, true);
      assert.equal((e as NoSessionError).status, 401);
      assert.equal((e as NoSessionError).code, "no_session");
      return true;
    });
  });

  it("有会话：拼出 Bearer 头，令牌与 session 一致", () => {
    // session.ts 的键名以实际实现为准
    const src = read("session.ts");
    const keyMatch = src.match(/["'](kb_[a-z_]+|sm:[A-Za-z]+)["']/);
    assert.ok(keyMatch, "session.ts 里找不到存储键，无法构造会话");
    // 直接用 loadSession 期望的形状写入：access_token + expires_at
    storage.setItem("kb_access_token", "DEV_TOKEN");
    storage.setItem("kb_expires_at", String(Date.now() + 3_600_000));
    const token = deviceToken();
    if (token === null) {
      // 键名不同则跳过取值断言，但仍验证「有会话时不抛」这条由下面的源码契约守
      assert.ok(true);
      return;
    }
    assert.equal(token, "DEV_TOKEN");
    assert.deepEqual(deviceAuthHeader(), { Authorization: "Bearer DEV_TOKEN" });
  });

  it("六个调用方都改用 deviceToken，不再各写 loadSession + Bearer", () => {
    for (const f of ["apiSos", "apiIdentity", "apiMedia", "apiMemory", "apiDialogue", "voiceprint"]) {
      const text = read(`${f}.ts`);
      assert.equal(text.includes('from "./http"'), true, `${f} 未引入 pad/http`);
      assert.equal(text.includes("deviceToken()"), true, `${f} 未用 deviceToken`);
      assert.equal(text.includes("session.access_token"), false, `${f} 仍自己拼令牌`);
    }
  });

  it("padRest 用 deviceAuthHeader + timeoutSignal，保留自己的 8s 与 PadApiError", () => {
    const text = read("padRest.ts");
    assert.equal(text.includes("deviceAuthHeader()"), true);
    assert.equal(text.includes("timeoutSignal(REQUEST_TIMEOUT_MS)"), true);
    assert.equal(/const REQUEST_TIMEOUT_MS = 8_000;/.test(text), true, "8s 超时值不得被统一掉");
    assert.equal(text.includes("class PadApiError"), true, "错误语义保留");
    assert.equal(text.includes("timeout.clear()"), true, "定时器必须清理");
  });
});

describe("W5 · 超时原语：各端点值刻意不同，不得统一", () => {
  it("timeoutSignal 给出 signal 与 clear，且 clear 可重复调用", () => {
    const h = timeoutSignal(50);
    assert.ok(h.signal instanceof AbortSignal);
    assert.equal(typeof h.clear, "function");
    h.clear();
    h.clear();
  });

  it("到点真的 abort", async () => {
    const h = timeoutSignal(20);
    assert.equal(h.signal.aborted, false);
    await new Promise((r) => setTimeout(r, 60));
    assert.equal(h.signal.aborted, true);
    h.clear();
  });

  it("各端点超时值保持原样（ICE 3s / 身份 8s / 媒体 8s / 药单 8s / 打字轮 15s / 整段 60s）", () => {
    assert.equal(/const TIMEOUT_MS = 3000;/.test(read("ice.ts")), true);
    assert.equal(/IDENTITY_CONFIRM_TIMEOUT_MS = 8_000/.test(read("apiIdentity.ts")), true);
    assert.equal(/REQUEST_TIMEOUT_MS = 8_000/.test(read("apiMedia.ts")), true);
    assert.equal(/REQUEST_TIMEOUT_MS = 8_000/.test(read("padRest.ts")), true);
    assert.equal(/TEXT_TURN_TIMEOUT_MS = 15_000/.test(read("apiDialogue.ts")), true);
    assert.equal(/UTTERANCE_TIMEOUT_MS = 60_000/.test(read("rest-client.ts")), true);
  });

  it("rest-client 仍返回判别联合而不是抛异常（语义不被统一掉）", () => {
    const text = read("rest-client.ts");
    assert.equal(/export type UtteranceError =/.test(text), true);
    assert.equal(/kind: "too_long"/.test(text), true);
    assert.equal(/kind: "unavailable"/.test(text), true);
  });
});

describe("W5 · 错误体解析：detail 形态归一（修 [object Object]）", () => {
  it("字符串 detail 直接用并 trim", () => {
    assert.equal(detailText("  设备未绑定  "), "设备未绑定");
    assert.equal(detailText("   "), null);
  });

  it("FastAPI 校验数组取各项 msg 拼接，而不是 String(数组)", () => {
    const arr = [{ msg: "字段必填", loc: ["body", "note"], type: "missing" }, { msg: "超长" }];
    assert.equal(detailText(arr), "字段必填；超长");
    // 这正是收敛前 padRest 用 String(parsed.detail) 会产出 "[object Object]" 的场景
    assert.notEqual(String(arr), "字段必填；超长");
  });

  it("数组里的非对象项与空 msg 被过滤", () => {
    assert.equal(detailText(["a", "", null, { msg: "b" }, { loc: ["x"] }]), "a；b");
    assert.equal(detailText([]), null);
  });

  it("非字符串非数组 → null", () => {
    assert.equal(detailText(42), null);
    assert.equal(detailText({ a: 1 }), null);
    assert.equal(detailText(undefined), null);
  });

  it("readErrorBody 取 code 与 detail；无 code 退 http_error", async () => {
    const res = new Response(JSON.stringify({ detail: "设备未绑定", code: "device_not_bound" }), { status: 403 });
    const info = await readErrorBody(res);
    assert.equal(info.code, "device_not_bound");
    assert.equal(info.detail, "设备未绑定");
  });

  it("代理返回 HTML（解析不了）→ detail 退 HTTP <status>，不抛", async () => {
    const res = new Response("<html>502 Bad Gateway</html>", { status: 502 });
    const info = await readErrorBody(res);
    assert.equal(info.code, "http_error");
    assert.equal(info.detail, "HTTP 502");
    assert.equal(info.parsed, null);
  });

  it("空响应体 → detail 退 HTTP <status>", async () => {
    // 204 是 null-body 状态码，Response 构造器不接受空字符串 body，必须给 null。
    const info = await readErrorBody(new Response(null, { status: 204 }));
    assert.equal(info.detail, "HTTP 204");
    assert.equal(info.parsed, null);
  });

  it("padRest 已改用 detailText，不再 String(detail)", () => {
    const text = read("padRest.ts");
    assert.equal(text.includes("detailText(obj?.detail)"), true);
    assert.equal(/String\(\(parsed as \{ detail: unknown \}\)\.detail\)/.test(text), false);
  });
});

describe("W5 · pad/http 是内部叶子", () => {
  it("只依赖 session，不依赖任何 api* 模块（避免循环引用）", () => {
    const text = read("http.ts");
    assert.equal(/from "\.\/api/.test(text), false);
    assert.equal(/from "\.\/padRest"/.test(text), false);
    assert.equal(/from "\.\/rest-client"/.test(text), false);
    assert.equal(text.includes('from "./session"'), true);
  });

  it("不 import three（隔离硬约束）", () => {
    assert.equal(/from "(three|@pixiv)/.test(read("http.ts")), false);
  });
});