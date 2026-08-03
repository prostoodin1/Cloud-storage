from cloud_storage.services.audit_log import AuditLog


def test_recent_audit_events_are_newest_first(tmp_path) -> None:
    log = AuditLog(tmp_path)
    log.record("first", "Первое действие")
    log.record("second", "Второе действие", "warning")

    events = log.recent()

    assert [event.action for event in events] == ["second", "first"]
    assert events[0].severity == "warning"
