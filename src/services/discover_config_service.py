"""Discover config service backed by a JSON file."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from src.core.config import Settings
from src.schemas.discover import DiscoverConfigResponse, DiscoverHeroItem
from src.services.media_url_service import (
    build_media_url,
    validate_discover_hero_object_key,
)

logger = logging.getLogger(__name__)


class DiscoverConfigService:
    """Loads lightweight discover config outside the primary database."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def get_public_config(self) -> DiscoverConfigResponse:
        payload = self._load_raw_config()
        hero_items = self._normalize_hero_items(payload.get("hero_items"))
        hero_character_ids = (
            [item.character_id for item in hero_items]
            if hero_items
            else self._normalize_hero_ids(payload.get("hero_character_ids"))
        )
        story_character_ids = self._normalize_hero_ids(
            payload.get("story_character_ids")
        )
        professional_character_ids = self._normalize_hero_ids(
            payload.get("professional_character_ids")
        )
        return DiscoverConfigResponse(
            hero_character_ids=hero_character_ids,
            hero_items=hero_items,
            story_character_ids=story_character_ids,
            professional_character_ids=professional_character_ids,
        )

    def _resolve_config_path(self) -> Path:
        path = Path(self._settings.DISCOVER_CONFIG_PATH)
        if path.is_absolute():
            return path
        return Path(__file__).resolve().parents[2] / path

    def _load_raw_config(self) -> dict[str, Any]:
        path = self._resolve_config_path()
        if not path.exists():
            logger.warning("Discover config file not found at %s; using defaults.", path)
            return {}

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            logger.error("Discover config JSON parse failed at %s: %s", path, exc)
            return {}
        except OSError as exc:
            logger.error("Discover config read failed at %s: %s", path, exc)
            return {}

        if not isinstance(raw, dict):
            logger.error("Discover config at %s must be a JSON object; using defaults.", path)
            return {}

        return raw

    @staticmethod
    def _normalize_hero_ids(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []

        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str):
                continue
            current = item.strip()
            if not current or current in seen:
                continue
            seen.add(current)
            normalized.append(current)
        return normalized

    @staticmethod
    def _normalize_hero_items(value: Any) -> list[DiscoverHeroItem]:
        if not isinstance(value, list):
            return []

        normalized: list[DiscoverHeroItem] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, dict):
                continue
            character_id = item.get("character_id")
            image_key = item.get("image_key")
            if not isinstance(character_id, str) or not isinstance(image_key, str):
                continue

            current_character_id = character_id.strip()
            current_image_key = validate_discover_hero_object_key(image_key)
            if not current_character_id or not current_image_key:
                continue
            if current_character_id in seen:
                continue

            cta_text = item.get("cta_text")
            current_cta_text = cta_text.strip() if isinstance(cta_text, str) else "开始对话"
            if not current_cta_text:
                current_cta_text = "开始对话"

            seen.add(current_character_id)
            normalized.append(
                DiscoverHeroItem(
                    character_id=current_character_id,
                    image_key=current_image_key,
                    image_url=build_media_url(current_image_key),
                    cta_text=current_cta_text[:24],
                )
            )
        return normalized
