from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from cloud_storage import __version__
from cloud_storage.core.config import CoreConfig
from cloud_storage.help.knowledge import KnowledgeBase
from cloud_storage.help.page import HelpPage
from cloud_storage.models import (
    AppSettings,
    DiskConfiguration,
    DiskMode,
    DiskRole,
    DiskSnapshot,
)
from cloud_storage.pairing import build_pairing_uri, build_server_code
from cloud_storage.services.audit_log import AuditLog
from cloud_storage.services.core_client import (
    CoreApiError,
    CoreClient,
    CoreSupervisor,
    CoreUnavailable,
)
from cloud_storage.services.disk_service import DiskService, mark_missing_disks
from cloud_storage.services.password_vault import ManagerPasswordVault
from cloud_storage.services.settings_store import SettingsStore
from cloud_storage.ui.connection_page import ConnectionPage
from cloud_storage.ui.control_page import ControlCenterPage
from cloud_storage.ui.dialogs import (
    CreateUserDialog,
    DiskDetailDialog,
    InvitationDialog,
    PasswordDialog,
    SetupDialog,
    SpaceDialog,
    UserEditDialog,
)
from cloud_storage.ui.pages import DashboardPage, DisksPage, SettingsPage
from cloud_storage.ui.transfer_page import TransfersPage
from cloud_storage.ui.update_page import UpdatePage


class _CoreHealthSignals(QObject):
    completed = Signal(object)
    finished = Signal()


class _CoreHealthTask(QRunnable):
    def __init__(self, client: CoreClient) -> None:
        super().__init__()
        self.client = client
        self.signals = _CoreHealthSignals()

    def run(self) -> None:
        try:
            self.signals.completed.emit(self.client.try_health(timeout=0.4))
        except Exception:  # Background health polling must never terminate the UI.
            self.signals.completed.emit(None)
        finally:
            self.signals.finished.emit()


class MainWindow(QMainWindow):
    def __init__(
        self,
        store: SettingsStore | None = None,
        disk_service: DiskService | None = None,
    ) -> None:
        super().__init__()
        custom_store = store is not None
        self.store = store or SettingsStore()
        self.settings: AppSettings = self.store.load()
        self.audit = AuditLog(self.store.data_directory)
        self.password_vault = ManagerPasswordVault(self.store.data_directory)
        self.knowledge = KnowledgeBase(self.store.data_directory / "knowledge.db")
        self.disk_service = disk_service or DiskService()
        environment_config = CoreConfig.from_environment()
        core_config = CoreConfig(
            data_directory=(
                self.store.data_directory if custom_store else environment_config.data_directory
            ),
            host=environment_config.host,
            port=environment_config.port,
            lan_enabled=self.settings.lan_enabled,
            lan_host=environment_config.lan_host,
            lan_port=self.settings.lan_port,
            discovery_port=environment_config.discovery_port,
            remote_enabled=self.settings.remote_enabled,
            remote_host=environment_config.remote_host,
            remote_port=self.settings.remote_port,
            remote_public_url=self.settings.remote_public_url,
            remote_pairing_enabled=self.settings.remote_pairing_enabled,
            zrok_enabled=self.settings.zrok_enabled,
            zrok_host=environment_config.zrok_host,
            zrok_port=self.settings.zrok_port,
            zrok_executable=self.settings.zrok_executable,
            zrok_share_name=self.settings.zrok_share_name,
            server_name=self.settings.server_name,
            max_upload_bytes=environment_config.max_upload_bytes,
            pairing_ttl_seconds=environment_config.pairing_ttl_seconds,
        )
        self.core_client = CoreClient(core_config)
        self.core_supervisor = CoreSupervisor(self.core_client)
        self.core_health: dict | None = None
        self.core_summary: dict | None = None
        self.core_users: list[dict] = []
        self.core_spaces: list[dict] = []
        self.core_devices: list[dict] = []
        self.core_audit: list[dict] = []
        self.core_storage_roots: list[dict] = []
        self.core_maintenance_jobs: list[dict] = []
        self.core_backup_jobs: list[dict] = []
        self.core_restore_jobs: list[dict] = []
        self.core_backup_automation: dict = {"policies": [], "verifications": []}
        self.core_mirror_state: dict = {"roots": [], "jobs": []}
        self.core_diagnostics: dict = {}
        self.core_tunnels: dict = {"zrok": {"enabled": False, "state": "disabled"}}
        self.core_automation: dict = {"rules": [], "runs": []}
        self.core_notifications: list[dict] = []
        self.core_integrations: dict = {"plugins": []}
        self.core_control: dict = {}
        self.core_transfers: dict = {
            "settings": {},
            "inbound": [],
            "outbound": [],
            "counts": {},
        }
        self.core_server_mode: dict = {
            "mode": "normal",
            "reason": "",
            "changed_at": "",
        }
        self.disks: list[DiskSnapshot] = []
        self._disk_reminder_hidden = False
        self._setup_banner_hidden = False
        self._nav_buttons: list[QPushButton] = []
        self._nav_pages: list[QWidget] = []
        self._health_task: _CoreHealthTask | None = None

        self.setWindowTitle(f"Cloud Storage Server Manager · {__version__}")
        self.setMinimumSize(1040, 700)
        self.resize(1380, 860)
        self._build_ui()
        self._connect_pages()

        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self._poll_core_health)
        self._reset_refresh_timer()
        QTimer.singleShot(0, self.refresh_disks)

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("AppRoot")
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(244)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(18, 22, 18, 20)
        sidebar_layout.setSpacing(8)
        brand = QHBoxLayout()
        mark = QLabel("CS")
        mark.setObjectName("BrandMark")
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name_box = QVBoxLayout()
        name = QLabel("CLOUD STORAGE")
        name.setObjectName("Brand")
        beta = QLabel(f"SERVER MANAGER · {__version__}")
        beta.setProperty("muted", True)
        beta.setStyleSheet("font-size: 10px;")
        name_box.addWidget(name)
        name_box.addWidget(beta)
        brand.addWidget(mark)
        brand.addLayout(name_box, 1)
        sidebar_layout.addLayout(brand)
        sidebar_layout.addSpacing(26)

        self.stack = QStackedWidget()
        self.dashboard_page = DashboardPage()
        self.connection_page = ConnectionPage()
        self.disks_page = DisksPage()
        self.receive_page = TransfersPage("inbound")
        self.send_page = TransfersPage("outbound")
        self.control_page = ControlCenterPage()
        self.settings_page = SettingsPage()
        self.update_page = UpdatePage(
            "server",
            self.store.data_directory,
            before_install=self._prepare_server_update,
        )
        self.help_page = HelpPage(self.knowledge, "server")
        for label, page in (
            ("⌂   Основная", self.dashboard_page),
            ("⌁   Подключение", self.connection_page),
            ("▣   Диски", self.disks_page),
            ("↓   Приём", self.receive_page),
            ("↑   Отправка", self.send_page),
            ("⚙   Настройки", self.settings_page),
            ("↻   Обновления", self.update_page),
            ("?   Помощь", self.help_page),
        ):
            button = QPushButton(label)
            button.setProperty("nav", True)
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, p=page: self._show_page(p))
            sidebar_layout.addWidget(button)
            self._nav_buttons.append(button)
            self._nav_pages.append(page)
            self.stack.addWidget(page)
        system_button = QPushButton("◆   Система")
        system_button.setProperty("nav", True)
        system_button.setCheckable(True)
        system_button.clicked.connect(lambda checked=False: self._show_page(self.control_page))
        sidebar_layout.addWidget(system_button)
        self._nav_buttons.append(system_button)
        self._nav_pages.append(self.control_page)
        self.stack.addWidget(self.control_page)
        self._nav_buttons[0].setChecked(True)
        sidebar_layout.addStretch()

        safety = QFrame()
        safety.setProperty("card", True)
        safety_layout = QVBoxLayout(safety)
        safety_layout.setContentsMargins(12, 10, 12, 10)
        safety_title = QLabel("БЕЗОПАСНЫЙ РЕЖИМ")
        safety_title.setStyleSheet("font-weight: 700; font-size: 11px; color: #43c778;")
        safety_text = QLabel("Нет форматирования · перенос только с SHA-256")
        safety_text.setWordWrap(True)
        safety_text.setProperty("muted", True)
        safety_layout.addWidget(safety_title)
        safety_layout.addWidget(safety_text)
        sidebar_layout.addWidget(safety)
        layout.addWidget(sidebar)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(22, 20, 12, 12)
        quick_row = QHBoxLayout()
        quick_row.addStretch()
        self.quick_navigation = QPushButton("Разделы ▾")
        self.quick_navigation.setProperty("primary", True)
        self.quick_navigation.setMinimumWidth(220)
        quick_menu = QMenu(self.quick_navigation)
        for label, page in zip(
            (
                "Основная",
                "Подключение",
                "Диски",
                "Приём",
                "Отправка",
                "Настройки",
                "Обновления",
                "Помощь",
                "Система",
            ),
            self._nav_pages,
            strict=True,
        ):
            action = quick_menu.addAction(label)
            action.triggered.connect(lambda checked=False, target=page: self._show_page(target))
        self.quick_navigation.setMenu(quick_menu)
        self.quick_navigation.setVisible(False)
        quick_row.addWidget(self.quick_navigation)
        quick_row.addStretch()
        content_layout.addLayout(quick_row)
        content_layout.addWidget(self.stack)
        layout.addWidget(content, 1)

    def _connect_pages(self) -> None:
        self.dashboard_page.setup_requested.connect(self.open_setup)
        self.dashboard_page.remind_later_requested.connect(self.hide_setup_reminder)
        self.connection_page.panel.generation_requested.connect(
            self.generate_connection_code
        )
        self.connection_page.panel.create_user_requested.connect(self.add_user)
        self.connection_page.panel.edit_user_requested.connect(self.edit_user)
        self.connection_page.panel.reset_password_requested.connect(self.reset_user_password)
        self.connection_page.panel.email_access_requested.connect(self.send_user_access_email)
        self.connection_page.panel.refresh_button.clicked.connect(self.refresh_core)
        self.disks_page.disk_selected.connect(self.open_disk)
        self.disks_page.refresh_requested.connect(self.refresh_disks)
        self.disks_page.configure_first_requested.connect(self.configure_first_unconfigured)
        self.disks_page.remind_later_requested.connect(self.hide_disk_reminder)
        self.disks_page.ignore_unconfigured_requested.connect(self.ignore_unconfigured)
        self.disks_page.create_space_requested.connect(self.create_space)
        self.disks_page.edit_space_requested.connect(self.edit_space)
        self.receive_page.refresh_requested.connect(self.refresh_core)
        self.receive_page.retry_requested.connect(self.retry_transfer)
        self.receive_page.configure_cache_requested.connect(
            lambda: self._show_page(self.disks_page)
        )
        self.send_page.refresh_requested.connect(self.refresh_core)
        self.control_page.refresh_requested.connect(self.refresh_core)
        self.control_page.settings_save_requested.connect(self.save_control_settings)
        self.control_page.preset_requested.connect(self.apply_system_preset)
        self.control_page.template_requested.connect(self.install_automation_template)
        self.control_page.report_requested.connect(self.create_control_report)
        self.control_page.cell_create_requested.connect(self.create_sandbox_cell)
        self.control_page.cell_run_requested.connect(self.run_sandbox_cell)
        self.control_page.open_container_manager_requested.connect(
            self.open_container_manager
        )
        self.control_page.docker_install_requested.connect(self.install_docker_desktop)
        self.control_page.ssh_enable_requested.connect(self.enable_windows_ssh)
        self.control_page.ssh_disable_requested.connect(self.disable_windows_ssh)
        self.control_page.open_updates_requested.connect(lambda: self._show_page(self.update_page))
        self.control_page.open_network_settings_requested.connect(
            lambda: self._show_page(self.settings_page)
        )
        self.control_page.integration_test_requested.connect(self.test_integration)
        self.control_page.wake_on_lan_requested.connect(self.send_wake_on_lan)
        self.help_page.navigate_requested.connect(self.navigate_to_route)
        self.settings_page.save_requested.connect(self.save_general_settings)
        self.settings_page.advanced_mode_changed.connect(self.save_advanced_mode)
        self.settings_page.refresh_requested.connect(self.refresh_disks)
        self.settings_page.core_start_requested.connect(self.start_core)
        self.settings_page.core_stop_requested.connect(self.stop_core)
        self.settings_page.core_refresh_requested.connect(self.refresh_core)
        self.settings_page.add_user_requested.connect(self.add_user)
        self.settings_page.invitation_requested.connect(self.create_invitation)
        self.settings_page.reset_password_requested.connect(self.reset_user_password)
        self.settings_page.approve_device_requested.connect(self.approve_device)
        self.settings_page.revoke_device_requested.connect(self.revoke_device)
        self.settings_page.migration_requested.connect(self.start_migration)
        self.settings_page.maintenance_refresh_requested.connect(self.refresh_core)
        self.settings_page.maintenance_resume_requested.connect(self.resume_maintenance_job)
        self.settings_page.maintenance_cancel_requested.connect(self.cancel_maintenance_job)
        self.settings_page.backup_requested.connect(self.start_backup)
        self.settings_page.backup_resume_requested.connect(self.resume_backup)
        self.settings_page.backup_cancel_requested.connect(self.cancel_backup)
        self.settings_page.backup_policy_requested.connect(self.save_backup_policy)
        self.settings_page.backup_policy_run_requested.connect(self.run_backup_policy)
        self.settings_page.backup_verify_requested.connect(self.verify_backup)
        self.settings_page.backup_verification_cancel_requested.connect(
            self.cancel_backup_verification
        )
        self.settings_page.restore_requested.connect(self.start_restore)
        self.settings_page.restore_resume_requested.connect(self.resume_restore)
        self.settings_page.restore_cancel_requested.connect(self.cancel_restore)
        self.settings_page.server_read_only_requested.connect(self.enter_read_only_mode)
        self.settings_page.server_normal_requested.connect(self.leave_read_only_mode)
        self.settings_page.mirror_reconcile_requested.connect(self.reconcile_mirror)
        self.settings_page.mirror_resume_requested.connect(self.resume_mirror_job)
        self.settings_page.mirror_cancel_requested.connect(self.cancel_mirror_job)
        self.settings_page.diagnostics_quick_requested.connect(
            lambda: self.start_diagnostic_scan("quick")
        )
        self.settings_page.diagnostics_full_requested.connect(
            lambda: self.start_diagnostic_scan("full")
        )
        self.settings_page.diagnostic_remediation_requested.connect(
            self.remediate_diagnostic_incident
        )
        self.settings_page.tunnel_restart_requested.connect(self.restart_tunnel)
        self.settings_page.support_bundle_requested.connect(self.export_support_bundle)
        self.settings_page.automation_rule_create_requested.connect(self.create_automation_rule)
        self.settings_page.automation_rule_update_requested.connect(self.update_automation_rule)
        self.settings_page.automation_rule_delete_requested.connect(self.delete_automation_rule)
        self.settings_page.automation_evaluate_requested.connect(self.evaluate_automation)
        self.settings_page.automation_preview_requested.connect(self.preview_automation)
        self.settings_page.automation_rule_preview_requested.connect(self.preview_automation_rule)
        self.settings_page.automation_settings_requested.connect(self.update_automation_settings)
        self.settings_page.notification_acknowledge_requested.connect(self.acknowledge_notification)
        self.settings_page.integration_test_requested.connect(self.test_integration)
        self.settings_page.open_updates_requested.connect(lambda: self._show_page(self.update_page))
        self.settings_page.disks_requested.connect(lambda: self._show_page(self.disks_page))
        self.settings_page.system_section_requested.connect(self._open_system_section)
        self.settings_page.connection_generation_requested.connect(
            self.generate_connection_code
        )

    def _prepare_server_update(self) -> None:
        if self.core_health is None:
            return
        try:
            stopped = self.core_supervisor.stop()
        except (CoreApiError, CoreUnavailable, OSError) as exc:
            raise OSError(f"Не удалось остановить сервер перед обновлением: {exc}") from exc
        if not stopped:
            raise OSError("Сервер не остановился перед обновлением")
        self.core_health = None

    def _show_page(self, page: QWidget) -> None:
        self.stack.setCurrentWidget(page)
        for index, button in enumerate(self._nav_buttons):
            button.setChecked(self.stack.widget(index) is page)

    def _open_system_section(self, route: str) -> None:
        self.control_page.open_tab(route)
        self._show_page(self.control_page)

    def open_container_manager(self) -> None:
        if getattr(sys, "frozen", False):
            install_root = Path(sys.executable).resolve().parent.parent
            candidates = [
                install_root / "ContainerManager" / "CloudStorageContainerManager.exe",
                install_root
                / "CloudStorageContainerManager"
                / "CloudStorageContainerManager.exe",
            ]
            executable = next((path for path in candidates if path.exists()), candidates[0])
            command = [str(executable)]
            if not executable.exists():
                QMessageBox.warning(
                    self,
                    "Менеджер контейнеров не найден",
                    f"Переустановите Server Manager: отсутствует {executable}",
                )
                return
        else:
            command = [sys.executable, "-m", "cloud_storage.container_manager.main"]
        arguments: dict[str, object] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
        }
        if os.name == "nt":
            arguments["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            subprocess.Popen(command, **arguments)
        except OSError as exc:
            QMessageBox.warning(self, "Не удалось открыть контейнеры", str(exc))

    def _apply_interface_mode(self, mode: str) -> None:
        simple = mode == "simple"
        visible_in_simple = {
            self.dashboard_page,
            self.connection_page,
            self.disks_page,
            self.help_page,
            self.control_page,
        }
        for button, page in zip(self._nav_buttons, self._nav_pages, strict=True):
            button.setVisible(not simple or page in visible_in_simple)
        self.quick_navigation.setVisible(simple)

    def navigate_to_route(self, route: str) -> None:
        routes = {
            "dashboard": self.dashboard_page,
            "connection": self.connection_page,
            "disks": self.disks_page,
            "receive": self.receive_page,
            "send": self.send_page,
            "system": self.control_page,
            "settings": self.settings_page,
            "updates": self.update_page,
            "help": self.help_page,
        }
        self._show_page(routes.get(route, self.help_page))

    def refresh_disks(self) -> None:
        self._refresh_core_state()
        try:
            discovered = self.disk_service.discover()
        except Exception as exc:  # UI boundary: discovery failures must not crash the manager
            self.audit.record("disk.scan.failed", f"Ошибка обнаружения дисков: {exc}", "warning")
            QMessageBox.warning(
                self,
                "Не удалось обновить диски",
                "Менеджер продолжит работу с последними данными. Подробность записана в журнал.",
            )
            self._refresh_pages()
            return
        self._reconcile_disk_identities(discovered)
        new_ids = [item.id for item in discovered if item.id not in self.settings.known_disk_ids]
        if new_ids:
            self.settings.known_disk_ids.extend(new_ids)
            self.settings.known_disk_ids = list(dict.fromkeys(self.settings.known_disk_ids))
            for disk in discovered:
                if disk.id in new_ids:
                    self.audit.record(
                        "disk.discovered",
                        f"Обнаружен накопитель {disk.label or disk.mountpoint} ({disk.mountpoint})",
                    )
            self.store.save(self.settings)
        self.disks = mark_missing_disks(discovered, self.settings)
        self._refresh_pages()

    def _reconcile_disk_identities(self, discovered: list[DiskSnapshot]) -> None:
        """Move saved roles when Windows reports the same volume under a new transient id."""
        changed = False
        for disk in discovered:
            if disk.id in self.settings.disk_configurations:
                continue
            mount = os.path.normcase(os.path.abspath(disk.mountpoint))
            serial = (disk.serial or "").strip().casefold()
            matches: list[str] = []
            for old_id, config in self.settings.disk_configurations.items():
                old_mount = (
                    os.path.normcase(os.path.abspath(config.identity_mountpoint))
                    if config.identity_mountpoint
                    else ""
                )
                old_serial = config.identity_serial.strip().casefold()
                if (serial and old_serial == serial) or (old_mount and old_mount == mount):
                    matches.append(old_id)
            if len(matches) != 1:
                continue
            old_id = matches[0]
            config = self.settings.disk_configurations.pop(old_id)
            config.identity_mountpoint = disk.mountpoint
            config.identity_device = disk.device
            config.identity_serial = disk.serial or ""
            self.settings.disk_configurations[disk.id] = config
            self.settings.known_disk_ids = [
                disk.id if item == old_id else item for item in self.settings.known_disk_ids
            ]
            self.settings.ignored_disk_ids = [
                disk.id if item == old_id else item for item in self.settings.ignored_disk_ids
            ]
            self.audit.record(
                "disk.identity.migrated",
                f"Восстановлено назначение диска {disk.mountpoint} после смены системного ID",
            )
            changed = True
        if changed:
            self.store.save(self.settings)

    def _refresh_pages(self) -> None:
        events = self.audit.recent()
        users_for_ui = [
            {
                **user,
                "managed_password": self.password_vault.load(str(user.get("id") or "")),
            }
            for user in self.core_users
            if user.get("id")
        ]
        self.dashboard_page.update_data(
            self.disks,
            self.settings,
            events,
            core_health=self.core_health,
            core_summary=self.core_summary,
        )
        self.dashboard_page.set_setup_banner_visible(
            not self.settings.setup_complete and not self._setup_banner_hidden
        )
        self.disks_page.update_data(
            self.disks,
            self.settings,
            reminder_hidden=self._disk_reminder_hidden,
            spaces=self.core_spaces,
            storage_roots=self.core_storage_roots,
        )
        self.connection_page.panel.set_data(
            self.core_health,
            users_for_ui,
            self.core_tunnels,
        )
        self.settings_page.load_settings(self.settings, str(self.store.path))
        self.settings_page.update_core_data(
            self.core_health,
            self.core_summary,
            users_for_ui,
            self.core_devices,
            self.core_storage_roots,
            self.core_maintenance_jobs,
            self.core_backup_jobs,
            self.core_mirror_state,
            self.core_diagnostics,
            self.core_server_mode,
            self.core_restore_jobs,
            self.core_backup_automation,
            self.core_audit,
            self.core_tunnels,
            self.core_automation,
            self.core_notifications,
            self.core_integrations,
            spaces=self.core_spaces,
        )
        self.receive_page.update_data(self.core_transfers, online=self.core_health is not None)
        self.send_page.update_data(self.core_transfers, online=self.core_health is not None)
        self.control_page.update_data(self.core_control, online=self.core_health is not None)
        self._apply_interface_mode(
            str(self.core_control.get("settings", {}).get("interface_mode", "detailed"))
        )
        unconfigured = sum(
            item.available
            and self.settings.configuration_for(item.id).role == DiskRole.UNCONFIGURED
            for item in self.disks
        )
        self._nav_buttons[2].setText(
            f"▣   Диски   • {unconfigured}" if unconfigured else "▣   Диски"
        )
        counts = self.core_transfers.get("counts", {})
        inbound_active = int(counts.get("inbound_active", 0))
        outbound_active = int(counts.get("outbound_active", 0))
        self._nav_buttons[3].setText(
            f"↓   Приём   • {inbound_active}" if inbound_active else "↓   Приём"
        )
        self._nav_buttons[4].setText(
            f"↑   Отправка   • {outbound_active}" if outbound_active else "↑   Отправка"
        )

    def _refresh_core_state(self) -> None:
        self.core_health = self.core_client.try_health()
        self.core_summary = None
        self.core_users = []
        self.core_spaces = []
        self.core_devices = []
        self.core_audit = []
        self.core_storage_roots = []
        self.core_maintenance_jobs = []
        self.core_backup_jobs = []
        self.core_restore_jobs = []
        self.core_backup_automation = {"policies": [], "verifications": []}
        self.core_mirror_state = {"roots": [], "jobs": []}
        self.core_diagnostics = {}
        self.core_tunnels = {"zrok": {"enabled": False, "state": "disabled"}}
        self.core_automation = {"rules": [], "runs": []}
        self.core_notifications = []
        self.core_integrations = {"plugins": []}
        self.core_control = {}
        self.core_transfers = {
            "settings": {},
            "inbound": [],
            "outbound": [],
            "counts": {},
        }
        self.core_server_mode = {"mode": "normal", "reason": "", "changed_at": ""}
        if not self.core_health:
            return
        try:
            self.core_summary = self.core_client.summary()
            self.core_users = self.core_client.list_users()
            self.core_spaces = self.core_client.list_spaces_admin()
            self.core_devices = self.core_client.list_devices()
            self.core_audit = self.core_client.list_audit(100)
            self.core_storage_roots = self.core_client.list_storage_roots()
            self.core_maintenance_jobs = self.core_client.list_maintenance_jobs()
            self.core_backup_jobs = self.core_client.list_backups()
            self.core_restore_jobs = self.core_client.list_restores()
            self.core_backup_automation = self.core_client.backup_automation()
            self.core_mirror_state = self.core_client.mirror_status()
            self.core_diagnostics = self.core_client.diagnostics()
            self.core_tunnels = self.core_client.tunnels()
            self.core_automation = self.core_client.automation()
            self.core_notifications = self.core_client.list_notifications(limit=100)
            self.core_integrations = self.core_client.integrations()
            self.core_control = self.core_client.control_center()
            self.core_transfers = self.core_client.transfers(limit=100)
            self.core_server_mode = self.core_client.server_mode()
        except (CoreApiError, CoreUnavailable) as exc:
            self.audit.record(
                "core.read.failed", f"Не удалось прочитать состояние ядра: {exc}", "warning"
            )

    def refresh_core(self) -> None:
        self._refresh_core_state()
        self._refresh_pages()

    def _poll_core_health(self) -> None:
        """Poll the lightweight endpoint off the UI thread.

        Heavy disk discovery and the full set of administrative endpoints are refreshed on
        startup, after mutations, and when the user presses Refresh. This prevents a periodic
        PowerShell/API burst from freezing open tabs or discarding unsaved edits.
        """

        if self._health_task is not None:
            return
        task = _CoreHealthTask(self.core_client)
        self._health_task = task
        task.signals.completed.connect(self._apply_polled_health)
        task.signals.finished.connect(self._health_poll_finished)
        QThreadPool.globalInstance().start(task)

    def _apply_polled_health(self, health: object) -> None:
        was_online = self.core_health is not None
        now_online = isinstance(health, dict)
        if was_online == now_online:
            if now_online:
                self.core_health = health
            return
        # A state transition is rare and deserves one complete refresh so every page agrees.
        self.refresh_core()

    def _health_poll_finished(self) -> None:
        self._health_task = None

    def start_core(self) -> None:
        try:
            health = self.core_supervisor.start()
        except (CoreApiError, CoreUnavailable, OSError) as exc:
            self.audit.record("core.start.failed", f"Ядро не запущено: {exc}", "error")
            QMessageBox.critical(
                self,
                "Ядро не запущено",
                f"{exc}\n\nПодробный журнал: {self.core_client.config.log_path}",
            )
            return
        self.audit.record("core.started", f"Серверное ядро {health.get('version', '')} запущено")
        self.core_health = health
        self._sync_storage_roots(show_errors=True)
        self.refresh_core()

    def stop_core(self) -> None:
        response = QMessageBox.question(
            self,
            "Остановить серверное ядро?",
            "Новые подключения и файловые операции станут недоступны. Файлы и настройки "
            "останутся на месте.",
        )
        if response != QMessageBox.StandardButton.Yes:
            return
        try:
            stopped = self.core_supervisor.stop()
        except (CoreApiError, CoreUnavailable, OSError) as exc:
            QMessageBox.warning(self, "Не удалось остановить ядро", str(exc))
            return
        if not stopped:
            QMessageBox.warning(
                self,
                "Ядро ещё работает",
                "Безопасное завершение не закончилось вовремя. Принудительная остановка не выполнялась.",
            )
            return
        self.audit.record("core.stopped", "Серверное ядро безопасно остановлено")
        self.refresh_core()

    def add_user(self) -> None:
        if not self.core_health:
            QMessageBox.information(self, "Ядро выключено", "Сначала запустите серверное ядро.")
            return
        dialog = CreateUserDialog(self.core_spaces, self)
        if not dialog.exec():
            return
        values = dialog.values()
        try:
            created = self.core_client.create_user(**values)
            access = created.get("access_package") or {}
            package = access.get("package") or {}
            invitation = (
                {
                    "id": access["invitation_id"],
                    "code": package["one_time_code"],
                    "expires_at": access["expires_at"],
                }
                if access
                else self.core_client.create_invitation(created["user"]["id"])
            )
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Пользователь не создан", self._core_error_text(exc))
            return
        self.audit.record(
            "core.user.created", f"Создан пользователь {created['user']['display_name']}"
        )
        self.password_vault.store(str(created["user"]["id"]), str(values["password"]))
        self._show_invitation(
            invitation["id"],
            created["user"]["display_name"],
            invitation["code"],
            invitation["expires_at"],
            str(created["user"].get("username") or values["username"]),
            str(values["password"]),
            user_id=str(created["user"]["id"]),
            access_path=str(access.get("download_path") or ""),
            email=str(values.get("email") or ""),
        )
        self.refresh_core()

    def create_space(self) -> None:
        if not self.core_health:
            QMessageBox.information(self, "Ядро выключено", "Сначала запустите серверное ядро.")
            return
        dialog = SpaceDialog(self.core_storage_roots, parent=self)
        if not dialog.exec():
            return
        values = dialog.values()
        values.pop("enabled", None)
        try:
            created = self.core_client.create_space(values)
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Пространство не создано", self._core_error_text(exc))
            return
        self.audit.record(
            "core.space.created", f"Создано пространство {created.get('name', '')}"
        )
        self.refresh_core()

    def edit_user(self, user_id: str) -> None:
        user = next(
            (item for item in self.core_users if str(item.get("id")) == user_id),
            None,
        )
        if user is None:
            return
        dialog = UserEditDialog(user, self.core_spaces, self)
        if not dialog.exec():
            return
        values, grants = dialog.values()
        try:
            self.core_client.update_user(user_id, values)
            for space_id, capabilities in grants.items():
                self.core_client.set_space_member(space_id, user_id, capabilities)
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Пользователь не сохранён", self._core_error_text(exc))
            return
        self.audit.record(
            "core.user.updated", f"Обновлён пользователь {user.get('display_name', '')}"
        )
        self.refresh_core()

    def edit_space(self, space_id: str) -> None:
        space = next(
            (item for item in self.core_spaces if str(item.get("id")) == space_id),
            None,
        )
        if space is None:
            return
        dialog = SpaceDialog(self.core_storage_roots, space, self)
        if not dialog.exec():
            return
        try:
            self.core_client.update_space(space_id, dialog.values())
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Пространство не сохранено", self._core_error_text(exc))
            return
        self.audit.record(
            "core.space.updated", f"Обновлено пространство {space.get('name', '')}"
        )
        self.refresh_core()

    def create_invitation(self, user_id: str, display_name: str) -> None:
        try:
            access = self.core_client.prepare_user_access(user_id, ttl_seconds=604800)
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Код не создан", self._core_error_text(exc))
            return
        invitation = access["package"]
        selected_user = next(
            (item for item in self.core_users if str(item.get("id")) == user_id), {}
        )
        self._show_invitation(
            access["invitation_id"],
            display_name,
            invitation["one_time_code"],
            access["expires_at"],
            str(selected_user.get("username") or ""),
            user_id=user_id,
            access_path=str(access.get("download_path") or ""),
            email=str(selected_user.get("email") or ""),
        )

    def generate_connection_code(self, user_id: str, username: str, mode: str) -> None:
        panel = self.connection_page.panel
        endpoint, fingerprint = panel.selected_endpoint(mode)
        if not endpoint:
            panel.show_error("Подходящий защищённый адрес сейчас недоступен.")
            if self.settings_page.connection_panel is not None:
                self.settings_page.connection_panel.show_error(
                    "Подходящий защищённый адрес сейчас недоступен."
                )
            return
        try:
            access = self.core_client.prepare_user_access(user_id, ttl_seconds=900)
            package = access["package"]
            connection_code = build_server_code(endpoint, fingerprint, username)
            alternatives = [
                str(address).rstrip("/")
                for address in package.get("addresses", [])
                if str(address).rstrip("/") != endpoint
                and (
                    mode == "internet"
                    or not str(address).startswith("http://127.")
                )
            ]
            pairing_link = build_pairing_uri(
                package["one_time_code"],
                endpoint,
                fingerprint,
                username,
                alternate_addresses=alternatives,
            )
        except (CoreApiError, CoreUnavailable, ValueError) as exc:
            message = f"Код не создан: {self._core_error_text(exc)}"
            panel.show_error(message)
            if self.settings_page.connection_panel is not None:
                self.settings_page.connection_panel.show_error(message)
            return
        scope = "из любой сети" if mode == "internet" else "в локальной сети"
        detail = (
            f"Готово: ссылка и QR работают {scope} до {access['expires_at']}. "
            "После первого входа устройство появится в списке на подтверждение."
        )
        panel.show_code(connection_code, detail, pairing_link)
        if getattr(self.settings_page, "connection_panel", None) is not None:
            self.settings_page.connection_panel.show_code(
                connection_code, detail, pairing_link
            )
        self.audit.record(
            "core.connection_code.created",
            f"Создан код подключения для {username or user_id} ({scope})",
        )

    def reset_user_password(self, user_id: str, display_name: str) -> None:
        dialog = PasswordDialog(display_name, self)
        if not dialog.exec():
            return
        new_password = dialog.value()
        try:
            self.core_client.set_user_password(user_id, new_password)
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Пароль не изменён", self._core_error_text(exc))
            return
        self.audit.record(
            "core.user.password.changed", f"Изменён пароль пользователя {display_name}"
        )
        self.password_vault.store(user_id, new_password)
        QMessageBox.information(
            self,
            "Пароль изменён",
            "Новый пароль сохранён и скопирован в буфер обмена. "
            "Активные интернет-сессии отозваны.",
        )
        QApplication.clipboard().setText(new_password)
        self.refresh_core()

    def _show_invitation(
        self,
        invitation_id: str,
        display_name: str,
        code: str,
        expires_at: str,
        username: str = "",
        password: str = "",
        user_id: str = "",
        access_path: str = "",
        email: str = "",
    ) -> None:
        lan = self.core_health.get("lan") if self.core_health else None
        remote = self.core_health.get("remote") if self.core_health else None
        endpoints = lan.get("endpoints", []) if lan else []
        use_remote = bool(remote and remote.get("pairing_enabled"))
        dialog = InvitationDialog(
            invitation_id,
            display_name,
            code,
            expires_at,
            (
                str(remote.get("public_url", ""))
                if use_remote
                else str(endpoints[0])
                if endpoints
                else ""
            ),
            (
                str(remote.get("fingerprint", ""))
                if use_remote
                else str(lan.get("fingerprint", ""))
                if lan
                else ""
            ),
            username=username,
            password=password,
            access_path=access_path,
            email=email,
            parent=self,
        )
        dialog.cancel_requested.connect(self.cancel_invitation)
        if user_id:
            dialog.email_requested.connect(
                lambda _invitation_id, recipient: self.send_user_access_email(
                    user_id, recipient
                )
            )
        dialog.exec()

    def send_user_access_email(self, user_id: str, email: str) -> None:
        try:
            result = self.core_client.prepare_user_access(
                user_id,
                email=email,
                ttl_seconds=604800,
                send_email=True,
            )
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Файл не отправлен", self._core_error_text(exc))
            return
        status = str(result.get("email_status") or "")
        if status == "delivered":
            QMessageBox.information(
                self,
                "Файл отправлен",
                f"Одноразовый файл входа отправлен на {email}. Предыдущий код отозван.",
            )
        elif status == "not-configured":
            QMessageBox.warning(
                self,
                "SMTP не настроен",
                "Настройте Email (SMTP) в разделе «Настройки → Уведомления».",
            )
        else:
            QMessageBox.warning(
                self,
                "Файл не отправлен",
                f"SMTP вернул состояние: {status or 'неизвестная ошибка'}",
            )

    def cancel_invitation(self, invitation_id: str) -> None:
        try:
            cancelled = self.core_client.cancel_invitation(invitation_id)
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Код не отменён", self._core_error_text(exc))
            return
        if cancelled:
            self.audit.record("core.invitation.cancelled", "Код подключения отменён")

    def approve_device(self, device_id: str) -> None:
        try:
            device = self.core_client.approve_device(device_id)
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Устройство не подтверждено", self._core_error_text(exc))
            return
        self.audit.record("core.device.approved", f"Подтверждено устройство {device['name']}")
        self.refresh_core()

    def revoke_device(self, device_id: str) -> None:
        response = QMessageBox.question(
            self,
            "Отключить устройство?",
            "Его токен будет немедленно отозван. Для повторного подключения потребуется новый код.",
        )
        if response != QMessageBox.StandardButton.Yes:
            return
        try:
            device = self.core_client.revoke_device(device_id)
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Устройство не отключено", self._core_error_text(exc))
            return
        self.audit.record("core.device.revoked", f"Отключено устройство {device['name']}")
        self.refresh_core()

    def start_migration(
        self, source_root_id: str, target_root_id: str, space_id: str = ""
    ) -> None:
        response = QMessageBox.question(
            self,
            "Начать перенос?",
            "Core скопирует каждый управляемый объект, проверит размер и SHA-256, затем "
            "переключит метаданные на целевой диск. Исходные объекты останутся страховочными "
            "копиями и не будут удалены автоматически.",
        )
        if response != QMessageBox.StandardButton.Yes:
            return
        try:
            job = self.core_client.create_migration(
                source_root_id, target_root_id, space_id or None
            )
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Перенос не запущен", self._core_error_text(exc))
            return
        self.audit.record(
            "core.storage.migration.started",
            f"Запущен перенос {source_root_id} → {target_root_id}; задание {job['id']}",
        )
        self.refresh_core()

    def retry_transfer(self, transfer_id: str) -> None:
        try:
            self.core_client.retry_transfer(transfer_id)
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(
                self,
                "Перенос не запущен",
                self._core_error_text(exc),
            )
            return
        self.audit.record(
            "core.transfer.retry",
            f"Повторно запущен перенос принятого файла на HDD: {transfer_id}",
        )
        self.refresh_core()

    def resume_maintenance_job(self, job_id: str) -> None:
        try:
            self.core_client.resume_maintenance_job(job_id)
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Задание не продолжено", self._core_error_text(exc))
            return
        self.refresh_core()

    def cancel_maintenance_job(self, job_id: str) -> None:
        response = QMessageBox.question(
            self,
            "Остановить задание?",
            "Уже проверенные и переключённые объекты останутся на целевом диске. "
            "Задание можно будет продолжить позже.",
        )
        if response != QMessageBox.StandardButton.Yes:
            return
        try:
            self.core_client.cancel_maintenance_job(job_id)
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Задание не остановлено", self._core_error_text(exc))
            return
        self.refresh_core()

    def start_backup(self, target_root_id: str) -> None:
        response = QMessageBox.question(
            self,
            "Создать резервный снимок?",
            "Core создаст отдельную копию метаданных и всех управляемых объектов, включая "
            "предыдущие версии. Снимок появится в списке готовых только после полной проверки.",
        )
        if response != QMessageBox.StandardButton.Yes:
            return
        try:
            job = self.core_client.create_backup(target_root_id)
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Снимок не запущен", self._core_error_text(exc))
            return
        self.audit.record(
            "core.backup.started",
            f"Запущен резервный снимок на {target_root_id}; задание {job['id']}",
        )
        self.refresh_core()

    def resume_backup(self, job_id: str) -> None:
        try:
            self.core_client.resume_backup(job_id)
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Снимок не продолжен", self._core_error_text(exc))
            return
        self.refresh_core()

    def cancel_backup(self, job_id: str) -> None:
        response = QMessageBox.question(
            self,
            "Отменить резервный снимок?",
            "Незавершённый staging-каталог будет удалён. Уже готовые снимки не затрагиваются.",
        )
        if response != QMessageBox.StandardButton.Yes:
            return
        try:
            self.core_client.cancel_backup(job_id)
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Снимок не отменён", self._core_error_text(exc))
            return
        self.refresh_core()

    def save_backup_policy(self, values: dict) -> None:
        enabled = bool(values.get("enabled"))
        if enabled:
            response = QMessageBox.warning(
                self,
                "Включить автоматические снимки?",
                f"Core будет создавать снимок каждые {values['interval_hours']} ч, выполнять "
                f"пробное восстановление и хранить последние {values['keep_last']}. Более "
                "старые снимки удаляются только после успешной проверки и записываются в аудит.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if response != QMessageBox.StandardButton.Yes:
                return
        try:
            policy = self.core_client.set_backup_policy(
                str(values["target_root_id"]),
                enabled=enabled,
                interval_hours=int(values["interval_hours"]),
                keep_last=int(values["keep_last"]),
                verification_root_id=values.get("verification_root_id"),
            )
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Автоматизация не сохранена", self._core_error_text(exc))
            return
        self.audit.record(
            "core.backup.policy.updated",
            f"Политика {policy['target_root_id']}: "
            f"{'включена' if policy['enabled'] else 'выключена'}",
        )
        self.refresh_core()

    def run_backup_policy(self, target_root_id: str) -> None:
        response = QMessageBox.question(
            self,
            "Запустить полный цикл сейчас?",
            "Core создаст снимок, выполнит пробное восстановление на выбранном основном "
            "диске и только после успеха применит политику хранения.",
        )
        if response != QMessageBox.StandardButton.Yes:
            return
        try:
            job = self.core_client.run_backup_policy(target_root_id)
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Цикл не запущен", self._core_error_text(exc))
            return
        self.audit.record(
            "core.backup.policy.run",
            f"Вручную запущен автоматический цикл {job['id']}",
        )
        self.refresh_core()

    def verify_backup(self, job_id: str, target_root_id: str) -> None:
        response = QMessageBox.question(
            self,
            "Проверить снимок восстановлением?",
            "Core прочитает базу и каждый объект снимка, временно скопирует данные на "
            "выбранный основной диск, проверит SHA-256 и удалит только тестовые файлы. "
            "Рабочие метаданные и пользовательские файлы не изменяются.",
        )
        if response != QMessageBox.StandardButton.Yes:
            return
        try:
            verification = self.core_client.verify_backup(job_id, target_root_id)
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Проверка не запущена", self._core_error_text(exc))
            return
        self.audit.record(
            "core.backup.verification.started",
            f"Запущено пробное восстановление {verification['id']}",
        )
        self.refresh_core()

    def cancel_backup_verification(self, verification_id: str) -> None:
        try:
            self.core_client.cancel_backup_verification(verification_id)
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Проверка не остановлена", self._core_error_text(exc))
            return
        self.refresh_core()

    def start_restore(self, backup_job_id: str, target_root_id: str) -> None:
        response = QMessageBox.question(
            self,
            "Восстановить повреждённые объекты?",
            "Core заново проверит объекты из выбранного снимка по размеру и SHA-256. "
            "Он восстановит только отсутствующие или повреждённые данные, которые всё ещё "
            "соответствуют снимку. Исправные и более новые версии файлов не перезаписываются.",
        )
        if response != QMessageBox.StandardButton.Yes:
            return
        try:
            job = self.core_client.create_restore(backup_job_id, target_root_id)
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Восстановление не запущено", self._core_error_text(exc))
            return
        self.audit.record(
            "core.restore.started",
            f"Запущено восстановление снимка {backup_job_id} на {target_root_id}; "
            f"задание {job['id']}",
        )
        self.refresh_core()

    def resume_restore(self, job_id: str) -> None:
        try:
            self.core_client.resume_restore(job_id)
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Восстановление не продолжено", self._core_error_text(exc))
            return
        self.refresh_core()

    def cancel_restore(self, job_id: str) -> None:
        response = QMessageBox.question(
            self,
            "Остановить восстановление?",
            "Уже проверенные и восстановленные объекты останутся на месте. Задание можно "
            "будет повторить позже.",
        )
        if response != QMessageBox.StandardButton.Yes:
            return
        try:
            self.core_client.cancel_restore(job_id)
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Восстановление не остановлено", self._core_error_text(exc))
            return
        self.refresh_core()

    def enter_read_only_mode(self) -> None:
        response = QMessageBox.warning(
            self,
            "Включить аварийный режим?",
            "Новые загрузки, удаления, подключения устройств и фоновые задания будут "
            "остановлены. Скачивание файлов, диагностика и проверяемое восстановление "
            "останутся доступны.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if response != QMessageBox.StandardButton.Yes:
            return
        reason, accepted = QInputDialog.getText(
            self,
            "Причина аварийного режима",
            "Коротко укажите причину (она попадёт в журнал):",
        )
        if not accepted:
            return
        try:
            result = self.core_client.set_server_mode(
                "read_only",
                reason.strip() or "Включено оператором через Server Manager",
                confirmed=True,
            )
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Режим не включён", self._core_error_text(exc))
            return
        self.audit.record(
            "core.server.read_only",
            f"Включён аварийный режим; остановлено заданий {result.get('cancelled_jobs', 0)}",
            "warning",
        )
        self.refresh_core()

    def leave_read_only_mode(self) -> None:
        response = QMessageBox.question(
            self,
            "Вернуть обычный режим?",
            "Загрузка, удаление и новые подключения снова станут доступны. Убедитесь, "
            "что причина аварийного режима устранена.",
        )
        if response != QMessageBox.StandardButton.Yes:
            return
        try:
            self.core_client.set_server_mode("normal", "Аварийный режим отключён оператором")
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Режим не изменён", self._core_error_text(exc))
            return
        self.audit.record("core.server.normal", "Сервер возвращён в обычный режим")
        self.refresh_core()

    def reconcile_mirror(self, root_id: str) -> None:
        response = QMessageBox.question(
            self,
            "Проверить зеркало?",
            "Core сверит каждую актуальную реплику по размеру и SHA-256. Отсутствующие или "
            "повреждённые копии будут созданы заново; основные файлы не изменятся.",
        )
        if response != QMessageBox.StandardButton.Yes:
            return
        try:
            job = self.core_client.reconcile_mirror(root_id)
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Проверка не запущена", self._core_error_text(exc))
            return
        self.audit.record(
            "core.mirror.reconcile.started",
            f"Запущена проверка зеркала {root_id}; задание {job['id']}",
        )
        self.refresh_core()

    def resume_mirror_job(self, job_id: str) -> None:
        try:
            self.core_client.resume_mirror_job(job_id)
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Проверка не продолжена", self._core_error_text(exc))
            return
        self.refresh_core()

    def cancel_mirror_job(self, job_id: str) -> None:
        response = QMessageBox.question(
            self,
            "Остановить проверку зеркала?",
            "Уже проверенные и восстановленные реплики останутся на месте. Проверку можно "
            "запустить снова позже.",
        )
        if response != QMessageBox.StandardButton.Yes:
            return
        try:
            self.core_client.cancel_mirror_job(job_id)
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Проверка не остановлена", self._core_error_text(exc))
            return
        self.refresh_core()

    def start_diagnostic_scan(self, kind: str) -> None:
        if kind == "full":
            response = QMessageBox.question(
                self,
                "Запустить полную проверку?",
                "Core прочитает все управляемые объекты и пересчитает SHA-256. Это безопасно, "
                "но на большом хранилище создаст заметную нагрузку на диски.",
            )
            if response != QMessageBox.StandardButton.Yes:
                return
        try:
            scan = self.core_client.start_diagnostic_scan(kind)
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Проверка не запущена", self._core_error_text(exc))
            return
        self.audit.record(
            "core.diagnostics.started",
            f"Запущена {'полная' if kind == 'full' else 'быстрая'} проверка; {scan['id']}",
        )
        QTimer.singleShot(800, self.refresh_core)

    def remediate_diagnostic_incident(self, incident_id: str, action: str) -> None:
        confirmed = False
        if action == "enter_read_only":
            response = QMessageBox.warning(
                self,
                "Защитить сервер режимом только чтения?",
                "Новые загрузки, подключения и фоновые задания будут остановлены. "
                "Скачивание и диагностика останутся доступны.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if response != QMessageBox.StandardButton.Yes:
                return
            confirmed = True
        elif action == "cleanup_expired_uploads":
            response = QMessageBox.question(
                self,
                "Очистить просроченные загрузки?",
                "Core удалит только временные части уже просроченных сессий. "
                "Готовые файлы не изменятся.",
            )
            if response != QMessageBox.StandardButton.Yes:
                return
        try:
            self.core_client.remediate_diagnostic_incident(
                incident_id,
                action,
                confirmed=confirmed,
            )
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Действие не выполнено", self._core_error_text(exc))
            return
        QTimer.singleShot(800 if action == "recheck" else 0, self.refresh_core)

    def create_automation_rule(self, values: dict) -> None:
        if not str(values.get("name", "")).strip():
            QMessageBox.warning(self, "Нет названия", "Введите понятное название правила.")
            return
        payload = dict(values)
        if payload.get("action_type") == "read_only":
            response = QMessageBox.warning(
                self,
                "Разрешить автоматический режим только чтения?",
                "Это правило сможет автоматически остановить новые записи и фоновые задания. "
                "Действие разрешено только для критического инцидента и попадёт в аудит.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if response != QMessageBox.StandardButton.Yes:
                return
            payload["confirmed"] = True
        try:
            self.core_client.create_automation_rule(payload)
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Правило не создано", self._core_error_text(exc))
            return
        if self.settings_page.automation_rule_name is not None:
            self.settings_page.automation_rule_name.clear()
        self.refresh_core()

    def update_automation_rule(self, rule_id: str, values: dict) -> None:
        payload = dict(values)
        if payload.get("action_type") == "read_only" and payload.get("enabled"):
            response = QMessageBox.warning(
                self,
                "Включить защитное правило?",
                "При критическом инциденте Core автоматически перейдёт в режим только чтения.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if response != QMessageBox.StandardButton.Yes:
                return
            payload["confirmed"] = True
        try:
            self.core_client.update_automation_rule(rule_id, payload)
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Правило не изменено", self._core_error_text(exc))
            return
        self.refresh_core()

    def delete_automation_rule(self, rule_id: str) -> None:
        response = QMessageBox.question(
            self,
            "Удалить правило?",
            "История запусков этого пользовательского правила также будет удалена.",
        )
        if response != QMessageBox.StandardButton.Yes:
            return
        try:
            self.core_client.delete_automation_rule(rule_id)
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Правило не удалено", self._core_error_text(exc))
            return
        self.refresh_core()

    def evaluate_automation(self) -> None:
        response = QMessageBox.question(
            self,
            "Выполнить совпавшие правила?",
            "Core повторно проверит все условия и сразу запустит готовые действия. "
            "Резервирование, полная диагностика и восстановление зеркала могут занять время.",
        )
        if response != QMessageBox.StandardButton.Yes:
            return
        try:
            result = self.core_client.evaluate_automation()
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Правила не проверены", self._core_error_text(exc))
            return
        self.refresh_core()
        QMessageBox.information(
            self,
            "Проверка завершена",
            f"Сработало правил: {result.get('matched_rules', 0)}.",
        )

    def preview_automation(self) -> None:
        try:
            result = self.core_client.preview_automation()
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Условия не проверены", self._core_error_text(exc))
            return
        self.settings_page.show_automation_preview(result)

    def preview_automation_rule(self, rule_id: str) -> None:
        try:
            result = self.core_client.preview_automation_rule(rule_id)
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Условие не проверено", self._core_error_text(exc))
            return
        rule_name = next(
            (
                str(item.get("name", "Правило"))
                for item in (self.core_automation.get("rules") or [])
                if item.get("id") == rule_id
            ),
            "Правило",
        )
        self.settings_page.show_automation_preview(result, rule_name=rule_name)

    def update_automation_settings(self, values: dict) -> None:
        try:
            self.core_client.update_automation_settings(values)
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Режим не сохранён", self._core_error_text(exc))
            return
        self.refresh_core()

    def acknowledge_notification(self, notification_id: str) -> None:
        try:
            self.core_client.acknowledge_notification(notification_id)
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Уведомление не закрыто", self._core_error_text(exc))
            return
        self.refresh_core()

    def save_control_settings(self, values: dict) -> None:
        payload = dict(values)
        current_power = self.core_control.get("settings", {}).get("power", {})
        enabling_system_sleep = (
            values.get("power", {}).get("allow_os_sleep") is True
            and current_power.get("allow_os_sleep") is not True
        )
        enabling_system_shutdown = (
            values.get("power", {}).get("allow_os_shutdown") is True
            and current_power.get("allow_os_shutdown") is not True
        )
        if enabling_system_sleep or enabling_system_shutdown:
            action_text = (
                "сон и аварийное выключение"
                if enabling_system_sleep and enabling_system_shutdown
                else "аварийное выключение"
                if enabling_system_shutdown
                else "системный сон"
            )
            response = QMessageBox.warning(
                self,
                f"Разрешить {action_text}?",
                "Core сможет выполнить выбранное системное действие только при отсутствии "
                "активных передач. Аварийное выключение дополнительно требует работы от "
                "батареи/ИБП и часовой задержки. Пробуждение требует настроенного Wake-on-LAN.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if response != QMessageBox.StandardButton.Yes:
                return
            payload["confirmed"] = True
        try:
            saved = self.core_client.update_control_settings(payload)
        except (CoreApiError, CoreUnavailable, ValueError) as exc:
            QMessageBox.warning(self, "Настройки Idea 4 не сохранены", self._core_error_text(exc))
            return
        self.core_control["settings"] = saved
        self.control_page.mark_settings_saved()
        self._apply_interface_mode(str(saved.get("interface_mode", "detailed")))
        self.refresh_core()

    def apply_system_preset(self, preset_id: str) -> None:
        try:
            self.core_client.apply_system_preset(preset_id)
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Профиль не применён", self._core_error_text(exc))
            return
        self.refresh_core()

    def install_automation_template(self, template_id: str) -> None:
        try:
            created = self.core_client.install_automation_template(template_id)
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Автоматизация не добавлена", self._core_error_text(exc))
            return
        QMessageBox.information(
            self,
            "Автоматизация добавлена",
            f"Правило «{created['rule']['name']}» включено и сохранено в Core.",
        )
        self.refresh_core()

    def create_control_report(self, values: dict) -> None:
        try:
            self.core_client.create_report_schedule(values)
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Расписание не создано", self._core_error_text(exc))
            return
        self.refresh_core()

    def create_sandbox_cell(self, values: dict) -> None:
        try:
            self.core_client.create_sandbox_cell(values)
        except (CoreApiError, CoreUnavailable, ValueError) as exc:
            QMessageBox.warning(self, "Ячейка не создана", self._core_error_text(exc))
            return
        self.refresh_core()

    def run_sandbox_cell(self, cell_id: str) -> None:
        try:
            result = self.core_client.run_sandbox_cell(cell_id)
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Ячейка не запущена", self._core_error_text(exc))
            return
        self.refresh_core()
        QMessageBox.information(
            self,
            "Запуск завершён",
            f"Статус: {result.get('status')}\n\n{str(result.get('last_result', ''))[-1200:]}",
        )

    def install_docker_desktop(self) -> None:
        response = QMessageBox.warning(
            self,
            "Установить Docker Desktop?",
            "Менеджер скачает Docker Desktop с официального сайта и установит его для всех "
            "пользователей Windows с WSL 2 и Linux-контейнерами. Нажимая «Да», вы "
            "подтверждаете установку и принятие Docker Subscription Service Agreement.\n\n"
            "Для личного использования и небольших компаний Docker Desktop обычно бесплатен; "
            "для крупных организаций может требоваться платная подписка.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if response != QMessageBox.StandardButton.Yes:
            return
        try:
            self.core_client.install_docker_desktop()
        except (CoreApiError, CoreUnavailable, ValueError) as exc:
            QMessageBox.warning(self, "Docker не устанавливается", self._core_error_text(exc))
            return
        QMessageBox.information(
            self,
            "Установка Docker началась",
            "Загрузка и установка идут в фоне. Статус виден во вкладке «Docker и SSH». "
            "После завершения откройте Docker Desktop один раз из меню Пуск.",
        )
        self.refresh_core()

    def enable_windows_ssh(self, values: dict) -> None:
        if not values.get("username") or not values.get("public_key"):
            QMessageBox.warning(
                self,
                "Не хватает данных",
                "Укажите локального администратора Windows и вставьте публичный SSH-ключ.",
            )
            return
        response = QMessageBox.warning(
            self,
            "Открыть SSH-доступ?",
            f"Будет установлен OpenSSH Server и открыт TCP-порт {values.get('port')} только "
            "для частной сети. Войти сможет только указанный Windows-пользователь и только "
            "по ключу; вход по паролю будет отключён.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if response != QMessageBox.StandardButton.Yes:
            return
        try:
            self.core_client.enable_windows_ssh(values)
        except (CoreApiError, CoreUnavailable, ValueError) as exc:
            QMessageBox.warning(self, "SSH не включается", self._core_error_text(exc))
            return
        QMessageBox.information(
            self,
            "Настройка SSH началась",
            "OpenSSH устанавливается и проверяет конфигурацию в фоне. Обновляйте статус "
            "во вкладке «Docker и SSH».",
        )
        self.refresh_core()

    def disable_windows_ssh(self) -> None:
        response = QMessageBox.warning(
            self,
            "Отключить SSH?",
            "Служба OpenSSH будет остановлена и отключена, а созданное Cloud Storage правило "
            "брандмауэра удалено. Ключ и резервная копия конфигурации сохранятся.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if response != QMessageBox.StandardButton.Yes:
            return
        try:
            self.core_client.disable_windows_ssh()
        except (CoreApiError, CoreUnavailable, ValueError) as exc:
            QMessageBox.warning(self, "SSH не отключён", self._core_error_text(exc))
            return
        QMessageBox.information(self, "SSH отключается", "Операция выполняется в фоне.")
        self.refresh_core()

    def test_integration(self, provider_id: str) -> None:
        try:
            self.core_client.test_integration(provider_id)
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Интеграция не отвечает", self._core_error_text(exc))
            return
        QMessageBox.information(
            self,
            "Интеграция работает",
            f"Встроенный провайдер {provider_id} принял тестовое уведомление.",
        )

    def send_wake_on_lan(self, mac_address: str, broadcast: str) -> None:
        if not mac_address:
            QMessageBox.warning(self, "MAC не указан", "Введите MAC-адрес сетевой карты сервера.")
            return
        try:
            result = self.core_client.wake_on_lan(mac_address, broadcast or "255.255.255.255")
        except (CoreApiError, CoreUnavailable, ValueError) as exc:
            QMessageBox.warning(self, "Wake-on-LAN не отправлен", self._core_error_text(exc))
            return
        QMessageBox.information(
            self,
            "Wake-on-LAN отправлен",
            f"Magic packet отправлен на {result.get('mac_address')} через {result.get('broadcast')}.",
        )

    def restart_tunnel(self, provider_id: str) -> None:
        try:
            status = self.core_client.restart_tunnel(provider_id)
        except (CoreApiError, CoreUnavailable, ValueError) as exc:
            QMessageBox.warning(
                self,
                "Туннель не перезапущен",
                self._core_error_text(exc),
            )
            return
        self.audit.record(
            "core.tunnel.restarted",
            f"Перезапущен встроенный провайдер {provider_id}",
        )
        self.refresh_core()
        state = str(status.get("state", "starting"))
        QMessageBox.information(
            self,
            "Команда отправлена",
            f"Провайдер {provider_id} перезапущен. Текущее состояние: {state}.",
        )

    def export_support_bundle(self) -> None:
        if not self.core_health:
            QMessageBox.information(self, "Ядро выключено", "Сначала запустите серверное ядро.")
            return
        suggested = (
            Path.home()
            / "Documents"
            / (f"CloudStorage-support-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip")
        )
        selected, _ = QFileDialog.getSaveFileName(
            self,
            "Сохранить обезличенный пакет поддержки",
            str(suggested),
            "ZIP-архив (*.zip)",
        )
        if not selected:
            return
        destination = Path(selected)
        if destination.suffix.casefold() != ".zip":
            destination = destination.with_suffix(".zip")
        try:
            bundle = self.core_client.download_support_bundle(destination)
        except (CoreApiError, CoreUnavailable, OSError) as exc:
            QMessageBox.warning(
                self,
                "Пакет не сохранён",
                self._core_error_text(exc),
            )
            return
        self.audit.record(
            "core.support_bundle.saved",
            f"Сохранён обезличенный пакет поддержки ({bundle['size_bytes']} байт)",
        )
        QMessageBox.information(
            self,
            "Пакет поддержки сохранён",
            f"Файл: {bundle['path']}\nSHA-256: {bundle['sha256']}",
        )

    @staticmethod
    def _core_error_text(error: Exception) -> str:
        if isinstance(error, CoreApiError):
            return error.detail
        return str(error)

    def _sync_storage_roots(self, show_errors: bool = False) -> None:
        if not self.core_health:
            return
        roots: list[dict] = []
        eligible_roles = {
            DiskRole.SHARED,
            DiskRole.PERSONAL,
            DiskRole.SHARED_FOLDERS,
            DiskRole.OVERFLOW,
            DiskRole.BACKUP,
            DiskRole.MIRROR,
            DiskRole.ARCHIVE,
            DiskRole.TEMPORARY,
        }
        for disk in self.disks:
            config = self.settings.configuration_for(disk.id)
            if (
                not disk.available
                or config.role not in eligible_roles
                or config.mode == DiskMode.DISCONNECTED
            ):
                continue
            directory_name = (
                "CloudStorageData" if disk.mountpoint.endswith("\\") else ".cloud-storage-data"
            )
            roots.append(
                {
                    "disk_id": disk.id,
                    "path": str(Path(disk.mountpoint) / directory_name),
                    "priority": config.write_priority,
                    "max_fill_percent": config.max_fill_percent,
                    "min_free_gib": config.min_free_gib,
                    "write_enabled": (config.mode == DiskMode.ACTIVE and not config.read_only),
                    "purpose": (
                        "backup"
                        if config.role == DiskRole.BACKUP
                        else "mirror"
                        if config.role == DiskRole.MIRROR
                        else "primary"
                    ),
                }
            )
        try:
            self.core_storage_roots = self.core_client.sync_storage_roots(roots)
            cache_candidates = []
            for disk in self.disks:
                config = self.settings.configuration_for(disk.id)
                if (
                    disk.available
                    and config.role == DiskRole.CACHE
                    and config.mode == DiskMode.ACTIVE
                    and not config.read_only
                ):
                    directory_name = (
                        "CloudStorageCache"
                        if disk.mountpoint.endswith("\\")
                        else ".cloud-storage-cache"
                    )
                    cache_candidates.append(
                        (
                            config.write_priority,
                            str(Path(disk.mountpoint) / directory_name),
                        )
                    )
            cache_candidates.sort(reverse=True)
            self.core_client.update_transfer_settings(
                staging_enabled=bool(cache_candidates),
                staging_path=cache_candidates[0][1] if cache_candidates else "",
            )
        except (CoreApiError, CoreUnavailable, OSError) as exc:
            self.audit.record(
                "core.storage.sync.failed",
                f"Не удалось применить диски к ядру: {self._core_error_text(exc)}",
                "warning",
            )
            if show_errors:
                QMessageBox.warning(
                    self,
                    "Хранилище не применено",
                    self._core_error_text(exc),
                )

    def open_setup(self) -> None:
        available = [item for item in self.disks if item.available]
        if not available:
            QMessageBox.information(
                self,
                "Нет доступных дисков",
                "Подключите накопитель и нажмите «Обновить». Настройку можно выполнить позднее.",
            )
            return
        dialog = SetupDialog(available, self.settings, self)
        if dialog.exec():
            configured = dialog.apply_to(self.settings)
            for disk in available:
                config = self.settings.configuration_for(disk.id)
                config.identity_mountpoint = disk.mountpoint
                config.identity_device = disk.device
                config.identity_serial = disk.serial or ""
            self.store.save(self.settings)
            self.audit.record(
                "setup.completed" if configured else "setup.deferred",
                "Первоначальная настройка сохранена"
                if configured
                else "Первоначальная настройка отложена",
            )
            self._setup_banner_hidden = False
            self._disk_reminder_hidden = False
            self._sync_storage_roots(show_errors=True)
            self._refresh_pages()
        else:
            self.hide_setup_reminder()

    def hide_setup_reminder(self) -> None:
        self._setup_banner_hidden = True
        self.settings.setup_reminded_later = True
        self.store.save(self.settings)
        self.audit.record("setup.deferred", "Первоначальная настройка отложена")
        self._refresh_pages()

    def hide_disk_reminder(self) -> None:
        self._disk_reminder_hidden = True
        self.audit.record("disk.setup.deferred", "Настройка новых дисков отложена")
        self._refresh_pages()

    def configure_first_unconfigured(self) -> None:
        disk = next(
            (
                item
                for item in self.disks
                if item.available
                and self.settings.configuration_for(item.id).role == DiskRole.UNCONFIGURED
            ),
            None,
        )
        if disk:
            self.open_disk(disk.id)

    def ignore_unconfigured(self) -> None:
        pending = [
            item
            for item in self.disks
            if item.available
            and self.settings.configuration_for(item.id).role == DiskRole.UNCONFIGURED
        ]
        if not pending:
            return
        response = QMessageBox.question(
            self,
            "Пока не использовать?",
            "Все ненастроенные накопители останутся видны, но менеджер не будет предлагать "
            "их настройку. Данные на дисках не изменятся.",
        )
        if response != QMessageBox.StandardButton.Yes:
            return
        for disk in pending:
            self.settings.configuration_for(disk.id).role = DiskRole.UNUSED
            if disk.id not in self.settings.ignored_disk_ids:
                self.settings.ignored_disk_ids.append(disk.id)
        self.store.save(self.settings)
        self.audit.record("disk.ignored", f"Не используется накопителей: {len(pending)}")
        self._refresh_pages()

    def open_disk(self, disk_id: str) -> None:
        disk = next((item for item in self.disks if item.id == disk_id), None)
        if disk is None:
            return
        dialog = DiskDetailDialog(disk, self.settings.configuration_for(disk.id), self)
        dialog.configuration_saved.connect(self.save_disk_configuration)
        dialog.refresh_requested.connect(self.refresh_disks)
        dialog.cleanup_requested.connect(self.cleanup_disk_temporary)
        dialog.exec()

    def cleanup_disk_temporary(self, disk_id: str) -> None:
        root = next(
            (item for item in self.core_storage_roots if str(item.get("disk_id") or "") == disk_id),
            None,
        )
        if root is None:
            QMessageBox.information(
                self,
                "Диск ещё не подключён к Core",
                "Сначала сохраните назначение диска, затем повторите очистку.",
            )
            return
        response = QMessageBox.question(
            self,
            "Очистить временные файлы?",
            "Будут удалены только просроченные сессии загрузки и неиспользуемые "
            "staging-файлы старше суток. Пользовательские файлы и корзина не удаляются.",
        )
        if response != QMessageBox.StandardButton.Yes:
            return
        try:
            result = self.core_client.cleanup_storage_root(str(root["id"]))
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Очистка не выполнена", self._core_error_text(exc))
            return
        QMessageBox.information(
            self,
            "Временные файлы очищены",
            f"Удалено файлов: {result.get('removed_files', 0)} · "
            f"освобождено: {result.get('removed_bytes', 0)} байт · "
            f"просроченных загрузок: {result.get('expired_sessions', 0)}.",
        )
        self.refresh_core()

    def save_disk_configuration(self, disk_id: str, configuration: DiskConfiguration) -> None:
        old = self.settings.configuration_for(disk_id)
        disk = next((item for item in self.disks if item.id == disk_id), None)
        if disk is not None:
            configuration = replace(
                configuration,
                identity_mountpoint=disk.mountpoint,
                identity_device=disk.device,
                identity_serial=disk.serial or "",
            )
        self.settings.disk_configurations[disk_id] = configuration
        if disk_id in self.settings.ignored_disk_ids and configuration.role != DiskRole.UNUSED:
            self.settings.ignored_disk_ids.remove(disk_id)
        configured = [
            item
            for item in self.settings.disk_configurations.values()
            if item.role not in {DiskRole.UNCONFIGURED, DiskRole.UNUSED}
        ]
        self.settings.setup_complete = bool(configured)
        self.store.save(self.settings)
        detail = f"Настройки диска сохранены: {configuration.display_name or disk_id}"
        if old.role != configuration.role:
            detail += f"; назначение {old.role.value} → {configuration.role.value}"
        self.audit.record("disk.configuration.saved", detail)
        self._disk_reminder_hidden = False
        self._sync_storage_roots(show_errors=True)
        self._refresh_pages()

    def save_general_settings(self, values: dict) -> None:
        if (
            len(
                {
                    self.core_client.config.port,
                    values["lan_port"],
                    values["remote_port"],
                    values["zrok_port"],
                }
            )
            != 4
        ):
            QMessageBox.warning(
                self,
                "Неверные сетевые порты",
                "Локальный API, LAN HTTPS, удалённый HTTPS и zrok-шлюз должны использовать разные порты.",
            )
            return
        if values["remote_pairing_enabled"] and not (
            values["remote_enabled"] or values["zrok_enabled"]
        ):
            QMessageBox.warning(
                self,
                "Удалённый доступ выключен",
                "Сначала включите удалённый HTTPS-вход или zrok, затем разрешайте подключение по коду.",
            )
            return
        try:
            replace(
                self.core_client.config,
                remote_enabled=values["remote_enabled"],
                remote_port=values["remote_port"],
                remote_public_url=values["remote_public_url"],
                remote_pairing_enabled=values["remote_pairing_enabled"],
                zrok_enabled=values["zrok_enabled"],
                zrok_port=values["zrok_port"],
                zrok_executable=values["zrok_executable"],
                zrok_share_name=values["zrok_share_name"],
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Неверный удалённый адрес", str(exc))
            return
        if values["remote_enabled"] and not self.settings.remote_enabled:
            response = QMessageBox.question(
                self,
                "Включить внешний HTTPS-вход?",
                "Будет открыт отдельный порт только для клиентских файловых функций. "
                "Административный API останется локальным. Cloud Storage не меняет роутер и firewall "
                "автоматически: используйте VPN либо вручную настройте перенаправление порта. Продолжить?",
            )
            if response != QMessageBox.StandardButton.Yes:
                return
        if values["zrok_enabled"] and not self.settings.zrok_enabled:
            response = QMessageBox.question(
                self,
                "Включить интернет-доступ через zrok?",
                "Core запустит установленный zrok и опубликует только клиентский шлюз на 127.0.0.1. "
                "Доступ к файлам потребует логин, пароль и токен подтверждённого устройства. "
                "Аккаунт zrok должен быть заранее включён штатной командой zrok enable. Продолжить?",
            )
            if response != QMessageBox.StandardButton.Yes:
                return
        if values["lan_port"] == self.core_client.config.port:
            QMessageBox.warning(
                self,
                "Неверный HTTPS-порт",
                "Порт локальной сети должен отличаться от локального административного порта.",
            )
            return
        network_changed = (
            self.settings.server_name != values["server_name"]
            or self.settings.lan_enabled != values["lan_enabled"]
            or self.settings.lan_port != values["lan_port"]
            or self.settings.remote_enabled != values["remote_enabled"]
            or self.settings.remote_port != values["remote_port"]
            or self.settings.remote_public_url != values["remote_public_url"]
            or self.settings.remote_pairing_enabled != values["remote_pairing_enabled"]
            or self.settings.zrok_enabled != values["zrok_enabled"]
            or self.settings.zrok_port != values["zrok_port"]
            or self.settings.zrok_executable != values["zrok_executable"]
            or self.settings.zrok_share_name != values["zrok_share_name"]
        )
        core_was_running = self.core_health is not None
        if network_changed and core_was_running:
            try:
                stopped = self.core_supervisor.stop()
            except (CoreApiError, CoreUnavailable, OSError) as exc:
                QMessageBox.warning(
                    self,
                    "Настройки сети не применены",
                    f"Не удалось безопасно остановить ядро: {exc}",
                )
                return
            if not stopped:
                QMessageBox.warning(
                    self,
                    "Настройки сети не применены",
                    "Ядро не остановилось вовремя. Повторите попытку после проверки его состояния.",
                )
                return
        self.settings.server_name = values["server_name"]
        self.settings.notifications_enabled = values["notifications_enabled"]
        self.settings.refresh_interval_seconds = values["refresh_interval_seconds"]
        self.settings.advanced_mode = values["advanced_mode"]
        self.settings.lan_enabled = values["lan_enabled"]
        self.settings.lan_port = values["lan_port"]
        self.settings.remote_enabled = values["remote_enabled"]
        self.settings.remote_port = values["remote_port"]
        self.settings.remote_public_url = values["remote_public_url"]
        self.settings.remote_pairing_enabled = values["remote_pairing_enabled"]
        self.settings.zrok_enabled = values["zrok_enabled"]
        self.settings.zrok_port = values["zrok_port"]
        self.settings.zrok_executable = values["zrok_executable"]
        self.settings.zrok_share_name = values["zrok_share_name"]
        self.store.save(self.settings)
        self.settings_page.mark_settings_saved()
        if network_changed:
            self.core_client = CoreClient(
                replace(
                    self.core_client.config,
                    lan_enabled=self.settings.lan_enabled,
                    lan_port=self.settings.lan_port,
                    remote_enabled=self.settings.remote_enabled,
                    remote_port=self.settings.remote_port,
                    remote_public_url=self.settings.remote_public_url,
                    remote_pairing_enabled=self.settings.remote_pairing_enabled,
                    zrok_enabled=self.settings.zrok_enabled,
                    zrok_port=self.settings.zrok_port,
                    zrok_executable=self.settings.zrok_executable,
                    zrok_share_name=self.settings.zrok_share_name,
                    server_name=self.settings.server_name,
                )
            )
            self.core_supervisor = CoreSupervisor(self.core_client)
            if core_was_running:
                try:
                    self.core_health = self.core_supervisor.start()
                except CoreUnavailable as exc:
                    QMessageBox.warning(
                        self,
                        "Ядро не перезапущено",
                        f"Настройки сохранены, но ядро не запустилось: {exc}",
                    )
        self.audit.record("settings.saved", "Общие настройки Server Manager сохранены")
        self._reset_refresh_timer()
        self._refresh_pages()

    def save_advanced_mode(self, enabled: bool) -> None:
        if self.settings.advanced_mode == enabled:
            return
        self.settings.advanced_mode = enabled
        self.store.save(self.settings)
        self.audit.record(
            "settings.mode.changed",
            "Включён расширенный режим" if enabled else "Включён простой режим",
        )

    def _reset_refresh_timer(self) -> None:
        self.refresh_timer.setInterval(self.settings.refresh_interval_seconds * 1000)
        self.refresh_timer.start()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.store.save(self.settings)
        event.accept()
