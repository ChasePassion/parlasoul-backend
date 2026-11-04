from __future__ import annotations

from src.services.growth_tokenizer import count_english_words, sum_english_words


def test_count_english_words_ignores_chinese_and_counts_contractions() -> None:
    assert count_english_words("你好 hello don't 123 world") == 3


def test_sum_english_words_aggregates_multiple_texts() -> None:
    assert sum_english_words(["Hello there", None, "再见 world"]) == 3
