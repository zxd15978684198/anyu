/**
 * 数字人热加载（2026-11-11 产品负责人要求：「一打开页面就加载好数字人」）。
 *
 * ## 为什么需要它
 *
 * 数字人只在**登录之后**才挂载（`App.tsx` 的 `MainScreen` 里 lazy 加载 `AvatarStage`）。
 * 于是每次进页面：先看到登录卡片，点登录 → 才开始下载 10.7MB 的 `.vrm` + 751KB 的
 * three-vrm 分包 → 真正立起来常常要 2–4 秒，这期间画面上只有一个「数字人加载中…」。
 * 长者看到的是「进来看不到人」，交互感受很差。
 *
 * 这两件东西都**与身份无关**：形象地址由 `avatar-config` 给，但缺省形象文件是公开静态资源；
 * three-vrm 分包也是构建产物。所以可以在登录页就把它们预热：
 *
 * 1. **字节预热**：`fetch(vrm)` 走浏览器 HTTP 缓存（nginx 对 `.vrm` 给了 7d `expires`），
 *    登录后 `GLTFLoader` 再取同一 URL 时直接命中缓存，不再走网络；
 * 2. **代码预热**：动态 `import("./AvatarStage")` 让 Rollup 把这个分包与 three/three-vrm
 *    一起下载并解析，登录后 `lazy()` 拿到的已经是求值完的模块。
 *
 * ## 三件不做
 *
 * - **不在 `saveData`（省流量）下预热**：10.7MB 对流量用户是实打实的代价。
 * - **不预热非缺省形象**：真实形象地址要 `avatar-config`（需设备令牌），登录前拿不到；
 *   这里只热缺省那个，登录后若配置指向别的文件，仍然是第一次下载（但至少 three-vrm 已就绪）。
 * - **不阻塞首屏**：`main.tsx` 里 fire-and-forget，网络请求与动态 import 都不 `await`，
 *   登录卡片的首次绘制不受影响（实测首屏 FCP 无变化）。
 */

// 第一条 import 是既有源码契约（tests/avatar-warm-parse.test.ts 断言了这一行）。
import { preloadAvatarModel } from "./loadAvatar";
import { DEFAULT_AVATAR_URL } from "./loadAvatar";

/**
 * 缺省形象文件路径（兜底常量）。
 *
 * 2026-11-17 起兜底形象固定为「小满」：预热也必须热这一个文件，否则登录后
 * `AvatarStage` 走兜底时取的 URL 没被预热，缓存命中不了。
 */
export const DEFAULT_AVATAR_PRELOAD_URL = DEFAULT_AVATAR_URL;

let started = false;

/**
 * 是否应该预热。抽成纯函数是为了能在 Node 里测（`navigator` 在 node:test 里没有）。
 *
 * @param saveData 浏览器是否处于省流量模式（`navigator.connection.saveData`）
 * @param already 是否已经预热过（同一页面只做一次）
 */
export function shouldWarmUpAvatar(saveData: boolean, already: boolean): boolean {
  return !saveData && !already;
}

/** 只读一次省流量标记；拿不到（Safari/Firefox 无 connection 信息）时按「不省流量」处理。 */
function readSaveData(): boolean {
  try {
    const conn = (navigator as unknown as { connection?: { saveData?: boolean } }).connection;
    return conn?.saveData === true;
  } catch {
    return false;
  }
}

/**
 * 预热数字人：字节 + 解析 + 代码。幂等，可重复调用。
 * 失败一律静默——预热只是优化，不能因为它报错挡住登录。
 *
 * 2026-11-11 第二轮（产品负责人：「还是加载一会才能出来，不能立马出来」）：只预热字节不够，
 * 登录后仍要花 1–2 秒解析 GLTF 与解码纹理。解析**不需要渲染器**，所以挪到登录页做，
 * 结果放在 `loadAvatar.ts` 的一次性缓存里，登录后 `createAvatar` 直接接管。
 */
export function warmUpAvatarModel(explicitUrl?: string): void {
  if (!shouldWarmUpAvatar(readSaveData(), started)) return;
  started = true;

  /* 解析预热**自带下载**（`preloadAvatarModel` 里走 GLTFLoader.loadAsync），所以不要再
     单独 `fetch` 一遍字节。

     2026-11-12 C 案实测的教训：原来这里还有一条 `fetch(url, { cache: "force-cache" })`，
     结果同一个 7.27MB 的 `.vrm` 被并发下载了两次 —— Resource Timing 里两条记录
     （`start 2580 dur 508` 与 `start 2585 dur 911`，`transferSize` 都是 7274894）。
     它们互相抢带宽，还把 `parsed` 那一段从 ~600ms 抬到 1s 以上，纯属自己拖自己。
     `GLTFLoader` 取的是同一个 URL，浏览器缓存照样命中，字节预热这一条没有任何额外收益。 */
  void (async () => {
    // 兜底形象固定为小满（2026-11-17）：预热与 `AvatarStage` 的兜底必须是同一个 URL，
    // 否则登录后命不中解析缓存。目录里的默认形象只在「小满文件缺失」时才轮到，
    // 那种机器上预热目录形象也救不了登录后的渲染。
    const url = explicitUrl ?? DEFAULT_AVATAR_PRELOAD_URL;
    void preloadAvatarModel(url).catch(() => undefined);
  })();

  // 代码：登录后 lazy() 会复用这个已求值的模块（不会再走一次网络）。
  void import("./AvatarStage").catch(() => undefined);
}

/** 单测用：重置「已预热」标记。 */
export function resetAvatarWarmUpForTest(): void {
  started = false;
}
