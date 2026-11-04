"""Discover-page config schemas."""

from __future__ import annotations

from sqlmodel import Field, SQLModel


class DiscoverHeroItem(SQLModel):
    character_id: str
    image_key: str
    image_url: str
    cta_text: str = "开始对话"


class DiscoverConfigResponse(SQLModel):
    hero_character_ids: list[str] = Field(default_factory=list)
    hero_items: list[DiscoverHeroItem] = Field(default_factory=list)
    story_character_ids: list[str] = Field(default_factory=list)
    professional_character_ids: list[str] = Field(default_factory=list)


class DiscoverHeroImageUploadResponse(SQLModel):
    image_key: str
    image_url: str
    object_key: str
    content_type: str
    size_bytes: int
    width: int
    height: int
