"""Per-character voice shaping for the Qwen3-TTS realtime model.

The active provider exposes three numeric controls in ``session.update``:
``speech_rate`` (0.5-2.0), ``volume`` (0-100) and ``pitch_rate`` (0.5-2.0).
``pitch_rate`` is implemented as resampling, so large values also change the
speaking duration; the allowed range here is deliberately narrower than the
provider limit to keep that side effect predictable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.core.exceptions import APIError

SPEECH_RATE_PRESETS: dict[str, float] = {
    "slow": 0.88,
    "calm": 0.95,
    "normal": 1.0,
    "brisk": 1.08,
    "rapid": 1.18,
}

VOLUME_PRESETS: dict[str, int] = {
    "soft": 35,
    "normal": 50,
    "full": 65,
}

PITCH_PRESETS: dict[str, float] = {
    "low": 0.88,
    "slightly_low": 0.95,
    "natural": 1.0,
    "slightly_high": 1.05,
    "high": 1.12,
}

SPEECH_RATE_MIN = 0.75
SPEECH_RATE_MAX = 1.35
PITCH_MIN = 0.85
PITCH_MAX = 1.15
VOLUME_MIN = 20
VOLUME_MAX = 80

DEFAULT_SPEECH_RATE = 1.0
DEFAULT_VOLUME = 50
DEFAULT_PITCH = 1.0


@dataclass(frozen=True)
class VoiceProfile:
    """Normalized voice shaping values for one character."""

    base_voice_id: str | None
    speech_rate: float = DEFAULT_SPEECH_RATE
    volume: int = DEFAULT_VOLUME
    pitch_rate: float = DEFAULT_PITCH

    def session_params(self) -> dict[str, float | int]:
        return {
            "speech_rate": self.speech_rate,
            "volume": self.volume,
            "pitch_rate": self.pitch_rate,
        }

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "speech_rate": self.speech_rate,
            "volume": self.volume,
            "pitch_rate": self.pitch_rate,
        }
        if self.base_voice_id:
            payload["base_voice_id"] = self.base_voice_id
        return payload


DEFAULT_VOICE_PROFILE = VoiceProfile(base_voice_id=None)


def _invalid(message: str) -> APIError:
    return APIError(status_code=422, code="validation_failed", message=message)


def _coerce_float(raw: Any, *, field: str, minimum: float, maximum: float) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise _invalid(f"validation failed: {field} must be a number")
    value = round(float(raw), 3)
    if value < minimum or value > maximum:
        raise _invalid(
            f"validation failed: {field} out of range; "
            f"value={value}; min={minimum}; max={maximum}"
        )
    return value


def _coerce_int(raw: Any, *, field: str, minimum: int, maximum: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise _invalid(f"validation failed: {field} must be a number")
    value = int(round(float(raw)))
    if value < minimum or value > maximum:
        raise _invalid(
            f"validation failed: {field} out of range; "
            f"value={value}; min={minimum}; max={maximum}"
        )
    return value


def normalize_voice_profile(raw: dict[str, Any] | None) -> VoiceProfile:
    """Validate a stored/requested voice profile and fill defaults.

    Unknown keys are ignored so older character rows stay readable after the
    schema evolves.
    """
    if not raw:
        return DEFAULT_VOICE_PROFILE

    speech_rate = raw.get("speech_rate", DEFAULT_SPEECH_RATE)
    volume = raw.get("volume", DEFAULT_VOLUME)
    pitch_rate = raw.get("pitch_rate", DEFAULT_PITCH)

    base_voice_id = raw.get("base_voice_id")
    if base_voice_id is not None and not isinstance(base_voice_id, str):
        raise _invalid("validation failed: base_voice_id must be a string")

    return VoiceProfile(
        base_voice_id=(base_voice_id or "").strip() or None,
        speech_rate=_coerce_float(
            speech_rate,
            field="speech_rate",
            minimum=SPEECH_RATE_MIN,
            maximum=SPEECH_RATE_MAX,
        ),
        volume=_coerce_int(
            volume,
            field="volume",
            minimum=VOLUME_MIN,
            maximum=VOLUME_MAX,
        ),
        pitch_rate=_coerce_float(
            pitch_rate,
            field="pitch_rate",
            minimum=PITCH_MIN,
            maximum=PITCH_MAX,
        ),
    )


def voice_profile_or_default(raw: dict[str, Any] | None) -> VoiceProfile:
    """Best-effort read for runtime paths that must not fail on bad legacy data."""
    try:
        return normalize_voice_profile(raw)
    except APIError:
        return DEFAULT_VOICE_PROFILE


__all__ = [
    "DEFAULT_VOICE_PROFILE",
    "PITCH_PRESETS",
    "SPEECH_RATE_PRESETS",
    "VOLUME_PRESETS",
    "VoiceProfile",
    "normalize_voice_profile",
    "voice_profile_or_default",
]
