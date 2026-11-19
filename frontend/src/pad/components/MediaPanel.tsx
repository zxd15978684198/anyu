/**
 * 戏曲/电影面板（FR-CON-03 / FR-CON-04）——把「挑一段」和「放」串起来的那一层。
 *
 * 分工：
 * - `../apiMedia` 只发请求、只回 `{ok:true,data}` / `{ok:false,message}`，永不抛；
 * - `./MediaPlayer` 只管 `<audio>` 与三种收尾（播完 / 不听了 / 换一段）；
 * - 本组件负责**进门那一步**：先 `pick` 挑节目，再 `play` 开一条历史，两条都成了才渲染播放器。
 *
 * ## 为什么必须两步（pick → play）
 * `pick` 只挑「该放哪一段」（去重最近 5 条、版权白名单都在服务端），它**不建播放历史**；
 * `play` 才建历史并返回 `history_id`。少了 `play`，播放器放完也没地方记账，
 * 完成度与偏好降权全丢——而这正是 FR-CON-06 推荐排序的输入。
 *
 * ## 为什么刷新按钮不是「重新 pick 同一个」
 * `pick` 每次都按服务端当前状态重挑，第二次点进来挑到的通常就是下一段。
 * 刷新按钮改的是 `attempt` 计数，只用来重新触发取数，不去缓存上一次的响应——
 * 缓存一份旧节目再放，等于绕过服务端的去重。
 *
 * ## 不做什么
 * - 不订阅 WS、不读路由决策：「大声点 / 停」由 App 分发（见文件头 `apiMedia` 的口径）。
 * - 不让数字人闭嘴：与 TTS 的互斥是父组件的事，本组件只报 `onSubtitle` 给父组件一句提示。
 * - 不传 `elder_id`：设备令牌决定身份，服务端从 `devices.elder_id` 取（AGENTS §4.9）。
 */
import { useEffect, useState } from "react";
import { pickMedia, playMedia, type MediaItem, type MediaKind } from "../apiMedia";
import { MediaPlayer } from "./MediaPlayer";
import type { MediaCommand } from "./MediaPlayer";

export interface MediaPanelProps {
  /** 默认 `opera`：长者端主入口是「听一段戏」，电影走 kind="movie" 的另一个入口。 */
  kind?: MediaKind;
  /**
   * 指定剧种（「放段越剧」语音命中规则层时由服务端下发）。
   *
   * 为什么剧种是「建议」而不是「必须」：`media_library` 里可能只登记了京剧，
   * 或者该剧种都被排除完了。传下去后由 `pickMedia` 的 SQL 过滤决定有没有，
   * 取不到就如实报错（不在这里兜底成别的剧种，那等于骗长者「给您放越剧」）。
   */
  genre?: string;
  /** 关掉面板（「不听了」也会走这里） */
  onClose: () => void;
  /** 给父组件一句可显示的话（进字幕/提示条），本组件不自己写字幕。 */
  onSubtitle?: (text: string) => void;
  /** 父组件下发的播放命令（语音「换一段 / 停」），原样透传给播放器 */
  command?: MediaCommand | null;
  /** 播放音量 0–1，由父组件持有（「大声点 / 小声点」） */
  volume?: number;
  /**
   * 音量变化回调。传了才显示「大声点 / 小声点」两个按钮。
   *
   * 为什么按钮放在面板而不是播放器：音量该由持有音频会话的那一层统一管
   * （戏曲、TTS、提示音共用一只「响亮程度」），播放器只负责把它落到 `<audio>` 上。
   */
  onVolumeChange?: (volume: number) => void;
}


type Phase =
  | { status: "loading" }
  | { status: "playing"; item: MediaItem; historyId: string }
  | { status: "error"; message: string };

const PICKING_TEXT = "正在给您挑一段…";

export function MediaPanel({
  kind = "opera",
  genre = "",
  onClose,
  onSubtitle,
  command = null,
  volume = 0.9,
  onVolumeChange,
}: MediaPanelProps) {
  const [phase, setPhase] = useState<Phase>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let gone = false;
    setPhase({ status: "loading" });

    async function open(): Promise<void> {
      const picked = await pickMedia(genre ? { kind, genre } : { kind });
      if (gone) return;
      if (!picked.ok) {
        setPhase({ status: "error", message: picked.message });
        return;
      }
      // `pick` 不建历史，必须再 `play` 一次才能拿到 `history_id`
      const opened = await playMedia(picked.data.item.media_id);
      if (gone) return;
      if (!opened.ok) {
        setPhase({ status: "error", message: opened.message });
        return;
      }
      setPhase({
        status: "playing",
        item: opened.data.item,
        historyId: opened.data.history_id,
      });
      onSubtitle?.(`给您放一段：${opened.data.item.title}`);
    }

    void open();
    return () => {
      gone = true;
    };
    // `onSubtitle` 每次渲染都是新函数，依赖它会无限重跑取数；只认 kind/genre 与重试计数。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind, genre, attempt]);

  if (phase.status === "playing") {
    return (
      <MediaPlayer
        item={phase.item}
        historyId={phase.historyId}
        onFinished={onClose}
        onClose={onClose}
        command={command}
        volume={volume}
        onVolumeChange={onVolumeChange}
      />
    );
  }

  return (
    <section
      data-testid="media-panel"
      className="flex w-full flex-col gap-6 rounded-2xl border border-line bg-surface p-8"
    >
      <p
        className="font-bold leading-tight text-ink"
        style={{ fontSize: "var(--pad-drawer-title, 34px)" }}
      >
        {phase.status === "error" ? "这段没放成" : PICKING_TEXT}
      </p>
      {/* 错误行常驻，避免面板在状态切换时长高、按钮跳位（与 MediaPlayer 同口径） */}
      <p role="status" className="min-h-8 text-2xl leading-snug text-danger-ink">
        {phase.status === "error" ? phase.message : ""}
      </p>
      <div className="flex flex-wrap items-center gap-4">
        {phase.status === "error" && (
          <button
            type="button"
            data-testid="media-panel-retry"
            onClick={() => setAttempt((n) => n + 1)}
            className="min-h-16 min-w-56 rounded-2xl bg-brand px-8 text-3xl font-bold text-ink-inverse transition-colors duration-150 active:bg-brand-strong"
          >
            再试一次
          </button>
        )}
        <button
          type="button"
          data-testid="media-panel-close"
          onClick={onClose}
          className="min-h-16 min-w-56 rounded-2xl border-2 border-line bg-surface px-8 text-3xl font-bold text-ink transition-colors duration-150 active:bg-surface-sunken"
        >
          {phase.status === "error" ? "先不听了" : "取消"}
        </button>
      </div>
    </section>
  );
}
