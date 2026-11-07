"""LLM preset tier constants for character model routing."""

from __future__ import annotations

LLM_PRESET_FREE = "free"
LLM_PRESET_FLAGSHIP = "flagship"
LLM_PRESET_IDS: frozenset[str] = frozenset({LLM_PRESET_FREE, LLM_PRESET_FLAGSHIP})
PAID_LLM_PRESET_IDS: frozenset[str] = frozenset({LLM_PRESET_FLAGSHIP})
