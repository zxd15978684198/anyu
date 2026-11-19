/**
 * FR-CON-03 语音控制链路（「换一段 / 停 / 大声点 / 小声点 / 放段越剧」）的用例。
 *
 * ## 为什么这条链路必须有用例
 *
 * 它由四段接起来，任何一段断掉都**不会报错**，只会「说了没反应」：
 *
 * 1. 服务端判定意图 → `media_control` 帧（后端用例在 `tests/test_media_control_downlink.py`）
 * 2. `protocol.ts` 认得这个 type（不认就是 `parseServerSignal` 返回 null，静默丢弃）
 * 3. `realtime.ts` 把帧交回上层（漏一个 case 就永远到不了 App）
 * 4. `App.tsx` 按 `seq` 去重后分派：音量直接改、play 开面板、next/stop 交给播放器
 *
 * 第 1 段在 Python 侧已钉住；这里钉 2–4。第 2 段是**真行为测试**（调用
 * `parseServerSignal`），3–4 两段是源码契约——它们在 React 组件与 WebSocket 回调里，
 * 没有 DOM 与网络栈就调不到，而源码契约至少保证「接线还在」。
 *
 * ## 一个容易踩的次序
 *
 * `App.tsx` 的 WS 回调只在建连时注册一次（依赖 `[]`），闭包读不到最新的 `mediaOpen`。
 * 所以「有没有播放器接得住 next/stop」必须走 `mediaOpenRef`，且它跟的是
 * `visibleOverlay === "media"` 而不是 `mediaOpen`——面板被 SOS 挤掉时组件已卸载。
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { parseServerSignal } from "../src/pad/protocol";

const pad = (name: string): string => readFileSync(new URL(`../src/pad/${name}`, import.meta.url), "utf8");
const APP = pad("App.tsx");
const REALTIME = pad("realtime.ts");
const PROTOCOL = pad("protocol.ts");

const frame = (payload: Record<string, unknown>): string => JSON.stringify(payload);

test("协议：media_control 帧能被解析出来（否则整条链路静默断掉）", () => {
  const parsed = parseServerSignal(frame({ type: "media_control", action: "next", seq: 3 }));
  assert.ok(parsed !== null, "parseServerSignal 不认 media_control：帧会被当成未知类型丢弃");
  assert.equal(parsed.type, "media_control");
  if (parsed.type === "media_control") {
    assert.equal(parsed.action, "next");
    assert.equal(parsed.seq, 3);
  }
});

test("协议：五种动作与可选剧种都原样带过去", () => {
  for (const action of ["next", "stop", "volume_up", "volume_down", "play"] as const) {
    const parsed = parseServerSignal(
      frame({ type: "media_control", action, seq: 7, kind: "opera", genre: "越剧" }),
    );
    assert.ok(parsed !== null, `${action} 帧没解析出来`);
    assert.equal(parsed.type, "media_control");
    if (parsed.type === "media_control") {
      assert.equal(parsed.action, action);
      assert.equal(parsed.kind, "opera");
      assert.equal(parsed.genre, "越剧", "剧种丢了，长者说「放越剧」就会随便放一段京剧");
    }
  }
});

test("协议：未知 type 仍然被丢弃（别为了这条帧放宽成什么都收）", () => {
  assert.equal(parseServerSignal(frame({ type: "media_whatever", action: "next", seq: 1 })), null);
});

test("协议白名单里有 media_control（否则 parseServerSignal 会提前 return null）", () => {
  assert.ok(
    PROTOCOL.includes('"media_control"'),
    "protocol.ts 的 SERVER_SIGNAL_TYPES 少了 media_control",
  );
});

test("realtime：收到帧要交给上层，且丢形状不对的 action", () => {
  const block = REALTIME.slice(REALTIME.indexOf('case "media_control"'));
  assert.notEqual(block, "", "realtime.ts 没处理 media_control");
  assert.ok(block.includes("this.cb.onMediaControl?.(command)"), "没把命令交回上层，等于白收");
  // 五个合法动作都要出现在校验里；拼错的 action 必须整条丢弃
  for (const action of ["next", "stop", "volume_up", "volume_down", "play"]) {
    assert.ok(block.includes(`"${action}"`), `校验里少了 ${action}`);
  }
  assert.ok(block.includes("typeof msg.seq === \"number\""), "seq 没做数值校验，会传 NaN 给去重逻辑");
});

test("App：seq 去重，迟到/重放的旧指令不执行", () => {
  assert.ok(APP.includes("onMediaControl:"), "App.tsx 没有接 onMediaControl，语音说了没反应");
  assert.ok(
    APP.includes("if (command.seq <= mediaSeqRef.current) return;"),
    "没有 seq 闸门：连说两句「换一段」会切两段",
  );
  assert.ok(APP.includes("mediaSeqRef.current = command.seq;"), "seq 记录没更新");
});

test("App：音量走同一个 MEDIA_VOLUME_STEP，且夹在 0–1", () => {
  assert.ok(APP.includes("const MEDIA_VOLUME_STEP = 0.15;"), "缺音量步进常量");
  assert.ok(
    APP.includes("Math.min(1, Math.max(0, Math.round((current + delta) * 100) / 100))"),
    "语音调音量没有夹紧：连说五次「大声点」会算出 1.65 这种值",
  );
});

test("App：play 动作同时带 kind 与 genre 打开面板", () => {
  const play = APP.slice(APP.indexOf('if (command.action === "play")'));
  assert.ok(play.includes('setMediaKind(command.kind ?? "opera")'), "没按服务端给的 kind 开面板");
  assert.ok(play.includes('setMediaGenre(command.genre ?? "")'), "剧种丢了，「放段越剧」会变成随便放");
  // 断言随结构更新（前端计划 R-3 / W5 批二）：面板可见性已从并列布尔改为
  // 由场景机派生，所以判据从 setMediaOpen(true) 改为 leisure.open dispatch，
  // 并**额外**钉住派生链本身，不是放宽。
  assert.ok(
    play.includes('dispatch({ type: "leisure.open", leisure: "media" })'),
    "说了「放段越剧」却没打开播放面板",
  );
  assert.ok(
    APP.includes("const mediaOpen = sceneLayers.media;"),
    "面板可见性必须从场景机派生，否则又变回两份真源",
  );
  assert.ok(
    APP.includes("const sceneLayers = visibleLayers(scene);"),
    "缺 visibleLayers(scene) 派生",
  );
  assert.ok(
    !APP.includes("setMediaOpen") && !APP.includes("setMemoryOpen"),
    "并列布尔的 setter 应已删除（与 dispatch 五处成对，属同一事实两份）",
  );
});

test("App：next/stop 只在面板真的可见时转发（用 ref，不用 state 闭包）", () => {
  assert.ok(
    APP.includes("if (!mediaOpenRef.current) return previous;"),
    "next/stop 没做可见性判断：面板关着时到达的指令会在下次打开时补执行",
  );
  assert.ok(
    APP.includes('mediaOpenRef.current = visibleOverlay === "media";'),
    "ref 跟的应该是「实际可见」，mediaOpen 在面板被 SOS 挤掉时仍是 true",
  );
});

test("App：点按钮进来的入口清空剧种（不替长者押剧种）", () => {
  const entry = APP.slice(APP.indexOf("onOpenMedia={(kind) =>"));
  assert.ok(entry.includes('setMediaGenre("")'), "按钮入口没清剧种，会沿用上一次语音指定的剧种");
});
