/**
 * 家属端趋势图 —— **薄适配层**（前端计划 W2；Q-2 已裁决）。
 *
 * 图形实现已收敛到 `shared/ui/TrendChart`，本文件只把 `TrendPoint` 映射成系列，
 * 并保留家属端原有的窄屏尺寸：320×190、3 条刻度、无右轴（家属端不展示睡眠）。
 *
 * **单点行为变更（Q-2，产品负责人 2026-11-13）**：原实现 `validDays < 2` 直接显示
 * 「暂无足够数据」，而照护台单点是画点的 → 同一份数据家属看到点、护工看到空白，
 * **护工会误判为「没采到」**。现在两端都画：单点画圆点并直接标注数值。
 *
 * **轴标签字号**：viewBox 320 在 375px 屏幕上实际渲染约 294px（比例 0.919），
 * 原来写 16 会得到有效 14.7px，破自家「正文 ≥16px」口径。这里给 18，
 * 有效约 16.5px，达标。
 */
import { trendPointTitle, type TrendPoint } from "../reports";
import { TrendChart as SharedTrendChart, dateTickLabel } from "../../shared/ui/TrendChart";

export function TrendChart({ points }: { points: TrendPoint[] }) {
  const dates = points.map((p) => p.date);
  const withValue = points.filter((p) => p.systolic !== null || p.diastolic !== null);
  const last = withValue[withValue.length - 1];

  return (
    <SharedTrendChart
      className="chart-wrap"
      dates={dates}
      width={320}
      height={190}
      pad={{ top: 16, right: 12, bottom: 30, left: 40 }}
      tickCount={3}
      axisFontSize={18}
      leftUnit="mmHg"
      summary={
        last
          ? `近 ${points.length} 天血压趋势，最近一次 ${trendPointTitle(last)}`
          : `近 ${points.length} 天还没有血压采集记录`
      }
      emptyTitle="暂无采集记录"
      emptyHint="有健康记录后，这里会显示血压趋势。"
      dots={points.map((p) => ({
        value: p.systolic,
        color: "var(--color-brand)",
        title: trendPointTitle(p),
      }))}
      series={[
        {
          key: "systolic",
          label: "收缩压",
          color: "var(--color-brand)",
          values: points.map((p) => p.systolic),
        },
        {
          key: "diastolic",
          label: "舒张压",
          color: "var(--color-info-ink)",
          values: points.map((p) => p.diastolic),
        },
      ]}
    />
  );
}

export { dateTickLabel };