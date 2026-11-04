"""3-gram repetition metric vectors (evaluation.md §3.3)."""

from __future__ import annotations

from src.evaluation.judges.rule_judge import (
    normalize_for_repetition,
    repetition_3gram_rate,
    tokenize,
)


def test_normalization_nfkc_casefold_whitespace() -> None:
    assert normalize_for_repetition("Ｈello\n  WORLD\t") == "hello world"


def test_empty_or_short_input_is_no_data() -> None:
    assert repetition_3gram_rate("").status == "no_data"
    assert repetition_3gram_rate("one two").status == "no_data"
    result = repetition_3gram_rate("one two")
    assert result.rate is None
    assert result.total_3grams == 0


def test_unique_trigrams_rate_zero() -> None:
    result = repetition_3gram_rate("a b c")
    assert result.status == "ok"
    assert result.total_3grams == 1
    assert result.repeated_3grams == 0
    assert result.rate == 0.0


def test_repeated_trigrams_counted_once_per_extra_occurrence() -> None:
    # tokens: a b a b a b -> trigrams (a,b,a)x2 (b,a,b)x2 -> repeated = 2, total = 4
    result = repetition_3gram_rate("a b a b a b")
    assert result.total_3grams == 4
    assert result.repeated_3grams == 2
    assert result.rate == 0.5


def test_case_variants_collapse() -> None:
    # "A a A A" casefolds to four "a" tokens -> 2 identical trigrams -> 1 repeat
    result = repetition_3gram_rate("A a A A")
    assert result.total_3grams == 2
    assert result.repeated_3grams == 1
    assert result.rate == 0.5


def test_tokenize_unicode_word_runs() -> None:
    tokens = tokenize("你好，世界！hello-world 42")
    assert tokens == ["你好", "世界", "hello", "world", "42"]
