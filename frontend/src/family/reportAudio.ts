/**
 * 日报语音条（PRD §4.8 FR-RPT-04）的渲染闸门 —— 纯函数、零 import。
 *
 * ## 为什么单独一个模块
 *
 * 「没有音频就不许出现播放器」是本卡的验收条款之一（计划件 §4 反向验收）。把判断塞在
 * 页面 JSX 里，用例只能靠渲染框架去测，而本工程没有前端测试设施；抽成一个纯函数后，
 * Node 内置 test runner 就能直接覆盖它（`reportAudio.test.mjs`），不需要引入 vitest。
 *
 * 渲染一个点了没声的播放器，比不渲染更糟：家属会以为**系统坏了**，甚至以为**有语音但
 * 自己听不见**。所以 `audio_url` 为空时整块（按钮 + `<audio>`）都不出现。
 *
 * ## 零 import 是硬约束
 *
 * 测试用 `dynamic import(new URL("./reportAudio.ts", import.meta.url))` 直接加载本文件
 * （Node ≥22 的原生类型擦除）。一旦这里 import 了别的模块，Node 侧就要解析 TS 的
 * 无扩展名说明符（`./http`），那条路径在此环境里走不通——所以闸门必须自包含。
 *
 * ## 服务端给的 `audio_url` 是**接口地址**，不是静态文件路径
 *
 * 形如 `/api/reports/{elder_id}/audio?date=YYYY-MM-DD`。音频是长者健康数据，读它要带
 * Bearer（裸 `<audio src>` 带不上认证头，只会 401），因此页面用 `authedFetch` 取 blob。
 */

/** 只依赖 `audio_url` 一个字段：调用方传整条日报或最小对象都行。 */
export interface ReportAudioCarrier {
  audio_url?: string | null;
}

/**
 * `audio_url` → 可请求的地址；没有音频时返回 `null`。
 *
 * 四种「没有」都归到 `null`，避免脏数据把播放器放出来：
 * 字段缺失、`null`、空串、全空白；非字符串（后端改了类型）同样算没有。
 */
export function audioUrlOf(item: ReportAudioCarrier | null | undefined): string | null {
  const raw = item?.audio_url;
  if (typeof raw !== "string") return null;
  const trimmed = raw.trim();
  return trimmed.length > 0 ? trimmed : null;
}
