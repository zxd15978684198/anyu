/**
 * 设备绑定页（W3）：设备列表 + 新建设备 + 绑定长者 + 重发凭证 + 解绑。
 *
 * 四条边界：
 * 1. 列表依赖 `GET /api/devices`（已上线）。这里保留「读接口未就绪」分支，但它现在的
 *    含义只是**后端不可达或该接口被下线**，不再是「W4 还没做完」——那句旧文案会把
 *    「服务挂了」误报成「功能没做」，排查时直奔错误方向。
 * 2. 列表项**没有** `room_id`、也没有 `device_code`（`devices` 表没有这两列中的 device_code，
 *    `DeviceListItem` 也不带 room_id），所以绑定必须先打 E11 候选接口：它回 `room_id` +
 *    本房间的合法候选，正好补上 `DeviceBindRequest{room_id, elder_id}` 缺的那一半。
 * 3. 解绑在服务端会**轮换长期令牌哈希**——旧设备凭证立即 401。文案必须说清这一点，
 *    不能写成「断开连接」，否则现场会以为插回网线就能上线。
 * 4. 重发凭证（E13）走的是「先解绑再签发」，因此它会**顺带清掉长者绑定**；
 *    新建设备（E14）与重发凭证返回的 `device_secret` 只在这次响应里存在，
 *    页面必须当场展示并写明「刷新后取不回，只能用重发凭证再换一份」。
 */
import { useEffect, useState } from "react";
import type { Principal } from "../api";
import { errorText } from "../http";
import { can } from "../permissions";
import { Badge, EmptyState, ErrorBanner } from "../ui/Feedback";
import { Notice } from "../../shared/ui/Notice";
import { DataTable, type Column } from "../ui/Table";
import { Pagination } from "../ui/Pagination";
import { Dialog } from "../ui/Dialog";
import { Field, FormActions, FormGrid, Select } from "../ui/Form";
import {
  bedLabel,
  bindDevice,
  createDevice,
  formatLastSeen,
  issueCredential,
  listDevices,
  listElderCandidates,
  shortId,
  unbindDevice,
  type DeviceRow,
  type ElderCandidate,
  type ElderCandidateList,
} from "../devices";
import { listRooms, type RoomOption } from "../rooms";
import { isEndpointMissing } from "../staff";
import { useListResource } from "../../shared/useListResource";

const PAGE_SIZE = 20;

const BOUND_FILTER_OPTIONS = [
  { value: "", label: "全部设备" },
  { value: "true", label: "已绑定长者" },
  { value: "false", label: "未绑定长者" },
];

const UNAVAILABLE_MESSAGE =
  "读接口不可用：本次 GET /api/devices 没有返回数据。常见原因是后端服务未启动或网络中断——请先确认服务在运行，再点「重新检查」。";

/** 凭证展示块：明文只在这一次响手里存在，样式上要显眼到不会被顺手关掉。 */
function SecretPanel({ secret, hint }: { secret: string; hint: string }) {
  return (
    <div>
      <Field label="设备凭证（只显示这一次）">
        <textarea
          className="input"
          readOnly
          rows={2}
          value={secret}
          onFocus={(event) => event.currentTarget.select()}
          style={{ fontFamily: "ui-monospace, monospace", wordBreak: "break-all" }}
        />
      </Field>
      <ErrorBanner message={hint} />
    </div>
  );
}

export function DevicesPage({ principal }: { principal: Principal }) {
  const canWrite = can(principal, "device:write");

  const [page, setPage] = useState(1);
  const [bound, setBound] = useState<"" | "true" | "false">("");
  const [notice, setNotice] = useState<string | null>(null);
  const [bindTarget, setBindTarget] = useState<DeviceRow | null>(null);
  const [unbindTarget, setUnbindTarget] = useState<DeviceRow | null>(null);
  const [credentialTarget, setCredentialTarget] = useState<DeviceRow | null>(null);
  const [createOpen, setCreateOpen] = useState(false);

  // 列表骨架收敛到 shared/useListResource（W4）：竞态取消 / 失败清空 /
  // 端点缺失降级 / reload 语义原先每页各写一遍，现在只有一处实现。
  const { rows, total, state, reload } = useListResource<DeviceRow>(
    () => listDevices({ page, pageSize: PAGE_SIZE, bound }),
    [page, bound],
    {
      errorText,
      isUnavailable: isEndpointMissing,
      unavailableMessage: UNAVAILABLE_MESSAGE,
    },
  );

  const columns: Column<DeviceRow>[] = [
    {
      key: "device",
      title: "设备",
      render: (row) => (
        // 完整 UUID 放 title：短号够看，核对时才展开。
        <span title={row.device_id} style={{ fontFamily: "ui-monospace, monospace" }}>
          {shortId(row)}
        </span>
      ),
    },
    {
      key: "room",
      title: "房间 / 床位",
      render: (row) =>
        row.room_no ? (
          `${row.room_no}${row.bed_no ? ` · ${bedLabel(row.bed_no)}` : ""}`
        ) : (
          <span className="cell-muted">—</span>
        ),
    },
    {
      key: "elder",
      title: "绑定长者",
      render: (row) =>
        row.bound && row.elder_name ? (
          row.elder_name
        ) : (
          <Badge tone="neutral">未绑定</Badge>
        ),
    },
    {
      key: "last_seen_at",
      title: "最后心跳",
      render: (row) => <span className="cell-muted">{formatLastSeen(row.last_seen_at)}</span>,
    },
  ];

  if (canWrite) {
    columns.push({
      key: "actions",
      title: "操作",
      align: "right",
      render: (row) => (
        <div style={{ display: "flex", gap: 12, justifyContent: "flex-end", flexWrap: "wrap" }}>
          <button type="button" className="btn-link" onClick={() => setBindTarget(row)}>
            {row.bound ? "改绑" : "绑定长者"}
          </button>
          <button type="button" className="btn-link" onClick={() => setCredentialTarget(row)}>
            重发凭证
          </button>
          {row.bound ? (
            <button type="button" className="btn-link btn-danger" onClick={() => setUnbindTarget(row)}>
              解绑
            </button>
          ) : null}
        </div>
      ),
    });
  }

  return (
    <section>
      <header className="page-head">
        <h1 className="page-title">设备绑定</h1>
        <p className="page-sub">
          {principal.name}
          ，新建 / 绑定 / 解绑 / 重发凭证仅管理员可用 · 解绑与重发都会立即失效该设备旧凭证
        </p>
        <div className="page-actions">
          {canWrite ? (
            <button type="button" className="btn-primary btn-sm" onClick={() => setCreateOpen(true)}>
              新建设备
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
            <Field label="绑定状态">
              <Select
                value={bound}
                options={BOUND_FILTER_OPTIONS}
                onChange={(value) => {
                  setPage(1);
                  setBound(value === "true" ? "true" : value === "false" ? "false" : "");
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
            rowKey={(row) => row.device_id}
            loading={state.kind === "loading"}
            error={state.kind === "error" ? state.message : null}
            onRetry={reload}
            emptyTitle="没有设备"
            emptyDescription="点右上角「新建设备」登记第一台，再把长者绑上去。"
            caption="设备绑定列表"
          />

          {state.kind === "ready" && total > 0 ? (
            <Pagination page={page} pageSize={PAGE_SIZE} total={total} onChange={setPage} />
          ) : null}
        </>
      )}

      {createOpen ? (
        <DeviceCreateDialog
          onClose={() => setCreateOpen(false)}
          onCreated={(message) => {
            setNotice(message);
            reload();
          }}
        />
      ) : null}

      {credentialTarget ? (
        <DeviceCredentialDialog
          device={credentialTarget}
          onClose={() => setCredentialTarget(null)}
          onIssued={(message) => {
            setNotice(message);
            reload();
          }}
        />
      ) : null}

      {bindTarget ? (
        <DeviceBindDialog
          device={bindTarget}
          onClose={() => setBindTarget(null)}
          onBound={(message) => {
            setNotice(message);
            reload();
          }}
        />
      ) : null}

      {unbindTarget ? (
        <DeviceUnbindDialog
          device={unbindTarget}
          onClose={() => setUnbindTarget(null)}
          onUnbound={(message) => {
            setNotice(message);
            reload();
          }}
        />
      ) : null}
    </section>
  );
}

// ---------------------------------------------------------------- 绑定弹窗

type CandidateState =
  | { kind: "loading" }
  | { kind: "ready"; data: ElderCandidateList }
  | { kind: "error"; message: string };

function DeviceBindDialog({
  device,
  onClose,
  onBound,
}: {
  device: DeviceRow;
  onClose: () => void;
  onBound: (message: string) => void;
}) {
  const [candidates, setCandidates] = useState<CandidateState>({ kind: "loading" });
  const [selected, setSelected] = useState<ElderCandidate | null>(null);
  const [phase, setPhase] = useState<"pick" | "confirm">("pick");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let alive = true;
    setCandidates({ kind: "loading" });
    setError(null);
    listElderCandidates(device.device_id)
      .then((data) => {
        if (!alive) return;
        setCandidates({ kind: "ready", data });
        // 单人房间只有一位候选：直接选中，少一次点击。
        if (data.mode === "single" && data.candidates.length === 1) {
          setSelected(data.candidates[0]);
        }
      })
      .catch((err: unknown) => {
        if (alive) setCandidates({ kind: "error", message: errorText(err) });
      });
    return () => {
      alive = false;
    };
  }, [device.device_id, reloadKey]);

  const confirm = async () => {
    if (!selected) {
      setError("请选择要绑定的长者");
      return;
    }
    if (candidates.kind !== "ready") {
      setError("候选长者未加载完成，无法提交");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await bindDevice(device.device_id, {
        room_id: candidates.data.room_id,
        elder_id: selected.elder_id,
      });
      onBound(
        `设备 ${shortId(device)} 已绑定到 ${selected.display_name || "所选长者"}${
          selected.bed_no ? `（${bedLabel(selected.bed_no)}）` : ""
        }。`,
      );
      onClose();
    } catch (err) {
      // 422 = 请求体形状不对（缺 room_id / elder_id 非 UUID）；404 = 设备或长者不存在。
      setError(errorText(err));
      setPhase("pick");
    } finally {
      setSaving(false);
    }
  };

  const roomLabel =
    candidates.kind === "ready"
      ? candidates.data.mode === "double"
        ? "双人间"
        : "单人间"
      : "未知房型";

  return (
    <Dialog
      open
      title={`绑定长者 · 设备 ${shortId(device)}`}
      onClose={onClose}
      width={620}
      footer={
        <FormActions>
          {phase === "confirm" ? (
            <button
              type="button"
              className="btn-secondary"
              onClick={() => setPhase("pick")}
              disabled={saving}
            >
              上一步
            </button>
          ) : null}
          <button type="button" className="btn-secondary" onClick={onClose} disabled={saving}>
            取消
          </button>
          {phase === "pick" ? (
            <button
              type="button"
              className="btn-primary"
              onClick={() => {
                if (!selected) {
                  setError("请选择要绑定的长者");
                  return;
                }
                setError(null);
                setPhase("confirm");
              }}
              disabled={candidates.kind !== "ready" || !selected}
            >
              下一步
            </button>
          ) : (
            <button type="button" className="btn-primary" onClick={() => void confirm()} disabled={saving}>
              {saving ? "绑定中…" : "确认绑定"}
            </button>
          )}
        </FormActions>
      }
    >
      {error ? (
        <ErrorBanner message={error} />
      ) : null}

      {phase === "pick" ? (
        <div>
          <p className="field-hint">
            候选来自本机房间（{roomLabel}）的在住长者，姓名已脱敏。绑定只影响本设备的会话归属，
            不改长者的房间或床位。
          </p>

          {candidates.kind === "loading" ? (
            <p className="field-hint">正在获取本房间候选长者…</p>
          ) : candidates.kind === "error" ? (
            <ErrorBanner message={candidates.message} onRetry={() => setReloadKey((key) => key + 1)} />
          ) : candidates.data.candidates.length === 0 ? (
            <EmptyState
              title="本房间暂无在住长者"
              description="请先在长者档案里完成入住登记，再回来绑定设备。"
            />
          ) : (
            <div role="radiogroup" aria-label="候选长者">
              {candidates.data.candidates.map((candidate) => (
                <label
                  key={candidate.elder_id}
                  style={{
                    display: "flex",
                    gap: 8,
                    alignItems: "center",
                    padding: "8px 10px",
                    marginBottom: 6,
                    border: "1px solid var(--color-line)",
                    borderRadius: 8,
                    background:
                      selected?.elder_id === candidate.elder_id
                        ? "var(--color-brand-soft)"
                        : "var(--color-surface)",
                    cursor: "pointer",
                  }}
                >
                  <input
                    type="radio"
                    name="elder-candidate"
                    checked={selected?.elder_id === candidate.elder_id}
                    onChange={() => setSelected(candidate)}
                  />
                  <span>
                    {candidate.display_name || "（姓名未回显）"}
                    {candidate.bed_no ? ` · ${bedLabel(candidate.bed_no)}` : ""}
                  </span>
                </label>
              ))}
            </div>
          )}
        </div>
      ) : (
        <FormGrid>
          <div className="field-span-2">
            <p className="field-hint">
              即将把设备 <strong>{shortId(device)}</strong> 绑定到：
            </p>
            <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
              <li>长者：{selected?.display_name || "（未选）"}</li>
              <li>床位：{selected?.bed_no || "—"}</li>
              <li>房间：{candidates.kind === "ready" ? candidates.data.room_id : "—"}（{roomLabel}）</li>
            </ul>
            {device.bound && device.elder_name ? (
              <p className="field-hint" style={{ marginTop: 8 }}>
                该设备当前绑定的是「{device.elder_name}」，确认后将改为上面这位长者。
              </p>
            ) : null}
          </div>
        </FormGrid>
      )}
    </Dialog>
  );
}

// ---------------------------------------------------------------- 解绑弹窗

function DeviceUnbindDialog({
  device,
  onClose,
  onUnbound,
}: {
  device: DeviceRow;
  onClose: () => void;
  onUnbound: (message: string) => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const submit = async () => {
    setSaving(true);
    setError(null);
    try {
      await unbindDevice(device.device_id);
      onUnbound(
        `已解绑设备 ${shortId(device)}：该设备凭证已失效（服务端已轮换令牌），需现场重新下发凭证才能上线。`,
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
      title={`解绑设备 ${shortId(device)}`}
      onClose={onClose}
      width={520}
      footer={
        <FormActions>
          <button type="button" className="btn-secondary" onClick={onClose} disabled={saving}>
            取消
          </button>
          <button type="button" className="btn-danger" onClick={() => void submit()} disabled={saving}>
            {saving ? "解绑中…" : "确认解绑"}
          </button>
        </FormActions>
      }
    >
      {error ? (
        <ErrorBanner message={error} />
      ) : null}

      <p>
        即将解绑设备 <strong>{shortId(device)}</strong>
        {device.room_no ? `（${device.room_no} 房）` : ""}与其长者的绑定关系。
      </p>
      <div className="alert-danger banner" role="alert">
        解绑后该设备凭证立即失效：长者端会掉线，且<strong>不能靠重启或换网络恢复</strong>，
        需现场重新下发凭证才能上线。
      </div>
      <p className="field-hint">
        长者档案、历史会话与音频回溯不受影响；如需再次使用，请在「设备绑定」里重新绑定，
        或直接点该设备的「重发凭证」拿一份新的。
      </p>
    </Dialog>
  );
}

// ---------------------------------------------------------------- 新建设备弹窗

type RoomState =
  | { kind: "loading" }
  | { kind: "ready"; rooms: RoomOption[] }
  | { kind: "error"; message: string };

function DeviceCreateDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (message: string) => void;
}) {
  const [rooms, setRooms] = useState<RoomState>({ kind: "loading" });
  const [roomId, setRoomId] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [created, setCreated] = useState<{ deviceId: string; secret: string } | null>(null);

  useEffect(() => {
    let alive = true;
    listRooms()
      .then((items) => {
        if (!alive) return;
        setRooms({ kind: "ready", rooms: items });
        // 只有一个房间时直接选中：演示环境就是一间 A-301。
        if (items.length === 1) setRoomId(items[0].room_id);
      })
      .catch((err: unknown) => {
        if (alive) setRooms({ kind: "error", message: errorText(err) });
      });
    return () => {
      alive = false;
    };
  }, []);

  const submit = async () => {
    if (!roomId) {
      setError("请选择房间");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const result = await createDevice({ room_id: roomId });
      setCreated({ deviceId: result.device_id, secret: result.device_secret });
    } catch (err) {
      setError(errorText(err));
    } finally {
      setSaving(false);
    }
  };

  // 创建成功后切到「只读凭证」阶段：不能让用户误以为还能再点一次创建。
  if (created) {
    return (
      <Dialog
        open
        title="设备已创建"
        onClose={() => {
          onCreated(
            `已新建设备 ${created.deviceId.slice(0, 8)}，状态 offline；请把下面的凭证录入平板后重新登录。`,
          );
          onClose();
        }}
        width={600}
        footer={
          <FormActions>
            <button
              type="button"
              className="btn-primary"
              onClick={() => {
                onCreated(
                  `已新建设备 ${created.deviceId.slice(0, 8)}，状态 offline；请把凭证录入平板后重新登录。`,
                );
                onClose();
              }}
            >
              我已记录，关闭
            </button>
          </FormActions>
        }
      >
        <p>
          设备 <strong style={{ fontFamily: "ui-monospace, monospace" }}>{created.deviceId}</strong>{" "}
          已创建，当前状态 <Badge tone="neutral">offline</Badge>（还没连过线）。
        </p>
        <SecretPanel
          secret={created.secret}
          hint="关掉这个窗口后就取不回来了。设备凭证等价于这台平板的长期口令，请当面录入；一旦泄露只能点「重发凭证」作废重来。"
        />
      </Dialog>
    );
  }

  const roomOptions =
    rooms.kind === "ready"
      ? rooms.rooms.map((room) => ({
          value: room.room_id,
          label: `${room.room_no}（${room.bed_count} 床 · ${room.status}）`,
        }))
      : [];
  if (roomOptions.length === 0) roomOptions.push({ value: "", label: "（暂无可用房间）" });

  return (
    <Dialog
      open
      title="新建设备"
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
            disabled={saving || rooms.kind !== "ready" || !roomId}
          >
            {saving ? "创建中…" : "创建设备"}
          </button>
        </FormActions>
      }
    >
      {error ? (
        <ErrorBanner message={error} />
      ) : null}

      <FormGrid>
        <div className="field-span-2">
          <Field
            label="所在房间"
            required
            hint="设备落在哪个房间，就决定它的声纹候选范围（本房间床位的在住长者）。"
            error={rooms.kind === "error" ? rooms.message : null}
          >
            <Select
              value={roomId}
              options={roomOptions}
              disabled={rooms.kind !== "ready"}
              onChange={setRoomId}
            />
          </Field>
        </div>
      </FormGrid>
      <p className="field-hint">
        新设备建好后是「未绑定长者」状态，需要再点「绑定长者」把它交给某位长者；
        也可以先建好放着，等长者入住后再绑。
      </p>
    </Dialog>
  );
}

// ---------------------------------------------------------------- 重发凭证弹窗

function DeviceCredentialDialog({
  device,
  onClose,
  onIssued,
}: {
  device: DeviceRow;
  onClose: () => void;
  onIssued: (message: string) => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [secret, setSecret] = useState<string | null>(null);

  const submit = async () => {
    setSaving(true);
    setError(null);
    try {
      const result = await issueCredential(device.device_id);
      setSecret(result.device_secret);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setSaving(false);
    }
  };

  if (secret) {
    const done = () => {
      onIssued(
        `设备 ${shortId(device)} 已重发凭证：旧凭证已失效，长者绑定已清空，设备状态 offline。`,
      );
      onClose();
    };
    return (
      <Dialog
        open
        title={`新凭证 · 设备 ${shortId(device)}`}
        onClose={done}
        width={600}
        footer={
          <FormActions>
            <button type="button" className="btn-primary" onClick={done}>
              我已记录，关闭
            </button>
          </FormActions>
        }
      >
        <SecretPanel
          secret={secret}
          hint="关掉这个窗口后就取不回来了。请当面录入平板；如果这串没记下来，只能再点一次「重发凭证」换一份新的。"
        />
      </Dialog>
    );
  }

  return (
    <Dialog
      open
      title={`重发凭证 · 设备 ${shortId(device)}`}
      onClose={onClose}
      width={560}
      footer={
        <FormActions>
          <button type="button" className="btn-secondary" onClick={onClose} disabled={saving}>
            取消
          </button>
          <button type="button" className="btn-danger" onClick={() => void submit()} disabled={saving}>
            {saving ? "签发中…" : "确认重发"}
          </button>
        </FormActions>
      }
    >
      {error ? (
        <ErrorBanner message={error} />
      ) : null}

      <p>
        即将为设备 <strong>{shortId(device)}</strong>
        {device.room_no ? `（${device.room_no} 房）` : ""}签发一份新的长期凭证。
      </p>
      <div className="alert-danger banner" role="alert">
        重发的语义是<strong>先解绑再签发</strong>：旧凭证立即失效、设备掉线，
        {device.bound && device.elder_name ? `并且「${device.elder_name}」的绑定会被清空，` : "并且已有的长者绑定（如果有）会被清空，"}
        之后需要重新绑定。
      </div>
      <p className="field-hint">
        只在凭证丢失（换平板、重装、现场重录）时使用。如果你只是想改绑长者，用列表里的「改绑」，
        那条路径不会作废旧凭证。
      </p>
    </Dialog>
  );
}
