// 播放侧的**唯一时钟**：数采样、量能量，两件事都在音频渲染线程上做。
//
// 为什么要有它（§4.3）：
// 改造前口型读 AnalyserNode、字幕读 performance.now()、打字机自己按字数估时——三个表
// 各自的零点和走速都不一样，句数越多偏得越远。rAF 的 16.7ms 粒度还会把「首帧出声」
// 这个零点量化掉，掉帧时更差。
//
// 本节点每 128 帧（2.67ms @48k）被调用一次，这个节奏由音频设备时钟驱动，与主线程
// 是否掉帧无关。累加的采样数就是播放进度本身：主线程拿 (samples − anchor) / sampleRate
// 就得到毫秒，口型与字幕读的是**同一条消息**，物理上不可能各走各的表。
//
// 出声通路不变：本节点是**旁路观测**，输入原样拷到输出，输出接 gain=0 的汇点
// （只为了让图被 pull，不出声）。声音仍由 <audio> 元素自己出——远端轨走 srcObject、
// URL 音源走 MediaElementSource→destination，两条路径都不经过这里发声，
// 因此不存在「同一份声音两路出」的双声风险。
const REPORT_MS = 20; // 上报周期：与下行帧长同量级，主线程每秒约 50 条消息

class PlayoutClock extends AudioWorkletProcessor {
  constructor() {
    super();
    // 已处理采样数：**单调递增、跨轮不复位**。轮次零点由主线程记 anchor 做差得到，
    // 在这里复位会让「上一轮的 anchor」减出负数。
    this.samples = 0;
    this.quanta = 0;
    this.reportQuanta = Math.max(1, Math.round((sampleRate * REPORT_MS) / 1000 / 128));
    this.peak = 0;
    // 峰值出现在哪个采样序号上：主线程用它定「首帧出声」的零点，精度 2.67ms 而不是
    // 上报周期的 20ms。只报窗口末尾的 samples 会把零点系统性推后最多 20ms。
    this.peakSamples = 0;
  }

  process(inputs, outputs) {
    const input = inputs[0]?.[0];
    const output = outputs[0]?.[0];
    if (output) {
      if (input) output.set(input);
      else output.fill(0);
    }
    // 没有输入也要推进计数：图在跑就说明播放时钟在走，停掉计数会让时间轴凭空卡住。
    const n = input ? input.length : 128;
    this.samples += n;
    if (input) {
      let sum = 0;
      for (let i = 0; i < input.length; i += 1) {
        const v = input[i];
        sum += v * v;
      }
      const rms = Math.sqrt(sum / input.length);
      if (rms > this.peak) {
        this.peak = rms;
        this.peakSamples = this.samples;
      }
    }
    this.quanta += 1;
    if (this.quanta >= this.reportQuanta) {
      this.port.postMessage({
        samples: this.samples,
        rms: this.peak,
        peakSamples: this.peakSamples,
        sampleRate,
      });
      this.quanta = 0;
      this.peak = 0;
    }
    return true;
  }
}

registerProcessor("playout-clock", PlayoutClock);