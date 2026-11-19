/**
 * 数字人角色（W1 · FR-3D-03）：9 个角色的固定资产一览。
 *
 * 只读页：名字 / 形象 / 音色 / 性格 / 开场白 / 口吻示例都由 `GET /api/elders/personas`
 * 返回，页面不提供新增、编辑或删除。真正把角色分配给长者的入口仍在「长者档案 → 详情 → 形象」，
 * 两处共用同一份列表数据。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import type { Principal } from "../api";
import { errorText } from "../http";
import { can } from "../permissions";
import { DataTable, type Column } from "../ui/Table";
import { Dialog } from "../ui/Dialog";
import { EmptyState, ErrorBanner, LoadingState } from "../ui/Feedback";
import { listPersonaAssets, personaAssetNo, personalityPreview, voiceLabel, type PersonaAsset } from "../personas";

function avatarFile(avatarUrl: string | null): string {
  if (!avatarUrl) return "跟随系统默认";
  return avatarUrl.split("/").pop() ?? avatarUrl;
}

function orDash(value: string): string {
  return value.trim() ? value : "—";
}

export function PersonasPage({ principal }: { principal: Principal }) {
  const allowed = can(principal, "persona:read");

  const [items, setItems] = useState<PersonaAsset[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [detail, setDetail] = useState<PersonaAsset | null>(null);
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
      const rows = await listPersonaAssets();
      if (mounted.current) setItems(rows);
    } catch (err) {
      if (mounted.current) setError(errorText(err));
    } finally {
      if (mounted.current) setLoading(false);
    }
  }, [allowed]);

  useEffect(() => {
    void load();
  }, [load]);

  const columns: Column<PersonaAsset>[] = [
    {
      key: "name",
      title: "角色",
      render: (item) => (
        <div className="persona-cell">
          <span className="persona-avatar" aria-hidden="true">
            {item.name.slice(0, 1)}
          </span>
          <div>
            <strong>{item.name}</strong>
            <p className="field-hint persona-code">资产号 {personaAssetNo(item.persona_id)}</p>
          </div>
        </div>
      ),
    },
    {
      key: "voice_id",
      title: "音色",
      render: (item) => <span>{voiceLabel(item.voice_id)}</span>,
    },
    {
      key: "personality",
      title: "性格",
      render: (item) => (
        <span className="cell-muted persona-clamp" title={item.personality || undefined}>
          {personalityPreview(item.personality)}
        </span>
      ),
    },
    {
      key: "assigned_elders",
      title: "已分配",
      align: "right",
      render: (item) => `${item.assigned_elders} 位`,
    },
    {
      key: "actions",
      title: "操作",
      align: "right",
      render: (item) => (
        <button type="button" className="btn-link" onClick={() => setDetail(item)}>
          查看
        </button>
      ),
    },
  ];

  if (!allowed) {
    return (
      <section>
        <header className="page-head">
          <h1 className="page-title">数字人角色</h1>
        </header>
        <EmptyState
          title="当前角色无权访问"
          description="数字人角色列表仅对管理员与医生开放。"
        />
      </section>
    );
  }

  return (
    <section>
      <header className="page-head">
        <h1 className="page-title">数字人角色</h1>
        <p className="page-sub">
          9 个角色的形象、音色、性格与口吻固定；分配给长者仍在「长者档案 → 详情 → 形象」。
        </p>
        <div className="page-actions">
          <button
            type="button"
            className="btn-secondary btn-sm"
            onClick={() => void load()}
            disabled={loading}
          >
            {loading ? "正在刷新…" : "刷新"}
          </button>
        </div>
      </header>

      {error ? <ErrorBanner message={error} onRetry={() => void load()} /> : null}
      {loading && items.length === 0 ? (
        <LoadingState text="正在加载数字人角色…" />
      ) : (
        <DataTable
          columns={columns}
          rows={items}
          rowKey={(item) => item.persona_id}
          loading={loading}
          error={null}
          emptyTitle="还没有角色"
          emptyDescription="先运行 scripts/seed_personas.py --personas-only 写入固定角色资产。"
          onRowClick={(item) => setDetail(item)}
          caption="数字人角色列表"
        />
      )}

      <Dialog
        open={detail !== null}
        title={detail ? `角色 · ${detail.name}` : "角色"}
        onClose={() => setDetail(null)}
        width={640}
      >
        {detail ? (
          <div>
            <div className="persona-summary">
              <div className="persona-fact">
                <p className="persona-fact-label">资产编号</p>
                <p className="persona-fact-value">{personaAssetNo(detail.persona_id)}</p>
              </div>
              <div className="persona-fact">
                <p className="persona-fact-label">已分配长者</p>
                <p className="persona-fact-value">{detail.assigned_elders} 位</p>
              </div>
              <div className="persona-fact">
                <p className="persona-fact-label">音色</p>
                <p className="persona-fact-value">{voiceLabel(detail.voice_id)}</p>
              </div>
            </div>

            <div className="persona-section">
              <h3 className="persona-section-title">形象文件</h3>
              <p className="persona-file">{avatarFile(detail.avatar_url)}</p>
              <p className="field-hint">{detail.avatar_url ?? "未单独指定，使用小满兜底形象"}</p>
            </div>

            <div className="persona-section">
              <h3 className="persona-section-title">性格</h3>
              <p className="persona-body">{orDash(detail.personality)}</p>
            </div>

            <div className="persona-section">
              <h3 className="persona-section-title">开场白</h3>
              <blockquote className="persona-quote">{orDash(detail.greeting)}</blockquote>
            </div>

            <div className="persona-section">
              <h3 className="persona-section-title">口吻示例</h3>
              {detail.tone_examples.length === 0 ? (
                <p className="persona-body">—</p>
              ) : (
                <ul className="persona-tone-list">
                  {detail.tone_examples.map((line) => (
                    <li key={line}>{line}</li>
                  ))}
                </ul>
              )}
            </div>
            <p className="field-hint">
              形象与音色是固定资产；分配入口在「长者档案 → 详情 → 形象」。
            </p>
          </div>
        ) : null}
      </Dialog>
    </section>
  );
}
