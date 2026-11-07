"""Service layer for saved item APIs."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError

from src.core.exceptions import APIError
from src.core.uow import UnitOfWork
from src.db.models import SavedItem, User
from src.repositories.saved_item_repository import SavedItemRepository
from src.schemas.learning import (
    SavedItemCreateRequest,
    SavedItemKind,
    SavedItemPayload,
    SavedItemResponse,
    SavedItemsPage,
)

_SUPPORTED_SAVED_ITEM_KINDS: set[str] = {"reply_card", "word_card", "feedback_card"}


def _parse_uuid_or_raise(value: str, *, code: str, field_name: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise APIError(
            status_code=400,
            code=code,
            message=f"invalid request: {field_name} must be a valid UUID",
        )


def _normalize_kind(kind: str) -> SavedItemKind:
    normalized = (kind or "").strip()
    if normalized not in _SUPPORTED_SAVED_ITEM_KINDS:
        raise APIError(
            status_code=400,
            code="saved_item_kind_invalid",
            message=(
                "invalid request: saved item kind is not supported; "
                "supported=reply_card,word_card,feedback_card"
            ),
        )
    return normalized  # type: ignore[return-value]


def _coerce_card_object(kind: str, card: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(card, dict):
        raise APIError(
            status_code=400,
            code="saved_item_card_invalid",
            message=f"invalid request: card must be an object; kind={kind}",
        )

    nested = card.get(kind)
    if isinstance(nested, dict):
        return dict(nested)
    return dict(card)


def _sanitize_saved_item_card(kind: str, card: dict[str, Any] | None) -> dict[str, Any]:
    cleaned = _coerce_card_object(kind, card)
    if kind == "reply_card":
        cleaned.pop("ipa_us", None)
    return cleaned


def _canonical_reply_card(card: dict[str, Any]) -> tuple[str, str]:
    surface = (card.get("surface") or "").strip()
    zh = (card.get("zh") or "").strip()
    if not surface or not zh:
        raise APIError(
            status_code=400,
            code="saved_item_card_invalid",
            message="invalid request: reply_card must include surface and zh",
        )
    return surface, zh


def _canonical_word_card(card: dict[str, Any]) -> tuple[str, str]:
    surface = (card.get("surface") or "").strip()
    if not surface:
        raise APIError(
            status_code=400,
            code="saved_item_card_invalid",
            message="invalid request: word_card must include surface",
        )

    zh = ""
    pos_groups = card.get("pos_groups")
    if isinstance(pos_groups, list):
        for group in pos_groups:
            if not isinstance(group, dict):
                continue
            senses = group.get("senses")
            if not isinstance(senses, list):
                continue
            for sense in senses:
                if not isinstance(sense, dict):
                    continue
                candidate = (sense.get("zh") or "").strip()
                if candidate:
                    zh = candidate
                    break
            if zh:
                break

    if not zh:
        example = card.get("example")
        if isinstance(example, dict):
            zh = (example.get("zh") or "").strip()

    if not zh:
        zh = "（暂无中文释义）"
    return surface, zh


def _canonical_feedback_card(card: dict[str, Any]) -> tuple[str, str]:
    surface = (card.get("original") or card.get("surface") or "").strip()
    zh = (card.get("zh") or "").strip()
    if not surface or not zh:
        raise APIError(
            status_code=400,
            code="saved_item_card_invalid",
            message="invalid request: feedback_card must include surface and zh",
        )
    return surface, zh


def _canonicalize_saved_item(kind: str, card: dict[str, Any] | None) -> tuple[dict[str, Any], str, str]:
    normalized_kind = _normalize_kind(kind)
    cleaned_card = _sanitize_saved_item_card(normalized_kind, card)
    if normalized_kind == "reply_card":
        surface, zh = _canonical_reply_card(cleaned_card)
    elif normalized_kind == "word_card":
        surface, zh = _canonical_word_card(cleaned_card)
    else:
        surface, zh = _canonical_feedback_card(cleaned_card)
    return cleaned_card, surface, zh


def _saved_item_response(item: SavedItem) -> SavedItemResponse:
    created_at = item.created_at
    if created_at is None:
        raise APIError(
            status_code=500,
            code="saved_item_created_at_missing",
            message="server state invalid: created_at is missing for saved item",
        )
    return SavedItemResponse(
        id=str(item.id),
        kind=_normalize_kind(item.kind),
        display={"surface": item.display_surface, "zh": item.display_zh},
        card=_sanitize_saved_item_card(item.kind, item.card),
        source={
            "role_id": str(item.source_role_id),
            "chat_id": str(item.source_chat_id),
            "message_id": item.source_message_id,
            "turn_id": str(item.source_turn_id) if item.source_turn_id else None,
            "candidate_id": str(item.source_candidate_id) if item.source_candidate_id else None,
            "meta": item.source_meta,
        },
        created_at=created_at,
    )


class SavedItemService:
    """Business logic for saved item lifecycle."""

    def __init__(self, repo: SavedItemRepository, uow: UnitOfWork):
        self._repo = repo
        self._uow = uow

    async def create_saved_item(
        self,
        *,
        request: SavedItemCreateRequest,
        current_user: User,
    ) -> SavedItemResponse:
        payload: SavedItemPayload = request.saved_item
        normalized_kind = _normalize_kind(payload.kind)
        cleaned_card, canonical_surface, canonical_zh = _canonicalize_saved_item(
            normalized_kind,
            payload.card,
        )

        source_meta = dict(payload.source.meta) if isinstance(payload.source.meta, dict) else {}
        if normalized_kind == "feedback_card":
            original = (cleaned_card.get("original") or "").strip()
            if original:
                source_meta.setdefault("original_content", original)

        async with self._uow.transactional():
            duplicate = await self._repo.find_duplicate_by_surface(
                user_id=current_user.id,
                kind=normalized_kind,
                surface=canonical_surface,
            )
            if duplicate:
                return _saved_item_response(duplicate)

            item = SavedItem(
                user_id=current_user.id,
                kind=normalized_kind,
                display_surface=canonical_surface,
                display_zh=canonical_zh,
                card=cleaned_card,
                source_role_id=payload.source.role_id,
                source_chat_id=payload.source.chat_id,
                source_message_id=payload.source.message_id,
                source_turn_id=payload.source.turn_id,
                source_candidate_id=payload.source.candidate_id,
                source_meta=source_meta or None,
            )
            try:
                created = await self._repo.create(item)
            except IntegrityError:
                duplicate = await self._repo.find_duplicate_by_surface(
                    user_id=current_user.id,
                    kind=normalized_kind,
                    surface=canonical_surface,
                )
                if duplicate:
                    return _saved_item_response(duplicate)
                raise

        return _saved_item_response(created)

    async def list_saved_items(
        self,
        *,
        current_user: User,
        kind: str | None,
        role_id: str | None,
        chat_id: str | None,
        cursor: str | None,
        limit: int,
    ) -> SavedItemsPage:
        parsed_role_id = None
        parsed_chat_id = None
        normalized_kind = _normalize_kind(kind) if kind else None
        if role_id:
            parsed_role_id = _parse_uuid_or_raise(
                role_id, code="saved_item_filter_invalid", field_name="role_id"
            )
        if chat_id:
            parsed_chat_id = _parse_uuid_or_raise(
                chat_id, code="saved_item_filter_invalid", field_name="chat_id"
            )
        if cursor:
            _parse_uuid_or_raise(cursor, code="saved_item_cursor_invalid", field_name="cursor")

        items, next_cursor, has_more = await self._repo.list_by_user(
            user_id=current_user.id,
            kind=normalized_kind,
            role_id=parsed_role_id,
            chat_id=parsed_chat_id,
            cursor=cursor,
            limit=limit,
        )
        return SavedItemsPage(
            items=[_saved_item_response(item) for item in items],
            next_cursor=next_cursor,
            has_more=has_more,
        )

    async def delete_saved_item(
        self,
        *,
        saved_item_id: str,
        current_user: User,
    ) -> None:
        item_id = _parse_uuid_or_raise(
            saved_item_id, code="saved_item_id_invalid", field_name="saved_item_id"
        )

        async with self._uow.transactional():
            item = await self._repo.get_by_id_and_user(
                saved_item_id=item_id,
                user_id=current_user.id,
            )
            if not item:
                raise APIError(
                    status_code=404,
                    code="saved_item_not_found",
                    message=f"resource not found: saved item does not exist; saved_item_id={saved_item_id}",
                )
            await self._repo.delete(item)
