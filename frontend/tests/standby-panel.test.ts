/**
 * 待机面板「看看今天的药」主按钮的源码契约用例（S6-批二 / task-30）。
 *
 * 为什么守源码而不是渲染：Node 的 `--test` 只做类型擦除、不认 TSX，仓库又不许引
 * jsdom/vitest（AGENTS §4-12）。这里要守的是**三处会悄悄回退的地方**：
 *
 * 1. 可选性 —— 一旦 `onOpenMedications?` 从可选变成必填，所有既有调用点（含测试桩、
 *    其他页面的 StandbyPanel）都会编译失败；反过来若丢了条件渲染，未接线时按钮会
 *    出现在待机屏上却按不动。
 * 2. 接线 —— 按钮的 `onClick` 必须直接是 `onOpenMedications`，不能被改包成
 *    `() => undefined` 这类「看起来接上了」的写法。
 * 3. 位置 —— 主按钮要在常问句 **之上**：它是「看清单」，不是第五个一问一答；
 *    排到四格下面会与「听一段戏」那类次级入口混成同一层。
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const SRC = readFileSync(
  fileURLToPath(new URL("../src/pad/components/StandbyPanel.tsx", import.meta.url)),
  "utf8",
);

/** 去掉注释：契约要查的是**代码**，不是文档里写的 prop 名。 */
const stripComments = (source: string): string =>
  source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");

const CODE = stripComments(SRC);

test("onOpenMedications 是可选 prop，且沿用既有 props 不破坏调用方", () => {
  assert.match(CODE, /onOpenMedications\?:/, "必须是可选（未接线时待机屏要与今天一致）");
  assert.match(CODE, /onAsk:\s*\(text:\s*string\)\s*=>\s*void/, "既有必填 prop 不许改成可选");
  assert.match(CODE, /onOpenMedia\?:/, "戏曲入口仍是可选");
  assert.match(CODE, /onOpenMemory\?:/, "记忆入口仍是可选");
});

test("按钮只在传入回调时渲染，点击直接交回调用方", () => {
  assert.match(CODE, /\{onOpenMedications\s*&&\s*\(/, "必须有条件渲染（&& 或三目）");
  assert.match(CODE, /onClick=\{onOpenMedications\}/, "不许包一层再调，否则等于没接上");
  assert.ok(SRC.includes("看看今天的药"), "文案固定，验收要能肉眼搜到");
  assert.match(CODE, /data-testid="open-medications"/, "留一个稳定的测试定位点");
});

test("主按钮排在常问句之上，且用品牌色而非次级描边", () => {
  const buttonAt = CODE.indexOf("open-medications");
  const gridAt = CODE.indexOf("grid shrink-0 grid-cols-2");
  assert.ok(buttonAt > 0 && gridAt > 0, "两块都应存在");
  assert.ok(buttonAt < gridAt, "主按钮必须在四格常问句之前");
  assert.match(CODE, /bg-brand\b/, "主按钮用品牌色实心，与次级入口的 bg-surface 描边分层");
  assert.match(CODE, /min-h-\[64px\]/, "长者端主按钮不小于 64px 高");
});

test("不新增样式表、不自己发请求", () => {
  assert.doesNotMatch(CODE, /from\s+["'][^"']*\.css["']/, "组件不许 import 样式表");
  assert.doesNotMatch(CODE, /\bfetch\s*\(/, "面板只回调，不发请求");
});
