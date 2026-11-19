/**
 * 新建 / 修改药单弹窗（从 MedicationsPage 抽出，前端计划 W4）。
 *
 * 口径不变：
 * - 改档必须写明「保存后版本号 +1，历史事件仍显示当时快照」，否则护工以为改的是历史；
 * - 乐观锁冲突由服务端 409 兜底，文案提示「刷新后重新编辑」，前端不自己合并；
 * - 停用日留空 = 长期有效，由医生评估后填写；
 * - 前端只挡「明显不可能」的输入（见 medications/model.validateCreate），
 *   格式与窗口先后仍由服务端 422 兜底。
 */
import { Dialog } from "../ui/Dialog";
import { ErrorBanner } from "../ui/Feedback";
import { Field, FormActions, FormGrid } from "../ui/Form";
import { Notice } from "../../shared/ui/Notice";
import type { MedicationPlanItem } from "../medications";
import type { CreateErrors, CreateForm } from "./model";

export interface PlanFormDialogProps {
  open: boolean;
  /** 非空 = 改档；空 = 新建。 */
  editing: MedicationPlanItem | null;
  form: CreateForm;
  errors: CreateErrors;
  submitting: boolean;
  submitError: string | null;
  onChange: (form: CreateForm) => void;
  onClose: () => void;
  onSubmit: () => void;
}

export function PlanFormDialog({
  open,
  editing,
  form,
  errors,
  submitting,
  submitError,
  onChange,
  onClose,
  onSubmit,
}: PlanFormDialogProps) {
  return (
    <Dialog
      open={open}
      title={editing ? "修改药单" : "新建药单"}
      onClose={onClose}
      footer={
        <FormActions>
          <button type="button" className="btn-secondary" onClick={onClose} disabled={submitting}>
            取消
          </button>
          <button type="button" className="btn-primary" onClick={onSubmit} disabled={submitting}>
            {submitting ? "保存中…" : "保存"}
          </button>
        </FormActions>
      }
    >
      {submitError ? <ErrorBanner message={submitError} /> : null}
      {editing ? (
        <Notice style={{ marginBottom: 12 }}>
          正在修改 <strong>{editing.drug_name}</strong>（当前 v{editing.version}）：保存后变为 v
          {editing.version + 1}，历史提醒事件仍显示当时的药名与剂量快照。
        </Notice>
      ) : null}
      <FormGrid>
        <Field label="药名" htmlFor="drug-name" required error={errors.drug_name}>
          <input
            id="drug-name"
            className="input"
            value={form.drug_name}
            maxLength={100}
            placeholder="如：氨氯地平"
            aria-invalid={errors.drug_name ? true : undefined}
            onChange={(event) => onChange({ ...form, drug_name: event.target.value })}
          />
        </Field>
        <Field label="剂量" htmlFor="dose" required error={errors.dose} hint="如「1 片」「5mg」">
          <input
            id="dose"
            className="input"
            value={form.dose}
            maxLength={50}
            placeholder="5mg"
            aria-invalid={errors.dose ? true : undefined}
            onChange={(event) => onChange({ ...form, dose: event.target.value })}
          />
        </Field>
        <Field
          label="每日服药时刻"
          htmlFor="schedule"
          required
          error={errors.schedule_cron}
          hint="24 小时制，如 08:00"
        >
          <input
            id="schedule"
            className="input"
            type="time"
            value={form.schedule_cron}
            aria-invalid={errors.schedule_cron ? true : undefined}
            onChange={(event) => onChange({ ...form, schedule_cron: event.target.value })}
          />
        </Field>
        <Field label="起始日" htmlFor="start-date" required error={errors.start_date}>
          <input
            id="start-date"
            className="input"
            type="date"
            value={form.start_date}
            aria-invalid={errors.start_date ? true : undefined}
            onChange={(event) => onChange({ ...form, start_date: event.target.value })}
          />
        </Field>
        <Field
          label="停用日"
          htmlFor="end-date"
          error={errors.end_date}
          hint="留空表示长期有效；由医生评估后填写"
        >
          <input
            id="end-date"
            className="input"
            type="date"
            value={form.end_date}
            aria-invalid={errors.end_date ? true : undefined}
            onChange={(event) => onChange({ ...form, end_date: event.target.value })}
          />
        </Field>
      </FormGrid>
      <p className="field-hint" style={{ marginTop: 12 }}>
        {editing
          ? "保存即记一次修改并令版本号自增；若药单已被他人改过，服务端返回 409，请刷新后重新编辑。"
          : "保存后可在药单行尾点「改」调整药名/剂量/时刻，或点「停」停用；历史提醒事件保留当时的快照。"}
      </p>
    </Dialog>
  );
}