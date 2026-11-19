/**
 * 长者退房前端契约（FR-ADM-09）。
 *
 * 钉三件事：
 * 1. 端点封装只发 `POST /api/elders/{id}/checkout`，body 只含 `archive` / `reason`；
 * 2. 能力矩阵只有 admin 有 `elder:checkout`，医生与护工看不到入口；
 * 3. 页面确实做了二次确认、影响清单与回执展示（源码断言，不引渲染依赖）。
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { CHECKOUT_REASON_OPTIONS, checkoutElder } from "../src/console/elders";
import { HttpError } from "../src/console/http";
import { can } from "../src/console/permissions";
import type { Principal } from "../src/console/api";

const here = dirname(fileURLToPath(import.meta.url));

function installStorage(): void {
  const store = new Map<string, string>();
  store.set("kb_console_access_token", "test-access-token");
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

interface Captured {
  url: string;
  init: RequestInit;
}

function installFetch(status: number, body?: unknown): Captured[] {
  const calls: Captured[] = [];
  const stub = async (input: RequestInfo | URL, init: RequestInit = {}) => {
    calls.push({ url: String(input), init });
    if (body === undefined) return new Response(null, { status });
    return new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  };
  Object.defineProperty(globalThis, "fetch", { value: stub, configurable: true });
  return calls;
}

function staff(role: string): Principal {
  return { kind: "staff", id: "staff-1", role, name: role };
}

const RECEIPT = {
  elder_id: "elder-1",
  elder_status: "away",
  archived: false,
  memories_purged: 4,
  voiceprints_removed: 2,
  sessions_closed: 1,
  relations_removed: 3,
  devices_unbound: 1,
  staff_cards_revoked: 1,
  audit_log_id: "audit-1",
};

test("退房：POST /api/elders/{id}/checkout，透传 archive 与 reason", async () => {
  installStorage();
  const calls = installFetch(200, RECEIPT);
  const receipt = await checkoutElder("elder/1", { archive: true, reason: "transfer" });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/api/elders/elder%2F1/checkout");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.body, JSON.stringify({ archive: true, reason: "transfer" }));
  assert.equal(receipt.devices_unbound, 1);
  assert.equal(receipt.audit_log_id, "audit-1");
});

test("退房：省略参数时落普通退房，reason 不带空串", async () => {
  installStorage();
  const calls = installFetch(200, { ...RECEIPT, archived: false });
  await checkoutElder("elder-1");
  assert.equal(calls[0].init.body, JSON.stringify({ archive: false, reason: "checkout" }));
});

test("退房失败保留 403 语义，不伪造成功回执", async () => {
  installStorage();
  installFetch(403, { detail: "role", code: "ROLE_FORBIDDEN" });
  await assert.rejects(
    () => checkoutElder("elder-1"),
    (err: unknown) => {
      assert.ok(err instanceof HttpError);
      assert.equal(err.status, 403);
      assert.equal(err.code, "ROLE_FORBIDDEN");
      return true;
    },
  );
});

test("退房原因闭集与服务端审计口径一致", () => {
  assert.deepEqual(
    CHECKOUT_REASON_OPTIONS.map((item) => item.value),
    ["checkout", "transfer", "deceased"],
  );
});

test("能力矩阵：只有 admin 能退房", () => {
  assert.equal(can(staff("admin"), "elder:checkout"), true);
  assert.equal(can(staff("doctor"), "elder:checkout"), false);
  assert.equal(can(staff("care_worker"), "elder:checkout"), false);
  assert.equal(can(null, "elder:checkout"), false);
});

test("页面有二次确认、影响清单与回执，不提供列表行内快捷退房", () => {
  const page = readFileSync(resolve(here, "../src/console/pages/EldersPage.tsx"), "utf8");
  assert.match(page, /办理退房/);
  assert.match(page, /此操作不可撤销/);
  assert.match(page, /我已核对长者身份与离院事由/);
  assert.match(page, /解除家属与护工关系/);
  assert.match(page, /checkoutReceipt\.memories_purged/);
  assert.match(page, /审计编号/);
  assert.match(page, /detail\.status !== "archived"/);
});
