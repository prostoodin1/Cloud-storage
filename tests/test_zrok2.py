from __future__ import annotations

import json
import subprocess
import sys
import time
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from cloud_storage.client.api_client import ClientApi, ClientConnectionError
from cloud_storage.client.settings import ClientProfile
from cloud_storage.client.window import ClientWindow
from cloud_storage.core.api import create_app
from cloud_storage.core.config import CoreConfig
from cloud_storage.core.tunnels import ZrokTunnelService
from cloud_storage.pairing import parse_dynamic_pairing_code


@pytest.fixture
def tunnel(tmp_path):
    return ZrokTunnelService(CoreConfig(data_directory=tmp_path, zrok_enabled=True))


@pytest.mark.parametrize("line", [
    "contacting https://api-v2.zrok.io",
    "error: read documentation at https://netfoundry.io/docs/zrok",
    "sharing http://127.0.0.1:8768",
    '{"level":"ERROR","msg":"failed to reach https://api-v2.zrok.io"}',
])
def test_log_urls_never_mark_tunnel_online(tunnel, line):
    tunnel._handle_output(line)
    assert tunnel._public_url == tunnel._candidate_url == ""
    assert tunnel._state != "online"


@pytest.mark.parametrize("structured", [True, False])
def test_official_endpoint_output_requires_probe(tunnel, structured):
    message = "access your zrok share at the following endpoints:\n https://qa.share.zrok.io"
    if structured:
        tunnel._handle_output(json.dumps({"level": "INFO", "msg": message}))
    else:
        for line in message.splitlines():
            tunnel._handle_output(line)
    assert tunnel._candidate_url == "https://qa.share.zrok.io"
    assert tunnel._public_url == ""
    assert tunnel._state == "checking"


@pytest.mark.parametrize("value", [None, {}, {"zrok": {}}, {"zrok": {"probe_id": "another-core"}}])
def test_probe_rejects_wrong_backend(tunnel, monkeypatch, value):
    tunnel._candidate_url = "https://qa.share.zrok.io"
    monkeypatch.setattr(ClientApi, "health", lambda *_args, **_kwargs: value)
    assert not tunnel._check_public_endpoint()
    assert tunnel._public_url == ""
    assert tunnel._state == "unreachable"


def test_probe_failure_and_stop_revoke_advertised_address(tunnel, monkeypatch):
    tunnel._candidate_url = "https://qa.share.zrok.io"
    monkeypatch.setattr(ClientApi, "health", lambda *_a, **_k: {
        "zrok": {"probe_id": tunnel.health_probe_id}
    })
    assert tunnel._check_public_endpoint()
    assert tunnel._state == "online"
    def unavailable(*args, **kwargs):
        raise ClientConnectionError("unreachable")
    monkeypatch.setattr(ClientApi, "health", unavailable)
    assert not tunnel._check_public_endpoint()
    assert tunnel._public_url == ""
    tunnel.stop()
    assert tunnel._candidate_url == ""


def test_account_required_has_actionable_state(tunnel):
    tunnel._handle_output("unable to create share: unable to load environment; did you 'zrok2 enable'?")
    assert tunnel._state == "account_required"
    assert "Подключите аккаунт" in tunnel._last_error


def test_enable_headless_profile_and_restart(tunnel, monkeypatch):
    monkeypatch.setattr(ZrokTunnelService, "_resolve_executable", lambda self: "zrok2")
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="enabled")
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(ZrokTunnelService, "restart", lambda self: calls.append("restart"))
    tunnel.enable("test-enable-token")
    command, kwargs = calls[0]
    assert command == ["zrok2", "enable", "--headless", "test-enable-token"]
    assert kwargs["env"]["HOME"] == str(tunnel.config.data_directory / "zrok2-profile")
    assert kwargs["env"]["DL_USE_JSON"] == "true"
    assert calls[1] == "restart"
    assert not tunnel._enabling


@pytest.mark.parametrize("timeout", [True, False])
def test_enable_errors_never_expose_token(tunnel, monkeypatch, timeout):
    monkeypatch.setattr(ZrokTunnelService, "_resolve_executable", lambda self: "zrok2")
    def run(command, **kwargs):
        if timeout:
            raise subprocess.TimeoutExpired(command, 45)
        return subprocess.CompletedProcess(command, 1, stdout="invalid test-enable-token")
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(RuntimeError) as error:
        tunnel.enable("test-enable-token")
    assert "test-enable-token" not in str(error.value)
    assert not tunnel._enabling


def test_missing_executable_can_be_retried(tunnel, monkeypatch):
    monkeypatch.setattr(ZrokTunnelService, "_resolve_executable", lambda self: None)
    for _ in range(2):
        tunnel.start()
        deadline = time.monotonic() + 2
        while tunnel._thread is not None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert tunnel._thread is None
        assert tunnel._state == "not_installed"


def test_dynamic_pairing_over_internet_needs_no_password_and_survives_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(ZrokTunnelService, "start", lambda self: None)
    config = CoreConfig(data_directory=tmp_path, zrok_enabled=True,
                        remote_pairing_enabled=True, zrok_port=18768,
                        zrok_executable="missing-zrok-test")
    app = create_app(config)
    service = app.state.runtime.tunnels.providers["zrok"]
    # ASGI gateway test: emulate only a verified zrok address, not internet transport.
    service._state, service._public_url = "online", "https://qa.share.zrok.io"
    headers = {"Authorization": f"Bearer {app.state.runtime.secrets.manager_token}"}
    with TestClient(app, base_url="http://127.0.0.1:18768") as remote:
        # The missing binary monitor clears the state during startup.
        service._state, service._public_url = "online", "https://qa.share.zrok.io"
        manager = TestClient(app)
        code = manager.get("/v1/admin/dynamic-pairing-code", headers=headers).json()["code"]
        assert parse_dynamic_pairing_code(code).server_url == "https://qa.share.zrok.io"
        paired = remote.post("/v1/pairing/redeem", json={"code": code, "device_name": "Internet QA", "platform": "Windows"})
        assert paired.status_code == 201, paired.text
        token = paired.json()["device_token"]
        device_id = paired.json()["device"]["id"]
        auth = {"Authorization": f"Bearer {token}"}
        assert remote.get("/v1/spaces", headers=auth).status_code == 200
        assert remote.get("/v1/spaces").status_code == 401
        assert remote.get("/v1/admin/users", headers=auth).status_code == 404
    restarted = create_app(config)
    with TestClient(restarted, base_url="http://127.0.0.1:18768") as remote:
        assert remote.get("/v1/spaces", headers=auth).status_code == 200
        restarted.state.runtime.repository.set_device_status(device_id, "revoked")
        assert remote.get("/v1/spaces", headers=auth).status_code == 403


def test_dynamic_device_migration_uses_audit_provenance(tmp_path):
    app = create_app(CoreConfig(data_directory=tmp_path))
    repository = app.state.runtime.repository
    dynamic = repository.redeem_dynamic_pairing(device_name="Existing device", platform="Windows", remote_address=None)
    with repository.database.transaction() as connection:
        connection.execute("ALTER TABLE devices DROP COLUMN pairing_method")
        connection.execute("DELETE FROM schema_migrations WHERE version = 19")
    repository.database.initialize()
    repository.database.initialize()
    assert repository.get_device(dynamic.device.id).pairing_method == "dynamic"


def test_client_bypasses_zrok_interstitial():
    assert ClientApi("https://qa.share.zrok.io")._headers()["skip_zrok_interstitial"] == "1"


def test_supervisor_owns_one_process_and_clears_endpoint_after_exit(tunnel, monkeypatch):
    monkeypatch.setattr(ZrokTunnelService, "_resolve_executable", lambda self: sys.executable)
    program = (
        "import json, time; print(json.dumps({'msg': "
        "'access your zrok share at the following endpoints:\\n https://qa.share.zrok.io'}), "
        "flush=True); time.sleep(0.8)"
    )
    monkeypatch.setattr(ZrokTunnelService, "_share_command", lambda *_: [sys.executable, "-u", "-c", program])
    monkeypatch.setattr(ClientApi, "health", lambda *_a, **_k: {"zrok": {"probe_id": tunnel.health_probe_id}})
    tunnel.start()
    original_thread = tunnel._thread
    tunnel.start()
    assert tunnel._thread is original_thread
    try:
        deadline = time.monotonic() + 5
        while tunnel._state != "online" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert tunnel._state == "online"
        process = tunnel._process
        deadline = time.monotonic() + 4
        while tunnel._restart_count == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert tunnel._restart_count == 1
        assert tunnel._public_url == ""
        assert process.poll() is not None
    finally:
        tunnel.stop()
    assert tunnel._thread is tunnel._process is None


def test_supervisor_stops_an_idle_child(tunnel, monkeypatch):
    monkeypatch.setattr(ZrokTunnelService, "_resolve_executable", lambda self: sys.executable)
    monkeypatch.setattr(ZrokTunnelService, "_share_command", lambda *_: [sys.executable, "-c", "import time; time.sleep(30)"])
    tunnel.start()
    deadline = time.monotonic() + 3
    while tunnel._process is None and time.monotonic() < deadline:
        time.sleep(0.02)
    process = tunnel._process
    assert process is not None
    tunnel.stop()
    assert process.poll() is not None
    assert tunnel._thread is None


def test_ipv6_loopback_backend_is_bracketed(tmp_path):
    service = ZrokTunnelService(CoreConfig(data_directory=tmp_path, zrok_host="::1"))
    assert service._share_command("zrok2")[-1] == "[::1]:8768"


def test_public_pairing_never_restores_old_lan_certificate(monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "information", lambda *_a: None)
    window = Mock()
    window.profile = ClientProfile(certificate_fingerprint="a" * 64)
    window.fingerprint.text.return_value = "a" * 64
    window.server_url.text.return_value = "https://192.168.1.10:8766"
    window.device_name.text.return_value = "QA"
    ClientWindow._pairing_complete(window, {
        "profile_id": window.profile.profile_id,
        "server_url": "https://qa.share.zrok.io", "fingerprint": "",
        "health": {"server_name": "QA"},
        "pairing": {"device_token": "test-token", "device": {"id": "device", "status": "trusted"}},
    })
    assert window.profile.certificate_fingerprint == ""
    window.fingerprint.setText.assert_called_with("")


def test_installer_download_uses_verified_transport_and_checksum(tmp_path, monkeypatch):
    import hashlib
    import io
    import tarfile

    from cloud_storage.core import zrok_installer
    from cloud_storage.updates import UpdateService

    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as package:
        member = tarfile.TarInfo("zrok2.exe")
        member.size = 4
        package.addfile(member, io.BytesIO(b"test"))
    payload = archive.getvalue()
    monkeypatch.setattr(zrok_installer.platform, "system", lambda: "Windows")
    monkeypatch.setattr(zrok_installer.platform, "machine", lambda: "AMD64")
    monkeypatch.setitem(zrok_installer._ASSETS, ("Windows", "amd64"), ("test.tar.gz", hashlib.sha256(payload).hexdigest()))
    monkeypatch.setattr(UpdateService, "_open_verified", lambda *_a, **_k: io.BytesIO(payload))
    assert zrok_installer.download_zrok2(tmp_path).read_bytes() == b"test"
    monkeypatch.setitem(zrok_installer._ASSETS, ("Windows", "amd64"), ("test.tar.gz", "0" * 64))
    with pytest.raises(RuntimeError, match="checksum"):
        zrok_installer.download_zrok2(tmp_path)


def test_invalid_endpoint_cannot_crash_log_reader(tunnel):
    tunnel._handle_output("access your zrok share at the following endpoints: https://[broken")
    assert tunnel._candidate_url == ""
    assert tunnel._state == "error"


@pytest.mark.parametrize("frontend", ["qa.shares.zrok.io", "http://qa.shares.zrok.io", "qa.custom.example:8443"])
def test_hosted_zrok2_frontend_formats(tunnel, frontend):
    tunnel._handle_output(json.dumps({"level": "INFO", "msg": "access your zrok share at the following endpoints:\n " + frontend}))
    assert tunnel._candidate_url == "https://" + frontend.removeprefix("http://")
    assert tunnel._public_url == ""
    assert tunnel._state == "checking"
