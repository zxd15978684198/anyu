import { useCallback, useEffect, useRef, useState } from "react";
import { StatusBadge } from "../components/StatusBadge";
import { TaskShell } from "../components/TaskShell";
import {
  PadApiError,
  fetchHealthCheckup,
  finishHealthCheckup,
  submitHealthCheckupAnswer,
  type CheckupSession,
} from "../padRest";

/**
 * 每日健康采集 5 问（PRD §4.6、FR-HLT-01、FR-UI-03/07）。
 *
 * 本组件是**真接线**：问句、进度、已答/缺失全部来自服务端会话
 * （`GET /api/health/checkup/{elder_id}`），回答打到
 * `POST /api/health/checkup/{elder_id}/answer`。前端不做业务裁决：
 *
 * - 数值范围、血压高低压交叉校验、每问两次无效标缺失、60 秒超时推进，全在
 *   `app/graphs/health_checkup_graph.py`；这里只把长者的字/数字原样提交。
 * - 分级（绿/黄/红）与写库由服务端负责，本页不显示、不推断，避免「界面显示正常、
 *   后台却判了红」这种两端结论不一致。
 * - 退出时**先逐题标缺失再收口**：服务端的「结束」会直接置终态但不会补缺失，
 *   少了这一步，早上被长者关掉的采集在报表里会像「一次都没做过」。
 */

type ServerHealthKey =
  | "systolic"
  | "diastolic"
  | "heart_rate"
  | "sleep_hours"
  | "mood_score";

/** 服务端 `QUESTION_ORDER` 同序（`app/graphs/health_checkup_graph.py`），只用于摘要行。 */
const QUESTION_KEYS: ServerHealthKey[] = [
  "systolic",
  "diastolic",
  "heart_rate",
  "sleep_hours",
  "mood_score",
];

interface KeyMeta {
  /** 服务端没给 prompt 时的兜底问句（正常情况用服务端那句，口径单一来源） */
  label: string;
  short: string;
  unit: string;
  kind: "number" | "mood";
  min: number;
  max: number;
  placeholder: string;
}

const KEY_META: Record<ServerHealthKey, KeyMeta> = {
  systolic: {
    label: "收缩压（高压）是多少呀？",
    short: "收缩压",
    unit: "mmHg",
    kind: "number",
    min: 0,
    max: 300,
    placeholder: "例如 128",
  },
  diastolic: {
    label: "低压（舒张压）是多少呢？",
    short: "舒张压",
    unit: "mmHg",
    kind: "number",
    min: 0,
    max: 300,
    placeholder: "例如 78",
  },
  heart_rate: {
    label: "心率是多少呀？就是心跳每分钟多少下。",
    short: "心率",
    unit: "次/分",
    kind: "number",
    min: 20,
    max: 250,
    placeholder: "例如 72",
  },
  sleep_hours: {
    label: "昨晚睡了几个小时呀？",
    short: "睡眠",
    unit: "小时",
    kind: "number",
    min: 0,
    max: 24,
    placeholder: "例如 7 或 7.5",
  },
  mood_score: {
    label: "今天心情怎么样？",
    short: "心情",
    unit: "分",
    kind: "mood",
    min: 1,
    max: 5,
    placeholder: "",
  },
};

/** 1–5 心情分的显示口径（提交给服务端的仍是数字，服务端只认十进制数）。 */
const MOOD_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "1", label: "1 很差" },
  { value: "2", label: "2 较差" },
  { value: "3", label: "3 一般" },
  { value: "4", label: "4 较好" },
  { value: "5", label: "5 很好" },
];

/**
 * 「跳过 / 退出时标缺失」用的固定文本。
 *
 * 走服务端自己的规则：同一问**两次无效**即标缺失。不新建「mark_missing」端点，
 * 是因为那会出现第二套缺失口径（AGENTS §4.8 一个能力只允许一种形态）；
 * 这句话不含任何数字，`coerce_measurement` 必然判无效，正好等价「长者答不上来」。
 */
const MISSING_TEXT = "不知道";

function isServerKey(value: string | null | undefined): value is ServerHealthKey {
  return (
    value === "systolic" ||
    value === "diastolic" ||
    value === "heart_rate" ||
    value === "sleep_hours" ||
    value === "mood_score"
  );
}

function formatAnswer(key: ServerHealthKey, value: number | undefined): string {
  if (value === undefined || value === null) return "未采集";
  const meta = KEY_META[key];
  if (key === "mood_score") {
    const label = MOOD_OPTIONS.find((option) => option.value === String(value))?.label;
    return label ?? `${value} 分`;
  }
  return `${value} ${meta.unit}`;
}

function progressText(session: CheckupSession | null): string {
  if (!session) return "正在读取今天的采集";
  const answered = QUESTION_KEYS.filter(
    (key) => session.answers[key] !== undefined && session.answers[key] !== null,
  ).length;
  const missing = QUESTION_KEYS.filter((key) => session.missing.includes(key)).length;
  return `已完成 ${answered} / 5 问${missing > 0 ? ` · 未采集 ${missing} 项` : ""}`;
}

interface HealthCheckupProps {
  onExit: () => void;
  onSubtitle?: (text: string) => void;
  /** 平板绑定长者的 ID（来自设备会话）；缺失时页面只提示、不发请求。 */
  elderId?: string | null;
  /**
   * 「再说一遍」（FR-UI-07）：重放上一段 TTS。
   *
   * 声音归 `RealtimeClient`（`<audio>` 与逐句字幕计划都在它手里），本页不碰播放器：
   * 只把「长者想再听一遍」这件事交回 App，由它调 `replayLastSpeech()`。
   */
  onReplaySpeech?: () => void;
}

export function HealthCheckup({
  onExit,
  onSubtitle,
  elderId,
  onReplaySpeech,
}: HealthCheckupProps) {
  const [session, setSession] = useState<CheckupSession | null>(null);
  /** 与 state 同步的镜像：退出/跳过的连续请求里要读**最新**的 current_key */
  const sessionRef = useRef<CheckupSession | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [exiting, setExiting] = useState(false);
  /**
   * 二次确认（审计 A2-3）：这个按钮一按就把当天没答的题统一记成「未采集」并收口，
   * 没有回滚入口 —— 长者手抖点一下，当天这次采集就废了，所以先问一句再动。
   */
  const [confirmExit, setConfirmExit] = useState(false);
  const [input, setInput] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [remaining, setRemaining] = useState(60);

  const applySession = useCallback((next: CheckupSession | null) => {
    sessionRef.current = next;
    setSession(next);
  }, []);

  const load = useCallback(async () => {
    if (!elderId) {
      setLoadError("这台平板还没有绑定长者，请联系工作人员");
      setLoading(false);
      return;
    }
    setLoading(true);
    setLoadError(null);
    try {
      applySession(await fetchHealthCheckup(elderId));
    } catch (error) {
      // 404 = 今天还没有这场采集（服务端 19:00 才建会话），不是故障；
      // 其余错误才是「连不上」，两者提示必须分开，否则长者会一直等一个不会来的问句。
      const notStarted = error instanceof PadApiError && error.status === 404;
      setLoadError(
        notStarted
          ? "今天这会儿还没有健康采集，晚上 7 点会来问您几个小问题"
          : "健康采集暂时连不上，请稍后再试",
      );
    } finally {
      setLoading(false);
    }
  }, [applySession, elderId]);

  useEffect(() => {
    void load();
  }, [load]);

  // 每问独立 60 秒窗口（与服务端 CHECKUP_ANSWER_TIMEOUT_SECONDS 对齐）；
  // 重问一次（attempt_no 变化）时重新计时，不然第二次问句只剩几秒。
  useEffect(() => {
    if (!session || session.status !== "asking") return;
    setRemaining(60);
    const timer = setInterval(() => setRemaining((value) => Math.max(0, value - 1)), 1000);
    return () => clearInterval(timer);
  }, [session?.current_key, session?.attempt_no, session?.status]);

  /** 提交一句话，返回推进后的会话（失败抛出去，由调用方决定提示）。 */
  const sendAnswer = useCallback(
    async (text: string): Promise<CheckupSession | null> => {
      if (!elderId) return null;
      const turn = await submitHealthCheckupAnswer(elderId, text);
      applySession(turn.session);
      return turn.session;
    },
    [applySession, elderId],
  );

  /**
   * 把当前这一问标成缺失：连发两次无效文本，命中服务端「两次无效即 missing」规则。
   * 每发一次都看 `current_key` 是否已换题——换了就说明这一问已经落定（答上了或已缺失），
   * 不再多发，避免把下一问也一起打成缺失。
   */
  const markMissing = useCallback(
    async (key: string) => {
      for (let attempt = 0; attempt < 2; attempt += 1) {
        let latest: CheckupSession | null = null;
        try {
          latest = await sendAnswer(MISSING_TEXT);
        } catch {
          return;
        }
        if (!latest || latest.current_key !== key) return;
      }
    },
    [sendAnswer],
  );

  const submit = async (text: string) => {
    const value = text.trim();
    if (!value || busy || !session || session.status !== "asking") return;
    setBusy(true);
    setNotice(null);
    try {
      const next = await sendAnswer(value);
      setInput("");
      onSubtitle?.(next && next.status === "asking" ? "好的，记下了" : "今天问完了，谢谢您");
    } catch {
      // 不推进本地进度：服务端没收到就不算答过，否则下一问会跳过这次没送达的回答。
      setNotice("刚才没传上去，您再说一次就好");
      onSubtitle?.("刚才没传上去，您再说一次就好");
    } finally {
      setBusy(false);
    }
  };

  const handleSkip = async () => {
    const key = session?.current_key;
    if (!key || busy || !isServerKey(key)) return;
    setBusy(true);
    setNotice(null);
    try {
      await markMissing(key);
      onSubtitle?.("好的，这一项先记为未采集");
    } finally {
      setBusy(false);
    }
  };

  /** 「没听清，再问一次」：先重放上一段 TTS（长者的诉求是「再听一遍」），再本地重置倒计时。 */
  const handleRetry = () => {
    setRemaining(60);
    setNotice(null);
    onReplaySpeech?.();
    onSubtitle?.("没听清，请您再说一次");
  };

  /**
   * 退出采集：逐题写缺失 → 仍在推进就发「结束」收口 → 关面板。
   * 网络失败也照样退出（长者要能离开这一页），缺失没写全的情况在服务端留在 asking，
   * 由 20:00 的强制结束兜底（PRD §4.6）。
   */
  const handleExit = async () => {
    if (exiting) return;
    setExiting(true);
    try {
      // 一次请求收口：缺失集合由服务端按图的 state 算完（`finishHealthCheckup`），
      // 端侧不再逐题发「标缺失」（那样最多 6 次串行往返，按钮要转 2–5 秒）。
      if (elderId) await finishHealthCheckup(elderId);
    } catch {
      // 网络失败也照样退出：长者要能离开这一页。缺失没写全的情况服务端仍留在 asking，
      // 由 20:00 的强制结束兜底（PRD §4.6）。
    } finally {
      setExiting(false);
      setConfirmExit(false);
      onExit();
    }
  };

  /** 只用于确认弹窗里说清「这一次会丢几条」，不参与任何业务判定 */
  const unansweredCount = QUESTION_KEYS.filter(
    (key) => session?.answers[key] === undefined && !session?.missing.includes(key),
  ).length;

  const exitConfirmDialog = confirmExit ? (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-6">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="health-exit-title"
        className="w-[min(40rem,92vw)] rounded-3xl border border-line bg-surface p-8 shadow-lg"
      >
        <h2 id="health-exit-title" className="text-3xl font-bold leading-tight text-ink">
          现在就结束今天的采集吗？
        </h2>
        <p className="mt-4 text-2xl leading-relaxed text-ink-muted">
          {unansweredCount > 0
            ? `还有 ${unansweredCount} 题没答，结束后会记成「未采集」，工作人员会看到这几项缺了。`
            : "五题都答完了，可以结束。"}
          {" "}结束以后不能撤销。
        </p>
        <div className="mt-8 flex gap-4">
          <button
            type="button"
            disabled={exiting}
            onClick={() => setConfirmExit(false)}
            className="h-20 flex-1 rounded-2xl bg-brand text-2xl font-bold text-ink-inverse active:bg-brand-strong disabled:opacity-50"
          >
            继续答题
          </button>
          <button
            type="button"
            disabled={exiting}
            onClick={() => void handleExit()}
            className="h-20 flex-1 rounded-2xl border border-line bg-surface text-2xl font-semibold text-ink-muted active:bg-surface-sunken disabled:opacity-50"
          >
            {exiting ? "正在保存…" : "就这样结束"}
          </button>
        </div>
      </div>
    </div>
  ) : null;

  const handleNumber = () => {
    const key = session?.current_key;
    const raw = input.trim().replace(/。/g, ".").replace(/[，,\s]/g, "");
    if (!isServerKey(key)) return;
    const meta = KEY_META[key];
    // sleep_hours 是 Numeric(4,1)，允许一位小数；其余指标是整数
    const pattern = key === "sleep_hours" ? /^\d{1,2}(\.\d)?$/ : /^\d{1,3}$/;
    if (!pattern.test(raw)) {
      setNotice("请只输入数字，比如 128");
      return;
    }
    const value = Number(raw);
    if (value < meta.min || value > meta.max) {
      setNotice(`这个数听着有点不寻常，请您核对一下（正常在 ${meta.min}–${meta.max}）`);
      return;
    }
    void submit(raw);
  };

  const progress = (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-2">
        {QUESTION_KEYS.map((key) => {
          const answered =
            session?.answers[key] !== undefined && session?.answers[key] !== null;
          const missing = session?.missing.includes(key) ?? false;
          const current = session?.current_key === key;
          return (
            <span
              key={key}
              className={`h-3 flex-1 rounded-full border ${
                answered
                  ? "border-brand bg-brand"
                  : missing
                    ? "border-line bg-surface-sunken"
                    : current
                      ? "border-brand-line bg-brand-soft"
                      : "border-line bg-line"
              }`}
            />
          );
        })}
      </div>
      <p className="text-xl text-ink-muted">{progressText(session)}</p>
    </div>
  );

  /* ---------------------------------------------------------------- 装载 / 不可用 */

  if (loading || loadError) {
    return (
      <TaskShell
        eyebrow="每日健康采集"
        title={loading ? "正在准备今天的采集" : "今天暂时问不了"}
        badge={
          <StatusBadge tone={loadError ? "warning" : "info"} size="md" pulse={!loadError}>
            {loadError ? "待会儿再来" : "正在连接"}
          </StatusBadge>
        }
        footer={
          <div className="flex items-center gap-4">
            {loadError && (
              <button
                type="button"
                onClick={() => void load()}
                className="h-24 flex-1 rounded-2xl border border-line bg-surface text-3xl font-semibold text-ink active:bg-surface-sunken"
              >
                再试一次
              </button>
            )}
            <button
              type="button"
              onClick={onExit}
              className="h-24 flex-1 rounded-2xl bg-brand text-3xl font-bold text-ink-inverse active:bg-brand-strong"
            >
              返回主界面
            </button>
          </div>
        }
      >
        <p className="text-2xl text-ink-muted">
          {loadError ?? "正在读取今天的问句，请稍等…"}
        </p>
      </TaskShell>
    );
  }

  /* ---------------------------------------------------------------- 终态 / 复核 */

  if (session && session.status !== "asking") {
    const ended = session.status === "ended";
    return (
      <TaskShell
        eyebrow="每日健康采集"
        title={ended ? "这次先到这里" : "今天问完了，谢谢您"}
        badge={
          <StatusBadge tone={ended ? "neutral" : "info"} size="md">
            {ended ? "已结束 · 未答项记为未采集" : "已提交 · 等待工作人员复核"}
          </StatusBadge>
        }
        progress={progress}
        footer={
          <button
            type="button"
            onClick={onExit}
            className="h-24 w-full rounded-2xl bg-brand text-3xl font-bold text-ink-inverse active:bg-brand-strong"
          >
            返回主界面
          </button>
        }
      >
        <div className="grid grid-cols-2 gap-4">
          {QUESTION_KEYS.map((key, index) => {
            const meta = KEY_META[key];
            const value = session.answers[key];
            const missing = session.missing.includes(key);
            return (
              <div
                key={key}
                className="flex items-center justify-between gap-4 rounded-2xl border border-line bg-surface-sunken px-6 py-5"
              >
                <span className="text-xl text-ink-muted">
                  {index + 1}. {meta.short}
                </span>
                {missing || value === undefined ? (
                  <StatusBadge tone="neutral" size="sm">
                    未采集
                  </StatusBadge>
                ) : (
                  <span className="text-2xl font-semibold text-ink">
                    {formatAnswer(key, value)}
                  </span>
                )}
              </div>
            );
          })}
        </div>
        <p className="mt-6 text-xl text-ink-muted">
          本页为演示模拟信息，不构成医疗诊断；分级结果由工作人员复核后录入。
        </p>
      </TaskShell>
    );
  }

  /* ---------------------------------------------------------------- 提问中 */

  const currentKey = isServerKey(session?.current_key) ? session.current_key : null;
  const meta = currentKey ? KEY_META[currentKey] : null;
  // 服务端问句是唯一口径（含「重问一次」的不同措辞）；没给时才用本地兜底文案。
  const questionText =
    (typeof session?.prompt?.prompt === "string" && session.prompt.prompt) ||
    meta?.label ||
    "请听题";
  const retrying = (session?.prompt?.attempt ?? 1) > 1;

  return (
    <>
      <TaskShell
        eyebrow="每日健康采集"
        title={questionText}
      badge={
        <StatusBadge tone={retrying ? "warning" : "info"} size="md" pulse={!retrying}>
          {busy ? "正在记录" : retrying ? "再问一次" : "正在等您回答"}
        </StatusBadge>
      }
      onExit={() => setConfirmExit(true)}
      exitLabel={exiting ? "正在结束…" : "结束采集"}
      progress={progress}
      footer={
        <div className="flex items-center gap-4">
          <button
            type="button"
            onClick={handleRetry}
            className="h-20 rounded-2xl border border-line bg-surface px-8 text-2xl font-semibold text-ink active:bg-surface-sunken"
          >
            没听清，再问一次
          </button>
          <button
            type="button"
            disabled={busy || exiting}
            onClick={() => void handleSkip()}
            className="h-20 rounded-2xl border border-line bg-surface px-8 text-2xl font-semibold text-ink-muted active:bg-surface-sunken disabled:opacity-50"
          >
            {retrying ? "仍答不上来，标缺失" : "这题先跳过"}
          </button>
          <span className="ml-auto text-2xl text-ink-muted">
            等待回答 {remaining} 秒
          </span>
        </div>
      }
    >
      <p className="text-2xl text-ink-muted">
        {currentKey ? `第 ${session?.attempt_no ?? 1} 次问这一项` : "正在准备下一问…"}
        {retrying && " · 您慢慢说，说数字就行"}
      </p>

      {/* 文字按钮兜底：麦克风权限被拒时仍能完成采集（FR-UI-03）。
          数字键由系统键盘决定，不在这里塞 inputmode —— 强塞会挡住按钮。 */}
      {currentKey && meta && meta.kind === "number" && (
        <div className="mt-8 flex items-end gap-4">
          <label className="flex flex-col gap-3">
            <span className="text-xl text-ink-muted">也可以直接输入数字</span>
            <input
              value={input}
              onChange={(event) => setInput(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") handleNumber();
              }}
              placeholder={meta.placeholder}
              className="h-20 w-72 rounded-2xl border border-line bg-surface px-6 text-4xl text-ink outline-none placeholder:text-ink-subtle focus:ring-2 focus:ring-brand"
            />
          </label>
          <button
            type="button"
            onClick={handleNumber}
            disabled={busy || input.trim() === ""}
            className="h-20 rounded-2xl bg-brand px-12 text-3xl font-bold text-ink-inverse active:bg-brand-strong disabled:opacity-50"
          >
            确认
          </button>
          <span className="pb-5 text-2xl text-ink-muted">{meta.unit}</span>
        </div>
      )}

      {currentKey === "mood_score" && (
        <div className="mt-8 grid grid-cols-3 gap-4">
          {MOOD_OPTIONS.map((option) => (
            <button
              key={option.value}
              type="button"
              disabled={busy}
              onClick={() => void submit(option.value)}
              className="h-20 rounded-2xl border border-line bg-surface text-3xl font-semibold text-ink active:bg-surface-sunken disabled:opacity-50"
            >
              {option.label}
            </button>
          ))}
        </div>
      )}

      {notice && (
        <p className="mt-6 rounded-2xl border border-line bg-surface-sunken px-6 py-4 text-2xl text-ink">
          {notice}
        </p>
      )}

      {/* 已答摘要：缺失项用中性徽标，不靠颜色判等级 */}
      <div className="mt-8 flex flex-wrap items-center gap-3 border-t border-line pt-6">
        <span className="text-xl text-ink-muted">已记录</span>
        {QUESTION_KEYS.filter(
          (key) =>
            session?.answers[key] !== undefined || session?.missing.includes(key),
        ).map((key) => (
          <span
            key={key}
            className="rounded-xl border border-line bg-surface-sunken px-4 py-2 text-xl text-ink"
          >
            {KEY_META[key].short}
            {"："}
            {session?.missing.includes(key)
              ? "未采集"
              : formatAnswer(key, session?.answers[key])}
          </span>
        ))}
        {!session ||
          (Object.keys(session.answers).length === 0 && session.missing.length === 0 && (
            <span className="text-xl text-ink-subtle">还没有记录</span>
          ))}
      </div>
      </TaskShell>
      {exitConfirmDialog}
    </>
  );
}
