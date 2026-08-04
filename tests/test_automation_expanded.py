from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from cloud_storage.core.api import create_app
from cloud_storage.core.config import CoreConfig
from cloud_storage.core.database import Database


def manager_client(tmp_path):
    app = create_app(CoreConfig(data_directory=tmp_path))
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {app.state.runtime.secrets.manager_token}"}
    return app, client, headers


def test_scheduler_settings_preview_and_scheduled_rule_are_persistent(tmp_path) -> None:
    app, client, headers = manager_client(tmp_path)
    settings = client.put(
        "/v1/admin/automation/settings",
        headers=headers,
        json={"enabled": False, "interval_seconds": 45},
    )
    assert settings.status_code == 200
    assert settings.json()["enabled"] is False

    created = client.post(
        "/v1/admin/automation/rules",
        headers=headers,
        json={
            "name": "Ежедневный отчёт",
            "trigger_type": "scheduled",
            "action_type": "notify",
            "cooldown_minutes": 1440,
        },
    )
    assert created.status_code == 201
    rule_id = created.json()["id"]

    preview = client.get(
        f"/v1/admin/automation/rules/{rule_id}/preview", headers=headers
    )
    assert preview.status_code == 200
    assert preview.json()["matched_rules"] == 1
    assert preview.json()["ready_rules"] == 1
    assert preview.json()["rules"][0]["would_run"] is True
    assert client.get("/v1/admin/automation", headers=headers).json()["runs"] == []

    evaluated = client.post("/v1/admin/automation/evaluate", headers=headers)
    assert evaluated.status_code == 200
    assert evaluated.json()["matched_rules"] == 1
    assert evaluated.json()["runs"][0]["status"] == "completed"
    after = client.get(
        f"/v1/admin/automation/rules/{rule_id}/preview", headers=headers
    ).json()
    assert after["rules"][0]["matched"] is True
    assert after["rules"][0]["would_run"] is False
    assert after["rules"][0]["cooldown_remaining_seconds"] > 0

    restarted = create_app(CoreConfig(data_directory=tmp_path))
    assert restarted.state.runtime.automation.settings()["enabled"] is False
    assert restarted.state.runtime.automation.settings()["interval_seconds"] == 45


def test_expanded_automation_catalog_rejects_unsafe_combinations(tmp_path) -> None:
    _app, client, headers = manager_client(tmp_path)
    overview = client.get("/v1/admin/automation", headers=headers).json()
    assert set(overview["supported_triggers"]) >= {
        "scheduled",
        "restore_failed",
        "mirror_degraded",
        "maintenance_failed",
        "pending_device",
    }
    assert set(overview["supported_actions"]) >= {
        "full_scan",
        "run_backup",
        "reconcile_mirrors",
        "restart_tunnel",
    }
    assert overview["action_types_by_trigger"]["pending_device"] == ["notify"]
    assert "restart_tunnel" in overview["action_types_by_trigger"]["tunnel_offline"]

    unsafe = client.post(
        "/v1/admin/automation/rules",
        headers=headers,
        json={
            "name": "Небезопасное правило",
            "trigger_type": "pending_device",
            "action_type": "full_scan",
            "cooldown_minutes": 60,
        },
    )
    assert unsafe.status_code == 422
    assert "not safe" in unsafe.json()["detail"]


def test_old_automation_database_is_migrated_without_losing_history(tmp_path) -> None:
    path = tmp_path / "core.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE automation_rules (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            enabled INTEGER NOT NULL,
            trigger_type TEXT NOT NULL CHECK(trigger_type IN (
                'diagnostic_warning', 'diagnostic_critical', 'storage_low',
                'backup_failed', 'tunnel_offline'
            )),
            action_type TEXT NOT NULL CHECK(action_type IN ('notify', 'quick_scan', 'read_only')),
            cooldown_seconds INTEGER NOT NULL,
            system_rule INTEGER NOT NULL,
            last_triggered_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE automation_runs (
            id TEXT PRIMARY KEY,
            rule_id TEXT NOT NULL REFERENCES automation_rules(id) ON DELETE CASCADE,
            trigger_source TEXT NOT NULL,
            status TEXT NOT NULL,
            condition_summary TEXT NOT NULL,
            action_result TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            completed_at TEXT NOT NULL
        );
        INSERT INTO automation_rules VALUES(
            'legacy-rule', 'Старое правило', 1, 'storage_low', 'notify', 3600, 0,
            NULL, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
        );
        INSERT INTO automation_runs VALUES(
            'legacy-run', 'legacy-rule', 'scheduler', 'completed', 'Старое условие',
            'notification:old', '', '2026-01-01T00:00:00+00:00',
            '2026-01-01T00:00:01+00:00'
        );
        """
    )
    connection.commit()
    connection.close()

    Database(path).initialize()
    migrated = sqlite3.connect(path)
    try:
        assert migrated.execute(
            "SELECT name FROM automation_rules WHERE id = 'legacy-rule'"
        ).fetchone()[0] == "Старое правило"
        assert migrated.execute(
            "SELECT action_result FROM automation_runs WHERE id = 'legacy-run'"
        ).fetchone()[0] == "notification:old"
        migrated.execute(
            """
            INSERT INTO automation_rules VALUES(
                'new-rule', 'Новое правило', 1, 'scheduled', 'full_scan', 3600, 0,
                NULL, '2026-01-02T00:00:00+00:00', '2026-01-02T00:00:00+00:00'
            )
            """
        )
        migrated.commit()
    finally:
        migrated.close()
