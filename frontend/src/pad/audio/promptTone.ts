/**
 * 「呼叫」按钮的提示音（FR-UI-09 降级入口）。
 *
 * 唤醒词不可用时（无麦克风 / 模型包缺失 / 初始化失败），长者端没有「小伴」可叫，
 * 只剩页面上一颗大按钮。按下去必须**立刻有声音**，否则他会以为按钮也是坏的、
 * 反复戳屏幕 —— 提示音在这里承担的是「按到了」的确认，不是装饰。
 *
 * 不引音频资源：两段 WebAudio 正弦音，约 0.36s，不占包体、不依赖网络。
 * 失败一律静默（没有 AudioContext、被自动播放策略挂起、设备没扬声器）：
 * 主提示是上屏的「我在呢，您说吧」，提示音只是锦上添花。
 */

/** 上行两音（G5 → C6）。长者高频听力先降，800Hz 附近比 3kHz 更容易听见。 */
const TONES = [784, 1046] as const;
const TONE_GAP_S = 0.16;
const TONE_LEN_S = 0.15;

export function playCallPrompt(): void {
  try {
    const Ctor: typeof AudioContext | undefined =
      window.AudioContext ??
      (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!Ctor) return;
    const ctx = new Ctor();
    // 被自动播放策略挂起时，resume() 在用户手势（点击）里调用是允许的
    if (ctx.state === "suspended") void ctx.resume().catch(() => {});
    const start = ctx.currentTime + 0.02;
    TONES.forEach((freq, index) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "sine";
      osc.frequency.value = freq;
      const at = start + index * TONE_GAP_S;
      // 包络必须有斜率：直接 setValueAtTime 到一个非零值会「啪」一声（爆音）
      gain.gain.setValueAtTime(0.0001, at);
      gain.gain.exponentialRampToValueAtTime(0.22, at + 0.03);
      gain.gain.exponentialRampToValueAtTime(0.0001, at + TONE_LEN_S);
      osc.connect(gain).connect(ctx.destination);
      osc.start(at);
      osc.stop(at + TONE_LEN_S + 0.01);
    });
    const closeInMs = Math.max(0, (start + TONE_GAP_S + TONE_LEN_S + 0.12 - ctx.currentTime) * 1000);
    window.setTimeout(() => {
      void ctx.close().catch(() => {});
    }, closeInMs);
  } catch {
    /* 提示音失败不影响呼叫本身 */
  }
}
