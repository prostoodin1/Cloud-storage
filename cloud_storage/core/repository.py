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
    email: str
    role: str
    quota_bytes: int
    enabled: bool
    has_password: bool
    created_at: str


@dataclass(frozen=True, slots=True)
class SpaceRecord:
    id: str
    owner_user_id: str | None
    name: str
    kind: str
    quota_bytes: int
    permission: str | None = None
    can_read: bool = False
    can_upload: bool = False
    can_modify: bool = False
    can_delete: bool = False
    can_share: bool = False
    primary_storage_root_id: str | None = None
    fallback_storage_root_id: str | None = None
    enabled: bool = True
    created_at: str = ""

    def allows(self, capability: str) -> bool:
        return bool(getattr(self, f"can_{capability}", False))


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
        password: str | None = None,
        email: str = "",
        create_personal_space: bool = True,
        primary_storage_root_id: str | None = None,
        fallback_storage_root_id: str | None = None,
    ) -> tuple[UserRecord, SpaceRecord | None]:
        username = self.credentials.validate_username(username)
        display_name = display_name.strip()
        if not display_name or len(display_name) > 80:
            raise InvalidCredential("display name must contain 1-80 characters")
        if role not in {"admin", "member"}:
            raise InvalidCredential("invalid user role")
        if quota_bytes < 1024**3:
            raise InvalidCredential("quota must be at least 1 GiB")
        email = email.strip()
        if len(email) > 254 or (email and "@" not in email):
            raise InvalidCredential("invalid email address")
        if primary_storage_root_id and primary_storage_root_id == fallback_storage_root_id:
            raise InvalidCredential("primary and fallback storage roots must differ")
        password_hash = self.credentials.hash_password(password) if password is not None else None
        password_version = 1 if password_hash else 0
        user_id = str(uuid.uuid4())
        space_id = str(uuid.uuid4())
        created = utc_text()
        try:
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO users(
                        id, username, display_name, email, password_hash, password_version, role,
                        quota_bytes, enabled, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (
                        user_id,
                        username,
                        display_name,
                        email,
                        password_hash,
                        password_version,
                        role,
                        quota_bytes,
                        created,
                    ),
                )
                if create_personal_space:
                    connection.execute(
                        """
                        INSERT INTO spaces(
                            id, owner_user_id, name, kind, quota_bytes,
                            primary_storage_root_id, fallback_storage_root_id, created_at
                        ) VALUES(?, ?, 'Мои файлы', 'personal', ?, ?, ?, ?)
                        """,
                        (
                            space_id,
                            user_id,
                            quota_bytes,
                            primary_storage_root_id,
                            fallback_storage_root_id,
                            created,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO space_members(
                            space_id, user_id, permission, can_read, can_upload,
                            can_modify, can_delete, can_share
                        ) VALUES(?, ?, 'owner', 1, 1, 1, 1, 1)
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
        return (
            self.get_user(user_id),
            self.get_space(space_id, user_id) if create_personal_space else None,
        )

    def list_users(self) -> list[UserRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT id, username, display_name, email, role, quota_bytes, enabled,
                       password_hash IS NOT NULL AS has_password, created_at
                FROM users ORDER BY display_name COLLATE NOCASE
                """
            ).fetchall()
        return [self._user(row) for row in rows]

    def user_runtime_overview(self) -> dict[str, dict[str, Any]]:
        """Return Manager-facing presence and current activity without exposing credentials."""

        with self.database.connection() as connection:
            device_rows = connection.execute(
                """
                SELECT u.id AS user_id, u.enabled,
                       MAX(COALESCE(d.last_seen_at, d.approved_at, d.created_at)) AS last_login_at,
                       MAX(CASE WHEN d.status = 'trusted' THEN d.last_seen_at END) AS trusted_seen_at,
                       SUM(CASE WHEN d.status = 'trusted' THEN 1 ELSE 0 END) AS trusted_devices,
                       SUM(CASE WHEN d.status = 'pending' THEN 1 ELSE 0 END) AS pending_devices
                FROM users u LEFT JOIN devices d ON d.user_id = u.id
                GROUP BY u.id
                """
            ).fetchall()
            transfer_rows = connection.execute(
                """
                SELECT tj.user_id, tj.direction, tj.status, tj.logical_path, tj.staging_path,
                       tj.temporary_path, tj.storage_root_id, tj.object_path, tj.updated_at
                FROM transfer_jobs tj JOIN users u ON u.id = tj.user_id
                WHERE u.enabled = 1 AND tj.status IN ('receiving', 'moving', 'sending')
                ORDER BY tj.updated_at DESC
                """
            ).fetchall()
            deletion_rows = connection.execute(
                """
                SELECT actor_id, action, target_type, target_id, detail, timestamp
                FROM audit_events
                WHERE actor_type = 'user'
                  AND action IN ('file.deleted', 'directory.deleted')
                ORDER BY timestamp DESC LIMIT 200
                """
            ).fetchall()

        now = utc_now()
        result: dict[str, dict[str, Any]] = {}
        for row in device_rows:
            trusted_seen = self._parse_timestamp(row["trusted_seen_at"])
            age = (now - trusted_seen).total_seconds() if trusted_seen else None
            if not bool(row["enabled"]):
                state = "offline"
            elif age is not None and age <= 90:
                state = "connected"
            elif age is not None and age <= 300:
                state = "online"
            elif int(row["pending_devices"] or 0) > 0:
                state = "online"
            else:
                state = "offline"
            result[str(row["user_id"])] = {
                "last_login_at": row["last_login_at"],
                "presence_state": state,
                "presence_detail": {
                    "offline": "Не в сети",
                    "online": "В сети, ресурсы сейчас не используются",
                    "connected": "Подключён, активного ввода-вывода нет",
                }[state],
                "activity_detail": "",
                "trusted_devices": int(row["trusted_devices"] or 0),
                "pending_devices": int(row["pending_devices"] or 0),
            }

        # A deletion is intentionally retained for a few seconds so the Manager poll can show it.
        for row in deletion_rows:
            user_id = str(row["actor_id"] or "")
            if (
                not user_id
                or user_id not in result
                or result[user_id].get("presence_state") == "deleting"
            ):
                continue
            timestamp = self._parse_timestamp(row["timestamp"])
            if timestamp is None or (now - timestamp).total_seconds() > 15:
                continue
            detail = str(row["detail"] or "")
            object_name = detail.partition(":")[2].strip() or str(row["target_id"] or "объект")
            target = "Корзина" if row["action"] == "file.deleted" else "Удалено"
            result[user_id].update(
                {
                    "presence_state": "deleting",
                    "presence_detail": "Удаление",
                    "activity_detail": (
                        f"Удаление: {object_name} | Откуда: {object_name} -> Куда: {target}"
                    ),
                }
            )

        seen_transfers: set[str] = set()
        for row in transfer_rows:
            user_id = str(row["user_id"] or "")
            if (
                not user_id
                or user_id in seen_transfers
                or user_id not in result
                or result[user_id]["presence_state"] == "deleting"
            ):
                continue
            seen_transfers.add(user_id)
            status_name = str(row["status"])
            logical_path = str(row["logical_path"] or "файл")
            if status_name == "receiving":
                action, source = "Загрузка", "Клиент"
                destination = str(row["staging_path"] or row["temporary_path"] or "Сервер")
            elif status_name == "moving":
                action = "Перенос"
                source = str(row["staging_path"] or row["temporary_path"] or "Кэш")
                destination = str(row["object_path"] or row["storage_root_id"] or "Хранилище")
            else:
                action = "Скачивание"
                source = str(row["object_path"] or row["storage_root_id"] or "Хранилище")
                destination = "Клиент"
            result[user_id].update(
                {
                    "presence_state": "transferring",
                    "presence_detail": "Идёт передача данных",
                    "activity_detail": (
                        f"{action}: {logical_path} | Откуда: {source} -> Куда: {destination}"
                    ),
                }
            )
        return result

    @staticmethod
    def _parse_timestamp(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
        except ValueError:
            return None

    def get_user(self, user_id: str) -> UserRecord:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT id, username, display_name, email, role, quota_bytes, enabled,
                       password_hash IS NOT NULL AS has_password, created_at
                FROM users WHERE id = ?
                """,
                (user_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError("user not found")
        return self._user(row)

    def authenticate_user_password(self, username: str, password: str) -> UserRecord:
        username = self.credentials.validate_username(username)
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT id, username, display_name, email, password_hash, role,
                       quota_bytes, enabled, password_hash IS NOT NULL AS has_password, created_at
                FROM users WHERE username = ? COLLATE NOCASE
                """,
                (username,),
            ).fetchone()
        if (
            row is None
            or not row["enabled"]
            or not row["password_hash"]
            or not self.credentials.verify_password(row["password_hash"], password)
        ):
            raise PermissionDeniedError("invalid username or password")
        return self._user(row)

    def user_password_version(self, user_id: str) -> int:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT password_version FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("user not found")
        return int(row["password_version"])

    def set_user_password(self, user_id: str, password: str) -> UserRecord:
        password_hash = self.credentials.hash_password(password)
        with self.database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE users
                SET password_hash = ?, password_version = password_version + 1
                WHERE id = ?
                """,
                (password_hash, user_id),
            )
            if changed.rowcount != 1:
                raise NotFoundError("user not found")
            self._audit_tx(
                connection,
                actor_type="manager",
                actor_id=None,
                action="user.password.changed",
                target_type="user",
                target_id=user_id,
                detail="Пароль пользователя изменён; активные интернет-сессии отозваны",
            )
        return self.get_user(user_id)

    def set_user_enabled(self, user_id: str, enabled: bool) -> UserRecord:
        with self.database.transaction() as connection:
            changed = connection.execute(
                "UPDATE users SET enabled = ? WHERE id = ?",
                (int(enabled), user_id),
            )
            if changed.rowcount != 1:
                raise NotFoundError("user not found")
            if not enabled:
                connection.execute(
                    "UPDATE devices SET status = 'revoked' WHERE user_id = ? AND status != 'revoked'",
                    (user_id,),
                )
            self._audit_tx(
                connection,
                actor_type="manager",
                actor_id=None,
                action="user.enabled" if enabled else "user.disabled",
                target_type="user",
                target_id=user_id,
                detail="Учётная запись включена" if enabled else "Учётная запись отключена",
            )
        return self.get_user(user_id)

    def update_user(
        self,
        user_id: str,
        *,
        display_name: str,
        email: str,
        role: str,
        quota_bytes: int,
        enabled: bool,
    ) -> UserRecord:
        display_name = display_name.strip()
        email = email.strip()
        if not display_name or len(display_name) > 80:
            raise InvalidCredential("display name must contain 1-80 characters")
        if len(email) > 254 or (email and "@" not in email):
            raise InvalidCredential("invalid email address")
        if role not in {"admin", "member"}:
            raise InvalidCredential("invalid user role")
        if quota_bytes < 1024**3:
            raise InvalidCredential("quota must be at least 1 GiB")
        with self.database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE users
                SET display_name = ?, email = ?, role = ?, quota_bytes = ?, enabled = ?
                WHERE id = ?
                """,
                (display_name, email, role, quota_bytes, int(enabled), user_id),
            )
            if changed.rowcount != 1:
                raise NotFoundError("user not found")
            connection.execute(
                "UPDATE spaces SET quota_bytes = ? WHERE owner_user_id = ? AND kind = 'personal'",
                (quota_bytes, user_id),
            )
            if not enabled:
                connection.execute(
                    "UPDATE devices SET status = 'revoked' WHERE user_id = ? AND status != 'revoked'",
                    (user_id,),
                )
            self._audit_tx(
                connection,
                actor_type="manager",
                actor_id=None,
                action="user.updated",
                target_type="user",
                target_id=user_id,
                detail=f"Обновлены параметры пользователя {display_name}",
            )
        return self.get_user(user_id)

    def create_invitation(
        self,
        user_id: str,
        ttl_seconds: int,
        *,
        purpose: str = "legacy",
    ) -> tuple[str, str, str]:
        self.get_user(user_id)
        if purpose not in {"legacy", "access_package"}:
            raise ValueError("invalid invitation purpose")
        invitation_id = str(uuid.uuid4())
        maximum = 604800 if purpose == "access_package" else 3600
        expires = utc_now() + timedelta(seconds=max(60, min(ttl_seconds, maximum)))
        for _ in range(5):
            code = self.credentials.generate_pairing_code()
            try:
                with self.database.transaction() as connection:
                    connection.execute(
                        """
                        INSERT INTO invitations(
                            id, user_id, code_hash, purpose, expires_at, created_at
                        ) VALUES(?, ?, ?, ?, ?, ?)
                        """,
                        (
                            invitation_id,
                            user_id,
                            self.credentials.pairing_code_hash(code),
                            purpose,
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
        password: str | None,
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
                SELECT i.id, i.user_id, i.purpose, i.expires_at, i.consumed_at, i.cancelled_at,
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
            password_required = invitation["purpose"] != "access_package"
            if password_required and password_hash:
                if not password or not self.credentials.verify_password(password_hash, password):
                    raise InvalidCredential("invalid account password")
            elif password_required and not password_hash:
                if not password:
                    raise InvalidCredential("account password is required")
                password_hash = self.credentials.hash_password(password)
                connection.execute(
                    """
                    UPDATE users
                    SET password_hash = ?, password_version = password_version + 1
                    WHERE id = ?
                    """,
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

    def redeem_dynamic_pairing(
        self,
        *,
        device_name: str,
        platform: str,
        remote_address: str | None,
    ) -> PairingResult:
        """Create an approved passwordless account for a newly paired device."""
        device_name, platform = self._validate_device_identity(device_name, platform)
        token = self.credentials.generate_device_token()
        token_hash = self.credentials.device_token_hash(token)
        user_id = str(uuid.uuid4())
        device_id = str(uuid.uuid4())
        space_id = str(uuid.uuid4())
        username = f"device_{device_id.replace('-', '')[:16]}"
        created = utc_text()
        quota_bytes = 100 * 1024**3
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO users(
                    id, username, display_name, email, password_hash, password_version,
                    role, quota_bytes, enabled, created_at
                ) VALUES(?, ?, ?, '', NULL, 0, 'member', ?, 1, ?)
                """,
                (user_id, username, device_name, quota_bytes, created),
            )
            connection.execute(
                """
                INSERT INTO spaces(
                    id, owner_user_id, name, kind, quota_bytes,
                    primary_storage_root_id, fallback_storage_root_id, created_at
                ) VALUES(?, ?, 'Мои файлы', 'personal', ?, NULL, NULL, ?)
                """,
                (space_id, user_id, quota_bytes, created),
            )
            connection.execute(
                """
                INSERT INTO space_members(
                    space_id, user_id, permission, can_read, can_upload,
                    can_modify, can_delete, can_share
                ) VALUES(?, ?, 'owner', 1, 1, 1, 1, 1)
                """,
                (space_id, user_id),
            )
            connection.execute(
                """
                INSERT INTO devices(
                    id, user_id, name, platform, token_hash, status, created_at, approved_at
                ) VALUES(?, ?, ?, ?, ?, 'trusted', ?, ?)
                """,
                (device_id, user_id, device_name, platform, token_hash, created, created),
            )
            self._audit_tx(
                connection,
                actor_type="device",
                actor_id=device_id,
                action="device.dynamic_pairing.completed",
                target_type="user",
                target_id=user_id,
                detail=f"Устройство {device_name} подключено динамическим кодом",
                remote_address=remote_address,
            )
        return PairingResult(device=self.get_device(device_id), device_token=token)

    def revoke_unused_access_invitations(self, user_id: str) -> int:
        with self.database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE invitations SET cancelled_at = ?
                WHERE user_id = ? AND purpose = 'access_package'
                  AND consumed_at IS NULL AND cancelled_at IS NULL
                """,
                (utc_text(), user_id),
            )
        return int(changed.rowcount)

    def request_device_login(
        self,
        username: str,
        password: str,
        device_name: str,
        platform: str,
        remote_address: str | None,
    ) -> PairingResult:
        user = self.authenticate_user_password(username, password)
        device_name, platform = self._validate_device_identity(device_name, platform)
        token = self.credentials.generate_device_token()
        token_hash = self.credentials.device_token_hash(token)
        device_id = str(uuid.uuid4())
        created = utc_text()
        with self.database.transaction() as connection:
            pending = connection.execute(
                "SELECT count(*) FROM devices WHERE user_id = ? AND status = 'pending'",
                (user.id,),
            ).fetchone()[0]
            if pending >= 10:
                raise ConflictError("too many devices are waiting for approval")
            connection.execute(
                """
                UPDATE devices SET status = 'revoked'
                WHERE user_id = ? AND name = ? COLLATE NOCASE
                      AND platform = ? COLLATE NOCASE AND status = 'pending'
                """,
                (user.id, device_name, platform),
            )
            connection.execute(
                """
                INSERT INTO devices(
                    id, user_id, name, platform, token_hash, status, created_at
                ) VALUES(?, ?, ?, ?, ?, 'pending', ?)
                """,
                (device_id, user.id, device_name, platform, token_hash, created),
            )
            self._audit_tx(
                connection,
                actor_type="device",
                actor_id=device_id,
                action="device.login.requested",
                target_type="device",
                target_id=device_id,
                detail=f"Запрошен вход нового устройства {device_name} по логину и паролю",
                remote_address=remote_address,
            )
        return PairingResult(device=self.get_device(device_id), device_token=token)

    @staticmethod
    def _validate_device_identity(device_name: str, platform: str) -> tuple[str, str]:
        device_name = device_name.strip()
        platform = platform.strip()
        if not device_name or len(device_name) > 100:
            raise InvalidCredential("device name must contain 1-100 characters")
        if not platform or len(platform) > 50:
            raise InvalidCredential("platform must contain 1-50 characters")
        return device_name, platform

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

    @staticmethod
    def _normalize_capabilities(value: dict[str, bool]) -> dict[str, bool]:
        capabilities = {
            name: bool(value.get(name, False))
            for name in ("read", "upload", "modify", "delete", "share")
        }
        if any(capabilities.values()):
            capabilities["read"] = True
        return capabilities

    def create_shared_space(
        self,
        *,
        name: str,
        quota_bytes: int,
        primary_storage_root_id: str | None,
        fallback_storage_root_id: str | None,
    ) -> SpaceRecord:
        name = name.strip()
        if not name or len(name) > 80:
            raise InvalidCredential("space name must contain 1-80 characters")
        if quota_bytes < 1024**3:
            raise InvalidCredential("quota must be at least 1 GiB")
        if primary_storage_root_id and primary_storage_root_id == fallback_storage_root_id:
            raise InvalidCredential("primary and fallback storage roots must differ")
        space_id, created = str(uuid.uuid4()), utc_text()
        with self.database.transaction() as connection:
            for root_id in (primary_storage_root_id, fallback_storage_root_id):
                if root_id and connection.execute(
                    "SELECT 1 FROM storage_roots WHERE id = ?", (root_id,)
                ).fetchone() is None:
                    raise NotFoundError("storage root not found")
            connection.execute(
                """
                INSERT INTO spaces(
                    id, owner_user_id, name, kind, quota_bytes,
                    primary_storage_root_id, fallback_storage_root_id, enabled, created_at
                ) VALUES(?, NULL, ?, 'shared', ?, ?, ?, 1, ?)
                """,
                (
                    space_id,
                    name,
                    quota_bytes,
                    primary_storage_root_id,
                    fallback_storage_root_id,
                    created,
                ),
            )
            self._audit_tx(
                connection,
                actor_type="manager",
                actor_id=None,
                action="space.created",
                target_type="space",
                target_id=space_id,
                detail=f"Создано общее пространство {name}",
            )
        return self.get_space(space_id)

    def update_space(
        self,
        space_id: str,
        *,
        name: str,
        quota_bytes: int,
        primary_storage_root_id: str | None,
        fallback_storage_root_id: str | None,
        enabled: bool,
    ) -> SpaceRecord:
        current = self.get_space(space_id)
        if current.kind != "shared":
            raise ConflictError("personal spaces are managed through their owner")
        name = name.strip()
        if not name or len(name) > 80 or quota_bytes < 1024**3:
            raise InvalidCredential("invalid space settings")
        if primary_storage_root_id and primary_storage_root_id == fallback_storage_root_id:
            raise InvalidCredential("primary and fallback storage roots must differ")
        with self.database.transaction() as connection:
            for root_id in (primary_storage_root_id, fallback_storage_root_id):
                if root_id and connection.execute(
                    "SELECT 1 FROM storage_roots WHERE id = ?", (root_id,)
                ).fetchone() is None:
                    raise NotFoundError("storage root not found")
            connection.execute(
                """
                UPDATE spaces
                SET name = ?, quota_bytes = ?, primary_storage_root_id = ?,
                    fallback_storage_root_id = ?, enabled = ?
                WHERE id = ?
                """,
                (
                    name,
                    quota_bytes,
                    primary_storage_root_id,
                    fallback_storage_root_id,
                    int(enabled),
                    space_id,
                ),
            )
            self._audit_tx(
                connection,
                actor_type="manager",
                actor_id=None,
                action="space.updated",
                target_type="space",
                target_id=space_id,
                detail=f"Обновлено пространство {name}",
            )
        return self.get_space(space_id)

    def set_space_member(
        self,
        space_id: str,
        user_id: str,
        capabilities: dict[str, bool],
    ) -> None:
        space = self.get_space(space_id)
        self.get_user(user_id)
        if space.owner_user_id == user_id:
            raise ConflictError("owner permissions cannot be changed")
        values = self._normalize_capabilities(capabilities)
        with self.database.transaction() as connection:
            if not values["read"]:
                connection.execute(
                    "DELETE FROM space_members WHERE space_id = ? AND user_id = ?",
                    (space_id, user_id),
                )
                action = "space.member.removed"
            else:
                permission = (
                    "write"
                    if any(values[name] for name in ("upload", "modify", "delete", "share"))
                    else "read"
                )
                connection.execute(
                    """
                    INSERT INTO space_members(
                        space_id, user_id, permission, can_read, can_upload,
                        can_modify, can_delete, can_share
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(space_id, user_id) DO UPDATE SET
                        permission = excluded.permission,
                        can_read = excluded.can_read,
                        can_upload = excluded.can_upload,
                        can_modify = excluded.can_modify,
                        can_delete = excluded.can_delete,
                        can_share = excluded.can_share
                    """,
                    (
                        space_id,
                        user_id,
                        permission,
                        int(values["read"]),
                        int(values["upload"]),
                        int(values["modify"]),
                        int(values["delete"]),
                        int(values["share"]),
                    ),
                )
                action = "space.member.updated"
            self._audit_tx(
                connection,
                actor_type="manager",
                actor_id=None,
                action=action,
                target_type="space",
                target_id=space_id,
                detail=f"Обновлены права пользователя {user_id}",
            )

    def list_spaces_admin(self) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            spaces = connection.execute(
                """
                SELECT id, owner_user_id, name, kind, quota_bytes,
                       primary_storage_root_id, fallback_storage_root_id,
                       enabled, created_at
                FROM spaces ORDER BY CASE kind WHEN 'personal' THEN 0 ELSE 1 END, name
                """
            ).fetchall()
            members = connection.execute(
                """
                SELECT sm.space_id, sm.user_id, u.username, u.display_name,
                       sm.permission, sm.can_read, sm.can_upload, sm.can_modify,
                       sm.can_delete, sm.can_share
                FROM space_members sm JOIN users u ON u.id = sm.user_id
                ORDER BY u.display_name COLLATE NOCASE
                """
            ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in members:
            grouped.setdefault(str(row["space_id"]), []).append(
                {
                    "user_id": row["user_id"],
                    "username": row["username"],
                    "display_name": row["display_name"],
                    "permission": row["permission"],
                    "capabilities": {
                        name: bool(row[f"can_{name}"])
                        for name in ("read", "upload", "modify", "delete", "share")
                    },
                }
            )
        return [
            {
                "id": row["id"],
                "owner_user_id": row["owner_user_id"],
                "name": row["name"],
                "kind": row["kind"],
                "quota_bytes": row["quota_bytes"],
                "primary_storage_root_id": row["primary_storage_root_id"],
                "fallback_storage_root_id": row["fallback_storage_root_id"],
                "enabled": bool(row["enabled"]),
                "created_at": row["created_at"],
                "members": grouped.get(str(row["id"]), []),
            }
            for row in spaces
        ]

    def list_spaces_for_user(self, user_id: str) -> list[SpaceRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT s.id, s.owner_user_id, s.name, s.kind, s.quota_bytes,
                       sm.permission, sm.can_read, sm.can_upload, sm.can_modify,
                       sm.can_delete, sm.can_share, s.primary_storage_root_id,
                       s.fallback_storage_root_id, s.enabled, s.created_at
                FROM spaces s
                JOIN space_members sm ON sm.space_id = s.id
                WHERE sm.user_id = ? AND s.enabled = 1 AND sm.can_read = 1
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
                           sm.permission, sm.can_read, sm.can_upload, sm.can_modify,
                           sm.can_delete, sm.can_share, s.primary_storage_root_id,
                           s.fallback_storage_root_id, s.enabled, s.created_at
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
                           NULL AS permission, 0 AS can_read, 0 AS can_upload,
                           0 AS can_modify, 0 AS can_delete, 0 AS can_share,
                           primary_storage_root_id, fallback_storage_root_id,
                           enabled, created_at
                    FROM spaces WHERE id = ?
                    """,
                    (space_id,),
                ).fetchone()
        if row is None:
            raise NotFoundError("space not found")
        return self._space(row)

    def require_space_permission(
        self,
        space_id: str,
        user_id: str,
        write: bool = False,
        *,
        capability: str | None = None,
    ) -> SpaceRecord:
        space = self.get_space(space_id, user_id)
        required = capability or ("upload" if write else "read")
        if required not in {"read", "upload", "modify", "delete", "share"}:
            raise ValueError("invalid space capability")
        if not space.enabled or not space.allows(required):
            raise PermissionDeniedError(f"space does not allow {required} for this user")
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

    def recent_user_operations(self, user_id: str, limit: int = 100) -> list[dict[str, Any]]:
        self.get_user(user_id)
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT timestamp, action, target_type, detail
                FROM audit_events
                WHERE actor_type = 'user' AND actor_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (user_id, max(1, min(limit, 200))),
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
            email=row["email"],
            role=row["role"],
            quota_bytes=row["quota_bytes"],
            enabled=bool(row["enabled"]),
            has_password=bool(row["has_password"]),
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
            can_read=bool(row["can_read"]),
            can_upload=bool(row["can_upload"]),
            can_modify=bool(row["can_modify"]),
            can_delete=bool(row["can_delete"]),
            can_share=bool(row["can_share"]),
            primary_storage_root_id=row["primary_storage_root_id"],
            fallback_storage_root_id=row["fallback_storage_root_id"],
            enabled=bool(row["enabled"]),
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
