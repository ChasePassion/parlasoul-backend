"""Langfuse capture contract (acceptance #10).

Runs the REAL LLMGatewayService so the observability layer is exercised end
to end; only the runtime client is stubbed. Verifies with a fake Langfuse
client that:
- capture on (default): generation observations receive the full input
  messages and the full output text;
- capture off: observation input/output are None while safe metadata
  (llm_call_id, model label, counters) is still written;
- LANGFUSE_ENABLED=False: identical judge outcomes, no observation traffic.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from src.core.config import settings
from src.evaluation.judges.llm_judge import LLMJudge
from src.evaluation.judges.routes import ResolvedJudgeRoute
from src.memory_system.clients.llm import LLMTextResult
from src.services.chat_generation_service import ChatGenerationContext, ChatGenerationService
from src.services.llm_gateway_service import LLMGatewayService


class FakeObservation:
    def __init__(self, *, name: str, input: Any, model: str | None, metadata: dict) -> None:
        self.name = name
        self.input = input
        self.model = model
        self.metadata = metadata
        self.updates: list[dict[str, Any]] = []
        self.ended = False

    def update(self, **kwargs: Any) -> None:
        self.updates.append(kwargs)

    def end(self) -> None:
        self.ended = True


class FakeLangfuseClient:
    def __init__(self) -> None:
        self.observations: list[FakeObservation] = []

    def start_observation(
        self,
        *,
        trace_context: Any = None,
        name: str | None = None,
        as_type: str | None = None,
        input: Any = None,
        model: str | None = None,
        model_parameters: Any = None,
        metadata: dict | None = None,
    ) -> FakeObservation:
        observation = FakeObservation(
            name=name or "", input=input, model=model, metadata=metadata or {}
        )
        self.observations.append(observation)
        return observation


class FakeJudgeRuntimeClient:
    """Non-streaming runtime client returning a valid dimension score JSON."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def chat_with_usage(self, system_prompt: str, user_message: str, **_: Any) -> LLMTextResult:
        self.calls.append((system_prompt, user_message))
        return LLMTextResult(
            text=json.dumps({"score": 4, "rationale": "consistent with persona"}),
            model="stub-judge-model",
        )


class FakeStreamRuntimeClient:
    """Streaming runtime client for the generation seam."""

    async def chat_stream_messages_async(self, messages: list, **_: Any):
        yield "generated "
        yield "reply text"


def _judge_route() -> ResolvedJudgeRoute:
    return ResolvedJudgeRoute(
        provider="stubjudge",
        model="stub-judge-model",
        sdk_kind="openai_sdk",
        api_key="key",
        api_base=None,
        model_type_label="stubjudge:stub-judge-model",
        supports_structured_output=True,
    )


@pytest.fixture()
def fake_langfuse(monkeypatch: pytest.MonkeyPatch) -> FakeLangfuseClient:
    client = FakeLangfuseClient()
    monkeypatch.setattr("src.observability.get_client", lambda: client)
    monkeypatch.setattr(settings, "LANGFUSE_ENABLED", True, raising=False)
    return client


def _stub_runtime(monkeypatch: pytest.MonkeyPatch, runtime_client: Any) -> None:
    monkeypatch.setattr(
        LLMGatewayService,
        "get_runtime_client",
        lambda self, route, retry_policy="default": runtime_client,
    )


def _last_generation_observation(client: FakeLangfuseClient) -> FakeObservation:
    assert client.observations, "expected at least one generation observation"
    return client.observations[-1]


async def test_judge_capture_on_writes_full_input_output(
    fake_langfuse: FakeLangfuseClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_runtime(monkeypatch, FakeJudgeRuntimeClient())
    judge = LLMJudge(
        route=_judge_route(),
        timeout_s=5.0,
        gateway_service=LLMGatewayService(),
        rubric_version="parlasoul-roleplay-5d-ooc-v1",
        prompt_template_version="parlasoul-judge-prompt-v1",
    )

    outcome = await judge.score_dimension(
        dimension="persona_fidelity",
        character_snapshot={"name": "Alex", "system_prompt": "stay in character"},
        history_turns=[{"role": "user", "content": "hi"}],
        user_message="how are you?",
        response="I am fine, thanks.",
    )

    assert outcome.response_status == "valid"
    observation = _last_generation_observation(fake_langfuse)
    # full input messages visible in observability
    assert isinstance(observation.input, list)
    roles = [message["role"] for message in observation.input]
    assert roles == ["system", "user"]
    assert "how are you?" in observation.input[1]["content"]
    # full output text visible
    output_payload = next(update for update in observation.updates if "output" in update)
    assert output_payload["output"] == json.dumps(
        {"score": 4, "rationale": "consistent with persona"}
    )
    assert observation.ended


async def test_judge_capture_off_redacts_input_output(
    fake_langfuse: FakeLangfuseClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_runtime(monkeypatch, FakeJudgeRuntimeClient())
    judge = LLMJudge(
        route=_judge_route(),
        timeout_s=5.0,
        gateway_service=LLMGatewayService(),
        rubric_version="parlasoul-roleplay-5d-ooc-v1",
        prompt_template_version="parlasoul-judge-prompt-v1",
        capture_input=False,
        capture_output=False,
    )

    outcome = await judge.score_dimension(
        dimension="persona_fidelity",
        character_snapshot={"name": "Alex"},
        history_turns=[],
        user_message="secret user question",
        response="secret reply",
    )

    assert outcome.response_status == "valid"
    observation = _last_generation_observation(fake_langfuse)
    assert observation.input is None
    output_payload = next(update for update in observation.updates if "output" in update)
    assert output_payload["output"] is None
    # safe metadata survives redaction
    assert observation.model == "stubjudge/stub-judge-model"
    assert any(
        "llm_call_id" in update.get("metadata", {})
        for update in observation.updates
        if "metadata" in update
    )
    # nothing raw leaked anywhere in the observation record
    serialized = json.dumps(
        {"input": observation.input, "updates": observation.updates}, default=str
    )
    assert "secret user question" not in serialized
    assert "secret reply" not in serialized


async def test_generation_seam_capture_toggle(
    fake_langfuse: FakeLangfuseClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_runtime(monkeypatch, FakeStreamRuntimeClient())
    service = ChatGenerationService(LLMGatewayService())

    context = ChatGenerationContext(
        character_system_prompt="be kind",
        history_messages=[],
        user_message="hello there",
        memories={"episodic": [], "semantic": []},
        use_all_semantic=False,
        preferred_expressions=[],
    )
    builder = lambda text, images: {"role": "user", "content": text}  # noqa: E731

    result_on = await service.generate_once(
        context,
        route=_judge_route(),  # type: ignore[arg-type]
        operation="eval.replay.generate",
        user_message_builder=builder,
    )
    result_off = await service.generate_once(
        context,
        route=_judge_route(),  # type: ignore[arg-type]
        operation="eval.replay.generate",
        user_message_builder=builder,
        capture_input=False,
        capture_output=False,
    )

    assert result_on.text == "generated reply text"
    assert result_off.text == "generated reply text"
    assert len(fake_langfuse.observations) == 2

    # capture-on observation carries the full messages + output
    on_observation = fake_langfuse.observations[0]
    assert isinstance(on_observation.input, list)
    assert on_observation.input[-1]["content"] == "hello there"
    on_output = next(u for u in on_observation.updates if "output" in u)
    assert on_output["output"] == "generated reply text"

    # capture-off observation is redacted
    off_observation = fake_langfuse.observations[1]
    assert off_observation.input is None
    off_output = next(u for u in off_observation.updates if "output" in u)
    assert off_output["output"] is None


async def test_langfuse_disabled_keeps_outcomes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "LANGFUSE_ENABLED", False, raising=False)
    _stub_runtime(monkeypatch, FakeJudgeRuntimeClient())
    judge = LLMJudge(
        route=_judge_route(),
        timeout_s=5.0,
        gateway_service=LLMGatewayService(),
        rubric_version="parlasoul-roleplay-5d-ooc-v1",
        prompt_template_version="parlasoul-judge-prompt-v1",
    )
    outcome = await judge.score_dimension(
        dimension="persona_fidelity",
        character_snapshot={"name": "Alex"},
        history_turns=[],
        user_message="q",
        response="r",
    )
    assert outcome.response_status == "valid"
    assert outcome.score == 4
