/**
 * 戏曲/电影播放链路的守卫用例（2026-11-12 接入 pad 时加的）。
 *
 * 这条链路上最容易的退化**都不会崩**，只会悄悄把数据记错，所以只能靠源码契约钉住：
 *
 * 1. **卸载补收尾**：`overlayPriority` 里 `media` 排在 `sos/identity/medication/health`
 *    之下，被压制时播放器是**直接卸载**的（不是 z-index 遮盖）。卸载若只 `pause()` 不报
 *    `finish`，该条历史 `finished_at` 永远 NULL；等长者下次「换一段」，服务端
 *    `media_service.switch_next()` 的 `_mark_interrupted()` 会把这批悬挂记录一次性刷成
 *    `completion_rate = 0.0`，FR-CON-06 的偏好输入就被「其实听了一半」的记录带偏。
 * 2. **「换一段」不补 `finish`**：`switch_next` 已按打断写死 0.0，再补一次会用实测进度
 *    覆盖掉那个降权信号（把「没听完就切了」记成「听了四成」）。
 * 3. **音量必须走 property**：`volume` 是 `HTMLMediaElement` 的 property 不是 attribute，
 *    写成 JSX 的 `volume={...}` 一直是 1.0，「大声点」按了没反应。
 * 4. **语音命令靠 `seq` 去重**：父组件每次渲染都会传新的 `command` 对象字面量，
 *    没有 `seq` 就会把同一句话执行两遍。
 * 5. **TTS 与媒体互斥**：远端轨有三处会把 `muted` 放回 false（`ontrack`、每段
 *    `tts_text`、`playReplyAudio`），上层直接改 `muted` 立刻被覆盖 —— 表现是
 *    「听着戏，数字人突然插一句」。
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const pad = (name: string): string => readFileSync(new URL(`../src/pad/${name}`, import.meta.url), "utf8");
const PLAYER = pad("components/MediaPlayer.tsx");
const PANEL = pad("components/MediaPanel.tsx");
const APP = pad("App.tsx");
const REALTIME = pad("realtime.ts");
const PRIORITY = pad("overlayPriority.ts");

test("源码契约：卸载时按当场进度补报一次 finish（否则悬挂记录会污染偏好）", () => {
  const cleanup = PLAYER.slice(PLAYER.indexOf("return () => {"), PLAYER.indexOf("}, []);"));
  assert.notEqual(cleanup, "", "找不到卸载 cleanup");
  assert.ok(cleanup.includes("readProgress(el)"), "卸载时必须先读一次当场进度再停");
  assert.ok(
    cleanup.includes("finishMedia(activeHistoryRef.current"),
    "卸载时没补 finishMedia：被 SOS/吃药回执挤掉的那条历史会永远 finished_at = NULL",
  );
  assert.ok(
    cleanup.includes("!reportedRef.current && !closedRef.current"),
    "补报缺幂等闸门：播完的、服务端已收尾的都会被重复报一次",
  );
  assert.ok(
    cleanup.includes("progress.progress_ms"),
    "缺「进度为 0 不报」的闸门：元素还没开播就卸载会记一条 0 秒噪声",
  );
  assert.ok(
    PLAYER.includes("activeHistoryRef"),
    "缺 activeHistoryRef：卸载 cleanup 是挂载那次渲染的闭包，直接读 historyId 在换过段之后是旧值",
  );
});

test("源码契约：「换一段」成功后不再补 finish（会覆盖服务端写的降权信号）", () => {
  const next = PLAYER.slice(PLAYER.indexOf("async function handleNext"), PLAYER.indexOf("// 父组件下发的命令"));
  assert.ok(next.includes("closedRef.current = true;"), "换段成功后没把 closedRef 置真");
  const afterSwitch = next.slice(next.indexOf("closedRef.current = true;"));
  assert.ok(
    !afterSwitch.includes("reportFinish("),
    "换段之后又调了 reportFinish：会把 switch_next 写入的 completion_rate = 0.0 覆盖成实测进度",
  );
});

test("源码契约：音量走 ref 落到元素上，不用 JSX 的 volume 属性", () => {
  assert.ok(
    /el\.volume = Math\.min\(1, Math\.max\(0, volume\)\)/.test(PLAYER),
    "没把音量落到 el.volume：`volume` 是 property 不是 attribute，JSX 写法一直是 1.0",
  );
  // 只看 `<audio ... />` 这个开标签本身：文件里别处的注释会提到 `volume={...}` 这种写法，
  // 用整份源码搜会被自己的注释绊倒（第一版就踩了）。
  // 文件头 docstring 里也写着 `<audio>`，所以取**最后一处**（JSX 在文件末尾）。
  const audioTagStart = PLAYER.lastIndexOf("<audio");
  const audioTag = PLAYER.slice(audioTagStart, PLAYER.indexOf("/>", audioTagStart));
  assert.notEqual(audioTag, "", "找不到 <audio> 开标签");
  assert.ok(
    !/\bvolume=/.test(audioTag),
    "用了 JSX 的 volume 属性：React 不会把它当受控属性，写了也不生效",
  );
  assert.ok(PLAYER.includes("}, [volume, playing.historyId]);"), "音量 effect 依赖缺 historyId：换段后音量丢失");
});

test("源码契约：语音命令靠递增 seq 去重，且「停」必须走 handleStop", () => {
  assert.ok(PLAYER.includes("export interface MediaCommand"), "缺 MediaCommand 定义");
  assert.ok(
    PLAYER.includes("if (command.seq <= lastSeqRef.current) return;"),
    "没按 seq 去重：父组件每次渲染都给新对象，同一句话会被执行两遍",
  );
  const cmd = PLAYER.slice(PLAYER.indexOf("if (command.type === \"next\")"));
  assert.ok(
    cmd.slice(0, 400).includes("handleStop();"),
    "「停」没走 handleStop：直接卸载只停声音，不记账",
  );
  assert.ok(PANEL.includes("command={command}"), "MediaPanel 没把 command 透传给播放器");
  assert.ok(APP.includes("command={mediaCommand}"), "App 没把 command 交给 MediaPanel");
});

test("源码契约：音量由 App 持有并透传，默认 0.9 不是满量", () => {
  assert.ok(APP.includes("useState(0.9)"), "App 的音量默认值不是 0.9：床头平板满量在耳边太吵");
  assert.ok(APP.includes("volume={mediaVolume}"), "App 没把音量交给 MediaPanel");
  assert.ok(PANEL.includes("volume={volume}"), "MediaPanel 没把音量交给播放器");
  assert.ok(PLAYER.includes("onVolumeChange"), "播放器缺「大声点/小声点」按钮的出口");
});

test("源码契约：媒体在播时压住数字人，且压住在 realtime 内部生效", () => {
  assert.ok(
    REALTIME.includes("setSpeechSuppressed(on: boolean): void"),
    "realtime 缺 setSpeechSuppressed：上层改 muted 会被 ontrack / tts_text / playReplyAudio 三处覆盖",
  );
  for (const site of ["this.cb.audioEl.muted = this.speechSuppressed;"]) {
    assert.ok(REALTIME.split(site).length - 1 >= 3, `只有 ${REALTIME.split(site).length - 1} 处改成受控静音，应为 3 处`);
  }
  assert.ok(
    APP.includes("clientRef.current?.setSpeechSuppressed(showMedia)"),
    "App 没按 showMedia 压住数字人：判 mediaOpen 的话，被 SOS 挤掉后数字人仍是哑的",
  );
});

test("源码契约：media / memory 两档浮层的优先级在 health 之后、enroll 之前", () => {
  const order = [...PRIORITY.matchAll(/"(sos|identity|medication|health|media|memory|enroll|wakeWord|settings)"/g)]
    .map((m) => m[1])
    .filter((v, i, arr) => arr.indexOf(v) === i);
  const at = (id: string): number => order.indexOf(id);
  assert.ok(at("media") >= 0 && at("memory") >= 0, "overlayPriority 缺 media / memory 两档");
  assert.ok(at("health") < at("media"), "media 排在了 health 之前：吃药回执要让位");
  assert.ok(at("media") < at("enroll"), "media 排在了 enroll 之后：初始化流程不该压过长者想听的戏");
  assert.ok(at("memory") < at("enroll"), "memory 排在了 enroll 之后");
});
