/**
 * A6 / R-语音-01 / R-语音-02 守卫：麦克风不可用时界面必须改口、必须留恢复入口。
 *
 * 为什么用源码契约：`RealtimeClient` 一实例化就要碰 `location` / `WebSocket` / `AudioContext`，
 * 在 node --test 里跑不起来；取值表与措辞已经在 `status-bar.test.ts` 用真代码断言，
 * 这里只钉「事件从哪来、按钮接到哪去」这条布线——那是本修复唯一会悄悄断掉的地方
 * （回调不接 → 顶栏永远「正在听」；按钮不接 → 提示条挂在那里没法恢复）。
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

function stripComments(src: string): string {
  return src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
}

function read(rel: string): string {
  return stripComments(
    readFileSync(fileURLToPath(new URL(`../src/pad/${rel}`, import.meta.url)), "utf8"),
  );
}

const REALTIME = read("realtime.ts");
const APP = read("App.tsx");

test("采集侧：onMicState 回调存在，且首发失败与拔麦重开失败都会上报 unavailable", () => {
  assert.match(
    REALTIME,
    /onMicState\?\s*:\s*\(state:\s*MicState\)\s*=>\s*void/,
    "RealtimeCallbacks 没有 onMicState：App 永远收不到麦克风故障",
  );
  const failures = REALTIME.match(/setMicState\("unavailable"\)/g) ?? [];
  assert.ok(
    failures.length >= 2,
    `unavailable 上报点只有 ${failures.length} 处：首发开麦失败与拔麦重开失败必须都覆盖`,
  );
});

test("采集侧：开麦成功、重开成功、退回待机都会复位成 ok", () => {
  const resets = REALTIME.match(/setMicState\("ok"\)/g) ?? [];
  assert.ok(
    resets.length >= 3,
    `ok 复位点只有 ${resets.length} 处：成功/重试成功/待机三条路径都应收掉告警`,
  );
  // 待机分支必须显式复位：否则长者睡下还看见「麦克风没声音」
  assert.match(
    REALTIME,
    /if \(!awake\) \{[\s\S]*?this\.setMicState\("ok"\)/,
    "setAwake(false) 没有复位麦克风状态：待机会被当成故障",
  );
});

test("重试：retryMic 重建媒体会话，且不复位信令状态机", () => {
  assert.match(REALTIME, /async retryMic\(\): Promise<void>/, "没有 retryMic：提示条上的按钮无处可接");
  const body = /async retryMic\(\)[\s\S]*?\n  \}/.exec(REALTIME)?.[0] ?? "";
  assert.match(body, /this\.reopenMic\(\)/, "已有会话时必须走拔麦重连的既有路径");
  assert.match(body, /setupSession\(gen\)/, "没有会话时必须能补建一次媒体会话，否则首发失败后永远修不好");
  assert.doesNotMatch(
    body,
    /this\.retry\(\)|socket\?\.retry/,
    "retryMic 不许复位信令：「重新连接」与「重试麦克风」是两个恢复动作，互相越界会打乱两个状态机",
  );
});

test("长者端：App 订阅 onMicState，并给提示条接了 retryMic 按钮", () => {
  assert.match(APP, /onMicState:\s*setMicState/, "App 没订阅 onMicState：故障到不了界面");
  assert.match(
    APP,
    /micState === "unavailable" &&[\s\S]*?clientRef\.current\?\.retryMic\(\)/,
    "提示条没有「重试」按钮或按钮没接 retryMic：长者只能刷新页面",
  );
  assert.match(
    APP,
    /麦克风没有声音了，可以先用下面的打字框/,
    "提示条没给出替代通路：长者的打字框就在下面，得告诉他",
  );
  assert.match(
    APP,
    /reason === "mic_unavailable"\) setMicState\("unavailable"\)/,
    "唤醒阶段因麦克风降级时没有立刻进入不可用态：要等一次建连失败才改口",
  );
});
