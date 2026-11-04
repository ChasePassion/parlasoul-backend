"""English-word counting helpers for Phase 5 growth metrics."""

from __future__ import annotations

import re
from collections.abc import Iterable

_ENGLISH_WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")


def count_english_words(text: str | None) -> int:
    """Count English words only.

    Rules:
    - ASCII letters count as words
    - apostrophe-linked contractions count as one word
    - Chinese, numbers, and punctuation do not count
    """

    if not text:
        return 0
    return len(_ENGLISH_WORD_RE.findall(text))


def sum_english_words(texts: Iterable[str | None]) -> int:
    return sum(count_english_words(text) for text in texts)
