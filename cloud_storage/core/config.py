from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CoreConfig:
    data_directory: Path
    host: str = "127.0.0.1"
    port: int = 8765
    lan_enabled: bool = False
    lan_host: str = "0.0.0.0"
    lan_port: int = 8766
    discovery_port: int = 47777
    server_name: str = "Домашнее облако"
    max_upload_bytes: int = 20 * 1024**3
    pairing_ttl_seconds: int = 15 * 60

    def __post_init__(self) -> None:
        if self.host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("the administrative API must bind to loopback")
        if not 1024 <= self.port <= 65535:
            raise ValueError("core port must be between 1024 and 65535")
        if self.lan_host not in {"0.0.0.0", "::"}:
            raise ValueError("LAN host must bind all local interfaces")
        if not 1024 <= self.lan_port <= 65535 or self.lan_port == self.port:
            raise ValueError("LAN port must be different and between 1024 and 65535")
        if not 1024 <= self.discovery_port <= 65535:
            raise ValueError("discovery port must be between 1024 and 65535")
        if not self.server_name.strip():
            raise ValueError("server name cannot be empty")

    @classmethod
    def from_environment(cls) -> CoreConfig:
        data_directory = default_core_data_directory()
        host = os.environ.get("CLOUD_STORAGE_CORE_HOST", "127.0.0.1")
        port = int(os.environ.get("CLOUD_STORAGE_CORE_PORT", "8765"))
        lan_enabled = _environment_bool("CLOUD_STORAGE_LAN_ENABLED", False)
        lan_host = os.environ.get("CLOUD_STORAGE_LAN_HOST", "0.0.0.0")
        lan_port = int(os.environ.get("CLOUD_STORAGE_LAN_PORT", "8766"))
        discovery_port = int(os.environ.get("CLOUD_STORAGE_DISCOVERY_PORT", "47777"))
        server_name = os.environ.get("CLOUD_STORAGE_SERVER_NAME", "Домашнее облако").strip()
        max_upload_gib = int(os.environ.get("CLOUD_STORAGE_MAX_UPLOAD_GIB", "20"))
        if not server_name:
            server_name = "Домашнее облако"
        return cls(
            data_directory=data_directory,
            host=host,
            port=port,
            lan_enabled=lan_enabled,
            lan_host=lan_host,
            lan_port=lan_port,
            discovery_port=discovery_port,
            server_name=server_name[:80],
            max_upload_bytes=max(1, max_upload_gib) * 1024**3,
        )

    @property
    def database_path(self) -> Path:
        return self.data_directory / "core.db"

    @property
    def secrets_path(self) -> Path:
        return self.data_directory / "core-secrets.json"

    @property
    def pid_path(self) -> Path:
        return self.data_directory / "core.pid"

    @property
    def log_path(self) -> Path:
        return self.data_directory / "core.log"

    @property
    def tls_certificate_path(self) -> Path:
        return self.data_directory / "tls" / "server-cert.pem"

    @property
    def tls_private_key_path(self) -> Path:
        return self.data_directory / "tls" / "server-key.pem"

    @property
    def default_storage_root(self) -> Path:
        return self.data_directory / "storage" / "default"

    def ensure_directories(self) -> None:
        self.data_directory.mkdir(parents=True, exist_ok=True)
        (self.data_directory / "tls").mkdir(parents=True, exist_ok=True)
        self.default_storage_root.mkdir(parents=True, exist_ok=True)
        (self.default_storage_root / ".staging").mkdir(exist_ok=True)
        (self.default_storage_root / ".trash").mkdir(exist_ok=True)
        (self.default_storage_root / "objects").mkdir(exist_ok=True)
        if os.name != "nt":
            self.data_directory.chmod(0o700)
            self.default_storage_root.chmod(0o700)


def _environment_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def default_core_data_directory() -> Path:
    override = os.environ.get("CLOUD_STORAGE_CORE_DATA_DIR") or os.environ.get(
        "CLOUD_STORAGE_CONFIG_DIR"
    )
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        program_data = os.environ.get("PROGRAMDATA")
        if program_data:
            return Path(program_data) / "CloudStorage"
        return Path.home() / "AppData" / "Local" / "CloudStorageCore"
    if os.environ.get("CLOUD_STORAGE_SERVICE") == "1":
        return Path("/var/lib/cloud-storage")
    data_home = os.environ.get("XDG_DATA_HOME")
    return (
        Path(data_home) / "cloud-storage"
        if data_home
        else Path.home() / ".local" / "share" / "cloud-storage"
    )


@dataclass(frozen=True, slots=True)
class CoreSecrets:
    manager_token: str
    hmac_secret: str

    @classmethod
    def load_or_create(cls, config: CoreConfig) -> CoreSecrets:
        config.ensure_directories()
        if config.secrets_path.exists():
            try:
                payload = json.loads(config.secrets_path.read_text(encoding="utf-8"))
                manager_token = str(payload["manager_token"])
                hmac_secret = str(payload["hmac_secret"])
                if len(manager_token) >= 32 and len(hmac_secret) >= 32:
                    _restrict_secret_file(config.secrets_path)
                    return cls(manager_token=manager_token, hmac_secret=hmac_secret)
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    "core secrets file is damaged; refusing to replace credentials"
                ) from exc

        result = cls(
            manager_token=secrets.token_urlsafe(48),
            hmac_secret=secrets.token_hex(48),
        )
        temporary = config.secrets_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "manager_token": result.manager_token,
                    "hmac_secret": result.hmac_secret,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        if os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, config.secrets_path)
        _restrict_secret_file(config.secrets_path)
        return result


def _restrict_secret_file(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o600)
        return
    try:
        import ntsecuritycon
        import win32api
        import win32security

        user_name = win32api.GetUserName()
        user_sid, _, _ = win32security.LookupAccountName(None, user_name)
        system_sid = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)
        dacl = win32security.ACL()
        dacl.AddAccessAllowedAce(
            win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, user_sid
        )
        dacl.AddAccessAllowedAce(
            win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, system_sid
        )
        win32security.SetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION
            | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            dacl,
            None,
        )
    except (ImportError, OSError):
        # The API still binds to loopback; packaging includes pywin32 on Windows.
        # Source-only environments without it retain the user's inherited ACL.
        return
