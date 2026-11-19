/**
 * 长者端 3D 舞台（FR-3D-01/02、AC-15）。
 *
 * 一条铁律：**任何情况下都不白屏**。三条路径都落到同一个容器里——
 *   ① 正常：`avatar-config` → 5s 内加载出 VRM → 显示 canvas
 *   ② 降级：加载失败 / 超 5s / 没配形象 → 显示小满的静态形象（`fallback_image_url` 优先，
 *      未配置时用仓库自带的 `/avatars/static-portrait.png`）
 *   ③ 兜底：静态图也 404 → 显示内置剪影，容器底色始终是 `brand-soft`
 *
 * `rotation_y_offset` 只作为「VRM1 额外补偿」透传给 VrmAvatar；VRM0 的 π 由 rotateVRM0
 * 在前端完成（C7：后端零 3D 逻辑）。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { loadSession } from "../session";
import {
  AvatarLoadError,
  DEFAULT_AVATAR_URL,
  buildSceneAnimationUrls,
  loadAvatarCatalog,
  pickAvatarUrl,
  preferLightAvatar,
  type AvatarCatalog,
} from "./loadAvatar";
import { AVATAR_BOX_CLASS } from "./avatarBox";
import { createAvatar, type AvatarController } from "./VrmAvatar";

/** C7 `GET /api/pad/avatar-config` 的 200 四键（app/api/pad.py:42-52） */
interface AvatarConfig {
  avatar_url: string | null;
  fallback_image_url: string | null;
  rotation_y_offset: number;
  version: number;
}

/** C5 `client_diag`：与 protocol.ts 的 ClientDiagMsg 结构一致（此处本地声明，避免与
 *  正在改 protocol.ts 的卡产生构建耦合）。
 *  `stage` 只有两档：`vrm_load` 报 3D 链路结果，`avatar_ready` 报「屏幕上已有形象」。 */
export interface AvatarDiag {
  type: "client_diag";
  stage: "vrm_load" | "avatar_ready";
  ok: boolean;
  error: string;
}

/**
 * 兜底形象固定为「小满」（2026-11-17 产品负责人：「数字人兜底形象就设置成小满」）。
 *
 * 小满的 3D 资产就是 `DEFAULT_AVATAR_URL`（`/avatars/vrm1_sample.vrm`，与
 * `scripts/persona_catalog.py` 里小满的 `avatar_url` 同一个文件）；静态形象是
 * 用软件渲染真 VRM 出的一帧。没有分配人设、配置取不到、3D 失败，都落到这一对资产上，
 * **不再**先选 `catalog.json` 的第一个形象（那是丽莎，长者会看到「换了个人」）。
 */
const DEFAULT_FALLBACK_IMAGE_URL = "/avatars/static-portrait.png";

export interface AvatarStageProps {
  /**
   * 诊断上报出口。
   * TODO(Lead 接线)：`RealtimeClient` 目前只有 private 的 `reportDiag`（realtime.ts:265），
   * 待其暴露 public `sendClientDiag` 后，在 App.tsx 把 `<AvatarStage />` 改成
   * `<AvatarStage onDiag={(d) => clientRef.current?.sendClientDiag(d)} />` 即可，本组件不用再动。
   */
  onDiag?: (diag: AvatarDiag) => void;
  /**
   * 3D 就绪后交出控制器（`setSpeaking` / `nod` / `setMouthOpen` / `getState`）；
   * 降级、卸载、以及 StrictMode 的双挂载都会以 `null` 回调，保证上层不会握着已释放的句柄。
   * 迭代 5 接 TTS 状态机时从这里往上接，本组件不用再改。
   */
  onReady?: (controller: AvatarController | null) => void;
}

/** 配置请求单独限时：整条链路要守住「5s 内出画面或降级」，不能把预算全花在配置上 */
const CONFIG_TIMEOUT_MS = 2000;
/** PRD FR-3D-02 */
const LOAD_TIMEOUT_MS = 5000;
/** 配置慢掉一部分预算后，模型至少还留这么多时间，否则直接降级不值得再抢 */
const MIN_LOAD_BUDGET_MS = 1500;



type ConfigResult =
  | { ok: true; config: AvatarConfig }
  | { ok: false; reason: string };

type Stage =
  | { kind: "loading" }
  | { kind: "vrm" }
  | { kind: "fallback"; src: string | null };

export function AvatarStage({ onDiag, onReady }: AvatarStageProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [stage, setStage] = useState<Stage>({ kind: "loading" });
  const [imageBroken, setImageBroken] = useState(false);

  // onDiag / onReady 走 ref：调用方传内联箭头函数时不会把整条加载链路重启一次
  const diagRef = useRef(onDiag);
  const readyRef = useRef(onReady);
  useEffect(() => {
    diagRef.current = onDiag;
    readyRef.current = onReady;
  });

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    let alive = true;
    let handle: AvatarController | null = null;
    /** 首帧是否真的画出来了：`ok:true` 与 contextlost 的处置都以它为准 */
    let firstFrameSeen = false;
    const controller = new AbortController();
    const startedAt = performance.now();

    const emit = (ok: boolean, error = "") => {
      diagRef.current?.({
        type: "client_diag",
        stage: "vrm_load",
        ok,
        error: ok ? "" : sanitize(error),
      });
      if (import.meta.env.DEV) {
        const cost = Math.round(performance.now() - startedAt);
        console.info(`[vrm_load] ok=${ok}${ok ? "" : ` reason=${error}`} ${cost}ms`);
      }
    };

    /**
     * 首帧耗时（挂载 → 画面上真的有人）打到 window 上，给「一打开页面就有人」这类
     * 观感问题留可复现的数字。生产环境只写一个对象，不产生日志噪音。
     */
    const publishTtfr = (firstFrameMs: number) => {
      (globalThis as unknown as Record<string, unknown>).__padAvatarTtfr = {
        mountToFirstFrameMs: Math.round(performance.now() - startedAt),
        firstFrameMs: Math.round(firstFrameMs),
      };
    };

    void (async () => {
      // 目录与配置并行拉：`catalog.json` 是 W1 的形象/动作来源（8 形象 + 6 动作），
      // `avatar-config` 仍是「这台设备该看谁」。两者各自限时，谁也不许把 5s 预算吃光。
      const [result, catalog] = await Promise.all([
        fetchAvatarConfig(controller.signal),
        loadAvatarCatalog().catch(() => null as AvatarCatalog | null),
      ]);
      if (!alive) return;

      // 未登录（还在欢迎页）时 `avatar-config` 必然 no_session：**不能因此不发请求/不建模型**。
      // 产品负责人要求「进入页面数字人就加载好」，所以这里先按兜底形象（小满）把 3D 拉起来。
      const config: AvatarConfig =
        result.ok
          ? result.config
          : {
              avatar_url: DEFAULT_AVATAR_URL,
              rotation_y_offset: 0,
              fallback_image_url: DEFAULT_FALLBACK_IMAGE_URL,
              version: 0,
            };
      if (!result.ok && result.reason !== "no_session") {
        // 已登录却取不到配置：配置这条路断了，直接落到小满的静态形象——兜底形象是产品口径，
        // 不是「拿内置形象冒充服务端配置」。
        setStage({ kind: "fallback", src: DEFAULT_FALLBACK_IMAGE_URL });
        emit(false, result.reason);
        return;
      }

      // 候选链：服务端配置 → **小满兜底** → catalog 默认（省流量/窄屏优先 light）。
      // 兜底钉在 DEFAULT_AVATAR_URL（小满）：没有分配人设时要看到小满，而不是目录里的第一个
      // 形象（丽莎）。catalog 默认只在小满文件在本机缺失（新 clone 未跑同步脚本）时兜最后一层。
      const catalogDefault = pickAvatarUrl(catalog, { preferLight: preferLightAvatar() });
      const candidates = dedupeUrls([config.avatar_url, DEFAULT_AVATAR_URL, catalogDefault]);
      const fallbackSrc = config.fallback_image_url ?? DEFAULT_FALLBACK_IMAGE_URL;
      if (candidates.length === 0) {
        setStage({ kind: "fallback", src: fallbackSrc });
        emit(false, "avatar_url_empty");
        return;
      }

      // 动作按目录场景备齐（静息/倾听/说话/坐姿说话/点头/展示/才艺），
      // 缺哪条由 loadAvatar 的回退链补上，仍然缺就只是少一层表现。
      const sceneAnimationUrls = buildSceneAnimationUrls(catalog);
      const deadline = startedAt + LOAD_TIMEOUT_MS;
      let lastError: unknown = null;
      for (const candidate of candidates) {
        if (!alive) return;
        // 配置/目录已经花掉的时间从总预算里扣，保证「5s 内出画面或降级」
        const budget = Math.max(MIN_LOAD_BUDGET_MS, deadline - performance.now());
        try {
          const created = await createAvatar(canvas, {
            avatarUrl: candidate,
            rotationYOffset: config.rotation_y_offset,
            timeoutMs: budget,
            sceneAnimationUrls,
            // P1-2：只有**首帧真的渲染出来**才算 ok。渲染器建好 ≠ 有画面——着色器没编译、
            // 上下文丢了、rAF 被节流，都会让一块黑 canvas 骗过 diag，把「加载成功」报成服务端事实。
            onFirstFrame: (ms) => {
              if (!alive) return;
              firstFrameSeen = true;
              setStage({ kind: "vrm" });
              emit(true);
              publishTtfr(ms);
              if (import.meta.env.DEV) console.info(`[vrm] first frame ${Math.round(ms)}ms`);
            },
            // P1-2：上下文丢了（驱动复位 / GPU 进程崩溃 / 休眠唤醒）→ 立刻换回静态头像。
            // 实例本身不 dispose：浏览器可能马上恢复，恢复后原地接着画，不必重下 10MB。
            onContextLost: () => {
              if (!alive) return;
              setStage({ kind: "fallback", src: fallbackSrc });
              emit(false, "context_lost");
            },
            // 恢复：three 内部已重传资源，把 canvas 放回来即可；首帧还没来的话交给 onFirstFrame
            onContextRestored: () => {
              if (!alive || !firstFrameSeen) return;
              setStage({ kind: "vrm" });
              if (import.meta.env.DEV) console.info("[vrm] webgl context restored");
            },
          });
          // 卸载与 createAvatar 完成的竞态：这一帧大概率已经没人要了，直接释放
          if (!alive) {
            created.dispose();
            return;
          }
          handle = created;
          // 控制器先交出去（加载已成功、后续 API 都可用）；画面可见性与 diag 由首帧回调决定
          readyRef.current?.(created);
          return;
        } catch (error) {
          lastError = error;
          if (!alive) return;
          // 只有「这个文件确实不行」（404 / 不是 VRM）才值得换下一个候选；
          // 超时不重试——同一台弱网重下 10MB 只会把降级推迟到 10s 之后。
          const reason = error instanceof AvatarLoadError ? error.reason : "unknown";
          if (reason !== "http" && reason !== "no_vrm") break;
        }
      }
      if (!alive) return;
      setStage({ kind: "fallback", src: fallbackSrc });
      emit(false, reasonOf(lastError));
    })();

    return () => {
      alive = false;
      controller.abort();
      readyRef.current?.(null);
      handle?.dispose();
    };
  }, []);

  // 只有**终态降级**才出兜底形象（2026-11-11 产品负责人：加载期不要任何占位元素）。
  // `loading` 期间既没有 canvas 画面、也没有剪影，屏幕上就是干净的一片——
  // 这正是「进入页面即有人」的反面要求：宁可空，也不许先摆一个假的站在那里。
  const showFallback = stage.kind === "fallback";
  const fallbackSrc = stage.kind === "fallback" ? stage.src : null;

  /* 阶段迁移时间轴（排障用）：记下每一档是什么时刻进去的。
     2026-11-12 C 案：探针报「首帧 1615ms 但 canvas 到 4821ms 才 visible」，
     光看首帧时间对不上 DOM 的真实状态，必须有一份 stage 自己的时间线才能定位。 */
  useEffect(() => {
    const log = (globalThis as unknown as { __padStageLog?: { kind: string; at: number }[] })
      .__padStageLog ?? [];
    log.push({ kind: stage.kind, at: Math.round(performance.now()) });
    (globalThis as unknown as { __padStageLog?: { kind: string; at: number }[] }).__padStageLog = log;
  }, [stage.kind]);
  /**
   * 加载期就要有人（2026-11-11：「一打开页面就加载好数字人」）。
   *
   * 3D 首帧实测还要 1.7s（解析已挪到登录前，剩下的是纹理上传 + MToon 着色器编译），
   * 这段时间如果只放一个空盒子，长者看到的是「没人」。所以**加载期直接铺静态头像**，
   * VRM 首帧出来后再用 500ms 淡入盖上去 —— 人是一进页面就在的，只是先平后立。
   * `avatar-config` 没配 `fallback_image_url` 时用仓库自带的那张，别再退回剪影。
   */
  const posterSrc = fallbackSrc;

  /**
   * 屏幕上真的有人了 → 告诉服务端可以开口。
   *
   * 为什么不直接用 `vrm_load`：`vrm_load` 报的是 3D 链路（指标口径要干净，失败就是失败）。
   * 而长者端要的是「屏幕上有人再开口」，两者不是一件事：headless / 无 WebGL 的机器上
   * `vrm_load false` + 静态头像/剪影已在，服务端若只认 `vrm_load` 就会白等到上限。
   * 判定口径 = `stage` 进了终态（`vrm` 或 `fallback`），`loading` 期间一律不发。
   */
  const announcedReadyRef = useRef(false);
  const announceReady = useCallback(() => {
    if (announcedReadyRef.current) return;
    announcedReadyRef.current = true;
    diagRef.current?.({ type: "client_diag", stage: "avatar_ready", ok: true, error: "" });
  }, []);

  /* 降级画面（静态头像**或内置剪影**）出现就算「屏幕上已经有一个人」。
     内置剪影也要发：无 WebGL 的平板、没配 `fallback_image_url` 的现场都会走剪影分支，
     不发就是让服务端白等到上限、开口晚 5 秒（产品负责人 2026-11-10 要求「先出人再说话」）。
     **`loading` 不算**：那是「还没出人」，这时发就等于没闸门（实测 1.2s 就开口）。 */
  useEffect(() => {
    if (stage.kind === "vrm" || stage.kind === "fallback") announceReady();
  }, [stage.kind, announceReady]);

  return (
    <div
      className={AVATAR_BOX_CLASS}
      // 加载期整块不可见（2026-11-11 产品负责人）：不摆剪影、不摆占位框，屏幕上就是干净的一片。
      // 用 visibility 而不是卸载——canvas 必须保持挂载与尺寸，否则相机 aspect 会按 0 算错。
      style={{ visibility: stage.kind === "loading" ? "hidden" : "visible" }}
    >
      {/* canvas 始终占位：3D 就绪前用 opacity 隐藏，避免挂载期尺寸为 0 导致相机 aspect 算错 */}
      <canvas
        ref={canvasRef}
        role="img"
        aria-label="数字人"
        className="h-full w-full transition-opacity duration-500"
        style={{ opacity: stage.kind === "vrm" ? 1 : 0 }}
      />

      {showFallback && (
        <div className="absolute inset-0 flex items-center justify-center">
          {posterSrc && !imageBroken ? (
            <img
              src={posterSrc}
              alt="数字人头像"
              className="h-full w-full object-cover"
              // 解码成功 = 这张脸真的看得见了；只有**终态**（fallback）才允许服务端开口，
              // 加载期这张图只是占位，别让它替 3D 抢跑去开闸（见 announceReady 的 stage 判定）
              onLoad={announceReady}
              onError={() => setImageBroken(true)}
            />
          ) : (
            <Silhouette />
          )}
        </div>
      )}
    </div>
  );
}

/** 内置兜底形象：连静态头像都没有时的最后一层，保证是「有画面」而不是白屏 */
function Silhouette() {
  return (
    <svg
      viewBox="0 0 120 120"
      className="h-[88%] w-[88%] text-brand-line"
      aria-hidden="true"
      focusable="false"
    >
      <circle cx="60" cy="42" r="23" fill="currentColor" opacity="0.6" />
      <path d="M18 116c0-23 19-38 42-38s42 15 42 38z" fill="currentColor" opacity="0.45" />
    </svg>
  );
}

/** 取形象配置；任何失败都折叠成一个可上报的原因码，绝不抛给渲染树 */
async function fetchAvatarConfig(signal: AbortSignal): Promise<ConfigResult> {
  const token = loadSession()?.access_token;
  if (!token) return { ok: false, reason: "no_session" };

  const ctrl = new AbortController();
  const abort = () => ctrl.abort();
  signal.addEventListener("abort", abort, { once: true });
  const timer = window.setTimeout(() => ctrl.abort(), CONFIG_TIMEOUT_MS);
  try {
    const res = await fetch("/api/pad/avatar-config", {
      headers: { Authorization: `Bearer ${token}` },
      signal: ctrl.signal,
    });
    if (!res.ok) {
      // 403 device_not_bound / 401 会话过期都在这里落降级；后者由 App 的到期守护兜住
      return { ok: false, reason: `config_${res.status}` };
    }
    return { ok: true, config: (await res.json()) as AvatarConfig };
  } catch {
    return { ok: false, reason: ctrl.signal.aborted ? "config_timeout" : "config_network" };
  } finally {
    window.clearTimeout(timer);
    signal.removeEventListener("abort", abort);
  }
}

function reasonOf(error: unknown): string {
  if (error instanceof AvatarLoadError) {
    // http 带上状态码便于定位（describeFailure 已保证 message 里没有 URL）
    return error.reason === "http" ? error.message : error.reason;
  }
  return "unknown";
}

/** 候选链去重：空串/空值不占位，重复地址只留第一次出现（保持优先级） */
function dedupeUrls(urls: Array<string | null | undefined>): string[] {
  const seen = new Set<string>();
  const ordered: string[] = [];
  for (const url of urls) {
    if (!url || seen.has(url)) continue;
    seen.add(url);
    ordered.push(url);
  }
  return ordered;
}

/**
 * 脱敏：diag 会进服务端日志，URL（含 query）、token、换行一律不许透出；
 * 同时截断到 200 字，与 realtime.ts 的 reportDiag 口径一致。
 */
function sanitize(text: string): string {
  return text
    .replace(/https?:\/\/\S+/gi, "<url>")
    .replace(/Bearer\s+\S+/gi, "Bearer <redacted>")
    .replace(/token[=:]\s*\S+/gi, "token=<redacted>")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 200);
}
