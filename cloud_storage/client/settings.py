from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(slots=True)
class ClientProfile:
    server_url: str = "http://127.0.0.1:8765"
    server_name: str = "Домашнее облако"
    certificate_fingerprint: str = ""
    device_id: str = ""
    device_name: str = ""
    device_status: str = "disconnected"
    download_directory: str = ""
    last_space_id: str = ""
    close_to_tray: bool = True

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
        if not self.path.exists():
            return ClientProfile(download_directory=str(default_download_directory()))
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("client settings root must be an object")
            result = ClientProfile.from_dict(value)
            if not result.download_directory:
                result.download_directory = str(default_download_directory())
            return result
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return ClientProfile(download_directory=str(default_download_directory()))

    def save(self, profile: ClientProfile) -> None:
        validate_server_url(profile.server_url)
        self.data_directory.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            prefix="client-settings-", suffix=".tmp", dir=self.data_directory
        )
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(asdict(profile), handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            if os.name != "nt":
                temporary.chmod(0o600)
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)


class DeviceTokenVault:
    def __init__(self, data_directory: Path) -> None:
        self.path = data_directory / "device-token.bin"

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
        if not self.path.exists():
            return None
        try:
            payload = self.path.read_bytes()
            if os.name == "nt":
                import win32crypt

                unprotected = win32crypt.CryptUnprotectData(payload, None, None, None, 0)
                payload = unprotected[1] if isinstance(unprotected, tuple) else unprotected
            token = payload.decode("utf-8")
            return token if token.startswith("csd_") and len(token) >= 50 else None
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
