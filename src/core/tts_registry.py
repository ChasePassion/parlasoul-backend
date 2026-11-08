"""Centralized TTS provider registry and route binding."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.core.exceptions import APIError
from src.core.tts_settings import TTSSettings, tts_settings
from src.core.tts_voice_catalog import (
    DEFAULT_DASHSCOPE_SYSTEM_VOICE_ID,
    DEFAULT_DASHSCOPE_TTS_MODEL,
    DEFAULT_TTS_SYSTEM_PROVIDER,
    dashscope_full_voice_catalog,
    dashscope_legacy_voice_catalog,
    minimax_system_voice_catalog,
)

TTSProviderName = Literal["dashscope", "minimax"]
TTSAudioFormat = Literal["opus", "mp3", "pcm", "flac", "wav"]
TTSVoiceSource = Literal["system", "clone"]

DEFAULT_SYSTEM_TTS_PROVIDER = DEFAULT_TTS_SYSTEM_PROVIDER
DEFAULT_SYSTEM_TTS_MODEL = DEFAULT_DASHSCOPE_TTS_MODEL
DEFAULT_SYSTEM_TTS_VOICE_ID = DEFAULT_DASHSCOPE_SYSTEM_VOICE_ID


@dataclass(frozen=True)
class TTSAudioProfile:
    format: TTSAudioFormat
    mime_type: str
    sample_rate: int | None = None
    channels: int | None = None
    bitrate: int | None = None
    streamable: bool = True


@dataclass(frozen=True)
class TTSProviderProfile:
    provider: TTSProviderName
    api_key: str | None
    websocket_url: str
    default_model_id: str
    model_aliases: dict[str, str]
    selectable_models: tuple[str, ...]
    supported_audio: dict[str, TTSAudioProfile]
    default_message_format: str
    default_realtime_format: str
    enabled: bool

    def model_for_alias(self, alias: str | None) -> str | None:
        alias_key = (alias or "default").strip().lower()
        return self.model_aliases.get(alias_key)


@dataclass(frozen=True)
class ResolvedTTSRoute:
    provider: TTSProviderName
    model: str
    api_key: str | None
    websocket_url: str
    source_type: TTSVoiceSource
    model_type_label: str


@dataclass(frozen=True)
class TTSVoiceBinding:
    provider: TTSProviderName
    model: str
    provider_voice_id: str
    source_type: TTSVoiceSource


@dataclass(frozen=True)
class TTSRouter:
    system_provider: TTSProviderName = "dashscope"
    clone_provider: TTSProviderName = "dashscope"


class TTSRegistry:
    """Single source of truth for TTS provider routing and system voices."""

    def __init__(self, settings: TTSSettings):
        self._settings = settings
        self._router = TTSRouter()

    @property
    def router(self) -> TTSRouter:
        return self._router

    def _dashscope_profile(self) -> TTSProviderProfile:
        model_id = self._settings.dashscope_model_id
        return TTSProviderProfile(
            provider="dashscope",
            api_key=self._settings.dashscope_api_key,
            websocket_url=self._settings.dashscope_websocket_url,
            default_model_id=model_id,
            model_aliases={
                "default": model_id,
                "system": model_id,
                "clone": model_id,
            },
            selectable_models=(model_id,),
            supported_audio={
                "opus": TTSAudioProfile(format="opus", mime_type="audio/opus"),
                "mp3": TTSAudioProfile(format="mp3", mime_type="audio/mpeg"),
                "pcm": TTSAudioProfile(
                    format="pcm", mime_type="audio/pcm;rate=24000", sample_rate=24000, channels=1
                ),
            },
            default_message_format="mp3",
            default_realtime_format="pcm",
            enabled=bool(self._settings.dashscope_api_key and model_id),
        )

    def _minimax_profile(self) -> TTSProviderProfile:
        model_id = self._settings.minimax_model_id
        sample_rate = int(self._settings.MINIMAX_TTS_SAMPLE_RATE or 24000)
        bitrate = int(self._settings.MINIMAX_TTS_BITRATE or 128000)
        channels = int(self._settings.MINIMAX_TTS_CHANNEL or 1)
        return TTSProviderProfile(
            provider="minimax",
            api_key=self._settings.minimax_api_key,
            websocket_url=self._settings.minimax_websocket_url,
            default_model_id=model_id,
            model_aliases={
                "default": model_id,
                "system": model_id,
            },
            selectable_models=(model_id, "speech-2.8-turbo", "speech-2.8-hd"),
            supported_audio={
                "mp3": TTSAudioProfile(
                    format="mp3",
                    mime_type="audio/mpeg",
                    sample_rate=sample_rate,
                    channels=channels,
                    bitrate=bitrate,
                ),
                "pcm": TTSAudioProfile(
                    format="pcm",
                    mime_type=f"audio/pcm;rate={sample_rate}",
                    sample_rate=sample_rate,
                    channels=channels,
                ),
                "flac": TTSAudioProfile(
                    format="flac",
                    mime_type="audio/flac",
                    sample_rate=sample_rate,
                    channels=channels,
                ),
                "wav": TTSAudioProfile(
                    format="wav",
                    mime_type="audio/wav",
                    sample_rate=sample_rate,
                    channels=channels,
                    streamable=False,
                ),
            },
            default_message_format="mp3",
            default_realtime_format="pcm",
            enabled=bool(self._settings.minimax_api_key and model_id),
        )

    def _profiles(self) -> dict[str, TTSProviderProfile]:
        return {
            "dashscope": self._dashscope_profile(),
            "minimax": self._minimax_profile(),
        }

    @staticmethod
    def _split_provider_ref(provider_ref: str) -> tuple[str, str | None]:
        normalized_ref = (provider_ref or "").strip().lower()
        if ":" not in normalized_ref:
            return normalized_ref, None
        provider_name, model_alias = normalized_ref.split(":", 1)
        return provider_name.strip(), model_alias.strip() or None

    def get_profile(self, provider: str) -> TTSProviderProfile | None:
        provider_name, _ = self._split_provider_ref(provider)
        return self._profiles().get(provider_name)

    def default_system_voice_binding(self) -> TTSVoiceBinding:
        profile = self.get_profile(self.router.system_provider)
        model = profile.default_model_id if profile else DEFAULT_SYSTEM_TTS_MODEL
        return TTSVoiceBinding(
            provider=self.router.system_provider,
            model=model,
            provider_voice_id=DEFAULT_SYSTEM_TTS_VOICE_ID,
            source_type="system",
        )

    def resolve_explicit_route(
        self,
        *,
        provider: str,
        model: str | None,
        source_type: str,
    ) -> ResolvedTTSRoute:
        provider_name = (provider or "").strip().lower()
        profile = self.get_profile(provider_name)
        if not profile:
            raise APIError(
                status_code=400,
                code="voice_provider_unsupported",
                message=f"invalid request: unsupported provider; provider={provider}",
            )

        normalized_model = (model or "").strip() or profile.default_model_id
        if profile.provider == "dashscope":
            if not self._dashscope_model_supported(normalized_model):
                raise APIError(
                    status_code=400,
                    code="voice_model_unsupported",
                    message=f"invalid request: unsupported tts model; provider=dashscope; model={normalized_model}",
                )
        elif normalized_model not in profile.selectable_models:
            raise APIError(
                status_code=400,
                code="voice_model_unsupported",
                message=f"invalid request: unsupported tts model; provider={provider_name}; model={normalized_model}",
            )

        normalized_source_type = (source_type or "").strip().lower()
        if normalized_source_type not in {"system", "clone"}:
            raise APIError(
                status_code=422,
                code="validation_failed",
                message=f"validation failed: unsupported voice_source_type; voice_source_type={source_type}",
            )

        return ResolvedTTSRoute(
            provider=profile.provider,
            model=normalized_model,
            api_key=profile.api_key,
            websocket_url=profile.websocket_url,
            source_type=normalized_source_type,  # type: ignore[arg-type]
            model_type_label=f"{profile.provider}:{normalized_model}",
        )

    def resolve_system_route(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> ResolvedTTSRoute:
        provider_name = (provider or self.router.system_provider).strip().lower()
        return self.resolve_explicit_route(
            provider=provider_name,
            model=model,
            source_type="system",
        )

    def resolve_audio_profile(
        self,
        *,
        provider: str,
        model: str | None,
        audio_format: str,
        stream_required: bool,
    ) -> TTSAudioProfile:
        profile = self.get_profile(provider)
        if not profile:
            raise APIError(
                status_code=400,
                code="voice_provider_unsupported",
                message=f"invalid request: unsupported provider; provider={provider}",
            )
        normalized_format = (audio_format or "").strip().lower()
        audio = profile.supported_audio.get(normalized_format)
        if audio is None:
            raise APIError(
                status_code=422,
                code="validation_failed",
                message=(
                    "validation failed: audio_format is not supported by provider; "
                    f"provider={profile.provider}; model={model or profile.default_model_id}; "
                    f"audio_format={audio_format}"
                ),
            )
        if stream_required and not audio.streamable:
            raise APIError(
                status_code=422,
                code="validation_failed",
                message=(
                    "validation failed: audio_format is not streamable for provider; "
                    f"provider={profile.provider}; audio_format={audio_format}"
                ),
            )
        return audio

    def list_system_voices(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
        catalog: str = "legacy",
    ) -> list[dict[str, str | None]]:
        route = self.resolve_system_route(provider=provider, model=model)
        if route.provider == "minimax":
            return [
                item.to_dict(provider_model=route.model)
                for item in minimax_system_voice_catalog(provider_model=route.model)
            ]
        if route.provider == "dashscope":
            catalog_items = (
                dashscope_full_voice_catalog(provider_model=route.model)
                if catalog == "full"
                else dashscope_legacy_voice_catalog(provider_model=route.model)
            )
            return [item.to_dict(provider_model=route.model) for item in catalog_items]
        raise APIError(
            status_code=400,
            code="voice_provider_unsupported",
            message=f"invalid request: unsupported provider; provider={route.provider}",
        )

    @staticmethod
    def _dashscope_model_supported(model: str) -> bool:
        normalized = model.strip().lower()
        return normalized.startswith(
            ("qwen3-tts", "qwen-tts", "cosyvoice-v3-plus", "cosyvoice-v3-flash")
        )


tts_registry = TTSRegistry(tts_settings)


__all__ = [
    "DEFAULT_SYSTEM_TTS_MODEL",
    "DEFAULT_SYSTEM_TTS_PROVIDER",
    "DEFAULT_SYSTEM_TTS_VOICE_ID",
    "ResolvedTTSRoute",
    "TTSAudioProfile",
    "TTSProviderProfile",
    "TTSRegistry",
    "TTSRouter",
    "TTSVoiceBinding",
    "tts_registry",
]
