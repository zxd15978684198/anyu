/**
 * 工单看板页（迭代 8 · task-11）：未处理工单列表 + 详情抽屉 + 按状态机流转。
 *
 * 四条边界（都来自计划件 `设计记录（工单看板补齐）` B3/B4）：
 * 1. **排序与筛选都不在前端做**：红档置顶是服务端 SQL 的
 *    `case((WorkOrder.severity == "red", 0), else_=1)` 再 `created_at ASC`，分页时
 *    前端重排只能改当前页，第 2 页的红档永远上不来。页面只负责传 status/severity。
 * 2. **流转按钮按状态机渲染**：可去的状态取自 `workOrders.ts::nextStatuses`（与
 *    `work_order_service.ALLOWED_TRANSITIONS` 同源）。不合法的一律不渲染，
 *    不让用户点了才吃 409。
 * 3. **关单三条必填**：非误报必填处置结果 `note`；勾「误报」后改为必填
 *    `false_alarm_reason`（此时 note 可空）；红档还要 `actual_handled_at`。
 *    这三条与服务端 `WorkOrderValidationError` 一一对应，前端先拦一遍只为省一次往返。
 * 4. **重开只给管理员、且必填原因**：服务端对非 admin 直接 `RoleForbiddenError`。
 *    按钮用 `isAdmin(principal)` 过滤——但记住前端判定不构成权限，服务端仍会校验。
 *
 * 「误报」不是状态：它就是「关闭」的一个附加标记，因此它只出现在关单弹窗里，
 * 列表的状态筛选里没有、也不该有「误报」这一项。
 */
import { useEffect, useState, type ReactNode } from "react";
import type { Principal } from "../api";
import { errorText } from "../http";
import { isAdmin, can } from "../permissions";
import { Badge, EmptyState, ErrorBanner } from "../ui/Feedback";
import { Notice } from "../../shared/ui/Notice";
import { DataTable, type Column } from "../ui/Table";
import { Pagination } from "../ui/Pagination";
import { Dialog } from "../ui/Dialog";
import { Field, FormActions, FormGrid, Select } from "../ui/Form";
import { isEndpointMissing } from "../staff";
import { useListResource } from "../../shared/useListResource";
import {
  ackRiskEvent,
  formatDateTime,
  getWorkOrder,
  listWorkOrders,
  nextStatuses,
  requiresHandledAt,
  severityText,
  severityTone,
  statusText,
  statusTone,
  transitionText,
  transitionWorkOrder,
  typeText,
  WORK_ORDER_STATUSES,
  type WorkOrderDetail,
  type WorkOrderRow,
  type WorkOrderStatus,
} from "../workOrders";

const PAGE_SIZE = 20;

/** `""` = 未处理四态（服务端默认），不是「全部」。 */
const STATUS_FILTER_OPTIONS = [
  { value: "", label: "未处理（默认）" },
  ...WORK_ORDER_STATUSES.map((status) => ({ value: status, label: statusText(status) })),
];

const SEVERITY_FILTER_OPTIONS = [
  { value: "", label: "全部等级" },
  { value: "red", label: "红档" },
  { value: "yellow", label: "黄档" },
];

const UNAVAILABLE_MESSAGE =
  "读接口不可用：本次 GET /api/work-orders 没有返回数据。常见原因是后端服务未启动或网络中断——请先确认服务在运行，再点「重新检查」。";

export function WorkOrdersPage({ principal }: { principal: Principal }) {
  // 三个角色（admin / care_worker / doctor）都能流转；重开另看 isAdmin。
  const admin = isAdmin(principal);
  const canRead = can(principal, "workorder:read");

  const [page, setPageNumber] = useState(1);
  const [status, setStatus] = useState<WorkOrderStatus | "">("");
  const [severity, setSeverity] = useState<"red" | "yellow" | "">("");
  const [notice, setNotice] = useState<string | null>(null);

  const [detailId, setDetailId] = useState<string | null>(null);
  const [detail, setDetail] = useState<WorkOrderDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // 工单详情不带 acknowledged_at；确认成功后只在本次会话里记住时间。
  const [acknowledgedAt, setAcknowledgedAt] = useState<string | null>(null);

  // 关单 / 重开 / 风险确认要在详情之上再开一层：两个 Dialog 同时挂载时 Esc 会双双关闭，
  // 所以下面把详情 Dialog 的 open 压成 `!actionOpen`。
  const [actionOpen, setActionOpen] = useState<"ack" | "close" | "reopen" | null>(null);

  // 列表骨架收敛到 shared/useListResource（W4）。reloadKey 仍要取出：
  // 详情抽屉的 effect 依赖它，关单/重开后列表与详情要一起刷新。
  const { rows, total, state, reload, reloadKey } = useListResource<WorkOrderRow>(
    () => listWorkOrders({ page, pageSize: PAGE_SIZE, status, severity }),
    [page, status, severity],
    { errorText, isUnavailable: isEndpointMissing, unavailableMessage: UNAVAILABLE_MESSAGE },
  );

  // 详情抽屉：每次打开都重新拉，不从列表行就地拼——列表项没有 transitions，
  // 而「完整状态变更历史」正是本页验收项（FR-ALT-02）。
  useEffect(() => {
    if (!detailId) {
      setDetail(null);
      setDetailError(null);
      return;
    }
    let alive = true;
    setDetailLoading(true);
    setDetailError(null);
    getWorkOrder(detailId)
      .then((item) => {
        if (!alive) return;
        setDetail(item);
      })
      .catch((err: unknown) => {
        if (alive) setDetailError(errorText(err));
      })
      .finally(() => {
        if (alive) setDetailLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [detailId, reloadKey]);

  const openDetail = (row: WorkOrderRow) => {
    setActionError(null);
    setActionOpen(null);
    setAcknowledgedAt(null);
    setDetailId(row.order_id);
  };

  const closeDetail = () => {
    setDetailId(null);
    setDetail(null);
    setActionOpen(null);
    setAcknowledgedAt(null);
  };

  /** 单步流转（接单 / 开始处理 / 标记解决）：无需附注，直接走。 */
  const runStep = async (order: WorkOrderDetail, to: WorkOrderStatus) => {
    setBusy(true);
    setActionError(null);
    try {
      const updated = await transitionWorkOrder(order.order_id, { to_status: to });
      setDetail(updated);
      setNotice(`工单 ${updated.order_no} 已${transitionText(to)}`);
      reload();
    } catch (err) {
      setActionError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const columns: Column<WorkOrderRow>[] = [
    {
      key: "order_no",
      title: "单号",
      render: (row) => (
        <span style={{ fontFamily: "ui-monospace, monospace" }} title={row.order_id}>
          {row.order_no || row.order_id.slice(0, 8)}
        </span>
      ),
    },
    {
      key: "elder_name",
      title: "长者",
      // 后端已经脱敏（姓 + * + 末字），这里原样显示，不再拼房间号（列表不返回房间）。
      render: (row) => row.elder_name || <span className="cell-muted">—</span>,
    },
    { key: "type", title: "类型", render: (row) => typeText(row.type) },
    {
      key: "severity",
      title: "等级",
      render: (row) => (
        <Badge tone={severityTone(row.severity)}>
          {severityText(row.severity)}
          {row.severity === "red" ? " · 须立即人工" : ""}
        </Badge>
      ),
    },
    {
      key: "status",
      title: "状态",
      render: (row) => (
        <>
          <Badge tone={statusTone(row.status)}>{statusText(row.status)}</Badge>
          {row.false_alarm ? (
            <>
              {" "}
              <Badge tone="neutral">误报</Badge>
            </>
          ) : null}
        </>
      ),
    },
    {
      key: "assignee_name",
      title: "责任人",
      render: (row) =>
        row.assignee_name ? row.assignee_name : <span className="cell-muted">未指派</span>,
    },
    {
      key: "created_at",
      title: "创建时间",
      render: (row) => <span className="cell-muted">{formatDateTime(row.created_at)}</span>,
    },
    {
      key: "actions",
      title: "操作",
      align: "right",
      render: (row) => (
        <button type="button" className="btn-link" onClick={() => openDetail(row)}>
          详情 / 流转
        </button>
      ),
    },
  ];

  const emptyTitle = status || severity ? "没有匹配的工单" : "没有待处理工单";
  const emptyDescription =
    status || severity
      ? "换一个筛选条件试试；「未处理」只看 new / acknowledged / in_progress / reopened 四态。"
      : "当前范围内没有待接单、待处理或已重开的工单。";

  return (
    <section>
      <header className="page-head">
        <h1 className="page-title">工单看板</h1>
        <p className="page-sub">
          {principal.name}
          ，默认只看未处理四态 · 红档由服务端置顶（分页也置顶）· 流转按钮按当前状态给出
        </p>
        <div className="page-actions">
          <button
            type="button"
            className="btn-secondary btn-sm"
            onClick={reload}
            disabled={state.kind === "loading"}
          >
            刷新
          </button>
        </div>
      </header>

      {notice ? (
        <Notice role="status">
          {notice}
        </Notice>
      ) : null}

      {!canRead ? (
        <EmptyState title="当前角色无权访问" description="请从左侧导航进入其他页面。" />
      ) : state.kind === "unavailable" ? (
        <EmptyState
          title="读接口不可用"
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
            <Field label="工单状态">
              <Select
                value={status}
                options={STATUS_FILTER_OPTIONS}
                onChange={(value) => {
                  setPageNumber(1);
                  setStatus(value as WorkOrderStatus | "");
                }}
              />
            </Field>
            <Field label="告警等级">
              <Select
                value={severity}
                options={SEVERITY_FILTER_OPTIONS}
                onChange={(value) => {
                  setPageNumber(1);
                  setSeverity(value === "red" ? "red" : value === "yellow" ? "yellow" : "");
                }}
              />
            </Field>
          </div>

          <DataTable
            columns={columns}
            rows={rows}
            rowKey={(row) => row.order_id}
            loading={state.kind === "loading"}
            error={state.kind === "error" ? state.message : null}
            onRetry={reload}
            emptyTitle={emptyTitle}
            emptyDescription={emptyDescription}
            caption="工单看板列表"
          />

          {state.kind === "ready" && total > 0 ? (
            <Pagination page={page} pageSize={PAGE_SIZE} total={total} onChange={setPageNumber} />
          ) : null}
        </>
      )}

      <Dialog
        open={detailId !== null && actionOpen === null}
        title={detail ? `工单 ${detail.order_no}` : "工单详情"}
        onClose={closeDetail}
        width={720}
        footer={
          <FormActions>
            <button type="button" className="btn-secondary" onClick={closeDetail}>
              关闭
            </button>
          </FormActions>
        }
      >
        {detailLoading && !detail ? (
          <p className="cell-muted">正在读取工单详情…</p>
        ) : detailError ? (
          <ErrorBanner message={detailError} />
        ) : detail ? (
          <WorkOrderDetailBody
            detail={detail}
            admin={admin}
            busy={busy}
            actionError={actionError}
            acknowledgedAt={acknowledgedAt}
            onAck={() => {
              setActionError(null);
              setActionOpen("ack");
            }}
            onStep={(to) => void runStep(detail, to)}
            onCloseOrder={() => {
              setActionError(null);
              setActionOpen("close");
            }}
            onReopen={() => {
              setActionError(null);
              setActionOpen("reopen");
            }}
          />
        ) : null}
      </Dialog>

      {actionOpen === "ack" && detail ? (
        <AckEventDialog
          order={detail}
          onCancel={() => setActionOpen(null)}
          onDone={(time) => {
            setActionOpen(null);
            setAcknowledgedAt(time);
            setNotice(`风险事件已确认接手（${formatDateTime(time)}）`);
          }}
        />
      ) : null}

      {actionOpen === "close" && detail ? (
        <CloseOrderDialog
          order={detail}
          onCancel={() => setActionOpen(null)}
          onDone={(message) => {
            setActionOpen(null);
            setNotice(message);
            reload();
          }}
        />
      ) : null}

      {actionOpen === "reopen" && detail ? (
        <ReopenOrderDialog
          order={detail}
          onCancel={() => setActionOpen(null)}
          onDone={(message) => {
            setActionOpen(null);
            setNotice(message);
            reload();
          }}
        />
      ) : null}
    </section>
  );
}

// ---------------------------------------------------------------- 详情正文

function Kv({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div style={{ display: "flex", gap: 12, padding: "4px 0" }}>
      <span className="cell-muted" style={{ minWidth: 96, flexShrink: 0 }}>
        {label}
      </span>
      <span>{children}</span>
    </div>
  );
}

function WorkOrderDetailBody({
  detail,
  admin,
  busy,
  actionError,
  acknowledgedAt,
  onAck,
  onStep,
  onCloseOrder,
  onReopen,
}: {
  detail: WorkOrderDetail;
  admin: boolean;
  busy: boolean;
  actionError: string | null;
  acknowledgedAt: string | null;
  onAck: () => void;
  onStep: (to: WorkOrderStatus) => void;
  onCloseOrder: () => void;
  onReopen: () => void;
}) {
  const transitions = nextStatuses(detail.status);
  // 重开只有管理员能做，且要填原因——它由下面的弹窗承接，不走 runStep。
  const stepTargets = transitions.filter((to) => to !== "closed" && to !== "reopened");

  return (
    <div>
      <Kv label="单号">{detail.order_no || "—"}</Kv>
      <Kv label="长者">{detail.elder_name || "—"}</Kv>
      <Kv label="类型">{typeText(detail.type)}</Kv>
      <Kv label="等级">
        <Badge tone={severityTone(detail.severity)}>{severityText(detail.severity)}</Badge>
      </Kv>
      <Kv label="状态">
        <Badge tone={statusTone(detail.status)}>{statusText(detail.status)}</Badge>
        {detail.false_alarm ? (
          <>
            {" "}
            <Badge tone="neutral">误报</Badge>
          </>
        ) : null}
      </Kv>
      <Kv label="责任人">{detail.assignee_name ?? <span className="cell-muted">未指派</span>}</Kv>
      <Kv label="创建时间">{formatDateTime(detail.created_at)}</Kv>
      <Kv label="关闭时间">
        {detail.closed_at ? formatDateTime(detail.closed_at) : <span className="cell-muted">未关闭</span>}
      </Kv>
      {detail.actual_handled_at ? (
        <Kv label="实际处置">{formatDateTime(detail.actual_handled_at)}</Kv>
      ) : null}
      {detail.note ? <Kv label="处置结果">{detail.note}</Kv> : null}
      {detail.false_alarm_reason ? (
        <Kv label="误报原因">{detail.false_alarm_reason}</Kv>
      ) : null}
      {detail.event_id ? (
        <Kv label="风险事件">
          <span style={{ fontFamily: "ui-monospace, monospace" }}>
            {detail.event_id.slice(0, 8)}
          </span>
          {detail.event_status ? (
            <>
              {" "}
              <Badge tone={detail.event_status === "open" ? "danger" : "neutral"}>
                {detail.event_status === "open"
                  ? "未关闭"
                  : detail.event_status === "escalated"
                    ? "已升级"
                    : detail.event_status}
              </Badge>
            </>
          ) : null}
          {detail.event_status === "open" ? (
            <div style={{ marginTop: 8 }}>
              {acknowledgedAt ? (
                <span className="cell-muted">
                  已确认接手 · {formatDateTime(acknowledgedAt)}（重复确认幂等，不会推后时间）
                </span>
              ) : (
                <>
                  <button
                    type="button"
                    className="btn-secondary btn-sm"
                    disabled={busy}
                    onClick={onAck}
                  >
                    确认接手
                  </button>
                  <p className="field-hint" style={{ margin: "6px 0 0" }}>
                    确认后 120 秒内不再升级；确认只记录接手时间，不改变事件状态，也不等于关闭工单。
                  </p>
                </>
              )}
            </div>
          ) : null}
        </Kv>
      ) : null}

      {actionError ? (
        <div style={{ marginTop: 12 }}>
          <ErrorBanner message={actionError ?? "操作失败，请重试。"} />
        </div>
      ) : null}

      {transitions.length > 0 ? (
        <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginTop: 16 }}>
          {stepTargets.map((to) => (
            <button
              key={to}
              type="button"
              className="btn-secondary btn-sm"
              disabled={busy}
              onClick={() => onStep(to)}
            >
              {transitionText(to)}
            </button>
          ))}
          {transitions.includes("closed") ? (
            <button type="button" className="btn-danger btn-sm" disabled={busy} onClick={onCloseOrder}>
              关闭工单
            </button>
          ) : null}
          {transitions.includes("reopened") && admin ? (
            <button type="button" className="btn-secondary btn-sm" disabled={busy} onClick={onReopen}>
              重新打开
            </button>
          ) : null}
          {transitions.includes("reopened") && !admin ? (
            <span className="cell-muted" style={{ alignSelf: "center" }}>
              重开仅管理员可操作
            </span>
          ) : null}
        </div>
      ) : (
        <p className="cell-muted" style={{ marginTop: 16 }}>
          当前状态没有可执行的流转。
        </p>
      )}

      <h3 style={{ marginTop: 24, marginBottom: 8, fontSize: 15 }}>状态变更历史</h3>
      {detail.transitions.length === 0 ? (
        <p className="cell-muted">暂无流转记录。</p>
      ) : (
        <ol style={{ listStyle: "none", padding: 0, margin: 0 }}>
          {detail.transitions.map((item) => (
            <li
              key={item.transition_id}
              style={{
                borderLeft: "2px solid #d8dbe2",
                paddingLeft: 12,
                paddingBottom: 12,
                marginLeft: 4,
              }}
            >
              <div style={{ fontSize: 13 }}>
                <span className="cell-muted">{formatDateTime(item.at)}</span>
                {" · "}
                {/* actor_id 为空 = 系统自动流转（服务端 TransitionResponse 的注释口径）。 */}
                <strong>{item.actor_name ?? "系统"}</strong>
                {" · "}
                {statusText(item.from_status)} → {statusText(item.to_status)}
              </div>
              {item.note ? <div className="cell-muted">备注：{item.note}</div> : null}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- 风险确认弹窗

function AckEventDialog({
  order,
  onCancel,
  onDone,
}: {
  order: WorkOrderDetail;
  onCancel: () => void;
  onDone: (acknowledgedAt: string) => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const submit = async () => {
    if (!order.event_id) {
      setError("这条工单没有关联风险事件");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const ack = await ackRiskEvent(order.event_id);
      onDone(ack.acknowledged_at);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog
      open
      title="确认接手风险事件"
      onClose={onCancel}
      width={520}
      footer={
        <FormActions>
          <button type="button" className="btn-secondary" onClick={onCancel} disabled={saving}>
            取消
          </button>
          <button type="button" className="btn-primary" onClick={() => void submit()} disabled={saving}>
            {saving ? "正在确认…" : "确认接手"}
          </button>
        </FormActions>
      }
    >
      <p>
        确认后系统会记录接手时间，<strong>120 秒内不再升级</strong>。
      </p>
      <p className="cell-muted">
        确认只回答“有没有人接手”，不改变事件状态，也不等于关闭工单。重复确认是幂等的，
        不会推后第一次确认的时间。
      </p>
      {error ? <ErrorBanner message={error} /> : null}
    </Dialog>
  );
}

// ---------------------------------------------------------------- 关单弹窗

function CloseOrderDialog({
  order,
  onCancel,
  onDone,
}: {
  order: WorkOrderDetail;
  onCancel: () => void;
  onDone: (message: string) => void;
}) {
  const [note, setNote] = useState("");
  const [falseAlarm, setFalseAlarm] = useState(false);
  const [falseAlarmReason, setFalseAlarmReason] = useState("");
  const [handledAt, setHandledAt] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const needsHandledAt = requiresHandledAt(order.severity);

  const submit = async () => {
    const text = note.trim();
    const reason = falseAlarmReason.trim();
    // 与服务端 WorkOrderValidationError 的三条一一对应，先拦一遍只为省一次往返。
    if (falseAlarm) {
      if (!reason) {
        setError("标注误报时必须填写误报原因");
        return;
      }
    } else if (!text) {
      setError("关闭工单必须填写处置结果");
      return;
    }
    if (needsHandledAt && !handledAt) {
      setError("红色工单关单必须填写实际处置时间");
      return;
    }

    setSaving(true);
    setError(null);
    try {
      // FR-ALT-02：带风险事件的单子在**同一事务里复合关闭**——服务端先关单、
      // 再关关联事件，事件归属/状态任何一条不成立就整笔回滚。前端只发一次请求，
      // 不拆两步：独立 close-event 端点要求工单已关闭，反序必然 409。
      const updated = await transitionWorkOrder(order.order_id, {
        to_status: "closed",
        // 误报时 note 可空；非误报时的处置结果是必填项，两者互斥不共存。
        note: falseAlarm ? null : text,
        false_alarm: falseAlarm,
        false_alarm_reason: falseAlarm ? reason : null,
        actual_handled_at: needsHandledAt ? new Date(handledAt).toISOString() : null,
        // 详情带了事件才传；没有关联事件时连键都不出现（后端 extra="forbid"）。
        ...(order.event_id ? { event_id: order.event_id } : {}),
      });
      onDone(
        `工单 ${updated.order_no} 已关闭${falseAlarm ? "（标注为误报）" : ""}` +
          (order.event_id ? "，关联风险事件已同步关闭" : ""),
      );
    } catch (err) {
      setError(
        order.event_id
          ? `关闭失败（工单与风险事件都保持原状态）：${errorText(err)}`
          : errorText(err),
      );
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog
      open
      title={`关闭工单 · ${order.order_no}`}
      onClose={onCancel}
      width={600}
      footer={
        <FormActions>
          <button type="button" className="btn-secondary" onClick={onCancel} disabled={saving}>
            取消
          </button>
          <button type="button" className="btn-danger" onClick={() => void submit()} disabled={saving}>
            {saving ? "提交中…" : "确认关单"}
          </button>
        </FormActions>
      }
    >
      {error ? (
        <ErrorBanner message={error} />
      ) : null}

      <p>
        关单后该工单离开待办队列；若是管理员误判，仍可在详情里「重新打开」并留下原因。
      </p>

      <FormGrid>
        <Field
          label="处置结果"
          required={!falseAlarm}
          hint={falseAlarm ? "已勾选误报：处置结果可留空，改为必填误报原因。" : "写清做了什么处置，交接时靠它。"}
        >
          <textarea
            className="input"
            rows={3}
            value={note}
            onChange={(event) => setNote(event.target.value)}
            disabled={falseAlarm}
          />
        </Field>

        <Field label="误报标记" hint="勾选后必须填写误报原因；「误报」不是状态，只是关单的附加标记。">
          <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <input
              type="checkbox"
              checked={falseAlarm}
              onChange={(event) => setFalseAlarm(event.target.checked)}
            />
            <span>这是误报（无需处置）</span>
          </label>
        </Field>

        {falseAlarm ? (
          <Field label="误报原因" required hint="复盘要靠它：写清为什么判定为误报。">
            <textarea
              className="input"
              rows={2}
              value={falseAlarmReason}
              onChange={(event) => setFalseAlarmReason(event.target.value)}
            />
          </Field>
        ) : null}

        {needsHandledAt ? (
          <Field
            label="实际处置时间"
            required
            hint="红档工单必填：真实到场/处置的时刻，不是点按钮的时刻。"
          >
            <input
              type="datetime-local"
              className="input"
              value={handledAt}
              onChange={(event) => setHandledAt(event.target.value)}
            />
          </Field>
        ) : null}
      </FormGrid>
    </Dialog>
  );
}

// ---------------------------------------------------------------- 重开弹窗

function ReopenOrderDialog({
  order,
  onCancel,
  onDone,
}: {
  order: WorkOrderDetail;
  onCancel: () => void;
  onDone: (message: string) => void;
}) {
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const submit = async () => {
    const text = note.trim();
    if (!text) {
      setError("重开工单必须填写重开原因");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const updated = await transitionWorkOrder(order.order_id, {
        to_status: "reopened",
        note: text,
      });
      onDone(`工单 ${updated.order_no} 已重新打开`);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog
      open
      title={`重新打开工单 · ${order.order_no}`}
      onClose={onCancel}
      width={560}
      footer={
        <FormActions>
          <button type="button" className="btn-secondary" onClick={onCancel} disabled={saving}>
            取消
          </button>
          <button
            type="button"
            className="btn-primary"
            onClick={() => void submit()}
            disabled={saving}
          >
            {saving ? "提交中…" : "确认重开"}
          </button>
        </FormActions>
      }
    >
      {error ? (
        <ErrorBanner message={error} />
      ) : null}

      <div className="alert-danger banner" role="alert">
        重开只限管理员，且必须写明原因：它会回到「已重开」而不是「待接单」，上一次的处置历史全部保留。
      </div>

      <Field label="重开原因" required hint="交接与复盘都要看这条。">
        <textarea
          className="input"
          rows={3}
          value={note}
          onChange={(event) => setNote(event.target.value)}
        />
      </Field>
    </Dialog>
  );
}
