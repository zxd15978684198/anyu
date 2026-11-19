/**
 * 长者端两级降级入口的守卫用例（task-69）。
 *
 * 背景：`HoldToTalk` 早已存在，但它只在 `App.tsx` 的 `{status === "disconnected" && (...)}`
 * 里渲染 —— 主链路一正常，老人就再也看不到「按住说话」；「页面输入框」这一级则根本没有。
 * 于是本轮修的不只是「加一个组件」，而是**入口不再随连接态消失**这件事。
 *
 * 为什么测纯函数 + 源码契约，而不渲染组件：
 * Node 的 `--test` 只做类型擦除、不认 TSX，仓库又不许引 jsdom/vitest（AGENTS §4-12）。
 * 取值判断拆到 `fallbackState.ts` 用真函数断言；组件层用一条源码契约兜住
 * 「入口被重新门进断线分支」「字号缩回小字」「组件自己拼端点」这三处会悄悄回退的地方。
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const fallback = await import("../src/pad/components/fallbackState");

const read = (relative: string): string =>
  readFileSync(fileURLToPath(new URL(`../src/pad/${relative}`, import.meta.url)), "utf8");

/** 去掉块注释与行注释：契约要查的是**代码**，不是文档里引用的既有路径。 */
const stripComments = (source: string): string =>
  source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");

const APP_SRC = read("App.tsx");
const TEXT_SRC = read("components/TextFallback.tsx");
const STATE_SRC = read("components/fallbackState.ts");
const ENTRIES_SRC = read("components/FallbackEntries.tsx");

const ALL_STATUSES = ["listening", "thinking", "speaking", "connecting", "disconnected"] as const;

test("主链路正常时「按住说话」依然可用，且提示语与断线时不同", () => {
  const live = fallback.resolveFallbackState({ status: "listening" });
  const offline = fallback.resolveFallbackState({ status: "disconnected" });

  assert.equal(live.talk.enabled, true, "连着的时候也必须能按（走 C3 整段上传）");
  assert.equal(offline.talk.enabled, true, "断线时更不能把唯一入口关掉");
  assert.equal(live.talk.hint, fallback.TALK_HINT_LIVE);
  assert.equal(offline.talk.hint, fallback.TALK_HINT_OFFLINE);
  assert.notEqual(live.talk.hint, offline.talk.hint, "两种连接态要说不同的话，否则提示等于没有");
});

test("五个连接态下「按住说话」都可用（无可点状态的组合不存在）", () => {
  for (const status of ALL_STATUSES) {
    const state = fallback.resolveFallbackState({ status });
    assert.equal(state.talk.enabled, true, `${status} 下入口被关掉了`);
    assert.ok(state.talk.hint.length > 0, `${status} 下没有「什么时候用它」的提示`);
  }
});

test("ASR 不可用时也不许把「按住说话」关掉，只做降级标记", () => {
  const state = fallback.resolveFallbackState({ status: "listening", asrAvailable: false });

  assert.equal(state.talk.enabled, true, "语音服务故障时按钮消失 = 断掉最后一层退路");
  assert.equal(state.talk.degraded, true, "要能看出此刻按下去可能拿到降级话术");
});

test("打字入口的可用性跟着「后端是否真有文本入口」走，缺入口时明说原因", () => {
  const blocked = fallback.resolveFallbackState({ status: "listening" });
  const ready = fallback.resolveFallbackState({
    status: "listening",
    textEntryAvailable: true,
  });

  assert.equal(blocked.text.enabled, false, "默认必须禁用：今天没有纯文本对话入口");
  assert.equal(blocked.text.ready, false);
  assert.equal(blocked.text.hint, fallback.TEXT_HINT_BLOCKED);
  assert.equal(ready.text.enabled, true);
  assert.equal(ready.text.ready, true);
  assert.equal(ready.text.hint, fallback.TEXT_HINT_READY);
  assert.match(fallback.TEXT_HINT_BLOCKED, /按住说话/, "被挡住时要把人指回仍然可用的那一级");
});

test("四条提示语互不相同、都不为空（不许拿同一句糊弄两个入口）", () => {
  const hints = [
    fallback.TALK_HINT_LIVE,
    fallback.TALK_HINT_OFFLINE,
    fallback.TEXT_HINT_READY,
    fallback.TEXT_HINT_BLOCKED,
  ];
  for (const hint of hints) assert.ok(hint.trim().length > 0, "提示语不能为空");
  assert.equal(new Set(hints).size, hints.length, "提示语重复 = 老人分不清两个入口");
});

test("同入参两次调用结果一致（判定是纯函数，不藏状态）", () => {
  const input = { status: "connecting" as const, textEntryAvailable: false };
  assert.deepEqual(fallback.resolveFallbackState(input), fallback.resolveFallbackState(input));
});

test("源码契约：两个入口都在 footer 常驻，不再被断线分支门住", () => {
  const start = APP_SRC.indexOf("两级降级入口**常驻**");
  assert.ok(start > 0, "App.tsx 里找不到降级入口那段注释，契约失去锚点");
  const footer = APP_SRC.slice(start, APP_SRC.indexOf("</footer>", start));

  // 2026-11-11 起 footer 允许两种写法：内联两块，或交给 `FallbackEntries` 做等宽两栏。
  const inline = footer.includes("<HoldToTalk") && footer.includes("<TextFallback");
  const extracted = footer.includes("<FallbackEntries");
  assert.ok(inline || extracted, "按住说话与打字输入都没有渲染在常驻块里");
  assert.equal(
    /status === "disconnected"\s*&&\s*\(/.test(footer),
    false,
    "入口又被门进断线分支了：这样主链路正常时老人依然看不到它",
  );
  assert.ok(
    footer.includes("{fallback.talk.hint}") || footer.includes("talkHint={fallback.talk.hint}"),
    "按住说话的提示必须来自 resolveFallbackState",
  );
  assert.ok(
    footer.includes("hint={fallback.text.hint}") || footer.includes("textHint={fallback.text.hint}"),
    "打字入口的提示必须来自 resolveFallbackState",
  );
  assert.ok(APP_SRC.includes("resolveFallbackState({ status"), "App 必须在渲染里调用纯函数取值");
});

test("源码契约：两栏与中部同轴（左=舞台、右=字幕栏），窄屏退回等宽", () => {
  assert.ok(
    ENTRIES_SRC.includes("pad-entry-grid w-full"),
    "两栏没有用与中部同轴的网格：按住说话会偏离数字人垂线、打字框也会与字幕卡错位",
  );
  assert.ok(ENTRIES_SRC.includes('data-testid="fallback-talk"'), "缺左栏锚点");
  assert.ok(ENTRIES_SRC.includes('data-testid="fallback-text"'), "缺右栏锚点");
  assert.ok(
    ENTRIES_SRC.includes('data-testid="fallback-talk-hint"') &&
      ENTRIES_SRC.includes('data-testid="fallback-text-hint"'),
    "两栏的「什么时候用它」提示必须各自渲染",
  );
  // 两栏同轴：左列撑满舞台宽度、按住按钮在列内居中；右列吃字幕栏宽度与同一个右槽
  assert.ok(ENTRIES_SRC.includes('className="pad-entry-talk flex'), "按住说话没有挂舞台列锚点类");
  assert.ok(ENTRIES_SRC.includes('className="pad-entry-text flex'), "打字栏没有挂字幕列锚点类");
  assert.ok(ENTRIES_SRC.includes('className="w-full"'), "按住说话没有撑满舞台列");
  assert.ok(ENTRIES_SRC.includes("showHint={false}"), "右栏会重复渲染同一句提示（外层已经渲染了）");

  const css = read("index.css");
  assert.match(
    css,
    /\.pad-entry-grid\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)\s*var\(--pad-subtitle-w\)/,
    "底栏没有按「舞台列 + 字幕栏宽」分栏：两栏轴线会与中部对不上",
  );
  assert.match(
    css,
    /\.pad-entry-talk\s*\{[^}]*margin-inline:\s*auto/,
    "舞台列里的按住说话没有居中：按钮会偏离数字人垂线",
  );
  assert.match(
    css,
    /\.pad-entry-talk\s*\{[^}]*max-width:\s*var\(--pad-avatar-max\)/,
    "按住说话宽度没有跟随数字人上限：宽屏下按钮会横跨整个舞台列",
  );
  assert.match(
    css,
    /\.pad-entry-text\s*\{[^}]*padding-right:\s*var\(--pad-gutter-x\)/,
    "打字栏没有让出与字幕卡相同的右槽：两者右边缘不会对齐",
  );
  assert.match(
    css,
    /@media[^{]*max-width:\s*1023px[^{]*\{[\s\S]*?\.pad-entry-grid\s*\{[^}]*minmax\(0,\s*1fr\)\s*minmax\(0,\s*1fr\)/,
    "堆叠档没有退回等宽两栏：字幕不再分左右时，底栏会只剩半个屏宽",
  );
});

test("源码契约：麦克风起不来时把人送到打字框（聚焦 + 滚进视口）", () => {
  assert.ok(ENTRIES_SRC.includes("onAskForText={focusText}"), "按住说话失败后没有指路回调");
  assert.ok(ENTRIES_SRC.includes("scrollIntoView"), "只 focus 不滚动：底栏在屏底，用户看不到光标");
  assert.ok(TEXT_SRC.includes("inputRef?: RefObject<HTMLTextAreaElement | null>"), "打字框没有暴露可聚焦的 ref");
});

test("源码契约：打字输入是长者端大字（≥20px），且发送按钮与输入框都到位", () => {
  const sizes = [...TEXT_SRC.matchAll(/text-\[(\d+)px\]/g)].map((m) => Number(m[1]));
  assert.ok(sizes.length > 0, "TextFallback 里没有任意像素字号，无法确认大字口径");
  assert.ok(Math.max(...sizes) >= 20, `最长者端口径至少要 20px，实际 ${Math.max(...sizes)}px`);

  assert.ok(TEXT_SRC.includes('data-testid="text-fallback-send"'), "缺发送按钮的可测锚点");
  assert.ok(TEXT_SRC.includes('data-testid="text-fallback-input"'), "缺输入框的可测锚点");
  assert.ok(TEXT_SRC.includes("maxLength={MAX_TEXT_LENGTH}"), "输入长度上限必须引用共享常量");
  assert.ok(TEXT_SRC.includes("aria-label={TEXT_ENTRY_LABEL}"), "输入框要有无障碍名");
});

test("源码契约：组件不许自己拼后端地址（没有文本入口就不假装有）", () => {
  const code = stripComments(TEXT_SRC);
  assert.equal(/\bfetch\s*\(/.test(code), false, "TextFallback 里出现了 fetch：提交必须由 onSubmit 注入");
  assert.equal(/\/api\//.test(code), false, "TextFallback 里出现了 /api/ 路径：不许自造端点");
  assert.ok(TEXT_SRC.includes("typeof onSubmit === \"function\""), "未注入提交函数时必须判定为不可用");
});

test("源码契约：判定函数只读入参，不改连接态与可用性口径", () => {
  assert.ok(STATE_SRC.includes("export function resolveFallbackState"), "纯函数没导出");
  assert.equal(
    /status\s*=\s*true/.test(STATE_SRC),
    false,
    "fallbackState 里出现对入参赋值：判定必须是只读的",
  );
});

test("源码契约：不许给系统输入法挡路（canvas 不许盖输入框、不许设 inputMode）", () => {
  // 手写不由本站实现：Windows 触摸键盘的手写面板、iPadOS 的随手写、Gboard 的手写，
  // 都是把笔迹识别成文字后写进**标准可编辑组件**。所以只要 `<textarea>` 还是普通的
  // 可编辑元素、且没被别的东西盖住，系统手写就自动可用——本仓库零代码。
  //
  // 两条最容易把这条路静默堵死的改法（都是「为了让手写好看一点」的合理动机）：
  //  1) 在 textarea 上面盖一层 <canvas> 当「手写区」：pointer 事件被 canvas 吃掉，
  //     系统手写落不到输入框里，长按也唤不出输入法面板；
  //  2) 给 textarea 设 `inputMode` / `inputmode`（例如 "none"）：软键盘被抑制，
  //     挂在软键盘上的手写面板随之不弹，且不会有任何报错——功能就这么没了。
  // 仓库不引 jsdom，渲染不了组件，只能用源码契约钉住这两行。
  const code = stripComments(TEXT_SRC);

  assert.equal(
    /<canvas/.test(code),
    false,
    "TextFallback 里出现了 <canvas>：盖在输入框上会让系统手写写不进来",
  );
  assert.equal(
    /input[Mm]ode\s*=/.test(code),
    false,
    "给 textarea 设了 inputMode/inputmode：软键盘与手写面板会被一起挡掉",
  );
});
