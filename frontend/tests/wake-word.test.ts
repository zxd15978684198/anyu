/**
 * 唤醒词（FR-UI-11 / AC-42）的单测：把「误删门控不会有人发现」这件事堵住。
 *
 * 复跑（无需装任何依赖，Node ≥ 22）：
 *   node --import ./scripts/node-ts-hook.mjs --test frontend/tests/wake-word.test.ts
 *
 * 为什么放在 `frontend/tests/` 而不是 `src/`：`frontend/tsconfig.json` 的 `include` 是 `["src"]`
 * 且 `types` 只有 `vite/client`，把用到 `node:test` 的用例放进 src 会让 `pnpm build`（tsc --noEmit）失败；
 * 放 tests/ 则完全不进构建产物。
 *
 * 分两层：
 * 1. **行为层** —— 真实 import kws.ts，测词表编码、调参冻结、两条降级分支、状态机迁移；
 * 2. **源码契约层** —— 直接读源码断言「门控/命中/降级」三处接线还在。这一层是刻意的：
 *    门控写在 App.tsx 的 effect 里，行为层测不到，但只要有人删掉那一行，对应断言立刻变红
 *    （本轮已做删/还原变异验证，见交付报告）。
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  initWakeWord,
  KEYWORD_SCORE,
  KEYWORD_THRESHOLD,
  KwsUnavailableError,
  UnsupportedKeywordError,
  WAKE_TOKENS,
  WakeWord,
} from "../src/pad/audio/kws";
import {
  DEFAULT_WAKE_TOKENS,
  DEFAULT_WAKE_WORD,
  MAX_WAKE_WORD_CHARS,
  PRESET_WAKE_WORDS,
  describeKeywordProblem,
  splitKeyword,
} from "../src/pad/audio/pinyin";
import { loadWakeWord, saveWakeWord, wakeWordStorageKey } from "../src/pad/audio/wakeWordStore";

/** 源码里定位片段并返回 1-based 行号；找不到直接失败，错误信息带片段，便于定位被删的那一行 */
function lineOf(source: string, needle: string): number {
  const index = source.indexOf(needle);
  assert.notEqual(index, -1, `源码里找不到片段：${needle}`);
  return source.slice(0, index).split("\n").length;
}

const KWS_SRC = readFileSync(new URL("../src/pad/audio/kws.ts", import.meta.url), "utf8");
const APP_SRC = readFileSync(new URL("../src/pad/App.tsx", import.meta.url), "utf8");

test("唤醒词编码 = tokens.txt 词表里的真实 token（不是拼错也能过）", () => {
  const tokensTxt = readFileSync(
    new URL("../public/kws-assets/model/tokens.txt", import.meta.url),
    "utf8",
  );
  // tokens.txt 每行形如 `<token> <id>`，与 sherpa-onnx 的 BPE 词表同构
  const vocabulary = new Set(
    tokensTxt
      .split(/\r?\n/)
      .map((line) => line.trim().split(/\s+/)[0])
      .filter(Boolean),
  );

  assert.equal(WAKE_TOKENS.length, 4, "「小伴」= x + iǎo + b + àn，四段缺一不可");
  assert.equal(new Set(WAKE_TOKENS).size, WAKE_TOKENS.length, "token 不应重复");
  for (const token of WAKE_TOKENS) {
    assert.ok(vocabulary.has(token), `词表里没有 token「${token}」，唤醒词必然永远不命中`);
  }
});

test("调参冻结：score=1 / threshold=0.25（这两个数改了就红）", () => {
  // 实测口径：score 取 2/3 反而漏检（16/20、12/20），threshold 取 0.5 正样本 0/3；
  // 0.05~0.25 在 score=1 下等价，取 0.25 留精度余量。要调必须先有新的 20 次记录。
  assert.equal(KEYWORD_SCORE, 1);
  assert.equal(KEYWORD_THRESHOLD, 0.25);
});

test("initWakeWord：环境无麦克风 → unsupported（降级 FR-UI-09 且留日志）", async (t) => {
  const info = t.mock.method(console, "info");
  const descriptor = Object.getOwnPropertyDescriptor(navigator, "mediaDevices");
  Object.defineProperty(navigator, "mediaDevices", { value: undefined, configurable: true });

  try {
    await assert.rejects(initWakeWord(() => {}), (err: unknown) => {
      assert.ok(err instanceof KwsUnavailableError, "必须是 KwsUnavailableError，App.tsx 靠它取 reason");
      assert.equal(err.reason, "unsupported");
      return true;
    });
    const degraded = info.mock.calls.some((call) =>
      String(call.arguments[0]).includes("降级呼叫按钮"),
    );
    assert.ok(degraded, "降级必须可观测：日志里要出现「降级呼叫按钮（FR-UI-09）」");
  } finally {
    if (descriptor) Object.defineProperty(navigator, "mediaDevices", descriptor);
    else Reflect.deleteProperty(navigator, "mediaDevices");
  }
});

test("initWakeWord：麦克风在、运行时资产不可用 → assets_unavailable（仍然可观测）", async (t) => {
  const warn = t.mock.method(console, "warn");
  const descriptor = Object.getOwnPropertyDescriptor(navigator, "mediaDevices");
  Object.defineProperty(navigator, "mediaDevices", {
    value: { getUserMedia: async () => ({}) },
    configurable: true,
  });

  try {
    // Node 里没有 /kws-assets/kws.js，loadRuntime 的动态 import 必然失败
    await assert.rejects(initWakeWord(() => {}), (err: unknown) => {
      assert.ok(err instanceof KwsUnavailableError);
      assert.equal(err.reason, "assets_unavailable");
      return true;
    });
    const warned = warn.mock.calls.some((call) => {
      const text = call.arguments.map(String).join(" ");
      return text.includes("降级呼叫按钮") && text.includes("assets_unavailable");
    });
    assert.ok(warned, "降级必须带 reason 落日志，禁止静默什么都不做");
  } finally {
    if (descriptor) Object.defineProperty(navigator, "mediaDevices", descriptor);
    else Reflect.deleteProperty(navigator, "mediaDevices");
  }
});

test("WakeWord 状态机：未监听时 pause/resume 无副作用、stop 幂等且不可复活", async (t) => {
  t.mock.method(console, "info");
  let woke = 0;
  const word = new WakeWord(() => {
    woke += 1;
  });

  assert.equal(word.state, "loading");
  word.pause();
  assert.equal(word.state, "loading", "loading 阶段 pause 不应改变状态");
  word.resume();
  assert.equal(word.state, "loading", "非 paused 的 resume 不应把状态顶成 listening");

  await word.stop();
  assert.equal(word.state, "stopped");
  await word.stop();
  assert.equal(word.state, "stopped", "stop 必须幂等（cleanup 可能被调用两次）");

  word.resume();
  assert.equal(word.state, "stopped", "stop 之后 resume 不能复活监听（否则麦克风会泄漏）");
  word.pause();
  assert.equal(word.state, "stopped");
  assert.equal(woke, 0, "没有命中就不该回调 onWake");
});

test("契约：媒体由 awake 门控、信令常驻（把 `if (!awake) return;` 加回去就红）", () => {
  // N62b 改了口径：信令必须常驻——否则待机时长者端收不到外呼 offer，护工永远呼不进来
  // （FR-UI-10）。FR-UI-11 约束的是「未命中唤醒词时原始音频不出端」，不是信令，
  // 所以门控从「建连」下沉到「媒体」：App 把 awake 交给 realtime，由它决定开不开麦。
  assert.equal(
    APP_SRC.includes("if (!awake) return;"),
    false,
    "App.tsx 不许再用 awake 挡住建连：信令常驻才可能被外呼叫到",
  );
  assert.match(
    APP_SRC,
    /clientRef\.current\?\.setAwake\(awake\)/,
    "App.tsx 必须把 awake 交给 realtime 门控媒体，否则唤醒后麦克风不会开",
  );

  // 唤醒回调 → setAwake(true)
  // 锚点只钉到 `initWakeWord(`：task-70 之后这个调用多了第二个实参（{ keyword }），
  // 若继续钉 `initWakeWord(() => {`，将来任何换行/加参都会让这条守卫无声失效。
  const wakeCbAt = APP_SRC.indexOf("initWakeWord(");
  assert.notEqual(wakeCbAt, -1, "App.tsx 必须调用 initWakeWord");
  assert.match(
    APP_SRC.slice(wakeCbAt, wakeCbAt + 300),
    /if \(alive\) setAwake\(true\);/,
    "唤醒回调必须 setAwake(true)，否则命中了也不建连",
  );

  // 降级分支：KwsUnavailableError → 仍然建连（FR-UI-09）
  const degradeAt = APP_SRC.indexOf("console.warn(`[pad] 唤醒词不可用");
  assert.notEqual(degradeAt, -1, "降级分支的日志被删了：降级必须可观测");
  // 窗口从 300 放宽到 700：A6 在 setWakeFallback 与 setAwake 之间插了「麦克风不可用」
  // 处理，300 字窗口会把 setAwake(true) 挤出去（断言本身没错，锚点太窄）。
  assert.match(
    APP_SRC.slice(degradeAt, degradeAt + 700),
    /if \(alive\) setAwake\(true\);/,
    "降级分支若不再 setAwake(true)，长者端会黑屏只剩一块砖",
  );
});

test("契约：命中 → 回调链路完整（删掉 `this.wake();` 就红）", () => {
  const hitsLine = lineOf(KWS_SRC, "if (hits.length === 0) return;");
  const wakeLine = lineOf(KWS_SRC, "this.wake();");
  assert.ok(wakeLine > hitsLine, "命中后必须调用 this.wake()，否则明明命中了也永远不唤醒");

  assert.match(
    KWS_SRC,
    /if \(this\.state_ === "woken" \|\| this\.state_ === "stopped"\) return;/,
    "wake() 的双触发守卫不能删：麦克风会每 60ms 再送一帧命中",
  );
  assert.match(KWS_SRC, /this\.onWake\(\);/, "wake() 最终必须回调 onWake，否则整条链路是空的");

  // 采集帧只有一个去处：handleFrame（原始音频不做第二份拷贝/第二个消费者）
  assert.match(KWS_SRC, /capture\.onFrame = \(pcm\) => this\.handleFrame\(pcm\);/);
});

test("契约：kws.ts 不做任何上行（未命中音频不出端，PRD:257 隐私红线）", () => {
  for (const banned of [
    "WebSocket",
    "sendBeacon",
    "XMLHttpRequest",
    "EventSource",
    "RealtimeClient",
  ]) {
    assert.equal(
      KWS_SRC.includes(banned),
      false,
      `kws.ts 出现 ${banned}：唤醒前的音频可能被上送，违反 FR-UI-11「未命中不出端」`,
    );
  }

  // fetch 只允许用于静态模型包（/kws-assets/…），不允许出现在逐帧处理函数里
  const frameStart = KWS_SRC.indexOf("private handleFrame(");
  const frameEnd = KWS_SRC.indexOf("private wake(): void");
  assert.ok(frameStart > 0 && frameEnd > frameStart);
  assert.equal(
    KWS_SRC.slice(frameStart, frameEnd).includes("fetch("),
    false,
    "handleFrame 里出现 fetch：逐帧音频被上送了",
  );
});

// ---------------------------------------------------------------------------
// task-70：唤醒词自定义（登录后设置）
//
// 这一组锁三件事，任何一件被改动都会立刻变红：
// 1. 出厂词与切分口径 —— 我们不是「猜拼音」，而是按 tokens.txt 的 191 个拼音 token 切；
// 2. 设备级持久化 —— 换台设备/换个账号不能串词，脏数据要回退出厂词而不是让唤醒失灵；
// 3. 源码契约 —— kws.ts 不许再写死唤醒词，切换必须走 setKeyword（重建解码器，不重启页面）。
// ---------------------------------------------------------------------------

/** tokens.txt 的词表（`<token> <id>` 每行一个），多处复用 */
function readVocabulary(): Set<string> {
  const tokensTxt = readFileSync(
    new URL("../public/kws-assets/model/tokens.txt", import.meta.url),
    "utf8",
  );
  return new Set(
    tokensTxt
      .split(/\r?\n/)
      .map((line) => line.trim().split(/\s+/)[0] ?? "")
      .filter((token) => token.length > 0),
  );
}

test("T70 切分口径：小伴 = x iǎo b àn，出厂词没被悄悄换掉", () => {
  assert.deepEqual(splitKeyword("小伴"), ["x", "iǎo", "b", "àn"]);
  assert.deepEqual([...DEFAULT_WAKE_TOKENS], ["x", "iǎo", "b", "àn"]);
  assert.deepEqual([...WAKE_TOKENS], ["x", "iǎo", "b", "àn"]);
  assert.equal(DEFAULT_WAKE_WORD, "小伴");
});

test("T70 六个预设词全部能切分，且每个 token 都在 tokens.txt 里", () => {
  const vocabulary = readVocabulary();
  assert.equal(PRESET_WAKE_WORDS.length, 6, "预设词数量变了：设置页与文档要同步");
  for (const word of PRESET_WAKE_WORDS) {
    const tokens = splitKeyword(word);
    assert.ok(tokens !== null, `预设词「${word}」切不出 token，说明内置拼音表与词表脱节`);
    assert.ok(tokens!.length > 0);
    for (const token of tokens!) {
      assert.equal(vocabulary.has(token), true, `「${word}」的 token「${token}」不在 tokens.txt 里`);
    }
  }
});

test("T70 切不出来就返回 null（空串 / 英文 / 生僻字 / 超长）", () => {
  assert.equal(splitKeyword(""), null, "空串不该被当成唤醒词");
  assert.equal(splitKeyword("Q"), null, "英文不该被硬塞进拼音 token");
  assert.equal(splitKeyword("𠮷"), null, "GB2312 外的生僻字没有内置读音，必须返回 null 让上层提示");
  assert.equal(
    splitKeyword("伴".repeat(MAX_WAKE_WORD_CHARS + 1)),
    null,
    "超过上限还返回 token：kws 每帧要跑 4 条 active path，长词会拖慢推理",
  );
  assert.ok(splitKeyword("伴".repeat(MAX_WAKE_WORD_CHARS)) !== null, "正好等于上限应当允许");
});

test("T70 失败原因给中文提示（不是静默返回 null）", () => {
  assert.match(describeKeywordProblem("") ?? "", /请先输入/);
  assert.match(describeKeywordProblem("Q") ?? "", /不在词表里/);
  assert.match(describeKeywordProblem("𠮷") ?? "", /不在词表里/);
  assert.match(describeKeywordProblem("伴".repeat(MAX_WAKE_WORD_CHARS + 1)) ?? "", /最多 8 个字/);
  assert.equal(describeKeywordProblem("小伴"), null, "能切分的词不该报错");
});

test("T70 自定义词真的进了 KWS 配置，切不出来抛 UnsupportedKeywordError", () => {
  const custom = new WakeWord(() => {}, { keyword: "康康" });
  assert.equal(custom.keyword, "康康");
  assert.deepEqual([...custom.tokens], ["k", "āng", "k", "āng"]);
  assert.equal(custom.state, "loading", "还没 start() 就离开了 loading：状态机被改了");

  assert.throws(
    () => new WakeWord(() => {}, { keyword: "𠮷" }),
    UnsupportedKeywordError,
    "生僻字应当显式抛错：静默忽略会让长者以为设置成功了",
  );
});

test("T70 按 device 持久化：存进去能读回、脏值回退默认、不同设备互不串", () => {
  const store = new Map<string, string>();
  const original = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => {
        store.set(key, value);
      },
      removeItem: (key: string) => {
        store.delete(key);
      },
    },
  });
  try {
    assert.equal(loadWakeWord("dev-a"), DEFAULT_WAKE_WORD, "没存过应当是出厂词");

    saveWakeWord("dev-a", "康康");
    assert.equal(store.get(wakeWordStorageKey("dev-a")), "康康");
    assert.equal(wakeWordStorageKey("dev-a").includes("dev-a"), true, "存储键必须带 device 维度");
    assert.equal(loadWakeWord("dev-a"), "康康");
    assert.equal(loadWakeWord("dev-b"), DEFAULT_WAKE_WORD, "不同设备之间串词了");

    store.set(wakeWordStorageKey("dev-a"), "𠮷");
    assert.equal(loadWakeWord("dev-a"), DEFAULT_WAKE_WORD, "存坏的字应当回退出厂词，而不是让唤醒失灵");

    store.set(wakeWordStorageKey("dev-a"), "");
    assert.equal(loadWakeWord("dev-a"), DEFAULT_WAKE_WORD);
  } finally {
    if (original) Object.defineProperty(globalThis, "localStorage", original);
    else delete (globalThis as { localStorage?: unknown }).localStorage;
  }
});

test("T70 源码契约：kws.ts 不再写死唤醒词，App 走 loadWakeWord + setKeyword", () => {
  assert.equal(
    KWS_SRC.includes("WAKE_TOKENS = ["),
    false,
    "kws.ts 里还有写死的 WAKE_TOKENS 字面量：自定义词会被覆盖",
  );
  assert.equal(
    KWS_SRC.includes('const WAKE_WORD = "小伴"'),
    false,
    "kws.ts 里还有写死的 WAKE_WORD：命中日志/配置会与实际词不一致",
  );
  assert.equal(
    KWS_SRC.includes("setKeyword(keyword: string)"),
    true,
    "kws.ts 缺 setKeyword：换词只能重启页面",
  );

  assert.equal(APP_SRC.includes("loadWakeWord("), true, "App 没有读取已保存的唤醒词");
  assert.equal(APP_SRC.includes("setKeyword("), true, "App 保存后没有调用运行时切换");
  assert.equal(APP_SRC.includes("<WakeWordSettings"), true, "设置面板没有挂到页面上");
});

test("T70 组件契约：触控尺寸够大、预设词与错误提示都在", () => {
  const settingsSrc = readFileSync(
    new URL("../src/pad/components/WakeWordSettings.tsx", import.meta.url),
    "utf8",
  );
  assert.equal(settingsSrc.includes("min-h-14"), true, "触控目标小于 44px（min-h-14 = 56px）");
  assert.equal(settingsSrc.includes("PRESET_WAKE_WORDS"), true, "预设词列表被删了");
  assert.equal(settingsSrc.includes('role="alert"'), true, "不可切分时没有可被读屏播报的错误提示");
  assert.equal(settingsSrc.includes("describeKeywordProblem"), true, "保存前没有做可切分校验");
  assert.equal(settingsSrc.includes("saveWakeWord"), true, "设置没有落盘");
});
