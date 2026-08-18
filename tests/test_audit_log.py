import json
from datetime import UTC, datetime, timedelta

from cloud_storage.services.audit_log import AuditLog


def test_recent_audit_events_are_newest_first(tmp_path) -> None:
    log = AuditLog(tmp_path)
    log.record("first", "Первое действие")
    log.record("second", "Второе действие", "warning")

    events = log.recent()

    assert [event.action for event in events] == ["second", "first"]
    assert events[0].severity == "warning"


def test_audit_log_removes_events_older_than_thirty_days(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    old = (datetime.now(UTC) - timedelta(days=31)).isoformat(timespec="seconds")
    current = datetime.now(UTC).isoformat(timespec="seconds")
    path.write_text(
        json.dumps({"timestamp": old, "action": "old", "detail": "old", "severity": "info"})
        + "\n"
        + json.dumps(
            {"timestamp": current, "action": "current", "detail": "now", "severity": "info"}
        )
        + "\n",
        encoding="utf-8",
    )

    log = AuditLog(tmp_path)

    assert [event.action for event in log.recent()] == ["current"]
