"""`POST /api/dialogue/text` 的请求/响应模型（打字 = 说话的同一条对话链）。

口径（2026-11-11 · 长者端 footer 文字输入）：

- 请求体**只有** `text`。长者身份只从设备令牌背后的服务端会话取（AGENTS §4-9），
  请求体里即使塞 `elder_id` 也被忽略（`extra="ignore"` 显式写出，不靠默认值）。
- `text` 先 strip 再校验 1–200 字：纯空白与超长都在 422 拒绝，不做静默截断
  ——截断等于替长者决定哪半句算数，而丢掉的半句可能正是「停」或「我胸口疼」。
- `audio_url`：`/api/dialogue/text` 里恒为 `null`（该端点只回文本，保持打字响应亚秒级）；
  语音由紧随其后的 `POST /api/dialogue/tts` 单独合成（见下方 `DialogueTtsResponse`）。
  拆成两步是刻意的：TTS 合成本机实测约 0.6× 实时（10s 回复要等 6s），把合成塞进打字响应
  会让每句话的回显都卡上几秒；先上字幕、再补声音，长者的「它理我了」来得快。
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.core.context import get_trace_id

logger = logging.getLogger(__name__)

#: 一句话的长度上限。超长多半是误粘贴整段文字（或注入 payload），不是长者口述。
MAX_TEXT_CHARS: Final[int] = 200

#: 先 strip 再算长度：`"   "` 是空串而不是 3 个字符。
TextContent = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_TEXT_CHARS),
]

#: 只要出现在请求体里，就说明有客户端在试「自己报身份」。
#: 这些键**一律丢弃**（长者不该因为客户端多塞一个字段就吃到 422），但必须留痕 ——
#: 静默吞掉等于把一次越权尝试从日志里抹掉（ADM-06 第 3 条）。
IDENTITY_FIELD_NAMES: Final[frozenset[str]] = frozenset(
    {"elder_id", "thread_id", "device_id", "identity_source", "staff_id", "confidence"}
)


class DropsIdentityFields(BaseModel):
    """对话请求体公共基类：多塞的身份字段**丢弃并留痕**。

    口径（AGENTS §4-9：`elder_id` / `thread_id` 只能由服务端 `runtime_context` 注入）：

    1. **行为上是丢弃**（`extra="ignore"`）：客户端夹带字段不该让长者看到 422，
       一句「今天天气怎么样」不该因为前端多传一个字而问不出去；
    2. **记录上留一条带 `trace_id` 的 warning**：被丢弃的字段名进日志，
       事后可查「谁在试着自己报身份」。这两件事必须同时成立 —— 只做第 1 条就是静默吞。
    """

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _drop_identity_fields(cls, data: Any) -> Any:
        """校验前扫一遍原始 dict：命中身份字段就记 warning（值本身不进日志）。

        只记**字段名**不记值：值可能是伪造的长者 ID，也可能是别人家的 ID，
        写进日志反而把越权目标固定下来了。返回值原样交回 Pydantic，多余键仍被忽略。
        """
        if isinstance(data, dict):
            dropped = sorted(str(key) for key in data if str(key) in IDENTITY_FIELD_NAMES)
            if dropped:
                logger.warning(
                    "dialogue_identity_fields_dropped",
                    extra={
                        "trace_id": get_trace_id() or "-",
                        "model": cls.__name__,
                        "fields": dropped,
                    },
                )
        return data


class DialogueTextRequest(DropsIdentityFields):
    """`{"text": "..."}`。

    `extra="ignore"`（继承自 `DropsIdentityFields`）是安全属性而不是便利：客户端塞
    `elder_id` / `thread_id` 这类字段时，Pydantic 直接丢弃，服务端没有任何地方会读到它
    （越权字段无从生效）；**丢弃动作会打一条带 trace_id 的 warning**，见基类 docstring。
    """

    text: TextContent = Field(description="长者输入的一句话，strip 后 1–200 字")


class DialogueReplyBody(BaseModel):
    """回复对象，字段与音频入口的 reply 对齐。"""

    text: str = Field(description="给长者看/念的回复文本")
    audio_url: str | None = Field(
        default=None,
        description="本轮回复的语音地址；合成失败或文本超长时为 null（前端退回纯字幕）",
    )
    audio_duration_ms: int | None = Field(
        default=None,
        description="语音时长（ms）；前端用它对齐字幕逐字节奏与口型收口，null 表示没有音频",
    )


class DialogueIdentity(BaseModel):
    """本轮说话人的**旁路**结论（只供前端提示，不参与授权）。

    形状与 `app/api/audio.py:79-85` 的 `UtteranceIdentity` 逐字段对齐：前端拿到打字
    与按住说话两条链路的响应时，用的是同一套解析代码，不该出现两种字段名。

    口径（与话音链路的差别必须写清，否则会被误读成「声纹失效」）：

    - 打字**没有音频**，因此这里永远跑不了声纹比对。`source="device"` + `reason="no_audio"`
      的含义是「按设备绑定的长者继续」，**不是**「声纹没认出来」。
    - 设备未绑长者 → `source="generic"` + `reason="elder_unbound"`，此时 `elder_id=None`，
      与 `DialogueIdentity`（`app/core/context.py:124-133`）的 generic 语义一致。
    - 想拿声纹结论只有按住说话那条路（`POST /api/audio/utterance`），它手里有整段音频。
    """

    source: str = Field(description="voice = 声纹命中；device = 按设备绑定长者；generic = 未绑长者")
    matched: bool = Field(description="true 仅当 source=voice；打字链路恒为 false")
    reason: str | None = Field(
        default=None,
        description="为什么没认出：no_audio（打字无音频）/ elder_unbound / low_confidence / ...",
    )
    confidence: float | None = Field(default=None, description="声纹相似度；打字链路恒为 null")
    elder_id: str | None = Field(default=None, description="服务端会话里的长者；generic 时为 null")


class DialogueTextResponse(BaseModel):
    """成功响应：`{"text": <回显>, "reply": {...}, "degraded": bool, "identity": {...}}`。"""

    text: str = Field(description="回显本轮输入（strip 后），供前端做气泡对齐")
    reply: DialogueReplyBody
    degraded: bool = Field(description="true = 模型不可用，走的是降级话术；HTTP 仍是 200")
    safety_alert: bool = Field(
        default=False,
        description=(
            "true = 本轮安全命中**确实落库**（risk_events/红单/通知都写成功）；"
            "与 degraded 正交：degraded 说的是模型可用性，不是安全结果"
        ),
    )
    identity: DialogueIdentity | None = Field(
        default=None,
        description="本轮说话人旁路结论；null = 未判定（旧客户端不受影响，字段可缺）",
    )


#: 单条回复合成语音的文本上限。与 `app.services.tts_audio_service.MAX_SYNTH_CHARS`
#: 保持一致：schema 先挡住超长（422），服务层再兜一次（返回 None）——两层都不能少，
#: 因为服务层也被按住说话那条链路直接调用，那里不经过本 schema。
MAX_TTS_TEXT_CHARS: Final[int] = 400

#: 先 strip 再算长度：`"   "` 是 422，不做静默截断（截断会把一句完整的话念半句）。
TtsTextContent = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_TTS_TEXT_CHARS),
]


class DialogueTtsRequest(DropsIdentityFields):
    """`{"text": "..."}`：要出声的那句回复。

    `extra="ignore"` 与 `DialogueTextRequest` 同口径（同继承自 `DropsIdentityFields`）：
    客户端塞 `elder_id` / `thread_id` 一律丢弃并留痕 —— 本端点只做合成，不涉及身份，
    任何身份字段都不该从请求体进来（AGENTS §4-9）。
    """

    text: TtsTextContent = Field(description="要合成的回复文本，strip 后 1–400 字")


class DialogueAudioSegment(BaseModel):
    """一句字幕的时间窗，与语音链路的 `tts_segments` 帧同字段同口径。

    有了它，打字回复的逐字动画能按**真实语音节奏**推进（句间停顿就停字），
    而不是按平均字速把 100 个字平均铺到 20 秒里 —— 后者必然与口型越走越偏。
    """

    text: str = Field(description="这一句的文本（服务端切句结果）")
    start_ms: int = Field(ge=0, description="相对整段音频起点的毫秒")
    duration_ms: int = Field(ge=1, description="这一句的音频时长（毫秒）")
    char_ms: list[int] | None = Field(
        default=None,
        description="句内逐字坐标：每个字的结束偏移（ms，相对句首），长度 = text 码点数；null = 未提供",
    )


class DialogueTtsResponse(BaseModel):
    """成功响应：`{"audio_url", "audio_duration_ms", "audio_segments", "degraded"}`。

    降级也是 **200**：`audio_url=null` + `degraded=true`，前端退回纯字幕。
    合成失败不该让「长者已经看到的那句话」变成错误页，这是 2026-11-11 的明确口径。
    """

    audio_url: str | None = Field(
        default=None,
        description="浏览器可直接播放的静态地址（`/static/mock/tts_*.wav`）；降级时为 null",
    )
    audio_duration_ms: int | None = Field(
        default=None,
        description="音频时长（ms），前端用它对齐字幕与口型；降级时为 null",
    )
    audio_segments: list[DialogueAudioSegment] = Field(
        default_factory=list,
        description="逐句时间窗（服务端按 split_sentences 切句）；降级时为空数组",
    )
    degraded: bool = Field(
        description="true = 合成失败/空音频/写盘失败，前端退回纯字幕；HTTP 仍是 200"
    )


__all__ = [
    "IDENTITY_FIELD_NAMES",
    "MAX_TEXT_CHARS",
    "MAX_TTS_TEXT_CHARS",
    "DialogueAudioSegment",
    "DialogueIdentity",
    "DialogueReplyBody",
    "DialogueTextRequest",
    "DialogueTextResponse",
    "DialogueTtsRequest",
    "DialogueTtsResponse",
    "DropsIdentityFields",
    "TtsTextContent",
]
