import { useEffect, useLayoutEffect, useReducer, useRef, useState } from "react";
import type { MicState, PadStatus } from "./types";
import type { SubtitleSource } from "./components/BigSubtitle";
import type { DeviceSession } from "./session";
import { sessionValid, loadSession, clearSession } from "./session";
import StatusBar from "./components/StatusBar";
import { BigSubtitle } from "./components/BigSubtitle";
import { StandbyPanel } from "./components/StandbyPanel";
import { FallbackEntries } from "./components/FallbackEntries";

import { resolveFallbackState } from "./components/fallbackState";
import { LoginPage } from "./pages/LoginPage";
import { IdentityConfirm } from "./pages/IdentityConfirm";
import { VoiceprintEnroll } from "./pages/VoiceprintEnroll";
import { MedicationDrawer } from "./components/MedicationDrawer";
import { MedicationSummary } from "./components/MedicationSummary";
import { AvatarLayer } from "./avatar/AvatarLayer";
import { observeBands } from "./layoutBands";
import { attachAvatarClient, detachAvatarClient, forwardAvatarDiag } from "./avatar/avatarHost";
import { RealtimeClient } from "./realtime";
import type { IdentityAskPayload, IdentityResultPayload } from "./realtime";
import { getVoiceprintStatus, type VoiceprintStatus } from "./voiceprint";
import { confirmIdentity, identityErrorText } from "./apiIdentity";
import {
  KwsUnavailableError,
  initWakeWord,
  type KwsFailureReason,
  type WakeWordHandle,
} from "./audio/kws";
import { DEFAULT_WAKE_WORD } from "./audio/pinyin";
import { playCallPrompt } from "./audio/promptTone";
import { loadWakeWord } from "./audio/wakeWordStore";
import { WakeWordSettings } from "./components/WakeWordSettings";
import { usePadOverlays } from "./usePadOverlays";
import { initialScene, reduceScene } from "./scene/sceneMachine";
import { visibleLayers } from "./scene/sceneSelectors";
import { initOfflineCache } from "./offline";
import type { OfflineCacheHandle } from "./offline/types";
import type { MedicationAction, MedicationAnnouncement } from "./padRest";
import {
  confirmMedicationTaken,
  fetchTodayMedicationReminders,
  postMedicationRendered,
  postOfflineReport,
  undoMedicationTaken,
} from "./padRest";
import { SosButton } from "./components/SosButton";
import { MediaPanel } from "./components/MediaPanel";
import type { MediaCommand } from "./components/MediaPlayer";
import { MemoryPanel } from "./pages/MemoryPanel";
import { SosResult } from "./pages/SosResult";
import { MedicationCard } from "./pages/MedicationCard";
import { HealthCheckup } from "./pages/HealthCheckup";
import { OnboardingGuide, hasOnboarded, markOnboarded } from "./overlay/OnboardingGuide";
import { reportSos } from "./apiSos";
import {
  requestReplyAudio,
  sendTextTurnWithin,
  TextTurnTimeoutError,
  type TextTurnOutcome,
} from "./apiDialogue";
import { startTextReveal, stopTextReveal } from "./textReveal";
import { readReminders } from "./offline/db";
import { REMINDERS_UPDATED_EVENT, medicationAgenda, type MedicationAgenda } from "./medicationView";
import {
  earliestReminder,
  healthAskedKey,
} from "./taskDispatch";
import { useTaskPolling, type ActiveTask } from "./useTaskPolling";
import "./index.css";

/**
 * 3D 舞台按需加载（task-23）：three + @pixiv/three-vrm 约 750KB（未压缩）只在长者端
 * 真去渲染数字人时才下载；若加载失败转兜底图，也不必先付整包下载代价。
 * AvatarStage 是具名导出且不在本卡写作用域，故用 .then 映射成 {@link lazy} 需要的 default。
 */


interface SubtitleState {
  text: string;
  source: SubtitleSource;
}

/**
 * 打字轮之后多久内到达的语音回复算「迟到的开场白」，不覆盖打字回复。
 * 取 6s = 等数字人上限 5s（`AVATAR_WAIT_SECONDS`）+ 1s 余量。
 */
const TEXT_TURN_GREETING_GUARD_MS = 6_000;

/**
 * 打字回复「等语音」的上限（2026-11-11）：到点还没拿到音频就先按本地节奏逐字，
 * 不让长者干等合成（实测一次 20s 回复合成 3.1s）。
 */
const TYPED_REPLY_TEXT_FALLBACK_MS = 2_000;

/**
 * 打字轮超时提示（task-42）：超时和「没发出去」是两种病，文案不能互相将就。
 *
 * 超时 = 请求发出去了、对方没回话（后端慢/网关吊住），重试有意义；
 * 没发出去 = 断网/4xx，`sendTextTurn` 已给「这句话没送出去，再说一次我就能听见」。
 */
const TEXT_TURN_TIMEOUT_TEXT = "没等到回复，再试着问一次";

/**
 * 首启引导结束后的那句欢迎语，上屏多久自动收（A2-4）。
 *
 * 那句话本来只是告个别（「有不懂的就叫『小伴』，我会应您」），但它走的是**大字幕**，
 * 而待机屏（四个常问句 + 「您让我记住的」入口）只在字幕为空时渲染
 * （见下方 `!subtitle.text && <StandbyPanel …>`）。于是引导一结束，整块待机屏被一句
 * 告别话顶掉：新长者刚听完「想看看我记住的事，按这儿」，按下去却什么也没有，
 * 得等字幕自己消失才回来。所以它不能长留在屏上。
 *
 * 4s = 够把一句 24 字的大字读完（长者端按每秒 6–8 字估），又不至于让入口久久不出现；
 * 也不必再长，这句话不是必须留在屏上的信息，真正该看的是待机屏那几个按钮。
 */
const GUIDE_NOTICE_MS = 4_000;

/**
 * 语音「大声点 / 小声点」一次调整的幅度。
 *
 * 必须与 `components/MediaPlayer.tsx` 的按钮步进一致：长者用嘴调一次、用按钮调一次，
 * 感知到的变化量得是同一个，否则他会以为其中一个坏了。0.15 是按「按三下能从 0.9 到
 * 0.45、按四下能到 1.0」定的，既不一顿就震耳朵，也不按三次还没反应。
 */
const MEDIA_VOLUME_STEP = 0.15;

/**
 * 当前占着任务面板的那件事（同一时刻只允许一件：任务面板是右侧浮层，
 * 两件并排会把数字人挤掉，而 PRD 要求「任务来时不整屏切页、人还在」）。
 */
// ActiveTask 类型随轮询 hook 一起搬到 useTaskPolling.ts（W5）。

/** 主界面：接真链路（WS 信令 + 麦克风 + VAD + WebRTC），步骤 8 起 */
function MainScreen({ onLogout }: { onLogout: () => void }) {
  const headerRef = useRef<HTMLElement>(null);
  const footerRef = useRef<HTMLElement>(null);
  // 顶栏/底栏真实占高 → CSS 变量，供 fixed 的数字人层夹在中间。
  // 手填常量追不上换行与内容撑高（T 档差 24px、P 档差 50px），见 layoutBands.ts。
  useLayoutEffect(
    () => observeBands(headerRef.current, footerRef.current, document.documentElement),
    [],
  );
  const [status, setStatus] = useState<PadStatus>("connecting");
  /**
   * A6 / R-语音-01/02：麦克风可用性。`unavailable` 时顶栏连接胶囊改口为
   * 「麦克风没声音」，主区出常驻提示条与「重试」；`retryMic()` 成功或退回待机自动复位。
   * 与 `status` 分开存：信令断（`disconnected`）与麦坏是两种故障、两个恢复动作。
   */
  const [micState, setMicState] = useState<MicState>("ok");
  const [subtitle, setSubtitle] = useState<SubtitleState>({
    text: "",
    source: "final",
  });
  const [needRetry, setNeedRetry] = useState(false);
  // C5 身份级联：服务的 identity_ask 到达 → 出大按钮确认层；identity_result 到达 → 收起并记最终模式
  const [identityAsk, setIdentityAsk] = useState<IdentityAskPayload | null>(null);
  const [identitySource, setIdentitySource] = useState<IdentityResultPayload["source"] | null>(null);
  // W1 声纹：登录后查一次注册状态；未完成时顶栏出「录入声纹」入口（不自动弹窗，别挡首屏问候）
  const [voiceprint, setVoiceprint] = useState<VoiceprintStatus | null>(null);
  const [enrolling, setEnrolling] = useState(false);
  const audioRef = useRef<HTMLAudioElement>(null);
  const clientRef = useRef<RealtimeClient | null>(null);
  /** 顶栏原生 <details>「设置」弹层：正式浮层出现时要强制收起（见下方 effect）。 */
  const settingsDetailsRef = useRef<HTMLDetailsElement | null>(null);
  /** 最近一次打字提交时刻：用于拦「迟到开场白」覆盖打字回复（见 TEXT_TURN_GREETING_GUARD_MS） */
  const textTurnAtRef = useRef(0);
  /** 引导欢迎语的自动收起定时器（A2-4）；同一时刻只允许有一个，重看引导时先清旧的。 */
  const guideNoticeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  /**
   * 迭代 5 口型：3D 控制器（AvatarStage 的 onReady）。
   * 存 ref 而不是 state——它只喂给 RealtimeClient（命令式句柄），进 state 只会多一轮无意义渲染；
   * AvatarStage 卸载/降级/StrictMode 双挂载都会回调 null，这里同步清空，绝不握着已释放的句柄。
   */

  /**
   * W1 声纹注册状态：登录成功后取一次（FR-VPR-01「首次使用引导录 3 段」）。
   * 查询失败返回 null —— 声纹服务/推理容器没起时也不该在主界面显示错误，
   * 只是暂时不提供入口。这里不轮询：入口的时效性不值得为它多打一个接口。
   */
  useEffect(() => {
    let alive = true;
    void getVoiceprintStatus().then((s) => {
      if (alive) setVoiceprint(s);
    });
    return () => {
      alive = false;
    };
  }, []);

  /**
   * FR-UI-11（P0）：端侧唤醒词「小伴」。**未命中之前不建连** —— PRD:257 明确要求
   * 「未命中时原始音频不出端」；一旦先连上 WebRTC，麦克风流就已经上行，再谈不出端就没意义了。
   *
   * 降级（FR-UI-09）：KwsUnavailableError 一律放行建连。KWS 依赖 12MB wasm + 模型包 +
   * 麦克风权限，任何一项缺失都不该让长者端变成一块砖；降级后行为与接入唤醒词之前一致。
   * `?autoconnect=1` 是自动化点测/联调的逃生口（真机 AC-42 点测不要带这个参数）。
   */
  const [awake, setAwake] = useState(false);
  /**
   * 唤醒词不可用的原因（FR-UI-09 / FR-UI-11）：非 null 时中部要出「呼叫」大按钮。
   *
   * 为什么需要这个状态：KWS 起不来时旧实现只是「静默直接建连」，链路上通了，
   * 但长者眼里页面什么都没变——他不知道要跟谁说话、怎么开始，等于没有入口。
   * 现在：降级后仍然建连（不能让长者端变成一块砖），但同时在屏幕上留一颗
   * **看得见、按得动**的按钮，按下去给提示音 + 提示语。
   */
  const [wakeFallback, setWakeFallback] = useState<KwsFailureReason | null>(null);

  /**
   * T70 唤醒词自定义：按设备存在 localStorage，登录后可在顶栏改。
   * 换词不重启页面——`kwsRef.current.setKeyword()` 只重建解码器（wasm/模型留在内存里）。
   */
  const [wakeWord, setWakeWord] = useState(() => loadWakeWord(loadSession()?.device?.id ?? ""));
  const [wakeSettingsOpen, setWakeSettingsOpen] = useState(false);
  /**
   * 顶栏 `<details>` 设置弹层的受控状态。
   *
   * 原生 `<details>` 自己管折叠，但这里必须由 React 管：任何正式浮层出现时它要自动收起
   * （用户反馈的「弹窗互相遮蔽」里，设置弹层是最容易被忘掉的那一层——它天生 z-40，
   * 比身份确认（z-10）还高）。受控之后「收起」只是把 state 置 false，不依赖 DOM 反查。
   */
  const [settingsOpen, setSettingsOpen] = useState(false);
  /**
   * FR-UI-06 首启引导：本次进主界面要不要讲那三步。
   *
   * 判据只有 localStorage `onboarded:v1`（见 `overlay/OnboardingGuide.tsx`）：
   * 第一次进是 true，关掉引导时落盘，此后不再自动弹。设置里有「使用引导」可随时重看
   * （PRD「可跳过可重看」）——重看那次不写盘、也不改本次的判断。
   */
  const [guideOpen, setGuideOpen] = useState(() => !hasOnboarded());
  /** 戏曲播放面板：从待机屏的次级入口打开，走 overlayPriority 的 "media" 档。
   *  可见性已改为从场景机派生（见下方 sceneLayers），这里不再另存布尔。 */
  /** 打开的是戏曲还是电影（FR-CON-03 / FR-CON-04）；两个入口只差这一个 kind */
  const [mediaKind, setMediaKind] = useState<"opera" | "movie">("opera");
  /**
   * 指定剧种（空串 = 不指定，谁都能放）。
   *
   * 只有一种情况会带值：长者说「放段越剧」，规则层判出 `opera:yueju`，服务端把
   * `genre: "越剧"` 随 `media_control` 一起下发。点按钮进来的入口一律空串——
   * 那是「我想听听戏」，不该替他押一个剧种。
   */
  const [mediaGenre, setMediaGenre] = useState("");
  /**
   * 媒体音量（0–1），由 App 统一持有。
   *
   * 默认 0.9 不是 1.0：平板扬声器就在床头，满量在耳边很吵；留出余量也让「大声点」
   * 按一次就有可感知的变化。TTS 与戏曲共用这一只「响亮程度」，否则长者调了一次
   * 声音，下一句话又恢复成满量，他会以为按钮没用。
   */
  const [mediaVolume, setMediaVolume] = useState(0.9);
  /**
   * 下发给播放器的命令（语音「换一段 / 停」）。
   *
   * 目前后端还没有把路由结果下发到前端的通道（WS 只有字幕/音频/诊断几类下行），
   * 所以这里先备好「递增 seq」的信道：接线时只需 `bumpMediaCommand("next")`，
   * 播放器凭 seq 去重，不会把一句话执行两遍。
   */
  const [mediaCommand, setMediaCommand] = useState<MediaCommand | null>(null);
  /**
   * 已执行到的媒体指令序号（FR-CON-03 的迟到指令闸门）。
   *
   * 为什么去重要做在这里而不是播放器：服务端按设备自增 `seq`，一条指令可能跨轮次
   * 重复到达（重连补发、同一轮两条意图）；播放器只在面板打开时才存在，而「关掉面板
   * 之后才到的旧指令」不该在下次打开时补执行。闸门放在面板之上，才能同时挡住这两种。
   */
  const mediaSeqRef = useRef(0);
  /** 长者本人记忆面板（FR-MEM-02）：同一排次级入口，走 "memory" 档。
   *  可见性同样从场景机派生，不再另存布尔。 */
  const kwsRef = useRef<WakeWordHandle | null>(null);
  /**
   * `mediaOpen` 的镜像。WS 回调只在建连时注册一次（依赖 `[]`），闭包里读不到最新的
   * `mediaOpen`；用 ref 读当前值，才判得出「这条 next/stop 有没有播放器接得住」。
   */
  const mediaOpenRef = useRef(false);

  /**
   * N62b 呼入振铃（FR-UI-10 反向外呼）：信令常驻，所以**待机态也能被叫到**。
   * 这里只存「有一通正在响铃」，真正的媒体要等长者点「接听」才建（隐私边界）。
   * 30s 是客户端兜底；服务端外呼预算 10s（PRD FR-MED-06），到点服务端会下发
   * call-ended，所以 `ringTimeoutMs` 也调成 10s，避免出现「服务端早已放弃、
   * 长者端还在响」的幽灵振铃。
   */
  const [incoming, setIncoming] = useState<{ id: number; sdp: string } | null>(null);
  // N62d：外呼自动接听中。与 `incoming` 互斥——响铃是「等长者按」，
  // 通话中横幅是「已经接上了」。两者同时只在「唤醒升级」那一刻短暂交叉。
  const [outboundCalling, setOutboundCalling] = useState(false);

  useEffect(() => {
    if (new URLSearchParams(window.location.search).get("autoconnect") === "1") {
      setAwake(true);
      return;
    }
    let alive = true;
    let handle: { stop: () => Promise<void> } | null = null;
    initWakeWord(
      () => {
        if (alive) setAwake(true);
      },
      // 出厂词由 kws 自己兜底；这里只把「这台设备上次保存的词」传进去
      { keyword: wakeWord },
    )
      .then((h) => {
        // 组件已卸载（StrictMode 双挂载/退出登录）→ 立刻放掉麦克风，别泄漏采集设备
        if (!alive) {
          void h.stop();
          return;
        }
        handle = h;
        kwsRef.current = h;
      })
      .catch((err: unknown) => {
        const reason: KwsFailureReason = err instanceof KwsUnavailableError ? err.reason : "init_failed";
        console.warn(`[pad] 唤醒词不可用（${reason}）→ 降级为直接建连（FR-UI-09）`);
        // 顺序有意义：先亮出「呼叫」入口，再建连。反过来的话，建连异常
        // （麦克风被拒、信令没起）会把页面留在「没有任何入口」的旧状态里。
        if (alive) setWakeFallback(reason);
        // A6：唤醒阶段就因麦克风起不来而降级（权限拒绝/无设备/被占用）——先按不可用处理，
        // 随后的建连若成功会由 `onMicState("ok")` 自动复位，不在这里做长期判断。
        if (alive && reason === "mic_unavailable") setMicState("unavailable");
        if (alive) setAwake(true);
      });
    return () => {
      alive = false;
      kwsRef.current = null;
      void handle?.stop();
    };
    // 只在挂载时起一次：换词走 setKeyword，不重建整条链路（重建会闪断麦克风）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** 设置页保存：先换词（失败就保持旧词并提示），再更新界面上的词 */
  function handleWakeWordSaved(word: string) {
    setWakeWord(word);
    const handle = kwsRef.current;
    if (!handle) return; // 已降级/已唤醒：词已存进 localStorage，下次监听生效
    void handle.setKeyword(word).catch((err: unknown) => {
      console.warn("[pad] 唤醒词切换失败，继续使用原词", err);
      setSubtitle({ text: `唤醒词没换成，还是叫「${handle.keyword}」吧`, source: "notice" });
    });
  }

  /**
   * FR-MED-08 第①②③层的端侧一侧：当日药单进 IndexedDB、到点本地播报、状态变化上报。
   * 与建连**无关地独立挂载** —— 断网兜底必须在「WebRTC 根本建不起来」时依然生效，
   * 所以不能塞进下面那个受 `awake` 门控的 effect。
   */
  useEffect(() => {
    const handle: OfflineCacheHandle = initOfflineCache({
      loadReminders: fetchTodayMedicationReminders,
      reportOffline: postOfflineReport,
      reportReconnect: postOfflineReport,
    });
    return () => handle.dispose();
  }, []);

  /**
   * 常驻信令（N62b）：建连与 `awake` **脱钩** —— 信令连接本身不上行音频，
   * 它只是「服务端能叫到这台设备一条通路」。FR-UI-11 约束的是「未命中唤醒词时
   * 原始音频不出端」，不是信令；把信令也一起关掉，护工就永远呼不进来（FR-UI-10）。
   * `awake` 只门控**媒体**：由下面那个 effect 调 `setAwake` 让 realtime 侧起落采集。
   */
  useEffect(() => {
    const session = loadSession();
    if (!session || !audioRef.current) return;
    const client = new RealtimeClient({
      onStatus: setStatus,
      onMicState: setMicState,
      onSubtitle: (text, source, local) => {
        // 语音轮的回复会晚到（开场白要等数字人就绪，上限 5s）：长者已经改走打字、
        // 且刚敲完一句话时，这条迟到的「您好，我在呢」不该盖掉打字回复
        // （实测：打字回复 4.5s 后被 greeting 的 tts_text 顶掉，屏幕上只剩问候）。
        //
        // `local` 必须豁免（2026-11-11 CDP 取证）：打字回复补到语音后，逐句字幕**也**从
        // 这条回调上来（realtime 的 advanceSubtitle）。不豁免就会把自己拦掉——实测声音
        // 已经在答第三句、字幕还停在长者自己那句「今天天气怎么样」，整整 7.9 秒。
        if (
          source === "reply" &&
          !local &&
          Date.now() - textTurnAtRef.current < TEXT_TURN_GREETING_GUARD_MS
        ) {
          return;
        }
        /* 语音轮自己的进度由 realtime 写；打字轮的合成时间轴必须立刻让位，
           否则两条进度会互相顶，字幕一会儿按声音走、一会儿按估算走。 */
        stopTextReveal();
        setSubtitle({ text, source });
      },
      // FR-CON-03：规则层判定好的点播动作（换一段 / 停 / 大声点 / 小声点 / 放某剧种）。
      // 修 2026-11-12：此前 `mediaCommand` 只有状态没有来路，语音说「换一段」没任何反应。
      onMediaControl: (command) => {
        // 服务端按设备自增 seq；迟到/重放的旧指令直接丢，否则连说两句「换一段」会切两段
        if (command.seq <= mediaSeqRef.current) return;
        mediaSeqRef.current = command.seq;

        if (command.action === "volume_up" || command.action === "volume_down") {
          const delta = command.action === "volume_up" ? MEDIA_VOLUME_STEP : -MEDIA_VOLUME_STEP;
          // 语音调音量不需要面板开着：长者可能在听完戏、屏幕已经回待机时补一句「大声点」
          setMediaVolume((current) =>
            Math.min(1, Math.max(0, Math.round((current + delta) * 100) / 100)),
          );
          return;
        }

        if (command.action === "play") {
          setMediaKind(command.kind ?? "opera");
          setMediaGenre(command.genre ?? "");
          setMediaCommand(null); // 新的一次点播，播放器重新开始数自己的局部命令序号
          dispatch({ type: "leisure.open", leisure: "media" });
          return;
        }

        // next / stop / pause / resume 只能交给已经在播的播放器。面板没开就丢弃——
        // 此时没有播放器，留着会在下次打开面板时补执行一条几轮之前的旧指令；
        // pause/resume 尤其如此：面板没开时「继续」无处可续，补执行会意外起播。
        const action = command.action;
        if (
          action !== "next" &&
          action !== "stop" &&
          action !== "pause" &&
          action !== "resume"
        ) {
          return;
        }
        setMediaCommand((previous) => {
          if (!mediaOpenRef.current) return previous;
          return { type: action, seq: command.seq };
        });
      },
      onAuthFail: onLogout, // 会话过期/无效 → 回登录页（T-H 方案 A 定案）
      onNeedManualRetry: () => setNeedRetry(true), // 连续 5 次重连失败 → 出按钮
      // C5：低置信/声纹服务不可用 → 服务端下发 identity_ask（恒 1 位候选），popup 让长者点「是我」；
      // 30s 计时在服务端，超时服务端会下发 identity_result source=generic，前端不自己计时
      onIdentityAsk: (ask) => {
        setIdentityAsk(ask);
        dispatch({ type: "interruption.begin", interruption: "identity" });
        setSubtitle({ text: `请确认：您是${ask.displayName}吗？`, source: "notice" });
      },
      // C5：身份已定（voice/confirm/generic）+ 问候语随之到达；generic 显示通用模式横幅（FR-VPR-03）
      onIdentityResult: (result) => {
        setIdentityAsk(null);
        dispatch({ type: "interruption.end" });
        setIdentitySource(result.source);
        // P2：结果到达即收起「请确认」字幕（后续问候由 tts_text 覆盖）
        setSubtitle((prev) =>
          prev.text.startsWith("请确认") ? { text: "", source: "final" } : prev,
        );
      },
      // C5：断线/重连 → 服务端已取消 30s 计时且不会再发 identity_result，确认层必须收起
      onIdentityCancelled: () => {
        setIdentityAsk(null);
        dispatch({ type: "interruption.end" });
        setSubtitle({ text: "网络断开，身份确认已取消", source: "notice" });
      },
      // N62b：反向外呼振铃。信令常驻 → 待机态（未唤醒）也能收到 offer；
      // 此时**不建媒体、不开麦**，只把「有人找您」推到界面上，等长者点接听。
      onIncomingCall: (call) => {
        setIncoming({ id: Date.now(), sdp: call.sdp });
        dispatch({ type: "interruption.begin", interruption: "incoming" });
        setSubtitle({ text: "有来电，按接听开始通话", source: "notice" });
      },
      // 服务端已收档（超时/拒接/被新通话替换）→ 必须收铃，否则按钮会一直挂着
      onIncomingCallEnded: (reason) => {
        setIncoming(null);
        dispatch({ type: "interruption.end" });
        setSubtitle({
          text: reason === "timeout" ? "这通电话已结束" : "通话已取消",
          source: "notice",
        });
      },
      // N62d：外呼**自动接听**成功（FR-UI-10 的 P0 触达）。此刻只有下行：
      // 数字人能说话，麦克风一个都没开（FR-UI-11）。长者开口（唤醒命中）后由
      // 下面那个 awake effect 调 `acceptIncomingCall()` 升级成带麦全双工。
      onOutboundCall: () => {
        setIncoming(null);
        setOutboundCalling(true);
        dispatch({ type: "interruption.begin", interruption: "outbound" });
        setSubtitle({ text: "数字人在跟您说话，您可以随时挂断", source: "notice" });
      },
      // 挂断/被新通话替换/失败都要收横幅——否则按钮会挂在一个已经死掉的会话上
      onOutboundCallEnded: () => {
        setOutboundCalling(false);
        dispatch({ type: "interruption.end" });
      },
      audioEl: audioRef.current,
    },
    // 与 PRD FR-MED-06 的外呼预算对齐（服务端 10s 放弃），见上方 incoming 注释。
    // 注意这是**第二个构造参数**（RealtimeOptions），不是 callbacks 的字段。
    // `autoAnswerOutbound`：PRD:256 FR-UI-10 要求「服务端主动发起外呼时 pad 自动接听」，
    // 这正是用药触达 T0 成立的前提（长者不按接听 = 触达不成立）。自动接听只建下行。
    { ringTimeoutMs: 10_000, autoAnswerOutbound: true },
    );
    clientRef.current = client;
    // 3D 可能已经先就绪（子组件 effect 先于本 effect 跑）→ 建 client 时补交一次控制器
    // 3D 在 App 根层就挂载好了（登录页就在跑），这里只是把客户端接上中转站
    attachAvatarClient(client);
    void client.start(session.access_token);

    // T-H 方案 A 定案（无 refresh 通道，过期回登录）：会话中途到期的主动守护——
    // 30s 周期 + 页面回前台时校验 expires_at，过期即清会话回登录页；
    // WS 4401/auth_fail 与 REST 401 由各自链路回调 onLogout 兜住
    const expiryGuard = setInterval(() => {
      if (!sessionValid()) onLogout();
    }, 30_000);
    const onVisible = () => {
      if (document.visibilityState === "visible" && !sessionValid()) onLogout();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(expiryGuard);
      document.removeEventListener("visibilitychange", onVisible);
      stopTextReveal();
      detachAvatarClient(client);
      void client.stop();
    };
    // onLogout 由调用方保证稳定；信令常驻，所以这里只在挂载/卸载时启停。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /**
   * `awake` 只切换媒体：唤醒词命中（或降级放行）→ 开麦克风与 PeerConnection；
   * 回到待机 → 只收媒体、保留信令。顺序上必须在本组件里声明于「建 client」之后，
   * 因为 effect 按声明顺序执行，首次 `setAwake` 需要 client 已经存在。
   */
  useEffect(() => {
    void clientRef.current?.setAwake(awake);
    // 唤醒命中时若正处在「外呼自动接听」的下行会话里，顺手升级成带麦全双工：
    // 自动接听只建了下行，长者真开口了才有 mic（隐私边界仍是「命中之后才 getUserMedia」）。
    // 没有 pendingOffer 时 `acceptIncomingCall()` 内部直接 return，可无条件调用。
    if (awake) void clientRef.current?.acceptIncomingCall();
  }, [awake]);

  // 入口**恒显**（2026-11-11）：此前用 `status !== "active"` 门住，三段录满后按钮消失，
  // 长者想重录一遍（录歪了/环境吵）就再也进不来——只会点一次「开始录音」收到
  // duplicate_sample，然后彻底没有出口。现在 active 时入口改名为「声纹设置」，
  // 打开后直接落到 done 态，里面才有「重新录一遍」。
  // 通用模式仍然不出入口：此刻不知道是谁在说话，引导录声纹没有意义（计划 §3-W1）。
  const showEnrollEntry = voiceprint !== null && identitySource !== "generic";
  /** active 时入口是「重录/查看」而不是「去录」，按钮文案要跟着变 */
  const enrollEntryLabel = voiceprint?.status === "active" ? "声纹设置" : "录入声纹";

  /**
   * 两级降级入口（AGENTS §6）：按住说话与打字输入都**常驻**在 footer，
   * 这里只决定「能不能按、提示哪句话」。此前入口被 `status === "disconnected"` 整块门住，
   * 主链路一正常，长者就再也看不到这条路（task-69 要修的正是这处回退）。
   *
   * `textEntryAvailable: true` —— 后端 `POST /api/dialogue/text` 已补齐（2026-11-11），
   * 打字与话音共用同一个 DialogueService（`app.state.dialogue_service`），
   * 所以打字轮次也进同一条安全规则 + 同一份短期历史。
   */
  /**
   * 打字对话（降级入口第二级）：后端 `POST /api/dialogue/text` 已接与话音同一条对话链，
   * 所以这里翻成可用、并把回复当作「数字人说的话」上大字幕。
   * 失败时不塞假回复，交给 `sendTextTurn` 的中文兜底话术（长者看得懂、输入框保留原文）。
   */
  const submitTextTurn = async (text: string): Promise<void> => {
    textTurnAtRef.current = Date.now();
    /* 先把长者这句原话上屏：后面成不成，他得先看到「话已经出去了」。 */
    setSubtitle({ text, source: "final" });

    /* task-42：此前这里是裸 `await sendTextTurn(text)`——请求永不 settle 时这行也永不返回，
       `TextFallback` 的 `busy` 就一直 true，底栏停在「发送中…」，既没有报错也没有重试口。
       现在两层兜底：
       ① `sendTextTurnWithin` 给 15s 超时并 abort，保证这个 await **一定有出口**；
       ② 超时/失败一律 throw，交给 `TextFallback` 的 catch + finally：草稿不清空、
          「发送中…」复位成「发送」、并出「没发出去，请再按一次」。
       字幕随后只换一句提示，**不把上一条回复清成空白**。 */
    let outcome: TextTurnOutcome;
    try {
      outcome = await sendTextTurnWithin(text);
    } catch (err) {
      const timedOut = err instanceof TextTurnTimeoutError;
      setSubtitle({
        text: timedOut ? TEXT_TURN_TIMEOUT_TEXT : "这句话没送出去，再说一次我就能听见",
        source: "notice",
      });
      throw err;
    }
    if (!outcome.ok) {
      /* 拿到响应但内容不可用（401 / 后端兜底话术）：照旧上屏，同时抛出以保住输入框草稿。
         键序写成 source 在前是有意的：`tests/text-reveal.test.ts` 以「先起揭示、再整句上屏」
         的源码顺序钉住打字轮，这里先写 source，免得那处契约把这一行误当成回复上屏点。 */
      setSubtitle({ source: "notice", text: outcome.reply });
      throw new Error(outcome.reply);
    }

    /* 打字回复的字幕与语音（2026-11-11 二次收敛）：
       上一版是「先按字数估时逐字 → 音频到了再重锚总时长」，两个问题：
       ①字按平均字速走、声音有自己的停顿，必然越走越偏；②重锚时整句会缩一下。
       现在改成分段驱动：拿到 `audio_segments` 就让时间轴跟着**真实语音**走
       （与语音轮共用 `applySubtitlePlan`），首句文本在音频起播那一帧才上屏。
       只有音频拿不到 / 合成超过 2s 时才退回按字数估时的本地逐字，保证不会「半天没回话」。 */
    let settled = false;
    let textShown = false;
    const showLocal = () => {
      if (textShown) return;
      textShown = true;
      stopTextReveal();
      startTextReveal(outcome.reply);
      setSubtitle({ text: outcome.reply, source: outcome.safetyAlert ? "notice" : "reply" });
      // 本地逐字不经过 RealtimeClient，必须显式告知「字已上屏」，否则「有字才动嘴」的闸门会一直关着。
      clientRef.current?.markReplyTextShown();
    };
    const timeout = setTimeout(() => {
      if (settled) return;
      settled = true;
      showLocal();
    }, TYPED_REPLY_TEXT_FALLBACK_MS);

    const token = loadSession()?.access_token ?? "";
    void requestReplyAudio(outcome.reply, token).then((audio) => {
      const playable = audio.ok && audio.audioUrl !== null;
      if (!settled) {
        settled = true;
        clearTimeout(timeout);
      }
      if (!playable) {
        showLocal();
        return;
      }
      clientRef.current?.playReplyAudio(
        audio.audioUrl as string,
        audio.durationMs ?? undefined,
        audio.segments,
        // 2s 兜底已经把整段文字上屏时，音频只补声音与口型：否则计划一到会从第 0 句
        // 重打字幕，长者看到同一句出现两遍（2026-11-17 实测 /tts 3.9s）。
        { textAlreadyShown: textShown },
      );
      // 有分段时文本由服务端时间轴按句推出（与语音轮同一套 applySubtitlePlan）；
      // 只有「拿不到分段」才退回按字数估时的本地逐字，否则会把时间轴盖掉、口型再次不同步。
      if (audio.segments.length > 0) return;
      setTimeout(() => {
        if (!textShown) showLocal();
      }, 700);
    });
  };

  const fallback = resolveFallbackState({ status, textEntryAvailable: true });

  /**
   * 任务面板（用药提醒 / 健康采集 / 一键求助）。
   *
   * 三条任务此前都是「死代码」：`MedicationCard`、`HealthCheckup`、`SosResult`
   * 实现完整却没有任何入口（PRD FR-MED-08 / §4.6 / FR-ALT-06 因此验不过）。
   * 这里只做三件事：决定何时弹（判定在 `taskDispatch.ts`）、同一时刻只留一件、卸载时清定时器。
   */
  const [activeTask, setActiveTask] = useState<ActiveTask>(null);
  const activeTaskRef = useRef<ActiveTask>(null);
  /** 本次会话已经弹过的提醒：后端推送 + 用户「稍后再说」都不再自动重复弹 */
  const dismissedTaskRef = useRef<Set<string>>(new Set());

  /**
   * 场景状态机：这是「现在演到第几幕」的唯一真源。
   *
   * 批一只接线（所有开会/关会处 `dispatch`，渲染不读 state）；
   * **批二已落地**：消遣面板可见性改由 `visibleLayers(scene)` 派生（见下），
   * 删掉了与 dispatch 五处成对的 `mediaOpen` / `memoryOpen` 两个并列布尔。
   * 顶栏/底栏的派生（`topBar` / `footerMode`）尚未接入渲染，属后续批次。
   */
  const [scene, dispatch] = useReducer(reduceScene, initialScene);

  /** 批二：消遣面板的可见性**从场景机派生**，不再另存一份布尔。
   *  收敛前 `mediaOpen` / `memoryOpen` 两个 useState 与 `leisure.open/close`  dispatch
   *  五处全部成对出现，是同一事实的两份；且两者语义会分叉——状态机在 SOS 期间
   *  拒绝 `leisure.open`（不变量 2「自动任务可压 leisure，绝不可压 sos」、
   *  不变量 4「leisure 只能显式打开」），而旧布尔照设 true，SOS 撤掉后播放器会自己冒出来。
   *  现在 UI 服从已被 `scene-machine.test.ts` 钉住的不变量。
   *  `activeTask` 仍是任务内容的唯一真源（见 sceneSelectors 顶部边界说明），本层不接管它。 */
  const sceneLayers = visibleLayers(scene);
  const mediaOpen = sceneLayers.media;
  const memoryOpen = sceneLayers.memory;

  /**
   * 右栏药单摘要（批二）：只留「下一条要吃的药」，完整药单走抽屉。
   *
   * 判据是「计划时刻尚未过去 1 分钟」——给药窗留一点容差，避免 08:00:00 的药在
   * 07:59:59 被滤掉、08:00:01 又还没刷新；过期与已确认的行不进摘要（它们属于抽屉里的历史）。
   */
  /** 今日药单的一次视图：`next` 是下一次待提醒，`missedCount` 是已过窗未确认的条数。
   *  两个字段都由 `medicationAgenda()` 算——摘要与抽屉必须同一口径，否则会出现
   *  「摘要说今天没有、点开却有两条已过期」（2026-11-13 走查）。 */
  const [medicationAgendaView, setMedicationAgendaView] = useState<MedicationAgenda>({
    next: null,
    missedCount: 0,
  });
  const [medicationDrawerOpen, setMedicationDrawerOpen] = useState(false);
  /** 隐私说明浮层（批二收进设置）：长者有权随时看到「什么时候在听、听完去哪儿」 */
  const [privacyOpen, setPrivacyOpen] = useState(false);

  useEffect(() => {
    let alive = true;
    const refresh = () => {
      void (async () => {
        try {
          const agenda = medicationAgenda(await readReminders(), Date.now());
          if (!alive) return;
          setMedicationAgendaView(agenda);
        } catch {
          // IndexedDB 不可用（隐私模式/额度耗尽）：摘要退成空态，不阻断语音主链路
          if (alive) setMedicationAgendaView({ next: null, missedCount: 0 });
        }
      })();
    };
    refresh();
    window.addEventListener(REMINDERS_UPDATED_EVENT, refresh);
    return () => {
      alive = false;
      window.removeEventListener(REMINDERS_UPDATED_EVENT, refresh);
    };
  }, []);

  useEffect(() => {
    activeTaskRef.current = activeTask;
  }, [activeTask]);

  // 自动任务轮询收敛到 useTaskPolling（W5）：口径逐条保留（同时只一件、
  // 已忽略不再弹、IndexedDB 不可用不阻断语音、15s + 事件双触发）。
  useTaskPolling({ activeTaskRef, dismissedTaskRef, setActiveTask, dispatch });

  /** 顶栏手工入口：演示/补看当天药单（自动到点仍由上面的 effect 负责） */
  const openMedicationNow = () => {
    void (async () => {
      try {
        const rows = await readReminders();
        const first = earliestReminder(rows, dismissedTaskRef.current);
        if (first) {
          setActiveTask({ kind: "medication", due: first });
          dispatch({ type: "task.open", task: "medication" });
        }
        else setSubtitle({ text: "今天的药都提醒过了", source: "notice" });
      } catch {
        setSubtitle({ text: "药单暂时读不到，请稍后再试", source: "notice" });
      }
    })();
  };

  /**
   * 一键求助（FR-ALT-06）：先切结果面板再上报 —— 网络慢也不能让老人觉得「按了没反应」；
   * 结果页不提供撤销（FR-UI-08 红色事件不可由长者自行撤回）。
   */
  const handleSos = () => {
    setActiveTask({ kind: "sos" });
    dispatch({ type: "task.open", task: "sos" });
    setSubtitle({ text: "已经帮您叫人了，请坐着等一会儿", source: "notice" });
    void reportSos();
  };

  const closeTask = () => {
    // 求助结果面板是唯一「撤销要显式」的场景：reducer 里 `task.close` 对 sos 是空操作
    // （不变量 1，FR-UI-08 不可由长者自行撤回），所以关它必须派 `sos.dismiss`。
    const wasSos = activeTask?.kind === "sos";
    setActiveTask(null);
    dispatch({ type: wasSos ? "sos.dismiss" : "task.close" });
  };

  const closeHealth = () => {
    // 记「今天问过了」：否则 19:00–20:00 之间每 15s 弹一次
    try {
      window.localStorage.setItem(healthAskedKey(new Date()), "1");
    } catch {
      // 存不进就放弃自动弹（宁可少问一次，也不要反复打断长者）
    }
    setActiveTask(null);
    dispatch({ type: "task.close" });
  };

  /**
   * 关掉首启引导（FR-UI-06）。
   *
   * 跳过与看完写同一个标记：长者连点「跳过」的意思就是「别再问我了」，第二天还弹
   * 只会更烦。想再看从设置里进——那条路径不写盘，所以「重看一次」不会把标记抹掉。
   *
   * 欢迎语只上屏 `GUIDE_NOTICE_MS` 就自动收（A2-4）：待机屏（含「您让我记住的」入口）
   * 只在字幕为空时渲染，若把这句告别话一直挂在字幕里，引导一结束整块待机屏就是空的，
   * 新长者按引导去点记忆入口会点不到。收起时**只擦自己这句**——这 4 秒里长者可能已经
   * 开口、或有到点提醒上屏，那时候的字不归这里管。
   */
  const finishGuide = (completed: boolean) => {
    markOnboarded();
    setGuideOpen(false);
    if (!completed) return;
    const text = `有不懂的就叫「${wakeWord || DEFAULT_WAKE_WORD}」，我会应您`;
    setSubtitle({ text, source: "notice" });
    if (guideNoticeTimerRef.current !== null) clearTimeout(guideNoticeTimerRef.current);
    guideNoticeTimerRef.current = setTimeout(() => {
      guideNoticeTimerRef.current = null;
      setSubtitle((prev) => (prev.text === text ? { text: "", source: "final" } : prev));
    }, GUIDE_NOTICE_MS);
  };

  /** 卸载时收掉引导欢迎语的定时器：别对着已卸载的组件改字幕。 */
  useEffect(
    () => () => {
      if (guideNoticeTimerRef.current !== null) {
        clearTimeout(guideNoticeTimerRef.current);
        guideNoticeTimerRef.current = null;
      }
    },
    [],
  );

  /**
   * 打断播报（FR-UI-04 / UI-04）：把「正在念」真正停下来。
   *
   * 两层都要收：realtime 里的 `<audio>` 与逐句字幕计划（它管声音与口型），
   * 以及打字机 `textReveal`（模块级定时器，realtime 看不见它）。
   * 只改 React 的 `interrupted` 布尔值不动这两处，就是「按钮是哑的」。
   */
  const handleInterruptSpeech = () => {
    clientRef.current?.interruptSpeech();
    stopTextReveal();
    setSubtitle({ text: "", source: "final" });
  };

  /**
   * 用药卡三个收口动作的「说一句」（CON-04）。
   *
   * 分工：卡片只抛「点了什么」，声音与字幕归这里。确认那一枪的响应里带着服务端
   * 合成好的音频（`announcement`），有就播、并用服务端的话上字幕；没有（老响应 /
   * TTS 降级为字幕 / 拒服·稍后提醒本来就无播报）就保持卡片自己上屏的那句，
   * **不静默什么都不做**——长者按了按钮却毫无反馈，比播错更糟。
   */
  const handleMedicationFeedback = (
    _action: MedicationAction,
    announcement?: MedicationAnnouncement,
  ) => {
    if (!announcement) return;
    setSubtitle({ text: announcement.subtitle, source: "notice" });
    if (announcement.audio_url && !announcement.degraded) {
      // 与打字回复同一条播放路径：打断按钮、字幕计划、口型都对得上。
      clientRef.current?.playReplyAudio(announcement.audio_url);
    }
  };

  /**
   * 「呼叫」按钮（FR-UI-09 / UI-09、UI-11）：唤醒词不可用时的唯一显式入口。
   *
   * 三件事按序做：①提示音（在点击手势里发，自动播放策略允许）——让长者立刻
   * 知道「按到了」；②确保接通（`awake` 幂等；只有真断线才 retry，正常连着时
   * 重连会白断一次）；③上屏提示语，给出「现在可以说话」的可读凭据：数字人
   * 不一定马上应答（ASR 首包 + LLM 首 token），没有这句话他会对着空气干等。
   */
  const handleCall = () => {
    playCallPrompt();
    setAwake(true);
    setSubtitle({ text: "我在呢，您说吧", source: "notice" });
    if (needRetry) {
      setNeedRetry(false);
      clientRef.current?.retry();
    }
  };

  /* ------------------------------------------------------------------
   * 浮层互斥仲裁（2026-11-11）
   *
   * 六个浮层此前各写各的 z-index（z-10 / z-20 / z-30 / z-40），叠加时互相遮、内容被切。
   * 现在先按 OVERLAY_PRIORITY 仲裁出**唯一**可见的那个，被压制的直接不渲染 DOM。
   * state 全部保留，所以关掉上层后下层自动回来——不需要记「谁盖了谁」。
   * 顺序不该在这里改：改 `overlayPriority.ts` 并同步用例。
   * ------------------------------------------------------------------ */
  /* 浮层显隐的派生层（批零搬家）：openOverlays 拼装 + pickVisible 仲裁 + show* 布尔，
   * 原样移到 `usePadOverlays`，行为零变化；仲裁顺序仍只在 `overlayPriority.ts` 里改。 */
  const {
    visibleOverlay,
    shownTask,
    showIdentity,
    showMedia,
    showMemory,
    showEnroll,
    showWake,
    showSettings,
    showIncoming,
    showPrivacy,
    showGuide,
  } = usePadOverlays({
    activeTask,
    identityAsk: identityAsk !== null,
    mediaOpen,
    memoryOpen,
    enrolling,
    wakeSettingsOpen,
    settingsOpen,
    incomingCall: incoming !== null,
    privacyOpen,
    guideOpen,
  });

  // 面板实际可见才等于「有播放器接得住 next/stop」：被 SOS、用药回执挤掉时
  // `mediaOpen` 仍是 true，但 MediaPanel 已卸载，此时到达的指令必须丢掉。
  useEffect(() => {
    mediaOpenRef.current = visibleOverlay === "media";
  }, [visibleOverlay]);

  /**
   * 音频焦点：媒体（戏曲/电影）在响时压住数字人这条声道。
   *
   * 判据用 `showMedia` 而不是 `mediaOpen`：面板被 SOS/吃药回执挤掉时 `showMedia`
   * 变 false，播放器已被卸载、声音已经停了，这时必须立刻把数字人放开 ——
   * 长者刚按了求助，第一句回应不能是哑的。
   */
  useEffect(() => {
    clientRef.current?.setSpeechSuppressed(showMedia);
  }, [showMedia]);
  // 原生 `<details>` 不受 overlayPriority 管：任何正式浮层出现时必须强制收起。
  // 不这么做，1024 档「抽屉 + 设置」会同屏，设置面板（z-40）压在抽屉（z-20）之上，
  // 盖住卡片标题区（2026-11-11 弹窗互斥复审 F1）。关闭动作用 DOM 写回，因为
  // `<details open={false}>` 的 prop 不变化时 React 不会把 open 属性写回去。
  useEffect(() => {
    if (shownTask === null && !showIdentity && !showEnroll && !showWake) return;
    const el = settingsDetailsRef.current;
    if (el?.open) el.open = false;
    setSettingsOpen(false);
  }, [shownTask, showIdentity, showEnroll, showWake]);

  /**
   * 设置弹层的最低优先级：任何正式浮层出现（含用户刚点开「用药提醒」）就主动收起。
   * 不复用 `showSettings` 做这件事——那只是「不渲染」，state 还开着会让上层关掉后
   * 它突然弹回来。
   */
  useEffect(() => {
    if (settingsOpen && visibleOverlay !== "settings") setSettingsOpen(false);
  }, [settingsOpen, visibleOverlay]);

  /**
   * Escape 关掉当前可见浮层（2026-11-16 补）。
   *
   * 收敛前全端只有 `WakeWordSettings` 一处接了 Escape，其余浮层打开了就关不上——
   * 对用键盘或开关辅助设备的长者，这是「进去了出不来」。
   *
   * **刻意只管信息类与自助类浮层**，不碰照护动作：
   * - `sos` 结果页不可由长者撤销（FR-UI-08），Escape 绝不能关；
   * - `identity` 必须回答「是谁」（FR-VPR-03 身份未定不写数据），不能一键跳过；
   * - `medication` / `health` 需要长者做出决定，误触按键不该替他把提醒抹掉；
   * - `incoming` 要显式接听或拒接，不能静默消失。
   * 一次只关仲裁选出的那一个，与「同一时刻只有一个浮层进 DOM」一致。
   */
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      switch (visibleOverlay) {
        case "privacy":
          setPrivacyOpen(false);
          break;
        case "guide":
          finishGuide(false);
          break;
        case "settings":
          setSettingsOpen(false);
          break;
        case "wakeWord":
          setWakeSettingsOpen(false);
          break;
        case "media":
        case "memory":
          dispatch({ type: "leisure.close" });
          break;
        default:
          // sos / identity / medication / health / incoming / enroll：见上方注释，不关。
          return;
      }
      event.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [visibleOverlay]);

  return (
    <div className="flex h-full flex-col">
      {/* TTS 下行播放（WebRTC 收轨） */}
      <audio ref={audioRef} />

      {/* 顶栏：状态条（1920×1080 布局，左侧安全边距）+ 声纹入口 + 退出（自检用） */}
      <header
        ref={headerRef}
        className="flex shrink-0 items-center justify-between px-[var(--pad-gutter-x)] pt-[var(--pad-header-pt)] pb-2"
      >
        <StatusBar
          connection={
            status === "disconnected"
              ? "disconnected"
              : micState === "unavailable"
                ? "mic_unavailable"
                : "listening"
          }
          identity={
            identitySource === null
              ? "unknown"
              : identitySource === "generic"
                ? "generic"
                : "confirmed"
          }
          playback={status === "speaking" ? "speaking" : "idle"}
        />
        <div className="flex items-center gap-4">
          {/* 批二收敛（版面目的一）：顶栏只留「状态条 + SOS + 设置」三样。
              原来并列的「使用引导 / 唤醒词 / 用药提醒」全部收进右侧设置弹层——
              它们都是低频动作，常驻只会把顶栏挤成两行（≤1439px 时尤其明显）。 */}
          <SosButton onClick={handleSos} active={activeTask?.kind === "sos"} />
          {/* T/P 档（≤1439px）顶栏只有约 1024px 可用：三枚胶囊 670px + 五个按钮 ≈700px 会换行，
              顶栏长到 146px、3D 只剩 256px。所以把「唤醒词 / 录入声纹」收进这个「设置」弹层，
              首屏只留「用药提醒 + 一键求助 + 退出登录」。用原生 <details> 而不是 useState：
              折叠逻辑留在顶栏这一段内，不动 App.tsx 其余结构（D 档 .pad-compact-only 为
              display:none，只是多一个不可见节点，1920×1080 的顶栏像素零 diff）。 */}
          <details
            className="relative"
            open={showSettings}
            ref={settingsDetailsRef}
            onToggle={(event) => {
              /* 原生 `<details>` 的点击与键盘 Enter/Space 都不经 overlayPriority
                 仲裁（audit-reverse 复审 P1）：抽屉/身份确认开着时也能把面板打开，
                 面板被压在下面 → 长者以为「点设置没反应」，抽屉一关它又冒出来。
                 所以这里先问仲裁：只要有别的正式浮层在场，就地关回去。 */
              const next = event.currentTarget.open;
              if (next && (activeTask !== null || identityAsk !== null || enrolling || wakeSettingsOpen)) {
                event.currentTarget.open = false;
                setSettingsOpen(false);
                return;
              }
              setSettingsOpen(next);
            }}
          >
            <summary className="pad-header-btn flex cursor-pointer list-none items-center rounded-2xl border border-line bg-surface text-ink [&::-webkit-details-marker]:hidden">
              设置
            </summary>
            <div className="absolute right-0 top-[calc(100%+8px)] z-10 flex w-[300px] flex-col gap-2 rounded-2xl border border-line bg-surface p-3 shadow-xl">
              <button
                onClick={() => {
                  setSettingsOpen(false);
                  openMedicationNow();
                }}
                className="min-h-14 rounded-xl bg-surface-sunken px-4 text-2xl text-ink active:bg-surface"
              >
                用药提醒
              </button>
              <button
                onClick={() => {
                  setSettingsOpen(false);
                  setWakeSettingsOpen(true);
                }}
                className="min-h-14 rounded-xl bg-surface-sunken px-4 text-2xl text-ink active:bg-surface"
              >
                唤醒词
              </button>
              <button
                onClick={() => {
                  setSettingsOpen(false);
                  setGuideOpen(true);
                }}
                className="min-h-14 rounded-xl bg-surface-sunken px-4 text-2xl text-ink active:bg-surface"
              >
                使用引导
              </button>
              {showEnrollEntry && (
                <button
                  onClick={() => {
                    setSettingsOpen(false);
                    setEnrolling(true);
                    dispatch({ type: "interruption.begin", interruption: "enroll" });
                  }}
                  className="min-h-14 rounded-xl bg-surface-sunken px-4 text-2xl text-ink active:bg-surface"
                >
                  {enrollEntryLabel}
                </button>
              )}
              <button
                onClick={() => {
                  setSettingsOpen(false);
                  setPrivacyOpen(true);
                }}
                className="min-h-14 rounded-xl bg-surface-sunken px-4 text-2xl text-ink active:bg-surface"
              >
                隐私说明
              </button>
              {/* PV（≤600 竖屏）首屏放不下四个按钮，「退出登录」挪到这里。
                  它是自检入口不是长者动作，收起来不影响任何 FR。 */}
              <button
                onClick={() => {
                  setSettingsOpen(false);
                  onLogout();
                }}
                className="min-h-14 items-center rounded-xl bg-surface-sunken px-4 text-2xl text-ink-muted active:bg-surface"
              >
                退出登录
              </button>
            </div>
          </details>

        </div>
      </header>

      {/* 中部：3D 数字人区（three-vrm；加载失败/超 5s 切静态头像，不白屏）。
          min-h-0 + min() 尺寸：1920×1080 为基线，小视口下先收缩本区，保证大字幕不被截断。
          尺寸、圆角、降级图与 diag 全部收在 AvatarStage 内部，这里只留布局盒 */}
      {/* 中部两栏：左栏整块留给整身数字人，右栏是「今日用药 + 对话记录」信息面板。
          2026-11-11 三改：药单原来贴在左栏左下角，正压在数字人腿上；它属于「读的信息」，
          和字幕同栏更顺，左栏也就空出来给整身。堆叠档（手机/平板竖屏）由
          --pad-main-dir 改成上下排，组件不必知道自己在哪一档。 */}
      <main className={`relative flex min-h-0 flex-1 items-stretch overflow-hidden transition-[padding] duration-300 ${shownTask !== null || showMedia ? "pr-[var(--pad-drawer-reserve)]" : ""}`}>
        {/* 数字人不在中部 DOM 里：它是 App 根层的 `AvatarLayer`（登录页就已经在渲染），
            这里保留同尺寸的空列，保证中部与右栏的宽度分配不变。
            舞台底纹画在这一列而不是 fixed 层上：抽屉让位时它跟着一起收，
            不会糊到抽屉下面去。 */}
        <div className="pad-stage relative flex min-h-0 flex-1 items-center justify-center" />

        {/* 右栏：药单在上、对话记录在下（≥1024 与 3D 并排；窄屏堆叠成上下带） */}
        <section
          aria-live="polite"
          className="flex min-h-0 shrink-0 flex-col gap-4 py-4 pr-[var(--pad-gutter-x)]"
          style={{ width: "var(--pad-subtitle-w)" }}
        >
          {/* 唤醒词不可用时的显式入口（FR-UI-09 / UI-09、UI-11）：没有「小伴」可叫，
              就给他一颗能按的大按钮。落位在字幕栏顶部、不落舞台列——舞台列是 fixed
              3D 层的占位盒，往里塞内容会盖在数字人身上（pad-layout 用例守着这条）。
              尺寸比长者端可点下限（48px）再放大一档：96px 高、36px 字，坐得远、
              手在抖也能按中；按钮顺手放在最上方，视线不用往下找。 */}
          {wakeFallback !== null && (
            <div className="shrink-0 rounded-[1.75rem] border border-line bg-surface px-6 py-5 shadow-[0_18px_48px_-28px_rgba(45,48,45,0.35)]">
              <button
                type="button"
                onClick={handleCall}
                aria-label="呼叫数字人小伴"
                className="min-h-[96px] w-full rounded-3xl bg-brand text-4xl font-bold text-ink-inverse active:bg-brand-strong"
              >
                呼叫
              </button>
              <p className="mt-3 text-center text-2xl text-ink-muted">
                按这里叫小伴，听到提示音后直接说话
              </p>
            </div>
          )}
          {/* 批二版面（右栏只做对话）：药单整卡换成一行摘要，完整药单走右侧抽屉。
              数据仍只读 IndexedDB 缓存（断网时它反而是唯一还活着的数据源），
              摘要在 REMINDERS_UPDATED_EVENT 到达时刷新。 */}
          <MedicationSummary
            plan={medicationAgendaView.next}
            missedCount={medicationAgendaView.missedCount}
            onOpen={() => setMedicationDrawerOpen(true)}
          />
          <div className="pad-subtitle-card flex min-h-0 flex-1 flex-col rounded-[1.75rem] border border-line bg-surface px-8 py-6 shadow-[0_18px_48px_-28px_rgba(45,48,45,0.35)]">
            <BigSubtitle text={subtitle.text} source={subtitle.source} />
            {/* 空字幕时不留纯白：一句「我在」+ 四个可直接按的常问句（走 L0 固定意图）。 */}
            {!subtitle.text && (
              <StandbyPanel
                /* 常问句按钮也必须吞掉失败：`submitTextTurn` 超时会 throw，
                   这里不 catch 就是一条 unhandled rejection（控制台红字），而失败提示
                   已经由 submitTextTurn 自己写上字幕、由输入框自己出问题行。 */
                onAsk={(text) => {
                  void submitTextTurn(text).catch(() => {});
                }}
                onOpenMedia={(kind) => {
                  setMediaKind(kind);
                  setMediaGenre(""); // 点按钮 = 「我想听听」，不替长者押剧种
                  setMediaCommand(null); // 每次打开从「没有命令」开始，seq 由播放器自己从 1 数
                  dispatch({ type: "leisure.open", leisure: "media" });
                }}
                onOpenMemory={() => {
                  dispatch({ type: "leisure.open", leisure: "memory" });
                }}
                onOpenMedications={() => setMedicationDrawerOpen(true)}
              />
            )}
            {needRetry && (
              <button
                onClick={() => {
                  setNeedRetry(false);
                  clientRef.current?.retry();
                }}
                className="mt-4 shrink-0 rounded-2xl bg-brand px-12 py-4 text-3xl font-bold text-ink-inverse active:bg-brand-strong"
              >
                重新连接
              </button>
            )}
          </div>
        </section>

        {/* C5 identity_ask 确认层：大字幕 + 单个「是我」大按钮（1v1，候选集恒 1 位）。
            遮罩只压 45% 且不做 backdrop-blur：本机声纹服务未起时这张卡会一直在，
            模糊整屏会把后面的数字人糊没，看起来像「3D 没加载出来」。 */}
        {showIdentity && identityAsk && (
          <div className="absolute inset-0 z-10 flex items-center justify-center bg-canvas/45">
            <IdentityConfirm
              key={identityAsk.askId}
              displayName={identityAsk.displayName}
              askId={identityAsk.askId}
              elderId={identityAsk.elderId}
              timeoutS={identityAsk.timeoutS}
              onConfirm={(askId, elderId) =>
                clientRef.current?.sendIdentityConfirm(askId, elderId) ?? false
              }
              onRestConfirm={async (askId, elderId) => {
                // WS 发不出去时的兜底（按住说话降级链路本来就没有 WS）。
                // 落库成功也把本地身份置成 confirm：否则长者点了「是我」、
                // 服务端也认了，顶栏胶囊却还停在「待确认」。
                const outcome = await confirmIdentity(elderId, askId);
                if (outcome.ok) setIdentitySource("confirm");
                return {
                  ok: outcome.ok,
                  message: outcome.ok ? undefined : identityErrorText(outcome.errorCode),
                };
              }}
              onExit={() => {
                // 本地收起，不下发 confirm；服务端计时器到点仍会下发 generic 结果
                setIdentityAsk(null);
                dispatch({ type: "interruption.end" });
                setSubtitle({ text: "已跳过本次身份确认", source: "notice" });
              }}
            />
          </div>
        )}
        {/* 任务面板：右侧浮层，不整屏切页（PRD S3「语音+字幕+按钮同时在场」）。
            是否渲染由 `pickVisible` 决定：身份确认开着时这里**不进 DOM**（state 仍在，
            确认完自动回来），因此不再靠 z-20 压 z-10 的写法解决重叠。
            指针事件只放开面板本身，免得遮罩吃掉底栏「按住说话」。 */}
        {shownTask !== null && (
          <div className="pointer-events-none fixed inset-y-0 right-0 z-20 flex items-center pr-[var(--pad-drawer-gap)]">
            <div className="pointer-events-auto max-h-full overflow-y-auto">
              {shownTask.kind === "medication" && (
                <MedicationCard
                  data={shownTask.due.snapshot}
                  reminderId={shownTask.due.reminderId}
                  onConfirm={confirmMedicationTaken}
                  onUndo={undoMedicationTaken}
                  onExit={closeTask}
                  onInterrupt={handleInterruptSpeech}
                  /* FR-UI-10：卡片真画到长者的屏上了，才敢替服务端记一枪 `card_rendered`。
                     失败只告警不打扰：回执是「送达凭证」，不该让长者看到一条他没法处理的错误。 */
                  onRendered={() => {
                    void postMedicationRendered(shownTask.due.reminderId).catch((error) => {
                      console.warn("[pad] card_rendered 回执上报失败", error);
                    });
                  }}
                  onSubtitle={(text) => setSubtitle({ text, source: "notice" })}
                  /* CON-04：确认/拒服/稍后提醒收口后说一句；确认那一枪带音频，
                     没有音频时只上字幕（见 handleMedicationFeedback）。 */
                  onFeedback={handleMedicationFeedback}
                />
              )}
              {shownTask.kind === "health" && (
                <HealthCheckup
                  /* 设备令牌绑定的长者：健康采集按 elder_id 定位会话（服务端只认这个 ID，
                     不认客户端随便传的），未绑定时组件自己提示「请联系工作人员」。 */
                  elderId={loadSession()?.device?.elder_id ?? null}
                  onExit={closeHealth}
                  onSubtitle={(text) => setSubtitle({ text, source: "notice" })}
                  /* FR-UI-07「再说一遍」：声音归 RealtimeClient，本页只把「想再听一遍」
                     这件事交回去。真没有可重放的音频（上一段是远端实时流）就上大字说明，
                     不能让长者对着没声音的屏幕等。 */
                  onReplaySpeech={() => {
                    const replayed = clientRef.current?.replayLastSpeech() ?? false;
                    if (!replayed) {
                      setSubtitle({
                        text: "刚才那段话已经放完了，请看屏幕上的大字",
                        source: "notice",
                      });
                    }
                  }}
                />
              )}
              {shownTask.kind === "sos" && <SosResult onExit={closeTask} />}
            </div>
          </div>
        )}

        {/* 戏曲/电影面板（FR-CON-03）：与任务抽屉同位置同层级，但开关是 mediaOpen。
            同样只在 `showMedia` 为真时进 DOM —— 身份确认或求助结果在屏上时，
            这里不进 DOM，也就不会出现「戏在响但看不见面板」。 */}
        {showMedia && (
          <div className="pointer-events-none fixed inset-y-0 right-0 z-20 flex items-center pr-[var(--pad-drawer-gap)]">
            <div className="pointer-events-auto max-h-full overflow-y-auto">
              <MediaPanel
                kind={mediaKind}
                genre={mediaGenre}
                onClose={() => {
                  dispatch({ type: "leisure.close" });
                }}
                onSubtitle={(text) => setSubtitle({ text, source: "notice" })}
                command={mediaCommand}
                volume={mediaVolume}
                onVolumeChange={setMediaVolume}
              />
            </div>
          </div>
        )}

        {/* 长者本人记忆面板（FR-MEM-02）：查看/纠正/撤回自己那条记忆。
            服务端只认设备令牌绑定的长者（AGENTS §4.9），本组件不传任何身份字段。 */}
        {showMemory && (
          <div className="pointer-events-none fixed inset-y-0 right-0 z-20 flex items-center pr-[var(--pad-drawer-gap)]">
            <div className="pointer-events-auto max-h-full overflow-y-auto">
              <MemoryPanel
                onClose={() => {
                  dispatch({ type: "leisure.close" });
                }}
              />
            </div>
          </div>
        )}

        {/* 「今天的药」全量抽屉：右栏摘要只显示最近一条，想看全量点这里。
            受控组件（open/onClose），内部只读 IndexedDB 缓存、不发请求（FR-MED-08 断网可用）。
            takenIds 暂不传：缓存行没有终态字段，传空集只会少一态，不谎报「已吃」。 */}
        <MedicationDrawer
          open={medicationDrawerOpen}
          onClose={() => setMedicationDrawerOpen(false)}
        />

        {/* 隐私说明纳入仲裁（2026-11-16）：原为全屏 z-40 且不参与仲裁，
            会盖住按仲裁渲染的用药卡（z-20），长者看不见也点不到一条用药提醒。
            现在被任何照护动作/来电/设置压住时**根本不进 DOM**。 */}
        {showPrivacy && (
          <div className="absolute inset-0 z-40 flex items-center justify-center bg-canvas/85 p-10">
            <div className="max-w-3xl rounded-[1.75rem] border border-line bg-surface p-10 shadow-xl">
              <h2 className="text-4xl font-bold text-ink">隐私说明</h2>
              <p className="mt-6 text-2xl leading-relaxed text-ink-muted">
                只有叫出唤醒词之后，小伴才开始听、才把声音传上去；没叫它的时候，
                声音不会离开这台平板。
              </p>
              <p className="mt-4 text-2xl leading-relaxed text-ink-muted">
                声音只用来认人（声纹）和记事儿，不走医疗诊断，也不卖给任何人。
                您说过的话可以随时查看、改正或删掉；要彻底清掉，让护工在照护台操作。
              </p>
              <button
                onClick={() => setPrivacyOpen(false)}
                className="mt-8 min-h-16 w-full rounded-2xl bg-brand text-3xl font-bold text-ink-inverse active:bg-brand-strong"
              >
                我知道了
              </button>
            </div>
          </div>
        )}

        <WakeWordSettings
          open={showWake}
          deviceId={loadSession()?.device?.id ?? ""}
          current={wakeWord || DEFAULT_WAKE_WORD}
          onSave={handleWakeWordSaved}
          onClose={() => setWakeSettingsOpen(false)}
        />

        {/* FR-UI-06 首启引导：屏上「什么都没有」时才讲那三步。
            仍然守同一套仲裁思路——任何正式浮层、来电、外呼在场时不进 DOM。
            到点弹的用药卡比引导重要，两层遮罩同时压住长者只会让他不知道该按哪。 */}
        {/* 首启引导纳入仲裁（2026-11-16）：原来手工列四个条件，等价于「优先级最低」，
            现在由 pickVisible 统一裁决。外呼是**非模态顶部横幅**、不参与仲裁，
            所以仍需单独守一条：横幅在场时不弹引导，免得两层同时压住长者。 */}
        {showGuide && !outboundCalling && (
          <OnboardingGuide wakeWord={wakeWord || DEFAULT_WAKE_WORD} onDone={finishGuide} />
        )}

        {/* N62d 外呼通话中：自动接听之后的下行状态条（不是模态遮罩——长者还要听数字人说话）。
            只给「挂断」，因为这时候没有别的事可做；麦克风要等唤醒词命中才开。 */}
        {outboundCalling && (
          <div className="absolute inset-x-0 top-24 z-30 flex items-center justify-between gap-6 rounded-3xl bg-brand/95 px-10 py-6 shadow-lg">
            <div>
              <p className="text-4xl font-bold text-ink-inverse">数字人在跟您说话</p>
              <p className="mt-2 text-2xl text-ink-inverse/80">
                听见「{wakeWord || DEFAULT_WAKE_WORD}」再说话就能对话；不想听就按右边挂断
              </p>
            </div>
            <button
              onClick={() => {
                setOutboundCalling(false);
                dispatch({ type: "interruption.end" });
                clientRef.current?.hangUpOutboundCall();
              }}
              className="shrink-0 rounded-2xl bg-surface px-10 py-5 text-3xl font-bold text-ink active:bg-surface-sunken"
            >
              挂断
            </button>
          </div>
        )}


        {/* N62b 来电振铃层：大字 + 两个大按钮（1920×1080 基线）。
            z-30 高于身份确认与声纹录入，因为「有人打电话进来」压过其它流程；
            点接听才真正 getUserMedia 建媒体（未接听前麦克风一次都不开）。 */}
        {showIncoming && (
          <div className="absolute inset-0 z-30 flex flex-col items-center justify-center gap-10 bg-canvas/85">
            <p className="text-6xl font-bold text-ink">有来电</p>
            <p className="text-3xl text-ink-muted">是护工打来的，按「接听」开始通话</p>
            <div className="flex gap-8">
              <button
                onClick={() => {
                  setIncoming(null);
                  dispatch({ type: "interruption.end" });
                  void clientRef.current?.acceptIncomingCall();
                }}
                className="rounded-3xl bg-brand px-16 py-8 text-4xl font-bold text-ink-inverse active:bg-brand-strong"
              >
                接听
              </button>
              <button
                onClick={() => {
                  setIncoming(null);
                  dispatch({ type: "interruption.end" });
                  void clientRef.current?.declineIncomingCall();
                }}
                className="rounded-3xl border border-line bg-surface px-16 py-8 text-4xl text-ink-muted active:bg-surface-sunken"
              >
                不接
              </button>
            </div>
          </div>
        )}

        {/* W1 声纹录入引导：只在长者主动点顶栏入口时打开（不自动弹，别挡首屏问候）；
            是否渲染由 `pickVisible` 仲裁决定（`enroll` 在优先级表里排在 identity 之后），
            所以这里不再靠 z-index 与身份确认层抢先后。
            2026-11-17：原来挂在 `main` 的 `absolute inset-0` 里，只拿到中部 429px 高度
            （1536×703 实测），标题/进度/页脚把正文区压到 0，三段引导词一个都看不见。
            改用视口级 fixed：面板可用整屏高度，与 main 的高度无关。 */}
        {showEnroll && (
          <div className="fixed inset-0 z-30 flex items-center justify-center bg-canvas/70 p-4">
            <VoiceprintEnroll
              initialRegistered={voiceprint?.registered ?? []}
              onClose={() => {
                setEnrolling(false);
                dispatch({ type: "interruption.end" });
              }}
              // 三段齐且后端确认 active：立刻藏掉入口按钮（本层仍显示「声纹已启用」等长者点完成）
              onActivated={() => setVoiceprint({ status: "active", registered: [1, 2, 3] })}
              // 重录成功（后端三行已删）：状态必须跟着回到未注册，
              // 否则入口按钮仍走 active 分支，长者退出后面板写着「声纹设置」却没有可录的段。
              onReset={() => setVoiceprint({ status: "not_registered", registered: [] })}
            />
          </div>
        )}
      </main>

      {/* C5 generic 模式横幅：不读个人记忆 / 不写健康记录 / 不确认用药（FR-VPR-03） */}
      {identitySource === "generic" && (
        <div
          role="status"
          className="mx-12 mb-2 rounded-2xl border border-warning-ink/40 bg-warning-soft px-6 py-4 text-center text-2xl text-warning-ink"
        >
          通用模式：暂不读取个人记忆，不记录健康数据，不确认用药
        </div>
      )}

      {/* A6 / R-语音-01/02：麦克风不可用提示条。必须**常驻**——此前只在字幕区闪一句
          「麦克风重连失败」，随后被下一句问候覆盖，长者只会觉得「喊它没反应」。
          按钮与顶栏胶囊指向同一状态；点「重试」调 `retryMic()`（只重建媒体会话，不复位信令）。 */}
      {micState === "unavailable" && (
        <div
          role="status"
          className="mx-12 mb-2 flex flex-wrap items-center justify-between gap-x-6 gap-y-3 rounded-2xl border border-warning-ink/40 bg-warning-soft px-6 py-4 text-2xl text-warning-ink"
        >
          <span>麦克风没有声音了，可以先用下面的打字框，或按「重试」</span>
          <button
            type="button"
            onClick={() => {
              void clientRef.current?.retryMic();
            }}
            className="min-h-14 shrink-0 rounded-2xl bg-warning-ink px-8 text-2xl font-bold text-ink-inverse active:opacity-90"
          >
            重试
          </button>
        </div>
      )}



      {/* 底栏：只放两级降级入口（大字按钮 + 打字框），不承载回复字幕。
          平板/手机档（≤1439px）抽屉打开时**不再整块隐藏**（2026-11-11 复审 F2：抽屉一开
          两个降级入口同时消失，长者连打字都找不到），改为压缩排版——按钮与输入框同排、
          字号降一档、提示语隐藏，保证「抽屉开着也按得到」。 */}
      <footer
        ref={footerRef}
        className={`flex min-h-[var(--pad-footer-h)] shrink-0 items-end justify-center pb-3 ${
          shownTask !== null || showMedia ? "max-[1439px]:min-h-[120px]" : ""
        }`}
      >
        {/* T-G 两级降级入口**常驻**（AGENTS §6）：主链路正常时也留着，断网时更要能按。
            此前它只在 status === "disconnected" 分支里渲染，连通后老人就找不到说话的路了。
            两栏排布由 FallbackEntries 自己管；抽屉开着时整块让到左侧 890px 之内（抽屉左缘 902）——
            抽屉是 fixed 覆盖右半屏，用 z-index 硬压会把「已服用/这次不吃」盖住。 */}
        <div
          className={
            shownTask !== null || showMedia
              ? "w-full pr-[var(--pad-footer-reserve)] [&_p]:hidden max-[1439px]:[&_button]:min-h-[64px] max-[1439px]:[&_button]:text-2xl max-[1439px]:[&_textarea]:text-xl"
              : "w-full"
          }
        >
          <FallbackEntries
            talkHint={fallback.talk.hint}
            token={loadSession()?.access_token ?? ""}
            textHint={fallback.text.hint}
            textEnabled={fallback.text.enabled}
            onSubmit={submitTextTurn}
            onSubtitle={(text, source) => setSubtitle({ text, source })}
            onAuthFail={onLogout}
            className={shownTask !== null || showMedia ? "pad-entry-stack" : ""}
          />
        </div>
      </footer>
    </div>
  );
}

/**
 * Suspense 兜底（task-23）：与 AvatarStage 内部 loading 态同形——同尺寸、同圆角、同 brand-soft
 * 底色，只是没有 canvas / 静态头像可用；保证 lazy chunk 下载期间是「有画面」而不是白屏。
 * 几何走 `AVATAR_BOX_CLASS` 单一常量：此前兜底写 min(560px,48vh)、真实容器写 min(760px,72vh)，
 * 加载完成瞬间画面会从 560 跳到 760（2026-11-10 产品负责人实测）。
 */


export function App() {
  // 会话本体存 sessionStorage（session.ts）；组件只持有登录态
  const [loggedIn, setLoggedIn] = useState(() => sessionValid());

  function handleLoggedIn(_s: DeviceSession) {
    setLoggedIn(true);
  }

  function handleLogout() {
    clearSession();
    setLoggedIn(false);
  }

  // 根层先挂 3D：登录页停留的这十几秒正好把 10.7MB VRM 的解析与着色器编译付掉，
  // 登录进主界面时人已经在画面里（产品负责人要求「进入页面数字人就加载好」）。
  return (
    <>
      {/* 登录页不出现数字人：挂载保留（预加载 VRM），只切可见性，见 AvatarLayer.visible 注释 */}
      <AvatarLayer
        centered={loggedIn}
        visible={loggedIn}
        onDiag={(d) => forwardAvatarDiag(d.stage, d.ok, d.error)}
      />
      {loggedIn ? (
        <MainScreen onLogout={handleLogout} />
      ) : (
        <LoginPage onLoggedIn={handleLoggedIn} />
      )}
    </>
  );
}
