/**
 * 长者端「同一时刻只允许一个浮层」的仲裁用例（2026-11-11）
 *
 * 为什么这组用例值得存在：用户报的 bug 是「弹窗互相遮蔽而且内容显示不全」，根因不在
 * 某一个浮层，而在**六个浮层各自写死 z-index、谁也没有最终裁决权**。只要有人再往
 * App.tsx 里加一个 `{xxx && <浮层/>}`，同样的遮挡就会换个组合重现。所以这里除了测
 * 纯函数的优先级，还钉死两条结构约束：
 *   1. App.tsx 必须用 `pickVisible(` 裁决，不得再出现并列的 `{identityAsk && (` /
 *      `{activeTask !== null && (` 这类「自己决定自己显示」的写法；
 *   2. 被压制的那层必须是**不渲染 DOM**（`shownTask !== null &&`），而不是靠 z-index 盖住。
 *
 * 放 tests/ 而不是 src/：`tsconfig.json` 的 include 是 `src`，node:test 的类型不在
 * `types: ["vite/client"]` 里，放进 src 会让 `tsc --noEmit` 直接红。
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import {
  OVERLAY_PRIORITY,
  overlaySuppressed,
  pickVisible,
  type OverlayId,
} from "../src/pad/overlayPriority.ts";
import { deriveOverlays } from "../src/pad/usePadOverlays.ts";

const APP_SRC = readFileSync(new URL("../src/pad/App.tsx", import.meta.url), "utf8");
/** 批零之后浮层拼装与仲裁在 hook 里，结构契约跟着落点走（判据只换文件，不放宽）。 */
const OVERLAYS_SRC = readFileSync(
  new URL("../src/pad/usePadOverlays.ts", import.meta.url),
  "utf8",
);

/** 把源码里的注释与空白去掉——注释里写 `setSettingsOpen(false)` 不算实现。 */
function stripComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/[^\n]*/g, "$1");
}

const APP_CODE = stripComments(APP_SRC);

describe("批三 · 场景机是唯一真源", () => {
  it("场景切换走 reduceScene 派发，不是各写各的布尔", () => {
    assert.ok(
      APP_CODE.includes("useReducer(reduceScene, initialScene)"),
      "App.tsx 必须用 reduceScene 初始化为 initialScene",
    );
    for (const ev of [
      'dispatch({ type: "task.open"',
      '"task.close"',
      '"leisure.open"',
      '"interruption.begin"',
      '"interruption.end"',
      '"sos.dismiss"',
    ]) {
      assert.ok(APP_CODE.includes(ev), `缺少场景事件派发：${ev}`);
    }
  });

  it("关求助必须派 sos.dismiss（task.close 在 reducer 里对 sos 是空操作）", () => {
    const matched = APP_CODE.match(/const closeTask = \(\) => \{[\s\S]*?\n  \};/);
    assert.ok(matched, "App.tsx 里找不到 closeTask");
    assert.ok(
      matched![0].includes("sos.dismiss"),
      "closeTask 必须按 kind 分流到 sos.dismiss，否则关掉结果面板后场景停在 sos",
    );
    assert.ok(matched![0].includes('"task.close"'), "普通任务仍走 task.close");
  });
});

describe("浮层优先级表", () => {
  it("顺序被钉死：sos 最高、identity 次之、settings 最低", () => {
    assert.deepEqual(
      [...OVERLAY_PRIORITY],
      // media / memory 于 2026-11-12 插入 health 之后：长者主动打开的消遣与查看，
      // 让位给照护动作（求助、身份、用药、健康），但高于初始化类浮层。
      [
        "sos",
        // 2026-11-16 纳入 incoming（仅次于 sos，语义同原 z-30 注释「来电压过其它流程」）
        "incoming",
        "identity",
        "medication",
        "health",
        "media",
        "memory",
        "enroll",
        "wakeWord",
        "settings",
        // 2026-11-16 纳入 privacy / guide 并置最低：信息层不得盖住任何照护动作
        "privacy",
        "guide",
      ],
      "优先级顺序被改过：调整前请想清楚 FR-UI-08（求助不可撤销）与 FR-VPR-03（身份未定不写数据）",
    );
  });

  it("没有重复项，每个 id 都在这张表里", () => {
    assert.equal(new Set(OVERLAY_PRIORITY).size, OVERLAY_PRIORITY.length, "优先级表里出现了重复 id");
    for (const id of [
      "sos",
      "incoming",
      "identity",
      "medication",
      "health",
      "media",
      "memory",
      "enroll",
      "wakeWord",
      "settings",
      "privacy",
      "guide",
    ] as const) {
      assert.ok(OVERLAY_PRIORITY.includes(id), `优先级表漏了 ${id}`);
    }
  });
});

describe("pickVisible：谁该被渲染", () => {
  it("什么都没开时返回 null", () => {
    assert.equal(pickVisible([]), null);
  });

  it("只开设置弹层时返回 settings（它自己也是合法浮层）", () => {
    assert.equal(pickVisible(["settings"]), "settings");
  });

  it("求助结果压过身份确认（FR-UI-08：按铃不可撤销，必须一直可读）", () => {
    assert.equal(pickVisible(["identity", "sos"]), "sos");
  });

  it("身份确认压过用药抽屉（身份没定，用药回执记不到人头上）", () => {
    assert.equal(pickVisible(["medication", "identity"]), "identity");
  });

  it("全开时只有 sos 可见", () => {
    assert.equal(
      pickVisible(["medication", "health", "enroll", "wakeWord", "settings", "identity", "sos"]),
      "sos",
    );
  });

  it("同一 id 重复只算一个，不会因为写了两遍就提权", () => {
    assert.equal(pickVisible(["health", "health", "health", "medication"]), "medication");
  });

  it("结果与传入顺序无关（调用方不必先排序）", () => {
    const a: OverlayId[] = ["wakeWord", "enroll", "medication", "settings"];
    const b: OverlayId[] = ["settings", "medication", "enroll", "wakeWord"];
    assert.equal(pickVisible(a), "medication");
    assert.equal(pickVisible(b), "medication");
    assert.equal(pickVisible(a), pickVisible(b));
  });

  it("模拟关掉上层后下层自动回来（state 不进算、只看出参）", () => {
    // 身份确认压着用药抽屉
    assert.equal(pickVisible(["medication", "identity"]), "identity");
    // 长者点了「是我」：identity 从打开列表里消失，用药抽屉自己就回到可见
    assert.equal(pickVisible(["medication"]), "medication");
  });
});

describe("overlaySuppressed：开着但被压住", () => {
  it("没打开的 id 一律 false（不是被压制，是根本没开）", () => {
    assert.equal(overlaySuppressed([], "identity"), false);
    assert.equal(overlaySuppressed(["sos"], "medication"), false);
  });

  it("可见的那个不算被压制", () => {
    assert.equal(overlaySuppressed(["sos", "identity"], "sos"), false);
  });

  it("优先级低的那个返回 true", () => {
    assert.equal(overlaySuppressed(["sos", "identity"], "identity"), true);
    assert.equal(overlaySuppressed(["medication", "identity"], "medication"), true);
  });
});

describe("App.tsx 结构契约：先仲裁、再渲染", () => {
  it("用 pickVisible 裁决，并且先把打开列表收集成一个数组", () => {
    assert.match(
      OVERLAYS_SRC,
      /const open: OverlayId\[\] = \[\]/,
      "usePadOverlays 没有把打开列表收集成数组 —— 浮层又开始各自决定自己显示了",
    );
    assert.match(
      OVERLAYS_SRC,
      /pickVisible\(openOverlays\)/,
      "usePadOverlays 没有用 pickVisible 裁决",
    );
    assert.match(
      APP_CODE,
      /usePadOverlays\(\{/,
      "App.tsx 没走 usePadOverlays —— 显隐又被拆回组件体里各算各的",
    );
  });

  it("被压制的浮层不渲染 DOM，而不是靠 z-index 遮住", () => {
    assert.match(
      APP_CODE,
      /shownTask !== null &&/,
      "抽屉不是按 shownTask 渲染的：被身份确认压住时仍会进 DOM，等于又回到「盖住」的老路",
    );
    assert.match(
      APP_CODE,
      /showIdentity && identityAsk &&/,
      "身份确认层没有走 showIdentity 开关",
    );
    assert.match(APP_CODE, /open=\{showWake\}/, "唤醒词设置没有按 showWake 打开");
    assert.match(APP_CODE, /showEnroll &&/, "声纹录入层没有按 showEnroll 打开");
  });

  it("不得再出现「自己决定自己显示」的并列写法", () => {
    assert.equal(
      APP_CODE.includes("{identityAsk && ("),
      false,
      "App.tsx 里还有直接按 identityAsk 渲染的浮层，绕过了仲裁",
    );
    assert.equal(
      APP_CODE.includes("activeTask !== null && ("),
      false,
      "App.tsx 里还有直接按 activeTask 渲染的抽屉，绕过了仲裁",
    );
  });

  it("设置弹层受控（<details open={showSettings}），且 onToggle 先问仲裁", () => {
    assert.match(APP_CODE, /open=\{showSettings\}/, "设置弹层没有受控 open");
    assert.match(
      APP_CODE,
      /onToggle=\{\(event\) => \{/,
      "onToggle 不再是可仲裁的块状写法",
    );
    // 原生 <details> 的点击与键盘 Enter/Space 都不经 overlayPriority（复审 P1）：
    // 抽屉/身份确认开着时也能打开，面板被压在下面 → 「点设置没反应，关抽屉又冒出来」。
    assert.match(
      APP_CODE,
      /if \(next && \(activeTask !== null \|\| identityAsk !== null \|\| enrolling \|\| wakeSettingsOpen\)\)/,
      "onToggle 没有先问仲裁：有正式浮层在场时还能把设置面板打开（旁路 P1）",
    );
    assert.match(
      APP_CODE,
      /event\.currentTarget\.open = false;/,
      "仲裁拦下后没有立刻把 DOM 的 open 关回去（会留下「开着但看不见」的面板）",
    );
    assert.equal(
      APP_CODE.includes('removeAttribute("open")'),
      false,
      "还在直接改 DOM 的 open 属性（受控后会和 React 打架）",
    );
  });

  it("任何浮层出现时设置弹层自动收起", () => {
    assert.match(
      APP_CODE,
      /if \(settingsOpen && visibleOverlay !== "settings"\) setSettingsOpen\(false\);/,
      "缺少「设置弹层让位给正式浮层」的收起逻辑",
    );
  });
});

describe("2026-11-16 纳入仲裁的三层：隐私 / 引导 / 来电", () => {
  const base = {
    activeTask: null as { kind: OverlayId } | null,
    identityAsk: false,
    mediaOpen: false,
    memoryOpen: false,
    enrolling: false,
    wakeSettingsOpen: false,
    settingsOpen: false,
    incomingCall: false,
    privacyOpen: false,
    guideOpen: false,
  };

  it("**原缺陷回归**：隐私说明开着时弹出用药卡，卡必须可见、隐私层必须不进 DOM", () => {
    // 修前：隐私说明是全屏 z-40 且不参与仲裁，用药卡按仲裁渲染在 z-20 被整个盖住，
    // 卡片状态是「已打开」但长者看不见也点不到 —— 一条用药提醒静默错过。
    const view = deriveOverlays({
      ...base,
      privacyOpen: true,
      activeTask: { kind: "medication" },
    });
    assert.equal(view.visibleOverlay, "medication", "用药卡必须赢得仲裁");
    assert.equal(view.shownTask?.kind, "medication", "用药卡必须真的进 DOM");
    assert.equal(view.showPrivacy, false, "隐私层必须被压制到不进 DOM，而不是靠 z-index 盖");
  });

  it("隐私说明压过首启引导，但两者都压不过任何照护动作", () => {
    assert.equal(
      deriveOverlays({ ...base, privacyOpen: true, guideOpen: true }).visibleOverlay,
      "privacy",
    );
    for (const kind of ["medication", "health", "sos"] as const) {
      const v = deriveOverlays({ ...base, privacyOpen: true, activeTask: { kind } });
      assert.equal(v.visibleOverlay, kind);
      assert.equal(v.showPrivacy, false);
    }
  });

  it("来电仅次于 SOS：压过身份确认与用药卡，但压不过求助结果", () => {
    assert.equal(
      deriveOverlays({ ...base, incomingCall: true, identityAsk: true }).visibleOverlay,
      "incoming",
    );
    assert.equal(
      deriveOverlays({ ...base, incomingCall: true, activeTask: { kind: "medication" } })
        .visibleOverlay,
      "incoming",
    );
    assert.equal(
      deriveOverlays({ ...base, incomingCall: true, activeTask: { kind: "sos" } }).visibleOverlay,
      "sos",
      "FR-UI-08：求助结果页必须一直可读，来电也不能压过它",
    );
  });

  it("设置面板压过隐私与引导，但压不过来电（护工打进来比设置重要）", () => {
    for (const patch of [{ privacyOpen: true }, { guideOpen: true }]) {
      const v = deriveOverlays({ ...base, settingsOpen: true, ...patch });
      assert.equal(v.visibleOverlay, "settings");
    }
    assert.equal(
      deriveOverlays({ ...base, settingsOpen: true, incomingCall: true }).visibleOverlay,
      "incoming",
      "来电仅次于 SOS，必须压过设置面板",
    );
  });

  it("三层都关掉后回到 null，不残留", () => {
    const v = deriveOverlays({ ...base, privacyOpen: true, guideOpen: true, incomingCall: true });
    assert.equal(v.visibleOverlay, "incoming");
    assert.equal(deriveOverlays(base).visibleOverlay, null);
  });

  it("源码契约：三层都由仲裁结果驱动，不得回潮成自己决定自己显示", () => {
    const app = readFileSync(
      fileURLToPath(new URL("../src/pad/App.tsx", import.meta.url)),
      "utf8",
    );
    const code = app.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
    assert.ok(/\{showPrivacy && \(/.test(code), "隐私说明必须由 showPrivacy 驱动");
    assert.ok(/\{showIncoming && \(/.test(code), "来电必须由 showIncoming 驱动");
    assert.ok(/\{showGuide && !outboundCalling && \(/.test(code), "引导必须由 showGuide 驱动");
    assert.ok(!/\{privacyOpen && \(/.test(code), "隐私说明不得回潮成自决");
    assert.ok(!/\{incoming && \(/.test(code), "来电不得回潮成自决");
    // 三个开关必须真的传进仲裁
    assert.ok(/incomingCall: incoming !== null/.test(code), "缺 incomingCall 入参");
    assert.ok(/^\s*privacyOpen,\s*$/m.test(code), "缺 privacyOpen 入参");
    assert.ok(/^\s*guideOpen,\s*$/m.test(code), "缺 guideOpen 入参");
  });
});
