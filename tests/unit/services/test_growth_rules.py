from __future__ import annotations

from datetime import date

from src.services.growth_rules import (
    advance_natural_signin,
    compute_streak_from_signed_dates,
    crossed_milestones,
    ring_progress,
)


def test_compute_streak_from_anchor_when_all_consecutive() -> None:
    dates: set[date] = {
        date(2026, 4, 1),
        date(2026, 4, 2),
        date(2026, 4, 3),
        date(2026, 4, 4),
    }
    assert compute_streak_from_signed_dates(dates, date(2026, 4, 4)) == 4


def test_compute_streak_stops_at_first_gap() -> None:
    dates = {date(2026, 4, 2), date(2026, 4, 3)}
    assert compute_streak_from_signed_dates(dates, date(2026, 4, 3)) == 2


def test_compute_streak_with_mixed_natural_and_makeup_dates() -> None:
    dates = {date(2026, 4, 1), date(2026, 4, 2), date(2026, 5, 10)}
    assert compute_streak_from_signed_dates(dates, date(2026, 5, 10)) == 1


def test_compute_streak_empty_set() -> None:
    dates: set[date] = set()
    assert compute_streak_from_signed_dates(dates, date(2026, 4, 5)) == 0


def test_compute_streak_anchor_not_in_set() -> None:
    dates = {date(2026, 4, 1), date(2026, 4, 2)}
    assert compute_streak_from_signed_dates(dates, date(2026, 4, 4)) == 0


def test_advance_natural_signin_grants_makeup_card_every_third_day() -> None:
    result = advance_natural_signin(
        current_natural_streak=2,
        longest_natural_streak=2,
        makeup_card_balance=0,
        last_natural_signin_date=date(2026, 4, 4),
        last_rewarded_natural_streak=0,
        signin_date=date(2026, 4, 5),
    )

    assert result.current_natural_streak == 3
    assert result.longest_natural_streak == 3
    assert result.makeup_card_balance == 1
    assert result.last_rewarded_natural_streak == 3
    assert result.reward_granted is True


def test_crossed_milestones_detects_thresholds_with_two_message_jump() -> None:
    assert crossed_milestones(99, 101) == [100]


def test_ring_progress_resets_after_full_loop() -> None:
    completed, progress_words, progress_ratio, remaining = ring_progress(600)
    assert completed == 2
    assert progress_words == 0
    assert progress_ratio == 0
    assert remaining == 300
