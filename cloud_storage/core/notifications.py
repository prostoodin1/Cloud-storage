from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from cloud_storage.core.database import Database
from cloud_storage.core.integrations import IntegrationRegistry
from cloud_storage.core.repository import CoreRepository, NotFoundError, utc_text


@dataclass(slots=True)
class NotificationService:
    database: Database
    repository: CoreRepository
    integrations: IntegrationRegistry

    def emit(
        self,
        *,
        severity: str,
        title: str,
        message: str,
        source: str,
        source_key: str = "",
    ) -> dict[str, Any]:
        if severity not in {"info", "warning", "critical"}:
            raise ValueError("notification severity must be info, warning or critical")
        notification_id = str(uuid.uuid4())
        created_at = utc_text()
        notification = {
            "id": notification_id,
            "created_at": created_at,
            "severity": severity,
            "title": title.strip()[:160],
            "message": message.strip()[:2000],
            "source": source.strip()[:80] or "core",
            "source_key": source_key.strip()[:200],
            "acknowledged_at": None,
        }
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO notifications(
                    id, created_at, severity, title, message, source, source_key,
                    acknowledged_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    notification_id,
                    created_at,
                    severity,
                    notification["title"],
                    notification["message"],
                    notification["source"],
                    notification["source_key"],
                ),
            )
        deliveries = self.integrations.deliver_notification(notification)
        with self.database.transaction() as connection:
            for delivery in deliveries:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO notification_deliveries(
                        notification_id, provider_id, status, error, attempted_at
                    ) VALUES(?, ?, ?, ?, ?)
                    """,
                    (
                        notification_id,
                        delivery["provider_id"],
                        delivery["status"],
                        delivery["error"],
                        utc_text(),
                    ),
                )
        notification["deliveries"] = deliveries
        return notification

    def list(self, *, include_acknowledged: bool = False, limit: int = 100) -> list[dict[str, Any]]:
        where = "" if include_acknowledged else "WHERE acknowledged_at IS NULL"
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM notifications {where}
                ORDER BY created_at DESC LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            ).fetchall()
            result = []
            for row in rows:
                deliveries = connection.execute(
                    """
                    SELECT provider_id, status, error, attempted_at
                    FROM notification_deliveries WHERE notification_id = ?
                    ORDER BY provider_id
                    """,
                    (row["id"],),
                ).fetchall()
                item = dict(row)
                item["deliveries"] = [dict(delivery) for delivery in deliveries]
                result.append(item)
        return result

    def acknowledge(self, notification_id: str) -> dict[str, Any]:
        acknowledged_at = utc_text()
        with self.database.transaction() as connection:
            updated = connection.execute(
                """
                UPDATE notifications SET acknowledged_at = ?
                WHERE id = ? AND acknowledged_at IS NULL
                """,
                (acknowledged_at, notification_id),
            )
            exists = connection.execute(
                "SELECT * FROM notifications WHERE id = ?", (notification_id,)
            ).fetchone()
            if exists is None:
                raise NotFoundError("notification not found")
            self.repository._audit_tx(
                connection,
                actor_type="manager",
                actor_id=None,
                action="notification.acknowledged",
                target_type="notification",
                target_id=notification_id,
                detail=(
                    "Уведомление подтверждено"
                    if updated.rowcount
                    else "Уведомление уже было подтверждено"
                ),
            )
        return dict(exists) | {"acknowledged_at": acknowledged_at}

    def test_provider(self, provider_id: str) -> dict[str, Any]:
        provider = self.integrations.notification_providers.get(provider_id)
        if provider is None:
            raise NotFoundError("notification provider not found")
        if provider_id == "manager-inbox":
            created = self.emit(
                severity="info",
                title="Проверка центра уведомлений",
                message="Встроенный центр уведомлений Manager работает.",
                source="manager-test",
                source_key=provider_id,
            )
            result = {
                "provider_id": provider_id,
                "status": "delivered",
                "notification_id": created["id"],
            }
        else:
            notification = {
                "id": "test",
                "created_at": utc_text(),
                "severity": "info",
                "title": "Проверка встроенной интеграции",
                "message": f"Провайдер {provider_id} работает и не использует внешнюю сеть.",
                "source": "manager-test",
                "source_key": provider_id,
            }
            provider.deliver(notification)
            result = {"provider_id": provider_id, "status": "delivered"}
        self.repository.record_audit(
            actor_type="manager",
            actor_id=None,
            action="integration.provider.tested",
            target_type="integration_provider",
            target_id=provider_id,
            detail=f"Проверен встроенный провайдер {provider_id}",
        )
        return result
