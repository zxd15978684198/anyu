/**
 * 首启引导结束「顶掉待机屏」的回归守卫（审计 A2-4，P2）。
 *
 * 病：引导看完时 `finishGuide(true)` 把一句欢迎语写进**大字幕**，而待机屏
 * （`StandbyPanel`：四个常问句 + 「您让我记住的」入口）只在字幕为空时渲染
 * （`App.tsx` 的 `{!subtitle.text && <StandbyPanel …>}`）。于是引导一结束，
 * 整块待机屏就空了：新长者刚听完「想看看我记住的事，按这儿」，按下去什么都没有，
 * 得等那句字幕自己被下一轮覆盖才回来。首用那一次尤其伤——他还没形成任何习惯，
 * 第一次点不到就会认定「这东西没有那个功能」。
 *
 * 修法：欢迎语照旧上屏（它是有用的告别话），但只挂 `GUIDE_NOTICE_MS` 就自动收；
 * 收起时**只擦自己那一句**——这 4 秒里长者可能已经开口、或到点提醒已经上屏，
 * 无条件清空是另一种 bug（把人家正在读的字擦掉）。
 *
 * 为什么守源码而不是渲染：Node 的 `--test` 只做类型擦除、不认 TSX，仓库又不许引
 * jsdom/vitest（AGENTS §4-12）；`finishGuide` 是 `MainScreen` 内部的闭包，没有可单测的
 * 纯函数出口。这里守的是四处**改一行就会悄悄回退**的地方：
 *
 *   1. 时长仍在 3–5s（长者按每秒 6–8 字读一句 24 字的大字，太短读不完、太长入口等于没了）；
 *   2. 「跳过」直接返回、不写任何字幕（跳过的人必须立刻看到待机屏，而不是再等一句告别）；
 *   3. 收起判据是「字幕还是我写的那句」，不是无条件清空；
 *   4. 待机屏的渲染闸门仍是「字幕为空」——这条是整段推理的前提，闸门一改，
 *      1/2/3 就都不再说明问题，逼人回来看这整段注释。
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const APP_SRC = readFileSync(
  fileURLToPath(new URL("../src/pad/App.tsx", import.meta.url)),
  "utf8",
);

/** 注释里写着「此前这里是 setSubtitle(...)」这类病根说明，剥掉，只认代码。 */
const CODE = APP_SRC.replace(/\/\*[\s\S]*?\*\//g, " ").replace(/^\s*\/\/.*$/gm, " ");

/** 源码里换行/缩进不该影响契约判定。 */
const flat = (source: string): string => source.replace(/\s+/g, " ");

const FLAT = flat(CODE);

/** 唯一性断言：同一片段出现两次以上，后面的顺序判断就没有意义了。 */
const countOf = (needle: string): number => flat(CODE).split(needle).length - 1;

const firstIndex = (needle: string): number => {
  const at = FLAT.indexOf(needle);
  assert.ok(at >= 0, `源码里找不到「${needle}」——契约被改掉了`);
  return at;
};

test("欢迎语自动收起的时长仍在 3–5 秒区间，且真的交给了 setTimeout", () => {
  const declared = /const GUIDE_NOTICE_MS = ([\d_]+);/.exec(CODE);
  assert.ok(declared, "GUIDE_NOTICE_MS 常量必须还在（时长不能散落在调用点）");
  const ms = Number((declared[1] ?? "").replace(/_/g, ""));
  assert.ok(
    ms >= 3_000 && ms <= 5_000,
    `引导欢迎语自动收起应在 3–5s，实际 ${ms}ms：太短长者读不完那句大字，太长待机屏入口等于又没了`,
  );
  assert.match(
    FLAT,
    /\}, GUIDE_NOTICE_MS\);/,
    "setTimeout 必须用 GUIDE_NOTICE_MS 作为延时，不能另写一个字面量",
  );
});

test("「跳过」直接返回，不写任何字幕（跳过的人必须立刻看到待机屏）", () => {
  assert.equal(
    countOf("if (!completed) return;"),
    1,
    "「跳过」必须有一个提前返回；没有它，跳过的人也要等一句告别话才看见待机屏",
  );
  const skipAt = firstIndex("if (!completed) return;");
  const guideAt = firstIndex("const finishGuide = (completed: boolean) => {");
  const subAt = firstIndex('setSubtitle({ text, source: "notice" });');
  assert.ok(skipAt > guideAt, "提前返回必须在 finishGuide 内");
  assert.ok(
    subAt > skipAt,
    "setSubtitle 必须排在「跳过」提前返回之后——否则跳过也会写欢迎语，A2-4 原样复发",
  );
});

test("收起时只擦自己那一句，不动这期间上屏的对话或提醒", () => {
  const startAt = firstIndex("guideNoticeTimerRef.current = setTimeout(");
  const guardAt = firstIndex("prev.text === text ? { text: \"\", source: \"final\" } : prev");
  const endAt = firstIndex("}, GUIDE_NOTICE_MS);");
  assert.ok(
    startAt < guardAt && guardAt < endAt,
    "清空字幕必须写在 setTimeout 回调里，且判据是「字幕仍是欢迎语那句」",
  );
  assert.doesNotMatch(
    FLAT.slice(startAt, endAt),
    /setSubtitle\(\{ text: "", source: "final" \}\)/,
    "不许在定时器里无条件清空字幕：那会把长者正在读的回复/提醒一起擦掉",
  );
});

test("待机屏的渲染闸门仍是「字幕为空」", () => {
  const gateAt = firstIndex("!subtitle.text && (");
  const standbyAt = firstIndex("<StandbyPanel");
  assert.ok(
    gateAt < standbyAt,
    "StandbyPanel 必须仍挂在 !subtitle.text 之下：这是「字幕有字就看不见记忆入口」的全部由来",
  );
});

test("定时器有卸载清理与重看引导时的旧定时器回收", () => {
  assert.equal(
    countOf("clearTimeout(guideNoticeTimerRef.current)"),
    2,
    "两处必须都清：①重看引导时先清上一个，②卸载时兜底",
  );
  assert.match(
    FLAT,
    /useEffect\( \(\) => \(\) => \{/,
    "卸载清理要挂在 useEffect 的返回函数里，不能只在事件回调里清",
  );
});
