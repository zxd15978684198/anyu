/**
 * 家属与绑定页（W3）：家属账号列表 + 建号 + 把家属绑定到长者。
 *
 * 三条边界：
 * 1. 读接口（W4 `GET /api/guardians`）未上线时列表显式说「读接口未就绪」；
 *    `POST /api/guardians`、`POST /api/elders/{elder_id}/guardians` 已在线，写操作不受影响。
 * 2. `guardian_id` 是 UUID，建号后**只有这一屏能拿到**——所以建号成功不急着关弹窗，
 *    而是把 id 回显 + 一键复制，避免管理员「建完就找不到人」。
 * 3. 每位长者 active 监护人上限 2 位（服务端 409），页面先把「当前已绑 N 位」摆出来，
 *    超限的报错才有上下文。
 * 4. 已绑家属每行可解绑（`DELETE /api/bindings/{binding_id}`，仅 admin）：绑定行主键
 *    由读接口回显（`binding_id`），页面不猜 ID；解绑只改绑定关系，不删家属账号。
 */
import { useEffect, useState } from "react";
import type { Principal } from "../api";
import { errorText } from "../http";
import { can } from "../permissions";
import { elderOptionLabel, isEndpointMissing, listElderOptions, type ElderOption } from "../staff";
import { useListResource } from "../../shared/useListResource";
import {
  RELATION_SUGGESTIONS,
  bindGuardian,
  createGuardian,
  listElderGuardians,
  listGuardians,
  unbindErrorText,
  unbindGuardian,
  type ElderGuardianRow,
  type GuardianCreated,
  type GuardianRow,
} from "../guardians";
import { AccountStatusBadge, EmptyState, ErrorBanner } from "../ui/Feedback";
import { Notice } from "../../shared/ui/Notice";
import { DataTable, type Column } from "../ui/Table";
import { Pagination } from "../ui/Pagination";
import { Dialog } from "../ui/Dialog";
import { Field, FormActions, FormGrid, Select } from "../ui/Form";
import { formatDateTimeFull } from "../../shared/format";

const PAGE_SIZE = 20;

const UNAVAILABLE_MESSAGE =
  "读接口未就绪：W4 的 GET /api/guardians 尚未上线（error-contract 并行开发中）。新建家属与绑定长者不受影响，可先用右上角按钮。";

/** 家属账号上限（PRD：每位长者 active 监护人最多 2 位）——文案里的数字与后端 409 保持一致。 */
const GUARDIAN_LIMIT = 2;

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function GuardiansPage({ principal }: { principal: Principal }) {
  const canWrite = can(principal, "guardian:write");

  const [page, setPage] = useState(1);
  const [phoneDraft, setPhoneDraft] = useState("");
  const [phone, setPhone] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [bindTarget, setBindTarget] = useState<{ guardianId: string } | null>(null);
  // 解绑确认框放在页面这一层：盖在绑定向导之上，且不会被向导的 Esc 一起关掉（见组件注释）。
  const [unbindTarget, setUnbindTarget] = useState<ElderGuardianRow | null>(null);
  // 解绑成功后用它驱动绑定向导重拉「已绑家属」列表（向导自己不重新挂载）。
  const [boundRefreshKey, setBoundRefreshKey] = useState(0);

  // 列表骨架收敛到 shared/useListResource（W4）。
  const { rows, total, state, reload } = useListResource<GuardianRow>(
    () => listGuardians({ page, pageSize: PAGE_SIZE, phone }),
    [page, phone],
    { errorText, isUnavailable: isEndpointMissing, unavailableMessage: UNAVAILABLE_MESSAGE },
  );

  const columns: Column<GuardianRow>[] = [
    { key: "name", title: "姓名" },
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
      key: "created_at",
      title: "创建时间",
      render: (row) =>
        row.created_at ? (
          formatDateTime(row.created_at)
        ) : (
          // `guardian_accounts` 没有 created_at 列：没有就说没有，不编一个时间出来。
          <span className="cell-muted">—</span>
        ),
    },
  ];

  if (canWrite) {
    columns.push({
      key: "actions",
      title: "操作",
      align: "right",
      render: (row) => (
        <button
          type="button"
          className="btn-link"
          onClick={() => setBindTarget({ guardianId: row.guardian_id })}
        >
          绑定到长者
        </button>
      ),
    });
  }

  const submitPhoneFilter = () => {
    setPage(1);
    setPhone(phoneDraft.trim());
  };

  return (
    <section>
      <header className="page-head">
        <h1 className="page-title">家属与绑定</h1>
        <p className="page-sub">
          {principal.name}
          ，建号与绑定仅管理员可用 · 每位长者最多 {GUARDIAN_LIMIT} 位在绑监护人
        </p>
        <div className="page-actions">
          {canWrite ? (
            <>
              <button type="button" className="btn-secondary" onClick={() => setBindTarget({ guardianId: "" })}>
                绑定到长者
              </button>
              <button type="button" className="btn-primary" onClick={() => setCreateOpen(true)}>
                新建家属
              </button>
            </>
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
            <Field label="手机号精确查询" htmlFor="guardian-phone-filter" hint="留空列出全部；输入 11 位精确匹配。">
              <input
                id="guardian-phone-filter"
                className="input"
                value={phoneDraft}
                inputMode="numeric"
                maxLength={11}
                onChange={(event) => setPhoneDraft(event.target.value.replace(/\D/g, ""))}
                onKeyDown={(event) => {
                  if (event.key === "Enter") submitPhoneFilter();
                }}
                placeholder="13800000000"
              />
            </Field>
            <div className="toolbar-actions">
              <button type="button" className="btn-secondary btn-sm" onClick={submitPhoneFilter}>
                查询
              </button>
              <button
                type="button"
                className="btn-link"
                onClick={() => {
                  setPhoneDraft("");
                  setPhone("");
                  setPage(1);
                }}
              >
                清除筛选
              </button>
              <button
                type="button"
                className="btn-secondary btn-sm"
                onClick={reload}
                disabled={state.kind === "loading"}
              >
                刷新
              </button>
            </div>
          </div>

          <DataTable
            columns={columns}
            rows={rows}
            rowKey={(row) => row.guardian_id || row.name}
            loading={state.kind === "loading"}
            error={state.kind === "error" ? state.message : null}
            onRetry={reload}
            emptyTitle={phone ? "没有匹配的家属" : "还没有家属账号"}
            emptyDescription={
              phone
                ? "手机号精确查询没有结果；请核对 11 位号码，或清除筛选看全部。"
                : canWrite
                  ? "点右上角「新建家属」创建第一个账号，建好后可立即绑定长者。"
                  : "当前没有家属账号。"
            }
            caption="家属账号列表"
          />

          {state.kind === "ready" && total > 0 ? (
            <Pagination page={page} pageSize={PAGE_SIZE} total={total} onChange={setPage} />
          ) : null}
        </>
      )}

      <GuardianCreateDialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={(created) => {
          setNotice(`已创建家属「${created.name || "（未回显姓名）"}」，家属 ID：${created.guardian_id}`);
          reload();
        }}
        onBind={(guardianId) => {
          setCreateOpen(false);
          setBindTarget({ guardianId });
        }}
      />

      {bindTarget ? (
        <GuardianBindDialog
          initialGuardianId={bindTarget.guardianId}
          refreshKey={boundRefreshKey}
          onClose={() => {
            // 确认框开着时，Esc / 点遮罩只关确认框，别把正在填的绑定向导一起关掉。
            if (unbindTarget) return;
            setBindTarget(null);
          }}
          onBound={(message) => {
            setNotice(message);
            reload();
          }}
          onRequestUnbind={(row) => setUnbindTarget(row)}
        />
      ) : null}

      <UnbindConfirmDialog
        target={unbindTarget}
        onClose={() => setUnbindTarget(null)}
        onDone={(message) => {
          setUnbindTarget(null);
          setBoundRefreshKey((key) => key + 1);
          setNotice(message);
        }}
      />
    </section>
  );
}

// ---------------------------------------------------------------- 建号弹窗

function GuardianCreateDialog({
  open,
  onClose,
  onCreated,
  onBind,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (created: GuardianCreated) => void;
  onBind: (guardianId: string) => void;
}) {
  const [name, setName] = useState("");
  const [phone, setPhone] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [created, setCreated] = useState<GuardianCreated | null>(null);
  const [copyState, setCopyState] = useState<"idle" | "ok" | "fail">("idle");

  useEffect(() => {
    if (!open) return;
    setName("");
    setPhone("");
    setPassword("");
    setError(null);
    setSaving(false);
    setCreated(null);
    setCopyState("idle");
  }, [open]);

  const validate = (): string | null => {
    if (!name.trim()) return "请填写姓名";
    if (name.trim().length > 64) return "姓名最多 64 个字";
    if (!/^1\d{10}$/.test(phone)) return "手机号需为 11 位数字且以 1 开头";
    if (password.length < 8) return "初始口令至少 8 位";
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
      const result = await createGuardian({ name: name.trim(), phone, password });
      setCreated(result);
      onCreated(result);
    } catch (err) {
      // 409 = 手机号已被员工或家属占用（跨表唯一）：只提示，不清空已填内容。
      setError(errorText(err));
    } finally {
      setSaving(false);
    }
  };

  const copyId = async (guardianId: string) => {
    try {
      await navigator.clipboard.writeText(guardianId);
      setCopyState("ok");
    } catch {
      setCopyState("fail");
    }
  };

  return (
    <Dialog
      open={open}
      title={created ? "家属账号已创建" : "新建家属"}
      onClose={onClose}
      width={560}
      footer={
        created ? (
          <FormActions>
            <button type="button" className="btn-secondary" onClick={onClose}>
              关闭
            </button>
            <button type="button" className="btn-primary" onClick={() => onBind(created.guardian_id)}>
              立即绑定到长者
            </button>
          </FormActions>
        ) : (
          <FormActions>
            <button type="button" className="btn-secondary" onClick={onClose} disabled={saving}>
              取消
            </button>
            <button type="button" className="btn-primary" onClick={() => void submit()} disabled={saving}>
              {saving ? "创建中…" : "创建家属"}
            </button>
          </FormActions>
        )
      }
    >
      {error ? (
        <ErrorBanner message={error} />
      ) : null}

      {created ? (
        <div>
          <p className="field-hint">
            家属账号已建好。`guardian_id` 只在建号响应里出现一次，请复制后交给「绑定到长者」使用。
          </p>
          <Field label="家属 ID" htmlFor="created-guardian-id" hint="UUID；绑定接口需要它。">
            <input id="created-guardian-id" className="input" value={created.guardian_id} readOnly />
          </Field>
          <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 8 }}>
            <button
              type="button"
              className="btn-secondary btn-sm"
              onClick={() => void copyId(created.guardian_id)}
            >
              复制 ID
            </button>
            <span className="field-hint">
              {copyState === "ok"
                ? "已复制到剪贴板"
                : copyState === "fail"
                  ? "复制失败：请手动选中上面的文本"
                  : ""}
            </span>
          </div>
          <p className="field-hint" style={{ marginTop: 12 }}>
            {created.name || "家属"} · {created.phone_masked || "手机号未回显"} ·{" "}
            {created.status === "active" ? "启用" : "停用"}
          </p>
        </div>
      ) : (
        <FormGrid>
          <Field label="姓名" htmlFor="guardian-name" required>
            <input
              id="guardian-name"
              className="input"
              value={name}
              maxLength={64}
              onChange={(event) => setName(event.target.value)}
              placeholder="如：李文静"
            />
          </Field>

          <Field
            label="手机号"
            htmlFor="guardian-phone"
            required
            hint="11 位；与员工共用一张手机号空间，重复会 409。"
          >
            <input
              id="guardian-phone"
              className="input"
              value={phone}
              inputMode="numeric"
              maxLength={11}
              onChange={(event) => setPhone(event.target.value.replace(/\D/g, ""))}
              placeholder="13800000001"
            />
          </Field>

          <Field label="初始口令" htmlFor="guardian-password" required hint="至少 8 位；请当面转交，勿写入工单。">
            <input
              id="guardian-password"
              className="input"
              type="password"
              value={password}
              maxLength={128}
              onChange={(event) => setPassword(event.target.value)}
              placeholder="至少 8 位"
            />
          </Field>
        </FormGrid>
      )}
    </Dialog>
  );
}

// ---------------------------------------------------------------- 绑定弹窗

function GuardianBindDialog({
  initialGuardianId,
  refreshKey,
  onClose,
  onBound,
  onRequestUnbind,
}: {
  initialGuardianId: string;
  /** 解绑成功后由页面 +1，触发「已绑家属」重拉（向导保持挂载，已选长者不丢）。 */
  refreshKey: number;
  onClose: () => void;
  onBound: (message: string) => void;
  onRequestUnbind: (row: ElderGuardianRow) => void;
}) {
  const [elders, setElders] = useState<ElderOption[] | null>(null);
  const [eldersError, setEldersError] = useState<string | null>(null);
  const [eldersLoading, setEldersLoading] = useState(false);
  const [elderId, setElderId] = useState("");
  const [guardianId, setGuardianId] = useState(initialGuardianId);
  const [relation, setRelation] = useState("儿子");
  const [bound, setBound] = useState<ElderGuardianRow[] | null>(null);
  const [boundState, setBoundState] = useState<"idle" | "loading" | "ready" | "unavailable" | "error">(
    "idle",
  );
  const [boundError, setBoundError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [reloadElders, setReloadElders] = useState(0);

  // 长者下拉（一次拉 100 位）。
  useEffect(() => {
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
  }, [reloadElders]);

  // 选定长者后看他已绑哪些家属：这是「上限 2 位 / 已绑过」的唯一上下文来源。
  useEffect(() => {
    if (!elderId) {
      setBound(null);
      setBoundState("idle");
      setBoundError(null);
      return;
    }
    let alive = true;
    setBoundState("loading");
    setBound(null);
    setBoundError(null);
    listElderGuardians(elderId)
      .then((result) => {
        if (!alive) return;
        setBound(result.items);
        setBoundState("ready");
      })
      .catch((err: unknown) => {
        if (!alive) return;
        if (isEndpointMissing(err)) {
          setBoundState("unavailable");
        } else {
          setBoundState("error");
          setBoundError(errorText(err));
        }
      });
    return () => {
      alive = false;
    };
  }, [elderId, refreshKey]);

  const submit = async () => {
    if (!elderId) {
      setError("请先选择要绑定的长者");
      return;
    }
    const trimmed = guardianId.trim();
    if (!UUID_PATTERN.test(trimmed)) {
      setError("家属 ID 应为 36 位 UUID（可在「新建家属」成功后复制）");
      return;
    }
    if (!relation.trim()) {
      setError("请填写与长者的关系");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await bindGuardian(elderId, { guardian_id: trimmed, relation: relation.trim() });
      onBound(`已把家属 ${trimmed.slice(0, 8)}… 绑定到所选长者（关系：${relation.trim()}）。`);
      onClose();
    } catch (err) {
      // 409 = 已绑过 / 已有 2 位在绑；404 = 长者或家属不存在。
      setError(errorText(err));
    } finally {
      setSaving(false);
    }
  };

  const elderOptions = (elders ?? []).map((elder) => ({
    value: elder.elder_id,
    label: elderOptionLabel(elder),
  }));

  return (
    <Dialog
      open
      title="绑定家属到长者"
      onClose={onClose}
      width={620}
      footer={
        <FormActions>
          <button type="button" className="btn-secondary" onClick={onClose} disabled={saving}>
            取消
          </button>
          <button type="button" className="btn-primary" onClick={() => void submit()} disabled={saving}>
            {saving ? "绑定中…" : "确认绑定"}
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
            label="长者"
            htmlFor="bind-elder"
            required
            hint={`先选长者：页面会显示他已绑 ${GUARDIAN_LIMIT} 位以内的家属，避免重复绑定。`}
          >
            {eldersLoading ? (
              <p className="field-hint">正在加载长者…</p>
            ) : eldersError ? (
              <ErrorBanner
                message={eldersError ?? "读取长者列表失败，请重试。"}
                onRetry={() => {
                  setElders(null);
                  setReloadElders((key) => key + 1);
                }}
              />
            ) : (
              <Select
                id="bind-elder"
                value={elderId}
                options={[{ value: "", label: "请选择长者" }, ...elderOptions]}
                onChange={(value) => setElderId(value)}
              />
            )}
          </Field>
        </div>

        <div className="field-span-2">
          <Field label="该长者已绑家属" hint={`上限 ${GUARDIAN_LIMIT} 位；只列在绑（active）记录。`}>
            {boundState === "idle" ? (
              <p className="field-hint">选定长者后显示。</p>
            ) : boundState === "loading" ? (
              <p className="field-hint">正在查询…</p>
            ) : boundState === "unavailable" ? (
              <p className="field-hint">
                读接口未就绪（GET /api/elders/&#123;elder_id&#125;/guardians 由 error-contract 并行实现）。
                绑定提交不受影响，但这里看不到「已绑几位」，请以服务端 409 提示为准。
              </p>
            ) : boundState === "error" ? (
              <ErrorBanner message={boundError ?? "读取已绑家属失败，请重试。"} />
            ) : bound && bound.length === 0 ? (
              <p className="field-hint">还没有家属绑定到这位长者。</p>
            ) : (
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {(bound ?? []).map((item) => (
                  <li
                    key={item.binding_id || item.guardian_id}
                    style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}
                  >
                    <span>
                      {item.name || "（姓名未回显）"} · {item.relation || "关系未填"} ·{" "}
                      <span className="cell-muted">
                        {item.phone_masked || "手机号未回显"}
                        {item.bound_at ? ` · ${formatDateTime(item.bound_at)}` : ""}
                      </span>
                    </span>
                    {item.binding_id ? (
                      <button
                        type="button"
                        className="btn-danger btn-sm"
                        onClick={() => onRequestUnbind(item)}
                      >
                        解绑
                      </button>
                    ) : (
                      // 旧后端没有 binding_id：不给可点的按钮，避免拼出 /api/bindings/ 打空。
                      <span className="cell-muted">缺少绑定 ID（后端未升级）</span>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </Field>
        </div>

        <Field label="家属 ID" htmlFor="bind-guardian" required hint="新建家属成功后复制过来的 UUID。">
          <input
            id="bind-guardian"
            className="input"
            value={guardianId}
            onChange={(event) => setGuardianId(event.target.value)}
            placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
          />
        </Field>

        <Field label="与长者关系" htmlFor="bind-relation" required hint="可从快捷项选，也可自由填写（1–32 字）。">
          <input
            id="bind-relation"
            className="input"
            list="guardian-relation-options"
            value={relation}
            maxLength={32}
            onChange={(event) => setRelation(event.target.value)}
            placeholder="如：女儿"
          />
          <datalist id="guardian-relation-options">
            {RELATION_SUGGESTIONS.map((item) => (
              <option key={item} value={item} />
            ))}
          </datalist>
        </Field>
      </FormGrid>
    </Dialog>
  );
}

// ---------------------------------------------------------------- 解绑确认

/**
 * 解绑二次确认（`DELETE /api/bindings/{binding_id}`，契约 E17）。
 *
 * 放在页面层而不是塞进绑定向导里：确认框要盖在向导之上，而 `Dialog` 的 Esc 监听是
 * 每个实例各挂一个——嵌套时一次 Esc 会把向导和确认框一起关掉。放在页面上、由父级
 * 在确认框打开时忽略向导的 onClose，就只剩确认框会关。
 *
 * 后果必须写全（AC-24 口径）：家属的可见范围只来自 `status='active'` 的绑定，
 * 解绑即失去这位长者的日报与通知，已登录会话的这份范围随之失效；账号**不删**，
 * 绑定行保留为 `unbound` 痕迹，可以重新绑定。
 */
function UnbindConfirmDialog({
  target,
  onClose,
  onDone,
}: {
  target: ElderGuardianRow | null;
  onClose: () => void;
  onDone: (message: string) => void;
}) {
  const [unbinding, setUnbinding] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 换了一条绑定就重置：上一条的失败红字不能带到下一条上。
  useEffect(() => {
    setError(null);
    setUnbinding(false);
  }, [target?.binding_id]);

  const confirm = async () => {
    if (!target?.binding_id) return;
    setUnbinding(true);
    setError(null);
    try {
      await unbindGuardian(target.binding_id);
      onDone(
        `已解绑家属 ${target.name || "（姓名未回显）"}：该家属不再能看到这位长者的日报与通知。`,
      );
    } catch (err) {
      // 失败留在弹窗内：403 无权 / 404 绑定没了 / 409 已解绑过，各自含义不同，
      // 顺手关掉提示只会让人以为解绑成功了。
      setError(unbindErrorText(err));
    } finally {
      setUnbinding(false);
    }
  };

  return (
    <Dialog
      open={target !== null}
      title="确认解绑这位家属？"
      onClose={() => {
        if (unbinding) return;
        onClose();
      }}
      width={520}
      footer={
        <FormActions>
          <button type="button" className="btn-secondary" onClick={onClose} disabled={unbinding}>
            取消
          </button>
          <button
            type="button"
            className="btn-danger"
            onClick={() => void confirm()}
            disabled={unbinding}
          >
            {unbinding ? "解绑中…" : "确认解绑"}
          </button>
        </FormActions>
      }
    >
      {error ? (
        <ErrorBanner message={error} />
      ) : null}

      <p style={{ marginTop: 0 }}>
        {target?.name || "（姓名未回显）"}（{target?.relation || "关系未填"}）与这位长者解绑后：
      </p>
      <ul style={{ margin: "0 0 8px", paddingLeft: 18 }}>
        <li>
          该家属<strong>立即看不到</strong>这位长者的日报与通知——家属的可见范围只认
          `status='active'` 的绑定。
        </li>
        <li>该家属已登录会话里对这位长者的读取范围同时失效。</li>
        <li>
          家属账号<strong>不会被删除</strong>，仍可正常登录；绑定记录保留为
          `unbound` 痕迹，需要时可以重新绑定。
        </li>
      </ul>
      <p className="field-hint" style={{ marginBottom: 0 }}>
        这一步不可撤销为「解绑前」状态：重新绑定会产生一条新的绑定记录。
      </p>
    </Dialog>
  );
}

// ---------------------------------------------------------------- 小工具

/** ISO 串 → `YYYY-MM-DD HH:mm`（**Asia/Shanghai**）；解析不了就原样回显，不猜。 */
function formatDateTime(value: string): string {
  return formatDateTimeFull(value);
}
