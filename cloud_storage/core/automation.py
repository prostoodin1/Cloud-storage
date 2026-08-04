from __future__ import annotations

import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from cloud_storage.core.backup_automation import BackupAutomationService
from cloud_storage.core.database import Database
from cloud_storage.core.diagnostics import DiagnosticsService
from cloud_storage.core.notifications import NotificationService
from cloud_storage.core.recovery import RecoveryService
from cloud_storage.core.repository import ConflictError, CoreRepository, NotFoundError, utc_text
from cloud_storage.core.storage import StorageService
from cloud_storage.core.tunnels import TunnelProviderRegistry

TRIGGER_TYPES = {
    "diagnostic_warning",
    "diagnostic_critical",
    "storage_low",
    "backup_failed",
    "restore_failed",
    "mirror_degraded",
    "maintenance_failed",
    "pending_device",
    "tunnel_offline",
    "scheduled",
}
ACTION_TYPES = {
    "notify",
    "quick_scan",
    "full_scan",
    "read_only",
    "run_backup",
    "reconcile_mirrors",
    "restart_tunnel",
}
ACTION_TYPES_BY_TRIGGER = {
    "diagnostic_warning": {"notify", "quick_scan", "full_scan"},
    "diagnostic_critical": {"notify", "quick_scan", "full_scan", "read_only"},
    "storage_low": {"notify", "quick_scan"},
    "backup_failed": {"notify", "quick_scan", "full_scan"},
    "restore_failed": {"notify", "quick_scan", "full_scan"},
    "mirror_degraded": {"notify", "quick_scan", "full_scan", "reconcile_mirrors"},
    "maintenance_failed": {"notify", "quick_scan", "full_scan"},
    "pending_device": {"notify"},
    "tunnel_offline": {"notify", "restart_tunnel"},
    "scheduled": {
        "notify",
        "quick_scan",
        "full_scan",
        "run_backup",
        "reconcile_mirrors",
    },
}


@dataclass(frozen=True, slots=True)
class AutomationRuleRecord:
    id: str
    name: str
    enabled: bool
    trigger_type: str
    action_type: str
    cooldown_seconds: int
    system_rule: bool
    last_triggered_at: str | None
    created_at: str
    updated_at: str


@dataclass(slots=True)
class AutomationService:
    database: Database
    repository: CoreRepository
    diagnostics: DiagnosticsService
    recovery: RecoveryService
    tunnels: TunnelProviderRegistry
    notifications: NotificationService
    storage: StorageService
    backup_automation: BackupAutomationService
    scheduler_interval_seconds: int = 60
    scheduler_enabled: bool = field(default=True, init=False)
    _evaluation_lock: threading.Lock = field(init=False, repr=False)
    _scheduler_guard: threading.Lock = field(init=False, repr=False)
    _scheduler_stop: threading.Event = field(init=False, repr=False)
    _scheduler_wake: threading.Event = field(init=False, repr=False)
    _scheduler_thread: threading.Thread | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        settings = self.settings()
        self.scheduler_enabled = bool(settings["enabled"])
        self.scheduler_interval_seconds = int(settings["interval_seconds"])
        self._evaluation_lock = threading.Lock()
        self._scheduler_guard = threading.Lock()
        self._scheduler_stop = threading.Event()
        self._scheduler_wake = threading.Event()
        self._scheduler_thread: threading.Thread | None = None
        self._seed_system_rules()

    def _seed_system_rules(self) -> None:
        now = utc_text()
        rules = (
            (
                "system-critical-alert",
                "Критические инциденты",
                "diagnostic_critical",
                "notify",
                900,
            ),
            (
                "system-low-space-alert",
                "Нехватка места",
                "storage_low",
                "notify",
                3600,
            ),
            (
                "system-backup-alert",
                "Ошибки резервных копий",
                "backup_failed",
                "notify",
                3600,
            ),
            (
                "system-tunnel-alert",
                "Сбой интернет-шлюза",
                "tunnel_offline",
                "notify",
                1800,
            ),
            (
                "system-restore-alert",
                "Ошибки восстановления",
                "restore_failed",
                "notify",
                3600,
            ),
            (
                "system-mirror-alert",
                "Зеркало требует восстановления",
                "mirror_degraded",
                "notify",
                3600,
            ),
            (
                "system-maintenance-alert",
                "Ошибка обслуживания диска",
                "maintenance_failed",
                "notify",
                3600,
            ),
            (
                "system-pending-device-alert",
                "Новое устройство ожидает подтверждения",
                "pending_device",
                "notify",
                900,
            ),
        )
        with self.database.transaction() as connection:
            for rule_id, name, trigger_type, action_type, cooldown in rules:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO automation_rules(
                        id, name, enabled, trigger_type, action_type, cooldown_seconds,
                        system_rule, last_triggered_at, created_at, updated_at
                    ) VALUES(?, ?, 1, ?, ?, ?, 1, NULL, ?, ?)
                    """,
                    (rule_id, name, trigger_type, action_type, cooldown, now, now),
                )

    def start_scheduler(self) -> None:
        with self._scheduler_guard:
            if self._scheduler_thread is not None and self._scheduler_thread.is_alive():
                return
            self._scheduler_stop.clear()
            self._scheduler_wake.clear()
            self._scheduler_thread = threading.Thread(
                target=self._scheduler_loop,
                name="cloud-storage-automation",
                daemon=True,
            )
            self._scheduler_thread.start()

    def stop_scheduler(self) -> None:
        with self._scheduler_guard:
            thread = self._scheduler_thread
            self._scheduler_thread = None
            self._scheduler_stop.set()
            self._scheduler_wake.set()
        if thread is not None:
            thread.join(timeout=5)

    def _scheduler_loop(self) -> None:
        if self._scheduler_wake.wait(2) and self._scheduler_stop.is_set():
            return
        self._scheduler_wake.clear()
        self._evaluate_safely()
        while not self._scheduler_stop.is_set():
            self._scheduler_wake.wait(self.scheduler_interval_seconds)
            self._scheduler_wake.clear()
            if self._scheduler_stop.is_set():
                return
            self._evaluate_safely()

    def _evaluate_safely(self) -> None:
        if not self.scheduler_enabled:
            return
        try:
            self.evaluate(trigger_source="scheduler")
        except Exception:
            return

    def list_rules(self) -> list[AutomationRuleRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM automation_rules
                ORDER BY system_rule DESC, name COLLATE NOCASE, created_at
                """
            ).fetchall()
        return [self._rule(row) for row in rows]

    def get_rule(self, rule_id: str) -> AutomationRuleRecord:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM automation_rules WHERE id = ?", (rule_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("automation rule not found")
        return self._rule(row)

    def create_rule(
        self,
        *,
        name: str,
        enabled: bool,
        trigger_type: str,
        action_type: str,
        cooldown_seconds: int,
    ) -> AutomationRuleRecord:
        self._validate_rule(trigger_type, action_type, cooldown_seconds)
        rule_id = str(uuid.uuid4())
        now = utc_text()
        clean_name = name.strip()[:120]
        if not clean_name:
            raise ValueError("automation rule name cannot be empty")
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO automation_rules(
                    id, name, enabled, trigger_type, action_type, cooldown_seconds,
                    system_rule, last_triggered_at, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, 0, NULL, ?, ?)
                """,
                (
                    rule_id,
                    clean_name,
                    int(enabled),
                    trigger_type,
                    action_type,
                    cooldown_seconds,
                    now,
                    now,
                ),
            )
            self.repository._audit_tx(
                connection,
                actor_type="manager",
                actor_id=None,
                action="automation.rule.created",
                target_type="automation_rule",
                target_id=rule_id,
                detail=f"Создано правило «{clean_name}»: {trigger_type} → {action_type}",
            )
        return self.get_rule(rule_id)

    def update_rule(
        self,
        rule_id: str,
        *,
        name: str,
        enabled: bool,
        trigger_type: str,
        action_type: str,
        cooldown_seconds: int,
    ) -> AutomationRuleRecord:
        current = self.get_rule(rule_id)
        self._validate_rule(trigger_type, action_type, cooldown_seconds)
        clean_name = name.strip()[:120]
        if not clean_name:
            raise ValueError("automation rule name cannot be empty")
        if current.system_rule and (
            trigger_type != current.trigger_type or action_type != current.action_type
        ):
            raise ConflictError("system automation rule type cannot be changed")
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE automation_rules
                SET name = ?, enabled = ?, trigger_type = ?, action_type = ?,
                    cooldown_seconds = ?, updated_at = ? WHERE id = ?
                """,
                (
                    clean_name,
                    int(enabled),
                    trigger_type,
                    action_type,
                    cooldown_seconds,
                    utc_text(),
                    rule_id,
                ),
            )
            self.repository._audit_tx(
                connection,
                actor_type="manager",
                actor_id=None,
                action="automation.rule.updated",
                target_type="automation_rule",
                target_id=rule_id,
                detail=f"Правило «{clean_name}» {'включено' if enabled else 'выключено'}",
            )
        return self.get_rule(rule_id)

    def delete_rule(self, rule_id: str) -> None:
        rule = self.get_rule(rule_id)
        if rule.system_rule:
            raise ConflictError("system automation rule can be disabled but not deleted")
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM automation_rules WHERE id = ?", (rule_id,))
            self.repository._audit_tx(
                connection,
                actor_type="manager",
                actor_id=None,
                action="automation.rule.deleted",
                target_type="automation_rule",
                target_id=rule_id,
                detail=f"Удалено правило «{rule.name}»",
            )

    def list_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT r.*, a.name AS rule_name, a.trigger_type, a.action_type
                FROM automation_runs r
                JOIN automation_rules a ON a.id = r.rule_id
                ORDER BY r.created_at DESC LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def overview(self) -> dict[str, Any]:
        rules = [self.rule_to_dict(item) for item in self.list_rules()]
        return {
            "scheduler": {
                "running": bool(
                    self._scheduler_thread is not None
                    and self._scheduler_thread.is_alive()
                ),
                "interval_seconds": self.scheduler_interval_seconds,
                "enabled": self.scheduler_enabled,
            },
            "supported_triggers": sorted(TRIGGER_TYPES),
            "supported_actions": sorted(ACTION_TYPES),
            "action_types_by_trigger": {
                key: sorted(value) for key, value in ACTION_TYPES_BY_TRIGGER.items()
            },
            "rules": rules,
            "runs": self.list_runs(50),
        }

    def settings(self) -> dict[str, Any]:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT enabled, interval_seconds, updated_at FROM automation_settings WHERE id = 1"
            ).fetchone()
        if row is None:
            return {"enabled": True, "interval_seconds": 60, "updated_at": ""}
        return {
            "enabled": bool(row["enabled"]),
            "interval_seconds": int(row["interval_seconds"]),
            "updated_at": str(row["updated_at"]),
        }

    def set_settings(self, *, enabled: bool, interval_seconds: int) -> dict[str, Any]:
        if not 10 <= interval_seconds <= 3600:
            raise ValueError("automation interval must be between 10 and 3600 seconds")
        now = utc_text()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO automation_settings(id, enabled, interval_seconds, updated_at)
                VALUES(1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET enabled = excluded.enabled,
                    interval_seconds = excluded.interval_seconds,
                    updated_at = excluded.updated_at
                """,
                (int(enabled), interval_seconds, now),
            )
            self.repository._audit_tx(
                connection,
                actor_type="manager",
                actor_id=None,
                action="automation.settings.updated",
                target_type="automation",
                target_id="scheduler",
                detail=(
                    f"Автоматическое выполнение {'включено' if enabled else 'выключено'}; "
                    f"проверка каждые {interval_seconds} сек"
                ),
            )
        self.scheduler_enabled = enabled
        self.scheduler_interval_seconds = interval_seconds
        self._scheduler_wake.set()
        return self.settings()

    def preview(self, rule_id: str | None = None) -> dict[str, Any]:
        rules = [self.get_rule(rule_id)] if rule_id else self.list_rules()
        result = []
        for rule in rules:
            condition = self._matching_condition(rule.trigger_type)
            remaining = self._cooldown_remaining_seconds(rule)
            result.append(
                {
                    "rule_id": rule.id,
                    "rule_name": rule.name,
                    "enabled": rule.enabled,
                    "matched": condition is not None,
                    "condition": condition,
                    "cooldown_remaining_seconds": remaining,
                    "would_run": bool(rule.enabled and condition is not None and remaining == 0),
                }
            )
        return {
            "matched_rules": sum(bool(item["matched"]) for item in result),
            "ready_rules": sum(bool(item["would_run"]) for item in result),
            "rules": result,
        }

    def evaluate(self, *, trigger_source: str = "manager") -> list[dict[str, Any]]:
        if trigger_source not in {"manager", "scheduler"}:
            raise ValueError("invalid automation trigger source")
        if not self._evaluation_lock.acquire(blocking=False):
            return []
        try:
            completed: list[dict[str, Any]] = []
            for rule in self.list_rules():
                if not rule.enabled or self._cooldown_active(rule):
                    continue
                condition = self._matching_condition(rule.trigger_type)
                if condition is None:
                    continue
                completed.append(self._execute_rule(rule, condition, trigger_source))
            return completed
        finally:
            self._evaluation_lock.release()

    def _matching_condition(self, trigger_type: str) -> dict[str, Any] | None:
        if trigger_type == "scheduled":
            return {
                "severity": "info",
                "summary": "Наступило время планового запуска",
                "detail": "Интервал правила истёк",
            }
        if trigger_type == "mirror_degraded":
            degraded = sum(
                int(item.get("degraded_files", 0))
                for item in self.storage.mirror_overview()
                if item.get("write_enabled")
            )
            if degraded:
                return {
                    "severity": "warning",
                    "summary": "Зеркало содержит неполные или устаревшие копии",
                    "detail": f"Требуют восстановления объектов: {degraded}",
                }
            return None
        if trigger_type == "tunnel_offline":
            offline = []
            for provider_id, provider in self.tunnels.providers.items():
                status = provider.status()
                if status.get("enabled") and status.get("state") in {
                    "error",
                    "not_installed",
                    "stopped",
                }:
                    offline.append(provider_id)
            if offline:
                return {
                    "severity": "warning",
                    "summary": "Интернет-шлюз недоступен",
                    "detail": f"Провайдеры: {', '.join(sorted(offline))}",
                }
            return None
        queries = {
            "diagnostic_warning": (
                "SELECT count(*) FROM diagnostic_incidents "
                "WHERE status = 'active' AND severity = 'warning'",
                "warning",
                "Активные предупреждения диагностики",
            ),
            "diagnostic_critical": (
                "SELECT count(*) FROM diagnostic_incidents "
                "WHERE status = 'active' AND severity = 'critical'",
                "critical",
                "Активные критические инциденты",
            ),
            "storage_low": (
                "SELECT count(*) FROM diagnostic_incidents "
                "WHERE status = 'active' AND check_key = 'storage.capacity'",
                "warning",
                "Хранилище вышло за безопасный порог",
            ),
            "backup_failed": (
                "SELECT (SELECT count(*) FROM backup_jobs WHERE status = 'failed') + "
                "(SELECT count(*) FROM backup_verifications WHERE status = 'failed')",
                "warning",
                "Ошибка резервного копирования или проверки",
            ),
            "restore_failed": (
                "SELECT count(*) FROM restore_jobs WHERE status = 'failed'",
                "warning",
                "Ошибка проверяемого восстановления",
            ),
            "maintenance_failed": (
                "SELECT count(*) FROM maintenance_jobs WHERE status = 'failed'",
                "warning",
                "Ошибка обслуживания или переноса диска",
            ),
            "pending_device": (
                "SELECT count(*) FROM devices WHERE status = 'pending'",
                "info",
                "Устройство ожидает подтверждения администратора",
            ),
        }
        query, severity, summary = queries[trigger_type]
        with self.database.connection() as connection:
            count = int(connection.execute(query).fetchone()[0])
        if count == 0:
            return None
        return {
            "severity": severity,
            "summary": summary,
            "detail": f"Обнаружено событий: {count}",
        }

    def _execute_rule(
        self,
        rule: AutomationRuleRecord,
        condition: dict[str, Any],
        trigger_source: str,
    ) -> dict[str, Any]:
        run_id = str(uuid.uuid4())
        created_at = utc_text()
        status = "completed"
        result = ""
        error = ""
        try:
            if rule.action_type == "notify":
                notification = self.notifications.emit(
                    severity=str(condition["severity"]),
                    title=rule.name,
                    message=f"{condition['summary']}. {condition['detail']}",
                    source="automation",
                    source_key=f"rule:{rule.id}",
                )
                result = f"notification:{notification['id']}"
            elif rule.action_type == "quick_scan":
                scan = self.diagnostics.create_scan("quick", source="monitor")
                threading.Thread(
                    target=self.diagnostics.run_scan_safely,
                    args=(scan.id,),
                    name="cloud-storage-automation-scan",
                    daemon=True,
                ).start()
                result = f"diagnostic_scan:{scan.id}"
            elif rule.action_type == "full_scan":
                scan = self.diagnostics.create_scan("full", source="monitor")
                threading.Thread(
                    target=self.diagnostics.run_scan_safely,
                    args=(scan.id,),
                    name="cloud-storage-automation-full-scan",
                    daemon=True,
                ).start()
                result = f"diagnostic_scan:{scan.id}"
            elif rule.action_type == "read_only":
                state = self.recovery.server_mode()
                if state["mode"] == "read_only":
                    status = "skipped"
                    result = "server_already_read_only"
                else:
                    changed = self.recovery.set_server_mode(
                        "read_only",
                        f"Автоматизация «{rule.name}»: {condition['summary']}",
                    )
                    result = f"server_mode:{changed['mode']}"
            elif rule.action_type == "run_backup":
                policies = [
                    item for item in self.backup_automation.list_policies() if item.enabled
                ]
                if not policies:
                    status = "skipped"
                    result = "no_enabled_backup_policy"
                else:
                    policy = min(
                        policies,
                        key=lambda item: (item.last_run_at or "", item.target_root_id),
                    )
                    job = self.backup_automation.queue_policy_run(policy.target_root_id)
                    threading.Thread(
                        target=self.backup_automation.run_backup_pipeline,
                        args=(job.id,),
                        name="cloud-storage-automation-backup",
                        daemon=True,
                    ).start()
                    result = f"backup_job:{job.id}"
            elif rule.action_type == "reconcile_mirrors":
                mirrors = [
                    item
                    for item in self.storage.mirror_overview()
                    if item.get("write_enabled")
                ]
                if not mirrors:
                    status = "skipped"
                    result = "no_writable_mirror"
                else:
                    target = max(
                        mirrors,
                        key=lambda item: int(item.get("degraded_files", 0)),
                    )
                    job = self.storage.create_mirror_job(str(target["root_id"]))
                    threading.Thread(
                        target=self.storage.run_mirror_job,
                        args=(job.id,),
                        name="cloud-storage-automation-mirror",
                        daemon=True,
                    ).start()
                    result = f"mirror_job:{job.id}"
            elif rule.action_type == "restart_tunnel":
                candidates = [
                    provider_id
                    for provider_id, provider in self.tunnels.providers.items()
                    if provider.status().get("enabled")
                    and provider.status().get("state") != "online"
                ]
                if not candidates:
                    status = "skipped"
                    result = "no_offline_tunnel"
                else:
                    provider_id = sorted(candidates)[0]
                    tunnel_state = self.tunnels.restart(provider_id)
                    result = f"tunnel:{provider_id}:{tunnel_state.get('state', 'starting')}"
        except (ConflictError, KeyError, OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
            status = "failed"
            error = str(exc)[:1000]
        completed_at = utc_text()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO automation_runs(
                    id, rule_id, trigger_source, status, condition_summary,
                    action_result, error, created_at, completed_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    rule.id,
                    trigger_source,
                    status,
                    f"{condition['summary']}: {condition['detail']}",
                    result,
                    error,
                    created_at,
                    completed_at,
                ),
            )
            connection.execute(
                "UPDATE automation_rules SET last_triggered_at = ?, updated_at = ? WHERE id = ?",
                (completed_at, completed_at, rule.id),
            )
            self.repository._audit_tx(
                connection,
                actor_type="automation",
                actor_id=rule.id,
                action=f"automation.rule.{status}",
                target_type="automation_run",
                target_id=run_id,
                detail=(
                    f"Правило «{rule.name}»: {rule.trigger_type} → {rule.action_type}; "
                    f"{error or result or status}"
                ),
            )
        return self.list_runs(1)[0]

    @staticmethod
    def _validate_rule(trigger_type: str, action_type: str, cooldown_seconds: int) -> None:
        if trigger_type not in TRIGGER_TYPES:
            raise ValueError("unsupported automation trigger")
        if action_type not in ACTION_TYPES:
            raise ValueError("unsupported automation action")
        if not 60 <= cooldown_seconds <= 604800:
            raise ValueError("automation cooldown must be between 60 and 604800 seconds")
        if action_type not in ACTION_TYPES_BY_TRIGGER[trigger_type]:
            raise ValueError("automation action is not safe for the selected trigger")

    @staticmethod
    def _cooldown_active(rule: AutomationRuleRecord) -> bool:
        return AutomationService._cooldown_remaining_seconds(rule) > 0

    @staticmethod
    def _cooldown_remaining_seconds(rule: AutomationRuleRecord) -> int:
        if not rule.last_triggered_at:
            return 0
        try:
            last = datetime.fromisoformat(rule.last_triggered_at)
        except ValueError:
            return 0
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        remaining = last + timedelta(seconds=rule.cooldown_seconds) - datetime.now(UTC)
        return max(0, int(remaining.total_seconds()))

    @staticmethod
    def _rule(row: sqlite3.Row) -> AutomationRuleRecord:
        return AutomationRuleRecord(
            id=str(row["id"]),
            name=str(row["name"]),
            enabled=bool(row["enabled"]),
            trigger_type=str(row["trigger_type"]),
            action_type=str(row["action_type"]),
            cooldown_seconds=int(row["cooldown_seconds"]),
            system_rule=bool(row["system_rule"]),
            last_triggered_at=row["last_triggered_at"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def rule_to_dict(rule: AutomationRuleRecord) -> dict[str, Any]:
        return asdict(rule)
