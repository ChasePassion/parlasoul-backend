"""Process-local realtime session manager."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
import uuid
from typing import Any

from src.core.exceptions import APIError
from src.db.models import Character, Chat, User
from src.realtime.bridge.event_bridge import EventBridge
from src.realtime.bridge.subtitle_bridge import SubtitleBridge
from src.realtime.pipeline.bus import RealtimeEventBus
from src.realtime.pipeline.interrupt_manager import InterruptManager
from src.realtime.pipeline.turn_commit_gate import TurnCommitGateConfig
from src.realtime.rtc.gateway import build_peer
from src.realtime.session.metrics import RealtimeSessionMetricsSink
from src.realtime.session.models import RealtimeSession, SessionConfig

logger = logging.getLogger(__name__)
_SESSION_ID_PATTERN = re.compile(r"^rt_[0-9a-f]{16}$")


class RealtimeSessionManager:
    def __init__(
        self,
        *,
        dialog_service,
        turn_service,
        voice_runtime,
        ice_config_service,
        vad_model,
        settings,
    ) -> None:
        self._dialog_service = dialog_service
        self._turn_service = turn_service
        self._voice_runtime = voice_runtime
        self._ice_config_service = ice_config_service
        self._vad_model = vad_model
        self._settings = settings
        self._sessions: dict[str, RealtimeSession] = {}
        self._reserved_session_ids: set[str] = set()
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._disconnect_tasks: dict[str, asyncio.Task[None]] = {}
        self._disconnect_grace_seconds = 8.0

    async def startup(self) -> None:
        self._vad_model.warmup(sample_rate=self._settings.REALTIME_VAD_SAMPLE_RATE)
        public_urls = self._settings.realtime_ice_public_urls
        backend_urls = self._settings.realtime_ice_backend_urls
        has_turn = any(
            str(url).lower().startswith(("turn:", "turns:"))
            for url in (*public_urls, *backend_urls)
        )
        logger.info(
            "RealtimeSessionManager started: public_ice_server_count=%s "
            "backend_ice_server_count=%s has_turn=%s negotiation_timeout_seconds=%.3f",
            len(public_urls),
            len(backend_urls),
            has_turn,
            float(self._settings.REALTIME_NEGOTIATION_TIMEOUT_SECONDS),
        )
        if not public_urls:
            logger.warning("Realtime public ICE URL list is empty")
        if not backend_urls:
            logger.warning("Realtime backend ICE URL list is empty; aiortc will use host candidates only")

    async def shutdown(self) -> None:
        disconnect_tasks = list(self._disconnect_tasks.values())
        self._disconnect_tasks.clear()
        for task in disconnect_tasks:
            task.cancel()
        if disconnect_tasks:
            await asyncio.gather(*disconnect_tasks, return_exceptions=True)
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            await self._finalize_session_metrics(
                session,
                status="failed",
                failure_stage="unknown",
                error_code="server_shutdown",
            )
            await session.close()
        current = asyncio.current_task()
        remaining = [task for task in self._tasks if task is not current and not task.done()]
        for task in remaining:
            task.cancel()
        if remaining:
            await asyncio.gather(*remaining, return_exceptions=True)
        logger.info("RealtimeSessionManager stopped")

    @staticmethod
    def generate_session_id() -> str:
        """Return an opaque process-session identifier safe for telemetry binding."""

        return f"rt_{uuid.uuid4().hex[:16]}"

    async def create_session(
        self,
        *,
        current_user: User,
        chat: Chat,
        character: Character,
        offer: dict,
        session_id: str | None = None,
        metrics_sink: RealtimeSessionMetricsSink | None = None,
    ) -> tuple[RealtimeSession, dict]:
        started_at = time.perf_counter()
        if chat.user_id != current_user.id:
            raise APIError(
                status_code=403,
                code="forbidden",
                message=f"authorization failed: chat is not accessible; chat_id={chat.id}",
            )
        if chat.character_id != character.id:
            raise APIError(
                status_code=409,
                code="resource_conflict",
                message="resource conflict: chat and character mismatch",
            )

        resolved_session_id = session_id or self.generate_session_id()
        if not _SESSION_ID_PATTERN.fullmatch(resolved_session_id):
            raise APIError(
                status_code=422,
                code="invalid_param",
                message="invalid parameter: realtime session_id format is invalid",
            )
        session_id = resolved_session_id
        peer, pacer = build_peer(
            session_id=session_id,
            configuration=self._ice_config_service.build_server_rtc_configuration(
                user_id=current_user.id
            ),
            pacer_prebuffer_ms=self._settings.REALTIME_AUDIO_PACER_PREBUFFER_MS,
            pacer_recovery_prebuffer_ms=getattr(
                self._settings,
                "REALTIME_AUDIO_PACER_RECOVERY_PREBUFFER_MS",
                60,
            ),
            pacer_fadeout_ms=self._settings.REALTIME_AUDIO_PACER_FADEOUT_MS,
            pacer_max_buffer_ms=getattr(
                self._settings,
                "REALTIME_AUDIO_PACER_MAX_BUFFER_MS",
                2000,
            ),
        )
        bus = RealtimeEventBus()
        event_bridge = EventBridge(bus=bus, send_json=peer.send_json)
        subtitle_bridge = SubtitleBridge(bus)
        interrupt_manager = InterruptManager(
            bus=bus,
            audio_pacer=pacer,
            vad_stream=self._vad_model.create_stream(
                sample_rate=self._settings.REALTIME_VAD_SAMPLE_RATE
            ),
            sample_rate=self._settings.REALTIME_VAD_SAMPLE_RATE,
            vad_threshold=self._settings.REALTIME_VAD_THRESHOLD,
            vad_end_threshold_delta=self._settings.REALTIME_VAD_END_THRESHOLD_DELTA,
            silence_ms=self._settings.REALTIME_VAD_MIN_SILENCE_MS,
            speech_pad_ms=self._settings.REALTIME_VAD_SPEECH_PAD_MS,
            preroll_ms=self._settings.REALTIME_VAD_PRE_ROLL_MS,
        )
        session = RealtimeSession(
            session_id=session_id,
            user_id=current_user.id,
            chat_id=chat.id,
            character_id=character.id,
            peer=peer,
            bus=bus,
            audio_pacer=pacer,
            event_bridge=event_bridge,
            subtitle_bridge=subtitle_bridge,
            interrupt_manager=interrupt_manager,
            dialog_service=self._dialog_service,
            turn_service=self._turn_service,
            voice_runtime=self._voice_runtime,
            config=SessionConfig(
                turn_commit=TurnCommitGateConfig(
                    commit_grace_ms=getattr(
                        self._settings,
                        "REALTIME_UTTERANCE_COMMIT_GRACE_MS",
                        400,
                    ),
                    min_audio_ms=getattr(
                        self._settings,
                        "REALTIME_UTTERANCE_MIN_AUDIO_MS",
                        300,
                    ),
                    min_voiced_ms=getattr(
                        self._settings,
                        "REALTIME_UTTERANCE_MIN_VOICED_MS",
                        200,
                    ),
                    sample_rate=self._settings.REALTIME_VAD_SAMPLE_RATE,
                ),
                interrupt_hard_confirm_ms=getattr(
                    self._settings,
                    "REALTIME_INTERRUPT_HARD_CONFIRM_MS",
                    300,
                ),
                interrupt_resume_grace_ms=getattr(
                    self._settings,
                    "REALTIME_INTERRUPT_RESUME_GRACE_MS",
                    250,
                ),
                backchannel_max_voiced_ms=getattr(
                    self._settings,
                    "REALTIME_BACKCHANNEL_MAX_VOICED_MS",
                    900,
                ),
                backchannel_max_wait_ms=getattr(
                    self._settings,
                    "REALTIME_BACKCHANNEL_MAX_WAIT_MS",
                    500,
                ),
            ),
            metrics_sink=metrics_sink,
        )

        session_id_conflict = False
        async with self._lock:
            if session_id in self._sessions or session_id in self._reserved_session_ids:
                session_id_conflict = True
            else:
                self._reserved_session_ids.add(session_id)
        if session_id_conflict:
            await peer.close()
            raise APIError(
                status_code=409,
                code="resource_conflict",
                message="resource conflict: realtime session_id is already in use",
            )

        def _on_datachannel(channel) -> None:
            @channel.on("message")
            def _on_message(message):
                self._spawn(
                    self._handle_datachannel_message(session, message),
                    name=f"realtime.datachannel.{session_id}",
                )

        negotiation_timeout = max(1.0, float(self._settings.REALTIME_NEGOTIATION_TIMEOUT_SECONDS))
        try:
            answer = await asyncio.wait_for(
                peer.negotiate(
                    offer=offer,
                    on_datachannel=_on_datachannel,
                    on_audio_track=lambda track: self._spawn(
                        session.attach_audio_track(track),
                        name=f"realtime.audio_track.{session_id}",
                    ),
                    on_connection_state_change=lambda state: self._spawn(
                        self._handle_connection_state(
                            session_id,
                            str(state).lower(),
                            session_hint=session,
                        ),
                        name=f"realtime.connection_state.{session_id}",
                    ),
                ),
                timeout=negotiation_timeout,
            )
        except TimeoutError as exc:
            logger.warning(
                "Realtime session negotiation timed out: session_id=%s timeout_seconds=%.3f elapsed_ms=%.3f",
                session_id,
                negotiation_timeout,
                (time.perf_counter() - started_at) * 1000,
            )
            await peer.close()
            async with self._lock:
                self._reserved_session_ids.discard(session_id)
            raise APIError(
                status_code=504,
                code="external_timeout",
                message="external timeout: realtime negotiation timed out",
            ) from exc
        except BaseException:
            await peer.close()
            async with self._lock:
                self._reserved_session_ids.discard(session_id)
            raise
        async with self._lock:
            self._reserved_session_ids.discard(session_id)
            self._sessions[session_id] = session
        self._spawn(
            self._start_session(session_id, session),
            name=f"realtime.session.start.{session_id}",
        )
        logger.info(
            "Realtime session created: session_id=%s user_id=%s chat_id=%s character_id=%s elapsed_ms=%.3f",
            session_id,
            current_user.id,
            chat.id,
            character.id,
            (time.perf_counter() - started_at) * 1000,
        )
        return session, answer

    async def _start_session(self, session_id: str, session: RealtimeSession) -> None:
        try:
            await session.start()
        except Exception as exc:
            logger.error(
                "Realtime session start failed: session_id=%s error_type=%s",
                session_id,
                type(exc).__name__,
            )
            await self.close_session(
                session_id,
                telemetry_status="failed",
                telemetry_failure_stage="signaling",
                telemetry_error_code="session_start_failed",
            )

    def build_client_ice_servers(self, *, user_id: uuid.UUID) -> list[dict[str, object]]:
        return self._ice_config_service.build_client_ice_servers(user_id=user_id)

    async def close_session(
        self,
        session_id: str,
        *,
        user_id: uuid.UUID | None = None,
        telemetry_status: str | None = None,
        telemetry_failure_stage: str | None = None,
        telemetry_error_code: str | None = None,
    ) -> None:
        await self._cancel_disconnect_grace(session_id)
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return
            if user_id is not None and session.user_id != user_id:
                raise APIError(
                    status_code=403,
                    code="forbidden",
                    message="authorization failed: session does not belong to current user",
                )
            session = self._sessions.pop(session_id)
        if telemetry_status is not None:
            await self._finalize_session_metrics(
                session,
                status=telemetry_status,
                failure_stage=telemetry_failure_stage,
                error_code=telemetry_error_code,
            )
        await session.close()

    async def get_session(self, session_id: str) -> RealtimeSession | None:
        async with self._lock:
            return self._sessions.get(session_id)

    async def _handle_datachannel_message(self, session: RealtimeSession, message: object) -> None:
        try:
            payload = message if isinstance(message, str) else bytes(message).decode("utf-8")
            decoded = json.loads(payload)
            if not isinstance(decoded, dict):
                raise ValueError("realtime client event must be an object")
            await session.handle_client_event(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
            logger.warning(
                "Realtime client event rejected: session_id=%s",
                session.session_id,
                extra={"event": "realtime.client_event.rejected"},
            )

    async def _handle_connection_state(
        self,
        session_id: str,
        state: str,
        *,
        session_hint: RealtimeSession | None = None,
    ) -> None:
        if state == "connected":
            await self._cancel_disconnect_grace(session_id)
            session = session_hint or await self.get_session(session_id)
            if session is not None:
                mark_connected = getattr(session, "mark_metrics_connected", None)
                if callable(mark_connected):
                    result = mark_connected()
                    if inspect.isawaitable(result):
                        await result
            return
        if state == "disconnected":
            existing = self._disconnect_tasks.get(session_id)
            if existing is None or existing.done():
                task = self._spawn(
                    self._close_after_disconnect_grace(session_id),
                    name=f"realtime.disconnect_grace.{session_id}",
                )
                self._disconnect_tasks[session_id] = task
            return
        if state in {"failed", "closed"}:
            if state == "failed":
                await self.close_session(
                    session_id,
                    telemetry_status="failed",
                    telemetry_failure_stage="ice",
                    telemetry_error_code="connection_failed",
                )
            else:
                await self.close_session(session_id)

    async def _close_after_disconnect_grace(self, session_id: str) -> None:
        current = asyncio.current_task()
        try:
            await asyncio.sleep(self._disconnect_grace_seconds)
            logger.info(
                "Realtime disconnect grace expired: session_id=%s grace_seconds=%.3f",
                session_id,
                self._disconnect_grace_seconds,
                extra={"event": "realtime.connection.disconnect_grace_expired"},
            )
            await self.close_session(
                session_id,
                telemetry_status="failed",
                telemetry_failure_stage="ice",
                telemetry_error_code="disconnect_grace_expired",
            )
        except asyncio.CancelledError:
            raise
        finally:
            if self._disconnect_tasks.get(session_id) is current:
                self._disconnect_tasks.pop(session_id, None)

    async def _cancel_disconnect_grace(self, session_id: str) -> None:
        task = self._disconnect_tasks.pop(session_id, None)
        if task is None or task is asyncio.current_task():
            return
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def _spawn(self, coroutine, *, name: str):
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)

        def _done(completed: asyncio.Task[Any]) -> None:
            self._tasks.discard(completed)
            if completed.cancelled():
                return
            exception = completed.exception()
            if exception is not None:
                logger.error(
                    "Realtime manager task failed: task=%s error_type=%s",
                    completed.get_name(),
                    type(exception).__name__,
                )

        task.add_done_callback(_done)
        return task

    @staticmethod
    async def _finalize_session_metrics(
        session,
        *,
        status: str,
        failure_stage: str | None,
        error_code: str | None,
    ) -> None:
        finalize = getattr(session, "finalize_metrics", None)
        if not callable(finalize):
            return
        result = finalize(
            status=status,
            failure_stage=failure_stage,
            error_code=error_code,
        )
        if inspect.isawaitable(result):
            await result
