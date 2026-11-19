/**
 * 日报详情（W4 §2）：当日健康 / 用药 / 数据来源 三块 + 近 14 天趋势 + 语音播报。
 *
 * 三条不越界的规则：
 * 1. **缺项写「—」**，不写 0、不写「正常」——没采到就是没采到。
 * 2. **用药块按服务端计数显示，前端不算命**：四类终态（已服自报/拒服/已送达未确认/
 *    通知失败）由 `payload.medication.counts` 原样输出，页面只做「机器码 → 中文」的
 *    翻译，不合并、不求和、不补 0；整块缺 `counts`（回填前的历史行）才退回服务端
 *    写的那句说明。自己编数据会直接把「不伪造」变成谎言。
 * 3. 异常原因只做「机器码 → 中文现象」的翻译，并统一给「建议咨询医生」，
 *    绝不出现下诊断结论的口径（只描述采集到的数值与现象）。
 *
 * 本页还负责单条日报的权威读取：列表数据只用于趋势图，
 * 详情以 `GET /api/reports/{elder_id}?date=` 为准，404 时提示「这份日报还没生成」。
 *
 * 语音条（FR-RPT-04）两条硬口径：
 * 1. `audio_url` 是**接口地址**（`/api/reports/{elder_id}/audio?date=…`），不是静态文件，
 *    裸 `<audio src>` 带不上 `Authorization` 只会 401 —— 因此先 `authedFetch` 取 blob
 *    再交给播放器；
 * 2. 服务端没给音频（`audio_url` 为空）时**整块播放器都不渲染**，不许摆一个点了没声的
 *    按钮假装有音频。闸门在 `../reportAudio` 的 `audioUrlOf`，由 `reportAudio.test.mjs`
 *    覆盖（计划件 §4 反向验收）。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { authedFetch } from "../api";
import { formatDateLabel } from "../format";
import { isNotFound } from "../http";
import { audioUrlOf } from "../reportAudio";
import {
  GRADE_LABEL,
  TREND_DAYS,
  displayNumber,
  getReport,
  gradeOf,
  healthBlockOf,
  healthSourceRows,
  medicationBlockOf,
  medicationCountRows,
  numberOrNull,
  payloadSourceNote,
  payloadSourceRows,
  reasonLabels,
  trendPoints,
  type DailyReportItem,
  type ReportHealthLatest,
} from "../reports";
import { ErrorBlock, InlineEmpty, ListSkeleton } from "../components/States";
import { SeverityDot } from "../components/SeverityDot";
import { SpeakerIcon } from "../components/Icons";
import { TrendChart } from "../components/TrendChart";

interface ReportDetailPageProps {
  elderId: string;
  elderName: string | null;
  date: string;
  /** 30 天日报列表（用于补日历轴画趋势；详情内容本身另取单条）。 */
  reports: DailyReportItem[];
  reportsLoading: boolean;
  reportsError: unknown;
  onRetryReports: () => void;
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="kv-row">
      <span className="kv-label">{label}</span>
      <span className="kv-value">{value}</span>
    </div>
  );
}

/** 「数值 + 单位」，缺值只留「—」——不写「— 次/分」这种半截话。 */
function withUnit(value: number | null | undefined, unit: string): string {
  const parsed = numberOrNull(value);
  return parsed === null ? "—" : `${displayNumber(parsed)} ${unit}`;
}

/** 血压两侧各自独立：缺哪边就写「—」，两边都缺只留一个「—」。 */
function bloodPressureText(latest: ReportHealthLatest | null): string {
  if (!latest) return "—";
  const systolic = numberOrNull(latest.systolic_mmhg);
  const diastolic = numberOrNull(latest.diastolic_mmhg);
  if (systolic === null && diastolic === null) return "—";
  const left = systolic === null ? "—" : displayNumber(systolic);
  const right = diastolic === null ? "—" : displayNumber(diastolic);
  return `${left}/${right} mmHg`;
}

export function ReportDetailPage({
  elderId,
  elderName,
  date,
  reports,
  reportsLoading,
  reportsError,
  onRetryReports,
}: ReportDetailPageProps) {
  const [item, setItem] = useState<DailyReportItem | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [attempt, setAttempt] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [audioSrc, setAudioSrc] = useState<string | null>(null);
  const [audioFailed, setAudioFailed] = useState(false);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    setPlaying(false);
    getReport(elderId, date)
      .then((data) => {
        if (!alive) return;
        setItem(data);
      })
      .catch((err: unknown) => {
        if (!alive) return;
        setItem(null);
        setError(err);
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [elderId, date, attempt]);

  const retry = useCallback(() => setAttempt((value) => value + 1), []);

  // 趋势图跟着「正在看的这一天」走：看 3 天前的日报，画的就是那之前的 14 天
  const points = trendPoints(reports, date, TREND_DAYS);
  // 闸门统一走 audioUrlOf：空串/空白/缺字段都算「没有音频」，见 ../reportAudio。
  const audioUrl = audioUrlOf(item);

  // 语音条取 blob：接口要 Bearer，<audio src> 裸开拿不到认证头。没有音频就完全不发请求。
  useEffect(() => {
    setAudioSrc(null);
    setAudioFailed(false);
    setPlaying(false);
    if (!audioUrl) return;
    let alive = true;
    let objectUrl: string | null = null;
    void (async () => {
      try {
        const res = await authedFetch(audioUrl);
        if (!res.ok) throw new Error(`audio request failed: ${res.status}`);
        const blob = await res.blob();
        objectUrl = URL.createObjectURL(blob);
        if (alive) setAudioSrc(objectUrl);
      } catch {
        // 取不到就退化成一句提示，不摆假按钮（音频文件被清理 / 网络抖动的共同出口）。
        if (alive) setAudioFailed(true);
      }
    })();
    return () => {
      alive = false;
      // 换日期或卸载时释放 blob URL，否则每看一天就漏一份音频内存。
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [audioUrl]);

  async function toggleAudio() {
    const audio = audioRef.current;
    if (!audio) return;
    if (audio.paused) {
      try {
        await audio.play();
      } catch {
        // 浏览器拒绝播放（无用户手势/媒体不可用）：留在暂停态，不抛到页面
        setPlaying(false);
      }
    } else {
      audio.pause();
    }
  }

  return (
    <div className="page">
      <header className="detail-head">
        {/* 页面 h1 由 App 顶栏承担，这里只做当日日期的大字标题 */}
        <p className="detail-date">{formatDateLabel(date)}</p>
        <div className="detail-meta">
          {elderName && <span className="detail-elder">{elderName}</span>}
          {item && <SeverityDot tone={gradeOf(item)} label={GRADE_LABEL[gradeOf(item)]} />}
        </div>
      </header>

      <section className="card">
        <h2 className="section-title">近 14 天血压趋势</h2>
        {reportsLoading ? (
          <p className="muted-note">正在加载趋势…</p>
        ) : reportsError ? (
          <ErrorBlock error={reportsError} onRetry={onRetryReports} />
        ) : (
          <TrendChart points={points} />
        )}
      </section>

      {loading ? (
        <ListSkeleton rows={3} />
      ) : error ? (
        isNotFound(error) ? (
          <InlineEmpty
            title="这份日报还没生成"
            hint="当天没有整理出日报，您可以回到列表看看其它日期。"
          />
        ) : (
          <ErrorBlock error={error} onRetry={retry} />
        )
      ) : item ? (
        <>
          <section className="card">
            <h2 className="section-title">当日健康</h2>
            {(() => {
              const health = healthBlockOf(item.payload);
              const latest = health?.latest ?? null;
              const reasons = latest ? reasonLabels(latest) : [];
              return (
                <>
                  {!latest && (
                    <p className="notice">当天没有采集到健康数据。</p>
                  )}
                  <div className="kv">
                    <Row label="血压" value={bloodPressureText(latest)} />
                    <Row label="心率" value={withUnit(latest?.heart_rate_bpm, "次/分")} />
                    <Row label="睡眠" value={withUnit(latest?.sleep_hours, "小时")} />
                    <Row
                      label="情绪"
                      value={latest?.mood_score != null ? `${displayNumber(latest.mood_score)} / 5 分` : "—"}
                    />
                  </div>

                  <h3 className="sub-title">异常原因</h3>
                  {reasons.length > 0 ? (
                    <>
                      <ul className="reason-list">
                        {reasons.map((reason, index) => (
                          <li key={`${reason}-${String(index)}`}>{reason}</li>
                        ))}
                      </ul>
                      <p className="muted-note">以上只是采集到的异常项，建议咨询医生。</p>
                    </>
                  ) : latest ? (
                    <p className="muted-note">本次没有记录到异常项。</p>
                  ) : (
                    <p className="muted-note">当天没有采集到健康数据。</p>
                  )}
                </>
              );
            })()}
          </section>

          <section className="card">
            <h2 className="section-title">用药</h2>
            {(() => {
              const block = medicationBlockOf(item.payload);
              const rows = medicationCountRows(block);
              if (rows.length > 0) {
                return (
                  <>
                    <div className="kv">
                      {rows.map((row) => (
                        <Row key={row.label} label={row.label} value={row.text} />
                      ))}
                    </div>
                    <p className="muted-note">
                      只统计当天到点的用药提醒。「已送达未确认」是提醒已推送、长者没有回应，
                      不代表没吃。
                    </p>
                  </>
                );
              }
              if (block?.state === "not_configured") {
                return <p className="muted-note">这位长者当天没有用药计划，也没有到点提醒。</p>;
              }
              return (
                <p className="detail-text">{block?.summary ?? "用药记录暂未生成。"}</p>
              );
            })()}
          </section>

          <section className="card">
            <h2 className="section-title">数据来源</h2>
            {(() => {
              const health = healthBlockOf(item.payload);
              const healthRows = healthSourceRows(health);
              const sourceRows = payloadSourceRows(item.payload);
              const note = payloadSourceNote(item.payload);
              if (healthRows.length === 0 && sourceRows.length === 0 && !note) {
                return <p className="muted-note">这一天没有记录来源信息。</p>;
              }
              return (
                <>
                  {healthRows.length > 0 && (
                    <div className="kv kv-source">
                      {healthRows.map((row) => (
                        <Row key={row.label} label={row.label} value={row.text} />
                      ))}
                    </div>
                  )}
                  {sourceRows.length > 0 && (
                    <div className="kv kv-source">
                      {sourceRows.map((row) => (
                        <Row key={row.label} label={row.label} value={row.text} />
                      ))}
                    </div>
                  )}
                  {note && <p className="muted-note">{note}</p>}
                </>
              );
            })()}
          </section>

          {audioUrl && (
            <div className="audio-wrap">
              {audioFailed ? (
                <p className="muted-note">语音播报暂时无法播放，您可以稍后再试。</p>
              ) : (
                <>
                  <button
                    type="button"
                    className="btn-secondary audio-btn"
                    disabled={!audioSrc}
                    onClick={() => void toggleAudio()}
                  >
                    <SpeakerIcon />
                    {audioSrc ? (playing ? "暂停播报" : "听语音播报") : "语音加载中…"}
                  </button>
                  <audio
                    ref={audioRef}
                    src={audioSrc ?? undefined}
                    preload="none"
                    className="sr-only"
                    onPlay={() => setPlaying(true)}
                    onPause={() => setPlaying(false)}
                    onEnded={() => setPlaying(false)}
                  />
                </>
              )}
            </div>
          )}
        </>
      ) : (
        <InlineEmpty title="这份日报还没生成" hint="您可以回到列表看看其它日期。" />
      )}
    </div>
  );
}
