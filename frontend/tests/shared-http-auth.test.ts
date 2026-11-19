/**
 * 共享认证 / 请求层契约（前端计划 W2；Q-1 裁决放开共享后的**核心保证**）。
 *
 * 旧裁决（task-05 §0）禁止 console/family 互相 import，目的是「同源 localStorage 下
 * 双端不互踢登录态」。放开后这个目的改由**实例隔离**达成 —— 本文件就是验证它真的成立，
 * 而不是只写在注释里：
 * - 两端 storage 键互不重叠，清一端不动另一端；
 * - refresh 单飞是**每实例**的，不会一端续期把另一端的在途请求顶掉；
 * - 401 收口回调是**每实例**的，一端失效不会把另一端也踹回登录页。
 *
 * 另验收敛掉的两类机制分叉：空响应体（写端允许 / 只读端报错）、错误分类（只有 invalid 判失效）。
 * 用语按 Q-3 裁决**刻意不统一**，这里分别钉住两端各自的说法。
 *
 * Node 无 localStorage / fetch 语义，用 Map 与可控 fetch 替身；每个用例后复原。
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, it } from "node:test";

import { ApiError, createAuthClient } from "../src/shared/auth.ts";
import { HttpError, createHttpClient, query } from "../src/shared/http.ts";

// ---------------------------------------------------------------- localStorage 替身

class MemoryStorage {
  private map = new Map<string, string>();
  getItem(k: string): string | null {
    return this.map.has(k) ? (this.map.get(k) as string) : null;
  }
  setItem(k: string, v: string): void {
    this.map.set(k, String(v));
  }
  removeItem(k: string): void {
    this.map.delete(k);
  }
  clear(): void {
    this.map.clear();
  }
  keys(): string[] {
    return [...this.map.keys()];
  }
}

let storage: MemoryStorage;
const realFetch = globalThis.fetch;
const realStorage = (globalThis as { localStorage?: unknown }).localStorage;

beforeEach(() => {
  storage = new MemoryStorage();
  (globalThis as { localStorage: unknown }).localStorage = storage;
});

afterEach(() => {
  globalThis.fetch = realFetch;
  if (realStorage === undefined) delete (globalThis as { localStorage?: unknown }).localStorage;
  else (globalThis as { localStorage: unknown }).localStorage = realStorage;
});

/** 造一个可控 fetch：按调用顺序返回预设响应，并记录每次请求。 */
function stubFetch(responses: Array<() => Response | Promise<Response>>) {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  let i = 0;
  globalThis.fetch = (async (url: string, init: RequestInit = {}) => {
    calls.push({ url: String(url), init });
    const make = responses[Math.min(i, responses.length - 1)];
    i += 1;
    return make();
  }) as typeof fetch;
  return calls;
}

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });

const PRINCIPAL = { kind: "staff" as const, id: "s-1", role: "admin", name: "管理员" };

// ---------------------------------------------------------------- 实例隔离

describe("W2 · 双端实例隔离（取代旧「禁止互相 import」裁决的目的）", () => {
  it("两端 storage 键前缀不同且互不重叠", () => {
    const console_ = createAuthClient({ storagePrefix: "kb_console" });
    const family = createAuthClient({ storagePrefix: "kb_family" });
    assert.equal(console_.keys.access, "kb_console_access_token");
    assert.equal(family.keys.access, "kb_family_access_token");
    const a = Object.values(console_.keys);
    const b = Object.values(family.keys);
    assert.equal(a.some((k) => b.includes(k)), false, "键重叠就会互踢登录态");
  });

  it("缺 storagePrefix 直接抛错，不允许退化成共用键", () => {
    assert.throws(() => createAuthClient({ storagePrefix: "" }), /storagePrefix/);
  });

  it("一端登录不影响另一端；清一端不动另一端", async () => {
    const console_ = createAuthClient({ storagePrefix: "kb_console" });
    const family = createAuthClient({ storagePrefix: "kb_family" });
    stubFetch([
      () => json({ access_token: "A", refresh_token: "R", principal: PRINCIPAL }),
      () => json({ access_token: "B", refresh_token: "S", principal: { ...PRINCIPAL, kind: "guardian", role: null } }),
    ]);
    await console_.login("13700000000", "x");
    await family.login("13900000002", "y");
    assert.equal(console_.hasSession(), true);
    assert.equal(family.hasSession(), true);
    assert.equal(storage.getItem("kb_console_access_token"), "A");
    assert.equal(storage.getItem("kb_family_access_token"), "B");

    console_.clearSession();
    assert.equal(console_.hasSession(), false);
    assert.equal(family.hasSession(), true, "清照护台把家属端也踢了 → 实例隔离失败");
    assert.equal(storage.getItem("kb_family_access_token"), "B");
  });

  it("401 收口回调是每实例的：一端失效不触发另一端", async () => {
    const console_ = createAuthClient({ storagePrefix: "kb_console" });
    const family = createAuthClient({ storagePrefix: "kb_family" });
    let consoleHits = 0;
    let familyHits = 0;
    console_.setUnauthorizedHandler(() => { consoleHits += 1; });
    family.setUnauthorizedHandler(() => { familyHits += 1; });
    stubFetch([() => new Response("", { status: 401 })]);
    await console_.authedFetch("/api/elders");
    assert.equal(consoleHits, 1);
    assert.equal(familyHits, 0, "照护台失效把家属端也踹回登录页 → 回调串了");
  });

  it("refresh 单飞是每实例的：同端并发只续一次，两端各自续", async () => {
    const console_ = createAuthClient({ storagePrefix: "kb_console" });
    const family = createAuthClient({ storagePrefix: "kb_family" });
    storage.setItem("kb_console_access_token", "oldA");
    storage.setItem("kb_console_refresh_token", "RA");
    storage.setItem("kb_family_access_token", "oldB");
    storage.setItem("kb_family_refresh_token", "RB");
    const calls = stubFetch([
      () => new Response("", { status: 401 }), // console 首个请求 401
      () => json({ access_token: "newA" }), // console refresh
      () => json({ ok: true }), // console 重放
      () => new Response("", { status: 401 }), // family 首个请求 401
      () => json({ access_token: "newB" }), // family refresh
      () => json({ ok: true }), // family 重放
    ]);
    await console_.authedFetch("/api/elders");
    await family.authedFetch("/api/reports/x/list");
    assert.equal(storage.getItem("kb_console_access_token"), "newA");
    assert.equal(storage.getItem("kb_family_access_token"), "newB");
    const refreshes = calls.filter((c) => c.url.includes("/api/sessions/refresh"));
    assert.equal(refreshes.length, 2, "两端应各续一次");
    assert.equal(
      JSON.parse(String(refreshes[0].init.body)).refresh_token,
      "RA",
      "照护台续期用了家属端的 refresh token → 串了",
    );
    assert.equal(JSON.parse(String(refreshes[1].init.body)).refresh_token, "RB");
  });

  it("refresh 被拒 → 清态 + 触发收口回调 + 返回 401（不抛错）", async () => {
    // 断言随行为修正更新：原实现让 refreshOnce 的异常直接冒出 authedFetch，
    // 导致 unauthorizedHandler **永不触发** —— 冷启动靠 App 自己的 catch 还能回登录页，
    // 但应用内请求失效时用户只看到错误横幅、停在失效界面。
    // 正确行为是落到 401 收口：清态 + 回调 + 返回 401 响应，由 http 层翻成「登录已失效」。
    const c = createAuthClient({ storagePrefix: "kb_console" });
    storage.setItem("kb_console_access_token", "old");
    storage.setItem("kb_console_refresh_token", "R");
    let hits = 0;
    c.setUnauthorizedHandler(() => {
      hits += 1;
    });
    stubFetch([
      () => new Response("", { status: 401 }),
      () => new Response("", { status: 401 }), // refresh 也被拒
    ]);
    const res = await c.authedFetch("/api/elders");
    assert.equal(res.status, 401);
    assert.equal(hits, 1, "收口回调必须触发，否则应用内失效不回登录页");
    assert.equal(c.hasSession(), false);
  });

  it("refresh 遇网络失败仍向上抛（断网不该把用户踹回登录页）", async () => {
    const c = createAuthClient({ storagePrefix: "kb_console" });
    storage.setItem("kb_console_access_token", "old");
    storage.setItem("kb_console_refresh_token", "R");
    let hits = 0;
    c.setUnauthorizedHandler(() => {
      hits += 1;
    });
    globalThis.fetch = (async (url: string) => {
      if (String(url).includes("/api/sessions/refresh")) throw new TypeError("Failed to fetch");
      return new Response("", { status: 401 });
    }) as typeof fetch;
    await assert.rejects(() => c.authedFetch("/api/elders"), (err: unknown) => {
      assert.equal(err instanceof ApiError, true);
      assert.equal((err as ApiError).kind, "network");
      return true;
    });
    assert.equal(hits, 0, "断网不得触发登录失效收口");
  });

  it("断网 → ApiError(network)，不被当成登录失效", async () => {
    const c = createAuthClient({ storagePrefix: "kb_console" });
    globalThis.fetch = (async () => {
      throw new TypeError("Failed to fetch");
    }) as typeof fetch;
    await assert.rejects(() => c.login("13700000000", "x"), (err: unknown) => {
      assert.equal(err instanceof ApiError, true);
      assert.equal((err as ApiError).kind, "network");
      return true;
    });
  });
});

// ---------------------------------------------------------------- http 机制

describe("W2 · 空响应体：写端允许、只读端报错（收敛前的真分叉）", () => {
  const ok = () => new Response(null, { status: 204 });

  it("写端（allowEmptyBody: true）204 当成功，返回 undefined", async () => {
    const client = createHttpClient({
      authedFetch: async () => ok(),
      messageOf: () => "x",
      networkText: "n",
      allowEmptyBody: true,
    });
    assert.equal(await client.apiSend("/api/x", "DELETE"), undefined);
  });

  it("只读端（allowEmptyBody: false）空体报 BAD_BODY，不静默当成功", async () => {
    const client = createHttpClient({
      authedFetch: async () => new Response("", { status: 200, headers: { "content-type": "application/json" } }),
      messageOf: (_s, code) => (code === "BAD_BODY" ? "服务暂时不可用" : "n"),
      networkText: "n",
      allowEmptyBody: false,
    });
    await assert.rejects(() => client.apiGet("/api/x"), (err: unknown) => {
      assert.equal(err instanceof HttpError, true);
      assert.equal((err as HttpError).code, "BAD_BODY");
      return true;
    });
  });

  it("content-length: 0 在写端也当成功", async () => {
    const client = createHttpClient({
      authedFetch: async () => new Response("", { status: 200, headers: { "content-length": "0" } }),
      messageOf: () => "x",
      networkText: "n",
      allowEmptyBody: true,
    });
    assert.equal(await client.apiGet("/api/x"), undefined);
  });
});

describe("W2 · 错误分类：只有 invalid 判登录失效", () => {
  it("ApiError(invalid) → 401 TOKEN_INVALID", async () => {
    const client = createHttpClient({
      authedFetch: async () => {
        throw new ApiError("invalid", "手机号或密码不正确");
      },
      messageOf: (s) => (s === 401 ? "登录已失效" : "n"),
      networkText: "n",
    });
    await assert.rejects(() => client.apiGet("/api/x"), (err: unknown) => {
      const e = err as HttpError;
      assert.equal(e.status, 401);
      assert.equal(e.code, "TOKEN_INVALID");
      assert.equal(e.message, "登录已失效");
      return true;
    });
  });

  it("ApiError(network) 与 TypeError → status 0，不踹回登录页", async () => {
    for (const thrown of [new ApiError("network", "n"), new TypeError("Failed to fetch")]) {
      const client = createHttpClient({
        authedFetch: async () => {
          throw thrown;
        },
        messageOf: () => "不该用这条",
        networkText: "网络不太好",
      });
      await assert.rejects(() => client.apiGet("/api/x"), (err: unknown) => {
        const e = err as HttpError;
        assert.equal(e.status, 0);
        assert.equal(e.code, "NETWORK");
        assert.equal(e.message, "网络不太好");
        return true;
      });
      assert.equal(client.isNetworkError(new HttpError(0, "NETWORK", "n")), true);
    }
  });

  it("后端给了 code 就用它；没给按状态码兜底，且兜底口径可配", async () => {
    const make = (fallbackCode: (s: number) => string) =>
      createHttpClient({
        authedFetch: async () => json({ detail: "internal english" }, 400),
        messageOf: (_s, code) => `code=${code}`,
        networkText: "n",
        fallbackCode,
      });
    await assert.rejects(() => make(() => "BAD_REQUEST").apiGet("/x"), (e: unknown) => {
      assert.equal((e as HttpError).code, "BAD_REQUEST");
      return true;
    });
    await assert.rejects(() => make((s) => `HTTP_${s}`).apiGet("/x"), (e: unknown) => {
      assert.equal((e as HttpError).code, "HTTP_400");
      return true;
    });
  });

  it("不回显 detail（那是给排障看的英文）", async () => {
    const client = createHttpClient({
      authedFetch: async () => json({ detail: "internal stack trace" }, 403),
      messageOf: () => "当前角色无权执行该操作",
      networkText: "n",
    });
    await assert.rejects(() => client.apiGet("/x"), (e: unknown) => {
      assert.equal((e as HttpError).message, "当前角色无权执行该操作");
      assert.equal((e as HttpError).code, "ROLE_FORBIDDEN");
      return true;
    });
  });
});

describe("W2 · 用语按角色保留，不跨端统一（Q-3 裁决）", () => {
  const read = (p: string) => readFileSync(new URL(`../src/${p}`, import.meta.url), "utf8");
  /** 只看代码行：注释里会正当地引用另一端的说法做对比，不能当成实现串味。 */
  const codeOf = (p: string) =>
    read(p)
      .split("\n")
      .filter((line) => {
        const t = line.trim();
        return !t.startsWith("//") && !t.startsWith("*") && !t.startsWith("/*");
      })
      .join("\n");

  it("照护台保留「当前角色无权执行该操作」，家属端保留「没有权限查看」", () => {
    assert.equal(codeOf("console/http.ts").includes("当前角色无权执行该操作"), true);
    assert.equal(codeOf("family/http.ts").includes("没有权限查看"), true);
    assert.equal(codeOf("console/http.ts").includes("没有权限查看"), false);
    assert.equal(codeOf("family/http.ts").includes("当前角色无权执行该操作"), false);
  });

  it("两端网络文案也不同，各自保留", () => {
    assert.equal(codeOf("console/http.ts").includes("网络异常，请稍后重试"), true);
    assert.equal(codeOf("family/http.ts").includes("网络不太好，请稍后重试"), true);
  });

  it("query 两端共用同一实现，空值一律跳过", () => {
    assert.equal(query({ a: 1, b: "", c: null, d: undefined, e: false }), "?a=1&e=false");
    assert.equal(query({}), "");
    assert.equal(read("console/http.ts").includes("export { HttpError, query }"), true);
    assert.equal(read("family/http.ts").includes("export { HttpError, query }"), true);
  });
});

describe("W2 · shared 是叶子，且旧「禁止互相 import」注释已同批更新", () => {
  const read = (p: string) => readFileSync(new URL(`../src/${p}`, import.meta.url), "utf8");

  it("shared/auth 与 shared/http 不 import 业务端，也不 import three", () => {
    for (const f of ["shared/auth.ts", "shared/http.ts"]) {
      const text = read(f);
      assert.equal(/from "\.\.\/(pad|console|family)\//.test(text), false, f);
      assert.equal(/from "(three|@pixiv)/.test(text), false, f);
    }
  });

  it("两端 api.ts 不再声称「禁止互相 import」，改为写明实例隔离", () => {
    for (const f of ["console/api.ts", "family/api.ts"]) {
      const text = read(f);
      assert.equal(
        /本模块与 (console|family)\/api\.ts 为\*\*独立同构实现\*\*/.test(text),
        false,
        `${f} 仍写着旧的独立同构口径，后人会照它再拆一份`,
      );
      assert.equal(text.includes("实例隔离"), true, `${f} 应写明新的隔离方式`);
      assert.equal(text.includes("createAuthClient"), true);
    }
  });

  it("仍然禁止 import src/pad/**（pad 是另一套凭证范式）", () => {
    for (const f of ["console/api.ts", "family/api.ts", "console/http.ts", "family/http.ts"]) {
      assert.equal(/from "\.\.\/pad\//.test(read(f)), false, f);
    }
  });

  it("两端 http 都委托共享工厂，不再各自实现 settle/send", () => {
    for (const f of ["console/http.ts", "family/http.ts"]) {
      const text = read(f);
      assert.equal(text.includes("createHttpClient"), true, f);
      assert.equal(/async function settle/.test(text), false, `${f} 仍自己实现 settle`);
      assert.equal(/async function send/.test(text), false, `${f} 仍自己实现 send`);
    }
  });
});