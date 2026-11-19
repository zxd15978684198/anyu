/**
 * 「今日药单」抽屉的结构契约（S5-批二，2026-11-13）
 *
 * 为什么读源码而不渲染：`MedicationDrawer.tsx` 依赖 IndexedDB（`../offline/db`）与
 * `window.setInterval`，node:test 里没有 DOM；而这个组件真正会退化的地方不是像素，
 * 是**约定**——「open 为 false 时必须什么都不渲染」「状态口径必须复用既有窗口函数、
 * 不许自己搓一套」「必须订阅缓存刷新事件，否则登录后当场开抽屉是空的」「不许通网」。
 * 这些都能从源码文本判定，且注释里写一遍不算（先 stripComments）。
 *
 * 与 `medication-summary.test.ts`（paddoc）的分工：那边是常驻右栏的一行摘要，
 * 这边是按需展开的全量抽屉；两者共用的窗口口径都在 `taskDispatch.medicationWindowEnd`
 * 与 `medicationView` 里，本文件只钉「有没有真的用它们」。
 *
 * 为什么放 tests/ 而不是 src/：`frontend/tsconfig.json` 的 include 只有 `["src"]`，
 * 用例进 src 会让 `tsc --noEmit` 报 TS2307 Cannot find module 'node:test'。
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";

const SRC = readFileSync(
  new URL("../src/pad/components/MedicationDrawer.tsx", import.meta.url),
  "utf8",
);

/** 去掉注释——注释里写 `return null` 不算实现，注释里写 `fetch(` 也不算通网。 */
function stripComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/[^\n]*/g, "$1");
}

const CODE = stripComments(SRC);

/** 抽出某个函数的函数体（从 `function name(` 到第一处同层 `^}`），用于局部断言。 */
function functionBody(name: string): string {
  const start = CODE.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `源码里找不到 function ${name}(`);
  const end = CODE.indexOf("\n}", start);
  assert.notEqual(end, -1, `function ${name}( 没有找到收尾的 }`);
  return CODE.slice(start, end);
}

describe("MedicationDrawer：受控与空态", () => {
  it("导出 MedicationDrawer 组件", () => {
    assert.match(CODE, /export function MedicationDrawer\(/);
  });

  it("open 为 false 时直接返回 null（关闭态不得渲染任何 DOM）", () => {
    assert.match(CODE, /if \(!open\) return null;/);
  });

  it("hooks 全部在早退之前调用（避免 hook 数量随开关变化）", () => {
    const guard = CODE.indexOf("if (!open) return null;");
    const lastHook = Math.max(
      CODE.lastIndexOf("useState<"),
      CODE.lastIndexOf("useEffect("),
      CODE.lastIndexOf("useCallback("),
    );
    assert.ok(lastHook !== -1, "组件里没有 hooks？");
    assert.ok(lastHook < guard, "存在早退之后的 hook 调用，切换 open 会崩");
  });

  it("空态文案是「今天没有要吃的药」", () => {
    assert.match(CODE, /export const EMPTY_TEXT = "今天没有要吃的药";/);
  });

  it("标题是「今天的药」，并且渲染时用 TITLE 常量", () => {
    assert.match(CODE, /export const TITLE = "今天的药";/);
    assert.match(CODE, /\{TITLE\}/);
  });
});

describe("MedicationDrawer：数据来源只有本地缓存", () => {
  it("从 ../offline/db 引入 readReminders", () => {
    assert.match(
      CODE,
      /import \{[^}]*readReminders[^}]*\} from "\.\.\/offline\/db";/,
    );
  });

  it("订阅 REMINDERS_UPDATED_EVENT 并在 cleanup 里解绑", () => {
    assert.match(
      CODE,
      /import \{[^}]*REMINDERS_UPDATED_EVENT[^}]*\} from "\.\.\/medicationView";/,
    );
    assert.match(CODE, /addEventListener\(REMINDERS_UPDATED_EVENT/);
    assert.match(CODE, /removeEventListener\(REMINDERS_UPDATED_EVENT/);
  });

  it("不通网：没有 fetch / axios / XMLHttpRequest", () => {
    assert.ok(!/\bfetch\s*\(/.test(CODE), "抽屉不该发请求：数据只来自 IndexedDB 缓存");
    assert.ok(!/axios|XMLHttpRequest/.test(CODE));
  });

  it("不 import App.tsx（组件不许反向依赖页面）", () => {
    assert.ok(!/from\s+"[^"]*App"/.test(CODE));
  });
});

describe("MedicationDrawer：状态口径复用既有函数", () => {
  it("「已吃 / 待吃 / 已过期」三态文案齐全", () => {
    assert.match(CODE, /taken: "已吃"/);
    assert.match(CODE, /pending: "待吃"/);
    assert.match(CODE, /missed: "已过期"/);
  });

  it("状态判定委托 medicationView.medicationStatus，抽屉自己不算窗口", () => {
    // task-32 之后三态只有一处实现：抽屉、摘要、状态栏都调 medicationStatus，
    // 窗口末尾（PRD §4.5）仍然只住在 taskDispatch.medicationWindowEnd 里。
    assert.match(
      CODE,
      /import \{[^}]*medicationStatus[^}]*\} from "\.\.\/medicationView";/,
      "必须从 medicationView 取状态函数",
    );
    assert.ok(!/medicationWindowEnd/.test(CODE), "抽屉不再直接碰窗口函数，避免第三份口径");
    const body = functionBody("reminderStatus");
    assert.match(body, /medicationStatus\(reminder, now, taken\)/, "只透传，不重算");
    // 不许把 PRD 的 5 分钟递进/60s 窗口本地重抄一遍：阈值只允许有一处真源。
    assert.ok(!/5 \* 60|\* 5 \* 60_000|windowSeconds \* 1000/.test(body));
  });

  it("「已吃」由 medicationStatus 判（taken 集合透传），不能拿 announcedAt 冒充", () => {
    assert.match(CODE, /takenIds\?: ReadonlySet<string>/);
    const body = functionBody("reminderStatus");
    assert.match(body, /medicationStatus\(reminder, now, taken\)/, "「本次会话已确认」要一路传下去");
    assert.ok(!/announcedAt/.test(CODE), "announcedAt 只是「播报过」，不能当「已吃」");
  });

  it("时间格式复用 medicationView.formatClock（24 小时制，非法值 --:--）", () => {
    assert.match(CODE, /import \{[^}]*formatClock[^}]*\} from "\.\.\/medicationView";/);
    assert.match(CODE, /formatClock\(reminder\.scheduledAt\)/);
  });
});

describe("MedicationDrawer：版面与 MediaPanel 同位置同层级", () => {
  it("外层是 pointer-events-none 的右侧 fixed 抽屉，z-20", () => {
    assert.match(CODE, /pointer-events-none fixed inset-y-0 right-0 z-20/);
  });

  it("内层 pointer-events-auto（只放开面板本身，不遮底栏按键）", () => {
    assert.match(CODE, /pointer-events-auto max-h-full/);
  });

  it("关闭按钮是 button 且绑定 onClose", () => {
    assert.match(CODE, /<button[\s\S]*?onClick=\{onClose\}[\s\S]*?<\/button>/);
    assert.match(CODE, /type="button"/);
  });
});
