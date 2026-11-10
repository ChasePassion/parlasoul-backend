"""3-gram repetition metric vectors (evaluation.md §3.3)."""

from __future__ import annotations

from src.evaluation.judges.rule_judge import (
    repetition_3gram_rate,
)


def test_repeated_trigrams_counted_once_per_extra_occurrence() -> None:
    # tokens: a b a b a b -> trigrams (a,b,a)x2 (b,a,b)x2 -> repeated = 2, total = 4
    result = repetition_3gram_rate("a b a b a b")
    assert result.total_3grams == 4
    assert result.repeated_3grams == 2
    assert result.rate == 0.5




