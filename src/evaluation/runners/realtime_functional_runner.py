"""RealtimeFunctionalRunner: execute scenario manifests on a mock transport.

Real calls are never started from the CLI. The mock transport simulates
action timing deterministically from the scenario's network_params so the
assertion machinery, telemetry shaping and sanitized reporting are exercised
end to end. Raw audio/transcripts never enter any field.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from src.core.error_codes import ErrorCode
from src.evaluation.datasets.schema import RealtimeScenario
from src.evaluation.runners.base import CaseExecutionContext, CaseOutcome, make_safe_error_detail

logger = logging.getLogger(__name__)

_BASE_TTFA_MS = 420
_BARGE_IN_STOP_MS = 140
_NEXT_TURN_AFTER_BARGE_MS = 520
_RECONNECT_MS = 650


@dataclass
class SimEvent:
    event: str
    offset_ms: int


@dataclass
class MockRealtimeTransport:
    """Deterministic functional stand-in for the realtime pipeline."""

    rtt_ms: int = 30
    jitter_ms: int = 5
    packet_loss_ratio: float = 0.0
    events: list[SimEvent] = field(default_factory=list)
    reconnect_count: int = 0

    def run(self, actions: list[Any]) -> None:
        clock = 0
        assistant_active = False
        for action in actions:
            action_type = action.type
            at_ms = action.at_ms
            duration = action.duration_ms or 0
            if at_ms is not None:
                clock = max(clock, at_ms)
            if action_type == "connect":
                self.events.append(SimEvent("session_started", clock))
            elif action_type == "send_user_audio_fixture":
                self.events.append(SimEvent("user_audio_sent", clock))
                ttfa = _BASE_TTFA_MS + self.rtt_ms + self.jitter_ms
                if self.packet_loss_ratio > 0:
                    ttfa += int(200 * self.packet_loss_ratio)
                self.events.append(SimEvent("assistant_audio_started", clock + ttfa))
                assistant_active = True
                clock += ttfa
            elif action_type == "barge_in":
                self.events.append(SimEvent("user_barge_in", clock))
                if assistant_active:
                    self.events.append(
                        SimEvent("assistant_audio_stopped", clock + _BARGE_IN_STOP_MS)
                    )
                    assistant_active = False
                self.events.append(
                    SimEvent("next_turn_started", clock + _NEXT_TURN_AFTER_BARGE_MS)
                )
            elif action_type == "silence":
                clock += duration
            elif action_type == "simulate_network_drop":
                self.events.append(SimEvent("network_dropped", clock))
                assistant_active = False
            elif action_type == "reconnect":
                self.reconnect_count += 1
                self.events.append(SimEvent("reconnected", clock + _RECONNECT_MS))
                clock += _RECONNECT_MS
            elif action_type == "disconnect":
                self.events.append(SimEvent("session_ended", clock))
                assistant_active = False

    def metrics(self) -> dict[str, Any]:
        started = next((e for e in self.events if e.event == "assistant_audio_started"), None)
        audio_sent = next((e for e in self.events if e.event == "user_audio_sent"), None)
        ttfa_ms = (started.offset_ms - audio_sent.offset_ms) if started and audio_sent else None
        return {
            "ttfa_ms": ttfa_ms,
            "reconnect_count": self.reconnect_count,
            "webrtc_rtt_ms": self.rtt_ms,
            "jitter_ms": self.jitter_ms,
            "packet_loss_ratio": self.packet_loss_ratio,
        }


def check_assertions(scenario: RealtimeScenario, events: list[SimEvent]) -> dict[str, Any]:
    offsets = {event.event: event.offset_ms for event in events}
    passed = 0
    failed = 0
    details: list[dict[str, Any]] = []
    for assertion in scenario.assertions:
        target_offset = offsets.get(assertion.event)
        anchor_offset = offsets.get(assertion.anchor or "", 0)
        if assertion.eventually or assertion.within_ms is None:
            ok = target_offset is not None
        else:
            ok = (
                target_offset is not None
                and target_offset - anchor_offset <= assertion.within_ms
            )
        if ok:
            passed += 1
        else:
            failed += 1
        details.append(
            {
                "event": assertion.event,
                "passed": ok,
            }
        )
    return {"passed": passed, "failed": failed, "details": details}


class RealtimeFunctionalRunner:
    runner_name = "realtime_functional"

    async def run_case(
        self, scenario: RealtimeScenario, context: CaseExecutionContext
    ) -> CaseOutcome:
        outcome = CaseOutcome(
            case_id=scenario.scenario_id,
            runner=self.runner_name,
            status="failed",
            response_status="runner_error",
            input_ref=f"scenario:{scenario.scenario_id}",
            output_ref=f"scenario:{scenario.scenario_id}",
        )

        try:
            params = scenario.network_params
            transport = MockRealtimeTransport(
                rtt_ms=params.rtt_ms if params and params.rtt_ms is not None else 30,
                jitter_ms=params.jitter_ms if params and params.jitter_ms is not None else 5,
                packet_loss_ratio=(
                    params.packet_loss_ratio
                    if params and params.packet_loss_ratio is not None
                    else 0.0
                ),
            )
            transport.run(scenario.actions)
        except Exception as exc:
            outcome.error_code = ErrorCode.INTERNAL_ERROR
            outcome.error_detail_safe = make_safe_error_detail(
                "scenario_execution_failed&kind={kind}", kind=type(exc).__name__
            )
            outcome.finish()
            return outcome

        assertions = check_assertions(scenario, transport.events)
        scenario_passed = assertions["failed"] == 0
        outcome.status = "completed"
        outcome.response_status = "valid"
        outcome.rule_metrics = {
            "scenario_passed": scenario_passed,
            "expected_result": scenario.expected_result,
            "assertions_passed": assertions["passed"],
            "assertions_failed": assertions["failed"],
            "events": [
                {"event": event.event, "offset_ms": event.offset_ms}
                for event in transport.events
            ],
            "metrics": transport.metrics(),
            "requested_metrics": list(scenario.metrics),
        }
        outcome.finish()
        return outcome
