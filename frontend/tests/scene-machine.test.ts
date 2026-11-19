/**
 * 场景状态机（场景化改造 · 批一）。
 *
 * 这组用例钉的是**七条不变量**，不是实现细节：谁压谁、谁能撤销、退出回哪一幕。
 * `popup-arbitration.test.ts` 钉「同一时刻只有一个浮层可见」，这里钉「现在处在哪一幕」。
 * 两者都不许为了过 CI 而放宽。
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";

import {
  initialScene,
  reduceScene,
  type SceneEvent,
  type SceneState,
} from "../src/pad/scene/sceneMachine.ts";

const run = (events: SceneEvent[], from: SceneState = initialScene): SceneState =>
  events.reduce(reduceScene, from);

describe("场景状态机：七条不变量", () => {
  it("初值是待机，且不预先假设任何任务/消遣", () => {
    assert.equal(initialScene.scene, "standby");
    assert.equal(initialScene.task, null);
    assert.equal(initialScene.leisure, null);
    assert.equal(initialScene.interruption, null);
    assert.equal(initialScene.previousScene, null);
  });

  it("不变量 1：sos 不能被 task.close 关掉，只能 sos.dismiss", () => {
    const sos = run([{ type: "task.open", task: "sos" }]);
    assert.equal(sos.task, "sos");

    const closed = run([{ type: "task.close" }], sos);
    assert.equal(closed.scene, "task", "task.close 把 SOS 关掉了 —— FR-UI-08 要求红色事件不可撤销");
    assert.equal(closed.task, "sos");

    const dismissed = run([{ type: "sos.dismiss" }], sos);
    assert.equal(dismissed.scene, "standby");
    assert.equal(dismissed.task, null);
  });

  it("不变量 2：自动任务可压 leisure，绝不可压 sos", () => {
    const leisure = run([{ type: "leisure.open", leisure: "media" }]);
    const overLeisure = run([{ type: "task.open", task: "medication" }], leisure);
    assert.equal(overLeisure.scene, "task", "任务应该能压住消遣（长者要吃药，戏可以暂停）");

    const sos = run([{ type: "task.open", task: "sos" }]);
    const overSos = run([{ type: "task.open", task: "medication" }], sos);
    assert.equal(overSos.task, "sos", "用药卡压住了 SOS —— 任何自动任务都不许盖住求助结果");
    assert.equal(overSos.scene, "task");
  });

  it("不变量 3：打断层期间 scene 不变，退出回进入前的场景（深度 1）", () => {
    const dialog = run([{ type: "dialog.ask" }]);
    const ringing = run([{ type: "interruption.begin", interruption: "incoming" }], dialog);
    assert.equal(ringing.interruption, "incoming");
    assert.equal(ringing.scene, "dialog", "打断层不该改变 scene");

    const ended = run([{ type: "interruption.end" }], ringing);
    assert.equal(ended.interruption, null);
    assert.equal(ended.scene, "dialog", "挂断后没有回到通话前那一幕");
    assert.equal(ended.previousScene, null);
  });

  it("不变量 3 补充：打断层不叠（深度 1）", () => {
    const once = run([{ type: "interruption.begin", interruption: "incoming" }]);
    const twice = run([{ type: "interruption.begin", interruption: "outbound" }], once);
    assert.equal(twice.interruption, "incoming", "第二个打断层把第一个顶掉了 —— 会丢来电/外呼状态");
  });

  it("不变量 4：leisure 只能显式打开，任何自动事件都进不去", () => {
    // 自动事件全集（除 leisure.open）都不该把场景带进 leisure
    const autoEvents: SceneEvent[] = [
      { type: "dialog.ask" },
      { type: "dialog.end" },
      { type: "task.open", task: "health" },
      { type: "task.close" },
      { type: "interruption.begin", interruption: "identity" },
      { type: "interruption.end" },
      { type: "standby.enter" },
      { type: "sos.dismiss" },
    ];
    for (const event of autoEvents) {
      const next = reduceScene(initialScene, event);
      assert.notEqual(next.scene, "leisure", `自动事件 ${event.type} 把场景推进了消遣`);
    }
  });

  it("不变量 5：dialog 不因打开 leisure 而结束（关掉戏能回到对话）", () => {
    const dialog = run([{ type: "dialog.ask" }]);
    const opera = run([{ type: "leisure.open", leisure: "media" }], dialog);
    assert.equal(opera.scene, "leisure");

    const backToDialog = run([{ type: "leisure.close" }], opera);
    assert.equal(backToDialog.scene, "dialog", "关掉播放器后回不到对话 —— previousScene 被消遣覆盖了");
  });

  it("不变量 6：activeTask 只读 —— 场景状态里没有任务内容，只有场景", () => {
    const state = run([{ type: "task.open", task: "medication" }]);
    const keys = Object.keys(state).sort();
    assert.deepEqual(
      keys,
      ["interruption", "leisure", "previousScene", "scene", "task"],
      "场景状态混进了任务内容/开关：activeTask 必须仍是唯一真源，reducer 只读不写",
    );
  });

  it("不变量 7：outbound 没有拒绝入口（只有 interruption.end 能结束）", () => {
    const source = readFileSync(
      new URL("../src/pad/scene/sceneMachine.ts", import.meta.url),
      "utf8",
    );
    assert.match(source, /"outbound"/, "打断层里没有 outbound");
    assert.equal(source.includes("outbound.decline"), false);
    assert.equal(source.includes("outbound.reject"), false);
    assert.equal(source.includes("interruption.reject"), false);

    const outbound = run([{ type: "interruption.begin", interruption: "outbound" }]);
    const ended = run([{ type: "interruption.end" }], outbound);
    assert.equal(ended.interruption, null);
    assert.equal(ended.scene, "standby");
  });

  it("有任务卡时不回待机：待机不该把长者的等待页撤掉", () => {
    const task = run([{ type: "task.open", task: "health" }]);
    const stillTask = run([{ type: "standby.enter" }], task);
    assert.equal(stillTask.scene, "task");
  });

  it("关闭任务后回到进入前的场景，不统一回待机", () => {
    const dialog = run([{ type: "dialog.ask" }]);
    const med = run([{ type: "task.open", task: "medication" }], dialog);
    const closed = run([{ type: "task.close" }], med);
    assert.equal(closed.scene, "dialog", "关掉药单应该回对话，而不是丢上下文回待机");
  });
});
