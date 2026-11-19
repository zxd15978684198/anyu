/**
 * 数字人热加载（2026-11-11 产品负责人要求「一打开页面就加载好数字人」）的前端守卫。
 *
 * 为什么这些断言值得写：
 *  - 预热会**在登录页就下载 10.7MB 的 `.vrm`**：省流量（`saveData`）下必须忍住不预取，
 *    否则流量用户平白多付一次；同一页面也只允许预热一次，重复调用不能叠加请求。
 *  - 「预热」是靠**两条与身份无关的侧路**实现的：`fetch(..., {cache:"force-cache"})` 暖磁盘缓存、
 *    动态 `import("./AvatarStage")` 提前把 three-vrm 分包求值。这两条一旦被改成普通 `fetch`
 *    或静态 import，预热就失效（改普通 fetch 会重新走网络，静态 import 会让分包进首屏包）。
 *  - 一旦有人在 `main.tsx` 里把它写成 `await`，首屏就会等 10.7MB + three-vrm 解析 ——
 *    这正是这次要修的病，所以用「位置 + 不 await」两条契约钉住。
 *
 * 渲染层不测（Node 只做类型擦除、不认 TSX，仓库不许引 jsdom/vitest）：
 * 纯函数真跑，接线用源码契约兜住。
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const preload = await import("../src/pad/avatar/preloadAvatar");

const read = (relative: string): string =>
  readFileSync(fileURLToPath(new URL(`../src/pad/${relative}`, import.meta.url)), "utf8");

/** 源码里换行/缩进不该影响契约判定：压成单行再找子串。 */
const flat = (source: string): string => source.replace(/\s+/g, " ");

/**
 * 只留代码，去掉注释（块注释整段删掉，行注释按行删）。
 *
 * 为什么需要：这条契约的注释里必须写明「原来这里有一条 `fetch(url, { cache: "force-cache" })`」，
 * 否则后人不知道为什么不能这么写；但注释里出现这个字面量就会让 `includes(...) === false` 永远红。
 * 所以断言只看代码——这也更准确：要禁的是代码，不是文字。
 * 注意块注释的续行不以 `*` 开头（本项目写法），不能靠逐行前缀判断。
 */
const codeOnly = (source: string): string =>
  source.replace(/\/\*[\s\S]*?\*\//g, " ").replace(/^\s*\/\/.*$/gm, " ");

const PRELOAD_SRC = read("avatar/preloadAvatar.ts");
const MAIN_SRC = read("main.tsx");

test("预热条件：不省流量且没预热过才做；省流量/已预热都不做", () => {
  assert.equal(preload.shouldWarmUpAvatar(false, false), true, "正常网络 + 首次 → 应该预热");
  assert.equal(
    preload.shouldWarmUpAvatar(true, false),
    false,
    "saveData（省流量）→ 不许预热：10.7MB 对流量用户是实打实的代价",
  );
  assert.equal(
    preload.shouldWarmUpAvatar(false, true),
    false,
    "同一页面只预热一次：重复调用不许叠加第二份请求",
  );
  assert.equal(preload.shouldWarmUpAvatar(true, true), false);
});

test("缺省形象地址与 nginx 静态路径一致", () => {
  assert.equal(
    preload.DEFAULT_AVATAR_PRELOAD_URL,
    "/avatars/vrm1_sample.vrm",
    "预热地址必须就是登录后 GLTFLoader 要取的那个 URL，否则缓存命中不了",
  );
});

test("源码契约：预热靠解析预热自带下载，不许再单独 fetch 一遍字节", () => {
  /* 2026-11-12 C 案实测的教训：原来这里既 `fetch(url, { cache: "force-cache" })` 又调
     `preloadAvatarModel(url)`（后者内部 GLTFLoader 自带下载），同一个 7.27MB 的 .vrm 被
     并发下载两次 —— Resource Timing 两条记录 `start 2580 dur 508` 与 `start 2585 dur 911`，
     transferSize 都是 7274894。两条互相抢带宽，把 parsed 从 ~600ms 抬到 1s 以上。
     所以这条断言从「必须有 fetch」反转为「必须没有 fetch」。 */
  assert.equal(
    flat(codeOnly(PRELOAD_SRC)).includes('fetch(url, { cache: "force-cache" })'),
    false,
    "预热不许再单独 fetch 一遍字节：preloadAvatarModel 里的 GLTFLoader 已经在下同一个文件，重复下载会抢带宽",
  );
  assert.ok(
    flat(codeOnly(PRELOAD_SRC)).includes("void preloadAvatarModel(url).catch(() => undefined);"),
    "解析预热必须保留：它才是真正把 VRM 变成场景树的那一步",
  );
});

test("源码契约：动态 import AvatarStage，把 three-vrm 分包提前求值", () => {
  assert.ok(
    flat(PRELOAD_SRC).includes('import("./AvatarStage")'),
    '必须出现动态 import("./AvatarStage")：静态 import 会把 three-vrm 打进首屏包，预热的收益就没了',
  );
});

test("源码契约：main.tsx 在 createRoot 之前调 warmUpAvatarModel()，且不是 await", () => {
  const warm = MAIN_SRC.indexOf("warmUpAvatarModel()");
  const root = MAIN_SRC.indexOf("createRoot(");
  assert.ok(warm >= 0, "main.tsx 必须调用 warmUpAvatarModel()");
  assert.ok(root >= 0, "main.tsx 必须仍然调用 createRoot(...)");
  assert.ok(warm < root, "预热必须在 createRoot 之前发起，否则首屏已经先被挂载逻辑占住了");
  assert.equal(
    MAIN_SRC.includes("await warmUpAvatarModel"),
    false,
    "预热必须 fire-and-forget：await 它等于让首屏等 10.7MB + three-vrm 解析",
  );
});

test("源码契约：main.tsx 保留 guardStaleBundle()，且在 createRoot 之前", () => {
  const guard = MAIN_SRC.indexOf("guardStaleBundle();");
  const root = MAIN_SRC.indexOf("createRoot(");
  assert.ok(guard >= 0, "旧 bundle 守卫不能被删：否则长者会看到「修过却仍然复现」的界面");
  assert.ok(root >= 0, "main.tsx 必须仍然调用 createRoot(...)");
  assert.ok(guard < root, "守卫必须先于挂载：先判断是不是旧 bundle，再渲染");
});
