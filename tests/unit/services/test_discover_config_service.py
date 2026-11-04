from __future__ import annotations

import json
from pathlib import Path

from src.core.config import settings
from src.services.discover_config_service import DiscoverConfigService


def test_discover_config_service_reads_and_normalizes_hero_items(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "discover.json"
    config_path.write_text(
        json.dumps(
            {
                "hero_items": [
                    {
                        "character_id": " 11111111-1111-1111-1111-111111111111 ",
                        "image_key": " /images/discover/heroes/elon-21-9.jpg ",
                        "cta_text": " 开始对话 ",
                    },
                    {
                        "character_id": "22222222-2222-2222-2222-222222222222",
                        "image_key": "images/discover/heroes/gork-21-9.jpg",
                    },
                    {
                        "character_id": "11111111-1111-1111-1111-111111111111",
                        "image_key": "images/discover/heroes/duplicate.jpg",
                    },
                    {
                        "character_id": "33333333-3333-3333-3333-333333333333",
                        "image_key": "private/object.jpg",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "DISCOVER_CONFIG_PATH", str(config_path), raising=False)
    monkeypatch.setattr(settings, "R2_PUBLIC_BASE_URL", None, raising=False)

    service = DiscoverConfigService(settings)

    result = service.get_public_config()

    assert result.hero_character_ids == [
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    ]
    assert [item.image_key for item in result.hero_items] == [
        "images/discover/heroes/elon-21-9.jpg",
        "images/discover/heroes/gork-21-9.jpg",
    ]
    assert result.hero_items[0].image_url == "/media/images/discover/heroes/elon-21-9.jpg"
    assert result.hero_items[0].cta_text == "开始对话"


def test_discover_config_service_keeps_legacy_hero_ids(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "discover.json"
    config_path.write_text(
        json.dumps(
            {
                "hero_character_ids": [
                    " 11111111-1111-1111-1111-111111111111 ",
                    "22222222-2222-2222-2222-222222222222",
                    "11111111-1111-1111-1111-111111111111",
                    "",
                    123,
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "DISCOVER_CONFIG_PATH", str(config_path), raising=False)
    monkeypatch.setattr(settings, "R2_PUBLIC_BASE_URL", None, raising=False)

    service = DiscoverConfigService(settings)

    result = service.get_public_config()

    assert result.hero_character_ids == [
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    ]
    assert result.hero_items == []


def test_discover_config_service_returns_empty_when_file_missing(monkeypatch, tmp_path: Path) -> None:
    missing_path = tmp_path / "missing-discover.json"
    monkeypatch.setattr(settings, "DISCOVER_CONFIG_PATH", str(missing_path), raising=False)
    monkeypatch.setattr(settings, "R2_PUBLIC_BASE_URL", None, raising=False)

    service = DiscoverConfigService(settings)

    result = service.get_public_config()

    assert result.hero_character_ids == []
    assert result.hero_items == []


def test_discover_config_service_normalizes_professional_character_ids(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "discover.json"
    config_path.write_text(
        json.dumps(
            {
                "story_character_ids": ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"],
                "professional_character_ids": [
                    " aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa ",
                    "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                    "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    "",
                    123,
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "DISCOVER_CONFIG_PATH", str(config_path), raising=False)
    monkeypatch.setattr(settings, "R2_PUBLIC_BASE_URL", None, raising=False)

    service = DiscoverConfigService(settings)

    result = service.get_public_config()

    assert result.story_character_ids == ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"]
    assert result.professional_character_ids == [
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    ]


def test_discover_config_service_defaults_professional_ids_when_field_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "discover.json"
    config_path.write_text(
        json.dumps({"story_character_ids": ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "DISCOVER_CONFIG_PATH", str(config_path), raising=False)
    monkeypatch.setattr(settings, "R2_PUBLIC_BASE_URL", None, raising=False)

    service = DiscoverConfigService(settings)

    result = service.get_public_config()

    assert result.professional_character_ids == []


def test_discover_config_service_defaults_professional_ids_when_file_missing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    missing_path = tmp_path / "missing-discover.json"
    monkeypatch.setattr(settings, "DISCOVER_CONFIG_PATH", str(missing_path), raising=False)
    monkeypatch.setattr(settings, "R2_PUBLIC_BASE_URL", None, raising=False)

    service = DiscoverConfigService(settings)

    result = service.get_public_config()

    assert result.professional_character_ids == []


def test_discover_config_service_defaults_professional_ids_when_value_not_list(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "discover.json"
    config_path.write_text(
        json.dumps({"professional_character_ids": "not-a-list"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "DISCOVER_CONFIG_PATH", str(config_path), raising=False)
    monkeypatch.setattr(settings, "R2_PUBLIC_BASE_URL", None, raising=False)

    service = DiscoverConfigService(settings)

    result = service.get_public_config()

    assert result.professional_character_ids == []


def test_discover_config_service_normalizes_professional_ids_independently(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "discover.json"
    config_path.write_text(
        json.dumps(
            {
                "story_character_ids": ["story-1", "story-2"],
                "professional_character_ids": ["pro-1", "pro-2", "pro-3"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "DISCOVER_CONFIG_PATH", str(config_path), raising=False)
    monkeypatch.setattr(settings, "R2_PUBLIC_BASE_URL", None, raising=False)

    service = DiscoverConfigService(settings)

    result = service.get_public_config()

    assert result.story_character_ids == ["story-1", "story-2"]
    assert result.professional_character_ids == ["pro-1", "pro-2", "pro-3"]
