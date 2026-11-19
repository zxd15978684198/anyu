"""`POST /api/dialogue/text`：打字输入 = 话音的同一条对话链（N-Text 验收）。

钉住的契约（2026-11-11，前端 `TextFallback` 等这个端点）：

1. 设备令牌鉴权；`elder_id` 只从服务端会话取——请求体里塞 `elder_id` 必须无效。
2. `text` strip 后 1–200 字；空串/纯空白/超长都是 422，不静默截断。
3. 正常一轮把这句话送进 `DialogueService`（LLM 替身被调用一次，prompt 含原文），
   响应形状 `{text, reply{text, audio_url: null}, degraded}`。
4. 急症句「我胸口疼」走**真路由层**（`wire_route`，L0 命中不触 Laya）：
   **零模型调用**，回复就是 `SAFETY_REPLIES["safety_emergency"]`，并且
   `risk_events` 真落了一行 `safety_alert`（带脱敏 evidence）+ 关联红单——
   这就是「先于一切」的可观测证据，不是只看返回文本像不像。
5. LLM 抛错 / 路由抛错时仍 200（`degraded=true` 或回落到模型），不吐 5xx。
6. 日志不含输入原文，带 device_id——隐私与可追查性各占一项。

替身边界：只换 `DialogueService` 的 LLM（`FakeLLM`）与（需要时）路由函数；
DB 是真实事务（`conftest.db_conn` 同款，teardown 回滚），告警服务是真的。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.api import dialogue as dialogue_api
from app.core.config import get_settings
from app.core.security import sha256_hex, sign_device_session
from app.db import get_session
from app.models import Device, Elder, Room
from app.models.work_order import RiskEvent, WorkOrder
from app.router.decision import RouteDecision
from app.router.rule_replies import SAFETY_REPLIES
from app.services import text_turn_service
from app.services.dialogue_service import DialogueService
from app.services.llm_service import LLMReply
from conftest import require_db_connection

# 随机 UUID：共享测试库上不与他人固定 ID 抢主键；断言只按这些引用。
ROOM_ID = uuid.uuid4()
ELDER_ID = uuid.uuid4()
OTHER_ELDER_ID = uuid.uuid4()
DEVICE_ID = uuid.uuid4()
UNBOUND_DEVICE_ID = uuid.uuid4()

DEVICE_SECRET = "dialogue-text-device-secret-a"  # pragma: allowlist secret
UNBOUND_DEVICE_SECRET = "dialogue-text-device-secret-b"  # pragma: allowlist secret

LLM_TEXT = "我在听，您慢慢说。"


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db(db_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """连不上库按 conftest 的统一策略处置（默认 fail，`ANYU_ALLOW_DB_SKIP=1` 才 skip）。"""
    conn = await require_db_connection(db_engine)
    trans = await conn.begin()
    try:
        yield conn
    finally:
        await trans.rollback()
        await conn.close()


@pytest.fixture
async def seeded(db: AsyncConnection) -> None:
    """一间房、一位长者、两台设备：一台绑定长者，一台未绑定（generic 身份路径）。

    用 Core 的 `execute(table.insert())` 而不是 ORM 对象：`db` 是 AsyncConnection
    （外层事务由夹具持有），ORM 的 unit-of-work 在这条连接上不保证 rooms 先于
    devices 落库，会撞 `fk_devices_room_id_rooms`。显式按依赖顺序插入，行为可预期。
    """
    await db.execute(
        Room.__table__.insert().values(room_id=ROOM_ID, room_no=f"DT-{ROOM_ID.hex[:6]}")
    )
    await db.execute(
        Elder.__table__.insert().values(elder_id=ELDER_ID, name="打字测试长者", status="active")
    )
    await db.execute(
        Device.__table__.insert(),
        [
            {
                "device_id": DEVICE_ID,
                "room_id": ROOM_ID,
                "elder_id": ELDER_ID,
                "token_hash": sha256_hex(DEVICE_SECRET),
                "status": "offline",
            },
            {
                "device_id": UNBOUND_DEVICE_ID,
                "room_id": ROOM_ID,
                "elder_id": None,
                "token_hash": sha256_hex(UNBOUND_DEVICE_SECRET),
                "status": "offline",
            },
        ],
    )


@pytest.fixture
async def application(db: AsyncConnection, seeded: None) -> AsyncIterator[Any]:
    """真实 `create_app()` 装配，只把 `get_session` 指向测试事务。"""
    from app.main import create_app

    app = create_app(get_settings())

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        session = AsyncSession(
            bind=db,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    app.dependency_overrides[get_session] = override_get_session
    yield app


class FakeLLM:
    """LLM 替身：计次 + 可控失败。调用次数是「短路是否真的零模型」的唯一证据。"""

    def __init__(self) -> None:
        self.error: Exception | None = None
        self.calls: list[dict[str, Any]] = []

    @property
    def count(self) -> int:
        return len(self.calls)

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        trace_id: str | None = None,
    ) -> LLMReply:
        self.calls.append({"messages": messages, "trace_id": trace_id})
        if self.error is not None:
            raise self.error
        return LLMReply(text=LLM_TEXT, degraded=False)


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
async def client(application: Any, fake_llm: FakeLLM) -> AsyncIterator[AsyncClient]:
    application.dependency_overrides[dialogue_api.get_dialogue_service] = lambda: DialogueService(
        llm=fake_llm
    )
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


def _headers(device_id: uuid.UUID = DEVICE_ID, secret: str = DEVICE_SECRET) -> dict[str, str]:
    token = sign_device_session(
        device_id=device_id,
        token_hash_prefix=sha256_hex(secret)[:16],
    )
    return {"Authorization": f"Bearer {token}"}


def _use_route(application: Any, route: Callable[..., Awaitable[Any]]) -> None:
    application.dependency_overrides[dialogue_api.get_route_text] = lambda: route


async def _l1_route(text: str, **kwargs: Any) -> RouteDecision:
    """最普通的 L1 决策：没命中 L0，交模型。"""
    elder = kwargs.get("elder_id")
    return RouteDecision(
        elder_id=str(elder) if elder else "",
        thread_id=str(kwargs.get("thread_id") or "device:-"),
        layer="L1",
        intent="chat",
        confidence=0.92,
        trace_id=kwargs.get("trace_id"),
    )


async def _risk_events(db: AsyncConnection) -> list[Any]:
    """显式列查询（不在 AsyncConnection 上 `select(实体)`——那会走 Core 行处理）。"""
    result = await db.execute(
        select(
            RiskEvent.event_id,
            RiskEvent.elder_id,
            RiskEvent.type,
            RiskEvent.severity,
            RiskEvent.detail,
        ).where(RiskEvent.elder_id == ELDER_ID)
    )
    return list(result.all())


async def _work_orders_for(db: AsyncConnection, event_id: uuid.UUID) -> list[Any]:
    result = await db.execute(
        select(WorkOrder.order_no, WorkOrder.type, WorkOrder.status).where(
            WorkOrder.event_id == event_id
        )
    )
    return list(result.all())


async def test_typed_turn_reaches_the_model_and_echoes_the_input(
    application: Any,
    client: AsyncClient,
    fake_llm: FakeLLM,
) -> None:
    _use_route(application, _l1_route)

    response = await client.post(
        "/api/dialogue/text",
        json={"text": "  今天有点闷  "},
        headers=_headers(),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["text"] == "今天有点闷"  # strip 后回显，前端气泡才对得上
    assert body["reply"]["text"] == LLM_TEXT
    assert body["reply"]["audio_url"] is None
    assert body["degraded"] is False
    assert fake_llm.count == 1
    prompt_blob = "".join(message["content"] for message in fake_llm.calls[0]["messages"])
    assert "今天有点闷" in prompt_blob


async def test_blank_text_is_rejected_with_422(client: AsyncClient) -> None:
    response = await client.post(
        "/api/dialogue/text",
        json={"text": "   "},
        headers=_headers(),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_overlong_text_is_rejected_not_truncated(client: AsyncClient) -> None:
    response = await client.post(
        "/api/dialogue/text",
        json={"text": "我" * 201},
        headers=_headers(),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_missing_text_field_is_rejected(client: AsyncClient) -> None:
    response = await client.post(
        "/api/dialogue/text",
        json={"utterance": "字段名写错了"},
        headers=_headers(),
    )

    assert response.status_code == 422


async def test_missing_device_token_is_401(client: AsyncClient) -> None:
    response = await client.post("/api/dialogue/text", json={"text": "你好"})

    assert response.status_code == 401
    assert response.json()["code"]


async def test_emergency_text_short_circuits_before_the_model(
    client: AsyncClient,
    fake_llm: FakeLLM,
    db: AsyncConnection,
) -> None:
    """急症句：**不覆盖** `get_route_text`，走真 `wire_route`（L0）——零模型 + 真落安全事件。"""
    response = await client.post(
        "/api/dialogue/text",
        json={"text": "我胸口疼"},
        headers=_headers(),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reply"]["text"] == SAFETY_REPLIES["safety_emergency"]
    assert body["degraded"] is False
    assert body["safety_alert"] is True, "急症落库成功但响应没回填 safety_alert"
    assert fake_llm.count == 0, "命中急症仍然调用了模型：短路链断了"

    events = await _risk_events(db)
    assert len(events) == 1, f"急症应落一条 risk_events，实际 {len(events)} 条"
    risk = events[0]
    assert risk.type == "safety_alert"
    assert risk.severity == "red"
    assert risk.detail is not None, "风险事件建出来了但 detail 是 NULL：没填命中证据"
    assert risk.detail.get("rule_id") == "safety_emergency"

    orders = await _work_orders_for(db, risk.event_id)
    assert len(orders) == 1
    assert orders[0].type == "safety_alert"


async def test_dizziness_text_alerts_instead_of_closing_the_turn(
    client: AsyncClient,
    fake_llm: FakeLLM,
    db: AsyncConnection,
) -> None:
    """「我有点头晕」必须走安全告警，而不是被当成普通主诉收口（task-43 / FR-HLT-03）。

    改前：这句落到 L2 主诉路径，当天采集已结束时只回「好，这次就问到这儿」，
    `safety_alert=false`，照护台上没有任何红/黄单。判据与正常急症同一条：
    零模型、话术取 `SAFETY_REPLIES`、`risk_events` 真落一行 `safety_alert`。
    严重度按语境判黄（「有点」是模糊修饰），但**仍然是告警**，不等于放行。
    """
    response = await client.post(
        "/api/dialogue/text",
        json={"text": "我有点头晕"},
        headers=_headers(),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reply"]["text"] == SAFETY_REPLIES["safety_emergency"]
    assert body["degraded"] is False
    assert body["safety_alert"] is True, "头晕命中安全层但响应没回填 safety_alert"
    assert fake_llm.count == 0, "命中急症仍然调用了模型：短路链断了"

    events = await _risk_events(db)
    assert len(events) == 1
    assert events[0].type == "safety_alert"
    assert events[0].severity == "yellow", "模糊修饰应判黄复核，而不是红色派人"
    assert events[0].detail is not None
    assert events[0].detail.get("rule_id") == "safety_emergency"


async def test_safety_alert_stays_false_when_persisting_fails(
    client: AsyncClient,
    fake_llm: FakeLLM,
    db: AsyncConnection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """落库失败时 `safety_alert` 必须是 False——它报的是「已通知」，不是「命中了」。

    反例正是这次复审要堵的：拿 `degraded` 或「路由说命中」推断安全状态，会在
    「模型正常 + 命中急症但落库失败」时误报给前端（界面变色、实际没人收到单子）。
    """

    async def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("alert service down")

    monkeypatch.setattr(text_turn_service, "handle_safety_alert", _boom)

    response = await client.post(
        "/api/dialogue/text",
        json={"text": "我胸口疼"},
        headers=_headers(),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    # 话仍要答出去，且是规则层的急症话术（不能因为落库失败退化成内部错误话术）
    assert body["reply"]["text"] == SAFETY_REPLIES["safety_emergency"]
    assert body["safety_alert"] is False, "落库失败却报了 safety_alert=True：界面会误变色"
    assert body["degraded"] is False, "degraded 是模型可用性，不该被落库失败带偏"
    assert fake_llm.count == 0
    assert await _risk_events(db) == [], "落库失败还是写进了 risk_events"


async def test_elder_id_in_request_body_is_ignored(
    client: AsyncClient,
    fake_llm: FakeLLM,
    db: AsyncConnection,
) -> None:
    """身份只从会话取：请求体塞别的长者 id，落库必须还是本机长者（AGENTS §4-9）。"""
    response = await client.post(
        "/api/dialogue/text",
        json={"text": "我胸口疼", "elder_id": str(OTHER_ELDER_ID)},
        headers=_headers(),
    )

    assert response.status_code == 200
    assert fake_llm.count == 0
    events = await _risk_events(db)
    assert len(events) == 1
    assert events[0].elder_id == ELDER_ID

    async with AsyncSession(bind=db, expire_on_commit=False) as session:
        other = await session.execute(
            select(RiskEvent.event_id).where(RiskEvent.elder_id == OTHER_ELDER_ID)
        )
        assert list(other.all()) == []


async def test_model_failure_still_returns_200_degraded(
    application: Any,
    client: AsyncClient,
    fake_llm: FakeLLM,
) -> None:
    _use_route(application, _l1_route)
    fake_llm.error = RuntimeError("upstream down")

    response = await client.post(
        "/api/dialogue/text",
        json={"text": "今天天气好吗"},
        headers=_headers(),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["degraded"] is True
    assert body["reply"]["text"], "降级也必须给一句人话"
    assert fake_llm.count == 1


async def test_route_failure_falls_back_to_the_model(
    application: Any,
    client: AsyncClient,
    fake_llm: FakeLLM,
) -> None:
    async def _boom(text: str, **kwargs: Any) -> RouteDecision:
        raise RuntimeError("route exploded")

    _use_route(application, _boom)

    response = await client.post(
        "/api/dialogue/text",
        json={"text": "今天天气好吗"},
        headers=_headers(),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reply"]["text"] == LLM_TEXT
    assert body["degraded"] is False
    assert fake_llm.count == 1


async def test_unbound_device_still_gets_a_reply(
    application: Any,
    client: AsyncClient,
    fake_llm: FakeLLM,
) -> None:
    """未绑长者的设备：身份是 generic，不能因为 DialogueIdentity 校验抛成 500。"""
    _use_route(application, _l1_route)

    response = await client.post(
        "/api/dialogue/text",
        json={"text": "有人吗"},
        headers=_headers(UNBOUND_DEVICE_ID, UNBOUND_DEVICE_SECRET),
    )

    assert response.status_code == 200, response.text
    assert response.json()["reply"]["text"] == LLM_TEXT
    assert fake_llm.count == 1


async def test_get_dialogue_service_prefers_the_shared_instance(
    application: Any,
    fake_llm: FakeLLM,
) -> None:
    """生产里 lifespan 已建好带 Redis 历史与 Agent 的服务；依赖必须优先复用它。

    否则打字与说话各拿一份「只有 LLM」的服务——同一台设备两套短期记忆，
    这是最容易被漏掉、也最难在联调时发现的一类退化。
    """
    shared = DialogueService(llm=fake_llm)
    application.state.dialogue_service = shared
    request = SimpleNamespace(app=application)

    resolved = dialogue_api.get_dialogue_service(request)  # type: ignore[arg-type]

    assert resolved is shared


async def test_log_records_never_contain_the_user_text(
    application: Any,
    client: AsyncClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _use_route(application, _l1_route)
    caplog.set_level(logging.INFO, logger="app.api.dialogue")
    text = "我想去院子里看看桂花开了没有"

    response = await client.post(
        "/api/dialogue/text",
        json={"text": text},
        headers=_headers(),
    )

    assert response.status_code == 200
    records = [record for record in caplog.records if record.name == "app.api.dialogue"]
    assert records, "本轮一条日志都没有，隐私与可追查性都无从谈起"
    assert all(text not in record.getMessage() for record in records)
    assert all(text not in str(getattr(record, "elder_id", "")) for record in records)
    assert any(getattr(record, "device_id", None) == str(DEVICE_ID) for record in records)


# ---------------------------------------------------------------------------
# 身份旁路结论（2026-11-11 · 打字回复补 `identity` 字段）
#
# 为什么打字链路必须是 `device` + `no_audio` 而不是去跑声纹：本端点手里**没有音频**，
# `identify_speaker()` 第一个判断就是 `if not audio_bytes: return None`。硬跑只会白等
# 推理容器超时，还会顺手开一条没人用的 conversation_threads。
# 这几条用例钉住「字段形如话音链路」+「不许因此多打一次容器」两个断言。
# ---------------------------------------------------------------------------


async def test_bound_device_reports_device_identity_without_audio(
    application: Any,
    client: AsyncClient,
    fake_llm: FakeLLM,
) -> None:
    """绑定长者的设备打字：`source=device`、`reason=no_audio`、`matched=false`。"""
    _use_route(application, _l1_route)

    response = await client.post(
        "/api/dialogue/text",
        json={"text": "今天有点闷"},
        headers=_headers(),
    )

    assert response.status_code == 200, response.text
    identity = response.json()["identity"]
    assert identity is not None, "打字回复没带 identity：前端拿不到这一轮是谁在说"
    assert identity["source"] == "device"
    assert identity["matched"] is False
    assert identity["reason"] == "no_audio", "打字无音频却报成了别的 reason，会被读成声纹失败"
    assert identity["confidence"] is None, "打字链路不可能有声纹相似度"
    assert identity["elder_id"] == str(ELDER_ID)


async def test_unbound_device_reports_generic_identity_without_elder(
    application: Any,
    client: AsyncClient,
    fake_llm: FakeLLM,
) -> None:
    """未绑长者的设备：`source=generic` 且 `elder_id` 必须是 null（AC-28）。"""
    _use_route(application, _l1_route)

    response = await client.post(
        "/api/dialogue/text",
        json={"text": "有人吗"},
        headers=_headers(UNBOUND_DEVICE_ID, UNBOUND_DEVICE_SECRET),
    )

    assert response.status_code == 200, response.text
    identity = response.json()["identity"]
    assert identity is not None
    assert identity["source"] == "generic"
    assert identity["matched"] is False
    assert identity["reason"] == "elder_unbound"
    assert identity["elder_id"] is None, "generic 身份带出了长者 id：个人数据进通用模式"


async def test_safety_turn_also_carries_identity(
    client: AsyncClient,
    fake_llm: FakeLLM,
) -> None:
    """急症短路那一轮同样要带 identity——字段不能只挂在「进了模型」的分支上。"""
    response = await client.post(
        "/api/dialogue/text",
        json={"text": "我胸口疼"},
        headers=_headers(),
    )

    assert response.status_code == 200, response.text
    identity = response.json()["identity"]
    assert identity is not None
    assert identity["source"] == "device"
    assert identity["elder_id"] == str(ELDER_ID)


async def test_identity_failure_never_breaks_the_reply(
    application: Any,
    client: AsyncClient,
    fake_llm: FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """身份视图构造抛错时：话照常回，`identity=null`，只记一条 warning。

    反例：把身份做成硬依赖，等于让「一行提示」有权把长者已经看到的那句话变成 500。
    """
    _use_route(application, _l1_route)
    caplog.set_level(logging.WARNING, logger="app.api.dialogue")

    def _boom(**kwargs: Any) -> Any:
        raise RuntimeError("identity view exploded")

    monkeypatch.setattr(dialogue_api, "DialogueIdentity", _boom)

    response = await client.post(
        "/api/dialogue/text",
        json={"text": "今天有点闷"},
        headers=_headers(),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reply"]["text"] == LLM_TEXT
    assert body["identity"] is None, "身份构造失败时该给 null，不是编一个人出来"
    assert any(record.msg == "dialogue_identity_skipped" for record in caplog.records)


async def test_identity_never_consults_the_voiceprint_container(
    application: Any,
    client: AsyncClient,
    fake_llm: FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """打字链路**不许**碰声纹：`identify_speaker` 与容器比对都不得被调用。

    这条是「打字 = 无音频」的实现级证据：一旦有人把话音链路那套搬进来，
    每次打字都会多等一次容器（本机实测 3s+），而这在响应时间上是不会被察觉的退化。
    """
    _use_route(application, _l1_route)
    calls: list[str] = []

    async def _spy_identify(*args: Any, **kwargs: Any) -> Any:
        calls.append("identify_speaker")
        return None

    async def _spy_verify(*args: Any, **kwargs: Any) -> Any:
        calls.append("verify_session_identity")
        return None

    monkeypatch.setattr(dialogue_api, "identify_speaker", _spy_identify, raising=False)
    monkeypatch.setattr(
        text_turn_service.identity_service,
        "verify_session_identity",
        _spy_verify,
    )

    response = await client.post(
        "/api/dialogue/text",
        json={"text": "今天有点闷"},
        headers=_headers(),
    )

    assert response.status_code == 200, response.text
    assert calls == [], f"打字链路跑了声纹：{calls}"
