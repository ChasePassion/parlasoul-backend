"""Deterministic rule-based metrics (no LLM).

Repetition Rate (evaluation.md §3.3, first version): single-candidate 3-gram
ratio over Unicode NFKC + casefold + whitespace-normalized letter/digit
tokens. Denominator zero returns ``no_data`` — never 0.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def normalize_for_repetition(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text or "")
    normalized = normalized.casefold()
    return " ".join(normalized.split())


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(normalize_for_repetition(text))


@dataclass(frozen=True)
class RepetitionResult:
    status: str  # "ok" | "no_data"
    total_3grams: int
    repeated_3grams: int
    rate: float | None


def repetition_3gram_rate(text: str) -> RepetitionResult:
    tokens = tokenize(text)
    total = max(len(tokens) - 2, 0)
    if total == 0:
        return RepetitionResult(status="no_data", total_3grams=0, repeated_3grams=0, rate=None)

    counts = Counter(tuple(tokens[i : i + 3]) for i in range(len(tokens) - 2))
    repeated = sum(max(count - 1, 0) for count in counts.values())
    return RepetitionResult(
        status="ok",
        total_3grams=total,
        repeated_3grams=repeated,
        rate=repeated / total,
    )
