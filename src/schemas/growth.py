"""Schema contracts for Phase 5 growth system."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import ConfigDict
from sqlmodel import Field, SQLModel

from src.schemas.character import CharacterStatusEnum
from src.schemas.media import AvatarUrls


class GrowthCalendarVisualStatus(StrEnum):
    NATURAL_SIGNED = "natural_signed"
    MAKEUP_SIGNED = "makeup_signed"
    MISSED = "missed"
    TODAY_PENDING = "today_pending"
    TODAY_NATURAL_SIGNED = "today_natural_signed"
    TODAY_MAKEUP_SIGNED = "today_makeup_signed"
    FUTURE = "future"


class GrowthShareCardKind(StrEnum):
    DAILY_SIGNIN_COMPLETED = "daily_signin_completed"
    CHARACTER_MESSAGE_MILESTONE = "character_message_milestone"


class GrowthEntryRequest(SQLModel):
    model_config = ConfigDict(extra="forbid")

    calendar_month: str | None = None


class GrowthMakeUpRequest(SQLModel):
    model_config = ConfigDict(extra="forbid")

    target_date: str


class GrowthReadingEquivalent(SQLModel):
    word_count: int
    cet4_equivalent: float
    cet6_equivalent: float


class GrowthTodaySummary(SQLModel):
    stat_date: date
    timezone: str
    signin_threshold: int
    user_signin_message_count: int
    remaining_user_messages: int
    is_checked_in: bool
    checked_in_at: datetime | None = None
    current_natural_streak: int
    longest_natural_streak: int
    makeup_card_balance: int
    today_total_message_count: int
    today_total_word_count: int
    today_reading_equivalent: GrowthReadingEquivalent


class GrowthCalendarDay(SQLModel):
    date: date
    day_of_month: int
    visual_status: GrowthCalendarVisualStatus
    is_today: bool
    is_future: bool
    is_natural_signed: bool
    is_makeup_signed: bool
    can_make_up: bool


class GrowthCalendarMonth(SQLModel):
    month: str
    days: list[GrowthCalendarDay] = Field(default_factory=list)


class GrowthPopup(SQLModel):
    should_show: bool
    slogan: str
    calendar: GrowthCalendarMonth


class GrowthEntryResponse(SQLModel):
    server_now: datetime
    today: GrowthTodaySummary
    popup: GrowthPopup


class GrowthCalendarResponse(SQLModel):
    today: GrowthTodaySummary
    calendar: GrowthCalendarMonth


class GrowthMakeUpResponse(SQLModel):
    target_date: date
    updated_day: GrowthCalendarDay
    makeup_card_balance: int
    current_natural_streak: int
    longest_natural_streak: int


class GrowthCharacterSummary(SQLModel):
    character_id: str
    character_name: str
    character_status: CharacterStatusEnum = CharacterStatusEnum.ACTIVE
    avatar_image_key: str | None = None
    avatar_urls: AvatarUrls | None = None
    total_message_count: int
    total_word_count: int
    total_reading_equivalent: GrowthReadingEquivalent
    chatted_days_count: int
    total_exchange_count: int
    last_chat_at: datetime | None = None


class GrowthChatSummary(SQLModel):
    total_message_count: int
    total_word_count: int
    total_reading_equivalent: GrowthReadingEquivalent
    total_exchange_count: int
    chatted_days_count: int


class GrowthChatHeaderResponse(SQLModel):
    chat_id: str
    character_id: str
    ring_unit_words: int
    current_chat_total_english_words: int
    completed_loops: int
    current_loop_progress_words: int
    current_loop_progress_ratio: float
    next_loop_remaining_words: int
    character_summary: GrowthCharacterSummary
    chat_summary: GrowthChatSummary


class GrowthCharacterHeadline(SQLModel):
    character_id: str
    character_name: str
    character_status: CharacterStatusEnum = CharacterStatusEnum.ACTIVE
    avatar_image_key: str | None = None
    avatar_urls: AvatarUrls | None = None
    total_message_count: int
    chatted_days_count: int
    total_exchange_count: int


class GrowthKpis(SQLModel):
    current_natural_streak: int
    longest_natural_streak: int
    makeup_card_balance: int
    distinct_characters_chatted: int
    total_message_count: int
    total_word_count: int
    today_message_count: int
    today_word_count: int
    top_character: GrowthCharacterHeadline | None = None


class GrowthTrendCharacterBreakdownItem(SQLModel):
    character_id: str
    character_name: str
    user_message_count: int
    word_count: int


class GrowthTrendPoint(SQLModel):
    stat_date: date
    total_message_count: int
    total_word_count: int
    is_natural_signed: bool
    is_makeup_signed: bool
    character_breakdown: list[GrowthTrendCharacterBreakdownItem] = Field(default_factory=list)


class GrowthRankingItem(SQLModel):
    character_id: str
    character_name: str
    character_status: CharacterStatusEnum = CharacterStatusEnum.ACTIVE
    avatar_image_key: str | None = None
    avatar_urls: AvatarUrls | None = None
    total_message_count: int
    total_word_count: int
    chatted_days_count: int
    total_exchange_count: int
    last_chat_at: datetime | None = None


class GrowthReadingEquivalenceBlock(SQLModel):
    global_today: GrowthReadingEquivalent
    global_history: GrowthReadingEquivalent
    focus_character_today: GrowthReadingEquivalent | None = None
    focus_character_history: GrowthReadingEquivalent | None = None
    focus_character: GrowthCharacterHeadline | None = None


class GrowthOverviewTrends(SQLModel):
    last_7_days: list[GrowthTrendPoint] = Field(default_factory=list)
    last_30_days: list[GrowthTrendPoint] = Field(default_factory=list)


class GrowthOverviewRankings(SQLModel):
    by_messages: list[GrowthRankingItem] = Field(default_factory=list)
    by_words: list[GrowthRankingItem] = Field(default_factory=list)
    by_chatted_days: list[GrowthRankingItem] = Field(default_factory=list)


class GrowthOverviewResponse(SQLModel):
    kpis: GrowthKpis
    trends: GrowthOverviewTrends
    rankings: GrowthOverviewRankings
    reading_equivalence: GrowthReadingEquivalenceBlock


class DailySigninShareCardPayload(SQLModel):
    stat_date: date
    today_user_message_count: int
    today_total_word_count: int
    reading_equivalent: GrowthReadingEquivalent
    current_natural_streak: int


class CharacterMilestoneShareCardPayload(SQLModel):
    character_id: str
    character_name: str
    avatar_image_key: str | None = None
    avatar_urls: AvatarUrls | None = None
    milestone_message_count: int
    total_word_count: int
    chatted_days_count: int
    total_exchange_count: int
    reading_equivalent: GrowthReadingEquivalent


class GrowthShareCard(SQLModel):
    id: str
    kind: GrowthShareCardKind
    triggered_at: datetime
    chat_id: str | None = None
    character_id: str | None = None
    title: str
    subtitle: str | None = None
    primary_button_label: str
    daily_signin_payload: DailySigninShareCardPayload | None = None
    character_milestone_payload: CharacterMilestoneShareCardPayload | None = None


class GrowthShareCardsPageResponse(SQLModel):
    items: list[GrowthShareCard] = Field(default_factory=list)
