from __future__ import annotations

import hashlib
import io
import json
import platform
import sys
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from cloud_storage import __version__
from cloud_storage.core.config import CoreConfig
from cloud_storage.core.diagnostics import DiagnosticsService
from cloud_storage.core.repository import CoreRepository
from cloud_storage.core.tunnels import TunnelProviderRegistry


@dataclass(frozen=True, slots=True)
class SupportBundle:
    filename: str
    content: bytes
    sha256: str


@dataclass(slots=True)
class SupportBundleService:
    config: CoreConfig
    repository: CoreRepository
    diagnostics: DiagnosticsService
    tunnels: TunnelProviderRegistry

    def build(self) -> SupportBundle:
        created_at = datetime.now(UTC)
        files = {
            "README.txt": self._readme(),
            "manifest.json": self._json(
                {
                    "format": "cloud-storage-support-v1",
                    "created_at": created_at.isoformat(timespec="seconds"),
                    "version": __version__,
                    "privacy": {
                        "database_included": False,
                        "logs_included": False,
                        "user_files_included": False,
                        "credentials_included": False,
                        "identifiers_included": False,
                        "network_addresses_included": False,
                    },
                }
            ),
            "system.json": self._json(self._system_snapshot()),
            "configuration.json": self._json(self._configuration_snapshot()),
            "database.json": self._json(self._database_snapshot()),
            "diagnostics.json": self._json(self._diagnostics_snapshot()),
            "plugins.json": self._json(
                {
                    "manifests": [
                        provider.manifest()
                        for provider in self.tunnels.providers.values()
                    ],
                    "status": self.tunnels.support_snapshot(),
                }
            ),
            "activity.json": self._json(self._activity_snapshot()),
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(
            buffer,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        payload = buffer.getvalue()
        stamp = created_at.strftime("%Y%m%dT%H%M%SZ")
        return SupportBundle(
            filename=f"CloudStorage-support-{stamp}.zip",
            content=payload,
            sha256=hashlib.sha256(payload).hexdigest(),
        )

    @staticmethod
    def _readme() -> str:
        return (
            "Cloud Storage support bundle\n"
            "============================\n\n"
            "Пакет содержит только обезличенные состояния и счётчики.\n"
            "База SQLite, журналы, пути, адреса, имена пользователей, идентификаторы, "
            "пароли, токены и пользовательские файлы намеренно не включены.\n"
        )

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    @staticmethod
    def _system_snapshot() -> dict[str, Any]:
        return {
            "application_version": __version__,
            "python_version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "operating_system": platform.system(),
            "operating_system_release": platform.release(),
            "architecture": platform.machine(),
            "frozen": bool(getattr(sys, "frozen", False)),
        }

    def _configuration_snapshot(self) -> dict[str, Any]:
        return {
            "local_api": {"loopback_only": True, "port": self.config.port},
            "lan": {
                "enabled": self.config.lan_enabled,
                "port": self.config.lan_port,
                "discovery_port": self.config.discovery_port,
            },
            "direct_remote": {
                "enabled": self.config.remote_enabled,
                "port": self.config.remote_port,
                "pairing_enabled": self.config.remote_pairing_enabled,
                "public_url_configured": bool(self.config.remote_public_url),
                "login_required": True,
            },
            "zrok": {
                "enabled": self.config.zrok_enabled,
                "port": self.config.zrok_port,
                "reserved_share_configured": bool(self.config.zrok_share_name),
                "custom_executable_configured": self.config.zrok_executable != "zrok",
                "login_required": True,
            },
            "limits": {
                "max_upload_bytes": self.config.max_upload_bytes,
                "pairing_ttl_seconds": self.config.pairing_ttl_seconds,
            },
        }

    def _database_snapshot(self) -> dict[str, Any]:
        quick_check = "unavailable"
        foreign_key_errors = -1
        try:
            with self.repository.database.connection() as connection:
                row = connection.execute("PRAGMA quick_check").fetchone()
                quick_check = str(row[0]) if row else "no-result"
                foreign_key_errors = len(
                    connection.execute("PRAGMA foreign_key_check").fetchall()
                )
        except Exception:
            quick_check = "error"
        log_metadata: dict[str, Any] = {"exists": False, "size_bytes": 0}
        try:
            if self.config.log_path.is_file():
                stat = self.config.log_path.stat()
                log_metadata = {"exists": True, "size_bytes": stat.st_size}
        except OSError:
            log_metadata = {"exists": True, "size_bytes": -1}
        return {
            "quick_check": quick_check,
            "foreign_key_errors": foreign_key_errors,
            "summary": self.repository.summary(),
            "core_log": log_metadata,
        }

    def _diagnostics_snapshot(self) -> dict[str, Any]:
        overview = self.diagnostics.overview()
        latest = overview.get("latest_scan") or {}
        return {
            "status": overview.get("status", "unknown"),
            "active_warning_count": int(overview.get("active_warning_count", 0)),
            "active_critical_count": int(overview.get("active_critical_count", 0)),
            "monitor": {
                "running": bool((overview.get("monitor") or {}).get("running")),
                "interval_seconds": int(
                    (overview.get("monitor") or {}).get("interval_seconds", 0)
                ),
            },
            "latest_scan": {
                "present": bool(latest),
                "kind": latest.get("kind"),
                "status": latest.get("status"),
                "checked_objects": int(latest.get("checked_objects", 0)),
                "checked_bytes": int(latest.get("checked_bytes", 0)),
                "warning_count": int(latest.get("warning_count", 0)),
                "critical_count": int(latest.get("critical_count", 0)),
                "failed": bool(latest.get("error")),
            },
        }

    def _activity_snapshot(self) -> dict[str, Any]:
        events = self.repository.recent_audit(500)
        actions = Counter(str(item.get("action", "unknown")) for item in events)
        actors = Counter(str(item.get("actor_type", "unknown")) for item in events)
        return {
            "sample_size": len(events),
            "action_counts": dict(sorted(actions.items())),
            "actor_type_counts": dict(sorted(actors.items())),
        }
