"""Share-card builders for Phase 5 growth system."""

from __future__ import annotations

from datetime import date, datetime

from src.schemas.growth import (
    CharacterMilestoneShareCardPayload,
    DailySigninShareCardPayload,
    GrowthShareCard,
    GrowthShareCardKind,
)

_ENTRY_SLOGANS = (
    "Stay with the work.",
    "One more page. One more day.",
    "Small steps, real miles.",
    "Show up and the rest follows.",
    "Quiet effort still shines.",
)


class GrowthShareCardService:
    @staticmethod
    def pick_entry_slogan(*, stat_date: date) -> str:
        index = stat_date.toordinal() % len(_ENTRY_SLOGANS)
        return _ENTRY_SLOGANS[index]

    @staticmethod
    def build_daily_signin_card(
        *,
        trigger_id: str,
        triggered_at: datetime,
        chat_id: str | None,
        payload: DailySigninShareCardPayload,
    ) -> GrowthShareCard:
        return GrowthShareCard(
            id=trigger_id,
            kind=GrowthShareCardKind.DAILY_SIGNIN_COMPLETED,
            triggered_at=triggered_at,
            chat_id=chat_id,
            title="今日签到完成",
            subtitle=f"今日已发送 {payload.today_user_message_count} 条消息",
            primary_button_label="分享",
            daily_signin_payload=payload,
        )

    @staticmethod
    def build_character_milestone_card(
        *,
        trigger_id: str,
        triggered_at: datetime,
        chat_id: str | None,
        character_id: str,
        payload: CharacterMilestoneShareCardPayload,
    ) -> GrowthShareCard:
        return GrowthShareCard(
            id=trigger_id,
            kind=GrowthShareCardKind.CHARACTER_MESSAGE_MILESTONE,
            triggered_at=triggered_at,
            chat_id=chat_id,
            character_id=character_id,
            title=f"你和 {payload.character_name} 已累计对话 {payload.milestone_message_count} 条消息",
            subtitle=f"累计 {payload.total_word_count} 词",
            primary_button_label="分享",
            character_milestone_payload=payload,
        )
