from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient
from PySide6.QtWidgets import QApplication

from cloud_storage.client.api_client import ClientApi, ClientApiError, ClientConnectionError
from cloud_storage.client.discovery import DiscoveredServer
from cloud_storage.client.drive import DriveManager
from cloud_storage.client.settings import (
    ClientProfile,
    ClientSettingsStore,
    DeviceTokenVault,
    RemoteSessionVault,
    validate_server_url,
)
from cloud_storage.client.window import ClientWindow
from cloud_storage.core.api import create_app
from cloud_storage.core.config import CoreConfig

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_client_profile_and_device_token_are_persisted(tmp_path) -> None:
    store = ClientSettingsStore(tmp_path)
    profile = ClientProfile(
        server_url="http://127.0.0.1:8765",
        device_id="device-1",
        device_name="Test laptop",
        device_status="pending",
        download_directory=str(tmp_path / "offline"),
    )
    store.save(profile)

    loaded = store.load()
    assert loaded.device_id == "device-1"
    assert loaded.device_status == "pending"

    vault = DeviceTokenVault(tmp_path)
    token = "csd_" + "x" * 64
    vault.store(token)
    assert vault.load() == token
    assert token.encode() not in vault.path.read_bytes() if os.name == "nt" else True
    vault.clear()
    assert vault.load() is None

    session_vault = RemoteSessionVault(tmp_path)
    session = "css_" + "x" * 120
    session_vault.store(session)
    assert session_vault.load() == session
    assert session.encode() not in session_vault.path.read_bytes() if os.name == "nt" else True
    session_vault.clear()
    assert session_vault.load() is None


def test_space_drive_letter_survives_profile_save_and_restart(tmp_path) -> None:
    store = ClientSettingsStore(tmp_path)
    profile = store.load()
    profile.drive_letter = "S"
    profile.drive_letters = {"personal": "S", "shared": "T"}
    store.save(profile)
    restarted = ClientSettingsStore(tmp_path)
    assert restarted.load().drive_letters == {"personal": "S", "shared": "T"}
    for _ in range(3):
        loaded = restarted.load()
        restarted.ensure_space_drive_letters(loaded, ["personal", "shared"])
    assert restarted.load().drive_letters == {"personal": "S", "shared": "T"}


def test_virtual_drive_uses_persistent_cloud_storage_icon() -> None:
    icon = DriveManager._icon_path()
    assert icon.is_file()
    assert icon.name.casefold() in {"cloud-storage.ico", "python.exe"}


def test_multiple_server_profiles_have_independent_tokens(tmp_path) -> None:
    store = ClientSettingsStore(tmp_path)
    first = store.load()
    first.server_name = "First cloud"
    first.server_url = "https://first.example:8767"
    first.certificate_fingerprint = "11" * 32
    store.save(first)

    second = store.add_profile()
    second.server_name = "Second cloud"
    second.server_url = "https://second.example:8767"
    second.certificate_fingerprint = "22" * 32
    store.save(second)

    first_token = "csd_" + "a" * 64
    second_token = "csd_" + "b" * 64
    DeviceTokenVault(tmp_path, first.profile_id).store(first_token)
    DeviceTokenVault(tmp_path, second.profile_id).store(second_token)

    assert len(store.list_profiles()) == 2
    assert store.load().profile_id == second.profile_id
    assert store.set_active(first.profile_id).server_name == "First cloud"
    assert DeviceTokenVault(tmp_path, first.profile_id).load() == first_token
    assert DeviceTokenVault(tmp_path, second.profile_id).load() == second_token
    assert DeviceTokenVault(tmp_path, first.profile_id).path != DeviceTokenVault(
        tmp_path, second.profile_id
    ).path


def test_legacy_single_profile_is_migrated_on_save(tmp_path) -> None:
    store = ClientSettingsStore(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        '{"server_url":"https://old.example:8766","device_id":"legacy-device"}',
        encoding="utf-8",
    )

    migrated = store.load()
    assert migrated.profile_id == "default"
    assert migrated.device_id == "legacy-device"
    store.save(migrated)

    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 4
    assert payload["active_profile_id"] == "default"
    assert len(payload["profiles"]) == 1


def test_remote_plain_http_is_rejected() -> None:
    assert validate_server_url("http://127.0.0.1:8765") == "http://127.0.0.1:8765"
    assert validate_server_url("https://cloud.home:8765/") == "https://cloud.home:8765"
    with pytest.raises(ValueError, match="HTTPS"):
        validate_server_url("http://192.168.1.50:8765")


def test_desktop_client_window_smoke(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = ClientWindow(ClientSettingsStore(tmp_path))
    window.reconnect_timer.stop()
    app.processEvents()

    assert window.stack.count() == 6
    assert window.help_page.article_list.count() > 0
    assert window.discover_button.text() == "Найти в сети"
    assert window.remote_login_button.text() == "Войти через интернет"
    assert window.account_login_button.text() == "Войти по логину"
    assert window.server_selector.count() == 1
    assert window.nav_buttons[1].isEnabled() is False
    window.add_server()
    assert window.server_selector.count() == 2
    assert window.profile.profile_id != "default"
    window.close()


def test_client_recovers_changed_lan_address_by_pinned_server_identity(
    tmp_path, monkeypatch
) -> None:
    app = QApplication.instance() or QApplication([])
    store = ClientSettingsStore(tmp_path)
    profile = store.load()
    profile.server_url = "https://192.168.1.10:8766"
    profile.certificate_fingerprint = "ab" * 32
    profile.device_status = "offline"
    store.save(profile)
    DeviceTokenVault(tmp_path).store("csd_" + "x" * 64)
    window = ClientWindow(store=store, smoke_test=True)
    tasks = []
    monkeypatch.setattr(window, "_start_task", lambda *args: tasks.append(args))
    monkeypatch.setattr(window, "_set_spaces", lambda *args, **kwargs: None)
    monkeypatch.setattr(window, "_reconcile_drives", lambda: None)

    class FakeApi:
        def __init__(self, server_url, **_kwargs):
            self.server_url = server_url

        def health(self):
            if self.server_url.endswith(".10:8766"):
                raise ClientConnectionError("old DHCP address")
            return {"status": "ok"}

        def pairing_status(self):
            return {"status": "trusted"}

        def list_spaces(self):
            return []

    monkeypatch.setattr("cloud_storage.client.window.ClientApi", FakeApi)
    monkeypatch.setattr(
        "cloud_storage.client.window.discover_servers",
        lambda timeout: [
            DiscoveredServer(
                "Home",
                "https://192.168.1.42:8766",
                "ab" * 32,
                ("ab" * 32)[:16],
            )
        ],
    )
    try:
        window.refresh_connection()
        task = tasks.pop()
        task[1](task[0]())
        assert store.load().server_url == "https://192.168.1.42:8766"
        assert store.load().device_status == "trusted"
        assert window.sidebar_state.text() == "ПОДКЛЮЧЕНО"
        assert DeviceTokenVault(tmp_path).load() is not None
    finally:
        window.close()
        app.processEvents()


def test_client_distinguishes_invalid_device_token_from_offline(
    tmp_path, monkeypatch
) -> None:
    app = QApplication.instance() or QApplication([])
    store = ClientSettingsStore(tmp_path)
    profile = store.load()
    profile.server_url = "https://192.168.1.42:8766"
    profile.certificate_fingerprint = "cd" * 32
    store.save(profile)
    DeviceTokenVault(tmp_path).store("csd_" + "y" * 64)
    window = ClientWindow(store=store, smoke_test=True)
    tasks = []
    monkeypatch.setattr(window, "_start_task", lambda *args: tasks.append(args))
    monkeypatch.setattr(window, "_set_spaces", lambda *args, **kwargs: None)
    monkeypatch.setattr(window, "_reconcile_drives", lambda: None)

    class FakeApi:
        def __init__(self, *_args, **_kwargs):
            pass

        def health(self):
            return {"status": "ok"}

        def pairing_status(self):
            raise ClientApiError(403, "device token is invalid")

    monkeypatch.setattr("cloud_storage.client.window.ClientApi", FakeApi)
    try:
        window.refresh_connection()
        task = tasks.pop()
        task[1](task[0]())
        assert store.load().device_status == "reconnect_required"
        assert DeviceTokenVault(tmp_path).load() is None
        assert window.sidebar_state.text() == "НУЖНО ПОДКЛЮЧЕНИЕ"
        assert "заново" in window.connection_title.text().casefold()
    finally:
        window.close()
        app.processEvents()


def test_client_api_pairing_and_file_round_trip(tmp_path) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = CoreConfig(data_directory=tmp_path / "core", port=port, max_upload_bytes=1024**2)
    app = create_app(config)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="critical",
            access_log=False,
            log_config=None,
            lifespan="off",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started

    try:
        manager = TestClient(app)
        manager_headers = {"Authorization": f"Bearer {app.state.runtime.secrets.manager_token}"}
        created = manager.post(
            "/v1/admin/users",
            headers=manager_headers,
            json={"username": "clientqa", "display_name": "Client QA", "quota_gib": 1},
        ).json()
        invitation = manager.post(
            "/v1/admin/invitations",
            headers=manager_headers,
            json={"user_id": created["user"]["id"]},
        ).json()

        pairing_api = ClientApi(f"http://127.0.0.1:{port}")
        paired = pairing_api.redeem_invitation(
            invitation["code"],
            "client qa secure password",
            "QA workstation",
            "Windows",
        )
        client = ClientApi(f"http://127.0.0.1:{port}", token=paired["device_token"])
        assert client.pairing_status()["status"] == "pending"

        manager.post(
            f"/v1/admin/devices/{paired['device']['id']}/approve",
            headers=manager_headers,
        ).raise_for_status()
        space = client.list_spaces()[0]
        source = tmp_path / "source.txt"
        source.write_text("desktop client round trip", encoding="utf-8")
        uploaded = client.upload_file(space["id"], "Документы/source.txt", source)
        assert uploaded["version"] == 1
        assert client.list_entries(space["id"], "Документы")[0]["name"] == "source.txt"

        destination = tmp_path / "download" / "source.txt"
        partial = destination.with_name(destination.name + ".part")
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial.write_bytes(b"desktop ")
        client.download_file(space["id"], "Документы/source.txt", destination)
        assert destination.read_text(encoding="utf-8") == "desktop client round trip"

        resumable_payload = b"chunk-one|chunk-two|chunk-three"
        resumable_hash = hashlib.sha256(resumable_payload).hexdigest()
        upload = client.create_resumable_upload(
            space["id"],
            "Документы/resume.bin",
            len(resumable_payload),
            sha256=resumable_hash,
        )
        first_chunk = resumable_payload[:12]
        client.append_resumable_upload(upload["id"], 0, first_chunk)
        resumed_client = ClientApi(
            f"http://127.0.0.1:{port}", token=paired["device_token"]
        )
        resumed = resumed_client.resumable_upload_status(upload["id"])
        assert resumed["received_bytes"] == len(first_chunk)
        resumed_client.append_resumable_upload(
            upload["id"], len(first_chunk), resumable_payload[len(first_chunk) :]
        )
        completed = resumed_client.complete_resumable_upload(upload["id"])
        assert completed["sha256"] == resumable_hash
        assert client.delete_file(space["id"], "Документы/source.txt")["deleted"] is True
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        assert not thread.is_alive()
