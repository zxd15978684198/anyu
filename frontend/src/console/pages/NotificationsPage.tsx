/**
 * 消息中心（迭代 8 · W4）。
 *
 * 数据源：`GET /api/notifications`（按角色可见范围过滤）+ `ack` / `deliver` 两个状态位动作，
 * 以及 N31 / N32 的两个运营动作 `resend`（没收到就再发一次）与 `revoke`（10 秒内收回）。
 *
 * 口径：
 * - 状态机与可见范围都在服务端（`notification_service`），前端只按状态显隐按钮，非法跳转由 409 兜底；
 * - 「送达」是本仓的**状态位模拟**（PRD：不接微信/短信），按钮文案写「模拟送达」；
 * - 严重度与状态徽标色统一走 `notifications.ts` 的映射，页面不另造颜色；
 * - 撤销倒计时以**服务端 `created_at`** 为基准（`revokeRemainingSeconds`），不是本页首次渲染时刻，
 *   否则刷新一下就能把 10 秒窗口续上；窗口一关按钮自己消失，不等人点了才吃 409。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { HttpError, errorText } from "../http";
import { Badge, ErrorBanner, LoadingState } from "../ui/Feedback";
import { Dialog } from "../ui/Dialog";
import { Field, FormActions, FormGrid, Select } from "../ui/Form";
import { Pagination } from "../ui/Pagination";
import type { Column } from "../ui/Table";
import { DataTable } from "../ui/Table";
import { formatDateTimeFull } from "../../shared/format";
import { useListResource } from "../../shared/useListResource";
import {
  KIND_OPTIONS,
  NOTIFICATION_PAGE_SIZE,
  REVOKE_WINDOW_SECONDS,
  SEVERITY_OPTIONS,
  STATUS_OPTIONS,
  ackNotification,
  canAck,
  canDeliver,
  canResend,
  canResendStatus,
  canRevoke,
  deliverNotification,
  kindLabel,
  listNotifications,
  payloadSummary,
  resendNotification,
  revokeNotification,
  revokeRemainingSeconds,
  severityLabel,
  severityTone,
  statusLabel,
  statusTone,
  type NotificationItem,
  type NotificationKind,
  type NotificationSeverity,
  type NotificationStatus,
} from "../notifications";

function formatTime(value?: string | null): string {
  return formatDateTimeFull(value);
}

/**
 * 重发 / 撤销的报错文案：动作前缀 + **服务端 `detail` 原文**，不许吞掉原因。
 *
 * `HttpError.message` 已由 `notifications.ts` 的 `postAction` 保留成后端 `detail`；
 * 但 `errorText` 会先查 `CODE_TEXT`，`invalid_transition` / `revoke_window_expired`
 * 这些新 code 不在表里，会被兜成「网络异常」——护工就分不清是窗口过了、状态不对，还是真断网。
 * 所以先按 code 特化这三类，其余一律把 detail 原样拼上；只有网络失败（status 0）
 * 或服务端干脆没给 detail 时，才回落到 `errorText` 的本地兜底文案。
 */
function actionErrorText(err: unknown, action: "resend" | "revoke"): string {
  if (!(err instanceof HttpError)) return errorText(err);
  const verb = action === "resend" ? "重发" : "撤销";
  const detail = err.message.trim();
  if (err.code === "revoke_window_expired") {
    return `已超过 ${REVOKE_WINDOW_SECONDS} 秒撤销窗口，服务端拒绝撤销（${detail}）`;
  }
  if (err.code === "invalid_transition") {
    return `${
      action === "resend" ? "该通知已结束，不能重发" : "该通知当前状态不能撤销"
    }（${detail}）`;
  }
  if (err.code === "ROLE_FORBIDDEN") {
    return `当前角色没有${verb}权限（${detail}）`;
  }
  if (err.status === 0 || !detail || detail.startsWith(`${verb}失败`)) {
    return errorText(err);
  }
  return `${verb}失败：${detail}`;
}

export function NotificationsPage() {
  const [page, setPage] = useState(1);
  const [kind, setKind] = useState<NotificationKind | "">("");
  const [severity, setSeverity] = useState<NotificationSeverity | "">("");
  const [status, setStatus] = useState<NotificationStatus | "">("");
  // 列表骨架收敛到 shared/useListResource（W4）。setRows 用于动作成功后就地替换行
  // （后端回完整 NotificationOut，且这些动作不改 severity/created_at，行顺序不变）。
  const {
    rows: items,
    total,
    state: listState,
    reload,
    setRows: setItems,
  } = useListResource<NotificationItem>(
    () => listNotifications({ page, kind, severity, status }),
    [page, kind, severity, status],
    { errorText },
  );
  const loading = listState.kind === "loading";
  const error = listState.kind === "error" ? listState.message : null;
  const [busyId, setBusyId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  /** 撤销确认弹窗的目标行；null = 未打开。 */
  const [revokeTarget, setRevokeTarget] = useState<NotificationItem | null>(null);

  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  /** 动作成功后**就地替换**这一行：后端返回的是完整 `NotificationOut`，不必整页重取。
   *  行顺序由服务端的「档位 → 创建时间倒序」决定，这几个动作都不改 severity / created_at，
   *  所以替换不会破坏分页与置顶口径。 */
  const applyItem = useCallback((updated: NotificationItem) => {
    setItems((prev) =>
      prev.map((row) => (row.notification_id === updated.notification_id ? updated : row)),
    );
  }, []);

  const run = useCallback(
    async (item: NotificationItem, action: "deliver" | "ack" | "resend") => {
      setBusyId(item.notification_id);
      setActionError(null);
      try {
        const updated =
          action === "deliver"
            ? await deliverNotification(item.notification_id)
            : action === "ack"
              ? await ackNotification(item.notification_id)
              : await resendNotification(item.notification_id);
        if (!mounted.current) return;
        applyItem(updated);
      } catch (err) {
        if (!mounted.current) return;
        setActionError(action === "resend" ? actionErrorText(err, "resend") : errorText(err));
      } finally {
        if (mounted.current) setBusyId(null);
      }
    },
    [applyItem],
  );

  const columns: Column<NotificationItem>[] = [
    { key: "kind", title: "类型", render: (row) => kindLabel(row.kind) },
    {
      key: "severity",
      title: "严重度",
      render: (row) => <Badge tone={severityTone(row.severity)}>{severityLabel(row.severity)}</Badge>,
    },
    {
      key: "status",
      title: "状态",
      render: (row) => <Badge tone={statusTone(row.status)}>{statusLabel(row.status)}</Badge>,
    },
    { key: "created_at", title: "创建时间", width: "170px", render: (row) => formatTime(row.created_at) },
    {
      key: "elder",
      title: "关联长者",
      render: (row) => (row.elder_id ? `长者 ${row.elder_id.slice(0, 8)}` : <span className="cell-muted">系统通知</span>),
    },
    {
      key: "payload",
      title: "摘要",
      render: (row) => <span className="cell-muted">{payloadSummary(row.payload, row.kind)}</span>,
    },
    {
      key: "actions",
      title: "操作",
      align: "right",
      render: (row) => {
        const busy = busyId === row.notification_id;
        // 「终态」只由**状态**决定：canResend 还叠了角色，用它判终态会把「医生看一条可重发的通知」
        // 误标成终态，所以这里用只看状态的 canResendStatus。
        if (!canDeliver(row.status) && !canAck(row.status) && !canResendStatus(row.status)) {
          return <span className="cell-muted">终态</span>;
        }
        return (
          <div className="cell-actions">
            {canDeliver(row.status) ? (
              <button
                type="button"
                className="btn-secondary btn-sm"
                disabled={busy}
                onClick={() => void run(row, "deliver")}
              >
                模拟送达
              </button>
            ) : null}
            {canAck(row.status) ? (
              <button
                type="button"
                className="btn-primary btn-sm"
                disabled={busy}
                onClick={() => void run(row, "ack")}
              >
                确认
              </button>
            ) : null}
            {canResend(row.status) ? (
              <button
                type="button"
                className="btn-secondary btn-sm"
                disabled={busy}
                onClick={() => void run(row, "resend")}
              >
                重发
              </button>
            ) : null}
            {canRevoke(row.status, row.created_at) ? (
              <RevokeCountdownButton
                item={row}
                disabled={busy}
                onOpen={() => {
                  setActionError(null);
                  setRevokeTarget(row);
                }}
              />
            ) : null}
          </div>
        );
      },
    },
  ];

  return (
    <section>
      <header className="page-head">
        <h1 className="page-title">消息中心</h1>
        <p className="page-sub">健康复核 / 用药升级 / 漏采派单 / 家属日报 · 按可见范围过滤</p>
        <div className="page-actions">
          <button
            type="button"
            className="btn-secondary btn-sm"
            onClick={reload}
          >
            刷新
          </button>
        </div>
      </header>

      <div className="toolbar">
        <Field label="类型" htmlFor="notif-kind">
          <Select
            id="notif-kind"
            value={kind}
            onChange={(value) => {
              setKind(value as NotificationKind | "");
              setPage(1);
            }}
            options={[{ value: "", label: "全部类型" }, ...KIND_OPTIONS]}
          />
        </Field>
        <Field label="严重度" htmlFor="notif-severity">
          <Select
            id="notif-severity"
            value={severity}
            onChange={(value) => {
              setSeverity(value as NotificationSeverity | "");
              setPage(1);
            }}
            options={[{ value: "", label: "全部严重度" }, ...SEVERITY_OPTIONS.map(({ value, label }) => ({ value, label }))]}
          />
        </Field>
        <Field label="状态" htmlFor="notif-status">
          <Select
            id="notif-status"
            value={status}
            onChange={(value) => {
              setStatus(value as NotificationStatus | "");
              setPage(1);
            }}
            options={[{ value: "", label: "全部状态" }, ...STATUS_OPTIONS.map(({ value, label }) => ({ value, label }))]}
          />
        </Field>
      </div>

      {actionError ? <ErrorBanner message={actionError} /> : null}
      {loading ? <LoadingState text="正在加载通知…" /> : null}

      <DataTable
        columns={columns}
        rows={items}
        rowKey={(row) => row.notification_id}
        error={error}
        onRetry={reload}
        emptyTitle="当前筛选下没有通知"
        emptyDescription="漏采派单与红/黄档复核会在这里出现。"
        caption="站内通知列表"
      />

      {total > 0 ? (
        <Pagination
          page={page}
          pageSize={NOTIFICATION_PAGE_SIZE}
          total={total}
          onChange={setPage}
          disabled={loading}
        />
      ) : null}

      {revokeTarget ? (
        <RevokeDialog
          item={revokeTarget}
          onClose={() => setRevokeTarget(null)}
          onRevoked={(updated) => {
            applyItem(updated);
            setActionError(null);
            setRevokeTarget(null);
          }}
        />
      ) : null}
    </section>
  );
}

// ---------------------------------------------------------------- 撤销：行内倒计时按钮 + 确认弹窗

/**
 * 行内「撤销」按钮：自带一秒心跳，按钮上直接写还剩几秒。
 *
 * 剩余秒数每次都用 `revokeRemainingSeconds(item.created_at)` **按当前墙钟重算**，
 * 不是挂载时快照——快照会让用户刷新页面后重新拿到满窗口，等于绕过服务端判定。
 * 窗口一关组件返回 null，连同心跳一起消失：不合法状态不留死按钮。
 */
function RevokeCountdownButton({
  item,
  disabled,
  onOpen,
}: {
  item: NotificationItem;
  disabled: boolean;
  onOpen: () => void;
}) {
  const [remaining, setRemaining] = useState(() => revokeRemainingSeconds(item.created_at));

  useEffect(() => {
    let timer = 0;
    const tick = () => {
      const next = revokeRemainingSeconds(item.created_at);
      setRemaining(next);
      // 归零后停表：这一行已经不该再有撤销按钮，没必要继续空转。
      if (next <= 0) window.clearInterval(timer);
    };
    timer = window.setInterval(tick, 1000);
    tick();
    return () => window.clearInterval(timer);
  }, [item.created_at]);

  if (remaining <= 0 || !canRevoke(item.status, item.created_at)) return null;

  return (
    <button
      type="button"
      className="btn-danger btn-sm"
      disabled={disabled}
      onClick={onOpen}
      title={`送达后 ${REVOKE_WINDOW_SECONDS} 秒内可撤销，剩余 ${remaining} 秒`}
    >
      撤销（剩 {remaining} 秒）
    </button>
  );
}

/**
 * 撤销确认弹窗：填一个原因（≤100 字，可留空），提交后把整行替换成服务端返回的状态。
 *
 * 窗口判定以服务端为准，这里只是提前拦一道：确认按钮随剩余秒数一起关掉，
 * 护工不必在弹窗里想半天再吃一个 409；真吃到 409 也把后端 `detail` 原样显示出来。
 */
function RevokeDialog({
  item,
  onClose,
  onRevoked,
}: {
  item: NotificationItem;
  onClose: () => void;
  onRevoked: (updated: NotificationItem) => void;
}) {
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [remaining, setRemaining] = useState(() => revokeRemainingSeconds(item.created_at));

  useEffect(() => {
    let timer = 0;
    const tick = () => {
      const next = revokeRemainingSeconds(item.created_at);
      setRemaining(next);
      if (next <= 0) window.clearInterval(timer);
    };
    timer = window.setInterval(tick, 1000);
    tick();
    return () => window.clearInterval(timer);
  }, [item.created_at]);

  const expired = remaining <= 0;

  const submit = async () => {
    setSaving(true);
    setError(null);
    try {
      const updated = await revokeNotification(item.notification_id, reason);
      onRevoked(updated);
    } catch (err) {
      setError(actionErrorText(err, "revoke"));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog
      open
      title="撤销通知"
      onClose={onClose}
      width={520}
      footer={
        <FormActions>
          <button type="button" className="btn-secondary" onClick={onClose} disabled={saving}>
            取消
          </button>
          <button
            type="button"
            className="btn-danger"
            onClick={() => void submit()}
            disabled={saving || expired}
          >
            {saving ? "撤销中…" : expired ? "撤销窗口已过" : "确认撤销"}
          </button>
        </FormActions>
      }
    >
      {error ? (
        <ErrorBanner message={error} />
      ) : null}

      <p>
        即将撤销通知 <strong>{item.notification_id.slice(0, 8)}</strong>
        （{kindLabel(item.kind)} · {severityLabel(item.severity)}）。
      </p>

      <FormGrid>
        <Field
          label="撤销原因"
          htmlFor="notif-revoke-reason"
          hint="可留空；≤100 字，会写进通知载荷与审计日志，写清楚为什么收回这条通知。"
        >
          <input
            id="notif-revoke-reason"
            className="input"
            type="text"
            maxLength={100}
            value={reason}
            placeholder="例如：已电话联系家属，无需再推送"
            onChange={(event) => setReason(event.currentTarget.value)}
          />
        </Field>
      </FormGrid>

      <p className="field-hint">
        撤销后这条通知置为「失败」状态并停用后续触达，动作不可撤销；
        窗口剩余约 {remaining} 秒，过了窗口再点会被服务端拒绝。
      </p>
    </Dialog>
  );
}

