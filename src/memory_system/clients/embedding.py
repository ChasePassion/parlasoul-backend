"""Embedding client using OpenAI-compatible API (DashScope)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from openai import OpenAI

from ..utils.retry import RetryExecutor

logger = logging.getLogger(__name__)

_RETRYABLE_EXCEPTIONS = (Exception,)

_DEFAULT_DIMENSIONS = 2048


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: list[list[float]]
    model: str
    usage_details: dict[str, Any] | None = None


class EmbeddingClient:
    """Embedding client backed by an OpenAI-compatible endpoint."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        dimensions: int = _DEFAULT_DIMENSIONS,
    ):
        if not (api_key or "").strip():
            raise ValueError("embedding api_key is required")

        self._api_key = api_key.strip()
        self._base_url = (base_url or "").strip() or None
        self._model = (model or "").strip()
        self._dim = dimensions
        self._max_retries = 3
        self._base_delay = 1.0
        self._client = OpenAI(
            api_key=self._api_key,
            base_url=self._base_url,
            timeout=20.0,
        )

    @property
    def dim(self) -> int:
        return self._dim

    def encode(self, texts: list[str]) -> list[list[float]]:
        return self.encode_with_usage(texts).vectors

    def encode_with_usage(self, texts: list[str]) -> EmbeddingResult:
        if not texts:
            return EmbeddingResult(vectors=[], model=self._model)

        executor = RetryExecutor(
            max_retries=self._max_retries,
            base_delay=self._base_delay,
            model=self._model,
            operation="embedding",
            retryable_exceptions=_RETRYABLE_EXCEPTIONS,
        )

        def do_encode() -> EmbeddingResult:
            response = self._client.embeddings.create(
                model=self._model,
                input=texts,
                dimensions=self._dim,
            )
            vectors = [item.embedding for item in response.data]
            if not vectors:
                raise ValueError("empty embedding response")

            usage = getattr(response, "usage", None)
            usage_details = None
            if usage is not None:
                usage_details = {}
                if hasattr(usage, "prompt_tokens") and usage.prompt_tokens is not None:
                    usage_details["input"] = usage.prompt_tokens
                if hasattr(usage, "total_tokens") and usage.total_tokens is not None:
                    usage_details["total"] = usage.total_tokens
                if not usage_details:
                    usage_details = None

            return EmbeddingResult(
                vectors=vectors,
                model=self._model,
                usage_details=usage_details,
            )

        return executor.execute(do_encode)
