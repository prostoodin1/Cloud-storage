from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    password_hash TEXT,
    password_version INTEGER NOT NULL DEFAULT 0 CHECK(password_version >= 0),
    role TEXT NOT NULL CHECK(role IN ('admin', 'member')),
    quota_bytes INTEGER NOT NULL CHECK(quota_bytes > 0),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS spaces (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT REFERENCES users(id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('personal', 'shared', 'backup')),
    quota_bytes INTEGER NOT NULL CHECK(quota_bytes > 0),
    primary_storage_root_id TEXT REFERENCES storage_roots(id) ON DELETE SET NULL,
    fallback_storage_root_id TEXT REFERENCES storage_roots(id) ON DELETE SET NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS space_members (
    space_id TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    permission TEXT NOT NULL CHECK(permission IN ('read', 'write', 'owner')),
    can_read INTEGER NOT NULL DEFAULT 1 CHECK(can_read IN (0, 1)),
    can_upload INTEGER NOT NULL DEFAULT 0 CHECK(can_upload IN (0, 1)),
    can_modify INTEGER NOT NULL DEFAULT 0 CHECK(can_modify IN (0, 1)),
    can_delete INTEGER NOT NULL DEFAULT 0 CHECK(can_delete IN (0, 1)),
    can_share INTEGER NOT NULL DEFAULT 0 CHECK(can_share IN (0, 1)),
    PRIMARY KEY(space_id, user_id)
);

CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    platform TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK(status IN ('pending', 'trusted', 'revoked')),
    created_at TEXT NOT NULL,
    approved_at TEXT,
    last_seen_at TEXT
);

CREATE TABLE IF NOT EXISTS invitations (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash TEXT NOT NULL UNIQUE,
    purpose TEXT NOT NULL DEFAULT 'legacy' CHECK(purpose IN ('legacy', 'access_package')),
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    consumed_at TEXT,
    cancelled_at TEXT
);

CREATE TABLE IF NOT EXISTS file_shares (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    space_id TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    logical_path TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('file', 'directory')),
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS storage_roots (
    id TEXT PRIMARY KEY,
    disk_id TEXT,
    path TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    write_enabled INTEGER NOT NULL DEFAULT 1 CHECK(write_enabled IN (0, 1)),
    purpose TEXT NOT NULL DEFAULT 'primary' CHECK(purpose IN ('primary', 'backup', 'mirror')),
    priority INTEGER NOT NULL DEFAULT 50 CHECK(priority BETWEEN 0 AND 100),
    max_fill_percent INTEGER NOT NULL DEFAULT 90 CHECK(max_fill_percent BETWEEN 50 AND 99),
    min_free_bytes INTEGER NOT NULL DEFAULT 10737418240 CHECK(min_free_bytes >= 0),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    logical_path TEXT NOT NULL,
    storage_root_id TEXT NOT NULL REFERENCES storage_roots(id) ON DELETE RESTRICT,
    object_path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    sha256 TEXT NOT NULL,
    content_type TEXT NOT NULL,
    uploaded_by TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    modified_at TEXT NOT NULL,
    deleted_at TEXT,
    UNIQUE(space_id, logical_path)
);

CREATE TABLE IF NOT EXISTS directories (
    space_id TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    logical_path TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    modified_at TEXT NOT NULL,
    PRIMARY KEY(space_id, logical_path)
);

CREATE TABLE IF NOT EXISTS file_versions (
    id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    storage_root_id TEXT NOT NULL REFERENCES storage_roots(id) ON DELETE RESTRICT,
    object_path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    version INTEGER NOT NULL,
    archived_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS upload_sessions (
    id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    storage_root_id TEXT NOT NULL REFERENCES storage_roots(id) ON DELETE RESTRICT,
    logical_path TEXT NOT NULL,
    temporary_path TEXT NOT NULL,
    staging_path TEXT,
    expected_size INTEGER NOT NULL CHECK(expected_size >= 0),
    expected_sha256 TEXT,
    received_bytes INTEGER NOT NULL DEFAULT 0 CHECK(received_bytes >= 0),
    content_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'completed')),
    result_file_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transfer_settings (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    staging_enabled INTEGER NOT NULL DEFAULT 0 CHECK(staging_enabled IN (0, 1)),
    staging_path TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transfer_jobs (
    id TEXT PRIMARY KEY,
    direction TEXT NOT NULL CHECK(direction IN ('inbound', 'outbound')),
    status TEXT NOT NULL CHECK(status IN (
        'receiving', 'moving', 'sending', 'completed', 'failed', 'cancelled'
    )),
    file_id TEXT NOT NULL DEFAULT '',
    space_id TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL DEFAULT '',
    logical_path TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
    total_bytes INTEGER NOT NULL DEFAULT 0 CHECK(total_bytes >= 0),
    network_bytes INTEGER NOT NULL DEFAULT 0 CHECK(network_bytes >= 0),
    storage_bytes INTEGER NOT NULL DEFAULT 0 CHECK(storage_bytes >= 0),
    sha256 TEXT NOT NULL DEFAULT '',
    staging_path TEXT NOT NULL DEFAULT '',
    temporary_path TEXT NOT NULL DEFAULT '',
    storage_root_id TEXT NOT NULL DEFAULT '',
    object_path TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS maintenance_jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('migration')),
    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'completed', 'failed', 'cancelled')),
    source_root_id TEXT NOT NULL REFERENCES storage_roots(id) ON DELETE RESTRICT,
    target_root_id TEXT NOT NULL REFERENCES storage_roots(id) ON DELETE RESTRICT,
    space_id TEXT REFERENCES spaces(id) ON DELETE SET NULL,
    total_files INTEGER NOT NULL CHECK(total_files >= 0),
    total_bytes INTEGER NOT NULL CHECK(total_bytes >= 0),
    processed_files INTEGER NOT NULL DEFAULT 0 CHECK(processed_files >= 0),
    processed_bytes INTEGER NOT NULL DEFAULT 0 CHECK(processed_bytes >= 0),
    retained_sources INTEGER NOT NULL DEFAULT 0 CHECK(retained_sources >= 0),
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backup_jobs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'completed', 'failed', 'cancelled')),
    target_root_id TEXT NOT NULL REFERENCES storage_roots(id) ON DELETE RESTRICT,
    total_files INTEGER NOT NULL CHECK(total_files >= 0),
    total_bytes INTEGER NOT NULL CHECK(total_bytes >= 0),
    processed_files INTEGER NOT NULL DEFAULT 0 CHECK(processed_files >= 0),
    processed_bytes INTEGER NOT NULL DEFAULT 0 CHECK(processed_bytes >= 0),
    snapshot_path TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    pruned_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backup_policies (
    target_root_id TEXT PRIMARY KEY REFERENCES storage_roots(id) ON DELETE CASCADE,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0, 1)),
    interval_hours INTEGER NOT NULL DEFAULT 24 CHECK(interval_hours BETWEEN 1 AND 8760),
    keep_last INTEGER NOT NULL DEFAULT 7 CHECK(keep_last BETWEEN 1 AND 365),
    verification_root_id TEXT REFERENCES storage_roots(id) ON DELETE SET NULL,
    next_run_at TEXT,
    last_run_at TEXT,
    last_job_id TEXT REFERENCES backup_jobs(id) ON DELETE SET NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backup_verifications (
    id TEXT PRIMARY KEY,
    backup_job_id TEXT NOT NULL REFERENCES backup_jobs(id) ON DELETE RESTRICT,
    target_root_id TEXT NOT NULL REFERENCES storage_roots(id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'completed', 'failed', 'cancelled')),
    total_objects INTEGER NOT NULL DEFAULT 0 CHECK(total_objects >= 0),
    total_bytes INTEGER NOT NULL DEFAULT 0 CHECK(total_bytes >= 0),
    checked_objects INTEGER NOT NULL DEFAULT 0 CHECK(checked_objects >= 0),
    checked_bytes INTEGER NOT NULL DEFAULT 0 CHECK(checked_bytes >= 0),
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mirror_replicas (
    file_id TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    storage_root_id TEXT NOT NULL REFERENCES storage_roots(id) ON DELETE RESTRICT,
    object_path TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0 CHECK(size_bytes >= 0),
    sha256 TEXT NOT NULL DEFAULT '',
    source_version INTEGER NOT NULL DEFAULT 0 CHECK(source_version >= 0),
    status TEXT NOT NULL CHECK(status IN ('current', 'stale', 'error')),
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(file_id, storage_root_id)
);

CREATE TABLE IF NOT EXISTS mirror_jobs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'completed', 'failed', 'cancelled')),
    target_root_id TEXT NOT NULL REFERENCES storage_roots(id) ON DELETE RESTRICT,
    total_files INTEGER NOT NULL CHECK(total_files >= 0),
    total_bytes INTEGER NOT NULL CHECK(total_bytes >= 0),
    processed_files INTEGER NOT NULL DEFAULT 0 CHECK(processed_files >= 0),
    processed_bytes INTEGER NOT NULL DEFAULT 0 CHECK(processed_bytes >= 0),
    healthy_files INTEGER NOT NULL DEFAULT 0 CHECK(healthy_files >= 0),
    repaired_files INTEGER NOT NULL DEFAULT 0 CHECK(repaired_files >= 0),
    failed_files INTEGER NOT NULL DEFAULT 0 CHECK(failed_files >= 0),
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS diagnostic_scans (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('quick', 'full')),
    source TEXT NOT NULL CHECK(source IN ('monitor', 'manager', 'startup')),
    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'completed', 'failed')),
    checks INTEGER NOT NULL DEFAULT 0 CHECK(checks >= 0),
    checked_objects INTEGER NOT NULL DEFAULT 0 CHECK(checked_objects >= 0),
    checked_bytes INTEGER NOT NULL DEFAULT 0 CHECK(checked_bytes >= 0),
    warning_count INTEGER NOT NULL DEFAULT 0 CHECK(warning_count >= 0),
    critical_count INTEGER NOT NULL DEFAULT 0 CHECK(critical_count >= 0),
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    completed_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS diagnostic_incidents (
    id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL UNIQUE,
    check_key TEXT NOT NULL,
    severity TEXT NOT NULL CHECK(severity IN ('warning', 'critical')),
    status TEXT NOT NULL CHECK(status IN ('active', 'resolved')),
    component TEXT NOT NULL,
    summary TEXT NOT NULL,
    detail TEXT NOT NULL,
    remediation TEXT NOT NULL,
    occurrences INTEGER NOT NULL DEFAULT 1 CHECK(occurrences >= 1),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    resolved_at TEXT,
    last_scan_id TEXT REFERENCES diagnostic_scans(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS server_state (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    mode TEXT NOT NULL CHECK(mode IN ('normal', 'read_only')),
    reason TEXT NOT NULL DEFAULT '',
    changed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS restore_jobs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'completed', 'failed', 'cancelled')),
    backup_job_id TEXT NOT NULL REFERENCES backup_jobs(id) ON DELETE RESTRICT,
    target_root_id TEXT NOT NULL REFERENCES storage_roots(id) ON DELETE RESTRICT,
    total_objects INTEGER NOT NULL DEFAULT 0 CHECK(total_objects >= 0),
    total_bytes INTEGER NOT NULL DEFAULT 0 CHECK(total_bytes >= 0),
    processed_objects INTEGER NOT NULL DEFAULT 0 CHECK(processed_objects >= 0),
    processed_bytes INTEGER NOT NULL DEFAULT 0 CHECK(processed_bytes >= 0),
    restored_objects INTEGER NOT NULL DEFAULT 0 CHECK(restored_objects >= 0),
    skipped_objects INTEGER NOT NULL DEFAULT 0 CHECK(skipped_objects >= 0),
    failed_objects INTEGER NOT NULL DEFAULT 0 CHECK(failed_objects >= 0),
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    severity TEXT NOT NULL CHECK(severity IN ('info', 'warning', 'critical')),
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    source TEXT NOT NULL,
    source_key TEXT NOT NULL DEFAULT '',
    acknowledged_at TEXT
);

CREATE TABLE IF NOT EXISTS notification_deliveries (
    notification_id TEXT NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,
    provider_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('delivered', 'failed')),
    error TEXT NOT NULL DEFAULT '',
    attempted_at TEXT NOT NULL,
    PRIMARY KEY(notification_id, provider_id)
);

CREATE TABLE IF NOT EXISTS automation_rules (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    trigger_type TEXT NOT NULL CHECK(trigger_type IN (
        'diagnostic_warning', 'diagnostic_critical', 'storage_low',
        'backup_failed', 'restore_failed', 'mirror_degraded',
        'maintenance_failed', 'pending_device', 'tunnel_offline', 'power_outage',
        'scheduled'
    )),
    action_type TEXT NOT NULL CHECK(action_type IN (
        'notify', 'quick_scan', 'full_scan', 'read_only', 'run_backup',
        'reconcile_mirrors', 'restart_tunnel', 'sleep_after_hour',
        'shutdown_after_hour'
    )),
    cooldown_seconds INTEGER NOT NULL DEFAULT 3600 CHECK(cooldown_seconds BETWEEN 60 AND 604800),
    system_rule INTEGER NOT NULL DEFAULT 0 CHECK(system_rule IN (0, 1)),
    last_triggered_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS automation_runs (
    id TEXT PRIMARY KEY,
    rule_id TEXT NOT NULL REFERENCES automation_rules(id) ON DELETE CASCADE,
    trigger_source TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('completed', 'failed', 'skipped')),
    condition_summary TEXT NOT NULL,
    action_result TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    completed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS automation_settings (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    interval_seconds INTEGER NOT NULL DEFAULT 60 CHECK(interval_seconds BETWEEN 10 AND 3600),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS control_settings (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    settings_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS report_schedules (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    interval_hours INTEGER NOT NULL CHECK(interval_hours BETWEEN 1 AND 8760),
    sections_json TEXT NOT NULL,
    delivery_channels_json TEXT NOT NULL,
    next_run_at TEXT,
    last_run_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS report_runs (
    id TEXT PRIMARY KEY,
    schedule_id TEXT REFERENCES report_schedules(id) ON DELETE SET NULL,
    status TEXT NOT NULL CHECK(status IN ('completed', 'failed')),
    sections_json TEXT NOT NULL,
    report_json TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sandbox_cells (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    image TEXT NOT NULL,
    command_json TEXT NOT NULL,
    cpu_limit REAL NOT NULL CHECK(cpu_limit > 0),
    memory_mib INTEGER NOT NULL CHECK(memory_mib BETWEEN 64 AND 1048576),
    storage_mib INTEGER NOT NULL CHECK(storage_mib BETWEEN 64 AND 10485760),
    timeout_seconds INTEGER NOT NULL CHECK(timeout_seconds BETWEEN 1 AND 86400),
    network_enabled INTEGER NOT NULL DEFAULT 0 CHECK(network_enabled IN (0, 1)),
    status TEXT NOT NULL CHECK(status IN ('ready', 'running', 'completed', 'failed', 'unsupported')),
    last_result TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    actor_type TEXT NOT NULL,
    actor_id TEXT,
    action TEXT NOT NULL,
    target_type TEXT,
    target_id TEXT,
    detail TEXT NOT NULL,
    remote_address TEXT
);

CREATE INDEX IF NOT EXISTS idx_devices_user ON devices(user_id);
CREATE INDEX IF NOT EXISTS idx_invitations_user ON invitations(user_id);
CREATE INDEX IF NOT EXISTS idx_file_shares_owner ON file_shares(owner_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_file_shares_expiry ON file_shares(expires_at, revoked_at);
CREATE INDEX IF NOT EXISTS idx_files_space_path ON files(space_id, logical_path);
CREATE INDEX IF NOT EXISTS idx_directories_space_path ON directories(space_id, logical_path);
CREATE INDEX IF NOT EXISTS idx_upload_sessions_user ON upload_sessions(user_id, status);
CREATE INDEX IF NOT EXISTS idx_upload_sessions_expiry ON upload_sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_transfer_jobs_direction_created
    ON transfer_jobs(direction, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_transfer_jobs_status
    ON transfer_jobs(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_maintenance_jobs_status ON maintenance_jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_backup_jobs_status ON backup_jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_backup_verifications_created
    ON backup_verifications(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_mirror_replicas_status
    ON mirror_replicas(storage_root_id, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_mirror_jobs_status ON mirror_jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_diagnostic_scans_created
    ON diagnostic_scans(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_diagnostic_incidents_status
    ON diagnostic_incidents(status, severity, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_restore_jobs_status ON restore_jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_notifications_created ON notifications(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notifications_acknowledged
    ON notifications(acknowledged_at, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_automation_rules_enabled
    ON automation_rules(enabled, trigger_type);
CREATE INDEX IF NOT EXISTS idx_automation_runs_created
    ON automation_runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_report_schedules_due
    ON report_schedules(enabled, next_run_at);
CREATE INDEX IF NOT EXISTS idx_report_runs_created
    ON report_runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sandbox_cells_updated
    ON sandbox_cells(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_events(timestamp DESC);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.executescript(SCHEMA)
            root_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(storage_roots)").fetchall()
            }
            if "write_enabled" not in root_columns:
                connection.execute(
                    "ALTER TABLE storage_roots ADD COLUMN write_enabled INTEGER NOT NULL DEFAULT 1 "
                    "CHECK(write_enabled IN (0, 1))"
                )
            if "purpose" not in root_columns:
                connection.execute(
                    "ALTER TABLE storage_roots ADD COLUMN purpose TEXT NOT NULL DEFAULT 'primary' "
                    "CHECK(purpose IN ('primary', 'backup', 'mirror'))"
                )
            backup_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(backup_jobs)").fetchall()
            }
            if "pruned_at" not in backup_columns:
                connection.execute("ALTER TABLE backup_jobs ADD COLUMN pruned_at TEXT")
            upload_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(upload_sessions)").fetchall()
            }
            if "staging_path" not in upload_columns:
                connection.execute("ALTER TABLE upload_sessions ADD COLUMN staging_path TEXT")
            connection.execute(
                "UPDATE upload_sessions SET staging_path = '' WHERE staging_path IS NULL"
            )
            user_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(users)").fetchall()
            }
            if "password_version" not in user_columns:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN password_version INTEGER NOT NULL DEFAULT 0 "
                    "CHECK(password_version >= 0)"
                )
            if "email" not in user_columns:
                connection.execute("ALTER TABLE users ADD COLUMN email TEXT NOT NULL DEFAULT ''")
            space_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(spaces)").fetchall()
            }
            for column, definition in (
                ("primary_storage_root_id", "TEXT REFERENCES storage_roots(id) ON DELETE SET NULL"),
                ("fallback_storage_root_id", "TEXT REFERENCES storage_roots(id) ON DELETE SET NULL"),
                ("enabled", "INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1))"),
            ):
                if column not in space_columns:
                    connection.execute(f"ALTER TABLE spaces ADD COLUMN {column} {definition}")
            member_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(space_members)").fetchall()
            }
            member_permissions_added = False
            for column in ("can_read", "can_upload", "can_modify", "can_delete", "can_share"):
                if column not in member_columns:
                    member_permissions_added = True
                    default = 1 if column == "can_read" else 0
                    connection.execute(
                        f"ALTER TABLE space_members ADD COLUMN {column} INTEGER NOT NULL "
                        f"DEFAULT {default} CHECK({column} IN (0, 1))"
                    )
            if member_permissions_added:
                connection.execute(
                    """
                    UPDATE space_members
                    SET can_read = 1,
                        can_upload = CASE WHEN permission IN ('write', 'owner') THEN 1 ELSE 0 END,
                        can_modify = CASE WHEN permission IN ('write', 'owner') THEN 1 ELSE 0 END,
                        can_delete = CASE WHEN permission IN ('write', 'owner') THEN 1 ELSE 0 END,
                        can_share = CASE WHEN permission IN ('write', 'owner') THEN 1 ELSE 0 END
                    """
                )
            invitation_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(invitations)").fetchall()
            }
            if "purpose" not in invitation_columns:
                connection.execute(
                    "ALTER TABLE invitations ADD COLUMN purpose TEXT NOT NULL DEFAULT 'legacy' "
                    "CHECK(purpose IN ('legacy', 'access_package'))"
                )
            maintenance_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(maintenance_jobs)").fetchall()
            }
            if "space_id" not in maintenance_columns:
                connection.execute(
                    "ALTER TABLE maintenance_jobs ADD COLUMN space_id TEXT "
                    "REFERENCES spaces(id) ON DELETE SET NULL"
                )
            self._upgrade_automation_schema(connection)
            connection.execute(
                "INSERT OR IGNORE INTO automation_settings(id, enabled, interval_seconds, updated_at) "
                "VALUES(1, 1, 60, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO transfer_settings("
                "id, staging_enabled, staging_path, updated_at) "
                "VALUES(1, 0, '', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
                "VALUES(1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
                "VALUES(2, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
                "VALUES(3, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
                "VALUES(4, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
                "VALUES(5, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
                "VALUES(6, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO server_state(id, mode, reason, changed_at) "
                "VALUES(1, 'normal', '', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
                "VALUES(7, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
                "VALUES(8, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
                "VALUES(9, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
                "VALUES(10, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
                "VALUES(11, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
                "VALUES(12, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
                "VALUES(13, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
                "VALUES(14, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
                "VALUES(15, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
                "VALUES(16, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
                "VALUES(17, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
                "VALUES(18, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
            )
            device_columns = {row[1] for row in connection.execute("PRAGMA table_info(devices)")}
            if "pairing_method" not in device_columns:
                connection.execute(
                    "ALTER TABLE devices ADD COLUMN pairing_method TEXT NOT NULL DEFAULT 'legacy' "
                    "CHECK(pairing_method IN ('legacy', 'dynamic'))"
                )
                # Never infer passwordless authorization from an absent password.
                connection.execute(
                    "UPDATE devices SET pairing_method = 'dynamic' WHERE id IN ("
                    "SELECT actor_id FROM audit_events WHERE actor_type = 'device' "
                    "AND action = 'device.dynamic_pairing.completed')"
                )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
                "VALUES(19, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS web_sessions ("
                "token_hash TEXT PRIMARY KEY, "
                "device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE, "
                "expires_at INTEGER NOT NULL)"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
                "VALUES(20, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
            )
            connection.commit()

    @staticmethod
    def _upgrade_automation_schema(connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'automation_rules'"
        ).fetchone()
        if row is None or all(
            marker in str(row["sql"])
            for marker in ("scheduled", "sleep_after_hour", "power_outage", "shutdown_after_hour")
        ):
            return
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                ALTER TABLE automation_runs RENAME TO automation_runs_legacy;
                ALTER TABLE automation_rules RENAME TO automation_rules_legacy;
                CREATE TABLE automation_rules (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
                    trigger_type TEXT NOT NULL CHECK(trigger_type IN (
                        'diagnostic_warning', 'diagnostic_critical', 'storage_low',
                        'backup_failed', 'restore_failed', 'mirror_degraded',
                        'maintenance_failed', 'pending_device', 'tunnel_offline',
                        'power_outage', 'scheduled'
                    )),
                    action_type TEXT NOT NULL CHECK(action_type IN (
                        'notify', 'quick_scan', 'full_scan', 'read_only', 'run_backup',
                        'reconcile_mirrors', 'restart_tunnel', 'sleep_after_hour',
                        'shutdown_after_hour'
                    )),
                    cooldown_seconds INTEGER NOT NULL DEFAULT 3600
                        CHECK(cooldown_seconds BETWEEN 60 AND 604800),
                    system_rule INTEGER NOT NULL DEFAULT 0 CHECK(system_rule IN (0, 1)),
                    last_triggered_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE automation_runs (
                    id TEXT PRIMARY KEY,
                    rule_id TEXT NOT NULL REFERENCES automation_rules(id) ON DELETE CASCADE,
                    trigger_source TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('completed', 'failed', 'skipped')),
                    condition_summary TEXT NOT NULL,
                    action_result TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL
                );
                INSERT INTO automation_rules SELECT * FROM automation_rules_legacy;
                INSERT INTO automation_runs SELECT * FROM automation_runs_legacy;
                DROP TABLE automation_runs_legacy;
                DROP TABLE automation_rules_legacy;
                CREATE INDEX IF NOT EXISTS idx_automation_rules_enabled
                    ON automation_rules(enabled, trigger_type);
                CREATE INDEX IF NOT EXISTS idx_automation_runs_created
                    ON automation_runs(created_at DESC);
                COMMIT;
                """
            )
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
