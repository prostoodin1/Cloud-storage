from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from cloud_storage.core.config import CoreConfig
from cloud_storage.core.tunnels import TunnelProviderRegistry


class NotificationProvider(Protocol):
    provider_id: str

    def manifest(self) -> dict[str, Any]: ...

    def deliver(self, notification: dict[str, Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class ManagerInboxProvider:
    provider_id: str = "manager-inbox"

    def manifest(self) -> dict[str, Any]:
        return {
            "id": self.provider_id,
            "name": "Центр уведомлений Manager",
            "kind": "notification",
            "built_in": True,
            "enabled": True,
            "loads_python_code": False,
            "external_network": False,
            "capabilities": ["deliver", "history", "acknowledge", "test"],
        }

    def deliver(self, notification: dict[str, Any]) -> None:
        return None


@dataclass(slots=True)
class SystemLogProvider:
    log_path: Path
    provider_id: str = "system-log"
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def manifest(self) -> dict[str, Any]:
        return {
            "id": self.provider_id,
            "name": "Системный журнал Core",
            "kind": "notification",
            "built_in": True,
            "enabled": True,
            "loads_python_code": False,
            "external_network": False,
            "capabilities": ["deliver", "test"],
        }

    def deliver(self, notification: dict[str, Any]) -> None:
        line = (
            f"{notification.get('created_at', '')} NOTIFICATION "
            f"{str(notification.get('severity', 'info')).upper()} "
            f"[{notification.get('source', 'core')}] "
            f"{notification.get('title', 'Notification')}: "
            f"{notification.get('message', '')}\n"
        )
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.log_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()


@dataclass(slots=True)
class IntegrationRegistry:
    tunnels: TunnelProviderRegistry
    notification_providers: dict[str, NotificationProvider]

    @classmethod
    def built_in(
        cls,
        tunnels: TunnelProviderRegistry,
        config: CoreConfig,
    ) -> IntegrationRegistry:
        providers: list[NotificationProvider] = [
            ManagerInboxProvider(),
            SystemLogProvider(config.log_path),
        ]
        return cls(
            tunnels=tunnels,
            notification_providers={item.provider_id: item for item in providers},
        )

    def manifests(self) -> list[dict[str, Any]]:
        manifests = [
            provider.manifest() for provider in self.tunnels.providers.values()
        ]
        manifests.extend(
            provider.manifest() for provider in self.notification_providers.values()
        )
        return sorted(manifests, key=lambda item: str(item["id"]))

    def overview(self) -> dict[str, Any]:
        return {
            "plugins": self.manifests(),
            "policy": {
                "built_in_only": True,
                "dynamic_python_loading": False,
                "third_party_directories_scanned": False,
            },
            "tunnels": self.tunnels.overview(),
        }

    def deliver_notification(self, notification: dict[str, Any]) -> list[dict[str, str]]:
        results: list[dict[str, str]] = []
        for provider_id, provider in self.notification_providers.items():
            try:
                provider.deliver(notification)
            except Exception as exc:
                results.append(
                    {
                        "provider_id": provider_id,
                        "status": "failed",
                        "error": str(exc)[:500],
                    }
                )
            else:
                results.append(
                    {"provider_id": provider_id, "status": "delivered", "error": ""}
                )
        return results
