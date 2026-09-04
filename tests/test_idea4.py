from __future__ import annotations

import json
import os
import time
from pathlib import Path

from fastapi.testclient import TestClient

from cloud_storage.core.api import create_app
from cloud_storage.core.config import CoreConfig, CoreSecrets
from cloud_storage.core.integrations import EmailProvider
from cloud_storage.pairing import build_server_code, parse_server_code


def manager_client(tmp_path: Path, **config_values):
    config = CoreConfig(data_directory=tmp_path, **config_values)
    app = create_app(config)
    client = TestClient(app)
    token = CoreSecrets.load_or_create(config).manager_token
    return app, client, {"Authorization": f"Bearer {token}"}


def test_idea4_control_center_persists_profiles_templates_reports_and_cells(
    tmp_path: Path,
) -> None:
    _app, client, headers = manager_client(tmp_path)

    overview = client.get("/v1/admin/control-center", headers=headers)
    assert overview.status_code == 200
    body = overview.json()
    assert len(body["presets"]) == 8
    assert len(body["automation_templates"]) == 20
    assert {item["category"] for item in body["automation_templates"]} == {
        "emergency",
        "normal",
    }
    assert sum(item["category"] == "emergency" for item in body["automation_templates"]) == 10
    assert sum(item["category"] == "normal" for item in body["automation_templates"]) == 10
    power_template = next(
        item
        for item in body["automation_templates"]
        if item["id"] == "emergency-power-shutdown"
    )
    assert power_template["rule"] == {
        "name": "Аварийное выключение без питания",
        "trigger_type": "power_outage",
        "action_type": "shutdown_after_hour",
        "cooldown_seconds": 3600,
    }
    assert body["sandbox"]["host_execution_allowed"] is False
    assert body["network"]["manager_api_exposed"] is False

    changed = client.put(
        "/v1/admin/control-center/settings",
        headers=headers,
        json={
            "interface_mode": "detailed",
            "browser_access": "nobody",
            "security": {"mode": "advanced"},
            "power": {"idle_minutes": 35},
            "integrations": {"telegram": {"enabled": True, "chat_id": "12345"}},
            "secrets": {"telegram_bot_token": "test-token-not-public"},
        },
    )
    assert changed.status_code == 200
    assert changed.json()["integrations"]["telegram"]["token_configured"] is True
    assert "test-token-not-public" not in changed.text

    installed = client.post(
        "/v1/admin/control-center/automations/emergency-critical-alert/install",
        headers=headers,
    )
    assert installed.status_code == 201
    assert installed.json()["rule"]["trigger_type"] == "diagnostic_critical"

    scheduled = client.post(
        "/v1/admin/control-center/reports",
        headers=headers,
        json={
            "name": "Daily server report",
            "enabled": True,
            "interval_hours": 24,
            "sections": ["system", "storage", "users", "power"],
            "delivery_channels": ["manager-inbox"],
        },
    )
    assert scheduled.status_code == 201
    assert scheduled.json()["next_run_at"]

    generated = client.post(
        "/v1/admin/control-center/reports/run",
        headers=headers,
        json={"sections": ["system", "storage"], "delivery_channels": []},
    )
    assert generated.status_code == 200
    assert generated.json()["status"] == "completed"
    assert "memory_bytes" in generated.json()["report"]["system"]

    maximum = body["sandbox"]["automatic_max"]
    cell = client.post(
        "/v1/admin/control-center/cells",
        headers=headers,
        json={
            "name": "isolated test",
            "image": "python:3.12-alpine",
            "command": ["python", "-c", "print('ok')"],
            "cpu_limit": min(0.25, maximum["cpu"]),
            "memory_mib": min(128, maximum["memory_mib"]),
            "storage_mib": 128,
            "timeout_seconds": 30,
            "network_enabled": False,
        },
    )
    assert cell.status_code == 201
    assert cell.json()["status"] in {"ready", "unsupported"}
    assert cell.json()["network_enabled"] is False

    updated_payload = {
        "name": "telegram bot space",
        "image": "python:3.12-alpine",
        "command": ["python", "-c", "print('updated')"],
        "cpu_limit": min(0.25, maximum["cpu"]),
        "memory_mib": min(128, maximum["memory_mib"]),
        "storage_mib": 256,
        "timeout_seconds": 60,
        "network_enabled": True,
    }
    updated = client.put(
        f"/v1/admin/control-center/cells/{cell.json()['id']}",
        headers=headers,
        json=updated_payload,
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "telegram bot space"
    assert updated.json()["network_enabled"] is True

    restarted = TestClient(create_app(CoreConfig(data_directory=tmp_path)))
    persisted = restarted.get("/v1/admin/control-center", headers=headers).json()
    assert persisted["settings"]["interface_mode"] == "detailed"
    assert persisted["settings"]["power"]["idle_minutes"] == 35
    assert len(persisted["reports"]["schedules"]) == 1
    assert len(persisted["cells"]) == 1
    assert "test-token-not-public" not in json.dumps(persisted)

    deleted = client.delete(
        f"/v1/admin/control-center/cells/{cell.json()['id']}", headers=headers
    )
    assert deleted.status_code == 204
    assert client.get("/v1/admin/control-center/cells", headers=headers).json() == []


def test_new_user_gets_personal_drive_and_downloadable_one_time_access_file(
    tmp_path: Path,
) -> None:
    _app, client, headers = manager_client(tmp_path)
    created = client.post(
        "/v1/admin/users",
        headers=headers,
        json={
            "username": "idea4-user",
            "display_name": "Idea Four",
            "quota_gib": 25,
            "password": "correct horse battery staple",
            "prepare_access": True,
        },
    )
    assert created.status_code == 201
    body = created.json()
    assert body["personal_space"]["kind"] == "personal"
    access = body["access_package"]
    assert access["package"]["username"] == "idea4-user"
    assert access["package"]["personal_drive_name"].startswith("Личный диск")
    assert len(access["package"]["one_time_code"]) >= 8

    package_path = Path(access["download_path"])
    assert package_path.is_file()
    assert json.loads(package_path.read_text(encoding="utf-8"))["format"] == (
        "cloud-storage-access-v2"
    )
    downloaded = client.get(access["download_url"], headers=headers)
    assert downloaded.status_code == 200
    assert "attachment" in downloaded.headers["content-disposition"]
    assert downloaded.json()["one_time_code"] == access["package"]["one_time_code"]


def test_access_package_has_no_password_is_single_use_and_revokes_older_file(
    tmp_path: Path,
) -> None:
    app, client, headers = manager_client(tmp_path)
    created = client.post(
        "/v1/admin/users",
        headers=headers,
        json={
            "username": "one-time-user",
            "display_name": "One Time",
            "quota_gib": 10,
            "password": "permanent password remains server side",
            "prepare_access": True,
        },
    ).json()
    first = created["access_package"]["package"]
    assert "password" not in first

    second_response = client.post(
        f"/v1/admin/users/{created['user']['id']}/access-package",
        headers=headers,
        json={"ttl_seconds": 604800},
    )
    assert second_response.status_code == 201
    second = second_response.json()["package"]

    request_body = {
        "device_name": "Fresh laptop",
        "platform": "Windows",
    }
    revoked = client.post(
        "/v1/pairing/redeem", json={**request_body, "code": first["one_time_code"]}
    )
    paired = client.post(
        "/v1/pairing/redeem", json={**request_body, "code": second["one_time_code"]}
    )
    replay = client.post(
        "/v1/pairing/redeem", json={**request_body, "code": second["one_time_code"]}
    )

    assert revoked.status_code == 422
    assert paired.status_code == 201
    assert paired.json()["device"]["status"] == "pending"
    assert replay.status_code == 422
    assert app.state.runtime.repository.summary()["pending_devices"] == 1


def test_duplicate_login_is_localized_and_space_rights_are_enforced_by_core(
    tmp_path: Path,
) -> None:
    _app, client, headers = manager_client(tmp_path)
    user_body = {
        "username": "rights-user",
        "display_name": "Rights User",
        "quota_gib": 10,
        "prepare_access": False,
    }
    created = client.post("/v1/admin/users", headers=headers, json=user_body)
    duplicate = client.post("/v1/admin/users", headers=headers, json=user_body)
    assert duplicate.status_code == 409
    assert duplicate.json() == {
        "code": "username_exists",
        "detail": "Этот логин уже используется",
    }

    user_id = created.json()["user"]["id"]
    space = client.post(
        "/v1/admin/spaces",
        headers=headers,
        json={"name": "Команда", "quota_gib": 50},
    ).json()
    grant = client.put(
        f"/v1/admin/spaces/{space['id']}/members/{user_id}",
        headers=headers,
        json={
            "read": True,
            "upload": False,
            "modify": False,
            "delete": False,
            "share": False,
        },
    )
    assert grant.status_code == 200

    access = client.post(
        f"/v1/admin/users/{user_id}/access-package",
        headers=headers,
        json={"ttl_seconds": 604800},
    ).json()["package"]
    paired = client.post(
        "/v1/pairing/redeem",
        json={
            "code": access["one_time_code"],
            "device_name": "Rights laptop",
            "platform": "Windows",
        },
    ).json()
    client.post(
        f"/v1/admin/devices/{paired['device']['id']}/approve", headers=headers
    )
    device_headers = {"Authorization": f"Bearer {paired['device_token']}"}

    visible = client.get("/v1/spaces", headers=device_headers).json()
    shared = next(item for item in visible if item["id"] == space["id"])
    assert shared["can_read"] is True
    assert shared["can_upload"] is False
    assert client.get(
        f"/v1/spaces/{space['id']}/entries", headers=device_headers
    ).status_code == 200
    assert client.put(
        f"/v1/spaces/{space['id']}/files/forbidden.txt",
        headers=device_headers,
        content=b"blocked",
    ).status_code == 403
    assert client.post(
        "/v1/shares",
        headers=device_headers,
        json={"space_id": space["id"], "logical_path": "forbidden.txt", "kind": "file"},
    ).status_code == 403


def test_cs2_server_code_hides_transport_details_but_round_trips() -> None:
    code = build_server_code(
        "https://cloud.example:8766",
        "AA:" * 31 + "AA",
        "anna",
        alternate_addresses=["https://192.0.2.10:8766"],
    )
    assert code.startswith("CS2.")
    assert "cloud.example" not in code
    locator = parse_server_code(code)
    assert locator.primary_address == "https://cloud.example:8766"
    assert locator.username == "anna"
    assert locator.certificate_fingerprint == "aa" * 32
    assert locator.addresses[1] == "https://192.0.2.10:8766"


def test_smtp_access_file_is_sent_as_real_attachment(monkeypatch) -> None:
    delivered = []

    class FakeSmtp:
        def __init__(self, host, port, timeout):
            assert (host, port, timeout) == ("smtp.example", 587, 10)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def starttls(self):
            return None

        def login(self, username, password):
            assert (username, password) == ("mailer", "secret")

        def send_message(self, message):
            delivered.append(message)

    monkeypatch.setattr("cloud_storage.core.integrations.smtplib.SMTP", FakeSmtp)
    provider = EmailProvider(
        host="smtp.example",
        port=587,
        username="mailer",
        password="secret",
        sender="cloud@example.com",
        recipient="",
    )
    provider.deliver(
        {
            "recipient": "user@example.com",
            "title": "Доступ",
            "message": "Импортируйте вложенный файл.",
            "attachments": [
                {
                    "filename": "login.cloud-access.json",
                    "content": b'{"format":"cloud-storage-access-v2"}',
                    "content_type": "application/json",
                }
            ],
        }
    )

    assert len(delivered) == 1
    message = delivered[0]
    attachment = next(message.iter_attachments())
    assert attachment.get_filename() == "login.cloud-access.json"
    assert attachment.get_content_type() == "application/json"
    assert attachment.get_payload(decode=True) == b'{"format":"cloud-storage-access-v2"}'


def test_space_falls_back_and_manual_migration_returns_verified_file_to_primary(
    tmp_path: Path,
) -> None:
    app, client, headers = manager_client(tmp_path)
    primary_parent = tmp_path / "primary-disk"
    fallback_parent = tmp_path / "fallback-disk"
    primary_parent.mkdir()
    fallback_parent.mkdir()
    primary_path = primary_parent / "CloudStorageData"
    fallback_path = fallback_parent / "CloudStorageData"

    def configure(primary_enabled: bool) -> list[dict]:
        response = client.put(
            "/v1/admin/storage-roots",
            headers=headers,
            json={
                "roots": [
                    {
                        "disk_id": "disk-primary-test",
                        "path": str(primary_path),
                        "priority": 90,
                        "max_fill_percent": 99,
                        "min_free_gib": 0,
                        "write_enabled": primary_enabled,
                        "purpose": "primary",
                    },
                    {
                        "disk_id": "disk-fallback-test",
                        "path": str(fallback_path),
                        "priority": 50,
                        "max_fill_percent": 99,
                        "min_free_gib": 0,
                        "write_enabled": True,
                        "purpose": "primary",
                    },
                ]
            },
        )
        assert response.status_code == 200
        return response.json()

    roots = configure(False)
    primary_id = next(item["id"] for item in roots if item["disk_id"] == "disk-primary-test")
    fallback_id = next(
        item["id"] for item in roots if item["disk_id"] == "disk-fallback-test"
    )
    space = client.post(
        "/v1/admin/spaces",
        headers=headers,
        json={
            "name": "Фото",
            "quota_gib": 10,
            "primary_storage_root_id": primary_id,
            "fallback_storage_root_id": fallback_id,
        },
    ).json()
    created = client.post(
        "/v1/admin/users",
        headers=headers,
        json={
            "username": "fallback-user",
            "display_name": "Fallback User",
            "quota_gib": 10,
            "create_personal_space": False,
            "space_grants": [
                {
                    "space_id": space["id"],
                    "capabilities": {
                        "read": True,
                        "upload": True,
                        "modify": True,
                        "delete": True,
                        "share": True,
                    },
                }
            ],
        },
    ).json()
    package = created["access_package"]["package"]
    paired = client.post(
        "/v1/pairing/redeem",
        json={
            "code": package["one_time_code"],
            "device_name": "Fallback laptop",
            "platform": "Windows",
        },
    ).json()
    client.post(
        f"/v1/admin/devices/{paired['device']['id']}/approve", headers=headers
    )
    device_headers = {"Authorization": f"Bearer {paired['device_token']}"}
    payload = b"verified fallback payload"
    uploaded = client.put(
        f"/v1/spaces/{space['id']}/files/photo.bin",
        headers=device_headers,
        content=payload,
    )
    assert uploaded.status_code == 201
    assert uploaded.json()["storage_root_id"] == fallback_id

    configure(True)
    migration = client.post(
        "/v1/admin/maintenance/migrations",
        headers=headers,
        json={
            "source_root_id": fallback_id,
            "target_root_id": primary_id,
            "space_id": space["id"],
        },
    )
    assert migration.status_code == 202
    job = client.get(
        f"/v1/admin/maintenance/jobs/{migration.json()['id']}", headers=headers
    ).json()
    assert job["status"] == "completed"
    record = app.state.runtime.storage.find_file(space["id"], "photo.bin")
    assert record is not None
    assert record.storage_root_id == primary_id
    physical = app.state.runtime.storage.resolve_download(
        space["id"], created["user"]["id"], "photo.bin"
    )[1]
    assert physical.read_bytes() == payload
    assert any(
        event["action"] == "storage.migration.completed"
        for event in app.state.runtime.repository.recent_audit(50)
    )


def test_zrok_browser_policy_can_block_all_browser_access(tmp_path: Path) -> None:
    _app, manager, headers = manager_client(tmp_path, zrok_enabled=True)
    changed = manager.put(
        "/v1/admin/control-center/settings",
        headers=headers,
        json={"browser_access": "nobody"},
    )
    assert changed.status_code == 200

    internet = TestClient(_app, base_url="http://127.0.0.1:8768")
    assert internet.get("/v1/health").status_code == 200
    denied = internet.get("/")
    assert denied.status_code == 403
    assert "administrator policy" in denied.json()["detail"]


def test_operating_system_sleep_requires_explicit_core_confirmation(tmp_path: Path) -> None:
    _app, client, headers = manager_client(tmp_path)
    denied = client.put(
        "/v1/admin/control-center/settings",
        headers=headers,
        json={"power": {"allow_os_sleep": True}},
    )
    assert denied.status_code == 409

    accepted = client.put(
        "/v1/admin/control-center/settings",
        headers=headers,
        json={"power": {"allow_os_sleep": True}, "confirmed": True},
    )
    assert accepted.status_code == 200
    assert accepted.json()["power"]["allow_os_sleep"] is True


def test_power_outage_shutdown_requires_confirmation_and_uses_real_power_trigger(
    tmp_path: Path,
) -> None:
    app, client, headers = manager_client(tmp_path)
    denied = client.put(
        "/v1/admin/control-center/settings",
        headers=headers,
        json={"power": {"allow_os_shutdown": True}},
    )
    assert denied.status_code == 409
    accepted = client.put(
        "/v1/admin/control-center/settings",
        headers=headers,
        json={"power": {"allow_os_shutdown": True}, "confirmed": True},
    )
    assert accepted.status_code == 200

    installed = client.post(
        "/v1/admin/control-center/automations/emergency-power-shutdown/install",
        headers=headers,
    )
    assert installed.status_code == 201
    actions: list[str] = []
    app.state.runtime.automation.power_state_handler = lambda: {
        "plugged": False,
        "minutes_left": 42,
    }
    app.state.runtime.automation.system_action_handler = lambda action: (
        actions.append(action) or "shutdown_scheduled:3600"
    )
    runs = app.state.runtime.automation.evaluate(trigger_source="manager")
    assert actions == ["shutdown_after_hour"]
    assert any(
        item["action_type"] == "shutdown_after_hour" and item["status"] == "completed"
        for item in runs
    )


def test_disk_cleanup_removes_only_old_unreferenced_staging_files(tmp_path: Path) -> None:
    _app, client, headers = manager_client(tmp_path)
    staging = tmp_path / "storage" / "default" / ".staging"
    old_orphan = staging / "old-orphan.part"
    fresh_orphan = staging / "fresh-orphan.part"
    old_orphan.write_bytes(b"old")
    fresh_orphan.write_bytes(b"fresh")
    old = time.time() - 2 * 24 * 60 * 60
    os.utime(old_orphan, (old, old))

    confirmation = client.post(
        "/v1/admin/storage-roots/default/cleanup",
        headers=headers,
        json={"confirmed": False},
    )
    assert confirmation.status_code == 409
    cleaned = client.post(
        "/v1/admin/storage-roots/default/cleanup",
        headers=headers,
        json={"confirmed": True},
    )
    assert cleaned.status_code == 200
    assert cleaned.json()["removed_files"] == 1
    assert not old_orphan.exists()
    assert fresh_orphan.exists()
