/**
 * 房间管理（FR-ADM-01）：列表 + 改名 / 改状态。
 *
 * 本轮只接 `PATCH /api/rooms/{room_id}`，不提供新建与床位维护：
 * - 新建房间需要 `POST /api/rooms`，不在本批 6 个端点内；
 * - 床位归长者档案维护，本页只展示 `bed_count`。
 *
 * 前端能力 `room:write` 只影响入口显隐，服务端 `require_roles("admin")` 仍是权限边界。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import type { Principal } from "../api";
import { errorText } from "../http";
import { can } from "../permissions";
import { Badge, EmptyState, ErrorBanner, LoadingState } from "../ui/Feedback";
import { Notice } from "../../shared/ui/Notice";
import { DataTable, type Column } from "../ui/Table";
import { Dialog } from "../ui/Dialog";
import { Field, FormActions, FormGrid, Select } from "../ui/Form";
import {
  ROOM_STATUS_OPTIONS,
  listRooms,
  roomStatusText,
  updateRoom,
  type RoomOption,
  type RoomStatus,
} from "../rooms";

const STATUS_TONE = {
  available: "success",
  partial: "info",
  full: "danger",
  maintenance: "warning",
} as const;

export function RoomsPage({ principal }: { principal: Principal }) {
  const allowed = can(principal, "room:write");

  const [rooms, setRooms] = useState<RoomOption[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [editing, setEditing] = useState<RoomOption | null>(null);
  const [roomNo, setRoomNo] = useState("");
  const [status, setStatus] = useState<RoomStatus>("available");
  const [formError, setFormError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const load = useCallback(async () => {
    if (!allowed) {
      setLoading(false);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const items = await listRooms();
      if (mounted.current) setRooms(items);
    } catch (err) {
      if (mounted.current) setError(errorText(err));
    } finally {
      if (mounted.current) setLoading(false);
    }
  }, [allowed]);

  useEffect(() => {
    void load();
  }, [load]);

  const openEdit = (room: RoomOption) => {
    setEditing(room);
    setRoomNo(room.room_no);
    setStatus(isRoomStatus(room.status) ? room.status : "available");
    setFormError(null);
  };

  const save = async () => {
    if (!editing) return;
    const nextNo = roomNo.trim();
    if (!nextNo) {
      setFormError("房号不能为空");
      return;
    }
    const payload: { room_no?: string; status?: RoomStatus } = {};
    if (nextNo !== editing.room_no) payload.room_no = nextNo;
    if (status !== editing.status) payload.status = status;
    if (Object.keys(payload).length === 0) {
      setFormError("没有修改任何字段");
      return;
    }

    setSaving(true);
    setFormError(null);
    try {
      const updated = await updateRoom(editing.room_id, payload);
      if (!mounted.current) return;
      setRooms((current) =>
        current.map((room) => (room.room_id === updated.room_id ? updated : room)),
      );
      setEditing(null);
      setNotice(`房间 ${updated.room_no} 已保存`);
    } catch (err) {
      if (mounted.current) setFormError(errorText(err));
    } finally {
      if (mounted.current) setSaving(false);
    }
  };

  const columns: Column<RoomOption>[] = [
    {
      key: "room_no",
      title: "房号",
      render: (room) => (
        <span style={{ fontFamily: "ui-monospace, monospace" }}>{room.room_no || "—"}</span>
      ),
    },
    {
      key: "status",
      title: "状态",
      render: (room) => (
        <Badge tone={isRoomStatus(room.status) ? STATUS_TONE[room.status] : "neutral"}>
          {roomStatusText(room.status)}
        </Badge>
      ),
    },
    {
      key: "bed_count",
      title: "床位数",
      align: "right",
      render: (room) => room.bed_count,
    },
    {
      key: "actions",
      title: "操作",
      align: "right",
      render: (room) => (
        <button type="button" className="btn-link" onClick={() => openEdit(room)}>
          编辑
        </button>
      ),
    },
  ];

  if (!allowed) {
    return (
      <section>
        <header className="page-head">
          <h1 className="page-title">房间管理</h1>
        </header>
        <EmptyState title="当前角色无权访问" description="房间维护仅对管理员开放。" />
      </section>
    );
  }

  return (
    <section>
      <header className="page-head">
        <h1 className="page-title">房间管理</h1>
        <p className="page-sub">改名与状态维护；床位由长者档案维护</p>
        <div className="page-actions">
          <button type="button" className="btn-secondary btn-sm" onClick={() => void load()} disabled={loading}>
            {loading ? "正在刷新…" : "刷新"}
          </button>
        </div>
      </header>

      {notice ? <Notice role="status">{notice}</Notice> : null}
      {error ? <ErrorBanner message={error} onRetry={() => void load()} /> : null}
      {loading && rooms.length === 0 ? (
        <LoadingState text="正在加载房间…" />
      ) : (
        <DataTable
          columns={columns}
          rows={rooms}
          rowKey={(room) => room.room_id}
          loading={loading}
          error={null}
          emptyTitle="还没有房间"
          emptyDescription="房间由初始化数据或管理端建立；本页只维护既有房间。"
          caption="房间列表"
        />
      )}

      <Dialog
        open={editing !== null}
        title={`编辑房间 ${editing?.room_no ?? ""}`}
        onClose={() => setEditing(null)}
        width={480}
        footer={
          <FormActions>
            <button type="button" className="btn-secondary" onClick={() => setEditing(null)} disabled={saving}>
              取消
            </button>
            <button type="button" className="btn-primary" onClick={() => void save()} disabled={saving}>
              {saving ? "保存中…" : "保存"}
            </button>
          </FormActions>
        }
      >
        {formError ? <ErrorBanner message={formError} /> : null}
        <FormGrid>
          <Field label="房号" hint="同一机构内不能重复">
            <input
              className="input"
              value={roomNo}
              onChange={(event) => setRoomNo(event.target.value)}
              maxLength={32}
            />
          </Field>
          <Field label="状态">
            <Select
              value={status}
              options={ROOM_STATUS_OPTIONS.map((item) => ({ value: item.value, label: item.label }))}
              onChange={(value) => setStatus(value as RoomStatus)}
            />
          </Field>
        </FormGrid>
        <p className="field-hint">床位 {editing?.bed_count ?? 0} 张；床位增删不在本页，避免与长者档案双写。</p>
      </Dialog>
    </section>
  );
}

function isRoomStatus(value: string): value is RoomStatus {
  return value === "available" || value === "partial" || value === "full" || value === "maintenance";
}
