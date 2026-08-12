from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class DiskRole(StrEnum):
    UNCONFIGURED = "unconfigured"
    SHARED = "shared"
    PERSONAL = "personal"
    SHARED_FOLDERS = "shared_folders"
    BACKUP = "backup"
    OVERFLOW = "overflow"
    MIRROR = "mirror"
    ARCHIVE = "archive"
    TEMPORARY = "temporary"
    CACHE = "cache"
    UNUSED = "unused"


class DiskMode(StrEnum):
    ACTIVE = "active"
    WRITES_PAUSED = "writes_paused"
    MAINTENANCE = "maintenance"
    DISCONNECTED = "disconnected"


class DiskStatus(StrEnum):
    HEALTHY = "healthy"
    ATTENTION = "attention"
    CHECK_RECOMMENDED = "check_recommended"
    DATA_LOSS_RISK = "data_loss_risk"
    ALMOST_FULL = "almost_full"
    WRITES_PAUSED = "writes_paused"
    MAINTENANCE = "maintenance"
    DISCONNECTED = "disconnected"
    UNAVAILABLE = "unavailable"
    UNCONFIGURED = "unconfigured"


@dataclass(slots=True)
class DiskSnapshot:
    id: str
    mountpoint: str
    device: str
    label: str = ""
    model: str | None = None
    serial: str | None = None
    interface: str | None = None
    filesystem: str | None = None
    total_bytes: int = 0
    used_bytes: int = 0
    free_bytes: int = 0
    temperature_c: float | None = None
    read_speed_mbps: float | None = None
    write_speed_mbps: float | None = None
    utilization_percent: float | None = None
    power_on_hours: int | None = None
    health_detail: str | None = None
    available: bool = True

    @property
    def used_percent(self) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return min(100.0, max(0.0, self.used_bytes / self.total_bytes * 100))


@dataclass(slots=True)
class DiskConfiguration:
    display_name: str = ""
    role: DiskRole = DiskRole.UNCONFIGURED
    mode: DiskMode = DiskMode.ACTIVE
    write_priority: int = 50
    max_fill_percent: int = 90
    min_free_gib: int = 10
    allowed_users: list[str] = field(default_factory=list)
    allowed_folders: list[str] = field(default_factory=list)
    auto_move_allowed: bool = False
    read_only: bool = False
    encryption_requested: bool = False
    check_schedule: str = "Еженедельно"
    maintenance_schedule: str = "Вручную"
    last_check_at: str | None = None
    identity_mountpoint: str = ""
    identity_device: str = ""
    identity_serial: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> DiskConfiguration:
        value = value or {}
        allowed = {item.name for item in cls.__dataclass_fields__.values()}
        safe = {key: val for key, val in value.items() if key in allowed}
        try:
            safe["role"] = DiskRole(safe.get("role", DiskRole.UNCONFIGURED))
        except ValueError:
            safe["role"] = DiskRole.UNCONFIGURED
        try:
            safe["mode"] = DiskMode(safe.get("mode", DiskMode.ACTIVE))
        except ValueError:
            safe["mode"] = DiskMode.ACTIVE
        return cls(**safe)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class AppSettings:
    schema_version: int = 5
    server_name: str = "Домашнее облако"
    setup_complete: bool = False
    setup_reminded_later: bool = False
    advanced_mode: bool = False
    notifications_enabled: bool = True
    refresh_interval_seconds: int = 30
    lan_enabled: bool = False
    lan_port: int = 8766
    remote_enabled: bool = False
    remote_port: int = 8767
    remote_public_url: str = ""
    remote_pairing_enabled: bool = False
    zrok_enabled: bool = False
    zrok_port: int = 8768
    zrok_executable: str = "zrok"
    zrok_share_name: str = ""
    known_disk_ids: list[str] = field(default_factory=list)
    ignored_disk_ids: list[str] = field(default_factory=list)
    disk_configurations: dict[str, DiskConfiguration] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> AppSettings:
        value = value or {}
        result = cls()
        result.schema_version = 5
        result.server_name = str(value.get("server_name", result.server_name))[:80]
        result.setup_complete = bool(value.get("setup_complete", False))
        result.setup_reminded_later = bool(value.get("setup_reminded_later", False))
        result.advanced_mode = bool(value.get("advanced_mode", False))
        result.notifications_enabled = bool(value.get("notifications_enabled", True))
        interval = int(value.get("refresh_interval_seconds", 30))
        result.refresh_interval_seconds = min(300, max(10, interval))
        result.lan_enabled = bool(value.get("lan_enabled", False))
        lan_port = int(value.get("lan_port", 8766))
        result.lan_port = lan_port if 1024 <= lan_port <= 65535 and lan_port != 8765 else 8766
        result.remote_enabled = bool(value.get("remote_enabled", False))
        remote_port = int(value.get("remote_port", 8767))
        fallback_remote_port = 8768 if result.lan_port == 8767 else 8767
        result.remote_port = (
            remote_port
            if 1024 <= remote_port <= 65535 and remote_port not in {8765, result.lan_port}
            else fallback_remote_port
        )
        result.remote_public_url = str(value.get("remote_public_url", "")).strip()[:2048]
        result.remote_pairing_enabled = bool(value.get("remote_pairing_enabled", False))
        result.zrok_enabled = bool(value.get("zrok_enabled", False))
        zrok_port = int(value.get("zrok_port", 8768))
        used_ports = {8765}
        if result.lan_enabled:
            used_ports.add(result.lan_port)
        if result.remote_enabled:
            used_ports.add(result.remote_port)
        result.zrok_port = (
            zrok_port if 1024 <= zrok_port <= 65535 and zrok_port not in used_ports else 8768
        )
        result.zrok_executable = str(value.get("zrok_executable", "zrok")).strip()[:2048]
        if not result.zrok_executable:
            result.zrok_executable = "zrok"
        result.zrok_share_name = str(value.get("zrok_share_name", "")).strip()[:63]
        result.known_disk_ids = [str(item) for item in value.get("known_disk_ids", [])]
        result.ignored_disk_ids = [str(item) for item in value.get("ignored_disk_ids", [])]
        configs = value.get("disk_configurations", {})
        if isinstance(configs, dict):
            result.disk_configurations = {
                str(key): DiskConfiguration.from_dict(item)
                for key, item in configs.items()
                if isinstance(item, dict)
            }
        return result

    def configuration_for(self, disk_id: str) -> DiskConfiguration:
        if disk_id not in self.disk_configurations:
            self.disk_configurations[disk_id] = DiskConfiguration()
        return self.disk_configurations[disk_id]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "server_name": self.server_name,
            "setup_complete": self.setup_complete,
            "setup_reminded_later": self.setup_reminded_later,
            "advanced_mode": self.advanced_mode,
            "notifications_enabled": self.notifications_enabled,
            "refresh_interval_seconds": self.refresh_interval_seconds,
            "lan_enabled": self.lan_enabled,
            "lan_port": self.lan_port,
            "remote_enabled": self.remote_enabled,
            "remote_port": self.remote_port,
            "remote_public_url": self.remote_public_url,
            "remote_pairing_enabled": self.remote_pairing_enabled,
            "zrok_enabled": self.zrok_enabled,
            "zrok_port": self.zrok_port,
            "zrok_executable": self.zrok_executable,
            "zrok_share_name": self.zrok_share_name,
            "known_disk_ids": self.known_disk_ids,
            "ignored_disk_ids": self.ignored_disk_ids,
            "disk_configurations": {
                key: config.to_dict() for key, config in self.disk_configurations.items()
            },
        }


ROLE_LABELS: dict[DiskRole, str] = {
    DiskRole.UNCONFIGURED: "Не настроен",
    DiskRole.SHARED: "Общее хранилище",
    DiskRole.PERSONAL: "Личное пространство",
    DiskRole.SHARED_FOLDERS: "Общие папки",
    DiskRole.BACKUP: "Резервные копии",
    DiskRole.OVERFLOW: "Дополнительный при заполнении",
    DiskRole.MIRROR: "Зеркало",
    DiskRole.ARCHIVE: "Архив",
    DiskRole.TEMPORARY: "Временное хранилище",
    DiskRole.CACHE: "Кэш",
    DiskRole.UNUSED: "Пока не использовать",
}

STATUS_LABELS: dict[DiskStatus, str] = {
    DiskStatus.HEALTHY: "Всё хорошо",
    DiskStatus.ATTENTION: "Требует внимания",
    DiskStatus.CHECK_RECOMMENDED: "Рекомендуется проверка",
    DiskStatus.DATA_LOSS_RISK: "Опасность потери данных",
    DiskStatus.ALMOST_FULL: "Почти заполнен",
    DiskStatus.WRITES_PAUSED: "Запись приостановлена",
    DiskStatus.MAINTENANCE: "Обслуживание",
    DiskStatus.DISCONNECTED: "Отключён",
    DiskStatus.UNAVAILABLE: "Недоступен",
    DiskStatus.UNCONFIGURED: "Не настроен",
}
