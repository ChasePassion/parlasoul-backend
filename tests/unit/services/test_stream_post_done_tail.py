from __future__ import annotations

import asyncio

import pytest

from src.schemas.learning import ReplyCardCore, ReplyCardPhrase, ReplySuggestion
from src.services.stream_post_done_tail import (
    PostDoneTailTasks,
    ReplyCardCoreReady,
    start_post_done_tail_tasks,
)


@pytest.mark.asyncio
async def test_post_done_tail_tasks_emit_suggestions_with_candidate_id() -> None:
    tasks = start_post_done_tail_tasks(
        assistant_candidate_id="cand-1",
        suggestions_coro=asyncio.sleep(
            0,
            result=[
                ReplySuggestion(type="relationship", en="How are you?", zh="你好吗？"),
            ],
        ),
        logger=__import__("logging").getLogger(__name__),
        suggestions_error_log_message="suggestions failed",
    )

    try:
        item = await tasks.wait_for_item(0.2)
        assert item == {
            "type": "reply_suggestions",
            "assistant_candidate_id": "cand-1",
            "suggestions": [
                {
                    "type": "relationship",
                    "en": "How are you?",
                    "zh": "你好吗？",
                }
            ],
        }
    finally:
        await tasks.aclose()


@pytest.mark.asyncio
async def test_post_done_tail_tasks_emit_reply_card_error_when_card_is_invalid() -> None:
    tasks = start_post_done_tail_tasks(
        assistant_candidate_id="cand-2",
        suggestions_coro=asyncio.sleep(0, result=[]),
        logger=__import__("logging").getLogger(__name__),
        suggestions_error_log_message="suggestions failed",
        reply_card_core_coro=asyncio.sleep(0, result=None),
        reply_card_error_log_message="reply card failed",
    )

    try:
        items: list[dict[str, object] | ReplyCardCoreReady] = []
        while tasks.pending:
            item = await tasks.wait_for_item(0.2)
            if item is not None:
                items.append(item)

        reply_card_errors = [
            item
            for item in items
            if isinstance(item, dict) and item.get("type") == "reply_card_error"
        ]
        assert len(reply_card_errors) == 1
        assert reply_card_errors[0]["assistant_candidate_id"] == "cand-2"
        assert reply_card_errors[0]["code"] == "reply_card_generation_failed"
    finally:
        await tasks.aclose()


@pytest.mark.asyncio
async def test_post_done_tail_tasks_emit_reply_card_core_ready_item() -> None:
    card_core = ReplyCardCore(
        surface="Nice to meet you",
        zh="很高兴见到你",
        key_phrases=[
            ReplyCardPhrase(
                surface="nice to meet you",
                ipa_us="/naɪs tə miːt juː/",
                zh="很高兴见到你",
            ),
            ReplyCardPhrase(
                surface="meet you",
                ipa_us="/miːt juː/",
                zh="见到你",
            ),
        ],
    )
    tasks: PostDoneTailTasks = start_post_done_tail_tasks(
        assistant_candidate_id="cand-3",
        suggestions_coro=asyncio.sleep(0, result=[]),
        logger=__import__("logging").getLogger(__name__),
        suggestions_error_log_message="suggestions failed",
        reply_card_core_coro=asyncio.sleep(0, result=card_core),
        reply_card_error_log_message="reply card failed",
    )

    try:
        items: list[dict[str, object] | ReplyCardCoreReady] = []
        while tasks.pending:
            item = await tasks.wait_for_item(0.2)
            if item is not None:
                items.append(item)

        assert any(
            isinstance(item, ReplyCardCoreReady)
            and item.assistant_candidate_id == "cand-3"
            and item.card_core == card_core
            for item in items
        )
    finally:
        await tasks.aclose()
