/**
 * 长者端戏曲 / 电影播放器（FR-CON-03 / FR-CON-04）。
 *
 * ## 本组件负责
 *
 * - 用原生 `<audio>` 放 `item.url`，`autoPlay` 一进来就响（面板总是由一次按钮点按打开，
 *   这次点按就是浏览器要求的那个用户手势）。
 * - 三种收尾各自记账：播完 → `finishMedia`（带真实完成度）；「不听了」→ `finishMedia`
 *   记中断；「换一段」→ `switchNextMedia`（服务端内部已把当前会话按打断收尾）。
 * - 卸载即断开音频：`pause()` + 摘 `src` + `load()`，否则面板关掉后长者端还在响。
 *
 * ## 本组件不负责
 *
 * - **怎么挑节目**：调用方用 `apiMedia.pickMedia` 挑好再把 `item` / `historyId` 传进来。
 * - **TTS 互斥 / 音频焦点**：让数字人先闭嘴是父组件的事，这里不抢也不让。
 * - **路由决策本身**：「大声点 / 小声点」是 pad 本地改 `volume`；规则层的「换一段 / 停」
 *   由父组件翻译成 `command` 下发（见 `MediaCommand`）。本组件不订阅 WS、不读路由决策，
 *   只执行「已经决定好的那一条命令」。
 * - **播放历史与偏好**：全在服务端，前端不缓存、不排序。
 *
 * ## 三个取舍（改之前先读）
 *
 * 1. **「换一段」自己连发两个请求**（`switch-next` → `play`），而不是回调父组件换 props：
 *    `switch-next` 的响应里没有 `history_id`，父组件拿到新 `item` 还得再 `play` 一次；
 *    两次都由本组件发，父组件只需要给一个初始 `item`。
 * 2. **「换一段」不再补 `finish`**：服务端 `switch_next` 内部会把当前未结束的会话按
 *    「被打断」收尾（`completion_rate = 0.0`，FR-CON-06 的偏好降权信号）。再补一次
 *    `finish` 会用实测进度**覆盖**掉那个 0.0，把「没听完就切了」记成「听了四成」，
 *    推荐排序就偏了。
 * 3. **上报不阻塞界面**：卧室平板弱网是常态，让长者盯着按钮等满 8 秒，比少记一条
 *    完成度糟得多。请求照发，界面先走；失败也不弹错——他已经在听下一段了。
 */

import { useEffect, useRef, useState } from "react";
import { finishMedia, playMedia, switchNextMedia, type MediaItem } from "../apiMedia";

export interface MediaPlayerProps {
  item: MediaItem;
  /** `play` 端点返回的 history_id：收尾上报的唯一凭据 */
  historyId: string;
  /** 这一段正常放完（或用户结束）后交回父组件 */
  onFinished: () => void;
  /** 「不听了」：已记中断，父组件可以把面板收起来 */
  onClose: () => void;
  /**
   * 父组件下发的播放指令（语音「换一段 / 停」走这里）。不传 = 只认屏幕上的按钮。
   *
   * 为什么用「序号 + 命令」而不是两个回调：命令来自 WS，同一条决策可能因为父组件
   * 重渲染而重复出现；用递增 `seq` 去重，组件才不会把一句话执行两遍。
   */
  command?: MediaCommand | null;
  /**
   * 播放音量 0–1，由父组件受控（FR-DLG-02「大声点 / 小声点」）。
   *
   * 为什么不给 1.0：平板扬声器就在床头，满量在耳边很吵，实测 0.9 已经够响；
   * 留一点余量也让「大声点」按一次就有可感知的变化，而不是第一次按了没反应。
   */
  volume?: number;
  /**
   * 音量变化回调（传了才显示「大声点 / 小声点」）。
   *
   * 长者端不放滑杆：拖动是精细动作，卧室里手抖按不准；两个大按钮一次走一档更稳。
   */
  onVolumeChange?: (volume: number) => void;
}

/** 一次「大声点 / 小声点」走 0.15：约十档，按三五次就能听出差别。 */
const VOLUME_STEP = 0.15;

/**
 * 一条播放命令。`seq` 必须**严格递增**（从 1 开始），父组件每发一次就 +1；
 * 组件只执行序号大于上一次的那条，所以 `seq` 不变的重渲染是安全的。
 *
 * `pause` / `resume`（FR-CON-03 后半，2026-11-12）：暂停时把 `currentTime` 记进
 * `pausedAtRef`，继续说「继续」就 seek 回原位接着放。二者都是**组件内动作**：
 * 不调 HTTP、不新建 `media_history`、pause 也不做收尾（长者随时可能接着说继续）。
 */
export interface MediaCommand {
  type: "next" | "stop" | "pause" | "resume";
  seq: number;
}

/** 放不出来时的一句话。不说「解码失败」，长者听不懂也不该操心。 */
const BROKEN_TEXT = "这段放不出来，我再给您换一段";

/**
 * 把 `<audio>` 的秒数换算成服务端要的毫秒。
 *
 * 量不到就整段不传（服务端落 NULL）：流式素材的 `duration` 是 `Infinity`，
 * 直接乘 1000 会变成 `null` 进 JSON，被 `gt=0` 判 422。
 */
function readProgress(el: HTMLAudioElement | null): {
  progress_ms?: number;
  duration_ms?: number;
} {
  if (!el) return {};
  const out: { progress_ms?: number; duration_ms?: number } = {};
  const { currentTime, duration } = el;
  if (Number.isFinite(currentTime) && currentTime >= 0) {
    out.progress_ms = Math.round(currentTime * 1000);
  }
  if (Number.isFinite(duration) && duration > 0) {
    out.duration_ms = Math.round(duration * 1000);
  }
  return out;
}

export function MediaPlayer({
  item,
  historyId,
  onFinished,
  onClose,
  command = null,
  volume = 0.9,
  onVolumeChange,
}: MediaPlayerProps) {
  /**
   * 当前真正在放的那一段。它与 props 分开，是因为「换一段」会在本组件内部
   * 换掉节目（见文件头取舍 1），父组件的 `item` 只代表「初始那一段」。
   */
  const [playing, setPlaying] = useState<{ item: MediaItem; historyId: string }>({
    item,
    historyId,
  });
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState<"next" | "stop" | null>(null);
  /**
   * 浏览器把 `play()` 拒了，需要长者再按一下才能出声。
   *
   * 为什么必须单独一态：`<audio autoPlay>` 被自动播放策略拦下时**不触发 `onError`**，
   * 面板会停在「正在放：某某」而一点声音都没有——长者只会以为平板坏了。
   * 面板本身虽由一次按钮点按打开（通常满足手势要求），但换段是**组件内部**
   * 换 `src`，新的一段仍然是 programmatic play，仍可能被拒。
   */
  const [needsTap, setNeedsTap] = useState(false);

  const audioRef = useRef<HTMLAudioElement | null>(null);
  /** 同一条 history 只报一次：`ended` 与「不听了」可能前后脚都触发 */
  const reportedRef = useRef(false);
  /** 这条 history 是否已由服务端（`switch-next`）收尾，已收尾就不再补 `finish` */
  const closedRef = useRef(false);
  /** 卸载后不再 `setState`、不再回调父组件 */
  const goneRef = useRef(false);
  /** 长者按了「不听了」：换段请求可能还在路上，回来时不许再换台 */
  const stoppedRef = useRef(false);
  /**
   * 当前这条历史的 id。卸载时要靠它补报，而卸载的 cleanup 是挂载时那次渲染的闭包，
   * 直接读 `historyId` 会拿到「一开始那一条」，换过段之后就是错的。
   */
  const activeHistoryRef = useRef(historyId);
  /** 已经执行到第几号命令：`seq` 不涨就不重复执行 */
  const lastSeqRef = useRef(0);
  /**
   * 「暂停住的那一秒」（元素 `currentTime`，单位秒）；`null` = 当前没暂停。
   *
   * 为什么必须用 ref 存、而不是读 `el.currentTime`：暂停后再读是准的，但
   * **resume 之前元素可能被换段（换 src）、被卸载重挂**，那时 `currentTime` 已经
   * 是 0 或新素材的位置。所以要的是「暂停那一刻的位置」这个快照。
   * `stop` / 换段时清空——下一段必须从头放，不能继承上一段的暂停点。
   */
  const pausedAtRef = useRef<number | null>(null);

  useEffect(() => {
    // 依赖只认身份（media_id + historyId），不认对象：父组件每次渲染都新建
    // `item` 字面量时，用 `[item, historyId]` 会让这个 effect 无限重跑。
    setPlaying({ item, historyId });
    setProblem(null);
    setBusy(null);
    activeHistoryRef.current = historyId;
    reportedRef.current = false;
    closedRef.current = false;
    stoppedRef.current = false;
    // 换了一段 = 新的播放位置：上一段的暂停点不能带过来（否则新段会从 aria 处开始）
    pausedAtRef.current = null;
  }, [item.media_id, historyId]);

  /**
   * 起播兜底：`autoPlay` 被拦时给长者一个能按的「开始播放」。
   *
   * 依赖认「哪一段」（media_id + historyId）而不是 `playing` 对象——每次渲染都是新对象。
   * `AbortError` 要放过：那是我们自己在换段/卸载时 `pause()` 造成的，不是策略拦截，
   * 把它当成「需要点一下」会让长者刚按下「换一段」就看到一句莫名其妙的话。
   */
  useEffect(() => {
    setNeedsTap(false);
    const el = audioRef.current;
    if (!el) return;
    let cancelled = false;
    void el.play().then(
      () => {
        if (!cancelled) setNeedsTap(false);
      },
      (error: unknown) => {
        if (cancelled || goneRef.current) return;
        if (error instanceof DOMException && error.name === "AbortError") return;
        setNeedsTap(true);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [playing.item.media_id, playing.historyId]);

  useEffect(() => {
    goneRef.current = false;
    return () => {
      goneRef.current = true;
      const el = audioRef.current;
      audioRef.current = null;
      if (!el) return;
      // 卸载 = 这一段到此为止。**必须补一次收尾**：面板被 SOS / 吃药回执这些更高
      // 优先的浮层挤掉时（见 `overlayPriority`），组件是被直接卸载的，长者没按任何键；
      // 不报的话这条历史会永远 `finished_at = NULL`，等他下次按「换一段」，服务端的
      // `_mark_interrupted()` 会把这一批悬挂记录一次性刷成 `completion_rate = 0.0`，
      // 推荐排序的输入就被这批「其实听了一半」的记录带偏了。
      const progress = readProgress(el);
      // `progress_ms` 为 0 / 量不到（元素还没真正开播就被卸载）就不报：那不算一次播放，
      // 报一条 0 秒历史只会给偏好统计添噪声。
      if (!reportedRef.current && !closedRef.current && progress.progress_ms) {
        reportedRef.current = true;
        void finishMedia(activeHistoryRef.current, progress);
      }
      // 先停再摘源：只 pause 的话缓冲流还在下载，宿舍网里会一直占带宽；
      // `load()` 让浏览器真正关掉媒体资源（否则长者端会带着后台声音）。
      el.pause();
      el.removeAttribute("src");
      el.load();
    };
  }, []);

  /**
   * 记一次收尾。重复调用只发一次请求，进度取**暂停前**那一刻。
   *
   * `closedRef` 是「这条历史服务端已经收过了」：`switch-next` 成功时服务端已把当前
   * 会话按打断写死 0.0，这时再发 `finish` 会用实测进度覆盖掉那个偏好信号，所以跳过。
   */
  function reportFinish(el: HTMLAudioElement | null, id: string): void {
    if (reportedRef.current || closedRef.current) return;
    reportedRef.current = true;
    void finishMedia(id, readProgress(el));
  }

  /** 换段失败时把当前这段放回去：长者按了「换一段」不该得到一片死寂。 */
  function resumeCurrent(): void {
    void audioRef.current?.play().catch(() => {
      // 播放被策略拦下就保持静默：界面还有「换一段 / 不听了」两个大按钮可走
    });
  }

  function handleEnded(): void {
    if (goneRef.current) return;
    reportFinish(audioRef.current, playing.historyId);
    // 完播的上报不 await：弱网里让长者对着放完的画面干等 8 秒更难受
    onFinished();
  }

  /**
   * 「暂停」：就地停住并**记下位置**（FR-CON-03 后半）。
   *
   * 三个刻意不做的事：
   * 1. **不 `reportFinish`**——暂停不是收尾。报了 `finished_at`，这条历史就关了，
   *    接着说「继续」也无处续（服务端那条记录已是过去式）；长者随时可能继续听。
   * 2. **不关面板**——关了就得重新点开、重新挑段，等于「不听了」。
   * 3. **不改 `busy`**——暂停是瞬时的，界面上不该出现「正在…」。
   */
  function handlePause(): void {
    if (goneRef.current) return;
    const el = audioRef.current;
    if (!el) return;
    el.pause();
    pausedAtRef.current = Number.isFinite(el.currentTime) ? el.currentTime : 0;
  }

  /**
   * 「继续」：seek 回暂停的位置再放（FR-CON-03 后半：**从原位置**播，不是从头）。
   *
   * 没有暂停点时也要合理：语音说「继续」而这段其实在播（或刚起播），直接 `play()`
   * 即可，不能把它 seek 到 0 把正在听的唱段倒回去。播放被策略拦下时置 `needsTap`，
   * 复用面板里那个「开始播放」按钮，不自己造第二套补救。
   */
  function handleResume(): void {
    if (goneRef.current) return;
    const el = audioRef.current;
    if (!el) return;
    const pausedAt = pausedAtRef.current;
    if (pausedAt !== null) {
      // 影视素材可能被重新编码/换源，超出时长就退回开头，别让 seek 卡成一片死寂
      const duration = el.duration;
      el.currentTime =
        Number.isFinite(duration) && duration > 0
          ? Math.min(pausedAt, Math.max(0, duration - 0.25))
          : pausedAt;
      pausedAtRef.current = null;
    }
    void el.play().catch((error: unknown) => {
      if (goneRef.current) return;
      if (error instanceof DOMException && error.name === "AbortError") return;
      setNeedsTap(true);
    });
  }

  function handleStop(): void {
    // 「换一段」在路上时也允许停：长者说了不听了就得马上停，不能等请求回来
    if (busy === "stop" || goneRef.current) return;
    stoppedRef.current = true;
    setBusy("stop");
    const el = audioRef.current;
    // 先暂停再读进度：否则读到的可能是「已经往下走了几帧」的位置
    el?.pause();
    // 「不听了」是收尾：暂停点作废，下次打开面板从新的一段开头放（不能续到旧位置）
    pausedAtRef.current = null;
    reportFinish(el, playing.historyId);
    onClose();
  }

  async function handleNext(): Promise<void> {
    if (busy !== null || goneRef.current) return;
    setBusy("next");
    setProblem(null);
    audioRef.current?.pause();
    // 换段 = 换播放位置：上一段的暂停点不能带到新一段上
    pausedAtRef.current = null;

    const next = await switchNextMedia({
      kind: playing.item.kind,
      genre: playing.item.genre ?? undefined,
    });
    if (goneRef.current || stoppedRef.current) return;
    if (!next.ok) {
      // 服务端没换成功（没下一段 / 版权 / 断网）：当前这条历史仍然开着，把声音放回去
      setBusy(null);
      setProblem(next.message);
      resumeCurrent();
      return;
    }
    // 服务端已经把这台设备上未结束的会话全部按打断收尾（含当前这条），到此不能再 finish
    closedRef.current = true;

    // `switch-next` 只挑节目、不建历史：要能收尾就必须再 open 一条 play
    const opened = await playMedia(next.data.item.media_id);
    if (goneRef.current || stoppedRef.current) return;
    if (!opened.ok) {
      // 新的一段没能建历史：保持安静，别再放旧的那段（那条记录已经关了，
      // 放下去也只会是一段「没人记账」的音频）。长者可以再按「换一段」。
      setBusy(null);
      setProblem(opened.message);
      return;
    }

    reportedRef.current = false;
    closedRef.current = false;
    activeHistoryRef.current = opened.data.history_id;
    setPlaying({ item: opened.data.item, historyId: opened.data.history_id });
    setBusy(null);
  }

  /**
   * 音量落到元素上。
   *
   * 不能写成 `<audio volume={...}>`：`volume` 是 `HTMLMediaElement` 的 **property**
   * 不是 attribute，React 不会把它当受控属性传下去（写了也一直是 1.0）。只能拿 ref 改。
   * 换段（`historyId` 变了）之后元素复用、属性不会自己回来，所以依赖里也带上它。
   */
  useEffect(() => {
    const el = audioRef.current;
    if (!el) return;
    el.volume = Math.min(1, Math.max(0, volume));
  }, [volume, playing.historyId]);

  // 父组件下发的命令（语音「换一段 / 停 / 暂停 / 继续」）。依赖只认 `seq` 与 `type`：
  // 父组件每次重渲染都会传一个新的 `command` 对象字面量，用对象做依赖会重复执行。
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (command === null || goneRef.current) return;
    if (command.seq <= lastSeqRef.current) return;
    lastSeqRef.current = command.seq;
    if (command.type === "next") {
      void handleNext();
    } else if (command.type === "pause") {
      handlePause();
    } else if (command.type === "resume") {
      handleResume();
    } else {
      // 必须走 handleStop 而不是让父组件直接卸载面板：卸载只停声音，
      // 而 handleStop 会先记一次中断收尾，账才是平的。
      handleStop();
    }
  }, [command?.seq, command?.type]);

  // 演员优先、其次剧种；都空就整行不渲染（`playing` 而不是 `item`：
  // 「换一段」之后这两行要跟着换，而 props 里还是初始那一段）
  const byline = playing.item.artist ?? playing.item.genre ?? "";
  const label = busy === "next" ? "正在换…" : "换一段";
  const stopLabel = busy === "stop" ? "正在关…" : "不听了";

  return (
    <section
      data-testid="media-player"
      className="flex w-full flex-col gap-6 rounded-2xl border border-line bg-surface p-8"
    >
      <div className="flex flex-col gap-2">
        {/* 一行大字说清「现在在放什么」：长者端不看小字、不猜进度 */}
        <p
          data-testid="media-player-now"
          className="font-bold leading-tight text-ink"
          style={{ fontSize: "var(--pad-drawer-title, 34px)" }}
        >
          正在放：{playing.item.title}
        </p>
        {byline !== "" && (
          <p data-testid="media-player-byline" className="text-2xl leading-snug text-ink-muted">
            {byline}
          </p>
        )}
      </div>

      {/* 错误行常驻（不报错时是空的）：不留位的话出错瞬间面板会长高，底下按钮跟着跳 */}
      <p role="status" className="min-h-8 text-2xl leading-snug text-danger-ink">
        {problem ?? ""}
      </p>

      {needsTap && (
        <button
          type="button"
          data-testid="media-player-tap-play"
          onClick={() => {
            const el = audioRef.current;
            if (!el) return;
            void el.play().then(
              () => setNeedsTap(false),
              () => setNeedsTap(true), // 还是被拒就保留按钮，别把话说一半就撤掉
            );
          }}
          className="min-h-16 rounded-2xl border-2 border-brand bg-surface px-8 text-3xl font-bold text-brand-ink transition-colors duration-150 active:bg-surface-sunken"
        >
          按这儿开始放
        </button>
      )}

      <div className="flex flex-wrap items-center gap-4">
        <button
          type="button"
          data-testid="media-player-next"
          disabled={busy !== null}
          onClick={() => void handleNext()}
          className="min-h-16 min-w-56 rounded-2xl bg-brand px-8 text-3xl font-bold text-ink-inverse transition-colors duration-150 active:bg-brand-strong disabled:bg-neutral-soft disabled:text-ink-subtle"
        >
          {label}
        </button>
        <button
          type="button"
          data-testid="media-player-stop"
          disabled={busy === "stop"}
          onClick={handleStop}
          className="min-h-16 min-w-56 rounded-2xl border-2 border-line bg-surface px-8 text-3xl font-bold text-ink transition-colors duration-150 active:bg-surface-sunken disabled:text-ink-subtle"
        >
          {stopLabel}
        </button>
      </div>

      {/* 「大声点 / 小声点」的屏幕版。语音那两条现在也有来路了：规则层判出
          `volume_up/volume_down` 后经 WS `media_control` 下发，App 直接改 `volume`。
          两条路都落在同一个 `volume` prop 上，所以长者用嘴调、用手按调是同一档。 */}
      {onVolumeChange && (
        <div className="flex flex-wrap items-center gap-4">
          <button
            type="button"
            data-testid="media-volume-down"
            onClick={() => onVolumeChange(Math.max(0, volume - VOLUME_STEP))}
            disabled={volume <= 0}
            className="min-h-14 min-w-40 rounded-2xl border-2 border-line bg-surface px-6 text-2xl font-bold text-ink transition-colors duration-150 active:bg-surface-sunken disabled:text-ink-subtle"
          >
            小声点
          </button>
          <button
            type="button"
            data-testid="media-volume-up"
            onClick={() => onVolumeChange(Math.min(1, volume + VOLUME_STEP))}
            disabled={volume >= 1}
            className="min-h-14 min-w-40 rounded-2xl border-2 border-line bg-surface px-6 text-2xl font-bold text-ink transition-colors duration-150 active:bg-surface-sunken disabled:text-ink-subtle"
          >
            大声点
          </button>
        </div>
      )}

      {/* 音频元素本身不显示：长者端不留一个小黑条占位，全部信息走上面两行大字 */}
      <audio
        data-testid="media-player-audio"
        ref={audioRef}
        src={playing.item.url}
        autoPlay
        className="hidden"
        onEnded={handleEnded}
        onError={() => {
          if (goneRef.current) return;
          // 绝不静默：素材 404 / 编码不支持时浏览器什么都不显示，界面必须自己说话
          setProblem(BROKEN_TEXT);
        }}
      />
    </section>
  );
}
