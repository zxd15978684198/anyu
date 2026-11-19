/**
 * 常驻内联提示原语（前端计划 W4）。
 *
 * ## 为什么不是 Toast
 * 计划初稿写「11 处 note-banner 统一到 Toast」，**这条前提错了**：
 * 实读后发现它们多数是**常驻说明**而非转瞬反馈——
 * 「这里维护的是全院共享的机构口径…」（KnowledgePage）、
 * 「统计概览只对管理员开放（服务端 403）」（HomePage）、
 * 各页的 `scopeNote` 权限范围说明。
 * 把这些改成 2 秒自动消失的 Toast，护工就再也看不到权限与口径说明了，是**功能退化**。
 *
 * 所以这里做的是「**一个组件、统一语义与样式、不改行为**」：
 * 常驻的继续常驻，操作反馈（`notice` state）也继续留在原位直到下次操作，
 * 只是不再各页手写 `<p className="note-banner">`。
 * 真正需要自动消失的场景（如门户的复制成功）另用 Toast，两者不混。
 *
 * 色板：只引用 tokens.css 既有语义色，不新造色相。
 */
import type { ReactNode } from "react";
import type { Tone } from "../tone";

export interface NoticeProps {
  children: ReactNode;
  /** 默认 warning（沿用原 .note-banner 的琥珀色）；权限/口径说明可用 info。 */
  tone?: Tone;
  /**
   * 操作反馈用 `status`（读屏会播报一次）；纯说明用 `note`。
   * 原代码里两种混用，这里显式区分。
   */
  role?: "status" | "note";
  className?: string;
  style?: React.CSSProperties;
}

export function Notice({
  children,
  tone = "warning",
  role = "note",
  className,
  style,
}: NoticeProps) {
  return (
    <p
      className={className ? `ui-notice ${className}` : "ui-notice"}
      data-tone={tone}
      role={role}
      style={style}
    >
      {children}
    </p>
  );
}