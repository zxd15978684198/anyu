"""集中配置：全部环境变量只在这里声明，禁止在业务代码里 `os.environ.get` 散读。

对齐仓库根目录 `.env.example` 的 17 组变量（技术文档未定义变量名，
本项目自定义口径见 README §6）。三条硬要求：

1. 每个字段都有默认值 —— 没有 `.env` 也能启动（迭代排期 1.4「Pydantic Settings 加载 .env」，
   而不是「没有 .env 就崩」）；
2. `extra="ignore"` —— `.env` 里多出不认识的键不报错，避免加变量时全组翻车；
3. 变量名与字段名大小写不敏感映射（`APP_NAME` -> `app_name`），保持 `.env.example`
   全大写、代码全小写的既有约定。
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any, get_args

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 仓库根：app/core/config.py -> app/core -> app -> 根
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENV_FILE = _REPO_ROOT / ".env"

# 声纹 embedding 的本地开发占位密钥（32 字节 ASCII "anyu-dev-voiceprint-key!!!" 的 b64）。
# 仓库里带着它只为「clone 下来不填 .env 也能跑通声纹链路」；生产必须经 .env 覆盖，
# 否则等于所有部署共用一把公开钥匙 —— `voiceprint_fernet_is_placeholder` 就是给这条兜底的。
_VOICEPRINT_DEV_FERNET_KEY = "c2lsdmVybWF0ZS1kZXYtdm9pY2VwcmludC1rZXkhISE="  # pragma: allowlist secret  # D12：仓库内占位值，生产环境拒用


def _is_plain_str(annotation: Any) -> bool:
    """注解是否就是 `str`（含 `str | None`）——只有它保留「空串」语义。"""
    if annotation is str:
        return True
    args = [arg for arg in get_args(annotation) if arg is not type(None)]
    return len(args) == 1 and args[0] is str


class Settings(BaseSettings):
    """全量配置。分组顺序与 `.env.example` 一一对应，便于双向核对。"""

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # 不用 `env_ignore_empty=True`：它是**全局**开关，会把所有字段的空串一起吞掉，
        # 而本项目有两类空串语义相反 ——
        #   * 数值字段（`LLM_TIMEOUT_SECONDS=`）：空占位只会解析失败，必须当作「没配」；
        #   * 字符串字段（`TURN_CREDENTIAL=`、`STUN_URL=`）：空串是**明确的业务语义**
        #     「关掉这项功能」（见 app/api/ice_config.py 与 tests/test_ice_config_api.py），
        #     吞掉它等于把「关」变成「用 .env 里的真值」，是更危险的错。
        # 所以只对非 str 字段忽略空串，规则写在下面这个 before-validator 里。
    )

    @model_validator(mode="before")
    @classmethod
    def _ignore_blank_for_non_string_fields(cls, data: Any) -> Any:
        """把非 `str` 字段的空串删掉，等价于「这个变量没配」，回落到字段默认值。

        只处理**已经是空串**的键：`None`、非法类型原样放行，让 Pydantic 自己报错——
        这里的目标是兼容 `.env` 的空占位，不是把配置错误吞成默认值。
        """
        if not isinstance(data, Mapping):
            return data
        cleaned: dict[str, Any] = {}
        for name, value in data.items():
            if (
                value == ""
                and name in cls.model_fields
                and not _is_plain_str(cls.model_fields[name].annotation)
            ):
                continue
            cleaned[name] = value
        return cleaned

    # ---------------------------- 应用 ----------------------------
    app_name: str = "anyu"
    app_env: str = "local"
    debug: bool = False
    api_host: str = "0.0.0.0"  # noqa: S104 - 容器内需监听全部网卡，对外暴露由 nginx+TLS 收口
    api_port: int = 8000
    public_base_url: str = "https://localhost"
    frontend_origin: str = "https://localhost:5173"
    log_level: str = "INFO"

    # ---- 本机演示：长者端「一键填入」用的设备凭证（生产环境一律不生效）----
    # 设备密钥在库里只有 sha256（`devices.token_hash`），明文无处可查，演示时靠这两项下发。
    # 留空 = 端点直接 404；`app_env=production` 时即使填了也 404（见 app/api/pad.py）。
    demo_device_id: str = ""
    demo_device_secret: str = ""

    # ---------------------------- 鉴权 / JWT（迭代 2 起使用） ----------------------------
    jwt_secret: str = ""
    # 签名算法不是配置项：钉死在 `app/core/security.py::_JWT_ALGORITHM`（底稿 §8-④）。
    access_token_expire_minutes: int = 120
    refresh_token_expire_days: int = 7
    elder_session_expire_hours: int = 24

    # ---------------------------- 身份级联：重连复用时效（迭代 4 · 2026-11-10 裁决 A） ----------------------------
    # 断线重连时复用库里 open thread 的**有效期**。超过它即视为「上一次会话早已结束」：
    # 结束时旧 thread、交回正常级联重新判定（有声纹走声纹，无音频出「是我」确认）。
    # 为什么需要：`find_open_thread` 原本只看 `ended_at IS NULL`，而全仓 `end_open_thread`
    # 只有换人/重绑一处调用 —— 于是第一次确认过的身份会**永久**回放成「已确认」，
    # 长者一登录就显示「身份 · 已确认」，实际这轮还没验过是谁。
    identity_reuse_window_seconds: int = 1800

    # ---------------------------- 3D 数字人资产（迭代 4 · A3） ----------------------------
    # 三层取值链的第二层：personas.avatar_url 为空时才用这里的默认值。
    # 库里与这里都存「相对路径」（如 /avatars/xxx.vrm）；响应层统一拼 public_base_url 成绝对 URL。
    default_avatar_url: str = ""
    default_fallback_avatar_url: str = ""  # 静态头像 PNG：3D 加载失败或超 5s 时前端使用（AC-15）
    avatar_rotation_y_offset: float = 0.0  # 仅 VRM0 需要 π 补偿；VRM1 保持 0.0（见计划 S-02）
    avatar_cache_version_salt: str = ""  # 想让前端强制失效缓存时改这个值，version 指纹会随之变化

    # ---------------------------- 数据库（业务表） ----------------------------
    database_url: str = "postgresql+asyncpg://anyu:anyu@localhost:5432/anyu"  # pragma: allowlist secret  # 本机 compose 占位凭据
    database_pool_size: int = 10
    database_pool_max_overflow: int = 20

    # ---------------------------- LangGraph Checkpoint（独立 schema + 独立连接池） ----------------------------
    langgraph_checkpoint_database_url: str = (
        "postgresql://anyu:anyu@localhost:5432/anyu"  # pragma: allowlist secret
    )
    langgraph_checkpoint_schema: str = "langgraph"
    langgraph_checkpoint_pool_min_size: int = 1
    langgraph_checkpoint_pool_max_size: int = 5

    # ---------------------------- 缓存 / 限流 ----------------------------
    redis_url: str = "redis://localhost:6379/0"
    redis_key_prefix: str = "anyu"
    rate_limit_per_minute: int = 60
    # ---------------------------- Celery（离线批处理，迭代 10 计划 §4 批 0） ----------------------------
    # 总开关：默认关。worker/beat 全在独立进程里，关掉时 backend 照旧只跑 APScheduler。
    celery_enabled: bool = False
    # broker 单独配一个 db（默认 /1），**不复用上面 redis_url 的 /0**：业务短期记忆也在 /0，
    # 混用会让 `FLUSHDB`、keyspace 告警、maxmemory 淘汰互相干扰。刻意不配 result_backend
    # （这批任务没有返回值语义，开了只会在 Redis 里堆 celery-task-meta-*）。
    celery_broker_url: str = "redis://127.0.0.1:6379/1"
    # ---- 请求层限流（攻防计划 §4.6 · S3，实现见 app/core/rate_limit.py） ----
    # 总开关。关掉 = 中间件全程透传（挂载点保留，不重建应用）。
    rate_limit_enabled: bool = True
    # test/local 默认放宽：门禁里成百上千个用例共用一个 TestClient IP，一开就集体 429。
    # 要在本地/单测里验证限流本身，显式置 True。
    rate_limit_enforce_in_dev: bool = False
    # 反代场景才打开：`X-Forwarded-For` 客户端可任意填写，直连时采信它等于送无限额度。
    rate_limit_trust_forwarded_for: bool = False
    # 打开上面的开关后，仅当直连对端 IP 在白名单里才采信 XFF；留空 = 采信所有直连对端。
    rate_limit_trusted_proxies: list[str] = Field(default_factory=list)

    # ---------------------------- System Two · 大模型 ----------------------------
    llm_provider: str = "deepseek"
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_timeout_seconds: int = 20
    llm_max_retries: int = 2
    llm_temperature: float = 0.6
    # 少数 OpenAI 兼容网关要求自有请求头（如 opencode 的 `x-opencode-session`）。
    # 用 JSON 放进配置，避免为每家供应商在客户端里加分支；默认空 = 行为不变。
    llm_extra_headers: dict[str, str] = Field(default_factory=dict)

    # ---------------------------- System One · Laya ----------------------------
    laya_enabled: bool = True
    laya_base_url: str = "http://localhost:7001"
    laya_api_key: str = ""
    laya_timeout_ms: int = 200
    laya_confidence_threshold: float = 0.85
    laya_fallback_to_llm: bool = True

    # ---------------------------- Embedding · 长期记忆向量 ----------------------------
    # `base_url` / `model` / `api_key` 三项**全空**时 `build_embedding_provider` 降级到
    # `DeterministicEmbeddingProvider`：链路照样跑通，但向量是 SHA-256 派生的噪声，
    # 只能证明「同文本 → 同向量」，**不能**作为召回质量证据（S-D7-1）。只填一部分时
    # 同样降级并记 warning —— 半配的供应商比没配更容易被误当成「已接通」。
    embedding_base_url: str = ""
    embedding_model: str = ""
    embedding_api_key: str = ""
    embedding_timeout_ms: int = 5000
    #: 向量维度。**必须与三处一致**：`models.{memory,knowledge}.EMBEDDING_DIM`、
    #: DDL 里的 `vector(N)`（alembic 0009/0011/0013）、供应商真实输出。
    #: 2026-11-11 定维 1024（bge-m3 dense 口径），不做运行时维度标签（见
    #: `设计记录（embedding维度对齐）`「为什么不搞弹性」）。
    #: 换模型（维度不同）= 改这里 + `models` 两处常量 + 一版迁移重建列 + 旧向量全部重算。
    embedding_dimension: int = 1024

    # ---------------------------- 语音识别 ASR ----------------------------
    asr_provider: str = ""
    asr_base_url: str = ""
    asr_app_id: str = ""
    asr_api_key: str = ""
    asr_language: str = "zh-CN"
    asr_timeout_ms: int = 300
    asr_emit_confidence: bool = True
    # FunASR runtime 镜像默认开 TLS 且内置自签证书（容器日志 "SSL is opened!"）。
    # 置 true 才跳过证书校验；公网/正式环境应换真证书并把这里关掉。
    asr_insecure: bool = False

    # ---------------------------- 语音合成 TTS ----------------------------
    tts_provider: str = ""
    tts_base_url: str = ""
    tts_app_id: str = ""
    tts_api_key: str = ""
    tts_voice_id: str = ""
    tts_speed: float = 1.0
    tts_timeout_ms: int = 300
    # 宿主机 TTS 桥（`scripts/tts_bridge.py`）。**为什么需要**：2026-11-11 实测容器内
    # TCP 能连上 `speech.platform.bing.com:443`，但 TLS 握手读 0 字节即 EOF（对
    # `www.google.com` 同样），而宿主机直连正常 —— 容器网段绕过了宿主机的 VPN/TUN，
    # 整个容器出网 TLS 不可用，edge-tts 在容器里 100% `TTSUnavailable`。
    # 填了 URL 且 `TTS_PROVIDER=edge` 时，取音顺序变成 bridge → edge（→ espeak，
    # 仅 `ALLOW_ROBOT_VOICE=true` 时）。本机是 `http://host.docker.internal:7811`；
    # 公网部署请留空（那时容器能直连）。
    tts_bridge_url: str = ""
    # 是否允许把 espeak-ng 的机器人声送到长者耳朵。默认 **false**（P4 口径：
    # 「宁可没声也不放机器音」）；断网时由上层落「大字幕 + speechSynthesis」。
    # 仅断网调试/CI 才打开。
    allow_robot_voice: bool = False
    # 固定话术音频包（`scripts/bake_voice_pack.py` 生成）。目录相对进程工作目录，
    # 命中 manifest 时零合成、零网络；缺少 manifest 时自动跳过这一层。
    voice_pack_enabled: bool = True
    voice_pack_dir: str = "static/voices"

    # ---------------------------- 语音三档首包超时（迭代 5 §4.2） ----------------------------
    # 阈值来自技术设计文档 line 1042：「ASR >2s、LLM 首 token >3s、TTS 首包 >1s 三档超时
    # 逐个演示降级」。PRD 未定义这三个值，故这里是**实现映射**而不是自裁（计划 §1 B3）。
    # 别和上面/下面的连接级超时混用：asr_timeout_ms / tts_timeout_ms 是**单次云调用**的
    # 建连预算，llm_timeout_seconds 是**整轮** LLM 预算；本节三档只管「第一包多久算超时」，
    # 由 app/core/timeouts.py::with_timeout 消费，超时一律降级不抛异常。
    asr_first_packet_timeout_ms: int = 2000
    llm_first_token_timeout_ms: int = 3000
    tts_first_packet_timeout_ms: int = 1000

    # ---------------------------- WebRTC / TURN ----------------------------
    stun_url: str = "stun:localhost:3478"
    turn_url: str = "turn:localhost:3478"
    turn_username: str = "anyu"
    turn_credential: str = ""
    turn_credential_ttl_seconds: int = 3600
    # TURN/STUN 对外端口 = coturn 的 `--listening-port`。STUN 与 TURN **共用一个监听端口**，
    # 所以下发时两种 URL 都取这一个值（不另立 stun_port，避免两份配置漂移）。
    # W2 的 `GET /api/audio/ice-config` 用「请求 Host 头 + 本端口」现拼 URL：
    # 本机访问 localhost 得到 turn:localhost:<turn_port>，服务器上得到 turn:example.com:<turn_port>，
    # 同一份 .env 两处都对，不需要按环境改 URL。
    turn_port: int = 3478
    # TURNS/DTLS 对外端口 = coturn 的 `--tls-listening-port`（2026-11-09 起启用）。
    # 与 turn_port 成对下发：浏览器同时拿到 turn:(udp) 与 turns:(tcp)，证书受信时走 TLS，
    # 不受信时自动回落到明文 TURN —— 见 MediaConfig.ice_servers_for_client。
    # 置 0 或负数表示「不下发 turns:」，此时前端只能用 3478。
    turn_tls_port: int = 5349
    webrtc_ice_timeout_seconds: int = 5
    # ICE 看门狗的宽限窗口（秒）。2026-11-10 修正：实测 aiortc 的候选对 `SUCCEEDED`
    # 往往早于 `pc.connectionState=connected` 1~4s，按 5s 一刀切会把「马上要通」的
    # 会话判死——开场白正是在这个窗口里被丢掉的（`identity_greeting_audio
    # played=false track_ready=false`）。到点后若 ICE 侧已有进展，再等这么久才判死。
    webrtc_ice_grace_seconds: int = 10
    webrtc_audio_bitrate_kbps: int = 32
    webrtc_vad_aggressiveness: int = 2

    # ---------------------------- 天气（唯一真实外部接口） ----------------------------
    weather_api_provider: str = ""
    weather_api_base_url: str = ""
    weather_api_key: str = ""
    weather_cache_ttl_seconds: int = 600
    weather_default_city: str = ""

    # ---------------------------- 通知 ----------------------------
    notify_channel: str = "inapp"
    webpush_vapid_public_key: str = ""
    webpush_vapid_private_key: str = ""
    webpush_subject: str = "mailto:admin@example.com"
    notify_retry_interval_seconds: int = 30
    notify_max_retries: int = 3

    # ---------------------------- MCP 客户端 ----------------------------
    mcp_weather_server_url: str = "http://localhost:7101/mcp"
    mcp_operations_server_url: str = "http://localhost:7102"
    mcp_call_timeout_seconds: int = 5
    mcp_enable_audit: bool = True

    # ---------------------------- 观测 ----------------------------
    langfuse_enabled: bool = False
    langfuse_host: str = "https://cloud.langfuse.com"
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_sample_rate: float = 1.0
    otel_enabled: bool = True
    # OTLP **/HTTP** 端点（SDK 侧用 http/protobuf，自动追加 /v1/traces|/v1/metrics）。
    # 本地默认 4318；云上用 Grafana Cloud 的 <otlp-gateway>/otlp。填 4317（gRPC 端口）
    # 只对 grpc exporter 有意义，HTTP 导出器会失败降级。
    otel_exporter_otlp_endpoint: str = "http://localhost:4318"
    otel_exporter_otlp_protocol: str = "http/protobuf"
    # `Authorization=Basic <base64(instance_id:token)>`。Grafana Cloud 的 Python 场景要把
    # "Basic " 写成 "Basic%20"（官方 tile 原文如此）；SDK 会原样放进请求头。
    otel_exporter_otlp_headers: str = ""
    otel_service_name: str = "anyu-backend"
    # trace 采样率（ParentBased + TraceIdRatio）；与 langfuse_sample_rate 分开，不复用
    otel_sample_rate: float = 1.0
    metrics_enabled: bool = True

    # ---- 机构知识输出层闸门（迭代 10 第四轮 W3，FR-KNW-01 / FR-KNW-05 / FR-KNW-03） ----
    # 此前「必须点明来源与生效日期」「查不到不许编」只写在 companion system prompt 里，
    # 输出层零校验（tests/test_knowledge_e2e.py 那条用例名自认是 prompt_rule_only）。
    # 打开后在答案出口再校一次：缺来源→降级、空检索→固定话术、冲突→按仲裁改写。
    # 关掉则行为与加这层之前完全一致（app/services/knowledge_output_guard.py 里判 disabled）。
    knowledge_output_guard_enabled: bool = True

    # ---------------------------- 数据保留 / 调度 ----------------------------
    audio_retention_hours: int = 72
    audio_debug_save: bool = False
    scheduler_timezone: str = "Asia/Shanghai"
    health_checkup_start: str = "19:00"
    health_checkup_end: str = "20:00"
    daily_report_generate_hour: str = "07:00"  # 每天 7 点为其一天生成家属日报（§4.8）
    # APScheduler 单进程前提：`uvicorn --reload` 会跑两个进程，调度器双触发 =
    # 用药重复外呼（迭代 7 教训）。装配处（app/main.py lifespan）发现 RELOAD
    # 环境变量为真时即使本值为 True 也不启动（迭代 8 计划裁决 S-D8-6）。
    scheduler_enabled: bool = True

    # ---------------------------- 业务阈值（改动需评审，见 AGENTS.md §7） ----------------------------
    max_tool_calls_per_turn: int = 8
    tool_repeat_window_seconds: int = 60
    tool_repeat_break_threshold: int = 3
    medication_reminder_attempts: int = 4
    medication_attempt_wait_seconds: int = 60
    # 声纹判定阈值：**0.55**（CAM++ 原生口径）。2026-11-09 产品负责人裁决由 0.85 下调——
    # 迭代 4 spike（spikes/voiceprint/README.md）用 4 条真实录音实测：同人 cosine 仅
    # 0.5679、异人 max −0.0146，0.85 在 CAM++ 上把本人判成「不是本人」，等同上线即恒落
    # 主动确认兜底。PRD v2.8 / AGENTS §7 与本行同一次提交改齐。
    voiceprint_similarity_threshold: float = 0.55

    # ---- 声纹推理容器（迭代 4 · A1） ----
    # 声纹免登录开关（2026-11-13 产品负责人裁决：取消「说一句话就进去」）。
    # 默认 False：登录页不再出现入口，端点也回 403。保留代码与开关是为了可回滚，
    # 不是留一条暗路 —— 要彻底删掉时，连同 `app/services/voiceprint_login_service.py`
    # 与 `tests/test_voiceprint_login.py` 一起删。
    voiceprint_login_enabled: bool = False

    # voiceprint_service 调这个容器的 POST /extract 端点，主服务不装 torch（容器隔离重依赖）
    voiceprint_base_url: str = "http://localhost:7003"
    voiceprint_extract_timeout_seconds: float = 5.0
    voiceprint_embedding_dimension: int = 192  # CAM++ 固定 192 维（spikes/voiceprint/ 验证）
    # Fernet 密钥：embedding 加密落库用。必须 url-safe base64 编码的 32 字节。
    # 生产经 .env 注入，.env.example 只放占位符（AGENTS §4.2 红线）。
    # 下方为本地开发唯一默认值（32 字节 ASCII "anyu-dev-voiceprint-key!!!" 的 b64），
    # 仅供本地跑通；换密钥会导致已落库 embedding 全部无法解密（dev 库需重录）。
    voiceprint_fernet_key: str = _VOICEPRINT_DEV_FERNET_KEY

    @property
    def voiceprint_fernet_is_placeholder(self) -> bool:
        """当前密钥是否仍是仓库里的本地开发占位值。

        `app_env=production` 时必须显式注入真密钥：拿占位值上线等于所有声纹 embedding
        用同一把人人都有的钥匙加密（等于明文），故此属性供调用方做 fail-closed 判定。
        """
        return self.voiceprint_fernet_key.strip() == _VOICEPRINT_DEV_FERNET_KEY

    @property
    def cors_origins(self) -> list[str]:
        """`FRONTEND_ORIGIN` 支持逗号分隔多来源；空则不开 CORS 通配。"""
        return [item.strip() for item in self.frontend_origin.split(",") if item.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in {"prod", "production"}

    @property
    def voiceprint_fernet_ready(self) -> bool:
        """Fernet 密钥是否可用（32 字节 url-safe base64）。

        只做长度与解码校验，不实例化 Fernet（避免每次调用建对象）。
        启动时校验一次：不合法就别让声纹链路「看起来能跑、实际永远降级」。
        """
        import base64
        import binascii

        key = self.voiceprint_fernet_key.strip()
        if not key:
            return False
        try:
            return len(base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))) == 32
        except (binascii.Error, ValueError):
            return False


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例。测试可用 `get_settings.cache_clear()` 重置。"""
    return Settings()
