/**
 * 构建指纹 + 「长驻标签页」守卫。
 *
 * 为什么需要（2026-11-11 实机）：长者端页面是一个长期开着的 SPA 标签页，重新构建前端后
 * nginx 已经能给出新 bundle，但**已经打开的标签页仍在跑旧 JS**。于是同一句反馈
 * （「播放·正在说」卡死、口型定住）在修好之后还会被复述一遍——排查方向被带偏。
 *
 * 做法：vite 每次构建把 ISO 时间戳注入 `__PAD_BUILD__`；入口加载时与 localStorage 记的
 * 上次指纹比对，不一致就强制刷新一次。新包自己会写回指纹，所以只会刷一次、不会循环。
 */
declare const __PAD_BUILD__: string;

/** 本次构建指纹（dev 下也由 vite define 注入；缺失时退化成 "dev"） */
export const PAD_BUILD_ID: string = typeof __PAD_BUILD__ === "string" ? __PAD_BUILD__ : "dev";

const BUILD_KEY = "sm:padBuild";

/**
 * 落后就重载（幂等）：只在「本地已记录过且与本次不一致」时刷新。
 * 首次访问不刷新——没有旧包，刷一次纯属多加载 10MB 模型。
 */
export function guardStaleBundle(storage: Storage | undefined = globalThis.localStorage): void {
  try {
    const seen = storage?.getItem(BUILD_KEY) ?? null;
    if (seen === null) {
      storage?.setItem(BUILD_KEY, PAD_BUILD_ID);
      return;
    }
    if (seen !== PAD_BUILD_ID) {
      storage?.setItem(BUILD_KEY, PAD_BUILD_ID);
      globalThis.location.reload();
    }
  } catch {
    // 隐私模式禁用 storage / 无 location（单测）：跳过守卫，不阻断启动
  }
}
