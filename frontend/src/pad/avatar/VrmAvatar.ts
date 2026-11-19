/**
 * 3D 数字人（VRM）最小舞台 —— 从 `本人既有工程 vrm-playground 的 src/vrmWorld.ts` 移植「直接可用」的那几段。
 *
 * 移植边界（只搬这几件，不搬编辑器/多角色/音频律动）：
 *   1. 相机 `bust` 半身取景：**按头骨高度自适应**，playground :1148-1155 的常量作为兜底
 *   2. 光照三件套（HemisphereLight + 主光 + 补光，:676-688）
 *   3. 渲染循环顺序（:727-742）：clock.getDelta() → mixer.update() → vrm.update(delta) → render
 *      —— 顺序不可调换：先由动画写骨骼，再由 vrm.update 收口（含 springbone / lookAt / 表情），
 *      最后才渲染，否则这一帧画的是上一帧的姿态，眨眼会慢一帧。
 *   4. 自动眨眼（:1424-1427）：phase 在 [0,1) 循环，phase > 0.92 时走一段正弦
 *
 * 动画三件套（task-12 交付的 Apache-2.0 `.vrma`，异步补齐，不参与首帧竞速）：
 *   `idle.vrma`（基线循环）→ `setSpeaking(true)` 切 `idle-talking.vrma` 循环 → `nod()` 叠一次 `nod.vrma`。
 *   任何一条加载失败都只是少一层表现，不会把角色拖进降级路径。
 */
import * as THREE from "three";
import type { VRM } from "@pixiv/three-vrm";
import { createVRMAnimationClip } from "@pixiv/three-vrm-animation";
import {
  DEFAULT_IDLE_ANIMATION_URL,
  DEFAULT_NOD_ANIMATION_URL,
  DEFAULT_TALKING_ANIMATION_URL,
  disposeAvatar,
  loadAvatar,
  loadVrmAnimation,
  type AvatarScene,
} from "./loadAvatar";

/** 眨眼节奏：0.52 相位/秒 → 约 1.9s 一次；0.92~1.0 共 8% 相位 ≈ 150ms 一眨 */
const BLINK_HZ = 0.52;
const BLINK_START = 0.92;
const BLINK_SPAN = 1 - BLINK_START;

/**
 * 口型双表情权重（engine.js:1150-1151 原样照抄；§5.2 v1.3 把单 aa 扩成 aa+ih 双表情）：
 * 每帧 `setValue("aa", level×0.85)` + `setValue("ih", level×0.12)`，权重只写这里。
 * 缺 ih 的模型自动降级 aa-only（写到 render 循环里就不再发 ih，见 hasIh 探测）。
 */
const MOUTH_AA_WEIGHT = 0.85;
const MOUTH_IH_WEIGHT = 0.12;

/**
 * 口型 expression 的候选名。
 *
 * VRM 1.0 规定 `aa` / `ih`；VRM 0.x 规范写大写 `A` / `I`，但实际素材常写成小写
 * `a` / `i`（本仓 Shenhe、Lisa、Ningguang、Sparkle、Jingyuan 五个都是），日系 MMD
 * 提取模型更直接写成 `29.あ` / `31.い`（流萤）。原实现只探测 `aa`，这些模型
 * `getExpression("aa")` 恒为 null，于是整段口型代码不执行 —— 只剩 VRM 1.0 的小满能张嘴。
 */
const MOUTH_AA_ALIASES: readonly string[] = ["aa", "A", "a", "あ", "ア"];
const MOUTH_IH_ALIASES: readonly string[] = ["ih", "I", "i", "い", "イ"];

/** 直接驱动网格 morph 的通道（`expressionManager` 认不出名字时的兜底） */
interface MorphChannel {
  mesh: THREE.Mesh;
  index: number;
}

/**
 * 在模型树上找一条可写的 morph 通道。
 *
 * 匹配顺序：**精确名**优先，其次 `.<候选名>` 结尾 —— 后者专门覆盖 `29.あ` 这种
 * 「序号.假名」的命名。必须带那个点：流萤、花火都有 `7.笑い`（笑）这种以 `い` 结尾的
 * 非口型 morph，只按结尾匹配会把「笑」当成 `ih` 写进去。
 */
function findMorphChannel(root: THREE.Object3D, aliases: readonly string[]): MorphChannel | null {
  let exact: MorphChannel | null = null;
  let suffix: MorphChannel | null = null;
  root.traverse((obj) => {
    if (exact) return;
    const mesh = obj as THREE.Mesh;
    const dict = mesh.morphTargetDictionary;
    const influences = mesh.morphTargetInfluences;
    if (!dict || !influences) return;
    for (const key of Object.keys(dict)) {
      const index = dict[key];
      if (typeof index !== "number" || index >= influences.length) continue;
      if (aliases.includes(key)) {
        exact = { mesh, index };
        return;
      }
      if (!suffix && aliases.some((alias) => key.endsWith(`.${alias}`))) {
        suffix = { mesh, index };
      }
    }
  });
  return exact ?? suffix;
}

/** 待机微摆振幅（弧度，≈1.1°）：没有 .vrma 时也让画面别像一张贴图 */
const SWAY_AMPLITUDE = 0.02;

/** 半身取景的 fov 与兜底机位（playground :1148-1155；自适应失败时原样使用） */
const CAMERA_FOV = 30;
const CAMERA_POSITION = new THREE.Vector3(0, 1.55, 2.2);
const CAMERA_TARGET = new THREE.Vector3(0, 1.45, 0);

/**
 * 自适应取景的推导基准。
 *
 * 上面那组兜底常量恰好是「头骨离地 1.55m」这一特例：机位与头骨同高、看向低 10cm、退 2.2m。
 * 于是把三者都写成 headY 的比例式，headY=1.55 时原样退回常量（改前/改后可直接对照），
 * 其它身材等比缩放 —— 矮个（本调试样本 headY=1.386）不再显得又小又偏下。
 */
const REFERENCE_HEAD_Y = 1.55;
const CAMERA_DISTANCE = 2.2;
const HEAD_TO_TARGET = 0.1;
/** 头高比例限幅：极端身材（儿童/巨人体/带缩放的模型）别把镜头怼到脸上或退到墙外 */
const MIN_FRAME_SCALE = 0.7;
const MAX_FRAME_SCALE = 1.4;

/** 待机与说话之间的交叉淡入淡出；点头的淡入淡出略快，读起来才有「回应」的节奏 */
const BASE_FADE_SECONDS = 0.35;
const NOD_FADE_SECONDS = 0.18;

/**
 * 静息姿势：资产没有 `.vrma` 待机动画时，不摆姿势就是 T-pose ——
 * spike 实测（spikes/avatar/RESULT.md）双臂横伸、半身取景里手会被裁到框外，
 * 看着像坏了。这里把上臂收回成自然的 A-pose。
 */
const RELAX_ARM_DROP = (70 * Math.PI) / 180;

/**
 * 上臂（VRM 归一化骨骼，左臂沿 +X）绕 Z 轴回收：左 −70°、右 +70°，正好落到身体两侧。
 * 只写归一化骨骼，浏览器侧由 `vrm.update()` 的 `humanoid.update()` 拷到原始骨骼，
 * 故这里必须在渲染循环启动前调用（早于第一次 `vrm.update`）。
 *
 * 这是 `.vrma` 到位前的姿势，也是动画加载失败时的兜底；anim 播放后骨骼由 clip 接管。
 */
function relaxArms(vrm: VRM): void {
  const left = vrm.humanoid?.getNormalizedBoneNode("leftUpperArm");
  const right = vrm.humanoid?.getNormalizedBoneNode("rightUpperArm");
  if (left) left.rotation.z = -RELAX_ARM_DROP;
  if (right) right.rotation.z = RELAX_ARM_DROP;
}

/** 取景结果（导出给 spike 与测试对照「改前/改后」数字） */
export interface BustFraming {
  source: "auto" | "fixed";
  /** 头骨世界高度（米）；`fixed` 时为 null */
  headY: number | null;
  scale: number;
  position: [number, number, number];
  target: [number, number, number];
}

/**
 * 由头骨高度推导半身机位。`headY` 缺失或不合理（非有限值、0.5m 以下、2.6m 以上）→ 兜底常量。
 * 纯函数，便于单测与 spike 对照。
 */
export function computeBustFraming(headY: number | null): BustFraming {
  if (headY == null || !Number.isFinite(headY) || headY < 0.5 || headY > 2.6) {
    return {
      source: "fixed",
      headY: null,
      scale: 1,
      position: [CAMERA_POSITION.x, CAMERA_POSITION.y, CAMERA_POSITION.z],
      target: [CAMERA_TARGET.x, CAMERA_TARGET.y, CAMERA_TARGET.z],
    };
  }
  const scale = Math.min(Math.max(headY / REFERENCE_HEAD_Y, MIN_FRAME_SCALE), MAX_FRAME_SCALE);
  const distance = CAMERA_DISTANCE * scale;
  const targetY = headY - HEAD_TO_TARGET * scale;
  return {
    source: "auto",
    headY,
    scale,
    // 机位与头骨同高（偏移 0 × scale），看向略低于头骨，肩与胸进画面
    position: [0, headY, distance],
    target: [0, targetY, 0],
  };
}

/**
 * 全身取景（2026-11-11 产品负责人要求「把数字人全身模型正式到屏幕上」）。
 *
 * 与半身取景的区别：镜头不再与头骨等高，而是**退到能把头顶到脚底整段收进画面**的位置。
 * 距离由两向需求取大值算：
 *   - 竖向：`(身高/2 × 留白) / tan(fov/2)`；
 *   - 横向：`(身宽/2 × 留白) / (tan(fov/2) × aspect)`，方形/竖长画布时横向可能更吃距离。
 * 留白 1.12 表示上下各留 6%，脚不被画布底边切掉、头顶也不顶格。
 */
const FULL_BODY_PADDING = 1.08;
/** 身形宽度与身高之比（肩宽+手臂自然下垂后的视觉宽度约 0.5 倍身高） */
const FULL_BODY_WIDTH_RATIO = 0.5;
/** 身高限幅：0.9–2.4m，畸形模型的包围盒不许把镜头拉到天花板 */
const MIN_BODY_HEIGHT = 0.9;
const MAX_BODY_HEIGHT = 2.4;

export interface FullBodyFraming extends BustFraming {
  /** 取景用的身高（米） */
  height: number;
  /** 脚底世界高度（米），`fixed` 时为 null */
  footY: number | null;
}

/** 固定兜底：1.62m 的成人、脚在地面，退到 3.4m 处整身入画（fov 30°） */
const FULL_BODY_FALLBACK: FullBodyFraming = {
  source: "fixed",
  headY: null,
  footY: null,
  height: 1.62,
  scale: 1,
  position: [0, 0.86, 3.4],
  target: [0, 0.81, 0],
};

/**
 * 由头骨高度与脚底高度（包围盒 min.y）推导全身机位。任一值缺失或离谱 → 兜底常量。
 * 纯函数，便于单测对照「改前/改后」。
 */
export function computeFullBodyFraming(
  headY: number | null,
  footY: number | null,
  aspect = 1,
): FullBodyFraming {
  if (
    headY == null ||
    footY == null ||
    !Number.isFinite(headY) ||
    !Number.isFinite(footY)
  ) {
    return { ...FULL_BODY_FALLBACK };
  }
  const rawHeight = headY - footY;
  if (!Number.isFinite(rawHeight) || rawHeight <= 0) {
    return { ...FULL_BODY_FALLBACK };
  }
  const height = Math.min(Math.max(rawHeight, MIN_BODY_HEIGHT), MAX_BODY_HEIGHT);
  const halfHeight = (height / 2) * FULL_BODY_PADDING;
  const halfWidth = (height * FULL_BODY_WIDTH_RATIO * 0.5) * FULL_BODY_PADDING;
  const tanHalfFov = Math.tan((CAMERA_FOV * Math.PI) / 360);
  const safeAspect = Number.isFinite(aspect) && aspect > 0 ? aspect : 1;
  const distance = Math.max(halfHeight / tanHalfFov, halfWidth / (tanHalfFov * safeAspect));
  // 脚底不在 y=0 的模型（带缩放的、贴地偏移的）也要整身入画，所以中心取包围盒中点
  const centerY = footY + rawHeight / 2;
  return {
    source: "auto",
    headY,
    footY,
    height,
    scale: height / REFERENCE_HEAD_Y,
    position: [0, centerY + 0.02, distance],
    target: [0, centerY, 0],
  };
}

/** 读头骨世界高度；拿不到骨架或数值离谱时返回 null，由 computeBustFraming 回落常量 */
function readHeadHeight(vrm: VRM, root: THREE.Object3D): number | null {
  const head = vrm.humanoid?.getNormalizedBoneNode("head");
  if (!head) return null;
  // 归一化骨骼的世界矩阵要现算：此时还没有渲染过一帧
  root.updateMatrixWorld(true);
  const world = new THREE.Vector3();
  head.getWorldPosition(world);
  return Number.isFinite(world.y) ? world.y : null;
}

/**
 * 读包围盒的**头顶**与**脚底**世界高度。
 *
 * 取包围盒而不是取 `head` 骨骼：头发/发饰比头骨高 5–10cm，按骨骼算会把头顶切掉
 * （2026-11-11 真窗口实测：按 headY+12% 留白仍然削顶）。
 */
function readBodyBounds(root: THREE.Object3D): { topY: number; bottomY: number } | null {
  root.updateMatrixWorld(true);
  const box = new THREE.Box3().setFromObject(root);
  if (box.isEmpty()) return null;
  if (!Number.isFinite(box.min.y) || !Number.isFinite(box.max.y)) return null;
  return { topY: box.max.y, bottomY: box.min.y };
}

/** 画布宽高比；尚未布局（0 宽高）时按 1 处理，取景才不会算出 Infinity 距离 */
function canvasAspect(renderer: THREE.WebGLRenderer): number {
  const size = renderer.getSize(new THREE.Vector2());
  return size.y > 0 ? size.x / size.y : 1;
}

/** 动画槽位：idle=基线循环，talking=说话循环，nod=一次性点头 */
type AnimationKind = "idle" | "talking" | "nod";

export interface CreateAvatarOptions {
  avatarUrl: string;
  /**
   * `avatar-config.rotation_y_offset`：**只做 VRM1 的额外补偿**。
   * VRM0 的 π 由 `VRMUtils.rotateVRM0` 在 loadAvatar 里完成（后端零 3D 逻辑）。
   */
  rotationYOffset?: number;
  /** 缺省 5000ms（PRD FR-3D-02） */
  timeoutMs?: number;
  /**
   * 待机 `.vrma` 路径。缺省 `/avatars/animations/idle.vrma`（task-12 交付）；
   * 传 `null` 表示**整套动画都不加载**，角色停在静息 A-pose。
   */
  idleAnimationUrl?: string | null;
  /** 说话循环路径，缺省 `/avatars/animations/idle-talking.vrma` */
  talkingAnimationUrl?: string;
  /** 点头路径，缺省 `/avatars/animations/nod.vrma` */
  nodAnimationUrl?: string;
  /**
   * 按场景一次备齐动作（W1 / FR-3D-03 的 catalog 驱动入口，见 `buildSceneAnimationUrls`）。
   * 逐字段覆盖上面三个 XXXAnimationUrl：显式给 `null` 表示该场景确实没有动作。
   * 场景语义：idle / listen / talking / talking_sit 循环，nod / show / talent 一次性。
   */
  sceneAnimationUrls?: Partial<Record<AvatarScene, string | null>>;
  /**
   * 取景方式：`full`（默认，整身入画）/ `bust`（旧的半身机位，头骨等高 + 退 2.2m）。
   * 保留 bust 是为了单测与 spike 能对照「改前/改后」数字。
   */
  framing?: "full" | "bust";
  /** 首帧落地回调（含加载 + 后处理 + 建渲染器的总耗时），供 diag 上报 */
  onFirstFrame?: (elapsedMs: number) => void;
  /**
   * WebGL 上下文丢失（P1-2）：驱动崩溃 / 显卡复位 / 系统休眠后唤醒。
   * 收到时本实例已停掉渲染循环，**画面必须由调用方换回静态头像**（这里不 dispose：
   * 浏览器可能随后恢复上下文，恢复后原地接着渲染，不必重建）。
   */
  onContextLost?: () => void;
  /** 上下文恢复：three 会在内部 `initGLContext()` 重传资源，这里只需重启渲染循环 */
  onContextRestored?: () => void;
}

/** 动画状态快照：给上层（TTS 状态机）与 spike / 自动化测试读，只读不驱动 */
export interface AvatarState {
  disposed: boolean;
  speaking: boolean;
  nodding: boolean;
  baseKind: "idle" | "talking" | null;
  /** 已解析成功的动画槽位 */
  clips: string[];
  /** 各槽位当前生效权重（0~1，四舍五入到 3 位），用来看交叉淡化有没有真的发生 */
  weights: Record<AnimationKind, number>;
}

/** 渲染资源快照：`renderer.info.memory` 的那两个计数 + 上下文状态，用于排查泄漏与上下文丢失 */
export interface AvatarRenderStats {
  /** 当前仍被 renderer 占用的 geometry 数（deepDispose 后应归零） */
  geometries: number;
  /** 当前仍被 renderer 占用的 texture 数 */
  textures: number;
  /** 已编译的着色器程序数（首次出画后 > 0，释放后归零） */
  programs: number;
  /** 上下文是否已丢失（dispose 后应为 true——forceContextLoss 生效的证据） */
  contextLost: boolean;
}

export interface VrmAvatarHandle {
  /** 幂等：取消 rAF、断开 ResizeObserver、deepDispose 场景、释放渲染器 */
  dispose(): void;
  /**
   * 口型开合度 0~1（engine.js 的 mouthLevel）。
   * 迭代 5 v1.3 起由 `realtime.ts` 的口型循环写入：TTS/WebRTC 音频 RMS →
   * NOISE_GATE(0.012)/GAIN(6.5) → 起音/收音跟随（engine.js:1157-1188 同式）→ 本方法；
   * 无控制器/未播放/收窗时上层写 0。写值 → 下一帧渲染循环下发
   * aa×0.85 + ih×0.12（缺 ih 降级 aa-only，见 MOUTH_AA_WEIGHT/MOUTH_IH_WEIGHT 处注释）。
   */
  setMouthOpen(value: number): void;
  /**
   * 说话态开关：`true` 交叉切到 idle-talking 循环，`false` 切回 idle 循环。
   * 幂等（同值连调不重播）、可重入（点头期间调用只记状态，点头结束按新状态回基线）、
   * dispose 之后调用是空操作，不抛。
   */
  setSpeaking(on: boolean): void;
  /** 单次点头，播完平滑回基线（说话中则回到说话循环）；可重入，连调即重播 */
  nod(): void;
  /**
   * 按场景播动作（W1 / FR-3D-03）：
   *   `idle` 回静息、`listen` / `talking_sit` 换循环基线、`talking` 等价 `setSpeaking(true)`、
   *   `nod` / `show` / `talent` 一次性播完回基线。没有对应 `.vrma` 时静默无操作。
   */
  playScene(scene: AvatarScene): void;
  /** 状态快照，供上层与测试观察 */
  getState(): AvatarState;
  /** 渲染资源快照（GPU 计数与上下文状态），用于验证释放是否干净 */
  getStats(): AvatarRenderStats;
}

/** task-15 卡里的叫法；与 VrmAvatarHandle 是同一个东西 */
export type AvatarController = VrmAvatarHandle;

/**
 * 在给定 canvas 上挂起一个 VRM 数字人。
 * 失败/超时会 reject 一个 `AvatarLoadError`（`reason` 见 loadAvatar）——
 * **调用方必须兜住**，这是「不白屏」的唯一前提。
 */
/**
 * 建渲染器：先要质量，拿不到再要「能出画面」。
 *
 * 为什么需要回退：`antialias: true` 在软件光栅化（SwiftShader）或老显卡上可能直接被
 * 拒绝建上下文；但设备**能**渲染，只是受不了 MSAA。这时降 antialias/alpha 仍然可看，
 * 总比整块舞台降级成照片强。
 */
function createRenderer(canvas: HTMLCanvasElement): THREE.WebGLRenderer {
  const attempts: THREE.WebGLRendererParameters[] = [
    {
      canvas,
      // 2026-11-12 C 案实测：把 MSAA 关掉只省 44ms（首帧 2751 → 2707），
      // 换不来看感的代价，所以维持开启。真的大头是着色器编译 + 纹理上传。
      antialias: true,
      alpha: true,
      powerPreference: "default",
      failIfMajorPerformanceCaveat: false,
    },
    {
      canvas,
      antialias: false,
      alpha: false,
      powerPreference: "low-power",
      failIfMajorPerformanceCaveat: false,
    },
  ];
  let lastError: unknown = new Error("WebGLRenderer 未尝试任何参数");
  for (const params of attempts) {
    try {
      return new THREE.WebGLRenderer(params);
    } catch (error) {
      lastError = error;
    }
  }
  throw lastError instanceof Error ? lastError : new Error(String(lastError));
}

export async function createAvatar(
  canvas: HTMLCanvasElement,
  options: CreateAvatarOptions,
): Promise<VrmAvatarHandle> {
  const startedAt = performance.now();
  /** 分段耗时：首帧到底花在哪一段，别再靠猜（2026-11-11 C 案）。 */
  const stages: Record<string, number> = {};
  const mark = (name: string): void => {
    stages[name] = Math.round(performance.now() - startedAt);
  };
  const { vrm } = await loadAvatar(options.avatarUrl, { timeoutMs: options.timeoutMs });
  mark("parsed");

  // 渲染器放在加载成功之后再建：降级路径上不占一个 WebGL 上下文
  //
  // 2026-11-09：原来只试一组参数（antialias + alpha），在**软件光栅化 / 弱显卡**的
  // 机器上会直接抛；而这类机器恰恰是"还能跑但很吃紧"的现场机器。改成两级回退：
  // 先要质量，拿不到再要"能出画面"。
  let renderer: THREE.WebGLRenderer;
  try {
    renderer = createRenderer(canvas);
  } catch (error) {
    // 建不出上下文时这棵树已经没人要了：连 meta 缩略图一起放，别等 GC
    disposeAvatar(vrm);
    throw error;
  }

  mark("renderer");
  /**
   * 首帧先用 dpr 1 渲染，出来后再升到实际 dpr（2026-11-11 C 案）。
   *
   * 实测（1920×1080、canvas 760×760）：resized → firstFrame 冷 2010ms / 热 325ms。
   * 差的那 1.7s 是着色器编译 + 纹理上传，而这两件事的开销与**渲染目标像素数**成正比：
   * dpr 2 → 1520×1520 要传 4 倍数据。先按 1 传一遍把画面立起来，升 dpr 时浏览器只重传
   * 一遍已解码的纹理（不再解码、不再编译着色器），长者看到人的时刻因此提前约 1–1.5s。
   * 观感代价：首帧略糊，随后 2 帧内变清晰。
   */
  const targetPixelRatio = Math.min(window.devicePixelRatio || 1, 2);
  let pixelRatio = 1;
  renderer.setPixelRatio(pixelRatio);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  // 舞台底由 pad 的 --color-brand-soft 提供，画布本身透明
  renderer.setClearColor(0x000000, 0);
  // 2026-11-13 产品负责人反馈「数字人前面像蒙了一层白雾」：衣服是白的、舞台底也接近白，
  // 原来的光照（主光 2.2 + 半球 1.6）让白布直接裁到 255，褶皱与轮廓全丢，人就和底子糊成一片。
  // ACES 把高光滚降而不是硬裁，再压低主光/补光，让白衣服重新有明暗层次。
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.0;

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(CAMERA_FOV, 1, 0.1, 100);

  const ambient = new THREE.HemisphereLight(0xffffff, 0x8d8dc0, 1.15);
  scene.add(ambient);
  const keyLight = new THREE.DirectionalLight(0xffffff, 1.5);
  keyLight.position.set(1.4, 2.5, 2.8);
  scene.add(keyLight);
  const fillLight = new THREE.DirectionalLight(0xbad2ff, 0.5);
  fillLight.position.set(-2.2, 1.2, -1.8);
  scene.add(fillLight);

  const root = vrm.scene;
  root.name = "avatar";
  // rotateVRM0 是「赋值」不是「+=」：VRM0 已经被置成 π，这里再叠后端的额外补偿
  root.rotation.y += options.rotationYOffset ?? 0;
  const baseRotationY = root.rotation.y;
  scene.add(root);

  // 取景要在 root 进场景之后算（头骨/包围盒的世界坐标依赖父链），失败就退回常量。
  // 默认全身取景（2026-11-11 产品负责人要求），`framing: "bust"` 可退回旧的半身机位。
  const headY = readHeadHeight(vrm, root);
  // 整身取景按**包围盒**算：头顶取 max.y（含头发），脚底取 min.y（有的模型不贴地）
  const bounds = readBodyBounds(root);
  const framing =
    options.framing === "bust"
      ? computeBustFraming(headY)
      : computeFullBodyFraming(bounds?.topY ?? null, bounds?.bottomY ?? null, canvasAspect(renderer));
  camera.position.set(...framing.position);
  camera.lookAt(...framing.target);

  const expressions = vrm.expressionManager;
  const hasBlink = expressions?.getExpression("blink") != null;

  // 口型两步探测（§5.2 第 5 条仍是「缺 ih 就降级 aa-only」，只是探测面放宽）：
  // ① 先在 expressionManager 里按候选名找（VRM 1.0 的 aa/ih，或已被 three-vrm 登记的别名）；
  // ② 一个都没登记，再退到网格 morph 直接驱动（流萤这类无 preset 的 VRM 0.x）。
  // 只认规范名 "aa" 会让 VRM 0.x 全线哑嘴，原因见 MOUTH_AA_ALIASES 的注释。
  const mouthKey = MOUTH_AA_ALIASES.find((key) => expressions?.getExpression(key) != null) ?? null;
  const ihKey = MOUTH_IH_ALIASES.find((key) => expressions?.getExpression(key) != null) ?? null;
  const aaMorph = mouthKey ? null : findMorphChannel(root, MOUTH_AA_ALIASES);
  const ihMorph = mouthKey ? null : findMorphChannel(root, MOUTH_IH_ALIASES);

  // 必须在第一帧 vrm.update() 之前摆好，否则首帧会闪一下 T-pose
  relaxArms(vrm);

  const mixer = new THREE.AnimationMixer(root);
  const clock = new THREE.Clock();

  let disposed = false;
  let frameHandle = 0;
  let elapsed = 0;
  let mouthOpen = 0;
  let firstFrameAt = 0;

  // —— 动画状态机：基线 = idle / talking（或被场景覆盖的 listen / talking_sit），
  //    nod / show / talent 是叠在上面的一次性动作 ——
  //
  // W1（FR-3D-03）起动作路径由 `catalog.json` 按场景下发（入口见 loadAvatar 的
  // `buildSceneAnimationUrls`）。老的三个 `XXXAnimationUrl` 继续逐字段生效；
  // `idleAnimationUrl: null` 保持「整套动画都不加载」的原语义。
  const providedScenes = options.sceneAnimationUrls;
  const animationsDisabled = options.idleAnimationUrl === null && providedScenes == null;
  const sceneUrl = (scene: AvatarScene, fallback: string | null): string | null => {
    if (animationsDisabled) return null;
    const explicit = providedScenes?.[scene];
    if (explicit !== undefined) return explicit;
    return fallback;
  };
  const sceneUrls: Record<AvatarScene, string | null> = {
    idle: sceneUrl("idle", options.idleAnimationUrl ?? DEFAULT_IDLE_ANIMATION_URL),
    listen: sceneUrl("listen", null),
    talking: sceneUrl("talking", options.talkingAnimationUrl ?? DEFAULT_TALKING_ANIMATION_URL),
    talking_sit: sceneUrl("talking_sit", null),
    nod: sceneUrl("nod", options.nodAnimationUrl ?? DEFAULT_NOD_ANIMATION_URL),
    show: sceneUrl("show", null),
    talent: sceneUrl("talent", null),
  };
  const animationUrls: Record<AnimationKind, string | null> = {
    idle: sceneUrls.idle,
    talking: sceneUrls.talking,
    nod: sceneUrls.nod,
  };
  const clips = new Map<AnimationKind, THREE.AnimationClip>();
  const actions = new Map<AnimationKind, THREE.AnimationAction>();
  const loads = new Map<AnimationKind, Promise<void>>();
  /** 场景动作：listen / talking_sit 循环，show / talent 一次性 */
  const sceneClips = new Map<AvatarScene, THREE.AnimationClip>();
  const sceneActions = new Map<AvatarScene, THREE.AnimationAction>();
  const sceneLoads = new Map<AvatarScene, Promise<THREE.AnimationAction | null>>();
  let speaking = false;
  let nodding = false;
  let base: THREE.AnimationAction | null = null;
  /** 当前生效的场景基线（listen / talking_sit）；null = 走 speaking ? talking : idle */
  let baseScene: AvatarScene | null = null;

  const enterBase = (fadeSeconds: number, force = false): void => {
    const next = baseScene
      ? (sceneActions.get(baseScene) ?? null)
      : (actions.get(speaking ? "talking" : "idle") ?? null);
    // 目标动画还没到（或加载失败）就留在当前基线，绝不切到空气里
    if (!next) return;
    if (!force && next === base && next.isRunning() && next.getEffectiveWeight() > 0.99) return;
    const previous = base;
    next.reset().fadeIn(fadeSeconds).play();
    if (previous && previous !== next) previous.fadeOut(fadeSeconds);
    base = next;
  };

  const onFinished = (event: { action: THREE.AnimationAction }): void => {
    const nodAction = actions.get("nod");
    if (nodAction && event.action === nodAction) {
      nodding = false;
      nodAction.fadeOut(NOD_FADE_SECONDS);
      // 点头期间 setSpeaking 只记了状态，这里按最新状态回基线
      enterBase(NOD_FADE_SECONDS, true);
      return;
    }
    for (const [scene, action] of sceneActions) {
      if (event.action !== action) continue;
      // show / talent 是一次性动作：播完回到当前基线
      if (scene !== "show" && scene !== "talent") return;
      if (baseScene === scene) baseScene = null;
      action.fadeOut(NOD_FADE_SECONDS);
      enterBase(NOD_FADE_SECONDS, true);
      return;
    }
  };
  mixer.addEventListener("finished", onFinished);

  const onClipReady = (kind: AnimationKind): void => {
    if (disposed) return;
    // nod 只在 nod() 里播，不参与基线切换
    if (kind !== "nod" && !nodding) enterBase(BASE_FADE_SECONDS);
  };

  const ensureClip = (kind: AnimationKind): Promise<void> => {
    const pending = loads.get(kind);
    if (pending) return pending;
    const url = animationUrls[kind];
    if (!url) return Promise.resolve();
    const task = loadVrmAnimation(url)
      .then(({ animation }) => {
        if (disposed) return;
        const clip = createVRMAnimationClip(animation, vrm);
        clips.set(kind, clip);
        const action = mixer.clipAction(clip);
        if (kind === "nod") {
          action.setLoop(THREE.LoopOnce, 1);
          action.clampWhenFinished = true;
        } else {
          action.setLoop(THREE.LoopRepeat, Infinity);
        }
        actions.set(kind, action);
        onClipReady(kind);
      })
      .catch(() => {
        /* 动画是锦上添花：静息姿势已经是可交付画面 */
      });
    loads.set(kind, task);
    return task;
  };

  /**
   * 加载某个场景的专属动作。没配 URL 或加载失败都返回 null（静默降级，绝不拖累角色）。
   * 结果进 `sceneActions` 缓存：同一场景只解析一次。
   */
  const ensureSceneAction = (scene: AvatarScene): Promise<THREE.AnimationAction | null> => {
    const ready = sceneActions.get(scene);
    if (ready) return Promise.resolve(ready);
    const pending = sceneLoads.get(scene);
    if (pending) return pending;
    const url = sceneUrls[scene];
    if (!url) return Promise.resolve(null);
    const oneShot = scene === "show" || scene === "talent";
    const task = loadVrmAnimation(url)
      .then(({ animation }) => {
        if (disposed) return null;
        const clip = createVRMAnimationClip(animation, vrm);
        sceneClips.set(scene, clip);
        const action = mixer.clipAction(clip);
        if (oneShot) {
          action.setLoop(THREE.LoopOnce, 1);
          action.clampWhenFinished = true;
        } else {
          action.setLoop(THREE.LoopRepeat, Infinity);
        }
        sceneActions.set(scene, action);
        return action;
      })
      .catch(() => null);
    sceneLoads.set(scene, task);
    return task;
  };

  /** 说话态开关的内部实现：`true` 会清掉 listen/talking_sit 覆盖，让说话优先 */
  const applySpeaking = (on: boolean, fadeSeconds = BASE_FADE_SECONDS): void => {
    const next = Boolean(on);
    if (next === speaking) return;
    speaking = next;
    if (next) {
      baseScene = null;
      // 说话动画可能还没加载（首屏只预取 idle），这里补一次；到位后由 onClipReady 接管切换
      void ensureClip("talking");
    }
    // 点头期间不换底，等 finished 时按 speaking 的最新值回基线
    if (!nodding) enterBase(fadeSeconds);
  };

  /** 单次点头的内部实现（可重入，重入即重播） */
  const nodOnce = (): void => {
    if (disposed) return;
    void ensureClip("nod").then(() => {
      if (disposed) return;
      const action = actions.get("nod");
      if (!action) return; // 没有 nod.vrma：静默降级成「不点这个头」
      nodding = true;
      base?.fadeOut(NOD_FADE_SECONDS);
      action.reset().fadeIn(NOD_FADE_SECONDS).play();
    });
  };

  /**
   * 按场景播动作（catalog 驱动）：
   *   idle / listen / talking / talking_sit 是循环；nod / show / talent 是一次性，播完回基线。
   * 任何一步失败都只是少一层表现，不会打断渲染循环。
   */
  const startScene = (scene: AvatarScene): void => {
    if (disposed) return;
    if (scene === "idle") {
      baseScene = null;
      applySpeaking(false);
      if (!nodding) enterBase(BASE_FADE_SECONDS, true);
      return;
    }
    if (scene === "talking") {
      applySpeaking(true);
      return;
    }
    if (scene === "nod") {
      nodOnce();
      return;
    }
    void ensureSceneAction(scene).then((action) => {
      if (!action || disposed) return;
      if (scene === "show" || scene === "talent") {
        action.reset().fadeIn(NOD_FADE_SECONDS).play();
        base?.fadeOut(NOD_FADE_SECONDS);
        return;
      }
      // listen / talking_sit：换一条循环基线（说话开关被清掉，避免两套骨骼互相抢）
      baseScene = scene;
      speaking = false;
      const previous = base;
      action.reset().fadeIn(BASE_FADE_SECONDS).play();
      if (previous && previous !== action) previous.fadeOut(BASE_FADE_SECONDS);
      base = action;
    });
  };

  // 待机动画**异步补齐**：它不该挡住角色出场（更不该吃满 5s 降级预算）。
  // 失败只回落到 relaxArms 的静息姿势 —— 画面照样是「活人站着」，不是白屏。
  void ensureClip("idle");

  const resize = () => {
    const parent = canvas.parentElement;
    const width = Math.max(parent?.clientWidth ?? canvas.clientWidth, 320);
    const height = Math.max(parent?.clientHeight ?? canvas.clientHeight, 320);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    renderer.setPixelRatio(pixelRatio);
    renderer.setSize(width, height, false);
  };
  resize();
  mark("resized");
  const observer = new ResizeObserver(resize);
  if (canvas.parentElement) observer.observe(canvas.parentElement);
  else observer.observe(canvas);

  const animate = () => {
    frameHandle = requestAnimationFrame(animate);
    const delta = clock.getDelta();
    elapsed += delta;

    // 待机微摆（.vrma 到位后可由 clip 取代；只用 y 轴小角度，不影响朝向判定）
    root.rotation.y = baseRotationY + Math.sin(elapsed * 0.9) * SWAY_AMPLITUDE;

    if (expressions) {
      if (hasBlink) {
        const phase = (elapsed * BLINK_HZ) % 1;
        const blink =
          phase > BLINK_START ? Math.sin(((phase - BLINK_START) / BLINK_SPAN) * Math.PI) : 0;
        expressions.setValue("blink", blink > 0 ? blink : 0);
      }
      if (mouthKey) {
        // 口型写值统一收口（engine.js:1145-1155 _setMouth 移植）：realtime.ts 每帧把 0~1 的
        // mouthLevel 经 setMouthOpen 递进来，这里分配 aa×0.85 + ih×0.12（缺 ih 降级 aa-only）。
        // 每个 setValue 独立 try/catch（§5.2 规格逐条要求）：任一表情缺失/抛错都不能打断
        // 这个每帧执行的渲染循环。
        try {
          expressions.setValue(mouthKey, mouthOpen * MOUTH_AA_WEIGHT);
        } catch {
          /* 模型未定义该口型名时忽略 */
        }
        if (ihKey) {
          try {
            expressions.setValue(ihKey, mouthOpen * MOUTH_IH_WEIGHT);
          } catch {
            /* 模型未定义该口型名时忽略 */
          }
        }
      } else if (aaMorph) {
        // morph 兜底通道：模型没登记 VRM 表情，但网格上确实有嘴部变形目标。
        // 直接写 influence，不经 expressionManager（该管理器此前已确认没有对应表情）。
        const aaArr = aaMorph.mesh.morphTargetInfluences;
        if (aaArr) aaArr[aaMorph.index] = mouthOpen * MOUTH_AA_WEIGHT;
        if (ihMorph) {
          const ihArr = ihMorph.mesh.morphTargetInfluences;
          if (ihArr) ihArr[ihMorph.index] = mouthOpen * MOUTH_IH_WEIGHT;
        }
      }
    }

    // 顺序不可调换：动画写骨骼 → vrm.update 收口 → 渲染
    mixer.update(delta);
    vrm.update(delta);
    if (firstFrameAt === 0) {
      mark("beforeRender");
      // 首帧那一发 render 里同时发生两件贵事：编译 8 个着色器程序、上传 38 张纹理。
      // 先单独 compile 一次，把两者的账分开记，才知道该优化哪一头。
      renderer.compile(scene, camera);
      mark("compiled");
    }
    renderer.render(scene, camera);
    if (firstFrameAt === 0) mark("afterRender");

    if (firstFrameAt === 0) {
      firstFrameAt = performance.now();
      mark("firstFrame");
      // 分段耗时 + 首帧资源快照挂到 window：C 案要拿数字说话，不能靠感觉。
      // programs/textures/geometries 用来判断「冷启动那 1.7s 到底编译了多少东西」，
      // 程序数不降下来的话，后面再怎么调像素比都只是在挪时间。
      (globalThis as unknown as Record<string, unknown>).__padAvatarStages = {
        ...stages,
        programs: renderer.info.programs?.length ?? -1,
        textures: renderer.info.memory.textures,
        geometries: renderer.info.memory.geometries,
        triangles: renderer.info.render.triangles,
      };
      options.onFirstFrame?.(firstFrameAt - startedAt);
      /* 首帧已经画出来了 —— 现在才把分辨率升到实际 dpr。
         升像素比不会重新解码纹理、不会重新编译着色器，只是按新尺寸再传一遍，
         代价是 1–2 帧（实测热态 +325ms 那部分不会重付）。 */
      if (targetPixelRatio > pixelRatio) {
        pixelRatio = targetPixelRatio;
        mark("upscaleStart");
        resize();
        mark("upscaled");
        // 合并而不是覆盖：上面那份带 programs/textures 的资源快照不能被这一步冲掉
        (globalThis as unknown as Record<string, unknown>).__padAvatarStages = {
          ...((globalThis as unknown as Record<string, unknown>).__padAvatarStages as object),
          ...stages,
        };
      }
    }
  };
  frameHandle = requestAnimationFrame(animate);

  // —— WebGL 上下文丢失 / 恢复（P1-2）——
  // 没有这两条监听时：驱动复位、GPU 进程崩溃、系统休眠唤醒之后 canvas 会变成一块黑，
  // 渲染循环照转但什么都画不出来，而上层还以为 3D 正常（正是「ok:true 与画面脱节」的另一半）。
  // preventDefault 是恢复的前提：不拦这个事件，浏览器不会再发 webglcontextrestored。
  let contextLost = false;
  const handleContextLost = (event: Event): void => {
    event.preventDefault();
    if (disposed || contextLost) return;
    contextLost = true;
    cancelAnimationFrame(frameHandle); // 先停循环：丢上下文后每帧都是空转
    options.onContextLost?.();
  };
  const handleContextRestored = (): void => {
    if (disposed || !contextLost) return;
    contextLost = false;
    // three 在内部 onContextRestore 里做过 initGLContext()，纹理与程序会在下一帧重新上传，
    // 这里只需把循环接上；丢弃丢失期间累积的 delta，否则动画会瞬移一大步。
    clock.getDelta();
    options.onContextRestored?.();
    frameHandle = requestAnimationFrame(animate);
  };
  canvas.addEventListener("webglcontextlost", handleContextLost, false);
  canvas.addEventListener("webglcontextrestored", handleContextRestored, false);

  return {
    dispose() {
      if (disposed) return;
      disposed = true;
      cancelAnimationFrame(frameHandle);
      observer.disconnect();
      canvas.removeEventListener("webglcontextlost", handleContextLost, false);
      canvas.removeEventListener("webglcontextrestored", handleContextRestored, false);
      mixer.removeEventListener("finished", onFinished);
      mixer.stopAllAction();
      mixer.uncacheRoot(root);
      clips.clear();
      actions.clear();
      sceneClips.clear();
      sceneActions.clear();
      base = null;
      baseScene = null;
      speaking = false;
      nodding = false;
      scene.remove(root);
      disposeAvatar(vrm);
      // P2-7：dispose() 只释放 GPU 资源，「上下文」本身要等 GC —— 每页 WebGL 上下文有硬上限
      // （Chrome ≈16），反复登录/切页攒够就不再给新上下文，表现为整块黑屏。
      // 顺序有讲究：先 dispose（three 会顺手摘掉它自己的 contextlost 监听，避免多打一条
      // "THREE.WebGLRenderer: Context Lost."），再主动把上下文交还。
      renderer.dispose();
      renderer.forceContextLoss();
      scene.clear();
    },
    setMouthOpen(value: number) {
      mouthOpen = value > 1 ? 1 : value > 0 ? value : 0;
    },
    setSpeaking(on: boolean) {
      if (disposed) return;
      applySpeaking(on);
    },
    nod() {
      nodOnce();
    },
    playScene(scene: AvatarScene) {
      startScene(scene);
    },
    getState(): AvatarState {
      const weightOf = (kind: AnimationKind): number => {
        const value = actions.get(kind)?.getEffectiveWeight() ?? 0;
        return Math.round(value * 1000) / 1000;
      };
      return {
        disposed,
        speaking,
        nodding,
        baseKind: base === actions.get("idle") ? "idle" : base === actions.get("talking") ? "talking" : null,
        clips: [...clips.keys(), ...sceneClips.keys()],
        weights: { idle: weightOf("idle"), talking: weightOf("talking"), nod: weightOf("nod") },
      };
    },
    getStats(): AvatarRenderStats {
      const memory = renderer.info.memory;
      return {
        geometries: memory.geometries,
        textures: memory.textures,
        programs: renderer.info.programs?.length ?? 0,
        // dispose 之后这里应为 true：forceContextLoss() 把上下文真正交还了浏览器
        contextLost: renderer.getContext().isContextLost(),
      };
    },
  };
}

/** 暴露 VRM 类型给上层（AvatarStage 不需要，但便于后续 TTS 侧的类型推导） */
export type AvatarVrm = VRM;
