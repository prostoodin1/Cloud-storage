from __future__ import annotations

import socket
import threading
import time

import pytest
from fastapi.testclient import TestClient

from cloud_storage.client.api_client import CertificateMismatch, ClientApi, ClientApiError
from cloud_storage.client.discovery import discover_servers
from cloud_storage.core.api import create_app
from cloud_storage.core.config import CoreConfig
from cloud_storage.core.main import CoreServerGroup
from cloud_storage.core.tls import load_or_create_tls_identity
from cloud_storage.pairing import build_pairing_uri, parse_pairing_uri
from cloud_storage.services.core_client import CoreClient


def _free_tcp_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_pairing_uri_round_trip_contains_tls_identity() -> None:
    fingerprint = "ab" * 32
    uri = build_pairing_uri("ABCD-2345", "https://192.168.1.20:8766", fingerprint)

    invitation = parse_pairing_uri(uri)

    assert invitation is not None
    assert invitation.code == "ABCD-2345"
    assert invitation.server_url == "https://192.168.1.20:8766"
    assert invitation.certificate_fingerprint == fingerprint


def test_tls_identity_is_created_once_and_remains_pinned(tmp_path) -> None:
    config = CoreConfig(data_directory=tmp_path, lan_enabled=True, server_name="Test Cloud")

    first = load_or_create_tls_identity(config)
    first_certificate = config.tls_certificate_path.read_bytes()
    second = load_or_create_tls_identity(config)

    assert first.fingerprint == second.fingerprint
    assert config.tls_certificate_path.read_bytes() == first_certificate
    assert config.tls_private_key_path.exists()
    assert len(first.fingerprint) == 64


def test_lan_listener_hides_manager_routes_in_asgi_scope(tmp_path) -> None:
    config = CoreConfig(data_directory=tmp_path, lan_enabled=True, lan_port=18766)
    app = create_app(config)
    manager_headers = {"Authorization": f"Bearer {app.state.runtime.secrets.manager_token}"}

    with TestClient(app, base_url="https://127.0.0.1:18766") as client:
        health = client.get("/v1/health")
        manager = client.get("/v1/admin/summary", headers=manager_headers)
        documentation = client.get("/docs")

    assert health.status_code == 200
    assert health.json()["lan"]["enabled"] is True
    assert manager.status_code == 404
    assert documentation.status_code == 404


def test_live_https_listener_pinning_discovery_and_local_admin_boundary(tmp_path) -> None:
    local_port = _free_tcp_port()
    lan_port = _free_tcp_port()
    discovery_port = _free_udp_port()
    config = CoreConfig(
        data_directory=tmp_path,
        port=local_port,
        lan_enabled=True,
        lan_port=lan_port,
        discovery_port=discovery_port,
        server_name="Test Home Cloud",
    )
    servers = CoreServerGroup(config)
    thread = threading.Thread(target=servers.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 8
    while not servers.local_server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert servers.local_server.started
    assert servers.lan_server is not None and servers.lan_server.started
    identity = servers.application.state.runtime.tls_identity
    assert identity is not None

    try:
        api = ClientApi(
            f"https://127.0.0.1:{lan_port}",
            token=servers.application.state.runtime.secrets.manager_token,
            certificate_fingerprint=identity.fingerprint,
        )
        assert api.health()["server_name"] == "Test Home Cloud"
        with pytest.raises(ClientApiError) as denied:
            api._json_request("/v1/admin/summary")
        assert denied.value.status_code == 404

        with pytest.raises(CertificateMismatch):
            ClientApi(
                f"https://127.0.0.1:{lan_port}",
                certificate_fingerprint="00" * 32,
            ).health()

        discovered = discover_servers(port=discovery_port, timeout=1.0)
        assert len(discovered) == 1
        assert discovered[0].server_name == "Test Home Cloud"
        assert discovered[0].fingerprint == identity.fingerprint

        local_client = CoreClient(config)
        assert local_client.summary()["users"] == 0
    finally:
        servers.request_shutdown(delay=False)
        thread.join(timeout=8)
    assert not thread.is_alive()
