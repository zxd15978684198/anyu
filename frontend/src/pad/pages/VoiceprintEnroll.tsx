import { useEffect, useRef, useState } from "react";
import { StatusBadge } from "../components/StatusBadge";
import { TaskShell } from "../components/TaskShell";
import {
  MAX_SAMPLE_SECONDS,
  VoiceSampler,
  deleteVoiceprints,
  getVoiceprintStatus,
  submitSample,
  type SampleIndex,
} from "../voiceprint";

/**
 * W1 声纹录入引导（PRD line 64 §1.5、FR-VPR-01；计划 adhoc-20261109 §3-W1）。
 *
 * 三段引导：①读一句话 ②数三个数 ③自由说一句。每段 ≤10s（到点自动停），
 * 单段失败/太短可原地重录，三段齐后回查 `GET /api/voiceprints/status` 确认
 * `active` 才显示「声纹已启用」。全程可「稍后再录」退出，绝不强制。
 *
 * 2026-11-11 产品负责人实测：「弹出框没有提示语」——打开只有录音按钮，长者不知道
 * 为什么录、录几段、每段说什么、失败了怎么办、录完剩几段。本轮补齐六类文案：
 *   ① 顶部一句为什么录；② 第 N 段 / 共 3 段；③ 录音中（含已录秒数）/ 识别中 /
 *   第 N 段已保存 / 失败按 code 分档；④ 隐私一句；⑤「稍后再录」常驻 + 顶栏可重开；
 *   ⑥ 已录段数用大号数字。正文一律 28px（长者端 FR-UI-02），按钮 ≥80px 高。
 *
 * 长者端口径（FR-UI-02）：大字、触控区 ≥48×48px、只用语义词 + tokens 颜色，
 * 不靠颜色区分状态；失败一律给「可以再试一次」的出口，不留死路。
 */
interface VoiceprintEnrollProps {
  /** 「稍后再说」/ 完成：收起本层（上层据此把入口按钮恢复/隐藏） */
  onClose: () => void;
  /** 三段齐且后端确认 active 时回调一次（上层可立即藏掉入口按钮） */
  onActivated?: () => void;
  /**
   * 长者点了「重新录一遍」并**删除成功**后回调一次：上层必须把 voiceprint 状态改回
   * 未注册，否则入口按钮仍然是「按 active 隐藏」的分支，人退出弹框就再也进不来。
   */
  onReset?: () => void;
  /** 续录场景：上层已查到的已录段（1/2/3 子集） */
  initialRegistered?: number[];
}

type Phase = "idle" | "recording" | "uploading" | "checking" | "done";

interface Segment {
  index: SampleIndex;
  title: string;
  /** 这一段的引导语：告诉长者现在要做什么（只上屏，不播报） */
  guide: string;
  demo: string;
  hint: string;
}

/** 三段固定话术：读起来越接近日常，声纹与真实使用越匹配（FR-VPR-01） */
const SEGMENTS: Segment[] = [
  {
    index: 1,
    title: "第一段 · 读一句话",
    guide: "请跟我说：",
    demo: "今天天气不错。",
    hint: "像平常说话一样，慢慢读一遍",
  },
  {
    index: 2,
    title: "第二段 · 数三个数",
    guide: "请跟我数：",
    demo: "一、二、三。",
    hint: "一个一个数，不要连得太快",
  },
  {
    index: 3,
    title: "第三段 · 随便说一句",
    guide: "请随便说一句您平时常说的话",
    demo: "比如：我想听一段越剧",
    hint: "说什么都行，像平时聊天一样",
  },
];

/** 视口级浮层的高度口径：整屏可用，不再按中部区/抽屉的 600px 封顶。
 *  1536×703（1920 屏 125% 缩放）下若沿用抽屉口径，正文区会被压到 0。
 *  上限 800px：大屏别把录入面板撑成整屏，矮屏则贴到视口边缘以内。 */
const SHELL_MAX_HEIGHT = "min(calc(100dvh - 32px), 800px)";

/** 后端错误码 → 长者能听懂的话（错误体见 app/api/voiceprints.py）。
 *  后端的 `detail` 是给排查看的（如「音频文件过小（疑似非法 WAV）」），不透给长者；
 *  也**不显示相似度**（声纹比对分数对长者没有意义，还会让人以为要「考及格」）。
 *  失败分三档说人话：声音太小 / 环境太吵 / 服务不可用。 */
function errorText(code: string | undefined): string {
  switch (code) {
    case "vp_extract_unavailable":
    case "vp_unavailable":
      // 本机声纹推理容器未起时就是这两条：不是长者的问题，如实说「稍后再试」
      return "声纹服务暂时不可用，请稍后再试";
    case "too_quiet":
      return "声音有点小，请靠近一点、大声一点再说一次";
    case "too_noisy":
      return "周围有点吵，请等安静一点再说一次";
    case "duplicate_sample":
      return "这一段已经录过了，请换下一段";
    case "device_unbound":
      return "这台设备还没绑定长者，请先完成配对";
    case "PAYLOAD_TOO_LARGE":
      return "录音有点长，请控制在 10 秒以内";
    case "VALIDATION_ERROR":
      return "这段没听清，可能是声音太小或周围太吵，请再说一次";
    case "network":
      return "网络连接失败，请检查网络后重试";
    case "no_session":
      return "登录已过期，请重新登录";
    default:
      return "这一段没存上，请再录一次";
  }
}

export function VoiceprintEnroll({
  onClose,
  onActivated,
  onReset,
  initialRegistered = [],
}: VoiceprintEnrollProps) {
  const [registered, setRegistered] = useState<number[]>(
    () => Array.from(new Set(initialRegistered)).sort(),
  );
  // 已经录满三段（status=active）时打开本层，必须直接落到 done：
  // 否则停在 idle 会出现「开始录音」，而后端对已有样本一律 409 duplicate_sample，
  // 长者点了只会收到「这一段已经录过了」——重录入口就藏在那个死循环后面。
  const [phase, setPhase] = useState<Phase>(() =>
    Array.from(new Set(initialRegistered)).length >= SEGMENTS.length ? "done" : "idle",
  );
  const [secondsLeft, setSecondsLeft] = useState(MAX_SAMPLE_SECONDS);
  const [error, setError] = useState("");
  /** 上一段刚存好时的回执（「第 2 段已保存」），下一次开录即清空 */
  const [notice, setNotice] = useState("");
  /** 二次确认：重录会删掉已注册的声纹，不能一点就删 */
  const [confirmingReset, setConfirmingReset] = useState(false);
  const [resetting, setResetting] = useState(false);

  const samplerRef = useRef<VoiceSampler | null>(null);
  /** 防重入（计时器到点与手动点「停止」可能同帧触发） */
  const busyRef = useRef(false);
  // 初值跟随 phase 的首帧值（useRef 只认第一次的实参）：录满三段直接进 done 时，
  // phaseRef 若还是 "idle"，`stopAndSubmit` 的重入与状态判断就会与 UI 不同步。
  const phaseRef = useRef<Phase>(phase);

  function setPhaseSafe(p: Phase) {
    phaseRef.current = p;
    setPhase(p);
  }

  /** 当前要录的段：第一个未录好的；全录好则停在第三段（此时应已 done） */
  const nextIndex = SEGMENTS.find((s) => !registered.includes(s.index))?.index;
  const segment: Segment =
    SEGMENTS.find((s) => s.index === (nextIndex ?? 3)) ?? SEGMENTS[SEGMENTS.length - 1];
  const targetIndex: SampleIndex = nextIndex ?? segment.index;

  useEffect(() => {
    return () => {
      // 卸载即释放麦克风：留下一个没关的 AudioContext 会让下次录入拿不到设备
      void samplerRef.current?.abort();
      samplerRef.current = null;
    };
  }, []);

  // 倒计时：只驱动 UI，真正的上限还有采集侧硬闸（VoiceSampler）
  useEffect(() => {
    if (phase !== "recording") return;
    const id = window.setInterval(() => {
      setSecondsLeft((s) => (s <= 1 ? 0 : s - 1));
    }, 1000);
    return () => window.clearInterval(id);
  }, [phase]);

  // 到点自动停：只在这里收口，避免「计时器里调异步函数」的时序坑
  useEffect(() => {
    if (phase === "recording" && secondsLeft === 0) void stopAndSubmit();
    // stopAndSubmit 每次渲染都是新的；本 effect 只关心「录制中且倒计时归零」这一刻
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase, secondsLeft]);

  async function startRecording() {
    if (busyRef.current) return;
    busyRef.current = true;
    setError("");
    setNotice("");
    const sampler = new VoiceSampler();
    try {
      await sampler.start();
    } catch (err) {
      busyRef.current = false;
      setError(err instanceof Error ? err.message : "麦克风启动失败，请检查后重试");
      return;
    }
    samplerRef.current = sampler;
    setSecondsLeft(MAX_SAMPLE_SECONDS);
    setPhaseSafe("recording");
    busyRef.current = false;
  }

  async function stopAndSubmit() {
    if (busyRef.current || phaseRef.current !== "recording") return;
    busyRef.current = true;
    const sampler = samplerRef.current;
    samplerRef.current = null;
    setPhaseSafe("uploading");

    let blob: Blob | null = null;
    try {
      blob = sampler ? await sampler.stop() : null;
    } catch {
      blob = null;
    }
    if (!blob) {
      busyRef.current = false;
      // 太短/没采到：长者最常见的两个原因是「声音太小」和「环境太吵」，一句说清
      setError("没听清：可能是声音太小，或周围有点吵，请靠近一点再说一次");
      setPhaseSafe("idle");
      return;
    }

    const target = targetIndex;
    const result = await submitSample(blob, target);
    if (!result.ok) {
      busyRef.current = false;
      setError(errorText(result.code));
      setPhaseSafe("idle");
      return;
    }

    const merged = Array.from(new Set([...registered, target])).sort();
    setRegistered(merged);
    busyRef.current = false;
    setNotice(
      merged.length >= SEGMENTS.length
        ? `第 ${target} 段已保存，三段都录好了`
        : `第 ${target} 段已保存，还剩 ${SEGMENTS.length - merged.length} 段`,
    );

    if (merged.length >= SEGMENTS.length) {
      await confirmActive();
      return;
    }
    setSecondsLeft(MAX_SAMPLE_SECONDS);
    setPhaseSafe("idle");
  }

  /** 三段齐后回查一次状态：只有后端说 active 才算启用（前端不自行宣布成功） */
  async function confirmActive() {
    setPhaseSafe("checking");
    const status = await getVoiceprintStatus();
    if (status === null) {
      setError("查不到声纹状态，请等网络恢复后再试一次");
      setPhaseSafe("idle");
      return;
    }
    if (status.status === "active") {
      setRegistered(status.registered.length > 0 ? status.registered : [1, 2, 3]);
      setPhaseSafe("done");
      onActivated?.();
      return;
    }
    setRegistered(status.registered);
    setError("还有一段没存好，请补录这一段");
    setPhaseSafe("idle");
  }

  async function handleClose() {
    busyRef.current = true;
    const sampler = samplerRef.current;
    samplerRef.current = null;
    await sampler?.abort();
    onClose();
  }

  /**
   * 重录：先删掉已注册的三段，再回到第一段。
   *
   * 为什么必须真的调后端：`status=active` 时后端会拒绝重复样本（`duplicate_sample`），
   * 前端只清本地状态是删不掉服务端那三行的，录进去只会一路 409。
   * 删除失败**不许**退回 idle：那样长者会以为可以重录，实际每一段都存不上去。
   */
  async function handleReset() {
    if (resetting) return;
    busyRef.current = true;
    setResetting(true);
    setError("");
    const result = await deleteVoiceprints();
    busyRef.current = false;
    setResetting(false);
    setConfirmingReset(false);
    if (!result.ok) {
      setError(errorText(result.code));
      return;
    }
    setRegistered([]);
    setNotice("已经清空，从第一段重新录");
    setPhaseSafe("idle");
    onReset?.();
  }

  const doneCount = registered.length;
  /** 录音中显示「已经录了几秒」：秒数从上限往下走，换成已录时长长者更好懂 */
  const elapsedSeconds = MAX_SAMPLE_SECONDS - secondsLeft;

  /** 状态行的四种说法（录音中 / 识别中 / 上一段已保存 / 失败原因） */
  const statusText =
    phase === "recording"
      ? `正在录音，已经录了 ${elapsedSeconds} 秒（最多 ${MAX_SAMPLE_SECONDS} 秒）`
      : phase === "uploading"
        ? "正在识别这一段的声纹，请稍等"
        : phase === "checking"
          ? "正在确认三段都录好了，请稍等"
          : resetting
            ? "正在清空原来的声纹，请稍等"
            : error !== ""
              ? error
              : notice;

  if (phase === "done") {
    return (
      <TaskShell
        eyebrow="声纹录入"
        title="声纹已启用"
        maxHeight={SHELL_MAX_HEIGHT}
        badge={
          <StatusBadge tone="success" size="md">
            全部录好了
          </StatusBadge>
        }
        exitLabel="完成"
        onExit={() => void handleClose()}
        footer={
          <div className="flex flex-col gap-3">
            {confirmingReset ? (
              <>
                <p
                  data-testid="reset-confirm"
                  className="text-[28px] font-semibold leading-snug text-warning-ink"
                >
                  重新录会把现在这三段删掉，要重新录一遍吗？
                </p>
                <div className="flex gap-3">
                  <button
                    type="button"
                    onClick={() => setConfirmingReset(false)}
                    disabled={resetting}
                    className="min-h-20 flex-1 rounded-2xl border border-line bg-surface text-3xl font-bold text-ink active:bg-surface-sunken disabled:opacity-60"
                  >
                    先留着
                  </button>
                  <button
                    type="button"
                    onClick={() => void handleReset()}
                    disabled={resetting}
                    className="min-h-20 flex-1 rounded-2xl bg-brand-strong text-3xl font-bold text-ink-inverse active:bg-brand disabled:opacity-60"
                  >
                    {resetting ? "正在清空…" : "清空，重新录"}
                  </button>
                </div>
              </>
            ) : (
              <>
                <button
                  type="button"
                  onClick={() => void handleClose()}
                  className="min-h-20 w-full rounded-2xl bg-brand text-3xl font-bold text-ink-inverse active:bg-brand-strong"
                >
                  完成
                </button>
                {/* 录歪了要能重来（2026-11-11）：此前 active 之后入口按设计隐藏、
                    前端又没有任何删除出口，长者只能找管理员清库。 */}
                <button
                  type="button"
                  data-testid="reset-voiceprint"
                  onClick={() => {
                    setError("");
                    setConfirmingReset(true);
                  }}
                  className="min-h-20 w-full rounded-2xl border border-line bg-surface text-3xl font-bold text-ink-muted active:bg-surface-sunken"
                >
                  重新录一遍
                </button>
              </>
            )}
          </div>
        }
      >
        <div className="flex min-h-0 flex-1 flex-col items-center gap-4 text-center">
          <span className="text-5xl text-success-ink" aria-hidden="true">
            ✓
          </span>
          <p className="text-4xl font-bold text-ink">声纹已启用</p>
          <p className="text-[28px] leading-relaxed text-ink-muted">
            以后您说话，系统就能认出您，不用每次都点确认。
          </p>
          <p data-testid="enroll-status" role={error !== "" ? "alert" : "status"} aria-live="polite" className={`min-h-9 text-[28px] font-semibold leading-snug ${error !== "" ? "text-warning-ink" : "text-ink-subtle"}`}>
            {statusText}
          </p>
          <p className="text-[28px] leading-relaxed text-ink-subtle">
            如果您觉得这次录得不准，可以点下面的「重新录一遍」。
          </p>
        </div>
      </TaskShell>
    );
  }

  return (
    <TaskShell
      eyebrow="声纹录入"
      title="录三段声音，让系统记住您"
      maxHeight={SHELL_MAX_HEIGHT}
      badge={
        <StatusBadge tone={doneCount === 0 ? "info" : "success"} size="md">
          {doneCount === 0 ? "还没开始" : `已经录了 ${doneCount} 段`}
        </StatusBadge>
      }
      exitLabel="稍后再录"
      onExit={() => void handleClose()}
      footer={
        <div className="flex flex-col gap-3">
          {/* 主按钮钉在页脚（TaskShell 的 footer 在滚动区之外）：矮屏上正文再长也滚不跑它 */}
          {phase === "recording" ? (
            <button
              type="button"
              onClick={() => void stopAndSubmit()}
              className="min-h-20 w-full shrink-0 rounded-2xl bg-brand-strong text-3xl font-bold text-ink-inverse active:bg-brand"
            >
              说完了，保存这一段
            </button>
          ) : (
            <button
              type="button"
              onClick={() => void startRecording()}
              disabled={phase === "uploading" || phase === "checking"}
              className="min-h-20 w-full shrink-0 rounded-2xl bg-brand text-3xl font-bold text-ink-inverse active:bg-brand-strong disabled:opacity-60"
            >
              {phase === "uploading"
                ? "正在保存这一段…"
                : phase === "checking"
                  ? "正在确认…"
                  : error !== ""
                    ? "再录一次"
                    : doneCount > 0
                      ? "录下一段"
                      : "开始录音"}
            </button>
          )}
        </div>
      }
    >
      {/* 正文区只放说明与状态：标题、主按钮都在正文区之外，矮屏（1024×768）下
          标题/正文/按钮同屏可见，正文超长时只滚这一块（2026-11-11 浮层内容显示不全）。
          2026-11-17：进度行并进提示卡（段标题行 + 右侧大号段数），固定高度只剩标题与页脚，
          1536×703 下正文从 0 恢复到 337px。 */}
      <div className="flex min-h-0 flex-1 flex-col gap-2">
        <p data-testid="enroll-purpose" className="text-[28px] font-semibold leading-snug text-ink">
          录 3 段声音，以后您一开口我就认得您
        </p>

        <div className="flex items-center justify-between gap-6 rounded-2xl bg-surface-sunken px-5 py-1">
          <div className="min-w-0">
            <p className="flex flex-wrap items-baseline gap-x-3 text-[28px] font-semibold text-ink-muted">
              <span>{segment.title}</span>
              <span data-testid="segment-index" className="text-[28px] font-semibold text-ink">
                现在是第 {segment.index} 段 · 共 {SEGMENTS.length} 段
              </span>
            </p>
            <p className="mt-1 text-[32px] font-bold leading-snug text-ink">{segment.guide}</p>
            <p className="mt-1 text-[40px] font-bold leading-tight text-ink">{segment.demo}</p>
            <p className="mt-1 text-[28px] leading-snug text-ink-muted">{segment.hint}</p>
          </div>
          {/* 右列：录音中显示倒计时；其余时候显示已录段数（大号数字，一眼看出还剩几段） */}
          {phase === "recording" ? (
            <p
              aria-live="polite"
              className="flex shrink-0 flex-col items-center text-brand"
            >
              <span className="text-6xl font-bold tabular-nums">{secondsLeft}</span>
              <span className="text-[28px] text-ink-muted">秒</span>
            </p>
          ) : (
            <p className="flex shrink-0 flex-col items-center text-brand">
              <span
                data-testid="recorded-count"
                className="text-6xl font-bold tabular-nums"
              >
                {doneCount}
              </span>
              <span className="text-[28px] text-ink-muted">/ 3 段已录好</span>
            </p>
          )}
        </div>

        <p
          data-testid="enroll-status"
          role={error !== "" ? "alert" : "status"}
          aria-live="polite"
          className={`min-h-9 text-[28px] font-semibold leading-snug ${
            error !== "" ? "text-warning-ink" : "text-ink-muted"
          }`}
        >
          {statusText}
        </p>

        <p data-testid="enroll-privacy" className="text-[28px] leading-snug text-ink-subtle">
          录音只在你家里的设备上转成声纹，原文不留。
        </p>

        {/* 这段说明放进正文滚动区（2026-11-17）：它属于「补充说明」，
            不该占页脚高度——1536×703 下页脚多这两行，正文区就被压没了。 */}
        <p data-testid="enroll-footer" className="text-[28px] leading-snug text-ink-muted">
          录不完也能退出：「稍后再录」随时可点；顶栏的「录入声纹」可以再次打开。
        </p>
      </div>
    </TaskShell>
  );
}
