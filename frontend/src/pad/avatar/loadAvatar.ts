/**
 * VRM 资产加载（FR-3D-02 的 5s 竞速点）。
 *
 * 只负责「把 URL 变成一棵已后处理的 VRM 场景树」：
 *   GLTFLoader + VRMLoaderPlugin（三连第 1 步）
 *   → removeUnnecessaryVertices / removeUnnecessaryJoints（第 2、3 步，省内存）
 *   → rotateVRM0（VRM0 补 π；VRM1 内部 no-op —— 见 three-vrm rotateVRM0 只判 metaVersion）
 *
 * 不建渲染器、不碰 DOM：超时只影响「还要不要继续等」，不会留下 WebGL 上下文。
 *
 * 但「不再等」≠「可以不管」：竞速输掉的那棵树仍在后台下载+解析（GLTFLoader 没有 abort 通道），
 * 迟到成功时没人接就会把 10.3MB 的场景图与纹理挂到 GC 为止 —— 见 raceWithTimeout 与
 * disposeAvatar 的注释（P1-1）。
 */
import type { Object3D } from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { VRMLoaderPlugin, VRMUtils, type VRM } from "@pixiv/three-vrm";
import { VRMAnimationLoaderPlugin, type VRMAnimation } from "@pixiv/three-vrm-animation";

/** 降级原因分类：进 client_diag 的 error 字段（脱敏后只剩分类码，不带 URL） */
export type AvatarLoadReason = "timeout" | "http" | "no_vrm" | "no_animation" | "network" | "unknown";

/** PRD FR-3D-02：加载失败或超过 5s → 切静态头像 */
const DEFAULT_TIMEOUT_MS = 5000;

/** 待机动画是锦上添花：它慢不该拖累角色出场，单独给一个更短的预算 */
const DEFAULT_ANIMATION_TIMEOUT_MS = 3000;

/** task-12 交付的 Apache-2.0 动画；`frontend/public/avatars/` → 构建后 `/avatars/` */
export const DEFAULT_IDLE_ANIMATION_URL = "/avatars/animations/idle.vrma";
/** 说话时的基线循环（`setSpeaking(true)` 切这条） */
export const DEFAULT_TALKING_ANIMATION_URL = "/avatars/animations/idle-talking.vrma";
/** 一次性点头（`nod()` 叠播一次） */
export const DEFAULT_NOD_ANIMATION_URL = "/avatars/animations/nod.vrma";

// ------------------------------------------------------------------ 资产目录（catalog.json）
//
// W1 / FR-3D-03：形象与动作不再写死在代码里。`frontend/public/avatars/catalog.json` 由
// `scripts/sync_avatar_assets.ps1` 从 vrm-playground 生成（8 个形象 + 6 条动作），前端只读它；
// 上面的 `DEFAULT_*` 常量降级为「目录缺失/单条缺失」时的兜底，保证任何情况下都不白屏。
//
// 三条容错口径（缺一不可）：
//   1. catalog.json 404 / 超时 / 不是合法 JSON → 返回 null，调用方走硬编码兜底；
//   2. 单条记录字段不合法 / URL 越出 `/avatars/` → 丢掉这一条，其余条目照用；
//   3. 某个场景没有动作 → 按场景回退链退到 idle，再退到 DEFAULT_IDLE_ANIMATION_URL。

/** 目录地址；`frontend/public/avatars/` → 构建后 `/avatars/` */
export const AVATAR_CATALOG_URL = "/avatars/catalog.json";

/** 只允许同源静态目录：catalog 被篡改时也不能把页面指向外部 URL */
const AVATAR_URL_PREFIX = "/avatars/";

/** 目录请求超时：它只是锦上添花，不能和 5s 降级预算抢时间 */
const CATALOG_TIMEOUT_MS = 2000;

/** 动作场景（与 `scripts/sync_avatar_assets.ps1` 的 `Scene` 字段一一对应） */
export type AvatarScene = "idle" | "listen" | "talking" | "talking_sit" | "nod" | "show" | "talent";

/** 目录里的一条形象 */
export interface AvatarModelEntry {
  id: string;
  display_name: string;
  /** `female` / `male`；缺省或未知时按 `unknown` 处理，不参与性别筛选 */
  gender: string;
  character?: string;
  file: string;
  /** 已校验为 `/avatars/...` 的静态路径 */
  url: string;
  bytes?: number;
  /** 低配平板优先挑 `light`（计划 §2.3） */
  weight?: string;
  license?: string;
  sha256?: string;
}

/** 目录里的一条动作 */
export interface AvatarAnimationEntry {
  id: string;
  display_name?: string;
  scene: string;
  file: string;
  url: string;
  bytes?: number;
  license?: string;
  /** 仓库内置的 3 条（idle / idle-talking / nod）不随同步脚本拷贝 */
  bundled?: boolean;
  sha256?: string;
}

/** `catalog.json` 的解析结果 */
export interface AvatarCatalog {
  version: number;
  generated_at?: string;
  source?: string;
  models: AvatarModelEntry[];
  animations: AvatarAnimationEntry[];
  /** 同步时没拷到的条目（源缺失 / 目标冲突），前端不用它，只用于排障 */
  skipped?: Array<{ id?: string; kind?: string; file?: string; reason?: string }>;
}

/** 场景缺省动作的硬编码兜底（catalog 缺失或某场景无条目时使用） */
export const DEFAULT_SCENE_ANIMATION_URLS: Record<AvatarScene, string> = {
  idle: DEFAULT_IDLE_ANIMATION_URL,
  listen: DEFAULT_IDLE_ANIMATION_URL,
  talking: DEFAULT_TALKING_ANIMATION_URL,
  talking_sit: DEFAULT_TALKING_ANIMATION_URL,
  nod: DEFAULT_NOD_ANIMATION_URL,
  show: DEFAULT_IDLE_ANIMATION_URL,
  talent: DEFAULT_IDLE_ANIMATION_URL,
};

/** 场景回退链：该场景没有专属动作时，依次退到这些场景的动作 */
const SCENE_FALLBACKS: Record<AvatarScene, readonly AvatarScene[]> = {
  idle: [],
  listen: ["idle"],
  talking: ["idle"],
  talking_sit: ["talking", "idle"],
  nod: [],
  show: ["idle"],
  talent: ["idle"],
};

/**
 * 目录缓存：同一页面只请求一次。
 *
 * 失败结果也缓存（null）：catalog 缺失是常态（干净 clone 里没有 `models/`），
 * 不让每次挂载都白等 2s 超时。页面刷新即可重试。
 */
let catalogPromise: Promise<AvatarCatalog | null> | null = null;

/** 单测用：重置目录缓存 */
export function resetAvatarCatalogForTest(): void {
  catalogPromise = null;
}

/**
 * 读资产目录。任何失败都返回 `null`（调用方必须能接受 null），**不抛**。
 * 与 `preloadAvatar` / `AvatarStage` 共用一个 promise，不会并发拉两份。
 */
export async function loadAvatarCatalog(
  options: { timeoutMs?: number } = {},
): Promise<AvatarCatalog | null> {
  if (!catalogPromise) {
    catalogPromise = fetchAvatarCatalog(options.timeoutMs ?? CATALOG_TIMEOUT_MS).catch(() => null);
  }
  return catalogPromise;
}

async function fetchAvatarCatalog(timeoutMs: number): Promise<AvatarCatalog | null> {
  if (typeof fetch !== "function") return null;
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(AVATAR_CATALOG_URL, { signal: controller.signal });
    if (!response.ok) return null;
    return normalizeAvatarCatalog(await response.json());
  } finally {
    window.clearTimeout(timer);
  }
}

/** 只认同源 `/avatars/` 下的相对路径（绝对 URL、`javascript:`、`//evil` 一律丢掉） */
function isAvatarUrl(value: unknown): value is string {
  return typeof value === "string" && value.startsWith(AVATAR_URL_PREFIX) && !value.includes("..");
}

function asModelEntry(raw: unknown): AvatarModelEntry | null {
  if (!raw || typeof raw !== "object") return null;
  const row = raw as Record<string, unknown>;
  if (typeof row.id !== "string" || !row.id) return null;
  if (!isAvatarUrl(row.url)) return null;
  return {
    id: row.id,
    display_name: typeof row.display_name === "string" && row.display_name ? row.display_name : row.id,
    gender: typeof row.gender === "string" ? row.gender : "unknown",
    character: typeof row.character === "string" ? row.character : undefined,
    file: typeof row.file === "string" ? row.file : row.url.split("/").pop() ?? row.id,
    url: row.url,
    bytes: typeof row.bytes === "number" ? row.bytes : undefined,
    weight: typeof row.weight === "string" ? row.weight : undefined,
    license: typeof row.license === "string" ? row.license : undefined,
    sha256: typeof row.sha256 === "string" ? row.sha256 : undefined,
  };
}

function asAnimationEntry(raw: unknown): AvatarAnimationEntry | null {
  if (!raw || typeof raw !== "object") return null;
  const row = raw as Record<string, unknown>;
  if (typeof row.id !== "string" || !row.id) return null;
  if (!isAvatarUrl(row.url)) return null;
  if (typeof row.scene !== "string" || !row.scene) return null;
  return {
    id: row.id,
    display_name: typeof row.display_name === "string" ? row.display_name : undefined,
    scene: row.scene,
    file: typeof row.file === "string" ? row.file : row.url.split("/").pop() ?? row.id,
    url: row.url,
    bytes: typeof row.bytes === "number" ? row.bytes : undefined,
    license: typeof row.license === "string" ? row.license : undefined,
    bundled: row.bundled === true,
    sha256: typeof row.sha256 === "string" ? row.sha256 : undefined,
  };
}

/**
 * 校验并归一化目录。`models` 与 `animations` 都是空（或整份坏掉）时返回 null ——
 * 「空的目录」与「没有目录」对调用方是同一件事：都该走兜底。
 */
export function normalizeAvatarCatalog(raw: unknown): AvatarCatalog | null {
  if (!raw || typeof raw !== "object") return null;
  const record = raw as Record<string, unknown>;
  const models = Array.isArray(record.models)
    ? record.models.map(asModelEntry).filter((entry): entry is AvatarModelEntry => entry !== null)
    : [];
  const animations = Array.isArray(record.animations)
    ? record.animations
        .map(asAnimationEntry)
        .filter((entry): entry is AvatarAnimationEntry => entry !== null)
    : [];
  if (models.length === 0 && animations.length === 0) return null;
  return {
    version: typeof record.version === "number" ? record.version : 0,
    generated_at: typeof record.generated_at === "string" ? record.generated_at : undefined,
    source: typeof record.source === "string" ? record.source : undefined,
    models,
    animations,
    skipped: Array.isArray(record.skipped) ? (record.skipped as AvatarCatalog["skipped"]) : undefined,
  };
}

/** 是否需要「低配优先」：省流量或窄屏（<1366px，平板竖屏 / 小笔记本）时挑 light 形象 */
export function preferLightAvatar(): boolean {
  try {
    const connection = (navigator as unknown as { connection?: { saveData?: boolean } }).connection;
    if (connection?.saveData === true) return true;
  } catch {
    /* 拿不到 connection 信息（Safari/Firefox）就按网络正常处理 */
  }
  try {
    return typeof window !== "undefined" && window.innerWidth > 0 && window.innerWidth < 1366;
  } catch {
    return false;
  }
}

/** 目录里的默认形象（无目录 → 仓库自带的调试样本，保证「至少有一个人」） */
export function pickAvatarUrl(
  catalog: AvatarCatalog | null,
  options: { gender?: "female" | "male"; preferLight?: boolean } = {},
): string {
  if (!catalog || catalog.models.length === 0) return DEFAULT_AVATAR_URL;
  const byGender =
    options.gender != null ? catalog.models.filter((model) => model.gender === options.gender) : [];
  const pool = byGender.length > 0 ? byGender : catalog.models;
  const ordered = options.preferLight
    ? [...pool.filter((model) => model.weight === "light"), ...pool.filter((model) => model.weight !== "light")]
    : pool;
  return ordered[0]?.url ?? DEFAULT_AVATAR_URL;
}

/** 按场景挑一条动作：场景本身 → 回退链 → 硬编码兜底 */
export function pickSceneAnimationUrl(
  catalog: AvatarCatalog | null,
  scene: AvatarScene,
): string {
  if (catalog) {
    const scenes = [scene, ...SCENE_FALLBACKS[scene]];
    for (const candidate of scenes) {
      const hit = catalog.animations.find((animation) => animation.scene === candidate);
      if (hit) return hit.url;
    }
  }
  return DEFAULT_SCENE_ANIMATION_URLS[scene];
}

/** 一次性给 `createAvatar` 备齐全部场景（缺项退回兜底，`null` 表示该场景确实没有动作） */
export function buildSceneAnimationUrls(
  catalog: AvatarCatalog | null,
): Record<AvatarScene, string | null> {
  return {
    idle: pickSceneAnimationUrl(catalog, "idle"),
    listen: catalog ? findSceneUrl(catalog, "listen") ?? DEFAULT_SCENE_ANIMATION_URLS.listen : DEFAULT_SCENE_ANIMATION_URLS.listen,
    talking: pickSceneAnimationUrl(catalog, "talking"),
    talking_sit: catalog
      ? findSceneUrl(catalog, "talking_sit") ?? DEFAULT_SCENE_ANIMATION_URLS.talking_sit
      : DEFAULT_SCENE_ANIMATION_URLS.talking_sit,
    nod: pickSceneAnimationUrl(catalog, "nod"),
    show: catalog ? findSceneUrl(catalog, "show") : DEFAULT_SCENE_ANIMATION_URLS.show,
    talent: catalog ? findSceneUrl(catalog, "talent") : DEFAULT_SCENE_ANIMATION_URLS.talent,
  };
}

/** 只按场景本身找（不回退）：用于「可选场景」，没有就是 null，别拿静息冒充才艺 */
function findSceneUrl(catalog: AvatarCatalog, scene: AvatarScene): string | null {
  return catalog.animations.find((animation) => animation.scene === scene)?.url ?? null;
}

/** 仓库自带的兜底形象（catalog 缺失、且没有登录/配置时用） */
export const DEFAULT_AVATAR_URL = "/avatars/vrm1_sample.vrm";

export class AvatarLoadError extends Error {
  readonly reason: AvatarLoadReason;

  constructor(reason: AvatarLoadReason, message: string, cause?: unknown) {
    super(message, { cause });
    this.name = "AvatarLoadError";
    this.reason = reason;
  }
}

export interface LoadAvatarOptions {
  /** 缺省 5000ms（FR-3D-02） */
  timeoutMs?: number;
}

export interface LoadedAvatar {
  vrm: VRM;
  /** 从调 loadAsync 到后处理完成（不含渲染）——spike 与线上首帧指标共用同一口径 */
  elapsedMs: number;
}

type GltfRoot = Awaited<ReturnType<GLTFLoader["loadAsync"]>>;

/**
 * 释放一棵**不会再用**的 VRM（超时后被竞速丢弃的资产、渲染器建不出来的回滚）。
 *
 * `VRMUtils.deepDispose` 只吃 Object3D，覆盖场景图里的 geometry / skeleton / material / texture
 * —— three-vrm 3.5.3 的 `deepDispose` 对普通材质只遍历 `Object.values(material)` 里的 Texture，
 * 对 ShaderMaterial 还额外遍历 uniforms；MToon 的贴图因此都在覆盖范围内。
 * 覆盖不到的是 VRM1 的缩略图：它是 `HTMLImageElement`，挂在 `vrm.meta.thumbnailImage`，不在场景图里，
 * 这条手动放（`HTMLImageElement` 没有 close()，置空 src 让浏览器回收已解码位图）。
 */
export function disposeAvatar(vrm: VRM): void {
  VRMUtils.deepDispose(vrm.scene);
  const thumbnail = (vrm.meta as { thumbnailImage?: HTMLImageElement } | undefined)?.thumbnailImage;
  if (thumbnail) thumbnail.src = "";
}

/** 只释放 GLTF 根：解析不出 VRM、或动画资产被丢弃时用（没有 meta 可放） */
function disposeScene(root: Object3D): void {
  VRMUtils.deepDispose(root);
}

export interface LoadedAnimation {
  animation: VRMAnimation;
  elapsedMs: number;
}

/**
 * 登录前解析好的 VRM，按 URL 存一份**一次性**缓存（2026-11-11「一打开页面就有人」）。
 *
 * 为什么缓存的是「解析结果」而不是字节：实测登录后到模型出现在画面上要 2–4 秒，其中
 * 下载 2.0s（已用 `fetch` 预热进 HTTP 缓存，仍然要解压+读盘）、GLTFLoader 解析 +
 * `removeUnnecessaryJoints` + 纹理解码约 1–2s。前者能在登录页等，后者只能等登录后才有
 * 渲染器——但**解析本身不需要渲染器**，所以可以在登录页就把 GLTF 解析完，登录时直接接管。
 */
const preloaded = new Map<string, Promise<LoadedAvatar>>();

/**
 * 缓存键：把相对 URL 与绝对 URL 归一成同一个（预热时用 `/avatars/x.vrm`，而
 * `avatar-config` 下发的是 `https://localhost/avatars/x.vrm` —— 不归一就永远不命中，
 * 2026-11-11 实测：首帧仍是 3.7s）。没有 `location`（Node 单测）时原样返回。
 */
function preloadKey(url: string): string {
  try {
    return new URL(url, globalThis.location?.href).href;
  } catch {
    return url;
  }
}

/**
 * 预热解析：把 URL 变成已后处理的 VRM 放进缓存，供随后的 `loadAvatar` 直接取走。
 * 失败静默（预热只是优化）；真正 `createAvatar` 时还会按未命中路径再试一次。
 */
export function preloadAvatarModel(
  url: string,
  options: LoadAvatarOptions = {},
): Promise<LoadedAvatar> {
  const key = preloadKey(url);
  const existing = preloaded.get(key);
  if (existing) return existing;
  const pending = loadAvatarUncached(url, options);
  // 失败就从缓存里摘掉：否则登录后拿到的是一个已 reject 的 promise，白白降级
  pending.catch(() => preloaded.delete(key));
  preloaded.set(key, pending);
  return pending;
}

/** 取走（并从缓存删除）预热结果。一次性：同一个 VRM 对象不能被两次 `createAvatar` 共用。 */
function takePreloadedAvatar(url: string): Promise<LoadedAvatar> | null {
  const key = preloadKey(url);
  const pending = preloaded.get(key);
  if (!pending) return null;
  preloaded.delete(key);
  return pending;
}

/**
 * 纹理上限（像素边长）。**这不是画质参数，是加载时长参数。**
 *
 * 2026-11-12 C 案实测：这个 VRM 带 19 张 PNG、合计 **22.3MP**（最大三张 2048×2048），
 * 首帧那一发 `renderer.render` 里 1.6s 基本全花在解码后的纹理上传上，比下载（0.47s）
 * 和 GLTF 解析（0.6s）都贵。而数字人在长者端屏幕上最大也就 582px 见方——
 * 2048 贴图是 3.5 倍过采样，肉眼在 1080p 平板上分辨不出 2048 与 1024 的差别。
 *
 * 降到 1024 后总像素降到 1/4，上传量同比例下降；下载体积不变（PNG 已在文件里）。
 * 要改这个数，得同时改 `frontend/tests/avatar-tex-cap.test.ts` 的期望值。
 */
export const AVATAR_TEXTURE_MAX_EDGE = 1024;

/** 把一张已解码的贴图画进一张更小的 canvas（等比，长边不超过 maxEdge）。原图不大于上限时返回 null。 */
function shrinkTextureImage(source: CanvasImageSource & { width: number; height: number }, maxEdge: number): HTMLCanvasElement | null {
  const longEdge = Math.max(source.width, source.height);
  if (longEdge <= maxEdge) return null;
  const scale = maxEdge / longEdge;
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(source.width * scale));
  canvas.height = Math.max(1, Math.round(source.height * scale));
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;
  ctx.drawImage(source, 0, 0, canvas.width, canvas.height);
  return canvas;
}

/**
 * 把场景里所有贴图降到 `AVATAR_TEXTURE_MAX_EDGE` 以内（就地改 `texture.image` 并标脏）。
 *
 * 为什么在**上传前**做而不是改资产文件：资产是上游给的 Apache-2.0 示例模型，改文件会让
 * 校验与授权说明都对不上；而这套降采样对任何 VRM 都适用（真实形象也可能是 4K 贴图）。
 *
 * 跳过压缩纹理（`CompressedTexture` 的 image 是 mipmap 数组，没有 canvas 可画）与
 * video / cube 贴图 —— 它们要么已压好，要么画不了。
 */
/**
 * 把一个材质上所有挂着的贴图收集出来。
 *
 * **为什么不能只 `Object.values(material)`**：MToon 的贴图是原型上的 getter
 * （`map`、`shadeMultiplyTexture`、`emissiveMap`… 都是 `class MToonMaterial extends Material`
 * 里的 `get map() { return this._map }`），own enumerable 属性里一张都没有。
 * 2026-11-12 第一版就是这么写的，实测 `seen:0`，一张都没降，白改一版。
 *
 * 所以要沿原型链走 `Object.getOwnPropertyNames`，再用 `Reflect.get` 取值（getter 会被调用）。
 * 只取名字里带 map/Map/texture/Texture 的，避免把 boundingSphere 之类的对象也反射一遍。
 */
function collectMaterialTextures(material: object): Array<{ key: string; texture: { isTexture: boolean; image?: unknown; needsUpdate?: boolean; name?: string } }> {
  const found: Array<{ key: string; texture: { isTexture: boolean; image?: unknown; needsUpdate?: boolean; name?: string } }> = [];
  const visited = new Set<string>();
  let current: object | null = material;
  while (current && current !== Object.prototype) {
    for (const key of Object.getOwnPropertyNames(current)) {
      if (visited.has(key)) continue;
      visited.add(key);
      if (!/map|texture/i.test(key)) continue;
      let value: unknown;
      try {
        value = Reflect.get(material, key);
      } catch {
        continue; // getter 自己抛错（未初始化的扩展）就当没有
      }
      const texture = value as { isTexture?: boolean } | null;
      if (texture?.isTexture) {
        found.push({ key, texture: texture as { isTexture: boolean; image?: unknown; needsUpdate?: boolean; name?: string } });
      }
    }
    current = Object.getPrototypeOf(current) as object | null;
  }
  return found;
}

/**
 * 把场景里所有贴图降到 `AVATAR_TEXTURE_MAX_EDGE` 以内（就地改 `texture.image` 并标脏）。
 *
 * 为什么在**上传前**做而不是改资产文件：资产是上游给的 Apache-2.0 示例模型，改文件会让
 * 校验与授权说明都对不上；而这套降采样对任何 VRM 都适用（真实形象也可能是 4K 贴图）。
 *
 * 跳过压缩纹理（`CompressedTexture` 的 image 是 mipmap 数组，没有 canvas 可画）与
 * cube 贴图 —— 它们要么已压好，要么画不了。
 */
function capTextureResolution(root: Object3D, maxEdge: number): number {
  const seen = new Set<unknown>();
  let changed = 0;
  root.traverse((object) => {
    const materials = (object as { material?: unknown }).material;
    if (!materials) return;
    for (const material of Array.isArray(materials) ? materials : [materials]) {
      if (!material) continue;
      for (const { texture } of collectMaterialTextures(material as object)) {
        if (seen.has(texture)) continue;
        seen.add(texture);
        const image = texture.image as { width?: number; height?: number; isCubeTexture?: boolean } | undefined;
        if (!image?.width || !image.height || image.isCubeTexture) continue;
        try {
          const shrunk = shrinkTextureImage(image as CanvasImageSource & { width: number; height: number }, maxEdge);
          if (!shrunk) continue;
          texture.image = shrunk;
          texture.needsUpdate = true;
          changed += 1;
        } catch {
          /* 跨域图片画不进 canvas：保持原图，不影响加载成功 */
        }
      }
    }
  });
  return changed;
}

/**
 * 加载并后处理一个 VRM。失败/超时抛 `AvatarLoadError`（带 `reason`）。
 *
 * 登录前预热过的 URL 会直接命中解析缓存（`preloadAvatarModel`），此时几乎不花时间。
 *
 * 超时不取消底层传输：three 的 GLTFLoader 没有 abort 通道（FileLoader 不能取消），
 * 所以超时后只是不再等它；原 promise 仍可能稍后 reject，这里挂了空 catch 防
 * unhandledrejection（否则控制台会多一条与降级无关的红字）。
 */
export async function loadAvatar(
  url: string,
  options: LoadAvatarOptions = {},
): Promise<LoadedAvatar> {
  const warm = takePreloadedAvatar(url);
  if (warm) return warm;
  return loadAvatarUncached(url, options);
}

async function loadAvatarUncached(
  url: string,
  options: LoadAvatarOptions = {},
): Promise<LoadedAvatar> {
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const startedAt = performance.now();

  const loader = new GLTFLoader();
  loader.register((parser) => new VRMLoaderPlugin(parser));

  const gltf = await raceWithTimeout(loader, url, timeoutMs, (late) => {
    // 迟到的资产要按「它是不是 VRM」分别释放：VRM 还有 meta 上的缩略图要收
    const lateVrm = late.userData.vrm as VRM | undefined;
    if (lateVrm) disposeAvatar(lateVrm);
    else disposeScene(late.scene);
  });

  const vrm = gltf.userData.vrm as VRM | undefined;
  if (!vrm) {
    // 解析不出 VRM 的这棵树同样没人要了，不释放就是一次纯泄漏
    disposeScene(gltf.scene);
    throw new AvatarLoadError("no_vrm", "文件解析不出 VRM 数据（可能不是 VRM 资产）");
  }

  // 三连第 2、3 步：删掉无用顶点/关节，10MB 级模型可省一截显存与 draw call。
  // removeUnnecessaryJoints 在 three-vrm 3.5 已标 deprecated（推荐 combineSkeletons），
  // 但本轮与 playground 保持逐字一致，换 API 属单独一次改动，不夹带在接入里。
  VRMUtils.removeUnnecessaryVertices(gltf.scene);
  VRMUtils.removeUnnecessaryJoints(gltf.scene);
  // 贴图降采样：首帧那 1.6s 的大头在这里砍（把 22.3MP 的上传量降到 1/4）
  const shrunk = capTextureResolution(gltf.scene, AVATAR_TEXTURE_MAX_EDGE);
  if (shrunk > 0 && typeof console !== "undefined") {
    console.info(`[vrm] 贴图降采样 ${shrunk} 张 → 长边 ≤${AVATAR_TEXTURE_MAX_EDGE}px`);
  }
  // VRM0 面向 -Z，不转就背对镜头；VRM1 内部 no-op，可无条件调用
  VRMUtils.rotateVRM0(vrm);

  return { vrm, elapsedMs: performance.now() - startedAt };
}

/**
 * 加载一条 `.vrma` 待机动画（task-12 的三个 Apache-2.0 文件）。
 * 与角色分开：动画失败只让角色停在静息姿势，不该把整台 3D 拖进降级。
 */
export async function loadVrmAnimation(
  url: string,
  options: LoadAvatarOptions = {},
): Promise<LoadedAnimation> {
  const timeoutMs = options.timeoutMs ?? DEFAULT_ANIMATION_TIMEOUT_MS;
  const startedAt = performance.now();

  const loader = new GLTFLoader();
  loader.register((parser) => new VRMAnimationLoaderPlugin(parser));

  const gltf = await raceWithTimeout(loader, url, timeoutMs);

  const animation = (gltf.userData.vrmAnimations as VRMAnimation[] | undefined)?.[0];
  if (!animation) {
    throw new AvatarLoadError("no_animation", "文件里没有 VRM 动画轨道（可能不是 .vrma）");
  }

  return { animation, elapsedMs: performance.now() - startedAt };
}

/**
 * 载入 + 竞速超时（GLTFLoader 无 abort 通道，超时只是不再等）。
 *
 * P1-1：超时赢下竞速后，`loadPromise` 仍在后台跑完，**迟到成功必须有人接管释放**，
 * 否则 10.3MB 的 VRM（几何 + 纹理）没人 dispose，反复降级必 OOM。
 * `releaseLate` 由调用方给（VRM 要连 meta 缩略图一起放），缺省只 deepDispose 场景。
 */
async function raceWithTimeout(
  loader: GLTFLoader,
  url: string,
  timeoutMs: number,
  releaseLate?: (gltf: GltfRoot) => void,
): Promise<GltfRoot> {
  let timedOut = false;
  const loadPromise = loader.loadAsync(url);
  loadPromise
    .then(
      (gltf) => {
        if (!timedOut) return; // 正常路径：资产交给了上层，这里不碰
        if (releaseLate) releaseLate(gltf);
        else disposeScene(gltf.scene);
      },
      () => {
        /* 超时之后才失败的场景：错误已由下面的 race 分支归类，这里只防 unhandledrejection */
      },
    )
    .catch(() => {
      /* 释放本身失败也不能把降级路径炸掉 */
    });

  let timer = 0;
  const timeoutPromise = new Promise<never>((_resolve, reject) => {
    timer = window.setTimeout(() => {
      timedOut = true; // 先立旗，再拒绝：迟到的那次 then 靠它判断要不要释放
      reject(new AvatarLoadError("timeout", `加载超时（>${timeoutMs}ms）`));
    }, timeoutMs);
  });

  try {
    return await Promise.race([loadPromise, timeoutPromise]);
  } catch (error) {
    if (error instanceof AvatarLoadError) throw error;
    throw new AvatarLoadError(classify(error), describeFailure(error), error);
  } finally {
    window.clearTimeout(timer);
  }
}

/** 失败分类（粗分即可：diag 只需要知道「是超时、是 HTTP、还是不是 VRM」） */
function classify(error: unknown): AvatarLoadReason {
  if (error instanceof Error) {
    if (error.name === "HttpError") return "http";
    if (error.name === "AbortError") return "network";
    if (/^(TypeError|NetworkError)$/.test(error.name)) return "network";
    // three r180 的 FileLoader 改走 fetch，失败时抛的是**普通 Error**（不再是老的 HttpError 类），
    // message 形如 `fetch for "https://…" responded with 404: Not Found`。
    // 只按 name 判会把 404 归成 "unknown"，让 diag 丢掉最有用的那一维。
    if (/responded with \d{3}/i.test(error.message)) return "http";
    if (/\b[45]\d\d\b/.test(error.message)) return "http";
    if (/failed to fetch|networkerror|err_network|load failed/i.test(error.message)) return "network";
  }
  return "unknown";
}

/**
 * 失败描述：只产出一句不含 URL / header / token 的短码，供 client_diag 上报。
 * 三条来源实测：HttpError 的 message 带完整 URL（含 query），绝不能原样透出。
 */
function describeFailure(error: unknown): string {
  if (error instanceof Error) {
    const status = /\b([45]\d\d)\b/.exec(error.message)?.[1];
    if (status) return `HTTP ${status}`;
    if (error.name === "HttpError") return "HTTP 错误";
    if (error.name === "AbortError") return "请求被取消";
    if (error.name === "TypeError") return "网络不可达";
    if (error.name === "SyntaxError") return "资产格式不合法";
  }
  return "下载或解析失败";
}
