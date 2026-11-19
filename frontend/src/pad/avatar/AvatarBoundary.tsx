/**
 * 3D 舞台的错误边界（P2-8）。
 *
 * 为什么必须有：`AvatarStage` 是 `React.lazy` 加载的（three + three-vrm ≈750KB 独立 chunk）。
 * chunk 拉取失败——重启后旧 HTML 仍指向已删除的 hash 文件、弱网/断网、代理拦截——时
 * `lazy` 会**抛错**，而不是走 `AvatarStage` 内部的降级分支（那段代码压根没下载到）。
 * 没有边界时这个错会冒到根节点：整页白屏，状态栏、大字幕、按住说话全部消失。
 *
 * 于是这里只做一件事：把**3D 区**单独降级成静态头像，页面其余部分照常工作。
 * 注意本文件会被打进 pad 主 chunk（App.tsx 静态 import），**绝不能** import
 * `./AvatarStage` / `./VrmAvatar`（会把 three 拖回主包），所以下面那枚剪影是复制的小 SVG。
 */
import { Component, useState, type ErrorInfo, type ReactNode } from "react";
import { describeWebGLFailure, detectWebGL } from "./webgl";

/** 与 AvatarStage 容器同一套几何值（min(560px, 48vh)），失败时版面不跳 */
const STAGE_BOX = { height: "min(560px, 48vh)", width: "min(560px, 48vh)" } as const;

const STAGE_BOX_CLASS =
  "relative flex items-center justify-center overflow-hidden rounded-[2rem] border border-brand-line bg-brand-soft";

/**
 * 静态形象回退图。
 *
 * 2026-11-09 换图：原来是 `/avatars/fallback.png`（"数字人形象加载中"占位卡）——
 * 在没有 3D 能力的设备上，这张卡会**永远**停在"加载中"，看起来像卡死。
 * 现在换成用软件渲染（SwiftShader）把真实 VRM 渲出来的一帧静态形象
 * （`/avatars/static-portrait.png`，1032×1032），降级时长者看到的仍是"小满本人"。
 */
const DEFAULT_FALLBACK_SRC = "/avatars/static-portrait.png";

/** 不支持 3D 时的一句说明：不解释的话，静态图会被当成"卡住了" */
const STATIC_NOTE = "当前设备未启用 3D，已切换为静态形象";

export interface AvatarBoundaryProps {
  children: ReactNode;
  /** 静态头像；缺省用下面的小满静态图（`/avatars/static-portrait.png`） */
  fallbackSrc?: string;
}

interface AvatarBoundaryState {
  failed: boolean;
}

export class AvatarBoundary extends Component<AvatarBoundaryProps, AvatarBoundaryState> {
  state: AvatarBoundaryState = { failed: false };

  /**
   * 挂载前先问一次「这台设备能不能建 WebGL 上下文」。
   *
   * 2026-11-09 补：之前的降级只挂在「chunk 拉取失败 / 加载超时」上，而**创建不了
   * WebGL 上下文**的情况既不会抛到边界、也走不到加载分支，结果是舞台全空（用户
   * 看到"没有数字人"）。探测在 `./webgl` 里做了模块级缓存，这里只读结论。
   */
  private readonly webgl = detectWebGL();

  componentDidMount(): void {
    if (!this.webgl.ok) {
      // 只记一句可定位的原因；不打印 canvas/UA 等无关信息
      console.warn(
        "[avatar] 浏览器无法创建 WebGL 上下文，已直接降级为静态头像：",
        this.webgl.reason,
      );
    }
  }

  static getDerivedStateFromError(): AvatarBoundaryState {
    // 只要子树抛错（含 lazy 的 import 失败）就永久降级：3D 是增强，不值得让整页陪葬
    return { failed: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // 只留可定位的一句：chunk 失败时 message 形如
    // "Failed to fetch dynamically imported module: https://.../AvatarStage-xxx.js"，
    // 里面的 URL 含部署地址，先脱敏再落日志。
    console.error(
      "[avatar] 3D 舞台不可用，已降级为静态头像：",
      redact(error?.message ?? String(error)),
      info.componentStack ?? "",
    );
  }

  render(): ReactNode {
    if (this.state.failed || !this.webgl.ok) {
      const noWebgl = !this.webgl.ok;
      return (
        <AvatarStaticImage
          src={this.props.fallbackSrc ?? DEFAULT_FALLBACK_SRC}
          // chunk 失败（网络/发版）多半是"这次没加载到"，不必解释；没有 3D 能力才需要说明
          note={noWebgl ? STATIC_NOTE : undefined}
          // 没有 3D 能力时把原因与修复路径摊开在页面上：让用户能自己把 3D 打开，
          // 而不是对着照片猜「为什么别人的能动」
          hint={
            noWebgl
              ? { reason: describeWebGLFailure(this.webgl), detail: this.webgl.reason }
              : undefined
          }
        />
      );
    }
    return this.props.children;
  }
}

/** 静态回退：回退图也 404 时再退一层内置剪影 —— 无论哪一层都不留白屏 */
function AvatarStaticImage({
  src,
  note,
  hint,
}: {
  src: string;
  note?: string;
  hint?: { reason: string; detail: string };
}) {
  const [broken, setBroken] = useState(false);
  return (
    <div className="flex flex-col items-center gap-2">
      <div className={STAGE_BOX_CLASS} style={STAGE_BOX} role="img" aria-label="数字人">
        {broken ? (
          <svg
            viewBox="0 0 120 120"
            className="h-2/3 w-2/3 text-brand-line"
            aria-hidden="true"
            focusable="false"
          >
            <circle cx="60" cy="42" r="23" fill="currentColor" opacity="0.6" />
            <path d="M18 116c0-23 19-38 42-38s42 15 42 38z" fill="currentColor" opacity="0.45" />
          </svg>
        ) : (
          // avatar-breathe：极轻的呼吸/摆动（视觉上"画面是活的"），
          // 真正的 3D 动作与口型仍然只属于 WebGL 舞台
          <img
            src={src}
            alt="数字人头像"
            className="avatar-breathe h-full w-full object-cover"
            onError={() => setBroken(true)}
          />
        )}
        {note ? (
          // 长者端字号不缩水：text-lg（18px），底色带 85% 不透明以压住人像细节
          <p className="absolute inset-x-4 bottom-4 rounded-full bg-surface/85 px-4 py-1 text-center text-lg text-ink-muted">
            {note}
          </p>
        ) : null}
      </div>
      {hint ? (
        <details className="w-full text-left" style={{ maxWidth: STAGE_BOX.width }}>
          <summary className="cursor-pointer list-none text-center text-base text-ink-subtle">
            3D 未能启动 · 点这里看原因与打开方法
          </summary>
          <div className="mt-2 space-y-2 rounded-2xl bg-surface px-4 py-3 text-base leading-relaxed text-ink-muted">
            <p>{hint.reason}</p>
            <ol className="list-decimal space-y-1 pl-5">
              <li>
                地址栏打开 <code className="font-mono">chrome://gpu</code>，看 WebGL / WebGL2 两行：
                <span className="whitespace-nowrap">Hardware accelerated</span>
                才是能用硬件跑 3D。
              </li>
              <li>
                设置 → 系统 → 打开「使用硬件加速（如可用）」，重启浏览器后回到本页重试。
              </li>
              <li>
                远程桌面 / 虚拟机常常开不出硬件加速：可在{" "}
                <code className="font-mono">chrome://flags/#enable-unsafe-swiftshader</code>{" "}
                设为 Enabled 并重启浏览器——允许用 CPU 软件渲染，帧率低一些，但人物会真的动起来。
              </li>
            </ol>
            <div className="flex items-center justify-between gap-3 pt-1">
              <code className="truncate font-mono text-sm text-ink-subtle">{hint.detail}</code>
              <button
                type="button"
                className="shrink-0 rounded-full bg-brand px-4 py-1 text-base text-ink-inverse"
                onClick={() => window.location.reload()}
              >
                重新检测
              </button>
            </div>
          </div>
        </details>
      ) : null}
    </div>
  );
}

/** 脱敏：去掉 URL / Bearer / token=，压平空白并截断（口径与 AvatarStage.sanitize 一致） */
function redact(text: string): string {
  return text
    .replace(/https?:\/\/\S+/gi, "<url>")
    .replace(/Bearer\s+\S+/gi, "Bearer <redacted>")
    .replace(/token[=:]\s*\S+/gi, "token=<redacted>")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 200);
}
