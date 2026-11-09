"""Centralized application settings."""

from __future__ import annotations

import os
from functools import cached_property
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _resolve_env_file(environment: str | None = None) -> str:
    """Resolve the dotenv file name from ENVIRONMENT.

    Development keeps loading `.env` so local workflows stay unchanged.
    Production uses `.env.production`, which matches the deployment files that
    already exist in this repo.
    """

    env_name = (environment or os.getenv("ENVIRONMENT", "")).strip().lower()
    if not env_name or env_name in {"development", "dev", "local"}:
        return ".env"
    if env_name == "production":
        return ".env.production"
    return f".env.{env_name}"


def _normalize_asyncpg_database_url(url: str) -> str:
    """Translate libpq-style SSL query params to asyncpg-compatible ones."""

    normalized = url.strip()
    if not normalized.startswith("postgresql+asyncpg://"):
        return normalized

    parts = urlsplit(normalized)
    query_pairs: list[tuple[str, str]] = []
    ssl_value: str | None = None

    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key == "sslmode":
            ssl_value = value
            continue
        if key == "channel_binding":
            continue
        query_pairs.append((key, value))

    if ssl_value is not None and not any(key == "ssl" for key, _ in query_pairs):
        query_pairs.append(("ssl", ssl_value))

    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(query_pairs),
            parts.fragment,
        )
    )


def _normalize_milvus_url(url: str) -> str:
    """Normalize Milvus/Zilliz URLs for pymilvus.

    pymilvus treats a bare `https://host` as `host:19530`, which is wrong for
    Zilliz Cloud Serverless endpoints that expect TLS on port 443. Preserve
    explicit ports and non-HTTP schemes; only add `:443` for http/https URLs
    that omit the port.
    """

    normalized = url.strip()
    if not normalized:
        return normalized

    parts = urlsplit(normalized)
    if parts.scheme not in {"http", "https"} or parts.port is not None:
        return normalized

    if not parts.hostname:
        return normalized

    netloc = parts.hostname
    if parts.username:
        auth = parts.username
        if parts.password:
            auth = f"{auth}:{parts.password}"
        netloc = f"{auth}@{netloc}"
    netloc = f"{netloc}:443"

    return urlunsplit(
        (
            parts.scheme,
            netloc,
            parts.path,
            parts.query,
            parts.fragment,
        )
    )


def _split_csv(value: str | None) -> list[str]:
    if value is None:
        return []

    items: list[str] = []
    seen: set[str] = set()
    for chunk in value.split(","):
        normalized = chunk.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        items.append(normalized)
    return items


class Settings(BaseSettings):
    # Application
    API_V1_STR: str = "/v1"
    PROJECT_NAME: str = "ParlaSoul"
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    API_RELOAD: bool = True
    # Kept explicit (not only inside _resolve_env_file) so downstream layers —
    # e.g. the evaluation CLI — never mistake an unknown environment for
    # development. Reads ENVIRONMENT from the process env via pydantic-settings.
    ENVIRONMENT: str = ""

    # Database
    DATABASE_URL: str
    DB_SCHEMA_COMPAT_CHECK_ENABLED: bool = True

    # Security
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 days
    BETTER_AUTH_JWKS_URL: str = "http://localhost:3001/api/auth/jwks"
    BETTER_AUTH_JWT_ISSUER: str = "http://localhost:3001"
    BETTER_AUTH_JWT_AUDIENCE: str = "http://localhost:3001"

    # Email / SMTP
    SMTP_HOST: str
    SMTP_PORT: int = 587
    SMTP_USER: str
    SMTP_PASSWORD: str
    EMAILS_FROM_EMAIL: str | None = None

    # CORS
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:4000,http://localhost:8000"

    UPLOAD_MAX_FILE_SIZE_BYTES: int = 5 * 1024 * 1024
    STT_MAX_FILE_SIZE_BYTES: int = 20 * 1024 * 1024
    DISCOVER_CONFIG_PATH: str = "config/discover.json"

    # Media storage and image variants
    REDIS_URL: str | None = None
    R2_ACCESS_KEY_ID: str | None = None
    R2_SECRET_ACCESS_KEY: str | None = None
    R2_REGION: str = "auto"
    R2_BUCKET_NAME: str = Field(
        default="parlasoul-media-public",
        validation_alias=AliasChoices("R2_BUCKET_NAME", "R2_BUCKET"),
    )
    R2_ENDPOINT_URL: str | None = Field(
        default=None,
        validation_alias=AliasChoices("R2_ENDPOINT_URL", "R2_ENDPOINT"),
    )
    R2_PUBLIC_BASE_URL: str | None = None
    MEDIA_PUBLIC_BASE_URL: str = "/media"
    MEDIA_ALLOWED_MIME: str = "image/jpeg,image/png,image/webp,image/avif"
    MEDIA_IMAGE_VARIANTS: str = "96,192,512"
    MEDIA_IMAGE_FORMAT: str = "avif"
    MEDIA_IMAGE_QUALITY: int = 55
    MEDIA_MAX_UPLOAD_BYTES: int = 5 * 1024 * 1024
    MEDIA_IMAGE_MAX_PIXELS: int = 16_777_216
    MEDIA_UPLOAD_SESSION_TTL_SECONDS: int = 900
    MEDIA_CACHE_MAX_BYTES: int = 256 * 1024
    MEDIA_CACHE_TTL_SECONDS: int = 7 * 24 * 60 * 60
    MEDIA_CACHE_NEGATIVE_TTL_SECONDS: int = 60
    MEDIA_PRESIGNED_URL_TTL_SECONDS: int = 900

    # Local staging: uploads are served from the host immediately and mirrored
    # to R2 in the background (R2 has no mainland China edge).
    LOCAL_MEDIA_ROOT: str = "data/local-media"
    LOCAL_MEDIA_SYNC_ENABLED: bool = True
    LOCAL_MEDIA_SYNC_WORKER_COUNT: int = 2
    LOCAL_MEDIA_SYNC_SCAN_SECONDS: int = 60
    LOCAL_MEDIA_RETENTION_HOURS: float = 24.0

    # Chat audio (voice-clone source recordings)
    CHAT_AUDIO_MAX_UPLOAD_BYTES: int = 20 * 1024 * 1024
    CHAT_AUDIO_ALLOWED_FORMATS: str = "wav,mp3,m4a,flac,opus"
    VOICE_CLONE_MAX_PROMPT_SECONDS: float = 10.0
    FFMPEG_BINARY: str = "ffmpeg"
    FFPROBE_BINARY: str = "ffprobe"

    # User settings
    USER_SETTINGS_MESSAGE_FONT_SIZE_MIN: int = 14
    USER_SETTINGS_MESSAGE_FONT_SIZE_MAX: int = 24
    USER_SETTINGS_MESSAGE_FONT_SIZE_DEFAULT: int = 16
    USER_SETTINGS_DISPLAY_MODE_DEFAULT: str = "concise"
    USER_SETTINGS_REPLY_CARD_ENABLED_DEFAULT: bool = True
    USER_SETTINGS_MIXED_INPUT_AUTO_TRANSLATE_ENABLED_DEFAULT: bool = True
    USER_SETTINGS_AUTO_READ_ALOUD_ENABLED_DEFAULT: bool = True
    USER_SETTINGS_PREFERRED_EXPRESSION_BIAS_ENABLED_DEFAULT: bool = True
    USER_SETTINGS_PROACTIVE_ENABLED_DEFAULT: bool = False
    USER_SETTINGS_TIMEZONE_DEFAULT: str = "Asia/Shanghai"

    # Dodo Payments
    DODO_PAYMENTS_API_KEY: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DODO_PAYMENTS_API_KEY"),
    )
    DODO_PAYMENTS_WEBHOOK_SECRET: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DODO_PAYMENTS_WEBHOOK_SECRET"),
    )
    DODO_PAYMENTS_SUBSCRIPTION_WEBHOOK_SECRET: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "DODO_PAYMENTS_SUBSCRIPTION_WEBHOOK_SECRET",
            "DODO_PAYMENTS_WEBHOOK_SECRET",
        ),
    )
    DODO_PAYMENTS_PAYMENT_WEBHOOK_SECRET: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DODO_PAYMENTS_PAYMENT_WEBHOOK_SECRET"),
    )
    DODO_PAYMENTS_ENVIRONMENT: str = Field(
        default="test_mode",
        validation_alias=AliasChoices("DODO_PAYMENTS_ENVIRONMENT"),
    )
    DODO_PAYMENTS_WEB_APP_BASE_URL: str = Field(
        default="http://localhost:3001",
        validation_alias=AliasChoices("DODO_PAYMENTS_WEB_APP_BASE_URL"),
    )
    BILLING_PAYWALL_ENABLED: bool = Field(
        default=False,
        validation_alias=AliasChoices("BILLING_PAYWALL_ENABLED"),
    )
    BILLING_CHECKOUT_ENABLED: bool = Field(
        default=False,
        validation_alias=AliasChoices("BILLING_CHECKOUT_ENABLED"),
    )
    DODO_SUBSCRIPTION_PLUS_PRODUCT_IDS: str = Field(
        default="",
        validation_alias=AliasChoices("DODO_SUBSCRIPTION_PLUS_PRODUCT_IDS"),
    )
    DODO_SUBSCRIPTION_PLUS_PRODUCT_ID: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DODO_SUBSCRIPTION_PLUS_PRODUCT_ID"),
    )
    DODO_SUBSCRIPTION_PRO_PRODUCT_IDS: str = Field(
        default="",
        validation_alias=AliasChoices("DODO_SUBSCRIPTION_PRO_PRODUCT_IDS"),
    )
    DODO_SUBSCRIPTION_PRO_PRODUCT_ID: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DODO_SUBSCRIPTION_PRO_PRODUCT_ID"),
    )
    DODO_WECHAT_PLUS_30D_PRODUCT_ID: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DODO_WECHAT_PLUS_30D_PRODUCT_ID"),
    )
    DODO_WECHAT_PRO_30D_PRODUCT_ID: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DODO_WECHAT_PRO_30D_PRODUCT_ID"),
    )
    DODO_WECHAT_PLUS_365D_PRODUCT_ID: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DODO_WECHAT_PLUS_365D_PRODUCT_ID"),
    )
    DODO_WECHAT_PRO_365D_PRODUCT_ID: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DODO_WECHAT_PRO_365D_PRODUCT_ID"),
    )

    # Jobs
    ENABLE_SEMANTIC_CONSOLIDATION_JOB: bool = True
    SEMANTIC_JOB_TIMEZONE: str = "Asia/Shanghai"
    SEMANTIC_JOB_HOUR: int = 3
    SEMANTIC_JOB_MINUTE: int = 0
    SEMANTIC_JOB_PAIR_TIMEOUT_SECONDS: float = 300.0
    ENABLE_PROACTIVE_MESSAGE_JOB: bool = False
    PROACTIVE_JOB_SCAN_INTERVAL_SECONDS: float = 60.0
    PROACTIVE_JOB_IDLE_SLEEP_SECONDS: float = 5.0
    PROACTIVE_JOB_WORKER_COUNT: int = 3
    PROACTIVE_JOB_DISPATCH_LEASE_SECONDS: int = 180
    PROACTIVE_JOB_MAX_RETRIES: int = 3
    PROACTIVE_JOB_RETRY_BASE_DELAY_SECONDS: float = 60.0
    PROACTIVE_JOB_MIN_MEMORY_COUNT: int = 3
    PROACTIVE_JOB_ACTIVITY_CAP: int = 50
    PROACTIVE_JOB_COOLDOWN_CAP_DAYS: int = 7
    PROACTIVE_JOB_ACTIVITY_WINDOW_DAYS: int = 7
    PROACTIVE_JOB_MAX_EPISODIC_MEMORIES: int = 50
    PROACTIVE_JOB_SLOT_HOURS: str = "10,15,21"

    # Memory system
    MILVUS_URL: str
    MILVUS_TOKEN: str | None = None
    MEMORY_COLLECTION_NAME: str = "memories"
    MEMORY_GROUP_COLLECTION_NAME: str = "groups"
    EMBEDDING_API_KEY: str = Field(
        validation_alias=AliasChoices("EMBEDDING_API_KEY", "SILICONFLOW_API_KEY"),
    )
    EMBEDDING_BASE_URL: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        validation_alias=AliasChoices("EMBEDDING_BASE_URL", "SILICONFLOW_BASE_URL"),
    )
    EMBEDDING_MODEL: str = Field(
        default="text-embedding-v4",
        validation_alias=AliasChoices("EMBEDDING_MODEL", "SILICONFLOW_EMBEDDING_MODEL"),
    )
    MEMORY_EMBEDDING_DIM: int = 2048
    LLM_TIMEOUT_SECONDS: float = Field(
        default=60.0,
        validation_alias=AliasChoices("LLM_TIMEOUT_SECONDS"),
    )
    LLM_MAX_RETRIES: int = Field(
        default=3,
        validation_alias=AliasChoices("LLM_MAX_RETRIES"),
    )
    LLM_RETRY_BASE_DELAY_SECONDS: float = Field(
        default=1.0,
        validation_alias=AliasChoices("LLM_RETRY_BASE_DELAY_SECONDS"),
    )
    CHAT_TITLE_DEFAULT: str = Field(
        default="New Chat",
        validation_alias=AliasChoices("CHAT_TITLE_DEFAULT"),
    )
    CHAT_TITLE_MAX_LENGTH: int = Field(
        default=120,
        validation_alias=AliasChoices("CHAT_TITLE_MAX_LENGTH"),
    )
    APP_LOG_DIR: str = Field(default="logs", validation_alias=AliasChoices("APP_LOG_DIR"))
    APP_LOG_LEVEL: str = Field(default="INFO", validation_alias=AliasChoices("APP_LOG_LEVEL"))
    APP_LOG_BACKUP_COUNT: int = Field(
        default=7,
        validation_alias=AliasChoices("APP_LOG_BACKUP_COUNT"),
    )
    APP_LOG_QUEUE_ENABLED: bool = Field(
        default=True,
        validation_alias=AliasChoices("APP_LOG_QUEUE_ENABLED"),
    )
    LLM_LOG_ENABLED: bool = Field(default=True, validation_alias=AliasChoices("LLM_LOG_ENABLED"))
    LLM_LOG_PROMPT_PREVIEW_ENABLED: bool = Field(
        default=False,
        validation_alias=AliasChoices("LLM_LOG_PROMPT_PREVIEW_ENABLED"),
    )
    LLM_LOG_PROMPT_PREVIEW_CHARS: int = Field(
        default=120,
        validation_alias=AliasChoices("LLM_LOG_PROMPT_PREVIEW_CHARS"),
    )

    # DashScope realtime speech
    DASHSCOPE_API_KEY: str | None = None
    DASHSCOPE_BASE_WEBSOCKET_API_URL: str = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
    DASHSCOPE_STT_REALTIME_WEBSOCKET_API_URL: str = (
        "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
    )
    DASHSCOPE_TTS_WEBSOCKET_API_URL: str = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
    DASHSCOPE_TTS_CUSTOMIZATION_API_URL: str = (
        "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization"
    )
    DASHSCOPE_STT_MODEL: str = "qwen3-asr-flash-realtime"
    DASHSCOPE_TTS_MODEL: str = "qwen3-tts-flash-realtime"
    DASHSCOPE_BILLING_REGION: str | None = None
    DASHSCOPE_VOICE_ENROLLMENT_MODEL: str = "voice-enrollment"
    DASHSCOPE_VOICE_CLONE_TARGET_MODEL: str = "cosyvoice-v3-flash"
    DASHSCOPE_HTTP_TIMEOUT_SECONDS: float = 30.0
    DASHSCOPE_STT_EVENT_TIMEOUT_SECONDS: float = 60.0
    # One-shot STT (the chat composer microphone button) uses the synchronous
    # Qwen-Audio HTTP API. Realtime voice calls keep using DASHSCOPE_STT_MODEL
    # over the realtime websocket, so the two transports are configured apart.
    DASHSCOPE_STT_HTTP_MODEL: str = "qwen-audio-3.1-asr-flash"
    DASHSCOPE_STT_HTTP_API_URL: str = (
        "https://dashscope.aliyuncs.com/api/v1/services/aigc/"
        "multimodal-generation/generation"
    )
    DASHSCOPE_STT_HTTP_TIMEOUT_SECONDS: float = 20.0
    # Declaring the languages the user may mix improves recognition accuracy.
    # Comma-separated, at most 4 values; empty leaves language detection to the model.
    DASHSCOPE_STT_LANGUAGE_HINTS: str = "zh,en"
    DASHSCOPE_STT_CONTEXT_MAX_TURNS: int = 3
    DASHSCOPE_TIMBRE_LIST_DOC_URL: str = (
        "https://help.aliyun.com/zh/model-studio/multimodal-timbre-list"
    )
    DASHSCOPE_TIMBRE_LIST_CACHE_SECONDS: int = 600
    DASHSCOPE_TTS_INSTRUCTIONS_DEFAULT: str = ""
    # Spoken language of every character voice. The product ships English audio,
    # so the value is sent explicitly instead of relying on the provider default (Auto).
    DASHSCOPE_TTS_LANGUAGE_TYPE: str = "English"

    # Character forge (conversational character creation)
    DEEPSEEK_SEARCH_API_KEY: str | None = None
    DEEPSEEK_SEARCH_API_BASE: str = "https://api.deepseek.com"
    DEEPSEEK_SEARCH_TIMEOUT_SECONDS: float = 20.0
    DEEPSEEK_SEARCH_PROXY: str | None = None
    CHARACTER_FORGE_MAX_STEPS: int = 5
    CHARACTER_FORGE_MAX_SEARCH_CALLS: int = 4
    CHARACTER_FORGE_MAX_OUTPUT_TOKENS: int = 32768
    CHARACTER_FORGE_HISTORY_LIMIT: int = 60
    CHARACTER_FORGE_RATE_LIMIT_REQUESTS: int = 30
    CHARACTER_FORGE_RATE_LIMIT_WINDOW_SECONDS: int = 600
    CHARACTER_FORGE_MAX_AVATAR_CALLS: int = 2

    # Qwen-Image avatar generation（百炼同步接口）
    DASHSCOPE_IMAGE_API_URL: str = (
        "https://dashscope.aliyuncs.com/api/v1/services/aigc/"
        "multimodal-generation/generation"
    )
    DASHSCOPE_IMAGE_MODEL: str = "qwen-image-3.0"
    DASHSCOPE_IMAGE_SIZE: str = "2048*2048"
    DASHSCOPE_IMAGE_TIMEOUT_SECONDS: float = 300.0
    REALTIME_VAD_MODEL_PATH: str = "models/silero_vad.onnx"
    REALTIME_VAD_SAMPLE_RATE: int = 16000
    REALTIME_VAD_THRESHOLD: float = 0.5
    REALTIME_VAD_END_THRESHOLD_DELTA: float = 0.15
    # Endpointing: silence that ends a turn. 500ms keeps natural mid-sentence
    # pauses intact while cutting ~100ms of dead air versus the old 600ms.
    REALTIME_VAD_MIN_SILENCE_MS: int = 500
    REALTIME_VAD_SPEECH_PAD_MS: int = 30
    REALTIME_VAD_PRE_ROLL_MS: int = 300
    # Progressive ASR: stream audio while the user speaks so only the tail is
    # decoded after end-of-turn.
    REALTIME_STT_STREAMING_ENABLED: bool = True
    # Upstream realtime ASR sessions go stale during long replies; replace a
    # pre-warmed stream older than this before the next utterance.
    REALTIME_STT_STREAM_MAX_AGE_SECONDS: float = 20.0
    # First-response prebuffer only needs to hide the first TTS chunk gap; 80ms
    # shaves 120ms off TTFA. Recovery prebuffer stays smaller for the same reason.
    REALTIME_AUDIO_PACER_PREBUFFER_MS: int = 80
    REALTIME_AUDIO_PACER_RECOVERY_PREBUFFER_MS: int = 40
    REALTIME_AUDIO_PACER_FADEOUT_MS: int = 50
    REALTIME_AUDIO_PACER_MAX_BUFFER_MS: int = 1200
    REALTIME_AUDIO_DEBUG_CAPTURE_ENABLED: bool = False
    REALTIME_AUDIO_DEBUG_CAPTURE_DIR: str = "tmp/realtime-audio-debug"
    REALTIME_AUDIO_DEBUG_CAPTURE_MAX_BYTES_PER_FILE: int = 50_000_000
    REALTIME_UTTERANCE_COMMIT_GRACE_MS: int = 120
    REALTIME_UTTERANCE_MIN_AUDIO_MS: int = 300
    REALTIME_UTTERANCE_MIN_VOICED_MS: int = 200
    REALTIME_INTERRUPT_HARD_CONFIRM_MS: int = 300
    REALTIME_INTERRUPT_RESUME_GRACE_MS: int = 250
    # A short acknowledgement while the bot speaks ("mm-hmm", "对") must not
    # cancel the reply. Below this much voiced audio the turn is treated as a
    # backchannel; above it the interruption is confirmed.
    REALTIME_BACKCHANNEL_MAX_VOICED_MS: int = 900
    REALTIME_BACKCHANNEL_MAX_WAIT_MS: int = 500
    REALTIME_TTS_FIRST_SEGMENT_TIMEOUT_MS: int = 300
    REALTIME_TTS_LATER_SEGMENT_TIMEOUT_MS: int = 700
    REALTIME_TTS_FIRST_SEGMENT_TARGET_CHARS: int = 28
    REALTIME_TTS_LATER_SEGMENT_MIN_CHARS: int = 40
    REALTIME_TTS_LATER_SEGMENT_MAX_CHARS: int = 160
    REALTIME_ICE_PUBLIC_URLS: str = ""
    REALTIME_ICE_BACKEND_URLS: str = ""
    REALTIME_TURN_SECRET: str | None = None
    REALTIME_TURN_REALM: str = "parlasoul.com"
    REALTIME_TURN_USERNAME_PREFIX: str = "parlasoul"
    REALTIME_TURN_CREDENTIAL_TTL_SECONDS: int = 3600
    REALTIME_NEGOTIATION_TIMEOUT_SECONDS: float = 5.0
    K_SEMANTIC: int = 5
    K_EPISODIC: int = 5
    USE_ALL_SEMANTIC: bool = True
    LANGFUSE_BASE_URL: str | None = None
    LANGFUSE_HOST: str | None = None
    LANGFUSE_SECRET_KEY: str | None = None
    LANGFUSE_PUBLIC_KEY: str | None = None
    LANGFUSE_ENABLED: bool = False
    NARRATIVE_SIMILARITY_THRESHOLD: float = 0.8
    MEMORY_MAX_WORKERS: int = 4
    CHAT_HISTORY_LIMIT: int = 20
    MEMORY_INIT_MAX_RETRIES: int = 3
    MEMORY_INIT_RETRY_BASE_SECONDS: float = 1.0
    MEMORY_WARMUP_TIMEOUT_SECONDS: float = 15.0

    # Evaluation harness (phase18). CLI-only allowlist; never consumed by the
    # FastAPI lifespan. Capture flags default off: evaluation calls must not
    # write raw prompts/completions into observability.
    EVAL_DATASET_PATH: str | None = None
    EVAL_RESULTS_DIR: str = "data/eval/results"
    EVAL_REPORT_ALLOWLIST_DIRS: str = "data/eval/results,tmp/eval"
    EVAL_JUDGE_PROVIDER: str | None = None
    EVAL_JUDGE_MODEL: str | None = None
    EVAL_RUBRIC_VERSION: str = "parlasoul-roleplay-5d-ooc-v1"
    EVAL_PROMPT_TEMPLATE_VERSION: str = "parlasoul-judge-prompt-v1"
    EVAL_CONCURRENCY: int = 4
    EVAL_GENERATION_TIMEOUT_SECONDS: float = 120.0
    EVAL_JUDGE_TIMEOUT_SECONDS: float = 90.0
    EVAL_CONTENT_MODE: str = "none"
    EVAL_RETENTION_DAYS: int = 90
    EVAL_LANGFUSE_ENABLED: bool = False
    EVAL_CAPTURE_INPUT: bool = True
    EVAL_CAPTURE_OUTPUT: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def _normalize_database_url(cls, value: str) -> str:
        return _normalize_asyncpg_database_url(value)

    @field_validator("MILVUS_URL", mode="before")
    @classmethod
    def _normalize_milvus_connection_url(cls, value: str) -> str:
        return _normalize_milvus_url(value)

    @cached_property
    def environment_name(self) -> str:
        return (self.ENVIRONMENT or os.getenv("ENVIRONMENT", "")).strip().lower()

    @cached_property
    def is_production(self) -> bool:
        return self.environment_name == "production"

    @cached_property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    @cached_property
    def media_allowed_mime_list(self) -> list[str]:
        return _split_csv(self.MEDIA_ALLOWED_MIME)

    @cached_property
    def chat_audio_allowed_formats(self) -> list[str]:
        return _split_csv(self.CHAT_AUDIO_ALLOWED_FORMATS)

    @cached_property
    def media_image_variants_list(self) -> list[int]:
        values: list[int] = []
        for item in _split_csv(self.MEDIA_IMAGE_VARIANTS):
            try:
                size = int(item)
            except ValueError:
                continue
            if size > 0 and size not in values:
                values.append(size)
        return values or [96, 192, 512]

    @cached_property
    def media_public_base_url_normalized(self) -> str:
        return self.MEDIA_PUBLIC_BASE_URL.rstrip("/") or "/media"

    @cached_property
    def upload_max_file_size_mb(self) -> int:
        return max(1, self.UPLOAD_MAX_FILE_SIZE_BYTES // (1024 * 1024))

    @cached_property
    def database_connect_args(self) -> dict[str, dict[str, str]]:
        if self.DATABASE_URL.startswith("postgresql+asyncpg://"):
            return {"server_settings": {"search_path": "public"}}
        return {}

    @cached_property
    def dodo_subscription_plus_product_ids(self) -> list[str]:
        return _split_csv(
            ",".join(
                item
                for item in (
                    self.DODO_SUBSCRIPTION_PLUS_PRODUCT_IDS,
                    self.DODO_SUBSCRIPTION_PLUS_PRODUCT_ID,
                )
                if item
            )
        )

    @cached_property
    def dodo_subscription_pro_product_ids(self) -> list[str]:
        return _split_csv(
            ",".join(
                item
                for item in (
                    self.DODO_SUBSCRIPTION_PRO_PRODUCT_IDS,
                    self.DODO_SUBSCRIPTION_PRO_PRODUCT_ID,
                )
                if item
            )
        )

    @cached_property
    def realtime_ice_public_urls(self) -> list[str]:
        return _split_csv(self.REALTIME_ICE_PUBLIC_URLS)

    @cached_property
    def realtime_ice_backend_urls(self) -> list[str]:
        return _split_csv(self.REALTIME_ICE_BACKEND_URLS)


settings = Settings(_env_file=_resolve_env_file())
