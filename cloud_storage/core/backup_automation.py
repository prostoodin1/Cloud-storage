from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from cloud_storage.core.database import Database
from cloud_storage.core.repository import (
    ConflictError,
    CoreRepository,
    NotFoundError,
    PermissionDeniedError,
    utc_now,
    utc_text,
)
from cloud_storage.core.storage import BackupJobRecord, StorageRootRecord, StorageService


@dataclass(frozen=True, slots=True)
class BackupPolicyRecord:
    target_root_id: str
    enabled: bool
    interval_hours: int
    keep_last: int
    verification_root_id: str | None
    next_run_at: str | None
    last_run_at: str | None
    last_job_id: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class BackupVerificationRecord:
    id: str
    backup_job_id: str
    target_root_id: str
    status: str
    total_objects: int
    total_bytes: int
    checked_objects: int
    checked_bytes: int
    error: str
    created_at: str
    started_at: str | None
    completed_at: str | None
    updated_at: str


class BackupAutomationService:
    def __init__(
        self,
        database: Database,
        repository: CoreRepository,
        storage: StorageService,
        *,
        scheduler_interval_seconds: int = 60,
    ) -> None:
        self.database = database
        self.repository = repository
        self.storage = storage
        self.scheduler_interval_seconds = max(10, scheduler_interval_seconds)
        self._scheduler_guard = threading.Lock()
        self._scheduler_stop = threading.Event()
        self._scheduler_thread: threading.Thread | None = None
        self._pipeline_lock = threading.Lock()
        self._verification_locks: dict[str, threading.Lock] = {}
        self._verification_locks_guard = threading.Lock()

    def start_scheduler(self) -> None:
        with self._scheduler_guard:
            if self._scheduler_thread is not None and self._scheduler_thread.is_alive():
                return
            self._scheduler_stop.clear()
            self._scheduler_thread = threading.Thread(
                target=self._scheduler_loop,
                name="cloud-storage-backup-scheduler",
                daemon=True,
            )
            self._scheduler_thread.start()

    def stop_scheduler(self) -> None:
        with self._scheduler_guard:
            thread = self._scheduler_thread
            self._scheduler_thread = None
            self._scheduler_stop.set()
        if thread is not None:
            thread.join(timeout=5)

    def recover_interrupted_verifications(self) -> int:
        now = utc_text()
        with self.database.transaction() as connection:
            updated = connection.execute(
                """
                UPDATE backup_verifications
                SET status = 'failed', error = ?, completed_at = ?, updated_at = ?
                WHERE status IN ('queued', 'running')
                """,
                (
                    "Проверка восстановления прервана перезапуском Core; повторите её",
                    now,
                    now,
                ),
            )
        return updated.rowcount

    def list_policies(self) -> list[BackupPolicyRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM backup_policies ORDER BY target_root_id"
            ).fetchall()
        return [self._policy(row) for row in rows]

    def get_policy(self, target_root_id: str) -> BackupPolicyRecord:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM backup_policies WHERE target_root_id = ?",
                (target_root_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError("backup policy not found")
        return self._policy(row)

    def set_policy(
        self,
        target_root_id: str,
        *,
        enabled: bool,
        interval_hours: int,
        keep_last: int,
        verification_root_id: str | None,
    ) -> BackupPolicyRecord:
        if not 1 <= interval_hours <= 8760:
            raise ValueError("backup interval must be between 1 and 8760 hours")
        if not 1 <= keep_last <= 365:
            raise ValueError("backup retention must keep between 1 and 365 snapshots")
        backup_root = self._root(target_root_id, purpose="backup", writable=True)
        verification_root = None
        if verification_root_id:
            verification_root = self._root(
                verification_root_id, purpose="primary", writable=True
            )
        if enabled and verification_root is None:
            raise ConflictError(
                "an enabled backup policy requires an active primary verification target"
            )
        now = utc_now()
        next_run = utc_text(now + timedelta(hours=interval_hours)) if enabled else None
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO backup_policies(
                    target_root_id, enabled, interval_hours, keep_last,
                    verification_root_id, next_run_at, last_run_at, last_job_id, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, NULL, NULL, ?)
                ON CONFLICT(target_root_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    interval_hours = excluded.interval_hours,
                    keep_last = excluded.keep_last,
                    verification_root_id = excluded.verification_root_id,
                    next_run_at = excluded.next_run_at,
                    updated_at = excluded.updated_at
                """,
                (
                    backup_root.id,
                    int(enabled),
                    interval_hours,
                    keep_last,
                    verification_root.id if verification_root else None,
                    next_run,
                    utc_text(now),
                ),
            )
            self.repository._audit_tx(
                connection,
                actor_type="manager",
                actor_id=None,
                action="backup.policy.updated",
                target_type="storage_root",
                target_id=backup_root.id,
                detail=(
                    f"Автоматические снимки {'включены' if enabled else 'выключены'}; "
                    f"интервал {interval_hours} ч; хранить {keep_last}; "
                    f"проверка на {verification_root.id if verification_root else 'не выбрана'}"
                ),
            )
        return self.get_policy(backup_root.id)

    def queue_policy_run(self, target_root_id: str) -> BackupJobRecord:
        policy = self.get_policy(target_root_id)
        self._require_server_writable()
        with self.database.connection() as connection:
            active = connection.execute(
                "SELECT id FROM backup_jobs WHERE status IN ('queued', 'running') LIMIT 1"
            ).fetchone()
        if active is not None:
            raise ConflictError("another backup snapshot is already queued or running")
        job = self.storage.create_backup_job(policy.target_root_id)
        now = utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE backup_policies
                SET last_run_at = ?, last_job_id = ?, next_run_at = ?, updated_at = ?
                WHERE target_root_id = ?
                """,
                (
                    utc_text(now),
                    job.id,
                    utc_text(now + timedelta(hours=policy.interval_hours))
                    if policy.enabled
                    else None,
                    utc_text(now),
                    policy.target_root_id,
                ),
            )
        return job

    def run_backup_pipeline(self, job_id: str) -> None:
        self._pipeline_lock.acquire()
        try:
            self.storage.run_backup_job(job_id)
            job = self.storage.get_backup_job(job_id)
            if job.status != "completed" or job.pruned_at is not None:
                return
            try:
                policy = self.get_policy(job.target_root_id)
            except NotFoundError:
                return
            if policy.verification_root_id is None:
                return
            verification = self.create_verification(
                job.id, policy.verification_root_id
            )
            self.run_verification(verification.id)
            if self.get_verification(verification.id).status == "completed":
                self.apply_retention(
                    job.target_root_id,
                    policy.keep_last,
                    newest_job_id=job.id,
                )
        finally:
            self._pipeline_lock.release()

    def run_due_policies(self, now: datetime | None = None) -> list[str]:
        if not self._server_is_writable():
            return []
        current = now or utc_now()
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT target_root_id FROM backup_policies
                WHERE enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ?
                ORDER BY next_run_at
                """,
                (utc_text(current),),
            ).fetchall()
        completed: list[str] = []
        for row in rows:
            if self._scheduler_stop.is_set():
                break
            try:
                job = self.queue_policy_run(str(row["target_root_id"]))
                self.run_backup_pipeline(job.id)
                completed.append(job.id)
            except (ConflictError, NotFoundError, OSError, ValueError) as exc:
                self._audit_scheduler_failure(str(row["target_root_id"]), exc)
        return completed

    def create_verification(
        self,
        backup_job_id: str,
        target_root_id: str,
    ) -> BackupVerificationRecord:
        _, manifest = self._load_snapshot(backup_job_id)
        target = self._root(target_root_id, purpose="primary", writable=True)
        objects = manifest["objects"]
        total_bytes = sum(int(item["size_bytes"]) for item in objects)
        verification_id = str(uuid.uuid4())
        now = utc_text()
        with self.database.transaction() as connection:
            active = connection.execute(
                """
                SELECT id FROM backup_verifications
                WHERE status IN ('queued', 'running') LIMIT 1
                """
            ).fetchone()
            if active is not None:
                raise ConflictError("another backup verification is already queued or running")
            connection.execute(
                """
                INSERT INTO backup_verifications(
                    id, backup_job_id, target_root_id, status, total_objects,
                    total_bytes, checked_objects, checked_bytes, error, created_at,
                    started_at, completed_at, updated_at
                ) VALUES(?, ?, ?, 'queued', ?, ?, 0, 0, '', ?, NULL, NULL, ?)
                """,
                (
                    verification_id,
                    backup_job_id,
                    target.id,
                    len(objects),
                    total_bytes,
                    now,
                    now,
                ),
            )
            self.repository._audit_tx(
                connection,
                actor_type="manager",
                actor_id=None,
                action="backup.verification.queued",
                target_type="backup_verification",
                target_id=verification_id,
                detail=(
                    f"Пробное восстановление снимка {backup_job_id} на {target.id}; "
                    f"объектов {len(objects)}"
                ),
            )
        return self.get_verification(verification_id)

    def list_verifications(self, limit: int = 50) -> list[BackupVerificationRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM backup_verifications ORDER BY created_at DESC LIMIT ?",
                (max(1, min(200, limit)),),
            ).fetchall()
        return [self._verification(row) for row in rows]

    def get_verification(self, verification_id: str) -> BackupVerificationRecord:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM backup_verifications WHERE id = ?",
                (verification_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError("backup verification not found")
        return self._verification(row)

    def run_verification(self, verification_id: str) -> None:
        lock = self._verification_lock(verification_id)
        if not lock.acquire(blocking=False):
            return
        staging: Path | None = None
        try:
            verification = self.get_verification(verification_id)
            if verification.status != "queued":
                return
            target = self._root(
                verification.target_root_id, purpose="primary", writable=True
            )
            snapshot, manifest = self._load_snapshot(verification.backup_job_id)
            self._verify_snapshot_database(snapshot / "metadata.sqlite3")
            objects = manifest["objects"]
            total_bytes = sum(int(item["size_bytes"]) for item in objects)
            started_at = utc_text()
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE backup_verifications
                    SET status = 'running', total_objects = ?, total_bytes = ?,
                        checked_objects = 0, checked_bytes = 0, error = '',
                        started_at = ?, completed_at = NULL, updated_at = ?
                    WHERE id = ? AND status = 'queued'
                    """,
                    (
                        len(objects),
                        total_bytes,
                        started_at,
                        started_at,
                        verification_id,
                    ),
                )
            staging = target.path / ".staging" / f"verify-{verification_id}"
            staging.mkdir(parents=False)
            checked_objects = 0
            checked_bytes = 0
            for item in objects:
                if self.get_verification(verification_id).status == "cancelled":
                    return
                expected_size = int(item["size_bytes"])
                expected_hash = str(item["sha256"])
                source = self._safe_file(
                    snapshot, str(item.get("backup_object_path", ""))
                )
                temporary = staging / f"{checked_objects:08d}.restore-drill"
                copied, digest = self._copy_verified(source, temporary)
                if copied != expected_size or digest != expected_hash:
                    raise ConflictError(
                        "backup object failed trial-restore size or SHA-256 verification"
                    )
                temporary.unlink()
                checked_objects += 1
                checked_bytes += copied
                with self.database.transaction() as connection:
                    connection.execute(
                        """
                        UPDATE backup_verifications
                        SET checked_objects = ?, checked_bytes = ?, updated_at = ?
                        WHERE id = ? AND status = 'running'
                        """,
                        (checked_objects, checked_bytes, utc_text(), verification_id),
                    )
            staging.rmdir()
            staging = None
            completed_at = utc_text()
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE backup_verifications
                    SET status = 'completed', completed_at = ?, updated_at = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (completed_at, completed_at, verification_id),
                )
                self.repository._audit_tx(
                    connection,
                    actor_type="manager",
                    actor_id=None,
                    action="backup.verification.completed",
                    target_type="backup_verification",
                    target_id=verification_id,
                    detail=(
                        f"Пробное восстановление успешно: объектов {checked_objects}, "
                        f"байт {checked_bytes}"
                    ),
                )
        except Exception as exc:
            completed_at = utc_text()
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE backup_verifications
                    SET status = 'failed', error = ?, completed_at = ?, updated_at = ?
                    WHERE id = ? AND status != 'cancelled'
                    """,
                    (str(exc)[:1000], completed_at, completed_at, verification_id),
                )
                self.repository._audit_tx(
                    connection,
                    actor_type="manager",
                    actor_id=None,
                    action="backup.verification.failed",
                    target_type="backup_verification",
                    target_id=verification_id,
                    detail=str(exc)[:1000],
                )
        finally:
            if staging is not None:
                self._remove_tree(staging)
            lock.release()

    def cancel_verification(self, verification_id: str) -> BackupVerificationRecord:
        verification = self.get_verification(verification_id)
        if verification.status in {"completed", "cancelled"}:
            return verification
        now = utc_text()
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE backup_verifications
                SET status = 'cancelled', completed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, now, verification_id),
            )
        return self.get_verification(verification_id)

    def apply_retention(
        self,
        target_root_id: str,
        keep_last: int,
        *,
        newest_job_id: str | None = None,
    ) -> list[str]:
        if not 1 <= keep_last <= 365:
            raise ValueError("backup retention must keep between 1 and 365 snapshots")
        root = self._root(target_root_id, purpose="backup", writable=True)
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT b.id, b.snapshot_path
                FROM backup_jobs AS b
                WHERE b.target_root_id = ? AND b.status = 'completed'
                  AND b.pruned_at IS NULL AND b.snapshot_path != ''
                  AND EXISTS(
                      SELECT 1 FROM backup_verifications AS v
                      WHERE v.backup_job_id = b.id AND v.status = 'completed'
                  )
                  AND NOT EXISTS(
                      SELECT 1 FROM restore_jobs AS r
                      WHERE r.backup_job_id = b.id AND r.status IN ('queued', 'running')
                  )
                ORDER BY CASE WHEN b.id = ? THEN 0 ELSE 1 END,
                         b.completed_at DESC, b.rowid DESC
                """,
                (root.id, newest_job_id or ""),
            ).fetchall()
        pruned: list[str] = []
        for row in rows[keep_last:]:
            snapshot = self._safe_snapshot_directory(root.path, str(row["snapshot_path"]))
            quarantine = (
                root.path / ".staging" / f"prune-{row['id']}-{uuid.uuid4().hex[:8]}"
            )
            os.replace(snapshot, quarantine)
            now = utc_text()
            try:
                with self.database.transaction() as connection:
                    updated = connection.execute(
                        """
                        UPDATE backup_jobs
                        SET snapshot_path = '', pruned_at = ?, updated_at = ?
                        WHERE id = ? AND pruned_at IS NULL
                        """,
                        (now, now, row["id"]),
                    )
                    if updated.rowcount != 1:
                        raise ConflictError("backup snapshot retention state changed")
                    self.repository._audit_tx(
                        connection,
                        actor_type="system",
                        actor_id=None,
                        action="backup.snapshot.pruned",
                        target_type="backup_job",
                        target_id=str(row["id"]),
                        detail=(
                            f"Проверенный снимок удалён политикой хранения; "
                            f"сохраняются последние {keep_last}"
                        ),
                    )
            except Exception:
                os.replace(quarantine, snapshot)
                raise
            self._remove_tree(quarantine)
            pruned.append(str(row["id"]))
        return pruned

    def _scheduler_loop(self) -> None:
        if self._scheduler_stop.wait(5):
            return
        while not self._scheduler_stop.is_set():
            try:
                self.run_due_policies()
            except Exception as exc:
                self._audit_scheduler_failure("scheduler", exc)
            self._scheduler_stop.wait(self.scheduler_interval_seconds)

    def _load_snapshot(self, backup_job_id: str) -> tuple[Path, dict[str, Any]]:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT b.status, b.snapshot_path, b.pruned_at, r.path AS root_path
                FROM backup_jobs AS b
                JOIN storage_roots AS r ON r.id = b.target_root_id
                WHERE b.id = ?
                """,
                (backup_job_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError("backup snapshot not found")
        if (
            row["status"] != "completed"
            or row["pruned_at"] is not None
            or not row["snapshot_path"]
        ):
            raise ConflictError("only a retained completed snapshot can be verified")
        snapshot = self._safe_snapshot_directory(
            Path(row["root_path"]), str(row["snapshot_path"])
        )
        manifest_path = snapshot / "manifest.json"
        metadata_path = snapshot / "metadata.sqlite3"
        if not manifest_path.is_file() or not metadata_path.is_file():
            raise NotFoundError("backup snapshot manifest or metadata is unavailable")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConflictError("backup manifest is damaged") from exc
        if manifest.get("schema_version") != 1 or not isinstance(
            manifest.get("objects"), list
        ):
            raise ConflictError("backup manifest schema is unsupported")
        return snapshot, manifest

    @staticmethod
    def _verify_snapshot_database(path: Path) -> None:
        try:
            connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
            try:
                quick = connection.execute("PRAGMA quick_check").fetchone()
                foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchone()
            finally:
                connection.close()
        except sqlite3.DatabaseError as exc:
            raise ConflictError("backup metadata database is damaged") from exc
        if quick is None or quick[0] != "ok" or foreign_keys is not None:
            raise ConflictError("backup metadata database failed integrity verification")

    def _root(
        self,
        root_id: str,
        *,
        purpose: str,
        writable: bool,
    ) -> StorageRootRecord:
        root = next((item for item in self.storage.list_roots() if item.id == root_id), None)
        if root is None:
            raise NotFoundError("managed storage root is unavailable")
        if root.purpose != purpose:
            raise ConflictError(f"storage root must have the {purpose} role")
        if writable and not root.write_enabled:
            raise ConflictError("managed storage root does not accept writes")
        return root

    def _require_server_writable(self) -> None:
        if not self._server_is_writable():
            raise ConflictError("server is in emergency read-only mode")

    def _server_is_writable(self) -> bool:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT mode FROM server_state WHERE id = 1"
            ).fetchone()
        return row is not None and row["mode"] == "normal"

    def _audit_scheduler_failure(self, target_id: str, exc: Exception) -> None:
        with self.database.transaction() as connection:
            self.repository._audit_tx(
                connection,
                actor_type="system",
                actor_id=None,
                action="backup.scheduler.failed",
                target_type="storage_root",
                target_id=target_id,
                detail=str(exc)[:1000],
            )

    @staticmethod
    def _safe_snapshot_directory(root: Path, relative: str) -> Path:
        try:
            resolved_root = root.resolve(strict=True)
            backups = (resolved_root / "backups").resolve(strict=True)
            path = (resolved_root / relative).resolve(strict=True)
        except OSError as exc:
            raise NotFoundError("backup snapshot directory is unavailable") from exc
        if (
            path.parent != backups
            or not path.is_dir()
            or path.is_symlink()
            or resolved_root not in path.parents
        ):
            raise PermissionDeniedError("backup snapshot failed containment validation")
        return path

    @staticmethod
    def _safe_file(root: Path, relative: str) -> Path:
        try:
            resolved_root = root.resolve(strict=True)
            path = (resolved_root / relative).resolve(strict=True)
        except OSError as exc:
            raise NotFoundError("backup object is unavailable") from exc
        if resolved_root not in path.parents or not path.is_file() or path.is_symlink():
            raise PermissionDeniedError("backup object failed containment validation")
        return path

    @staticmethod
    def _copy_verified(source: Path, destination: Path) -> tuple[int, str]:
        digest = hashlib.sha256()
        copied = 0
        with source.open("rb") as reader, destination.open("xb") as writer:
            while chunk := reader.read(4 * 1024 * 1024):
                writer.write(chunk)
                digest.update(chunk)
                copied += len(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        return copied, digest.hexdigest()

    @staticmethod
    def _remove_tree(path: Path) -> None:
        if not path.exists():
            return

        def make_writable_and_retry(function, item, _exc_info) -> None:
            item_path = Path(item)
            try:
                if os.name == "nt":
                    os.chmod(item_path, stat.S_IREAD | stat.S_IWRITE)
                else:
                    os.chmod(
                        item_path,
                        item_path.stat().st_mode | stat.S_IWUSR | stat.S_IRUSR,
                    )
                function(item)
            except OSError:
                raise

        shutil.rmtree(path, onexc=make_writable_and_retry)

    def _verification_lock(self, verification_id: str) -> threading.Lock:
        with self._verification_locks_guard:
            return self._verification_locks.setdefault(verification_id, threading.Lock())

    @staticmethod
    def _policy(row: sqlite3.Row) -> BackupPolicyRecord:
        values = {field: row[field] for field in BackupPolicyRecord.__dataclass_fields__}
        values["enabled"] = bool(values["enabled"])
        return BackupPolicyRecord(**values)

    @staticmethod
    def _verification(row: sqlite3.Row) -> BackupVerificationRecord:
        return BackupVerificationRecord(
            **{
                field: row[field]
                for field in BackupVerificationRecord.__dataclass_fields__
            }
        )

    @staticmethod
    def policy_to_dict(record: BackupPolicyRecord) -> dict[str, Any]:
        return asdict(record)

    @staticmethod
    def verification_to_dict(record: BackupVerificationRecord) -> dict[str, Any]:
        return asdict(record)
