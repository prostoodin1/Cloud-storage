from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

TRANSFER_STATUSES = {"queued", "running", "paused", "completed", "failed", "cancelled"}


@dataclass(frozen=True, slots=True)
class TransferRecord:
    id: str
    kind: str
    server_url: str
    space_id: str
    logical_path: str
    local_path: str
    remote_session_id: str
    expected_sha256: str
    total_bytes: int
    transferred_bytes: int
    source_mtime_ns: int
    status: str
    error: str
    created_at: str
    updated_at: str


class TransferStore:
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
                CREATE TABLE IF NOT EXISTS transfers (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK(kind IN ('upload', 'download')),
                    server_url TEXT NOT NULL,
                    space_id TEXT NOT NULL,
                    logical_path TEXT NOT NULL,
                    local_path TEXT NOT NULL,
                    remote_session_id TEXT NOT NULL DEFAULT '',
                    expected_sha256 TEXT NOT NULL DEFAULT '',
                    total_bytes INTEGER NOT NULL CHECK(total_bytes >= 0),
                    transferred_bytes INTEGER NOT NULL DEFAULT 0 CHECK(transferred_bytes >= 0),
                    source_mtime_ns INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL CHECK(
                        status IN ('queued', 'running', 'paused', 'completed', 'failed', 'cancelled')
                    ),
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_transfers_status
                ON transfers(status, created_at);
                """
            )

    def recover_interrupted(self) -> int:
        with self._connection() as connection:
            updated = connection.execute(
                """
                UPDATE transfers
                SET status = 'queued', error = 'Продолжение после перезапуска', updated_at = ?
                WHERE status = 'running'
                """,
                (_now(),),
            )
            return updated.rowcount

    def queue_upload(
        self,
        server_url: str,
        space_id: str,
        logical_path: str,
        source: Path,
    ) -> TransferRecord:
        stat = source.stat()
        return self._insert(
            kind="upload",
            server_url=server_url,
            space_id=space_id,
            logical_path=logical_path,
            local_path=str(source.resolve()),
            total_bytes=stat.st_size,
            source_mtime_ns=stat.st_mtime_ns,
        )

    def queue_download(
        self,
        server_url: str,
        space_id: str,
        logical_path: str,
        destination: Path,
        *,
        total_bytes: int,
        expected_sha256: str,
    ) -> TransferRecord:
        return self._insert(
            kind="download",
            server_url=server_url,
            space_id=space_id,
            logical_path=logical_path,
            local_path=str(destination.resolve()),
            total_bytes=total_bytes,
            expected_sha256=expected_sha256,
        )

    def _insert(
        self,
        *,
        kind: str,
        server_url: str,
        space_id: str,
        logical_path: str,
        local_path: str,
        total_bytes: int,
        expected_sha256: str = "",
        source_mtime_ns: int = 0,
    ) -> TransferRecord:
        transfer_id = str(uuid.uuid4())
        now = _now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO transfers(
                    id, kind, server_url, space_id, logical_path, local_path, remote_session_id,
                    expected_sha256, total_bytes, transferred_bytes, source_mtime_ns,
                    status, error, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, '', ?, ?, 0, ?, 'queued', '', ?, ?)
                """,
                (
                    transfer_id,
                    kind,
                    server_url,
                    space_id,
                    logical_path,
                    local_path,
                    expected_sha256,
                    total_bytes,
                    source_mtime_ns,
                    now,
                    now,
                ),
            )
        return self.get(transfer_id)

    def get(self, transfer_id: str) -> TransferRecord:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM transfers WHERE id = ?", (transfer_id,)
            ).fetchone()
        if row is None:
            raise KeyError(transfer_id)
        return self._record(row)

    def list(self, *, limit: int = 100) -> list[TransferRecord]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM transfers ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._record(row) for row in rows]

    def queued(self, *, limit: int = 2) -> list[TransferRecord]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM transfers WHERE status = 'queued' "
                "ORDER BY created_at LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._record(row) for row in rows]

    def update_progress(
        self,
        transfer_id: str,
        transferred_bytes: int,
        *,
        remote_session_id: str | None = None,
        total_bytes: int | None = None,
    ) -> None:
        assignments = ["transferred_bytes = ?", "updated_at = ?"]
        parameters: list[object] = [max(0, transferred_bytes), _now()]
        if remote_session_id is not None:
            assignments.append("remote_session_id = ?")
            parameters.append(remote_session_id)
        if total_bytes is not None:
            assignments.append("total_bytes = ?")
            parameters.append(max(0, total_bytes))
        parameters.append(transfer_id)
        with self._connection() as connection:
            connection.execute(
                f"UPDATE transfers SET {', '.join(assignments)} WHERE id = ?",
                parameters,
            )

    def set_status(self, transfer_id: str, status: str, error: str = "") -> None:
        if status not in TRANSFER_STATUSES:
            raise ValueError("invalid transfer status")
        with self._connection() as connection:
            connection.execute(
                "UPDATE transfers SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                (status, error[:500], _now(), transfer_id),
            )

    def set_expected_sha256(self, transfer_id: str, sha256: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE transfers SET expected_sha256 = ?, updated_at = ? WHERE id = ?",
                (sha256.casefold(), _now(), transfer_id),
            )

    def clear_finished(self) -> int:
        with self._connection() as connection:
            deleted = connection.execute(
                "DELETE FROM transfers WHERE status IN ('completed', 'cancelled')"
            )
            return deleted.rowcount

    @staticmethod
    def _record(row: sqlite3.Row) -> TransferRecord:
        return TransferRecord(
            **{field: row[field] for field in TransferRecord.__dataclass_fields__}
        )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
