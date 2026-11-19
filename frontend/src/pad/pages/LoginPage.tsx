import { useEffect, useRef, useState } from "react";
import {
  MOCK_ENABLED,
  deviceLogin,
  fetchDemoCredential,
  loginErrorText,
  mockDeviceLogin,
  LoginError,
  type DemoCredential,
  type LoginErrorCode,
} from "../api";
import { saveSession, readLastDeviceId, type DeviceSession } from "../session";

const RATE_LIMIT_SECONDS = 60;

interface LoginPageProps {
  onLoggedIn: (session: DeviceSession) => void;
}

/** T-B 登录页：大字表单（FR-UI-02 触控区 ≥48px 正文 ≥20px），粘贴 device_id + device_secret 换证 */
export function LoginPage({ onLoggedIn }: LoginPageProps) {
  const [deviceId, setDeviceId] = useState("");
  const [deviceSecret, setDeviceSecret] = useState("");
  // 设备密钥默认遮起来：这是设备的长期凭证，明文摊在屏幕上（尤其长者端是公共平板）
  // 等于把设备交出去。要核对时临时点小眼睛看一眼，输完自动回到遮罩态。
  const [secretVisible, setSecretVisible] = useState(false);
  const [error, setError] = useState<LoginErrorCode | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [countdown, setCountdown] = useState(0);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const submitRef = useRef<HTMLButtonElement | null>(null);


  // P0-2：上次登录的设备编号留在本机（localStorage），退出/过期后不必再打 36 位 UUID。
  // 只读一次：进页面时的快照足够，登录成功后 saveSession 会把它写回去。
  const [lastDeviceId] = useState<string | null>(() => readLastDeviceId());

  /** 「上次用的设备」一键带回：填进输入框并把焦点送到「登录」，老人只需按一下。 */
  function useLastDevice() {
    if (!lastDeviceId) return;
    setDeviceId(lastDeviceId);
    setError(null);
    // 焦点移到「登录」按钮：填完设备号就不用再碰键盘（声纹免登录已于 2026-11-13 取消）
    window.setTimeout(() => submitRef.current?.focus(), 0);
  }

  // 429 倒计时：到 0 解锁
  useEffect(() => {
    if (countdown <= 0) return;
    timerRef.current = setInterval(
      () => setCountdown((c) => Math.max(0, c - 1)),
      1000,
    );
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [countdown > 0]);

  const locked = countdown > 0;

  // 演示环境才有的「填入」按钮：后端没配 DEMO_DEVICE_*（或生产环境）时拿到 null，按钮不渲染。
  // 主动 fetch 一次而不是写死常量：设备密钥明文不在库里，只有后端知道当前种的是哪一套。
  const [demo, setDemo] = useState<DemoCredential | null>(null);
  useEffect(() => {
    let alive = true;
    void fetchDemoCredential().then((cred) => {
      if (alive) setDemo(cred);
    });
    return () => {
      alive = false;
    };
  }, []);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (submitting || locked) return;
    if (!deviceId.trim() || !deviceSecret.trim()) {
      setError("unauthorized"); // 空输入统一按「请检查输入」话术提示
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const session = MOCK_ENABLED
        ? await mockDeviceLogin(deviceId, deviceSecret)
        : await deviceLogin(deviceId.trim(), deviceSecret);
      saveSession(session);
      onLoggedIn(session);
    } catch (err) {
      if (err instanceof LoginError) {
        setError(err.code);
        if (err.code === "rate_limited") setCountdown(RATE_LIMIT_SECONDS);
      } else {
        setError("network");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    // 外层是**滚动容器**，内层才是居中排布：原来 form 自己 `h-full flex-col justify-center`，
    // 内容比视口高时 flex 子项会被压缩（实测「登录」按钮 h-20=80px 被压到 36px，破 48px 触控下限），
    // justify-center 又把溢出的一半顶到屏幕外（h1 y=-4、末行提示 y=650/702）。
    // 改成 min-h-full 的内层：装得下就居中，装不下就自然长高并由外层滚动，谁也不许被压扁。
    <form
      onSubmit={handleSubmit}
      className="pad-transcript mx-auto h-full w-[min(var(--pad-login-w),calc(100vw-48px))] overflow-y-auto"
    >
      <div className="pad-login flex min-h-full flex-col justify-center gap-[var(--pad-login-gap)] py-1">
        <div className="pad-login-col flex min-w-0 flex-col gap-[var(--pad-login-gap)]">
          <h1
            className="text-center font-bold text-brand"
            style={{ fontSize: "var(--pad-login-h1)" }}
          >
            安语
          </h1>
          <p
            className="text-center text-ink-muted"
            style={{ fontSize: "var(--pad-login-text)" }}
          >
            请输入设备编号与设备密钥登录
          </p>

          <label className="flex flex-col gap-2">
            <span style={{ fontSize: "var(--pad-login-text)" }}>设备编号</span>
            <input
              value={deviceId}
              onChange={(e) => setDeviceId(e.target.value)}
              disabled={locked}
              className="h-[var(--pad-login-field-h)] rounded-2xl border border-line bg-surface px-6 text-3xl text-ink outline-none placeholder:text-ink-subtle focus:ring-2 focus:ring-brand disabled:opacity-50"
            />
          </label>

          {/* P0-2：设备编号留在本机，退出/过期后一句话就能进去，不必再打 36 位 UUID。
              只显示后 3 位——公共平板上不把完整设备号摊开。 */}
          {lastDeviceId && deviceId.trim() !== lastDeviceId && (
            <button
              type="button"
              onClick={useLastDevice}
              disabled={locked}
              data-testid="use-last-device"
              style={{ fontSize: "var(--pad-login-text)" }}
              className="min-h-14 self-start rounded-2xl border-2 border-brand bg-transparent px-6 py-3 font-semibold text-brand active:bg-brand/10 disabled:opacity-50"
            >
              上次用的设备：…{lastDeviceId.slice(-3)}（点这里直接用）
            </button>
          )}
          {lastDeviceId && deviceId.trim() === lastDeviceId && (
            <p className="text-ink-muted" style={{ fontSize: "var(--pad-login-text)" }}>
              已填好上次的设备编号，直接说一句话就能进去
            </p>
          )}

          <label className="flex flex-col gap-2">
            <span style={{ fontSize: "var(--pad-login-text)" }}>设备密钥</span>
            <span className="relative flex">
              <input
                value={deviceSecret}
                onChange={(e) => setDeviceSecret(e.target.value)}
                disabled={locked}
                type={secretVisible ? "text" : "password"}
                autoComplete="off"
                autoCapitalize="off"
                spellCheck={false}
                className="h-[var(--pad-login-field-h)] w-full rounded-2xl border border-line bg-surface pl-6 pr-20 text-3xl text-ink outline-none placeholder:text-ink-subtle focus:ring-2 focus:ring-brand disabled:opacity-50"
              />
              {/* 小眼睛：触控区 48×48 以上（FR-UI-02），常显不占位——平板没有 hover 态 */}
              <button
                type="button"
                onClick={() => setSecretVisible((v) => !v)}
                disabled={locked}
                aria-label={secretVisible ? "隐藏设备密钥" : "显示设备密钥"}
                aria-pressed={secretVisible}
                title={secretVisible ? "隐藏设备密钥" : "显示设备密钥"}
                className="absolute right-2 top-1/2 flex h-12 w-12 -translate-y-1/2 items-center justify-center rounded-xl text-ink-muted outline-none active:bg-brand-soft focus-visible:ring-2 focus-visible:ring-brand disabled:opacity-40"
              >
                {secretVisible ? (
                  // 睁眼（当前可见，点击后遮起来）
                  <svg viewBox="0 0 24 24" className="h-7 w-7" aria-hidden="true" focusable="false">
                    <path
                      d="M12 5c5 0 8.7 4 10 7-1.3 3-5 7-10 7S3.3 15 2 12c1.3-3 5-7 10-7z"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="1.8"
                    />
                    <circle cx="12" cy="12" r="3.2" fill="currentColor" />
                  </svg>
                ) : (
                  // 闭眼：斜杠压住眼球，明确表示"现在是遮住的"
                  <svg viewBox="0 0 24 24" className="h-7 w-7" aria-hidden="true" focusable="false">
                    <path
                      d="M12 5c5 0 8.7 4 10 7-1.3 3-5 7-10 7S3.3 15 2 12c1.3-3 5-7 10-7z"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="1.8"
                    />
                    <circle cx="12" cy="12" r="3.2" fill="currentColor" opacity="0.35" />
                    <path
                      d="M4 4l16 16"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2"
                      strokeLinecap="round"
                    />
                  </svg>
                )}
              </button>
            </span>
            {secretVisible && (
              <span className="text-ink-subtle" style={{ fontSize: "var(--pad-login-note)" }}>
                密钥当前可见，核对完请点小眼睛遮回去
              </span>
            )}
          </label>

          {error && (
            <p
              role="alert"
              className="font-semibold text-danger-ink"
              style={{ fontSize: "var(--pad-login-text)" }}
            >
              {loginErrorText(error)}
            </p>
          )}

          <button
            ref={submitRef}
            type="submit"
            disabled={submitting || locked}
            className="h-[var(--pad-login-btn-h)] rounded-2xl bg-brand text-3xl font-bold text-ink-inverse active:bg-brand-strong disabled:opacity-50"
          >
            {locked ? `请等待 ${countdown} 秒后重试` : submitting ? "登录中…" : "登录"}
          </button>
        </div>

        {/* 第二列：演示账号（本机演示用；声纹免登录已于 2026-11-13 取消，登录页只留设备密钥一条路）。
            单列时它接在登录按钮下面，视觉顺序与原来一致；矮屏横屏（高 ≤560px）
            由 index.css 改成左右并排——8 块内容在 430px 高度里单列必然溢出。 */}
        <div className="pad-login-col flex min-w-0 flex-col gap-[var(--pad-login-gap)]">
          {demo && (
            <button
              type="button"
              onClick={() => {
                setDeviceId(demo.device_id);
                setDeviceSecret(demo.device_secret);
                setSecretVisible(false);
                setError(null);
              }}
              style={{ fontSize: "var(--pad-login-text)" }}
              className="h-[var(--pad-login-sub-h)] rounded-2xl border-2 border-brand bg-transparent font-semibold text-brand active:bg-brand/10"
            >
              填入演示账号
            </button>
          )}
          {demo && (
            <p
              className="text-center text-ink-subtle"
              style={{ fontSize: "var(--pad-login-note)" }}
            >
              本机演示：点上方按钮填入设备号与密钥，再点「登录」
            </p>
          )}

          {MOCK_ENABLED && (
            <p
              className="text-center text-ink-subtle"
              style={{ fontSize: "var(--pad-login-note)" }}
            >
              mock 模式：密钥输 401 / 429 触发对应错误，其余成功
            </p>
          )}
        </div>
      </div>
    </form>
  );
}
