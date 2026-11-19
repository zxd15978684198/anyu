/**
 * WebGL 能力探测（2026-11-09 修复：数字人「一片空白」的真因之一）。
 *
 * 为什么要提前探测，而不是等 three 抛错：
 * - `new THREE.WebGLRenderer()` 在**当前浏览器无法创建上下文**时只往控制台打三条
 *   `THREE.WebGLRenderer: A WebGL context could not be created.`，随后在渲染循环里
 *   静默失败；`AvatarStage` 内部的降级分支挂在加载/超时上，**等不到这个错误**，
 *   于是用户看到的是一块**空白的舞台**（不是白屏、也没有静态头像）。
 * - 常见触发场景不是"坏浏览器"：远程桌面、Windows 上关掉"使用图形加速"、
 *   显卡驱动被 Chrome 拉黑、虚拟机无 3D 加速——都是 PRD FR-3D-02 要求"降级不白屏"
 *   必须覆盖的现实情况；新版 Chrome 还默认禁掉了软件 WebGL（SwiftShader）。
 *
 * 因此：**挂载 3D 之前先问一次**，答"不行"就直接走降级形象，并把可读原因带到界面上，
 * 让用户知道该去开哪个开关，而不是对着空白乱猜。
 * 探测结果按模块缓存——上下文创建有成本，且同一页面里能力不会变。
 */

/** 探测结论；`reason` 只进日志/界面（不含 URL 等部署信息） */
export type WebGLSupport =
  | { readonly ok: true; readonly version: "webgl2" | "webgl"; readonly renderer: string }
  | { readonly ok: false; readonly reason: string };

let cached: WebGLSupport | null = null;

/**
 * 探测本页能否创建 WebGL 上下文（结果缓存）。
 *
 * 探测用的 canvas **不需要插进 DOM**；拿到上下文后立刻丢弃引用，靠浏览器回收，
 * 避免为了一次探测长期占着一个 GL 上下文（Chrome 同时对页面的活动上下文数量有限制）。
 */
export function detectWebGL(): WebGLSupport {
  if (cached !== null) return cached;
  cached = probe();
  return cached;
}

/** 仅供测试重置缓存（生产代码不要调用） */
export function resetWebGLSupportForTests(): void {
  cached = null;
}

/** 把探测原因翻成一句人话（界面上给用户看，措辞不要出现内部字段名） */
export function describeWebGLFailure(support: WebGLSupport): string {
  if (support.ok) return "";
  if (support.reason === "no_document") return "当前环境没有浏览器画布，3D 不可用";
  if (support.reason === "no_context") {
    return "浏览器没有提供 WebGL 画布：多半是「硬件加速」被关闭，或显卡被浏览器列入黑名单";
  }
  if (support.reason.startsWith("threw:")) return "浏览器拒绝创建 WebGL 画布（可能被安全策略禁用）";
  return "浏览器未能启用 3D 渲染";
}

function probe(): WebGLSupport {
  if (typeof document === "undefined") {
    return { ok: false, reason: "no_document" };
  }
  let canvas: HTMLCanvasElement | null = null;
  try {
    canvas = document.createElement("canvas");
    // 先 webgl2：three r150+ 默认走 WebGL2；只有拿到 webgl2 才算"能跑"，
    // 拿不到再退 webgl1（three 仍可用），两者都没有才判降级。
    //
    // 属性刻意给"最宽松"的一组：failIfMajorPerformanceCaveat:false 允许软件光栅化
    // （远程桌面/无独显的机器只有 SwiftShader），powerPreference:default 不去挑独显。
    // 若这里都拿不到上下文，three 也一定拿不到——不是我们参数选错。
    const attrs: WebGLContextAttributes = {
      alpha: true,
      antialias: false,
      depth: false,
      stencil: false,
      failIfMajorPerformanceCaveat: false,
      powerPreference: "default",
    };
    const gl2 = canvas.getContext("webgl2", attrs);
    if (gl2) {
      const renderer = readRenderer(gl2);
      release(gl2);
      return { ok: true, version: "webgl2", renderer };
    }
    const gl1 = canvas.getContext("webgl", attrs);
    if (gl1) {
      const renderer = readRenderer(gl1);
      release(gl1);
      return { ok: true, version: "webgl", renderer };
    }
    return { ok: false, reason: "no_context" };
  } catch (error) {
    // 某些环境（企业策略/沙箱）在 getContext 上直接抛 SecurityError
    const message = error instanceof Error ? error.message : String(error);
    return { ok: false, reason: `threw:${message.slice(0, 80)}` };
  } finally {
    // 断开引用，交给 GC；不要把 canvas 挂到 DOM
    canvas = null;
  }
}

/** 读显卡/光栅器名字（只用于诊断行，例如 "ANGLE (… SwiftShader Device …)"） */
function readRenderer(gl: WebGLRenderingContext | WebGL2RenderingContext): string {
  try {
    const info = gl.getExtension("WEBGL_debug_renderer_info");
    const raw = info
      ? gl.getParameter(info.UNMASKED_RENDERER_WEBGL)
      : gl.getParameter(gl.RENDERER);
    return typeof raw === "string" ? raw.slice(0, 96) : "unknown";
  } catch {
    return "unknown";
  }
}

/** 主动释放上下文，别等 GC（部分实现会一直占着） */
function release(gl: WebGLRenderingContext | WebGL2RenderingContext): void {
  try {
    gl.getExtension("WEBGL_lose_context")?.loseContext();
  } catch {
    // 释放失败不影响探测结论
  }
}
