from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psutil

from cloud_storage.core.automation import AutomationService
from cloud_storage.core.config import CoreConfig, _restrict_secret_file
from cloud_storage.core.database import Database
from cloud_storage.core.diagnostics import DiagnosticsService
from cloud_storage.core.host_tools import HostToolsService
from cloud_storage.core.integrations import IntegrationRegistry
from cloud_storage.core.notifications import NotificationService
from cloud_storage.core.repository import CoreRepository, NotFoundError, utc_text
from cloud_storage.core.storage import StorageService
from cloud_storage.core.tunnels import TunnelProviderRegistry
from cloud_storage.pairing import build_pairing_uri

REPORT_SECTIONS = {
    "system",
    "disks",
    "storage",
    "connections",
    "internet",
    "users",
    "transfers",
    "diagnostics",
    "backups",
    "power",
}

SYSTEM_PRESETS: dict[str, dict[str, Any]] = {
    "default": {
        "name": "По умолчанию",
        "description": "Сбалансированные настройки домашнего облака.",
        "settings": {"profile": "default", "power": {"idle_minutes": 20}},
    },
    "manual": {
        "name": "Ручной",
        "description": "Никаких автоматических сна, отчётов или системных действий.",
        "settings": {
            "profile": "manual",
            "power": {"idle_sleep_enabled": False, "allow_os_sleep": False},
        },
    },
    "automatic": {
        "name": "Автоматический",
        "description": "Автоматические проверки, отчёты и экономия питания.",
        "settings": {"profile": "automatic", "power": {"idle_sleep_enabled": True}},
    },
    "best": {
        "name": "Максимальная производительность",
        "description": "Сервер не засыпает и оставляет больше ресурсов задачам.",
        "settings": {
            "profile": "best",
            "power": {"idle_sleep_enabled": False},
            "sandbox": {"resource_percent": 70},
        },
    },
    "energy": {
        "name": "Энергосбережение",
        "description": "Сон через 20 минут простоя и более строгие лимиты ячеек.",
        "settings": {
            "profile": "energy",
            "power": {"idle_sleep_enabled": True, "idle_minutes": 20},
            "sandbox": {"resource_percent": 25},
        },
    },
    "mirror": {
        "name": "Зеркало",
        "description": "Приоритет проверки зеркал и целостности.",
        "settings": {"profile": "mirror", "storage_strategy": "mirror"},
    },
    "cache": {
        "name": "SSD-кэш",
        "description": "Приём на быстрый диск с последующей выгрузкой на HDD.",
        "settings": {"profile": "cache", "storage_strategy": "staging"},
    },
    "recommended": {
        "name": "Рекомендуемый",
        "description": "Безопасная автоматика, зеркало и сон только после явного разрешения ОС.",
        "settings": {
            "profile": "recommended",
            "storage_strategy": "balanced",
            "power": {"idle_sleep_enabled": True, "idle_minutes": 20},
            "security": {"mode": "advanced"},
        },
    },
}


def _template(
    template_id: str,
    category: str,
    name: str,
    description: str,
    trigger: str,
    action: str,
    cooldown_minutes: int,
) -> dict[str, Any]:
    return {
        "id": template_id,
        "category": category,
        "name": name,
        "description": description,
        "blocks": [
            {"kind": "when", "value": trigger},
            {"kind": "then", "value": action},
            {"kind": "cooldown", "value": cooldown_minutes},
        ],
        "rule": {
            "name": name,
            "trigger_type": trigger,
            "action_type": action,
            "cooldown_seconds": cooldown_minutes * 60,
        },
    }


AUTOMATION_TEMPLATES = (
    _template(
        "emergency-critical-alert",
        "emergency",
        "Критическая тревога",
        "Сообщить обо всех критических инцидентах.",
        "diagnostic_critical",
        "notify",
        15,
    ),
    _template(
        "emergency-read-only",
        "emergency",
        "Аварийная защита записи",
        "При критическом инциденте перевести сервер в read-only.",
        "diagnostic_critical",
        "read_only",
        15,
    ),
    _template(
        "emergency-full-scan",
        "emergency",
        "Полная проверка после сбоя",
        "Проверить SHA-256 после критической ошибки.",
        "diagnostic_critical",
        "full_scan",
        60,
    ),
    _template(
        "emergency-low-space",
        "emergency",
        "Критически мало места",
        "Предупредить о нехватке места.",
        "storage_low",
        "notify",
        60,
    ),
    _template(
        "emergency-backup",
        "emergency",
        "Сбой резервной копии",
        "Сообщить о неудачной резервной копии.",
        "backup_failed",
        "notify",
        60,
    ),
    _template(
        "emergency-restore",
        "emergency",
        "Сбой восстановления",
        "Сообщить о неудачном восстановлении.",
        "restore_failed",
        "notify",
        60,
    ),
    _template(
        "emergency-mirror",
        "emergency",
        "Восстановить зеркало",
        "Автоматически согласовать повреждённое зеркало.",
        "mirror_degraded",
        "reconcile_mirrors",
        60,
    ),
    _template(
        "emergency-maintenance",
        "emergency",
        "Сбой обслуживания диска",
        "Сообщить об ошибке обслуживания.",
        "maintenance_failed",
        "notify",
        60,
    ),
    _template(
        "emergency-tunnel",
        "emergency",
        "Восстановить интернет-доступ",
        "Перезапустить упавший zrok-туннель.",
        "tunnel_offline",
        "restart_tunnel",
        30,
    ),
    _template(
        "emergency-power-shutdown",
        "emergency",
        "Аварийное выключение без питания",
        "При работе от батареи/ИБП предупредить и поставить безопасное выключение через час.",
        "power_outage",
        "shutdown_after_hour",
        60,
    ),
    _template(
        "normal-daily-backup",
        "normal",
        "Регулярная резервная копия",
        "Запустить включённую политику копирования по расписанию.",
        "scheduled",
        "run_backup",
        1440,
    ),
    _template(
        "normal-mirror",
        "normal",
        "Проверять зеркало",
        "Регулярно сверять новые данные с зеркалом.",
        "scheduled",
        "reconcile_mirrors",
        60,
    ),
    _template(
        "normal-quick-scan",
        "normal",
        "Быстрая проверка",
        "Проверять состояние сервера по расписанию.",
        "scheduled",
        "quick_scan",
        360,
    ),
    _template(
        "normal-full-scan",
        "normal",
        "Ночная полная проверка",
        "Регулярно проверять целостность файлов.",
        "scheduled",
        "full_scan",
        1440,
    ),
    _template(
        "normal-status",
        "normal",
        "Плановый статус",
        "Отправлять обычное уведомление о работе.",
        "scheduled",
        "notify",
        720,
    ),
    _template(
        "normal-warning-scan",
        "normal",
        "Проверка по предупреждению",
        "Запускать быструю проверку при предупреждении.",
        "diagnostic_warning",
        "quick_scan",
        60,
    ),
    _template(
        "normal-backup-scan",
        "normal",
        "Проверка после сбоя backup",
        "Проверить данные после ошибки резервирования.",
        "backup_failed",
        "full_scan",
        120,
    ),
    _template(
        "normal-restore-scan",
        "normal",
        "Проверка после сбоя restore",
        "Проверить данные после ошибки восстановления.",
        "restore_failed",
        "quick_scan",
        120,
    ),
    _template(
        "normal-mirror-alert",
        "normal",
        "Сообщить о зеркале",
        "Предупредить, если зеркало отстаёт.",
        "mirror_degraded",
        "notify",
        60,
    ),
    _template(
        "normal-tunnel-alert",
        "normal",
        "Сообщить о zrok",
        "Сообщить, если интернет-шлюз недоступен.",
        "tunnel_offline",
        "notify",
        30,
    ),
)

DEFAULT_SETTINGS: dict[str, Any] = {
    "profile": "recommended",
    "interface_mode": "simple",
    "storage_strategy": "balanced",
    "browser_access": "approved",
    "security": {
        "mode": "basic",
        "require_login": True,
        "remote_pairing": False,
        "audit_remote": True,
        "inert_files": True,
    },
    "power": {
        "idle_sleep_enabled": False,
        "idle_minutes": 20,
        "allow_os_sleep": False,
        "allow_os_shutdown": False,
        "notify_on_outage": True,
        "notify_below_minutes": 20,
    },
    "sandbox": {"resource_percent": 40, "runtime": "auto"},
    "integrations": {
        "telegram": {"enabled": False, "chat_id": ""},
        "email": {
            "enabled": False,
            "host": "",
            "port": 587,
            "username": "",
            "sender": "",
            "recipient": "",
            "starttls": True,
        },
        "webhook": {"enabled": False, "url": ""},
    },
}


def _merge(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


@dataclass(slots=True)
class ControlCenterService:
    config: CoreConfig
    database: Database
    repository: CoreRepository
    storage: StorageService
    diagnostics: DiagnosticsService
    tunnels: TunnelProviderRegistry
    integrations: IntegrationRegistry
    notifications: NotificationService
    automation: AutomationService
    tls_fingerprint: str = ""
    host_tools: HostToolsService = field(init=False, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _last_access_monotonic: float = field(default_factory=time.monotonic, init=False, repr=False)
    _last_plugged: bool | None = field(default=None, init=False, repr=False)
    _power_alert_sent: bool = field(default=False, init=False, repr=False)
    _telegram_offset: int = field(default=0, init=False, repr=False)
    _sleep_timer: threading.Timer | None = field(default=None, init=False, repr=False)
    _shutdown_timer: threading.Timer | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.host_tools = HostToolsService(self.config.data_directory)
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO control_settings(id, settings_json, updated_at) VALUES(1, ?, ?)",
                (json.dumps(DEFAULT_SETTINGS, ensure_ascii=False), utc_text()),
            )
        self.integrations.configure_external(self.settings()["integrations"], self._load_secrets())
        self.automation.system_action_handler = self._handle_system_action
        self.automation.power_state_handler = self.power_status

    @property
    def secrets_path(self) -> Path:
        return self.config.data_directory / "integration-secrets.json"

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._monitor_loop, name="cloud-storage-control-center", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5)
        if self._sleep_timer is not None:
            self._sleep_timer.cancel()
            self._sleep_timer = None
        if self._shutdown_timer is not None:
            self._shutdown_timer.cancel()
            self._shutdown_timer = None

    def note_access(self) -> None:
        self._last_access_monotonic = time.monotonic()

    def settings(self) -> dict[str, Any]:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT settings_json FROM control_settings WHERE id = 1"
            ).fetchone()
        try:
            stored = json.loads(row["settings_json"]) if row else {}
        except (TypeError, json.JSONDecodeError):
            stored = {}
        result = _merge(DEFAULT_SETTINGS, stored if isinstance(stored, dict) else {})
        result["integrations"]["telegram"]["token_configured"] = bool(
            self._load_secrets().get("telegram_bot_token")
        )
        result["integrations"]["email"]["password_configured"] = bool(
            self._load_secrets().get("smtp_password")
        )
        result["integrations"]["webhook"]["token_configured"] = bool(
            self._load_secrets().get("webhook_token")
        )
        return result

    def update_settings(self, update: dict[str, Any]) -> dict[str, Any]:
        current = self.settings()
        for integration in current["integrations"].values():
            for marker in ("token_configured", "password_configured"):
                integration.pop(marker, None)
        secrets_update = dict(update.pop("secrets", {})) if "secrets" in update else {}
        allowed = {
            "profile",
            "interface_mode",
            "storage_strategy",
            "browser_access",
            "security",
            "power",
            "sandbox",
            "integrations",
        }
        unknown = set(update) - allowed
        if unknown:
            raise ValueError(f"unsupported control setting: {sorted(unknown)[0]}")
        merged = _merge(current, update)
        self._validate_settings(merged)
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE control_settings SET settings_json = ?, updated_at = ? WHERE id = 1",
                (json.dumps(merged, ensure_ascii=False), utc_text()),
            )
        if secrets_update:
            secrets = self._load_secrets()
            for key in ("telegram_bot_token", "smtp_password", "webhook_token"):
                if key in secrets_update:
                    value = str(secrets_update[key])
                    if value:
                        secrets[key] = value
                    else:
                        secrets.pop(key, None)
            self._save_secrets(secrets)
        self.integrations.configure_external(merged["integrations"], self._load_secrets())
        self.repository.record_audit(
            actor_type="manager",
            actor_id=None,
            action="control.settings.updated",
            target_type="system",
            target_id="control-center",
            detail="Обновлены настройки центра управления Idea 4",
        )
        return self.settings()

    def apply_preset(self, preset_id: str) -> dict[str, Any]:
        preset = SYSTEM_PRESETS.get(preset_id)
        if preset is None:
            raise NotFoundError("system preset not found")
        settings = self.update_settings(deepcopy(preset["settings"]))
        return {"preset": {"id": preset_id, **preset}, "settings": settings}

    def overview(self) -> dict[str, Any]:
        return {
            "settings": self.settings(),
            "presets": [{"id": key, **value} for key, value in SYSTEM_PRESETS.items()],
            "power": self.power_status(),
            "sandbox": self.sandbox_capabilities(),
            "automation_templates": list(AUTOMATION_TEMPLATES),
            "reports": {"sections": sorted(REPORT_SECTIONS), "schedules": self.list_reports()},
            "cells": self.list_cells(),
            "network": self.network_status(),
            "host_tools": self.host_tools.overview(),
        }

    def install_docker(self, *, confirmed: bool) -> dict[str, Any]:
        result = self.host_tools.install_docker(confirmed=confirmed)
        self.repository.record_audit(
            actor_type="manager",
            actor_id=None,
            action="host.docker.install.requested",
            target_type="system",
            target_id="docker",
            detail="Запрошена установка Docker Desktop из интерфейса менеджера",
        )
        return result

    def enable_ssh(
        self,
        *,
        username: str,
        public_key: str,
        port: int,
        confirmed: bool,
    ) -> dict[str, Any]:
        result = self.host_tools.enable_ssh(
            username=username,
            public_key=public_key,
            port=port,
            confirmed=confirmed,
        )
        self.repository.record_audit(
            actor_type="manager",
            actor_id=None,
            action="host.ssh.enable.requested",
            target_type="system",
            target_id="openssh",
            detail=f"Запрошен SSH по ключу для {username.strip()} на TCP/{port}",
        )
        return result

    def disable_ssh(self, *, confirmed: bool) -> dict[str, Any]:
        result = self.host_tools.disable_ssh(confirmed=confirmed)
        self.repository.record_audit(
            actor_type="manager",
            actor_id=None,
            action="host.ssh.disable.requested",
            target_type="system",
            target_id="openssh",
            detail="Запрошено отключение SSH и правила брандмауэра",
        )
        return result

    def network_status(self) -> dict[str, Any]:
        tunnel = self.tunnels.status("zrok")
        local = f"http://127.0.0.1:{self.config.port}"
        lan_addresses: list[str] = []
        if self.config.lan_enabled:
            try:
                addresses = socket.gethostbyname_ex(socket.gethostname())[2]
            except OSError:
                addresses = []
            lan_addresses = [
                f"https://{address}:{self.config.lan_port}"
                for address in addresses
                if address and not address.startswith("127.")
            ]
        return {
            "local_address": local,
            "lan_addresses": list(dict.fromkeys(lan_addresses)),
            "zrok": tunnel,
            "desired_zrok_name": self.config.zrok_share_name,
            "browser_access": self.settings()["browser_access"],
            "policy_options": ["all", "approved", "nobody"],
            "manager_api_exposed": False,
        }

    def power_status(self) -> dict[str, Any]:
        battery = psutil.sensors_battery()
        if battery is None:
            source = "unknown"
            plugged = None
            percent = None
            seconds_left = None
        else:
            plugged = bool(battery.power_plugged)
            source = "mains" if plugged else "battery"
            percent = round(float(battery.percent), 1)
            seconds_left = (
                int(battery.secsleft)
                if battery.secsleft not in {psutil.POWER_TIME_UNKNOWN, psutil.POWER_TIME_UNLIMITED}
                and battery.secsleft >= 0
                else None
            )
        idle = max(0, round(time.monotonic() - self._last_access_monotonic))
        policy = self.settings()["power"]
        return {
            "source": source,
            "plugged": plugged,
            "percent": percent,
            "seconds_left": seconds_left,
            "minutes_left": round(seconds_left / 60) if seconds_left is not None else None,
            "idle_seconds": idle,
            "sleep_after_seconds": int(policy["idle_minutes"]) * 60,
            "sleep_armed": bool(policy["idle_sleep_enabled"] and policy["allow_os_sleep"]),
            "shutdown_armed": bool(policy["allow_os_shutdown"]),
            "wake_on_access": "hardware-wol",
            "wake_note": "Wake-on-LAN must also be enabled in BIOS/UEFI and the network adapter.",
        }

    def wake_on_lan(
        self, mac_address: str, *, broadcast: str = "255.255.255.255"
    ) -> dict[str, Any]:
        compact = re.sub(r"[^0-9A-Fa-f]", "", mac_address)
        if len(compact) != 12:
            raise ValueError("invalid MAC address")
        packet = bytes.fromhex("FF" * 6 + compact * 16)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(packet, (broadcast, 9))
        return {"sent": True, "mac_address": compact.upper(), "broadcast": broadcast}

    def install_template(self, template_id: str, *, enabled: bool = True) -> dict[str, Any]:
        template = next((item for item in AUTOMATION_TEMPLATES if item["id"] == template_id), None)
        if template is None:
            raise NotFoundError("automation template not found")
        rule = template["rule"]
        created = self.automation.create_rule(
            name=rule["name"],
            enabled=enabled,
            trigger_type=rule["trigger_type"],
            action_type=rule["action_type"],
            cooldown_seconds=rule["cooldown_seconds"],
        )
        return {"template_id": template_id, "rule": asdict(created)}

    def list_reports(self) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM report_schedules ORDER BY name COLLATE NOCASE"
            ).fetchall()
        return [self._report_schedule(row) for row in rows]

    def save_report(
        self,
        *,
        name: str,
        enabled: bool,
        interval_hours: int,
        sections: list[str],
        delivery_channels: list[str],
        report_id: str | None = None,
    ) -> dict[str, Any]:
        clean_name = name.strip()[:120]
        clean_sections = sorted(set(sections))
        if not clean_name or not clean_sections or set(clean_sections) - REPORT_SECTIONS:
            raise ValueError("report name and supported sections are required")
        if not 1 <= interval_hours <= 8760:
            raise ValueError("report interval must be between 1 and 8760 hours")
        allowed_channels = set(self.integrations.notification_providers)
        channels = sorted(set(delivery_channels))
        if set(channels) - allowed_channels:
            raise ValueError("unknown report delivery channel")
        report_id = report_id or str(uuid.uuid4())
        now = datetime.now(UTC)
        next_run = now + timedelta(hours=interval_hours) if enabled else None
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO report_schedules(
                    id, name, enabled, interval_hours, sections_json,
                    delivery_channels_json, next_run_at, last_run_at, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name, enabled=excluded.enabled,
                    interval_hours=excluded.interval_hours, sections_json=excluded.sections_json,
                    delivery_channels_json=excluded.delivery_channels_json,
                    next_run_at=excluded.next_run_at, updated_at=excluded.updated_at
                """,
                (
                    report_id,
                    clean_name,
                    int(enabled),
                    interval_hours,
                    json.dumps(clean_sections),
                    json.dumps(channels),
                    next_run.isoformat().replace("+00:00", "Z") if next_run else None,
                    utc_text(now),
                    utc_text(now),
                ),
            )
        return next(item for item in self.list_reports() if item["id"] == report_id)

    def generate_report(
        self,
        sections: list[str],
        *,
        schedule_id: str | None = None,
        delivery_channels: list[str] | None = None,
    ) -> dict[str, Any]:
        selected = sorted(set(sections))
        if not selected or set(selected) - REPORT_SECTIONS:
            raise ValueError("unsupported report section")
        report: dict[str, Any] = {"generated_at": utc_text(), "sections": selected}
        summary = self.repository.summary()
        if "system" in selected:
            report["system"] = {
                "platform": platform.platform(),
                "cpu_count": psutil.cpu_count(),
                "memory_bytes": psutil.virtual_memory().total,
            }
        if "storage" in selected:
            report["storage"] = {"files": summary["files"], "stored_bytes": summary["stored_bytes"]}
        if "users" in selected:
            report["users"] = {
                "users": summary["users"],
                "trusted_devices": summary["trusted_devices"],
                "pending_devices": summary["pending_devices"],
            }
        if "disks" in selected:
            report["disks"] = [
                asdict(item) | {"path": str(item.path)} for item in self.storage.list_roots()
            ]
        if "connections" in selected or "internet" in selected:
            report["network"] = self.network_status()
        if "transfers" in selected:
            report["transfers"] = self.storage.transfer_overview(limit=20)["counts"]
        if "diagnostics" in selected:
            diagnostic = self.diagnostics.overview()
            report["diagnostics"] = {
                key: diagnostic.get(key) for key in ("status", "counts", "last_scan")
            }
        if "backups" in selected:
            with self.database.connection() as connection:
                report["backups"] = dict(
                    connection.execute(
                        "SELECT count(*) AS total, sum(status = 'completed') AS completed FROM backup_jobs WHERE pruned_at IS NULL"
                    ).fetchone()
                )
        if "power" in selected:
            report["power"] = self.power_status()
        run_id = str(uuid.uuid4())
        channels = delivery_channels or []
        error = ""
        status = "completed"
        for channel in channels:
            provider = self.integrations.notification_providers.get(channel)
            if provider is None:
                continue
            try:
                provider.deliver(
                    {
                        "severity": "info",
                        "title": "Отчёт Cloud Storage",
                        "message": json.dumps(report, ensure_ascii=False, indent=2),
                        "created_at": report["generated_at"],
                        "source": "report",
                    }
                )
            except Exception as exc:
                status, error = "failed", str(exc)[:500]
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO report_runs(id, schedule_id, status, sections_json, report_json, error, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    schedule_id,
                    status,
                    json.dumps(selected),
                    json.dumps(report, ensure_ascii=False),
                    error,
                    utc_text(),
                ),
            )
        return {"id": run_id, "status": status, "error": error, "report": report}

    def sandbox_capabilities(self) -> dict[str, Any]:
        configured = self.settings()["sandbox"]
        requested = configured.get("runtime", "auto")
        runtime = None
        candidates = ("docker", "podman") if requested == "auto" else (str(requested),)
        for candidate in candidates:
            path = shutil.which(candidate)
            if path:
                runtime = path
                break
        percent = int(configured["resource_percent"])
        memory = psutil.virtual_memory().total // (1024**2)
        cpu = psutil.cpu_count(logical=True) or 1
        return {
            "available": runtime is not None,
            "runtime": Path(runtime).name if runtime else None,
            "runtime_path": runtime,
            "host_execution_allowed": False,
            "network_default": False,
            "automatic_max": {
                "cpu": max(0.25, round(cpu * percent / 100, 2)),
                "memory_mib": max(128, int(memory * percent / 100)),
                "parallel_cells": max(1, min(8, int(cpu * percent / 100) or 1)),
            },
        }

    def create_cell(
        self,
        *,
        name: str,
        image: str,
        command: list[str],
        cpu_limit: float,
        memory_mib: int,
        storage_mib: int,
        timeout_seconds: int,
        network_enabled: bool = False,
    ) -> dict[str, Any]:
        self._validate_cell_values(
            name=name,
            image=image,
            command=command,
            cpu_limit=cpu_limit,
            memory_mib=memory_mib,
            storage_mib=storage_mib,
            timeout_seconds=timeout_seconds,
        )
        cell_id, now = str(uuid.uuid4()), utc_text()
        state = "ready" if self.sandbox_capabilities()["available"] else "unsupported"
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO sandbox_cells(
                    id, name, image, command_json, cpu_limit, memory_mib, storage_mib,
                    timeout_seconds, network_enabled, status, last_result, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?)""",
                (
                    cell_id,
                    name.strip()[:120],
                    image,
                    json.dumps(command),
                    cpu_limit,
                    memory_mib,
                    storage_mib,
                    timeout_seconds,
                    int(network_enabled),
                    state,
                    now,
                    now,
                ),
            )
        return self.get_cell(cell_id)

    def update_cell(
        self,
        cell_id: str,
        *,
        name: str,
        image: str,
        command: list[str],
        cpu_limit: float,
        memory_mib: int,
        storage_mib: int,
        timeout_seconds: int,
        network_enabled: bool = False,
    ) -> dict[str, Any]:
        self.get_cell(cell_id)
        self._validate_cell_values(
            name=name,
            image=image,
            command=command,
            cpu_limit=cpu_limit,
            memory_mib=memory_mib,
            storage_mib=storage_mib,
            timeout_seconds=timeout_seconds,
        )
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE sandbox_cells SET
                    name=?, image=?, command_json=?, cpu_limit=?, memory_mib=?, storage_mib=?,
                    timeout_seconds=?, network_enabled=?, status=?, updated_at=?
                   WHERE id=?""",
                (
                    name.strip()[:120],
                    image,
                    json.dumps(command),
                    cpu_limit,
                    memory_mib,
                    storage_mib,
                    timeout_seconds,
                    int(network_enabled),
                    "ready" if self.sandbox_capabilities()["available"] else "unsupported",
                    utc_text(),
                    cell_id,
                ),
            )
        return self.get_cell(cell_id)

    def delete_cell(self, cell_id: str) -> bool:
        cell = self.get_cell(cell_id)
        if cell["status"] == "running":
            raise RuntimeError("a running container cell cannot be deleted")
        with self.database.transaction() as connection:
            deleted = connection.execute(
                "DELETE FROM sandbox_cells WHERE id=?", (cell_id,)
            )
        return deleted.rowcount == 1

    def _validate_cell_values(
        self,
        *,
        name: str,
        image: str,
        command: list[str],
        cpu_limit: float,
        memory_mib: int,
        storage_mib: int,
        timeout_seconds: int,
    ) -> None:
        caps = self.sandbox_capabilities()["automatic_max"]
        if not name.strip() or not re.fullmatch(r"[A-Za-z0-9._/@:-]{1,200}", image):
            raise ValueError("cell name and a valid container image are required")
        if not command or len(command) > 64 or any(len(item) > 1024 for item in command):
            raise ValueError("cell command must contain 1-64 arguments")
        if not 0 < cpu_limit <= float(caps["cpu"]):
            raise ValueError("cell CPU limit exceeds the automatic safe maximum")
        if not 64 <= memory_mib <= int(caps["memory_mib"]):
            raise ValueError("cell memory limit exceeds the automatic safe maximum")
        if not 64 <= storage_mib <= 10 * 1024 or not 1 <= timeout_seconds <= 86400:
            raise ValueError("invalid cell storage or timeout limit")

    def list_cells(self) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM sandbox_cells ORDER BY updated_at DESC"
            ).fetchall()
        return [self._cell(row) for row in rows]

    def get_cell(self, cell_id: str) -> dict[str, Any]:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM sandbox_cells WHERE id = ?", (cell_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("sandbox cell not found")
        return self._cell(row)

    def run_cell(self, cell_id: str) -> dict[str, Any]:
        cell, caps = self.get_cell(cell_id), self.sandbox_capabilities()
        runtime = caps["runtime_path"]
        if not runtime:
            raise RuntimeError("Docker or Podman is not installed")
        command = [
            runtime,
            "run",
            "--rm",
            "--read-only",
            "--cpus",
            str(cell["cpu_limit"]),
            "--memory",
            f"{cell['memory_mib']}m",
            "--pids-limit",
            "128",
            "--tmpfs",
            f"/workspace:rw,noexec,nosuid,size={cell['storage_mib']}m",
            "--workdir",
            "/workspace",
            "--network",
            "bridge" if cell["network_enabled"] else "none",
            cell["image"],
            *cell["command"],
        ]
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE sandbox_cells SET status='running', updated_at=? WHERE id=?",
                (utc_text(), cell_id),
            )
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=cell["timeout_seconds"],
                check=False,
            )
            result = (completed.stdout + completed.stderr)[-16000:]
            status = "completed" if completed.returncode == 0 else "failed"
        except subprocess.TimeoutExpired as exc:
            result, status = f"timeout after {cell['timeout_seconds']} seconds: {exc}", "failed"
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE sandbox_cells SET status=?, last_result=?, updated_at=? WHERE id=?",
                (status, result, utc_text(), cell_id),
            )
        return self.get_cell(cell_id)

    def prepare_user_access(
        self,
        user_id: str,
        *,
        email: str = "",
        ttl_seconds: int = 604800,
        send_email: bool = False,
    ) -> dict[str, Any]:
        user = self.repository.get_user(user_id)
        self.repository.revoke_unused_access_invitations(user_id)
        invitation_id, code, expires_at = self.repository.create_invitation(
            user_id,
            ttl_seconds,
            purpose="access_package",
        )
        health_addresses: list[str] = []
        tunnel = self.tunnels.status("zrok")
        if self.config.remote_pairing_enabled and tunnel.get("public_url"):
            health_addresses.append(str(tunnel["public_url"]).rstrip("/"))
        if self.config.remote_pairing_enabled and self.config.remote_public_url:
            health_addresses.append(self.config.remote_public_url.rstrip("/"))
        if self.config.lan_enabled:
            try:
                local_addresses = socket.gethostbyname_ex(socket.gethostname())[2]
            except OSError:
                local_addresses = []
            health_addresses.extend(
                f"https://{address}:{self.config.lan_port}"
                for address in local_addresses
                if address and not address.startswith("127.")
            )
        health_addresses.append(f"http://127.0.0.1:{self.config.port}")
        health_addresses = list(dict.fromkeys(health_addresses))
        primary_address = health_addresses[0]
        pairing_fingerprint = (
            self.tls_fingerprint
            if primary_address.startswith("https://")
            and f":{self.config.lan_port}" in primary_address
            else ""
        )
        package = {
            "format": "cloud-storage-access-v2",
            "server_name": self.config.server_name,
            "username": user.username,
            "display_name": user.display_name,
            "one_time_code": code,
            "expires_at": expires_at,
            "addresses": health_addresses,
            "certificate_fingerprint": pairing_fingerprint,
            "certificate_fingerprints": {
                address: self.tls_fingerprint
                for address in health_addresses
                if address.startswith("https://")
                and f":{self.config.lan_port}" in address
                and self.tls_fingerprint
            },
            "login_link": build_pairing_uri(
                code,
                primary_address,
                pairing_fingerprint,
                user.username,
            ),
            "personal_drive_name": f"Личный диск — {user.display_name}",
            "instructions": (
                "Откройте Client и импортируйте этот файл. Постоянный пароль вводить не нужно; "
                "устройство появится у администратора на подтверждение."
            ),
        }
        directory = self.config.data_directory / "access-packages"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"access-{invitation_id}.cloud-access.json"
        self._write_private_json(path, package)
        email_status = "not-requested"
        provider = self.integrations.notification_providers.get("email")
        if send_email and email:
            if provider is None:
                email_status = "not-configured"
            else:
                try:
                    attachment = json.dumps(package, ensure_ascii=False, indent=2).encode("utf-8")
                    provider.deliver(
                        {
                            "recipient": email,
                            "title": "Доступ к личному облаку",
                            "message": (
                                f"Здравствуйте, {user.display_name}.\n\n"
                                "Во вложении находится одноразовый файл входа Cloud Storage. "
                                "Он действует 7 дней. Импортируйте его в Client и дождитесь "
                                "подтверждения устройства администратором."
                            ),
                            "attachments": [
                                {
                                    "filename": f"cloud-storage-{user.username}.cloud-access.json",
                                    "content": attachment,
                                    "content_type": "application/json",
                                }
                            ],
                            "severity": "info",
                            "created_at": utc_text(),
                            "source": "provisioning",
                        }
                    )
                    email_status = "delivered"
                except Exception as exc:
                    email_status = f"failed: {str(exc)[:200]}"
        elif send_email:
            email_status = "recipient-missing"
        return {
            "invitation_id": invitation_id,
            "expires_at": expires_at,
            "download_path": str(path),
            "package": package,
            "email_status": email_status,
        }

    def _monitor_loop(self) -> None:
        while not self._stop.wait(30):
            try:
                self._check_power()
                self._poll_telegram_commands()
                self._run_due_reports()
                self._check_idle_sleep()
            except Exception:
                continue

    def _check_power(self) -> None:
        status, policy = self.power_status(), self.settings()["power"]
        plugged = status["plugged"]
        if plugged is False and self._last_plugged is not False and policy["notify_on_outage"]:
            remaining = status["minutes_left"]
            suffix = (
                f" Оценка: {remaining} мин."
                if remaining is not None
                else " Время работы ИБП не определено."
            )
            self.notifications.emit(
                severity="critical",
                title="Пропало внешнее питание",
                message="Сервер работает от батареи/ИБП." + suffix,
                source="power",
                source_key="mains-lost",
            )
        threshold = int(policy["notify_below_minutes"])
        if (
            plugged is False
            and status["minutes_left"] is not None
            and status["minutes_left"] <= threshold
            and not self._power_alert_sent
        ):
            self.notifications.emit(
                severity="critical",
                title="ИБП скоро разрядится",
                message=f"Осталось примерно {status['minutes_left']} мин.",
                source="power",
                source_key="ups-low",
            )
            self._power_alert_sent = True
        if plugged:
            self._power_alert_sent = False
            if self._shutdown_timer is not None:
                self._shutdown_timer.cancel()
                self._shutdown_timer = None
                self.notifications.emit(
                    severity="info",
                    title="Внешнее питание восстановлено",
                    message="Запланированное аварийное выключение отменено.",
                    source="power",
                    source_key="power-shutdown-cancelled",
                )
        self._last_plugged = plugged

    def _poll_telegram_commands(self) -> None:
        provider = self.integrations.notification_providers.get("telegram")
        if provider is None or not hasattr(provider, "poll_commands"):
            return
        self._telegram_offset, commands = provider.poll_commands(self._telegram_offset)
        for command in commands:
            summary = self.repository.summary()
            power = self.power_status()
            tunnel = self.tunnels.status("zrok")
            if command == "/status":
                answer = (
                    f"Cloud Storage: работает\nФайлов: {summary['files']}\n"
                    f"Занято: {summary['stored_bytes']} байт\n"
                    f"Устройств: {summary['trusted_devices']}\n"
                    f"zrok: {tunnel.get('state', 'disabled')}"
                )
            elif command == "/power":
                answer = (
                    f"Питание: {power['source']}\nЗаряд: {power['percent']}%\n"
                    f"Осталось: {power['minutes_left']} мин\n"
                    f"Сон активирован: {'да' if power['sleep_armed'] else 'нет'}"
                )
            elif command == "/storage":
                answer = (
                    f"Файлов: {summary['files']}\n"
                    f"Хранится: {summary['stored_bytes']} байт\n"
                    f"Пользователей: {summary['users']}"
                )
            else:
                answer = "Команды: /status, /power, /storage, /help"
            provider.send_text(answer)

    def _check_idle_sleep(self) -> None:
        policy, status = self.settings()["power"], self.power_status()
        if not policy["idle_sleep_enabled"] or not policy["allow_os_sleep"]:
            return
        if status["idle_seconds"] < status["sleep_after_seconds"]:
            return
        with self.database.connection() as connection:
            active = connection.execute(
                "SELECT count(*) FROM transfer_jobs WHERE status IN ('queued', 'running')"
            ).fetchone()[0]
        if active:
            return
        self._last_access_monotonic = time.monotonic()
        self._perform_system_sleep()

    def _handle_system_action(self, action: str) -> str:
        power = self.settings()["power"]
        if action == "sleep_after_hour":
            if not power["allow_os_sleep"]:
                return "sleep_not_armed"
            if self._sleep_timer is not None and self._sleep_timer.is_alive():
                return "sleep_already_scheduled"
            self._sleep_timer = threading.Timer(3600, self._perform_system_sleep)
            self._sleep_timer.daemon = True
            self._sleep_timer.start()
            return "sleep_scheduled:3600"
        if action == "shutdown_after_hour":
            if not power["allow_os_shutdown"]:
                return "shutdown_not_armed"
            if self._shutdown_timer is not None and self._shutdown_timer.is_alive():
                return "shutdown_already_scheduled"
            self._shutdown_timer = threading.Timer(3600, self._perform_system_shutdown)
            self._shutdown_timer.daemon = True
            self._shutdown_timer.start()
            self.notifications.emit(
                severity="critical",
                title="Запланировано безопасное выключение",
                message="Внешнего питания нет. Сервер выключится через 60 минут, если питание не восстановится.",
                source="power",
                source_key="power-shutdown-scheduled",
            )
            return "shutdown_scheduled:3600"
        raise ValueError("unsupported system automation action")

    def _perform_system_sleep(self) -> None:
        self._sleep_timer = None
        with self.database.connection() as connection:
            active = connection.execute(
                "SELECT count(*) FROM transfer_jobs WHERE status IN ('queued', 'running')"
            ).fetchone()[0]
        if active or not self.settings()["power"]["allow_os_sleep"]:
            return
        command = (
            ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"]
            if os.name == "nt"
            else ["pmset", "sleepnow"]
            if sys_platform() == "darwin"
            else ["systemctl", "suspend"]
        )
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )

    def _perform_system_shutdown(self) -> None:
        self._shutdown_timer = None
        power = self.power_status()
        with self.database.connection() as connection:
            active = connection.execute(
                "SELECT count(*) FROM transfer_jobs WHERE status IN ('queued', 'running')"
            ).fetchone()[0]
        if power.get("plugged") is not False or active:
            return
        if not self.settings()["power"]["allow_os_shutdown"]:
            return
        command = (
            ["shutdown.exe", "/s", "/t", "0"]
            if os.name == "nt"
            else ["osascript", "-e", 'tell application "System Events" to shut down']
            if sys_platform() == "darwin"
            else ["systemctl", "poweroff"]
        )
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )

    def _run_due_reports(self) -> None:
        now = utc_text()
        for report in self.list_reports():
            if not report["enabled"] or not report["next_run_at"] or report["next_run_at"] > now:
                continue
            self.generate_report(
                report["sections"],
                schedule_id=report["id"],
                delivery_channels=report["delivery_channels"],
            )
            next_run = datetime.now(UTC) + timedelta(hours=report["interval_hours"])
            with self.database.transaction() as connection:
                connection.execute(
                    "UPDATE report_schedules SET last_run_at=?, next_run_at=?, updated_at=? WHERE id=?",
                    (now, utc_text(next_run), now, report["id"]),
                )

    def _load_secrets(self) -> dict[str, str]:
        try:
            value = json.loads(self.secrets_path.read_text(encoding="utf-8"))
            return (
                {str(key): str(item) for key, item in value.items()}
                if isinstance(value, dict)
                else {}
            )
        except (OSError, ValueError, json.JSONDecodeError):
            return {}

    def _save_secrets(self, secrets: dict[str, str]) -> None:
        self._write_private_json(self.secrets_path, secrets)

    @staticmethod
    def _write_private_json(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=path.stem + "-", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            if os.name != "nt":
                temporary.chmod(0o600)
            os.replace(temporary, path)
            _restrict_secret_file(path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _validate_settings(value: dict[str, Any]) -> None:
        if value["profile"] not in SYSTEM_PRESETS:
            raise ValueError("invalid system profile")
        if value["interface_mode"] not in {"simple", "detailed"}:
            raise ValueError("invalid interface mode")
        if value["browser_access"] not in {"all", "approved", "nobody"}:
            raise ValueError("invalid browser access policy")
        if value["security"]["mode"] not in {"basic", "advanced"}:
            raise ValueError("invalid security mode")
        if not 5 <= int(value["power"]["idle_minutes"]) <= 1440:
            raise ValueError("idle sleep must be between 5 and 1440 minutes")
        if not 10 <= int(value["sandbox"]["resource_percent"]) <= 80:
            raise ValueError("sandbox resource percent must be between 10 and 80")
        url = str(value["integrations"]["webhook"].get("url", ""))
        if url and not url.startswith("https://"):
            raise ValueError("webhook URL must use HTTPS")

    @staticmethod
    def _report_schedule(row: Any) -> dict[str, Any]:
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        item["sections"] = json.loads(item.pop("sections_json"))
        item["delivery_channels"] = json.loads(item.pop("delivery_channels_json"))
        return item

    @staticmethod
    def _cell(row: Any) -> dict[str, Any]:
        item = dict(row)
        item["command"] = json.loads(item.pop("command_json"))
        item["network_enabled"] = bool(item["network_enabled"])
        return item


def sys_platform() -> str:
    return platform.system().casefold()
