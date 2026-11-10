"""LLMJudge: strict parsing, timeout, single attempt, capture disabled."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from src.evaluation.judges.llm_judge import LLMJudge
from src.evaluation.judges.routes import ResolvedJudgeRoute

_CONTEXT = {
    "character_snapshot": {
        "name": "Alex",
        "description": "companion",
        "system_prompt": "stay in character",
        "facts": [],
        "relationship_state": [],
    },
    "history_turns": [{"role": "user", "content": "hi"}],
    "user_message": "how are you?",
}


def _judge(
    generate_text,
    *,
    timeout_s: float = 5.0,
    capture_input: bool = True,
    capture_output: bool = True,
) -> LLMJudge:
    class _Gateway:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def generate_text(self, **kwargs: Any) -> str:
            self.calls.append(kwargs)
            return await generate_text(**kwargs)

    gateway = _Gateway()
    judge = LLMJudge(
        route=ResolvedJudgeRoute(
            provider="deepseek",
            model="deepseek-v4-flash",
            sdk_kind="openai_sdk",
            api_key="key",
            api_base=None,
            model_type_label="deepseek:deepseek-v4-flash",
            supports_structured_output=True,
        ),
        timeout_s=timeout_s,
        gateway_service=gateway,  # type: ignore[arg-type]
        rubric_version="parlasoul-roleplay-5d-ooc-v1",
        prompt_template_version="parlasoul-judge-prompt-v1",
        capture_input=capture_input,
        capture_output=capture_output,
    )
    return judge, gateway  # type: ignore[return-value]


async def test_valid_dimension_score() -> None:
    async def ok(**_: Any) -> str:
        return json.dumps({"score": 4, "rationale": "consistent with persona"})

    judge, gateway = _judge(ok)
    outcome = await judge.score_dimension(dimension="persona_fidelity", response="reply", **_CONTEXT)

    assert outcome.response_status == "valid"
    assert outcome.score == 4
    assert outcome.upstream_attempts == 1
    call = gateway.calls[0]
    assert call["retry_policy"] == "none"
    # capture defaults on so Judge prompts/replies stay inspectable
    assert call["capture_input"] is True
    assert call["capture_output"] is True
    assert call["operation"] == "eval.judge.score"




@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not json",
        '{"score": 9, "rationale": "x"}',  # out of range
        '{"score": 4, "rationale": "x", "evil": 1}',  # extra field
        "[1, 2, 3]",  # not an object
        '{"score": "4"}',  # wrong type
        '```json\n{"score": 4, "rationale": "x"}\n```',  # fenced -> strict fails
    ],
)
async def test_invalid_responses_are_parse_failed(raw: str) -> None:
    async def emit(**_: Any) -> str:
        return raw

    judge, _ = _judge(emit)
    outcome = await judge.score_dimension(dimension="persona_fidelity", response="reply", **_CONTEXT)

    assert outcome.response_status == "judge_parse_failed"
    assert outcome.score is None
    assert outcome.error_code == "validation_failed"


async def test_timeout_is_judge_timeout() -> None:
    async def slow(**_: Any) -> str:
        await asyncio.sleep(5)
        return "{}"

    judge, _ = _judge(slow, timeout_s=0.01)
    outcome = await judge.score_dimension(dimension="persona_fidelity", response="reply", **_CONTEXT)

    assert outcome.response_status == "judge_timeout"
    assert outcome.error_code == "external_timeout"




async def test_pairwise_compare_returns_position_winner() -> None:
    async def emit(**_: Any) -> str:
        return json.dumps({"winner": "response_2", "rationale": "more in character"})

    judge, gateway = _judge(emit)
    outcome = await judge.compare(response_1="A", response_2="B", **_CONTEXT)

    assert outcome.response_status == "valid"
    assert outcome.winner_position == "response_2"
    call = gateway.calls[0]
    assert call["operation"] == "eval.judge.compare"
    # blind prompt must not leak variant identity markers
    assert "variant_ref" not in call["user_message"]
    assert "generation_provider" not in call["user_message"]


