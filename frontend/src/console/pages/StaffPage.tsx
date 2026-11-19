/**
 * 员工账号页（W3）：列表（角色筛选 + 分页）+ 建号 + 改档。
 *
 * 三条边界，别越界：
 * 1. 读接口（W4 `GET /api/staff`）还没上线时，列表区**显式**说「读接口未就绪」，
 *    不伪装成空数据；写操作（POST / PATCH）已在线，读不可用不影响建号与改档。
 * 2. 写操作只对 `can(principal, "staff:write")` 的角色渲染按钮；前端判定不构成权限，
 *    服务端每个写端点仍然独立校验（PRD FR-ADM-03 / AC-18）。
 * 3. 409 / 422 / 403 一律用 `errorText` 落成弹窗顶部的一行中文，**不清空已填内容**——
 *    管理员刚敲完 6 个字段，不能因为手机号重复就让他重填。
 *
 * 后端当前只支持 `PATCH {role, status}`（`app/api/schemas/staff.py::StaffUpdateRequest`），
 * 因此编辑弹窗把姓名 / 手机号 / 负责长者做成**只读**并明写原因，不做「改了但没保存」的假象。
 */
import { useEffect, useState } from "react";
import type { Principal } from "../api";
import { errorText } from "../http";
import { can } from "../permissions";
import {
  STAFF_ROLE_OPTIONS,
  STAFF_ROLES,
  STAFF_STATUS_OPTIONS,
  createStaff,
  elderOptionLabel,
  isEndpointMissing,
  listElderOptions,
  listStaff,
  staffRoleLabel,
  updateStaff,
  type ElderOption,
  type StaffRole,
  type StaffRow,
  type StaffStatus,
} from "../staff";
import { useListResource } from "../../shared/useListResource";
import { AccountStatusBadge, EmptyState, ErrorBanner } from "../ui/Feedback";
import { Notice } from "../../shared/ui/Notice";
import { DataTable, type Column } from "../ui/Table";
import { Pagination } from "../ui/Pagination";
import { Dialog } from "../ui/Dialog";
import { Field, FormActions, FormGrid, Select } from "../ui/Form";

const PAGE_SIZE = 20;

const ROLE_FILTER_OPTIONS = [{ value: "", label: "全部角色" }, ...STAFF_ROLE_OPTIONS];

/** 列表区四态：读接口没上线是独立一态，不是空列表。 */
const UNAVAILABLE_MESSAGE =
  "读接口未就绪：W4 的 GET /api/staff 尚未上线（task-28 并行开发中）。新建员工与改档不受影响，可先用右上角按钮。";

function asStaffRole(role: string): StaffRole {
  return role === "admin" || role === "care_worker" || role === "doctor" ? role : "care_worker";
}

export function StaffPage({ principal }: { principal: Principal }) {
  const canWrite = can(principal, "staff:write");

  const [page, setPage] = useState(1);
  const [role, setRole] = useState<StaffRole | "">("");
  const [notice, setNotice] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [editing, setEditing] = useState<StaffRow | null>(null);

  // 列表骨架收敛到 shared/useListResource（W4）。`alive` 竞态取消由 hook 保证，
  // StrictMode 双挂载与快速切页都不会用旧响应覆盖新状态。
  const { rows, total, state, reload } = useListResource<StaffRow>(
    () => listStaff({ page, pageSize: PAGE_SIZE, role }),
    [page, role],
    { errorText, isUnavailable: isEndpointMissing, unavailableMessage: UNAVAILABLE_MESSAGE },
  );

  const columns: Column<StaffRow>[] = [
    { key: "name", title: "姓名" },
    {
      key: "role",
      title: "角色",
      render: (row) => (row.role ? staffRoleLabel(row.role) : "—"),
    },
    {
      key: "phone_masked",
      title: "手机号",
      render: (row) => <span className="cell-muted">{row.phone_masked || "—"}</span>,
    },
    {
      key: "status",
      title: "状态",
      render: (row) =>
        row.status ? <AccountStatusBadge status={row.status} /> : <span className="cell-muted">—</span>,
    },
    {
      key: "elder_ids",
      title: "负责长者",
      render: (row) =>
        row.elder_ids.length > 0 ? (
          `${row.elder_ids.length} 位`
        ) : (
          <span className="cell-muted">未分配</span>
        ),
    },
  ];

  if (canWrite) {
    columns.push({
      key: "actions",
      title: "操作",
      align: "right",
      render: (row) => (
        <button type="button" className="btn-link" onClick={() => setEditing(row)}>
          改档
        </button>
      ),
    });
  }

  return (
    <section>
      <header className="page-head">
        <h1 className="page-title">员工账号</h1>
        <p className="page-sub">
          {principal.name}
          ，建号与改档仅管理员可用 · 手机号在员工与家属之间全局唯一
        </p>
        <div className="page-actions">
          {canWrite ? (
            <button type="button" className="btn-primary" onClick={() => setCreateOpen(true)}>
              新建员工
            </button>
          ) : null}
        </div>
      </header>

      {notice ? (
        <Notice role="status">
          {notice}
        </Notice>
      ) : null}

      {state.kind === "unavailable" ? (
        // 显式「未就绪」：不是空态，也不是错误——W4 落地后这一屏自然消失。
        <EmptyState
          title="读接口未就绪"
          description={state.message}
          action={
            <button type="button" className="btn-secondary btn-sm" onClick={reload}>
              重新检查
            </button>
          }
        />
      ) : (
        <>
          <div className="toolbar">
            <Field label="角色筛选">
              <Select
                value={role}
                options={ROLE_FILTER_OPTIONS}
                onChange={(value) => {
                  setPage(1);
                  setRole(value as StaffRole | "");
                }}
              />
            </Field>
            <button
              type="button"
              className="btn-secondary btn-sm"
              onClick={reload}
              disabled={state.kind === "loading"}
            >
              刷新
            </button>
          </div>

          <DataTable
            columns={columns}
            rows={rows}
            rowKey={(row) => row.staff_id || row.name}
            loading={state.kind === "loading"}
            error={state.kind === "error" ? state.message : null}
            onRetry={reload}
            emptyTitle="还没有员工账号"
            emptyDescription={
              canWrite ? "点右上角「新建员工」创建第一个账号。" : "当前筛选条件下没有员工。"
            }
            caption="员工账号列表"
          />

          {state.kind === "ready" && total > 0 ? (
            <Pagination page={page} pageSize={PAGE_SIZE} total={total} onChange={setPage} />
          ) : null}
        </>
      )}

      <StaffCreateDialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onSaved={(message) => {
          setNotice(message);
          reload();
        }}
      />

      {editing ? (
        <StaffEditDialog
          staff={editing}
          onClose={() => setEditing(null)}
          onSaved={(message) => {
            setNotice(message);
            reload();
          }}
        />
      ) : null}
    </section>
  );
}

// ---------------------------------------------------------------- 建号弹窗

function StaffCreateDialog({
  open,
  onClose,
  onSaved,
}: {
  open: boolean;
  onClose: () => void;
  onSaved: (message: string) => void;
}) {
  const [name, setName] = useState("");
  const [role, setRole] = useState<StaffRole>("care_worker");
  const [phone, setPhone] = useState("");
  const [password, setPassword] = useState("");
  const [elderIds, setElderIds] = useState<string[]>([]);
  const [elders, setElders] = useState<ElderOption[] | null>(null);
  const [eldersLoading, setEldersLoading] = useState(false);
  const [eldersError, setEldersError] = useState<string | null>(null);
  const [elderFilter, setElderFilter] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // 每次打开都是空白表单：关掉再开不该残留上一位的手机号与口令。
  useEffect(() => {
    if (!open) return;
    setName("");
    setRole("care_worker");
    setPhone("");
    setPassword("");
    setElderIds([]);
    setElderFilter("");
    setError(null);
    setSaving(false);
  }, [open]);

  // 长者下拉只在第一次打开时拉一次，之后复用（同一批在住长者，没必要每次重拉）。
  useEffect(() => {
    if (!open || elders !== null) return;
    let alive = true;
    setEldersLoading(true);
    setEldersError(null);
    listElderOptions()
      .then((items) => {
        if (alive) setElders(items);
      })
      .catch((err: unknown) => {
        if (alive) setEldersError(errorText(err));
      })
      .finally(() => {
        if (alive) setEldersLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [open, elders]);

  const toggleElder = (elderId: string) => {
    setElderIds((current) =>
      current.includes(elderId) ? current.filter((id) => id !== elderId) : [...current, elderId],
    );
  };

  const visibleElders = (elders ?? []).filter((elder) => {
    const keyword = elderFilter.trim();
    if (!keyword) return true;
    const text = `${elder.name} ${elder.room_no} ${elder.bed_no}`;
    return text.includes(keyword);
  });

  const validate = (): string | null => {
    if (!name.trim()) return "请填写姓名";
    if (name.trim().length > 64) return "姓名最多 64 个字";
    if (!/^1\d{10}$/.test(phone)) return "手机号需为 11 位数字且以 1 开头";
    if (password.length < 8) return "初始口令至少 8 位";
    if (role === "admin" && elderIds.length > 0) return "管理员不绑定长者，请取消已选长者";
    return null;
  };

  const submit = async () => {
    const invalid = validate();
    if (invalid) {
      setError(invalid);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const created = await createStaff({
        name: name.trim(),
        role,
        phone,
        password,
        elder_ids: role === "admin" ? [] : elderIds,
      });
      onSaved(
        `已创建员工「${created.name || name.trim()}」· ${staffRoleLabel(created.role || role)} · ${
          created.phone_masked || "手机号未回显"
        }。初始口令请在本次交付中当面转交。`,
      );
      onClose();
    } catch (err) {
      // 409 手机号重复 / 422 格式 / 403 角色不允许：只加一行提示，已填内容原样保留。
      setError(errorText(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog
      open={open}
      title="新建员工"
      onClose={onClose}
      width={620}
      footer={
        <FormActions>
          <button type="button" className="btn-secondary" onClick={onClose} disabled={saving}>
            取消
          </button>
          <button type="button" className="btn-primary" onClick={() => void submit()} disabled={saving}>
            {saving ? "创建中…" : "创建员工"}
          </button>
        </FormActions>
      }
    >
      {error ? (
        <ErrorBanner message={error} />
      ) : null}

      <FormGrid>
        <Field label="姓名" htmlFor="staff-name" required>
          <input
            id="staff-name"
            className="input"
            value={name}
            maxLength={64}
            onChange={(event) => setName(event.target.value)}
            placeholder="如：王秀兰"
          />
        </Field>

        <Field label="角色" htmlFor="staff-role" required hint="管理员不绑定长者；护工与医生需要指定负责长者。">
          <Select
            id="staff-role"
            value={role}
            options={STAFF_ROLE_OPTIONS}
            onChange={(value) => {
              const next = asStaffRole(value);
              setRole(next);
              if (next === "admin") setElderIds([]);
            }}
          />
        </Field>

        <Field label="手机号" htmlFor="staff-phone" required hint="11 位，全局唯一（重复会提示冲突）。">
          <input
            id="staff-phone"
            className="input"
            value={phone}
            inputMode="numeric"
            maxLength={11}
            onChange={(event) => setPhone(event.target.value.replace(/\D/g, ""))}
            placeholder="13800000000"
          />
        </Field>

        <Field label="初始口令" htmlFor="staff-password" required hint="至少 8 位；请当面转交，勿写入工单。">
          <input
            id="staff-password"
            className="input"
            type="password"
            value={password}
            maxLength={128}
            onChange={(event) => setPassword(event.target.value)}
            placeholder="至少 8 位"
          />
        </Field>

        <div className="field-span-2">
          <Field
            label="负责长者"
            required={role !== "admin"}
            hint="可多选；只列出前 100 位在住长者。建号后如需调整，当前后端 PATCH 尚不支持（见页面提示）。"
          >
            {role === "admin" ? (
              <p className="field-hint">管理员按角色划分权限，不绑定具体长者。</p>
            ) : eldersLoading ? (
              <p className="field-hint">正在加载长者…</p>
            ) : eldersError ? (
              <ErrorBanner
                message={eldersError ?? "读取长者列表失败，请重试。"}
                onRetry={() => {
                  setElders(null);
                  setEldersError(null);
                }}
              />
            ) : (
              <>
                <input
                  className="input"
                  value={elderFilter}
                  onChange={(event) => setElderFilter(event.target.value)}
                  placeholder="按姓名 / 房间号筛选"
                  aria-label="筛选长者"
                />
                <div
                  style={{
                    marginTop: 8,
                    maxHeight: 180,
                    overflowY: "auto",
                    border: "1px solid var(--color-line)",
                    borderRadius: 8,
                    padding: "6px 10px",
                    background: "var(--color-surface)",
                  }}
                >
                  {visibleElders.length === 0 ? (
                    <p className="field-hint">没有匹配的长者</p>
                  ) : (
                    visibleElders.map((elder) => (
                      <label
                        key={elder.elder_id}
                        style={{
                          display: "flex",
                          gap: 8,
                          alignItems: "center",
                          padding: "3px 0",
                          cursor: "pointer",
                        }}
                      >
                        <input
                          type="checkbox"
                          checked={elderIds.includes(elder.elder_id)}
                          onChange={() => toggleElder(elder.elder_id)}
                        />
                        <span>{elderOptionLabel(elder)}</span>
                      </label>
                    ))
                  )}
                </div>
                <p className="field-hint">已选 {elderIds.length} 位</p>
              </>
            )}
          </Field>
        </div>
      </FormGrid>
    </Dialog>
  );
}

// ---------------------------------------------------------------- 改档弹窗

function StaffEditDialog({
  staff,
  onClose,
  onSaved,
}: {
  staff: StaffRow;
  onClose: () => void;
  onSaved: (message: string) => void;
}) {
  const [role, setRole] = useState<StaffRole>(asStaffRole(staff.role));
  const [status, setStatus] = useState<StaffStatus>(staff.status === "disabled" ? "disabled" : "active");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const unchanged = role === staff.role && status === staff.status;

  const submit = async () => {
    setSaving(true);
    setError(null);
    try {
      await updateStaff(staff.staff_id, { role, status });
      onSaved(
        `已更新「${staff.name}」的角色 / 状态。该账号旧登录态已失效（AC-24），本人需重新登录。`,
      );
      onClose();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog
      open
      title={`改档 · ${staff.name}`}
      onClose={onClose}
      width={560}
      footer={
        <FormActions>
          <button type="button" className="btn-secondary" onClick={onClose} disabled={saving}>
            取消
          </button>
          <button
            type="button"
            className="btn-primary"
            onClick={() => void submit()}
            disabled={saving || unchanged}
          >
            {saving ? "保存中…" : "保存"}
          </button>
        </FormActions>
      }
    >
      {error ? (
        <ErrorBanner message={error} />
      ) : null}

      <FormGrid>
        <Field label="角色" htmlFor="edit-role" required>
          <Select
            id="edit-role"
            value={role}
            options={STAFF_ROLE_OPTIONS}
            onChange={(value) => setRole(asStaffRole(value))}
          />
        </Field>

        <Field label="状态" htmlFor="edit-status" required hint="停用后该账号立即无法登录，但历史记录保留。">
          <Select
            id="edit-status"
            value={status}
            options={STAFF_STATUS_OPTIONS}
            onChange={(value) => setStatus(value === "disabled" ? "disabled" : "active")}
          />
        </Field>

        {role === "admin" && staff.role !== "admin" ? (
          // 裁决 D-01：后端在 PATCH role=admin 时清掉该员工全部 elder_staff_assignments。
          // 弹窗里必须提前说清，否则管理员会以为「保存只是改了个头衔」，回到列表才发现
          // 负责长者被清空。只在**目标角色是 admin**时提示，避免护工/医生互改时误报。
          <div className="field-span-2">
            <Notice role="status">
              改为管理员将清除其负责长者，之后需重新分配。
            </Notice>
          </div>
        ) : null}

        <div className="field-span-2">
          {/* 这三个字段后端 PATCH 还不接受，做成只读展示而不是可编辑：宁可少一个输入框，
              也不让管理员以为「改完保存了」。差异已报产品负责人。 */}
          <Field label="姓名 / 手机号 / 负责长者" hint="当前后端 PATCH 只接受角色与状态（app/api/schemas/staff.py），这三项暂不可改。">
            <input
              className="input"
              value={`${staff.name} · ${staff.phone_masked || "手机号未回显"} · 负责 ${
                staff.elder_ids.length
              } 位长者`}
              readOnly
              disabled
            />
          </Field>
        </div>
      </FormGrid>
    </Dialog>
  );
}

/** 角色闭集给筛选器复用（`STAFF_ROLES` 与文案表同源，避免两处漂移）。 */
export const STAFF_ROLE_KEYS: readonly StaffRole[] = STAFF_ROLES;
