/**
 * 批量导入药单向导（从 MedicationsPage 抽出，前端计划 W4）。
 *
 * 两步式口径不变（FR-MED-07）：**先预检只读不写库，看过逐行结果再确认导入**；
 * 预检通过前「确认导入」不可点；服务端全量预检，任一行不合法整批不写。
 * 失败行必须带行号 + 字段 + 原因，护工才能回 Excel 精准改那一格。
 *
 * 三张结果表的列配置只在这里用，随组件一起搬过来，页面不再持有它们。
 */
import type { RefObject } from "react";
import { Dialog } from "../ui/Dialog";
import { Badge, ErrorBanner } from "../ui/Feedback";
import { Field, FormActions, FormGrid } from "../ui/Form";
import { DataTable, type Column } from "../ui/Table";
import { Notice } from "../../shared/ui/Notice";
import {
  windowText,
  type MedicationImportIssue,
  type MedicationImportResult,
  type MedicationImportRow,
  type MedicationImportSkip,
} from "../medications";

const planColumns: Column<MedicationImportRow>[] = [
  { key: "line", title: "行号", width: "70px", render: (row) => row.line || "—" },
  { key: "elder_name", title: "长者", width: "14%", render: (row) => row.elder_name || "—" },
  { key: "drug_name", title: "药名" },
  { key: "dose", title: "剂量", width: "14%" },
  { key: "schedule_cron", title: "每日时刻", width: "12%" },
  {
    key: "window",
    title: "服药区间",
    render: (row) => <span className="cell-muted">{windowText(row.start_date, row.end_date)}</span>,
  },
  {
    key: "state",
    title: "状态",
    width: "10%",
    render: (row) =>
      row.plan_id ? <Badge tone="success">已创建</Badge> : <Badge tone="neutral">待创建</Badge>,
  },
];

const errorColumns: Column<MedicationImportIssue>[] = [
  {
    key: "line",
    title: "行号",
    width: "80px",
    render: (row) => (row.line === 0 ? "整文件" : `${row.line}`),
  },
  { key: "field", title: "字段", width: "18%", render: (row) => row.field },
  { key: "message", title: "原因", render: (row) => row.message },
];

const skippedColumns: Column<MedicationImportSkip>[] = [
  { key: "line", title: "行号", width: "80px", render: (row) => `${row.line}` },
  { key: "drug_name", title: "药名", width: "22%" },
  { key: "message", title: "跳过原因", render: (row) => row.message },
];

export interface ImportWizardProps {
  open: boolean;
  busy: boolean;
  error: string | null;
  file: File | null;
  /** 已写入的结果优先于预检结果（页面侧 `importApplied ?? importPreview`）。 */
  result: MedicationImportResult | null;
  applied: boolean;
  inputRef: RefObject<HTMLInputElement | null>;
  onClose: () => void;
  onPickFile: (file: File | null) => void;
  onPreview: () => void;
  onApply: () => void;
  onDownloadTemplate: () => void;
}

export function ImportWizard({
  open,
  busy,
  error,
  file,
  result,
  applied,
  inputRef,
  onClose,
  onPickFile,
  onPreview,
  onApply,
  onDownloadTemplate,
}: ImportWizardProps) {
  return (
    <Dialog
      open={open}
      title="批量导入药单"
      onClose={onClose}
      width={760}
      footer={
        <FormActions>
          <button type="button" className="btn-secondary" onClick={onClose} disabled={busy}>
            关闭
          </button>
          {applied ? (
            <span className="field-hint">这批已写入；关闭弹窗即可，重复点不会二次落库。</span>
          ) : (
            <>
              {/* 两个按钮文案刻意不同：前者只读预检，后者才写库，防手滑直接落 200 条。 */}
              <button
                type="button"
                className="btn-secondary"
                onClick={onPreview}
                disabled={busy || !file}
              >
                {busy ? "处理中…" : "先看看会怎样（预检）"}
              </button>
              <button
                type="button"
                className="btn-primary"
                onClick={onApply}
                disabled={busy || !result?.accepted}
              >
                {busy ? "处理中…" : "确认导入"}
              </button>
            </>
          )}
        </FormActions>
      }
    >
      {error ? <ErrorBanner message={error} /> : null}

      <Notice style={{ marginBottom: 12 }}>
        支持 CSV / XLSX，单次最多 200 行、5MB 以内。服务端先做全量预检：
        <strong>任一行不合法就整批不写</strong>，失败行的行号、字段与原因会在下方列出，改完再导一次即可。
      </Notice>

      <FormGrid>
        <Field
          label="药单文件"
          htmlFor="med-import-file"
          required
          hint="列：长者姓名、药名、剂量、每日服药时刻（HH:MM）、起始日、停用日（可空）"
        >
          <input
            id="med-import-file"
            ref={inputRef}
            className="input"
            type="file"
            accept=".csv,.xlsx,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            onChange={(event) => onPickFile(event.target.files?.[0] ?? null)}
          />
        </Field>
      </FormGrid>

      <div
        style={{
          display: "flex",
          gap: 16,
          alignItems: "center",
          margin: "8px 0 12px",
          flexWrap: "wrap",
        }}
      >
        <button type="button" className="btn-secondary btn-sm" onClick={onDownloadTemplate}>
          下载模板
        </button>
        <span className="field-hint">
          导入分两步：先「预检」只读不写库，看过逐行结果再点「确认导入」；预检通过前确认按钮不可点。
        </span>
      </div>

      {result ? (
        <>
          <Notice role="status" style={{ marginBottom: 12 }}>
            {result.accepted
              ? result.dry_run
                ? `预检通过：共 ${result.total_rows} 行，将新建 ${result.will_create} 条药单` +
                  (result.skipped.length > 0 ? `，跳过重复 ${result.skipped.length} 条。` : "。")
                : `导入完成：新建 ${result.created.length} 条药单` +
                  (result.skipped.length > 0 ? `，跳过重复 ${result.skipped.length} 条。` : "。")
              : `预检未通过：共 ${result.total_rows} 行，${result.errors.length} 处问题，整批未写入。`}
          </Notice>
          {!result.accepted ? (
            <p className="field-hint" style={{ marginBottom: 12 }}>
              整批未写入：按下面的行号把那一格改好后重新选文件、再预检一次；预检通过前「确认导入」始终不可点。
            </p>
          ) : null}

          {result.errors.length > 0 ? (
            <div style={{ marginBottom: 16 }}>
              <h3 style={{ fontSize: 15, margin: "0 0 8px" }}>失败行（需修正后才能导入）</h3>
              <DataTable<MedicationImportIssue>
                columns={errorColumns}
                rows={result.errors}
                rowKey={(row) => `${row.line}-${row.field}-${row.message}`}
                caption="导入失败行"
                emptyTitle="没有失败行"
              />
            </div>
          ) : null}

          {result.created.length > 0 ? (
            <div style={{ marginBottom: 16 }}>
              <h3 style={{ fontSize: 15, margin: "0 0 8px" }}>
                {result.dry_run ? "将创建的药单" : "已创建的药单"}
              </h3>
              <DataTable<MedicationImportRow>
                columns={planColumns}
                rows={result.created}
                rowKey={(row) => `${row.line}-${row.plan_id ?? "pending"}`}
                caption="导入药单明细"
                emptyTitle="没有可创建的药单"
              />
            </div>
          ) : null}

          {result.skipped.length > 0 ? (
            <div>
              <h3 style={{ fontSize: 15, margin: "0 0 8px" }}>跳过的重复行</h3>
              <DataTable<MedicationImportSkip>
                columns={skippedColumns}
                rows={result.skipped}
                rowKey={(row) => `${row.line}-${row.reason}`}
                caption="导入跳过行"
                emptyTitle="没有重复行"
              />
            </div>
          ) : null}
        </>
      ) : null}
    </Dialog>
  );
}