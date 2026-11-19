/**
 * 媒体库维护（FR-ADM-02）：列表 + 编辑 + 删除。
 *
 * 只接管理端三个端点：
 * - `GET /api/media/library/items`（含未授权素材，带 media_id / playable）；
 * - `PATCH /api/media/library/{media_id}`；
 * - `DELETE /api/media/library/{media_id}`。
 *
 * 不提供新增与上传：`POST /api/media/library` 不在本批 6 个端点内。授权状态下拉
 * 只有服务端白名单的两个值；删除遇播放历史会 409，页面如实提示，不伪造“下架”状态。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import type { Principal } from "../api";
import { errorText } from "../http";
import { can } from "../permissions";
import { Badge, EmptyState, ErrorBanner, LoadingState } from "../ui/Feedback";
import { Notice } from "../../shared/ui/Notice";
import { DataTable, type Column } from "../ui/Table";
import { Pagination } from "../ui/Pagination";
import { Dialog } from "../ui/Dialog";
import { Field, FormActions, FormGrid, Select } from "../ui/Form";
import {
  COPYRIGHT_OPTIONS,
  MEDIA_KIND_OPTIONS,
  copyrightText,
  deleteLibraryItem,
  listLibraryItems,
  mediaKindText,
  updateLibraryItem,
  type CopyrightStatus,
  type MediaKind,
  type MediaLibraryItem,
  type MediaLibraryPatch,
} from "../mediaLibrary";

const PAGE_SIZE = 20;

const KIND_OPTIONS = [
  { value: "", label: "全部类型" },
  ...MEDIA_KIND_OPTIONS.map((item) => ({ value: item.value, label: item.label })),
];

interface EditForm {
  title: string;
  media_url: string;
  copyright_status: string;
  genre: string;
  era: string;
  artist: string;
  duration: string;
}

function formOf(item: MediaLibraryItem): EditForm {
  return {
    title: item.title,
    media_url: item.media_url,
    copyright_status: item.copyright_status,
    genre: item.genre ?? "",
    era: item.era ?? "",
    artist: item.artist ?? "",
    duration: item.duration_seconds === null ? "" : String(item.duration_seconds),
  };
}

function isCopyrightStatus(value: string): value is CopyrightStatus {
  return COPYRIGHT_OPTIONS.some((item) => item.value === value);
}

export function MediaLibraryPage({ principal }: { principal: Principal }) {
  const allowed = can(principal, "media:write");

  const [kind, setKind] = useState<MediaKind | "">("");
  const [page, setPage] = useState(1);
  const [items, setItems] = useState<MediaLibraryItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [editing, setEditing] = useState<MediaLibraryItem | null>(null);
  const [form, setForm] = useState<EditForm | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const [deleting, setDeleting] = useState<MediaLibraryItem | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [deleteSaving, setDeleteSaving] = useState(false);

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
      const data = await listLibraryItems({
        kind,
        limit: PAGE_SIZE,
        offset: (page - 1) * PAGE_SIZE,
      });
      if (!mounted.current) return;
      setItems(data.items);
      setTotal(data.total);
    } catch (err) {
      if (mounted.current) setError(errorText(err));
    } finally {
      if (mounted.current) setLoading(false);
    }
  }, [allowed, kind, page]);

  useEffect(() => {
    void load();
  }, [load]);

  const openEdit = (item: MediaLibraryItem) => {
    setEditing(item);
    setForm(formOf(item));
    setFormError(null);
  };

  const save = async () => {
    if (!editing || !form) return;
    const title = form.title.trim();
    const mediaUrl = form.media_url.trim();
    if (!title) {
      setFormError("标题不能为空");
      return;
    }
    if (!mediaUrl) {
      setFormError("素材地址不能为空");
      return;
    }

    const durationText = form.duration.trim();
    let duration: number | null = null;
    if (durationText) {
      const parsed = Number(durationText);
      if (!Number.isInteger(parsed) || parsed <= 0) {
        setFormError("时长必须是正整数秒（未知留空，不要填 0）");
        return;
      }
      duration = parsed;
    }

    const patch: MediaLibraryPatch = {};
    if (title !== editing.title) patch.title = title;
    if (mediaUrl !== editing.media_url) patch.media_url = mediaUrl;
    if (form.copyright_status !== editing.copyright_status) {
      if (!isCopyrightStatus(form.copyright_status)) {
        setFormError("授权状态不在允许范围内");
        return;
      }
      patch.copyright_status = form.copyright_status;
    }
    const nullable = (value: string) => {
      const text = value.trim();
      return text ? text : null;
    };
    const genre = nullable(form.genre);
    const era = nullable(form.era);
    const artist = nullable(form.artist);
    if (genre !== editing.genre) patch.genre = genre;
    if (era !== editing.era) patch.era = era;
    if (artist !== editing.artist) patch.artist = artist;
    if (duration !== editing.duration_seconds) patch.duration = duration;
    if (Object.keys(patch).length === 0) {
      setFormError("没有修改任何字段");
      return;
    }

    setSaving(true);
    setFormError(null);
    try {
      const updated = await updateLibraryItem(editing.media_id, patch);
      if (!mounted.current) return;
      setItems((current) =>
        current.map((item) => (item.media_id === updated.media_id ? updated : item)),
      );
      setEditing(null);
      setForm(null);
      setNotice(`素材「${updated.title}」已保存`);
    } catch (err) {
      if (mounted.current) setFormError(errorText(err));
    } finally {
      if (mounted.current) setSaving(false);
    }
  };

  const confirmDelete = async () => {
    if (!deleting) return;
    setDeleteSaving(true);
    setDeleteError(null);
    try {
      await deleteLibraryItem(deleting.media_id);
      if (!mounted.current) return;
      const removed = deleting;
      setDeleting(null);
      setNotice(`素材「${removed.title}」已删除`);
      // 删除后当前页可能空掉：直接重载，total 由服务端重新给。
      await load();
    } catch (err) {
      if (mounted.current) setDeleteError(errorText(err));
    } finally {
      if (mounted.current) setDeleteSaving(false);
    }
  };

  const columns: Column<MediaLibraryItem>[] = [
    { key: "kind", title: "类型", render: (item) => mediaKindText(item.kind) },
    {
      key: "title",
      title: "标题",
      render: (item) => (
        <span title={item.media_url}>
          {item.title}
          {item.genre ? <span className="cell-muted"> · {item.genre}</span> : null}
        </span>
      ),
    },
    {
      key: "copyright_status",
      title: "授权",
      render: (item) => (
        <Badge tone={item.playable ? "success" : "warning"}>{copyrightText(item.copyright_status)}</Badge>
      ),
    },
    {
      key: "playable",
      title: "可播",
      render: (item) =>
        item.playable ? (
          <Badge tone="success">可播</Badge>
        ) : (
          <Badge tone="warning">不可播</Badge>
        ),
    },
    {
      key: "duration_seconds",
      title: "时长",
      align: "right",
      render: (item) => (item.duration_seconds === null ? "—" : formatDuration(item.duration_seconds)),
    },
    {
      key: "actions",
      title: "操作",
      align: "right",
      render: (item) => (
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", flexWrap: "wrap" }}>
          <button type="button" className="btn-link" onClick={() => openEdit(item)}>
            编辑
          </button>
          <button
            type="button"
            className="btn-link"
            onClick={() => {
              setDeleting(item);
              setDeleteError(null);
            }}
          >
            删除
          </button>
        </div>
      ),
    },
  ];

  if (!allowed) {
    return (
      <section>
        <header className="page-head">
          <h1 className="page-title">媒体库</h1>
        </header>
        <EmptyState title="当前角色无权访问" description="媒体库维护仅对管理员开放。" />
      </section>
    );
  }

  return (
    <section>
      <header className="page-head">
        <h1 className="page-title">媒体库</h1>
        <p className="page-sub">素材维护 · 授权状态决定长者端能否点播</p>
        <div className="page-actions">
          <button type="button" className="btn-secondary btn-sm" onClick={() => void load()} disabled={loading}>
            {loading ? "正在刷新…" : "刷新"}
          </button>
        </div>
      </header>

      {notice ? <Notice role="status">{notice}</Notice> : null}
      {error ? <ErrorBanner message={error} onRetry={() => void load()} /> : null}

      <div className="toolbar">
        <Field label="素材类型">
          <Select
            value={kind}
            options={KIND_OPTIONS}
            onChange={(value) => {
              setPage(1);
              setKind(value === "opera" || value === "movie" ? value : "");
            }}
          />
        </Field>
      </div>

      {loading && items.length === 0 ? (
        <LoadingState text="正在加载素材…" />
      ) : (
        <>
          <DataTable
            columns={columns}
            rows={items}
            rowKey={(item) => item.media_id}
            loading={loading}
            error={null}
            emptyTitle="没有匹配的素材"
            emptyDescription="换一个类型筛选试试；新增素材接口不在本页。"
            caption="媒体库列表"
          />
          {total > 0 ? (
            <Pagination page={page} pageSize={PAGE_SIZE} total={total} onChange={setPage} disabled={loading} />
          ) : null}
        </>
      )}

      <Dialog
        open={editing !== null}
        title={`编辑素材 ${editing?.title ?? ""}`}
        onClose={() => {
          setEditing(null);
          setForm(null);
        }}
        width={680}
        footer={
          <FormActions>
            <button
              type="button"
              className="btn-secondary"
              onClick={() => {
                setEditing(null);
                setForm(null);
              }}
              disabled={saving}
            >
              取消
            </button>
            <button type="button" className="btn-primary" onClick={() => void save()} disabled={saving}>
              {saving ? "保存中…" : "保存"}
            </button>
          </FormActions>
        }
      >
        {formError ? <ErrorBanner message={formError} /> : null}
        {form ? (
          <FormGrid>
            <Field label="标题" required>
              <input
                className="input"
                value={form.title}
                maxLength={120}
                onChange={(event) => setForm((c) => (c ? { ...c, title: event.target.value } : c))}
              />
            </Field>
            <Field label="素材地址" hint="站内相对路径或已授权外链">
              <input
                className="input"
                value={form.media_url}
                maxLength={512}
                onChange={(event) => setForm((c) => (c ? { ...c, media_url: event.target.value } : c))}
              />
            </Field>
            <Field label="授权状态" hint="只有白名单内的值才能被长者端点播">
              <Select
                value={form.copyright_status}
                options={COPYRIGHT_OPTIONS.map((item) => ({ value: item.value, label: item.label }))}
                onChange={(value) =>
                  setForm((c) => (c ? { ...c, copyright_status: value } : c))
                }
              />
            </Field>
            <Field label="流派">
              <input
                className="input"
                value={form.genre}
                maxLength={50}
                onChange={(event) => setForm((c) => (c ? { ...c, genre: event.target.value } : c))}
              />
            </Field>
            <Field label="年代">
              <input
                className="input"
                value={form.era}
                maxLength={50}
                onChange={(event) => setForm((c) => (c ? { ...c, era: event.target.value } : c))}
              />
            </Field>
            <Field label="演员 / 艺术家">
              <input
                className="input"
                value={form.artist}
                maxLength={120}
                onChange={(event) => setForm((c) => (c ? { ...c, artist: event.target.value } : c))}
              />
            </Field>
            <Field label="时长（秒）" hint="未知留空；不要填 0">
              <input
                className="input"
                inputMode="numeric"
                value={form.duration}
                onChange={(event) => setForm((c) => (c ? { ...c, duration: event.target.value } : c))}
              />
            </Field>
            <Field label="可播状态">
              <div className="cell-muted">
                {editing?.playable ? "当前可播" : "当前不可播（授权待补）"}
              </div>
            </Field>
          </FormGrid>
        ) : null}
      </Dialog>

      <Dialog
        open={deleting !== null}
        title="删除素材"
        onClose={() => setDeleting(null)}
        width={520}
        footer={
          <FormActions>
            <button
              type="button"
              className="btn-secondary"
              onClick={() => setDeleting(null)}
              disabled={deleteSaving}
            >
              取消
            </button>
            <button
              type="button"
              className="btn-danger"
              onClick={() => void confirmDelete()}
              disabled={deleteSaving}
            >
              {deleteSaving ? "正在删除…" : "确认删除"}
            </button>
          </FormActions>
        }
      >
        {deleteError ? <ErrorBanner message={deleteError} /> : null}
        <p>
          确定删除素材「<strong>{deleting?.title ?? "—"}</strong>」？
        </p>
        <p className="cell-muted">
          有播放历史的素材不能删除（服务端会返回 409）。本页不提供“下架”状态：
          服务端授权白名单只有“公版 / 样例授权”两项，不伪造第三种状态。
        </p>
      </Dialog>
    </section>
  );
}

function formatDuration(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return `${minutes}:${String(rest).padStart(2, "0")}`;
}
