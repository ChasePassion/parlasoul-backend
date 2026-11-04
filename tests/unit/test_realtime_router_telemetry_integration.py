from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.api.routers.realtime import create_realtime_session
from src.core.exceptions import APIError
from src.realtime.schemas.server_events import RealtimeSessionCreateRequest


def _request(*, chat_id: uuid.UUID, character_id: uuid.UUID, observation_id: uuid.UUID):
    return RealtimeSessionCreateRequest(
        chat_id=str(chat_id),
        character_id=str(character_id),
        observation_id=observation_id,
        sdp={"type": "offer", "sdp": "offer"},
    )


def _dependencies():
    user_id = uuid.uuid4()
    chat_id = uuid.uuid4()
    character_id = uuid.uuid4()
    observation_id = uuid.uuid4()
    runtime_session_id = "rt_0123456789abcdef"
    current_user = SimpleNamespace(id=user_id)
    chat = SimpleNamespace(id=chat_id, user_id=user_id, character_id=character_id)
    character = SimpleNamespace(id=character_id, status="ACTIVE")
    chat_repo = SimpleNamespace(get_by_id=AsyncMock(return_value=chat))
    character_repo = SimpleNamespace(get_by_id=AsyncMock(return_value=character))
    session_manager = SimpleNamespace(
        generate_session_id=Mock(return_value=runtime_session_id),
        create_session=AsyncMock(),
    )
    ice_config_service = SimpleNamespace(
        build_client_ice_servers=Mock(return_value=[]),
        credential_ttl_seconds=Mock(return_value=0),
    )
    telemetry_service = SimpleNamespace(attach_runtime_session=AsyncMock())
    recorder = SimpleNamespace(
        finalize=AsyncMock(),
        finalize_failed=AsyncMock(),
        close=AsyncMock(),
    )
    return SimpleNamespace(
        user_id=user_id,
        chat_id=chat_id,
        character_id=character_id,
        observation_id=observation_id,
        runtime_session_id=runtime_session_id,
        current_user=current_user,
        chat=chat,
        character=character,
        chat_repo=chat_repo,
        character_repo=character_repo,
        session_manager=session_manager,
        ice_config_service=ice_config_service,
        telemetry_service=telemetry_service,
        recorder=recorder,
    )


@pytest.mark.asyncio
async def test_create_realtime_session_claims_observation_before_manager_start() -> None:
    deps = _dependencies()
    session = SimpleNamespace(session_id=deps.runtime_session_id)
    answer = {"type": "answer", "sdp": "answer"}
    deps.session_manager.create_session.return_value = (session, answer)
    factory = Mock()
    factory.create.return_value = deps.recorder

    with patch(
        "src.api.routers.realtime.RealtimeTelemetryRecorderFactory",
        return_value=factory,
    ):
        response = await create_realtime_session(
            request=_request(
                chat_id=deps.chat_id,
                character_id=deps.character_id,
                observation_id=deps.observation_id,
            ),
            current_user=deps.current_user,
            chat_repo=deps.chat_repo,
            character_repo=deps.character_repo,
            session_manager=deps.session_manager,
            ice_config_service=deps.ice_config_service,
            telemetry_service=deps.telemetry_service,
        )

    deps.telemetry_service.attach_runtime_session.assert_awaited_once_with(
        observation_id=deps.observation_id,
        user_id=deps.user_id,
        chat_id=deps.chat_id,
        character_id=deps.character_id,
        rtc_session_id=deps.runtime_session_id,
    )
    factory.create.assert_called_once_with(
        observation_id=deps.observation_id,
        user_id=deps.user_id,
        runtime_session_id=deps.runtime_session_id,
    )
    deps.session_manager.create_session.assert_awaited_once_with(
        current_user=deps.current_user,
        chat=deps.chat,
        character=deps.character,
        offer={"type": "offer", "sdp": "offer"},
        session_id=deps.runtime_session_id,
        metrics_sink=deps.recorder,
    )
    deps.recorder.close.assert_not_awaited()
    assert response.status_code == 201
    assert json.loads(response.body)["data"]["session_id"] == deps.runtime_session_id


@pytest.mark.asyncio
async def test_create_realtime_session_finalizes_bound_observation_on_negotiation_failure() -> None:
    deps = _dependencies()
    deps.session_manager.create_session.side_effect = APIError(
        504,
        "external_timeout",
        "external timeout: realtime negotiation timed out",
    )
    factory = Mock()
    factory.create.return_value = deps.recorder

    with (
        patch(
            "src.api.routers.realtime.RealtimeTelemetryRecorderFactory",
            return_value=factory,
        ),
        pytest.raises(APIError, match="negotiation timed out"),
    ):
        await create_realtime_session(
            request=_request(
                chat_id=deps.chat_id,
                character_id=deps.character_id,
                observation_id=deps.observation_id,
            ),
            current_user=deps.current_user,
            chat_repo=deps.chat_repo,
            character_repo=deps.character_repo,
            session_manager=deps.session_manager,
            ice_config_service=deps.ice_config_service,
            telemetry_service=deps.telemetry_service,
        )

    deps.recorder.finalize_failed.assert_awaited_once()
    failure = deps.recorder.finalize_failed.await_args.kwargs
    assert failure["failure_stage"] == "signaling"
    assert failure["error_code"] == "negotiation_timeout"
    assert failure["duration_ms"] >= 0
    deps.recorder.close.assert_awaited_once_with()
