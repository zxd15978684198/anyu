# owner=A3 · 依据 C7 v1.0 · PRD FR-3D-01/02（前端渲染所需的前置数据）
"""数字人形象配置领域服务（C7 v1.0，消费方 = 长者端 pad）。

本文件只回答一件事：**这台平板该渲染哪个数字人**。答案固定是 C7 的四个键，
取值链按契约「persona → Settings → 前端内置」三层降级，且降级点在**后端**：

1. `elders.persona_id` → `personas.avatar_url`（人设自己配了形象）；
2. `settings.default_avatar_url`（人设没配形象的统一默认）；
3. `None` —— 接口仍然 200，前端直接加载内置兜底模型（FR-3D-02 的最后一层）。

为什么是纯读服务、不写库：换形象只 UPDATE `personas.avatar_url`（C7 语义约定，零代码改动），
那是照护台的人设维护接口；本接口只被平板高频调用，写库在这里没有落点。
放在服务层而不是 API 层，是为了让 `app/api/pad.py` 只做鉴权与序列化（同 `elder_service.py`）。

`version` 为什么用内容指纹而不是计数器：C7 要求「`persona.avatar_url` 变更即递增」，
但库里没有这一列，本卡也不许改模型与迁移。指纹把「响应里那几个值的组合」映射成整数 ——
换形象、换默认、换补偿角、改 salt 都会得到新值，效果等价于递增且没有写库副作用；
crc32 截到 31 位是为了恒非负（前端拿它当缓存键的一部分，负数只会无谓地多一种形态）。

依赖方向：`app/api/pad.py → 本文件 → models`（AGENTS §2）。这里刻意不碰
`voiceprint_service` / `identity_service` / `llm_service` / `dialogue_service`：
形象配置与这三件事无关，串进来只会让平板首屏多背一串不必要的导入。

## 追加（迭代 9 · W4b③）：人设 **prompt 文件**清单与读取

上面那一半回答「`personas` 表里给这位长者配了什么形象」，下面追加的一半回答
「`app/prompts/personas/` 目录里现有哪些人设 prompt 文件」。两件事刻意留在同一个模块：
一张表管运行期的形象与开场白，一个目录存进 Git 的提示词正文，按 AGENTS §2 的目录约定
它们本来就该由同一个领域服务出面。

`list_personas()` / `get_persona(name)` 只读：不写库、不改文件。`list_personas` 每个文件
**只读第一行**（版本头）就返回，不把正文加载进内存 —— 人设列表页不该因为目录里有 20 份
提示词就把 20 份全文读一遍。版本号取首行的约定见 `_parse_version`；没标注的一律 `"0"`，
不从文件名或正文里猜。
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
import zlib
from pathlib import Path
from typing import Any, Final, TypedDict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.context import PersonaContext, get_trace_id
from app.models import Elder, Persona

logger = logging.getLogger(__name__)


class AvatarConfig(TypedDict):
    """C7 的 200 响应体：**严格四个键**，增删任何一个都是契约变更（需走契约变更流程）。"""

    avatar_url: str | None
    fallback_image_url: str | None
    rotation_y_offset: float
    version: int


def _absolutize(value: str | None, *, base_url: str) -> str | None:
    """相对路径 → 绝对 URL（C7 第 6 条）；任何「空」都收敛成 `None`。

    三条取值规则，每条都对应一个真实会出现的输入：

    1. `None` / 空串 / 纯空白 → `None`：`default_avatar_url` 的默认值就是空串，
       空串表达「未配置」；若直接拼接会得到 `https://localhost/` 这种「看起来像资源」
       的地址，前端会拿它去发请求再 404，比直接给 `None` 更糟。
    2. 已以 `http` 开头（`http://` 与 `https://`）→ 原样返回：允许运维把形象放到 CDN，
       绝对地址不该被再拼一层 `public_base_url`。
    3. 其余按 `base_url.rstrip("/") + "/" + path.lstrip("/")` 拼接：两侧都归一，
       避免 `https://localhost//avatars/x.vrm` 这类双斜杠（nginx 不会替你纠正）。

    `strip()` 只用于判定是否为空与清洗路径两端，不改变路径中间的内容 —— 文件名里
    带空格是合法的，不能被顺手改掉。

    实现约定：`base_url` 走参数而不是在这里读 Settings。本函数是纯函数，单测可以直接断言
    拼接规则，不必去动环境变量（这也是把它单独抽出来、「方便后续单测」的落点）。
    """

    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if candidate.startswith("http"):
        return candidate
    return f"{base_url.rstrip('/')}/{candidate.lstrip('/')}"


def _fingerprint(
    *,
    avatar_url: str | None,
    fallback_image_url: str | None,
    rotation_y_offset: float,
    salt: str,
) -> int:
    """响应四值（除 `version` 外）的内容指纹，充当前端缓存失效键（C7 第 7 条）。

    两个刻意的口径：

    - 指纹打在**响应值**（已绝对化的 URL）上，而不是库里的原始相对路径。前端缓存的键
      就是这两个绝对 URL，改 `PUBLIC_BASE_URL`（换域名、加路径前缀）同样必须让旧缓存
      失效；若只哈希库里的相对路径，换域名后 `version` 不变，平板会继续用旧域名下的缓存。
    - `None` 交给 f-string 写成字面量 `"None"`，与任何真实文件名都不可能相同，
      不需要额外编码；`|` 是分隔符，URL 里出现 `|` 的概率可以忽略，且这里只要求
      「同一内容同值、不同内容大概率不同值」，不承担防碰撞职责。
    """

    material = f"{avatar_url}|{fallback_image_url}|{rotation_y_offset}|{salt}"
    return zlib.crc32(material.encode("utf-8")) & 0x7FFFFFFF


async def _persona_avatar_url(session: AsyncSession, *, elder_id: uuid.UUID) -> str | None:
    """第①层取值：`elders.persona_id → personas.avatar_url`；任一环缺失都返回 `None`。

    分两次查询而不是一条 JOIN，是为了区分两种「查不到」：

    - 长者行不存在：设备绑了一个 `elders` 里没有的 id，属数据缺口，要留一条带
      `trace_id` 的告警日志才查得出来；
    - 人设没配形象（`persona_id IS NULL` 或 `avatar_url` 为空）：正常配置状态，走 Settings 默认。

    一条 JOIN 会把两者压成同一个 `NULL`，日志里就再也分不开了。两张表在本系统的规模都是
    个位数行，多一次往返对平板首屏可以忽略。

    **长者行不存在时不抛 404**：C7 只定义了 200 与 401/403，且「形象查不到」不该让整页白屏
    （FR-3D-02 的降级要求）。缺行按「没配形象」处理，落到 Settings 那层再落到前端内置兜底；
    这也不泄露任何信息 —— 设备本来就知道自己绑的是哪个 id，响应里没有任何长者数据。
    """

    elder = await session.get(Elder, elder_id)
    if elder is None:
        logger.warning(
            "avatar_config_elder_missing",
            extra={"trace_id": get_trace_id() or "-", "elder_id": str(elder_id)},
        )
        return None
    if elder.persona_id is None:
        return None

    avatar_url = (
        await session.execute(
            select(Persona.avatar_url).where(Persona.persona_id == elder.persona_id)
        )
    ).scalar_one_or_none()
    # FK 保证人设存在；真查不到（行被绕过外键删掉）也按「没配形象」降级，不 500。
    if avatar_url is None or not avatar_url.strip():
        return None
    return avatar_url.strip()


async def _persona_greeting(session: AsyncSession, *, elder_id: uuid.UUID) -> str | None:
    """`elders.persona_id → personas.greeting`；任一环缺失都返回 `None`。

    与 `_persona_avatar_url` 分成两个函数而不是一次取回两个字段，理由相同：
    形象和开场白是**两条独立的降级链**（形象有 Settings 默认、开场白有硬编码兜底），
    合在一起取会让「形象配了但开场白为空」这种情况也跟着变成「形象没配」。
    """
    elder = await session.get(Elder, elder_id)
    if elder is None:
        logger.warning(
            "greeting_elder_missing",
            extra={"trace_id": get_trace_id() or "-", "elder_id": str(elder_id)},
        )
        return None
    if elder.persona_id is None:
        return None

    greeting = (
        await session.execute(
            select(Persona.greeting).where(Persona.persona_id == elder.persona_id)
        )
    ).scalar_one_or_none()
    if greeting is None or not greeting.strip():
        return None
    return greeting.strip()


async def _persona_voice_id(session: AsyncSession, *, elder_id: uuid.UUID) -> str | None:
    """`elders.persona_id → personas.voice_id`；人设未绑或音色为空都返回 `None`。"""
    elder = await session.get(Elder, elder_id)
    if elder is None or elder.persona_id is None:
        return None
    voice_id = (
        await session.execute(
            select(Persona.voice_id).where(Persona.persona_id == elder.persona_id)
        )
    ).scalar_one_or_none()
    if voice_id is None or not voice_id.strip():
        return None
    return voice_id.strip()


async def get_persona_voice_id(session: AsyncSession, *, elder_id: uuid.UUID) -> str | None:
    """人设音色读取入口：读不到一律 `None`，由 `build_tts` 回落到 `TTS_VOICE_ID`。"""
    try:
        return await _persona_voice_id(session, elder_id=elder_id)
    except Exception as exc:
        logger.warning(
            "persona_voice_lookup_failed",
            extra={
                "trace_id": get_trace_id() or "-",
                "elder_id": str(elder_id),
                "error_type": type(exc).__name__,
            },
        )
        return None


async def get_persona_greeting(session: AsyncSession, *, elder_id: uuid.UUID) -> str | None:
    """人设开场白读取入口（迭代 4 收口 W5 · G-D9）。

    契约 `契约记录` 登记过这个缺口：
    「`persona_service` 当前只有 `get_avatar_config`、没有 greeting 读口…
    实现暂用 `app/audio/media_bridge.py` 的两个模块常量」。本函数把那条链路补上。

    返回 `None` 的语义是「这轮不该由人设决定开场白」，此时调用方用通用话术兜底：
    长者行缺失、没绑人设、`personas.greeting` 为空。**不抛异常**——开场白读不到
    不该让「平板打开」这件事失败（FR-3D-02 的降级精神）。
    """

    try:
        return await _persona_greeting(session, elder_id=elder_id)
    except Exception as exc:  # 读库失败同样只降级，不阻断连接
        logger.warning(
            "greeting_lookup_failed",
            extra={
                "trace_id": get_trace_id() or "-",
                "elder_id": str(elder_id),
                "error_type": type(exc).__name__,
            },
        )
        return None


async def get_avatar_config(session: AsyncSession, *, elder_id: uuid.UUID) -> AvatarConfig:
    """按 C7 的三层取值链产出平板渲染数字人所需的四个值。

    `elder_id` 必须由调用方从 `Identity` 取（`app/api/pad.py` 用 `current_device`），
    **不接受请求参数或模型传入**：AGENTS §4.9 要求长者身份只从服务端上下文注入，
    否则平板一说「我是别人」，形象就跟着换了。
    """

    settings = get_settings()

    source = await _persona_avatar_url(session, elder_id=elder_id)
    if source is None:
        # 第②层：人设没配形象时用统一默认。可能是空串，交给 `_absolutize` 再收敛成 None。
        source = settings.default_avatar_url

    avatar_url = _absolutize(source, base_url=settings.public_base_url)
    fallback_image_url = _absolutize(
        settings.default_fallback_avatar_url, base_url=settings.public_base_url
    )
    rotation_y_offset = settings.avatar_rotation_y_offset

    return AvatarConfig(
        # 第③层：连 Settings 默认都没有 → None，接口仍 200，前端用内置兜底。
        avatar_url=avatar_url,
        fallback_image_url=fallback_image_url,
        # VRM0 需要 π 补偿、VRM1 恒 0.0（见计划 S-02）。这里原样透传，不猜模型版本：
        # 猜错会把数字人整体转向 180°，而配置项本来就是让人改的。
        rotation_y_offset=rotation_y_offset,
        version=_fingerprint(
            avatar_url=avatar_url,
            fallback_image_url=fallback_image_url,
            rotation_y_offset=rotation_y_offset,
            salt=settings.avatar_cache_version_salt,
        ),
    )


# ============================================================================
# 以下为 迭代 9 · W4b③ 追加：人设 prompt 文件清单与读取
# ============================================================================


class PersonaNotFound(LookupError):
    """人设不存在，或给出的名字不是一个可用的人设名。

    继承 `LookupError` 而不是 `ValueError`：调用方要把「没有这个人设」映射成 404，
    而「参数格式不对」那一类才是 422。两种原因走同一个异常，是因为对使用者而言
    它们同样意味着「查无此人设」——多分一个类型只会让 API 层多一个分支。
    """


#: 人设提示词目录：`app/prompts/personas/`（AGENTS §2 目录约定）。
PERSONAS_DIR: Final[Path] = Path(__file__).resolve().parents[1] / "prompts" / "personas"

#: 认作人设文件的扩展名，顺序即优先级（同名的 `.jinja` 胜过 `.md`）。
PERSONA_SUFFIXES: Final[tuple[str, ...]] = (".jinja", ".md", ".txt", ".prompt")

#: 目录里的说明文件不算人设。
_NON_PERSONA_STEMS: Final[frozenset[str]] = frozenset({"readme", "index"})

#: 人设名白名单：ASCII 字母数字开头，允许 `-` `_`，最长 64。
#: 关键是**不含路径分隔符、点与盘符**，所以 `../../../etc/passwd` 在正则这一关就被挡掉，
#: 不会走到「拼出路径再检查」的时序窗口。
_PERSONA_NAME_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")

#: 「v1.0」这种带 v 前缀的版本标记（正文里随手写的也算，只用于版本头）。
_VERSION_WITH_V_RE: Final[re.Pattern[str]] = re.compile(r"\bv(\d+(?:\.\d+)+)\b", re.IGNORECASE)
#: 「version: 1.0」「版本 1.0」这种显式声明（无 v 前缀）。
_VERSION_LABELED_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:version|版本)\s*[:：=]?\s*v?(\d+(?:\.\d+)+)", re.IGNORECASE
)

#: 未标注版本时的返回值。用 `"0"` 而不是 `"1.0"`：真实版本至少 `1.0`，
#: 一眼能看出「这份文件没人管版本」。
UNVERSIONED: Final[str] = "0"

#: 单个人设文件的大小上限（256 KiB）。提示词是给模型吃的文本，超过这个体量一定是
#: 放错了东西（数据集、日志、音频）；列表跳过、读取拒绝，避免一次请求吃满内存。
MAX_PERSONA_BYTES: Final[int] = 256 * 1024

#: 只读首行时最多看这么多字符（版本头是一行短声明，再长就是正文了）。
FIRST_LINE_MAX_CHARS: Final[int] = 200


def _repo_root() -> Path:
    """仓库根 = `app/prompts/personas/` 往上三层，用来把人设路径写成仓内相对路径。"""
    return PERSONAS_DIR.parents[2]


def _display_path(path: Path) -> str:
    """仓内文件返回 `app/prompts/personas/x.jinja`，仓外文件退化成绝对路径。

    返回值会进 API 响应，所以统一用 `/` 分隔：Windows 的 `\\` 进了 JSON 还得再转义一层，
    而这份路径只用于展示与排障，不是给 `open()` 用的。
    """
    try:
        return path.relative_to(_repo_root()).as_posix()
    except ValueError:
        return path.as_posix()


def _parse_version(header: str) -> str:
    """从人设文件首行解析版本号；解析不到返回 `UNVERSIONED`。

    认两种写法（取自仓库既有 prompt 的书写习惯）：
    - `# 小伴 v1.2` → `"1.2"`
    - `version: 1.2` / `版本：1.2` → `"1.2"`

    两种都命不中就是没标注，返回 `"0"`，**不从正文或文件名里猜**：猜错版本比没有版本更糟，
    它会让「这次上线用的是哪版人设」这条排查线索变成一条假线索。
    """
    labeled = _VERSION_LABELED_RE.search(header)
    if labeled is not None:
        return labeled.group(1)
    tagged = _VERSION_WITH_V_RE.search(header)
    if tagged is not None:
        return tagged.group(1)
    return UNVERSIONED


def _check_persona_name(name: str) -> str:
    """校验人设名，合法则原样返回；非法抛 `PersonaNotFound`。

    名字来自 URL / 前端下拉，属于不可信输入；整串匹配白名单，不信任调用方做过消毒。
    """
    if not isinstance(name, str) or _PERSONA_NAME_RE.fullmatch(name) is None:
        raise PersonaNotFound(f"非法人设名：{name!r}")
    if name.lower() in _NON_PERSONA_STEMS:
        raise PersonaNotFound(f"不是人设文件：{name!r}")
    return name


def _resolve_persona_path(name: str) -> Path:
    """按 `PERSONA_SUFFIXES` 顺序找人设文件；找不到抛 `PersonaNotFound`。

    同名多后缀按优先级取第一个（`.jinja` 优先），不做「两份都返回」：
    调用方要的是「这个人的提示词」，同时返回两份才是歧义。
    """
    for suffix in PERSONA_SUFFIXES:
        candidate = PERSONAS_DIR / f"{name}{suffix}"
        if candidate.is_file():
            return candidate
    raise PersonaNotFound(f"人设不存在：{name!r}")


def _read_first_line(path: Path) -> str:
    """读首行（同步，供 `asyncio.to_thread` 调用）。

    用二进制模式读一小段再解码：`TextIOWrapper.readline()` 遇到没有换行的巨型单行文件
    会把整个文件读进来，而这里只要一行。坏字节按 `replace` 宽松解码，版本正则自然匹配不到，
    不为此抛错 —— 列表不能被一个编码坏掉的文件带崩。
    """
    with path.open("rb") as fp:
        raw = fp.read(FIRST_LINE_MAX_CHARS * 4)
    if not raw:
        return ""
    text = raw.decode("utf-8", errors="replace")
    return text.splitlines()[0]


def _read_full_text(path: Path) -> str:
    """读全文（同步，供 `asyncio.to_thread` 调用）；超上限抛 `PersonaNotFound`。"""
    with path.open("rb") as fp:
        raw = fp.read(MAX_PERSONA_BYTES + 1)
    if len(raw) > MAX_PERSONA_BYTES:
        raise PersonaNotFound(f"人设文件过大（>{MAX_PERSONA_BYTES} 字节）：{_display_path(path)}")
    return raw.decode("utf-8", errors="replace")


def _list_personas_blocking() -> list[dict[str, Any]]:
    """同步扫描人设目录（供 `asyncio.to_thread` 调用）。"""
    if not PERSONAS_DIR.is_dir():
        logger.warning(
            "personas_dir_missing",
            extra={"trace_id": get_trace_id() or "-", "dir": _display_path(PERSONAS_DIR)},
        )
        return []

    # 外层按后缀优先级、内层按文件名排序：同名的 `.jinja` 先占位，后面的 `.md` 只记一条 warning。
    chosen: dict[str, Path] = {}
    for suffix in PERSONA_SUFFIXES:
        for path in sorted(PERSONAS_DIR.glob(f"*{suffix}")):
            if not path.is_file():
                continue
            stem = path.stem
            if stem.lower() in _NON_PERSONA_STEMS:
                continue
            if _PERSONA_NAME_RE.fullmatch(stem) is None:
                logger.warning(
                    "persona_skipped_illegal_name",
                    extra={"trace_id": get_trace_id() or "-", "path": _display_path(path)},
                )
                continue
            if stem in chosen:
                logger.warning(
                    "persona_same_stem_ignored",
                    extra={
                        "trace_id": get_trace_id() or "-",
                        "kept": _display_path(chosen[stem]),
                        "ignored": _display_path(path),
                    },
                )
                continue
            chosen[stem] = path

    records: list[dict[str, Any]] = []
    for name in sorted(chosen):
        path = chosen[name]
        try:
            size = path.stat().st_size
        except OSError:
            logger.warning(
                "persona_skipped_unreadable",
                extra={"trace_id": get_trace_id() or "-", "path": _display_path(path)},
            )
            continue
        if size == 0 or size > MAX_PERSONA_BYTES:
            logger.warning(
                "persona_skipped_size",
                extra={
                    "trace_id": get_trace_id() or "-",
                    "path": _display_path(path),
                    "size": size,
                },
            )
            continue
        try:
            header = _read_first_line(path)
        except OSError:
            logger.warning(
                "persona_skipped_unreadable",
                extra={"trace_id": get_trace_id() or "-", "path": _display_path(path)},
            )
            continue
        records.append(
            {
                "name": name,
                "version": _parse_version(header),
                "path": _display_path(path),
            }
        )
    return records


async def list_personas() -> list[dict[str, Any]]:
    """列出 `app/prompts/personas/` 下的人设文件清单（name / version / path）。

    - **只读**：不改文件、不写库（列表页的审计交给 API 层，本函数没有 actor 可用）。
    - 每个文件**只读首行**解析版本，正文不进内存；要正文用 `get_persona`。
    - 目录不存在返回 `[]` 并打 warning：仓库里还没人设时列表该是空的，不是 500。
    - 跳过（各打一条 warning）：`README.md`/`index.*` 等说明文件、名字不合白名单的文件、
      空文件、超过 `MAX_PERSONA_BYTES` 的文件、读不了的文件。跳过而非抛错：
      一个坏文件不该让整个列表 500。
    - 文件 IO 走 `asyncio.to_thread`（ruff `ASYNC230`：async 函数里不做阻塞 IO）。
    """
    return await asyncio.to_thread(_list_personas_blocking)


async def get_persona(name: str) -> str:
    """读取一个人设 prompt 的全文。

    非法 `name`、文件不存在、文件过大 → `PersonaNotFound`（API 层映射 404）。
    `name` 是 URL 参数，属于不可信输入：白名单在拼路径**之前**整串匹配，
    见 `_check_persona_name`；这里不接受 `..`、分隔符或带后缀的写法
    （`get_persona("x.jinja")` 会被当成非法名拒绝，后缀由本模块按优先级补）。
    """
    path = _resolve_persona_path(_check_persona_name(name))
    return await asyncio.to_thread(_read_full_text, path)


# -----------------------------------------------------------
# 人设读口（迭代 10 · T4 / ADM-07）：对话链路的 `PersonaContextLoader`
# -----------------------------------------------------------


def _coerce_tone_examples(raw: Any) -> tuple[str, ...]:
    """把 `personas.tone_examples`（JSONB）收敛成例句序列。

    实库里这一列**基本是 NULL**：`scripts/seed_personas.py:121` 故意不写 few-shot
    （由 prompt 侧维护），所以「读不到」是常态，不是异常。

    三条纪律：

    1. **保持原序**，不排序不去重 —— 顺序是 system prompt 字节的一部分，重排会让
       prefix cache 整段失效（`app/core/context.py:144-145` 明确写了这条）。
    2. **dict 绝不当例句**：模型上这一列的类型标注是 `dict | None`
       （`app/models/organization.py:49-51`），真按标注迭代会拿到 **key 名**去当例句念。
       这里只认「dict 里的第一个序列值」；取不到就按脏数据返回 `()`，绝不猜。
    3. **单个字符串不拆字**：`"您慢点儿说，我听着呢。"` 是**一**条例句，不是十几条。
    """
    if raw is None:
        return ()
    candidates: Any = raw
    if isinstance(raw, dict):
        candidates = next(
            (value for value in raw.values() if isinstance(value, list | tuple)),
            None,
        )
    if isinstance(candidates, str):
        text = candidates.strip()
        return (text,) if text else ()
    if not isinstance(candidates, list | tuple):
        return ()
    return tuple(item.strip() for item in candidates if isinstance(item, str) and item.strip())


async def load_persona_context(elder_id: str) -> PersonaContext | None:
    """对话链路的 `PersonaContextLoader`：`elder_id` → 人设上下文。

    签名严格等于 `app/dialogue_runtime.py:78` 的
    `PersonaContextLoader = Callable[[str], Awaitable[PersonaContext | None]]`，
    由 `DialogueRuntime._load_persona` 每轮按**当轮识别出的长者**调一次，
    结果拼进 system prompt 的固定前缀（`app/core/context.py:216-227`）。

    契约：

    - **永不抛**：`elder_id` 不是 UUID、长者没绑人设、库连不上 —— 全部收敛成 `None`，
      调用方此时退回组装层默认人设。人设读不到只该少一段前缀，不该把整轮对话打成错误。
    - **独立只读会话**：调用点（`DialogueRuntime`）手里没有请求级 session，它在音频/WS
      回调里跑；这里自己开一个、用完就关，**不 commit**。
    - **不写库、不改文件**：与本模块另一半天（`get_persona` 读 prompt 文件）同口径。
    """
    try:
        elder_uuid = uuid.UUID(str(elder_id))
    except (AttributeError, TypeError, ValueError):
        logger.warning(
            "persona_context_bad_elder_id",
            extra={"trace_id": get_trace_id() or "-"},
        )
        return None

    try:
        # 局部 import：`app.db` 在导入时就会造 engine。只有真要用的时候才把它拉进来，
        # 免得「只想读 prompt 文件」的调用方也被迫背一个数据库连接池。
        from app.db import session_factory

        async with session_factory() as session:
            row = (
                await session.execute(
                    select(
                        Persona.name,
                        Persona.personality,
                        Persona.tone_examples,
                        Persona.voice_id,
                    )
                    .join(Elder, Elder.persona_id == Persona.persona_id)
                    .where(Elder.elder_id == elder_uuid)
                )
            ).first()
    except Exception as exc:
        logger.warning(
            "persona_context_load_failed",
            extra={"trace_id": get_trace_id() or "-", "error_type": type(exc).__name__},
        )
        return None

    if row is None:
        # 长者没绑人设（`elders.persona_id IS NULL`）→ 用组装层默认名，不算错误。
        logger.info(
            "persona_context_unset",
            extra={"trace_id": get_trace_id() or "-", "elder_id": str(elder_uuid)},
        )
        return None

    name, personality, tone_examples, voice_id = row
    return PersonaContext(
        name=(name or "").strip(),
        personality=(personality or "").strip(),
        tone_examples=_coerce_tone_examples(tone_examples),
        voice_id=(voice_id or "").strip() or None,
    )


__all__ = [
    "MAX_PERSONA_BYTES",
    "PERSONAS_DIR",
    "PERSONA_SUFFIXES",
    "UNVERSIONED",
    "AvatarConfig",
    "PersonaNotFound",
    "get_avatar_config",
    "get_persona",
    "get_persona_greeting",
    "get_persona_voice_id",
    "list_personas",
    "load_persona_context",
]
