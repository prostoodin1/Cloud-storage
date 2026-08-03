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
from pathlib import Path
from typing import Any

from cloud_storage.core.database import Database
from cloud_storage.core.repository import (
    ConflictError,
    CoreRepository,
    NotFoundError,
    PermissionDeniedError,
    utc_text,
)
from cloud_storage.core.storage import StorageCapacityError, StorageRootRecord, StorageService
from cloud_storage.services.security import make_managed_file_inert


@dataclass(frozen=True, slots=True)
class RestoreJobRecord:
    id: str
    status: str
    backup_job_id: str
    target_root_id: str
    total_objects: int
    total_bytes: int
    processed_objects: int
    processed_bytes: int
    restored_objects: int
    skipped_objects: int
    failed_objects: int
    error: str
    created_at: str
    started_at: str | None
    completed_at: str | None
    updated_at: str


class RecoveryService:
    def __init__(
        self,
        database: Database,
        repository: CoreRepository,
        storage: StorageService,
    ) -> None:
        self.database = database
        self.repository = repository
        self.storage = storage
        self._job_locks: dict[str, threading.Lock] = {}
        self._job_locks_guard = threading.Lock()

    def server_mode(self) -> dict[str, Any]:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT mode, reason, changed_at FROM server_state WHERE id = 1"
            ).fetchone()
        if row is None:
            raise NotFoundError("server state is unavailable")
        return {
            "mode": str(row["mode"]),
            "reason": str(row["reason"]),
            "changed_at": str(row["changed_at"]),
        }

    def set_server_mode(self, mode: str, reason: str = "") -> dict[str, Any]:
        if mode not in {"normal", "read_only"}:
            raise ValueError("server mode must be normal or read_only")
        now = utc_text()
        reason = reason.strip()[:500]
        cancelled = 0
        with self.database.transaction() as connection:
            previous = connection.execute(
                "SELECT mode FROM server_state WHERE id = 1"
            ).fetchone()
            connection.execute(
                "UPDATE server_state SET mode = ?, reason = ?, changed_at = ? WHERE id = 1",
                (mode, reason, now),
            )
            if mode == "read_only":
                for table in (
                    "maintenance_jobs",
                    "backup_jobs",
                    "mirror_jobs",
                    "restore_jobs",
                ):
                    updated = connection.execute(
                        f"""
                        UPDATE {table}
                        SET status = 'cancelled', completed_at = ?, updated_at = ?
                        WHERE status IN ('queued', 'running')
                        """,
                        (now, now),
                    )
                    cancelled += updated.rowcount
            self.repository._audit_tx(
                connection,
                actor_type="manager",
                actor_id=None,
                action="server.mode.changed",
                target_type="server",
                target_id=None,
                detail=(
                    f"Режим {previous['mode'] if previous else 'unknown'} → {mode}; "
                    f"остановлено заданий {cancelled}; причина: {reason or 'не указана'}"
                ),
            )
        result = self.server_mode()
        result["cancelled_jobs"] = cancelled
        return result

    def require_writable(self) -> None:
        if self.server_mode()["mode"] != "normal":
            raise ConflictError("server is in emergency read-only mode")

    def recover_interrupted_jobs(self) -> int:
        now = utc_text()
        with self.database.transaction() as connection:
            updated = connection.execute(
                """
                UPDATE restore_jobs
                SET status = 'failed', error = ?, completed_at = ?, updated_at = ?
                WHERE status IN ('queued', 'running')
                """,
                (
                    "Восстановление прервано перезапуском Core; его можно повторить",
                    now,
                    now,
                ),
            )
        return updated.rowcount

    def create_restore_job(
        self,
        backup_job_id: str,
        target_root_id: str,
    ) -> RestoreJobRecord:
        target = self._target_root(target_root_id)
        _, manifest = self._load_snapshot(backup_job_id)
        objects = manifest["objects"]
        total_bytes = sum(int(item["size_bytes"]) for item in objects)
        try:
            shutil.disk_usage(target.path)
        except OSError as exc:
            raise StorageCapacityError("restore target storage is unavailable") from exc
        job_id = str(uuid.uuid4())
        now = utc_text()
        with self.database.transaction() as connection:
            active = connection.execute(
                "SELECT id FROM restore_jobs WHERE status IN ('queued', 'running') LIMIT 1"
            ).fetchone()
            if active is not None:
                raise ConflictError("another restore job is already queued or running")
            connection.execute(
                """
                INSERT INTO restore_jobs(
                    id, status, backup_job_id, target_root_id, total_objects, total_bytes,
                    processed_objects, processed_bytes, restored_objects, skipped_objects,
                    failed_objects, error, created_at, started_at, completed_at, updated_at
                ) VALUES(?, 'queued', ?, ?, ?, ?, 0, 0, 0, 0, 0, '', ?, NULL, NULL, ?)
                """,
                (
                    job_id,
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
                action="recovery.restore.queued",
                target_type="restore_job",
                target_id=job_id,
                detail=(
                    f"Снимок {backup_job_id} → {target.id}; объектов {len(objects)}, "
                    f"байт {total_bytes}"
                ),
            )
        return self.get_restore_job(job_id)

    def list_restore_jobs(self, limit: int = 50) -> list[RestoreJobRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM restore_jobs ORDER BY created_at DESC LIMIT ?",
                (max(1, min(200, limit)),),
            ).fetchall()
        return [self._restore_job(row) for row in rows]

    def get_restore_job(self, job_id: str) -> RestoreJobRecord:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM restore_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("restore job not found")
        return self._restore_job(row)

    def run_restore_job(self, job_id: str) -> None:
        lock = self._job_lock(job_id)
        if not lock.acquire(blocking=False):
            return
        try:
            job = self.get_restore_job(job_id)
            if job.status != "queued":
                return
            target = self._target_root(job.target_root_id)
            snapshot, manifest = self._load_snapshot(job.backup_job_id)
            objects = manifest["objects"]
            total_bytes = sum(int(item["size_bytes"]) for item in objects)
            started_at = utc_text()
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE restore_jobs
                    SET status = 'running', total_objects = ?, total_bytes = ?,
                        processed_objects = 0, processed_bytes = 0, restored_objects = 0,
                        skipped_objects = 0, failed_objects = 0, error = '',
                        started_at = ?, completed_at = NULL, updated_at = ?
                    WHERE id = ? AND status = 'queued'
                    """,
                    (len(objects), total_bytes, started_at, started_at, job_id),
                )
            processed = 0
            processed_bytes = 0
            restored = 0
            skipped = 0
            failed = 0
            errors: list[str] = []
            for item in objects:
                if self.get_restore_job(job_id).status == "cancelled":
                    return
                try:
                    outcome = self._restore_item(snapshot, target, item)
                    if outcome == "restored":
                        restored += 1
                    else:
                        skipped += 1
                except Exception as exc:
                    failed += 1
                    errors.append(f"{item.get('entity')}:{item.get('id')}: {exc}")
                processed += 1
                processed_bytes += int(item["size_bytes"])
                with self.database.transaction() as connection:
                    connection.execute(
                        """
                        UPDATE restore_jobs
                        SET processed_objects = ?, processed_bytes = ?, restored_objects = ?,
                            skipped_objects = ?, failed_objects = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            processed,
                            processed_bytes,
                            restored,
                            skipped,
                            failed,
                            utc_text(),
                            job_id,
                        ),
                    )
            completed_at = utc_text()
            final_status = "failed" if failed else "completed"
            error = "; ".join(errors)[:1000]
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE restore_jobs
                    SET status = ?, error = ?, completed_at = ?, updated_at = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (final_status, error, completed_at, completed_at, job_id),
                )
                self.repository._audit_tx(
                    connection,
                    actor_type="manager",
                    actor_id=None,
                    action=(
                        "recovery.restore.completed" if not failed else "recovery.restore.degraded"
                    ),
                    target_type="restore_job",
                    target_id=job_id,
                    detail=(
                        f"Восстановлено {restored}, пропущено {skipped}, ошибок {failed}"
                    ),
                )
        except Exception as exc:
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE restore_jobs SET status = 'failed', error = ?, updated_at = ?
                    WHERE id = ? AND status != 'cancelled'
                    """,
                    (str(exc)[:1000], utc_text(), job_id),
                )
        finally:
            lock.release()

    def resume_restore_job(self, job_id: str) -> RestoreJobRecord:
        job = self.get_restore_job(job_id)
        if job.status not in {"failed", "cancelled"}:
            raise ConflictError("only failed or cancelled restore jobs can be resumed")
        with self.database.transaction() as connection:
            active = connection.execute(
                "SELECT id FROM restore_jobs WHERE status IN ('queued', 'running') LIMIT 1"
            ).fetchone()
            if active is not None:
                raise ConflictError("another restore job is already queued or running")
            connection.execute(
                "UPDATE restore_jobs SET status = 'queued', error = '', completed_at = NULL, "
                "updated_at = ? WHERE id = ?",
                (utc_text(), job_id),
            )
        return self.get_restore_job(job_id)

    def cancel_restore_job(self, job_id: str) -> RestoreJobRecord:
        job = self.get_restore_job(job_id)
        if job.status in {"completed", "cancelled"}:
            return job
        now = utc_text()
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE restore_jobs SET status = 'cancelled', completed_at = ?, updated_at = ? "
                "WHERE id = ?",
                (now, now, job_id),
            )
        return self.get_restore_job(job_id)

    def _restore_item(
        self,
        snapshot: Path,
        target: StorageRootRecord,
        item: dict[str, Any],
    ) -> str:
        entity = str(item.get("entity"))
        table = {"files": "files", "file_versions": "file_versions"}.get(entity)
        if table is None:
            raise ConflictError("backup manifest contains an unsupported entity")
        object_id = str(item.get("id", ""))
        expected_size = int(item.get("size_bytes", -1))
        expected_sha256 = str(item.get("sha256", ""))
        if not object_id or expected_size < 0 or len(expected_sha256) != 64:
            raise ConflictError("backup manifest object metadata is invalid")
        with self.database.connection() as connection:
            live = connection.execute(
                f"""
                SELECT id, storage_root_id, object_path, size_bytes, sha256
                FROM {table} WHERE id = ?
                """,
                (object_id,),
            ).fetchone()
        if (
            live is None
            or int(live["size_bytes"]) != expected_size
            or str(live["sha256"]) != expected_sha256
        ):
            return "skipped"
        if self._live_object_is_healthy(live):
            return "skipped"

        source = self._safe_file(snapshot, str(item.get("backup_object_path", "")))
        try:
            usage = shutil.disk_usage(target.path)
        except OSError as exc:
            raise StorageCapacityError("restore target storage is unavailable") from exc
        projected_percent = (
            (usage.used + expected_size) / usage.total * 100 if usage.total else 100
        )
        if (
            usage.free - expected_size < target.min_free_bytes
            or projected_percent > target.max_fill_percent
        ):
            raise StorageCapacityError("restore target has insufficient safe free space")

        new_id = uuid.uuid4().hex
        relative = (
            Path("objects") / "recovered" / object_id[:2] / f"{object_id}.{new_id[:8]}.blob"
        )
        destination = target.path / relative
        staging = target.path / ".staging" / f"{new_id}.restore"
        destination.parent.mkdir(parents=True, exist_ok=True)
        published = False
        try:
            copied, digest = self._copy_verified(source, staging)
            if copied != expected_size or digest != expected_sha256:
                raise ConflictError("backup object failed size or SHA-256 verification")
            os.replace(staging, destination)
            published = True
            make_managed_file_inert(destination, target.path)
            with self.database.transaction() as connection:
                updated = connection.execute(
                    f"""
                    UPDATE {table}
                    SET storage_root_id = ?, object_path = ?
                    WHERE id = ? AND storage_root_id = ? AND object_path = ?
                      AND size_bytes = ? AND sha256 = ?
                    """,
                    (
                        target.id,
                        relative.as_posix(),
                        object_id,
                        live["storage_root_id"],
                        live["object_path"],
                        expected_size,
                        expected_sha256,
                    ),
                )
                if updated.rowcount != 1:
                    self._remove_file(destination)
                    return "skipped"
        except Exception:
            self._remove_file(staging)
            if published:
                self._remove_file(destination)
            raise
        return "restored"

    def _live_object_is_healthy(self, row: sqlite3.Row) -> bool:
        with self.database.connection() as connection:
            root = connection.execute(
                "SELECT path FROM storage_roots WHERE id = ?",
                (row["storage_root_id"],),
            ).fetchone()
        if root is None:
            return False
        try:
            path = self._safe_file(Path(root["path"]), str(row["object_path"]))
            return (
                path.stat().st_size == int(row["size_bytes"])
                and self._sha256(path) == row["sha256"]
            )
        except (OSError, NotFoundError, PermissionDeniedError):
            return False

    def _load_snapshot(self, backup_job_id: str) -> tuple[Path, dict[str, Any]]:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT b.status, b.snapshot_path, r.path AS root_path
                FROM backup_jobs b
                JOIN storage_roots r ON r.id = b.target_root_id
                WHERE b.id = ?
                """,
                (backup_job_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError("backup snapshot not found")
        if row["status"] != "completed" or not row["snapshot_path"]:
            raise ConflictError("only a completed backup snapshot can be restored")
        root = Path(row["root_path"])
        snapshot = self._safe_directory(root, str(row["snapshot_path"]))
        metadata_path = snapshot / "metadata.sqlite3"
        manifest_path = snapshot / "manifest.json"
        if not metadata_path.is_file() or not manifest_path.is_file():
            raise NotFoundError("backup snapshot metadata or manifest is unavailable")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ConflictError("backup manifest is damaged") from exc
        if manifest.get("schema_version") != 1 or not isinstance(manifest.get("objects"), list):
            raise ConflictError("backup manifest schema is unsupported")
        return snapshot, manifest

    def _target_root(self, root_id: str) -> StorageRootRecord:
        root = next((item for item in self.storage.list_roots() if item.id == root_id), None)
        if root is None:
            raise NotFoundError("restore target storage is unavailable")
        if root.purpose != "primary" or not root.write_enabled:
            raise ConflictError("restore target must be an active writable primary storage")
        return root

    @staticmethod
    def _safe_file(root: Path, relative: str) -> Path:
        try:
            resolved_root = root.resolve(strict=True)
            path = (resolved_root / relative).resolve(strict=True)
        except OSError as exc:
            raise NotFoundError("managed object is unavailable") from exc
        if resolved_root not in path.parents or not path.is_file() or path.is_symlink():
            raise PermissionDeniedError("managed object failed containment validation")
        return path

    @staticmethod
    def _safe_directory(root: Path, relative: str) -> Path:
        try:
            resolved_root = root.resolve(strict=True)
            path = (resolved_root / relative).resolve(strict=True)
        except OSError as exc:
            raise NotFoundError("backup snapshot directory is unavailable") from exc
        if resolved_root not in path.parents or not path.is_dir() or path.is_symlink():
            raise PermissionDeniedError("backup snapshot failed containment validation")
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
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(4 * 1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _remove_file(path: Path) -> None:
        if not path.exists():
            return
        try:
            if os.name == "nt":
                os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
            else:
                os.chmod(path, path.stat().st_mode | stat.S_IWUSR | stat.S_IRUSR)
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def _job_lock(self, job_id: str) -> threading.Lock:
        with self._job_locks_guard:
            return self._job_locks.setdefault(job_id, threading.Lock())

    @staticmethod
    def _restore_job(row: sqlite3.Row) -> RestoreJobRecord:
        return RestoreJobRecord(
            **{field: row[field] for field in RestoreJobRecord.__dataclass_fields__}
        )

    @staticmethod
    def restore_job_to_dict(record: RestoreJobRecord) -> dict[str, Any]:
        return asdict(record)
