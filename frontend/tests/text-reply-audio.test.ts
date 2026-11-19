/**
 * 打字回复出声 + 口型同步（2026-11-11 产品负责人要求「打字问的也要出声、嘴要动」）的前端守卫。
 *
 * 为什么这些断言值得写：
 *  - **响应优先**：打字回显不能等 TTS。`submitTextTurn` 先 `startTextReveal(...)` 把字打出来，
 *    再用 `void requestReplyAudio(...)` 在后台取音频 —— 一旦有人改成 `await requestReplyAudio(`，
 *    长者打完字就要盯着空屏等一次网络往返 + 云端合成，这是这次要修的病本身。
 *  - **音频到了按真实时长重锚打字机**：`startTextReveal` 的 `durationMs` 必须优先于按字数估算，
 *    否则字已经打完、人还在说话（或反过来），口型与文字对不上。
 *  - **出声前必须解除 `muted`**：`<audio>` 在字幕（tts_text）路径上会被置静音（G-D12 竞态），
 *    打字回复走的是「先字幕、后音频」，若 `playReplyAudio` 不先 `muted = false`，音频元素在播但没声音。
 *  - **口型靠能量驱动**：`playReplyAudio` 要开说话窗（`openSpeakingWindow()`）、挂元素分析器
 *    （`ensureElementAnalyser()`）、`el.src = url` 起播、`onended` 时 `endPlayback()` 收口 ——
 *    少一步就是「有声音但嘴不动」或「声音停了嘴还在动」。
 *
 * 渲染层不测（Node 只做类型擦除、不认 TSX），行为用真函数 + 假 fetch 断言，接线用源码契约钉住。
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

// `apiDialogue.ts` 只 import `./session`（存取实现都在函数里，模块顶层不碰 storage），Node 里可安全求值。
const dialogue = await import("../src/pad/apiDialogue");

const read = (relative: string): string =>
  readFileSync(fileURLToPath(new URL(`../src/pad/${relative}`, import.meta.url)), "utf8");

/** 源码里换行/缩进不该影响契约判定。 */
const flat = (source: string): string => source.replace(/\s+/g, " ");

const API_SRC = read("apiDialogue.ts");
const REALTIME_SRC = read("realtime.ts");
const TEXT_REVEAL_SRC = read("textReveal.ts");
const APP_SRC = read("App.tsx");

/** 取某个标识符之后的一段源码（从 `index` 起 `length` 个字符），拿不到就返回空串。 */
const after = (source: string, needle: string, length = 2000): string => {
  const at = source.indexOf(needle);
  return at < 0 ? "" : source.slice(at, at + length);
};

/** 临时替换 globalThis.fetch，用例结束后务必还原（否则同进程里后面的用例全被带偏）。 */
async function withFetch<T>(
  stub: (url: string, init?: RequestInit) => Promise<Response>,
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

test("requestReplyAudio：POST /api/dialogue/tts，body 只有 text，令牌走 Bearer", async () => {
  await withFetch(
    () =>
      Promise.resolve(
        new Response(JSON.stringify({ audio_url: "/static/tts/reply.mp3", audio_duration_ms: 2400 }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    async (captured) => {
      const outcome = await dialogue.requestReplyAudio("今天天气怎么样", "token-abc");

      const request = captured();
      assert.equal(request.url, "/api/dialogue/tts", "端点写错就整条链路不通");
      assert.equal(request.init?.method, "POST");

      const headers = new Headers(request.init?.headers);
      assert.equal(headers.get("Content-Type"), "application/json");
      assert.equal(
        headers.get("Authorization"),
        "Bearer token-abc",
        "文本回复也是受保护接口：令牌必须走 Authorization，不能塞进 body 或 query",
      );

      assert.deepEqual(JSON.parse(String(request.init?.body)), { text: "今天天气怎么样" });
      assert.deepEqual(outcome, {
        ok: true,
        audioUrl: "/static/tts/reply.mp3",
        durationMs: 2400,
        segments: [],
      });
    },
  );
});

test("requestReplyAudio：把 audio_segments 逐句时间窗原样带回来（打字回复靠它对齐口型）", async () => {
  await withFetch(
    () =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            audio_url: "/static/mock/tts_1.wav",
            audio_duration_ms: 3000,
            audio_segments: [
              { text: "第一句。", start_ms: 0, duration_ms: 1200 },
              { text: "第二句。", start_ms: 1200, duration_ms: 1800 },
            ],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    async () => {
      const outcome = await dialogue.requestReplyAudio("第一句。第二句。", "token-abc");
      assert.equal(outcome.segments.length, 2);
      assert.deepEqual(outcome.segments[1], { text: "第二句。", start_ms: 1200, duration_ms: 1800 });
      // 形状不对的句子必须丢弃，而不是把 NaN 传进时间轴
      assert.equal(
        JSON.stringify(outcome.segments).includes("null"),
        false,
        "坏数据不能让时间轴出现 null/NaN",
      );
    },
  );
});

test("requestReplyAudio：char_ms 逐字坐标必须带回来（丢了就退回匀速，句中停顿会抢跑）", async () => {
  await withFetch(
    () =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            audio_url: "/static/mock/tts_1.wav",
            audio_duration_ms: 1600,
            audio_segments: [
              { text: "记得吃药", start_ms: 0, duration_ms: 1600, char_ms: [200, 400, 1400, 1600] },
            ],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    async () => {
      const outcome = await dialogue.requestReplyAudio("记得吃药", "token-abc");
      assert.deepEqual(outcome.segments[0].char_ms, [200, 400, 1400, 1600]);
    },
  );
});

test("requestReplyAudio：坐标里混进坏值就整组作废（抽掉一个会让后面的字全部错位）", async () => {
  const bad = [
    [200, "400", 1400, 1600],
    [200, -1, 1400, 1600],
    [200, Number.NaN, 1400, 1600],
    [],
    "200,400",
  ];
  for (const charMs of bad) {
    await withFetch(
      () =>
        Promise.resolve(
          new Response(
            JSON.stringify({
              audio_url: "/static/mock/tts_1.wav",
              audio_duration_ms: 1600,
              audio_segments: [
                { text: "记得吃药", start_ms: 0, duration_ms: 1600, char_ms: charMs },
              ],
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
        ),
      async () => {
        const outcome = await dialogue.requestReplyAudio("记得吃药", "token-abc");
        assert.equal(outcome.segments.length, 1, "坐标坏了不该把整句时间窗也丢掉");
        assert.equal(
          outcome.segments[0].char_ms,
          undefined,
          `坏坐标 ${JSON.stringify(charMs)} 被部分保留，会让后面的字整体错位`,
        );
      },
    );
  }
});

test("requestReplyAudio：audio_url 为 null 时返回 {ok:false,audioUrl:null}，不抛错", async () => {
  await withFetch(
    () =>
      Promise.resolve(
        new Response(JSON.stringify({ audio_url: null }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    async () => {
      const outcome = await dialogue.requestReplyAudio("在吗", "token-abc");
      assert.equal(outcome.ok, false, "没有音频是正常分支（TTS 不可用/被跳过），不能当异常抛到界面");
      assert.equal(outcome.audioUrl, null);
      assert.equal(outcome.durationMs, null);
    },
  );
});

test("源码契约：realtime.playReplyAudio 开说话窗、挂分析器、起播并在 onended 收口", () => {
  const body = after(REALTIME_SRC, "playReplyAudio(");
  assert.notEqual(body, "", "realtime.ts 必须新增公开方法 playReplyAudio(url: string): void");
  for (const marker of [
    "openSpeakingWindow()",
    "ensureElementAnalyser()",
    "el.src = url",
    "el.onended",
    "endPlayback()",
  ]) {
    assert.ok(
      body.includes(marker),
      `playReplyAudio 里缺 ${marker}：少了它就会出现「有声音但嘴不动」或「声音停了嘴还在动」`,
    );
  }
});

test("源码契约：startTextReveal 接受 options.durationMs，且优先于按字数估算", () => {
  // 契约要的是「能传真实时长」，不是某一种书写形状：内联对象类型与命名接口
  // （落地的 `TextRevealOptions`）等价，两种都认；但「有没有这个参数」必须钉死。
  const signature = flat(after(TEXT_REVEAL_SRC, "export function startTextReveal(", 400));
  assert.ok(
    /options\??:/.test(signature),
    "startTextReveal 必须接受 options 参数：音频时长才是真实播报时长，按字数估算是猜的",
  );
  assert.ok(
    /durationMs\?\s*:\s*number/.test(flat(TEXT_REVEAL_SRC)),
    "options 里必须声明 durationMs?: number（内联或命名接口都行）",
  );

  const realDurationAt = TEXT_REVEAL_SRC.indexOf("options?.durationMs");
  const estimateAt = TEXT_REVEAL_SRC.indexOf("TEXT_REVEAL_MS_PER_CHAR", realDurationAt);
  assert.ok(
    realDurationAt >= 0,
    "必须出现 options?.durationMs —— 音频到达后按真实时长重锚打字机，否则文字与声音对不上",
  );
  assert.ok(
    estimateAt < 0 || realDurationAt < estimateAt,
    "priority 反了：options.durationMs 必须先于 TEXT_REVEAL_MS_PER_CHAR 估算生效",
  );
});

test("源码契约：App.submitTextTurn 先打回显、再 void 发起取音频并播放", () => {
  const submit = after(APP_SRC, "const submitTextTurn", 4000);
  assert.notEqual(submit, "", "App.tsx 必须有 submitTextTurn");

  const revealAt = submit.indexOf("startTextReveal(");
  const requestAt = submit.indexOf("void requestReplyAudio(");
  const playAt = submit.indexOf("playReplyAudio(");

  assert.ok(revealAt >= 0, "submitTextTurn 里要先 startTextReveal(...) 把回复打出来（响应优先）");
  assert.ok(requestAt >= 0, "submitTextTurn 里必须有 void requestReplyAudio( —— 不允许 await 阻塞回显");
  assert.ok(playAt >= 0, "拿到 audioUrl 后必须调 playReplyAudio(...) 才能真正出声");
  assert.ok(revealAt < requestAt, "顺序错了：先回显、后取音频");
  assert.ok(requestAt < playAt, "先取到 audioUrl，才有 url 可以交给 playReplyAudio");
});

test("源码契约：App 把 audio.segments 交给 playReplyAudio（逐句时间轴驱动字幕）", () => {
  const submit = after(APP_SRC, "const submitTextTurn", 4000);
  assert.ok(
    /playReplyAudio\([^)]*audio\.segments/.test(flat(submit)),
    "submitTextTurn 必须把 audio.segments 传给 playReplyAudio —— 只有逐句时间窗才能让逐字动画跟着真实语音节奏走（2026-11-11 三步走第二步）",
  );
  assert.equal(
    /startTextReveal\([^)]*durationMs/.test(flat(submit)),
    false,
    "不许再用「音频总时长重锚」的旧写法：整段按平均字速铺开才是与口型不同步的根因",
  );
});

test("反向契约：App 里不许出现 await requestReplyAudio（响应优先，不许阻塞回显）", () => {
  assert.equal(
    APP_SRC.includes("await requestReplyAudio("),
    false,
    "await 它等于让长者打完字干等一次网络往返 + 云端合成：这条是这次要修的病本身",
  );
});

test("源码契约：playReplyAudio 先解除静音，再 el.play()", () => {
  const body = after(REALTIME_SRC, "playReplyAudio(");
  assert.notEqual(body, "", "realtime.ts 必须新增 playReplyAudio(url: string): void");

  // 三种等价写法都认：元素局部 `el.muted = false`、经回调的 `this.cb.audioEl.muted = false`，
  // 以及 2026-11-12 起的受控写法 `this.cb.audioEl.muted = this.speechSuppressed`（媒体在播时压住）。
  // 要钉的是「解除静音发生在起播之前」这一因果，而不是某一种访问路径或某个字面量。
  const unmuteAt = [
    body.indexOf("el.muted = false;"),
    body.indexOf("this.cb.audioEl.muted = false;"),
    body.indexOf("this.cb.audioEl.muted = this.speechSuppressed;"),
  ]
    .filter((at) => at >= 0)
    .sort((a, b) => a - b)[0];
  const playAt = body.indexOf("el.play()");
  assert.ok(
    unmuteAt !== undefined,
    "缺解除静音（el.muted = false）—— 字幕路径置过静音，不解就「在播但没声」（实测开头丢 4.21s）",
  );
  assert.ok(playAt >= 0, "必须在 playReplyAudio 里调 el.play()");
  assert.ok(
    unmuteAt < playAt,
    "顺序反了：先解除静音，再起播（G-D12 竞态：tts_text 先到会把元素置静音）",
  );
});

/* ------------------------------------------------------------------ *
 * 打字回复的字幕被「迟到开场白」闸门吞掉（2026-11-11 B8 CDP 取证）
 *
 * 现象：长者打字问「今天天气怎么样」，声音已经在答第三句，大字幕还停在长者自己
 * 那句问题上，整整 7.9 秒。根因不是时钟——`advanceSubtitle` 一直在推，是 App 的
 * `onSubtitle` 把 `source === "reply"` 一律按「迟到开场白」拦了 6 秒，而打字回复
 * 补到语音后**自己的**逐句字幕也走这条回调。
 *
 * 修法：回调带 `local` 标记（本机 <audio> 驱动 = true），闸门只拦服务端推来的。
 * 下面钉住三处接线，少一处这个 bug 就会原样回来。
 * ------------------------------------------------------------------ */

test("源码契约：打字回复的字幕带 local 标记，不被「迟到开场白」闸门吞掉", () => {
  assert.match(
    REALTIME_SRC,
    /onSubtitle: \(text: string, source: SubtitleSource, local\?: boolean\) => void;/,
    "onSubtitle 少了 local 形参：上层分不清「打字回复自己的时间轴」和「迟到的开场白」",
  );
  assert.match(
    REALTIME_SRC,
    /this\.localReply = true;/,
    "playReplyAudio 没把本轮标成 local：打字回复的逐句字幕会被上层当开场白拦掉",
  );
  assert.match(
    REALTIME_SRC,
    /case "tts_text":[\s\S]{0,200}this\.localReply = false;/,
    "服务端 tts_text 接管时没把 local 复位：下一轮语音回复会绕过开场白闸门",
  );
  assert.match(
    REALTIME_SRC,
    /this\.cb\.onSubtitle\(progress\.text, "reply", this\.localReply\);/,
    "advanceSubtitle 没把 local 传上去：逐句字幕仍会被闸门吞掉",
  );

  const guard = after(APP_SRC, "onSubtitle: (text, source, local)", 900);
  assert.notEqual(guard, "", "App 的 onSubtitle 没接 local 形参");
  assert.ok(
    /source === "reply" &&\s*!local &&/.test(guard),
    "闸门没豁免 local：打字回复自己的字幕会被拦 6 秒（实测声音答到第三句、字幕还停在问题上）",
  );
});
