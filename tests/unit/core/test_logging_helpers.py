from __future__ import annotations

import logging

import src.observability as observability_module
from src.core.logging import ContextFilter, HumanFriendlyFormatter
from src.observability import (
    _NOOP_CLIENT,
    LLMCallRequest,
    _normalize_propagated_metadata,
    bind_llm_generation_context,
    start_llm_generation,
)


def _record(message: str, *, level: int = logging.ERROR) -> logging.LogRecord:
    return logging.LogRecord(
        name="src.observability",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


def test_human_friendly_formatter_keeps_stable_order() -> None:
    record = _record("Feedback card generation failed.")
    record.log_module = "llm"
    record.event = "llm.request.error"
    record.llm_call_id = "call-123"
    record.operation = "learning.feedback_card.generate"
    record.model = "dashscope/qwen3.5-flash"

    output = HumanFriendlyFormatter().format(record)

    assert "[ERROR] [llm] Feedback card generation failed." in output
    assert "[event=llm.request.error]" in output
    assert "[llm_call_id=call-123]" in output
    assert "[operation=learning.feedback_card.generate]" in output
    assert "[model=dashscope/qwen3.5-flash]" in output
    assert output.index("[event=llm.request.error]") < output.index("[llm_call_id=call-123]")


def test_human_friendly_formatter_omits_missing_fields() -> None:
    record = _record("Only summary.")
    record.log_module = "llm"

    output = HumanFriendlyFormatter().format(record)

    assert "[ERROR] [llm] Only summary." in output
    assert "[chat_id=" not in output
    assert "[llm_call_id=" not in output


def test_context_filter_injects_llm_generation_context(monkeypatch) -> None:
    monkeypatch.setattr(observability_module, "get_client", lambda: _NOOP_CLIENT)
    handle = start_llm_generation(
        LLMCallRequest(
            operation="chat.reply.stream",
            route_role="chat",
            provider="openrouter",
            model="openai/gpt-4.1-mini",
            sdk_kind="pydantic_ai",
            user_id="user-1",
            chat_id="chat-1",
            turn_id="turn-1",
            candidate_id="candidate-1",
            character_id="character-1",
        )
    )

    record = _record("context-bound", level=logging.INFO)
    with bind_llm_generation_context(handle):
        ContextFilter().filter(record)

    assert record.llm_call_id == handle.llm_call_id
    assert record.operation == "chat.reply.stream"
    assert record.chat_id == "chat-1"
    assert record.turn_id == "turn-1"


def test_normalize_propagated_metadata_stringifies_non_string_values() -> None:
    normalized = _normalize_propagated_metadata(
        {
            "query_length": 42,
            "memory_ids_count": 2,
            "flag": True,
            "skip": None,
        }
    )

    assert normalized == {
        "query_length": "42",
        "memory_ids_count": "2",
        "flag": "True",
    }
