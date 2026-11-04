from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import deps
from src.api.exceptions import register_exception_handlers
from src.api.routers import growth
from src.core.dependencies import get_growth_service
from src.db.models import User
from src.schemas.growth import (
    GrowthCalendarDay,
    GrowthCalendarMonth,
    GrowthCalendarResponse,
    GrowthCharacterSummary,
    GrowthChatHeaderResponse,
    GrowthChatSummary,
    GrowthEntryResponse,
    GrowthKpis,
    GrowthMakeUpResponse,
    GrowthOverviewRankings,
    GrowthOverviewResponse,
    GrowthOverviewTrends,
    GrowthPopup,
    GrowthReadingEquivalenceBlock,
    GrowthReadingEquivalent,
    GrowthShareCardsPageResponse,
    GrowthTodaySummary,
    GrowthTrendCharacterBreakdownItem,
    GrowthTrendPoint,
)


class _StubGrowthService:
    def __init__(self) -> None:
        today_summary = GrowthTodaySummary(
            stat_date=datetime(2026, 4, 5, tzinfo=UTC).date(),
            timezone="Asia/Shanghai",
            signin_threshold=15,
            user_signin_message_count=1,
            remaining_user_messages=14,
            is_checked_in=False,
            checked_in_at=None,
            current_natural_streak=0,
            longest_natural_streak=0,
            makeup_card_balance=0,
            today_total_message_count=2,
            today_total_word_count=10,
            today_reading_equivalent=GrowthReadingEquivalent(
                word_count=10,
                cet4_equivalent=0.0,
                cet6_equivalent=0.0,
            ),
        )
        self.consume_entry = AsyncMock(
            return_value=GrowthEntryResponse(
                server_now=datetime(2026, 4, 5, 1, 0, 0, tzinfo=UTC),
                today=today_summary,
                popup=GrowthPopup(
                    should_show=True,
                    slogan="Stay with the work.",
                    calendar=GrowthCalendarMonth(month="2026-04", days=[]),
                ),
            )
        )
        self.get_calendar = AsyncMock(
            return_value=GrowthCalendarResponse(
                today=today_summary,
                calendar=GrowthCalendarMonth(month="2026-04", days=[]),
            )
        )
        self.apply_makeup = AsyncMock(
            return_value=GrowthMakeUpResponse(
                target_date=datetime(2026, 4, 4, tzinfo=UTC).date(),
                updated_day=GrowthCalendarDay(
                    date=datetime(2026, 4, 4, tzinfo=UTC).date(),
                    day_of_month=4,
                    visual_status="makeup_signed",
                    is_today=False,
                    is_future=False,
                    is_natural_signed=False,
                    is_makeup_signed=True,
                    can_make_up=False,
                ),
                makeup_card_balance=0,
                current_natural_streak=1,
                longest_natural_streak=2,
            )
        )
        self.get_chat_header = AsyncMock(
            return_value=GrowthChatHeaderResponse(
                chat_id=str(uuid.uuid4()),
                character_id=str(uuid.uuid4()),
                ring_unit_words=300,
                current_chat_total_english_words=120,
                completed_loops=0,
                current_loop_progress_words=120,
                current_loop_progress_ratio=0.4,
                next_loop_remaining_words=180,
                character_summary=GrowthCharacterSummary(
                    character_id=str(uuid.uuid4()),
                    character_name="Luna",
                    avatar_image_key=None,
                    avatar_urls=None,
                    total_message_count=100,
                    total_word_count=1000,
                    total_reading_equivalent=GrowthReadingEquivalent(word_count=1000, cet4_equivalent=3.3, cet6_equivalent=2.5),
                    chatted_days_count=10,
                    total_exchange_count=50,
                    last_chat_at=datetime(2026, 4, 5, 1, 0, 0, tzinfo=UTC),
                ),
                chat_summary=GrowthChatSummary(
                    total_message_count=20,
                    total_word_count=120,
                    total_reading_equivalent=GrowthReadingEquivalent(word_count=120, cet4_equivalent=0.4, cet6_equivalent=0.3),
                    total_exchange_count=10,
                    chatted_days_count=3,
                ),
            )
        )
        self.trend_character_id = str(uuid.uuid4())
        self.get_overview = AsyncMock(
            return_value=GrowthOverviewResponse(
                kpis=GrowthKpis(
                    current_natural_streak=1,
                    longest_natural_streak=2,
                    makeup_card_balance=0,
                    distinct_characters_chatted=1,
                    total_message_count=100,
                    total_word_count=1000,
                    today_message_count=2,
                    today_word_count=10,
                    top_character=None,
                ),
                trends=GrowthOverviewTrends(
                    last_7_days=[
                        GrowthTrendPoint(
                            stat_date=datetime(2026, 4, 5, tzinfo=UTC).date(),
                            total_message_count=100,
                            total_word_count=1000,
                            is_natural_signed=True,
                            is_makeup_signed=False,
                            character_breakdown=[
                                GrowthTrendCharacterBreakdownItem(
                                    character_id=self.trend_character_id,
                                    character_name="Luna",
                                    user_message_count=40,
                                    word_count=800,
                                )
                            ],
                        )
                    ],
                    last_30_days=[],
                ),
                rankings=GrowthOverviewRankings(by_messages=[], by_words=[], by_chatted_days=[]),
                reading_equivalence=GrowthReadingEquivalenceBlock(
                    global_today=GrowthReadingEquivalent(word_count=10, cet4_equivalent=0.0, cet6_equivalent=0.0),
                    global_history=GrowthReadingEquivalent(word_count=1000, cet4_equivalent=3.3, cet6_equivalent=2.5),
                    focus_character_today=None,
                    focus_character_history=None,
                    focus_character=None,
                ),
            )
        )
        self.list_pending_share_cards = AsyncMock(return_value=GrowthShareCardsPageResponse(items=[]))
        self.consume_share_card = AsyncMock(return_value=None)


def _create_app(service: _StubGrowthService) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(growth.router)

    async def _override_current_user() -> User:
        return User(id=uuid.uuid4(), email="user@example.com")

    app.dependency_overrides[deps.get_current_user] = _override_current_user
    app.dependency_overrides[get_growth_service] = lambda: service
    return app


def test_growth_routes_delegate_to_service() -> None:
    service = _StubGrowthService()
    app = _create_app(service)
    chat_id = uuid.uuid4()
    trigger_id = uuid.uuid4()

    with TestClient(app) as client:
        assert client.post("/v1/growth/entry", json={}).status_code == 200
        assert client.get("/v1/growth/calendar?month=2026-04").status_code == 200
        assert client.post("/v1/growth/make-up", json={"target_date": "2026-04-04"}).status_code == 200
        assert client.get(f"/v1/growth/chats/{chat_id}/header").status_code == 200
        overview_response = client.get("/v1/growth/overview")
        assert overview_response.status_code == 200
        assert client.get("/v1/growth/share-cards/pending").status_code == 200
        assert client.post(f"/v1/growth/share-cards/{trigger_id}/consume").status_code == 204

    overview_payload = overview_response.json()
    assert overview_payload["data"]["trends"]["last_7_days"][0]["character_breakdown"] == [
        {
            "character_id": service.trend_character_id,
            "character_name": "Luna",
            "user_message_count": 40,
            "word_count": 800,
        }
    ]
    service.consume_entry.assert_awaited_once()
    service.get_calendar.assert_awaited_once()
    service.apply_makeup.assert_awaited_once()
    service.get_chat_header.assert_awaited_once()
    service.get_overview.assert_awaited_once()
    service.list_pending_share_cards.assert_awaited_once()
    service.consume_share_card.assert_awaited_once()


def test_growth_routes_return_400_for_invalid_params() -> None:
    service = _StubGrowthService()
    app = _create_app(service)

    with TestClient(app) as client:
        assert client.get("/v1/growth/calendar?month=202604").status_code == 400
        assert client.post("/v1/growth/make-up", json={}).status_code == 422
        assert client.get("/v1/growth/overview?focus_character_id=bad-uuid").status_code == 400
        assert client.get("/v1/growth/share-cards/pending?chat_id=bad-uuid").status_code == 400

    service.get_calendar.assert_not_awaited()
    service.apply_makeup.assert_not_awaited()
    service.get_overview.assert_not_awaited()
    service.list_pending_share_cards.assert_not_awaited()
