/**
 * 列表资源 hook（前端计划 W4）—— 收敛 console 14 个页面各写一遍的列表骨架。
 *
 * ## 收敛前的重复
 * `ListState` 四份逐字拷贝（DevicesPage:76 / GuardiansPage:47 / StaffPage:46 / WorkOrdersPage:66）；
 * `let alive` 加载 effect 全仓 15 处，其中三页约 24 行逐行同构；
 * `reloadKey + reload` 模式 4 页。每页都自己处理「竞态取消 / 失败清空 / 端点缺失降级」。
 *
 * ## 这里保留的口径
 * 1. **竞态取消**：`alive` 标志 + cleanup，翻页或换筛选时旧响应不得覆盖新结果；
 * 2. **失败清空**：出错时 rows 清空、total 归零，避免旧数据配新错误横幅；
 * 3. **端点缺失降级**：读接口未上线时给 `unavailable` 而不是 `error`，
 *    文案是「该功能尚未上线」而非「出错了」——两者对护工的含义完全不同；
 * 4. **reload 语义**：`reload()` 只递增 key，不改筛选与页码，用于「操作成功后刷新当前视图」。
 *
 * ## 为什么 fetcher 走 ref
 * 调用方每次渲染都会新建 fetcher 闭包。若把它放进 effect 依赖会每次渲染都重新请求。
 * 所以用 ref 存最新闭包，effect 只由**显式 deps + reloadKey** 触发。
 * deps 长度必须稳定（React 要求），各页传的是固定长度的筛选/页码数组。
 */
import { useCallback, useEffect, useRef, useState } from "react";

export type ListState =
  | { kind: "loading" }
  | { kind: "ready" }
  | { kind: "unavailable"; message: string }
  | { kind: "error"; message: string };

export interface ListResult<T> {
  items: T[];
  total: number;
}

export interface UseListResourceOptions {
  /** 把异常翻成该端角色读得懂的话（各端用语不同，故注入）。 */
  errorText: (err: unknown) => string;
  /** 判定「读接口尚未上线」；不传则一律按 error。 */
  isUnavailable?: (err: unknown) => boolean;
  /** unavailable 时的文案。 */
  unavailableMessage?: string;
}

export interface ListResource<T> {
  rows: T[];
  total: number;
  state: ListState;
  /** 操作成功后刷新当前视图（不改页码与筛选）。 */
  reload: () => void;
  reloadKey: number;
  /**
   * 就地改 rows（乐观更新 / 用后端返回的完整行替换旧行）。
   * 有些动作后端直接回完整对象，不必整页重取——例如通知的送达/确认/撤销，
   * 它们不改 severity 与 created_at，所以行顺序不变，就地替换即可。
   */
  setRows: (updater: T[] | ((prev: T[]) => T[])) => void;
}

export function useListResource<T>(
  fetcher: () => Promise<ListResult<T>>,
  deps: readonly unknown[],
  options: UseListResourceOptions,
): ListResource<T> {
  const [rows, setRows] = useState<T[]>([]);
  const [total, setTotal] = useState(0);
  const [state, setState] = useState<ListState>({ kind: "loading" });
  const [reloadKey, setReloadKey] = useState(0);

  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  const optionsRef = useRef(options);
  optionsRef.current = options;

  useEffect(() => {
    let alive = true;
    setState({ kind: "loading" });
    fetcherRef
      .current()
      .then((result) => {
        if (!alive) return;
        setRows(result.items);
        setTotal(result.total);
        setState({ kind: "ready" });
      })
      .catch((err: unknown) => {
        if (!alive) return;
        setRows([]);
        setTotal(0);
        const opts = optionsRef.current;
        setState(
          opts.isUnavailable?.(err)
            ? {
                kind: "unavailable",
                message: opts.unavailableMessage ?? "该功能尚未上线",
              }
            : { kind: "error", message: opts.errorText(err) },
        );
      });
    return () => {
      alive = false;
    };
    // deps 由调用方给（筛选/页码），长度必须稳定；reloadKey 用于显式刷新。
  }, [...deps, reloadKey]);

  const reload = useCallback(() => setReloadKey((key) => key + 1), []);

  return { rows, total, state, reload, reloadKey, setRows };
}