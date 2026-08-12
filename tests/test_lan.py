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
from cloud_storage.core.tunnels import ZrokTunnelService
from cloud_storage.pairing import (
    build_connection_code,
    build_pairing_uri,
    parse_connection_code,
    parse_pairing_uri,
)
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
    uri = build_pairing_uri(
        "ABCD-2345",
        "https://192.168.1.20:8766",
        fingerprint,
        "alex",
    )

    invitation = parse_pairing_uri(uri)

    assert invitation is not None
    assert invitation.code == "ABCD-2345"
    assert invitation.server_url == "https://192.168.1.20:8766"
    assert invitation.certificate_fingerprint == fingerprint
    assert invitation.username == "alex"


def test_connection_code_contains_address_tls_identity_and_username() -> None:
    fingerprint = "cd" * 32
    code = build_connection_code(
        "ABCD-2345",
        "https://cloud.example:8768",
        fingerprint,
        "alex",
    )

    invitation = parse_connection_code(code)

    assert code.startswith("CS1.")
    assert invitation is not None
    assert invitation.code == "ABCD-2345"
    assert invitation.server_url == "https://cloud.example:8768"
    assert invitation.certificate_fingerprint == fingerprint
    assert invitation.username == "alex"
    assert parse_pairing_uri(code) == invitation


def test_connection_code_rejects_corruption() -> None:
    with pytest.raises(ValueError, match="connection code"):
        parse_connection_code("CS1.not-valid-compressed-data")


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
        root = client.get("/")
        health = client.get("/v1/health")
        manager = client.get("/v1/admin/summary", headers=manager_headers)
        documentation = client.get("/docs")

    assert root.status_code == 200
    assert root.json()["status"] == "ok"
    assert health.status_code == 200
    assert health.json()["lan"]["enabled"] is True
    assert manager.status_code == 404
    assert documentation.status_code == 404


def test_remote_listener_is_default_deny_and_pairing_requires_explicit_opt_in(
    tmp_path,
) -> None:
    config = CoreConfig(
        data_directory=tmp_path,
        remote_enabled=True,
        remote_port=18767,
        remote_public_url="https://cloud.example.net:18767",
    )
    app = create_app(config)
    manager_headers = {"Authorization": f"Bearer {app.state.runtime.secrets.manager_token}"}

    with TestClient(app) as manager, TestClient(
        app, base_url="https://127.0.0.1:18767"
    ) as remote:
        health = remote.get("/v1/health")
        hidden_admin = remote.get("/v1/admin/summary", headers=manager_headers)
        hidden_unknown = remote.get("/v1/future-route")
        pairing = remote.post(
            "/v1/pairing/redeem",
            json={
                "code": "ABCD-2345",
                "password": "a secure test password",
                "device_name": "Remote test",
                "platform": "Windows",
            },
        )
        audit = manager.get("/v1/admin/audit", headers=manager_headers).json()

    assert health.status_code == 200
    assert health.json()["remote"]["public_url"] == "https://cloud.example.net:18767"
    assert health.json()["remote"]["manager_api_exposed"] is False
    assert hidden_admin.status_code == 404
    assert hidden_unknown.status_code == 404
    assert pairing.status_code == 403
    assert any(
        item["action"] == "remote.access.denied"
        and item["remote_address"] == "testclient"
        for item in audit
    )


def test_remote_pairing_works_when_administrator_enables_it(tmp_path) -> None:
    config = CoreConfig(
        data_directory=tmp_path,
        remote_enabled=True,
        remote_port=18767,
        remote_public_url="https://cloud.example.net:18767",
        remote_pairing_enabled=True,
    )
    app = create_app(config)
    manager_headers = {"Authorization": f"Bearer {app.state.runtime.secrets.manager_token}"}

    with TestClient(app) as manager, TestClient(
        app, base_url="https://127.0.0.1:18767"
    ) as remote:
        user = manager.post(
            "/v1/admin/users",
            headers=manager_headers,
            json={"username": "remoteqa", "display_name": "Remote QA", "quota_gib": 1},
        ).json()["user"]
        invitation = manager.post(
            "/v1/admin/invitations",
            headers=manager_headers,
            json={"user_id": user["id"]},
        ).json()
        paired = remote.post(
            "/v1/pairing/redeem",
            json={
                "code": invitation["code"],
                "password": "a secure test password",
                "device_name": "Remote laptop",
                "platform": "Windows",
            },
        )

    assert paired.status_code == 201
    assert paired.json()["device"]["status"] == "pending"


def test_zrok_gateway_requires_account_login_and_hides_manager_api(tmp_path) -> None:
    config = CoreConfig(
        data_directory=tmp_path,
        zrok_enabled=True,
        zrok_port=18768,
        zrok_executable="missing-zrok-for-test",
        remote_pairing_enabled=True,
    )
    app = create_app(config)
    manager_headers = {"Authorization": f"Bearer {app.state.runtime.secrets.manager_token}"}

    with TestClient(app) as manager, TestClient(
        app, base_url="http://127.0.0.1:18768"
    ) as internet:
        user = manager.post(
            "/v1/admin/users",
            headers=manager_headers,
            json={"username": "internetqa", "display_name": "Internet QA", "quota_gib": 1},
        ).json()["user"]
        invitation = manager.post(
            "/v1/admin/invitations",
            headers=manager_headers,
            json={"user_id": user["id"]},
        ).json()
        paired = internet.post(
            "/v1/pairing/redeem",
            json={
                "code": invitation["code"],
                "password": "internet qa secure password",
                "device_name": "Internet laptop",
                "platform": "Windows",
            },
        ).json()
        manager.post(
            f"/v1/admin/devices/{paired['device']['id']}/approve",
            headers=manager_headers,
        ).raise_for_status()
        device_headers = {"Authorization": f"Bearer {paired['device_token']}"}

        assert internet.get("/v1/admin/summary", headers=manager_headers).status_code == 404
        assert (
            internet.get("/v1/admin/support-bundle", headers=manager_headers).status_code
            == 404
        )
        assert (
            internet.post(
                "/v1/admin/tunnels/zrok/restart", headers=manager_headers
            ).status_code
            == 404
        )
        assert internet.get("/v1/admin/automation", headers=manager_headers).status_code == 404
        assert (
            internet.get("/v1/admin/notifications", headers=manager_headers).status_code
            == 404
        )
        assert (
            internet.get("/v1/admin/integrations", headers=manager_headers).status_code
            == 404
        )
        assert internet.get("/v1/spaces", headers=device_headers).status_code == 403
        wrong = internet.post(
            "/v1/remote/session",
            headers=device_headers,
            json={"username": "internetqa", "password": "wrong password value"},
        )
        assert wrong.status_code == 403
        login = internet.post(
            "/v1/remote/session",
            headers=device_headers,
            json={
                "username": "internetqa",
                "password": "internet qa secure password",
            },
        )
        assert login.status_code == 200
        spaces = internet.get(
            "/v1/spaces",
            headers={
                **device_headers,
                "X-Cloud-Remote-Session": login.json()["session_token"],
            },
        )

    assert spaces.status_code == 200
    assert spaces.json()[0]["name"] == "Мои файлы"
    assert app.state.runtime.tunnels.status("zrok")["installed"] is False


def test_zrok_gateway_allows_password_request_for_a_new_device(tmp_path) -> None:
    config = CoreConfig(
        data_directory=tmp_path,
        zrok_enabled=True,
        zrok_port=18768,
        zrok_executable="missing-zrok-for-test",
    )
    app = create_app(config)
    manager_headers = {"Authorization": f"Bearer {app.state.runtime.secrets.manager_token}"}

    with TestClient(app) as manager, TestClient(
        app, base_url="http://127.0.0.1:18768"
    ) as internet:
        manager.post(
            "/v1/admin/users",
            headers=manager_headers,
            json={
                "username": "mobileqa",
                "display_name": "Mobile QA",
                "quota_gib": 1,
                "password": "mobile qa secure password",
            },
        ).raise_for_status()
        requested = internet.post(
            "/v1/auth/device-login",
            json={
                "username": "mobileqa",
                "password": "mobile qa secure password",
                "device_name": "QA Phone",
                "platform": "Android",
            },
        )
        hidden = internet.get("/v1/admin/devices", headers=manager_headers)

    assert requested.status_code == 201
    assert requested.json()["device"]["status"] == "pending"
    assert hidden.status_code == 404


def test_zrok_mobile_admin_control_requires_confirmed_device_and_session(tmp_path) -> None:
    config = CoreConfig(
        data_directory=tmp_path,
        zrok_enabled=True,
        zrok_port=18768,
        zrok_executable="missing-zrok-for-test",
    )
    app = create_app(config)
    manager_headers = {"Authorization": f"Bearer {app.state.runtime.secrets.manager_token}"}
    with TestClient(app) as manager, TestClient(
        app, base_url="http://127.0.0.1:18768"
    ) as internet:
        manager.post(
            "/v1/admin/users",
            headers=manager_headers,
            json={
                "username": "phoneadmin",
                "display_name": "Phone Admin",
                "quota_gib": 1,
                "role": "admin",
                "password": "phone admin secure password",
            },
        ).raise_for_status()
        phone = internet.post(
            "/v1/auth/device-login",
            json={
                "username": "phoneadmin",
                "password": "phone admin secure password",
                "device_name": "Admin iPhone",
                "platform": "iOS",
            },
        ).json()
        manager.post(
            f"/v1/admin/devices/{phone['device']['id']}/approve",
            headers=manager_headers,
        ).raise_for_status()
        device_headers = {"Authorization": f"Bearer {phone['device_token']}"}
        assert internet.get("/v1/mobile/admin/overview", headers=device_headers).status_code == 403
        session = internet.post(
            "/v1/remote/session",
            headers=device_headers,
            json={
                "username": "phoneadmin",
                "password": "phone admin secure password",
            },
        ).json()["session_token"]
        overview = internet.get(
            "/v1/mobile/admin/overview",
            headers={**device_headers, "X-Cloud-Remote-Session": session},
        )
        hidden_manager = internet.get("/v1/admin/summary", headers=manager_headers)

    assert overview.status_code == 200
    assert overview.json()["server_mode"]["mode"] == "normal"
    assert hidden_manager.status_code == 404


def test_zrok_process_environment_does_not_inherit_unrelated_secrets(monkeypatch) -> None:
    monkeypatch.setenv("UNRELATED_API_SECRET", "must-not-leak")
    monkeypatch.setenv("ZROK_API_ENDPOINT", "https://zrok.example")

    environment = ZrokTunnelService._subprocess_environment()

    assert "UNRELATED_API_SECRET" not in environment
    assert environment["ZROK_API_ENDPOINT"] == "https://zrok.example"


def test_live_https_listener_pinning_discovery_and_local_admin_boundary(tmp_path) -> None:
    local_port = _free_tcp_port()
    lan_port = _free_tcp_port()
    remote_port = _free_tcp_port()
    discovery_port = _free_udp_port()
    config = CoreConfig(
        data_directory=tmp_path,
        port=local_port,
        lan_enabled=True,
        lan_port=lan_port,
        discovery_port=discovery_port,
        remote_enabled=True,
        remote_port=remote_port,
        remote_public_url=f"https://127.0.0.1:{remote_port}",
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
    assert servers.remote_server is not None and servers.remote_server.started
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

        remote_api = ClientApi(
            f"https://127.0.0.1:{remote_port}",
            token=servers.application.state.runtime.secrets.manager_token,
            certificate_fingerprint=identity.fingerprint,
        )
        assert remote_api.health()["remote"]["manager_api_exposed"] is False
        with pytest.raises(ClientApiError) as remote_denied:
            remote_api._json_request("/v1/admin/summary")
        assert remote_denied.value.status_code == 404

        discovered = discover_servers(port=discovery_port, timeout=1.0)
        assert len(discovered) == 1
        assert discovered[0].server_name == "Test Home Cloud"
        assert discovered[0].fingerprint == identity.fingerprint

        local_client = CoreClient(config)
        assert local_client.summary()["users"] == 0
        assert len(local_client.automation()["rules"]) == 8
        assert local_client.integrations()["policy"]["built_in_only"] is True
        tested_inbox = local_client.test_integration("manager-inbox")
        notifications = local_client.list_notifications()
        assert notifications[0]["id"] == tested_inbox["notification_id"]
        local_client.acknowledge_notification(notifications[0]["id"])
        rule = local_client.create_automation_rule(
            {
                "name": "Live client rule",
                "trigger_type": "backup_failed",
                "action_type": "notify",
                "cooldown_minutes": 60,
            }
        )
        local_client.update_automation_rule(
            rule["id"],
            {
                "name": rule["name"],
                "enabled": False,
                "trigger_type": rule["trigger_type"],
                "action_type": rule["action_type"],
                "cooldown_minutes": rule["cooldown_seconds"] // 60,
            },
        )
        local_client.delete_automation_rule(rule["id"])
        bundle_path = tmp_path / "exported-support.zip"
        bundle = local_client.download_support_bundle(bundle_path)
        assert bundle["path"] == str(bundle_path.resolve())
        assert bundle["size_bytes"] == bundle_path.stat().st_size
        assert bundle_path.read_bytes().startswith(b"PK")
    finally:
        servers.request_shutdown(delay=False)
        thread.join(timeout=8)
    assert not thread.is_alive()


def test_live_zrok_backend_stays_loopback_and_core_survives_missing_binary(tmp_path) -> None:
    local_port = _free_tcp_port()
    zrok_port = _free_tcp_port()
    config = CoreConfig(
        data_directory=tmp_path,
        port=local_port,
        zrok_enabled=True,
        zrok_port=zrok_port,
        zrok_executable="missing-zrok-for-live-test",
    )
    servers = CoreServerGroup(config)
    thread = threading.Thread(target=servers.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 8
    while not servers.local_server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert servers.local_server.started
    assert servers.zrok_server is not None and servers.zrok_server.started

    try:
        gateway = ClientApi(
            f"http://127.0.0.1:{zrok_port}",
            token=servers.application.state.runtime.secrets.manager_token,
        )
        assert gateway.health()["zrok"]["manager_api_exposed"] is False
        with pytest.raises(ClientApiError) as denied:
            gateway._json_request("/v1/admin/summary")
        assert denied.value.status_code == 404
        deadline = time.monotonic() + 3
        while (
            servers.application.state.runtime.tunnels.status("zrok")["state"] == "starting"
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        assert (
            servers.application.state.runtime.tunnels.status("zrok")["state"]
            == "not_installed"
        )
    finally:
        servers.request_shutdown(delay=False)
        thread.join(timeout=8)
    assert not thread.is_alive()
