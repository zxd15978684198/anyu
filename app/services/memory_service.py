"""长期记忆五步：写入 / 召回 / 衰减 / 容量淘汰 / 删除（迭代 7 · 计划 §W1）。

## 五个函数，一条主线

| 函数 | 一句话 | PRD |
| --- | --- | --- |
| `remember` | 写入一条记忆（**够像且语义矛盾**才 superseded），provider 不可用也不阻断 | FR-MEM-01/03 |
| `recall` | 同 `elder_id` + `status='active'` + `category ∈ (private, family)` + **相关性下限**的余弦 top-k | FR-MEM-05 |
| `decay` | 90 天未访问的 active 条目降权一半 | FR-MEM-07 |
| `enforce_capacity` | 超 `CAPACITY_PER_ELDER` 时按「最不重要 + 最久没被召回」软删 | FR-MEM-07 |
| `delete_by_elder` | 退房/撤回：软删全部 active + **同事务**写 `audit_logs` | FR-MEM-02 / AC-14 |

## 长者本人端（FR-MEM-02）三个入口：`load_chunk` / `correct_chunk` / `delete_chunk`

五步主线之外，长者本人要能「查看 / 纠正 / 删除**自己的一条**」——PRD FR-MEM-02 的原话，
不是管理员代办。三者都落在本模块，理由是不让第二套写库路径出现（AGENTS §4 第 8 条）：

- `load_chunk` 把 `(elder_id, chunk_id)` 一起放进 `WHERE`，跨长者与不存在在 SQL 层就是
  同一个空结果（404 不区分，避免可探测面）；
- `correct_chunk` 只改正文并重算向量，**不碰 `importance`**（权重不是正文）；
- `delete_chunk` 是软删，且**每次调用都写审计**（重复删除也是事实，见 G4 口径）。

三者都由 `app/api/memory.py` 的 `/api/memory/me/*` 调用，`elder_id` 只从设备令牌的
`identity.elder_id` 取（AGENTS §4.9），服务层签名里没有「从参数里指定长者」的口子。

## 闸门②（记忆隔离）落在这里，而且是**每一条向量查询**

技术设计文档 §3.4 对 `memory_chunks.elder_id` 的口径是一句全大写的硬话：
「**所有查询强制 `WHERE elder_id=:runtime.elder_id`**」（FR-ADM-06 / AC-39）。
迭代 9 闸门② 会**静态扫描**向量查询有没有 `elder_id`，所以本文件的每一处
`cosine_distance(...)` 都在同一条 select 的 `where` 里带 `MemoryChunk.elder_id == elder_id`，
连 `last_accessed` 的 update 也再带一次 —— 少一处就是越权召回 B 长者记忆的入口。

`elder_id` 只能由调用方（服务端 `runtime_context`）传入，本模块**不接受**来自模型/客户端的
自报身份（AGENTS §4.9）：签名里就没有「从 content 里推断是谁」这种口子。

## 来源系数：推断出来的事**不许**和长者亲口说的事同权（讲义 §8.3）

`remember(..., source=...)` 按来源给 `importance` 打折：**明确陈述**（长者自己说出口、家属
明确告知）系数 1.0，**行为推断**（系统从步数、作息、点播记录里推出来的偏好/感受）系数 0.6。
两者等权时，模型会把「他今天只走了两百步」当成「他说他腿疼」来复述——推断被洗成陈述，
而一旦写进库，就再也分不出原始出处了。来源**不落库**（不改表结构，见下），写时就折算进
`importance`；已有的 90 天衰减与容量淘汰照旧按 `importance` 走，不另设第二套权重。

## 与计划/TD 的两处已知偏离（已在任务报告登记）

1. **没有 `superseded_by` 列**：计划 §W1 写「`superseded_by` 指新行」，但技术设计文档
   §3.4 的 `memory_chunks` 列定义里只有 `status`，没有 `superseded_by`。本实现**不加列**
   （迁移与模型归 Lead），只用 `status='superseded'` 表达「这条已失效」；新条与旧条的
   先后关系由 `created_at` + 同 `category` 的相似度可复算。
2. **计划写 `visibility IN ('private','family')`，TD 写 `category`**：以技术设计文档的
   列名 `category` 为准（PRD FR-MEM-08：「本版仅 private」，family 是预留取值）。召回照样
   同时接受 `private` 与 `family`，与计划的行为口径一致。

## 为什么 `remember` 不在 provider 不可用时抛异常

计划 §P1 的红线是「记忆召回失败 → 本轮不加载记忆，**不得臆造**」，而不是「写不进记忆」。
写入时拿不到向量只意味着这条记忆暂时不能语义召回（`embedding IS NULL`，`recall` 会跳过它），
但文本本身仍有价值（关键词兜底、长者可查可改）。所以记 `memory_write_degraded_total` 后
**继续写**，`embedding=None`。

## 两个相似度阈值：一个判「是不是同一件事」，一个判「扯不扯得上」

FR-MEM-03 的取代与 FR-MEM-05 的召回都要相似度，但**不能共用一个数**，否则总有一头是错的：

| 常量 | 值 | 回答的问题 | 判错的代价 | 方向 |
| --- | --- | --- | --- | --- |
| `SUPERSEDE_SIMILARITY` | 0.9 | 两条记忆是不是同一件事的两种说法 | **删数据**（旧条被标失效、无入口恢复） | 宁严勿松 |
| `RECALL_MIN_SIMILARITY` | 0.35 | 这条记忆跟这句话扯不扯得上 | **漏召回**（该想起来的事没想起来） | 宁松勿严 |

复用 0.9 当召回下限，「我几点睡觉」问不到「我晚上九点睡觉」；复用 0.35 当取代阈值，
只沾边的两条记忆就会互相取代。取值理由各自写在常量注释里。

## 取代 = 够像 **且** 语义矛盾（不是一个阈值的事）

旧实现（`_supersede_conflicts` 取最近一条、`similarity > 0.9` 即取代）有两个错：

1. **相似不等于矛盾**：「我喜欢越剧」与「我喜欢京剧」余弦也能过 0.9，旧实现会把前者抹掉。
   FR-MEM-03 要求的是「旧值标 superseded」在**矛盾**时发生，两份并存的偏好不该互相覆盖
   （「高相似但不矛盾的两条必须并存」）。
2. **只看最近一条**：真正被推翻的旧条常排第二（长者先说过一条近义句，又说反话）。
   现在窗口取 `SUPERSEDE_SCAN_LIMIT`（5）条，逐条判。

矛盾判定落在 `memories_contradict`：**确定性纯函数**，不调模型。理由是调用点在 `remember`
的事务里 —— 写库层不能在事务里发外部请求（外部一慢就把事务拖成锁等待），而且
「这两句话是不是一正一负」本来就不需要模型：否定词 + 谓词表 + 少数反义对足够覆盖
「喜欢/不喜欢」「吃/不吃」「能/不能」这类改口。判不出来的一律**不取代**（并存），
因为漏判只是多留一条，误判是抹掉长者亲口说过的话。
"""

from __future__ import annotations

import datetime
import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

import numpy as np
from sqlalchemy import (
    ColumnElement,
    CursorResult,
    Numeric,
    Result,
    cast,
    func,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.metrics import counter
from app.models import AuditLog, Elder, MemoryChunk
from app.services.embedding import EmbeddingProvider, EmbeddingUnavailable

logger = logging.getLogger("app.services.memory_service")

# ------------------------------------------------------------------ 常量（阈值只在代码里）

#: 单条记忆正文上限（计划 §W1「只收值得记的摘要句（≤200 字）」）。超长**拒收**，
#: 不静默截断：截断后的半句话被当成完整记忆召回，比写不进去更难查。
MAX_CONTENT_LENGTH: Final[int] = 200

#: 取代的第一道闸：新记忆与同长者同类 active 记忆余弦 **> 0.9** 才算「可能说的是同一件事」
#: （FR-MEM-03）。注意它只是**必要条件**：够像不等于矛盾（「我喜欢越剧」与「我喜欢京剧」
#: 余弦能过 0.9，但该并存），是否取代还要再过 `memories_contradict`。
SUPERSEDE_SIMILARITY: Final[float] = 0.9

#: 取代的候选窗口：只看最像的 `SUPERSEDE_SCAN_LIMIT` 条同类 active 记忆。
#: 旧实现取 `.limit(1)`：只要「最像的那一条」恰好不矛盾，就一条都不取代 —— 而真正被推翻的
#: 旧条常常排第二（长者先说过一条近义句，又说反话）。窗口开到 5 与召回 top-k 同量级，
#: 覆盖一两周内的改口；再大就要在事务里逐条比对，写库路径不值得。
SUPERSEDE_SCAN_LIMIT: Final[int] = 5

#: 召回相关性下限（FR-MEM-05「召回后二次过滤」）：余弦相似度低于此值的命中不进上下文。
#: **与 `SUPERSEDE_SIMILARITY` 是两个阈值、两个常量，禁止复用**，因为回答的是两个问题：
#: - 0.9 回答「这两条记忆是不是同一条事实的两种说法」——判错了会**删数据**，宁严勿松；
#: - 0.35 回答「这条记忆跟这句话扯不扯得上」——判错了会**漏召回**，宁松勿严。
#: 若复用 0.9：「我几点睡觉」问不到「我晚上九点睡觉」（换了说法的同一事实被挡掉）；
#: 若复用 0.35：只沾边的两条记忆就会被判成冲突而互相取代（长者亲口说的话被悄悄抹掉）。
#: 取 0.35 的理由：中文句向量（bge-m3 一类）上 0.35 大致对应「同一话题」，能把「今天天气
#: 不错」这类寒暄挡在记忆注入之外；0.5 以上开始漏掉换措辞的同一事实，0.2 以下等于没设闸门。
RECALL_MIN_SIMILARITY: Final[float] = 0.35

#: 衰减：90 天未被召回 → `importance *= DECAY_FACTOR`（FR-MEM-07；D7-Q2 待裁决的默认值）。
DECAY_DAYS: Final[int] = 90
DECAY_FACTOR: Final[float] = 0.5

#: 每位长者的 active 记忆容量上限（FR-MEM-07「容量上限按 elder_id 计」；D7-Q2 默认值）。
CAPACITY_PER_ELDER: Final[int] = 500

#: 可写入/可召回的类别。TD §3.4：本版仅 private，family 是预留取值；
#: 两者都可召回（计划 §W1 的 `visibility IN ('private','family')` 口径）。
RECALL_CATEGORIES: Final[tuple[str, str]] = ("private", "family")

DEFAULT_CATEGORY: Final[str] = "private"

#: `memory_chunks.status` 的三个合法取值（与 `app/models/memory.py` 的 CHECK 同源）。
MEMORY_STATUSES: Final[tuple[str, ...]] = ("active", "superseded", "deleted")

#: 后台列表的分页口径：默认 50 条、单页上限 200。上限存在的意义是防止
#: `?limit=100000` 把某位长者的全部记忆一次拖进响应体（既是性能问题，也是隐私暴露面）。
DEFAULT_LIST_LIMIT: Final[int] = 50
MAX_LIST_LIMIT: Final[int] = 200

#: `importance` 的合法区间：TD §3.4 是 `numeric(3,2)`，而 `app/models/memory.py` 的
#: `CheckConstraint("importance BETWEEN 0 AND 1")` 把语义收得更窄（权重 0..1 而不是 0..9.99）。
#: 以模型为准：写了 1.5 会在 flush 时炸成 IntegrityError，不如在这里给出可读的 ValueError。
_IMPORTANCE_MIN: Final[Decimal] = Decimal("0.00")
_IMPORTANCE_MAX: Final[Decimal] = Decimal("1.00")

#: 来源系数（讲义 §8.3）：`explicit` = 长者/家属**明确陈述**，`inferred` = 系统**从行为推断**。
#: 取值写死在代码里、只做乘法：来源不落库（不新增列），折算后的 `importance` 仍落在
#: `numeric(3,2)` 的 [0,1] 区间内，衰减与容量淘汰不需要第二套逻辑。
MEMORY_SOURCE_COEFFICIENTS: Final[dict[str, float]] = {"explicit": 1.0, "inferred": 0.6}

#: 未声明来源时的默认值。默认取 `explicit`（系数 1.0）而不是 0.6：既有调用方按默认
#: `importance=1.0` 写入的落库结果一条都不变，新增的惩罚只作用在**显式标了推断**的写入上。
DEFAULT_MEMORY_SOURCE: Final[str] = "explicit"

#: 长者本人端（FR-MEM-02）两条审计动作名。写进 `audit_logs.action` 后按名字检索，
#: 改名等于让历史审计查不到，故与契约 §6（`docs/接口契约_记忆.md`）同源，改一处必须改两处。
ACTION_CHUNK_CORRECT: Final[str] = "memory.chunk.correct"
ACTION_CHUNK_DELETE: Final[str] = "memory.chunk.delete"


# ------------------------------------------------------------------ 小工具


def _now() -> datetime.datetime:
    """统一 UTC（AGENTS §8「存储一律 UTC」）。"""
    return datetime.datetime.now(tz=datetime.UTC)


def _affected(result: Result[Any]) -> int:
    """`DELETE` / `UPDATE` 的受影响行数。

    `Session.execute` 的静态返回类型统一是 `Result`，`rowcount` 只长在运行期真正返回的
    `CursorResult` 上（SQLAlchemy 2.x 没有按语句类型收窄的重载），故在此收窄一次。
    DML 必返回 `CursorResult`；`TypeError` 分支只在有人把 `SELECT` 结果喂进来时才走。
    """
    if not isinstance(result, CursorResult):
        raise TypeError(f"expected CursorResult from DML, got {type(result).__name__}")
    return int(result.rowcount or 0)


def _last_seen() -> ColumnElement[datetime.datetime]:
    """「最后一次露面」= `last_accessed` 回落到 `created_at`。

    只被召回过的条目才有 `last_accessed`；从没被召回的老条目若按 `NULL` 处理，衰减与
    容量淘汰都会把它们当成「刚写的」永远放过——那是把冷数据留成热数据。
    """
    return func.coalesce(MemoryChunk.last_accessed, MemoryChunk.created_at)


def _normalise_content(content: str) -> str:
    """去首尾空白并校验长度；空串或超长一律 `ValueError`（不静默截断）。"""
    text = (content or "").strip()
    if not text:
        raise ValueError("memory content must not be empty")
    if len(text) > MAX_CONTENT_LENGTH:
        raise ValueError(f"memory content exceeds {MAX_CONTENT_LENGTH} characters")
    return text


def _normalise_importance(importance: float) -> Decimal:
    """`importance` 落 `numeric(3,2)`：先转 Decimal 再判区间，避免 float 精度漏判。"""
    try:
        value = Decimal(str(importance))
    except Exception as exc:  # 非数值（含 NaN 字符串化后）统一按非法处理
        raise ValueError("importance must be a number") from exc
    if value != value or value < _IMPORTANCE_MIN or value > _IMPORTANCE_MAX:  # NaN 比较恒 False
        raise ValueError(f"importance must be within [{_IMPORTANCE_MIN}, {_IMPORTANCE_MAX}]")
    return value.quantize(Decimal("0.01"))


def _apply_source_weight(importance: float, source: str) -> Decimal:
    """把来源系数折进 `importance`，结果夹回 [0,1] 再交给 ORM。

    顺序是「先判 base、再乘系数、最后夹边界」，三条各有理由：

    - **base 越界仍旧 `ValueError`**：静默夹掉会让调用方以为写进去了（原有口径没变）；
    - **乘系数用 `Decimal`**：`0.99 * 0.6` 走 float 会得到 `0.594` 这类尾巴，落到
      `numeric(3,2)` 前必须先定死舍入，否则「同一段内容两个来源」的断言会随平台漂移；
    - **乘积夹边界**：给将来留口子（系数若被调成 >1，或 base 恰好等于边界），不让
      `CheckConstraint` 在 `flush()` 时才炸成 IntegrityError。
    """
    coefficient = MEMORY_SOURCE_COEFFICIENTS.get(source)
    if coefficient is None:
        raise ValueError(f"source must be one of {tuple(MEMORY_SOURCE_COEFFICIENTS)}")
    base = _normalise_importance(importance)
    weighted = base * Decimal(str(coefficient))
    clamped = min(_IMPORTANCE_MAX, max(_IMPORTANCE_MIN, weighted))
    return clamped.quantize(Decimal("0.01"))


async def _embed_text(
    provider: EmbeddingProvider,
    text: str,
    *,
    metric: str,
) -> list[float] | None:
    """取一段文本的向量；不可用/失败返回 `None` 并打点，**绝不把异常抛给调用方**。

    `metric` 区分写入降级与召回降级（`memory_write_degraded_total` /
    `memory_recall_degraded_total`），label 固定 `reason`，取 `EmbeddingUnavailable.reason`
    的封闭枚举 —— 运维能按 reason 直接出告警规则。
    """
    if not provider.available:
        counter(metric, reason="not_configured")
        return None

    try:
        vectors = await provider.embed([text])
    except EmbeddingUnavailable as exc:
        counter(metric, reason=exc.reason)
        logger.warning(
            "embedding_unavailable",
            extra={"reason": exc.reason, "metric_name": metric, "detail": exc.detail},
        )
        return None

    if len(vectors) != 1:
        # 批量接口回的行数对不上：宁可当次不用向量，也不要拿别人的向量写进这条记忆。
        counter(metric, reason="bad_payload")
        return None

    vector = [float(value) for value in vectors[0]]
    if len(vector) != provider.dimension:
        counter(metric, reason="bad_payload")
        return None
    if not all(np.isfinite(vector)):  # 元素级检查走 numpy：1024 维纯 Python 太慢
        counter(metric, reason="bad_payload")
        return None
    return vector


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """两向量的余弦相似度（-1..1）；维度不等或含非有限值时 `ValueError`。

    用途是**纯计算**：测试断言、离线复算「这两条记忆为什么会冲突」。数据库侧的排序
    用 pgvector 的 `<=>`（`cosine_distance`），两者口径一致（`1 - distance`）。

    零向量返回 0.0 而不是抛错：空记忆 / 全零占位向量不该让整轮回滚，
    「没有方向」本身就不是相似。
    """
    left = np.asarray(a, dtype=np.float64).ravel()
    right = np.asarray(b, dtype=np.float64).ravel()
    if left.size == 0 or right.size == 0:
        raise ValueError("cosine_similarity needs non-empty vectors")
    if left.size != right.size:
        raise ValueError("cosine_similarity needs vectors of the same dimension")
    if not (np.isfinite(left).all() and np.isfinite(right).all()):
        raise ValueError("cosine_similarity rejects non-finite values")

    norm = float(np.linalg.norm(left) * np.linalg.norm(right))
    if norm == 0.0:
        return 0.0
    # 浮点误差可能给出 1.0000000000000002，夹回 [-1, 1] 让「>0.9」这类判断稳定。
    return max(-1.0, min(1.0, float(np.dot(left, right) / norm)))


# ------------------------------------------------------------------ 矛盾判定（确定性纯函数）

#: 谓词表：长者陈述里「会随改口翻转」的高频动词/形容词。只收正负极明确的一小撮；
#: 表外措辞（「搬到杭州了」「孙子叫小宇」）不判矛盾 —— 宁可两条并存，也不要误删长者的话。
_CONFLICT_PREDICATES: Final[tuple[str, ...]] = (
    "喜欢",
    "爱",
    "讨厌",
    "愿意",
    "想",
    "要",
    "吃",
    "喝",
    "抽",
    "去",
    "来",
    "能",
    "会",
    "是",
    "有",
    "习惯",
    "同意",
    "打算",
    "需要",
    "看",
    "听",
    "唱",
    "玩",
    "住",
)

#: 否定前缀：出现在谓词**前面 1~2 个字以内**，就认为该谓词被否定
#: （「不喜欢」「不太喜欢」「没去过」）。只看谓词前，不看整句：句首的「不」常常修饰别的
#: 成分（「不知道她喜不喜欢」），按整句判会把肯定句误认成否定句。
_NEGATIONS: Final[tuple[str, ...]] = ("不", "没", "别", "勿", "无", "未", "甭")

#: 反义对：两侧都是肯定式，靠词义对立判矛盾（「喜欢」对「讨厌」）。否定式已由
#: `_NEGATIONS` 覆盖，故这里只列「都带肯定词、意思却相反」的少数几组。
_ANTONYMS: Final[tuple[tuple[str, str], ...]] = (
    ("喜欢", "讨厌"),
    ("爱", "讨厌"),
    ("爱", "恨"),
    ("愿意", "拒绝"),
)

#: 判矛盾前先抹掉的字符：空白与标点不携带语义，留着会让「喜欢越剧。」与「不喜欢越剧」
#: 在字符串层面看起来毫无关系，纯函数的判据必须先把这层噪音去掉。
_NOISE_RE: Final[re.Pattern[str]] = re.compile(r"[\s，。、；：！？…—\-·\"'“”‘’（）()《》〈〉]+")


def _strip_noise(text: str) -> str:
    """抹掉空白与标点，只留字面本身（判矛盾的输入规范化，不做任何语义改写）。"""
    return _NOISE_RE.sub("", text)


def _polarity(text: str, predicate: str) -> int:
    """`predicate` 在 `text` 里的极性：`+1` 肯定 / `-1` 否定 / `0` 没出现。

    出现多次时「任意一次肯定即视为肯定」：长者说「我喜欢越剧，也不喜欢太吵的」，
    这条记忆的主干仍是「喜欢」，不该因为句尾一个否定就被判成否定式。
    """
    start = 0
    negated = False
    while True:
        index = text.find(predicate, start)
        if index < 0:
            # 出现过但每次都被否定 → 这是「否定式」；一次都没出现 → 这个谓词与本句无关。
            return -1 if negated else 0
        window = text[max(0, index - 2) : index]
        if not any(negation in window for negation in _NEGATIONS):
            return 1
        negated = True
        start = index + len(predicate)


def memories_contradict(old: str, new: str) -> bool:
    """两条记忆是否**语义矛盾**（确定性判定，不调模型、无 I/O、无随机）。

    FR-MEM-03 的「取代」要同时满足两件事：够像（余弦 > `SUPERSEDE_SIMILARITY`，由调用方判）
    **且**说的相反（本函数判）。这里只做一件事：**同一槽位一正一负**。

    - 判据一：同一个谓词一侧肯定、一侧否定（喜欢 / 不喜欢；去 / 没去过）。
    - 判据二：命中一组反义对（喜欢 / 讨厌）—— 两侧都是肯定词，靠词义对立判。
    - 「我不吃香菜」与「我不吃辣」两侧都否定 → **不矛盾**（各说各的忌口，必须并存）；
      「我喜欢越剧」与「我喜欢京剧」→ 没有一正一负 → **不矛盾**（两份偏好并存）。
    - 表外措辞、人名地点数字的差异一律**不判矛盾**：漏判的代价只是两条都留着、召回时按
      相似度排序；误判的代价是把长者亲口说过的话标成失效，且没有任何入口能恢复 ——
      故本函数宁可漏判（这是有意的取舍，不是能力不足）。

    纯函数这一点是硬要求：调用点在 `remember` 的事务里，写库层不能在事务里发外部请求，
    而「判矛盾要不要问模型」的答案就是「不问」——同一对输入必须永远同一个结果。
    """
    left = _strip_noise(old)
    right = _strip_noise(new)
    if not left or not right or left == right:
        return False

    for predicate in _CONFLICT_PREDICATES:
        # 两侧极性的乘积为 -1 才是「一正一负」；同为 +1 或同为 -1 都不算矛盾。
        if _polarity(left, predicate) * _polarity(right, predicate) == -1:
            return True

    return any(
        (first in left and second in right) or (first in right and second in left)
        for first, second in _ANTONYMS
    )


# ------------------------------------------------------------------ 1. 写入


async def remember(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    content: str,
    embedding_provider: EmbeddingProvider,
    category: str = DEFAULT_CATEGORY,
    importance: float = 1.0,
    source: str = DEFAULT_MEMORY_SOURCE,
    source_thread_id: str | None = None,
) -> MemoryChunk:
    """写入一条记忆；与同长者同类 active 条目「够像**且**说的相反」时把旧条置 `superseded`。

    - 正文 `strip()` 后为空或超 200 字 → `ValueError`（**不静默截断**）；
    - `source` 决定来源系数（`MEMORY_SOURCE_COEFFICIENTS`）：`explicit` 1.0 / `inferred` 0.6，
      落库前 `importance *= 系数` 并夹回 [0,1]；来源非法同样 `ValueError`；
    - provider 不可用 / 调用失败 → `embedding=None` + `memory_write_degraded_total{reason}`，
      **照写不误**（见模块 docstring 的「为什么」）；
    - 冲突判定只看**同一长者、同一类别、active、有向量**的条目：跨长者的相似不该影响
      本人记忆（闸门②），跨类别的相似若也 supersede，会把家属可见的记忆悄悄删掉；
    - **相似度不是取代的充分条件**（FR-MEM-03）：余弦 > `SUPERSEDE_SIMILARITY` 只是把候选
      收窄，还要 `memories_contradict` 判「一正一负」才真的取代。高相似但不矛盾的两条
      （「喜欢越剧」与「喜欢京剧」）**必须并存**；完全相同的一句按去重取代。

    不 commit：本函数只做「这一条」的写入，事务边界由调用方（API 的 `get_session`
    或子图的服务入口）掌握 —— 与 `health_service` 同口径，异常时整体回滚。
    """
    text = _normalise_content(content)
    if category not in RECALL_CATEGORIES:
        raise ValueError(f"category must be one of {RECALL_CATEGORIES}")

    vector = await _embed_text(embedding_provider, text, metric="memory_write_degraded_total")
    superseded = 0
    supersede_reason: str | None = None
    if vector is not None:
        superseded, supersede_reason = await _supersede_conflicts(
            session, elder_id=elder_id, category=category, vector=vector, content=text
        )

    row = MemoryChunk(
        elder_id=elder_id,
        content=text,
        embedding=vector,
        category=category,
        importance=_apply_source_weight(importance, source),
        status="active",
        source_thread_id=source_thread_id,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)

    logger.info(
        "memory_remembered",
        extra={
            "chunk_id": str(row.chunk_id),
            "elder_id": str(elder_id),
            "category": category,
            "has_embedding": vector is not None,
            "superseded": superseded,
            "supersede_reason": supersede_reason or "-",
            "content_length": len(text),
            "source": source,
            "importance": float(row.importance),
        },
    )
    return row


async def _supersede_conflicts(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    category: str,
    vector: list[float],
    content: str,
) -> tuple[int, str | None]:
    """把「够像**且**说的相反」的同长者同类 active 条目置 `superseded`（FR-MEM-03/06）。

    返回 `(取代条数, 触发原因)`；触发原因只进日志（`"duplicate"` / `"contradiction"`，
    一条都没取代时为 `None`）。两道闸缺一不可：

    1. 余弦 > `SUPERSEDE_SIMILARITY`：把候选收窄到「大概在说同一件事」。离谱的两句话
       即使一正一负（「我喜欢越剧」与「我不吃香菜」）也不该互相取代。
    2. `memories_contradict`：够像但说的是两件事（「我喜欢越剧」与「我喜欢京剧」）必须
       **并存**。这一步是 FR-MEM-03 最容易做错的地方 —— 只凭相似度取代，会把长者的
       第二份偏好直接抹掉，而且没有任何入口能恢复。

    完全相同的一句话视为「同一事实的重复写入」，按去重取代（既有口径：记忆回填与
    `test_memory_api_source` 都依赖它）。其余「够像但不矛盾」的一律不动。

    候选窗口取 `SUPERSEDE_SCAN_LIMIT` 条（理由见常量注释）。这条查询同样带 `elder_id`
    （闸门②静态扫描要求）；整段是纯 SQL + 纯函数，事务里不发外部请求。
    """
    distance = MemoryChunk.embedding.cosine_distance(vector)
    rows = (
        await session.execute(
            select(MemoryChunk, distance.label("distance"))
            .where(
                MemoryChunk.elder_id == elder_id,
                MemoryChunk.status == "active",
                MemoryChunk.category == category,
                MemoryChunk.embedding.is_not(None),
            )
            .order_by(distance.asc())
            .limit(SUPERSEDE_SCAN_LIMIT)
        )
    ).all()
    if not rows:
        return 0, None

    target = _strip_noise(content)
    replaced = 0
    reason: str | None = None
    for previous, raw_distance in rows:
        similarity = 1.0 - float(raw_distance)
        if similarity <= SUPERSEDE_SIMILARITY:
            continue

        if _strip_noise(previous.content) == target:
            cause = "duplicate"
        elif memories_contradict(previous.content, content):
            cause = "contradiction"
        else:
            # 够像但不矛盾：留着。这条分支就是 FR-MEM-03 与旧实现的全部差别。
            continue

        previous.status = "superseded"
        replaced += 1
        reason = reason or cause
        logger.info(
            "memory_superseded",
            extra={
                "chunk_id": str(previous.chunk_id),
                "elder_id": str(elder_id),
                "category": category,
                "similarity": round(similarity, 4),
                "cause": cause,
            },
        )

    if replaced:
        await session.flush()
    return replaced, reason


# ------------------------------------------------------------------ 2. 召回


async def recall(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    query: str,
    embedding_provider: EmbeddingProvider,
    top_k: int = 5,
) -> list[MemoryChunk]:
    """按余弦取该长者 top-k active 记忆，并把这些行的 `last_accessed` 刷成现在。

    **一条 select 里同时带 `elder_id` / `status='active'` / `category IN (private, family)`**
    —— 闸门② 的静态扫描与运行期断言都认这一条（FR-ADM-06 / AC-39）。

    provider 不可用、查询文本为空、或没有向量可用时返回 `[]` 并发
    `memory_recall_degraded_total{reason=...}`：**不得臆造**（AGENTS §6 降级列：
    「召回失败 → 本轮不加载记忆」）。`status='deleted'` / `superseded` 的条目即使向量
    仍在库（TD §3.4 说 deleted 是硬删前状态、向量物理残留）也不会被召回 —— 这就是 FR-MEM-05
    的「召回后二次过滤」在 SQL 层的落法。

    相关性下限 `RECALL_MIN_SIMILARITY` 也在这条 SQL 里（`distance <= 1 - 下限`），理由：
    「同一位长者、还 active」只保证这条记忆**关于他**，不保证它**关于这句话**——库里
    永远有 5 条最近邻，不设下限就等于把最不相关的那几条也当上下文喂给模型（AC-47 的
    记忆版）。下限与取代阈值分属两个常量，见 `RECALL_MIN_SIMILARITY` 的注释。
    过滤后为空**不是降级**（不记 degraded 打点）：库是好的，只是这次问的事没记过。
    """
    if top_k <= 0:
        raise ValueError("top_k must be positive")

    text = (query or "").strip()
    if not text:
        counter("memory_recall_degraded_total", reason="empty_query")
        return []

    vector = await _embed_text(embedding_provider, text, metric="memory_recall_degraded_total")
    if vector is None:
        return []

    distance = MemoryChunk.embedding.cosine_distance(vector)
    # 「已失效」和「不相关」是两件事，两条条件都要在（FR-MEM-05）：前者由 status 挡，
    # 后者由这条距离上限挡。距离上限写成 SQL 条件而不是取回后再过滤，是为了让 LIMIT
    # 作用在「够相关的行」上 —— 否则最近 5 条全被滤掉时会返回空，而第 6 条其实是能用的。
    relevance_floor = 1.0 - RECALL_MIN_SIMILARITY
    rows = (
        (
            await session.execute(
                select(MemoryChunk)
                .where(
                    MemoryChunk.elder_id == elder_id,
                    MemoryChunk.status == "active",
                    MemoryChunk.category.in_(RECALL_CATEGORIES),
                    MemoryChunk.embedding.is_not(None),
                    distance <= relevance_floor,
                )
                # 余弦距离升序 = 相似度降序；同分时新条目优先，避免「刚记住的说不出来」。
                .order_by(distance.asc(), MemoryChunk.created_at.desc())
                .limit(top_k)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return []

    now = _now()
    await session.execute(
        update(MemoryChunk)
        .where(
            MemoryChunk.chunk_id.in_([row.chunk_id for row in rows]),
            # 再带一次 elder_id：即便上游把 chunk_id 列表拼错，也改不到别人家的行。
            MemoryChunk.elder_id == elder_id,
        )
        .values(last_accessed=now)
        .execution_options(synchronize_session=False)
    )
    # ORM 对象与 Core UPDATE 不会自动同步；手工对齐，免得调用方读到过期的 last_accessed。
    for row in rows:
        row.last_accessed = now
    logger.info(
        "memory_recalled",
        extra={
            "elder_id": str(elder_id),
            "hits": len(rows),
            "top_k": top_k,
            "min_similarity": RECALL_MIN_SIMILARITY,
        },
    )
    return list(rows)


async def list_chunks(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    status: str | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
) -> Sequence[MemoryChunk]:
    """照护台「记忆管理」列表：按创建时间倒序列出某位长者的记忆条目。

    与 `recall` 的区别是**不做相似度检索**：后台要的是「这个人现在有哪些记忆、
    哪条被归并了」，不是「和这句话最像的 5 条」。因此这条查询不需要
    `embedding_provider`，也不会写 `last_accessed` —— 护工翻列表不该影响记忆的
    新旧排序（否则护工一翻页，衰减与容量淘汰的判据就被污染）。

    两处安全口径与其余函数一致：①`WHERE elder_id` 硬绑，`elder_id` 只能由
    `runtime_context` 注入（AGENTS §4.9），本函数没有从入参文本推身份的口子；
    ②只读、只 flush 不 commit，事务边界归 `get_session`。

    `status` 只接受列 CHECK 里的三个取值（`active`/`superseded`/`deleted`）；
    传别的值直接抛 ValueError，而不是静默返回空列表 —— 静默会让前端误以为
    「这个人没有记忆」，属于把配置错误伪装成业务事实。
    """
    if limit < 1 or limit > MAX_LIST_LIMIT:
        raise ValueError(f"limit must be in [1, {MAX_LIST_LIMIT}]")
    if offset < 0:
        raise ValueError("offset must not be negative")
    if status is not None and status not in MEMORY_STATUSES:
        raise ValueError(f"status must be one of {MEMORY_STATUSES}")

    conditions = [MemoryChunk.elder_id == elder_id]
    if status is not None:
        conditions.append(MemoryChunk.status == status)

    rows = (
        (
            await session.execute(
                select(MemoryChunk)
                .where(*conditions)
                # chunk_id 做次序键：同一批写入的 created_at 可能完全相同（server_default
                # 是语句级 now()），只按时间排序会得到不稳定分页，翻页时条目会重复/漏掉。
                .order_by(MemoryChunk.created_at.desc(), MemoryChunk.chunk_id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    logger.info(
        "memory_listed",
        extra={
            "elder_id": str(elder_id),
            "status": status or "*",
            "limit": limit,
            "offset": offset,
            "rows": len(rows),
        },
    )
    return list(rows)


# ------------------------------------------------------------------ 3. 衰减


async def decay(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID | None = None,
    days: int = DECAY_DAYS,
    factor: float = DECAY_FACTOR,
) -> int:
    """把 `days` 天未「露面」的 active 条目 `importance *= factor`，返回受影响行数。

    「露面」= `last_accessed`，从未召回过的按 `created_at` 算（见 `_last_seen`）。
    `elder_id=None` 表示全库跑一遍（APScheduler 的每日维护任务用它），传了就只动那一位
    长者（单长者重算/测试用）。`factor` 只接受 (0,1)：等于 1 是空操作、大于 1 是「越冷越
    重要」，两种都不该被一条任务悄悄执行。
    """
    if days <= 0:
        raise ValueError("days must be positive")
    if not 0.0 < factor < 1.0:
        raise ValueError("factor must be in (0, 1)")

    cutoff = _now() - datetime.timedelta(days=days)
    conditions = [MemoryChunk.status == "active", _last_seen() <= cutoff]
    if elder_id is not None:
        conditions.append(MemoryChunk.elder_id == elder_id)

    # factor 走 Decimal 而不是 float：`numeric * float8` 在 PG 里会先落成 double precision，
    # 而 `round(double precision, int)` 这个函数**在 PG 里不存在**（只有 `round(numeric, int)`）。
    # 用 Decimal 绑参 + `cast(..., NUMERIC(3,2))`：乘出来是 numeric，转回列精度时按 PG 规则
    # 四舍五入到两位，0.5 的衰减不会在列里留下 0.125 这种超出 scale 的尾巴。
    decay_factor = Decimal(str(factor))
    result = await session.execute(
        update(MemoryChunk)
        .where(*conditions)
        .values(importance=cast(MemoryChunk.importance * decay_factor, Numeric(3, 2)))
        .execution_options(synchronize_session=False)
    )
    changed = _affected(result)
    logger.info(
        "memory_decayed",
        extra={
            "elder_id": str(elder_id) if elder_id is not None else "*",
            "days": days,
            "factor": factor,
            "changed": changed,
        },
    )
    return changed


# ------------------------------------------------------------------ 4. 容量淘汰


async def enforce_capacity(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    cap: int = CAPACITY_PER_ELDER,
) -> int:
    """active 条目超过 `cap` 时软删（`status='deleted'`）最该淘汰的那些，返回条数。

    淘汰顺序：`importance` 升序 → 最后露面最旧优先。两个键都必须有：只按 importance
    淘汰会把「很久没提但很重要」的条目先丢；只按时间淘汰会把每天被召回的低价值寒暄
    一直留着。

    **软删**而不是 DELETE：TD §3.4 的 `status='deleted'` 语义就是「已删但向量物理残留」，
    硬删由定期清理任务负责（AC-14 要求删除有审计可查）。软删后 `recall` 的
    `status='active'` 过滤立刻让它不再进入上下文（FR-MEM-05）。
    """
    if cap < 0:
        raise ValueError("cap must not be negative")

    active = int(
        (
            await session.execute(
                select(func.count())
                .select_from(MemoryChunk)
                .where(
                    MemoryChunk.elder_id == elder_id,
                    MemoryChunk.status == "active",
                )
            )
        ).scalar_one()
    )
    excess = active - cap
    if excess <= 0:
        return 0

    victims = (
        (
            await session.execute(
                select(MemoryChunk.chunk_id)
                .where(
                    MemoryChunk.elder_id == elder_id,
                    MemoryChunk.status == "active",
                )
                .order_by(MemoryChunk.importance.asc(), _last_seen().asc())
                .limit(excess)
            )
        )
        .scalars()
        .all()
    )
    if not victims:
        return 0

    await session.execute(
        update(MemoryChunk)
        .where(
            MemoryChunk.chunk_id.in_(list(victims)),
            MemoryChunk.elder_id == elder_id,
        )
        .values(status="deleted")
        .execution_options(synchronize_session=False)
    )
    logger.info(
        "memory_capacity_enforced",
        extra={
            "elder_id": str(elder_id),
            "cap": cap,
            "active": active,
            "deleted": len(victims),
        },
    )
    return len(victims)


# ------------------------------------------------------------------ 5. 删除（退房/撤回）


async def delete_by_elder(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    actor_id: uuid.UUID | None = None,
    reason: str = "checkout",
) -> int:
    """软删该长者全部 active 记忆，并**在同一事务**写一条 `audit_logs`，返回删除条数。

    范式抄 迭代 4 的 `voiceprint_service.delete_all_for_elder`：删除与审计同事务提交，
    要么「删了且有痕迹」，要么都不发生（AC-14 / FR-MEM-02「删除后立即停止检索注入并记审计」）。
    本函数**不 commit**，两条语句都在调用方的事务里 —— 中途失败整体回滚，不会留下
    「删了没记录」或「记录了没删」的半截状态。

    `actor_type` 由 `actor_id` 推：给了操作者 ID 就是 `staff`（护工/管理员代办），
    没给就是 `system`（退房定时清理）。TD 的 `audit_logs.actor_type` 只有
    staff/guardian/system/device 四值，长者本人删记忆走 `device` 由 API 层另行传值 ——
    但本签名只有 `actor_id`，所以这里不臆造角色，只用这两个最稳的取值。
    """
    result = await session.execute(
        update(MemoryChunk)
        .where(
            MemoryChunk.elder_id == elder_id,
            MemoryChunk.status == "active",
        )
        .values(status="deleted")
        .execution_options(synchronize_session=False)
    )
    deleted = _affected(result)

    session.add(
        AuditLog(
            actor_type="staff" if actor_id is not None else "system",
            actor_id=actor_id if actor_id is not None else elder_id,
            action="delete_memory",
            resource_type="memory_chunks",
            resource_id=elder_id,
            detail={
                "reason": reason,
                "deleted_chunks": deleted,
                "soft_delete": True,
            },
        )
    )
    await session.flush()
    logger.info(
        "memory_delete_by_elder",
        extra={
            "elder_id": str(elder_id),
            "deleted": deleted,
            "reason": reason,
            "has_actor": actor_id is not None,
        },
    )
    return deleted


# ------------------------------------------------------------------ 6. 长者本人端（FR-MEM-02）


class MemoryChunkNotFoundError(Exception):
    """按 `(elder_id, chunk_id)` 取不到行。

    「不存在」「属于别人」「已删除」三种情况**在 API 层一律 404 且错误体不区分**：
    把「这条存在但不属于你」单独回 403，等于给客户端一个可探测的面
    （遍历 UUID 就能确认哪些 chunk 真实存在）。故本异常只表达「本次取不到」。
    """


async def load_chunk(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    chunk_id: uuid.UUID,
    include_deleted: bool = False,
) -> MemoryChunk:
    """按 `(elder_id, chunk_id)` 取一条记忆，取不到抛 `MemoryChunkNotFoundError`。

    `elder_id` 与 `chunk_id` **同时**进 `WHERE`：先按 chunk_id 查、再在 Python 里比
    `row.elder_id == elder_id` 也能得到同样结果，但那样闸门②（「所有查询强制带
    `WHERE elder_id`」）就只存在于调用方的 if 里，迭代 9 的静态扫描看不见它，
    后续重构也容易把这一句删掉。跨长者与不存在在 SQL 层就是同一个空结果。
    """

    conditions = [MemoryChunk.chunk_id == chunk_id, MemoryChunk.elder_id == elder_id]
    if not include_deleted:
        conditions.append(MemoryChunk.status != "deleted")
    row = (await session.execute(select(MemoryChunk).where(*conditions))).scalars().first()
    if row is None:
        raise MemoryChunkNotFoundError(f"memory chunk {chunk_id} is not available for this elder")
    return row


async def correct_chunk(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    chunk_id: uuid.UUID,
    content: str,
    embedding_provider: EmbeddingProvider | None = None,
    actor_id: uuid.UUID | None = None,
) -> MemoryChunk:
    """长者本人纠正一条记忆的正文（FR-MEM-02），并**同事务**写 `audit_logs`。

    只动 `content`（以及随正文重算的 `embedding`），**不碰 `importance`**：
    `importance` 是权重不是正文，改一句话不该把「这条有多重要」重置回写入默认值——
    否则长者亲自确认过的一条记忆会被衰减/容量淘汰当成新写的一条重新排位，
    「纠正错别字」和「重新写一遍」被折成同一种事实。

    重算向量的理由与口径：正文变了，旧向量描述的是旧句子，留着它会让这条记忆按**旧语义**
    被召回（比召回不到更难查）。所以传了 provider 就重算；拿不到向量时**清空**
    `embedding`（`recall` 会跳过它，文本仍可查可改），不保留一条与正文不符的向量——
    与 `remember` 的降级口径一致（同一打点 `memory_write_degraded_total`）。

    `actor_id` 由调用方传入设备 ID（长者端令牌的主体），服务层不臆造身份。
    """

    text = _normalise_content(content)
    row = await load_chunk(session, elder_id=elder_id, chunk_id=chunk_id)
    embedding: list[float] | None = None
    if embedding_provider is not None:
        embedding = await _embed_text(
            embedding_provider, text, metric="memory_write_degraded_total"
        )
    row.content = text
    row.embedding = embedding

    session.add(
        AuditLog(
            actor_type="device",
            actor_id=actor_id if actor_id is not None else elder_id,
            action=ACTION_CHUNK_CORRECT,
            resource_type="memory_chunks",
            resource_id=chunk_id,
            detail={
                "elder_id": str(elder_id),
                # 正文一律不进审计/日志：长度足够判断「改了多少」，且不落第二份原文。
                "content_length": len(text),
                "importance_unchanged": True,
                "embedding_updated": embedding is not None,
                "embedding_provider": (
                    type(embedding_provider).__name__ if embedding_provider is not None else None
                ),
            },
        )
    )
    await session.flush()
    logger.info(
        "memory_chunk_corrected",
        extra={
            "elder_id": str(elder_id),
            "chunk_id": str(chunk_id),
            "content_length": len(text),
            "has_embedding": embedding is not None,
            "has_actor": actor_id is not None,
        },
    )
    return row


async def delete_chunk(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    chunk_id: uuid.UUID,
    actor_id: uuid.UUID | None = None,
) -> bool:
    """长者本人软删自己的一条记忆；返回「本次是否真的把一条非 deleted 翻成 deleted」。

    已删除的条目再删一次**不报错**、返回 `False`，但**照样写一条审计**
    （`detail.already_deleted=True`）：重复删除本身是事实（FR-MEM-02 的删除要可回查），
    吞掉它等于丢掉一次操作痕迹。跨长者与不存在仍抛 `MemoryChunkNotFoundError`（404），
    这两者不写审计——它们根本没发生在这条记忆上。
    """

    row = await load_chunk(session, elder_id=elder_id, chunk_id=chunk_id, include_deleted=True)
    already_deleted = row.status == "deleted"
    if not already_deleted:
        row.status = "deleted"

    session.add(
        AuditLog(
            actor_type="device",
            actor_id=actor_id if actor_id is not None else elder_id,
            action=ACTION_CHUNK_DELETE,
            resource_type="memory_chunks",
            resource_id=chunk_id,
            detail={
                "elder_id": str(elder_id),
                "already_deleted": already_deleted,
                "soft_delete": True,
            },
        )
    )
    await session.flush()
    logger.info(
        "memory_chunk_deleted",
        extra={
            "elder_id": str(elder_id),
            "chunk_id": str(chunk_id),
            "already_deleted": already_deleted,
            "has_actor": actor_id is not None,
        },
    )
    return not already_deleted


# ------------------------------------------------------------------ 7. 周归并（FR-MEM-07）


#: 归并的审计动作名（FR-MEM-07）：`audit_logs.action = "memory.merge"`。
#: 与 `ACTION_CHUNK_CORRECT` / `ACTION_CHUNK_DELETE` 同一张表、同一套检索方式 ——
#: **不新建表**：归并是「一次状态翻转 + 一条痕迹」，再开一张 `memory_merges` 只是把
#: 审计表里已有的能力复制一份（AGENTS §4 第 8 条：一个能力只允许一种形态）。
ACTION_MERGE: Final[str] = "memory.merge"

#: 周归并的相似度阈值：**与 `SUPERSEDE_SIMILARITY` 同为 0.9 但不是同一个常量**。
#: 两者回答的问题不同：`SUPERSEDE_SIMILARITY` 是「新写入的这一条是否推翻旧的」，
#: 归并是「两条已存在的记忆是不是同一件事的两种说法」。前者还会再过
#: `memories_contradict` 判矛盾；后者只看相似度，因此触发条件是**整个类别内两两比较**，
#: 判错会并掉不同事实（不可逆但可查），故阈值走参数、默认 0.9，且记录里带上实际相似度。
MERGE_SIMILARITY: Final[float] = 0.9

#: 单次归并最多取多少对「够像」的切片。`CAPACITY_PER_ELDER` 是 500，最坏情况一个类别
#: 500 条两两比较是 12.5 万对；周任务没必要为一次近乎全同的极端数据拉这么多行。
#: 取 5000（≈100 条全同的数据量）：按距离升序取最像的那批，本轮并不完的留给下周，
#: **不静默改变判定口径**——超出部分只是没被本次看到。
MERGE_PAIR_SCAN_LIMIT: Final[int] = 5000


@dataclass(frozen=True, slots=True)
class MergeReport:
    """一轮「全库归并」的计数（`merged` 是被并掉的条数，不是组数）。"""

    elders: int
    failed: int
    merged: int
    threshold: float


def _merge_rank(row: Any) -> tuple[Decimal, datetime.datetime, uuid.UUID]:
    """保留者的排序键：`importance` 高优先 → 最近露面新优先 → `chunk_id` 定序。

    第三个键只为**确定性**：两条重要性、时间都相同（同一次批量写入）时，
    没有它结果依赖行返回顺序，同一份数据跑两次可能保留不同的一条。
    """
    seen = row.last_accessed or row.created_at or datetime.datetime.min.replace(tzinfo=datetime.UTC)
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=datetime.UTC)
    return (row.importance, seen, row.chunk_id)


async def merge_duplicates(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    threshold: float = MERGE_SIMILARITY,
) -> int:
    """把该长者**同一类别内**互为重复的 active 切片归并成一条，返回被并掉的条数（FR-MEM-07）。

    口径（计划 §2.5）：

    - 候选是 `status='active'` **且有向量**的切片；没有向量的条目无从比较，一律不动
      （与 `decay` 的口径一致：拿不到向量是降级状态，不该被当成"不重复"的证据）；
    - 只在**同一 `category` 内**比：`private` 与 `family` 是两种可见范围，跨类别归并会
      把家属可见的那份悄悄挪进只给本人看的类别（或反之），属于改数据语义；
    - 余弦 **> `threshold`** 视为重复；保留 `importance` 最高的一条，平手取
      `last_accessed` 最新的一条（见 `_merge_rank`），其余置 `status='superseded'`；
    - 「重复」按**传递闭包**处理：A≈B、B≈C 时三条并成一组。理由是与「两两重复即一组」
      相比，传递闭包不会留下"中间那条被并了、端点还 active"的半截状态——而被留下的端点
      正是召回时还会被读出来的那一条；
    - 每组写一条 `audit_logs`（`action='memory.merge'`、`resource_type='memory_chunks'`、
      `detail={kept_id, merged_ids, similarity, threshold, category}`），**与状态翻转同事务**：
      本函数不 commit，要么"并了且有痕迹"，要么都不发生；
    - 没有可归并的条目时**不写审计**（空跑不是事实），返回 0 —— 这也是重复执行的幂等口径：
      第二次跑的时候库里已经只剩 active 的那一条，`merged=0`。

    正文一律不进审计与日志（AGENTS §4 第 11 条）：`detail` 里只有 ID、类别与相似度。
    """
    if not 0.0 < threshold <= 1.0:
        raise ValueError("threshold must be in (0, 1]")

    rows = (
        await session.execute(
            select(
                MemoryChunk.chunk_id,
                MemoryChunk.category,
                MemoryChunk.importance,
                MemoryChunk.last_accessed,
                MemoryChunk.created_at,
            )
            .where(
                MemoryChunk.elder_id == elder_id,
                MemoryChunk.status == "active",
                MemoryChunk.embedding.is_not(None),
            )
            .order_by(MemoryChunk.category, MemoryChunk.chunk_id)
        )
    ).all()
    if len(rows) < 2:
        return 0

    left = aliased(MemoryChunk)
    right = aliased(MemoryChunk)
    distance = left.embedding.cosine_distance(right.embedding)
    pairs = (
        await session.execute(
            select(left.chunk_id, right.chunk_id, distance.label("distance"))
            .where(
                left.elder_id == elder_id,
                right.elder_id == elder_id,
                left.status == "active",
                right.status == "active",
                left.embedding.is_not(None),
                right.embedding.is_not(None),
                left.category == right.category,
                left.chunk_id < right.chunk_id,
                distance < 1.0 - threshold,
            )
            .order_by(distance.asc())
            .limit(MERGE_PAIR_SCAN_LIMIT)
        )
    ).all()
    if not pairs:
        return 0

    # 并查集：同一组里的切片共享一个根。传递闭包的理由见 docstring。
    parent: dict[uuid.UUID, uuid.UUID] = {row.chunk_id: row.chunk_id for row in rows}

    def _find(node: uuid.UUID) -> uuid.UUID:
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != root:
            parent[node], node = root, parent[node]
        return root

    similarities: dict[tuple[uuid.UUID, uuid.UUID], float] = {}
    for first, second, raw_distance in pairs:
        similarity = 1.0 - float(raw_distance)
        if similarity <= threshold:  # SQL 的浮点边界兜底：口径以这里为准
            continue
        similarities[(min(first, second), max(first, second))] = similarity
        root_a, root_b = _find(first), _find(second)
        if root_a != root_b:
            parent[root_b] = root_a

    groups: dict[uuid.UUID, list[Any]] = {}
    for row in rows:
        groups.setdefault(_find(row.chunk_id), []).append(row)

    merged_rows = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=_merge_rank, reverse=True)
        kept = members[0]
        merged_ids = [member.chunk_id for member in members[1:]]
        member_ids = {member.chunk_id for member in members}
        group_similarity = max(
            (
                value
                for (first, second), value in similarities.items()
                if first in member_ids and second in member_ids
            ),
            default=0.0,
        )

        await session.execute(
            update(MemoryChunk)
            .where(
                MemoryChunk.elder_id == elder_id,
                MemoryChunk.chunk_id.in_(merged_ids),
                MemoryChunk.status == "active",
            )
            .values(status="superseded")
            .execution_options(synchronize_session=False)
        )
        session.add(
            AuditLog(
                actor_type="system",
                actor_id=elder_id,
                action=ACTION_MERGE,
                resource_type="memory_chunks",
                resource_id=kept.chunk_id,
                detail={
                    "elder_id": str(elder_id),
                    "kept_id": str(kept.chunk_id),
                    "merged_ids": [str(chunk_id) for chunk_id in merged_ids],
                    "similarity": round(group_similarity, 4),
                    "threshold": threshold,
                    "category": kept.category,
                    "merged_count": len(merged_ids),
                },
            )
        )
        merged_rows += len(merged_ids)
        logger.info(
            "memory_merged",
            extra={
                "elder_id": str(elder_id),
                "kept_id": str(kept.chunk_id),
                "merged": len(merged_ids),
                "category": kept.category,
                "similarity": round(group_similarity, 4),
                "threshold": threshold,
            },
        )

    if merged_rows:
        await session.flush()
    return merged_rows


async def merge_all_active_elders(
    session: AsyncSession,
    *,
    threshold: float = MERGE_SIMILARITY,
) -> MergeReport:
    """遍历在住长者逐个归并，**单长者失败 continue 并计数**；返回本轮汇总。

    为什么循环放在服务层而不是调度器：手动入口 `POST /api/memory/merge`（不带 `elder_id`）
    也要"全量跑一遍"，而 `app/api/**` 不许自己发 `select(Elder)`（AGENTS §2.1 分层）。
    两条路共用这一份循环，口径就不会分叉。

    每位长者一个 SAVEPOINT（`begin_nested`）：一位长者失败只回滚他那一段，前面已经并掉的
    不回退（与 `run_memory_capacity_eviction` 的 `rollback()` 相比更保守——那里用的是整事务
    回滚，因为容量淘汰可以整体重来，归并是**不可逆**的状态翻转，已写下的痕迹不该被别人的
    失败抹掉）。本函数不 commit，事务边界仍归调用方。
    """
    if not 0.0 < threshold <= 1.0:
        raise ValueError("threshold must be in (0, 1]")

    elder_ids = (
        (
            await session.execute(
                select(Elder.elder_id).where(Elder.status == "active").order_by(Elder.elder_id)
            )
        )
        .scalars()
        .all()
    )

    merged = 0
    failed = 0
    done = 0
    for elder_id in elder_ids:
        try:
            async with session.begin_nested():
                merged += await merge_duplicates(session, elder_id=elder_id, threshold=threshold)
        except Exception:
            failed += 1
            logger.warning(
                "memory_merge_failed",
                extra={"elder_id": str(elder_id), "threshold": threshold},
                exc_info=True,
            )
            continue
        done += 1

    logger.info(
        "memory_merge_finished",
        extra={
            "elders": len(elder_ids),
            "done": done,
            "failed": failed,
            "merged": merged,
            "threshold": threshold,
        },
    )
    return MergeReport(elders=done, failed=failed, merged=merged, threshold=threshold)


__all__ = [
    "ACTION_CHUNK_CORRECT",
    "ACTION_CHUNK_DELETE",
    "ACTION_MERGE",
    "CAPACITY_PER_ELDER",
    "DECAY_DAYS",
    "DECAY_FACTOR",
    "DEFAULT_CATEGORY",
    "DEFAULT_LIST_LIMIT",
    "DEFAULT_MEMORY_SOURCE",
    "MAX_CONTENT_LENGTH",
    "MAX_LIST_LIMIT",
    "MEMORY_SOURCE_COEFFICIENTS",
    "MEMORY_STATUSES",
    "MERGE_PAIR_SCAN_LIMIT",
    "MERGE_SIMILARITY",
    "RECALL_CATEGORIES",
    "RECALL_MIN_SIMILARITY",
    "SUPERSEDE_SCAN_LIMIT",
    "SUPERSEDE_SIMILARITY",
    "MemoryChunkNotFoundError",
    "MergeReport",
    "correct_chunk",
    "cosine_similarity",
    "decay",
    "delete_by_elder",
    "delete_chunk",
    "enforce_capacity",
    "list_chunks",
    "load_chunk",
    "memories_contradict",
    "merge_all_active_elders",
    "merge_duplicates",
    "recall",
    "remember",
]
