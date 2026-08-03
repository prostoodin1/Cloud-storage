from __future__ import annotations

import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cloud_storage.core.database import Database
from cloud_storage.core.security import CredentialService, InvalidCredential


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_text(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat(timespec="seconds")


class NotFoundError(LookupError):
    pass


class ConflictError(ValueError):
    pass


class PermissionDeniedError(PermissionError):
    pass


@dataclass(frozen=True, slots=True)
class UserRecord:
    id: str
    username: str
    display_name: str
    role: str
    quota_bytes: int
    enabled: bool
    created_at: str


@dataclass(frozen=True, slots=True)
class SpaceRecord:
    id: str
    owner_user_id: str | None
    name: str
    kind: str
    quota_bytes: int
    permission: str | None = None
    created_at: str = ""


@dataclass(frozen=True, slots=True)
class DeviceRecord:
    id: str
    user_id: str
    username: str
    user_display_name: str
    name: str
    platform: str
    status: str
    created_at: str
    approved_at: str | None
    last_seen_at: str | None


@dataclass(frozen=True, slots=True)
class PairingResult:
    device: DeviceRecord
    device_token: str


class CoreRepository:
    def __init__(self, database: Database, credentials: CredentialService) -> None:
        self.database = database
        self.credentials = credentials

    def initialize_default_storage(self, path: Path) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO storage_roots(
                    id, disk_id, path, enabled, priority, max_fill_percent,
                    min_free_bytes, created_at
                ) VALUES(?, NULL, ?, 1, 50, 90, ?, ?)
                """,
                ("default", str(path.resolve()), 10 * 1024**3, utc_text()),
            )

    def create_user(
        self,
        username: str,
        display_name: str,
        quota_bytes: int,
        role: str = "member",
    ) -> tuple[UserRecord, SpaceRecord]:
        username = self.credentials.validate_username(username)
        display_name = display_name.strip()
        if not display_name or len(display_name) > 80:
            raise InvalidCredential("display name must contain 1-80 characters")
        if role not in {"admin", "member"}:
            raise InvalidCredential("invalid user role")
        if quota_bytes < 1024**3:
            raise InvalidCredential("quota must be at least 1 GiB")
        user_id = str(uuid.uuid4())
        space_id = str(uuid.uuid4())
        created = utc_text()
        try:
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO users(
                        id, username, display_name, password_hash, role,
                        quota_bytes, enabled, created_at
                    ) VALUES(?, ?, ?, NULL, ?, ?, 1, ?)
                    """,
                    (user_id, username, display_name, role, quota_bytes, created),
                )
                connection.execute(
                    """
                    INSERT INTO spaces(id, owner_user_id, name, kind, quota_bytes, created_at)
                    VALUES(?, ?, 'Мои файлы', 'personal', ?, ?)
                    """,
                    (space_id, user_id, quota_bytes, created),
                )
                connection.execute(
                    """
                    INSERT INTO space_members(space_id, user_id, permission)
                    VALUES(?, ?, 'owner')
                    """,
                    (space_id, user_id),
                )
                self._audit_tx(
                    connection,
                    actor_type="manager",
                    actor_id=None,
                    action="user.created",
                    target_type="user",
                    target_id=user_id,
                    detail=f"Создан пользователь {display_name} ({username})",
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("username already exists") from exc
        return self.get_user(user_id), self.get_space(space_id, user_id)

    def list_users(self) -> list[UserRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT id, username, display_name, role, quota_bytes, enabled, created_at
                FROM users ORDER BY display_name COLLATE NOCASE
                """
            ).fetchall()
        return [self._user(row) for row in rows]

    def get_user(self, user_id: str) -> UserRecord:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT id, username, display_name, role, quota_bytes, enabled, created_at
                FROM users WHERE id = ?
                """,
                (user_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError("user not found")
        return self._user(row)

    def create_invitation(self, user_id: str, ttl_seconds: int) -> tuple[str, str, str]:
        self.get_user(user_id)
        invitation_id = str(uuid.uuid4())
        expires = utc_now() + timedelta(seconds=max(60, min(ttl_seconds, 3600)))
        for _ in range(5):
            code = self.credentials.generate_pairing_code()
            try:
                with self.database.transaction() as connection:
                    connection.execute(
                        """
                        INSERT INTO invitations(
                            id, user_id, code_hash, expires_at, created_at
                        ) VALUES(?, ?, ?, ?, ?)
                        """,
                        (
                            invitation_id,
                            user_id,
                            self.credentials.pairing_code_hash(code),
                            utc_text(expires),
                            utc_text(),
                        ),
                    )
                    self._audit_tx(
                        connection,
                        actor_type="manager",
                        actor_id=None,
                        action="invitation.created",
                        target_type="user",
                        target_id=user_id,
                        detail="Создано одноразовое приглашение",
                    )
                return invitation_id, code, utc_text(expires)
            except sqlite3.IntegrityError:
                continue
        raise RuntimeError("could not generate a unique invitation")

    def redeem_invitation(
        self,
        code: str,
        password: str,
        device_name: str,
        platform: str,
        remote_address: str | None,
    ) -> PairingResult:
        device_name = device_name.strip()
        platform = platform.strip()
        if not device_name or len(device_name) > 100:
            raise InvalidCredential("device name must contain 1-100 characters")
        if not platform or len(platform) > 50:
            raise InvalidCredential("platform must contain 1-50 characters")
        code_hash = self.credentials.pairing_code_hash(code)
        token = self.credentials.generate_device_token()
        token_hash = self.credentials.device_token_hash(token)
        device_id = str(uuid.uuid4())
        now = utc_now()
        with self.database.transaction() as connection:
            invitation = connection.execute(
                """
                SELECT i.id, i.user_id, i.expires_at, i.consumed_at, i.cancelled_at,
                       u.password_hash, u.enabled
                FROM invitations i
                JOIN users u ON u.id = i.user_id
                WHERE i.code_hash = ?
                """,
                (code_hash,),
            ).fetchone()
            if invitation is None:
                raise InvalidCredential("invitation is invalid or expired")
            expires = datetime.fromisoformat(invitation["expires_at"])
            if (
                invitation["consumed_at"]
                or invitation["cancelled_at"]
                or expires <= now
                or not invitation["enabled"]
            ):
                raise InvalidCredential("invitation is invalid or expired")
            password_hash = invitation["password_hash"]
            if password_hash:
                if not self.credentials.verify_password(password_hash, password):
                    raise InvalidCredential("invalid account password")
            else:
                password_hash = self.credentials.hash_password(password)
                connection.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ?",
                    (password_hash, invitation["user_id"]),
                )
            updated = connection.execute(
                """
                UPDATE invitations SET consumed_at = ?
                WHERE id = ? AND consumed_at IS NULL AND cancelled_at IS NULL
                """,
                (utc_text(now), invitation["id"]),
            )
            if updated.rowcount != 1:
                raise ConflictError("invitation was already used")
            connection.execute(
                """
                INSERT INTO devices(
                    id, user_id, name, platform, token_hash, status, created_at
                ) VALUES(?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    device_id,
                    invitation["user_id"],
                    device_name,
                    platform,
                    token_hash,
                    utc_text(now),
                ),
            )
            self._audit_tx(
                connection,
                actor_type="device",
                actor_id=device_id,
                action="device.pairing.requested",
                target_type="device",
                target_id=device_id,
                detail=f"Запрошено подключение устройства {device_name}",
                remote_address=remote_address,
            )
        return PairingResult(device=self.get_device(device_id), device_token=token)

    def cancel_invitation(self, invitation_id: str) -> bool:
        with self.database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE invitations SET cancelled_at = ?
                WHERE id = ? AND consumed_at IS NULL AND cancelled_at IS NULL
                """,
                (utc_text(), invitation_id),
            )
            if changed.rowcount:
                self._audit_tx(
                    connection,
                    actor_type="manager",
                    actor_id=None,
                    action="invitation.cancelled",
                    target_type="invitation",
                    target_id=invitation_id,
                    detail="Одноразовое приглашение отменено",
                )
        return bool(changed.rowcount)

    def authenticate_device(self, token: str, allow_pending: bool = False) -> DeviceRecord:
        token_hash = self.credentials.device_token_hash(token)
        with self.database.transaction() as connection:
            row = self._device_query(connection, "d.token_hash = ?", (token_hash,))
            if row is None or row["status"] == "revoked":
                raise PermissionDeniedError("device token is invalid")
            if row["status"] != "trusted" and not allow_pending:
                raise PermissionDeniedError("device is waiting for administrator approval")
            connection.execute(
                "UPDATE devices SET last_seen_at = ? WHERE id = ?",
                (utc_text(), row["id"]),
            )
        return self._device(row)

    def list_devices(self) -> list[DeviceRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                self._device_select() + " ORDER BY d.created_at DESC"
            ).fetchall()
        return [self._device(row) for row in rows]

    def get_device(self, device_id: str) -> DeviceRecord:
        with self.database.connection() as connection:
            row = self._device_query(connection, "d.id = ?", (device_id,))
        if row is None:
            raise NotFoundError("device not found")
        return self._device(row)

    def set_device_status(self, device_id: str, status: str) -> DeviceRecord:
        if status not in {"trusted", "revoked"}:
            raise ValueError("invalid device status")
        with self.database.transaction() as connection:
            timestamp = utc_text() if status == "trusted" else None
            changed = connection.execute(
                "UPDATE devices SET status = ?, approved_at = COALESCE(approved_at, ?) WHERE id = ?",
                (status, timestamp, device_id),
            )
            if changed.rowcount != 1:
                raise NotFoundError("device not found")
            self._audit_tx(
                connection,
                actor_type="manager",
                actor_id=None,
                action=f"device.{status}",
                target_type="device",
                target_id=device_id,
                detail="Устройство подтверждено" if status == "trusted" else "Устройство отозвано",
            )
        return self.get_device(device_id)

    def list_spaces_for_user(self, user_id: str) -> list[SpaceRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT s.id, s.owner_user_id, s.name, s.kind, s.quota_bytes,
                       sm.permission, s.created_at
                FROM spaces s
                JOIN space_members sm ON sm.space_id = s.id
                WHERE sm.user_id = ?
                ORDER BY CASE s.kind WHEN 'personal' THEN 0 ELSE 1 END, s.name
                """,
                (user_id,),
            ).fetchall()
        return [self._space(row) for row in rows]

    def get_space(self, space_id: str, user_id: str | None = None) -> SpaceRecord:
        with self.database.connection() as connection:
            if user_id:
                row = connection.execute(
                    """
                    SELECT s.id, s.owner_user_id, s.name, s.kind, s.quota_bytes,
                           sm.permission, s.created_at
                    FROM spaces s
                    JOIN space_members sm ON sm.space_id = s.id
                    WHERE s.id = ? AND sm.user_id = ?
                    """,
                    (space_id, user_id),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT id, owner_user_id, name, kind, quota_bytes,
                           NULL AS permission, created_at
                    FROM spaces WHERE id = ?
                    """,
                    (space_id,),
                ).fetchone()
        if row is None:
            raise NotFoundError("space not found")
        return self._space(row)

    def require_space_permission(self, space_id: str, user_id: str, write: bool) -> SpaceRecord:
        space = self.get_space(space_id, user_id)
        if write and space.permission not in {"write", "owner"}:
            raise PermissionDeniedError("space is read-only for this user")
        return space

    def summary(self) -> dict[str, Any]:
        with self.database.connection() as connection:
            users = connection.execute("SELECT count(*) FROM users WHERE enabled = 1").fetchone()[0]
            devices = connection.execute(
                "SELECT count(*) FROM devices WHERE status = 'trusted'"
            ).fetchone()[0]
            pending = connection.execute(
                "SELECT count(*) FROM devices WHERE status = 'pending'"
            ).fetchone()[0]
            files = connection.execute(
                "SELECT count(*), COALESCE(sum(size_bytes), 0) FROM files WHERE deleted_at IS NULL"
            ).fetchone()
        return {
            "users": users,
            "trusted_devices": devices,
            "pending_devices": pending,
            "files": files[0],
            "stored_bytes": files[1],
        }

    def recent_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT id, timestamp, actor_type, actor_id, action, target_type,
                       target_id, detail, remote_address
                FROM audit_events ORDER BY id DESC LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_audit(
        self,
        *,
        actor_type: str,
        actor_id: str | None,
        action: str,
        target_type: str | None,
        target_id: str | None,
        detail: str,
        remote_address: str | None = None,
    ) -> None:
        with self.database.connection() as connection:
            self._audit_tx(
                connection,
                actor_type=actor_type,
                actor_id=actor_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                detail=detail,
                remote_address=remote_address,
            )

    @staticmethod
    def _user(row: sqlite3.Row) -> UserRecord:
        return UserRecord(
            id=row["id"],
            username=row["username"],
            display_name=row["display_name"],
            role=row["role"],
            quota_bytes=row["quota_bytes"],
            enabled=bool(row["enabled"]),
            created_at=row["created_at"],
        )

    @staticmethod
    def _space(row: sqlite3.Row) -> SpaceRecord:
        return SpaceRecord(
            id=row["id"],
            owner_user_id=row["owner_user_id"],
            name=row["name"],
            kind=row["kind"],
            quota_bytes=row["quota_bytes"],
            permission=row["permission"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _device_select() -> str:
        return """
            SELECT d.id, d.user_id, u.username, u.display_name AS user_display_name,
                   d.name, d.platform, d.status, d.created_at, d.approved_at, d.last_seen_at
            FROM devices d JOIN users u ON u.id = d.user_id
        """

    def _device_query(
        self, connection: sqlite3.Connection, where: str, parameters: tuple[Any, ...]
    ) -> sqlite3.Row | None:
        return connection.execute(self._device_select() + " WHERE " + where, parameters).fetchone()

    @staticmethod
    def _device(row: sqlite3.Row) -> DeviceRecord:
        return DeviceRecord(
            id=row["id"],
            user_id=row["user_id"],
            username=row["username"],
            user_display_name=row["user_display_name"],
            name=row["name"],
            platform=row["platform"],
            status=row["status"],
            created_at=row["created_at"],
            approved_at=row["approved_at"],
            last_seen_at=row["last_seen_at"],
        )

    @staticmethod
    def _audit_tx(
        connection: sqlite3.Connection,
        *,
        actor_type: str,
        actor_id: str | None,
        action: str,
        target_type: str | None,
        target_id: str | None,
        detail: str,
        remote_address: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_events(
                timestamp, actor_type, actor_id, action, target_type,
                target_id, detail, remote_address
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_text(),
                actor_type,
                actor_id,
                action,
                target_type,
                target_id,
                detail,
                remote_address,
            ),
        )

    @staticmethod
    def to_dict(record: Any) -> dict[str, Any]:
        return asdict(record)
