"""Realtime ICE/TURN configuration helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid

from aiortc import RTCConfiguration, RTCIceServer


def _is_turn_url(url: str) -> bool:
    normalized = url.lower()
    return normalized.startswith("turn:") or normalized.startswith("turns:")


def _is_stun_url(url: str) -> bool:
    return url.lower().startswith("stun:")


class RealtimeIceConfigService:
    def __init__(self, settings) -> None:
        self._settings = settings

    def build_client_ice_servers(self, *, user_id: uuid.UUID) -> list[dict[str, object]]:
        credentials = self._build_turn_credentials(user_id=user_id)
        return self._build_serialized_ice_servers(
            urls=self._settings.realtime_ice_public_urls,
            turn_credentials=credentials,
        )

    def build_server_rtc_configuration(self, *, user_id: uuid.UUID) -> RTCConfiguration:
        credentials = self._build_turn_credentials(user_id=user_id)
        ice_servers = self._build_aiortc_ice_servers(
            urls=self._settings.realtime_ice_backend_urls,
            turn_credentials=credentials,
        )
        return RTCConfiguration(iceServers=ice_servers)

    def credential_ttl_seconds(self) -> int:
        return max(60, int(self._settings.REALTIME_TURN_CREDENTIAL_TTL_SECONDS))

    def _build_turn_credentials(self, *, user_id: uuid.UUID) -> tuple[str, str] | None:
        secret = (self._settings.REALTIME_TURN_SECRET or "").strip()
        if not secret:
            return None

        ttl_seconds = self.credential_ttl_seconds()
        expiry = int(time.time()) + ttl_seconds
        username = f"{expiry}:{self._settings.REALTIME_TURN_USERNAME_PREFIX}-{user_id.hex}"
        digest = hmac.new(secret.encode("utf-8"), username.encode("utf-8"), hashlib.sha1).digest()
        password = base64.b64encode(digest).decode("ascii")
        return username, password

    def _build_serialized_ice_servers(
        self,
        *,
        urls: list[str],
        turn_credentials: tuple[str, str] | None,
    ) -> list[dict[str, object]]:
        stun_urls = [url for url in urls if _is_stun_url(url)]
        turn_urls = [url for url in urls if _is_turn_url(url)]

        servers: list[dict[str, object]] = []
        if stun_urls:
            servers.append({"urls": stun_urls})
        if turn_urls and turn_credentials is not None:
            username, credential = turn_credentials
            servers.append(
                {
                    "urls": turn_urls,
                    "username": username,
                    "credential": credential,
                }
            )
        return servers

    def _build_aiortc_ice_servers(
        self,
        *,
        urls: list[str],
        turn_credentials: tuple[str, str] | None,
    ) -> list[RTCIceServer]:
        stun_urls = [url for url in urls if _is_stun_url(url)]
        turn_urls = [url for url in urls if _is_turn_url(url)]

        servers: list[RTCIceServer] = []
        if stun_urls:
            servers.append(RTCIceServer(urls=stun_urls))
        if turn_urls and turn_credentials is not None:
            username, credential = turn_credentials
            servers.append(RTCIceServer(urls=turn_urls, username=username, credential=credential))
        return servers
