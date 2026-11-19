# owner=W2 · 依据 设计记录（全量缺口补齐） · Laya 服务壳（N31）
"""Laya System One 服务壳：把「七类 intent + 三个 noul」包成 `POST /v1/classify`。

## 为什么是一层薄壳，而不是直接复用 `laya-serve`

PyPI 上的 `laya-serve`（0.2.1）只提供 Jev 协议端点 `/v1/systemone`、
`/v1/systemone/batch`、`/health`，响应体是 Jev 的 TypeSafe 结构，**没有可插拔的
字段映射钩子**；而我们的契约是 `client.py` 里的五字段 `POST /v1/classify`。
在它前面再套一层代理等于多一跳 + 多一次序列化，故直接用 SDK `laya.load()` 写薄壳。
加载器选 SDK 也已实测：同一批问题逐条判定 0 条不一致，加载 15052ms vs 权重包自带
`rl_agent_api` 的 86835ms（5.8×），单句延迟更低（见 `README.md` 对照表）。

## 契约（唯一来源 `app/system_one/client.py`）

- 入参 `{"text": str}`；出参**且仅且是**五字段：
  `intent: str` / `sensitive: bool` / `confidence: float∈[0,1]` /
  `privacy: bool` / `is_out_of_domain: bool`。
- 七类 intent 的取值**只从** `app/system_one/questions.py::INTENT_CLASSES` 取，
  本文件不另写一份类目表（N30 的两处不一致就是缺这个常量造成的）。

## 四信号怎么来的（N30 实测结论，不是猜的）

- `intent` → choice 型问题的选中项；`confidence` 用其 `confidence`（1 − 归一化熵，
  与 N30 口径一致），**不用** `answer_confidence`（那是选中项概率，语义不同）。
- `is_out_of_domain` → 独立 noul 问题。**不能塞进七类 choice**：N30 实测域外三条
  塞进 choice 命中 0/3，独立 noul 却给 0.69 / 0.95 / 0.57。
- `sensitive` → noul ≥ 阈值。**L1 的 sensitive 只是参考信号，不是安全门**：
  N30 实测中文健康主诉的概率 ≤ 0.05（「头有点晕浑身没力气」只有 0.05），
  安全一票否决只在 L0 规则层（`app/router/safety.py`），任何路径不得把这里当兜底。

## 非法响应注入

`?fault=<kind>` 只在 `LAYA_FAULT_INJECT=1` 时生效（默认关），用来演练
`client.py` 的 `LayaBadPayload` / `LayaBadStatus` 拒绝路径：
`missing_field` / `bad_confidence` / `bad_intent` / `bad_bool` / `not_json` /
`bad_status` / `slow`。关着时返回 403，不会在生产环境意外打开。
"""

from __future__ import annotations

import asyncio
import logging
import math
import secrets
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.system_one.questions import INTENT_CLASSES

logger = logging.getLogger("deploy.laya.server")


def _configure_logging() -> None:
    """让加载日志真的出现在 `docker logs` 里。

    uvicorn 只给自己那几个 logger 装了 handler，本模块的 INFO 会走 root 的
    `lastResort`（WARNING 起才打印）——结果是容器「加载中」时日志一片空白，
    现场排查只能看 `/health` 的 JSON。这里补一个 handler，只装一次。
    """
    if logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


class LayaShellSettings(BaseSettings):
    """服务壳自己的配置（`LAYA_` 前缀，与后端同名变量共用一套 env 命名）。

    `extra="ignore"`：后端也在用 `LAYA_` 前缀（`LAYA_BASE_URL` / `LAYA_TIMEOUT_MS`
    等），同一份 compose env 里混着两边的键，多出来的不许炸。
    """

    model_config = SettingsConfigDict(env_prefix="LAYA_", env_file=None, extra="ignore")

    #: 权重根目录（容器里挂载 `/models/laya`，其下有 `multilingual/` 与 `rl_agent_config.json`）。
    model_root: str = "/models/laya"
    #: 子目录：中文必须用 `multilingual`（mmBERT-base）；根目录是英文面的 ModernBERT-large。
    subfolder: str = "multilingual"
    device: str = "cpu"
    #: torch CPU 线程数：实测 4 线程在 8 核机上最优，超过只会跟后端抢核。
    threads: int = 4
    #: 非空则要求 `authorization: Bearer <api_key>`，与 `client.py` 的 `laya_api_key` 对应。
    api_key: str = ""
    #: 非法响应注入总开关，默认关（仅供契约测试）。
    fault_inject: bool = False
    #: noul 三问转 bool 的阈值。0.5 是概率语义中点；调低只会让 L1 更爱报，
    #: 而真正的安全判定在 L0，所以不给它「宁可错杀」的职责。
    noul_threshold: float = 0.5
    max_text_chars: int = 512


def _build_questions() -> dict[str, dict[str, Any]]:
    """四问一次问完（一次前向出五字段）。

    措辞与 N30 `spikes/laya-system-one/probe.py` 逐字相同——那批实测数字（70% 准确率、
    p50 253/851ms）只在同一套问法下可复现；改措辞必须重测，不许悄悄改。
    """
    return {
        "intent": {
            "type": "choice",
            "instructions": "这位老人刚说的这句话，属于下面哪一类意图？只选一类。",
            # 单一出处：七类只从 questions.py 取，不在这里抄。
            "criteria": dict(INTENT_CLASSES),
        },
        "sensitive": {
            "type": "noul",
            "instructions": (
                "这句话是否涉及需要立刻人工介入的安全风险："
                "急症（胸痛、喘不上气、晕倒）、摔倒、自伤、走失、受虐待？"
            ),
            "criteria": {
                "false": "没有安全风险，只是日常说话",
                "true": "存在上述安全风险，必须马上叫人或告警",
            },
        },
        "privacy": {
            "type": "noul",
            "instructions": (
                "这句话是否涉及他人的隐私，或同住另一半/旁人的事务"
                "（打听别人病情、翻别人东西、替别人做主）？"
            ),
            "criteria": {
                "false": "只关乎说话人自己，或纯公共信息",
                "true": "涉及他人隐私或同住旁人的事务",
            },
        },
        "is_out_of_domain": {
            "type": "noul",
            "instructions": (
                "这句话是否超出养老陪伴助手的职责范围"
                "（法律纠纷、股票理财、写代码、政治时事、医疗诊断结论）？"
            ),
            "criteria": {
                "false": "在陪伴、提醒、点播、天气、健康采集的职责内",
                "true": "超出职责范围",
            },
        },
    }


_QUESTIONS: Final[dict[str, dict[str, Any]]] = _build_questions()


def _positive(answer: dict[str, Any], threshold: float) -> bool:
    """noul 的 P(true) → bool；阈值判断只在这里做一次，三个 noul 共用。"""
    return float(answer["noul"]) >= threshold


def _clamp_confidence(raw: Any) -> float:
    """把 choice 的 `confidence` 收进 [0,1]（契约要求），NaN 直接判环境错。"""
    value = float(raw)
    if not math.isfinite(value):  # pragma: no cover - 模型正常不会给 NaN
        raise RuntimeError("laya returned non-finite confidence")
    return min(1.0, max(0.0, value))


class LayaEngine:
    """一次 `laya.load()` + 串行前向。

    串行是刻意的：单句前向 250–850ms、峰值 RSS 约 1032MB，在这台 3.7GiB 的 Docker VM 上
    并发只会把内存和 CPU 一起打爆；L1 本来就是「一句话一次」的调用形态，
    排队比 OOM 好。
    """

    def __init__(self, settings: LayaShellSettings) -> None:
        self._settings = settings
        self._agent: Any = None
        self._lock = threading.Lock()
        self.state: str = "loading"
        self.error: str | None = None
        self.load_ms: float = 0.0

    # ---------------------------------------------------------------- 生命周期

    def load(self) -> None:
        """阻塞加载（在 worker 线程里跑，不挡事件循环）。"""
        try:
            import torch  # 延迟导入：没有 torch 的环境也能 import 本模块做静态检查

            torch.set_num_threads(self._settings.threads)

            import laya

            started = time.perf_counter()
            self._agent = laya.load(
                self._settings.model_root,
                subfolder=self._settings.subfolder,
                device=self._settings.device,
            )
            self.load_ms = (time.perf_counter() - started) * 1000
            self.state = "ready"
            logger.info(
                "laya model loaded",
                extra={"load_ms": round(self.load_ms), "model_root": self._settings.model_root},
            )
        except Exception as exc:
            self.state = "failed"
            self.error = f"{type(exc).__name__}: {exc}"
            logger.error("laya model load failed", extra={"error_type": type(exc).__name__})

    # ---------------------------------------------------------------- 推理

    def classify(self, text: str) -> dict[str, Any]:
        """一句话 → 五字段（同步、阻塞；由调用方丢进线程池）。"""
        if self._agent is None:
            raise RuntimeError(f"laya engine not ready (state={self.state})")

        with self._lock:  # 串行化：同一时刻只跑一次前向
            answers = self._agent.predict(text, _QUESTIONS)["answers"]

        intent = answers["intent"]
        choice = intent["choice"]
        if not isinstance(choice, str):
            raise RuntimeError(f"laya returned non-string choice: {type(choice).__name__}")
        if choice not in INTENT_CLASSES:  # pragma: no cover - 类目由 criteria 决定，正常不会漂
            logger.warning("laya choice outside INTENT_CLASSES", extra={"choice": choice})

        threshold = self._settings.noul_threshold
        return {
            "intent": choice,
            "sensitive": _positive(answers["sensitive"], threshold),
            "confidence": _clamp_confidence(intent["confidence"]),
            "privacy": _positive(answers["privacy"], threshold),
            "is_out_of_domain": _positive(answers["is_out_of_domain"], threshold),
        }


# ---------------------------------------------------------------- 非法响应注入

#: 每种 fault 给出的错误形态，逐一对应 `client.py` 的一个拒绝分支。
_FAULT_PAYLOADS: Final[dict[str, dict[str, Any]]] = {
    # 缺 privacy / is_out_of_domain → LayaBadPayload(missing fields)
    "missing_field": {"intent": "chitchat", "sensitive": False, "confidence": 0.9},
    # confidence 越界 → LayaBadPayload(confidence out of [0,1])
    "bad_confidence": {
        "intent": "chitchat",
        "sensitive": False,
        "confidence": 1.5,
        "privacy": False,
        "is_out_of_domain": False,
    },
    # intent 非字符串 → LayaBadPayload(intent must be string)
    "bad_intent": {
        "intent": 7,
        "sensitive": False,
        "confidence": 0.9,
        "privacy": False,
        "is_out_of_domain": False,
    },
    # 布尔位给了字符串 → LayaBadPayload(sensitive must be bool)
    "bad_bool": {
        "intent": "chitchat",
        "sensitive": "yes",
        "confidence": 0.9,
        "privacy": False,
        "is_out_of_domain": False,
    },
}

#: `slow` 的睡眠时长：远大于客户端默认 200ms，用来触发 `LayaTimeout`。
_FAULT_SLOW_SECONDS: Final[float] = 1.0

#: 文本前缀式注入钩子：`client.py` 不发 query 参数，契约测试又必须走**未改动的**
#: 真实客户端，所以再加一条「文本以它开头即当 fault」的通道。
#: 同样受 `LAYA_FAULT_INJECT` 控制；长者不会说出这种句子，关掉后只剩 403。
_FAULT_TEXT_PREFIX: Final[str] = "__fault:"


async def _fault_response(kind: str) -> Response | None:
    """按 `kind` 造一个非法响应；未知 kind 返回 None（继续正常推理）。"""
    if kind == "slow":
        await asyncio.sleep(_FAULT_SLOW_SECONDS)
        return None
    if kind == "not_json":
        return PlainTextResponse("this is not json")
    if kind == "bad_status":
        return JSONResponse({"detail": "injected failure"}, status_code=503)
    payload = _FAULT_PAYLOADS.get(kind)
    if payload is None:
        return None
    return JSONResponse(payload)


# ---------------------------------------------------------------- FastAPI 装配


class ClassifyRequest(BaseModel):
    """入参契约：只有 `text`（`client.py` 固定发这一个键）。"""

    text: str = Field(min_length=1, max_length=1024)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """加载放到后台线程：uvicorn 立刻接连接，`/health` 汇报 `loading/ready/failed`。

    这样 compose 的 `--wait` 能等到真正就绪，而加载期进来的请求拿到 503
    （`client.py` 归 `LayaBadStatus`）而不是连接被拒，故障语义更准。
    """
    _configure_logging()
    settings = LayaShellSettings()
    engine = LayaEngine(settings)
    application.state.settings = settings
    application.state.engine = engine
    application.state.load_task = asyncio.create_task(asyncio.to_thread(engine.load))
    logger.info("laya shell starting", extra={"model_root": settings.model_root})
    yield
    logger.info("laya shell stopping")


app = FastAPI(title="anyu-laya", version="1.0.0", lifespan=lifespan)


def _authorize(request: Request) -> None:
    """可选 Bearer 鉴权：与 `client.py` 的 `laya_api_key` 对称；空 key = 不校验（仅回环端口）。"""
    expected = request.app.state.settings.api_key.strip()
    if not expected:
        return
    header = request.headers.get("authorization", "")
    if not secrets.compare_digest(header, f"Bearer {expected}"):
        raise HTTPException(status_code=401, detail="unauthorized")


@app.get("/health")
async def health(request: Request) -> Response:
    """就绪探针：`ready` 才算健康；`failed` 直接 503 并把错误类别暴露出来（不带正文）。"""
    settings: LayaShellSettings = request.app.state.settings
    engine: LayaEngine = request.app.state.engine
    body: dict[str, Any] = {
        "status": engine.state,
        "model_root": settings.model_root,
        "subfolder": settings.subfolder,
        "device": settings.device,
        "load_ms": round(engine.load_ms),
        "fault_inject": settings.fault_inject,
        "noul_threshold": settings.noul_threshold,
        # 类目从 questions.py 取，外部可直接核对「七类只有一份」。
        "intents": list(INTENT_CLASSES),
        "error": engine.error,
    }
    return JSONResponse(body, status_code=200 if engine.state == "ready" else 503)


@app.post("/v1/classify")
async def classify(
    request: Request,
    payload: ClassifyRequest,
    fault: str | None = None,
) -> Response:
    """一句话 → 五字段。**且仅且是**五字段，多一个都不许（契约由 client.py 校验）。"""
    _authorize(request)
    settings: LayaShellSettings = request.app.state.settings
    engine: LayaEngine = request.app.state.engine

    kind = fault
    if kind is None and payload.text.startswith(_FAULT_TEXT_PREFIX):
        kind = payload.text[len(_FAULT_TEXT_PREFIX) :]
    if kind:
        # 注入默认关闭：生产误带 ?fault= / 误发 __fault: 只会拿到 403，不会拿到假信号。
        if not settings.fault_inject:
            raise HTTPException(status_code=403, detail="fault injection disabled")
        injected = await _fault_response(kind)
        if injected is not None:
            logger.warning("fault injected", extra={"kind": kind})
            return injected

    text = payload.text[: settings.max_text_chars]
    if engine.state != "ready":
        raise HTTPException(status_code=503, detail=f"laya not ready ({engine.state})")

    started = time.perf_counter()
    try:
        signal = await asyncio.to_thread(engine.classify, text)
    except RuntimeError as exc:
        # 推理期环境错（非模型输出错）：500 让客户端归 LayaBadStatus 后降级，不伪装成功。
        logger.error("laya inference failed", extra={"error_type": type(exc).__name__})
        raise HTTPException(status_code=500, detail="laya inference failed") from exc

    latency_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "laya classified",
        extra={
            "intent": signal["intent"],
            "confidence": round(float(signal["confidence"]), 3),
            "latency_ms": round(latency_ms),
            "chars": len(text),
        },
    )
    # 延迟放进响应头：契约规定响应体只有五字段，不能为了观测往里加第六个键。
    return JSONResponse(
        signal,
        headers={
            "x-laya-latency-ms": f"{latency_ms:.0f}",
            "x-laya-intent": str(signal["intent"]),
        },
    )
