import { useCallback, useEffect, useState } from "react";
import { StatusBadge } from "../components/StatusBadge";
import { TaskShell } from "../components/TaskShell";
import {
  MEMORY_CONTENT_MAX,
  correctMyMemory,
  deleteMyMemory,
  listMyMemories,
  memoryCategoryLabel,
} from "../apiMemory";
import type { MemoryChunk } from "../apiMemory";

/**
 * 「您让我记住的」：长者本人查看 / 纠正 / 撤回自己的长期记忆（PRD FR-MEM-02）。
 *
 * 负责什么：把 `apiMemory` 取回的记忆一条条摆出来，允许本人就地改正文、撤回一条。
 * 不负责什么：入口放在哪（待机屏还是任务区由 App 决定）、记忆的写入与召回、
 * 也不决定「这是谁」—— 服务端只认设备令牌绑定的那位（AGENTS §4 第 9 条）。
 *
 * 两条界面上的取舍：
 * 1. **任何操作后只改本地那一条，不整页重取**。重取会让整列闪一下并跳回顶部，
 *    长者刚按过的那条跑掉就等于「我刚才点的是哪一条」；重取失败时连已经改成功的
 *    内容都会被抹回去。
 * 2. **失败只在那一行下面红字出一句，不动列表**。把失败画成空列表，长者会以为
 *    记忆被清空了——那比报错更吓人。
 *
 * 版式沿用长者端下限：正文 ≥20px、触控 ≥48px（PRD FR-UI-02）。
 */

export function MemoryPanel({ onClose }: { onClose: () => void }) {
  const [items, setItems] = useState<MemoryChunk[]>([]);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);
  const [needsLogin, setNeedsLogin] = useState(false);
  /** 正在改的那一条（同时只开一条，避免两个输入框抢焦点） */
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  /** 正在等二次确认的那一条（删除不可撤销，必须多一步） */
  const [confirmingId, setConfirmingId] = useState<string | null>(null);
  /** 正在请求的那一条：只锁这一行，别把整页按死 */
  const [busyId, setBusyId] = useState<string | null>(null);
  /** 按条累积的错误，键是 chunk_id；换行/别的行操作不受影响 */
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<string | null>(null);

  const reload = useCallback(async () => {
    setLoading(true);
    setListError(null);
    setNotice(null);
    // 数据层不抛异常：失败时 `ok:false` + 一句人话，所以这里不写 try/catch
    const result = await listMyMemories();
    setItems(result.items);
    setListError(result.ok ? null : result.message);
    setNeedsLogin(result.needsLogin);
    setLoading(false);
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  function clearRowError(chunkId: string) {
    setRowErrors((prev) => {
      if (!(chunkId in prev)) return prev;
      const next = { ...prev };
      delete next[chunkId];
      return next;
    });
  }

  function startEdit(item: MemoryChunk) {
    setEditingId(item.chunk_id);
    setDraft(item.content);
    setConfirmingId(null);
    setNotice(null);
    clearRowError(item.chunk_id);
  }

  function cancelEdit() {
    setEditingId(null);
    setDraft("");
  }

  async function submitEdit(item: MemoryChunk) {
    const text = draft.trim();
    if (text === "") {
      setRowErrors((prev) => ({ ...prev, [item.chunk_id]: "这句话是空的，写点什么再改" }));
      return;
    }
    // 没改就别打扰服务端，也别让长者等一次没意义的转圈
    if (text === item.content.trim()) {
      cancelEdit();
      return;
    }

    setBusyId(item.chunk_id);
    const result = await correctMyMemory(item.chunk_id, text);
    setBusyId(null);

    if (!result.ok) {
      setRowErrors((prev) => ({
        ...prev,
        [item.chunk_id]: result.message ?? "刚才没改成，再说一次就行",
      }));
      return;
    }

    // 服务端会去掉首尾空白：能拿到回包就用回包，拿不到（body 读不出）就用本地草稿顶上
    const applied = result.item ?? { ...item, content: text };
    setItems((prev) =>
      prev.map((it) => (it.chunk_id === item.chunk_id ? applied : it)),
    );
    clearRowError(item.chunk_id);
    cancelEdit();
  }

  async function confirmForget(item: MemoryChunk) {
    setBusyId(item.chunk_id);
    const result = await deleteMyMemory(item.chunk_id);
    setBusyId(null);

    if (!result.ok) {
      setConfirmingId(null);
      setRowErrors((prev) => ({
        ...prev,
        [item.chunk_id]: result.message,
      }));
      return;
    }

    setItems((prev) => prev.filter((it) => it.chunk_id !== item.chunk_id));
    setConfirmingId(null);
    // 正在改的那条被删掉了：编辑态跟着收，否则输入框会留在一条不存在的记忆上
    if (editingId === item.chunk_id) cancelEdit();
    setNotice(result.message);
  }

  const busy = busyId !== null;

  const actionButton =
    "h-14 rounded-2xl px-6 text-xl active:bg-surface-sunken disabled:opacity-50";

  return (
    <TaskShell
      eyebrow="记忆"
      title="您让我记住的"
      onExit={onClose}
      exitLabel="返回"
    >
      {/* 「已删掉」这类回执：行已经从列表消失，必须另有一句话说明是你删的 */}
      {notice !== null && !loading && listError === null && (
        <p
          data-testid="memory-notice"
          role="status"
          className="mb-4 shrink-0 text-xl text-ink-muted"
        >
          {notice}
        </p>
      )}

      {loading ? (
        <div
          data-testid="memory-loading"
          className="flex flex-1 items-center justify-center py-16"
        >
          <p role="status" className="text-3xl text-ink-muted">
            正在找您让我记住的事…
          </p>
        </div>
      ) : listError !== null ? (
        <div
          data-testid="memory-error"
          className="flex flex-1 flex-col items-center justify-center gap-6 py-16 text-center"
        >
          <p role="alert" className="max-w-lg text-2xl leading-snug text-danger-ink">
            {listError}
          </p>
          <div className="flex flex-wrap items-center justify-center gap-4">
            {!needsLogin && (
              <button
                type="button"
                onClick={() => void reload()}
                className="h-16 rounded-2xl bg-brand px-10 text-2xl font-bold text-ink-inverse active:bg-brand-strong"
              >
                再试一次
              </button>
            )}
            <button
              type="button"
              onClick={onClose}
              className="h-16 rounded-2xl border border-line bg-surface px-10 text-2xl text-ink active:bg-surface-sunken"
            >
              返回
            </button>
          </div>
        </div>
      ) : items.length === 0 ? (
        <div
          data-testid="memory-empty"
          className="flex flex-1 flex-col items-center justify-center gap-6 py-16 text-center"
        >
          <p className="text-3xl font-semibold text-ink">我还没记住什么呢</p>
          <p className="max-w-lg text-2xl leading-snug text-ink-muted">
            您跟我说过的家常、喜好和要紧事，我都会记在这儿，随时可以回来看看。
          </p>
          <button
            type="button"
            onClick={onClose}
            className="h-16 rounded-2xl bg-brand px-10 text-2xl font-bold text-ink-inverse active:bg-brand-strong"
          >
            返回
          </button>
        </div>
      ) : (
        <div data-testid="memory-list" className="flex flex-col gap-4">
          {items.map((item) => {
            const rowBusy = busyId === item.chunk_id;
            const editing = editingId === item.chunk_id;
            const confirming = confirmingId === item.chunk_id;
            const rowError = rowErrors[item.chunk_id];

            return (
              <article
                key={item.chunk_id}
                data-testid="memory-item"
                data-chunk-id={item.chunk_id}
                className="rounded-2xl border border-line bg-surface-sunken px-6 py-5"
              >
                <div className="flex items-start justify-between gap-5">
                  <p className="min-w-0 text-3xl font-semibold leading-snug text-ink">
                    {item.content}
                  </p>
                  <span className="shrink-0">
                    <StatusBadge tone="neutral" size="md">
                      {memoryCategoryLabel(item.category)}
                    </StatusBadge>
                  </span>
                </div>

                {editing ? (
                  <div className="mt-4 flex flex-col gap-3">
                    <textarea
                      value={draft}
                      autoFocus
                      rows={3}
                      maxLength={MEMORY_CONTENT_MAX}
                      onChange={(event) => setDraft(event.target.value)}
                      aria-label="改一下这条记忆"
                      className="w-full resize-none rounded-2xl border border-line bg-surface px-5 py-4 text-2xl leading-relaxed text-ink outline-none focus:ring-2 focus:ring-brand"
                    />
                    <div className="flex flex-wrap items-center gap-3">
                      <button
                        type="button"
                        data-testid="memory-edit-save"
                        disabled={rowBusy}
                        onClick={() => void submitEdit(item)}
                        className="h-14 rounded-2xl bg-brand px-8 text-xl font-bold text-ink-inverse active:bg-brand-strong disabled:opacity-50"
                      >
                        {rowBusy ? "正在改…" : "就这样改"}
                      </button>
                      <button
                        type="button"
                        disabled={rowBusy}
                        onClick={cancelEdit}
                        className={`${actionButton} border border-line bg-surface text-ink-muted`}
                      >
                        先不改了
                      </button>
                      <span className="ml-auto text-xl text-ink-muted">
                        {draft.trim().length} / {MEMORY_CONTENT_MAX}
                      </span>
                    </div>
                  </div>
                ) : confirming ? (
                  <div className="mt-4 flex flex-col gap-3">
                    <p className="text-2xl leading-snug text-ink">
                      忘掉以后我就想不起来了，确定吗？
                    </p>
                    <div className="flex flex-wrap items-center gap-3">
                      {/* 「还留着」做成主按钮、「还是忘掉」做成次按钮：默认动作必须是不删的那一个 */}
                      <button
                        type="button"
                        disabled={rowBusy}
                        onClick={() => setConfirmingId(null)}
                        className="h-14 rounded-2xl bg-brand px-8 text-xl font-bold text-ink-inverse active:bg-brand-strong disabled:opacity-50"
                      >
                        还留着
                      </button>
                      <button
                        type="button"
                        data-testid="memory-forget-confirm"
                        disabled={rowBusy}
                        onClick={() => void confirmForget(item)}
                        className={`${actionButton} border border-line bg-surface text-ink-muted`}
                      >
                        {rowBusy ? "正在忘…" : "还是忘掉"}
                      </button>
                    </div>
                  </div>
                ) : (
                  <div className="mt-4 flex flex-wrap items-center gap-3">
                    <button
                      type="button"
                      data-testid="memory-edit"
                      disabled={busy}
                      onClick={() => startEdit(item)}
                      className={`${actionButton} border border-line bg-surface text-ink`}
                    >
                      说错了，改一下
                    </button>
                    <button
                      type="button"
                      data-testid="memory-forget"
                      disabled={busy}
                      onClick={() => {
                        setConfirmingId(item.chunk_id);
                        setEditingId(null);
                        setNotice(null);
                        clearRowError(item.chunk_id);
                      }}
                      className={`${actionButton} border border-line bg-surface text-ink-muted`}
                    >
                      忘掉这个
                    </button>
                  </div>
                )}

                {rowError !== undefined && (
                  <p
                    data-testid="memory-row-error"
                    role="alert"
                    className="mt-3 text-xl text-danger-ink"
                  >
                    {rowError}
                  </p>
                )}
              </article>
            );
          })}
        </div>
      )}
    </TaskShell>
  );
}
