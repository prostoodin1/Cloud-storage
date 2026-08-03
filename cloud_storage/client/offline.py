from __future__ import annotations

import hashlib
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class OfflineRecord:
    id: str
    server_url: str
    space_id: str
    logical_path: str
    local_path: str
    baseline_sha256: str
    local_sha256: str
    remote_sha256: str
    size_bytes: int
    local_mtime_ns: int
    remote_exists: int | None
    status: str
    error: str
    created_at: str
    updated_at: str


class OfflineStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS offline_files (
                    id TEXT PRIMARY KEY,
                    server_url TEXT NOT NULL,
                    space_id TEXT NOT NULL,
                    logical_path TEXT NOT NULL,
                    local_path TEXT NOT NULL,
                    baseline_sha256 TEXT NOT NULL,
                    local_sha256 TEXT NOT NULL,
                    remote_sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
                    local_mtime_ns INTEGER NOT NULL DEFAULT 0,
                    remote_exists INTEGER CHECK(remote_exists IN (0, 1) OR remote_exists IS NULL),
                    status TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(server_url, space_id, logical_path)
                );
                CREATE INDEX IF NOT EXISTS idx_offline_files_status
                ON offline_files(status, updated_at);
                """
            )

    def mark_synced(
        self,
        server_url: str,
        space_id: str,
        logical_path: str,
        local_path: Path,
        remote_sha256: str,
        size_bytes: int,
    ) -> OfflineRecord:
        resolved = local_path.resolve()
        local_sha256 = remote_sha256 or _file_sha256(resolved)
        stat = resolved.stat()
        now = _now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO offline_files(
                    id, server_url, space_id, logical_path, local_path,
                    baseline_sha256, local_sha256, remote_sha256, size_bytes,
                    local_mtime_ns, remote_exists, status, error, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'current', '', ?, ?)
                ON CONFLICT(server_url, space_id, logical_path) DO UPDATE SET
                    local_path = excluded.local_path,
                    baseline_sha256 = excluded.baseline_sha256,
                    local_sha256 = excluded.local_sha256,
                    remote_sha256 = excluded.remote_sha256,
                    size_bytes = excluded.size_bytes,
                    local_mtime_ns = excluded.local_mtime_ns,
                    remote_exists = 1,
                    status = 'current',
                    error = '',
                    updated_at = excluded.updated_at
                """,
                (
                    str(uuid.uuid4()),
                    server_url.rstrip("/"),
                    space_id,
                    logical_path,
                    str(resolved),
                    local_sha256,
                    local_sha256,
                    local_sha256,
                    max(0, size_bytes),
                    stat.st_mtime_ns,
                    now,
                    now,
                ),
            )
        record = self.find(server_url, space_id, logical_path)
        if record is None:
            raise RuntimeError("offline index update failed")
        return record

    def scan_local(self, record_id: str) -> OfflineRecord:
        record = self.get(record_id)
        path = Path(record.local_path)
        if path.is_file():
            stat = path.stat()
            if stat.st_mtime_ns == record.local_mtime_ns and record.local_sha256:
                local_sha256 = record.local_sha256
            else:
                local_sha256 = _file_sha256(path)
            status = self._status(record.baseline_sha256, local_sha256, record.remote_sha256, record.remote_exists)
            error = ""
            local_mtime_ns = stat.st_mtime_ns
            size_bytes = stat.st_size
        else:
            local_sha256 = ""
            status = "local_missing" if record.remote_exists != 0 else "missing_both"
            error = "Локальная копия не найдена"
            local_mtime_ns = 0
            size_bytes = record.size_bytes
        self._update_state(
            record_id,
            local_sha256=local_sha256,
            size_bytes=size_bytes,
            local_mtime_ns=local_mtime_ns,
            status=status,
            error=error,
        )
        return self.get(record_id)

    def apply_remote_state(
        self,
        record_id: str,
        *,
        exists: bool,
        sha256: str = "",
        size_bytes: int = 0,
    ) -> OfflineRecord:
        record = self.scan_local(record_id)
        remote_sha256 = sha256 if exists else ""
        if not exists:
            status = "remote_missing" if record.local_sha256 else "missing_both"
            error = "Файл удалён на сервере"
        else:
            status = self._status(
                record.baseline_sha256,
                record.local_sha256,
                remote_sha256,
                1,
            )
            error = ""
        self._update_state(
            record_id,
            remote_sha256=remote_sha256,
            remote_exists=1 if exists else 0,
            size_bytes=max(0, size_bytes) if exists else record.size_bytes,
            status=status,
            error=error,
        )
        return self.get(record_id)

    def set_error(self, record_id: str, message: str) -> None:
        self._update_state(record_id, status="error", error=message[:500])

    def get(self, record_id: str) -> OfflineRecord:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM offline_files WHERE id = ?", (record_id,)
            ).fetchone()
        if row is None:
            raise KeyError(record_id)
        return self._record(row)

    def find(self, server_url: str, space_id: str, logical_path: str) -> OfflineRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM offline_files
                WHERE server_url = ? AND space_id = ? AND logical_path = ?
                """,
                (server_url.rstrip("/"), space_id, logical_path),
            ).fetchone()
        return self._record(row) if row is not None else None

    def list(self) -> list[OfflineRecord]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM offline_files ORDER BY logical_path COLLATE NOCASE"
            ).fetchall()
        return [self._record(row) for row in rows]

    def remove(self, record_id: str) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM offline_files WHERE id = ?", (record_id,))

    def _update_state(self, record_id: str, **changes: object) -> None:
        allowed = {
            "local_sha256",
            "remote_sha256",
            "size_bytes",
            "local_mtime_ns",
            "remote_exists",
            "status",
            "error",
        }
        if not changes or not set(changes).issubset(allowed):
            raise ValueError("invalid offline index update")
        assignments = [f"{name} = ?" for name in changes]
        parameters = [*changes.values(), _now(), record_id]
        with self._connection() as connection:
            connection.execute(
                f"UPDATE offline_files SET {', '.join(assignments)}, updated_at = ? WHERE id = ?",
                parameters,
            )

    @staticmethod
    def _status(
        baseline_sha256: str,
        local_sha256: str,
        remote_sha256: str,
        remote_exists: int | None,
    ) -> str:
        if not local_sha256:
            return "local_missing" if remote_exists != 0 else "missing_both"
        if remote_exists == 0:
            return "remote_missing"
        local_changed = local_sha256 != baseline_sha256
        remote_changed = bool(remote_sha256) and remote_sha256 != baseline_sha256
        if local_changed and remote_changed:
            return "conflict"
        if local_changed:
            return "local_changed"
        if remote_changed:
            return "remote_changed"
        return "current"

    @staticmethod
    def _record(row: sqlite3.Row) -> OfflineRecord:
        return OfflineRecord(**{field: row[field] for field in OfflineRecord.__dataclass_fields__})


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
