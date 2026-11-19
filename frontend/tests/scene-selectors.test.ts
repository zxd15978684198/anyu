/**
 * 场景选择器（场景化改造 · 批一）。
 *
 * 这组用例钉的是**选择器的分支**，不是状态机：`scene-machine.test.ts` 负责「现在处在哪一幕」，
 * 这里负责「这一幕该渲染哪些层、顶栏有什么、底部兜底区什么形态」。两者合成之后，
 * 批二把 `App.tsx` 的 21 个 `useState` 收敛成几个派生量时，回归面才可穷举。
 *
 * 用例尽量从状态机事件走出来（`run([...])`），而不是手搓 `SceneState` 字面量：
 * 手搓能过不代表真实路径能过——比如 `sos` 必须经过 `task.open` 才会变成 `scene === "task"`。
 * 只有「状态机到不了、但选择器仍要给出确定答案」的形态（多场景 × 两个恒 false 的键）才用字面量。
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  initialScene,
  reduceScene,
  type SceneEvent,
  type SceneState,
} from "../src/pad/scene/sceneMachine.ts";
import { footerMode, topBar, visibleLayers } from "../src/pad/scene/sceneSelectors.ts";

const run = (events: SceneEvent[], from: SceneState = initialScene): SceneState =>
  events.reduce(reduceScene, from);

/** 四个场景各一条真实到达路径——给「恒 false 的两键」做跨场景覆盖。 */
const oneOfEachScene: Array<[string, SceneState]> = [
  ["待机", initialScene],
  ["对话", run([{ type: "dialog.ask" }])],
  ["任务", run([{ type: "task.open", task: "medication" }])],
  ["消遣", run([{ type: "leisure.open", leisure: "media" }])],
];

describe("visibleLayers：场景 → 在场层", () => {
  it("待机：没有任何层在场", () => {
    assert.deepEqual(visibleLayers(initialScene), {
      task: false,
      media: false,
      memory: false,
      identity: false,
      enroll: false,
      wakeWord: false,
      settings: false,
    });
  });

  it("任务场景：task 在场，消遣层不在场", () => {
    const layers = visibleLayers(run([{ type: "task.open", task: "medication" }]));
    assert.equal(layers.task, true);
    assert.equal(layers.media, false);
    assert.equal(layers.memory, false);
  });

  it("SOS 场景：task 仍为 true（SOS 落在任务卡上，不单开一层）", () => {
    const sos = run([{ type: "task.open", task: "sos" }]);
    assert.equal(sos.task, "sos");
    const layers = visibleLayers(sos);
    assert.equal(layers.task, true, "SOS 结果页没有任务层 —— 长者看不到求助结果");
    assert.equal(layers.media, false);
  });

  it("消遣 → 戏曲：media 在场、memory 不在场", () => {
    const layers = visibleLayers(run([{ type: "leisure.open", leisure: "media" }]));
    assert.equal(layers.media, true);
    assert.equal(layers.memory, false);
    assert.equal(layers.task, false);
  });

  it("消遣 → 记忆回顾：memory 在场、media 不在场", () => {
    const layers = visibleLayers(run([{ type: "leisure.open", leisure: "memory" }]));
    assert.equal(layers.memory, true);
    assert.equal(layers.media, false);
  });

  it("打断层身份确认：identity 在场，且不改写当前场景的层", () => {
    const dialog = run([{ type: "dialog.ask" }]);
    const confirming = run([{ type: "interruption.begin", interruption: "identity" }], dialog);
    assert.equal(confirming.scene, "dialog", "打断层不该改 scene");
    const layers = visibleLayers(confirming);
    assert.equal(layers.identity, true);
    assert.equal(layers.enroll, false);
  });

  it("打断层声纹录入：enroll 在场", () => {
    const layers = visibleLayers(
      run([{ type: "interruption.begin", interruption: "enroll" }]),
    );
    assert.equal(layers.enroll, true);
    assert.equal(layers.identity, false);
  });

  it("来电/外呼：不点亮任何舞台层（打断层自己渲染）", () => {
    for (const interruption of ["incoming", "outbound"] as const) {
      const layers = visibleLayers(run([{ type: "interruption.begin", interruption }]));
      assert.deepEqual(
        layers,
        {
          task: false,
          media: false,
          memory: false,
          identity: false,
          enroll: false,
          wakeWord: false,
          settings: false,
        },
        `${interruption} 不应占用任何舞台层`,
      );
    }
  });

  it("wakeWord / settings 不进场景机：四个场景下恒为 false", () => {
    for (const [name, state] of oneOfEachScene) {
      const layers = visibleLayers(state);
      assert.equal(layers.wakeWord, false, `${name}场景点亮了 wakeWord —— 该键应由设置入口决定`);
      assert.equal(layers.settings, false, `${name}场景点亮了 settings —— 该键应由设置入口决定`);
    }
  });

  it("打断层与任务卡可同时在场：谁可见交给 overlayPriority，选择器不裁决", () => {
    const task = run([{ type: "task.open", task: "medication" }]);
    const confirming = run([{ type: "interruption.begin", interruption: "identity" }], task);
    const layers = visibleLayers(confirming);
    assert.equal(layers.task, true);
    assert.equal(layers.identity, true);
  });
});

describe("topBar：批二收敛目标为三项恒在", () => {
  it("四个场景都返回 status / sos / settings 三项", () => {
    for (const [name, state] of oneOfEachScene) {
      assert.deepEqual(
        topBar(state),
        { sos: true, settings: true, status: true },
        `${name}场景顶栏与批二目标不一致`,
      );
    }
  });
});

describe("footerMode：打断层优先于对话形态", () => {
  it("待机 / 任务 / 消遣都是 fallback", () => {
    assert.equal(footerMode(initialScene), "fallback");
    assert.equal(footerMode(run([{ type: "task.open", task: "medication" }])), "fallback");
    assert.equal(footerMode(run([{ type: "leisure.open", leisure: "media" }])), "fallback");
  });

  it("对话场景是 dialog", () => {
    assert.equal(footerMode(run([{ type: "dialog.ask" }])), "dialog");
  });

  it("任何打断层都是 hidden", () => {
    for (const interruption of ["incoming", "outbound", "identity", "enroll"] as const) {
      assert.equal(
        footerMode(run([{ type: "interruption.begin", interruption }])),
        "hidden",
        `${interruption} 期间兜底条没让位`,
      );
    }
  });

  it("对话 + 身份确认同时成立时取 hidden（优先级不能反）", () => {
    const confirming = run(
      [{ type: "interruption.begin", interruption: "identity" }],
      run([{ type: "dialog.ask" }]),
    );
    assert.equal(confirming.scene, "dialog");
    assert.equal(footerMode(confirming), "hidden");
  });
});

describe("选择器是纯函数", () => {
  it("调用不修改入参状态", () => {
    const state = run([
      { type: "leisure.open", leisure: "media" },
      { type: "interruption.begin", interruption: "identity" },
    ]);
    const snapshot = JSON.parse(JSON.stringify(state)) as SceneState;
    visibleLayers(state);
    topBar(state);
    footerMode(state);
    assert.deepEqual(state, snapshot);
  });
});
