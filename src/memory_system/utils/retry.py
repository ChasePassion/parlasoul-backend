"""Retry utilities for resilient upstream calls."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, Callable
from typing import TypeVar

from src.observability import LLMRetryEvent, LLMRetryObserver

from ..exceptions import LLMCallError

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RetryExecutor:
    """Unified retry executor for sync, async, and streaming operations."""

    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = 1.0,
        model: str = "",
        operation: str = "",
        retryable_exceptions: tuple[type[Exception], ...] = (Exception,),
        retry_observer: LLMRetryObserver | None = None,
    ):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.model = model
        self.operation = operation
        self.retryable_exceptions = retryable_exceptions
        self.retry_observer = retry_observer
        self._last_error: Exception | None = None

    def _calculate_delay(self, attempt: int) -> float:
        return self.base_delay * (2**attempt)

    def _should_retry(self, error: Exception) -> bool:
        return isinstance(error, self.retryable_exceptions)

    def _notify_retry(self, attempt: int, error: Exception, delay_s: float) -> None:
        if self.retry_observer is None:
            return
        self.retry_observer(
            LLMRetryEvent(
                attempt=attempt + 1,
                max_retries=self.max_retries,
                delay_ms=int(delay_s * 1000),
                retryable=self._should_retry(error),
                error_type=type(error).__name__,
                error_message=str(error).strip(),
            )
        )

    def _log_retry(self, attempt: int, error: Exception, *, is_async: bool = False) -> None:
        mode = "async " if is_async else ""
        logger.debug(
            "%s%s attempt %s/%s for model %s failed: %s",
            mode,
            self.operation or "API",
            attempt + 1,
            self.max_retries,
            self.model,
            error,
        )

    def _raise_final_error(self) -> None:
        raise LLMCallError(self.model, self.max_retries, self._last_error)

    def execute(self, operation_fn: Callable[[], T]) -> T:
        for attempt in range(self.max_retries):
            try:
                return operation_fn()
            except Exception as exc:
                if not self._should_retry(exc):
                    raise
                self._last_error = exc
                self._log_retry(attempt, exc)
                if attempt < self.max_retries - 1:
                    delay_s = self._calculate_delay(attempt)
                    self._notify_retry(attempt, exc, delay_s)
                    time.sleep(delay_s)
        self._raise_final_error()

    async def stream_async(
        self,
        stream_fn: Callable[[], AsyncGenerator[T, None]],
    ) -> AsyncGenerator[T, None]:
        for attempt in range(self.max_retries):
            try:
                async for item in stream_fn():
                    yield item
                return
            except Exception as exc:
                if not self._should_retry(exc):
                    raise
                self._last_error = exc
                self._log_retry(attempt, exc, is_async=True)
                if attempt < self.max_retries - 1:
                    delay_s = self._calculate_delay(attempt)
                    self._notify_retry(attempt, exc, delay_s)
                    await asyncio.sleep(delay_s)
        self._raise_final_error()
