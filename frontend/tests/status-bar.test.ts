/**
 * 长者端状态胶囊「可读性」的守卫用例（2026-11-10 产品负责人实测后加的）。
 *
 * 背景：三枚胶囊原来只显示取值（`已断开` `已确认` `待机`），产品负责人看不到卡片时说不出
 * 「哪枚管什么」，其中两枚还是同样的灰底。修法是每枚自报家门：`标签 · 取值`。
 *
 * 为什么测 `statusViews.ts` 而不是渲染 `StatusBar.tsx`：
 * Node 的 `--test` 只做类型擦除，不认 TSX；仓库又不许引 jsdom/vitest（AGENTS §4-12）。
 * 所以取值表拆到纯 `.ts` 里测真代码，组件层用一条**源码契约**兜住「标签被删回只剩取值」——
 * 那是本次修复唯一会悄悄回退的地方。
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const views = await import("../src/pad/components/statusViews");

const BAR_SRC = readFileSync(
  fileURLToPath(new URL("../src/pad/components/StatusBar.tsx", import.meta.url)),
  "utf8",
)
  // 契约查代码，不查注释：docstring 里「原来 gap-3」这类历史说明不算回退
  .replace(/\/\*[\s\S]*?\*\//g, "")
  .replace(/^\s*\/\/.*$/gm, "");

test("三枚胶囊的标签互不相同，且分别是连接 / 身份 / 播放", () => {
  const names = [
    views.CONNECTION_VIEW.listening.name,
    views.IDENTITY_VIEW.confirmed.name,
    views.PLAYBACK_VIEW.idle.name,
  ];
  assert.deepEqual(names, ["连接", "身份", "播放"]);
  assert.equal(new Set(names).size, 3, "三枚标签不许重名，否则等于没有标签");
});

test("标签在同一枚胶囊的多个取值上保持不变（标签描述“是什么”，不是“怎么样”）", () => {
  for (const [a, b] of [
    [views.CONNECTION_VIEW.listening, views.CONNECTION_VIEW.disconnected],
    [views.CONNECTION_VIEW.listening, views.CONNECTION_VIEW.mic_unavailable],
    [views.IDENTITY_VIEW.confirmed, views.IDENTITY_VIEW.unknown],
    [views.IDENTITY_VIEW.generic, views.IDENTITY_VIEW.unknown],
    [views.PLAYBACK_VIEW.idle, views.PLAYBACK_VIEW.speaking],
  ] as const) {
    assert.equal(a.name, b.name);
    assert.notEqual(a.label, b.label, "同一枚胶囊的不同取值必须能区分");
  }
});

test("身份三档各不相同：识别中 / 已确认 / 待确认", () => {
  const labels = [
    views.IDENTITY_VIEW.unknown.label,
    views.IDENTITY_VIEW.confirmed.label,
    views.IDENTITY_VIEW.generic.label,
  ];
  assert.deepEqual(labels, ["识别中", "已确认", "待确认"]);
  assert.equal(new Set(labels).size, 3);
});

test("八个取值的中文与色彩档位符合契约", () => {
  assert.deepEqual(
    [views.CONNECTION_VIEW.listening.label, views.CONNECTION_VIEW.listening.tone],
    ["正在听", "info"],
  );
  assert.deepEqual(
    [views.CONNECTION_VIEW.disconnected.label, views.CONNECTION_VIEW.disconnected.tone],
    ["已断开", "neutral"],
  );
  // A6：麦克风不可用是「要长者看一眼」，用 warning 不用 danger（红色只留给急症/红档）
  assert.deepEqual(
    [views.CONNECTION_VIEW.mic_unavailable.label, views.CONNECTION_VIEW.mic_unavailable.tone],
    ["麦克风没声音", "warning"],
  );
  assert.deepEqual(
    [views.IDENTITY_VIEW.unknown.label, views.IDENTITY_VIEW.unknown.tone],
    ["识别中", "neutral"],
  );
  assert.deepEqual(
    [views.IDENTITY_VIEW.confirmed.label, views.IDENTITY_VIEW.confirmed.tone],
    ["已确认", "success"],
  );
  assert.deepEqual(
    [views.IDENTITY_VIEW.generic.label, views.IDENTITY_VIEW.generic.tone],
    ["待确认", "warning"],
  );
  assert.deepEqual([views.PLAYBACK_VIEW.idle.label, views.PLAYBACK_VIEW.idle.tone], ["待机", "neutral"]);
  assert.deepEqual(
    [views.PLAYBACK_VIEW.speaking.label, views.PLAYBACK_VIEW.speaking.tone],
    ["正在说", "info"],
  );
});

test("屏幕文案是「标签 · 取值」，不是光秃秃的取值", () => {
  assert.equal(views.pillText(views.CONNECTION_VIEW.disconnected), "连接 · 已断开");
  assert.equal(views.pillText(views.IDENTITY_VIEW.confirmed), "身份 · 已确认");
  assert.equal(views.pillText(views.PLAYBACK_VIEW.speaking), "播放 · 正在说");
});

test("读屏名称仍以「…状态：」开头（短词是屏幕文案，不是 aria 的替代品）", () => {
  assert.match(views.CONNECTION_VIEW.listening.ariaLabel, /^连接状态：/);
  assert.match(views.IDENTITY_VIEW.generic.ariaLabel, /^身份状态：/);
  assert.match(views.PLAYBACK_VIEW.idle.ariaLabel, /^播放状态：/);
});

test("源码契约：组件必须渲染 view.name，不许回退成只显示取值", () => {
  assert.match(
    BAR_SRC,
    /\{view\.name\}/,
    "StatusBar 里找不到 {view.name}：标签被删了，三枚胶囊又会变成看不出差别",
  );
  assert.doesNotMatch(
    BAR_SRC,
    /CONNECTION_VIEW\s*:\s*Record/,
    "取值表不该回到 StatusBar.tsx 里——那会让本用例失去被测对象",
  );
});

/**
 * 2026-11-10 产品负责人实测：**刚登录设备就显示「身份·已确认」**。
 * 根因是 `identitySource === "generic" ? "generic" : "confirmed"` 把 `null`（还没有任何判定）
 * 也归进了 confirmed。这条用例守的就是「不知道 ≠ 确认了」。
 */
test("源码契约：身份判定为空时必须走 unknown，不许默认「已确认」", () => {
  const appSrc = readFileSync(fileURLToPath(new URL("../src/pad/App.tsx", import.meta.url)), "utf8");
  assert.match(
    appSrc,
    /identitySource === null\s*\?\s*"unknown"/,
    "App.tsx 没有把 identitySource === null 映射成 unknown：登录瞬间会直接显示「已确认」",
  );
  // 只匹配**旧的一行式**写法（字面量比对，不用正则的 \s*：它会连换行一起吃掉，
  // 把新的多行嵌套三元也判成命中，用例就永远红）
  assert.ok(
    !appSrc.includes('identitySource === "generic" ? "generic" : "confirmed"'),
    "旧的二选一映射还在：null 会被当成 confirmed",
  );
});

/**
 * 2026-11-11 产品负责人看截图提的第 6 条：三枚胶囊间距 12px 偏挤、字号比顶栏按钮小一档，
 * 远看像「连接 · 正在听身份 · 待确认播放 · 正在说」一句话。这里钉住新的尺寸口径：
 * 与顶栏按钮同高（56px）、字号 24px、间距 16px。
 */
/** 2026-11-11 多端适配：上面三个像素值改由 `--pad-status-*` 令牌给，D 档默认值不变。 */
const CSS_SRC = readFileSync(
  fileURLToPath(new URL("../src/pad/index.css", import.meta.url)),
  "utf8",
).replace(/\/\*[\s\S]*?\*\//g, "");

function tokenDefaultPx(name: string): number | null {
  const match = new RegExp(`--${name}:\\s*([0-9.]+)(rem|px);`).exec(CSS_SRC);
  if (!match) return null;
  const value = Number(match[1]);
  return match[2] === "rem" ? value * 16 : value;
}

test("源码契约：胶囊与顶栏按钮同高同字号，三枚之间 16px", () => {
  assert.match(BAR_SRC, /--pad-status-gap/, "间距没走 --pad-status-gap：三枚会挤成一团");
  assert.equal(tokenDefaultPx("pad-status-gap"), 16, "胶囊间距默认不是 16px：三枚又回到挤在一起");
  assert.match(BAR_SRC, /--pad-status-h/, "高度没走 --pad-status-h：胶囊与顶栏按钮不在同一基线");
  assert.equal(tokenDefaultPx("pad-status-h"), 56, "胶囊默认高度不是 56px（= min-h-14）");
  assert.match(BAR_SRC, /--pad-status-text/, "字号没走 --pad-status-text：胶囊比顶栏按钮小一档");
  assert.equal(tokenDefaultPx("pad-status-text"), 24, "胶囊默认字号不是 24px（= text-2xl）");
  assert.doesNotMatch(BAR_SRC, /gap-3/, "旧的 gap-3 还在");
});

/**
 * 2026-11-13 · 状态条措辞：产品负责人实测「刚进页面就说已断开、待确认，像在报故障」。
 *
 * 口径已按产品负责人 2026-11-13 裁决**下沉回 `statusViews.ts`**：措辞与判定都是纯 TS，
 * 所以下面直接断言真代码；`StatusBar.tsx` 只剩「曾经连上过没有」这个 ref 与一次函数调用，
 * 那一层用源码契约钉（Node 的 `--test` 不认 TSX）。
 */
test("连接：未连成过的 disconnected 说「准备中」，连过又掉才说「已断开」", () => {
  const preparing = views.connectionView("disconnected", false);
  assert.equal(preparing.label, "准备中", "刚进页面还在建连，却说「已断开」= 报故障");
  assert.equal(preparing.name, "连接", "「准备中」丢了自报家门，长者看不出这枚管什么");
  assert.match(preparing.ariaLabel, /^连接状态：准备中/);

  const dropped = views.connectionView("disconnected", true);
  assert.equal(dropped, views.CONNECTION_VIEW.disconnected, "连过又掉时改了口径，弱化了真断线");
  assert.equal(dropped.label, "已断开");

  assert.equal(views.connectionView("listening", false).label, "正在听");
  assert.equal(
    views.connectionView("listening", true).label,
    "正在听",
    "连上之后不该因为「连过」而换词",
  );
});

test("连接：麦克风不可用独立成档，与「连成过没有」无关", () => {
  const before = views.connectionView("mic_unavailable", false);
  const after = views.connectionView("mic_unavailable", true);
  assert.equal(before.label, "麦克风没声音", "麦克风坏了还说「准备中」，长者会一直等下去");
  assert.equal(after, before, "麦克风状态是确定的故障，不该被「曾经连上过」改变措辞");
  assert.equal(before.name, "连接", "第三档也属于「连接」这枚胶囊，不许新开一枚");
  assert.match(before.ariaLabel, /^连接状态：麦克风没有声音/);
  assert.equal(before.tone, "warning", "麦克风故障不是急症，禁止占用 danger 红");
});

test("源码契约：App 把麦克风不可用映射成 mic_unavailable，且断线优先级更高", () => {
  const appSrc = readFileSync(fileURLToPath(new URL("../src/pad/App.tsx", import.meta.url)), "utf8");
  assert.match(
    appSrc,
    /status === "disconnected"\s*\?\s*"disconnected"\s*:\s*micState === "unavailable"\s*\?\s*"mic_unavailable"\s*:\s*"listening"/,
    "App.tsx 没有把 micState 接进连接胶囊：拔麦后顶栏会一直显示「正在听」",
  );
});

test("身份：unknown 说「识别中」、generic 说「待确认」，都不喊系统词", () => {
  assert.equal(views.IDENTITY_VIEW.unknown.label, "识别中");
  assert.equal(views.IDENTITY_VIEW.generic.label, "待确认");
  assert.equal(views.IDENTITY_VIEW.confirmed.label, "已确认");
  assert.ok(
    !Object.values(views.IDENTITY_VIEW).some((view) => view.label === "通用模式"),
    "屏幕上不许再出现「通用模式」——那是给开发看的词",
  );
  assert.match(
    views.IDENTITY_VIEW.generic.ariaLabel,
    /^身份状态：待确认/,
    "读屏名称没跟上屏幕文案",
  );
});

test("源码契约：StatusBar 只留 ref 与 connectionView() 调用，措辞不回组件里", () => {
  assert.match(
    BAR_SRC,
    /if\s*\(connection\s*===\s*"listening"\)\s*\{\s*hasConnectedBefore\.current\s*=\s*true;?\s*\}/,
    "没有记录「曾经连接成功过」：分不出「还没连上」和「连过又掉」",
  );
  assert.match(
    BAR_SRC,
    /connectionView\(connection,\s*hasConnectedBefore\.current\)/,
    "组件没把「连成过没有」交给 connectionView：判定又散回组件里了",
  );
  assert.doesNotMatch(
    BAR_SRC,
    /label:\s*"(准备中|识别中|待确认|已断开)"/,
    "组件里又出现写死的措辞：底座与屏幕会再次分叉",
  );
});

test("无障碍不退化：胶囊的 aria-label 与 title 同源，都来自取值表", () => {
  assert.match(BAR_SRC, /aria-label=\{view\.ariaLabel\}/, "aria-label 没接 view.ariaLabel");
  assert.match(
    BAR_SRC,
    /title=\{view\.ariaLabel\}/,
    "title 缺失或另起一套文案：鼠标悬停与读屏会说出两种字",
  );
});

test("措辞不许靠定时器或新 props 实现", () => {
  assert.doesNotMatch(BAR_SRC, /setTimeout|setInterval/, "措辞变化不许引入定时器");
  assert.doesNotMatch(BAR_SRC, /useEffect/, "纯派生状态就够，不引 effect");
  const props = /export interface StatusBarProps \{[\s\S]*?\}/.exec(BAR_SRC);
  assert.ok(props, "StatusBarProps 没找到");
  assert.equal(
    (props[0].match(/;/g) ?? []).length,
    3,
    "props 数量变了：措辞必须在取值表里派生，不许加新入参",
  );
});
