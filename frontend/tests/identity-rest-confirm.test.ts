/**
 * 身份确认的 REST 出口（`POST /api/identity/confirm`）+ 弹层降级接线的守卫。
 *
 * 为什么必须有这些断言（2026-11-11，产品负责人问「前端的身份确认功能实现完整吗」）：
 *
 *  - **这个出口存在但没人调**。后端 `app/api/identity.py` 早就交付、8 例测试绿；
 *    前端对 `frontend/src/**` grep `identity/confirm` 零命中。按住说话降级链路
 *    本来就没有 WS，点「是我」只会得到「网络还没连好，请稍等一下再点一次」——
 *    长者会一直按、然后放弃确认。这些用例把「WS 发不出去要走 REST」钉死。
 *  - **顺序不能反**：先 WS、发不出去才 REST。WS 那条路上服务端手里有 `ask_id`，
 *    能对这一张卡做超时与结果回推；REST 只保证「这台设备确认过」。
 *    若有人改成「先 REST 再 WS」，屏幕上那张卡的「已确认」就与服务端结果帧脱钩。
 *  - **永不抛**：身份确认失败要能把话讲给长者听。未捕获的 Promise 拒绝会白屏，
 *    长者连「稍后再说」都点不到。
 *  - **不许把「没确认上」显示成成功**：`ok:false` 时必须允许再点，
 *    否则按钮锁死在「正在确认…」。
 *
 * 渲染层不测（Node 只做类型擦除、不认 TSX）；`apiIdentity.ts` 用真函数 + 假 fetch
 * 断言行为，组件与 App 的接线用源码契约钉住。
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const identity = await import("../src/pad/apiIdentity");

const read = (relative: string): string =>
  readFileSync(fileURLToPath(new URL(`../src/pad/${relative}`, import.meta.url)), "utf8");

/** 源码里换行/缩进不该影响契约判定。 */
const flat = (source: string): string => source.replace(/\s+/g, " ");

const API_SRC = read("apiIdentity.ts");
const DIALOG_SRC = read("pages/IdentityConfirm.tsx");
const APP_SRC = read("App.tsx");

/** 取某个标识符之后的一段源码，拿不到就返回空串。 */
const after = (source: string, needle: string, length = 3000): string => {
  const at = source.indexOf(needle);
  return at < 0 ? "" : source.slice(at, at + length);
};

/**
 * 装一次假 sessionStorage：`login: true` 时放一份有效设备会话。
 * `apiIdentity.ts` 顶层不碰 storage（只在函数里读），所以可以逐用例装/卸。
 */
function withSession<T>(body: () => Promise<T>, login = true): Promise<T> {
  const original = globalThis.sessionStorage;
  const store = new Map<string, string>();
  if (login) {
    store.set("kb_access_token", "device-token-abc");
    store.set("kb_expires_at", String(Date.now() + 3_600_000));
    store.set("kb_device", JSON.stringify({ id: "dev-1", room_id: "room-1", elder_id: "elder-1" }));
  }
  globalThis.sessionStorage = {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => void store.set(key, value),
    removeItem: (key: string) => void store.delete(key),
    clear: () => store.clear(),
    key: (index: number) => Array.from(store.keys())[index] ?? null,
    get length() {
      return store.size;
    },
  } as Storage;
  return body().finally(() => {
    globalThis.sessionStorage = original;
  });
}

/** 临时替换 globalThis.fetch；用例结束后务必还原。 */
async function withFetch<T>(
  stub: (url: string, init?: RequestInit) => Promise<Response> | Response,
  body: (captured: () => { url: string; init?: RequestInit }) => Promise<T>,
): Promise<T> {
  const original = globalThis.fetch;
  let captured: { url: string; init?: RequestInit } = { url: "" };
  let touched = false;
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    captured = { url: String(input), init };
    touched = true;
    return stub(captured.url, init);
  }) as typeof globalThis.fetch;
  try {
    return await body(() => {
      assert.ok(touched, "这条用例应该真的发一次请求");
      return captured;
    });
  } finally {
    globalThis.fetch = original;
  }
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/* ---------------------------------------------------------------- 行为 */

test("confirmIdentity：POST /api/identity/confirm，令牌走 Bearer，body 带 elder_id 与 ask_id", async () => {
  await withSession(async () => {
    await withFetch(
      () =>
        jsonResponse({
          thread_id: "11111111-2222-3333-4444-555555555555",
          identity_source: "confirm",
          elder_id: "elder-1",
          created: true,
        }),
      async (captured) => {
        const outcome = await identity.confirmIdentity("elder-1", "ask-77");

        const request = captured();
        assert.equal(request.url, "/api/identity/confirm", "端点写错就整条降级链路不通");
        assert.equal(request.init?.method, "POST");

        const headers = new Headers(request.init?.headers);
        assert.equal(
          headers.get("Authorization"),
          "Bearer device-token-abc",
          "身份确认也是受保护接口：令牌必须走 Authorization，不能塞进 body",
        );

        assert.deepEqual(JSON.parse(String(request.init?.body)), {
          elder_id: "elder-1",
          ask_id: "ask-77",
        });
        assert.deepEqual(outcome, {
          ok: true,
          threadId: "11111111-2222-3333-4444-555555555555",
          created: true,
        });
      },
    );
  });
});

test("confirmIdentity：没有 ask_id 时不硬塞空串（服务端不需要它当幂等键）", async () => {
  await withSession(async () => {
    await withFetch(
      () => jsonResponse({ thread_id: "t-1", identity_source: "confirm", elder_id: "elder-1", created: false }),
      async (captured) => {
        const outcome = await identity.confirmIdentity("elder-1");

        assert.deepEqual(JSON.parse(String(captured().init?.body)), { elder_id: "elder-1" });
        assert.equal(outcome.ok, true);
        assert.equal(outcome.created, false, "created=false 是「复用已有确认态」，不是失败");
      },
    );
  });
});

test("confirmIdentity：没有会话时本地就返回 no_session，不打后端", async () => {
  await withSession(async () => {
    const original = globalThis.fetch;
    let called = false;
    globalThis.fetch = (async () => {
      called = true;
      return jsonResponse({});
    }) as typeof globalThis.fetch;
    try {
      const outcome = await identity.confirmIdentity("elder-1", "ask-1");
      assert.equal(called, false, "没有令牌还去请求：只会拿到 401，白跑一趟网络");
      assert.deepEqual(outcome, { ok: false, threadId: null, errorCode: "no_session" });
    } finally {
      globalThis.fetch = original;
    }
  }, false);
});

test("confirmIdentity：403 elder_mismatch 原样透出错误码，不假装成功", async () => {
  await withSession(async () => {
    await withFetch(
      () => jsonResponse({ detail: "确认的长者与设备绑定的长者不一致", code: "elder_mismatch" }, 403),
      async () => {
        const outcome = await identity.confirmIdentity("other-elder", "ask-1");
        assert.equal(outcome.ok, false);
        assert.equal(outcome.threadId, null);
        assert.equal(
          outcome.errorCode,
          "elder_mismatch",
          "把 403 吞成成功，等于让前端把身份绑到别人身上",
        );
      },
    );
  });
});

test("confirmIdentity：409 device_unbound 与 503 identity_unavailable 都只回错误码", async () => {
  await withSession(async () => {
    for (const [status, code] of [
      [409, "device_unbound"],
      [503, "identity_unavailable"],
    ] as const) {
      await withFetch(
        () => jsonResponse({ detail: "x", code }, status),
        async () => {
          const outcome = await identity.confirmIdentity("elder-1");
          assert.equal(outcome.ok, false);
          assert.equal(outcome.errorCode, code);
        },
      );
    }
  });
});

test("confirmIdentity：body 没有 code 字段时按 HTTP 状态兜底（不猜语义）", async () => {
  await withSession(async () => {
    await withFetch(
      () => jsonResponse({ detail: "Internal Server Error" }, 500),
      async () => {
        const outcome = await identity.confirmIdentity("elder-1");
        assert.equal(outcome.ok, false);
        assert.equal(outcome.errorCode, "http_500");
      },
    );
  });
});

test("confirmIdentity：网络异常兜成 network，绝不抛给调用方", async () => {
  await withSession(async () => {
    const original = globalThis.fetch;
    globalThis.fetch = (async () => {
      throw new TypeError("Failed to fetch");
    }) as typeof globalThis.fetch;
    try {
      const outcome = await identity.confirmIdentity("elder-1", "ask-1");
      assert.deepEqual(outcome, { ok: false, threadId: null, errorCode: "network" });
    } finally {
      globalThis.fetch = original;
    }
  });
});

test("confirmIdentity：非 JSON 响应体不算成功（2xx 但没有 thread_id 也不编一个）", async () => {
  await withSession(async () => {
    await withFetch(
      () => new Response("<html>gateway</html>", { status: 200 }),
      async () => {
        const outcome = await identity.confirmIdentity("elder-1");
        assert.equal(outcome.ok, true, "2xx 就是确认上了");
        assert.equal(outcome.threadId, null, "拿不到 thread_id 时给 null，不许编");
      },
    );
  });
});

test("identityErrorText：每类失败都给长者一句人话，不出现状态码", () => {
  const codes = [
    "no_session",
    "device_unbound",
    "elder_mismatch",
    "identity_unavailable",
    "network",
    "http_500",
    undefined,
  ];
  for (const code of codes) {
    const text = identity.identityErrorText(code);
    assert.ok(text.length > 0, `${code} 没有话术`);
    assert.ok(!/http_|5\d\d|4\d\d/.test(text), `${code} 的话术里漏出了状态码：${text}`);
  }
  assert.equal(identity.identityErrorText("no_session"), "登录已过期，请重新登录后再确认。");
});

/* ---------------------------------------------------------------- 契约 */

test("IdentityConfirm：WS 发不出去时才走 REST，且「先 WS 后 REST」顺序不许反", () => {
  const handle = flat(after(DIALOG_SRC, "function handleConfirm()"));
  assert.ok(handle.includes("const sent = onConfirm(askId, elderId);"), "首选必须还是 WS");
  assert.ok(
    handle.includes("void runRestConfirm();"),
    "WS 发不出去却没接 REST：降级链路里长者对着点不动的按钮按",
  );
  assert.ok(
    handle.indexOf("onConfirm(") < handle.indexOf("runRestConfirm()"),
    "顺序反了：REST 先落库会让屏幕上的「已确认」与服务端结果帧脱钩",
  );
});

test("IdentityConfirm：没给 REST 出口时保持原行为（提示重试，不假装成功）", () => {
  const rest = flat(after(DIALOG_SRC, "async function runRestConfirm()"));
  assert.ok(
    rest.includes("if (!onRestConfirm)"),
    "缺 onRestConfirm 时必须显式处理，否则 REST 失败会被当成成功",
  );
  assert.ok(rest.includes("setSubmitted(false)"), "失败要放开按钮，不能锁死在「正在确认…」");
  assert.ok(
    rest.includes("const outcome = await onRestConfirm(askId, elderId);"),
    "必须用真实结果决定成败",
  );
});

test("IdentityConfirm：REST 在途时按钮禁用但文案是「正在确认…」", () => {
  assert.ok(
    flat(DIALOG_SRC).includes("disabled={submitted || restPending}"),
    "REST 在途时不禁用会重复落库",
  );
  assert.ok(
    flat(DIALOG_SRC).includes('restPending ? "正在确认…" : "是我"'),
    "在途必须有明确文案，否则长者以为没点上",
  );
});

test("App：降级链路把「是我」接到确认端点，成功后本地身份置 confirm", () => {
  const app = flat(APP_SRC);
  assert.ok(app.includes('from "./apiIdentity"'), "App 没引入 REST 出口");
  assert.ok(app.includes("onRestConfirm={async (askId, elderId) =>"), "确认层没接 REST 回退");
  assert.ok(
    app.includes("const outcome = await confirmIdentity(elderId, askId);"),
    "回退没真的调确认端点",
  );
  assert.ok(
    app.includes('if (outcome.ok) setIdentitySource("confirm");'),
    "落库成功却没更新本地身份：顶栏胶囊会一直停在「待确认」",
  );
  assert.ok(
    app.includes("message: outcome.ok ? undefined : identityErrorText(outcome.errorCode)"),
    "失败原因没有翻成人话就传下去了",
  );
});

test("apiIdentity：超时用 AbortSignal.timeout，且常量不会被顺手改大", () => {
  assert.ok(
    flat(API_SRC).includes("signal: AbortSignal.timeout(IDENTITY_CONFIRM_TIMEOUT_MS)"),
    "没有超时：断网时按钮会永远停在「正在确认…」",
  );
  assert.ok(
    API_SRC.includes("IDENTITY_CONFIRM_TIMEOUT_MS = 8_000;"),
    "超时窗口被改了：8s 是按「一次 INSERT + commit」定的",
  );
});
