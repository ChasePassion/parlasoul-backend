"""Service layer for Phase 5 growth system."""

from __future__ import annotations

import calendar
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from src.core.exceptions import APIError
from src.core.uow import UnitOfWork
from src.db.models import Character, Chat, GrowthCharacterStats, GrowthShareTrigger, User
from src.repositories.candidate_repository import CandidateRepository
from src.repositories.character_repository import CharacterRepository
from src.repositories.chat_repository import ChatRepository
from src.repositories.growth_repository import GrowthRepository
from src.repositories.turn_repository import TurnRepository
from src.schemas.growth import (
    CharacterMilestoneShareCardPayload,
    DailySigninShareCardPayload,
    GrowthCalendarDay,
    GrowthCalendarMonth,
    GrowthCalendarResponse,
    GrowthCharacterHeadline,
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
    GrowthRankingItem,
    GrowthReadingEquivalenceBlock,
    GrowthReadingEquivalent,
    GrowthShareCard,
    GrowthShareCardsPageResponse,
    GrowthTodaySummary,
    GrowthTrendCharacterBreakdownItem,
    GrowthTrendPoint,
)
from src.services.growth_rules import (
    BEIJING_TZ,
    READING_RING_UNIT,
    advance_natural_signin,
    calendar_visual_status,
    compute_streak_from_signed_dates,
    crossed_milestones,
    now_in_beijing,
    reading_equivalent,
    remaining_signin_messages,
    ring_progress,
    today_in_beijing,
)
from src.services.growth_share_card_service import GrowthShareCardService
from src.services.growth_tokenizer import count_english_words, sum_english_words
from src.services.media_url_service import build_avatar_urls


@dataclass(frozen=True)
class GrowthStreamUpdate:
    today: GrowthTodaySummary
    share_cards: list[GrowthShareCard]


@dataclass(frozen=True)
class _ChatBranchStats:
    total_word_count: int
    total_message_count: int
    total_exchange_count: int
    chatted_days_count: int


def _avatar_payload(character: Character) -> dict[str, object]:
    return {
        "avatar_image_key": character.avatar_image_key,
        "avatar_urls": build_avatar_urls(character.avatar_image_key),
    }


class GrowthService:
    def __init__(
        self,
        *,
        uow: UnitOfWork,
        growth_repo: GrowthRepository,
        chat_repo: ChatRepository,
        turn_repo: TurnRepository,
        candidate_repo: CandidateRepository,
        character_repo: CharacterRepository,
        share_card_service: GrowthShareCardService | None = None,
    ) -> None:
        self._uow = uow
        self._growth_repo = growth_repo
        self._chat_repo = chat_repo
        self._turn_repo = turn_repo
        self._candidate_repo = candidate_repo
        self._character_repo = character_repo
        self._share_card_service = share_card_service or GrowthShareCardService()

    def _to_reading_equivalent(self, word_count: int) -> GrowthReadingEquivalent:
        cet4, cet6 = reading_equivalent(word_count)
        return GrowthReadingEquivalent(
            word_count=max(0, int(word_count)),
            cet4_equivalent=cet4,
            cet6_equivalent=cet6,
        )

    def _to_today_summary(self, *, today: date, daily_stats, user_stats) -> GrowthTodaySummary:
        total_word_count = int(daily_stats.total_word_count if daily_stats else 0)
        user_signin_message_count = int(daily_stats.user_signin_message_count if daily_stats else 0)
        is_checked_in = bool(
            (daily_stats and daily_stats.is_natural_signed) or (daily_stats and daily_stats.is_makeup_signed)
        )
        checked_in_at = None
        if daily_stats:
            checked_in_at = daily_stats.natural_signed_at or daily_stats.makeup_signed_at

        return GrowthTodaySummary(
            stat_date=today,
            timezone=str(BEIJING_TZ),
            signin_threshold=15,
            user_signin_message_count=user_signin_message_count,
            remaining_user_messages=remaining_signin_messages(user_signin_message_count),
            is_checked_in=is_checked_in,
            checked_in_at=checked_in_at,
            current_natural_streak=int(user_stats.current_natural_streak if user_stats else 0),
            longest_natural_streak=int(user_stats.longest_natural_streak if user_stats else 0),
            makeup_card_balance=int(user_stats.makeup_card_balance if user_stats else 0),
            today_total_message_count=int(daily_stats.total_message_count if daily_stats else 0),
            today_total_word_count=total_word_count,
            today_reading_equivalent=self._to_reading_equivalent(total_word_count),
        )

    async def _build_calendar_month(self, *, user_id: uuid.UUID, month: str | None, today: date) -> GrowthCalendarMonth:
        if month:
            try:
                year = int(month[:4])
                month_value = int(month[5:7])
                first_day = date(year, month_value, 1)
            except ValueError as exc:
                raise APIError(status_code=400, code="invalid_param", message="invalid request: month is invalid") from exc
        else:
            first_day = today.replace(day=1)

        _, last_day_num = calendar.monthrange(first_day.year, first_day.month)
        last_day = first_day.replace(day=last_day_num)
        rows = await self._growth_repo.list_daily_stats_range(
            user_id,
            start_date=first_day,
            end_date=last_day,
        )
        row_by_date = {row.stat_date: row for row in rows}
        days: list[GrowthCalendarDay] = []
        for day_number in range(1, last_day_num + 1):
            current_day = first_day.replace(day=day_number)
            row = row_by_date.get(current_day)
            is_natural_signed = bool(row.is_natural_signed) if row else False
            is_makeup_signed = bool(row.is_makeup_signed) if row else False
            days.append(
                GrowthCalendarDay(
                    date=current_day,
                    day_of_month=day_number,
                    visual_status=calendar_visual_status(
                        stat_date=current_day,
                        today=today,
                        is_natural_signed=is_natural_signed,
                        is_makeup_signed=is_makeup_signed,
                    ),
                    is_today=current_day == today,
                    is_future=current_day > today,
                    is_natural_signed=is_natural_signed,
                    is_makeup_signed=is_makeup_signed,
                    can_make_up=current_day < today and not is_natural_signed and not is_makeup_signed,
                )
            )
        return GrowthCalendarMonth(month=f"{first_day.year:04d}-{first_day.month:02d}", days=days)

    def _build_share_card_from_trigger(self, trigger: GrowthShareTrigger) -> GrowthShareCard:
        payload = dict(trigger.payload or {})
        kind = payload.get("kind") or trigger.trigger_kind
        if kind == "daily_signin_completed":
            daily_payload = DailySigninShareCardPayload.model_validate(payload["daily_signin_payload"])
            return self._share_card_service.build_daily_signin_card(
                trigger_id=str(trigger.id),
                triggered_at=trigger.triggered_at,
                chat_id=str(trigger.chat_id) if trigger.chat_id else None,
                payload=daily_payload,
            )

        character_payload = CharacterMilestoneShareCardPayload.model_validate(payload["character_milestone_payload"])
        return self._share_card_service.build_character_milestone_card(
            trigger_id=str(trigger.id),
            triggered_at=trigger.triggered_at,
            chat_id=str(trigger.chat_id) if trigger.chat_id else None,
            character_id=str(trigger.character_id) if trigger.character_id else character_payload.character_id,
            payload=character_payload,
        )

    async def _append_trigger_if_absent(
        self,
        *,
        user_id: uuid.UUID,
        chat_id: uuid.UUID | None,
        character_id: uuid.UUID | None,
        trigger_kind: str,
        trigger_key: str,
        payload: dict,
    ) -> GrowthShareCard | None:
        existing = await self._growth_repo.get_share_trigger_by_key(trigger_key)
        if existing:
            return None

        trigger, created = await self._growth_repo.create_share_trigger_if_absent(
            user_id=user_id,
            chat_id=chat_id,
            character_id=character_id,
            trigger_kind=trigger_kind,
            trigger_key=trigger_key,
            payload=payload,
        )
        if not created:
            return None
        return self._build_share_card_from_trigger(trigger)

    async def _append_milestone_cards(
        self,
        *,
        user_id: uuid.UUID,
        chat_id: uuid.UUID,
        character_id: uuid.UUID,
        character: Character,
        character_stats: GrowthCharacterStats,
        previous_total_message_count: int,
    ) -> list[GrowthShareCard]:
        share_cards: list[GrowthShareCard] = []
        for milestone in crossed_milestones(previous_total_message_count, int(character_stats.total_message_count)):
            payload_model = CharacterMilestoneShareCardPayload(
                character_id=str(character.id),
                character_name=character.name,
                **_avatar_payload(character),
                milestone_message_count=milestone,
                total_word_count=int(character_stats.total_word_count),
                chatted_days_count=int(character_stats.chatted_days_count),
                total_exchange_count=int(character_stats.total_exchange_count),
                reading_equivalent=self._to_reading_equivalent(int(character_stats.total_word_count)),
            )
            share_card = await self._append_trigger_if_absent(
                user_id=user_id,
                chat_id=chat_id,
                character_id=character_id,
                trigger_kind="character_message_milestone",
                trigger_key=f"character_message_milestone:{user_id}:{character_id}:{milestone}",
                payload={
                    "kind": "character_message_milestone",
                    "character_milestone_payload": payload_model.model_dump(mode="json"),
                },
            )
            if share_card is not None:
                share_cards.append(share_card)
        return share_cards

    async def consume_entry(self, *, current_user: User, calendar_month: str | None) -> GrowthEntryResponse:
        today = today_in_beijing()
        now = now_in_beijing()
        should_show_popup = False
        async with self._uow.transactional():
            user_stats = await self._growth_repo.get_or_create_user_stats(current_user.id)
            daily_stats = await self._growth_repo.get_or_create_daily_stats(current_user.id, today, for_update=True)
            should_show_popup = daily_stats.popup_consumed_at is None
            if should_show_popup:
                daily_stats.popup_consumed_at = now

        daily_stats = await self._growth_repo.get_daily_stats(current_user.id, today)
        user_stats = await self._growth_repo.get_or_create_user_stats(current_user.id)
        today_summary = self._to_today_summary(today=today, daily_stats=daily_stats, user_stats=user_stats)
        calendar_month_payload = await self._build_calendar_month(
            user_id=current_user.id,
            month=calendar_month,
            today=today,
        )
        popup = GrowthPopup(
            should_show=should_show_popup,
            slogan=self._share_card_service.pick_entry_slogan(stat_date=today),
            calendar=calendar_month_payload,
        )
        return GrowthEntryResponse(server_now=now, today=today_summary, popup=popup)

    async def get_calendar(self, *, current_user: User, month: str | None) -> GrowthCalendarResponse:
        today = today_in_beijing()
        user_stats = await self._growth_repo.get_or_create_user_stats(current_user.id)
        daily_stats = await self._growth_repo.get_daily_stats(current_user.id, today)
        return GrowthCalendarResponse(
            today=self._to_today_summary(today=today, daily_stats=daily_stats, user_stats=user_stats),
            calendar=await self._build_calendar_month(user_id=current_user.id, month=month, today=today),
        )

    async def apply_makeup(self, *, current_user: User, target_date: date) -> GrowthMakeUpResponse:
        today = today_in_beijing()
        if target_date >= today:
            raise APIError(status_code=400, code="invalid_param", message="invalid request: target_date must be before today")

        now = now_in_beijing()
        async with self._uow.transactional():
            user_stats = await self._growth_repo.get_or_create_user_stats(current_user.id, for_update=True)
            if int(user_stats.makeup_card_balance) <= 0:
                raise APIError(status_code=409, code="resource_conflict", message="resource conflict: makeup card balance is empty")

            daily_stats = await self._growth_repo.get_or_create_daily_stats(current_user.id, target_date, for_update=True)
            if daily_stats.is_natural_signed or daily_stats.is_makeup_signed:
                raise APIError(status_code=409, code="resource_conflict", message="resource conflict: target date is already signed")

            daily_stats.is_makeup_signed = True
            daily_stats.makeup_signed_at = now
            user_stats.makeup_card_balance = max(0, int(user_stats.makeup_card_balance) - 1)

            anchor = max(
                user_stats.last_natural_signin_date or target_date,
                target_date,
            )
            lookback_start = anchor - timedelta(days=max(400, int(user_stats.current_natural_streak) + 30))
            rows = await self._growth_repo.list_daily_stats_range(
                current_user.id,
                start_date=lookback_start,
                end_date=anchor,
            )
            signed_dates = {
                row.stat_date for row in rows
                if row.is_natural_signed or row.is_makeup_signed
            }
            new_streak = compute_streak_from_signed_dates(signed_dates, anchor)
            if new_streak > 0:
                user_stats.current_natural_streak = new_streak
                user_stats.longest_natural_streak = max(
                    int(user_stats.longest_natural_streak), new_streak,
                )
                user_stats.last_natural_signin_date = anchor
                user_stats.last_rewarded_natural_streak = max(
                    int(user_stats.last_rewarded_natural_streak),
                    (new_streak // 3) * 3,
                )

        user_stats = await self._growth_repo.get_or_create_user_stats(current_user.id)
        daily_stats = await self._growth_repo.get_daily_stats(current_user.id, target_date)
        assert daily_stats is not None
        return GrowthMakeUpResponse(
            target_date=target_date,
            updated_day=GrowthCalendarDay(
                date=target_date,
                day_of_month=target_date.day,
                visual_status=calendar_visual_status(
                    stat_date=target_date,
                    today=today,
                    is_natural_signed=bool(daily_stats.is_natural_signed),
                    is_makeup_signed=bool(daily_stats.is_makeup_signed),
                ),
                is_today=False,
                is_future=False,
                is_natural_signed=bool(daily_stats.is_natural_signed),
                is_makeup_signed=bool(daily_stats.is_makeup_signed),
                can_make_up=False,
            ),
            makeup_card_balance=int(user_stats.makeup_card_balance),
            current_natural_streak=int(user_stats.current_natural_streak),
            longest_natural_streak=int(user_stats.longest_natural_streak),
        )

    async def get_chat_header(self, *, current_user: User, chat_id: uuid.UUID) -> GrowthChatHeaderResponse:
        chat = await self._chat_repo.get_by_id(chat_id)
        if not chat:
            raise APIError(status_code=404, code="chat_not_found", message=f"resource not found: chat does not exist; chat_id={chat_id}")
        if chat.user_id != current_user.id:
            raise APIError(status_code=403, code="chat_forbidden", message=f"authorization failed: chat is not accessible; chat_id={chat_id}")

        character = await self._character_repo.get_by_id(chat.character_id)
        if not character:
            raise APIError(status_code=404, code="character_not_found", message=f"resource not found: character does not exist; character_id={chat.character_id}")

        branch_stats = await self._compute_chat_branch_stats(chat)
        current_chat_total_english_words = branch_stats.total_word_count
        completed_loops, current_loop_progress_words, current_loop_progress_ratio, next_loop_remaining_words = ring_progress(
            current_chat_total_english_words
        )
        character_stats = await self._growth_repo.get_character_stats(current_user.id, character.id)
        summary = GrowthCharacterSummary(
            character_id=str(character.id),
            character_name=character.name,
            **_avatar_payload(character),
            total_message_count=int(character_stats.total_message_count if character_stats else 0),
            total_word_count=int(character_stats.total_word_count if character_stats else 0),
            total_reading_equivalent=self._to_reading_equivalent(int(character_stats.total_word_count if character_stats else 0)),
            chatted_days_count=int(character_stats.chatted_days_count if character_stats else 0),
            total_exchange_count=int(character_stats.total_exchange_count if character_stats else 0),
            last_chat_at=character_stats.last_chat_at if character_stats else None,
        )
        chat_summary = GrowthChatSummary(
            total_message_count=branch_stats.total_message_count,
            total_word_count=branch_stats.total_word_count,
            total_reading_equivalent=self._to_reading_equivalent(branch_stats.total_word_count),
            total_exchange_count=branch_stats.total_exchange_count,
            chatted_days_count=branch_stats.chatted_days_count,
        )
        return GrowthChatHeaderResponse(
            chat_id=str(chat.id),
            character_id=str(character.id),
            ring_unit_words=READING_RING_UNIT,
            current_chat_total_english_words=current_chat_total_english_words,
            completed_loops=completed_loops,
            current_loop_progress_words=current_loop_progress_words,
            current_loop_progress_ratio=current_loop_progress_ratio,
            next_loop_remaining_words=next_loop_remaining_words,
            character_summary=summary,
            chat_summary=chat_summary,
        )

    async def _compute_chat_branch_stats(self, chat: Chat) -> _ChatBranchStats:
        leaf_turn_id = chat.active_leaf_turn_id or chat.last_turn_id
        if leaf_turn_id is None:
            return _ChatBranchStats(
                total_word_count=0,
                total_message_count=0,
                total_exchange_count=0,
                chatted_days_count=0,
            )

        collected_turns, _ = await self._turn_repo.get_ancestor_chain(
            leaf_turn_id=leaf_turn_id, chat_id=chat.id,
        )

        candidate_ids = [turn.primary_candidate_id for turn in collected_turns if turn.primary_candidate_id is not None]
        candidates = await self._candidate_repo.get_by_ids(candidate_ids)
        candidate_map = {candidate.id: candidate for candidate in candidates}

        total_word_count = sum_english_words(
            candidate_map[turn.primary_candidate_id].content
            for turn in reversed(collected_turns)
            if turn.primary_candidate_id in candidate_map
            and candidate_map[turn.primary_candidate_id].is_final
            and (candidate_map[turn.primary_candidate_id].content or "").strip()
        )

        total_message_count = len(collected_turns)
        total_exchange_count = sum(1 for t in collected_turns if t.author_type == "USER")
        chat_dates = set()
        for t in collected_turns:
            if t.created_at is not None:
                chat_dates.add(t.created_at.date())
        chatted_days_count = len(chat_dates)

        return _ChatBranchStats(
            total_word_count=total_word_count,
            total_message_count=total_message_count,
            total_exchange_count=total_exchange_count,
            chatted_days_count=chatted_days_count,
        )

    async def get_overview(self, *, current_user: User, focus_character_id: uuid.UUID | None) -> GrowthOverviewResponse:
        today = today_in_beijing()
        user_stats = await self._growth_repo.get_or_create_user_stats(current_user.id)
        today_stats = await self._growth_repo.get_daily_stats(current_user.id, today)
        character_rows = await self._growth_repo.list_character_stats(
            current_user.id,
            only_with_exchanges=True,
        )
        distinct_characters_chatted = await self._growth_repo.count_character_rows_with_exchanges(current_user.id)

        total_message_count = sum(int(stats.total_message_count) for stats, _ in character_rows)
        total_word_count = sum(int(stats.total_word_count) for stats, _ in character_rows)

        ranked_by_messages = sorted(
            character_rows,
            key=lambda row: (
                int(row[0].total_message_count),
                int(row[0].chatted_days_count),
                int(row[0].total_exchange_count),
                str(row[0].character_id),
            ),
            reverse=True,
        )
        ranked_by_words = sorted(
            character_rows,
            key=lambda row: (
                int(row[0].total_word_count),
                int(row[0].chatted_days_count),
                int(row[0].total_exchange_count),
                str(row[0].character_id),
            ),
            reverse=True,
        )
        ranked_by_days = sorted(
            character_rows,
            key=lambda row: (
                int(row[0].chatted_days_count),
                int(row[0].total_message_count),
                int(row[0].total_exchange_count),
                str(row[0].character_id),
            ),
            reverse=True,
        )

        top_character = next((row for row in ranked_by_messages if int(row[0].total_exchange_count) > 0), None)
        top_character_headline = None
        if top_character:
            stats, character = top_character
            top_character_headline = GrowthCharacterHeadline(
                character_id=str(character.id),
                character_name=character.name,
                **_avatar_payload(character),
                total_message_count=int(stats.total_message_count),
                chatted_days_count=int(stats.chatted_days_count),
                total_exchange_count=int(stats.total_exchange_count),
            )

        last_30_start = today - timedelta(days=29)
        last_7_start = today - timedelta(days=6)
        daily_rows = await self._growth_repo.list_daily_stats_range(
            current_user.id,
            start_date=last_30_start,
            end_date=today,
        )
        character_daily_rows = await self._growth_repo.list_character_daily_stats_range(
            current_user.id,
            start_date=last_30_start,
            end_date=today,
        )
        daily_map = {row.stat_date: row for row in daily_rows}
        character_breakdown_map: dict[date, list[GrowthTrendCharacterBreakdownItem]] = {}
        for character_daily_stats, character in character_daily_rows:
            entries = character_breakdown_map.setdefault(character_daily_stats.stat_date, [])
            entries.append(
                GrowthTrendCharacterBreakdownItem(
                    character_id=str(character.id),
                    character_name=character.name,
                    user_message_count=int(character_daily_stats.total_exchange_count),
                    word_count=int(character_daily_stats.total_word_count),
                )
            )

        def _build_trend_points(start: date, end: date) -> list[GrowthTrendPoint]:
            points: list[GrowthTrendPoint] = []
            current = start
            while current <= end:
                row = daily_map.get(current)
                points.append(
                    GrowthTrendPoint(
                        stat_date=current,
                        total_message_count=int(row.total_message_count if row else 0),
                        total_word_count=int(row.total_word_count if row else 0),
                        is_natural_signed=bool(row.is_natural_signed) if row else False,
                        is_makeup_signed=bool(row.is_makeup_signed) if row else False,
                        character_breakdown=list(character_breakdown_map.get(current, [])),
                    )
                )
                current += timedelta(days=1)
            return points

        focus_character_headline = None
        focus_character_today = None
        focus_character_history = None
        if focus_character_id is not None:
            focus_character = await self._character_repo.get_by_id(focus_character_id)
            focus_stats = await self._growth_repo.get_character_stats(current_user.id, focus_character_id)
            focus_daily = await self._growth_repo.get_character_daily_stats(current_user.id, focus_character_id, today)
            if focus_character is not None:
                focus_character_headline = GrowthCharacterHeadline(
                    character_id=str(focus_character.id),
                    character_name=focus_character.name,
                    **_avatar_payload(focus_character),
                    total_message_count=int(focus_stats.total_message_count if focus_stats else 0),
                    chatted_days_count=int(focus_stats.chatted_days_count if focus_stats else 0),
                    total_exchange_count=int(focus_stats.total_exchange_count if focus_stats else 0),
                )
                focus_character_today = self._to_reading_equivalent(int(focus_daily.total_word_count if focus_daily else 0))
                focus_character_history = self._to_reading_equivalent(int(focus_stats.total_word_count if focus_stats else 0))

        return GrowthOverviewResponse(
            kpis=GrowthKpis(
                current_natural_streak=int(user_stats.current_natural_streak),
                longest_natural_streak=int(user_stats.longest_natural_streak),
                makeup_card_balance=int(user_stats.makeup_card_balance),
                distinct_characters_chatted=distinct_characters_chatted,
                total_message_count=total_message_count,
                total_word_count=total_word_count,
                today_message_count=int(today_stats.total_message_count if today_stats else 0),
                today_word_count=int(today_stats.total_word_count if today_stats else 0),
                top_character=top_character_headline,
            ),
            trends=GrowthOverviewTrends(
                last_7_days=_build_trend_points(last_7_start, today),
                last_30_days=_build_trend_points(last_30_start, today),
            ),
            rankings=GrowthOverviewRankings(
                by_messages=[self._to_ranking_item(stats, character) for stats, character in ranked_by_messages[:10]],
                by_words=[self._to_ranking_item(stats, character) for stats, character in ranked_by_words[:10]],
                by_chatted_days=[self._to_ranking_item(stats, character) for stats, character in ranked_by_days[:10]],
            ),
            reading_equivalence=GrowthReadingEquivalenceBlock(
                global_today=self._to_reading_equivalent(int(today_stats.total_word_count if today_stats else 0)),
                global_history=self._to_reading_equivalent(total_word_count),
                focus_character_today=focus_character_today,
                focus_character_history=focus_character_history,
                focus_character=focus_character_headline,
            ),
        )

    def _to_ranking_item(self, stats, character: Character) -> GrowthRankingItem:
        return GrowthRankingItem(
            character_id=str(character.id),
            character_name=character.name,
            **_avatar_payload(character),
            total_message_count=int(stats.total_message_count),
            total_word_count=int(stats.total_word_count),
            chatted_days_count=int(stats.chatted_days_count),
            total_exchange_count=int(stats.total_exchange_count),
            last_chat_at=stats.last_chat_at,
        )

    async def list_pending_share_cards(
        self,
        *,
        current_user: User,
        chat_id: uuid.UUID | None,
        limit: int,
    ) -> GrowthShareCardsPageResponse:
        rows = await self._growth_repo.list_pending_share_triggers(
            user_id=current_user.id,
            chat_id=chat_id,
            limit=limit,
        )
        return GrowthShareCardsPageResponse(items=[self._build_share_card_from_trigger(row) for row in rows])

    async def consume_share_card(self, *, current_user: User, trigger_id: uuid.UUID) -> None:
        async with self._uow.transactional():
            trigger = await self._growth_repo.get_share_trigger_by_id(
                user_id=current_user.id,
                trigger_id=trigger_id,
            )
            if trigger is None:
                raise APIError(status_code=404, code="resource_not_found", message="resource not found: share trigger does not exist")
            if trigger.consumed_at is None:
                trigger.consumed_at = now_in_beijing()

    async def record_greeting_inserted(
        self,
        *,
        user_id: uuid.UUID,
        character_id: uuid.UUID,
        chat_id: uuid.UUID,
        greeting_text: str,
        occurred_at: datetime | None = None,
    ) -> list[GrowthShareCard]:
        now = occurred_at or now_in_beijing()
        stat_date = now.astimezone(BEIJING_TZ).date()
        word_count = count_english_words(greeting_text)

        daily_stats = await self._growth_repo.get_or_create_daily_stats(user_id, stat_date, for_update=True)
        character_daily_stats = await self._growth_repo.get_or_create_character_daily_stats(
            user_id, character_id, stat_date, for_update=True
        )
        character_stats = await self._growth_repo.get_or_create_character_stats(user_id, character_id, for_update=True)

        previous_total_message_count = int(character_stats.total_message_count)

        daily_stats.total_message_count = int(daily_stats.total_message_count) + 1
        daily_stats.total_word_count = int(daily_stats.total_word_count) + word_count

        character_daily_stats.total_message_count = int(character_daily_stats.total_message_count) + 1
        character_daily_stats.total_word_count = int(character_daily_stats.total_word_count) + word_count
        character_daily_stats.last_chat_at = now

        character_stats.total_message_count = previous_total_message_count + 1
        character_stats.total_word_count = int(character_stats.total_word_count) + word_count
        character_stats.last_chat_at = now

        character = await self._character_repo.get_by_id(character_id)
        if character is None:
            return []

        return await self._append_milestone_cards(
            user_id=user_id,
            chat_id=chat_id,
            character_id=character_id,
            character=character,
            character_stats=character_stats,
            previous_total_message_count=previous_total_message_count,
        )

    async def record_canonical_chat_completed(
        self,
        *,
        user_id: uuid.UUID,
        character_id: uuid.UUID,
        chat_id: uuid.UUID,
        user_message: str,
        assistant_message: str,
        occurred_at: datetime | None = None,
    ) -> GrowthStreamUpdate:
        now = occurred_at or now_in_beijing()
        stat_date = now.astimezone(BEIJING_TZ).date()
        total_word_delta = count_english_words(user_message) + count_english_words(assistant_message)

        user_stats = await self._growth_repo.get_or_create_user_stats(user_id, for_update=True)
        daily_stats = await self._growth_repo.get_or_create_daily_stats(user_id, stat_date, for_update=True)
        character_daily_stats = await self._growth_repo.get_or_create_character_daily_stats(
            user_id, character_id, stat_date, for_update=True
        )
        character_stats = await self._growth_repo.get_or_create_character_stats(user_id, character_id, for_update=True)
        character = await self._character_repo.get_by_id(character_id)

        previous_total_message_count = int(character_stats.total_message_count)
        previous_exchange_count_for_day = int(character_daily_stats.total_exchange_count)

        daily_stats.user_signin_message_count = int(daily_stats.user_signin_message_count) + 1
        daily_stats.total_message_count = int(daily_stats.total_message_count) + 2
        daily_stats.total_word_count = int(daily_stats.total_word_count) + total_word_delta

        character_daily_stats.total_message_count = int(character_daily_stats.total_message_count) + 2
        character_daily_stats.total_word_count = int(character_daily_stats.total_word_count) + total_word_delta
        character_daily_stats.total_exchange_count = int(character_daily_stats.total_exchange_count) + 1
        character_daily_stats.last_chat_at = now

        character_stats.total_message_count = previous_total_message_count + 2
        character_stats.total_word_count = int(character_stats.total_word_count) + total_word_delta
        character_stats.total_exchange_count = int(character_stats.total_exchange_count) + 1
        character_stats.last_chat_at = now
        if previous_exchange_count_for_day == 0:
            character_stats.chatted_days_count = int(character_stats.chatted_days_count) + 1

        share_cards: list[GrowthShareCard] = []
        if not daily_stats.is_natural_signed and int(daily_stats.user_signin_message_count) >= 15:
            daily_stats.is_natural_signed = True
            daily_stats.natural_signed_at = now
            advance_result = advance_natural_signin(
                current_natural_streak=int(user_stats.current_natural_streak),
                longest_natural_streak=int(user_stats.longest_natural_streak),
                makeup_card_balance=int(user_stats.makeup_card_balance),
                last_natural_signin_date=user_stats.last_natural_signin_date,
                last_rewarded_natural_streak=int(user_stats.last_rewarded_natural_streak),
                signin_date=stat_date,
            )
            user_stats.current_natural_streak = advance_result.current_natural_streak
            user_stats.longest_natural_streak = advance_result.longest_natural_streak
            user_stats.makeup_card_balance = advance_result.makeup_card_balance
            user_stats.last_natural_signin_date = advance_result.last_natural_signin_date
            user_stats.last_rewarded_natural_streak = advance_result.last_rewarded_natural_streak

            signin_payload = DailySigninShareCardPayload(
                stat_date=stat_date,
                today_user_message_count=int(daily_stats.user_signin_message_count),
                today_total_word_count=int(daily_stats.total_word_count),
                reading_equivalent=self._to_reading_equivalent(int(daily_stats.total_word_count)),
                current_natural_streak=int(user_stats.current_natural_streak),
            )
            share_card = await self._append_trigger_if_absent(
                user_id=user_id,
                chat_id=chat_id,
                character_id=character_id,
                trigger_kind="daily_signin_completed",
                trigger_key=f"daily_signin_completed:{user_id}:{stat_date.isoformat()}",
                payload={
                    "kind": "daily_signin_completed",
                    "daily_signin_payload": signin_payload.model_dump(mode="json"),
                },
            )
            if share_card is not None:
                share_cards.append(share_card)

        if character is not None:
            share_cards.extend(
                await self._append_milestone_cards(
                    user_id=user_id,
                    chat_id=chat_id,
                    character_id=character_id,
                    character=character,
                    character_stats=character_stats,
                    previous_total_message_count=previous_total_message_count,
                )
            )

        return GrowthStreamUpdate(
            today=self._to_today_summary(today=stat_date, daily_stats=daily_stats, user_stats=user_stats),
            share_cards=share_cards,
        )
