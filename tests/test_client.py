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

from cloud_storage.client.api_client import ClientApi
from cloud_storage.client.settings import (
    ClientProfile,
    ClientSettingsStore,
    DeviceTokenVault,
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
    assert payload["schema_version"] == 2
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

    assert window.stack.count() == 5
    assert window.help_page.article_list.count() > 0
    assert window.discover_button.text() == "Найти в сети"
    assert window.server_selector.count() == 1
    assert window.nav_buttons[1].isEnabled() is False
    window.add_server()
    assert window.server_selector.count() == 2
    assert window.profile.profile_id != "default"
    window.close()


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
