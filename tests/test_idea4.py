from __future__ import annotations

import json
import os
import time
from pathlib import Path

from fastapi.testclient import TestClient

from cloud_storage.core.api import create_app
from cloud_storage.core.config import CoreConfig, CoreSecrets


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

    restarted = TestClient(create_app(CoreConfig(data_directory=tmp_path)))
    persisted = restarted.get("/v1/admin/control-center", headers=headers).json()
    assert persisted["settings"]["interface_mode"] == "detailed"
    assert persisted["settings"]["power"]["idle_minutes"] == 35
    assert len(persisted["reports"]["schedules"]) == 1
    assert len(persisted["cells"]) == 1
    assert "test-token-not-public" not in json.dumps(persisted)


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
        "cloud-storage-access-v1"
    )
    downloaded = client.get(access["download_url"], headers=headers)
    assert downloaded.status_code == 200
    assert "attachment" in downloaded.headers["content-disposition"]
    assert downloaded.json()["one_time_code"] == access["package"]["one_time_code"]


def test_zrok_browser_policy_can_block_all_browser_access(tmp_path: Path) -> None:
    _app, manager, headers = manager_client(tmp_path, zrok_enabled=True)
    changed = manager.put(
        "/v1/admin/control-center/settings",
        headers=headers,
        json={"browser_access": "nobody"},
    )
    assert changed.status_code == 200

    internet = TestClient(_app, base_url="http://127.0.0.1:8768")
    denied = internet.get("/v1/health")
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
