/**
 * T-G 按住说话组件：WS/WebRTC 主链路不可用时的降级入口（C3 REST 整段上传）。
 * 按下开始攒帧（preRoll + 按住期间帧），松开组 WAV 上传；结果经回调渲染，全程不白屏。
 * 自持独立 MicCapture（主链路断开时其实例已停，无设备竞争；占用错误走既有话术映射）。
 *
 * 交互口径（2026-11-11 产品负责人实测后重做）：
 *  - **按下要有手感**：只有文案从「按住」变「松开」时，长者会以为没按上。现在按住即
 *    `scale-[0.97]` + 外环加粗，`duration-150`（≤200ms，不做长动画）。
 *  - **松开到出结果之间**显示「正在听您说…」，而不是把按钮灰掉——灰掉会被当成坏了。
 *  - 麦克风起不来（权限被拒 / 设备被占用）不是「不可用」，而是**换一条路**：
 *    按钮保持实底、文案改成「暂时听不了，按这里打字」，点击即把焦点交给打字入口。
 *  - **到点主动收口**：PRD FR-UI-01 允许单次 2 分钟。超过 2 分钟若仍继续攒帧，服务端会
 *    按超长整段拒收（413），长者说了半天只得到一句「说话有点长」——比提前停止更糟。
 *    所以到点先给出可读提示，再自动结束并上传已说的那一段。
 */
import { useEffect, useRef, useState } from "react";
import { FRAME_SAMPLES, MicCapture, PRE_ROLL_FRAMES, TARGET_RATE } from "../audio/capture";
import { sendUtterance } from "../rest-client";
import type { SubtitleSource } from "./BigSubtitle";

/** FR-UI-01：单次最长 120 秒。服务端 `MAX_AUDIO_SECONDS` 是同一口径的硬闸。 */
const MAX_TALK_MS = 120_000;
/** preRoll 会拼到句首（按下前 600ms），所以按住时长必须比上限短这一段 */
const PRE_ROLL_MS = Math.round((PRE_ROLL_FRAMES * FRAME_SAMPLES * 1000) / TARGET_RATE);
/** 再留一帧（60ms）余量：帧采样与墙钟有抖动，顶到 120 秒整会被服务端 `>` 判超长 */
const HOLD_LIMIT_MS = MAX_TALK_MS - PRE_ROLL_MS - 60;
const MAX_TALK_NOTICE = "已经说到最长 2 分钟，先帮您发出去";

interface Props {
  token: string;
  onSubtitle: (text: string, source: SubtitleSource) => void;
  onAuthFail: () => void;
  /** 麦克风起不来时点这里：把长者引到打字入口（由 App 注入，可缺省） */
  onAskForText?: () => void;
  /** 外层布局用（等宽两栏时传 `w-full`） */
  className?: string;
}

type TalkState = "idle" | "holding" | "busy" | "blocked";

const STATE_CLASS: Record<TalkState, string> = {
  // 按下态靠「缩小 + 外环」表达：大字场景里改底色不够显眼，缩放在余光里也能看见。
  idle: "bg-brand text-ink-inverse shadow-lg active:scale-[0.98] active:bg-brand-strong",
  holding: "scale-[0.97] bg-brand-strong text-ink-inverse shadow-inner ring-4 ring-brand/40",
  busy: "bg-brand-soft text-ink shadow-none",
  // 不灰、不半透明：留实底 + 描边，说明「按钮没坏，只是该换打字」
  blocked: "bg-surface-sunken text-ink ring-2 ring-line",
};

const STATE_TEXT: Record<TalkState, string> = {
  idle: "按住 说话",
  holding: "松开 结束",
  busy: "正在听您说…",
  blocked: "暂时听不了，按这里打字",
};

export function HoldToTalk({
  token,
  onSubtitle,
  onAuthFail,
  onAskForText,
  className = "",
}: Props) {
  const [holding, setHolding] = useState(false);
  const [busy, setBusy] = useState(false);
  const [blocked, setBlocked] = useState(false);
  const micRef = useRef<MicCapture | null>(null);
  const framesRef = useRef<Int16Array[]>([]);
  const startedAtRef = useRef(0);
  /** begin/end 竞态守卫：每次 begin 递增；start 返回后发现已被新按下/松开作废则不接管采集 */
  const seqRef = useRef(0);
  /** 是否正处于一次有效采集：自动收口后抬手的 pointerup 不应再上传第二遍 */
  const activeRef = useRef(false);
  const limitTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const state: TalkState = blocked ? "blocked" : busy ? "busy" : holding ? "holding" : "idle";

  function clearLimitTimer(): void {
    if (limitTimerRef.current !== null) {
      clearTimeout(limitTimerRef.current);
      limitTimerRef.current = null;
    }
  }

  // 卸载时清掉未到点的定时器：否则组件已消失还会回调 end()，把已废的采集再送一次
  useEffect(() => clearLimitTimer, []);

  async function begin(): Promise<void> {
    if (blocked) {
      onAskForText?.();
      return;
    }
    if (activeRef.current) return;
    const seq = ++seqRef.current;
    activeRef.current = true;
    framesRef.current = [];
    startedAtRef.current = Date.now();
    setHolding(true);
    onSubtitle("在听您说，说完请松开", "notice");
    clearLimitTimer();
    limitTimerRef.current = setTimeout(() => {
      limitTimerRef.current = null;
      // 先提示再收口：只停不出声，长者会以为按钮坏了（同「松开后要有可读状态」的口径）
      onSubtitle(MAX_TALK_NOTICE, "notice");
      void end();
    }, HOLD_LIMIT_MS);
    if (!micRef.current) micRef.current = new MicCapture();
    const mic = micRef.current;
    if (!mic.stream) {
      try {
        await mic.start();
      } catch (err) {
        onSubtitle(err instanceof Error ? err.message : "麦克风启动失败", "notice");
        activeRef.current = false;
        clearLimitTimer();
        setHolding(false);
        setBlocked(true);
        return;
      }
    }
    if (seqRef.current !== seq) {
      // 已被 end() 作废（快速点按）：释放本次启动的采集，避免悬挂
      await mic.stop();
      return;
    }
    mic.onFrame = (pcm) => framesRef.current.push(pcm);
  }

  async function end(): Promise<void> {
    if (!activeRef.current) return; // 自动收口已收过尾，抬手不再触发第二次上传
    activeRef.current = false;
    clearLimitTimer();
    seqRef.current++; // 作废任何在途的 begin()
    setHolding(false);
    const mic = micRef.current;
    if (!mic) return;
    mic.onFrame = null;
    // preRoll 补句首（600ms 环形缓冲），随后接按住期间整段
    const frames = [...mic.takePreRoll(), ...framesRef.current];
    const durationMs = Date.now() - startedAtRef.current;
    framesRef.current = [];
    if (frames.length === 0) {
      // 快速点击/无音频：明确提示而不是停在「在听您说」
      onSubtitle("没听清，请按住按钮再说话", "notice");
      return;
    }

    setBusy(true);
    onSubtitle("正在听您说…", "notice");
    const result = await sendUtterance(token, frames, durationMs);
    setBusy(false);

    if ("kind" in result) {
      if (result.kind === "auth") {
        onAuthFail();
        return;
      }
      onSubtitle(result.message ?? "语音服务暂时不可用，请稍后再试", "notice");
      return;
    }
    // 成功：识别文本 + 回复话术 + 可选服务端音频
    onSubtitle(result.text, "final");
    if (result.replyText) onSubtitle(result.replyText, "reply");
    if (result.replyAudioUrl) {
      const player = new Audio(new URL(result.replyAudioUrl, location.href).href);
      void player.play().catch(() => {
        onSubtitle("点击屏幕后开始播放语音", "notice");
      });
    }
  }

  return (
    <button
      type="button"
      data-testid="hold-to-talk"
      data-state={state}
      aria-pressed={holding}
      aria-label={STATE_TEXT[state]}
      disabled={busy}
      onClick={blocked ? () => onAskForText?.() : undefined}
      onPointerDown={blocked ? undefined : () => void begin()}
      onPointerUp={blocked ? undefined : () => void end()}
      onPointerLeave={() => holding && void end()}
      className={`min-h-[var(--pad-talk-h)] rounded-2xl px-[var(--pad-talk-px)] py-[var(--pad-talk-py)] text-[length:var(--pad-talk-text)] font-bold transition-[transform,background-color,box-shadow] duration-150 ease-out ${STATE_CLASS[state]} ${className}`}
    >
      {STATE_TEXT[state]}
    </button>
  );
}
