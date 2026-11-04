from types import SimpleNamespace

from src.realtime.rtc import gateway


def test_build_peer_without_configuration_disables_aiortc_default_ice_servers(monkeypatch) -> None:
    created = {}

    def fake_peer_connection(*, configuration):
        created["configuration"] = configuration
        return SimpleNamespace()

    monkeypatch.setattr(gateway, "RTCPeerConnection", fake_peer_connection)

    gateway.build_peer(session_id="rt_test")

    assert created["configuration"].iceServers == []

