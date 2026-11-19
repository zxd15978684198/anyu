import { useRef } from "react";
import type { ReactElement } from "react";
import {
  IDENTITY_VIEW,
  PLAYBACK_VIEW,
  connectionView,
  type BadgeTone,
  type ConnectionState,
  type IdentityState,
  type PlaybackState,
  type PillView,
} from "./statusViews";

/**
 * 长者端状态条（迭代 4 · W4；PRD line 571「状态条」）。
 *
 * 三枚胶囊：①连接（准备中 / 正在听 / 已断开 / 麦克风没声音）②身份（识别中 / 待确认 / 已确认）
 * ③播放（待机 / 正在说）。
 * 措辞于 2026-11-13 按长者视角重定（`已断开`/`待确认` 会被读成「出故障 / 要我做点什么」）：
 * 三行文案与「准备中」的判定都在 `statusViews.ts`（`connectionView()` / `IDENTITY_VIEW`），
 * 本组件只留一个动态部分——`useRef` 记住「曾经 listening 过」，再拿它去选键。
 * 契约由 `设计记录（身份闭环缺口补齐）` §3-W4 冻结（S-D4-4：先定契约后写），
 * 挂载在 `App.tsx` 顶栏（W1 完成迁移后，旧五态重载已删除）。
 *
 * **每枚必须自报家门**：显示 `标签 · 取值`（`连接 · 已断开`），而不是只显示取值。
 * 2026-11-10 产品负责人实测发现：只给取值时三枚胶囊（`已断开` `已确认` `待机`）看不出差别——
 * 两枚还都是灰底，「这枚管什么」全靠猜。取值表搬到 `statusViews.ts`，本组件只负责渲染。
 *
 * 颜色口径（`frontend/src/theme/tokens.css`、`StatusBadge.tsx` 顶部约定）：
 *  - 待确认（通用模式）= `warning`（琥珀）：身份未确认时要长者「多看一眼」，不是错误；
 *  - 已断开 = `neutral`（灰）：**红色只留给急症 / 红色事件 / 表单错误**，断开不占红；
 *  - 正在听 / 正在说 = `info`（进行中）、已确认 = `success`（正常）、待机 = `neutral`。
 *    颜色只做第三线索——文字标签与状态点始终在场，不靠颜色单独区分。
 *
 * 无障碍：外层 `role="status"` + `aria-live="polite"`；每枚胶囊 `role="group"` + `aria-label`
 * 说明「是什么状态 + 当前值」；状态点与分隔符 `aria-hidden`（纯装饰，不重复播报）。
 * 胶囊**不复用** `StatusBadge` 组件本体：它每枚自带 `role="status"`，三枚嵌进外层
 * `role="status"` 会形成嵌套 live region，读屏会重复播报。
 *
 * 长者端标准：正文 `text-2xl`（24px）≥ 20px，`min-h-14`（56px）与顶栏按钮同高、同基线；
 * 三枚间距 `gap-4`（16px）——原来 `gap-3` 只有 12px，三枚挤成一团，远看像一句话。
 * 本组件不可点，仍按 56px 留高对齐顶栏那一排按钮，右侧「唤醒词 / 用药提醒 / 一键求助」不跳行。
 *
 * 多端适配（2026-11-11）：上述三项不再写死，改成 `--pad-status-*` 令牌，由 index.css
 * 按断点给值（D 档与上面写的像素完全一致；P 档 20px/48px 正压两条下限，不再降）。
 * 三枚胶囊的**文案始终是「标签 · 取值」**，不因窄屏省成短词——11-10 实测过只给取值
 * 会分不清哪枚管什么，这条信息量不随屏幕变小。
 * 不加 `animate-pulse`：动效需要新的 `prefers-reduced-motion` 兜底类，而本波禁止新增 CSS 文件，
 * 静态状态点已足够。
 *
 * 返回类型用 `ReactElement` 而非 `JSX.Element`：@types/react 19 已删除全局 `JSX` 命名空间
 * （只剩 `React.JSX`），仓库既有代码（如 console 端）同样用 `ReactElement`。
 */

export type { ConnectionState, IdentityState, PlaybackState };

export interface StatusBarProps {
  connection: ConnectionState;
  identity: IdentityState;
  playback: PlaybackState;
}

/** 胶囊底/字色：与 StatusBadge 的信号色同源（100 级淡底 + 700 级深字） */
const TONE_STYLE: Record<BadgeTone, string> = {
  danger: "bg-danger-soft text-danger-ink",
  warning: "bg-warning-soft text-warning-ink",
  success: "bg-success-soft text-success-ink",
  info: "bg-info-soft text-info-ink",
  neutral: "bg-neutral-soft text-neutral-ink",
};

function Pill({ view }: { view: PillView }): ReactElement {
  return (
    <span
      role="group"
      aria-label={view.ariaLabel}
      title={view.ariaLabel}
      className={`inline-flex min-h-[var(--pad-status-h)] items-center gap-2 rounded-full px-[var(--pad-status-px)] py-2 text-[length:var(--pad-status-text)] font-semibold ${TONE_STYLE[view.tone]}`}
    >
      <span
        aria-hidden="true"
        className="h-2.5 w-2.5 shrink-0 rounded-full bg-current"
      />
      {/* 标签与取值同字号，标签靠透明度降权：长者端字号不降，两者仍是一句话 */}
      <span className="opacity-70">{view.name}</span>
      <span aria-hidden="true" className="opacity-40">
        ·
      </span>
      <span>{view.label}</span>
    </span>
  );
}

export default function StatusBar({
  connection,
  identity,
  playback,
}: StatusBarProps): ReactElement {
  // 记住「曾经连接成功过没有」：只在 listening 那一帧置位，之后掉线也不清空。
  // 不用 effect/定时器——这是渲染期的纯推导，ref 只是跨渲染保留一个事实。
  const hasConnectedBefore = useRef(false);
  if (connection === "listening") {
    hasConnectedBefore.current = true;
  }

  // 判定本身在 statusViews.connectionView：本组件只负责提供「曾经 listening 过」这个事实
  const connectionPill = connectionView(connection, hasConnectedBefore.current);

  return (
    <div
      role="status"
      aria-live="polite"
      className="inline-flex flex-wrap items-center gap-[var(--pad-status-gap)]"
    >
      <Pill view={connectionPill} />
      <Pill view={IDENTITY_VIEW[identity]} />
      <Pill view={PLAYBACK_VIEW[playback]} />
    </div>
  );
}
