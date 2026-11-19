/**
 * 语音链路沙盘（门户主交互，计划 §10.4）。
 *
 * 数据是**基线快照**，不是实时测量：三份实测报告口径各自标注、不混算。
 * 控件常驻可见（场景 / 分位 / 播放 / 速度 / 重置 / 预算线 / 导出），不折叠不藏弹窗。
 * 未测段（① 唤醒 ② ASR ⑥ 前端播放）渲染为斜纹且**不计入合计**——不编数字。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  LATENCY_BUDGET_MS,
  LATENCY_SCENARIOS,
  exportSnapshot,
  type LatencyScenario,
} from "../baselines";
import { prefersReducedMotion } from "../hooks";
import { IconDownload, IconPause, IconPlay, IconReset } from "./Icons";

type Quantile = "p50" | "p95" | "max";

const QUANTILES: { value: Quantile; label: string }[] = [
  { value: "p50", label: "P50" },
  { value: "p95", label: "P95" },
  { value: "max", label: "max" },
];

const SPEEDS = [0.5, 1, 2];
/** 一次播放的基准时长（ms），再除以速度。与真实耗时无关，只影响动画观感。 */
const SWEEP_MS = 2000;
const UNTESTED_WIDTH = 7;
const MIN_MEASURED_WIDTH = 4;

const TONES: Record<string, string[]> = {
  mock: ["total"],
  real: ["reply", "tts"],
  stream: ["probe", "total"],
};

const TONE_LABEL: Record<string, string> = {
  reply: "组装 + LLM/Agent",
  tts: "TTS 首块",
  total: "回复侧合计",
  probe: "首句入队",
};

const SWATCH: Record<string, string> = {
  reply: "var(--color-info-ink)",
  tts: "var(--color-brand)",
  total: "var(--color-brand-strong)",
  probe: "var(--color-success-ink)",
  untested: "var(--color-neutral-soft)",
};

function fmt(ms: number): string {
  if (ms >= 1000) return `${(ms / 1000).toFixed(2)}s`;
  // 100ms 以下保留一位小数：Mock 与探针场景的差异全在个位毫秒，
  // 取整会把 12.2 与 12.8 显示成同一个数，沙盘就失去意义。
  return `${ms < 100 ? ms.toFixed(1) : Math.round(ms)}ms`;
}

export function LatencySandbox({ onToast }: { onToast: (msg: string) => void }) {
  const [scenarioId, setScenarioId] = useState(LATENCY_SCENARIOS[1].id);
  const [quantile, setQuantile] = useState<Quantile>("p50");
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [showBudget, setShowBudget] = useState(true);
  const [progress, setProgress] = useState(1);

  const scenario = useMemo<LatencyScenario>(
    () => LATENCY_SCENARIOS.find((s) => s.id === scenarioId) ?? LATENCY_SCENARIOS[0],
    [scenarioId],
  );

  const timerRef = useRef<number | null>(null);
  const lastRef = useRef<number | null>(null);

  // 单一计时器循环：依赖变化时先清旧的再起新的，绝不叠加。
  // **刻意不用 rAF**：页面 hidden / 未获焦点时 rAF 不触发（实测 rafFired=false），
  // 播放会永远停在原地。沙盘是数据可视化不是 60fps 动画，32ms 计时器足够且更稳。
  useEffect(() => {
    if (!playing) {
      lastRef.current = null;
      return;
    }
    if (prefersReducedMotion()) {
      setProgress(1);
      setPlaying(false);
      return;
    }
    lastRef.current = Date.now();
    const id = window.setInterval(() => {
      const now = Date.now();
      const last = lastRef.current ?? now;
      lastRef.current = now;
      const delta = ((now - last) / SWEEP_MS) * speed;
      setProgress((prev) => Math.min(1, prev + delta));
    }, 32);
    timerRef.current = id;
    return () => {
      window.clearInterval(id);
      timerRef.current = null;
      lastRef.current = null;
    };
  }, [playing, speed]);

  // 播完即停：与递增分开，避免在 state updater 里产生副作用。
  useEffect(() => {
    if (playing && progress >= 1) setPlaying(false);
  }, [playing, progress]);

  const measured = useMemo(() => scenario.segments.filter((s) => s.measured), [scenario]);
  const untestedCount = scenario.segments.length - measured.length;
  const measuredTotal = useMemo(() => measured.reduce((sum, s) => sum + (s.ms ?? 0), 0), [measured]);

  const scaleMax = Math.max(measuredTotal, showBudget ? LATENCY_BUDGET_MS : 0) * 1.1 || 1;
  const available = 100 - UNTESTED_WIDTH * untestedCount;

  const widths = useMemo(() => {
    const raw = measured.map((s) => ((s.ms ?? 0) / scaleMax) * available);
    const floored = raw.map((v) => Math.max(v, MIN_MEASURED_WIDTH));
    const sum = floored.reduce((a, b) => a + b, 0) || 1;
    return floored.map((v) => (v / sum) * available);
  }, [measured, scaleMax, available]);

  const tones = TONES[scenario.id] ?? TONES.mock;

  /** 预算线只在真的落在已测区间内时画，否则用右侧标记——避免画一条位置误导的线。 */
  const budgetPos = useMemo(() => {
    if (!showBudget || LATENCY_BUDGET_MS > measuredTotal) return null;
    let accMs = 0;
    let accW = UNTESTED_WIDTH * 2; // ① ② 在已测段之前
    for (let i = 0; i < measured.length; i += 1) {
      const ms = measured[i].ms ?? 0;
      if (accMs + ms >= LATENCY_BUDGET_MS) {
        return accW + ((LATENCY_BUDGET_MS - accMs) / (ms || 1)) * widths[i];
      }
      accMs += ms;
      accW += widths[i];
    }
    return null;
  }, [showBudget, measured, measuredTotal, widths]);

  const measuredRegionStart = UNTESTED_WIDTH * 2;
  const measuredRegionWidth = widths.reduce((a, b) => a + b, 0);
  const playheadPos = measuredRegionStart + measuredRegionWidth * progress;

  const value = scenario[quantile];
  const over = value > LATENCY_BUDGET_MS;

  const reset = useCallback(() => {
    setPlaying(false);
    setProgress(0);
    lastRef.current = null;
  }, []);

  const pickScenario = (id: string) => {
    setScenarioId(id);
    setPlaying(false);
    setProgress(1);
  };

  const togglePlay = () => {
    if (playing) {
      setPlaying(false);
      return;
    }
    if (progress >= 1) setProgress(0);
    lastRef.current = null;
    setPlaying(true);
  };

  const doExport = () => {
    try {
      const text = exportSnapshot(scenario, quantile);
      const blob = new Blob([text], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `anyu-latency-${scenario.id}-${quantile}.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      onToast(`已导出 ${scenario.name} · ${quantile.toUpperCase()} 快照`);
    } catch {
      onToast("导出失败：当前浏览器不支持文件下载");
    }
  };

  let cursor = 0;
  const bars = scenario.segments.map((seg, i) => {
    const isMeasured = seg.measured;
    const width = isMeasured ? widths[measured.indexOf(seg)] : UNTESTED_WIDTH;
    const tone = isMeasured ? tones[measured.indexOf(seg)] ?? "total" : "untested";
    const left = cursor;
    cursor += width;
    const segOver = isMeasured && showBudget && left + width > (budgetPos ?? Infinity);
    return (
      <div
        key={`${seg.label}-${i}`}
        className={segOver ? "p-seg-bar is-over" : "p-seg-bar"}
        data-tone={tone}
        style={{ width: `${width}%`, ["--p-share" as string]: width }}
        title={`${seg.label}：${isMeasured ? fmt(seg.ms ?? 0) : "未测"} — ${seg.note}`}
      >
        <span className="p-seg-label">
          {isMeasured ? `${seg.label} ${fmt(seg.ms ?? 0)}` : seg.label}
        </span>
      </div>
    );
  });

  return (
    <div className="p-card p-sandbox">
      <div className="p-sandbox-head">
        <div>
          <h3 className="p-h2" style={{ fontSize: "1.35rem", marginBottom: 6 }}>
            {scenario.name}
          </h3>
          <p className="p-scenario-desc">{scenario.blurb}</p>
        </div>
        <span className="p-badge" data-tone={scenario.verdict === "pass" ? "success" : "danger"}>
          {scenario.passRate} 在 1.5s 内
        </span>
      </div>

      {/* 常驻控件区 */}
      <div className="p-controls" role="group" aria-label="沙盘控件">
        <div className="p-ctl-group">
          <span className="p-ctl-label" id="p-scenario-label">场景</span>
          <div className="p-seg" role="group" aria-labelledby="p-scenario-label">
            {LATENCY_SCENARIOS.map((s) => (
              <button
                key={s.id}
                type="button"
                className={s.id === scenarioId ? "p-seg-btn is-active" : "p-seg-btn"}
                aria-pressed={s.id === scenarioId}
                onClick={() => pickScenario(s.id)}
              >
                {s.name}
              </button>
            ))}
          </div>
        </div>

        <div className="p-ctl-group">
          <span className="p-ctl-label" id="p-quantile-label">分位</span>
          <div className="p-seg" role="group" aria-labelledby="p-quantile-label">
            {QUANTILES.map((q) => (
              <button
                key={q.value}
                type="button"
                className={q.value === quantile ? "p-seg-btn is-active" : "p-seg-btn"}
                aria-pressed={q.value === quantile}
                onClick={() => setQuantile(q.value)}
              >
                {q.label}
              </button>
            ))}
          </div>
        </div>

        <div className="p-ctl-group">
          <button type="button" className="p-btn p-btn-primary p-btn-sm" onClick={togglePlay}>
            {playing ? <IconPause /> : <IconPlay />}
            {playing ? "暂停" : progress >= 1 ? "重播" : "播放"}
          </button>
          <button type="button" className="p-btn p-btn-ghost p-btn-sm" onClick={reset}>
            <IconReset />
            重置
          </button>
        </div>

        <div className="p-ctl-group">
          <label className="p-ctl-label" htmlFor="p-speed">速度</label>
          <select
            id="p-speed"
            className="p-select"
            value={speed}
            onChange={(e) => setSpeed(Number(e.target.value))}
          >
            {SPEEDS.map((s) => (
              <option key={s} value={s}>{`${s}×`}</option>
            ))}
          </select>
        </div>

        <label className="p-switch">
          <input
            type="checkbox"
            checked={showBudget}
            onChange={(e) => setShowBudget(e.target.checked)}
          />
          <span className="p-switch-track" />
          1.5s 预算线
        </label>

        <button type="button" className="p-btn p-btn-secondary p-btn-sm" onClick={doExport}>
          <IconDownload />
          导出 JSON
        </button>
      </div>

      <div className="p-track-wrap">
        {showBudget && budgetPos === null && (
          <span
            className="p-budget-flag"
            style={{ left: `${measuredRegionStart + measuredRegionWidth}%` }}
          >
            预算 1.5s · 未超
          </span>
        )}
        {budgetPos !== null && (
          <>
            <span className="p-budget-flag" style={{ left: `${budgetPos}%` }}>
              预算 1.5s
            </span>
            <span className="p-budget-line" style={{ left: `${budgetPos}%` }} />
          </>
        )}
        <div
          className="p-track"
          role="img"
          aria-label={`${scenario.name} 分段耗时：${measured
            .map((s) => `${s.label} ${fmt(s.ms ?? 0)}`)
            .join("，")}；合计 ${fmt(measuredTotal)}`}
        >
          {bars}
        </div>
        <span className="p-playhead" style={{ left: `${playheadPos}%` }} />
      </div>

      <div className="p-legend">
        {measured.map((s, i) => (
          <span className="p-legend-item" key={s.label}>
            <span
              className="p-legend-swatch"
              style={{ background: SWATCH[tones[i] ?? "total"] }}
            />
            {TONE_LABEL[tones[i] ?? "total"] ?? s.label}
            <span className="p-legend-ms">{fmt(s.ms ?? 0)}</span>
          </span>
        ))}
        <span className="p-legend-item">
          <span
            className="p-legend-swatch"
            style={{
              backgroundImage:
                "repeating-linear-gradient(45deg, var(--color-neutral-soft) 0 4px, var(--color-surface-sunken) 4px 8px)",
            }}
          />
          未测（不计入合计）
        </span>
      </div>

      <div className="p-verdict" data-verdict={scenario.verdict}>
        <span className="p-verdict-big">{fmt(value)}</span>
        <div>
          <p className="p-verdict-text">
            <strong>{scenario.metricLabel}</strong> · {quantile.toUpperCase()} ·{" "}
            {over ? `超出 1.5s 预算 ${fmt(value - LATENCY_BUDGET_MS)}` : "在 1.5s 预算内"}
          </p>
          <p className="p-verdict-text">
            达标 {scenario.passRate} · 来源 {scenario.source.from}
            {scenario.source.at ? ` @ ${scenario.source.at}` : ""}
          </p>
        </div>
      </div>

      <ul className="p-footnotes">
        {scenario.footnotes.map((f) => (
          <li key={f}>{f}</li>
        ))}
        <li>
          条宽为可读性设了 {MIN_MEASURED_WIDTH}% 下限，**数值以标注与图例为准**；未测段固定宽度、不计入合计。
        </li>
        <li>本页为基线快照，非实时测量；门户不接后端指标，避免成为第二个 /metrics 消费口径。</li>
      </ul>
    </div>
  );
}