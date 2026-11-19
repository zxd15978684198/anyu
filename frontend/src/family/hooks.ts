/**
 * 家属端数据 hooks（W4）。
 *
 * - `useReports`：近 30 天日报。列表与详情共用同一份数据 ——
 *   详情页的趋势图要靠它补日历轴（服务端不给缺失日补空行）。
 * - `useElderNames`：家属只能从日报里的 `elder_name` 反推长者姓名
 *   （`/api/elders` 对家属是 403，不能碰）。拉不到就退回「家人 N」，绝不显示 UUID。
 */
import { useCallback, useEffect, useState } from "react";
import { REPORT_WINDOW_DAYS, listReports, type DailyReportItem } from "./reports";

export interface ReportsState {
  reports: DailyReportItem[];
  loading: boolean;
  error: unknown;
  reload: () => void;
}

export function useReports(elderId: string | null): ReportsState {
  const [reports, setReports] = useState<DailyReportItem[]>([]);
  const [loading, setLoading] = useState(elderId !== null);
  const [error, setError] = useState<unknown>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!elderId) {
      setReports([]);
      setLoading(false);
      setError(null);
      return;
    }
    let alive = true;
    setLoading(true);
    setError(null);
    listReports(elderId, REPORT_WINDOW_DAYS)
      .then((items) => {
        if (!alive) return;
        setReports(items);
      })
      .catch((err: unknown) => {
        if (!alive) return;
        setReports([]);
        setError(err);
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [elderId, attempt]);

  const reload = useCallback(() => setAttempt((value) => value + 1), []);

  return { reports, loading, error, reload };
}

/** 长者姓名表（elder_id → 姓名）。按 id 串做依赖，避免每次渲染都重新请求。 */
export function useElderNames(elderIds: string[]): Record<string, string> {
  const [names, setNames] = useState<Record<string, string>>({});
  const key = elderIds.join(",");

  useEffect(() => {
    if (!key) {
      setNames({});
      return;
    }
    let alive = true;
    const ids = key.split(",");
    void Promise.all(
      ids.map(async (id): Promise<readonly [string, string | null]> => {
        try {
          const items = await listReports(id, 1);
          return [id, items[0]?.elder_name ?? null] as const;
        } catch {
          // 姓名只影响 tab 文案：拿不到就保持「家人 N」，不打断页面
          return [id, null] as const;
        }
      }),
    ).then((pairs) => {
      if (!alive) return;
      const next: Record<string, string> = {};
      for (const [id, name] of pairs) {
        if (name && name.trim() !== "") next[id] = name.trim();
      }
      setNames(next);
    });
    return () => {
      alive = false;
    };
  }, [key]);

  return names;
}
