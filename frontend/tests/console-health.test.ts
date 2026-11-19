/**
 * console「健康采集」数据层（task-41 / 批A）门禁用例。
 *
 * 为什么放在 `frontend/tests/`：`frontend/tsconfig.json` 的 include 只有 src、types 只有
 * vite/client，放进 src 会让 `tsc --noEmit` 找不到 `node:test` 的类型。
 *
 * 钉的是**请求体**：`POST /api/health/review/{id}/resolve` 在 FastAPI 侧的 `payload` 参数是
 * 必填（`app/api/health.py:432`），空 body 直接 422 `loc: ["body"]`。此前 `resolveHealthReview`
 * 不传 body，页面上「标记已处理」100% 失败，而这条链路的静态审计看不出来——所以必须有
 * 一条断言「body 里有 decision」的用例。
 *
 * 删掉什么会变红（反向验收口径）：
 * 1. `resolveHealthReview` 的第三个实参被删 → 请求体用例红；
 * 2. `decision` 写成别的取值 → 请求体用例红（服务端 Literal 会 422）；
 * 3. 改回直接 `fetch` 而不走 `apiSend` → 「带 Authorization」与「Content-Type」两例红。
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { HttpError } from "../src/console/http";
import { resolveHealthReview } from "../src/console/health";

const here = dirname(fileURLToPath(import.meta.url));

interface Stored {
  store: Map<string, string>;
}

/** 最小 localStorage 桩：`api.ts` 只用到 getItem / setItem / removeItem。 */
function installStorage(prefix: string): Stored {
  const store = new Map<string, string>();
  store.set(`${prefix}_access_token`, "test-access-token");
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
  return { store };
}

interface Captured {
  url: string;
  init: RequestInit;
}

/** 打桩 `fetch`：记录一次请求，按 `status` 返回响应。 */
function installFetch(status: number, body?: string): Captured[] {
  const calls: Captured[] = [];
  const stub = async (input: RequestInfo | URL, init: RequestInit = {}) => {
    calls.push({ url: String(input), init });
    if (body === undefined) return new Response(null, { status });
    return new Response(body, { status, headers: { "Content-Type": "application/json" } });
  };
  Object.defineProperty(globalThis, "fetch", { value: stub, configurable: true });
  return calls;
}

test("resolveHealthReview 必带 body：{ decision: \"acknowledged\" }", async () => {
  installStorage("kb_console");
  const calls = installFetch(204);
  await resolveHealthReview("notif-1");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/api/health/review/notif-1/resolve");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.body, JSON.stringify({ decision: "acknowledged" }));
});

test("resolveHealthReview 走鉴权通道：带 Authorization 与 JSON Content-Type", async () => {
  installStorage("kb_console");
  const calls = installFetch(204);
  await resolveHealthReview("notif-2");
  const headers = (calls[0].init.headers ?? {}) as Record<string, string>;
  assert.equal(headers.Authorization, "Bearer test-access-token");
  assert.equal(headers["Content-Type"], "application/json");
});

test("通知 id 做 URL 编码，不拼出跨路径", async () => {
  installStorage("kb_console");
  const calls = installFetch(204);
  await resolveHealthReview("a/b?c");
  assert.equal(calls[0].url, "/api/health/review/a%2Fb%3Fc/resolve");
});

test("服务端 422 被翻成 VALIDATION_ERROR（body 缺失时的真实表现）", async () => {
  installStorage("kb_console");
  installFetch(422, JSON.stringify({ detail: [{ type: "missing", loc: ["body"] }] }));
  await assert.rejects(
    () => resolveHealthReview("notif-3"),
    (err: unknown) => {
      assert.ok(err instanceof HttpError);
      assert.equal(err.status, 422);
      assert.equal(err.code, "VALIDATION_ERROR");
      return true;
    },
  );
});

test("源码契约：resolveHealthReview 的 apiSend 必须带第三个实参", () => {
  const source = readFileSync(resolve(here, "../src/console/health.ts"), "utf8");
  assert.match(
    source,
    /resolveHealthReview\([\s\S]*?apiSend<void>\([^;]*?"POST",\s*\{\s*decision: "acknowledged",?\s*\}\s*\)/,
    "resolveHealthReview 必须把 decision 放进请求体",
  );
});
