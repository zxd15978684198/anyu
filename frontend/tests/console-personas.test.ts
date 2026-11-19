/**
 * 数字人角色列表（2026-11-16）：接口契约、权限矩阵与只读边界。
 *
 * 锁三件事：
 * 1. 列表只走既有 `GET /api/elders/personas`，不新增第二套读口；
 * 2. 能力 `persona:read` 只给 admin / doctor，与后端 `check_roles` 一致；
 * 3. 角色页是只读页：不出现新增 / 编辑 / 删除请求。
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { listPersonaAssets, personaAssetNo, voiceLabel } from "../src/console/personas";
import { can, capabilitiesOf, type Capability } from "../src/console/permissions";
import type { Principal } from "../src/console/api";

const here = dirname(fileURLToPath(import.meta.url));
const read = (path: string) => readFileSync(resolve(here, "../..", path), "utf8");
const stripComments = (text: string) =>
  text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");

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

test("角色列表：GET /api/elders/personas，收敛完整资产", async () => {
  installStorage();
  const calls = installFetch(200, {
    items: [
      {
        persona_id: "p-1",
        name: "小满",
        avatar_url: "/avatars/vrm1_sample.vrm",
        voice_id: "zh-CN-XiaoxiaoNeural",
        personality: "温和、耐心",
        greeting: "您好呀",
        tone_examples: ["慢慢说", 3, null],
        assigned_elders: 2,
      },
      { persona_id: "", name: "坏数据" },
    ],
  });

  const items = await listPersonaAssets();

  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/api/elders/personas");
  assert.equal(calls[0].init.method, "GET");
  assert.equal(items.length, 1, "没有 persona_id 的行必须丢弃");
  assert.equal(items[0].voice_id, "zh-CN-XiaoxiaoNeural");
  assert.deepEqual(items[0].tone_examples, ["慢慢说"]);
  assert.equal(items[0].assigned_elders, 2);
});

test("persona:read 只给 admin / doctor，护工拿不到", () => {
  const capability = "persona:read" as Capability;
  assert.equal(can(staff("admin"), capability), true);
  assert.equal(can(staff("doctor"), capability), true);
  assert.equal(can(staff("care_worker"), capability), false);
  assert.equal(can({ kind: "guardian", id: "g-1", role: null, name: "家属" } as Principal, capability), false);
  assert.equal(capabilitiesOf(staff("care_worker")).includes(capability), false);
});

test("导航与页面同源：/personas 进导航表且挂 persona:read", () => {
  const router = read("frontend/src/console/router.tsx");
  const app = read("frontend/src/console/App.tsx");

  assert.match(router, /path:\s*"\/personas"[^}]*capability:\s*"persona:read"/);
  assert.match(app, /path:\s*"\/personas"[\s\S]*?capability:\s*"persona:read"/);
  assert.match(app, /import\s+\{\s*PersonasPage\s*\}\s+from\s+"\.\/pages\/PersonasPage"/);
});

test("角色页只读：没有新增 / 编辑 / 删除请求", () => {
  const page = stripComments(read("frontend/src/console/pages/PersonasPage.tsx"));
  const data = stripComments(read("frontend/src/console/personas.ts"));

  assert.equal(page.includes("apiSend"), false);
  assert.equal(data.includes("apiSend"), false);
  assert.equal(/"(POST|PATCH|PUT|DELETE)"/.test(page + data), false);
  assert.equal(page.includes("新建"), false);
  assert.equal(page.includes("删除"), false);
});

test("资产号取末段后 4 位：9 个固定角色 1201–1209 可区分", () => {
  assert.equal(personaAssetNo("11111111-1111-4111-8111-111111111201"), "1201");
  assert.equal(personaAssetNo("11111111-1111-4111-8111-111111111209"), "1209");
  assert.notEqual(
    personaAssetNo("11111111-1111-4111-8111-111111111201"),
    personaAssetNo("11111111-1111-4111-8111-111111111202"),
  );
  assert.equal(personaAssetNo(""), "—");
});

test("音色展示中文名，未知值原样回显", () => {
  assert.equal(voiceLabel("zh-CN-XiaoxiaoNeural"), "晓晓（女声）");
  assert.equal(voiceLabel("zh-CN-YunjianNeural"), "云健（男声）");
  assert.equal(voiceLabel("zh-CN-UnknownNeural"), "zh-CN-UnknownNeural");
  assert.equal(voiceLabel("  "), "—");
});

test("角色页不再用 UUID 前 8 位当编号，形象文件从列表移到详情", () => {
  const page = read("frontend/src/console/pages/PersonasPage.tsx");
  assert.doesNotMatch(page, /slice\(0,\s*8\)/);
  assert.doesNotMatch(page, /title:\s*"形象"/);
  assert.match(page, /personaAssetNo\(/);
  assert.match(page, /voiceLabel\(/);
  assert.match(page, /persona-summary/);
  assert.match(page, /资产号/);
});
