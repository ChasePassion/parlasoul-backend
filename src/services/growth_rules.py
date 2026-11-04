"""Pure rules for Phase 5 growth system."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

BEIJING_TZ = ZoneInfo("Asia/Shanghai")
SIGNIN_THRESHOLD = 15
MAKEUP_CARD_MAX = 10
READING_RING_UNIT = 300
CET4_READING_UNIT = 300
CET6_READING_UNIT = 400
CHARACTER_MESSAGE_MILESTONES = (100, 300, 500, 1000, 3000, 10000)


def now_in_beijing(now: datetime | None = None) -> datetime:
    current = now or datetime.now(BEIJING_TZ)
    if current.tzinfo is None:
        return current.replace(tzinfo=BEIJING_TZ)
    return current.astimezone(BEIJING_TZ)


def today_in_beijing(now: datetime | None = None) -> date:
    return now_in_beijing(now).date()


def remaining_signin_messages(user_signin_message_count: int) -> int:
    return max(0, SIGNIN_THRESHOLD - max(0, int(user_signin_message_count)))


def reading_equivalent(word_count: int) -> tuple[float, float]:
    words = max(0, int(word_count))
    return round(words / CET4_READING_UNIT, 1), round(words / CET6_READING_UNIT, 1)


def ring_progress(total_word_count: int) -> tuple[int, int, float, int]:
    words = max(0, int(total_word_count))
    completed_loops = words // READING_RING_UNIT
    current_loop_progress_words = words % READING_RING_UNIT
    progress_ratio = current_loop_progress_words / READING_RING_UNIT
    next_loop_remaining_words = READING_RING_UNIT - current_loop_progress_words
    if next_loop_remaining_words == 0:
        next_loop_remaining_words = READING_RING_UNIT
    return completed_loops, current_loop_progress_words, progress_ratio, next_loop_remaining_words


def calendar_visual_status(
    *,
    stat_date: date,
    today: date,
    is_natural_signed: bool,
    is_makeup_signed: bool,
) -> str:
    if stat_date > today:
        return "future"
    if stat_date == today:
        if is_natural_signed:
            return "today_natural_signed"
        if is_makeup_signed:
            return "today_makeup_signed"
        return "today_pending"
    if is_natural_signed:
        return "natural_signed"
    if is_makeup_signed:
        return "makeup_signed"
    return "missed"


def crossed_milestones(previous_total: int, current_total: int) -> list[int]:
    previous = max(0, int(previous_total))
    current = max(0, int(current_total))
    return [
        milestone
        for milestone in CHARACTER_MESSAGE_MILESTONES
        if previous < milestone <= current
    ]


@dataclass(frozen=True)
class NaturalSignInAdvanceResult:
    current_natural_streak: int
    longest_natural_streak: int
    makeup_card_balance: int
    last_natural_signin_date: date
    last_rewarded_natural_streak: int
    reward_granted: bool


def compute_streak_from_signed_dates(signed_dates: set[date], anchor: date) -> int:
    """Count consecutive signed days (natural or makeup) backward from anchor."""
    streak = 0
    current = anchor
    while current in signed_dates:
        streak += 1
        current -= timedelta(days=1)
    return streak


def advance_natural_signin(
    *,
    current_natural_streak: int,
    longest_natural_streak: int,
    makeup_card_balance: int,
    last_natural_signin_date: date | None,
    last_rewarded_natural_streak: int,
    signin_date: date,
) -> NaturalSignInAdvanceResult:
    yesterday = signin_date - timedelta(days=1)
    if last_natural_signin_date == yesterday:
        new_current = max(0, int(current_natural_streak)) + 1
        new_last_rewarded = max(0, int(last_rewarded_natural_streak))
    else:
        new_current = 1
        new_last_rewarded = 0

    new_longest = max(max(0, int(longest_natural_streak)), new_current)
    new_balance = max(0, int(makeup_card_balance))
    reward_granted = False
    if new_current % 3 == 0 and new_current > new_last_rewarded:
        if new_balance < MAKEUP_CARD_MAX:
            new_balance += 1
        new_last_rewarded = new_current
        reward_granted = True

    return NaturalSignInAdvanceResult(
        current_natural_streak=new_current,
        longest_natural_streak=new_longest,
        makeup_card_balance=new_balance,
        last_natural_signin_date=signin_date,
        last_rewarded_natural_streak=new_last_rewarded,
        reward_granted=reward_granted,
    )
