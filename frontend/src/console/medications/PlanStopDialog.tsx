/**
 * 停用药单弹窗（从 MedicationsPage 抽出，前端计划 W4）。
 *
 * 口径不变：停用只改药单状态、不删历史事件；今天已发出未确认的提醒仍会走完；
 * 重复停用幂等。这三条必须写在弹窗里，否则护工会以为「停用=撤掉今天的提醒」。
 */
import { Dialog } from "../ui/Dialog";
import { ErrorBanner } from "../ui/Feedback";
import { Field, FormActions } from "../ui/Form";
import { Notice } from "../../shared/ui/Notice";
import { scheduleText, windowText, type MedicationPlanItem } from "../medications";

export interface PlanStopDialogProps {
  target: MedicationPlanItem | null;
  reason: string;
  busy: boolean;
  error: string | null;
  onReasonChange: (value: string) => void;
  onClose: () => void;
  onSubmit: () => void;
}

export function PlanStopDialog({
  target,
  reason,
  busy,
  error,
  onReasonChange,
  onClose,
  onSubmit,
}: PlanStopDialogProps) {
  return (
    <Dialog
      open={target !== null}
      title="停用药单"
      onClose={onClose}
      width={520}
      footer={
        <FormActions>
          <button type="button" className="btn-secondary" onClick={onClose} disabled={busy}>
            取消
          </button>
          <button type="button" className="btn-danger" onClick={onSubmit} disabled={busy}>
            {busy ? "提交中…" : "确认停用"}
          </button>
        </FormActions>
      }
    >
      {error ? <ErrorBanner message={error} /> : null}
      {target ? (
        <div style={{ marginBottom: 12 }}>
          <p>
            停用后不再生成新的服药提醒：
            <strong>
              {target.drug_name} {target.dose}
            </strong>
          </p>
          <p className="cell-muted">
            每日 {scheduleText(target.schedule_cron)} ·{" "}
            {windowText(target.start_date, target.end_date)} · 当前 v{target.version}
          </p>
          <Notice style={{ marginTop: 8 }}>
            停用只改药单状态，不删历史事件；今天已经发出、还没确认的提醒仍会走完，不会静默丢掉。
            重复停用不会出错（服务端幂等）。
          </Notice>
        </div>
      ) : null}
      <Field label="停用原因（可选）" htmlFor="stop-reason" hint="如「医嘱调整」「出院停药」，会写进审计">
        <input
          id="stop-reason"
          className="input"
          value={reason}
          maxLength={200}
          onChange={(event) => onReasonChange(event.target.value)}
        />
      </Field>
    </Dialog>
  );
}