/**
 * 长者端「场景机接线」的结构契约（S2-批一，2026-11-13）
 *
 * 为什么这组用例值得存在：批一把「页面此刻处于哪个场景」从散落各处的 useState/DOM 判断
 * 收敛成一台显式的状态机（`src/pad/scene/sceneMachine.ts`）。状态机本身有
 * `scene-machine.test.ts` 管纯函数；但**接线**这一步没有任何东西能兜住——reducer 写了却
 * 没人 `useReducer`、事件类型造了却没人 dispatch、OnboardingGuide 的入场条件在重排 JSX
 * 时被顺手删掉一个判据，这几种退化都不会让现有用例变红，只会让长者端「答话时抽屉还开着」。
 *
 * 所以这里只钉四条**结构**判据（判据来自任务卡，不新造）：
 *   1. App.tsx 必须 `useReducer(`，且 reducer 来自 `./scene/sceneMachine`；
 *   2. App.tsx 必须对 SceneEvent 有 dispatch，`dispatch({ type:` 至少 3 处，且
 *      `task.open` / `leisure.open` / `interruption.begin` 三类都在里面
 *      （App.tsx 里 useReducer 的 dispatch 变量叫 `dispatchScene`，判据按 `dispatch*` 匹配，
 *      不锁标识符拼写——换个变量名不算违约，不 dispatch 才算）；
 *   3. OnboardingGuide 的「五重与」不能被削：`{guideOpen &&` 附近同时出现
 *      `shownTask === null`、`visibleOverlay === null`、`incoming === null`、`!outboundCalling`；
 *   4. 不得回潮成 `{identityAsk && (` / `activeTask !== null && (` 这类「自己决定自己显示」
 *      的并列写法（绕开场景机与浮层仲裁）。
 *
 * 与 popup-arbitration.test.ts 的分工：那边管**浮层谁可见**，这边管**事件怎么进来**。
 * 两组都读源码而不是渲染组件——App.tsx 依赖 WebRTC/three/浏览器 API，node:test 里起不来；
 * 结构契约只需要源码文本，用 stripComments 去掉注释，避免注释里写一遍就算实现。
 *
 * 为什么放 tests/ 而不是 src/：`frontend/tsconfig.json` 的 include 只有 `["src"]`、
 * types 只有 `["vite/client"]`，`.ts` 用例一旦进 src，`pnpm build` 前置的 `tsc --noEmit`
 * 会报 TS2307 Cannot find module 'node:test'。
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { deriveOverlays } from "../src/pad/usePadOverlays.ts";
import { OVERLAY_PRIORITY } from "../src/pad/overlayPriority.ts";
import { describe, it } from "node:test";

const APP_SRC = readFileSync(new URL("../src/pad/App.tsx", import.meta.url), "utf8");

/** 把源码里的注释去掉——注释里写 `dispatch({ type: "task.open" })` 不算接线。 */
function stripComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/[^\n]*/g, "$1");
}

const APP_CODE = stripComments(APP_SRC);

/**
 * 截出 `{guideOpen &&` 之后的窗口，用来判「判据是否还在同处」。
 * 只要求同处出现，不锁换行、缩进与判据顺序——重排 JSX 是允许的，删判据不允许。
 */
function guideWindow(source: string, size = 400): string | null {
  const start = source.indexOf("{guideOpen &&");
  if (start < 0) return null;
  return source.slice(start, start + size);
}

describe("场景机接线：reducer 与 dispatch", () => {
  it("用 useReducer 驱动场景，reducer 来自 ./scene/sceneMachine", () => {
    assert.match(
      APP_CODE,
      /useReducer\(/,
      "App.tsx 没有 useReducer —— 场景又回到 useState 里各算各的，宿敌（多个布尔互相打架）复活",
    );
    assert.match(
      APP_CODE,
      /from\s+["']\.\/scene\/sceneMachine["']/,
      "App.tsx 没有从 ./scene/sceneMachine 取东西 —— reducer 要么没接，要么绕开状态机自己判了",
    );
    assert.match(
      APP_CODE,
      /\breduceScene\b/,
      "没有引用 reduceScene：useReducer 用的不是这台状态机的 reducer",
    );
  });

  it("对 SceneEvent 有 dispatch，`dispatch({ type:` 至少 3 处", () => {
    const calls = APP_CODE.match(/dispatch\w*\(\{\s*type:/g) ?? [];
    assert.ok(
      calls.length >= 3,
      `只找到 ${calls.length} 处 dispatch({ type: … })：场景切换还散在手写的 setState 里`,
    );
  });

  it("三类关键事件都真的被 dispatch：task.open / leisure.open / interruption.begin", () => {
    // 每类单独断言，红了能直接看出漏的是哪一条，不用去数 dispatch 的总数。
    const required: readonly [string, string][] = [
      ["task.open", "任务场景（用药/健康/求助）没接到状态机"],
      ["leisure.open", "消遣场景（戏曲/电影/记忆）没接到状态机"],
      ["interruption.begin", "来电等打断没接到状态机——双人间外呼一响，场景机不知道"],
    ];
    for (const [type, why] of required) {
      const pattern = new RegExp(
        `dispatch\\w*\\(\\{\\s*type:\\s*["']${type.replace(/\./g, "\\.")}["']`,
      );
      assert.match(APP_CODE, pattern, `${why}：没有 dispatch({ type: "${type}" … })`);
    }
  });
});

describe("OnboardingGuide 的入场条件：安静时再教，不许削", () => {
  // 断言随结构更新（2026-11-16）：原来手工列四个「没有别的东西在场」判据，
  // 现已把 guide 纳入浮层仲裁并置**最低优先级**，那四条由 pickVisible 统一保证。
  // 判据改为「仲裁结果 + 外呼横幅守卫」，并**额外用纯函数逐层反证**，比原来的
  // 字符串匹配更强：任何一层在场都必须让引导不进 DOM。
  it("渲染条件是 showGuide 且外呼横幅不在场", () => {
    assert.ok(
      /\{showGuide && !outboundCalling && \(/.test(APP_CODE),
      "引导必须由仲裁结果 showGuide 驱动，并保留外呼横幅守卫（外呼是非模态横幅，不参与仲裁）",
    );
    assert.ok(
      !/\{guideOpen &&/.test(APP_CODE),
      "不得回潮成 guideOpen 自己决定自己显示（绕开仲裁）",
    );
  });

  it("guide 在优先级表最末，任何其它层在场都压过它", () => {
    assert.equal(
      OVERLAY_PRIORITY[OVERLAY_PRIORITY.length - 1],
      "guide",
      "guide 必须是最低优先级，否则它会盖住照护动作",
    );
    const base = {
      activeTask: null,
      identityAsk: false,
      mediaOpen: false,
      memoryOpen: false,
      enrolling: false,
      wakeSettingsOpen: false,
      settingsOpen: false,
      incomingCall: false,
      privacyOpen: false,
      guideOpen: true,
    };
    assert.equal(deriveOverlays(base).showGuide, true, "安静时引导应该出现");
    const rivals: readonly [string, Partial<typeof base>][] = [
      ["用药卡", { activeTask: { kind: "medication" } as never }],
      ["健康采集", { activeTask: { kind: "health" } as never }],
      ["求助结果", { activeTask: { kind: "sos" } as never }],
      ["身份确认", { identityAsk: true }],
      ["来电", { incomingCall: true }],
      ["戏曲", { mediaOpen: true }],
      ["记忆", { memoryOpen: true }],
      ["声纹录入", { enrolling: true }],
      ["唤醒词设置", { wakeSettingsOpen: true }],
      ["本机设置", { settingsOpen: true }],
      ["隐私说明", { privacyOpen: true }],
    ];
    for (const [name, patch] of rivals) {
      assert.equal(
        deriveOverlays({ ...base, ...patch }).showGuide,
        false,
        `${name}在场时引导仍会冒出来 —— 「安静时再教」退化成「随时插播」`,
      );
    }
  });
});

describe("不得回潮：浮层自己决定自己显示", () => {
  it("不得出现 {identityAsk && (", () => {
    assert.equal(
      APP_CODE.includes("{identityAsk && ("),
      false,
      "身份确认又直接按 identityAsk 渲染了：绕过拾取仲裁，会和求助/用药叠在一起",
    );
  });

  it("不得出现 activeTask !== null && (", () => {
    assert.equal(
      APP_CODE.includes("activeTask !== null && ("),
      false,
      "任务抽屉又直接按 activeTask 渲染了：绕过场景机与浮层仲裁",
    );
  });
});
