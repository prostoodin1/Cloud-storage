from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import stat
import threading
import time
import unicodedata
import uuid
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from cloud_storage.core.config import CoreConfig
from cloud_storage.core.database import Database
from cloud_storage.core.repository import (
    ConflictError,
    CoreRepository,
    NotFoundError,
    PermissionDeniedError,
    utc_now,
    utc_text,
)
from cloud_storage.services.security import make_managed_file_inert


class InvalidLogicalPath(ValueError):
    pass


class InvalidStorageRoot(ValueError):
    pass


class StorageCapacityError(OSError):
    pass


@dataclass(frozen=True, slots=True)
class StorageRootRecord:
    id: str
    disk_id: str | None
    path: Path
    write_enabled: bool
    purpose: str
    priority: int
    max_fill_percent: int
    min_free_bytes: int


@dataclass(frozen=True, slots=True)
class FileRecord:
    id: str
    space_id: str
    logical_path: str
    storage_root_id: str
    object_path: str
    size_bytes: int
    sha256: str
    content_type: str
    uploaded_by: str
    version: int
    created_at: str
    modified_at: str
    deleted_at: str | None


@dataclass(frozen=True, slots=True)
class PublicShareRecord:
    id: str
    owner_user_id: str
    space_id: str
    logical_path: str
    kind: str
    token: str
    expires_at: str
    created_at: str
    revoked_at: str | None


@dataclass(frozen=True, slots=True)
class ManagedRootRequest:
    disk_id: str
    path: Path
    priority: int
    max_fill_percent: int
    min_free_bytes: int
    write_enabled: bool = True
    purpose: str = "primary"


@dataclass(frozen=True, slots=True)
class ResumableUploadRecord:
    id: str
    space_id: str
    user_id: str
    storage_root_id: str
    logical_path: str
    temporary_path: str
    staging_path: str
    expected_size: int
    expected_sha256: str | None
    received_bytes: int
    content_type: str
    status: str
    result_file_id: str | None
    created_at: str
    updated_at: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class TransferRecord:
    id: str
    direction: str
    status: str
    file_id: str
    space_id: str
    user_id: str
    logical_path: str
    content_type: str
    total_bytes: int
    network_bytes: int
    storage_bytes: int
    sha256: str
    staging_path: str
    temporary_path: str
    storage_root_id: str
    object_path: str
    error: str
    created_at: str
    updated_at: str
    completed_at: str | None


@dataclass(frozen=True, slots=True)
class MigrationJobRecord:
    id: str
    kind: str
    status: str
    source_root_id: str
    target_root_id: str
    space_id: str | None
    total_files: int
    total_bytes: int
    processed_files: int
    processed_bytes: int
    retained_sources: int
    error: str
    created_at: str
    started_at: str | None
    completed_at: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class BackupJobRecord:
    id: str
    status: str
    target_root_id: str
    total_files: int
    total_bytes: int
    processed_files: int
    processed_bytes: int
    snapshot_path: str
    error: str
    created_at: str
    started_at: str | None
    completed_at: str | None
    pruned_at: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class MirrorReplicaRecord:
    file_id: str
    storage_root_id: str
    object_path: str
    size_bytes: int
    sha256: str
    source_version: int
    status: str
    error: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class MirrorJobRecord:
    id: str
    status: str
    target_root_id: str
    total_files: int
    total_bytes: int
    processed_files: int
    processed_bytes: int
    healthy_files: int
    repaired_files: int
    failed_files: int
    error: str
    created_at: str
    started_at: str | None
    completed_at: str | None
    updated_at: str


def normalize_logical_path(value: str) -> str:
    value = unicodedata.normalize("NFC", value.strip())
    if not value or value.startswith(("/", "\\")) or "\\" in value or "//" in value:
        raise InvalidLogicalPath("path must be a non-empty relative POSIX path")
    if len(value.encode("utf-8")) > 1024:
        raise InvalidLogicalPath("path is too long")
    path = PurePosixPath(value)
    parts = path.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise InvalidLogicalPath("path traversal is not allowed")
    for part in parts:
        if len(part.encode("utf-8")) > 255:
            raise InvalidLogicalPath("path component is too long")
        if any(ord(char) < 32 or ord(char) == 127 for char in part):
            raise InvalidLogicalPath("control characters are not allowed")
    return path.as_posix()


def sanitize_content_type(value: str | None) -> str:
    candidate = (value or "application/octet-stream").split(";", 1)[0].strip().lower()
    if not candidate or len(candidate) > 127 or any(char in candidate for char in "\r\n"):
        return "application/octet-stream"
    return candidate


class UploadSession:
    def __init__(
        self,
        service: StorageService,
        *,
        root: StorageRootRecord,
        space_id: str,
        user_id: str,
        logical_path: str,
        content_type: str,
        maximum_bytes: int,
        expected_size: int | None,
    ) -> None:
        self.service = service
        self.root = root
        self.space_id = space_id
        self.user_id = user_id
        self.logical_path = logical_path
        self.content_type = content_type
        self.maximum_bytes = maximum_bytes
        self.file_id = str(uuid.uuid4())
        self.transfer_id = self.file_id
        self.object_relative = (
            Path("objects") / space_id / self.file_id[:2] / f"{self.file_id}.blob"
        )
        self.final_path = root.path / self.object_relative
        self.staging_root = service._select_staging_path(
            expected_size or min(maximum_bytes, 64 * 1024**2), root
        )
        staging_directory = (
            self.staging_root / ".staging"
            if self.staging_root == root.path
            else self.staging_root / "incoming"
        )
        staging_directory.mkdir(parents=True, exist_ok=True)
        self.temporary_path = staging_directory / f"{self.file_id}.part"
        self.temporary_relative = self.temporary_path.relative_to(self.staging_root)
        self._handle: BinaryIO = self.temporary_path.open("xb")
        self._hash = hashlib.sha256()
        self._size = 0
        self._finished = False
        try:
            self.service._create_inbound_transfer(
                transfer_id=self.transfer_id,
                file_id=self.file_id,
                space_id=self.space_id,
                user_id=self.user_id,
                logical_path=self.logical_path,
                content_type=self.content_type,
                total_bytes=expected_size or 0,
                staging_path=self.staging_root,
                temporary_path=self.temporary_relative,
                storage_root_id=self.root.id,
                object_path=self.object_relative,
            )
        except Exception:
            self._handle.close()
            self.temporary_path.unlink(missing_ok=True)
            raise

    def write(self, chunk: bytes) -> None:
        if self._finished:
            raise RuntimeError("upload session is already closed")
        if not chunk:
            return
        if self._size + len(chunk) > self.maximum_bytes:
            raise StorageCapacityError("upload exceeds quota or configured size limit")
        self._handle.write(chunk)
        self._hash.update(chunk)
        self._size += len(chunk)
        self.service._update_transfer_network(self.transfer_id, self._size)

    def commit(self) -> FileRecord:
        if self._finished:
            raise RuntimeError("upload session is already closed")
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._handle.close()
        digest = self._hash.hexdigest()
        self._finished = True
        try:
            self.service._prepare_inbound_move(
                self.transfer_id,
                total_bytes=self._size,
                sha256=digest,
            )
            return self.service._finalize_inbound_transfer(self.transfer_id)
        except Exception as exc:
            self.service._fail_transfer(self.transfer_id, str(exc))
            raise

    def abort(self) -> None:
        if self._finished:
            return
        self._handle.close()
        self.temporary_path.unlink(missing_ok=True)
        self.service._cancel_transfer(self.transfer_id, "Приём файла прерван")
        self._finished = True

    def __enter__(self) -> UploadSession:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if not self._finished:
            self.abort()


class StorageService:
    def __init__(
        self,
        config: CoreConfig,
        database: Database,
        repository: CoreRepository,
    ) -> None:
        self.config = config
        self.database = database
        self.repository = repository
        self._resumable_locks: dict[str, threading.RLock] = {}
        self._resumable_locks_guard = threading.Lock()
        self._maintenance_locks: dict[str, threading.Lock] = {}
        self._maintenance_locks_guard = threading.Lock()
        self._transfer_locks: dict[str, threading.RLock] = {}
        self._transfer_locks_guard = threading.Lock()

    def sync_managed_roots(self, requests: list[ManagedRootRequest]) -> list[StorageRootRecord]:
        prepared: list[ManagedRootRequest] = []
        seen_disks: set[str] = set()
        seen_paths: set[Path] = set()
        for request in requests:
            if request.disk_id in seen_disks:
                raise InvalidStorageRoot("a physical disk can have only one managed storage root")
            if not 0 <= request.priority <= 100:
                raise InvalidStorageRoot("storage priority must be between 0 and 100")
            if not 50 <= request.max_fill_percent <= 99:
                raise InvalidStorageRoot("max fill percent must be between 50 and 99")
            if request.min_free_bytes < 0:
                raise InvalidStorageRoot("minimum free space cannot be negative")
            if request.purpose not in {"primary", "backup", "mirror"}:
                raise InvalidStorageRoot("invalid storage purpose")
            path = request.path.expanduser()
            if not path.is_absolute() or path.name not in {
                "CloudStorageData",
                ".cloud-storage-data",
            }:
                raise InvalidStorageRoot(
                    "managed root must be an absolute CloudStorageData directory"
                )
            parent = path.parent.resolve(strict=True)
            path = parent / path.name
            if path in seen_paths:
                raise InvalidStorageRoot("duplicate managed storage path")
            self._initialize_managed_root(path, request.disk_id)
            prepared.append(
                ManagedRootRequest(
                    disk_id=request.disk_id,
                    path=path,
                    priority=request.priority,
                    max_fill_percent=request.max_fill_percent,
                    min_free_bytes=request.min_free_bytes,
                    write_enabled=request.write_enabled,
                    purpose=request.purpose,
                )
            )
            seen_disks.add(request.disk_id)
            seen_paths.add(path)

        now = utc_text()
        root_ids: list[str] = []
        with self.database.transaction() as connection:
            connection.execute("UPDATE storage_roots SET enabled = 0 WHERE id != 'default'")
            connection.execute(
                "UPDATE storage_roots SET enabled = ? WHERE id = 'default'",
                (0 if prepared else 1,),
            )
            for request in prepared:
                root_id = (
                    "managed-" + hashlib.sha256(request.disk_id.encode("utf-8")).hexdigest()[:16]
                )
                root_ids.append(root_id)
                connection.execute(
                    """
                    INSERT INTO storage_roots(
                        id, disk_id, path, enabled, write_enabled, purpose,
                        priority, max_fill_percent, min_free_bytes, created_at
                    ) VALUES(?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        disk_id = excluded.disk_id,
                        path = excluded.path,
                        enabled = 1,
                        write_enabled = excluded.write_enabled,
                        purpose = excluded.purpose,
                        priority = excluded.priority,
                        max_fill_percent = excluded.max_fill_percent,
                        min_free_bytes = excluded.min_free_bytes
                    """,
                    (
                        root_id,
                        request.disk_id,
                        str(request.path),
                        int(request.write_enabled),
                        request.purpose,
                        request.priority,
                        request.max_fill_percent,
                        request.min_free_bytes,
                        now,
                    ),
                )
            self.repository._audit_tx(
                connection,
                actor_type="manager",
                actor_id=None,
                action="storage.roots.synced",
                target_type="storage",
                target_id=None,
                detail=f"Активных управляемых хранилищ: {len(prepared)}",
            )
        active = {item.id: item for item in self.list_roots()}
        expected = root_ids or ["default"]
        return [active[item] for item in expected if item in active]

    @staticmethod
    def _initialize_managed_root(path: Path, disk_id: str) -> None:
        marker = path / ".cloud-storage-root.json"
        if path.exists() and not path.is_dir():
            raise InvalidStorageRoot("managed storage path is not a directory")
        if path.exists() and not marker.exists() and any(path.iterdir()):
            raise InvalidStorageRoot("refusing to adopt a non-empty directory without a marker")
        path.mkdir(parents=False, exist_ok=True)
        if marker.exists():
            try:
                payload = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise InvalidStorageRoot("managed storage marker is damaged") from exc
            if payload.get("disk_id") != disk_id:
                raise InvalidStorageRoot("managed storage belongs to a different physical disk")
        else:
            temporary = marker.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(
                    {"schema_version": 1, "disk_id": disk_id, "created_at": utc_text()},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            os.replace(temporary, marker)
        for name in (".staging", ".trash", "objects"):
            (path / name).mkdir(exist_ok=True)
        if os.name != "nt":
            path.chmod(0o700)

    def prepare_upload(
        self,
        *,
        space_id: str,
        user_id: str,
        logical_path: str,
        content_type: str | None,
        expected_size: int | None,
    ) -> UploadSession:
        self._require_server_writable()
        space = self.repository.require_space_permission(space_id, user_id, capability="upload")
        logical_path = normalize_logical_path(logical_path)
        self._require_file_path_available(space_id, logical_path)
        existing = self.find_file(space_id, logical_path, include_deleted=True)
        if existing and not existing.deleted_at:
            self.repository.require_space_permission(space_id, user_id, capability="modify")
        existing_size = existing.size_bytes if existing and not existing.deleted_at else 0
        used = self.space_usage(space_id)
        remaining_quota = max(0, space.quota_bytes - used + existing_size)
        maximum = min(self.config.max_upload_bytes, remaining_quota)
        if maximum <= 0:
            raise StorageCapacityError("personal storage quota is exhausted")
        if expected_size is not None:
            if expected_size < 0:
                raise ValueError("invalid Content-Length")
            if expected_size > maximum:
                raise StorageCapacityError("upload exceeds personal storage quota")
        root = self._select_root(expected_size or min(maximum, 64 * 1024**2), space)
        return UploadSession(
            self,
            root=root,
            space_id=space_id,
            user_id=user_id,
            logical_path=logical_path,
            content_type=sanitize_content_type(content_type),
            maximum_bytes=maximum,
            expected_size=expected_size,
        )

    def create_resumable_upload(
        self,
        *,
        space_id: str,
        user_id: str,
        logical_path: str,
        expected_size: int,
        content_type: str | None,
        expected_sha256: str | None = None,
    ) -> ResumableUploadRecord:
        self._require_server_writable()
        space = self.repository.require_space_permission(space_id, user_id, capability="upload")
        logical_path = normalize_logical_path(logical_path)
        self._require_file_path_available(space_id, logical_path)
        if expected_size < 0 or expected_size > self.config.max_upload_bytes:
            raise StorageCapacityError("upload exceeds configured size limit")
        expected_sha256 = (expected_sha256 or "").strip().casefold() or None
        if expected_sha256 and (
            len(expected_sha256) != 64
            or any(character not in "0123456789abcdef" for character in expected_sha256)
        ):
            raise ValueError("expected SHA-256 must contain 64 hexadecimal characters")
        existing = self.find_file(space_id, logical_path, include_deleted=True)
        if existing and not existing.deleted_at:
            self.repository.require_space_permission(space_id, user_id, capability="modify")
        existing_size = existing.size_bytes if existing and not existing.deleted_at else 0
        remaining_quota = max(
            0,
            space.quota_bytes - self.space_usage(space_id) + existing_size,
        )
        if expected_size > remaining_quota:
            raise StorageCapacityError("upload exceeds personal storage quota")
        root = self._select_root(expected_size, space)
        upload_id = str(uuid.uuid4())
        object_relative = Path("objects") / space_id / upload_id[:2] / f"{upload_id}.blob"
        staging_root = self._select_staging_path(expected_size, root)
        staging_directory = (
            staging_root / ".staging" if staging_root == root.path else staging_root / "incoming"
        )
        staging_directory.mkdir(parents=True, exist_ok=True)
        temporary_path = staging_directory / f"{upload_id}.resume"
        temporary_relative = temporary_path.relative_to(staging_root)
        now = utc_now()
        try:
            with temporary_path.open("xb") as handle:
                handle.flush()
                os.fsync(handle.fileno())
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO upload_sessions(
                        id, space_id, user_id, storage_root_id, logical_path,
                        temporary_path, staging_path, expected_size,
                        expected_sha256, received_bytes,
                        content_type, status, result_file_id, created_at, updated_at, expires_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 'active', NULL, ?, ?, ?)
                    """,
                    (
                        upload_id,
                        space_id,
                        user_id,
                        root.id,
                        logical_path,
                        temporary_relative.as_posix(),
                        str(staging_root),
                        expected_size,
                        expected_sha256,
                        sanitize_content_type(content_type),
                        utc_text(now),
                        utc_text(now),
                        utc_text(now + timedelta(hours=24)),
                    ),
                )
                self._insert_transfer_tx(
                    connection,
                    transfer_id=upload_id,
                    direction="inbound",
                    status="receiving",
                    file_id=upload_id,
                    space_id=space_id,
                    user_id=user_id,
                    logical_path=logical_path,
                    content_type=sanitize_content_type(content_type),
                    total_bytes=expected_size,
                    staging_path=staging_root,
                    temporary_path=temporary_relative,
                    storage_root_id=root.id,
                    object_path=object_relative,
                    now=utc_text(now),
                )
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise
        return self.get_resumable_upload(upload_id, user_id)

    def get_resumable_upload(
        self,
        upload_id: str,
        user_id: str,
    ) -> ResumableUploadRecord:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM upload_sessions WHERE id = ? AND user_id = ?",
                (upload_id, user_id),
            ).fetchone()
        if row is None:
            raise NotFoundError("upload session not found")
        record = self._resumable_upload(row)
        if record.status == "active" and record.expires_at < utc_text():
            self.cancel_resumable_upload(upload_id, user_id)
            raise NotFoundError("upload session expired")
        if record.status == "active":
            path = self._resumable_path(record)
            actual_size = path.stat().st_size
            if actual_size > record.expected_size:
                raise ConflictError("partial upload exceeds expected size")
            if actual_size != record.received_bytes:
                with self.database.transaction() as connection:
                    connection.execute(
                        "UPDATE upload_sessions SET received_bytes = ?, updated_at = ? WHERE id = ?",
                        (actual_size, utc_text(), record.id),
                    )
                return self.get_resumable_upload(upload_id, user_id)
        return record

    def append_resumable_upload(
        self,
        upload_id: str,
        user_id: str,
        offset: int,
        payload: bytes,
    ) -> ResumableUploadRecord:
        self._require_server_writable()
        if not payload:
            raise ValueError("upload chunk cannot be empty")
        with self._resumable_lock(upload_id):
            record = self.get_resumable_upload(upload_id, user_id)
            if record.status != "active":
                raise ConflictError("upload session is already completed")
            if offset != record.received_bytes:
                raise ConflictError(f"upload offset mismatch; expected {record.received_bytes}")
            if offset + len(payload) > record.expected_size:
                raise StorageCapacityError("upload chunk exceeds declared file size")
            path = self._resumable_path(record)
            with path.open("r+b") as handle:
                handle.seek(0, os.SEEK_END)
                if handle.tell() != offset:
                    raise ConflictError("partial upload size changed unexpectedly")
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            now = utc_now()
            with self.database.transaction() as connection:
                updated = connection.execute(
                    """
                    UPDATE upload_sessions
                    SET received_bytes = ?, updated_at = ?, expires_at = ?
                    WHERE id = ? AND user_id = ? AND status = 'active' AND received_bytes = ?
                    """,
                    (
                        offset + len(payload),
                        utc_text(now),
                        utc_text(now + timedelta(hours=24)),
                        upload_id,
                        user_id,
                        offset,
                    ),
                )
                if updated.rowcount != 1:
                    raise ConflictError("upload session changed concurrently")
                connection.execute(
                    "UPDATE transfer_jobs SET network_bytes = ?, updated_at = ? WHERE id = ?",
                    (offset + len(payload), utc_text(now), upload_id),
                )
        return self.get_resumable_upload(upload_id, user_id)

    def complete_resumable_upload(
        self,
        upload_id: str,
        user_id: str,
    ) -> FileRecord:
        self._require_server_writable()
        with self._resumable_lock(upload_id):
            record = self.get_resumable_upload(upload_id, user_id)
            if record.status == "completed":
                result = self.find_file(record.space_id, record.logical_path)
                if result is None or result.id != record.result_file_id:
                    raise ConflictError("completed upload metadata is unavailable")
                return result
            if record.received_bytes != record.expected_size:
                raise ConflictError(
                    f"upload is incomplete; expected {record.expected_size}, "
                    f"received {record.received_bytes}"
                )
            temporary_path = self._resumable_path(record)
            digest = self._file_sha256(temporary_path)
            if record.expected_sha256 and digest != record.expected_sha256:
                self._fail_transfer(upload_id, "SHA-256 принятого файла не совпадает")
                raise ConflictError("uploaded file SHA-256 does not match")
            try:
                self._prepare_inbound_move(
                    upload_id,
                    total_bytes=record.expected_size,
                    sha256=digest,
                )
                result = self._finalize_inbound_transfer(upload_id)
            except Exception as exc:
                self._fail_transfer(upload_id, str(exc))
                raise
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE upload_sessions
                    SET status = 'completed', result_file_id = ?, updated_at = ?, expires_at = ?
                    WHERE id = ?
                    """,
                    (
                        result.id,
                        utc_text(),
                        utc_text(utc_now() + timedelta(hours=24)),
                        upload_id,
                    ),
                )
        return result

    def cancel_resumable_upload(self, upload_id: str, user_id: str) -> bool:
        with self._resumable_lock(upload_id):
            with self.database.connection() as connection:
                row = connection.execute(
                    "SELECT * FROM upload_sessions WHERE id = ? AND user_id = ?",
                    (upload_id, user_id),
                ).fetchone()
            if row is None:
                return False
            record = self._resumable_upload(row)
            if record.status == "active":
                try:
                    self._resumable_path(record).unlink(missing_ok=True)
                except NotFoundError:
                    pass
                self._cancel_transfer(upload_id, "Загрузка отменена клиентом")
            with self.database.transaction() as connection:
                connection.execute("DELETE FROM upload_sessions WHERE id = ?", (upload_id,))
        return True

    def cleanup_expired_uploads(self) -> int:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT id, user_id FROM upload_sessions WHERE expires_at < ?",
                (utc_text(),),
            ).fetchall()
        cleaned = 0
        for row in rows:
            if self.cancel_resumable_upload(row["id"], row["user_id"]):
                cleaned += 1
        return cleaned

    def cleanup_root_temporary(self, root_id: str) -> dict[str, int]:
        """Remove only expired sessions and old unreferenced staging files."""

        root = next((item for item in self.list_roots() if item.id == root_id), None)
        if root is None:
            raise NotFoundError("storage root not found")
        expired_sessions = self.cleanup_expired_uploads()
        staging = (root.path / ".staging").resolve()
        staging.mkdir(parents=True, exist_ok=True)
        with self.database.connection() as connection:
            active = {
                str(Path(row["staging_path"]).resolve())
                for row in connection.execute(
                    "SELECT staging_path FROM upload_sessions WHERE status = 'active'"
                ).fetchall()
                if row["staging_path"]
            }
        cutoff = time.time() - 24 * 60 * 60
        removed_files = 0
        removed_bytes = 0
        for candidate in staging.rglob("*"):
            try:
                resolved = candidate.resolve(strict=True)
                if (
                    not resolved.is_file()
                    or staging not in resolved.parents
                    or str(resolved) in active
                    or resolved.stat().st_mtime > cutoff
                ):
                    continue
                size = resolved.stat().st_size
                self._make_staging_file_writable(resolved)
                resolved.unlink()
                removed_files += 1
                removed_bytes += size
            except OSError:
                continue
        self.repository.record_audit(
            actor_type="manager",
            actor_id=None,
            action="storage.root.temporary-cleaned",
            target_type="storage_root",
            target_id=root_id,
            detail=(
                f"Удалено временных файлов: {removed_files}; "
                f"просроченных сессий: {expired_sessions}"
            ),
        )
        return {
            "expired_sessions": expired_sessions,
            "removed_files": removed_files,
            "removed_bytes": removed_bytes,
        }

    def set_transfer_settings(self, *, staging_enabled: bool, staging_path: str) -> dict[str, Any]:
        prepared_path = ""
        if staging_enabled:
            candidate = Path(staging_path).expanduser()
            if not candidate.is_absolute() or candidate.name not in {
                "CloudStorageCache",
                ".cloud-storage-cache",
            }:
                raise InvalidStorageRoot(
                    "staging path must be an absolute CloudStorageCache directory"
                )
            parent = candidate.parent.resolve(strict=True)
            candidate = parent / candidate.name
            self._initialize_staging_root(candidate)
            prepared_path = str(candidate)
        now = utc_text()
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE transfer_settings
                SET staging_enabled = ?, staging_path = ?, updated_at = ?
                WHERE id = 1
                """,
                (int(staging_enabled), prepared_path, now),
            )
            self.repository._audit_tx(
                connection,
                actor_type="manager",
                actor_id=None,
                action="transfers.staging.updated",
                target_type="transfer_settings",
                target_id="1",
                detail=(
                    f"SSD staging включён: {prepared_path}"
                    if staging_enabled
                    else "SSD staging выключен; используется staging целевого диска"
                ),
            )
        return self.transfer_settings()

    def transfer_settings(self) -> dict[str, Any]:
        with self.database.connection() as connection:
            row = connection.execute("SELECT * FROM transfer_settings WHERE id = 1").fetchone()
        enabled = bool(row["staging_enabled"]) if row else False
        path_text = str(row["staging_path"]) if row else ""
        available = False
        total_bytes = 0
        free_bytes = 0
        if enabled and path_text:
            try:
                path = Path(path_text).resolve(strict=True)
                usage = shutil.disk_usage(path)
                available = path.is_dir() and not path.is_symlink()
                total_bytes = usage.total
                free_bytes = usage.free
            except OSError:
                available = False
        return {
            "staging_enabled": enabled,
            "staging_path": path_text,
            "available": available,
            "total_bytes": total_bytes,
            "free_bytes": free_bytes,
            "updated_at": str(row["updated_at"]) if row else "",
        }

    def transfer_overview(self, *, limit: int = 100) -> dict[str, Any]:
        inbound = self.list_transfers("inbound", limit=limit)
        outbound = self.list_transfers("outbound", limit=limit)
        active_statuses = {"receiving", "moving", "sending"}
        return {
            "settings": self.transfer_settings(),
            "inbound": [self.transfer_to_dict(item) for item in inbound],
            "outbound": [self.transfer_to_dict(item) for item in outbound],
            "counts": {
                "inbound_active": sum(item.status in active_statuses for item in inbound),
                "outbound_active": sum(item.status in active_statuses for item in outbound),
                "failed": sum(item.status == "failed" for item in (*inbound, *outbound)),
            },
        }

    def list_transfers(self, direction: str, *, limit: int = 100) -> list[TransferRecord]:
        if direction not in {"inbound", "outbound"}:
            raise ValueError("unknown transfer direction")
        safe_limit = max(1, min(int(limit), 500))
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM transfer_jobs
                WHERE direction = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (direction, safe_limit),
            ).fetchall()
        return [self._transfer(row) for row in rows]

    def get_transfer(self, transfer_id: str) -> TransferRecord:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM transfer_jobs WHERE id = ?", (transfer_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("transfer not found")
        return self._transfer(row)

    def retry_transfer(self, transfer_id: str) -> dict[str, Any]:
        transfer = self.get_transfer(transfer_id)
        if transfer.direction != "inbound" or transfer.status not in {"failed", "moving"}:
            raise ConflictError("only a failed inbound SSD-to-storage move can be retried")
        if transfer.network_bytes != transfer.total_bytes or not transfer.sha256:
            raise ConflictError("the incoming file was not received completely")
        self._finalize_inbound_transfer(transfer_id)
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE upload_sessions
                SET status = 'completed', result_file_id = ?, updated_at = ?, expires_at = ?
                WHERE id = ?
                """,
                (
                    transfer.file_id,
                    utc_text(),
                    utc_text(utc_now() + timedelta(hours=24)),
                    transfer_id,
                ),
            )
        return self.transfer_to_dict(self.get_transfer(transfer_id))

    def queue_transfer_retry(self, transfer_id: str) -> dict[str, Any]:
        transfer = self.get_transfer(transfer_id)
        if transfer.direction != "inbound" or transfer.status != "failed":
            raise ConflictError("only a failed inbound SSD-to-storage move can be retried")
        if transfer.network_bytes != transfer.total_bytes or not transfer.sha256:
            raise ConflictError("the incoming file was not received completely")
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE transfer_jobs SET status = 'moving', storage_bytes = 0,
                    error = '', updated_at = ? WHERE id = ? AND status = 'failed'
                """,
                (utc_text(), transfer_id),
            )
        return self.transfer_to_dict(self.get_transfer(transfer_id))

    def recover_interrupted_transfers(self) -> None:
        now = utc_text()
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE transfer_jobs
                SET status = 'failed', error = 'Core перезапущен во время отправки; клиент может скачать файл повторно',
                    updated_at = ?
                WHERE status = 'sending'
                """,
                (now,),
            )
            connection.execute(
                """
                UPDATE transfer_jobs
                SET status = 'failed', error = 'Core перезапущен во время переноса с SSD; нажмите «Повторить перенос»',
                    updated_at = ?
                WHERE status = 'moving'
                """,
                (now,),
            )
            connection.execute(
                """
                UPDATE transfer_jobs
                SET status = 'failed', error = 'Непотоковый приём прерван; отправьте файл повторно',
                    updated_at = ?
                WHERE status = 'receiving'
                  AND id NOT IN (
                      SELECT id FROM upload_sessions WHERE status = 'active'
                  )
                """,
                (now,),
            )

    def stream_download(
        self,
        record: FileRecord,
        physical_path: Path,
        user_id: str,
    ) -> tuple[str, Iterator[bytes]]:
        transfer_id = str(uuid.uuid4())
        now = utc_text()
        with self.database.transaction() as connection:
            self._insert_transfer_tx(
                connection,
                transfer_id=transfer_id,
                direction="outbound",
                status="sending",
                file_id=record.id,
                space_id=record.space_id,
                user_id=user_id,
                logical_path=record.logical_path,
                content_type=record.content_type,
                total_bytes=record.size_bytes,
                staging_path=Path(),
                temporary_path=Path(),
                storage_root_id=record.storage_root_id,
                object_path=Path(record.object_path),
                now=now,
            )

        def iterator() -> Iterator[bytes]:
            sent = 0
            try:
                with physical_path.open("rb") as handle:
                    while chunk := handle.read(4 * 1024 * 1024):
                        sent += len(chunk)
                        self._update_transfer_network(transfer_id, sent)
                        yield chunk
                with self.database.transaction() as connection:
                    connection.execute(
                        """
                        UPDATE transfer_jobs
                        SET status = 'completed', network_bytes = total_bytes,
                            error = '', updated_at = ?, completed_at = ?
                        WHERE id = ?
                        """,
                        (utc_text(), utc_text(), transfer_id),
                    )
            except BaseException as exc:
                self._fail_transfer(
                    transfer_id,
                    str(exc) or "Отправка прервана клиентом",
                )
                raise

        return transfer_id, iterator()

    def _select_staging_path(
        self,
        expected_size: int,
        destination_root: StorageRootRecord,
    ) -> Path:
        settings = self.transfer_settings()
        if not settings["staging_enabled"] or not settings["available"]:
            return destination_root.path
        candidate = Path(str(settings["staging_path"]))
        try:
            resolved = candidate.resolve(strict=True)
            usage = shutil.disk_usage(resolved)
        except OSError:
            return destination_root.path
        reserve = max(1024**3, int(usage.total * 0.02))
        if usage.free - max(0, expected_size) < reserve:
            return destination_root.path
        return resolved

    @staticmethod
    def _initialize_staging_root(path: Path) -> None:
        marker = path / ".cloud-storage-cache.json"
        if path.exists() and not path.is_dir():
            raise InvalidStorageRoot("staging path is not a directory")
        if path.exists() and not marker.exists() and any(path.iterdir()):
            raise InvalidStorageRoot(
                "refusing to adopt a non-empty staging directory without a marker"
            )
        path.mkdir(parents=False, exist_ok=True)
        if marker.exists():
            try:
                value = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise InvalidStorageRoot("staging marker is damaged") from exc
            if value.get("schema_version") != 1:
                raise InvalidStorageRoot("staging marker version is unsupported")
        else:
            temporary = marker.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(
                    {"schema_version": 1, "created_at": utc_text()},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            os.replace(temporary, marker)
        (path / "incoming").mkdir(exist_ok=True)
        if os.name != "nt":
            path.chmod(0o700)

    def _create_inbound_transfer(
        self,
        *,
        transfer_id: str,
        file_id: str,
        space_id: str,
        user_id: str,
        logical_path: str,
        content_type: str,
        total_bytes: int,
        staging_path: Path,
        temporary_path: Path,
        storage_root_id: str,
        object_path: Path,
    ) -> None:
        with self.database.transaction() as connection:
            self._insert_transfer_tx(
                connection,
                transfer_id=transfer_id,
                direction="inbound",
                status="receiving",
                file_id=file_id,
                space_id=space_id,
                user_id=user_id,
                logical_path=logical_path,
                content_type=content_type,
                total_bytes=total_bytes,
                staging_path=staging_path,
                temporary_path=temporary_path,
                storage_root_id=storage_root_id,
                object_path=object_path,
                now=utc_text(),
            )

    @staticmethod
    def _insert_transfer_tx(
        connection: sqlite3.Connection,
        *,
        transfer_id: str,
        direction: str,
        status: str,
        file_id: str,
        space_id: str,
        user_id: str,
        logical_path: str,
        content_type: str,
        total_bytes: int,
        staging_path: Path,
        temporary_path: Path,
        storage_root_id: str,
        object_path: Path,
        now: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO transfer_jobs(
                id, direction, status, file_id, space_id, user_id, logical_path,
                content_type, total_bytes, network_bytes, storage_bytes, sha256,
                staging_path, temporary_path, storage_root_id, object_path,
                error, created_at, updated_at, completed_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, '', ?, ?, ?, ?, '', ?, ?, NULL)
            """,
            (
                transfer_id,
                direction,
                status,
                file_id,
                space_id,
                user_id,
                logical_path,
                content_type,
                max(0, total_bytes),
                str(staging_path) if str(staging_path) != "." else "",
                temporary_path.as_posix() if str(temporary_path) != "." else "",
                storage_root_id,
                object_path.as_posix() if str(object_path) != "." else "",
                now,
                now,
            ),
        )

    def _update_transfer_network(self, transfer_id: str, transferred: int) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE transfer_jobs
                SET network_bytes = ?, updated_at = ?
                WHERE id = ? AND status IN ('receiving', 'sending')
                """,
                (max(0, transferred), utc_text(), transfer_id),
            )

    def _prepare_inbound_move(
        self,
        transfer_id: str,
        *,
        total_bytes: int,
        sha256: str,
    ) -> None:
        with self.database.transaction() as connection:
            updated = connection.execute(
                """
                UPDATE transfer_jobs
                SET status = 'moving', total_bytes = ?, network_bytes = ?,
                    storage_bytes = 0, sha256 = ?, error = '', updated_at = ?
                WHERE id = ? AND direction = 'inbound'
                  AND status IN ('receiving', 'failed', 'moving')
                """,
                (total_bytes, total_bytes, sha256, utc_text(), transfer_id),
            )
            if updated.rowcount != 1:
                raise ConflictError("incoming transfer state changed concurrently")

    def _finalize_inbound_transfer(self, transfer_id: str) -> FileRecord:
        with self._transfer_lock(transfer_id):
            transfer = self.get_transfer(transfer_id)
            if transfer.direction != "inbound" or transfer.status not in {"moving", "failed"}:
                raise ConflictError("incoming transfer is not ready for storage")
            if transfer.network_bytes != transfer.total_bytes or not transfer.sha256:
                raise ConflictError("incoming transfer is incomplete")
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE transfer_jobs
                    SET status = 'moving', storage_bytes = 0, error = '', updated_at = ?
                    WHERE id = ?
                    """,
                    (utc_text(), transfer_id),
                )
            source = self._transfer_staging_file(transfer)
            root = self._root_by_id(transfer.storage_root_id)
            object_relative = Path(transfer.object_path)
            final_path = (root.path / object_relative).resolve()
            if root.path not in final_path.parents:
                raise PermissionDeniedError("destination failed containment validation")
            final_path.parent.mkdir(parents=True, exist_ok=True)
            same_root = Path(transfer.staging_path).resolve() == root.path.resolve()
            destination_staging = root.path / ".staging" / f"{transfer.file_id}.incoming"
            destination_staging.unlink(missing_ok=True)
            try:
                if same_root:
                    os.replace(source, final_path)
                    self._update_transfer_storage(transfer_id, transfer.total_bytes)
                else:
                    digest = hashlib.sha256()
                    copied = 0
                    with source.open("rb") as reader, destination_staging.open("xb") as writer:
                        while chunk := reader.read(4 * 1024 * 1024):
                            writer.write(chunk)
                            digest.update(chunk)
                            copied += len(chunk)
                            self._update_transfer_storage(transfer_id, copied)
                        writer.flush()
                        os.fsync(writer.fileno())
                    if copied != transfer.total_bytes or digest.hexdigest() != transfer.sha256:
                        raise ConflictError("SSD-to-storage verification failed")
                    os.replace(destination_staging, final_path)
                make_managed_file_inert(final_path, root.path)
                result = self._commit_upload(
                    file_id=transfer.file_id,
                    root=root,
                    space_id=transfer.space_id,
                    user_id=transfer.user_id,
                    logical_path=transfer.logical_path,
                    object_path=transfer.object_path,
                    size_bytes=transfer.total_bytes,
                    sha256=transfer.sha256,
                    content_type=transfer.content_type,
                )
            except Exception as exc:
                destination_staging.unlink(missing_ok=True)
                if final_path.exists():
                    self._make_staging_file_writable(final_path)
                    if same_root and not source.exists():
                        os.replace(final_path, source)
                    else:
                        final_path.unlink(missing_ok=True)
                self._fail_transfer(transfer_id, str(exc))
                raise
            if not same_root:
                source.unlink(missing_ok=True)
            now = utc_text()
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE transfer_jobs
                    SET status = 'completed', network_bytes = total_bytes,
                        storage_bytes = total_bytes, error = '', updated_at = ?, completed_at = ?
                    WHERE id = ?
                    """,
                    (now, now, transfer_id),
                )
            return result

    def _transfer_staging_file(self, transfer: TransferRecord) -> Path:
        try:
            root = Path(transfer.staging_path).resolve(strict=True)
            path = (root / transfer.temporary_path).resolve(strict=True)
        except OSError as exc:
            raise NotFoundError("staged upload data is unavailable") from exc
        if root not in path.parents or not path.is_file() or path.is_symlink():
            raise PermissionDeniedError("staged upload failed containment validation")
        return path

    def _update_transfer_storage(self, transfer_id: str, transferred: int) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE transfer_jobs SET storage_bytes = ?, updated_at = ?
                WHERE id = ? AND status = 'moving'
                """,
                (max(0, transferred), utc_text(), transfer_id),
            )

    def _fail_transfer(self, transfer_id: str, error: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE transfer_jobs SET status = 'failed', error = ?, updated_at = ?
                WHERE id = ? AND status != 'completed'
                """,
                ((error or "Неизвестная ошибка")[:1000], utc_text(), transfer_id),
            )

    def _cancel_transfer(self, transfer_id: str, reason: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE transfer_jobs SET status = 'cancelled', error = ?,
                    updated_at = ?, completed_at = ?
                WHERE id = ? AND status NOT IN ('completed', 'cancelled')
                """,
                (reason[:1000], utc_text(), utc_text(), transfer_id),
            )

    def _transfer_lock(self, transfer_id: str) -> threading.RLock:
        with self._transfer_locks_guard:
            return self._transfer_locks.setdefault(transfer_id, threading.RLock())

    def _require_server_writable(self) -> None:
        with self.database.connection() as connection:
            row = connection.execute("SELECT mode FROM server_state WHERE id = 1").fetchone()
        if row is not None and row["mode"] != "normal":
            raise ConflictError("server is in emergency read-only mode")

    def _select_root(
        self,
        expected_size: int,
        space: Any | None = None,
    ) -> StorageRootRecord:
        candidates: list[tuple[float, StorageRootRecord]] = []
        roots = self.list_roots()
        preferred_ids = []
        if space is not None:
            preferred_ids = [
                value
                for value in (
                    space.primary_storage_root_id,
                    space.fallback_storage_root_id,
                )
                if value
            ]
        ordered_roots = (
            [root for root_id in preferred_ids for root in roots if root.id == root_id]
            if preferred_ids
            else roots
        )
        for preference, root in enumerate(ordered_roots):
            if not root.write_enabled or root.purpose != "primary":
                continue
            try:
                usage = shutil.disk_usage(root.path)
            except OSError:
                continue
            projected_used = usage.used + expected_size
            projected_percent = projected_used / usage.total * 100 if usage.total else 100
            free_after = usage.free - expected_size
            if free_after < root.min_free_bytes or projected_percent > root.max_fill_percent:
                continue
            score = (
                (10_000 if preferred_ids and preference == 0 else 5_000 if preferred_ids else 0)
                + root.priority * 10
                + (free_after / max(1, usage.total)) * 100
            )
            candidates.append((score, root))
        if not candidates:
            raise StorageCapacityError("no configured storage root has enough safe free space")
        return max(candidates, key=lambda item: item[0])[1]

    def list_roots(self) -> list[StorageRootRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT id, disk_id, path, write_enabled, purpose, priority,
                       max_fill_percent, min_free_bytes
                FROM storage_roots WHERE enabled = 1
                ORDER BY priority DESC
                """
            ).fetchall()
        result = []
        for row in rows:
            path = Path(row["path"]).resolve()
            if path.is_dir():
                result.append(
                    StorageRootRecord(
                        id=row["id"],
                        disk_id=row["disk_id"],
                        path=path,
                        write_enabled=bool(row["write_enabled"]),
                        purpose=row["purpose"],
                        priority=row["priority"],
                        max_fill_percent=row["max_fill_percent"],
                        min_free_bytes=row["min_free_bytes"],
                    )
                )
        return result

    def space_usage(self, space_id: str) -> int:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(sum(size_bytes), 0) AS total
                FROM files WHERE space_id = ? AND deleted_at IS NULL
                """,
                (space_id,),
            ).fetchone()
        return int(row["total"])

    def find_file(
        self, space_id: str, logical_path: str, include_deleted: bool = False
    ) -> FileRecord | None:
        logical_path = normalize_logical_path(logical_path)
        deleted_clause = "" if include_deleted else " AND deleted_at IS NULL"
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM files WHERE space_id = ? AND logical_path = ?" + deleted_clause,
                (space_id, logical_path),
            ).fetchone()
        return self._file(row) if row else None

    def list_entries(
        self, space_id: str, user_id: str, directory: str = ""
    ) -> list[dict[str, Any]]:
        self.repository.require_space_permission(space_id, user_id, capability="read")
        directory = directory.strip("/")
        if directory:
            directory = normalize_logical_path(directory)
            prefix = directory + "/"
        else:
            prefix = ""
        with self.database.connection() as connection:
            directory_rows = connection.execute(
                """
                SELECT logical_path, modified_at
                FROM directories
                WHERE space_id = ? AND logical_path LIKE ? ESCAPE '\\'
                ORDER BY logical_path
                """,
                (space_id, self._like_prefix(prefix) + "%"),
            ).fetchall()
            rows = connection.execute(
                """
                SELECT id, logical_path, size_bytes, sha256, content_type, version, modified_at
                FROM files
                WHERE space_id = ? AND deleted_at IS NULL AND logical_path LIKE ? ESCAPE '\\'
                ORDER BY logical_path
                """,
                (space_id, self._like_prefix(prefix) + "%"),
            ).fetchall()
        entries: dict[str, dict[str, Any]] = {}
        for row in directory_rows:
            remainder = row["logical_path"][len(prefix) :]
            name, separator, _ = remainder.partition("/")
            if not name:
                continue
            item = entries.setdefault(name, {"name": name, "type": "directory"})
            if not separator:
                item["modified_at"] = row["modified_at"]
        for row in rows:
            remainder = row["logical_path"][len(prefix) :]
            name, separator, _ = remainder.partition("/")
            if separator:
                entries.setdefault(name, {"name": name, "type": "directory"})
            else:
                entries[name] = {
                    "id": row["id"],
                    "name": name,
                    "type": "file",
                    "size_bytes": row["size_bytes"],
                    "sha256": row["sha256"],
                    "content_type": row["content_type"],
                    "version": row["version"],
                    "modified_at": row["modified_at"],
                }
        return list(entries.values())

    def search_entries(
        self,
        space_id: str,
        user_id: str,
        query: str,
        *,
        directory: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        self.repository.require_space_permission(space_id, user_id, capability="read")
        query = query.strip()
        if len(query) < 2 or len(query) > 200:
            raise ValueError("search query must contain 2-200 characters")
        directory = directory.strip("/")
        prefix = normalize_logical_path(directory) + "/" if directory else ""
        escaped_query = self._like_prefix(query)
        pattern = self._like_prefix(prefix) + "%" + escaped_query + "%"
        safe_limit = max(1, min(limit, 200))
        with self.database.connection() as connection:
            directories = connection.execute(
                """
                SELECT logical_path, modified_at FROM directories
                WHERE space_id = ? AND logical_path LIKE ? ESCAPE '\\' COLLATE NOCASE
                ORDER BY modified_at DESC LIMIT ?
                """,
                (space_id, pattern, safe_limit),
            ).fetchall()
            remaining = max(0, safe_limit - len(directories))
            files = connection.execute(
                """
                SELECT id, logical_path, size_bytes, sha256, content_type, version, modified_at
                FROM files
                WHERE space_id = ? AND deleted_at IS NULL
                  AND logical_path LIKE ? ESCAPE '\\' COLLATE NOCASE
                ORDER BY modified_at DESC LIMIT ?
                """,
                (space_id, pattern, remaining),
            ).fetchall()
        result = [
            {
                "name": PurePosixPath(row["logical_path"]).name,
                "logical_path": row["logical_path"],
                "type": "directory",
                "modified_at": row["modified_at"],
            }
            for row in directories
        ]
        result.extend(
            {
                "id": row["id"],
                "name": PurePosixPath(row["logical_path"]).name,
                "logical_path": row["logical_path"],
                "type": "file",
                "size_bytes": row["size_bytes"],
                "sha256": row["sha256"],
                "content_type": row["content_type"],
                "version": row["version"],
                "modified_at": row["modified_at"],
            }
            for row in files
        )
        result.sort(key=lambda item: str(item["modified_at"]), reverse=True)
        return result[:safe_limit]

    def create_directory(self, space_id: str, user_id: str, logical_path: str) -> dict[str, Any]:
        self._require_server_writable()
        self.repository.require_space_permission(space_id, user_id, capability="upload")
        logical_path = normalize_logical_path(logical_path)
        parts = PurePosixPath(logical_path).parts
        prefixes = ["/".join(parts[:index]) for index in range(1, len(parts) + 1)]
        now = utc_text()
        with self.database.transaction() as connection:
            placeholders = ",".join("?" for _ in prefixes)
            file_collision = connection.execute(
                "SELECT logical_path FROM files "
                f"WHERE space_id = ? AND deleted_at IS NULL AND logical_path IN ({placeholders}) "
                "LIMIT 1",
                (space_id, *prefixes),
            ).fetchone()
            if file_collision is not None:
                raise ConflictError("a file already occupies part of this directory path")
            for index in range(1, len(parts) + 1):
                current = "/".join(parts[:index])
                connection.execute(
                    """
                    INSERT INTO directories(space_id, logical_path, created_by, created_at, modified_at)
                    VALUES(?, ?, ?, ?, ?)
                    ON CONFLICT(space_id, logical_path) DO NOTHING
                    """,
                    (space_id, current, user_id, now, now),
                )
        self.repository.record_audit(
            actor_type="user",
            actor_id=user_id,
            action="directory.created",
            target_type="directory",
            target_id=f"{space_id}:{logical_path}",
            detail=f"Создан каталог: {logical_path}",
        )
        return {"path": logical_path, "type": "directory", "modified_at": now}

    def delete_directory(self, space_id: str, user_id: str, logical_path: str) -> bool:
        self._require_server_writable()
        self.repository.require_space_permission(space_id, user_id, capability="delete")
        logical_path = normalize_logical_path(logical_path)
        prefix = logical_path + "/"
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT 1 FROM directories WHERE space_id = ? AND logical_path = ?",
                (space_id, logical_path),
            ).fetchone()
            if existing is None:
                raise NotFoundError("directory not found")
            child = connection.execute(
                """
                SELECT 1 FROM directories
                WHERE space_id = ? AND logical_path LIKE ? ESCAPE '\\' LIMIT 1
                """,
                (space_id, self._like_prefix(prefix) + "%"),
            ).fetchone()
            file_child = connection.execute(
                """
                SELECT 1 FROM files
                WHERE space_id = ? AND deleted_at IS NULL
                  AND logical_path LIKE ? ESCAPE '\\' LIMIT 1
                """,
                (space_id, self._like_prefix(prefix) + "%"),
            ).fetchone()
            if child is not None or file_child is not None:
                raise ConflictError("directory is not empty")
            connection.execute(
                "DELETE FROM directories WHERE space_id = ? AND logical_path = ?",
                (space_id, logical_path),
            )
        self.repository.record_audit(
            actor_type="user",
            actor_id=user_id,
            action="directory.deleted",
            target_type="directory",
            target_id=f"{space_id}:{logical_path}",
            detail=f"Удалён пустой каталог: {logical_path}",
        )
        return True

    def move_entry(
        self,
        space_id: str,
        user_id: str,
        source_path: str,
        destination_path: str,
        kind: str,
    ) -> dict[str, Any]:
        self._require_server_writable()
        self.repository.require_space_permission(space_id, user_id, capability="modify")
        source_path = normalize_logical_path(source_path)
        destination_path = normalize_logical_path(destination_path)
        if source_path == destination_path:
            return {"source_path": source_path, "destination_path": destination_path, "type": kind}
        if kind not in {"file", "directory"}:
            raise ValueError("entry type must be file or directory")
        if kind == "directory" and destination_path.startswith(source_path + "/"):
            raise ConflictError("directory cannot be moved into itself")
        now = utc_text()
        with self.database.transaction() as connection:
            self._require_destination_available(connection, space_id, destination_path)
            if kind == "file":
                changed = connection.execute(
                    """
                    UPDATE files SET logical_path = ?, modified_at = ?
                    WHERE space_id = ? AND logical_path = ? AND deleted_at IS NULL
                    """,
                    (destination_path, now, space_id, source_path),
                )
                if changed.rowcount != 1:
                    raise NotFoundError("file not found")
            else:
                directory = connection.execute(
                    "SELECT 1 FROM directories WHERE space_id = ? AND logical_path = ?",
                    (space_id, source_path),
                ).fetchone()
                if directory is None:
                    raise NotFoundError("directory not found")
                source_prefix = source_path + "/"
                destination_prefix = destination_path + "/"
                nested_directories = connection.execute(
                    """
                    SELECT logical_path FROM directories
                    WHERE space_id = ? AND logical_path LIKE ? ESCAPE '\\'
                    """,
                    (space_id, self._like_prefix(source_prefix) + "%"),
                ).fetchall()
                nested_files = connection.execute(
                    """
                    SELECT logical_path FROM files
                    WHERE space_id = ? AND deleted_at IS NULL
                      AND logical_path LIKE ? ESCAPE '\\'
                    """,
                    (space_id, self._like_prefix(source_prefix) + "%"),
                ).fetchall()
                destinations = [
                    destination_prefix + row["logical_path"][len(source_prefix) :]
                    for row in [*nested_directories, *nested_files]
                ]
                for target in destinations:
                    self._require_destination_available(connection, space_id, target)
                connection.execute(
                    "UPDATE directories SET logical_path = ?, modified_at = ? "
                    "WHERE space_id = ? AND logical_path = ?",
                    (destination_path, now, space_id, source_path),
                )
                for row in sorted(nested_directories, key=lambda item: len(item["logical_path"])):
                    target = destination_prefix + row["logical_path"][len(source_prefix) :]
                    connection.execute(
                        "UPDATE directories SET logical_path = ?, modified_at = ? "
                        "WHERE space_id = ? AND logical_path = ?",
                        (target, now, space_id, row["logical_path"]),
                    )
                for row in nested_files:
                    target = destination_prefix + row["logical_path"][len(source_prefix) :]
                    connection.execute(
                        "UPDATE files SET logical_path = ?, modified_at = ? "
                        "WHERE space_id = ? AND logical_path = ? AND deleted_at IS NULL",
                        (target, now, space_id, row["logical_path"]),
                    )
        self.repository.record_audit(
            actor_type="user",
            actor_id=user_id,
            action=f"{kind}.moved",
            target_type=kind,
            target_id=f"{space_id}:{destination_path}",
            detail=f"Перемещено: {source_path} → {destination_path}",
        )
        return {"source_path": source_path, "destination_path": destination_path, "type": kind}

    def create_public_share(
        self,
        space_id: str,
        user_id: str,
        logical_path: str,
        kind: str,
        *,
        ttl_hours: int = 24,
    ) -> PublicShareRecord:
        self.repository.require_space_permission(space_id, user_id, capability="share")
        logical_path = normalize_logical_path(logical_path)
        if kind not in {"file", "directory"}:
            raise ValueError("share type must be file or directory")
        with self.database.connection() as connection:
            if kind == "file":
                exists = connection.execute(
                    "SELECT 1 FROM files WHERE space_id = ? AND logical_path = ? "
                    "AND deleted_at IS NULL",
                    (space_id, logical_path),
                ).fetchone()
            else:
                exists = connection.execute(
                    "SELECT 1 FROM directories WHERE space_id = ? AND logical_path = ?",
                    (space_id, logical_path),
                ).fetchone()
        if exists is None:
            raise NotFoundError(f"{kind} not found")
        share_id = str(uuid.uuid4())
        token = "csh_" + secrets.token_urlsafe(40)
        token_hash = self.repository.credentials.fingerprint(token, "public-share")
        created_at = utc_text()
        expires_at = (utc_now() + timedelta(hours=max(1, min(ttl_hours, 24 * 30)))).isoformat(
            timespec="seconds"
        )
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO file_shares(
                    id, owner_user_id, space_id, logical_path, kind, token_hash,
                    expires_at, created_at, revoked_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    share_id,
                    user_id,
                    space_id,
                    logical_path,
                    kind,
                    token_hash,
                    expires_at,
                    created_at,
                ),
            )
            self.repository._audit_tx(
                connection,
                actor_type="user",
                actor_id=user_id,
                action="share.created",
                target_type=kind,
                target_id=f"{space_id}:{logical_path}",
                detail=f"Создана ссылка общего доступа на {ttl_hours} ч.",
            )
        return PublicShareRecord(
            id=share_id,
            owner_user_id=user_id,
            space_id=space_id,
            logical_path=logical_path,
            kind=kind,
            token=token,
            expires_at=expires_at,
            created_at=created_at,
            revoked_at=None,
        )

    def list_public_shares(self, user_id: str) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT id, space_id, logical_path, kind, expires_at, created_at, revoked_at
                FROM file_shares WHERE owner_user_id = ? ORDER BY created_at DESC LIMIT 200
                """,
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def revoke_public_share(self, share_id: str, user_id: str) -> bool:
        revoked_at = utc_text()
        with self.database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE file_shares SET revoked_at = ?
                WHERE id = ? AND owner_user_id = ? AND revoked_at IS NULL
                """,
                (revoked_at, share_id, user_id),
            )
            if changed.rowcount != 1:
                raise NotFoundError("share not found")
            self.repository._audit_tx(
                connection,
                actor_type="user",
                actor_id=user_id,
                action="share.revoked",
                target_type="share",
                target_id=share_id,
                detail="Ссылка общего доступа отозвана",
            )
        return True

    def resolve_public_share(self, token: str) -> PublicShareRecord:
        if not token.startswith("csh_") or not 40 <= len(token) <= 256:
            raise NotFoundError("share not found")
        token_hash = self.repository.credentials.fingerprint(token, "public-share")
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT id, owner_user_id, space_id, logical_path, kind,
                       expires_at, created_at, revoked_at
                FROM file_shares
                WHERE token_hash = ? AND revoked_at IS NULL AND expires_at > ?
                """,
                (token_hash, utc_text()),
            ).fetchone()
        if row is None:
            raise NotFoundError("share not found")
        return PublicShareRecord(token=token, **dict(row))

    def public_share_overview(self, token: str) -> dict[str, Any]:
        share = self.resolve_public_share(token)
        result: dict[str, Any] = {
            "name": PurePosixPath(share.logical_path).name,
            "type": share.kind,
            "expires_at": share.expires_at,
        }
        if share.kind == "file":
            record = self.find_file(share.space_id, share.logical_path)
            if record is None:
                raise NotFoundError("shared file not found")
            result.update(
                size_bytes=record.size_bytes,
                content_type=record.content_type,
                sha256=record.sha256,
            )
        else:
            result["entries"] = self.list_entries(
                share.space_id,
                share.owner_user_id,
                share.logical_path,
            )
        return result

    def resolve_public_share_download(
        self, token: str, relative_path: str = ""
    ) -> tuple[PublicShareRecord, FileRecord, Path]:
        share = self.resolve_public_share(token)
        if share.kind == "file":
            if relative_path:
                raise NotFoundError("shared file not found")
            logical_path = share.logical_path
        else:
            relative_path = normalize_logical_path(relative_path)
            logical_path = f"{share.logical_path}/{relative_path}"
        record, physical_path = self.resolve_download(
            share.space_id,
            share.owner_user_id,
            logical_path,
        )
        return share, record, physical_path

    def mobile_storage_overview(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for root in self.list_roots():
            try:
                usage = shutil.disk_usage(root.path)
                total_bytes, used_bytes, free_bytes = usage.total, usage.used, usage.free
                available = True
            except OSError:
                total_bytes = used_bytes = free_bytes = 0
                available = False
            result.append(
                {
                    "id": root.id,
                    "purpose": root.purpose,
                    "write_enabled": root.write_enabled,
                    "available": available,
                    "total_bytes": total_bytes,
                    "used_bytes": used_bytes,
                    "free_bytes": free_bytes,
                    "temperature_c": None,
                    "status": (
                        "unavailable"
                        if not available
                        else "write_paused"
                        if not root.write_enabled
                        else "healthy"
                    ),
                }
            )
        return result

    def set_root_write_enabled(
        self, root_id: str, enabled: bool, *, actor_user_id: str
    ) -> dict[str, Any]:
        with self.database.transaction() as connection:
            changed = connection.execute(
                "UPDATE storage_roots SET write_enabled = ? WHERE id = ? AND enabled = 1",
                (int(enabled), root_id),
            )
            if changed.rowcount != 1:
                raise NotFoundError("storage root not found")
            self.repository._audit_tx(
                connection,
                actor_type="device",
                actor_id=actor_user_id,
                action="storage.write.resumed" if enabled else "storage.write.paused",
                target_type="storage_root",
                target_id=root_id,
                detail=(
                    "Запись на накопитель возобновлена с Mobile Server Control"
                    if enabled
                    else "Новые записи на накопитель приостановлены с Mobile Server Control"
                ),
            )
        return next(item for item in self.mobile_storage_overview() if item["id"] == root_id)

    def _require_file_path_available(self, space_id: str, logical_path: str) -> None:
        parts = PurePosixPath(logical_path).parts
        parents = ["/".join(parts[:index]) for index in range(1, len(parts))]
        with self.database.connection() as connection:
            collision = connection.execute(
                "SELECT 1 FROM directories WHERE space_id = ? AND logical_path = ?",
                (space_id, logical_path),
            ).fetchone()
            file_parent = None
            if parents:
                placeholders = ",".join("?" for _ in parents)
                file_parent = connection.execute(
                    "SELECT 1 FROM files "
                    f"WHERE space_id = ? AND deleted_at IS NULL "
                    f"AND logical_path IN ({placeholders}) LIMIT 1",
                    (space_id, *parents),
                ).fetchone()
        if collision is not None:
            raise ConflictError("a directory already exists at this path")
        if file_parent is not None:
            raise ConflictError("a file already occupies part of this path")

    @staticmethod
    def _require_destination_available(
        connection: sqlite3.Connection, space_id: str, logical_path: str
    ) -> None:
        parts = PurePosixPath(logical_path).parts
        parents = ["/".join(parts[:index]) for index in range(1, len(parts))]
        file_row = connection.execute(
            "SELECT 1 FROM files WHERE space_id = ? AND logical_path = ? AND deleted_at IS NULL",
            (space_id, logical_path),
        ).fetchone()
        directory_row = connection.execute(
            "SELECT 1 FROM directories WHERE space_id = ? AND logical_path = ?",
            (space_id, logical_path),
        ).fetchone()
        file_parent = None
        if parents:
            placeholders = ",".join("?" for _ in parents)
            file_parent = connection.execute(
                "SELECT 1 FROM files "
                f"WHERE space_id = ? AND deleted_at IS NULL "
                f"AND logical_path IN ({placeholders}) LIMIT 1",
                (space_id, *parents),
            ).fetchone()
        if file_row is not None or directory_row is not None:
            raise ConflictError("destination path already exists")
        if file_parent is not None:
            raise ConflictError("a file already occupies part of the destination path")

    def resolve_download(
        self, space_id: str, user_id: str, logical_path: str
    ) -> tuple[FileRecord, Path]:
        self.repository.require_space_permission(space_id, user_id, capability="read")
        record = self.find_file(space_id, logical_path)
        if record is None:
            raise NotFoundError("file not found")
        root = next((item for item in self.list_roots() if item.id == record.storage_root_id), None)
        if root is not None:
            try:
                path = (root.path / record.object_path).resolve(strict=True)
                if (
                    root.path in path.parents
                    and path.is_file()
                    and not path.is_symlink()
                    and path.stat().st_size == record.size_bytes
                ):
                    return record, path
            except OSError:
                pass
        mirror = self._resolve_mirror_download(record)
        if mirror is not None:
            return record, mirror
        raise NotFoundError("physical storage and a current mirror replica are unavailable")

    def soft_delete(self, space_id: str, user_id: str, logical_path: str) -> FileRecord:
        self._require_server_writable()
        self.repository.require_space_permission(space_id, user_id, capability="delete")
        record = self.find_file(space_id, logical_path)
        if record is None:
            raise NotFoundError("file not found")
        root = next((item for item in self.list_roots() if item.id == record.storage_root_id), None)
        if root is None:
            raise NotFoundError("physical storage is unavailable")
        source = (root.path / record.object_path).resolve(strict=True)
        if root.path not in source.parents or not source.is_file():
            raise PermissionDeniedError("stored object failed containment validation")
        trash_relative = (
            Path(".trash") / f"{record.id}.v{record.version}.{uuid.uuid4().hex[:8]}.blob"
        )
        trash_path = root.path / trash_relative
        trash_path.parent.mkdir(exist_ok=True)
        os.replace(source, trash_path)
        deleted_at = utc_text()
        try:
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE files
                    SET object_path = ?, deleted_at = ?, modified_at = ?
                    WHERE id = ?
                    """,
                    (trash_relative.as_posix(), deleted_at, deleted_at, record.id),
                )
                self.repository._audit_tx(
                    connection,
                    actor_type="user",
                    actor_id=user_id,
                    action="file.deleted",
                    target_type="file",
                    target_id=record.id,
                    detail=f"Файл перемещён в корзину: {record.logical_path}",
                )
        except Exception:
            os.replace(trash_path, source)
            raise
        return self.find_file(space_id, logical_path, include_deleted=True)  # type: ignore[return-value]

    def _commit_upload(
        self,
        *,
        file_id: str,
        root: StorageRootRecord,
        space_id: str,
        user_id: str,
        logical_path: str,
        object_path: str,
        size_bytes: int,
        sha256: str,
        content_type: str,
    ) -> FileRecord:
        self._require_server_writable()
        now = utc_text()
        with self.database.transaction() as connection:
            quota = connection.execute(
                "SELECT quota_bytes FROM spaces WHERE id = ?", (space_id,)
            ).fetchone()
            if quota is None:
                raise NotFoundError("space not found")
            existing = connection.execute(
                "SELECT * FROM files WHERE space_id = ? AND logical_path = ?",
                (space_id, logical_path),
            ).fetchone()
            used = connection.execute(
                """
                SELECT COALESCE(sum(size_bytes), 0) AS total
                FROM files WHERE space_id = ? AND deleted_at IS NULL
                """,
                (space_id,),
            ).fetchone()["total"]
            replaced_size = (
                existing["size_bytes"] if existing and existing["deleted_at"] is None else 0
            )
            if used - replaced_size + size_bytes > quota["quota_bytes"]:
                raise StorageCapacityError("personal storage quota is exhausted")
            if existing:
                version = existing["version"] + 1
                connection.execute(
                    """
                    INSERT INTO file_versions(
                        id, file_id, storage_root_id, object_path, size_bytes,
                        sha256, version, archived_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        existing["id"],
                        existing["storage_root_id"],
                        existing["object_path"],
                        existing["size_bytes"],
                        existing["sha256"],
                        existing["version"],
                        now,
                    ),
                )
                connection.execute(
                    """
                    UPDATE files
                    SET storage_root_id = ?, object_path = ?, size_bytes = ?, sha256 = ?,
                        content_type = ?, uploaded_by = ?, version = ?, modified_at = ?,
                        deleted_at = NULL
                    WHERE id = ?
                    """,
                    (
                        root.id,
                        object_path,
                        size_bytes,
                        sha256,
                        content_type,
                        user_id,
                        version,
                        now,
                        existing["id"],
                    ),
                )
                effective_id = existing["id"]
                action = "file.updated"
            else:
                version = 1
                effective_id = file_id
                connection.execute(
                    """
                    INSERT INTO files(
                        id, space_id, logical_path, storage_root_id, object_path,
                        size_bytes, sha256, content_type, uploaded_by, version,
                        created_at, modified_at, deleted_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, NULL)
                    """,
                    (
                        effective_id,
                        space_id,
                        logical_path,
                        root.id,
                        object_path,
                        size_bytes,
                        sha256,
                        content_type,
                        user_id,
                        now,
                        now,
                    ),
                )
                action = "file.created"
            self.repository._audit_tx(
                connection,
                actor_type="user",
                actor_id=user_id,
                action=action,
                target_type="file",
                target_id=effective_id,
                detail=f"Сохранён файл {logical_path}, версия {version}",
            )
        record = self.find_file(space_id, logical_path)
        if record is None:
            raise ConflictError("file metadata commit failed")
        self.sync_file_to_mirrors(record)
        return record

    def create_migration_job(
        self,
        source_root_id: str,
        target_root_id: str,
        space_id: str | None = None,
    ) -> MigrationJobRecord:
        if source_root_id == target_root_id:
            raise InvalidStorageRoot("source and target storage roots must be different")
        source = self._root_by_id(source_root_id)
        target = self._root_by_id(target_root_id)
        if source.purpose != "primary" or target.purpose != "primary":
            raise ConflictError("migration requires primary storage roots")
        if source.write_enabled and not space_id:
            raise ConflictError("pause writes on the source storage before migration")
        if not target.write_enabled:
            raise ConflictError("target storage does not accept writes")
        if space_id:
            space = self.repository.get_space(space_id)
            if space.primary_storage_root_id != target.id:
                raise ConflictError("space migration target must be its primary storage root")
            if space.fallback_storage_root_id != source.id:
                raise ConflictError("space migration source must be its fallback storage root")
        total_files, total_bytes = self._migration_totals(source_root_id, space_id)
        try:
            usage = shutil.disk_usage(target.path)
        except OSError as exc:
            raise StorageCapacityError("target storage is unavailable") from exc
        projected_percent = (usage.used + total_bytes) / usage.total * 100 if usage.total else 100
        if (
            usage.free - total_bytes < target.min_free_bytes
            or projected_percent > target.max_fill_percent
        ):
            raise StorageCapacityError("target storage cannot safely contain the migration")
        job_id = str(uuid.uuid4())
        now = utc_text()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO maintenance_jobs(
                    id, kind, status, source_root_id, target_root_id, space_id,
                    total_files, total_bytes, processed_files, processed_bytes,
                    retained_sources, error, created_at, started_at, completed_at, updated_at
                ) VALUES(?, 'migration', 'queued', ?, ?, ?, ?, ?, 0, 0, 0, '', ?, NULL, NULL, ?)
                """,
                (
                    job_id,
                    source.id,
                    target.id,
                    space_id,
                    total_files,
                    total_bytes,
                    now,
                    now,
                ),
            )
            self.repository._audit_tx(
                connection,
                actor_type="manager",
                actor_id=None,
                action="storage.migration.queued",
                target_type="maintenance_job",
                target_id=job_id,
                detail=(
                    f"Перенос {source.id} → {target.id}"
                    f"{f' для пространства {space_id}' if space_id else ''}: "
                    f"объектов {total_files}, байт {total_bytes}"
                ),
            )
        return self.get_migration_job(job_id)

    def recover_interrupted_maintenance(self) -> int:
        with self.database.transaction() as connection:
            updated = connection.execute(
                """
                UPDATE maintenance_jobs
                SET status = 'failed',
                    error = 'Задание прервано перезапуском Core; его можно продолжить',
                    updated_at = ?
                WHERE status = 'running'
                """,
                (utc_text(),),
            )
            backups = connection.execute(
                """
                UPDATE backup_jobs
                SET status = 'failed',
                    error = 'Резервное копирование прервано перезапуском Core; его можно продолжить',
                    updated_at = ?
                WHERE status = 'running'
                """,
                (utc_text(),),
            )
            mirrors = connection.execute(
                """
                UPDATE mirror_jobs
                SET status = 'failed',
                    error = 'Проверка зеркала прервана перезапуском Core; её можно продолжить',
                    updated_at = ?
                WHERE status = 'running'
                """,
                (utc_text(),),
            )
            return updated.rowcount + backups.rowcount + mirrors.rowcount

    def create_backup_job(self, target_root_id: str) -> BackupJobRecord:
        self._require_server_writable()
        target = self._root_by_id(target_root_id)
        if target.purpose != "backup":
            raise ConflictError("target storage is not assigned the backup role")
        if not target.write_enabled:
            raise ConflictError("backup storage does not accept writes")
        total_files, total_bytes = self._all_object_totals()
        try:
            usage = shutil.disk_usage(target.path)
        except OSError as exc:
            raise StorageCapacityError("backup storage is unavailable") from exc
        projected_percent = (usage.used + total_bytes) / usage.total * 100 if usage.total else 100
        if (
            usage.free - total_bytes < target.min_free_bytes
            or projected_percent > target.max_fill_percent
        ):
            raise StorageCapacityError("backup storage cannot safely contain this snapshot")
        job_id = str(uuid.uuid4())
        now = utc_text()
        with self.database.transaction() as connection:
            active = connection.execute(
                "SELECT id FROM backup_jobs WHERE status IN ('queued', 'running') LIMIT 1"
            ).fetchone()
            if active is not None:
                raise ConflictError("another backup snapshot is already queued or running")
            connection.execute(
                """
                INSERT INTO backup_jobs(
                    id, status, target_root_id, total_files, total_bytes,
                    processed_files, processed_bytes, snapshot_path, error,
                    created_at, started_at, completed_at, updated_at
                ) VALUES(?, 'queued', ?, ?, ?, 0, 0, '', '', ?, NULL, NULL, ?)
                """,
                (job_id, target.id, total_files, total_bytes, now, now),
            )
            self.repository._audit_tx(
                connection,
                actor_type="manager",
                actor_id=None,
                action="backup.snapshot.queued",
                target_type="backup_job",
                target_id=job_id,
                detail=f"Резервный снимок: объектов {total_files}, байт {total_bytes}",
            )
        return self.get_backup_job(job_id)

    def list_backup_jobs(self, limit: int = 50) -> list[BackupJobRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM backup_jobs ORDER BY created_at DESC LIMIT ?",
                (max(1, min(200, limit)),),
            ).fetchall()
        return [self._backup_job(row) for row in rows]

    def get_backup_job(self, job_id: str) -> BackupJobRecord:
        with self.database.connection() as connection:
            row = connection.execute("SELECT * FROM backup_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise NotFoundError("backup job not found")
        return self._backup_job(row)

    def run_backup_job(self, job_id: str) -> None:
        lock = self._maintenance_lock("backup:" + job_id)
        if not lock.acquire(blocking=False):
            return
        staging: Path | None = None
        try:
            job = self.get_backup_job(job_id)
            if job.status not in {"queued", "failed"}:
                return
            target_root = self._root_by_id(job.target_root_id)
            if target_root.purpose != "backup" or not target_root.write_enabled:
                raise ConflictError("backup storage role or write mode changed")
            now = utc_text()
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE backup_jobs
                    SET status = 'running', processed_files = 0, processed_bytes = 0,
                        snapshot_path = '', error = '', started_at = ?, completed_at = NULL,
                        updated_at = ? WHERE id = ?
                    """,
                    (now, now, job_id),
                )
            staging = target_root.path / ".staging" / f"backup-{job_id}-{uuid.uuid4().hex[:8]}"
            staging.mkdir(parents=False)
            snapshot_database = staging / "metadata.sqlite3"
            source_connection = self.database.connect()
            destination_connection = sqlite3.connect(snapshot_database)
            try:
                source_connection.backup(destination_connection)
            finally:
                destination_connection.close()
                source_connection.close()

            objects, roots = self._backup_snapshot_objects(snapshot_database)
            total_files = len(objects)
            total_bytes = sum(int(item["size_bytes"]) for item in objects)
            with self.database.transaction() as connection:
                connection.execute(
                    "UPDATE backup_jobs SET total_files = ?, total_bytes = ?, updated_at = ? "
                    "WHERE id = ?",
                    (total_files, total_bytes, utc_text(), job_id),
                )
            manifest_objects: list[dict[str, Any]] = []
            processed_files = 0
            processed_bytes = 0
            for item in objects:
                if self.get_backup_job(job_id).status == "cancelled":
                    self._remove_backup_staging(staging, target_root.path)
                    return
                source_root_path = roots.get(str(item["storage_root_id"]))
                if source_root_path is None:
                    raise NotFoundError("backup source root metadata is unavailable")
                source_root = Path(source_root_path).resolve(strict=True)
                source = (source_root / str(item["object_path"])).resolve(strict=True)
                if source_root not in source.parents or not source.is_file() or source.is_symlink():
                    raise PermissionDeniedError(
                        "backup source object failed containment validation"
                    )
                destination_relative = (
                    Path("objects")
                    / str(item["entity"])
                    / f"{item['id']}-{uuid.uuid4().hex[:8]}.blob"
                )
                destination = staging / destination_relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                copied, digest = self._copy_verified(source, destination)
                if copied != int(item["size_bytes"]) or digest != item["sha256"]:
                    raise ConflictError("source changed while the backup snapshot was created")
                processed_files += 1
                processed_bytes += copied
                manifest_objects.append(
                    {
                        "entity": item["entity"],
                        "id": item["id"],
                        "source_root_id": item["storage_root_id"],
                        "source_object_path": item["object_path"],
                        "backup_object_path": destination_relative.as_posix(),
                        "size_bytes": copied,
                        "sha256": digest,
                    }
                )
                with self.database.transaction() as connection:
                    connection.execute(
                        """
                        UPDATE backup_jobs
                        SET processed_files = ?, processed_bytes = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (processed_files, processed_bytes, utc_text(), job_id),
                    )
            manifest = {
                "schema_version": 1,
                "created_at": utc_text(),
                "core_database": "metadata.sqlite3",
                "objects": manifest_objects,
            }
            manifest_path = staging / "manifest.json"
            with manifest_path.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(manifest, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            backups_directory = target_root.path / "backups"
            backups_directory.mkdir(exist_ok=True)
            final_relative = Path("backups") / (utc_now().strftime("%Y%m%dT%H%M%SZ") + "-" + job_id)
            final_path = target_root.path / final_relative
            os.replace(staging, final_path)
            staging = None
            completed_at = utc_text()
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE backup_jobs
                    SET status = 'completed', snapshot_path = ?, completed_at = ?, updated_at = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (final_relative.as_posix(), completed_at, completed_at, job_id),
                )
                self.repository._audit_tx(
                    connection,
                    actor_type="manager",
                    actor_id=None,
                    action="backup.snapshot.completed",
                    target_type="backup_job",
                    target_id=job_id,
                    detail=f"Резервный снимок проверен: {final_relative.as_posix()}",
                )
        except Exception as exc:
            if staging is not None:
                try:
                    target = self._root_by_id(self.get_backup_job(job_id).target_root_id)
                    self._remove_backup_staging(staging, target.path)
                except Exception:
                    pass
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE backup_jobs SET status = 'failed', error = ?, updated_at = ?
                    WHERE id = ? AND status != 'cancelled'
                    """,
                    (str(exc)[:1000], utc_text(), job_id),
                )
        finally:
            lock.release()

    def resume_backup_job(self, job_id: str) -> BackupJobRecord:
        job = self.get_backup_job(job_id)
        if job.status not in {"failed", "cancelled"}:
            raise ConflictError("only failed or cancelled backup jobs can be resumed")
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE backup_jobs SET status = 'queued', error = '', completed_at = NULL, "
                "updated_at = ? WHERE id = ?",
                (utc_text(), job_id),
            )
        return self.get_backup_job(job_id)

    def cancel_backup_job(self, job_id: str) -> BackupJobRecord:
        job = self.get_backup_job(job_id)
        if job.status in {"completed", "cancelled"}:
            return job
        now = utc_text()
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE backup_jobs SET status = 'cancelled', completed_at = ?, updated_at = ? "
                "WHERE id = ?",
                (now, now, job_id),
            )
        return self.get_backup_job(job_id)

    def _all_object_totals(self) -> tuple[int, int]:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT count(*) AS files, COALESCE(sum(size_bytes), 0) AS bytes FROM (
                    SELECT size_bytes FROM files
                    UNION ALL SELECT size_bytes FROM file_versions
                )
                """
            ).fetchone()
        return int(row["files"]), int(row["bytes"])

    @staticmethod
    def _backup_snapshot_objects(
        snapshot_database: Path,
    ) -> tuple[list[sqlite3.Row], dict[str, str]]:
        connection = sqlite3.connect(snapshot_database)
        connection.row_factory = sqlite3.Row
        try:
            objects = connection.execute(
                """
                SELECT 'files' AS entity, id, storage_root_id, object_path, size_bytes, sha256
                FROM files
                UNION ALL
                SELECT 'file_versions' AS entity, id, storage_root_id, object_path, size_bytes, sha256
                FROM file_versions
                """
            ).fetchall()
            roots = {
                row["id"]: row["path"]
                for row in connection.execute("SELECT id, path FROM storage_roots").fetchall()
            }
            return objects, roots
        finally:
            connection.close()

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
    def _remove_backup_staging(staging: Path, storage_root: Path) -> None:
        resolved_root = storage_root.resolve(strict=True)
        resolved_staging = staging.resolve(strict=True)
        expected_parent = (resolved_root / ".staging").resolve(strict=True)
        if expected_parent not in resolved_staging.parents:
            raise PermissionDeniedError("backup staging path failed containment validation")
        shutil.rmtree(resolved_staging)

    @staticmethod
    def _backup_job(row: sqlite3.Row) -> BackupJobRecord:
        return BackupJobRecord(
            **{field: row[field] for field in BackupJobRecord.__dataclass_fields__}
        )

    @staticmethod
    def backup_to_dict(job: BackupJobRecord) -> dict[str, Any]:
        return asdict(job)

    def sync_file_to_mirrors(self, record: FileRecord) -> None:
        """Best-effort replication that never turns a committed upload into a failure."""
        if record.deleted_at is not None:
            return
        for target in self.list_roots():
            if target.purpose != "mirror" or not target.write_enabled:
                continue
            try:
                self._ensure_mirror_replica(record, target)
            except Exception as exc:
                self._mark_mirror_replica_error(record, target.id, exc)

    def mirror_overview(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        with self.database.connection() as connection:
            totals = connection.execute(
                """
                SELECT count(*) AS files, COALESCE(sum(size_bytes), 0) AS bytes
                FROM files WHERE deleted_at IS NULL
                """
            ).fetchone()
            for root in self.list_roots():
                if root.purpose != "mirror":
                    continue
                current = connection.execute(
                    """
                    SELECT count(*) AS files, COALESCE(sum(f.size_bytes), 0) AS bytes,
                           max(m.updated_at) AS last_checked_at
                    FROM files AS f
                    JOIN mirror_replicas AS m
                      ON m.file_id = f.id AND m.storage_root_id = ?
                    WHERE f.deleted_at IS NULL AND m.status = 'current'
                      AND m.source_version = f.version AND m.size_bytes = f.size_bytes
                      AND m.sha256 = f.sha256
                    """,
                    (root.id,),
                ).fetchone()
                current_files = int(current["files"])
                total_files = int(totals["files"])
                result.append(
                    {
                        "root_id": root.id,
                        "disk_id": root.disk_id,
                        "path": str(root.path),
                        "write_enabled": root.write_enabled,
                        "total_files": total_files,
                        "total_bytes": int(totals["bytes"]),
                        "current_files": current_files,
                        "current_bytes": int(current["bytes"]),
                        "degraded_files": max(0, total_files - current_files),
                        "last_checked_at": current["last_checked_at"],
                    }
                )
        return result

    def create_mirror_job(self, target_root_id: str) -> MirrorJobRecord:
        target = self._root_by_id(target_root_id)
        if target.purpose != "mirror":
            raise ConflictError("target storage is not assigned the mirror role")
        if not target.write_enabled:
            raise ConflictError("mirror storage does not accept writes")
        with self.database.connection() as connection:
            totals = connection.execute(
                """
                SELECT count(*) AS files, COALESCE(sum(size_bytes), 0) AS bytes
                FROM files WHERE deleted_at IS NULL
                """
            ).fetchone()
        job_id = str(uuid.uuid4())
        now = utc_text()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO mirror_jobs(
                    id, status, target_root_id, total_files, total_bytes,
                    processed_files, processed_bytes, healthy_files, repaired_files,
                    failed_files, error, created_at, started_at, completed_at, updated_at
                ) VALUES(?, 'queued', ?, ?, ?, 0, 0, 0, 0, 0, '', ?, NULL, NULL, ?)
                """,
                (
                    job_id,
                    target.id,
                    int(totals["files"]),
                    int(totals["bytes"]),
                    now,
                    now,
                ),
            )
            self.repository._audit_tx(
                connection,
                actor_type="manager",
                actor_id=None,
                action="mirror.reconcile.queued",
                target_type="mirror_job",
                target_id=job_id,
                detail=f"Проверка зеркала {target.id}: объектов {int(totals['files'])}",
            )
        return self.get_mirror_job(job_id)

    def list_mirror_jobs(self, limit: int = 50) -> list[MirrorJobRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM mirror_jobs ORDER BY created_at DESC LIMIT ?",
                (max(1, min(200, limit)),),
            ).fetchall()
        return [self._mirror_job(row) for row in rows]

    def get_mirror_job(self, job_id: str) -> MirrorJobRecord:
        with self.database.connection() as connection:
            row = connection.execute("SELECT * FROM mirror_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise NotFoundError("mirror job not found")
        return self._mirror_job(row)

    def run_mirror_job(self, job_id: str) -> None:
        lock = self._maintenance_lock("mirror:" + job_id)
        if not lock.acquire(blocking=False):
            return
        try:
            job = self.get_mirror_job(job_id)
            if job.status not in {"queued", "failed"}:
                return
            target = self._root_by_id(job.target_root_id)
            if target.purpose != "mirror" or not target.write_enabled:
                raise ConflictError("mirror storage role or write mode changed")
            with self.database.connection() as connection:
                rows = connection.execute(
                    "SELECT * FROM files WHERE deleted_at IS NULL ORDER BY id"
                ).fetchall()
            files = [self._file(row) for row in rows]
            total_bytes = sum(item.size_bytes for item in files)
            now = utc_text()
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE mirror_jobs
                    SET status = 'running', total_files = ?, total_bytes = ?,
                        processed_files = 0, processed_bytes = 0, healthy_files = 0,
                        repaired_files = 0, failed_files = 0, error = '',
                        started_at = ?, completed_at = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (len(files), total_bytes, now, now, job_id),
                )
            processed_files = 0
            processed_bytes = 0
            healthy_files = 0
            repaired_files = 0
            failed_files = 0
            errors: list[str] = []
            for record in files:
                if self.get_mirror_job(job_id).status == "cancelled":
                    return
                try:
                    outcome = self._ensure_mirror_replica(record, target)
                    if outcome == "healthy":
                        healthy_files += 1
                    else:
                        repaired_files += 1
                except Exception as exc:
                    failed_files += 1
                    errors.append(f"{record.logical_path}: {exc}")
                    self._mark_mirror_replica_error(record, target.id, exc)
                processed_files += 1
                processed_bytes += record.size_bytes
                with self.database.transaction() as connection:
                    connection.execute(
                        """
                        UPDATE mirror_jobs
                        SET processed_files = ?, processed_bytes = ?, healthy_files = ?,
                            repaired_files = ?, failed_files = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            processed_files,
                            processed_bytes,
                            healthy_files,
                            repaired_files,
                            failed_files,
                            utc_text(),
                            job_id,
                        ),
                    )
            completed_at = utc_text()
            final_status = "failed" if failed_files else "completed"
            error = "; ".join(errors)[:1000]
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE mirror_jobs
                    SET status = ?, error = ?, completed_at = ?, updated_at = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (final_status, error, completed_at, completed_at, job_id),
                )
                self.repository._audit_tx(
                    connection,
                    actor_type="manager",
                    actor_id=None,
                    action="mirror.reconcile.completed"
                    if not failed_files
                    else "mirror.reconcile.degraded",
                    target_type="mirror_job",
                    target_id=job_id,
                    detail=(
                        f"Зеркало {target.id}: исправлено {repaired_files}, "
                        f"уже исправно {healthy_files}, ошибок {failed_files}"
                    ),
                )
        except Exception as exc:
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE mirror_jobs SET status = 'failed', error = ?, updated_at = ?
                    WHERE id = ? AND status != 'cancelled'
                    """,
                    (str(exc)[:1000], utc_text(), job_id),
                )
        finally:
            lock.release()

    def resume_mirror_job(self, job_id: str) -> MirrorJobRecord:
        job = self.get_mirror_job(job_id)
        if job.status not in {"failed", "cancelled"}:
            raise ConflictError("only failed or cancelled mirror jobs can be resumed")
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE mirror_jobs SET status = 'queued', error = '', completed_at = NULL, "
                "updated_at = ? WHERE id = ?",
                (utc_text(), job_id),
            )
        return self.get_mirror_job(job_id)

    def cancel_mirror_job(self, job_id: str) -> MirrorJobRecord:
        job = self.get_mirror_job(job_id)
        if job.status in {"completed", "cancelled"}:
            return job
        now = utc_text()
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE mirror_jobs SET status = 'cancelled', completed_at = ?, updated_at = ? "
                "WHERE id = ?",
                (now, now, job_id),
            )
        return self.get_mirror_job(job_id)

    def _ensure_mirror_replica(
        self,
        record: FileRecord,
        target: StorageRootRecord,
    ) -> str:
        if target.purpose != "mirror" or not target.write_enabled:
            raise ConflictError("mirror storage is unavailable for replication")
        with self.database.connection() as connection:
            existing = connection.execute(
                "SELECT * FROM mirror_replicas WHERE file_id = ? AND storage_root_id = ?",
                (record.id, target.id),
            ).fetchone()
        if (
            existing is not None
            and existing["status"] == "current"
            and existing["source_version"] == record.version
            and existing["size_bytes"] == record.size_bytes
            and existing["sha256"] == record.sha256
            and self._replica_matches(existing, target)
        ):
            with self.database.transaction() as connection:
                connection.execute(
                    "UPDATE mirror_replicas SET updated_at = ?, error = '' "
                    "WHERE file_id = ? AND storage_root_id = ?",
                    (utc_text(), record.id, target.id),
                )
            return "healthy"

        source_root = self._root_by_id(record.storage_root_id)
        try:
            source = (source_root.path / record.object_path).resolve(strict=True)
        except OSError as exc:
            raise NotFoundError("mirror source object is unavailable") from exc
        if source_root.path not in source.parents or not source.is_file() or source.is_symlink():
            raise PermissionDeniedError("mirror source object failed containment validation")
        try:
            usage = shutil.disk_usage(target.path)
        except OSError as exc:
            raise StorageCapacityError("mirror storage is unavailable") from exc
        projected_percent = (
            (usage.used + record.size_bytes) / usage.total * 100 if usage.total else 100
        )
        if (
            usage.free - record.size_bytes < target.min_free_bytes
            or projected_percent > target.max_fill_percent
        ):
            raise StorageCapacityError("mirror storage has insufficient safe free space")

        object_id = uuid.uuid4().hex
        relative = (
            Path("objects")
            / "mirrors"
            / record.id[:2]
            / f"{record.id}.v{record.version}.{object_id[:8]}.blob"
        )
        destination = target.path / relative
        staging = target.path / ".staging" / f"{object_id}.mirror"
        destination.parent.mkdir(parents=True, exist_ok=True)
        published = False
        try:
            copied, digest = self._copy_verified(source, staging)
            if copied != record.size_bytes or digest != record.sha256:
                raise ConflictError("mirror source failed size or SHA-256 verification")
            os.replace(staging, destination)
            published = True
            make_managed_file_inert(destination, target.path)
            now = utc_text()
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO mirror_replicas(
                        file_id, storage_root_id, object_path, size_bytes, sha256,
                        source_version, status, error, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, 'current', '', ?, ?)
                    ON CONFLICT(file_id, storage_root_id) DO UPDATE SET
                        object_path = excluded.object_path,
                        size_bytes = excluded.size_bytes,
                        sha256 = excluded.sha256,
                        source_version = excluded.source_version,
                        status = 'current', error = '', updated_at = excluded.updated_at
                    """,
                    (
                        record.id,
                        target.id,
                        relative.as_posix(),
                        record.size_bytes,
                        record.sha256,
                        record.version,
                        now,
                        now,
                    ),
                )
        except Exception:
            staging.unlink(missing_ok=True)
            if published:
                destination.unlink(missing_ok=True)
            raise
        old_object_path = str(existing["object_path"]) if existing is not None else ""
        if old_object_path and old_object_path != relative.as_posix():
            self._retain_replaced_replica(target, old_object_path)
        return "repaired"

    def _replica_matches(self, row: sqlite3.Row, target: StorageRootRecord) -> bool:
        if not row["object_path"]:
            return False
        try:
            path = (target.path / row["object_path"]).resolve(strict=True)
            if target.path not in path.parents or not path.is_file() or path.is_symlink():
                return False
            if path.stat().st_size != int(row["size_bytes"]):
                return False
            return self._file_sha256(path) == row["sha256"]
        except OSError:
            return False

    def _mark_mirror_replica_error(
        self,
        record: FileRecord,
        target_root_id: str,
        error: Exception,
    ) -> None:
        now = utc_text()
        detail = str(error)[:1000]
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT object_path FROM mirror_replicas WHERE file_id = ? AND storage_root_id = ?",
                (record.id, target_root_id),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO mirror_replicas(
                        file_id, storage_root_id, object_path, size_bytes, sha256,
                        source_version, status, error, created_at, updated_at
                    ) VALUES(?, ?, '', 0, '', ?, 'error', ?, ?, ?)
                    """,
                    (record.id, target_root_id, record.version, detail, now, now),
                )
            else:
                connection.execute(
                    """
                    UPDATE mirror_replicas
                    SET status = ?, error = ?, updated_at = ?
                    WHERE file_id = ? AND storage_root_id = ?
                    """,
                    (
                        "stale" if existing["object_path"] else "error",
                        detail,
                        now,
                        record.id,
                        target_root_id,
                    ),
                )

    @staticmethod
    def _retain_replaced_replica(target: StorageRootRecord, object_path: str) -> None:
        try:
            source = (target.path / object_path).resolve(strict=True)
            if target.path not in source.parents or not source.is_file() or source.is_symlink():
                return
            trash = target.path / ".trash" / "mirrors"
            trash.mkdir(parents=True, exist_ok=True)
            destination = trash / f"{source.name}.{uuid.uuid4().hex[:8]}.replaced"
            os.replace(source, destination)
        except OSError:
            return

    @staticmethod
    def _mirror_job(row: sqlite3.Row) -> MirrorJobRecord:
        return MirrorJobRecord(
            **{field: row[field] for field in MirrorJobRecord.__dataclass_fields__}
        )

    @staticmethod
    def mirror_job_to_dict(job: MirrorJobRecord) -> dict[str, Any]:
        return asdict(job)

    def list_migration_jobs(self, limit: int = 50) -> list[MigrationJobRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM maintenance_jobs ORDER BY created_at DESC LIMIT ?",
                (max(1, min(200, limit)),),
            ).fetchall()
        return [self._migration_job(row) for row in rows]

    def get_migration_job(self, job_id: str) -> MigrationJobRecord:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM maintenance_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("maintenance job not found")
        return self._migration_job(row)

    def run_migration_job(self, job_id: str) -> None:
        lock = self._maintenance_lock(job_id)
        if not lock.acquire(blocking=False):
            return
        try:
            job = self.get_migration_job(job_id)
            if job.status not in {"queued", "failed"}:
                return
            source = self._root_by_id(job.source_root_id)
            target = self._root_by_id(job.target_root_id)
            if (source.write_enabled and not job.space_id) or not target.write_enabled:
                raise ConflictError("storage write modes changed; migration was not started")
            now = utc_text()
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE maintenance_jobs
                    SET status = 'running', error = '', started_at = COALESCE(started_at, ?),
                        completed_at = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, now, job_id),
                )
            while True:
                current = self.get_migration_job(job_id)
                if current.status == "cancelled":
                    return
                item = self._next_migration_object(source.id, current.space_id)
                if item is None:
                    break
                moved = self._copy_and_switch_object(source, target, item)
                if not moved:
                    continue
                processed_files = current.processed_files + 1
                processed_bytes = current.processed_bytes + int(item["size_bytes"])
                with self.database.transaction() as connection:
                    connection.execute(
                        """
                        UPDATE maintenance_jobs
                        SET processed_files = ?, processed_bytes = ?, retained_sources = ?,
                            total_files = MAX(total_files, ?),
                            total_bytes = MAX(total_bytes, ?), updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            processed_files,
                            processed_bytes,
                            processed_files,
                            processed_files,
                            processed_bytes,
                            utc_text(),
                            job_id,
                        ),
                    )
            completed_at = utc_text()
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE maintenance_jobs
                    SET status = 'completed', completed_at = ?, updated_at = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (completed_at, completed_at, job_id),
                )
                self.repository._audit_tx(
                    connection,
                    actor_type="manager",
                    actor_id=None,
                    action="storage.migration.completed",
                    target_type="maintenance_job",
                    target_id=job_id,
                    detail=("Перенос завершён; исходные объекты сохранены как страховочные копии"),
                )
        except Exception as exc:
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE maintenance_jobs
                    SET status = 'failed', error = ?, updated_at = ?
                    WHERE id = ? AND status != 'cancelled'
                    """,
                    (str(exc)[:1000], utc_text(), job_id),
                )
        finally:
            lock.release()

    def resume_migration_job(self, job_id: str) -> MigrationJobRecord:
        job = self.get_migration_job(job_id)
        if job.status not in {"failed", "cancelled"}:
            raise ConflictError("only failed or cancelled jobs can be resumed")
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE maintenance_jobs
                SET status = 'queued', error = '', completed_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (utc_text(), job_id),
            )
        return self.get_migration_job(job_id)

    def cancel_migration_job(self, job_id: str) -> MigrationJobRecord:
        job = self.get_migration_job(job_id)
        if job.status in {"completed", "cancelled"}:
            return job
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE maintenance_jobs
                SET status = 'cancelled', completed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (utc_text(), utc_text(), job_id),
            )
        return self.get_migration_job(job_id)

    def _migration_totals(
        self, source_root_id: str, space_id: str | None = None
    ) -> tuple[int, int]:
        with self.database.connection() as connection:
            if space_id:
                row = connection.execute(
                    """
                    SELECT count(*) AS files, COALESCE(sum(size_bytes), 0) AS bytes
                    FROM (
                        SELECT size_bytes FROM files
                        WHERE storage_root_id = ? AND space_id = ?
                        UNION ALL
                        SELECT versions.size_bytes
                        FROM file_versions AS versions
                        JOIN files ON files.id = versions.file_id
                        WHERE versions.storage_root_id = ? AND files.space_id = ?
                    )
                    """,
                    (source_root_id, space_id, source_root_id, space_id),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT count(*) AS files, COALESCE(sum(size_bytes), 0) AS bytes
                    FROM (
                        SELECT size_bytes FROM files WHERE storage_root_id = ?
                        UNION ALL
                        SELECT size_bytes FROM file_versions WHERE storage_root_id = ?
                    )
                    """,
                    (source_root_id, source_root_id),
                ).fetchone()
        return int(row["files"]), int(row["bytes"])

    def _next_migration_object(
        self, source_root_id: str, space_id: str | None = None
    ) -> sqlite3.Row | None:
        with self.database.connection() as connection:
            if space_id:
                return connection.execute(
                    """
                    SELECT 'files' AS entity, id, object_path, size_bytes, sha256
                    FROM files WHERE storage_root_id = ? AND space_id = ?
                    UNION ALL
                    SELECT 'file_versions' AS entity, versions.id, versions.object_path,
                           versions.size_bytes, versions.sha256
                    FROM file_versions AS versions
                    JOIN files ON files.id = versions.file_id
                    WHERE versions.storage_root_id = ? AND files.space_id = ?
                    LIMIT 1
                    """,
                    (source_root_id, space_id, source_root_id, space_id),
                ).fetchone()
            return connection.execute(
                """
                SELECT 'files' AS entity, id, object_path, size_bytes, sha256
                FROM files WHERE storage_root_id = ?
                UNION ALL
                SELECT 'file_versions' AS entity, id, object_path, size_bytes, sha256
                FROM file_versions WHERE storage_root_id = ?
                LIMIT 1
                """,
                (source_root_id, source_root_id),
            ).fetchone()

    def _copy_and_switch_object(
        self,
        source_root: StorageRootRecord,
        target_root: StorageRootRecord,
        item: sqlite3.Row,
    ) -> bool:
        try:
            source = (source_root.path / item["object_path"]).resolve(strict=True)
        except OSError as exc:
            raise NotFoundError("managed source object is unavailable") from exc
        if source_root.path not in source.parents or not source.is_file() or source.is_symlink():
            raise PermissionDeniedError("managed source object failed containment validation")
        object_id = str(uuid.uuid4())
        relative = Path("objects") / "migrated" / object_id[:2] / f"{object_id}.blob"
        target = target_root.path / relative
        staging = target_root.path / ".staging" / f"{object_id}.migration"
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        copied = 0
        try:
            with source.open("rb") as reader, staging.open("xb") as writer:
                while chunk := reader.read(4 * 1024 * 1024):
                    writer.write(chunk)
                    digest.update(chunk)
                    copied += len(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            if copied != int(item["size_bytes"]) or digest.hexdigest() != item["sha256"]:
                raise ConflictError("source object failed size or SHA-256 verification")
            os.replace(staging, target)
            make_managed_file_inert(target, target_root.path)
            table = "files" if item["entity"] == "files" else "file_versions"
            with self.database.transaction() as connection:
                updated = connection.execute(
                    f"""
                    UPDATE {table}
                    SET storage_root_id = ?, object_path = ?
                    WHERE id = ? AND storage_root_id = ? AND object_path = ? AND sha256 = ?
                    """,
                    (
                        target_root.id,
                        relative.as_posix(),
                        item["id"],
                        source_root.id,
                        item["object_path"],
                        item["sha256"],
                    ),
                )
                if updated.rowcount != 1:
                    target.unlink(missing_ok=True)
                    return False
            return True
        except Exception:
            staging.unlink(missing_ok=True)
            if target.exists():
                target.unlink(missing_ok=True)
            raise

    def _maintenance_lock(self, job_id: str) -> threading.Lock:
        with self._maintenance_locks_guard:
            return self._maintenance_locks.setdefault(job_id, threading.Lock())

    @staticmethod
    def _migration_job(row: sqlite3.Row) -> MigrationJobRecord:
        return MigrationJobRecord(
            **{field: row[field] for field in MigrationJobRecord.__dataclass_fields__}
        )

    @staticmethod
    def migration_to_dict(job: MigrationJobRecord) -> dict[str, Any]:
        return asdict(job)

    def _resumable_lock(self, upload_id: str) -> threading.RLock:
        with self._resumable_locks_guard:
            return self._resumable_locks.setdefault(upload_id, threading.RLock())

    def _root_by_id(self, root_id: str) -> StorageRootRecord:
        root = next((item for item in self.list_roots() if item.id == root_id), None)
        if root is None:
            raise NotFoundError("storage root is unavailable")
        return root

    def _resumable_path(self, record: ResumableUploadRecord) -> Path:
        storage_root = self._root_by_id(record.storage_root_id)
        root = (
            Path(record.staging_path).resolve(strict=True)
            if record.staging_path
            else storage_root.path
        )
        try:
            path = (root / record.temporary_path).resolve(strict=True)
        except OSError as exc:
            raise NotFoundError("partial upload data is unavailable") from exc
        if root not in path.parents or not path.is_file() or path.is_symlink():
            raise PermissionDeniedError("partial upload failed containment validation")
        return path

    def _resolve_mirror_download(self, record: FileRecord) -> Path | None:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT m.object_path, m.size_bytes, r.path AS root_path
                FROM mirror_replicas AS m
                JOIN storage_roots AS r ON r.id = m.storage_root_id
                WHERE m.file_id = ? AND m.status = 'current'
                  AND m.source_version = ? AND m.size_bytes = ? AND m.sha256 = ?
                  AND r.enabled = 1 AND r.purpose = 'mirror'
                ORDER BY m.updated_at DESC
                """,
                (record.id, record.version, record.size_bytes, record.sha256),
            ).fetchall()
        for row in rows:
            try:
                root = Path(row["root_path"]).resolve(strict=True)
                path = (root / row["object_path"]).resolve(strict=True)
                if (
                    root in path.parents
                    and path.is_file()
                    and not path.is_symlink()
                    and path.stat().st_size == int(row["size_bytes"])
                ):
                    return path
            except OSError:
                continue
        return None

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(4 * 1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _make_staging_file_writable(path: Path) -> None:
        if not path.exists():
            return
        current = path.stat().st_mode
        if os.name == "nt":
            os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
        else:
            os.chmod(path, current | stat.S_IWUSR | stat.S_IRUSR)

    @staticmethod
    def _like_prefix(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    @staticmethod
    def _file(row: sqlite3.Row) -> FileRecord:
        fields = {key: row[key] for key in FileRecord.__dataclass_fields__}
        return FileRecord(**fields)

    @staticmethod
    def _resumable_upload(row: sqlite3.Row) -> ResumableUploadRecord:
        fields = {key: row[key] for key in ResumableUploadRecord.__dataclass_fields__}
        return ResumableUploadRecord(**fields)

    @staticmethod
    def _transfer(row: sqlite3.Row) -> TransferRecord:
        fields = {key: row[key] for key in TransferRecord.__dataclass_fields__}
        return TransferRecord(**fields)

    @staticmethod
    def to_dict(record: FileRecord) -> dict[str, Any]:
        return asdict(record)

    @staticmethod
    def resumable_to_dict(record: ResumableUploadRecord) -> dict[str, Any]:
        return {
            "id": record.id,
            "space_id": record.space_id,
            "logical_path": record.logical_path,
            "expected_size": record.expected_size,
            "expected_sha256": record.expected_sha256,
            "received_bytes": record.received_bytes,
            "content_type": record.content_type,
            "status": record.status,
            "result_file_id": record.result_file_id,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "expires_at": record.expires_at,
        }

    @staticmethod
    def transfer_to_dict(record: TransferRecord) -> dict[str, Any]:
        if record.status == "moving":
            progress_bytes = record.storage_bytes
        else:
            progress_bytes = record.network_bytes
        progress_percent = (
            min(100.0, progress_bytes / record.total_bytes * 100) if record.total_bytes else 0.0
        )
        return {
            **asdict(record),
            "progress_bytes": progress_bytes,
            "progress_percent": round(progress_percent, 1),
            "retryable": (
                record.direction == "inbound"
                and record.status == "failed"
                and record.network_bytes == record.total_bytes
                and bool(record.sha256)
            ),
        }
