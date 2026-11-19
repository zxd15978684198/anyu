/**
 * 健康等级色点（W4）。
 *
 * 无障碍口径：**颜色不是唯一信息**——色点旁边永远跟一句中文（`GRADE_LABEL`），
 * 色点本身 `aria-hidden`，读屏只读文字，色弱用户也读得懂。
 */

export type Tone = "red" | "yellow" | "green" | "missing";

interface SeverityDotProps {
  tone: Tone;
  label: string;
}

export function SeverityDot({ tone, label }: SeverityDotProps) {
  return (
    <span className={`sev sev-${tone}`}>
      <span className="sev-dot" aria-hidden="true" />
      <span className="sev-label">{label}</span>
    </span>
  );
}
