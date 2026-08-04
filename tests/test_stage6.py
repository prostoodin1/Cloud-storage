from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from cloud_storage.core.api import create_app
from cloud_storage.core.config import CoreConfig
from cloud_storage.core.repository import utc_text


def _manager_client(tmp_path):
    app = create_app(CoreConfig(data_directory=tmp_path))
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {app.state.runtime.secrets.manager_token}"}
    return app, client, headers


def _insert_incident(app, *, severity: str, check_key: str) -> str:
    incident_id = str(uuid.uuid4())
    now = utc_text()
    with app.state.runtime.database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO diagnostic_incidents(
                id, fingerprint, check_key, severity, status, component, summary,
                detail, remediation, occurrences, first_seen_at, last_seen_at,
                resolved_at, last_scan_id
            ) VALUES(?, ?, ?, ?, 'active', 'Test', ?, 'test detail',
                     'test remediation', 1, ?, ?, NULL, NULL)
            """,
            (
                incident_id,
                uuid.uuid4().hex,
                check_key,
                severity,
                f"{severity} test incident",
                now,
                now,
            ),
        )
    return incident_id


def test_builtin_integrations_and_notification_inbox_are_persistent(tmp_path) -> None:
    app, client, headers = _manager_client(tmp_path)

    overview = client.get("/v1/admin/integrations", headers=headers)
    denied = client.get("/v1/admin/integrations")
    emitted = app.state.runtime.notifications.emit(
        severity="warning",
        title="Test warning",
        message="Persistent notification",
        source="test",
    )
    listed = client.get("/v1/admin/notifications", headers=headers)
    tested = client.post(
        "/v1/admin/integrations/system-log/test", headers=headers
    )
    acknowledged = client.post(
        f"/v1/admin/notifications/{emitted['id']}/acknowledge", headers=headers
    )

    assert overview.status_code == 200
    assert overview.json()["policy"] == {
        "built_in_only": True,
        "dynamic_python_loading": False,
        "third_party_directories_scanned": False,
    }
    assert {item["id"] for item in overview.json()["plugins"]} == {
        "manager-inbox",
        "system-log",
        "zrok",
    }
    assert all(item["loads_python_code"] is False for item in overview.json()["plugins"])
    assert denied.status_code == 401
    assert listed.json()[0]["id"] == emitted["id"]
    assert {item["status"] for item in listed.json()[0]["deliveries"]} == {"delivered"}
    assert tested.json()["status"] == "delivered"
    assert "Проверка встроенной интеграции" in app.state.runtime.config.log_path.read_text(
        encoding="utf-8"
    )
    assert acknowledged.status_code == 200
    assert client.get("/v1/admin/notifications", headers=headers).json() == []
    historical = client.get(
        "/v1/admin/notifications?include_acknowledged=true", headers=headers
    ).json()
    assert historical[0]["acknowledged_at"] is not None


def test_automation_rules_cooldown_persistence_and_read_only_action(tmp_path) -> None:
    app, client, headers = _manager_client(tmp_path)
    initial = client.get("/v1/admin/automation", headers=headers).json()
    assert len(initial["rules"]) == 8
    assert all(rule["system_rule"] for rule in initial["rules"])

    created = client.post(
        "/v1/admin/automation/rules",
        headers=headers,
        json={
            "name": "Notify about warnings",
            "trigger_type": "diagnostic_warning",
            "action_type": "notify",
            "cooldown_minutes": 60,
        },
    )
    assert created.status_code == 201
    warning_rule = created.json()
    _insert_incident(app, severity="warning", check_key="test.warning")

    first = client.post("/v1/admin/automation/evaluate", headers=headers)
    second = client.post("/v1/admin/automation/evaluate", headers=headers)
    assert first.json()["matched_rules"] == 1
    assert first.json()["runs"][0]["status"] == "completed"
    assert second.json()["matched_rules"] == 0
    assert client.get("/v1/admin/notifications", headers=headers).json()[0][
        "source"
    ] == "automation"

    restarted = create_app(CoreConfig(data_directory=tmp_path))
    restarted_client = TestClient(restarted)
    restarted_headers = {
        "Authorization": f"Bearer {restarted.state.runtime.secrets.manager_token}"
    }
    persisted = restarted_client.get(
        "/v1/admin/automation", headers=restarted_headers
    ).json()
    assert any(rule["id"] == warning_rule["id"] for rule in persisted["rules"])
    assert persisted["runs"][0]["rule_id"] == warning_rule["id"]

    unconfirmed = restarted_client.post(
        "/v1/admin/automation/rules",
        headers=restarted_headers,
        json={
            "name": "Protect automatically",
            "trigger_type": "diagnostic_critical",
            "action_type": "read_only",
            "cooldown_minutes": 60,
        },
    )
    invalid_trigger = restarted_client.post(
        "/v1/admin/automation/rules",
        headers=restarted_headers,
        json={
            "name": "Invalid protection",
            "trigger_type": "storage_low",
            "action_type": "read_only",
            "cooldown_minutes": 60,
            "confirmed": True,
        },
    )
    protected = restarted_client.post(
        "/v1/admin/automation/rules",
        headers=restarted_headers,
        json={
            "name": "Protect automatically",
            "trigger_type": "diagnostic_critical",
            "action_type": "read_only",
            "cooldown_minutes": 60,
            "confirmed": True,
        },
    )
    assert unconfirmed.status_code == 400
    assert invalid_trigger.status_code == 422
    assert protected.status_code == 201

    _insert_incident(restarted, severity="critical", check_key="test.critical")
    evaluated = restarted_client.post(
        "/v1/admin/automation/evaluate", headers=restarted_headers
    )
    assert evaluated.status_code == 200
    assert evaluated.json()["matched_rules"] == 2
    assert restarted_client.get(
        "/v1/admin/server-mode", headers=restarted_headers
    ).json()["mode"] == "read_only"

    system_rule_id = next(
        rule["id"] for rule in persisted["rules"] if rule["system_rule"]
    )
    assert (
        restarted_client.delete(
            f"/v1/admin/automation/rules/{system_rule_id}",
            headers=restarted_headers,
        ).status_code
        == 409
    )


def test_extended_diagnostics_cleanup_and_protective_remediation(tmp_path) -> None:
    app, client, headers = _manager_client(tmp_path)
    warning_id = _insert_incident(
        app,
        severity="warning",
        check_key="uploads.expired",
    )
    critical_id = _insert_incident(
        app,
        severity="critical",
        check_key="security.secrets",
    )

    wrong_action = client.post(
        f"/v1/admin/diagnostics/incidents/{warning_id}/remediate",
        headers=headers,
        json={"action": "enter_read_only", "confirmed": True},
    )
    unconfirmed = client.post(
        f"/v1/admin/diagnostics/incidents/{critical_id}/remediate",
        headers=headers,
        json={"action": "enter_read_only"},
    )
    recheck = client.post(
        f"/v1/admin/diagnostics/incidents/{warning_id}/remediate",
        headers=headers,
        json={"action": "recheck"},
    )

    assert wrong_action.status_code == 409
    assert unconfirmed.status_code == 400
    assert recheck.status_code == 200
    overview = client.get("/v1/admin/diagnostics", headers=headers).json()
    assert "history" in overview
    assert "resolved_incident_count" in overview
