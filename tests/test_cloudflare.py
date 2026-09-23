from __future__ import annotations

import threading

from fastapi.testclient import TestClient

from cloud_storage.core import cloudflared_installer
from cloud_storage.core.api import create_app
from cloud_storage.core.config import CoreConfig
from cloud_storage.core.tunnels import CloudflareTunnelService
from cloud_storage.pairing import parse_dynamic_pairing_code


def test_cloudflare_backend_is_loopback_only(tmp_path) -> None:
    config = CoreConfig(
        data_directory=tmp_path,
        cloudflare_enabled=True,
        cloudflare_port=18769,
        cloudflare_executable="missing-cloudflared-test",
        cloudflare_public_url="https://storage.example.test",
        remote_pairing_enabled=True,
    )
    service = CloudflareTunnelService(config)

    status = service.status()

    assert status["listener"] == "http://127.0.0.1:18769"
    assert status["manager_api_exposed"] is False
    assert status["installed"] is False


def test_cloudflared_install_downloads_in_background_and_reports_completion(
    tmp_path, monkeypatch
) -> None:
    completed = threading.Event()
    proceed = threading.Event()
    config = CoreConfig(data_directory=tmp_path, cloudflare_executable="cloudflared")
    service = CloudflareTunnelService(config)

    def fake_download(data_directory):
        proceed.wait(2)
        folder = data_directory / "tools" / "cloudflared"
        folder.mkdir(parents=True)
        binary = folder / "cloudflared.exe"
        binary.write_bytes(b"test binary")
        completed.set()
        return binary

    monkeypatch.setattr(cloudflared_installer, "download_cloudflared", fake_download)
    status = service.install()

    assert status["installing"] is True
    proceed.set()
    assert completed.wait(2)
    # The installer worker updates status shortly after the file appears.
    for _ in range(100):
        status = service.status()
        if not status["installing"]:
            break
        threading.Event().wait(0.01)
    assert status["installed"] is True
    assert status["state"] == "disabled"


def test_cloudflare_token_is_stored_privately_and_never_returned(tmp_path) -> None:
    config = CoreConfig(
        data_directory=tmp_path,
        cloudflare_executable="missing-cloudflared-test",
    )
    service = CloudflareTunnelService(config)
    token = "eyJ" + "a" * 80

    status = service.enable(token)

    assert service.token_path.read_text(encoding="utf-8") == token
    assert token not in repr(status)
    assert status["login_required"] is False


def test_dynamic_code_prefers_verified_cloudflare_url(tmp_path) -> None:
    app = create_app(
        CoreConfig(
            data_directory=tmp_path,
            lan_enabled=True,
            lan_port=18766,
            cloudflare_enabled=True,
            cloudflare_port=18769,
            cloudflare_executable="missing-cloudflared-test",
            cloudflare_public_url="https://storage.example.test",
            remote_pairing_enabled=True,
        )
    )
    tunnel = app.state.runtime.tunnels.providers["cloudflare"]
    tunnel._state = "online"
    tunnel._public_url = "https://storage.example.test"
    headers = {"Authorization": f"Bearer {app.state.runtime.secrets.manager_token}"}

    with TestClient(app) as client:
        response = client.get("/v1/admin/dynamic-pairing-code", headers=headers)

    assert response.status_code == 200
    parsed = parse_dynamic_pairing_code(response.json()["code"])
    assert parsed is not None
    assert parsed.server_url == "https://storage.example.test"
    assert any(address.startswith("https://") for address in parsed.alternate_addresses)


def test_cloudflare_listener_hides_manager_routes(tmp_path) -> None:
    app = create_app(
        CoreConfig(
            data_directory=tmp_path,
            cloudflare_enabled=True,
            cloudflare_port=18769,
            cloudflare_executable="missing-cloudflared-test",
            cloudflare_public_url="https://storage.example.test",
            remote_pairing_enabled=True,
        )
    )
    headers = {"Authorization": f"Bearer {app.state.runtime.secrets.manager_token}"}

    with TestClient(app, base_url="http://127.0.0.1:18769") as internet:
        assert internet.get("/v1/health").status_code == 200
        assert internet.get("/v1/admin/summary", headers=headers).status_code == 404
        assert internet.get("/openapi.json").status_code == 404
