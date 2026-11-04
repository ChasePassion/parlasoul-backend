"""Centralized raw TTS provider environment settings."""

from __future__ import annotations

from functools import cached_property

from pydantic_settings import BaseSettings, SettingsConfigDict

from src.core.config import _resolve_env_file, settings


class TTSSettings(BaseSettings):
    """Raw env-backed TTS provider settings.

    Provider routing decisions live in `tts_registry.py`; this module only
    normalizes provider-local connection and model settings.
    """

    MINIMAX_TTS_API_KEY: str | None = None
    MINIMAX_TTS_WEBSOCKET_API_URL: str = "wss://api.minimaxi.com/ws/v1/t2a_v2"
    MINIMAX_TTS_MODEL_ID: str = "speech-2.8-turbo"
    MINIMAX_TTS_SAMPLE_RATE: int = 24000
    MINIMAX_TTS_BITRATE: int = 128000
    MINIMAX_TTS_CHANNEL: int = 1
    MINIMAX_TTS_DEFAULT_SPEED: float = 1.0
    MINIMAX_TTS_DEFAULT_VOLUME: float = 1.0
    MINIMAX_TTS_DEFAULT_PITCH: int = 0
    MINIMAX_TTS_EVENT_TIMEOUT_SECONDS: float = 30.0
    MINIMAX_TTS_OPEN_TIMEOUT_SECONDS: float = 15.0
    MINIMAX_TTS_WARM_CONNECTION_ENABLED: bool = True
    MINIMAX_TTS_WARM_CONNECTION_TTL_SECONDS: float = 105.0
    MINIMAX_TTS_WARM_CONNECTION_WAIT_SECONDS: float = 3.0
    MINIMAX_TTS_WARM_CONNECTION_KEEPALIVE_SECONDS: float = 900.0
    MINIMAX_TTS_WARM_CONNECTION_REFRESH_MARGIN_SECONDS: float = 20.0
    MINIMAX_TTS_WARM_HEALTH_CHECK_INTERVAL_SECONDS: float = 15.0

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    @staticmethod
    def _normalize_optional(value: str | None) -> str | None:
        candidate = (value or "").strip()
        return candidate or None

    @cached_property
    def minimax_api_key(self) -> str | None:
        return self._normalize_optional(self.MINIMAX_TTS_API_KEY)

    @cached_property
    def minimax_websocket_url(self) -> str:
        return self.MINIMAX_TTS_WEBSOCKET_API_URL.strip()

    @cached_property
    def minimax_model_id(self) -> str:
        return self.MINIMAX_TTS_MODEL_ID.strip() or "speech-2.8-turbo"

    @cached_property
    def dashscope_api_key(self) -> str | None:
        return self._normalize_optional(settings.DASHSCOPE_API_KEY)

    @cached_property
    def dashscope_websocket_url(self) -> str:
        return settings.DASHSCOPE_TTS_WEBSOCKET_API_URL.strip()

    @cached_property
    def dashscope_model_id(self) -> str:
        return settings.DASHSCOPE_TTS_MODEL.strip()


tts_settings = TTSSettings(_env_file=_resolve_env_file())


__all__ = ["TTSSettings", "tts_settings"]
