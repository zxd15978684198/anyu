/**
 * 「播放态卡死」与「旧 bundle」两道兜底的守卫用例（2026-11-11 产品负责人复述
 * 「数字人一直张口卡顿、保持一个姿势、胶囊一直播放·正在说」后加的）。
 *
 * 这类 bug 的特点是**只在某个标签页的某种时序下出现**，靠人工点很难稳定复现，
 * 所以把两件事钉成源码契约：
 * 1. `realtime.ts` 必须有「连续无能量 → 强制 endPlayback」的兜底（而不是只依赖静音/计划时长）；
 * 2. 入口必须跑构建指纹守卫，旧 bundle 自动重载一次（否则修完仍被复述成「没修」）。
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { guardStaleBundle, PAD_BUILD_ID } from "../src/pad/buildInfo.ts";

const read = (rel: string): string => readFileSync(new URL(`../src/pad/${rel}`, import.meta.url), "utf8");

const makeStorage = (initial: Record<string, string> = {}) => {
  const data = new Map(Object.entries(initial));
  return {
    getItem: (k: string) => data.get(k) ?? null,
    setItem: (k: string, v: string) => void data.set(k, v),
    data,
  };
};

test("卡死兜底：realtime.ts 必须有连续无能量 → endPlayback 的硬上限", () => {
  const src = read("realtime.ts");
  assert.match(src, /const PLAYBACK_STUCK_TIMEOUT = 12;/, "硬上限常量必须存在（12 秒）");
  assert.match(src, /this\.lipLastProgressAt = now;/, "读到能量时必须刷新心跳");
  assert.match(
    src,
    /now - this\.lipLastProgressAt > PLAYBACK_STUCK_TIMEOUT \* 1000/,
    "必须按「距上次心跳」判定卡死",
  );
  assert.match(src, /this\.endPlayback\(\);\s*\n\s*return;/, "卡死必须走 endPlayback（会发 listening 复位胶囊）");
});

test("卡死兜底：开窗时心跳归位，避免拿上一轮的时间戳误判", () => {
  const src = read("realtime.ts");
  assert.match(src, /this\.lipLastProgressAt = this\.lipLastAt;/, "openSpeakingWindow 里必须把心跳对齐到开窗时刻");
});

test("构建指纹守卫：本地没有记录时只写入、不重载", () => {
  const storage = makeStorage();
  let reloads = 0;
  const g = globalThis as { location?: { reload: () => void } };
  const saved = g.location;
  g.location = { reload: () => void reloads++ };
  try {
    guardStaleBundle(storage as unknown as Storage);
    assert.equal(reloads, 0);
    assert.equal(storage.data.get("sm:padBuild"), PAD_BUILD_ID);
  } finally {
    g.location = saved;
  }
});

test("构建指纹守卫：指纹落后时重载一次并写回新指纹（不会循环）", () => {
  const storage = makeStorage({ "sm:padBuild": "2026-11-11T00:00:00.000Z" });
  let reloads = 0;
  const g = globalThis as { location?: { reload: () => void } };
  const saved = g.location;
  g.location = { reload: () => void reloads++ };
  try {
    guardStaleBundle(storage as unknown as Storage);
    assert.equal(reloads, 1);
    assert.equal(storage.data.get("sm:padBuild"), PAD_BUILD_ID);
    // 重载后再跑一次（模拟新页面启动）：指纹已一致，不许再刷
    guardStaleBundle(storage as unknown as Storage);
    assert.equal(reloads, 1, "第二次启动不得再次重载");
  } finally {
    g.location = saved;
  }
});

test("构建指纹守卫：storage 不可用时静默跳过，不阻断启动", () => {
  const throwing = {
    getItem: () => {
      throw new Error("SecurityError");
    },
    setItem: () => {
      throw new Error("SecurityError");
    },
  };
  assert.doesNotThrow(() => guardStaleBundle(throwing as unknown as Storage));
});

test("入口接线：main.tsx 必须先跑守卫再挂载", () => {
  const entry = readFileSync(new URL("../src/pad/main.tsx", import.meta.url), "utf8");
  assert.match(entry, /import \{ guardStaleBundle \} from "\.\/buildInfo";/);
  assert.match(entry, /^guardStaleBundle\(\);$/m, "守卫必须在 createRoot 之前调用");
});

test("口型帧抛错不能永久掐断 rAF 链（嘴停在张开度 + 胶囊卡正在说）", () => {
  const src = read("realtime.ts");
  assert.match(src, /try \{\s*this\.lipFrame\(\);\s*\} catch \(err\) \{\s*this\.onLipFault\(err\);/, "stepLipSync 必须整帧兜错");
  assert.match(src, /private lipFrame\(\): void \{/, "帧体要单独成函数，便于兜错与复用");
  assert.match(src, /private onLipFault\(err: unknown\): void \{/, "兜错必须收口：嘴归零 + 胶囊回待机");
  assert.match(src, /this\.avatar\?\.setMouthOpen\(0\);/, "收口要显式闭嘴（不能留上一帧的开合度）");
  assert.match(src, /this\.markPlaybackIdle\(\);\s*this\.closeSpeakingWindow\(\);/, "收口要同时复位胶囊与说话窗");
});

test("唤醒/待机切换必须推进代次，否则两条建连路径互相覆盖 pc", () => {
  const src = read("realtime.ts");
  assert.match(
    src,
    /this\.mediaEnabled = awake;\s*[\s\S]{0,400}?this\.generation \+= 1;/,
    "setAwake 必须在切换后立刻 generation += 1（审计 P1）",
  );
});
