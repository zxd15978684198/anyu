/**
 * 长者端首启引导（FR-UI-06）：第一次打开平板时讲清三件事，讲完就不再自动出现。
 *
 * 为什么单独一个 overlay 组件、不塞进 App.tsx：App 已经 1100+ 行，引导是一个
 * 「讲三句话 + 两个按钮」的自包含流程，装配点只需要一个 `open` 状态。
 *
 * 三个必须讲清的点（PRD FR-UI-06「手持平板是否真能自己用起来」的验收面）：
 *   ① 怎么叫醒数字人（出厂唤醒词「小伴」，可在设置里换）；
 *   ② 想说话又没有唤醒词可叫时，那颗「呼叫」大按钮在哪；
 *   ③ 听不清看哪里 —— 屏幕上的大字幕，以及字已经是大号这一事实。
 *
 * 交互约束（长者端 1920×1080）：
 *   - 触控目标全部 ≥96px 高（`h-24` / `min-h-[96px]`），远超 44px 无障碍下限；
 *   - 正文 ≥28px、标题 48px 起，两米外可读；
 *   - 「跳过」始终可见且**不是**小字灰链接 —— 长者误入时必须能一眼离开；
 *   - 只出现一次：localStorage `onboarded:v1`。清掉它即可验收「首次进入可见」，
 *     设置里的「使用引导」也能手动重看（PRD「可跳过可重看」）。
 */

import { useState } from "react";

/** 首启引导的完成标记。清掉这个键，引导就会再出现一次（验收用）。 */
export const ONBOARDED_KEY = "onboarded:v1";

/** 是否已经看过引导；localStorage 不可用（隐私模式）时按「看过」处理，不再打扰。 */
export function hasOnboarded(): boolean {
  try {
    return globalThis.localStorage?.getItem(ONBOARDED_KEY) === "1";
  } catch {
    return true;
  }
}

/**
 * 标记引导已看过。
 *
 * 跳过也算看过：长者连点两次「跳过」的意思就是「别再问我了」，重复弹窗比不讲更烦人。
 * 想再看可以从设置里进来。
 */
export function markOnboarded(): void {
  try {
    globalThis.localStorage?.setItem(ONBOARDED_KEY, "1");
  } catch {
    // 存不进只影响「下次还弹」；不阻断本次使用（AGENTS §9 的教训：端侧存储失败不许变白屏）
  }
}

interface Step {
  /** 步骤序号，同时用作进度点的 key */
  no: number;
  title: string;
  /** 一句一句的说明，长者端不写长段落 */
  lines: string[];
  /** 示意区：用大字/色块画，不用截图（截图会随版本过期，且要额外的图资源） */
  art: "wake" | "call" | "subtitle";
}

function buildSteps(wakeWord: string): Step[] {
  return [
    {
      no: 1,
      title: `叫一声「${wakeWord}」，我就来了`,
      lines: [
        `对着平板说「${wakeWord}」，屏幕上会出现提示音。`,
        "听到提示音以后，再慢慢说您想说的话。",
        "想换个叫法（比如叫「老伴」「丫头」），让家里人按右上角「设置」里的「唤醒词」改。",
      ],
      art: "wake",
    },
    {
      no: 2,
      title: "叫不醒我时，就按「呼叫」",
      lines: [
        "要是哪天平板上听不到我说话，屏幕右边会多出一颗蓝色的「呼叫」，按一下就能跟我说话。",
        "平时看不到它是正常的 —— 只有唤醒不好用的时候它才出现，不用特意去找。",
        "最上面一排右边的红色「一键求助」一直都在；不舒服就按它，会马上叫人来。",
      ],
      art: "call",
    },
    {
      no: 3,
      title: "听不清，就看屏幕上的大字",
      lines: [
        "我说的每一句话，都会同时在屏幕上显示成大字。",
        "这里的字已经调到最大号了；要是还看不清，跟工作人员说一声，把平板挪近一点、或者把屏幕调亮。",
        "想再听一遍，说「再说一遍」就行。",
      ],
      art: "subtitle",
    },
  ];
}

/** 三步流程图里的示意块。纯 CSS/文字，不依赖图片资源（离线也能显示）。 */
function StepArt({ kind, wakeWord }: { kind: Step["art"]; wakeWord: string }) {
  if (kind === "wake") {
    return (
      <div className="flex flex-col items-center gap-4">
        <div className="rounded-full border-2 border-brand-line bg-brand-soft px-10 py-6 text-4xl font-bold text-brand">
          说「{wakeWord}」
        </div>
        <div className="text-3xl text-ink-muted">↓ 听到提示音再说话</div>
        <div className="rounded-3xl border border-line bg-surface px-10 py-6 text-3xl text-ink">
          我在呢，您说
        </div>
      </div>
    );
  }
  if (kind === "call") {
    return (
      <div className="flex flex-col items-center gap-4">
        <div className="flex min-h-[96px] w-72 items-center justify-center rounded-3xl bg-brand text-4xl font-bold text-ink-inverse">
          呼叫
        </div>
        <div className="text-3xl text-ink-muted">屏幕右边 · 叫不醒时才出现</div>
        <div className="flex min-h-[72px] items-center justify-center rounded-2xl bg-[#B3261E] px-10 text-3xl font-bold text-white">
          一键求助
        </div>
        <div className="text-3xl text-ink-muted">最上面一排右侧 · 一直都在</div>
      </div>
    );
  }
  return (
    <div className="flex flex-col items-center gap-4">
      <div className="w-full rounded-3xl border border-line bg-surface px-8 py-6 text-4xl leading-snug text-ink">
        早饭吃过啦？今天记得把药吃了。
      </div>
      <div className="text-3xl text-ink-muted">每句话都会变成这样的大字</div>
    </div>
  );
}

interface OnboardingGuideProps {
  /** 当前生效的唤醒词（未设置时由调用方传出厂词） */
  wakeWord: string;
  /** 关闭引导：跳过与看完走同一个出口，差别只在提示语 */
  onDone: (completed: boolean) => void;
}

export function OnboardingGuide({ wakeWord, onDone }: OnboardingGuideProps) {
  const [index, setIndex] = useState(0);
  const steps = buildSteps(wakeWord);
  const step = steps[index] ?? steps[0];
  const isLast = index === steps.length - 1;

  return (
    <div
      className="fixed inset-0 z-40 flex items-center justify-center bg-canvas/85 p-12"
      role="dialog"
      aria-modal="true"
      aria-label="使用引导"
    >
      <div className="flex max-h-full w-full max-w-[1280px] flex-col overflow-y-auto rounded-[2rem] border border-line bg-surface px-12 py-10 shadow-[0_24px_64px_-24px_rgba(45,48,45,0.45)]">
        <div className="flex items-center justify-between gap-6">
          <p className="text-2xl text-ink-muted">
            第一次用 · 第 {step.no} / {steps.length} 步
          </p>
          <div className="flex items-center gap-3">
            {steps.map((item) => (
              <span
                key={item.no}
                className={`h-4 w-16 rounded-full ${
                  item.no <= step.no ? "bg-brand" : "bg-line"
                }`}
              />
            ))}
          </div>
        </div>

        <h2 className="mt-8 text-5xl font-bold leading-tight text-ink">{step.title}</h2>

        <div className="mt-8">
          <StepArt kind={step.art} wakeWord={wakeWord} />
        </div>

        <ul className="mt-8 flex flex-col gap-4">
          {step.lines.map((line) => (
            <li key={line} className="flex gap-3 text-3xl leading-snug text-ink-muted">
              <span aria-hidden="true" className="text-brand">
                ·
              </span>
              <span>{line}</span>
            </li>
          ))}
        </ul>

        {/* 按钮区常驻底部：不在流程里把「跳过」藏起来，长者随时能退出 */}
        <div className="mt-10 flex items-center gap-4 border-t border-line pt-8">
          <button
            type="button"
            onClick={() => onDone(false)}
            className="h-24 rounded-2xl border border-line bg-surface px-10 text-3xl font-semibold text-ink-muted active:bg-surface-sunken"
          >
            跳过，我会用
          </button>
          {index > 0 && (
            <button
              type="button"
              onClick={() => setIndex((value) => Math.max(0, value - 1))}
              className="h-24 rounded-2xl border border-line bg-surface px-10 text-3xl font-semibold text-ink active:bg-surface-sunken"
            >
              上一步
            </button>
          )}
          <button
            type="button"
            onClick={() => {
              if (isLast) {
                onDone(true);
                return;
              }
              setIndex((value) => Math.min(steps.length - 1, value + 1));
            }}
            className="ml-auto h-24 min-w-[280px] rounded-2xl bg-brand px-12 text-3xl font-bold text-ink-inverse active:bg-brand-strong"
          >
            {isLast ? "知道了，开始用" : "下一步"}
          </button>
        </div>
      </div>
    </div>
  );
}
