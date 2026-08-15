from __future__ import annotations

import base64
import json
import smtplib
import threading
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from email.message import EmailMessage
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


@dataclass(frozen=True, slots=True)
class TelegramBotProvider:
    """Telegram delivery implemented with the documented HTTPS Bot API."""

    bot_token: str
    chat_id: str
    provider_id: str = "telegram"

    def manifest(self) -> dict[str, Any]:
        return {
            "id": self.provider_id,
            "name": "Telegram bot",
            "kind": "notification",
            "built_in": True,
            "enabled": bool(self.bot_token and self.chat_id),
            "configured": bool(self.bot_token and self.chat_id),
            "loads_python_code": False,
            "external_network": True,
            "capabilities": ["deliver", "test", "power-alerts", "reports"],
        }

    def deliver(self, notification: dict[str, Any]) -> None:
        if not self.bot_token or not self.chat_id:
            raise RuntimeError("Telegram bot is not configured")
        text = (
            f"{str(notification.get('severity', 'info')).upper()}: "
            f"{notification.get('title', 'Cloud Storage')}\n"
            f"{notification.get('message', '')}"
        )[:4096]
        self.send_text(text)

    def send_text(self, text: str) -> None:
        payload = json.dumps({"chat_id": self.chat_id, "text": text[:4096]}).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "CloudStorageCore/1"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not result.get("ok"):
            raise RuntimeError("Telegram rejected the notification")

    def poll_commands(self, offset: int) -> tuple[int, list[str]]:
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self.bot_token}/getUpdates?timeout=0&offset={offset}",
            headers={"User-Agent": "CloudStorageCore/1"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            value = json.loads(response.read().decode("utf-8"))
        if not value.get("ok"):
            raise RuntimeError("Telegram update polling failed")
        commands: list[str] = []
        next_offset = offset
        for update in value.get("result", []):
            next_offset = max(next_offset, int(update.get("update_id", 0)) + 1)
            message = update.get("message") or {}
            chat = message.get("chat") or {}
            if str(chat.get("id", "")) != self.chat_id:
                continue
            text = str(message.get("text", "")).strip().casefold().split("@", 1)[0]
            if text.startswith("/"):
                commands.append(text.split(maxsplit=1)[0])
        return next_offset, commands


@dataclass(frozen=True, slots=True)
class EmailProvider:
    host: str
    port: int
    username: str
    password: str
    sender: str
    recipient: str
    starttls: bool = True
    auth_mode: str = "password"
    gmail_client_id: str = ""
    gmail_refresh_token: str = ""
    provider_id: str = "email"

    def manifest(self) -> dict[str, Any]:
        configured = bool(self.host and self.sender) and (
            bool(self.gmail_client_id and self.gmail_refresh_token)
            if self.auth_mode == "gmail_oauth"
            else True
        )
        return {
            "id": self.provider_id,
            "name": "Email (SMTP)",
            "kind": "notification",
            "built_in": True,
            "enabled": configured,
            "configured": configured,
            "loads_python_code": False,
            "external_network": True,
            "capabilities": ["deliver", "test", "power-alerts", "reports", "access-link"],
        }

    def deliver(self, notification: dict[str, Any]) -> None:
        recipient = str(notification.get("recipient") or self.recipient).strip()
        if not self.host or not self.sender or not recipient:
            raise RuntimeError("email delivery is not configured")
        message = EmailMessage()
        message["Subject"] = f"[Cloud Storage] {notification.get('title', 'Notification')}"
        message["From"] = self.sender
        message["To"] = recipient
        message.set_content(str(notification.get("message", "")))
        for attachment in notification.get("attachments") or []:
            content = attachment.get("content", b"")
            if isinstance(content, str):
                content = content.encode("utf-8")
            content_type = str(attachment.get("content_type") or "application/octet-stream")
            main_type, _, sub_type = content_type.partition("/")
            message.add_attachment(
                bytes(content),
                maintype=main_type or "application",
                subtype=sub_type or "octet-stream",
                filename=str(attachment.get("filename") or "attachment.bin"),
            )
        with smtplib.SMTP(self.host, self.port, timeout=10) as client:
            if self.starttls:
                client.starttls()
            if self.auth_mode == "gmail_oauth":
                access_token = self._gmail_access_token()
                oauth = base64.b64encode(
                    f"user={self.username}\x01auth=Bearer {access_token}\x01\x01".encode()
                ).decode("ascii")
                code, response = client.docmd("AUTH", "XOAUTH2 " + oauth)
                if code != 235:
                    raise RuntimeError(f"Gmail OAuth не принял вход: {response.decode(errors='replace')}")
            elif self.username:
                client.login(self.username, self.password)
            client.send_message(message)

    def _gmail_access_token(self) -> str:
        if not self.gmail_client_id or not self.gmail_refresh_token:
            raise RuntimeError("Войдите в Google в настройках Email")
        payload = urllib.parse.urlencode(
            {"client_id": self.gmail_client_id, "refresh_token": self.gmail_refresh_token,
             "grant_type": "refresh_token"}
        ).encode("ascii")
        request = urllib.request.Request(
            "https://oauth2.googleapis.com/token", data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            value = json.loads(response.read().decode("utf-8"))
        token = str(value.get("access_token") or "")
        if not token:
            raise RuntimeError("Google не выдал токен для отправки почты")
        return token


@dataclass(frozen=True, slots=True)
class WebhookProvider:
    url: str
    bearer_token: str = ""
    provider_id: str = "webhook"

    def manifest(self) -> dict[str, Any]:
        return {
            "id": self.provider_id,
            "name": "HTTP webhook",
            "kind": "notification",
            "built_in": True,
            "enabled": bool(self.url),
            "configured": bool(self.url),
            "loads_python_code": False,
            "external_network": True,
            "capabilities": ["deliver", "test", "bots", "reports"],
        }

    def deliver(self, notification: dict[str, Any]) -> None:
        if not self.url.startswith("https://"):
            raise RuntimeError("webhook must use HTTPS")
        headers = {"Content-Type": "application/json", "User-Agent": "CloudStorageCore/1"}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        request = urllib.request.Request(
            self.url,
            data=json.dumps(notification, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status >= 300:
                raise RuntimeError(f"webhook returned HTTP {response.status}")


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
        manifests = [provider.manifest() for provider in self.tunnels.providers.values()]
        manifests.extend(provider.manifest() for provider in self.notification_providers.values())
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

    def configure_external(self, settings: dict[str, Any], secrets: dict[str, str]) -> None:
        """Atomically replace optional providers; public settings never contain credentials."""

        optional: list[NotificationProvider] = []
        telegram = settings.get("telegram", {})
        if telegram.get("enabled"):
            optional.append(
                TelegramBotProvider(
                    bot_token=secrets.get("telegram_bot_token", ""),
                    chat_id=str(telegram.get("chat_id", "")),
                )
            )
        email = settings.get("email", {})
        if email.get("enabled"):
            optional.append(
                EmailProvider(
                    host=str(email.get("host", "")),
                    port=int(email.get("port", 587)),
                    username=str(email.get("username", "")),
                    password=secrets.get("smtp_password", ""),
                    sender=str(email.get("sender", "")),
                    recipient=str(email.get("recipient", "")),
                    starttls=bool(email.get("starttls", True)),
                    auth_mode=str(email.get("auth_mode", "password")),
                    gmail_client_id=str(email.get("gmail_client_id", "")),
                    gmail_refresh_token=secrets.get("gmail_refresh_token", ""),
                )
            )
        webhook = settings.get("webhook", {})
        if webhook.get("enabled"):
            optional.append(
                WebhookProvider(
                    url=str(webhook.get("url", "")),
                    bearer_token=secrets.get("webhook_token", ""),
                )
            )
        for provider_id in ("telegram", "email", "webhook"):
            self.notification_providers.pop(provider_id, None)
        self.notification_providers.update({item.provider_id: item for item in optional})

    def deliver_notification(self, notification: dict[str, Any]) -> list[dict[str, str]]:
        results: list[dict[str, str]] = []
        for provider_id, provider in list(self.notification_providers.items()):
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
                results.append({"provider_id": provider_id, "status": "delivered", "error": ""})
        return results
