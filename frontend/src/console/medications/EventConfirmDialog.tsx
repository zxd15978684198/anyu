/**
 * 代确认服药弹窗（从 MedicationsPage 抽出，前端计划 W4）。
 *
 * 口径不变：代确认等于替长者作证「已服药」，会留审计（渠道记为照护台按钮），
 * 所以弹窗里必须把「计划时刻 / 已触达次数 / 当前状态」摆出来让护工核实后再点。
 */
import { Dialog } from "../ui/Dialog";
import { ErrorBanner } from "../ui/Feedback";
import { Field, FormActions } from "../ui/Form";
import { Notice } from "../../shared/ui/Notice";
import { eventStatusLabel, timeText, type MedicationEventItem } from "../medications";

export interface EventConfirmDialogProps {
  target: MedicationEventItem | null;
  note: string;
  busy: boolean;
  error: string | null;
  onNoteChange: (value: string) => void;
  onClose: () => void;
  onSubmit: () => void;
}

export function EventConfirmDialog({
  target,
  note,
  busy,
  error,
  onNoteChange,
  onClose,
  onSubmit,
}: EventConfirmDialogProps) {
  return (
    <Dialog
      open={target !== null}
      title="代确认服药"
      onClose={onClose}
      width={520}
      footer={
        <FormActions>
          <button type="button" className="btn-secondary" onClick={onClose} disabled={busy}>
            取消
          </button>
          <button type="button" className="btn-primary" onClick={onSubmit} disabled={busy}>
            {busy ? "提交中…" : "确认已服"}
          </button>
        </FormActions>
      }
    >
      {error ? <ErrorBanner message={error} /> : null}
      {target ? (
        <div style={{ marginBottom: 12 }}>
          <p>
            计划时刻：<strong>{timeText(target.scheduled_at)}</strong>
          </p>
          <p className="cell-muted">
            已触达 {target.retry_count} 次 · 当前状态 {eventStatusLabel(target.status)}
          </p>
          <Notice style={{ marginTop: 8 }}>
            代确认等于替长者作证「已服药」，会留审计（渠道记为照护台按钮）。请核实后再点。
          </Notice>
        </div>
      ) : null}
      <Field label="备注（可选）" htmlFor="confirm-note" hint="如「家属来电确认，长者已服」">
        <input
          id="confirm-note"
          className="input"
          value={note}
          maxLength={200}
          onChange={(event) => onNoteChange(event.target.value)}
        />
      </Field>
    </Dialog>
  );
}