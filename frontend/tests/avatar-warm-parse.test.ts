/**
 * 登录前「解析预热」（2026-11-11 第二轮，产品负责人：「还是加载一会才能出来，不能立马出来」）。
 *
 * 为什么值得钉住：
 *  - 只预热字节不够。登录后到模型真正画出来，下载占 2.0s、GLTF 解析 + 关节清理 + 纹理解码
 *    还要 1–2s。解析**不需要渲染器**，所以必须在登录页做完，登录后 `createAvatar` 直接接管。
 *  - 缓存的是一次性对象（同一个 VRM 场景树不能被两次 `createAvatar` 共用），取走就必须删除；
 *    否则第二次挂载会拿到同一棵树，报「object already added to scene」或直接双份显存。
 *  - 预热失败要把缓存项摘掉，否则登录后拿到一个已 reject 的 promise，必降级静态头像。
 *
 * 渲染层仍不测（Node 只做类型擦除、不认 TSX）：这里测「缓存语义」的源码契约 + 纯函数。
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const read = (relative: string): string =>
  readFileSync(fileURLToPath(new URL(`../src/pad/${relative}`, import.meta.url)), "utf8");

const flat = (source: string): string => source.replace(/\s+/g, " ");

const LOAD_SRC = flat(read("avatar/loadAvatar.ts"));
const PRELOAD_SRC = flat(read("avatar/preloadAvatar.ts"));
const STAGE_SRC = flat(read("avatar/AvatarStage.tsx"));

test("loadAvatar 导出解析预热入口，并保留一次性取用", () => {
  assert.match(LOAD_SRC, /export function preloadAvatarModel\(/);
  assert.match(LOAD_SRC, /function takePreloadedAvatar\(/);
  assert.match(LOAD_SRC, /const preloaded = new Map<string, Promise<LoadedAvatar>>\(\);/);
});

test("loadAvatar 先查预热缓存，未命中才走真实加载", () => {
  assert.match(LOAD_SRC, /const warm = takePreloadedAvatar\(url\);/);
  assert.match(LOAD_SRC, /if \(warm\) return warm;/);
  assert.match(LOAD_SRC, /return loadAvatarUncached\(url, options\);/);
  // 真实加载路径必须改名存在，不能出现「缓存命中后又解析一次」
  assert.match(LOAD_SRC, /async function loadAvatarUncached\(/);
});

test("预热失败会把缓存项摘掉（否则登录后必降级）", () => {
  assert.match(LOAD_SRC, /pending\.catch\(\(\) => preloaded\.delete\(key\)\);/);
});

test("缓存键把相对/绝对 URL 归一，否则 avatar-config 的绝对地址永远不命中", () => {
  assert.match(LOAD_SRC, /function preloadKey\(url: string\): string \{/);
  assert.match(LOAD_SRC, /return new URL\(url, globalThis\.location\?\.href\)\.href;/);
  assert.match(LOAD_SRC, /const key = preloadKey\(url\);/);
  // 三处（存、取、删）都必须用归一后的 key
  assert.match(LOAD_SRC, /preloaded\.set\(key, pending\);/);
  assert.match(LOAD_SRC, /const pending = preloaded\.get\(key\);/);
  assert.match(LOAD_SRC, /preloaded\.delete\(key\);/);
});

test("preloadAvatar 在登录页就解析模型，而不只是下载字节", () => {
  assert.match(PRELOAD_SRC, /import \{ preloadAvatarModel \} from "\.\/loadAvatar";/);
  assert.match(PRELOAD_SRC, /void preloadAvatarModel\(url\)\.catch\(\(\) => undefined\);/);
  // 字节预热与代码预热仍在（两条侧路缺一条都会让首屏变慢）
  assert.match(PRELOAD_SRC, /fetch\(url, \{ cache: "force-cache" \}\)/);
  assert.match(PRELOAD_SRC, /import\("\.\/AvatarStage"\)/);
});

test("AvatarStage 把「挂载 → 首帧」耗时写到 window，便于复现观感数字", () => {
  assert.match(STAGE_SRC, /const publishTtfr = \(firstFrameMs: number\) => \{/);
  assert.match(STAGE_SRC, /__padAvatarTtfr/);
  assert.match(STAGE_SRC, /mountToFirstFrameMs: Math\.round\(performance\.now\(\) - startedAt\)/);
  assert.match(STAGE_SRC, /publishTtfr\(ms\);/);
});

test("主入口仍在 createRoot 之前 fire-and-forget 调用预热", () => {
  const main = flat(read("main.tsx"));
  assert.match(main, /warmUpAvatarModel\(\);/);
  assert.doesNotMatch(main, /await warmUpAvatarModel/);
  assert.ok(main.indexOf("warmUpAvatarModel()") < main.indexOf("createRoot("));
});
