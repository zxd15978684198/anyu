/**
 * 照护台趋势图 —— **薄适配层**（前端计划 W2；Q-2 已裁决）。
 *
 * 图形实现已收敛到 `shared/ui/TrendChart`，本文件只把 `HealthTrendPoint`
 * 映射成共享组件的系列/色点，并保留照护台原有的尺寸与口径：
 * 760×248、4 条左轴刻度、睡眠走右轴虚线、分级色点画在收缩压节点上。
 *
 * **单点行为变更（Q-2）**：原实现单点居中画点，家属端却直接不画 →
 * 同一份数据护工看到空白会误判「没采到」。现在两端都画（共享组件默认 showSinglePoint）。
 */
import { gradeTone, type HealthTrendPoint } from "../health";
import { TrendChart as SharedTrendChart, dateTickLabel, type TrendDot } from "../../shared/ui/TrendChart";

const TONE_COLOR: Record<string, string> = {
  success: "var(--color-success-ink)",
  warning: "var(--color-warning-ink)",
  danger: "var(--color-danger-ink)",
  neutral: "var(--color-ink-muted)",
  info: "var(--color-info-ink)",
};

export function TrendChart({ points }: { points: HealthTrendPoint[] }) {
  if (points.length === 0) {
    return (
      <div className="trend-empty">
        近 14 天还没有采集记录。提交一次代填后，这里会出现收缩压 / 舒张压 / 睡眠时长折线。
      </div>
    );
  }

  const dates = points.map((p) => p.date);
  const last = points[points.length - 1];
  const sleepSummary =
    typeof last.sleep_hours === "number" ? `、睡眠 ${last.sleep_hours} 小时` : "";

  const dots: TrendDot[] = points.map((p) => ({
    value: typeof p.systolic === "number" ? p.systolic : null,
    color: TONE_COLOR[gradeTone(p.grade)] ?? TONE_COLOR.neutral,
    title: `${dateTickLabel(p.date)} ${p.systolic ?? "缺失"}/${p.diastolic ?? "缺失"} mmHg${
      typeof p.sleep_hours === "number" ? ` · 睡眠 ${p.sleep_hours} 小时` : ""
    }`,
  }));

  return (
    <SharedTrendChart
      className="trend-wrap"
      dates={dates}
      width={760}
      height={248}
      pad={{ top: 18, right: 44, bottom: 36, left: 50 }}
      tickCount={4}
      axisFontSize={13}
      leftUnit="mmHg"
      dots={dots}
      legendNote="色点 = 当次分级（绿 / 黄 / 红 / 缺失）"
      summary={`近 ${points.length} 天趋势：最近一次 ${dateTickLabel(last.date)} 收缩压 ${
        last.systolic ?? "缺失"
      }、舒张压 ${last.diastolic ?? "缺失"}（mmHg）${sleepSummary}`}
      emptyTitle="近 14 天还没有采集记录"
      emptyHint="提交一次代填后，这里会出现收缩压 / 舒张压 / 睡眠时长折线。"
      series={[
        {
          key: "systolic",
          label: "收缩压",
          color: "var(--color-brand)",
          values: points.map((p) => (typeof p.systolic === "number" ? p.systolic : null)),
        },
        {
          key: "diastolic",
          label: "舒张压",
          color: "var(--color-info-ink)",
          values: points.map((p) => (typeof p.diastolic === "number" ? p.diastolic : null)),
        },
        {
          key: "sleep",
          label: "睡眠时长",
          color: "var(--color-success-ink)",
          dashed: true,
          axis: "right",
          unit: "小时",
          values: points.map((p) =>
            typeof p.sleep_hours === "number" && Number.isFinite(p.sleep_hours)
              ? p.sleep_hours
              : null,
          ),
        },
      ]}
    />
  );
}