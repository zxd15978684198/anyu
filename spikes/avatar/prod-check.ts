/**
 * T5 收尾（task-15）的**正式代码**验证：直接 import `frontend/src/pad/avatar/VrmAvatar.ts`
 * （不是 spike 里另写一份），在真浏览器里跑一遍状态机。
 *
 * 为什么这里不再手抄一份渲染逻辑：`setSpeaking` / `nod` 的幂等、重入、dispose 后不抛，
 * 只有跑在正式模块上才算数。spike 目录没有打包器，所以用 frontend 自带的 esbuild 打一次：
 *
 *   cd frontend
 *   node_modules/.pnpm/@esbuild+win32-x64@0.28.2/node_modules/@esbuild/win32-x64/esbuild.exe \
 *     ../spikes/avatar/prod-check.ts --bundle --format=esm --outfile=../spikes/avatar/prod-check.bundle.js
 *   cd ../spikes/avatar && node serve.mjs   # → http://127.0.0.1:5273/prod.html
 *
 * 产物 `prod-check.bundle.js` 是一次性的，不入库。
 *
 * ⚠️ 时钟必须用「主动请求帧」而不是 `setTimeout`：headless Chromium 在页面空闲时会冻结 rAF
 * （实测渲染循环 720ms 内一帧都没跑），用 setTimeout 采样会把交叉淡化读成「卡在起点值」，
 * 看起来像状态机 bug，实际只是没有帧。`await nextFrame()` 强制浏览器出帧，动画时钟才真的走。
 */
import { createAvatar, type AvatarController } from "../../frontend/src/pad/avatar/VrmAvatar";

interface Step {
  readonly name: string;
  readonly state: unknown;
  readonly error?: string;
}

const steps: Step[] = [];
const out = document.getElementById("out") as HTMLPreElement;

const nextFrame = () => new Promise<void>((resolve) => window.requestAnimationFrame(() => resolve()));

/** 推进 n 帧（60fps 下 30 帧 ≈ 500ms），返回期间是否真的出了帧 */
const advance = async (count: number): Promise<void> => {
  for (let index = 0; index < count; index += 1) await nextFrame();
};

const snapshot = (controller: AvatarController) => {
  try {
    return controller.getState();
  } catch (error) {
    return { getStateThrew: String(error) };
  }
};

const record = (name: string, controller: AvatarController | null) => {
  steps.push({ name, state: controller ? snapshot(controller) : null });
  out.textContent = JSON.stringify({ steps }, null, 2);
};

/** 高频采样：每 5 帧记一次权重，用来看淡化曲线而不是只看两个端点 */
async function sample(
  controller: AvatarController,
  label: string,
  ticks: number,
  framesPerTick = 4,
): Promise<void> {
  const frames: unknown[] = [];
  for (let index = 0; index < ticks; index += 1) {
    const state = snapshot(controller) as { weights?: unknown; baseKind?: unknown };
    frames.push({ frame: index * framesPerTick, base: state.baseKind, w: state.weights });
    await advance(framesPerTick);
  }
  steps.push({ name: label, state: frames });
  out.textContent = JSON.stringify({ steps }, null, 2);
}

async function waitUntil(predicate: () => boolean, maxFrames: number): Promise<boolean> {
  for (let index = 0; index < maxFrames; index += 1) {
    if (predicate()) return true;
    await advance(3);
  }
  return false;
}

/** 记录一次「调用不抛」：抛了就把它写进 steps，而不是让整条序列挂掉 */
function tryCall(name: string, call: () => void, controller: AvatarController): void {
  try {
    call();
    record(`${name} (无异常)`, controller);
  } catch (error) {
    steps.push({ name, state: null, error: String(error) });
    out.textContent = JSON.stringify({ steps }, null, 2);
  }
}

async function main(): Promise<void> {
  const canvas = document.getElementById("stage") as HTMLCanvasElement;
  const startedAt = performance.now();

  // `?mode=missing`：把 talking / nod 指向不存在的路径，验证「没有该动画时静默留在 idle」
  const missingMode = new URLSearchParams(window.location.search).get("mode") === "missing";
  const bogus = "/avatars/animations/__missing__.vrma";

  let firstFrameMs: number | null = null;
  const controller = await createAvatar(canvas, {
    avatarUrl: "/avatars/vrm1_sample.vrm",
    ...(missingMode ? { talkingAnimationUrl: bogus, nodAnimationUrl: bogus } : {}),
    onFirstFrame: (ms) => {
      firstFrameMs = Math.round(ms);
    },
  });

  const result: Record<string, unknown> = {
    mode: missingMode ? "missing" : "full",
    loadMs: Math.round(performance.now() - startedAt),
    sequence: steps,
  };
  (window as unknown as { __PROD__: unknown }).__PROD__ = result;

  record("load 完成（idle 尚在异步加载）", controller);
  await advance(15);
  result.firstFrameMs = firstFrameMs;

  const okIdle = await waitUntil(() => controller.getState().clips.includes("idle"), 120);
  result.idleLoaded = okIdle;
  await advance(2);
  record("idle.vrma 到位", controller);

  if (missingMode) {
    // 动画缺失路径：切换请求必须**静默降级**（不抛、不切到空气里、基线仍是 idle）
    tryCall("setSpeaking(true)（talking 404）", () => controller.setSpeaking(true), controller);
    await advance(30);
    record("talking 缺失时 +30 帧（应仍 idle、无异常）", controller);
    tryCall("nod()（nod 404）", () => controller.nod(), controller);
    await advance(30);
    record("nod 缺失时 +30 帧（应不点头、无异常）", controller);
    controller.setSpeaking(false);
    await advance(10);
    record("setSpeaking(false) 收尾", controller);
    controller.dispose();
    record("dispose 后", controller);
    result.done = true;
    (window as unknown as { __PROD__: unknown }).__PROD__ = result;
    out.textContent = JSON.stringify(result, null, 2);
    document.body.dataset.ready = "1";
    return;
  }


  // ① setSpeaking(true) → 逐帧采样，看 idle 是否真的淡到 0、talking 是否升到 1
  controller.setSpeaking(true);
  await sample(controller, "setSpeaking(true) 后逐帧曲线（应 idle→0 / talking→1）", 13);

  // ② 幂等：同值再调一次，不能重播/重置淡化
  controller.setSpeaking(true);
  await advance(5);
  record("setSpeaking(true) 重复调用（幂等）", controller);

  // ③ setSpeaking(false) → 回 idle
  controller.setSpeaking(false);
  await advance(30);
  record("setSpeaking(false) 后 30 帧（应 talking→0 / idle→1）", controller);

  // ④ nod()：一次性动作压过基线
  controller.nod();
  await advance(12);
  record("nod() 后 12 帧（应 nod→1、基线让位）", controller);

  const nodEnded = await waitUntil(() => !controller.getState().nodding, 240);
  result.nodEnded = nodEnded;
  await sample(controller, "nod 结束后逐帧曲线（应淡回基线 idle）", 10);

  // ⑤ 重入：说话中连点两次 nod，不能抛、不能卡死在 nodding
  controller.setSpeaking(true);
  controller.nod();
  await advance(8);
  controller.nod();
  await advance(12);
  record("说话中 nod() ×2（重入重播）", controller);
  await waitUntil(() => !controller.getState().nodding, 240);
  await advance(24);
  record("重入后回基线（应回到 talking）", controller);

  // ⑥ dispose 后调用：必须不抛
  controller.setSpeaking(false);
  controller.dispose();
  record("dispose 后", controller);
  tryCall("dispose 后 setSpeaking(true)", () => controller.setSpeaking(true), controller);
  tryCall("dispose 后 nod()", () => controller.nod(), controller);
  tryCall("dispose 后 setMouthOpen(0.5)", () => controller.setMouthOpen(0.5), controller);
  tryCall("重复 dispose()", () => controller.dispose(), controller);

  result.done = true;
  (window as unknown as { __PROD__: unknown }).__PROD__ = result;
  out.textContent = JSON.stringify(result, null, 2);
  document.body.dataset.ready = "1";
}

main().catch((error: unknown) => {
  const message = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
  out.textContent = JSON.stringify({ error: message, steps }, null, 2);
  document.body.dataset.ready = "error";
});
