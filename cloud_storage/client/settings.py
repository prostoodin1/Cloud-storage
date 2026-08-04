from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(slots=True)
class ClientProfile:
    profile_id: str = "default"
    server_url: str = "http://127.0.0.1:8765"
    server_name: str = "Домашнее облако"
    certificate_fingerprint: str = ""
    device_id: str = ""
    device_name: str = ""
    device_status: str = "disconnected"
    username: str = ""
    download_directory: str = ""
    last_space_id: str = ""
    close_to_tray: bool = True
    drive_enabled: bool = True
    drive_letter: str = "S"

    @classmethod
    def from_dict(cls, value: dict | None) -> ClientProfile:
        value = value or {}
        allowed = cls.__dataclass_fields__
        fields = {
            key: (
                bool(value.get(key, field.default))
                if isinstance(field.default, bool)
                else str(value.get(key, field.default))
            )
            for key, field in allowed.items()
        }
        return cls(**fields)


def default_client_data_directory() -> Path:
    override = os.environ.get("CLOUD_STORAGE_CLIENT_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return local / "CloudStorageClient"
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "cloud-storage-client"


class ClientSettingsStore:
    def __init__(self, data_directory: Path | None = None) -> None:
        self.data_directory = data_directory or default_client_data_directory()
        self.path = self.data_directory / "client-settings.json"

    def load(self) -> ClientProfile:
        profiles, active_profile_id = self._load_document()
        return next(
            (item for item in profiles if item.profile_id == active_profile_id),
            profiles[0],
        )

    def list_profiles(self) -> list[ClientProfile]:
        profiles, _ = self._load_document()
        return profiles

    def add_profile(self) -> ClientProfile:
        profiles, _ = self._load_document()
        profile = ClientProfile(
            profile_id=uuid.uuid4().hex,
            server_name="Новый сервер",
            download_directory=str(default_download_directory()),
            drive_letter=_next_drive_letter({item.drive_letter for item in profiles}),
        )
        profiles.append(profile)
        self._write_document(profiles, profile.profile_id)
        return profile

    def set_active(self, profile_id: str) -> ClientProfile:
        profiles, _ = self._load_document()
        selected = next((item for item in profiles if item.profile_id == profile_id), None)
        if selected is None:
            raise KeyError(profile_id)
        self._write_document(profiles, selected.profile_id)
        return selected

    def remove_profile(self, profile_id: str) -> ClientProfile:
        profiles, active_profile_id = self._load_document()
        remaining = [item for item in profiles if item.profile_id != profile_id]
        if len(remaining) == len(profiles):
            raise KeyError(profile_id)
        if not remaining:
            remaining = [
                ClientProfile(download_directory=str(default_download_directory()))
            ]
        next_active = (
            active_profile_id
            if active_profile_id != profile_id
            and any(item.profile_id == active_profile_id for item in remaining)
            else remaining[0].profile_id
        )
        self._write_document(remaining, next_active)
        return next(item for item in remaining if item.profile_id == next_active)

    def _load_document(self) -> tuple[list[ClientProfile], str]:
        default = ClientProfile(download_directory=str(default_download_directory()))
        if not self.path.exists():
            return [default], default.profile_id
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("client settings root must be an object")
            raw_profiles = value.get("profiles")
            if isinstance(raw_profiles, list):
                profiles = [
                    ClientProfile.from_dict(item)
                    for item in raw_profiles
                    if isinstance(item, dict)
                ]
                profiles = self._normalize_profiles(profiles)
                active = str(value.get("active_profile_id", ""))
                if not any(item.profile_id == active for item in profiles):
                    active = profiles[0].profile_id
                return profiles, active
            migrated = ClientProfile.from_dict(value)
            migrated.profile_id = "default"
            if not migrated.download_directory:
                migrated.download_directory = str(default_download_directory())
            return [migrated], migrated.profile_id
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return [default], default.profile_id

    def save(self, profile: ClientProfile, *, make_active: bool = True) -> None:
        validate_server_url(profile.server_url)
        profiles, active_profile_id = self._load_document()
        for index, existing in enumerate(profiles):
            if existing.profile_id == profile.profile_id:
                profiles[index] = profile
                break
        else:
            profiles.append(profile)
        self._write_document(
            profiles,
            profile.profile_id if make_active else active_profile_id,
        )

    def _normalize_profiles(self, profiles: list[ClientProfile]) -> list[ClientProfile]:
        result: list[ClientProfile] = []
        identifiers: set[str] = set()
        drive_letters: set[str] = set()
        for profile in profiles:
            candidate = re.sub(r"[^a-zA-Z0-9_-]", "", profile.profile_id)[:64]
            if not candidate or candidate in identifiers:
                candidate = uuid.uuid4().hex
            profile.profile_id = candidate
            if not profile.download_directory:
                profile.download_directory = str(default_download_directory())
            letter = profile.drive_letter.strip().upper().rstrip(":")
            if not re.fullmatch(r"[D-Z]", letter) or letter in drive_letters:
                letter = _next_drive_letter(drive_letters)
            profile.drive_letter = letter
            drive_letters.add(letter)
            identifiers.add(candidate)
            result.append(profile)
        if not result:
            result.append(ClientProfile(download_directory=str(default_download_directory())))
        return result

    def _write_document(
        self, profiles: list[ClientProfile], active_profile_id: str
    ) -> None:
        profiles = self._normalize_profiles(profiles)
        if not any(item.profile_id == active_profile_id for item in profiles):
            active_profile_id = profiles[0].profile_id
        for profile in profiles:
            validate_server_url(profile.server_url)
        self.data_directory.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            prefix="client-settings-", suffix=".tmp", dir=self.data_directory
        )
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    {
                        "schema_version": 3,
                        "active_profile_id": active_profile_id,
                        "profiles": [asdict(item) for item in profiles],
                    },
                    handle,
                    ensure_ascii=False,
                    indent=2,
                )
                handle.flush()
                os.fsync(handle.fileno())
            if os.name != "nt":
                temporary.chmod(0o600)
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)


class DeviceTokenVault:
    def __init__(self, data_directory: Path, profile_id: str = "default") -> None:
        safe_profile_id = re.sub(r"[^a-zA-Z0-9_-]", "", profile_id)[:64]
        if not safe_profile_id:
            raise ValueError("invalid client profile id")
        self.profile_id = safe_profile_id
        self.path = data_directory / "tokens" / f"{safe_profile_id}.bin"
        self.legacy_path = data_directory / "device-token.bin"

    def store(self, token: str) -> None:
        if not token.startswith("csd_") or len(token) < 50:
            raise ValueError("invalid device token")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = token.encode("utf-8")
        if os.name == "nt":
            import win32crypt

            protected = win32crypt.CryptProtectData(
                payload,
                "Cloud Storage device token",
                None,
                None,
                None,
                0,
            )
            payload = protected[1] if isinstance(protected, tuple) else protected
        temporary = self.path.with_suffix(".tmp")
        temporary.write_bytes(payload)
        if os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, self.path)

    def load(self) -> str | None:
        source = self.path
        migrate_legacy = (
            self.profile_id == "default" and not source.exists() and self.legacy_path.exists()
        )
        if migrate_legacy:
            source = self.legacy_path
        if not source.exists():
            return None
        try:
            payload = source.read_bytes()
            if os.name == "nt":
                import win32crypt

                unprotected = win32crypt.CryptUnprotectData(payload, None, None, None, 0)
                payload = unprotected[1] if isinstance(unprotected, tuple) else unprotected
            token = payload.decode("utf-8")
            if not token.startswith("csd_") or len(token) < 50:
                return None
            if migrate_legacy:
                self.store(token)
                self.legacy_path.unlink(missing_ok=True)
            return token
        except (OSError, UnicodeDecodeError):
            return None

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
        if self.profile_id == "default":
            self.legacy_path.unlink(missing_ok=True)


class RemoteSessionVault:
    def __init__(self, data_directory: Path, profile_id: str = "default") -> None:
        safe_profile_id = re.sub(r"[^a-zA-Z0-9_-]", "", profile_id)[:64]
        if not safe_profile_id:
            raise ValueError("invalid client profile id")
        self.path = data_directory / "sessions" / f"{safe_profile_id}.bin"

    def store(self, token: str) -> None:
        if not token.startswith("css_") or len(token) < 80:
            raise ValueError("invalid internet session token")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = token.encode("utf-8")
        if os.name == "nt":
            import win32crypt

            protected = win32crypt.CryptProtectData(
                payload,
                "Cloud Storage internet session",
                None,
                None,
                None,
                0,
            )
            payload = protected[1] if isinstance(protected, tuple) else protected
        temporary = self.path.with_suffix(".tmp")
        temporary.write_bytes(payload)
        if os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, self.path)

    def load(self) -> str | None:
        if not self.path.exists():
            return None
        try:
            payload = self.path.read_bytes()
            if os.name == "nt":
                import win32crypt

                unprotected = win32crypt.CryptUnprotectData(payload, None, None, None, 0)
                payload = unprotected[1] if isinstance(unprotected, tuple) else unprotected
            token = payload.decode("utf-8")
            return token if token.startswith("css_") and len(token) >= 80 else None
        except (OSError, UnicodeDecodeError):
            return None

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


def validate_server_url(value: str) -> str:
    candidate = value.strip().rstrip("/")
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("server address must be an http:// or https:// URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("server address cannot contain credentials, query or fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError("server address must not contain a path")
    loopback = parsed.hostname.casefold() in {"127.0.0.1", "::1", "localhost"}
    if parsed.scheme != "https" and not loopback:
        raise ValueError("remote servers require HTTPS")
    return candidate


def default_download_directory() -> Path:
    downloads = Path.home() / "Downloads"
    base = downloads if downloads.is_dir() else Path.home()
    return base / "Cloud Storage"


def _next_drive_letter(used: set[str]) -> str:
    normalized = {value.strip().upper().rstrip(":") for value in used}
    preferred = "STUVWXYZRQPONMLKJIHGFED"
    return next((letter for letter in preferred if letter not in normalized), "S")
