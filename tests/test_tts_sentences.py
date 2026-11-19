"""按句合成编排层的回归测试：切句口径（迭代 5 §4.2b）与逐句编排的两条硬约束。

钉住三件事（设计记录（延迟护栏与口型） §4.2b）：

1. `split_sentences()`：全角「。！？；」、半角「.!?;」与换行都能终止；``\\r\\n`` 算一个
   换行；中文省略号「……」是字面量不切；纯空白 / 纯标点片段丢弃；过短片段（< 6 字）
   并入下一句、末尾过短并入前一句；无终止符长文本整体一段；不产出空串；
2. `synthesize_by_sentence()`：切出的每句文本对 provider **恰好调用一次**、调用顺序 =
   切句顺序、逐句 yield（次数 = 句数）；
3. provider 抛异常时异常**原样透传**给调用方（本模块不吞、不降级）——调用方语义。

写法沿用本仓 pytest-asyncio auto 模式（async def test_ 无 marker，见 tests/test_audio_tts.py）。
"""

from __future__ import annotations

import asyncio
import struct
from collections.abc import AsyncIterator

import pytest

from app.audio.asr import TTSProvider
from app.audio.tts_sentences import (
    char_offsets_ms,
    split_sentences,
    synthesize_by_sentence,
    synthesize_sentences_stream,
)

# ---------------------------------------------------------------------------
# 切句：终止符与切分口径（每个期望值都按「≥6 字不合并 / <6 字合并」手工核算过）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 中文四个终止符各自成句
        (
            "今天天气不错。明天会更好！后天要去公园吗？先把药吃了；然后再睡觉。",
            ["今天天气不错。", "明天会更好！", "后天要去公园吗？", "先把药吃了；", "然后再睡觉。"],
        ),
        # 换行终止
        ("第一段写完了。\n第二段接着写。", ["第一段写完了。", "第二段接着写。"]),
        # \r\n 只算一个换行：断点落在 \n 上，\r 不进任何片段（否则两行之间会多出残片）
        ("那就先吃早饭。\r\n再出门散步。", ["那就先吃早饭。", "再出门散步。"]),
        # 连续空行：空片段丢弃，句子数不增
        ("好的先开始播放。\n\n那么继续排队。", ["好的先开始播放。", "那么继续排队。"]),
        # 英文半角等价终止符 . ! ? ;
        (
            "Good morning! How are you? I am fine. Let me check; see you again",
            ["Good morning!", "How are you?", "I am fine.", "Let me check;", "see you again"],
        ),
        # 全角？与半角？等价终止
        (
            "今天真的暖和吗？昨天不就下雪吗? 秋天真舒服。",
            ["今天真的暖和吗？", "昨天不就下雪吗?", "秋天真舒服。"],
        ),
        # 混合中英标点；末尾过短句「明天见。」并回前一句
        (
            "今天晚上好！Hello rights。第二句中文！Take care;明天见。",
            ["今天晚上好！", "Hello rights。", "第二句中文！", "Take care; 明天见。"],
        ),
        # 无终止符长文本 → 整体一段
        ("今天天气真不错我们出去走走吧", ["今天天气真不错我们出去走走吧"]),
        # 过短片段（<6 字）并入下一句；合并用单个空格连接（英文防粘连）
        ("好的。今天天气真不错。", ["好的。 今天天气真不错。"]),
        # 末尾过短片段并入前一句
        ("今天天气真不错。好呀。", ["今天天气真不错。 好呀。"]),
        # 连续重复终止符：纯标点片段丢弃
        ("阿姨早上好！！！今天别忘了吃药。", ["阿姨早上好！", "今天别忘了吃药。"]),
        # 中文省略号不终止：随所在句整体保留（第二句以「……」开头）
        (
            "你到底真的走吗？…… 那就再坐一会儿。",
            ["你到底真的走吗？", "…… 那就再坐一会儿。"],
        ),
        # min_len 边界：恰好 6 字的片段保留，不合并
        ("再来一次吧。真的有进步。", ["再来一次吧。", "真的有进步。"]),
        # 全是标点 → 全部丢弃
        ("。！？？？？？.!?;", []),
        # 单独的省略号无句可归 → 丢弃（它是标点不是句）
        ("……", []),
        # 全空白 → []；空串 → []
        ("", []),
        ("   \n  \r\n", []),
    ],
)
def test_split_sentences_cases(text: str, expected: list[str]) -> None:
    got = split_sentences(text)

    assert got == expected, f"切句口径不符：{text!r} → {got!r}（期望 {expected!r}）"
    # 不变量：任何输入下都不产出空串
    assert all(sentence for sentence in got)


def test_split_sentences_respects_custom_min_len() -> None:
    # default(min_len=6) 下两句各自保留；min_len=8 时两句都过短 → 合并成一句
    assert split_sentences("一二三四五。请再想想看。", min_len=8) == ["一二三四五。 请再想想看。"]
    # min_len=2：没有任何片段算过短，两句照切
    assert split_sentences("一二三四五。请再想想看。", min_len=2) == [
        "一二三四五。",
        "请再想想看。",
    ]
    # 只有唯一片段且过短：无处可并入，按原样保留（不产出空串、不丢弃）
    assert split_sentences("好的。", min_len=6) == ["好的。"]


# ---------------------------------------------------------------------------
# synthesize_by_sentence：逐句编排（调用次数 / 顺序 / 异常透传）
# ---------------------------------------------------------------------------
_BY_SENTENCE_TEXT = "今天天气不错。小明在家吗？他出门散步了。"
_BY_SENTENCE_EXPECT = ["今天天气不错。", "小明在家吗？", "他出门散步了。"]


class _FakeTTSProvider(TTSProvider):
    """假 provider：逐调用记录文本、按调用序返回互不相同的 bytes（可选第 N 次抛异常）。"""

    def __init__(
        self,
        payloads: list[bytes] | None = None,
        *,
        fail_on_call: int | None = None,
        fail_exc: Exception | None = None,
    ) -> None:
        self.calls: list[str] = []
        self._payloads = payloads
        self._fail_on_call = fail_on_call
        self._fail_exc = fail_exc

    async def synthesize(self, text: str) -> bytes:
        self.calls.append(text)
        index = len(self.calls)
        if self._fail_on_call == index:
            if self._fail_exc is not None:
                raise self._fail_exc
            raise RuntimeError("synthesize failed")
        if self._payloads is not None and index <= len(self._payloads):
            return self._payloads[index - 1]
        return f"pcm-{index - 1}".encode("ascii")


async def test_synthesize_by_sentence_calls_provider_per_sentence() -> None:
    provider = _FakeTTSProvider()

    chunks = [pcm async for pcm in synthesize_by_sentence(_BY_SENTENCE_TEXT, provider=provider)]

    # yield 次数 = 句数，每句文本恰好调用一次，调用顺序与切句顺序一致
    assert chunks == [b"pcm-0", b"pcm-1", b"pcm-2"]
    assert provider.calls == _BY_SENTENCE_EXPECT


async def test_synthesize_by_sentence_no_sentences_yields_nothing() -> None:
    provider = _FakeTTSProvider()

    chunks = [pcm async for pcm in synthesize_by_sentence("", provider=provider)]
    assert chunks == []
    assert provider.calls == []

    chunks = [pcm async for pcm in synthesize_by_sentence("   \r\n  ", provider=provider)]
    assert chunks == []
    assert provider.calls == []


async def test_synthesize_by_sentence_propagates_failure_on_first_sentence() -> None:
    provider = _FakeTTSProvider(fail_on_call=1, fail_exc=ValueError("首句就失败"))
    chunks: list[bytes] = []

    with pytest.raises(ValueError, match="首句就失败"):
        async for pcm in synthesize_by_sentence(_BY_SENTENCE_TEXT, provider=provider):
            chunks.append(pcm)

    assert chunks == []
    assert provider.calls == [_BY_SENTENCE_EXPECT[0]]  # 先记录后抛出：出错的句也被如实记录


async def test_synthesize_by_sentence_propagates_exception_mid_stream() -> None:
    provider = _FakeTTSProvider(fail_on_call=2, fail_exc=RuntimeError("第 2 句合成失败"))
    chunks: list[bytes] = []

    with pytest.raises(RuntimeError, match="第 2 句合成失败"):
        async for pcm in synthesize_by_sentence(_BY_SENTENCE_TEXT, provider=provider):
            chunks.append(pcm)

    # 异常前的句子已经 yield 出去（调用方可先用），异常原样透传，不吞不降级
    assert chunks == [b"pcm-0"]
    assert provider.calls == _BY_SENTENCE_EXPECT[:2]


# ---------------------------------------------------------------------------
# synthesize_sentences_stream：当前句流式、后续句预取、取消不留悬挂任务
# ---------------------------------------------------------------------------


class _StreamProvider(TTSProvider):
    """当前句按块吐、预取句整句返回；记录每句被哪种入口取走。"""

    def __init__(self, *, block_second: asyncio.Event | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.cancelled = 0
        self._block_second = block_second

    async def synthesize(self, text: str) -> bytes:
        self.calls.append(("whole", text))
        if self._block_second is not None and text == _BY_SENTENCE_EXPECT[1]:
            try:
                await self._block_second.wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
        return f"whole:{text}".encode()

    async def synthesize_stream(self, text: str) -> AsyncIterator[bytes]:
        self.calls.append(("stream", text))
        yield f"a:{text}".encode()
        yield f"b:{text}".encode()


async def test_stream_uses_stream_first_and_prefetches_rest() -> None:
    provider = _StreamProvider()

    chunks = [
        chunk async for chunk in synthesize_sentences_stream(_BY_SENTENCE_TEXT, provider=provider)
    ]

    assert [chunk.index for chunk in chunks] == [0, 0, 1, 2]
    assert [chunk.final for chunk in chunks] == [False, True, True, True]
    assert chunks[0].sentence == _BY_SENTENCE_EXPECT[0]
    assert chunks[2].pcm == f"whole:{_BY_SENTENCE_EXPECT[1]}".encode()
    assert provider.calls[0] == ("stream", _BY_SENTENCE_EXPECT[0])
    assert {call[1] for call in provider.calls[1:]} == set(_BY_SENTENCE_EXPECT[1:])


async def test_stream_prefetches_next_sentence_while_current_is_playing() -> None:
    gate = asyncio.Event()
    provider = _StreamProvider(block_second=gate)
    iterator = synthesize_sentences_stream(_BY_SENTENCE_TEXT, provider=provider)

    first = await anext(iterator)
    # 让预取任务跑起来：消费者还在看第一块，第 2 句已经开合成。
    await asyncio.sleep(0.01)

    assert first.sentence == _BY_SENTENCE_EXPECT[0]
    assert any(call == ("whole", _BY_SENTENCE_EXPECT[1]) for call in provider.calls)

    gate.set()
    rest = [chunk async for chunk in iterator]
    assert [chunk.index for chunk in rest] == [0, 1, 2]


async def test_stream_close_cancels_prefetch_tasks() -> None:
    gate = asyncio.Event()
    provider = _StreamProvider(block_second=gate)
    iterator = synthesize_sentences_stream(_BY_SENTENCE_TEXT, provider=provider)

    await anext(iterator)
    await asyncio.sleep(0.01)
    await iterator.aclose()
    await asyncio.sleep(0.01)  # 给取消任务一个收尾机会

    # 被取消的预取任务不能变成「未取异常」；事件循环里也不该再有它。
    assert gate.is_set() is False
    assert provider.cancelled == 1


async def test_stream_queue_depth_returns_to_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.audio.tts_sentences as module

    deltas: list[int] = []
    monkeypatch.setattr(module, "gauge", lambda name, value: deltas.append(value))
    provider = _StreamProvider()

    _ = [chunk async for chunk in synthesize_sentences_stream(_BY_SENTENCE_TEXT, provider=provider)]

    assert sum(deltas) == 0


# ---------------------------------------------------------------------------
# 句内能量包络：每个字符的结束偏移（三线同步的「一份坐标」）
# ---------------------------------------------------------------------------


def _pcm_frames(*amplitudes: int) -> bytes:
    """每个入参 = 一个 20ms 帧的恒定幅度（0 = 静音）；16kHz / mono / s16le。"""
    return b"".join(struct.pack("<320h", *([amp] * 320)) for amp in amplitudes)


def test_char_offsets_empty_text_returns_empty() -> None:
    assert char_offsets_ms(_pcm_frames(8000), "") == []


def test_char_offsets_no_pcm_returns_zeros() -> None:
    """没有 PCM 也没有句时长：全 0，调用方据此回退到匀速比例法。"""
    assert char_offsets_ms(b"", "今天天气不错。") == [0] * 7


def test_char_offsets_monotonic_and_lands_on_sentence_end() -> None:
    text = "今天天气不错。"
    offsets = char_offsets_ms(_pcm_frames(*([8000] * 10)), text)

    assert len(offsets) == len(text)
    assert offsets == sorted(offsets), "偏移必须单调不减，否则打字机会往回退"
    assert offsets[-1] == 200, "末字必须落在句尾，否则最后一个字永远打不出来"


def test_char_offsets_pause_does_not_consume_chars() -> None:
    """句中的长停顿不该「播字」：停顿期间没有新字完成。

    匀速比例法会把 4 个字摊成 60/120/180/240ms —— 静音的那 160ms 里凭空多出两个字，
    这就是「字跑在声音前面」的句内形态。
    """
    pcm = _pcm_frames(8000, 8000, *([0] * 8), 8000, 8000)  # 40ms 有声 + 160ms 静音 + 40ms 有声
    offsets = char_offsets_ms(pcm, "一二三四")

    assert offsets[1] <= 40, "前两个字必须在停顿开始前就播完"
    assert offsets[2] >= 200, "第三个字必须等到停顿结束、声音重新起来"
    assert offsets[-1] == 240
    assert offsets != [60, 120, 180, 240]


def test_char_offsets_all_silence_falls_back_to_uniform() -> None:
    """Mock provider 返回纯静音：退回匀速，不能全 0 把整句卡在第一个字。"""
    offsets = char_offsets_ms(_pcm_frames(*([0] * 10)), "一二三四")
    assert offsets == [50, 100, 150, 200]


def test_char_offsets_punctuation_follows_previous_char() -> None:
    """标点不发音，跟随前一个可朗读字符 —— 否则「您好，」的逗号会卡住半拍。"""
    offsets = char_offsets_ms(_pcm_frames(*([8000] * 10)), "您好，世界")
    assert offsets[2] == offsets[1]


def test_char_offsets_duration_ms_rescales_to_caption_timeline() -> None:
    """入队帧数换算的句时长与 16k PCM 时长差 ≤20ms 残样：按 duration_ms 缩放对齐。"""
    pcm = _pcm_frames(*([8000] * 10))
    base = char_offsets_ms(pcm, "一二三四")
    scaled = char_offsets_ms(pcm, "一二三四", duration_ms=400)

    assert scaled[-1] == 400
    assert scaled == [value * 2 for value in base]


def test_char_offsets_pure_punctuation_spreads_over_duration() -> None:
    """整句没有可朗读字符：按总时长匀速走，不返回全 0。"""
    assert char_offsets_ms(_pcm_frames(*([8000] * 5)), "。。。") == [33, 67, 100]
