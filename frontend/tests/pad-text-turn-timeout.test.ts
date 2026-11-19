/**
 * 打字轮「发送中…」卡死（task-42）的前端守卫。
 *
 * 病：`TextFallback` 的 `busy` 只在 `onSubmit` 的 `finally` 里复位，而 `submitTextTurn`
 * 之前是裸 `await sendTextTurn(text)` —— 后端/网关把连接吊住时 `fetch` 永不 settle，
 * 这个 await 就永不返回：底栏永远停在「发送中…」，没有报错、没有重试口，长者只能刷新。
 *
 * 修法两条腿，各测一条：
 *  1. `sendTextTurnWithin(text, ms)` 到点 abort + 抛 `TextTurnTimeoutError`，保证 await 一定有出口；
 *     **必须真 abort**（只抛错不掐请求，慢请求会继续占着连接）。
 *  2. `submitTextTurn` 超时/失败一律 throw，靠 `TextFallback` 已有的 `finally` 复位、`catch` 保草稿。
 *
 * 渲染层不测（Node 只做类型擦除、不认 TSX）：行为用真函数 + 假 fetch，接线用源码契约钉住。
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

/**
 * `session.ts` 直接读写 `sessionStorage`（模块顶层不碰，但函数里不判存在性），
 * Node 没有这个全局，所以先塞一个内存实现，再让 `sendTextTurn` 能拿到令牌。
 */
const sessionStore = new Map<string, string>();
(globalThis as unknown as { sessionStorage: Storage }).sessionStorage = {
  get length() {
    return sessionStore.size;
  },
  clear: () => sessionStore.clear(),
  getItem: (key: string) => sessionStore.get(key) ?? null,
  key: (index: number) => [...sessionStore.keys()][index] ?? null,
  removeItem: (key: string) => {
    sessionStore.delete(key);
  },
  setItem: (key: string, value: string) => {
    sessionStore.set(key, String(value));
  },
} as Storage;

const dialogue = await import("../src/pad/apiDialogue");
const session = await import("../src/pad/session");

const read = (relative: string): string =>
  readFileSync(fileURLToPath(new URL(`../src/pad/${relative}`, import.meta.url)), "utf8");

/** 源码里换行/缩进不该影响契约判定。 */
const flat = (source: string): string => source.replace(/\s+/g, " ");

/**
 * 去掉注释再断言「不许出现某某调用」——App 里有一段解释病根的文字，本身就写着
 * 「此前这里是裸 `await sendTextTurn(text)`」，不剥注释就会把说明文字当成违规代码。
 */
const stripComments = (source: string): string =>
  source.replace(/\/\*[\s\S]*?\*\//g, " ").replace(/^\s*\/\/.*$/gm, " ");

const APP_SRC = read("App.tsx");
const TEXT_FALLBACK_SRC = read("components/TextFallback.tsx");
const FALLBACK_ENTRIES_SRC = read("components/FallbackEntries.tsx");

/** 取 needle 之后 length 个字符，取不到给空串。 */
const after = (source: string, needle: string, length = 1200): string => {
  const at = source.indexOf(needle);
  return at < 0 ? "" : source.slice(at, at + length);
};

type Captured = { url: string; init?: RequestInit };

/** 临时替换 globalThis.fetch，结束必还原（否则同进程后面的用例全被带偏）。 */
async function withFetch<T>(
  stub: (url: string, init?: RequestInit) => Promise<Response>,
  body: (captured: () => Captured) => Promise<T>,
): Promise<T> {
  const original = globalThis.fetch;
  let captured: Captured = { url: "" };
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

/** 让 `sendTextTurn` 走到 fetch（没有会话它会在本地就返回 UNAUTHORIZED）。 */
function signIn(): void {
  session.saveSession({
    access_token: "token-abc",
    expires_at: Date.now() + 60 * 60 * 1000,
    device: { id: "device-1", room_id: "A-301", elder_id: null },
  });
}

const REPLY_BODY = JSON.stringify({ reply: { text: "好，我在听。" }, degraded: false, safety_alert: false });

test("超时上限是 15s：短了会误杀正常慢请求，长了长者要对着「发送中…」干等", () => {
  assert.equal(dialogue.TEXT_TURN_TIMEOUT_MS, 15_000);
  assert.match(
    flat(APP_SRC),
    /sendTextTurnWithin\(text\)/,
    "App 走带超时的入口、用默认 15s；自己另写一个数字，改口径时就会被漏掉",
  );
});

test("响应永不返回时到点抛 TextTurnTimeoutError，并且真的 abort 了那条请求", async () => {
  await withFetch(
    // 关键：永不 settle。这正是一个被网关吊住的连接的形状。
    () => new Promise<Response>(() => {}),
    async (captured) => {
      signIn();
      const startedAt = Date.now();
      const error = await dialogue.sendTextTurnWithin("今天几点了", 25).then(
        () => null,
        (err: unknown) => err,
      );
      const elapsed = Date.now() - startedAt;

      assert.ok(error instanceof dialogue.TextTurnTimeoutError, "超时必须抛自己的类型，别混进网络失败");
      assert.equal(error.timeoutMs, 25, "提示里要说清等了多少毫秒");
      assert.ok(elapsed < 2_000, `超时要及时生效，实际等了 ${elapsed}ms`);

      const request = captured();
      assert.equal(request.url, "/api/dialogue/text", "掐的必须是这条请求本身");
      assert.ok(request.init?.signal, "不给 fetch 传 signal 就等于没掐：慢请求会继续占着连接");
      assert.equal(request.init.signal?.aborted, true, "到点后 signal 应该是 aborted");
      session.clearSession();
    },
  );
});

test("正常回包时超时不误报，且调用方给的 signal 原样透传给 fetch", async () => {
  await withFetch(
    () => Promise.resolve(new Response(REPLY_BODY, { status: 200, headers: { "Content-Type": "application/json" } })),
    async (captured) => {
      signIn();
      const controller = new AbortController();
      const outcome = await dialogue.sendTextTurn("今天几点了", controller.signal);

      assert.equal(outcome.ok, true);
      assert.equal(outcome.reply, "好，我在听。");
      assert.equal(
        captured().init?.signal,
        controller.signal,
        "外部 signal 必须原样透传：调用方靠它取消，中间再造一个就取消了",
      );
      assert.equal(controller.signal.aborted, false, "成功路径不该乱 abort");

      // 反证：同上一次请求换成 60ms 上限，也应在远小于 60ms 内正常返回（定时器被 finally 清掉）。
      const quick = await dialogue.sendTextTurnWithin("今天几点了", 60);
      assert.equal(quick.ok, true);
      session.clearSession();
    },
  );
});

test("submitTextTurn：超时与失败都要抛出去，交给 TextFallback 的 finally 复位「发送中…」", () => {
  const app = flat(APP_SRC);
  const submit = after(app, "const submitTextTurn = async (text: string)");

  assert.match(submit, /sendTextTurnWithin\(text\)/, "提交必须走带超时的入口，不能退回裸 await");
  assert.ok(
    !/await sendTextTurn\(text\)/.test(flat(stripComments(APP_SRC))),
    "App 里不许再有裸 `await sendTextTurn(text)`：这正是卡死的病根",
  );
  assert.match(submit, /throw err/, "超时要往外抛，否则 TextFallback 的 catch/finally 永远收不到信号");
  assert.match(submit, /TEXT_TURN_TIMEOUT_TEXT/, "超时得有一句专门的提示，不能和「没送出去」混用");
  assert.match(
    submit,
    /instanceof TextTurnTimeoutError/,
    "超时与网络失败的文案要分开：前者「没等到回复」，后者「没送出去」",
  );

  // 收到响应但内容不可用（401 / 后端兜底话术）也要抛，否则草稿被清、长者以为发出去了。
  assert.match(
    submit,
    /if \(!outcome\.ok\) \{[\s\S]*throw new Error\(outcome\.reply\)/,
    "`ok:false` 的分支同样要 throw，保住输入框里的原话",
  );

  const handleSend = after(flat(TEXT_FALLBACK_SRC), "function handleSend");
  assert.match(handleSend, /finally \{[\s\S]*setBusy\(false\)/, "复位只在 finally 里，异常路径也必须走到");
  const catchPart = handleSend.slice(handleSend.indexOf("catch"), handleSend.indexOf("} finally"));
  assert.ok(
    !/setDraft\(""\)/.test(catchPart),
    "失败分支不能清草稿：长者手打的字被抹掉，得重新说一遍",
  );
  assert.match(handleSend, /setProblem\(/, "失败要按它自己的口径出问题行");

  // 常问句（StandbyPanel）也是 submitTextTurn 的调用方，同样不能漏 catch，否则控制台红字。
  const onAsk = after(flat(FALLBACK_ENTRIES_SRC), "onAsk", 400) + after(app, "onAsk=", 400);
  assert.match(onAsk, /\.catch\(/, "另一条调用链也要吞掉 throw，避免 unhandled rejection");

  // 发送按钮文案仍由 busy 驱动：「发送中…」只有在 busy 真被复位时才会消失。
  assert.match(flat(TEXT_FALLBACK_SRC), /busy \? "发送中…" : "发送"/);
});
