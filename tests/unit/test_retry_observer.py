from __future__ import annotations

from src.memory_system.utils.retry import RetryExecutor


def test_retry_executor_emits_observer_event_before_retry() -> None:
    attempts = {"count": 0}
    events = []

    def _operation() -> str:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise ValueError("temporary upstream error")
        return "ok"

    executor = RetryExecutor(
        max_retries=2,
        base_delay=0.01,
        model="gpt-test",
        operation="chat",
        retryable_exceptions=(ValueError,),
        retry_observer=events.append,
    )

    result = executor.execute(_operation)

    assert result == "ok"
    assert attempts["count"] == 2
    assert len(events) == 1
    event = events[0]
    assert event.attempt == 1
    assert event.max_retries == 2
    assert event.delay_ms == 10
    assert event.retryable is True
    assert event.error_type == "ValueError"
    assert event.error_message == "temporary upstream error"
