from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
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
from cloud_storage.services.audit_log import AuditLog
from cloud_storage.services.core_client import (
    CoreApiError,
    CoreClient,
    CoreSupervisor,
    CoreUnavailable,
)
from cloud_storage.services.disk_service import DiskService, mark_missing_disks
from cloud_storage.services.settings_store import SettingsStore
from cloud_storage.ui.dialogs import (
    CreateUserDialog,
    DiskDetailDialog,
    InvitationDialog,
    SetupDialog,
)
from cloud_storage.ui.pages import DashboardPage, DisksPage, SettingsPage


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
            server_name=self.settings.server_name,
            max_upload_bytes=environment_config.max_upload_bytes,
            pairing_ttl_seconds=environment_config.pairing_ttl_seconds,
        )
        self.core_client = CoreClient(core_config)
        self.core_supervisor = CoreSupervisor(self.core_client)
        self.core_health: dict | None = None
        self.core_summary: dict | None = None
        self.core_users: list[dict] = []
        self.core_devices: list[dict] = []
        self.core_storage_roots: list[dict] = []
        self.core_maintenance_jobs: list[dict] = []
        self.core_backup_jobs: list[dict] = []
        self.core_mirror_state: dict = {"roots": [], "jobs": []}
        self.disks: list[DiskSnapshot] = []
        self._disk_reminder_hidden = False
        self._setup_banner_hidden = False
        self._nav_buttons: list[QPushButton] = []

        self.setWindowTitle(f"Cloud Storage Server Manager · Beta {__version__}")
        self.setMinimumSize(1050, 700)
        self.resize(1320, 820)
        self._build_ui()
        self._connect_pages()

        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh_disks)
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
        sidebar.setFixedWidth(245)
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
        beta = QLabel(f"SERVER MANAGER · BETA {__version__}")
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
        self.disks_page = DisksPage()
        self.settings_page = SettingsPage()
        self.help_page = HelpPage(self.knowledge, "server")
        for label, page in (
            ("⌂   Основная", self.dashboard_page),
            ("▣   Диски", self.disks_page),
            ("⚙   Настройки", self.settings_page),
            ("?   Помощь", self.help_page),
        ):
            button = QPushButton(label)
            button.setProperty("nav", True)
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, p=page: self._show_page(p))
            sidebar_layout.addWidget(button)
            self._nav_buttons.append(button)
            self.stack.addWidget(page)
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
        content_layout.setContentsMargins(26, 22, 10, 10)
        content_layout.addWidget(self.stack)
        layout.addWidget(content, 1)

    def _connect_pages(self) -> None:
        self.dashboard_page.setup_requested.connect(self.open_setup)
        self.dashboard_page.remind_later_requested.connect(self.hide_setup_reminder)
        self.disks_page.disk_selected.connect(self.open_disk)
        self.disks_page.refresh_requested.connect(self.refresh_disks)
        self.disks_page.configure_first_requested.connect(self.configure_first_unconfigured)
        self.disks_page.remind_later_requested.connect(self.hide_disk_reminder)
        self.disks_page.ignore_unconfigured_requested.connect(self.ignore_unconfigured)
        self.settings_page.save_requested.connect(self.save_general_settings)
        self.settings_page.advanced_mode_changed.connect(self.save_advanced_mode)
        self.settings_page.refresh_requested.connect(self.refresh_disks)
        self.settings_page.core_start_requested.connect(self.start_core)
        self.settings_page.core_stop_requested.connect(self.stop_core)
        self.settings_page.core_refresh_requested.connect(self.refresh_core)
        self.settings_page.add_user_requested.connect(self.add_user)
        self.settings_page.invitation_requested.connect(self.create_invitation)
        self.settings_page.approve_device_requested.connect(self.approve_device)
        self.settings_page.revoke_device_requested.connect(self.revoke_device)
        self.settings_page.migration_requested.connect(self.start_migration)
        self.settings_page.maintenance_refresh_requested.connect(self.refresh_core)
        self.settings_page.maintenance_resume_requested.connect(self.resume_maintenance_job)
        self.settings_page.maintenance_cancel_requested.connect(self.cancel_maintenance_job)
        self.settings_page.backup_requested.connect(self.start_backup)
        self.settings_page.backup_resume_requested.connect(self.resume_backup)
        self.settings_page.backup_cancel_requested.connect(self.cancel_backup)
        self.settings_page.mirror_reconcile_requested.connect(self.reconcile_mirror)
        self.settings_page.mirror_resume_requested.connect(self.resume_mirror_job)
        self.settings_page.mirror_cancel_requested.connect(self.cancel_mirror_job)

    def _show_page(self, page: QWidget) -> None:
        self.stack.setCurrentWidget(page)
        for index, button in enumerate(self._nav_buttons):
            button.setChecked(self.stack.widget(index) is page)

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

    def _refresh_pages(self) -> None:
        events = self.audit.recent()
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
            self.disks, self.settings, reminder_hidden=self._disk_reminder_hidden
        )
        self.settings_page.load_settings(self.settings, str(self.store.path))
        self.settings_page.update_core_data(
            self.core_health,
            self.core_summary,
            self.core_users,
            self.core_devices,
            self.core_storage_roots,
            self.core_maintenance_jobs,
            self.core_backup_jobs,
            self.core_mirror_state,
        )
        unconfigured = sum(
            item.available
            and self.settings.configuration_for(item.id).role == DiskRole.UNCONFIGURED
            for item in self.disks
        )
        self._nav_buttons[1].setText(
            f"▣   Диски   • {unconfigured}" if unconfigured else "▣   Диски"
        )

    def _refresh_core_state(self) -> None:
        self.core_health = self.core_client.try_health()
        self.core_summary = None
        self.core_users = []
        self.core_devices = []
        self.core_storage_roots = []
        self.core_maintenance_jobs = []
        self.core_backup_jobs = []
        self.core_mirror_state = {"roots": [], "jobs": []}
        if not self.core_health:
            return
        try:
            self.core_summary = self.core_client.summary()
            self.core_users = self.core_client.list_users()
            self.core_devices = self.core_client.list_devices()
            self.core_storage_roots = self.core_client.list_storage_roots()
            self.core_maintenance_jobs = self.core_client.list_maintenance_jobs()
            self.core_backup_jobs = self.core_client.list_backups()
            self.core_mirror_state = self.core_client.mirror_status()
        except (CoreApiError, CoreUnavailable) as exc:
            self.audit.record(
                "core.read.failed", f"Не удалось прочитать состояние ядра: {exc}", "warning"
            )

    def refresh_core(self) -> None:
        self._refresh_core_state()
        self._refresh_pages()

    def start_core(self) -> None:
        try:
            health = self.core_supervisor.start()
        except (CoreApiError, CoreUnavailable, OSError) as exc:
            QMessageBox.critical(self, "Ядро не запущено", str(exc))
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
        dialog = CreateUserDialog(self)
        if not dialog.exec():
            return
        values = dialog.values()
        try:
            created = self.core_client.create_user(**values)
            invitation = self.core_client.create_invitation(created["user"]["id"])
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Пользователь не создан", self._core_error_text(exc))
            return
        self.audit.record(
            "core.user.created", f"Создан пользователь {created['user']['display_name']}"
        )
        self._show_invitation(
            invitation["id"],
            created["user"]["display_name"],
            invitation["code"],
            invitation["expires_at"],
        )
        self.refresh_core()

    def create_invitation(self, user_id: str, display_name: str) -> None:
        try:
            invitation = self.core_client.create_invitation(user_id)
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Код не создан", self._core_error_text(exc))
            return
        self._show_invitation(
            invitation["id"],
            display_name,
            invitation["code"],
            invitation["expires_at"],
        )

    def _show_invitation(
        self, invitation_id: str, display_name: str, code: str, expires_at: str
    ) -> None:
        lan = self.core_health.get("lan") if self.core_health else None
        endpoints = lan.get("endpoints", []) if lan else []
        dialog = InvitationDialog(
            invitation_id,
            display_name,
            code,
            expires_at,
            str(endpoints[0]) if endpoints else "",
            str(lan.get("fingerprint", "")) if lan else "",
            self,
        )
        dialog.cancel_requested.connect(self.cancel_invitation)
        dialog.exec()

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

    def start_migration(self, source_root_id: str, target_root_id: str) -> None:
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
            job = self.core_client.create_migration(source_root_id, target_root_id)
        except (CoreApiError, CoreUnavailable) as exc:
            QMessageBox.warning(self, "Перенос не запущен", self._core_error_text(exc))
            return
        self.audit.record(
            "core.storage.migration.started",
            f"Запущен перенос {source_root_id} → {target_root_id}; задание {job['id']}",
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
            DiskRole.OVERFLOW,
            DiskRole.BACKUP,
            DiskRole.MIRROR,
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
                    "write_enabled": (
                        config.mode == DiskMode.ACTIVE and not config.read_only
                    ),
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
        dialog.exec()

    def save_disk_configuration(self, disk_id: str, configuration: DiskConfiguration) -> None:
        old = self.settings.configuration_for(disk_id)
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
        self.store.save(self.settings)
        if network_changed:
            self.core_client = CoreClient(
                replace(
                    self.core_client.config,
                    lan_enabled=self.settings.lan_enabled,
                    lan_port=self.settings.lan_port,
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
