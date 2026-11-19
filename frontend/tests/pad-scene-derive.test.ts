/**
 * pad 场景派生契约（前端计划 W5 批二，2026-11-15）。
 *
 * 批二把消遣面板（戏曲/影视、记忆）的可见性从两个并列布尔改为
 * `visibleLayers(scene)` 派生。本文件钉住**组合后的语义**，
 * 特别是那个真实的行为差异：SOS 期间开播放器会被状态机拒绝。
 *
 * 收敛前：`setMediaOpen(true)` 无条件置真，SOS 撤掉后播放器会自己冒出来；
 * 收敛后：`leisure.open` 在 `task === "sos"` 时返回原状态（不变量 2/4），
 * 面板根本不会打开。UI 从此服从已被 scene-machine 测试钉住的不变量。
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";

import { initialScene, reduceScene, type SceneState } from "../src/pad/scene/sceneMachine.ts";
import { visibleLayers } from "../src/pad/scene/sceneSelectors.ts";

const APP = readFileSync(new URL("../src/pad/App.tsx", import.meta.url), "utf8");

function run(events: Parameters<typeof reduceScene>[1][]): SceneState {
  return events.reduce((state, event) => reduceScene(state, event), initialScene);
}

describe("W5 批二 · 消遣可见性由场景机派生", () => {
  it("显式打开戏曲 → media 层在场、memory 不在", () => {
    const layers = visibleLayers(run([{ type: "leisure.open", leisure: "media" }]));
    assert.equal(layers.media, true);
    assert.equal(layers.memory, false);
  });

  it("显式打开记忆 → memory 在场、media 不在", () => {
    const layers = visibleLayers(run([{ type: "leisure.open", leisure: "memory" }]));
    assert.equal(layers.memory, true);
    assert.equal(layers.media, false);
  });

  it("关闭消遣 → 两层都不在场", () => {
    const layers = visibleLayers(
      run([{ type: "leisure.open", leisure: "media" }, { type: "leisure.close" }]),
    );
    assert.equal(layers.media, false);
    assert.equal(layers.memory, false);
  });

  it("**SOS 期间开播放器被拒**（不变量 2/4）——这是收敛前后的真实行为差异", () => {
    const state = run([
      { type: "task.open", task: "sos" },
      { type: "leisure.open", leisure: "media" },
    ]);
    assert.equal(state.task, "sos", "SOS 不能被 leisure 顶掉");
    const layers = visibleLayers(state);
    assert.equal(layers.media, false, "SOS 期间播放器不该在场");
    assert.equal(layers.task, true);
  });

  it("SOS 撤掉后播放器不会自己冒出来（旧布尔行为会）", () => {
    const state = run([
      { type: "task.open", task: "sos" },
      { type: "leisure.open", leisure: "media" }, // 被拒
      { type: "sos.dismiss" },
    ]);
    assert.equal(visibleLayers(state).media, false);
  });

  it("自动任务可以压掉消遣（不变量 2），但不压 SOS", () => {
    const pressed = run([
      { type: "leisure.open", leisure: "media" },
      { type: "task.open", task: "medication" },
    ]);
    assert.equal(visibleLayers(pressed).media, false);
    assert.equal(visibleLayers(pressed).task, true);

    const sosGuard = run([
      { type: "task.open", task: "sos" },
      { type: "task.open", task: "medication" },
    ]);
    assert.equal(sosGuard.task, "sos");
  });

  it("从对话进消遣再关掉，能回到对话（不变量 5）", () => {
    const state = run([
      { type: "dialog.ask" },
      { type: "leisure.open", leisure: "media" },
      { type: "leisure.close" },
    ]);
    assert.equal(state.scene, "dialog");
    assert.equal(visibleLayers(state).media, false);
  });

  it("打断层期间消遣层仍「在场」但不抢焦点（由仲裁层裁决，选择器不排序）", () => {
    const state = run([
      { type: "leisure.open", leisure: "media" },
      { type: "interruption.begin", interruption: "identity" },
    ]);
    const layers = visibleLayers(state);
    assert.equal(layers.media, true, "在场");
    assert.equal(layers.identity, true, "打断层也在场，谁可见由 overlayPriority 决定");
  });
});

describe("W5 批二 · App 侧接线契约", () => {
  it("读 scene state 并派生 layers，不再只解构 dispatch", () => {
    assert.equal(/const \[scene, dispatch\] = useReducer\(reduceScene, initialScene\)/.test(APP), true);
    assert.equal(APP.includes("const sceneLayers = visibleLayers(scene);"), true);
    assert.equal(APP.includes("const mediaOpen = sceneLayers.media;"), true);
    assert.equal(APP.includes("const memoryOpen = sceneLayers.memory;"), true);
  });

  it("并列布尔的 useState 与 setter 全部删除", () => {
    assert.equal(/const \[mediaOpen, setMediaOpen\]/.test(APP), false);
    assert.equal(/const \[memoryOpen, setMemoryOpen\]/.test(APP), false);
    assert.equal(APP.includes("setMediaOpen"), false);
    assert.equal(APP.includes("setMemoryOpen"), false);
  });

  it("五处 leisure dispatch 仍在（打开/关闭的唯一入口）", () => {
    const opens = (APP.match(/dispatch\(\{ type: "leisure\.open"/g) ?? []).length;
    const closes = (APP.match(/dispatch\(\{ type: "leisure\.close" \}\)/g) ?? []).length;
    assert.equal(opens, 3, "media 两处（服务端点播 + 待机入口）+ memory 一处");
  // 3 处 = 两个面板各一个 onClose + Escape 关闭当前可见消遣浮层（2026-11-16 补）。
  assert.equal(closes, 3, "两个面板 onClose + Escape 各一处 leisure.close");
  assert.ok(
    APP.includes('case "memory":') &&
      /case "memory":[\s\S]{0,80}dispatch\(\{ type: "leisure\.close" \}\)/.test(APP),
    "Escape 必须经场景机 leisure.close 关闭，不得另立第二套关闭路径",
  );
  });

  it("仲裁层仍拿 mediaOpen/memoryOpen 当输入（派生值也是布尔，接口不变）", () => {
    assert.equal(/mediaOpen,\s*\n\s*memoryOpen,/.test(APP), true);
  });

  it("activeTask 仍是任务内容真源，本批不接管（选择器边界）", () => {
    assert.equal(APP.includes("const [activeTask, setActiveTask]"), true);
    assert.equal(/const sceneLayers = visibleLayers\(scene\);[\s\S]{0,200}activeTask/.test(APP), false);
  });

  it("同步核心未被本批触碰（禁触清单）", () => {
    // 批二只改消遣可见性派生，不得动 lipFrame / 锚点 / playbackElapsedMs
    assert.equal(/private lipFrame/.test(readFileSync(new URL("../src/pad/realtime.ts", import.meta.url), "utf8")), true);
  });
});
describe("Escape 关闭浮层的安全边界（2026-11-16）", () => {
  const esc = APP.slice(APP.indexOf("const onKey = (event: KeyboardEvent)"));
  const block = esc.slice(0, esc.indexOf("window.addEventListener"));

  it("只监听 Escape，且只关仲裁选出的那一个", () => {
    assert.ok(block.includes('if (event.key !== "Escape") return;'), "必须只认 Escape");
    assert.ok(block.includes("switch (visibleOverlay)"), "必须按仲裁结果分派，不是遍历所有开关");
  });

  it("信息类与自助类可关：privacy / guide / settings / wakeWord / media / memory", () => {
    for (const [layer, action] of [
      ["privacy", "setPrivacyOpen(false)"],
      ["guide", "finishGuide(false)"],
      ["settings", "setSettingsOpen(false)"],
      ["wakeWord", "setWakeSettingsOpen(false)"],
    ] as const) {
      const at = block.indexOf(`case "${layer}":`);
      assert.ok(at >= 0, `缺 ${layer} 分支`);
      assert.ok(block.slice(at, at + 160).includes(action), `${layer} 的关闭动作不对`);
    }
    assert.ok(/case "media":\s*case "memory":/.test(block), "media 与 memory 共用 leisure.close");
  });

  it("**照护动作与求助绝不可被 Escape 关掉**（FR-UI-08 / FR-VPR-03）", () => {
    for (const layer of ["sos", "identity", "medication", "health", "incoming", "enroll"]) {
      assert.ok(
        !block.includes(`case "${layer}":`),
        `${layer} 出现在 Escape 分支里 —— 误触按键会替长者抹掉提醒/跳过身份确认/撤销求助`,
      );
    }
    assert.ok(block.includes("default:"), "必须有 default 兜住不可关的层");
    assert.ok(/default:[\s\S]{0,200}return;/.test(block), "default 必须直接 return，不关任何东西");
  });

  it("监听有清理，卸载后不再响应", () => {
    const tail = APP.slice(APP.indexOf("const onKey = (event: KeyboardEvent)"));
    assert.ok(tail.includes('window.removeEventListener("keydown", onKey)'), "必须摘监听");
  });
});
