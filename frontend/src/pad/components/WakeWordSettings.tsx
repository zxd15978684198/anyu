/**
 * 长者端「唤醒词」设置（task-70）：登录后设置一次，之后每天叫的这个词就生效。
 *
 * 为什么长者可自己改：AC-42 的唤醒词是产品给的「小伴」，但方言/口音差异很大——
 * 有的长者叫「小伴」会被识别成「小半」，换成自己顺口的词（小银/阿福/康康）成功率更高。
 * 不做成「管理端下发」是因为这个词是**给长者自己听的**，护工替他选没有意义。
 *
 * 交互口径（长者端标准，PRD §5.2）：
 *  - 触控目标 ≥44px：所有按钮 `min-h-14`（56px）、字号 ≥24px；
 *  - 不该让长者猜为什么失败：切不出词时给中文原因（点名到字），而不是「保存失败」；
 *  - 保存即生效：`onSave` 由 App 立刻调 `setKeyword` 换解码器，不用重启页面/重新登录；
 *  - 持久化按 device 维度：同一台平板上的不同分机/重装浏览器互不影响。
 *
 * 存储键为什么带 device_id：`localStorage` 是按**浏览器**隔离的，不是按账号；
 * 一台演示机可能先后登录多个设备号，不带 device 维度会互相覆盖。
 */

import { useEffect, useRef, useState } from "react";

import {
  DEFAULT_WAKE_WORD,
  MAX_WAKE_WORD_CHARS,
  PRESET_WAKE_WORDS,
  describeKeywordProblem,
  splitKeyword,
} from "../audio/pinyin";
import { saveWakeWord } from "../audio/wakeWordStore";

export interface WakeWordSettingsProps {
  open: boolean;
  /** 当前设备号（来自登录态 `session.device.id`），用于按设备持久化 */
  deviceId: string;
  /** 当前生效的唤醒词 */
  current: string;
  /** 校验通过后回调；App 负责换词 + 持久化 */
  onSave: (word: string) => void;
  onClose: () => void;
}

export function WakeWordSettings({
  open,
  deviceId,
  current,
  onSave,
  onClose,
}: WakeWordSettingsProps) {
  const [draft, setDraft] = useState(current);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  // 每次打开都从「当前生效值」重来：上次没保存的草稿不该在下次打开时冒出来
  useEffect(() => {
    if (!open) return;
    setDraft(current);
    setError(null);
    // 打开后把焦点给输入框：长者端没有鼠标，键盘/读屏要能直接落到可编辑处
    const timer = setTimeout(() => inputRef.current?.focus(), 0);
    return () => clearTimeout(timer);
  }, [open, current]);

  // Esc 关闭：长者端只有触屏时也有硬件键盘可用，成本很低
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  const tokens = splitKeyword(draft);

  function handleSave() {
    const word = draft.trim();
    const problem = describeKeywordProblem(word);
    if (problem) {
      setError(problem);
      return;
    }
    saveWakeWord(deviceId, word);
    onSave(word);
    onClose();
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="wake-word-title"
      data-testid="wake-word-dialog"
      className="fixed inset-0 z-40 flex items-start justify-center overflow-y-auto bg-canvas/80 p-6 md:items-center"
    >
      {/* 高度口径同 TaskShell：矮屏下 `dvh - 48px` 封顶，标题与底部按钮都在滚动区之外，
          中间只滚正文 —— 1024×768 下标题、说明、按钮同屏可见。 */}
      <div
        data-testid="wake-word-card"
        style={{ maxHeight: "min(92vh, calc(100dvh - 48px))" }}
        className="flex w-[min(860px,calc(100vw-32px))] flex-col overflow-hidden rounded-3xl bg-surface p-8 shadow-xl"
      >
        <div className="shrink-0">
          <h2 id="wake-word-title" className="text-4xl font-bold text-ink">
            唤醒词
          </h2>
          <p className="mt-3 text-2xl text-ink-muted">
            对着平板叫这个词，就能跟数字人说话。叫不出来就换一个短一点的。
          </p>
        </div>

        <div
          data-testid="wake-word-body"
          className="mt-4 flex min-h-0 flex-1 flex-col overflow-y-auto pr-1"
        >

          {/* 预设词：覆盖常见昵称/叠字；点一下填进输入框，仍要按「保存」——避免误触直接改掉唤醒词 */}
          <div className="mt-6 flex flex-wrap gap-4">
            {PRESET_WAKE_WORDS.map((word) => (
              <button
                key={word}
                type="button"
                onClick={() => {
                  setDraft(word);
                  setError(null);
                }}
                className={`min-h-14 rounded-2xl border px-6 text-3xl ${
                  draft.trim() === word
                    ? "border-brand bg-brand text-ink-inverse"
                    : "border-line bg-surface text-ink active:bg-surface-sunken"
                }`}
              >
                {word}
              </button>
            ))}
          </div>

          <label htmlFor="wake-word-input" className="mt-8 block text-2xl text-ink">
            或者自己输入（{MAX_WAKE_WORD_CHARS} 个字以内）
          </label>
          <input
            ref={inputRef}
            id="wake-word-input"
            value={draft}
            onChange={(event) => {
              setDraft(event.target.value);
              setError(null);
            }}
            onKeyDown={(event) => {
              if (event.key === "Enter") handleSave();
            }}
            className="mt-3 min-h-14 w-full rounded-2xl border border-line bg-canvas px-6 text-3xl text-ink"
            placeholder={DEFAULT_WAKE_WORD}
          />

          {/* 把切分结果直接摆出来：长者/护工能一眼看出「这个词模型是怎么读的」 */}
          <p className="mt-3 text-xl text-ink-muted" data-testid="wake-word-preview">
            {tokens ? `识别为：${draft.trim()}（${tokens.join(" ")}）` : "这个词暂时还识别不了"}
          </p>

          {error && (
            <p role="alert" className="mt-4 text-2xl text-danger">
              {error}
            </p>
          )}
        </div>

        <div
          data-testid="wake-word-footer"
          className="mt-6 flex shrink-0 justify-end gap-4 border-t border-line pt-6"
        >
          <button
            type="button"
            onClick={onClose}
            className="min-h-14 rounded-2xl border border-line bg-surface px-8 text-2xl text-ink-muted active:bg-surface-sunken"
          >
            取消
          </button>
          <button
            type="button"
            onClick={handleSave}
            className="min-h-14 rounded-2xl bg-brand px-10 text-2xl font-bold text-ink-inverse active:bg-brand-strong"
          >
            保存
          </button>
        </div>
      </div>
    </div>
  );
}
