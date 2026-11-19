"""按句合成的 TTS 编排层（迭代 5 · F1a，设计记录（延迟护栏与口型） §4.2b）。

**为什么要这一层**：调用方现状是 ``await provider.synthesize(整段文本)``，而 C1 契约的
`TTSProvider.synthesize` 是整段合成、非流式——首包 = 整段合成完的时刻，回复越长越慢。
PRD 1.5s「开口」口径（docs/安语3D数字人_PRD.md §5.3）在整段合成口径下不可能达标。

**方案**：不改动已冻结的上游接口，在其**上方**加编排层。接口原文：`TTSProvider` 定义在
app/audio/asr.py:88-93（``async def synthesize(self, text: str) -> bytes``，契约冻结），
app/audio/tts.py:33 只 import 它，MockTTSProvider / EdgeTTSProvider / CloudTTSProvider
各自实现。本模块做两件事：`split_sentences()` 把整段文本按句切开；`synthesize_by_sentence()`
逐句 ``yield await provider.synthesize(sentence)``，调用方拿到一句就入队一句——
首包口径因此从「整段完成时刻」变为**第一句完成时刻**。

**职责边界（纯编排，零副作用）**：

- 不做 logging；轮级耗时打点按 迭代 5 §4.2b 收口在**回复管线**
  （app/audio/reply_stub.py / app/dialogue_runtime.py 消费侧：tts_first_packet_ms /
  tts_total_ms 按轮各一次；app/audio/tts.py::_record 已回归纯日志 record_tts）。
  唯一的例外是 **S7-3 追加的 `tts_queue_depth`**：队列深度是本层独有的事实
  （「还有几句没合成完」），只有生成器自己能数准，放到消费侧去数反而要复制状态；
  该 gauge 只增减、不读业务字段，不改变本模块「纯编排」的语义；- 不做打断与入队——逐句代际检查、逐句 ``enqueue_*``、轮级打点，全部由
  调用方（并行 F1b / dialogue_runtime 主路径）落地；
- 不改 TTSProvider 契约与任何实现类，app/audio/tts.py 不在本文件的修改范围内。

**切句口径（迭代 5 计划 §4.2b 钉死，勿自行调整）**：

- 终止符 = 全角「。！？；」+ 半角等价「.!?;」+ 换行；``\\r\\n`` 算**一个**换行；
- 过短片段（strip 后 < min_len，默认 6 字）：有下一句并入**下一句**，已是末尾并入
  **前一句**；合并用单个空格连接（英文单词防粘连，中文读音不受影响）；
- 纯空白 / 纯标点片段直接丢弃；函数最终不产出空串；
- 中文省略号「……」（U+2026 ×2）**不是**终止符，是字面量、随所在句整体保留；
- 无任何终止符的文本 → 整体作为一段返回。

参照实现：``本人既有工程 asr 的 tts_pipeline.py``（SentenceSplitter :41 /
SentenceTTSStream :108 / 推句 :280-294 / 取消 :157-182），编排思路同源；但常量
（min_len=6、终止符集合、合并方向、连接符）一律以本仓 迭代 5 计划为准，参照源只取思路。
"""

from __future__ import annotations

import asyncio
import sys
from array import array
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Final

from app.audio.asr import TTSProvider
from app.core.metrics import gauge

#: 本层的「待合成句数」深度（入队 +1、消费 -1）。
#:
#: 口径说明：这是 **逐句合成队列**的深度，不是下行播放队列（`MediaSession._playback_queue`
#: 在 app/audio/webrtc.py，不在本模块写作用域内）。两者都叫「TTS 排队」，但前者回答
#: 「还有几句话没合成出来」——首包慢/打断时最先涨的就是它。
TTS_QUEUE_DEPTH_GAUGE: Final = "tts_queue_depth"

# ---------------------------------------------------------------------------
# 切句口径（迭代 5 §4.2b 钉死的常量）
# ---------------------------------------------------------------------------
_NEWLINE: Final[str] = "\n"
# 全部终止符：全角「。！？；」与半角「.!?;」。半角分号也算句末（英文分号结束一个
# 独立子句）。**刻意不含**：逗号「，,」（碎在逗号上只会把短句压得更碎，§4.2b 的
# 例子「好的。」单独合成反而多一次往返就是反例）；省略号「…」（U+2026，中文
# 「……」是两个 U+2026，不是句界而是字面量）。
_SENTENCE_TERMINATORS: Final[frozenset[str]] = frozenset("。！？；.!?;")
# 合并过短片段时的连接符：英文必须用空格（否则 "OK"+"moment" → "OKmoment"），
# 中文无伤大雅（朗读上等价于一个微停顿）。
_MERGE_JOIN: Final[str] = " "


def _has_speakable(text: str) -> bool:
    """片段里是否还有「可读内容」（至少一个汉字或 ASCII 字母数字）。

    连续终止符（「！！！」「??」）切出的纯标点残片没有可读内容，丢弃比合成
    「只有标点」的一句音频干净。中文省略号与其它标点一样**不算**可读内容，
    但它只随所在句子整体保留或整体丢弃，不会被切开。
    """
    for char in text:
        if "\u4e00" <= char <= "\u9fff":
            return True
        if char.isascii() and char.isalnum():
            return True
    return False


def _split_raw(text: str) -> list[str]:
    """按终止符集合粗切，返回**未清洗**的片段（含空片段）。

    终止字符留在片段末尾——读法上它就是这句语气的一部分。换行只充当「无声终止符」，
    自身不进入片段；``\\r\\n`` 因此只断一次（``\\r`` 是普通空白字符，最终被 strip 掉，
    不构成第二个断点）。本实现**不用正则**：终止符是有限集合，逐字符扫描即可把
    「……不切」的语义写死（U+2026 不在终止符集合里，天然留在句内）。
    """
    fragments: list[str] = []
    buffer: list[str] = []
    for char in text:
        if char in _SENTENCE_TERMINATORS:
            buffer.append(char)
            fragments.append("".join(buffer))
            buffer.clear()
        elif char == _NEWLINE:
            fragments.append("".join(buffer))
            buffer.clear()
        else:
            buffer.append(char)
    fragments.append("".join(buffer))
    return fragments


def _merge_short(sentences: list[str], *, min_len: int) -> list[str]:
    """过短片段合并：有下一句并入下一句，末尾过短并回前一句；保证不产出空串。

    并入后若仍过短，会继续与更下一段合并（链式直到凑够或到尾）；全文只有一段
    且过短时无法并入邻居，按原样保留——丢弃它反而会吞掉唯一内容。入参的
    sentences 必须是已 strip 的片段（长度判定按 strip 后算，见 `split_sentences`）。
    """
    merged: list[str] = []
    pending: str | None = None
    for sentence in sentences:
        if pending is not None:
            sentence = f"{pending}{_MERGE_JOIN}{sentence}"
        if len(sentence) < min_len:
            pending = sentence
            continue
        pending = None
        merged.append(sentence)
    if pending is not None:
        if merged:
            merged[-1] = f"{merged[-1]}{_MERGE_JOIN}{pending}"
        else:
            merged.append(pending)
    return merged


def split_sentences(text: str, *, min_len: int = 6) -> list[str]:
    """把整段文本切成逐句可合成的片段列表（口径全文见模块 docstring「切句口径」）。

    规则摘要：终止符 = 全角「。！？；」+ 半角「.!?;」+ 换行（``\\r\\n`` 算一个换行）；
    strip 后长度 < min_len 的片段并入下一句，末尾过短并入前一句；纯空白 / 纯标点
    片段丢弃；不产出空串；中文省略号「……」不是终止符（字面量随句保留）；
    无任何终止符的长文本 → 整体作为一段。
    """
    if not text:
        return []
    sentences = [raw.strip() for raw in _split_raw(text)]
    sentences = [sentence for sentence in sentences if sentence and _has_speakable(sentence)]
    return _merge_short(sentences, min_len=min_len)


async def synthesize_by_sentence(
    text: str, *, provider: TTSProvider
) -> AsyncGenerator[bytes, None]:
    """按句合成：逐句 ``await provider.synthesize(sentence)`` 并逐句 yield PCM bytes。

    - 顺序逐句（不并行）：第 i+1 句在第 i 句 yield 之后才开始合成，调用方逐句
      入队即得「首包 = 第一句完成时刻」的口径；
    - `provider` 是任意实现冻结契约 ``synthesize(text: str) -> bytes`` 的实例
      （app/audio/asr.py:88-93；app/audio/tts.py 的 Mock / Edge / Cloud 均适用）；
    - 异常原样向上透传：某句合成抛错时生成器即抛出，不吞、不重试、不记日志，
      由调用方决定降级；
    - text 切不出任何句子（空串 / 全空白 / 全是标点）时什么也不 yield。

    **`tts_queue_depth`（S7-3）**：进入时按句数入队（+N），消费方取走一句后再 -1；
    `finally` 把没被消费的余量一次补减。为什么必须在 finally 补：gauge 是累计增减，
    打断（barge-in）关掉生成器时剩下的句子永远不会有消费方，不补减就会把深度
    永久抬高，之后的读数全是假高。
    """
    sentences = split_sentences(text)
    if not sentences:
        return
    pending = len(sentences)
    gauge(TTS_QUEUE_DEPTH_GAUGE, value=pending)
    try:
        for sentence in sentences:
            yield await provider.synthesize(sentence)
            # 走到这里 = 上一句已被消费方取走并继续要下一句，出队一句。
            pending -= 1
            gauge(TTS_QUEUE_DEPTH_GAUGE, value=-1)
    finally:
        if pending:
            gauge(TTS_QUEUE_DEPTH_GAUGE, value=-pending)


@dataclass(frozen=True, slots=True)
class TtsChunk:
    """流式合成的一块 PCM，带所属句子与「是否本句最后一块」。

    `pcm` 允许为空（该句一句音频都没合成出来）：消费方据此把句子序号推进，
    但不要把空块写进播放轨。
    """

    index: int
    sentence: str
    pcm: bytes
    final: bool


def _split_pcm(pcm: bytes, size: int) -> list[bytes]:
    usable = len(pcm) - len(pcm) % 2
    return [pcm[offset : offset + size] for offset in range(0, usable, size)]


async def synthesize_sentences_stream(
    text: str,
    *,
    provider: TTSProvider,
    prefetch: int = 2,
    chunk_size: int = 4096,
) -> AsyncGenerator[TtsChunk, None]:
    """流式逐句合成（P1/P2）：当前句边合成边出块，后面 `prefetch` 句整句预取。

    为什么预取整句而不是预取「流的首块」：当前句已经靠流式把首包压到第一块，
    预取的目标只是「下一句在上一句播完前就绪」。整句任务最简单、可取消、不会把
    多个 edge 流同时挂在事件循环里；`prefetch=2` 对应计划里的队列深度 2。

    取消：消费方 `aclose()` / 打断时，`finally` 取消所有未完成的预取任务并补减
    `tts_queue_depth`，不留悬挂任务。
    """
    sentences = split_sentences(text)
    if not sentences:
        return

    pending = len(sentences)
    gauge(TTS_QUEUE_DEPTH_GAUGE, value=pending)
    tasks: dict[int, asyncio.Task[bytes]] = {}

    def schedule(index: int) -> None:
        if index >= len(sentences) or index in tasks:
            return
        tasks[index] = asyncio.create_task(provider.synthesize(sentences[index]))

    for index in range(1, min(len(sentences), prefetch + 1)):
        schedule(index)

    try:
        for index, sentence in enumerate(sentences):
            if index in tasks:
                pcm = await tasks.pop(index)
                chunks = _split_pcm(pcm, chunk_size)
                if chunks:
                    for offset, chunk in enumerate(chunks):
                        yield TtsChunk(index, sentence, chunk, offset == len(chunks) - 1)
                else:
                    yield TtsChunk(index, sentence, b"", True)
            else:
                # 鸭子类型的实现（测试替身 / 第三方 provider）可能只有冻结契约
                # `synthesize()`；缺流式方法时按整句合成退回，语义与旧编排一致。
                stream = getattr(provider, "synthesize_stream", None)
                if stream is None:
                    chunks = _split_pcm(await provider.synthesize(sentence), chunk_size)
                    if chunks:
                        for offset, chunk in enumerate(chunks):
                            yield TtsChunk(index, sentence, chunk, offset == len(chunks) - 1)
                    else:
                        yield TtsChunk(index, sentence, b"", True)
                else:
                    iterator = stream(sentence)
                    buffered: bytes | None = None
                    try:
                        async for chunk in iterator:
                            if not chunk:
                                continue
                            if buffered is not None:
                                yield TtsChunk(index, sentence, buffered, False)
                            buffered = chunk
                        if buffered is None:
                            yield TtsChunk(index, sentence, b"", True)
                        else:
                            yield TtsChunk(index, sentence, buffered, True)
                    finally:
                        await iterator.aclose()
            pending -= 1
            gauge(TTS_QUEUE_DEPTH_GAUGE, value=-1)
            schedule(index + prefetch + 1)
    finally:
        for task in tasks.values():
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks.values(), return_exceptions=True)
        if pending:
            gauge(TTS_QUEUE_DEPTH_GAUGE, value=-pending)


# ---------------------------------------------------------------------------
# 句内节奏：能量包络 → 每字结束偏移
#
# 为什么需要它：字幕此前按「已播 / 句时长」的**匀速比例**揭示字符，而真实朗读是
# 「快—停—快」（逗号、数字、药名都会停顿）。匀速必然出现「字先说完、声音还在」。
# 这里从同一份 PCM 里量出**有声时间**，把字符按有声时间分配 —— 停顿不占字。
#
# 这是近似，不是对齐：不做音素、不做 forced alignment（技术设计文档 line 146
# 「不做高精度 3D/动捕」，PRD line 258 口型口径为「能量近似」）。纯函数、零副作用，
# 与本模块「纯编排」的边界一致。
# ---------------------------------------------------------------------------

#: 包络帧长，与下行帧长同源（app/audio/webrtc.py:101 DOWNLINK_FRAME_MS），不引第二个粒度。
_ENVELOPE_FRAME_MS: Final[int] = 20
#: TTS 下行 PCM 固定 16 kHz / mono / s16le → 32 字节 = 1 ms。
_PCM_BYTES_PER_MS: Final[float] = 32.0
#: 有声门限取「本句峰值的 8%」与绝对下限的较大者：相对值适配不同 provider 的音量，
#: 绝对下限防止整句极轻时把底噪当成有声。
_VOICED_REL_GATE: Final[float] = 0.08
_VOICED_ABS_GATE: Final[float] = 0.006


def _is_spoken_char(char: str) -> bool:
    """这个字符是否会被读出来（汉字 / ASCII 字母数字）。标点与空白不占朗读时间。"""
    if "\u4e00" <= char <= "\u9fff":
        return True
    return char.isascii() and char.isalnum()


def _frame_rms(pcm: bytes, samples_per_frame: int) -> list[float]:
    """逐帧归一化 RMS（0~1）。末尾不足一帧的残样丢弃（≤20ms，不影响分配）。"""
    usable = len(pcm) - len(pcm) % 2
    samples = array("h")
    samples.frombytes(pcm[:usable])
    if sys.byteorder != "little":  # PCM 契约是小端；大端机器上必须翻回来
        samples.byteswap()
    out: list[float] = []
    for start in range(0, len(samples) - samples_per_frame + 1, samples_per_frame):
        total = 0.0
        for i in range(start, start + samples_per_frame):
            value = samples[i] / 32768.0
            total += value * value
        out.append((total / samples_per_frame) ** 0.5)
    return out


def char_offsets_ms(
    pcm: bytes,
    text: str,
    *,
    duration_ms: int | None = None,
) -> list[int]:
    """每个字符的**结束偏移**（毫秒，相对句首），长度 = `text` 的码点数。

    口径：
    - 有声帧 = RMS 超过 `max(绝对下限, 本句峰值 × 8%)` 的 20ms 帧；
    - 可朗读字符（汉字 / 字母数字）按**累计有声时间**均分，标点与空白跟随前一个字符
      （它们不发音，不该单独占时间，否则「您好，」的逗号会卡住半拍）；
    - 整句静音（provider 返回纯静音 / 空 PCM）→ 退回按总时长匀速分配，不返回全 0；
    - 结果单调不减，末位恒等于句时长；`duration_ms` 给定时按它缩放（入队帧数换算的
      句时长与 16k PCM 时长可能差 ≤20ms 的残样，缩放后与字幕时间轴严格一致）。

    没有字符或没有 PCM 时返回空列表 / 全 0，调用方据此回退到匀速比例法。
    """
    chars = list(text)
    if not chars:
        return []
    pcm_ms = len(pcm) / _PCM_BYTES_PER_MS
    total_ms = float(duration_ms) if duration_ms and duration_ms > 0 else pcm_ms
    if total_ms <= 0:
        return [0] * len(chars)

    spoken_positions = [i for i, char in enumerate(chars) if _is_spoken_char(char)]
    if not spoken_positions:
        # 整句没有可朗读字符（纯标点）：跟着总时长匀速走，避免全 0 卡在空白。
        return [round(total_ms * (i + 1) / len(chars)) for i in range(len(chars))]

    rms = _frame_rms(pcm, int(16_000 * _ENVELOPE_FRAME_MS / 1000))
    voiced_ms: list[float] = []
    if rms:
        gate = max(_VOICED_ABS_GATE, max(rms) * _VOICED_REL_GATE)
        running = 0.0
        for value in rms:
            if value > gate:
                running += _ENVELOPE_FRAME_MS
            voiced_ms.append(running)

    offsets = [0] * len(chars)
    total_voiced = voiced_ms[-1] if voiced_ms else 0.0
    if total_voiced <= 0:
        # 没有有声帧：按总时长匀速分配（Mock 静音段 / 合成失败的兜底）。
        for order, position in enumerate(spoken_positions):
            offsets[position] = round(total_ms * (order + 1) / len(spoken_positions))
    else:
        scale = total_ms / (len(rms) * _ENVELOPE_FRAME_MS)
        cursor = 0
        for order, position in enumerate(spoken_positions):
            target = total_voiced * (order + 1) / len(spoken_positions)
            while cursor < len(voiced_ms) - 1 and voiced_ms[cursor] < target:
                cursor += 1
            offsets[position] = round((cursor + 1) * _ENVELOPE_FRAME_MS * scale)

    # 标点/空白跟随前一个可朗读字符；句首的标点没有前驱，保持 0。
    spoken = set(spoken_positions)
    previous = 0
    for i in range(len(chars)):
        if i in spoken:
            previous = offsets[i]
        else:
            offsets[i] = previous
    # 单调 + 收尾：末字必须落在句尾，否则最后一个字永远打不出来。
    running_max = 0
    for i in range(len(chars)):
        running_max = max(running_max, min(offsets[i], round(total_ms)))
        offsets[i] = running_max
    offsets[-1] = round(total_ms)
    return offsets
