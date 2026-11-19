/**
 * T-E 信令 WS 状态机（C2 契约消费方）。
 * 状态：connecting → authing（open 后立即首发 auth，服务端活体窗口 3000ms）→ ready ⇄ connecting。
 * 心跳：20s 周期 ping；45s 无任何入站帧 → 主动 close 触发重连（与服务端判死口径一致）。
 * 重连：异常断开按 1/2/4/8/15s 退避（上限 15s）；auth_fail 的 code 决定去留 ——
 *   `timeout` 是瞬时故障（首帧没赶上窗口），走退避重连；`invalid` / `expired` 才是会话失效，
 *   回调 onAuthFail 回登录页（未被 timeout 标记的 4401 同样按会话失效处理）；
 * 4002（同设备踢旧留新）也不重连，回调 onSuperseded —— 见 `WS_CLOSE_SUPERSEDED` 的注释。
 */
import {
  SIGNAL_WS_PATH,
  WS_CLOSE_SUPERSEDED,
  WS_CLOSE_UNAUTHORIZED,
  parseServerSignal,
  type ClientSignal,
  type ServerSignal,
} from "./protocol";

export type SocketState = "connecting" | "authing" | "ready" | "closed";

const PING_INTERVAL_MS = 20_000;
const DEAD_THRESHOLD_MS = 45_000;
const DEAD_CHECK_INTERVAL_MS = 5_000;
const BACKOFF_STEPS_MS = [1_000, 2_000, 4_000, 8_000, 15_000];
/** 连续失败达到该次数 → 停止自动重连，状态条出「点击重试」（adhoc §3.3） */
const MAX_AUTO_RETRIES = 5;

export class SignalSocket {
  private ws: WebSocket | null = null;
  private pingTimer: ReturnType<typeof setInterval> | null = null;
  private deadTimer: ReturnType<typeof setInterval> | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private lastFrameAt = 0;
  private backoffIndex = 0;
  private failCount = 0;
  private stopped = false;
  /**
   * 本条连接已收到 `auth_fail code=timeout`（首帧没赶上活体窗口）。
   * 服务端随后仍会以 4401 收尾这条连接 —— 该 4401 不得再判成会话失效，
   * 否则同一事件双判：timeout 走了重连，4401 又清 sessionStorage 踢回登录页。
   */
  private authTimedOut = false;

  onStateChange: ((state: SocketState) => void) | null = null;
  onServerSignal: ((msg: ServerSignal) => void) | null = null;
  /** 会话失效（auth_fail invalid/expired，或未被 timeout 标记的 4401）：调用方回登录页 */
  onAuthFail: (() => void) | null = null;
  /** 同一设备被新连接接管（4002）：停止重连，避免两端互相踢人 */
  onSuperseded: (() => void) | null = null;
  /** 连续 5 次自动重连失败：停止重连，由用户手动触发 retry() */
  onGiveUp: (() => void) | null = null;

  constructor(private token: string) {}

  connect(): void {
    if (this.stopped) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    this.setState("connecting");
    this.authTimedOut = false; // 每条连接独立：上一次 timeout 不得污染这一条
    const ws = new WebSocket(`${proto}://${location.host}${SIGNAL_WS_PATH}`);
    this.ws = ws;

    ws.onopen = () => {
      // C2：连接后 3000ms 活体窗口内必须首发 auth；这里同步发送，正常情况下远小于窗口。
      // 但刷新时主线程可能被 VRM 解析/着色器编译占住，实际到达仍可能晚 —— 见 auth_fail 分支。
      this.send({ type: "auth", token: this.token });
      this.setState("authing");
      this.lastFrameAt = Date.now();
      this.pingTimer = setInterval(() => this.send({ type: "ping" }), PING_INTERVAL_MS);
      this.deadTimer = setInterval(() => {
        if (Date.now() - this.lastFrameAt > DEAD_THRESHOLD_MS) ws.close();
      }, DEAD_CHECK_INTERVAL_MS);
    };

    ws.onmessage = (e) => {
      this.lastFrameAt = Date.now();
      const msg = parseServerSignal(typeof e.data === "string" ? e.data : "");
      if (!msg) return; // 非 JSON/type 未知：记 trace 后丢弃，不白屏
      if (msg.type === "auth_ok") {
        this.backoffIndex = 0; // 认证成功，重连退避复位
        this.failCount = 0;
        this.setState("ready");
      } else if (msg.type === "auth_fail") {
        // 3000ms 窗口仍可能被主线程长任务吃掉（刷新时 VRM 解析/着色器编译），
        // 所以端侧必须容忍 timeout：它是「这条连接没赶上窗口」的瞬时故障，
        // 不是凭证失效 —— 不能据此清 sessionStorage 回登录页。
        if (msg.code === "timeout") {
          this.authTimedOut = true;
          // 不 stop()：主动关掉本连接，onclose 落到退避重连；
          // 服务端也会 close(4401)，由 onclose 里的 authTimedOut 短路，避免二次判失效。
          this.ws?.close();
          return;
        }
        this.stop();
        this.onAuthFail?.();
        return;
      }
      this.onServerSignal?.(msg);
    };

    ws.onclose = (e) => {
      this.clearTimers();
      if (this.stopped) return;
      if (this.authTimedOut) {
        // 已按瞬时故障处理（见 auth_fail timeout 分支）：服务端收尾的 4401 只是关闭握手，
        // 清掉标志后直接落下方退避重连，不再回调 onAuthFail。
        this.authTimedOut = false;
      } else if (e.code === WS_CLOSE_UNAUTHORIZED) {
        this.stop();
        this.onAuthFail?.();
        return;
      }
      if (e.code === WS_CLOSE_SUPERSEDED) {
        // 服务端已经让「另一个连接」代表这台设备：这条不能重连，否则两分钟里
        // 两端会按 1.5s 节奏互相顶掉（开场白跟着反复重播/丢失）。
        this.stop();
        this.onSuperseded?.();
        return;
      }
      // 异常断开 → 退避重连（±20% 随机抖动，防雪崩）
      const base = BACKOFF_STEPS_MS[Math.min(this.backoffIndex, BACKOFF_STEPS_MS.length - 1)];
      this.backoffIndex++;
      this.failCount++;
      if (this.failCount >= MAX_AUTO_RETRIES) {
        this.setState("closed");
        this.onGiveUp?.();
        return;
      }
      const jitter = base * (0.8 + Math.random() * 0.4);
      this.reconnectTimer = setTimeout(() => this.connect(), Math.round(jitter));
    };

    ws.onerror = () => {
      /* 统一走 onclose 的重连路径，不在 error 里处理 */
    };
  }

  /**
   * 发送；返回 false 表示 WS 未 OPEN、本条被丢弃。
   * 调用方（如身份确认的「是我」）必须据此判断，否则会卡在「已提交」UI 态。
   */
  send(msg: ClientSignal): boolean {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg));
      return true;
    }
    return false;
  }

  get ready(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }

  /** 「点击重试」后调用：复位计数并重新连接 */
  retry(): void {
    this.failCount = 0;
    this.backoffIndex = 0;
    this.stopped = false;
    this.connect();
  }

  stop(): void {
    this.stopped = true;
    this.clearTimers();
    this.ws?.close();
    this.ws = null;
    this.setState("closed");
  }

  private setState(state: SocketState): void {
    this.onStateChange?.(state);
  }

  private clearTimers(): void {
    if (this.pingTimer) clearInterval(this.pingTimer);
    if (this.deadTimer) clearInterval(this.deadTimer);
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.pingTimer = this.deadTimer = this.reconnectTimer = null;
  }
}
