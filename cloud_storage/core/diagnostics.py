from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from cloud_storage.core.config import CoreConfig
from cloud_storage.core.database import Database
from cloud_storage.core.repository import ConflictError, CoreRepository, NotFoundError, utc_text


@dataclass(frozen=True, slots=True)
class DiagnosticFinding:
    check_key: str
    identity: str
    severity: str
    component: str
    summary: str
    detail: str
    remediation: str

    @property
    def fingerprint(self) -> str:
        value = f"{self.check_key}\0{self.identity}".encode()
        return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True, slots=True)
class DiagnosticScanRecord:
    id: str
    kind: str
    source: str
    status: str
    checks: int
    checked_objects: int
    checked_bytes: int
    warning_count: int
    critical_count: int
    error: str
    created_at: str
    completed_at: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class DiagnosticIncidentRecord:
    id: str
    fingerprint: str
    check_key: str
    severity: str
    status: str
    component: str
    summary: str
    detail: str
    remediation: str
    occurrences: int
    first_seen_at: str
    last_seen_at: str
    resolved_at: str | None
    last_scan_id: str | None


class DiagnosticsService:
    def __init__(
        self,
        config: CoreConfig,
        database: Database,
        repository: CoreRepository,
        *,
        monitor_interval_seconds: int = 300,
    ) -> None:
        self.config = config
        self.database = database
        self.repository = repository
        self.monitor_interval_seconds = max(30, monitor_interval_seconds)
        self._scan_lock = threading.Lock()
        self._monitor_guard = threading.Lock()
        self._monitor_stop = threading.Event()
        self._monitor_thread: threading.Thread | None = None

    def recover_interrupted_scans(self) -> int:
        now = utc_text()
        with self.database.transaction() as connection:
            updated = connection.execute(
                """
                UPDATE diagnostic_scans
                SET status = 'failed', error = ?, completed_at = ?, updated_at = ?
                WHERE status IN ('queued', 'running')
                """,
                (
                    "Проверка прервана перезапуском Core; запустите её повторно",
                    now,
                    now,
                ),
            )
        return updated.rowcount

    def start_monitor(self) -> None:
        with self._monitor_guard:
            if self._monitor_thread is not None and self._monitor_thread.is_alive():
                return
            self._monitor_stop.clear()
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop,
                name="cloud-storage-diagnostics",
                daemon=True,
            )
            self._monitor_thread.start()

    def stop_monitor(self) -> None:
        with self._monitor_guard:
            thread = self._monitor_thread
            self._monitor_thread = None
            self._monitor_stop.set()
        if thread is not None:
            thread.join(timeout=5)

    def create_scan(self, kind: str, *, source: str = "manager") -> DiagnosticScanRecord:
        if kind not in {"quick", "full"}:
            raise ValueError("diagnostic scan kind must be quick or full")
        if source not in {"monitor", "manager", "startup"}:
            raise ValueError("invalid diagnostic scan source")
        scan_id = str(uuid.uuid4())
        now = utc_text()
        with self.database.transaction() as connection:
            active = connection.execute(
                "SELECT id FROM diagnostic_scans WHERE status IN ('queued', 'running') LIMIT 1"
            ).fetchone()
            if active is not None:
                raise ConflictError("another diagnostic scan is already queued or running")
            connection.execute(
                """
                INSERT INTO diagnostic_scans(
                    id, kind, source, status, checks, checked_objects, checked_bytes,
                    warning_count, critical_count, error, created_at, completed_at, updated_at
                ) VALUES(?, ?, ?, 'queued', 0, 0, 0, 0, 0, '', ?, NULL, ?)
                """,
                (scan_id, kind, source, now, now),
            )
        return self.get_scan(scan_id)

    def run_scan(self, scan_id: str) -> DiagnosticScanRecord:
        if not self._scan_lock.acquire(blocking=False):
            raise ConflictError("another diagnostic scan is already running")
        try:
            scan = self.get_scan(scan_id)
            if scan.status != "queued":
                raise ConflictError("only queued diagnostic scans can be started")
            kind = scan.kind
            source = scan.source
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE diagnostic_scans SET status = 'running', updated_at = ?
                    WHERE id = ? AND status = 'queued'
                    """,
                    (utc_text(), scan_id),
                )
            findings, metrics, covered_keys = self._collect_findings(kind)
            self._update_incidents(scan_id, findings, covered_keys)
            completed_at = utc_text()
            warning_count = sum(item.severity == "warning" for item in findings)
            critical_count = sum(item.severity == "critical" for item in findings)
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE diagnostic_scans
                    SET status = 'completed', checks = ?, checked_objects = ?, checked_bytes = ?,
                        warning_count = ?, critical_count = ?, completed_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        metrics["checks"],
                        metrics["checked_objects"],
                        metrics["checked_bytes"],
                        warning_count,
                        critical_count,
                        completed_at,
                        completed_at,
                        scan_id,
                    ),
                )
                if source == "manager":
                    self.repository._audit_tx(
                        connection,
                        actor_type="manager",
                        actor_id=None,
                        action="diagnostics.scan.completed",
                        target_type="diagnostic_scan",
                        target_id=scan_id,
                        detail=(
                            f"Проверка {kind}: предупреждений {warning_count}, "
                            f"критических проблем {critical_count}, объектов "
                            f"{metrics['checked_objects']}"
                        ),
                    )
            return self.get_scan(scan_id)
        except Exception as exc:
            completed_at = utc_text()
            try:
                with self.database.transaction() as connection:
                    connection.execute(
                        """
                        UPDATE diagnostic_scans
                        SET status = 'failed', error = ?, completed_at = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (str(exc)[:1000], completed_at, completed_at, scan_id),
                    )
            except sqlite3.Error:
                pass
            raise
        finally:
            self._scan_lock.release()

    def run_scan_now(self, kind: str, *, source: str = "manager") -> DiagnosticScanRecord:
        scan = self.create_scan(kind, source=source)
        return self.run_scan(scan.id)

    def run_scan_safely(self, scan_id: str) -> None:
        try:
            self.run_scan(scan_id)
        except Exception:
            return

    def overview(self) -> dict[str, Any]:
        with self.database.connection() as connection:
            latest = connection.execute(
                "SELECT * FROM diagnostic_scans ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            counts = connection.execute(
                """
                SELECT
                    sum(CASE WHEN severity = 'warning' THEN 1 ELSE 0 END) AS warnings,
                    sum(CASE WHEN severity = 'critical' THEN 1 ELSE 0 END) AS critical
                FROM diagnostic_incidents WHERE status = 'active'
                """
            ).fetchone()
        incidents = self.list_incidents(include_resolved=False, limit=100)
        return {
            "status": "critical"
            if int(counts["critical"] or 0)
            else "warning"
            if int(counts["warnings"] or 0)
            else "healthy",
            "active_warning_count": int(counts["warnings"] or 0),
            "active_critical_count": int(counts["critical"] or 0),
            "latest_scan": self.scan_to_dict(self._scan(latest)) if latest else None,
            "incidents": [self.incident_to_dict(item) for item in incidents],
            "monitor": {
                "running": bool(
                    self._monitor_thread is not None and self._monitor_thread.is_alive()
                ),
                "interval_seconds": self.monitor_interval_seconds,
            },
        }

    def list_scans(self, limit: int = 50) -> list[DiagnosticScanRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM diagnostic_scans ORDER BY created_at DESC LIMIT ?",
                (max(1, min(200, limit)),),
            ).fetchall()
        return [self._scan(row) for row in rows]

    def get_scan(self, scan_id: str) -> DiagnosticScanRecord:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM diagnostic_scans WHERE id = ?", (scan_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("diagnostic scan not found")
        return self._scan(row)

    def list_incidents(
        self,
        *,
        include_resolved: bool = False,
        limit: int = 200,
    ) -> list[DiagnosticIncidentRecord]:
        status_clause = "" if include_resolved else "WHERE status = 'active'"
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM diagnostic_incidents {status_clause}
                ORDER BY CASE severity WHEN 'critical' THEN 0 ELSE 1 END,
                         last_seen_at DESC LIMIT ?
                """,
                (max(1, min(500, limit)),),
            ).fetchall()
        return [self._incident(row) for row in rows]

    def _monitor_loop(self) -> None:
        self._run_monitor_scan("startup")
        while not self._monitor_stop.wait(self.monitor_interval_seconds):
            self._run_monitor_scan("monitor")

    def _run_monitor_scan(self, source: str) -> None:
        try:
            self.run_scan_now("quick", source=source)
        except Exception:
            return

    def _collect_findings(
        self,
        kind: str,
    ) -> tuple[list[DiagnosticFinding], dict[str, int], set[str]]:
        findings: list[DiagnosticFinding] = []
        metrics = {"checks": 0, "checked_objects": 0, "checked_bytes": 0}
        covered_keys = {
            "database.integrity",
            "database.foreign_keys",
            "storage.available",
            "storage.marker",
            "storage.capacity",
            "jobs.failed",
        }
        database_findings, checks = self._check_database()
        findings.extend(database_findings)
        metrics["checks"] += checks

        root_findings, roots, available_roots, checks = self._check_storage_roots()
        findings.extend(root_findings)
        metrics["checks"] += checks

        job_findings, checks = self._check_failed_jobs()
        findings.extend(job_findings)
        metrics["checks"] += checks

        object_limit = None if kind == "full" else 200
        object_findings, checked_objects, checked_bytes = self._check_objects(
            roots,
            available_roots,
            verify_hash=kind == "full",
            limit=object_limit,
        )
        findings.extend(object_findings)
        metrics["checked_objects"] += checked_objects
        metrics["checked_bytes"] += checked_bytes
        metrics["checks"] += checked_objects
        covered_keys.update({"object.available", "object.size"})
        if kind == "full":
            covered_keys.add("object.checksum")

        mirror_findings, checked_objects, checked_bytes = self._check_mirror_replicas(
            roots,
            available_roots,
            verify_hash=kind == "full",
            limit=None if kind == "full" else 100,
        )
        findings.extend(mirror_findings)
        metrics["checked_objects"] += checked_objects
        metrics["checked_bytes"] += checked_bytes
        metrics["checks"] += checked_objects
        covered_keys.update({"mirror.available", "mirror.size"})
        if kind == "full":
            covered_keys.add("mirror.checksum")

        backup_findings, checks = self._check_backup_snapshots(roots, available_roots)
        findings.extend(backup_findings)
        metrics["checks"] += checks
        covered_keys.add("backup.snapshot")
        return findings, metrics, covered_keys

    def _check_database(self) -> tuple[list[DiagnosticFinding], int]:
        findings: list[DiagnosticFinding] = []
        with self.database.connection() as connection:
            integrity = connection.execute("PRAGMA quick_check(1)").fetchone()
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchmany(20)
        if integrity is None or str(integrity[0]).casefold() != "ok":
            findings.append(
                DiagnosticFinding(
                    "database.integrity",
                    "core.db",
                    "critical",
                    "База данных",
                    "SQLite сообщает о нарушении целостности",
                    str(integrity[0] if integrity else "quick_check не вернул результат"),
                    "Остановите новые записи и восстановите базу из проверенного снимка.",
                )
            )
        if foreign_keys:
            findings.append(
                DiagnosticFinding(
                    "database.foreign_keys",
                    "core.db",
                    "critical",
                    "База данных",
                    "Обнаружены нарушенные связи метаданных",
                    json.dumps([list(row) for row in foreign_keys], ensure_ascii=False),
                    "Не удаляйте файлы вручную; сохраните журнал и используйте проверенное восстановление.",
                )
            )
        return findings, 2

    def _check_storage_roots(
        self,
    ) -> tuple[list[DiagnosticFinding], dict[str, sqlite3.Row], set[str], int]:
        findings: list[DiagnosticFinding] = []
        roots: dict[str, sqlite3.Row] = {}
        available: set[str] = set()
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT r.*,
                    (SELECT count(*) FROM files f WHERE f.storage_root_id = r.id) +
                    (SELECT count(*) FROM file_versions v WHERE v.storage_root_id = r.id)
                    AS referenced_objects
                FROM storage_roots r
                """
            ).fetchall()
        for row in rows:
            roots[str(row["id"])] = row
            root_id = str(row["id"])
            path = Path(row["path"]).resolve()
            should_exist = bool(row["enabled"]) or int(row["referenced_objects"]) > 0
            if not path.is_dir():
                if should_exist:
                    findings.append(
                        DiagnosticFinding(
                            "storage.available",
                            root_id,
                            "critical",
                            "Хранилище",
                            "Диск или управляемый каталог недоступен",
                            f"{root_id}: {path}",
                            "Проверьте питание, кабель и букву диска. Не создавайте пустой каталог вместо диска.",
                        )
                    )
                continue
            available.add(root_id)
            if root_id.startswith("managed-") and not (path / ".cloud-storage-root.json").is_file():
                findings.append(
                    DiagnosticFinding(
                        "storage.marker",
                        root_id,
                        "critical",
                        "Хранилище",
                        "Маркер управляемого диска отсутствует",
                        str(path),
                        "Не подключайте каталог заново до проверки, что это исходный физический диск.",
                    )
                )
            try:
                usage = shutil.disk_usage(path)
            except OSError as exc:
                findings.append(
                    DiagnosticFinding(
                        "storage.available",
                        root_id,
                        "critical",
                        "Хранилище",
                        "Не удалось прочитать свободное место диска",
                        str(exc),
                        "Проверьте состояние файловой системы и подключение диска.",
                    )
                )
                available.discard(root_id)
                continue
            fill_percent = usage.used / usage.total * 100 if usage.total else 100.0
            if (
                usage.free < int(row["min_free_bytes"])
                or fill_percent > int(row["max_fill_percent"])
            ):
                findings.append(
                    DiagnosticFinding(
                        "storage.capacity",
                        root_id,
                        "warning",
                        "Хранилище",
                        "Диск вышел за безопасный порог заполнения",
                        f"Заполнено {fill_percent:.1f}%, свободно {usage.free} байт",
                        "Освободите место, подключите дополнительный диск или скорректируйте политику.",
                    )
                )
        return findings, roots, available, len(rows)

    def _check_failed_jobs(self) -> tuple[list[DiagnosticFinding], int]:
        findings: list[DiagnosticFinding] = []
        tables = (
            ("maintenance_jobs", "Обслуживание"),
            ("backup_jobs", "Резервные копии"),
            ("mirror_jobs", "Зеркало"),
        )
        with self.database.connection() as connection:
            for table, component in tables:
                rows = connection.execute(
                    f"SELECT id, error FROM {table} WHERE status = 'failed'"
                ).fetchall()
                for row in rows:
                    findings.append(
                        DiagnosticFinding(
                            "jobs.failed",
                            f"{table}:{row['id']}",
                            "warning",
                            component,
                            "Фоновое задание завершилось ошибкой",
                            str(row["error"] or row["id"]),
                            "Откройте соответствующий раздел Manager и повторите задание после устранения причины.",
                        )
                    )
        return findings, len(tables)

    def _check_objects(
        self,
        roots: dict[str, sqlite3.Row],
        available_roots: set[str],
        *,
        verify_hash: bool,
        limit: int | None,
    ) -> tuple[list[DiagnosticFinding], int, int]:
        query = """
            SELECT 'file' AS entity, id, storage_root_id, object_path, size_bytes, sha256
            FROM files
            UNION ALL
            SELECT 'version' AS entity, id, storage_root_id, object_path, size_bytes, sha256
            FROM file_versions
            ORDER BY entity, id
        """
        parameters: tuple[int, ...] = ()
        if limit is not None:
            query += " LIMIT ?"
            parameters = (limit,)
        with self.database.connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        findings: list[DiagnosticFinding] = []
        checked = 0
        checked_bytes = 0
        for row in rows:
            root_id = str(row["storage_root_id"])
            if root_id not in available_roots or root_id not in roots:
                continue
            checked += 1
            expected_size = int(row["size_bytes"])
            checked_bytes += expected_size
            identity = f"{row['entity']}:{row['id']}"
            path, error = self._safe_object_path(Path(roots[root_id]["path"]), row["object_path"])
            if path is None:
                findings.append(
                    DiagnosticFinding(
                        "object.available",
                        identity,
                        "critical",
                        "Объекты",
                        "Управляемый объект отсутствует или небезопасен",
                        error,
                        "Не создавайте файл вручную. Проверьте зеркало или восстановите объект из снимка.",
                    )
                )
                continue
            actual_size = path.stat().st_size
            if actual_size != expected_size:
                findings.append(
                    DiagnosticFinding(
                        "object.size",
                        identity,
                        "critical",
                        "Объекты",
                        "Размер управляемого объекта изменился",
                        f"Ожидалось {expected_size}, найдено {actual_size}: {path}",
                        "Остановите запись на диске и запустите восстановление из зеркала или снимка.",
                    )
                )
                continue
            if verify_hash and self._sha256(path) != row["sha256"]:
                findings.append(
                    DiagnosticFinding(
                        "object.checksum",
                        identity,
                        "critical",
                        "Объекты",
                        "SHA-256 управляемого объекта не совпадает",
                        str(path),
                        "Остановите запись на диске и восстановите объект из проверенной копии.",
                    )
                )
        return findings, checked, checked_bytes

    def _check_mirror_replicas(
        self,
        roots: dict[str, sqlite3.Row],
        available_roots: set[str],
        *,
        verify_hash: bool,
        limit: int | None,
    ) -> tuple[list[DiagnosticFinding], int, int]:
        query = """
            SELECT file_id, storage_root_id, object_path, size_bytes, sha256
            FROM mirror_replicas WHERE status = 'current'
            ORDER BY updated_at DESC
        """
        parameters: tuple[int, ...] = ()
        if limit is not None:
            query += " LIMIT ?"
            parameters = (limit,)
        with self.database.connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        findings: list[DiagnosticFinding] = []
        checked = 0
        checked_bytes = 0
        for row in rows:
            root_id = str(row["storage_root_id"])
            if root_id not in available_roots or root_id not in roots:
                continue
            checked += 1
            expected_size = int(row["size_bytes"])
            checked_bytes += expected_size
            identity = f"{row['file_id']}:{root_id}"
            path, error = self._safe_object_path(Path(roots[root_id]["path"]), row["object_path"])
            if path is None:
                findings.append(
                    DiagnosticFinding(
                        "mirror.available",
                        identity,
                        "warning",
                        "Зеркало",
                        "Актуальная зеркальная реплика отсутствует",
                        error,
                        "Запустите «Проверить и восстановить зеркало» в разделе автоматизации.",
                    )
                )
                continue
            actual_size = path.stat().st_size
            if actual_size != expected_size:
                findings.append(
                    DiagnosticFinding(
                        "mirror.size",
                        identity,
                        "warning",
                        "Зеркало",
                        "Размер зеркальной реплики не совпадает",
                        f"Ожидалось {expected_size}, найдено {actual_size}: {path}",
                        "Запустите проверку зеркала: повреждённая реплика будет создана заново.",
                    )
                )
                continue
            if verify_hash and self._sha256(path) != row["sha256"]:
                findings.append(
                    DiagnosticFinding(
                        "mirror.checksum",
                        identity,
                        "warning",
                        "Зеркало",
                        "SHA-256 зеркальной реплики не совпадает",
                        str(path),
                        "Запустите проверку зеркала для автоматического восстановления реплики.",
                    )
                )
        return findings, checked, checked_bytes

    def _check_backup_snapshots(
        self,
        roots: dict[str, sqlite3.Row],
        available_roots: set[str],
    ) -> tuple[list[DiagnosticFinding], int]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT id, target_root_id, snapshot_path FROM backup_jobs
                WHERE status = 'completed' AND pruned_at IS NULL
                ORDER BY completed_at DESC LIMIT 20
                """
            ).fetchall()
        findings: list[DiagnosticFinding] = []
        for row in rows:
            root_id = str(row["target_root_id"])
            if root_id not in available_roots or root_id not in roots:
                continue
            root = Path(roots[root_id]["path"])
            snapshot, error = self._safe_directory_path(root, row["snapshot_path"])
            if (
                snapshot is None
                or not (snapshot / "metadata.sqlite3").is_file()
                or not (snapshot / "manifest.json").is_file()
            ):
                findings.append(
                    DiagnosticFinding(
                        "backup.snapshot",
                        str(row["id"]),
                        "warning",
                        "Резервные копии",
                        "Готовый резервный снимок неполон или недоступен",
                        error or str(snapshot),
                        "Не удаляйте запись задания. Проверьте диск и создайте новый проверенный снимок.",
                    )
                )
        return findings, len(rows)

    def _update_incidents(
        self,
        scan_id: str,
        findings: list[DiagnosticFinding],
        covered_keys: set[str],
    ) -> None:
        now = utc_text()
        active_fingerprints = {item.fingerprint for item in findings}
        with self.database.transaction() as connection:
            for item in findings:
                connection.execute(
                    """
                    INSERT INTO diagnostic_incidents(
                        id, fingerprint, check_key, severity, status, component,
                        summary, detail, remediation, occurrences, first_seen_at,
                        last_seen_at, resolved_at, last_scan_id
                    ) VALUES(?, ?, ?, ?, 'active', ?, ?, ?, ?, 1, ?, ?, NULL, ?)
                    ON CONFLICT(fingerprint) DO UPDATE SET
                        severity = excluded.severity, status = 'active',
                        component = excluded.component, summary = excluded.summary,
                        detail = excluded.detail, remediation = excluded.remediation,
                        occurrences = diagnostic_incidents.occurrences + 1,
                        last_seen_at = excluded.last_seen_at, resolved_at = NULL,
                        last_scan_id = excluded.last_scan_id
                    """,
                    (
                        str(uuid.uuid4()),
                        item.fingerprint,
                        item.check_key,
                        item.severity,
                        item.component,
                        item.summary,
                        item.detail,
                        item.remediation,
                        now,
                        now,
                        scan_id,
                    ),
                )
            placeholders = ",".join("?" for _ in covered_keys)
            rows = connection.execute(
                f"""
                SELECT id, fingerprint FROM diagnostic_incidents
                WHERE status = 'active' AND check_key IN ({placeholders})
                """,
                tuple(sorted(covered_keys)),
            ).fetchall()
            resolved_ids = [
                str(row["id"])
                for row in rows
                if str(row["fingerprint"]) not in active_fingerprints
            ]
            if resolved_ids:
                resolved_placeholders = ",".join("?" for _ in resolved_ids)
                connection.execute(
                    f"""
                    UPDATE diagnostic_incidents
                    SET status = 'resolved', resolved_at = ?, last_scan_id = ?
                    WHERE id IN ({resolved_placeholders})
                    """,
                    (now, scan_id, *resolved_ids),
                )

    @staticmethod
    def _safe_object_path(root: Path, relative: str) -> tuple[Path | None, str]:
        try:
            resolved_root = root.resolve(strict=True)
            path = (resolved_root / str(relative)).resolve(strict=True)
            if resolved_root not in path.parents or not path.is_file() or path.is_symlink():
                return None, f"Путь не прошёл проверку границ: {relative}"
            return path, ""
        except OSError as exc:
            return None, f"{relative}: {exc}"

    @staticmethod
    def _safe_directory_path(root: Path, relative: str) -> tuple[Path | None, str]:
        try:
            resolved_root = root.resolve(strict=True)
            path = (resolved_root / str(relative)).resolve(strict=True)
            if resolved_root not in path.parents or not path.is_dir() or path.is_symlink():
                return None, f"Каталог не прошёл проверку границ: {relative}"
            return path, ""
        except OSError as exc:
            return None, f"{relative}: {exc}"

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(4 * 1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _scan(row: sqlite3.Row) -> DiagnosticScanRecord:
        return DiagnosticScanRecord(
            **{field: row[field] for field in DiagnosticScanRecord.__dataclass_fields__}
        )

    @staticmethod
    def _incident(row: sqlite3.Row) -> DiagnosticIncidentRecord:
        return DiagnosticIncidentRecord(
            **{field: row[field] for field in DiagnosticIncidentRecord.__dataclass_fields__}
        )

    @staticmethod
    def scan_to_dict(record: DiagnosticScanRecord) -> dict[str, Any]:
        return asdict(record)

    @staticmethod
    def incident_to_dict(record: DiagnosticIncidentRecord) -> dict[str, Any]:
        return asdict(record)
