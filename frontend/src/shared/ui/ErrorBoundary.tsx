/**
 * 页面级错误边界（前端计划 A6 / W3）。
 *
 * ## 为什么必须有
 * 实测：`/api/stats/overview` 返回形状与 `OverviewOut` 不符时，`HomePage` 读
 * `data.elders.active` 抛 `TypeError`，**React 整棵树卸载、`#root` 清空**，
 * 用户看到纯白页且没有任何提示。后端字段演进、网关返回 HTML、部分字段缺失都会触发。
 *
 * ## 为什么包在「页面」外层而不是整个应用外层
 * 包整个应用的话，一个页面崩了连导航一起没了，用户只能刷新。
 * 包在页面外层则**导航与顶栏存活**，用户能切到别的页面继续干活，
 * 并且边界内给「重试」与「返回」两个真实出口。
 *
 * ## 为什么 key 用路由
 * 换页时必须重置边界状态，否则 A 页崩过之后切到 B 页仍显示错误屏。
 */
import { Component, type ErrorInfo, type ReactNode } from "react";

export interface ErrorBoundaryProps {
  /** 换页时传路由，用它当 key 让边界随页面重置。 */
  resetKey: string;
  /** 出错区域的名称，用于文案（如「首页」「日报详情」）。 */
  label: string;
  /** 返回可用页面；不传则只给重试。 */
  onFallback?: () => void;
  /** 上报钩子（接观测时用）；不传则只 console.error。 */
  onError?: (error: Error, info: ErrorInfo) => void;
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // 不吞错：仍然打到控制台，便于排障与后续接观测。
    console.error(`[ErrorBoundary] ${this.props.label}`, error, info.componentStack);
    this.props.onError?.(error, info);
  }

  private retry = (): void => {
    this.setState({ error: null });
  };

  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;

    return (
      <div className="ui-error-boundary" role="alert">
        <span className="ui-error-boundary-mark" aria-hidden="true">
          !
        </span>
        <h2 className="ui-error-boundary-title">{this.props.label}暂时打不开</h2>
        <p className="ui-error-boundary-text">
          这一页的数据没能正确读出来，其他页面不受影响。可以先重试，或切到别的页面继续。
        </p>
        <p className="ui-error-boundary-detail">
          {error.message || "未知错误"}
        </p>
        <div className="ui-error-boundary-actions">
          <button type="button" className="ui-btn ui-btn-primary" onClick={this.retry}>
            重试这一页
          </button>
          {this.props.onFallback && (
            <button type="button" className="ui-btn ui-btn-ghost" onClick={this.props.onFallback}>
              返回可用页面
            </button>
          )}
        </div>
      </div>
    );
  }
}