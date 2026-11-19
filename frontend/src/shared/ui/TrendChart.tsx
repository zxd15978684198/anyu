/**
 * 三端共享趋势图（前端计划 W2；Q-2 已裁决）。**纯 SVG，不引任何图表库。**
 *
 * ## 收敛前两份实现的差异
 * `console/components/TrendChart.tsx` 288 行、`family/components/TrendChart.tsx` 239 行，
 * 字面重复仅 ~15%，但语义同一、配色同源 token。真正的差异是：
 * - viewBox 760×248 vs 320×190；Y 域算法不同；刻度 4 条 vs 3 条；
 * - console 多一个睡眠右轴与分级色点；
 * - **单点语义相反**：family `validDays < 2` 直接不画（显示「暂无足够数据」），
 *   console 单点居中画点。→ 同一份数据家属看到一个点、护工看到空白，
 *   **护工会误判为「没采到」**。
 *
 * ## Q-2 裁决（产品负责人 2026-11-13）
 * **只有一个数据点时两端都画出来**：有数据就该让人看见。单点画圆点并直接标注数值，
 * 不画线段（一个点连不成线），也不再显示「暂无足够数据」。
 *
 * ## 保留的口径
 * 1. 空值（null）**断开折线**而不是补 0——缺一次采集不代表血压是 0；
 * 2. 横轴由调用方给连续日历日，图右边界不因缺数据缩水；
 * 3. 每点都有 `<title>`，整图 `role="img"` + 一句概述承担语义，图形不承载唯一信息；
 * 4. 轴标签字号按 viewBox 与实际渲染宽的比例给，避免「viewBox 里 16px、
 *    渲染出来只有 14.7px」这种破自家 ≥16px 口径的情况（实测踩过）。
 */

export interface TrendSeries {
  key: string;
  label: string;
  /** 取 token，如 `var(--color-brand)`；禁止硬编码色值。 */
  color: string;
  values: (number | null)[];
  /** 虚线（用于与主量纲不同的次要系列，如睡眠时长）。 */
  dashed?: boolean;
  /** 走右轴独立刻度（量纲不同时用）。 */
  axis?: "left" | "right";
  /** 右轴单位文案，如「小时」。 */
  unit?: string;
}

export interface TrendDot {
  /** 画在哪条系列的值上；null 表示落到横轴（缺失值）。 */
  value: number | null;
  /** 取 token 的颜色。 */
  color: string;
  title: string;
}

export interface TrendChartProps {
  /** 连续日历日（YYYY-MM-DD），长度决定横轴。 */
  dates: string[];
  series: TrendSeries[];
  /** 分级色点（可选）。长度须与 dates 一致。 */
  dots?: TrendDot[];
  width?: number;
  height?: number;
  pad?: { top: number; right: number; bottom: number; left: number };
  /** 左轴刻度条数（console 原 4、family 原 3）。 */
  tickCount?: number;
  /** 轴标签在 viewBox 单位下的字号。调用方按实际渲染宽比例给，保证有效字号达标。 */
  axisFontSize?: number;
  /** 左轴单位文案，如 mmHg。 */
  leftUnit?: string;
  /** 整图概述（读屏用）。 */
  summary: string;
  /** 完全无数据时的文案。 */
  emptyTitle: string;
  emptyHint: string;
  /** 图例末尾的补充说明（如「色点 = 当次分级」）。 */
  legendNote?: string;
  className?: string;
  /** 单点是否画出来。**默认 true**（Q-2 裁决）；置 false 可恢复旧 family 行为。 */
  showSinglePoint?: boolean;
}

const DEFAULT_PAD = { top: 18, right: 44, bottom: 36, left: 50 };

function isNum(v: number | null | undefined): v is number {
  return typeof v === "number" && Number.isFinite(v);
}

/** 按「连续有值的游程」切段：缺值断开，绝不把漏采几天拉成一条假趋势。 */
function buildSegments(
  values: (number | null)[],
  x: (i: number) => number,
  y: (v: number) => number,
): string[] {
  const segments: string[] = [];
  let current: string[] = [];
  values.forEach((value, index) => {
    if (!isNum(value)) {
      if (current.length > 1) segments.push(current.join(" "));
      current = [];
      return;
    }
    current.push(`${current.length === 0 ? "M" : "L"}${x(index).toFixed(1)},${y(value).toFixed(1)}`);
  });
  if (current.length > 1) segments.push(current.join(" "));
  return segments;
}

/** 左轴域：±10 对齐到 10，最小跨度 10（console 原算法，比 family 的 15% padding 更稳）。 */
function leftDomain(values: number[]): { lo: number; hi: number } {
  if (values.length === 0) return { lo: 60, hi: 140 };
  const rawMin = Math.min(...values);
  const rawMax = Math.max(...values);
  const lo = Math.max(0, Math.floor((rawMin - 10) / 10) * 10);
  const hi = Math.ceil((rawMax + 10) / 10) * 10;
  return { lo, hi: Math.max(lo + 10, hi) };
}

/** 右轴域：整数上下各留 1，最小跨度 2（睡眠 6.2–7.4 也看得出起伏）。 */
function rightDomain(values: number[]): { lo: number; hi: number } {
  if (values.length === 0) return { lo: 0, hi: 2 };
  const lo = Math.max(0, Math.floor(Math.min(...values)) - 1);
  const hi = Math.max(lo + 2, Math.min(24, Math.ceil(Math.max(...values)) + 1));
  return { lo, hi };
}

export function dateTickLabel(date: string): string {
  const parts = date.slice(0, 10).split("-");
  return parts.length === 3 ? `${parts[1]}-${parts[2]}` : date;
}

function hourLabel(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}

export function TrendChart({
  dates,
  series,
  dots,
  width = 760,
  height = 248,
  pad = DEFAULT_PAD,
  tickCount = 4,
  axisFontSize = 13,
  leftUnit,
  summary,
  emptyTitle,
  emptyHint,
  legendNote,
  className,
  showSinglePoint = true,
}: TrendChartProps) {
  const leftSeries = series.filter((s) => s.axis !== "right");
  const rightSeries = series.filter((s) => s.axis === "right");

  const leftValues = leftSeries.flatMap((s) => s.values.filter(isNum));
  const rightValues = rightSeries.flatMap((s) => s.values.filter(isNum));
  const validDays = dates.filter((_, i) =>
    series.some((s) => isNum(s.values[i])),
  ).length;

  // 完全无数据 → 空态。单点不再走空态（Q-2）：有数据就该看得见。
  if (dates.length === 0 || validDays === 0 || (!showSinglePoint && validDays < 2)) {
    return (
      <div className="ui-chart-empty">
        <p className="ui-chart-empty-title">{emptyTitle}</p>
        <p className="ui-chart-empty-hint">{emptyHint}</p>
      </div>
    );
  }

  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const single = validDays === 1;

  const { lo, hi } = leftDomain(leftValues);
  const span = hi - lo;
  const x = (i: number): number =>
    dates.length === 1 ? pad.left + plotW / 2 : pad.left + (i * plotW) / (dates.length - 1);
  const yLeft = (v: number): number => pad.top + (1 - (v - lo) / span) * plotH;

  const right = rightDomain(rightValues);
  const rightSpan = right.hi - right.lo;
  const yRight = (v: number): number => pad.top + (1 - (v - right.lo) / rightSpan) * plotH;

  const ticks = Array.from({ length: tickCount }, (_, i) => lo + (span * i) / (tickCount - 1));
  const rightTicks = [0, 0.5, 1].map((r) => right.lo + rightSpan * r);

  // 右轴只在真的存在右轴系列数据时出现：缺数据不该长出一条刻度齐全却没有线的轴。
  const hasRight = rightSeries.length > 0 && rightValues.length > 0;
  const labelIndexes = Array.from(
    new Set([0, Math.floor((dates.length - 1) / 2), dates.length - 1]),
  );

  return (
    <div className={className ? `ui-chart ${className}` : "ui-chart"}>
      <div className="ui-chart-legend">
        {series.map((s) => {
          if (s.axis === "right" && !hasRight) return null;
          return (
            <span className="ui-chart-legend-item" key={s.key}>
              <i
                className="ui-chart-swatch"
                style={{
                  background: s.dashed
                    ? `repeating-linear-gradient(90deg, ${s.color} 0 3px, transparent 3px 6px)`
                    : s.color,
                }}
                aria-hidden="true"
              />
              {s.label}
              {s.axis === "right" && s.unit ? `（右轴 · ${s.unit}）` : ""}
            </span>
          );
        })}
        {legendNote && <span className="ui-chart-legend-note">{legendNote}</span>}
      </div>

      <svg
        className="ui-chart-svg"
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label={summary}
        preserveAspectRatio="xMidYMid meet"
      >
        <title>{summary}</title>

        {ticks.map((tick) => (
          <g key={`t-${tick}`}>
            <line
              x1={pad.left}
              x2={width - pad.right}
              y1={yLeft(tick)}
              y2={yLeft(tick)}
              stroke="var(--color-line)"
              strokeWidth={1}
            />
            <text
              x={pad.left - 8}
              y={yLeft(tick) + axisFontSize / 3}
              textAnchor="end"
              fontSize={axisFontSize}
              fill="var(--color-ink-muted)"
            >
              {Math.round(tick)}
            </text>
          </g>
        ))}

        {leftUnit && (
          <text
            x={pad.left - 8}
            y={pad.top - 5}
            textAnchor="end"
            fontSize={axisFontSize}
            fill="var(--color-ink-muted)"
          >
            {leftUnit}
          </text>
        )}

        {hasRight && (
          <g>
            <text
              x={width - pad.right + 8}
              y={height - pad.bottom + axisFontSize + 6}
              textAnchor="start"
              fontSize={axisFontSize}
              fill="var(--color-ink-muted)"
            >
              {rightSeries[0]?.unit ?? ""}
            </text>
            {rightTicks.map((tick) => (
              <text
                key={`rt-${tick}`}
                x={width - pad.right + 8}
                y={yRight(tick) + axisFontSize / 3}
                textAnchor="start"
                fontSize={axisFontSize}
                fill="var(--color-ink-muted)"
              >
                {hourLabel(tick)}
              </text>
            ))}
          </g>
        )}

        {/* 折线：单点时不画线（一个点连不成线），只画点与数值标注 */}
        {!single &&
          series.map((s) => {
            if (s.axis === "right" && !hasRight) return null;
            const y = s.axis === "right" ? yRight : yLeft;
            return buildSegments(s.values, x, y).map((path, i) => (
              <path
                key={`${s.key}-${i}`}
                d={path}
                fill="none"
                stroke={s.color}
                strokeWidth={2}
                strokeDasharray={s.dashed ? "6 4" : undefined}
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            ));
          })}

        {/* 数据点：每个有值的点都画，单点也画（Q-2）。分级色点优先，否则用系列色。 */}
        {dates.map((date, i) => {
          const dot = dots?.[i];
          const primary = leftSeries[0]?.values[i] ?? null;
          const value = dot ? dot.value : primary;
          const hasValue = isNum(value) || series.some((s) => isNum(s.values[i]));
          if (!hasValue && !dot) return null;
          const cy = isNum(value) ? yLeft(value) : pad.top + plotH;
          const color = dot?.color ?? leftSeries[0]?.color ?? "var(--color-brand)";
          const title =
            dot?.title ??
            `${dateTickLabel(date)} ${series
              .map((s) => `${s.label} ${isNum(s.values[i]) ? s.values[i] : "缺失"}`)
              .join(" · ")}`;
          return (
            <circle
              key={`dot-${date}-${i}`}
              cx={x(i)}
              cy={cy}
              r={single ? 5 : 4}
              fill={color}
              stroke="var(--color-surface)"
              strokeWidth={1.5}
            >
              <title>{title}</title>
            </circle>
          );
        })}

        {/* 单点：直接把数值标在点旁，避免「只有一个点看不出是多少」 */}
        {single &&
          leftSeries.map((s) => {
            const i = s.values.findIndex(isNum);
            if (i < 0) return null;
            const v = s.values[i] as number;
            return (
              <text
                key={`single-${s.key}`}
                x={x(i) + 10}
                y={yLeft(v) + axisFontSize / 3}
                fontSize={axisFontSize}
                fontWeight={700}
                fill={s.color}
              >
                {`${s.label} ${v}`}
              </text>
            );
          })}

        {labelIndexes.map((i) => (
          <text
            key={`label-${i}`}
            x={x(i)}
            y={height - pad.bottom + axisFontSize + 6}
            textAnchor={i === 0 ? "start" : i === dates.length - 1 ? "end" : "middle"}
            fontSize={axisFontSize}
            fill="var(--color-ink-muted)"
          >
            {dateTickLabel(dates[i])}
          </text>
        ))}
      </svg>
    </div>
  );
}