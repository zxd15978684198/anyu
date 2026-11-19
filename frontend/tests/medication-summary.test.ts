/**
 * 药单一行摘要的源码级契约用例（S3-批二 / task-27）。
 *
 * 为什么是源码契约而不是渲染断言：Node 的 `--test` 只做类型擦除、不认 TSX，仓库又不许
 * 引 jsdom/vitest（AGENTS §4-12），`.tsx` 在这里根本进不来。所以本轮守的是**不会悄悄回退
 * 的那四处**：导出名、回调名、时间格式化调用、空态文案。它们一旦被改名或删掉，
 * `App.tsx` 那边的接线与验收文案会同时失配，而这是纯 tsx 组件最容易在重构里静默丢掉的部分。
 *
 * 同时守住两条版面红线：不新增全局 CSS（不许出现第二个样式表）、不自带数据源
 * （不许出现 fetch / IndexedDB）——摘要行一旦自己去取数据，就会出现两套药单真源。
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const SRC = readFileSync(
  fileURLToPath(
    new URL("../src/pad/components/MedicationSummary.tsx", import.meta.url),
  ),
  "utf8",
);

/** 去掉块注释与行注释：契约要查的是**代码**，不是文档里引用的既有路径。 */
const stripComments = (source: string): string =>
  source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");

const CODE = stripComments(SRC);

test("导出名是 MedicationSummary，且 props 里带 onOpen 回调", () => {
  assert.match(CODE, /export function MedicationSummary\(/, "必须是具名导出，App.tsx 才能按名接线");
  assert.match(CODE, /onOpen/, "开关必须由调用方给，组件不自己决定打开谁");
  assert.match(CODE, /onClick=\{onOpen\}/, "按钮必须真的把点击交回调用方");
});

test("时间走 toLocaleTimeString，非法日期退成 --:--", () => {
  assert.match(CODE, /new Date\(/, "时刻必须从字符串解析成 Date，不做字符串切片");
  assert.match(
    CODE,
    /toLocaleTimeString\("zh-CN",\s*\{\s*hour:\s*"2-digit",\s*minute:\s*"2-digit"/,
    "固定 zh-CN + 两段式时分，避免 8:30 AM / 8:30:00 这类会被念错的形态",
  );
  assert.match(CODE, /isNaN\(/, "非法日期要能判出来，否则 Invalid Date 会被当正文显示");
  assert.ok(CODE.includes("--:--"), "占位必须是 --:--，不能留空白或 Invalid Date");
});

test("空态与按钮文案原样保留，且摘要写明是「下一次」", () => {
  assert.ok(SRC.includes("今天没有待提醒的药"), "空态不许改写成「今天没有药」");
  assert.ok(SRC.includes("查看药单"), "按钮文案固定，验收要能肉眼搜到");
  assert.ok(SRC.includes("今日下一次："), "必须点明是下一次，而不是今天全部");
  assert.ok(SRC.includes("drugName"), "药名是这一行的主语");
  assert.ok(SRC.includes("dosage"), "剂量要和药名同屏，不能只给药名");
});

test("不新增全局样式、不自带数据源", () => {
  assert.doesNotMatch(CODE, /from\s+["'][^"']*\.css["']/, "组件不许 import 样式表");
  assert.doesNotMatch(CODE, /src\/pad\/offline/, "摘要行不直连 IndexedDB，真源由调用方给");
  assert.doesNotMatch(CODE, /\bfetch\s*\(/, "纯展示组件不发请求");
  assert.doesNotMatch(CODE, /\buseState\b|\buseEffect\b/, "受控组件不持有状态");
});

/* ---------------------------------------------------------------------------
 * task-32（2026-11-13）：摘要与抽屉共用一份「今天还剩什么」，三分支必须互斥
 * ------------------------------------------------------------------------ */

test("三分支：有下一次说下一次，没有但今天有漏确认说条数，都没有才空态", () => {
  assert.match(
    CODE,
    /MISSED_TEXT_TEMPLATE\s*=/,
    "漏确认文案要能被用例与调用方同时引用，不能只写死在 JSX 里",
  );
  assert.ok(CODE.includes("条药没确认"), "条数分支必须说清「没确认」，而不是含糊地报个数字");
  assert.match(CODE, /export function missedText\(/, "条数替换要有个纯函数，便于钉死文案");
  assert.match(CODE, /missedCount\s*>\s*0/, "没有下一次时要先问「今天有没有漏确认」，再退回空态");
  assert.match(
    CODE,
    /plan === null\s*\?/,
    "三分支的入口必须是 plan 是否为空，而不是在渲染里各处判断",
  );
});

test("剂量两种字段名都收，新口径 dose 优先", () => {
  assert.match(CODE, /plan\.dose \?\? plan\.dosage/, "dose 是 agenda 口径，dosage 是旧结构，必须兼容");
  assert.match(CODE, /missedCount\?: number/, "missedCount 必须是可选 prop，旧调用点不必改");
});

test("摘要行挂 pad-med-summary，不再用 truncate 截断", () => {
  assert.ok(CODE.includes("pad-med-summary"), "样式钩子由 task-33 在 CSS 里定义，类名要稳定");
  assert.doesNotMatch(CODE, /truncate/, "已去掉 truncate：摘要要能换行说清「今天有 N 条没确认」");
});

test("不许出现补服/漏服这类医疗建议措辞", () => {
  // 查的是代码而不是注释：文案常量里一条都不许出现，注释里解释「为什么不这么说」是允许的。
  assert.doesNotMatch(CODE, /补吃|补服|漏服/, "本系统不做服药依从性判断，也不给补服建议");
});
