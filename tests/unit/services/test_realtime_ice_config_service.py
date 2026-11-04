from __future__ import annotations

import base64
import hashlib
import hmac
import uuid
from types import SimpleNamespace

from src.realtime.services import realtime_ice_config_service as ice_module
from src.realtime.services.realtime_ice_config_service import RealtimeIceConfigService


def _build_settings(**overrides):
    defaults = {
        "realtime_ice_public_urls": [
            "stun:parlasoul.com:3478",
            "turn:parlasoul.com:3478?transport=udp",
            "turns:parlasoul.com:5349?transport=tcp",
        ],
        "realtime_ice_backend_urls": [
            "stun:host.docker.internal:3478",
            "turn:host.docker.internal:3478?transport=udp",
            "turns:host.docker.internal:5349?transport=tcp",
        ],
        "REALTIME_TURN_SECRET": "super-secret",
        "REALTIME_TURN_CREDENTIAL_TTL_SECONDS": 600,
        "REALTIME_TURN_USERNAME_PREFIX": "parlasoul",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_build_client_ice_servers_groups_stun_and_turn(monkeypatch) -> None:
    monkeypatch.setattr(ice_module.time, "time", lambda: 1_700_000_000)
    service = RealtimeIceConfigService(_build_settings())
    user_id = uuid.UUID("11111111-1111-1111-1111-111111111111")

    servers = service.build_client_ice_servers(user_id=user_id)

    assert servers[0] == {"urls": ["stun:parlasoul.com:3478"]}

    expected_username = f"1700000600:parlasoul-{user_id.hex}"
    expected_password = base64.b64encode(
        hmac.new(b"super-secret", expected_username.encode("utf-8"), hashlib.sha1).digest()
    ).decode("ascii")
    assert servers[1] == {
        "urls": [
            "turn:parlasoul.com:3478?transport=udp",
            "turns:parlasoul.com:5349?transport=tcp",
        ],
        "username": expected_username,
        "credential": expected_password,
    }


def test_build_server_rtc_configuration_uses_backend_urls() -> None:
    service = RealtimeIceConfigService(_build_settings())

    configuration = service.build_server_rtc_configuration(user_id=uuid.uuid4())

    assert configuration is not None
    assert configuration.iceServers[0].urls == ["stun:host.docker.internal:3478"]
    assert configuration.iceServers[1].urls == [
        "turn:host.docker.internal:3478?transport=udp",
        "turns:host.docker.internal:5349?transport=tcp",
    ]


def test_build_server_rtc_configuration_skips_turn_without_secret() -> None:
    service = RealtimeIceConfigService(
        _build_settings(
            REALTIME_TURN_SECRET=None,
        )
    )

    configuration = service.build_server_rtc_configuration(user_id=uuid.uuid4())

    assert configuration is not None
    assert len(configuration.iceServers) == 1
    assert configuration.iceServers[0].urls == ["stun:host.docker.internal:3478"]


def test_build_server_rtc_configuration_does_not_fallback_to_public_urls() -> None:
    service = RealtimeIceConfigService(
        _build_settings(
            realtime_ice_public_urls=["stun:parlasoul.com:3478"],
            realtime_ice_backend_urls=[],
        )
    )

    configuration = service.build_server_rtc_configuration(user_id=uuid.uuid4())

    assert configuration.iceServers == []


def test_build_client_ice_servers_returns_empty_when_public_urls_empty() -> None:
    service = RealtimeIceConfigService(
        _build_settings(
            realtime_ice_public_urls=[],
        )
    )

    servers = service.build_client_ice_servers(user_id=uuid.uuid4())

    assert servers == []


def test_credential_ttl_seconds_has_minimum() -> None:
    service = RealtimeIceConfigService(
        _build_settings(
            REALTIME_TURN_CREDENTIAL_TTL_SECONDS=10,
        )
    )

    assert service.credential_ttl_seconds() == 60
