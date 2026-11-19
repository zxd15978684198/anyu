/**
 * kws.ts（唤醒词）可「删坏」证据。
 *
 * 为什么放在 `frontend/tests/` 而不是 `frontend/src/pad/audio/`：
 * `tsconfig.json` 是 `"types": ["vite/client"]` + `"include": ["src"]`，项目又不新增 `@types/node`；
 * 把 `node:test` 用例放进 `src/` 会让 `tsc --noEmit` 直接报 `TS2307 Cannot find module 'node:test'`，
 * 连带 `pnpm build`（= tsc --noEmit && vite build）变红。放到 `tests/` 后产品构建不受影响，
 * 门禁的 glob 已扩成 src 与 tests 两个目录下的 test 文件，照样收得到。
 *
 * 为什么不用 vitest / jsdom：
 * AGENTS §4-12 禁止擅自引入依赖。这里用 Node 自带的 `node:test` + `node:assert`，
 * 浏览器侧能力（navigator.mediaDevices、AudioContext、AudioWorkletNode）用最小 fake 顶上，
 * 这样测的就是 kws.ts / capture.ts 的**真实代码路径**，而不是另写一份逻辑。
 *
 * 为什么能测到「先释放、再回调」：
 * wake() 里 `void this.disposeResources().finally(() => this.onWake())`。
 * 把假的 spotter.dispose 与假 capture.stop 写进同一个 `order` 数组，再断言 `onWake` 在最后，
 * 就能把「顺序调换」这种回归打死（见下方「命中后必须先释放资源」用例）。
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { registerHooks } from "node:module";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, test } from "node:test";

// ---------------------------------------------------------------------------
// 1. 把 kws.ts 里那句动态 `import("/kws-assets/kws.js")` 换成内存里的假运行时。
//    为什么在加载层拦而不是改产品代码：AC 要求测的是**产品那份代码**；这里只替换
//    「sherpa-onnx wasm 运行时」这个外部依赖，kws.ts 的加载、错误归一化、
//    状态机、命中回调整条链路都还是真的。
// ---------------------------------------------------------------------------
const RUNTIME_SPECIFIER = "/kws-assets/kws.js";

const FAKE_RUNTIME_SOURCE = `
export async function initKWSModule() {
  const fake = globalThis.__KWS_FAKE__;
  if (!fake) throw new Error("__KWS_FAKE__ 未注入");
  return { FS: { fake: true } };
}
export function loadModelPack() {
  return ["/encoder.onnx", "/decoder.onnx", "/joiner.onnx", "/tokens.txt"];
}
export function createKeywordSpotter(module, config) {
  const fake = globalThis.__KWS_FAKE__;
  globalThis.__KWS_LAST_CONFIG__ = config;
  globalThis.__KWS_LAST_MODULE__ = module;
  return fake.createSpotter(config);
}
`;

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier !== RUNTIME_SPECIFIER) return nextResolve(specifier, context);
    // 模拟「资产下不下来」：交给 Node 正常解析，必然找不到 → loadRuntime 抛 assets_unavailable
    if (globalThis.__KWS_RUNTIME_MISSING__) return nextResolve(specifier, context);
    const encoded = Buffer.from(FAKE_RUNTIME_SOURCE, "utf8").toString("base64");
    return { url: `data:text/javascript;base64,${encoded}`, shortCircuit: true };
  },
});

const kws = await import("../src/pad/audio/kws");
const { initWakeWord, WakeWord, KwsUnavailableError, WAKE_TOKENS, KEYWORD_SCORE, KEYWORD_THRESHOLD } = kws;

const TOKENS_FILE = fileURLToPath(new URL("../public/kws-assets/model/tokens.txt", import.meta.url));
const KWS_SOURCE_FILE = fileURLToPath(new URL("../src/pad/audio/kws.ts", import.meta.url));

// ---------------------------------------------------------------------------
// 2. 最小浏览器 fake：只提供 capture.ts 真正用到的那些方法，行为可观测（都往 order 里记一笔）。
// ---------------------------------------------------------------------------
const REAL_GLOBALS = {
  navigatorDescriptor: Object.getOwnPropertyDescriptor(globalThis, "navigator"),
  fetch: globalThis.fetch,
  AudioContext: globalThis.AudioContext,
  AudioWorkletNode: globalThis.AudioWorkletNode,
};

function setMediaDevices(mediaDevices) {
  // Node 26 自带 navigator（有 userAgent，但没有 mediaDevices）。直接给 navigator 挂属性，
  // 比整体替换 navigator 更贴近真实浏览器，也不影响 Node 自己的其它 navigator 字段。
  const target = globalThis.navigator;
  if (!target) {
    Object.defineProperty(globalThis, "navigator", { value: { mediaDevices }, configurable: true, writable: true });
    return () => delete globalThis.navigator;
  }
  Object.defineProperty(target, "mediaDevices", { value: mediaDevices, configurable: true, writable: true });
  return () => {
    delete target.mediaDevices;
  };
}

/**
 * 装一套假浏览器，返回可供断言的事件顺序。
 * @param {{ micError?: Error }} options micError 非空则 getUserMedia 直接抛错（模拟用户拒绝授权）
 */
function installFakeBrowser(options = {}) {
  const order = [];
  const workletNodes = [];
  const stream = {
    getTracks: () => [{ stop: () => order.push("track.stop") }],
  };

  const mediaDevices = {
    getUserMedia: async () => {
      order.push("getUserMedia");
      if (options.micError) throw options.micError;
      return stream;
    },
    addEventListener() {},
    removeEventListener() {},
  };
  const restoreNavigator = setMediaDevices(mediaDevices);

  class FakeAudioContext {
    constructor() {
      this.state = "running";
      this.audioWorklet = {
        addModule: async () => {
          order.push("addModule");
        },
      };
    }
    createMediaStreamSource() {
      return {
        connect: () => {
          order.push("source.connect");
        },
      };
    }
    async close() {
      order.push("ctx.close");
    }
  }

  class FakeAudioWorkletNode {
    constructor() {
      this.port = {
        onmessage: null,
        postMessage() {},
        close: () => order.push("port.close"),
      };
      workletNodes.push(this);
    }
    connect() {}
    disconnect() {
      order.push("node.disconnect");
    }
  }

  globalThis.AudioContext = FakeAudioContext;
  globalThis.AudioWorkletNode = FakeAudioWorkletNode;

  return {
    order,
    mediaDevices,
    /** 往「麦克风」里灌一帧 16k PCM16（走 capture.ts 真实的 port.onmessage → onFrame 通路） */
    feed(samples) {
      const node = workletNodes.at(-1);
      assert.ok(node, "尚未创建 AudioWorkletNode，无法灌帧");
      node.port.onmessage({ data: Int16Array.from(samples).buffer });
    },
    restore() {
      restoreNavigator();
      if (REAL_GLOBALS.AudioContext === undefined) delete globalThis.AudioContext;
      else globalThis.AudioContext = REAL_GLOBALS.AudioContext;
      if (REAL_GLOBALS.AudioWorkletNode === undefined) delete globalThis.AudioWorkletNode;
      else globalThis.AudioWorkletNode = REAL_GLOBALS.AudioWorkletNode;
    },
  };
}

/** 假模型包下载：fetchModelPack 会先取 metadata 再取 data。 */
function stubModelPackFetch() {
  globalThis.fetch = async (url) => {
    const target = String(url);
    if (target.endsWith("preload.js.metadata")) {
      return { ok: true, status: 200, json: async () => ({ files: [] }) };
    }
    if (target.endsWith("preload.data")) {
      return { ok: true, status: 200, arrayBuffer: async () => new ArrayBuffer(8) };
    }
    throw new Error(`测试里出现了意外的请求：${target}`);
  };
}

/**
 * 假 sherpa-onnx 运行时：createKeywordSpotter 的行为由本函数控制。
 * spotter.queue 里预置 detection，下一次 processAudio 就返回它们 —— 用来模拟「听见小伴」。
 * order 与假浏览器共用同一个数组，才能比较「释放」与「回调」的先后。
 */
function installFakeRuntime(options = {}) {
  const order = options.order ?? [];
  const spotter = {
    processed: 0,
    resets: 0,
    disposed: 0,
    lastRate: 0,
    queue: [],
    processAudio(samples, rate) {
      this.processed += 1;
      this.lastRate = rate;
      return this.queue.splice(0);
    },
    reset() {
      this.resets += 1;
    },
    dispose() {
      this.disposed += 1;
      order.push("spotter.dispose");
    },
  };

  globalThis.__KWS_FAKE__ = {
    createSpotter(config) {
      if (options.createError) throw options.createError;
      return spotter;
    },
  };

  return { spotter, order };
}

function captureConsole() {
  const lines = { info: [], warn: [], error: [] };
  const original = { info: console.info, warn: console.warn, error: console.error };
  console.info = (...args) => lines.info.push(args.join(" "));
  console.warn = (...args) => lines.warn.push(args.join(" "));
  console.error = (...args) => lines.error.push(args.join(" "));
  return {
    lines,
    restore() {
      console.info = original.info;
      console.warn = original.warn;
      console.error = original.error;
    },
  };
}

async function waitFor(predicate, timeoutMs = 3000, label = "条件") {
  const started = Date.now();
  while (!predicate()) {
    if (Date.now() - started > timeoutMs) throw new Error(`等待超时：${label}`);
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

let activeBrowser = null;
let activeConsole = null;

beforeEach(() => {
  delete globalThis.__KWS_RUNTIME_MISSING__;
  delete globalThis.__KWS_FAKE__;
  delete globalThis.__KWS_LAST_CONFIG__;
});

afterEach(() => {
  activeBrowser?.restore();
  activeBrowser = null;
  activeConsole?.restore();
  activeConsole = null;
  globalThis.fetch = REAL_GLOBALS.fetch;
  if (REAL_GLOBALS.navigatorDescriptor) {
    Object.defineProperty(globalThis, "navigator", REAL_GLOBALS.navigatorDescriptor);
  }
  delete globalThis.__KWS_RUNTIME_MISSING__;
  delete globalThis.__KWS_FAKE__;
});

// ---------------------------------------------------------------------------
// 3. 唤醒词编码契约：这四个 token 一旦被人「顺手改掉」，唤醒会静默失效（不报错、不崩）。
//    所以既断言字面值，也回查 tokens.txt 词表 —— 写错一个字（比如 "iao" 少了 ǐ）就红。
// ---------------------------------------------------------------------------
test("唤醒词编码契约：x iǎo b àn 四 token 都在 sherpa 词表里", () => {
  assert.deepEqual([...WAKE_TOKENS], ["x", "iǎo", "b", "àn"]);

  const vocabulary = new Set(
    readFileSync(TOKENS_FILE, "utf8")
      .split(/\r?\n/)
      .map((line) => line.trim().split(/\s+/)[0])
      .filter(Boolean),
  );
  assert.ok(vocabulary.size > 100, `tokens.txt 解析可疑，只读到 ${vocabulary.size} 个 token`);
  for (const token of WAKE_TOKENS) {
    assert.ok(vocabulary.has(token), `唤醒词 token「${token}」不在 tokens.txt 里，唤醒永远不会命中`);
  }
});

test("关键词参数契约：score=1、threshold=0.25（实测最优，被调大会漏检）", () => {
  assert.equal(KEYWORD_SCORE, 1);
  assert.equal(KEYWORD_THRESHOLD, 0.25);
});

test("关键词参数真的传给了检测器（不是只写了个常量没人用）", async () => {
  activeBrowser = installFakeBrowser();
  activeConsole = captureConsole();
  stubModelPackFetch();
  const { spotter } = installFakeRuntime();

  const handle = await initWakeWord(() => {});
  try {
    const config = globalThis.__KWS_LAST_CONFIG__;
    assert.ok(config, "createKeywordSpotter 从未被调用");
    assert.equal(config.model.encoder, "/encoder.onnx");
    assert.equal(config.model.decoder, "/decoder.onnx");
    assert.equal(config.model.joiner, "/joiner.onnx");
    assert.equal(config.model.tokens, "/tokens.txt");
    assert.equal(config.keywords.length, 1);
    assert.equal(config.keywords[0].label, "小伴");
    assert.deepEqual(config.keywords[0].matches[0].tokens, [...WAKE_TOKENS]);
    assert.equal(config.keywords[0].matches[0].score, KEYWORD_SCORE);
    assert.equal(config.keywords[0].matches[0].threshold, KEYWORD_THRESHOLD);
    assert.ok(spotter.processed >= 1, "预热帧没有送进检测器");
    assert.equal(spotter.lastRate, 16000);
  } finally {
    await handle.stop();
  }
});

// ---------------------------------------------------------------------------
// 4. 四类 KwsUnavailableError.reason：上层（App.tsx）靠 reason 决定降级文案与呼叫按钮，
//    所以每条失败路径都必须给出正确的 reason，并且**一定有日志**（静默降级 = 线上无法排查）。
// ---------------------------------------------------------------------------
test("reason=unsupported：环境没有 mediaDevices → 降级且打出呼叫按钮日志", async () => {
  activeConsole = captureConsole();
  // 故意不装假 mediaDevices：Node 里 navigator 存在但没有 mediaDevices，正好覆盖「不支持」分支
  await assert.rejects(
    () => initWakeWord(() => {}),
    (err) => {
      assert.ok(err instanceof KwsUnavailableError, "应当抛 KwsUnavailableError");
      assert.equal(err.reason, "unsupported");
      assert.equal(err.name, "KwsUnavailableError");
      return true;
    },
  );
  assert.ok(
    activeConsole.lines.info.some((line) => line.includes("降级呼叫按钮（FR-UI-09）")),
    "降级必须在日志里可见（可观测性要求）",
  );
});

test("reason=assets_unavailable：运行时 JS 取不到 → 降级且有 warn", async () => {
  activeBrowser = installFakeBrowser();
  activeConsole = captureConsole();
  globalThis.__KWS_RUNTIME_MISSING__ = true;

  await assert.rejects(
    () => initWakeWord(() => {}),
    (err) => {
      assert.equal(err.reason, "assets_unavailable");
      return true;
    },
  );
  assert.ok(
    activeConsole.lines.warn.some((line) => line.includes("reason=assets_unavailable") && line.includes("降级呼叫按钮")),
    "降级 warn 必须带 reason，否则排障只能猜",
  );
});

test("reason=assets_unavailable：模型元数据 404 → 降级（不是挂在超时上）", async () => {
  activeBrowser = installFakeBrowser();
  activeConsole = captureConsole();
  globalThis.__KWS_FAKE__ = { createSpotter: () => assert.fail("模型都没下来，不应创建检测器") };
  globalThis.fetch = async () => ({ ok: false, status: 404, json: async () => ({}) });

  await assert.rejects(
    () => initWakeWord(() => {}),
    (err) => {
      assert.equal(err.reason, "assets_unavailable");
      assert.match(err.message, /404/);
      return true;
    },
  );
});

test("reason=init_failed：检测器创建失败 → 降级且 handle 被停掉", async () => {
  activeBrowser = installFakeBrowser();
  activeConsole = captureConsole();
  stubModelPackFetch();
  installFakeRuntime({ createError: new Error("onnx 权重损坏") });

  await assert.rejects(
    () => initWakeWord(() => {}),
    (err) => {
      assert.equal(err.reason, "init_failed");
      assert.match(err.message, /KWS 初始化失败/);
      return true;
    },
  );
  assert.ok(
    activeConsole.lines.warn.some((line) => line.includes("reason=init_failed")),
    "初始化失败的日志必须可查",
  );
});

test("reason=mic_unavailable：麦克风被拒绝 → 降级（不能让用户面对一个没反应的页面）", async () => {
  activeBrowser = installFakeBrowser({ micError: new Error("permission denied") });
  activeConsole = captureConsole();
  stubModelPackFetch();
  installFakeRuntime();

  await assert.rejects(
    () => initWakeWord(() => {}),
    (err) => {
      assert.equal(err.reason, "mic_unavailable");
      assert.match(err.message, /麦克风/);
      return true;
    },
  );
  assert.ok(
    activeConsole.lines.warn.some((line) => line.includes("reason=mic_unavailable")),
    "麦克风失败的日志必须可查",
  );
});

// ---------------------------------------------------------------------------
// 5. 状态机与命中链路（真实 kws.ts + capture.ts，只替换 wasm 运行时与浏览器能力）
// ---------------------------------------------------------------------------
test("状态机：loading → listening → paused → listening → woken", async () => {
  activeBrowser = installFakeBrowser();
  activeConsole = captureConsole();
  stubModelPackFetch();
  const { spotter } = installFakeRuntime();

  const onWakeCalls = [];
  const handle = await initWakeWord(() => onWakeCalls.push(Date.now()));
  assert.ok(handle instanceof WakeWord);
  assert.equal(handle.state, "listening", "麦克风启动成功后必须进入 listening");

  handle.pause();
  assert.equal(handle.state, "paused");

  const processedBeforeFrame = spotter.processed;
  activeBrowser.feed([0, 10, -10, 0]);
  assert.equal(spotter.processed, processedBeforeFrame, "paused 时不应再送检测器（TTS 期间必须真的停）");

  const resetsBeforeResume = spotter.resets;
  handle.resume();
  assert.equal(handle.state, "listening");
  assert.ok(spotter.resets > resetsBeforeResume, "resume 必须 reset 检测器，否则会接上暂停前的半截音频");

  spotter.queue.push({ label: "小伴", tokens: [...WAKE_TOKENS], startTime: 0, timestamps: [] });
  activeBrowser.feed([0, 1200, -1200, 0]);

  await waitFor(() => onWakeCalls.length === 1, 3000, "命中回调");
  assert.equal(handle.state, "woken");
});

test("命中后必须先释放资源、再回调 onWake（顺序调换即红）", async () => {
  activeBrowser = installFakeBrowser();
  activeConsole = captureConsole();
  stubModelPackFetch();
  const { spotter, order } = installFakeRuntime({ order: activeBrowser.order });

  const onWakeCalls = [];
  const handle = await initWakeWord(() => {
    order.push("onWake");
    onWakeCalls.push(Date.now());
  });

  spotter.queue.push({ label: "小伴", tokens: [...WAKE_TOKENS], startTime: 0, timestamps: [] });
  activeBrowser.feed([0, 1200, -1200, 0]);

  // 关键断言 1：回调不能同步发生 —— 必须等 disposeResources() 走完
  assert.equal(onWakeCalls.length, 0, "命中瞬间就回调了，说明没有先释放麦克风");
  // 关键断言 2：此刻检测器已经释放（dispose 在 disposeResources 的同步段里）
  assert.ok(order.includes("spotter.dispose"), "命中后应先释放检测器");

  await waitFor(() => onWakeCalls.length === 1, 3000, "命中回调");

  const wakeIndex = order.indexOf("onWake");
  assert.ok(wakeIndex >= 0, "onWake 没有被调用");
  assert.equal(wakeIndex, order.length - 1, `onWake 必须是最后一步，实际顺序：${order.join(" → ")}`);
  for (const stage of ["spotter.dispose", "port.close", "node.disconnect", "track.stop", "ctx.close"]) {
    const index = order.indexOf(stage);
    assert.ok(index >= 0, `释放链路缺少一步：${stage}`);
    assert.ok(index < wakeIndex, `「${stage}」发生在 onWake 之后，违反「先释放再回调」`);
  }
});

test("命中后不再重复回调（woken 是终态，重复帧不能把页面拉走两遍）", async () => {
  activeBrowser = installFakeBrowser();
  activeConsole = captureConsole();
  stubModelPackFetch();
  const { spotter } = installFakeRuntime();

  const onWakeCalls = [];
  const handle = await initWakeWord(() => onWakeCalls.push(Date.now()));

  spotter.queue.push({ label: "小伴", tokens: [...WAKE_TOKENS], startTime: 0, timestamps: [] });
  activeBrowser.feed([0, 1200, -1200, 0]);
  await waitFor(() => onWakeCalls.length === 1);

  // 后续帧与显式再次 wake 都不应重复回调
  activeBrowser.feed([0, 1200, -1200, 0]);
  handle.wake();
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(onWakeCalls.length, 1);
  assert.equal(handle.state, "woken");
});

test("stop 幂等，且 stopped 之后 resume 不能把监听复活", async () => {
  activeBrowser = installFakeBrowser();
  activeConsole = captureConsole();
  stubModelPackFetch();
  const { spotter } = installFakeRuntime();

  const onWakeCalls = [];
  const handle = await initWakeWord(() => onWakeCalls.push(Date.now()));

  await handle.stop();
  assert.equal(handle.state, "stopped");
  await handle.stop();
  assert.equal(handle.state, "stopped");
  assert.equal(spotter.disposed, 1, "stop 两次不应重复 dispose（会触发 sherpa 侧重复释放）");

  handle.resume();
  assert.equal(handle.state, "stopped", "stopped 是终态，不能被 resume 拉回 listening");

  const processedBefore = spotter.processed;
  activeBrowser.feed([0, 1200, -1200, 0]);
  assert.equal(spotter.processed, processedBefore, "停止后仍在处理音频帧");
  assert.equal(onWakeCalls.length, 0);
});

// ---------------------------------------------------------------------------
// 6. 隐私契约（PRD:257）：唤醒词阶段的音频只能留在本机。
//    这属于「删掉一行就静默违规」的条款，必须由用例把住。
// ---------------------------------------------------------------------------
test("隐私契约：kws.ts 不出现任何上行通道，且只从 /kws-assets 取资产", () => {
  const source = readFileSync(KWS_SOURCE_FILE, "utf8");

  for (const banned of ["WebSocket", "sendBeacon", "XMLHttpRequest", "EventSource", "RTCPeerConnection"]) {
    assert.ok(!source.includes(banned), `kws.ts 里出现了上行/联网能力「${banned}」，违反 PRD:257`);
  }

  // 模型资产是用变量拼的（`${MODEL_ASSETS}/preload.data`），所以既查变量定义也查调用点：
  // 一旦有人加一个「把音频送到服务端」的请求，这里必然出现非 MODEL_ASSETS 的地址。
  const modelUrls = [...source.matchAll(/const\s+(\w+)\s*=\s*`([^`]+)`;/g)]
    .map((match) => ({ name: match[1], value: match[2] }))
    .filter((entry) => entry.value.includes("preload"));
  assert.ok(modelUrls.length >= 2, "没有找到模型资产地址，用例可能已失效");
  for (const entry of modelUrls) {
    assert.ok(
      entry.value.startsWith("${MODEL_ASSETS}"),
      `模型资产地址「${entry.value}」没走 /kws-assets/model，可能被改到了服务端`,
    );
  }

  const callArgs = [...source.matchAll(/fetchWithTimeout\(\s*([^)]+?)\s*\)/g)]
    .map((match) => match[1])
    .filter((arg) => !arg.includes(":"));
  assert.ok(callArgs.length >= 2, "没有找到模型下载调用，用例可能已失效");
  for (const arg of callArgs) {
    assert.ok(
      modelUrls.some((entry) => entry.name === arg),
      `fetchWithTimeout 的实参「${arg}」不是模型资产变量：唤醒阶段的任何数据都不得离开端侧`,
    );
  }
});
