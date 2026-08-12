from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from cloud_storage.models import ROLE_LABELS, AppSettings, DiskRole, DiskSnapshot, DiskStatus
from cloud_storage.services.audit_log import AuditEvent
from cloud_storage.services.disk_service import evaluate_status
from cloud_storage.ui.connection_page import ConnectionCodePanel
from cloud_storage.ui.theme import COLORS
from cloud_storage.ui.widgets import DiskCard, StatCard, clear_layout, format_bytes, make_header

AUTOMATION_TRIGGER_LABELS = {
    "diagnostic_warning": "предупреждение диагностики",
    "diagnostic_critical": "критический инцидент",
    "storage_low": "нехватка места",
    "backup_failed": "ошибка резервной копии",
    "restore_failed": "ошибка восстановления",
    "mirror_degraded": "зеркало требует восстановления",
    "maintenance_failed": "ошибка обслуживания диска",
    "pending_device": "устройство ожидает подтверждения",
    "tunnel_offline": "сбой интернет-шлюза",
    "power_outage": "пропало внешнее питание",
    "scheduled": "наступило время планового запуска",
}
AUTOMATION_ACTION_LABELS = {
    "notify": "создать уведомление",
    "quick_scan": "запустить быструю проверку",
    "full_scan": "запустить полную проверку SHA-256",
    "read_only": "включить режим только чтения",
    "run_backup": "создать и проверить резервную копию",
    "reconcile_mirrors": "проверить и восстановить зеркало",
    "restart_tunnel": "перезапустить интернет-шлюз",
    "sleep_after_hour": "перевести сервер в сон через час",
    "shutdown_after_hour": "безопасно выключить сервер через час",
}
AUTOMATION_ACTIONS_BY_TRIGGER = {
    "diagnostic_warning": ("notify", "quick_scan", "full_scan"),
    "diagnostic_critical": ("notify", "quick_scan", "full_scan", "read_only"),
    "storage_low": ("notify", "quick_scan"),
    "backup_failed": ("notify", "quick_scan", "full_scan"),
    "restore_failed": ("notify", "quick_scan", "full_scan"),
    "mirror_degraded": ("notify", "quick_scan", "full_scan", "reconcile_mirrors"),
    "maintenance_failed": ("notify", "quick_scan", "full_scan"),
    "pending_device": ("notify",),
    "tunnel_offline": ("notify", "restart_tunnel"),
    "power_outage": ("notify", "shutdown_after_hour"),
    "scheduled": (
        "notify",
        "quick_scan",
        "full_scan",
        "run_backup",
        "reconcile_mirrors",
        "sleep_after_hour",
    ),
}


def format_automation_result(value: object) -> str:
    text = str(value or "")
    labels = {
        "server_already_read_only": "сервер уже работает только на чтение",
        "no_enabled_backup_policy": "нет включённой политики резервирования",
        "no_writable_mirror": "нет доступного зеркального диска",
        "no_offline_tunnel": "интернет-шлюз уже работает или выключен",
        "shutdown_not_armed": "аварийное выключение не разрешено в Системе",
        "shutdown_already_scheduled": "аварийное выключение уже запланировано",
    }
    if text in labels:
        return labels[text]
    prefixes = {
        "notification:": "уведомление создано",
        "diagnostic_scan:": "диагностическая проверка запущена",
        "server_mode:": "защитный режим сервера изменён",
        "backup_job:": "резервная копия поставлена в очередь",
        "mirror_job:": "восстановление зеркала поставлено в очередь",
        "tunnel:": "перезапуск интернет-шлюза запущен",
        "shutdown_scheduled:": "аварийное выключение запланировано",
    }
    for prefix, label in prefixes.items():
        if text.startswith(prefix):
            return label
    return text


def _scroll_page(content: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setWidget(content)
    return area


class DashboardPage(QWidget):
    setup_requested = Signal()
    remind_later_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        content = QWidget()
        self.layout = QVBoxLayout(content)
        self.layout.setContentsMargins(4, 4, 16, 24)
        self.layout.setSpacing(18)
        self.layout.addWidget(
            make_header(
                "Основная",
                "Состояние менеджера, накопителей и серверного ядра.",
            )
        )

        self.setup_banner = QFrame()
        self.setup_banner.setProperty("accent", "red")
        banner_layout = QHBoxLayout(self.setup_banner)
        banner_layout.setContentsMargins(18, 14, 18, 14)
        text = QVBoxLayout()
        banner_title = QLabel("Первоначальная настройка не завершена")
        banner_title.setStyleSheet("font-weight: 700; font-size: 16px;")
        banner_body = QLabel(
            "Можно настроить диски сейчас или отложить. Менеджер не форматирует и не переносит данные."
        )
        banner_body.setProperty("muted", True)
        banner_body.setWordWrap(True)
        text.addWidget(banner_title)
        text.addWidget(banner_body)
        banner_layout.addLayout(text, 1)
        later = QPushButton("Напомнить позже")
        later.clicked.connect(self.remind_later_requested)
        begin = QPushButton("Начать настройку")
        begin.setProperty("primary", True)
        begin.clicked.connect(self.setup_requested)
        banner_layout.addWidget(later)
        banner_layout.addWidget(begin)
        self.layout.addWidget(self.setup_banner)

        metrics = QHBoxLayout()
        metrics.setSpacing(12)
        self.server_card = StatCard("Сервер", "Ожидает настройки", "Ядро — следующий этап")
        self.storage_card = StatCard("Хранилище", "—", "Диски не обнаружены")
        self.disk_card = StatCard("Накопители", "0", "Нет данных")
        self.connection_card = StatCard("Доступ", "Локально", "Удалённый доступ выключен")
        for card in (self.server_card, self.storage_card, self.disk_card, self.connection_card):
            metrics.addWidget(card, 1)
        self.layout.addLayout(metrics)

        storage = QFrame()
        storage.setProperty("card", True)
        storage_layout = QVBoxLayout(storage)
        storage_layout.setContentsMargins(18, 16, 18, 16)
        storage_title_row = QHBoxLayout()
        title = QLabel("Использование хранилища")
        title.setObjectName("SectionTitle")
        self.storage_summary = QLabel("—")
        self.storage_summary.setProperty("muted", True)
        storage_title_row.addWidget(title)
        storage_title_row.addStretch()
        storage_title_row.addWidget(self.storage_summary)
        self.storage_progress = QProgressBar()
        self.storage_progress.setRange(0, 100)
        self.storage_progress.setTextVisible(False)
        storage_layout.addLayout(storage_title_row)
        storage_layout.addWidget(self.storage_progress)
        note = QLabel(
            "Прогноз заполнения появится после накопления реальной истории использования."
        )
        note.setProperty("muted", True)
        storage_layout.addWidget(note)
        self.layout.addWidget(storage)

        lower = QHBoxLayout()
        lower.setSpacing(12)
        health = QFrame()
        health.setProperty("card", True)
        health_layout = QVBoxLayout(health)
        health_layout.setContentsMargins(18, 16, 18, 16)
        health_title = QLabel("Состояние дисков")
        health_title.setObjectName("SectionTitle")
        self.health_rows = QVBoxLayout()
        health_layout.addWidget(health_title)
        health_layout.addLayout(self.health_rows)
        health_layout.addStretch()
        lower.addWidget(health, 1)

        activity = QFrame()
        activity.setProperty("card", True)
        activity_layout = QVBoxLayout(activity)
        activity_layout.setContentsMargins(18, 16, 18, 16)
        activity_title = QLabel("Последние действия")
        activity_title.setObjectName("SectionTitle")
        self.activity_rows = QVBoxLayout()
        activity_layout.addWidget(activity_title)
        activity_layout.addLayout(self.activity_rows)
        activity_layout.addStretch()
        lower.addWidget(activity, 1)
        self.layout.addLayout(lower)
        self.layout.addStretch()
        outer.addWidget(_scroll_page(content))

    def set_setup_banner_visible(self, visible: bool) -> None:
        self.setup_banner.setVisible(visible)

    def update_data(
        self,
        disks: list[DiskSnapshot],
        settings: AppSettings,
        events: list[AuditEvent],
        core_health: dict | None = None,
        core_summary: dict | None = None,
    ) -> None:
        configured = [
            item
            for item in disks
            if settings.configuration_for(item.id).role
            not in {DiskRole.UNCONFIGURED, DiskRole.UNUSED}
        ]
        total = sum(item.total_bytes for item in configured if item.available)
        used = sum(item.used_bytes for item in configured if item.available)
        free = sum(item.free_bytes for item in configured if item.available)

        if core_health:
            uptime = int(core_health.get("uptime_seconds", 0))
            runtime = "Go Core" if core_health.get("runtime") == "go" else "Core"
            self.server_card.set_value(
                "Ядро работает",
                f"{runtime} {core_health.get('version', '—')} · {uptime // 60} мин",
            )
        elif settings.setup_complete:
            self.server_card.set_value("Ядро выключено", "Запускается в настройках сервера")
        else:
            self.server_card.set_value("Ожидает настройки", "Безопасный режим")
        self.setup_banner.setVisible(not settings.setup_complete and self.setup_banner.isVisible())
        self.storage_card.set_value(
            format_bytes(total) if total else "—",
            f"Свободно {format_bytes(free)}" if total else "Нет настроенных дисков",
        )
        unavailable = sum(not item.available for item in disks)
        self.disk_card.set_value(str(len(disks)), f"Недоступно: {unavailable}")
        if core_health:
            users = (core_summary or {}).get("users", 0)
            devices = (core_summary or {}).get("trusted_devices", 0)
            remote = core_health.get("remote")
            lan = core_health.get("lan")
            access = "Удалённо · HTTPS" if remote else "LAN · HTTPS" if lan else "Только локально"
            self.connection_card.set_value(access, f"Пользователи: {users} · Устройства: {devices}")
        else:
            self.connection_card.set_value("Недоступно", "Серверное ядро выключено")

        percent = round(used / total * 100) if total else 0
        self.storage_progress.setValue(percent)
        self.storage_summary.setText(
            f"{format_bytes(used)} из {format_bytes(total)}" if total else "Нет настроенных дисков"
        )

        clear_layout(self.health_rows)
        if not disks:
            label = QLabel("Физические накопители не обнаружены")
            label.setProperty("muted", True)
            self.health_rows.addWidget(label)
        for disk in disks[:5]:
            config = settings.configuration_for(disk.id)
            status = evaluate_status(disk, config)
            row = QHBoxLayout()
            name = QLabel(config.display_name or disk.label or disk.mountpoint)
            state = QLabel(
                {
                    DiskStatus.HEALTHY: "Всё хорошо",
                    DiskStatus.UNCONFIGURED: "Не настроен",
                    DiskStatus.ALMOST_FULL: "Почти заполнен",
                }.get(status, status.value.replace("_", " "))
            )
            color = (
                COLORS["green"]
                if status == DiskStatus.HEALTHY
                else (COLORS["gray"] if status == DiskStatus.UNCONFIGURED else COLORS["yellow"])
            )
            state.setStyleSheet(f"color: {color};")
            row.addWidget(name, 1)
            row.addWidget(state)
            self.health_rows.addLayout(row)
            name.show()
            state.show()

        clear_layout(self.activity_rows)
        if not events:
            label = QLabel("Действий пока нет")
            label.setProperty("muted", True)
            self.activity_rows.addWidget(label)
        for event in events[:5]:
            row = QVBoxLayout()
            label = QLabel(event.detail)
            label.setWordWrap(True)
            try:
                stamp = (
                    datetime.fromisoformat(event.timestamp).astimezone().strftime("%d.%m · %H:%M")
                )
            except ValueError:
                stamp = event.timestamp
            time_label = QLabel(stamp)
            time_label.setProperty("muted", True)
            row.addWidget(label)
            row.addWidget(time_label)
            self.activity_rows.addLayout(row)
            label.show()
            time_label.show()


class DisksPage(QWidget):
    disk_selected = Signal(str)
    refresh_requested = Signal()
    configure_first_requested = Signal()
    remind_later_requested = Signal()
    ignore_unconfigured_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        content = QWidget()
        self.layout = QVBoxLayout(content)
        self.layout.setContentsMargins(4, 4, 16, 24)
        self.layout.setSpacing(18)
        self.layout.addWidget(
            make_header(
                "Диски",
                "Реальные накопители системы. Beta 0.2 не форматирует и не переносит данные.",
                ("Обновить", self.refresh_requested.emit),
            )
        )

        view_controls = QHBoxLayout()
        self.physical_view_button = QPushButton("Физические диски")
        self.roles_view_button = QPushButton("Назначения и роли")
        self.physical_view_button.setCheckable(True)
        self.roles_view_button.setCheckable(True)
        self.physical_view_button.clicked.connect(lambda: self._set_view("physical"))
        self.roles_view_button.clicked.connect(lambda: self._set_view("roles"))
        view_controls.addWidget(self.physical_view_button)
        view_controls.addWidget(self.roles_view_button)
        view_controls.addStretch()
        self.layout.addLayout(view_controls)
        self._view = "physical"
        self._disks: list[DiskSnapshot] = []
        self._settings = AppSettings()
        self._reminder_hidden = False

        self.new_disk_banner = QFrame()
        self.new_disk_banner.setProperty("accent", "blue")
        banner_layout = QHBoxLayout(self.new_disk_banner)
        banner_layout.setContentsMargins(18, 14, 18, 14)
        banner_text = QVBoxLayout()
        self.new_disk_title = QLabel("Обнаружен новый диск")
        self.new_disk_title.setStyleSheet("font-size: 16px; font-weight: 700;")
        description = QLabel("Выберите назначение или вернитесь к настройке позднее.")
        description.setProperty("muted", True)
        banner_text.addWidget(self.new_disk_title)
        banner_text.addWidget(description)
        banner_layout.addLayout(banner_text, 1)
        ignore = QPushButton("Пока не использовать")
        ignore.clicked.connect(self.ignore_unconfigured_requested)
        later = QPushButton("Напомнить позже")
        later.clicked.connect(self.remind_later_requested)
        configure = QPushButton("Настроить")
        configure.setProperty("primary", True)
        configure.clicked.connect(self.configure_first_requested)
        banner_layout.addWidget(ignore)
        banner_layout.addWidget(later)
        banner_layout.addWidget(configure)
        self.layout.addWidget(self.new_disk_banner)

        self.cards = QGridLayout()
        self.cards.setHorizontalSpacing(12)
        self.cards.setVerticalSpacing(12)
        self.layout.addLayout(self.cards)
        self.empty = QLabel("Накопители не обнаружены. Подключите диск и нажмите «Обновить».")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setProperty("muted", True)
        self.empty.setMinimumHeight(180)
        self.layout.addWidget(self.empty)
        self.layout.addStretch()
        outer.addWidget(_scroll_page(content))

    def update_data(
        self, disks: list[DiskSnapshot], settings: AppSettings, reminder_hidden=False
    ) -> None:
        self._disks = disks
        self._settings = settings
        self._reminder_hidden = bool(reminder_hidden)
        self._render_cards()

    def _set_view(self, view: str) -> None:
        self._view = view if view in {"physical", "roles"} else "physical"
        self._render_cards()

    def _render_cards(self) -> None:
        disks = self._disks
        settings = self._settings
        while self.cards.count():
            item = self.cards.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        pending = [
            disk
            for disk in disks
            if settings.configuration_for(disk.id).role == DiskRole.UNCONFIGURED
            and disk.id not in settings.ignored_disk_ids
            and disk.available
        ]
        self.new_disk_banner.setVisible(bool(pending) and not self._reminder_hidden)
        self.new_disk_title.setText(
            "Обнаружен новый диск"
            if len(pending) == 1
            else f"Обнаружено новых дисков: {len(pending)}"
        )
        self.physical_view_button.setChecked(self._view == "physical")
        self.roles_view_button.setChecked(self._view == "roles")
        self.empty.setVisible(not disks)
        if self._view == "physical":
            for index, disk in enumerate(disks):
                card = DiskCard(disk, settings.configuration_for(disk.id))
                card.selected.connect(self.disk_selected)
                self.cards.addWidget(card, index // 2, index % 2)
            return
        grouped: list[tuple[DiskRole, list[DiskSnapshot]]] = []
        for role in DiskRole:
            assigned = [
                disk for disk in disks if settings.configuration_for(disk.id).role == role
            ]
            if assigned:
                grouped.append((role, assigned))
        for index, (role, assigned) in enumerate(grouped):
            card = QFrame()
            card.setProperty("card", True)
            card_layout = QVBoxLayout(card)
            heading = QLabel(ROLE_LABELS[role])
            heading.setStyleSheet("font-size: 16px; font-weight: 700;")
            total = sum(item.total_bytes for item in assigned if item.available)
            detail = QLabel(
                f"Дисков: {len(assigned)} · Объём: {format_bytes(total)}\n"
                + "\n".join(
                    f"• {settings.configuration_for(item.id).display_name or item.label or item.mountpoint} ({item.mountpoint})"
                    for item in assigned
                )
            )
            detail.setWordWrap(True)
            detail.setProperty("muted", True)
            card_layout.addWidget(heading)
            card_layout.addWidget(detail)
            open_first = QPushButton("Открыть первый диск")
            open_first.clicked.connect(
                lambda _checked=False, disk_id=assigned[0].id: self.disk_selected.emit(disk_id)
            )
            card_layout.addWidget(open_first)
            self.cards.addWidget(card, index // 2, index % 2)


class SettingsPage(QWidget):
    save_requested = Signal(dict)
    advanced_mode_changed = Signal(bool)
    refresh_requested = Signal()
    core_start_requested = Signal()
    core_stop_requested = Signal()
    core_refresh_requested = Signal()
    add_user_requested = Signal()
    invitation_requested = Signal(str, str)
    reset_password_requested = Signal(str, str)
    approve_device_requested = Signal(str)
    revoke_device_requested = Signal(str)
    migration_requested = Signal(str, str)
    maintenance_refresh_requested = Signal()
    maintenance_resume_requested = Signal(str)
    maintenance_cancel_requested = Signal(str)
    backup_requested = Signal(str)
    backup_resume_requested = Signal(str)
    backup_cancel_requested = Signal(str)
    backup_policy_requested = Signal(dict)
    backup_policy_run_requested = Signal(str)
    backup_verify_requested = Signal(str, str)
    backup_verification_cancel_requested = Signal(str)
    mirror_reconcile_requested = Signal(str)
    mirror_resume_requested = Signal(str)
    mirror_cancel_requested = Signal(str)
    diagnostics_quick_requested = Signal()
    diagnostics_full_requested = Signal()
    diagnostic_remediation_requested = Signal(str, str)
    tunnel_restart_requested = Signal(str)
    support_bundle_requested = Signal()
    automation_rule_create_requested = Signal(dict)
    automation_rule_update_requested = Signal(str, dict)
    automation_rule_delete_requested = Signal(str)
    automation_evaluate_requested = Signal()
    automation_preview_requested = Signal()
    automation_rule_preview_requested = Signal(str)
    automation_settings_requested = Signal(dict)
    notification_acknowledge_requested = Signal(str)
    integration_test_requested = Signal(str)
    server_read_only_requested = Signal()
    server_normal_requested = Signal()
    restore_requested = Signal(str, str)
    restore_resume_requested = Signal(str)
    restore_cancel_requested = Signal(str)
    open_updates_requested = Signal()
    system_section_requested = Signal(str)
    disks_requested = Signal()
    connection_generation_requested = Signal(str, str, str)

    _SECTIONS = [
        ("Сервер", False),
        ("Подключение", False),
        ("Система", False),
        ("Docker", False),
        ("SSH", False),
        ("Хранилище", False),
        ("Диски", False),
        ("Пользователи", False),
        ("Права доступа", True),
        ("Доверенные устройства", False),
        ("Подключение устройств", False),
        ("Резервные копии", True),
        ("Автоматизация", True),
        ("Сеть", True),
        ("Удалённый доступ", True),
        ("Безопасность", False),
        ("Уведомления", False),
        ("Интерфейс", False),
        ("Журналы", True),
        ("Диагностика", False),
        ("Обслуживание", True),
        ("Обновления", False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._loading_settings = False
        self._saved_settings_payload: dict = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 16, 24)
        root.setSpacing(18)
        root.addWidget(
            make_header("Настройки", "Простой и расширенный режимы управления сервером.")
        )

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("Режим:"))
        self.mode = QComboBox()
        self.mode.addItem("Простой", False)
        self.mode.addItem("Расширенный", True)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        toolbar.addWidget(self.mode)
        toolbar.addStretch()
        self.save_button = QPushButton("Сохранить настройки")
        self.save_button.setProperty("primary", True)
        self.save_button.clicked.connect(self._emit_save)
        self.save_button.setVisible(False)
        toolbar.addWidget(self.save_button)
        root.addLayout(toolbar)

        body = QHBoxLayout()
        body.setSpacing(16)
        self.section_list = QListWidget()
        self.section_list.setObjectName("SettingsSections")
        self.section_list.setFixedWidth(205)
        self.section_list.setWordWrap(True)
        self.section_list.setSpacing(1)
        self.section_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.section_list.currentRowChanged.connect(self._show_section)
        self.stack = QStackedWidget()
        body.addWidget(self.section_list)
        body.addWidget(self.stack, 1)
        root.addLayout(body, 1)

        self.server_name = QLineEdit()
        self.server_name.setMaxLength(80)
        self.notifications = QCheckBox("Показывать системные уведомления")
        self.refresh_interval = QSpinBox()
        self.refresh_interval.setRange(10, 300)
        self.refresh_interval.setSuffix(" сек")
        self.config_path = QLabel("—")
        self.config_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.server_state_label: QLabel | None = None
        self.connection_panel: ConnectionCodePanel | None = None
        self.core_status_label: QLabel | None = None
        self.core_details_label: QLabel | None = None
        self.core_start_button: QPushButton | None = None
        self.core_stop_button: QPushButton | None = None
        self.server_mode_label: QLabel | None = None
        self.server_mode_detail: QLabel | None = None
        self.read_only_button: QPushButton | None = None
        self.normal_mode_button: QPushButton | None = None
        self.lan_enabled = QCheckBox("Разрешить защищённое подключение устройств из локальной сети")
        self.lan_port = QSpinBox()
        self.lan_port.setRange(1024, 65535)
        self.lan_port.setValue(8766)
        self.lan_status_label: QLabel | None = None
        self.lan_details_label: QLabel | None = None
        self.remote_enabled = QCheckBox(
            "Включить отдельный защищённый HTTPS-вход для клиентов из интернета"
        )
        self.remote_port = QSpinBox()
        self.remote_port.setRange(1024, 65535)
        self.remote_port.setValue(8767)
        self.remote_public_url = QLineEdit()
        self.remote_public_url.setPlaceholderText("https://cloud.example.net:8767")
        self.remote_pairing_enabled = QCheckBox(
            "Разрешить погашение одноразовых кодов через удалённый вход"
        )
        self.remote_status_label: QLabel | None = None
        self.remote_details_label: QLabel | None = None
        self.remote_audit_rows: QVBoxLayout | None = None
        self.zrok_enabled = QCheckBox(
            "Подключать сервер к интернету через zrok без открытия порта роутера"
        )
        self.zrok_port = QSpinBox()
        self.zrok_port.setRange(1024, 65535)
        self.zrok_port.setValue(8768)
        self.zrok_executable = QLineEdit("zrok")
        self.zrok_executable.setPlaceholderText("zrok или полный путь к zrok.exe")
        self.zrok_share_name = QLineEdit()
        self.zrok_share_name.setMaxLength(63)
        self.zrok_share_name.setPlaceholderText("Необязательно: имя заранее созданного reserved share")
        self.zrok_status_label: QLabel | None = None
        self.zrok_details_label: QLabel | None = None
        self.zrok_restart_button: QPushButton | None = None
        self.users_rows: QVBoxLayout | None = None
        self.trusted_device_rows: QVBoxLayout | None = None
        self.pending_device_rows: QVBoxLayout | None = None
        self.migration_source: QComboBox | None = None
        self.migration_target: QComboBox | None = None
        self.migration_start_button: QPushButton | None = None
        self.maintenance_rows: QVBoxLayout | None = None
        self.backup_target: QComboBox | None = None
        self.backup_start_button: QPushButton | None = None
        self.backup_rows: QVBoxLayout | None = None
        self.restore_target: QComboBox | None = None
        self.restore_rows: QVBoxLayout | None = None
        self.backup_policy_enabled: QCheckBox | None = None
        self.backup_policy_interval: QSpinBox | None = None
        self.backup_policy_keep: QSpinBox | None = None
        self.backup_policy_save_button: QPushButton | None = None
        self.backup_policy_run_button: QPushButton | None = None
        self.backup_verification_rows: QVBoxLayout | None = None
        self._backup_policies: list[dict] = []
        self._backup_online = False
        self.mirror_target: QComboBox | None = None
        self.mirror_start_button: QPushButton | None = None
        self.mirror_rows: QVBoxLayout | None = None
        self.mirror_job_rows: QVBoxLayout | None = None
        self.automation_rule_name: QLineEdit | None = None
        self.automation_trigger: QComboBox | None = None
        self.automation_action: QComboBox | None = None
        self.automation_cooldown: QSpinBox | None = None
        self.automation_create_button: QPushButton | None = None
        self.automation_evaluate_button: QPushButton | None = None
        self.automation_preview_button: QPushButton | None = None
        self.automation_scheduler_enabled: QCheckBox | None = None
        self.automation_scheduler_interval: QSpinBox | None = None
        self.automation_scheduler_status: QLabel | None = None
        self.automation_settings_button: QPushButton | None = None
        self.automation_rule_hint: QLabel | None = None
        self.automation_rule_rows: QVBoxLayout | None = None
        self.automation_run_rows: QVBoxLayout | None = None
        self.notification_rows: QVBoxLayout | None = None
        self.integration_rows: QVBoxLayout | None = None
        self.log_rows: QVBoxLayout | None = None
        self.log_path: QLabel | None = None
        self.diagnostics_status_label: QLabel | None = None
        self.diagnostics_detail_label: QLabel | None = None
        self.diagnostics_rows: QVBoxLayout | None = None
        self.diagnostics_quick_button: QPushButton | None = None
        self.diagnostics_full_button: QPushButton | None = None
        self.support_bundle_button: QPushButton | None = None
        self.security_overall_label: QLabel | None = None
        self.security_network_label: QLabel | None = None
        self.security_files_label: QLabel | None = None
        self.security_connection_label: QLabel | None = None
        self.storage_status_label: QLabel | None = None
        self.disk_settings_status_label: QLabel | None = None
        self.access_status_label: QLabel | None = None
        self._current_settings = AppSettings()
        self._pages_by_name = {name: self._make_section(name) for name, _ in self._SECTIONS}
        self._rebuild_sections()
        self._connect_settings_tracking()

    def update_core_data(
        self,
        health: dict | None,
        summary: dict | None,
        users: list[dict],
        devices: list[dict],
        storage_roots: list[dict] | None = None,
        maintenance_jobs: list[dict] | None = None,
        backup_jobs: list[dict] | None = None,
        mirror_state: dict | None = None,
        diagnostics: dict | None = None,
        server_mode: dict | None = None,
        restore_jobs: list[dict] | None = None,
        backup_automation: dict | None = None,
        audit_events: list[dict] | None = None,
        tunnels: dict | None = None,
        automation_rules: dict | None = None,
        notifications: list[dict] | None = None,
        integrations: dict | None = None,
    ) -> None:
        online = health is not None
        if self.connection_panel is not None:
            self.connection_panel.set_data(health, users, tunnels)
        if self.core_status_label is not None:
            self.core_status_label.setText("Работает" if online else "Выключено")
            self.core_status_label.setStyleSheet(
                "color: #43c778; font-weight: 700;" if online else "color: #949ca8;"
            )
        if self.core_details_label is not None:
            if health and health.get("runtime") == "go":
                details = (
                    f"API {health.get('bind')} · Go runtime {health.get('supervisor_version')} · "
                    f"совместимый API {health.get('version')}"
                )
            elif health:
                details = f"API {health.get('bind')} · версия {health.get('version')}"
            else:
                details = (
                    "Go-ядро запускается отдельной службой и продолжает работу после "
                    "закрытия менеджера."
                )
            self.core_details_label.setText(details)
        if self.core_start_button is not None:
            self.core_start_button.setEnabled(not online)
        if self.core_stop_button is not None:
            self.core_stop_button.setEnabled(online)
        mode = (server_mode or {}).get("mode", "normal")
        read_only = online and mode == "read_only"
        if self.server_mode_label is not None:
            self.server_mode_label.setText(
                "Аварийный режим · только чтение" if read_only else "Обычный режим"
            )
            self.server_mode_label.setStyleSheet(
                "color: #e2383f; font-weight: 700;"
                if read_only
                else "color: #43c778; font-weight: 700;"
            )
        if self.server_mode_detail is not None:
            reason = str((server_mode or {}).get("reason") or "Причина не указана")
            changed = str((server_mode or {}).get("changed_at") or "—")
            self.server_mode_detail.setText(
                f"Запись, удаление, новые подключения и фоновые задания остановлены. "
                f"Чтение и диагностика доступны.\nПричина: {reason} · изменено {changed}"
                if read_only
                else "Загрузка, удаление, подключение устройств и фоновые задания разрешены."
            )
        if self.read_only_button is not None:
            self.read_only_button.setEnabled(online and not read_only)
        if self.normal_mode_button is not None:
            self.normal_mode_button.setEnabled(online and read_only)
        if self.security_overall_label is not None:
            incidents = (diagnostics or {}).get("incidents") or []
            critical = sum(
                str(item.get("severity", "")).casefold() == "critical"
                and str(item.get("status", "open")).casefold() not in {"resolved", "closed"}
                for item in incidents
            )
            self.security_overall_label.setText(
                "Защита активна · критических инцидентов нет"
                if online and not critical
                else f"Требуется внимание · критических инцидентов: {critical}"
                if online
                else "Ядро выключено — проверка недоступна"
            )
            self.security_overall_label.setStyleSheet(
                "color: #43c778; font-weight: 700;"
                if online and not critical
                else "color: #e2383f; font-weight: 700;"
            )
        if self.security_network_label is not None:
            lan = (health or {}).get("lan") or {}
            remote_security = (health or {}).get("remote") or {}
            zrok_security = (tunnels or {}).get("zrok") or {}
            lan_active = bool(lan.get("enabled", lan.get("endpoints")))
            remote_active = bool(remote_security.get("enabled"))
            zrok_active = zrok_security.get("state") == "online"
            self.security_network_label.setText(
                f"LAN HTTPS: {'включён' if lan_active else 'выключен'}\n"
                f"Интернет-вход: {'активен' if remote_active or zrok_active else 'выключен'}\n"
                "Manager API: только 127.0.0.1\n"
                "Передача: TLS; сертификат проверяется по SHA-256"
            )
        if self.security_files_label is not None:
            configured = [
                config
                for config in self._current_settings.disk_configurations.values()
                if config.role not in {DiskRole.UNCONFIGURED, DiskRole.UNUSED}
            ]
            encrypted = sum(config.encryption_requested for config in configured)
            writable = sum(
                config.mode.value == "active" and not config.read_only for config in configured
            )
            self.security_files_label.setText(
                "Загруженные файлы не запускаются на сервере.\n"
                "Запись атомарная, SHA-256 проверяется при фиксации.\n"
                f"Дисков с записью: {writable} · запрошено шифрование: {encrypted}.\n"
                f"Режим сервера: {'только чтение' if read_only else 'обычный'}"
            )
        if self.security_connection_label is not None:
            trusted = sum(item.get("status") == "trusted" for item in devices)
            pending = sum(item.get("status") == "pending" for item in devices)
            lan = (health or {}).get("lan") or {}
            remote_security = (health or {}).get("remote") or {}
            pairing_enabled = bool(remote_security.get("pairing_enabled"))
            fingerprint = str(
                remote_security.get("display_fingerprint")
                or lan.get("display_fingerprint")
                or "—"
            )
            self.security_connection_label.setText(
                f"Доверенных устройств: {trusted} · ожидают: {pending}\n"
                f"Удалённые одноразовые коды: {'разрешены' if pairing_enabled else 'закрыты'}\n"
                "Пароли: Argon2id; токены устройств хранятся отдельно.\n"
                f"TLS SHA-256: {fingerprint}"
            )
        roots = list(storage_roots or [])
        if self.storage_status_label is not None:
            writable_roots = sum(bool(item.get("write_enabled", True)) for item in roots)
            backup_roots = sum(item.get("purpose") == "backup" for item in roots)
            self.storage_status_label.setText(
                f"Ядро: {'работает' if online else 'выключено'}\n"
                f"Активных путей хранения: {len(roots)} · с записью: {writable_roots}\n"
                f"Путей резервных копий: {backup_roots}"
            )
        if self.disk_settings_status_label is not None:
            configurations = list(self._current_settings.disk_configurations.values())
            configured_count = sum(
                item.role not in {DiskRole.UNCONFIGURED, DiskRole.UNUSED}
                for item in configurations
            )
            cache_count = sum(item.role == DiskRole.CACHE for item in configurations)
            self.disk_settings_status_label.setText(
                f"Обнаружено конфигураций: {len(configurations)} · назначено: {configured_count}\n"
                f"Кэш-дисков: {cache_count} · синхронизировано с Core: {len(roots)}"
            )
        if self.access_status_label is not None:
            trusted = sum(item.get("status") == "trusted" for item in devices)
            pending = sum(item.get("status") == "pending" for item in devices)
            admins = sum(item.get("role") == "admin" for item in users)
            self.access_status_label.setText(
                f"Пользователей: {len(users)} · администраторов: {admins}\n"
                f"Доверенных устройств: {trusted} · ожидают подтверждения: {pending}\n"
                "Изоляция личных пространств выполняется серверным ядром."
            )
        if self.lan_status_label is not None:
            lan = health.get("lan") if health else None
            if lan:
                self.lan_status_label.setText("HTTPS включён")
                self.lan_status_label.setStyleSheet("color: #43c778; font-weight: 700;")
            elif self._current_settings.lan_enabled and online:
                self.lan_status_label.setText("Требуется перезапуск ядра")
                self.lan_status_label.setStyleSheet("color: #f5bd4f; font-weight: 700;")
            else:
                self.lan_status_label.setText("Выключен")
                self.lan_status_label.setStyleSheet("color: #949ca8;")
        if self.lan_details_label is not None:
            lan = health.get("lan") if health else None
            if lan:
                endpoints = lan.get("endpoints") or []
                endpoint_text = " · ".join(endpoints) if endpoints else "сетевой адрес не найден"
                fingerprint = str(lan.get("display_fingerprint", ""))
                self.lan_details_label.setText(f"Адреса: {endpoint_text}\nSHA-256: {fingerprint}")
            else:
                self.lan_details_label.setText(
                    "После включения Core создаст постоянный сертификат и отдельный HTTPS-вход для клиентов."
                )

        remote = health.get("remote") if health else None
        if self.remote_status_label is not None:
            if remote:
                pairing = " · подключение по коду разрешено" if remote.get("pairing_enabled") else ""
                self.remote_status_label.setText(f"HTTPS включён{pairing}")
                self.remote_status_label.setStyleSheet("color: #43c778; font-weight: 700;")
            elif self._current_settings.remote_enabled and online:
                self.remote_status_label.setText("Требуется перезапуск ядра")
                self.remote_status_label.setStyleSheet("color: #f5bd4f; font-weight: 700;")
            else:
                self.remote_status_label.setText("Выключен")
                self.remote_status_label.setStyleSheet("color: #949ca8;")
        if self.remote_details_label is not None:
            if remote:
                self.remote_details_label.setText(
                    f"Публичный адрес: {remote.get('public_url', '—')}\n"
                    f"SHA-256: {remote.get('display_fingerprint', '')}\n"
                    "Административный API: недоступен снаружи · автоматическая настройка роутера: выключена"
                )
            else:
                self.remote_details_label.setText(
                    "Рекомендуется VPN. Для прямого доступа вручную направьте внешний TCP-порт на указанный "
                    "HTTPS-порт этого сервера и проверьте соединение из другой сети."
                )
        if self.remote_audit_rows is not None:
            clear_layout(self.remote_audit_rows)
            remote_events = [
                event
                for event in (audit_events or [])
                if str(event.get("action", "")).startswith("remote.access.")
            ][:8]
            if not remote_events:
                self._add_muted(self.remote_audit_rows, "Внешних обращений пока не зарегистрировано.")
            for event in remote_events:
                status = "разрешено" if event.get("action") == "remote.access.allowed" else "отклонено"
                label = QLabel(
                    f"{event.get('timestamp', '—')} · {event.get('remote_address') or '—'} · "
                    f"{status} · {event.get('detail', '')}"
                )
                label.setWordWrap(True)
                label.setProperty("muted", True)
                self.remote_audit_rows.addWidget(label)

        zrok = (tunnels or {}).get("zrok", {})
        if self.zrok_restart_button is not None:
            self.zrok_restart_button.setEnabled(online and bool(zrok.get("enabled")))
        if self.zrok_status_label is not None:
            state = str(zrok.get("state", "disabled"))
            labels = {
                "online": "Подключён к интернету",
                "starting": "Запускается",
                "not_installed": "zrok не установлен",
                "error": "Ошибка — будет повтор",
                "stopped": "Остановлен",
                "disabled": "Выключен",
            }
            self.zrok_status_label.setText(labels.get(state, state))
            color = "#43c778" if state == "online" else "#e2383f" if state in {"error", "not_installed"} else "#f5bd4f" if state == "starting" else "#949ca8"
            self.zrok_status_label.setStyleSheet(f"color: {color}; font-weight: 700;")
        if self.zrok_details_label is not None:
            public_url = str(zrok.get("public_url") or "будет показан после запуска")
            error = str(zrok.get("last_error") or "")
            detail = (
                f"Публичный адрес: {public_url}\n"
                f"Локальный шлюз: {zrok.get('listener', f'http://127.0.0.1:{self._current_settings.zrok_port}')}\n"
                "Защита: логин + пароль + токен подтверждённого устройства · Manager API скрыт"
            )
            if error:
                detail += f"\nДиагностика: {error}"
            self.zrok_details_label.setText(detail)

        if self.users_rows is not None:
            clear_layout(self.users_rows)
            if not online:
                self._add_muted(
                    self.users_rows, "Запустите серверное ядро, чтобы управлять пользователями."
                )
            elif not users:
                self._add_muted(self.users_rows, "Пользователей пока нет.")
            for user in users:
                card = QFrame()
                card.setProperty("card", True)
                row = QHBoxLayout(card)
                text = QVBoxLayout()
                title = QLabel(user["display_name"])
                title.setStyleSheet("font-weight: 700; font-size: 16px;")
                quota = user["quota_bytes"] / 1024**3
                detail = QLabel(
                    f"@{user['username']} · {'Администратор' if user['role'] == 'admin' else 'Пользователь'} · "
                    f"{quota:.0f} ГБ · {'пароль установлен' if user.get('has_password') else 'пароль не задан'}"
                )
                detail.setProperty("muted", True)
                text.addWidget(title)
                text.addWidget(detail)
                row.addLayout(text, 1)
                password = QPushButton("Сменить пароль")
                password.clicked.connect(
                    lambda checked=False, user_id=user["id"], name=user["display_name"]: (
                        self.reset_password_requested.emit(user_id, name)
                    )
                )
                row.addWidget(password)
                invite = QPushButton("Код подключения")
                invite.clicked.connect(
                    lambda checked=False, user_id=user["id"], name=user["display_name"]: (
                        self.invitation_requested.emit(user_id, name)
                    )
                )
                row.addWidget(invite)
                self.users_rows.addWidget(card)
                card.show()

        trusted = [item for item in devices if item.get("status") == "trusted"]
        pending = [item for item in devices if item.get("status") == "pending"]
        if self.trusted_device_rows is not None:
            clear_layout(self.trusted_device_rows)
            if not trusted:
                self._add_muted(self.trusted_device_rows, "Доверенных устройств пока нет.")
            for device in trusted:
                self._add_device_card(self.trusted_device_rows, device, pending=False)
        if self.pending_device_rows is not None:
            clear_layout(self.pending_device_rows)
            if not online:
                self._add_muted(self.pending_device_rows, "Серверное ядро выключено.")
            elif not pending:
                self._add_muted(self.pending_device_rows, "Новых запросов на подключение нет.")
            for device in pending:
                self._add_device_card(self.pending_device_rows, device, pending=True)
        self._update_maintenance(storage_roots or [], maintenance_jobs or [], online)
        self._update_backups(
            storage_roots or [],
            backup_jobs or [],
            restore_jobs or [],
            backup_automation or {"policies": [], "verifications": []},
            online,
        )
        self._update_mirrors(mirror_state or {"roots": [], "jobs": []}, online)
        self._update_automation(automation_rules or {}, online)
        self._update_notifications(notifications or [], integrations or {}, online)
        self._update_diagnostics(diagnostics or {}, online)
        if self.log_rows is not None:
            clear_layout(self.log_rows)
            events = list(audit_events or [])[:20]
            if not events:
                self._add_muted(
                    self.log_rows,
                    "Событий Core пока нет. Они появятся после запуска ядра и первых действий.",
                )
            for event in events:
                label = QLabel(
                    f"{event.get('timestamp', '—')} · {event.get('action', 'событие')}\n"
                    f"{event.get('detail', '')}"
                )
                label.setWordWrap(True)
                label.setProperty("emptyState", True)
                self.log_rows.addWidget(label)

    def _update_backups(
        self,
        roots: list[dict],
        jobs: list[dict],
        restore_jobs: list[dict],
        automation: dict,
        online: bool,
    ) -> None:
        self._backup_online = online
        self._backup_policies = list(automation.get("policies") or [])
        if self.backup_target is not None:
            previous = self.backup_target.currentData()
            self.backup_target.clear()
            for root in roots:
                if root.get("purpose") == "backup" and root.get("write_enabled", True):
                    self.backup_target.addItem(
                        f"{root.get('disk_id') or root['id']} · {root['path']}", root["id"]
                    )
            previous_index = self.backup_target.findData(previous)
            if previous_index >= 0:
                self.backup_target.setCurrentIndex(previous_index)
            if self.backup_start_button is not None:
                self.backup_start_button.setEnabled(online and self.backup_target.count() > 0)
            if self.backup_policy_save_button is not None:
                self.backup_policy_save_button.setEnabled(
                    online and self.backup_target.count() > 0
                )
            self._load_selected_backup_policy()
        if self.restore_target is not None:
            previous = self.restore_target.currentData()
            self.restore_target.clear()
            for root in roots:
                if root.get("purpose") == "primary" and root.get("write_enabled", True):
                    self.restore_target.addItem(
                        f"{root.get('disk_id') or root['id']} · {root['path']}", root["id"]
                    )
            previous_index = self.restore_target.findData(previous)
            if previous_index >= 0:
                self.restore_target.setCurrentIndex(previous_index)
        self._render_restore_jobs(restore_jobs, online)
        self._render_backup_verifications(
            list(automation.get("verifications") or []), online
        )
        if self.backup_rows is None:
            return
        clear_layout(self.backup_rows)
        if not online:
            self._add_muted(self.backup_rows, "Запустите Core для резервного копирования.")
            return
        if not jobs:
            self._add_muted(self.backup_rows, "Проверенных снимков пока нет.")
            return
        status_labels = {
            "queued": "В очереди",
            "running": "Выполняется",
            "completed": "Готов и проверен",
            "failed": "Ошибка",
            "cancelled": "Отменено",
        }
        for job in jobs:
            card = QFrame()
            card.setProperty("card", True)
            card_layout = QVBoxLayout(card)
            title = QLabel(f"Резервный снимок · {job['target_root_id']}")
            title.setStyleSheet("font-weight: 700;")
            total = int(job.get("total_bytes", 0))
            processed = int(job.get("processed_bytes", 0))
            detail = QLabel(
                f"{status_labels.get(job['status'], job['status'])} · "
                f"{format_bytes(processed)} из {format_bytes(total)} · "
                f"объектов {job.get('processed_files', 0)}/{job.get('total_files', 0)}"
            )
            detail.setProperty("muted", True)
            progress = QProgressBar()
            progress.setRange(0, 100)
            progress.setValue(
                round(processed / total * 100)
                if total
                else (100 if job["status"] == "completed" else 0)
            )
            card_layout.addWidget(title)
            card_layout.addWidget(detail)
            card_layout.addWidget(progress)
            if job.get("snapshot_path"):
                path = QLabel(str(job["snapshot_path"]))
                path.setProperty("muted", True)
                card_layout.addWidget(path)
            if job.get("pruned_at"):
                pruned = QLabel(f"Удалён политикой хранения · {job['pruned_at']}")
                pruned.setStyleSheet("color: #949ca8;")
                card_layout.addWidget(pruned)
            if job.get("error"):
                error = QLabel(str(job["error"]))
                error.setWordWrap(True)
                error.setStyleSheet("color: #e2383f;")
                card_layout.addWidget(error)
            controls = QHBoxLayout()
            if job["status"] in {"failed", "cancelled"}:
                resume = QPushButton("Повторить снимок")
                resume.clicked.connect(
                    lambda _checked=False, job_id=job["id"]: self.backup_resume_requested.emit(
                        job_id
                    )
                )
                controls.addWidget(resume)
            if job["status"] in {"queued", "running"}:
                cancel = QPushButton("Отменить")
                cancel.clicked.connect(
                    lambda _checked=False, job_id=job["id"]: self.backup_cancel_requested.emit(
                        job_id
                    )
                )
                controls.addWidget(cancel)
            if (
                job["status"] == "completed"
                and not job.get("pruned_at")
                and self.restore_target is not None
            ):
                verify = QPushButton("Проверить пробным восстановлением")
                verify.setEnabled(self.restore_target.count() > 0)
                verify.clicked.connect(
                    lambda _checked=False, backup_id=job["id"]: (
                        self._emit_backup_verification(backup_id)
                    )
                )
                controls.addWidget(verify)
                restore = QPushButton("Восстановить повреждённые объекты")
                restore.setEnabled(self.restore_target.count() > 0)
                restore.clicked.connect(
                    lambda _checked=False, backup_id=job["id"]: self._emit_restore(
                        backup_id
                    )
                )
                controls.addWidget(restore)
            controls.addStretch()
            card_layout.addLayout(controls)
            self.backup_rows.addWidget(card)

    def _render_backup_verifications(self, jobs: list[dict], online: bool) -> None:
        if self.backup_verification_rows is None:
            return
        clear_layout(self.backup_verification_rows)
        if not online:
            self._add_muted(
                self.backup_verification_rows, "Запустите Core для проверки снимков."
            )
            return
        if not jobs:
            self._add_muted(
                self.backup_verification_rows,
                "Пробных восстановлений пока не запускали.",
            )
            return
        status_labels = {
            "queued": "В очереди",
            "running": "Пробное восстановление",
            "completed": "Снимок пригоден",
            "failed": "Снимок не прошёл проверку",
            "cancelled": "Остановлено",
        }
        for job in jobs:
            card = QFrame()
            card.setProperty("card", True)
            box = QVBoxLayout(card)
            title = QLabel(f"Проверка снимка · {job['backup_job_id']}")
            title.setStyleSheet("font-weight: 700;")
            detail = QLabel(
                f"{status_labels.get(job['status'], job['status'])} · "
                f"объектов {job.get('checked_objects', 0)}/{job.get('total_objects', 0)} · "
                f"прочитано и записано {format_bytes(int(job.get('checked_bytes', 0)))}"
            )
            detail.setProperty("muted", True)
            box.addWidget(title)
            box.addWidget(detail)
            if job.get("error"):
                error = QLabel(str(job["error"]))
                error.setWordWrap(True)
                error.setStyleSheet("color: #e2383f;")
                box.addWidget(error)
            if job["status"] in {"queued", "running"}:
                controls = QHBoxLayout()
                cancel = QPushButton("Остановить проверку")
                cancel.clicked.connect(
                    lambda _checked=False, verification_id=job["id"]: (
                        self.backup_verification_cancel_requested.emit(verification_id)
                    )
                )
                controls.addWidget(cancel)
                controls.addStretch()
                box.addLayout(controls)
            self.backup_verification_rows.addWidget(card)
    def _render_restore_jobs(self, jobs: list[dict], online: bool) -> None:
        if self.restore_rows is None:
            return
        clear_layout(self.restore_rows)
        if not online:
            self._add_muted(self.restore_rows, "Запустите Core для восстановления объектов.")
            return
        if not jobs:
            self._add_muted(self.restore_rows, "Заданий восстановления пока нет.")
            return
        status_labels = {
            "queued": "В очереди",
            "running": "Восстанавливается",
            "completed": "Завершено",
            "failed": "Завершено с ошибками",
            "cancelled": "Остановлено",
        }
        for job in jobs:
            card = QFrame()
            card.setProperty("card", True)
            box = QVBoxLayout(card)
            title = QLabel(f"Восстановление · снимок {job['backup_job_id']}")
            title.setStyleSheet("font-weight: 700;")
            total = int(job.get("total_bytes", 0))
            processed = int(job.get("processed_bytes", 0))
            detail = QLabel(
                f"{status_labels.get(job['status'], job['status'])} · "
                f"{format_bytes(processed)} из {format_bytes(total)} · "
                f"восстановлено {job.get('restored_objects', 0)} · "
                f"пропущено {job.get('skipped_objects', 0)} · "
                f"ошибок {job.get('failed_objects', 0)}"
            )
            detail.setProperty("muted", True)
            progress = QProgressBar()
            progress.setRange(0, 100)
            progress.setValue(
                round(processed / total * 100)
                if total
                else (100 if job["status"] == "completed" else 0)
            )
            box.addWidget(title)
            box.addWidget(detail)
            box.addWidget(progress)
            if job.get("error"):
                error = QLabel(str(job["error"]))
                error.setWordWrap(True)
                error.setStyleSheet("color: #e2383f;")
                box.addWidget(error)
            controls = QHBoxLayout()
            if job["status"] in {"failed", "cancelled"}:
                resume = QPushButton("Повторить восстановление")
                resume.clicked.connect(
                    lambda _checked=False, job_id=job["id"]: (
                        self.restore_resume_requested.emit(job_id)
                    )
                )
                controls.addWidget(resume)
            if job["status"] in {"queued", "running"}:
                cancel = QPushButton("Остановить")
                cancel.clicked.connect(
                    lambda _checked=False, job_id=job["id"]: (
                        self.restore_cancel_requested.emit(job_id)
                    )
                )
                controls.addWidget(cancel)
            controls.addStretch()
            box.addLayout(controls)
            self.restore_rows.addWidget(card)

    def _update_mirrors(self, state: dict, online: bool) -> None:
        roots = list(state.get("roots") or [])
        jobs = list(state.get("jobs") or [])
        if self.mirror_target is not None:
            previous = self.mirror_target.currentData()
            self.mirror_target.clear()
            for root in roots:
                if root.get("write_enabled", True):
                    self.mirror_target.addItem(
                        f"{root.get('disk_id') or root['root_id']} · {root['path']}",
                        root["root_id"],
                    )
            previous_index = self.mirror_target.findData(previous)
            if previous_index >= 0:
                self.mirror_target.setCurrentIndex(previous_index)
            if self.mirror_start_button is not None:
                self.mirror_start_button.setEnabled(online and self.mirror_target.count() > 0)
        if self.mirror_rows is not None:
            clear_layout(self.mirror_rows)
            if not online:
                self._add_muted(self.mirror_rows, "Запустите Core для контроля зеркал.")
            elif not roots:
                self._add_muted(
                    self.mirror_rows,
                    "Назначьте отдельному диску роль «Зеркало». Обычные загрузки на него не направляются.",
                )
            for root in roots:
                card = QFrame()
                card.setProperty("card", True)
                box = QVBoxLayout(card)
                title = QLabel(f"Зеркало · {root.get('disk_id') or root['root_id']}")
                title.setStyleSheet("font-weight: 700;")
                current = int(root.get("current_files", 0))
                total = int(root.get("total_files", 0))
                degraded = int(root.get("degraded_files", 0))
                status = "Полная копия" if degraded == 0 else f"Требует восстановления: {degraded}"
                detail = QLabel(
                    f"{status} · актуально {current}/{total} · "
                    f"{format_bytes(int(root.get('current_bytes', 0)))}"
                )
                detail.setProperty("muted", True)
                if degraded:
                    detail.setStyleSheet("color: #f5bd4f;")
                box.addWidget(title)
                box.addWidget(detail)
                checked = root.get("last_checked_at")
                if checked:
                    verified = QLabel(f"Последняя проверка: {checked}")
                    verified.setProperty("muted", True)
                    box.addWidget(verified)
                self.mirror_rows.addWidget(card)
        if self.mirror_job_rows is None:
            return
        clear_layout(self.mirror_job_rows)
        if not jobs:
            self._add_muted(self.mirror_job_rows, "Проверок зеркала пока не запускали.")
            return
        status_labels = {
            "queued": "В очереди",
            "running": "Проверяется",
            "completed": "Зеркало исправно",
            "failed": "Остались ошибки",
            "cancelled": "Остановлено",
        }
        for job in jobs:
            card = QFrame()
            card.setProperty("card", True)
            box = QVBoxLayout(card)
            title = QLabel(f"Проверка · {job['target_root_id']}")
            title.setStyleSheet("font-weight: 700;")
            detail = QLabel(
                f"{status_labels.get(job['status'], job['status'])} · "
                f"обработано {job.get('processed_files', 0)}/{job.get('total_files', 0)} · "
                f"исправлено {job.get('repaired_files', 0)} · ошибок {job.get('failed_files', 0)}"
            )
            detail.setProperty("muted", True)
            box.addWidget(title)
            box.addWidget(detail)
            if job.get("error"):
                error = QLabel(str(job["error"]))
                error.setWordWrap(True)
                error.setStyleSheet("color: #e2383f;")
                box.addWidget(error)
            controls = QHBoxLayout()
            if job["status"] in {"failed", "cancelled"}:
                resume = QPushButton("Проверить снова")
                resume.clicked.connect(
                    lambda _checked=False, job_id=job["id"]: self.mirror_resume_requested.emit(
                        job_id
                    )
                )
                controls.addWidget(resume)
            if job["status"] in {"queued", "running"}:
                cancel = QPushButton("Остановить")
                cancel.clicked.connect(
                    lambda _checked=False, job_id=job["id"]: self.mirror_cancel_requested.emit(
                        job_id
                    )
                )
                controls.addWidget(cancel)
            controls.addStretch()
            box.addLayout(controls)
            self.mirror_job_rows.addWidget(card)

    def _update_automation(self, automation: dict, online: bool) -> None:
        scheduler = automation.get("scheduler") or {}
        if self.automation_scheduler_enabled is not None:
            self.automation_scheduler_enabled.blockSignals(True)
            self.automation_scheduler_enabled.setChecked(
                bool(scheduler.get("enabled", True))
            )
            self.automation_scheduler_enabled.setEnabled(online)
            self.automation_scheduler_enabled.blockSignals(False)
        if self.automation_scheduler_interval is not None:
            self.automation_scheduler_interval.blockSignals(True)
            self.automation_scheduler_interval.setValue(
                int(scheduler.get("interval_seconds", 60))
            )
            self.automation_scheduler_interval.setEnabled(online)
            self.automation_scheduler_interval.blockSignals(False)
        if self.automation_scheduler_status is not None:
            if not online:
                status = "Core выключен — правила сохранены, но не выполняются."
            elif not scheduler.get("enabled", True):
                status = "Автоматическое выполнение приостановлено администратором."
            elif scheduler.get("running"):
                status = (
                    "Планировщик работает · следующая проверка не позднее чем через "
                    f"{int(scheduler.get('interval_seconds', 60))} сек."
                )
            else:
                status = "Планировщик запускается вместе с Core."
            self.automation_scheduler_status.setText(status)
        if self.automation_create_button is not None:
            self.automation_create_button.setEnabled(online)
        if self.automation_evaluate_button is not None:
            self.automation_evaluate_button.setEnabled(online)
        if self.automation_preview_button is not None:
            self.automation_preview_button.setEnabled(online)
        if self.automation_settings_button is not None:
            self.automation_settings_button.setEnabled(online)
        if self.automation_rule_rows is not None:
            clear_layout(self.automation_rule_rows)
            rules = list(automation.get("rules") or [])
            if not online:
                self._add_muted(
                    self.automation_rule_rows,
                    "Запустите Core для управления постоянными правилами.",
                )
            elif not rules:
                self._add_muted(self.automation_rule_rows, "Правил автоматизации пока нет.")
            for rule in rules:
                card = QFrame()
                card.setProperty("card", True)
                box = QVBoxLayout(card)
                state = "Включено" if rule.get("enabled") else "Выключено"
                title = QLabel(
                    f"{rule.get('name', 'Правило')} · {state}"
                    + (" · системное" if rule.get("system_rule") else "")
                )
                title.setStyleSheet("font-weight: 700;")
                detail = QLabel(
                    f"Если: {AUTOMATION_TRIGGER_LABELS.get(rule.get('trigger_type'), rule.get('trigger_type'))} · "
                    f"то: {AUTOMATION_ACTION_LABELS.get(rule.get('action_type'), rule.get('action_type'))} · "
                    f"не чаще {max(1, int(rule.get('cooldown_seconds', 60)) // 60)} мин"
                )
                detail.setWordWrap(True)
                detail.setProperty("muted", True)
                box.addWidget(title)
                box.addWidget(detail)
                if rule.get("last_triggered_at"):
                    last = QLabel(f"Последний запуск: {rule['last_triggered_at']}")
                    last.setProperty("muted", True)
                    box.addWidget(last)
                controls = QHBoxLayout()
                preview = QPushButton("Проверить условие")
                preview.clicked.connect(
                    lambda _checked=False, rule_id=str(rule["id"]): (
                        self.automation_rule_preview_requested.emit(rule_id)
                    )
                )
                controls.addWidget(preview)
                toggle = QPushButton("Выключить" if rule.get("enabled") else "Включить")
                update_payload = {
                    "name": str(rule.get("name", "Правило")),
                    "enabled": not bool(rule.get("enabled")),
                    "trigger_type": str(rule.get("trigger_type")),
                    "action_type": str(rule.get("action_type")),
                    "cooldown_minutes": max(
                        1, int(rule.get("cooldown_seconds", 60)) // 60
                    ),
                }
                toggle.clicked.connect(
                    lambda _checked=False, rule_id=str(rule["id"]), values=update_payload: (
                        self.automation_rule_update_requested.emit(rule_id, values)
                    )
                )
                controls.addWidget(toggle)
                if not rule.get("system_rule"):
                    remove = QPushButton("Удалить")
                    remove.clicked.connect(
                        lambda _checked=False, rule_id=str(rule["id"]): (
                            self.automation_rule_delete_requested.emit(rule_id)
                        )
                    )
                    controls.addWidget(remove)
                controls.addStretch()
                box.addLayout(controls)
                self.automation_rule_rows.addWidget(card)
        if self.automation_run_rows is None:
            return
        clear_layout(self.automation_run_rows)
        runs = list(automation.get("runs") or [])[:12]
        if not runs:
            self._add_muted(self.automation_run_rows, "История запусков пока пуста.")
            return
        status_labels = {
            "completed": "выполнено",
            "failed": "ошибка",
            "skipped": "пропущено безопасно",
        }
        for run in runs:
            label = QLabel(
                f"{run.get('created_at', '—')} · {run.get('rule_name', 'Правило')} · "
                f"{status_labels.get(run.get('status'), run.get('status'))} · "
                f"{format_automation_result(run.get('action_result')) or run.get('error') or run.get('condition_summary', '')}"
            )
            label.setWordWrap(True)
            label.setProperty("muted", True)
            self.automation_run_rows.addWidget(label)

    def _update_notifications(
        self,
        notifications: list[dict],
        integrations: dict,
        online: bool,
    ) -> None:
        if self.integration_rows is not None:
            clear_layout(self.integration_rows)
            providers = [
                item
                for item in (integrations.get("plugins") or [])
                if item.get("kind") == "notification"
            ]
            if not online:
                self._add_muted(self.integration_rows, "Запустите Core для проверки интеграций.")
            for provider in providers:
                card = QFrame()
                card.setProperty("card", True)
                row = QHBoxLayout(card)
                text = QVBoxLayout()
                title = QLabel(str(provider.get("name", provider.get("id"))))
                title.setStyleSheet("font-weight: 700;")
                detail = QLabel(
                    "Встроенный · без внешней сети · без загрузки стороннего кода"
                )
                detail.setProperty("muted", True)
                text.addWidget(title)
                text.addWidget(detail)
                row.addLayout(text, 1)
                test = QPushButton("Проверить")
                test.setEnabled(online)
                test.clicked.connect(
                    lambda _checked=False, provider_id=str(provider["id"]): (
                        self.integration_test_requested.emit(provider_id)
                    )
                )
                row.addWidget(test)
                self.integration_rows.addWidget(card)
        if self.notification_rows is None:
            return
        clear_layout(self.notification_rows)
        if not online:
            self._add_muted(self.notification_rows, "Core выключен.")
            return
        if not notifications:
            self._add_muted(self.notification_rows, "Новых уведомлений нет.")
            return
        for notification in notifications:
            card = QFrame()
            card.setProperty("card", True)
            box = QVBoxLayout(card)
            severity = str(notification.get("severity", "info"))
            title = QLabel(str(notification.get("title", "Уведомление")))
            title.setStyleSheet(
                "font-weight: 700; color: "
                + (
                    "#e2383f;"
                    if severity == "critical"
                    else "#f5bd4f;"
                    if severity == "warning"
                    else "#7ab7ff;"
                )
            )
            message = QLabel(str(notification.get("message", "")))
            message.setWordWrap(True)
            source = QLabel(
                f"{notification.get('created_at', '—')} · {notification.get('source', 'Core')}"
            )
            source.setProperty("muted", True)
            box.addWidget(title)
            box.addWidget(message)
            box.addWidget(source)
            acknowledge = QPushButton("Прочитано")
            acknowledge.clicked.connect(
                lambda _checked=False, notification_id=str(notification["id"]): (
                    self.notification_acknowledge_requested.emit(notification_id)
                )
            )
            box.addWidget(acknowledge)
            self.notification_rows.addWidget(card)

    def _update_diagnostics(self, diagnostics: dict, online: bool) -> None:
        latest = diagnostics.get("latest_scan") or {}
        scan_running = latest.get("status") in {"queued", "running"}
        if self.diagnostics_quick_button is not None:
            self.diagnostics_quick_button.setEnabled(online and not scan_running)
        if self.diagnostics_full_button is not None:
            self.diagnostics_full_button.setEnabled(online and not scan_running)
        if self.support_bundle_button is not None:
            self.support_bundle_button.setEnabled(online)
        if self.diagnostics_status_label is not None:
            status_value = diagnostics.get("status") if online else "offline"
            labels = {
                "healthy": "Проблем не обнаружено",
                "warning": "Требуется внимание",
                "critical": "Критическая проблема",
                "offline": "Core выключен",
            }
            colors = {
                "healthy": "#43c778",
                "warning": "#f5bd4f",
                "critical": "#e2383f",
                "offline": "#949ca8",
            }
            self.diagnostics_status_label.setText(labels.get(status_value, "Ожидает проверки"))
            self.diagnostics_status_label.setStyleSheet(
                f"color: {colors.get(status_value, '#949ca8')}; font-weight: 700;"
            )
        if self.diagnostics_detail_label is not None:
            if not online:
                detail = "Автоматический мониторинг работает внутри Server Core."
            elif latest:
                scan_names = {"quick": "Быстрая", "full": "Полная"}
                status_names = {
                    "queued": "в очереди",
                    "running": "выполняется",
                    "completed": "завершена",
                    "failed": "ошибка",
                }
                detail = (
                    f"{scan_names.get(latest.get('kind'), latest.get('kind'))} проверка · "
                    f"{status_names.get(latest.get('status'), latest.get('status'))} · "
                    f"объектов {latest.get('checked_objects', 0)} · "
                    f"{format_bytes(int(latest.get('checked_bytes', 0)))}"
                )
                if latest.get("error"):
                    detail += f"\n{latest['error']}"
            else:
                detail = "Core выполнит быструю проверку после запуска."
            self.diagnostics_detail_label.setText(detail)
        if self.diagnostics_rows is None:
            return
        clear_layout(self.diagnostics_rows)
        incidents = list(diagnostics.get("incidents") or [])
        if not online:
            self._add_muted(self.diagnostics_rows, "Запустите Core для просмотра инцидентов.")
            return
        if not incidents:
            self._add_muted(
                self.diagnostics_rows,
                "Активных инцидентов нет. Результаты основаны только на выполненных проверках.",
            )
            return
        for incident in incidents:
            card = QFrame()
            card.setProperty("card", True)
            box = QVBoxLayout(card)
            title = QLabel(str(incident.get("summary", "Инцидент")))
            title.setStyleSheet(
                "font-weight: 700; color: "
                + ("#e2383f;" if incident.get("severity") == "critical" else "#f5bd4f;")
            )
            component = QLabel(
                f"{incident.get('component', 'Core')} · обнаружено "
                f"{incident.get('last_seen_at', '—')} · повторов {incident.get('occurrences', 1)}"
            )
            component.setProperty("muted", True)
            detail = QLabel(str(incident.get("detail", "")))
            detail.setWordWrap(True)
            detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            remediation = QLabel(f"Что делать: {incident.get('remediation', 'Проверьте журнал Core.')}")
            remediation.setWordWrap(True)
            remediation.setStyleSheet("color: #7ab7ff;")
            box.addWidget(title)
            box.addWidget(component)
            box.addWidget(detail)
            box.addWidget(remediation)
            controls = QHBoxLayout()
            recheck = QPushButton("Перепроверить")
            recheck.clicked.connect(
                lambda _checked=False, incident_id=str(incident["id"]): (
                    self.diagnostic_remediation_requested.emit(incident_id, "recheck")
                )
            )
            controls.addWidget(recheck)
            if incident.get("check_key") == "uploads.expired":
                cleanup = QPushButton("Очистить просроченные загрузки")
                cleanup.clicked.connect(
                    lambda _checked=False, incident_id=str(incident["id"]): (
                        self.diagnostic_remediation_requested.emit(
                            incident_id, "cleanup_expired_uploads"
                        )
                    )
                )
                controls.addWidget(cleanup)
            if incident.get("severity") == "critical":
                protect = QPushButton("Защитить сервер: только чтение")
                protect.clicked.connect(
                    lambda _checked=False, incident_id=str(incident["id"]): (
                        self.diagnostic_remediation_requested.emit(
                            incident_id, "enter_read_only"
                        )
                    )
                )
                controls.addWidget(protect)
            controls.addStretch()
            box.addLayout(controls)
            self.diagnostics_rows.addWidget(card)

    def _update_maintenance(
        self,
        roots: list[dict],
        jobs: list[dict],
        online: bool,
    ) -> None:
        if self.migration_source is not None and self.migration_target is not None:
            previous_source = self.migration_source.currentData()
            previous_target = self.migration_target.currentData()
            self.migration_source.clear()
            self.migration_target.clear()
            for root in roots:
                label = f"{root.get('disk_id') or root['id']} · {root['path']}"
                if not root.get("write_enabled", True):
                    self.migration_source.addItem(label, root["id"])
                if root.get("write_enabled", True):
                    self.migration_target.addItem(label, root["id"])
            source_index = self.migration_source.findData(previous_source)
            target_index = self.migration_target.findData(previous_target)
            if source_index >= 0:
                self.migration_source.setCurrentIndex(source_index)
            if target_index >= 0:
                self.migration_target.setCurrentIndex(target_index)
            if self.migration_start_button is not None:
                self.migration_start_button.setEnabled(
                    online
                    and self.migration_source.count() > 0
                    and self.migration_target.count() > 0
                )
        if self.maintenance_rows is None:
            return
        clear_layout(self.maintenance_rows)
        if not online:
            self._add_muted(self.maintenance_rows, "Запустите Core для заданий обслуживания.")
            return
        if not jobs:
            self._add_muted(self.maintenance_rows, "Заданий переноса пока нет.")
            return
        status_labels = {
            "queued": "В очереди",
            "running": "Выполняется",
            "completed": "Завершено",
            "failed": "Ошибка",
            "cancelled": "Отменено",
        }
        for job in jobs:
            card = QFrame()
            card.setProperty("card", True)
            card_layout = QVBoxLayout(card)
            title = QLabel(f"Перенос {job['source_root_id']} → {job['target_root_id']}")
            title.setStyleSheet("font-weight: 700;")
            total = int(job.get("total_bytes", 0))
            processed = int(job.get("processed_bytes", 0))
            detail = QLabel(
                f"{status_labels.get(job['status'], job['status'])} · "
                f"{format_bytes(processed)} из {format_bytes(total)} · "
                f"объектов {job.get('processed_files', 0)}/{job.get('total_files', 0)}"
            )
            detail.setProperty("muted", True)
            progress = QProgressBar()
            progress.setRange(0, 100)
            progress.setValue(
                round(processed / total * 100)
                if total
                else (100 if job["status"] == "completed" else 0)
            )
            card_layout.addWidget(title)
            card_layout.addWidget(detail)
            card_layout.addWidget(progress)
            if job.get("error"):
                error = QLabel(str(job["error"]))
                error.setWordWrap(True)
                error.setStyleSheet("color: #e2383f;")
                card_layout.addWidget(error)
            controls = QHBoxLayout()
            if job["status"] in {"failed", "cancelled"}:
                resume = QPushButton("Продолжить")
                resume.clicked.connect(
                    lambda _checked=False, job_id=job["id"]: (
                        self.maintenance_resume_requested.emit(job_id)
                    )
                )
                controls.addWidget(resume)
            if job["status"] in {"queued", "running"}:
                cancel = QPushButton("Отменить")
                cancel.clicked.connect(
                    lambda _checked=False, job_id=job["id"]: (
                        self.maintenance_cancel_requested.emit(job_id)
                    )
                )
                controls.addWidget(cancel)
            controls.addStretch()
            card_layout.addLayout(controls)
            self.maintenance_rows.addWidget(card)

    def load_settings(self, settings: AppSettings, config_path: str) -> None:
        self._loading_settings = True
        self._current_settings = settings
        self.server_name.setText(settings.server_name)
        self.notifications.setChecked(settings.notifications_enabled)
        self.refresh_interval.setValue(settings.refresh_interval_seconds)
        self.lan_enabled.setChecked(settings.lan_enabled)
        self.lan_port.setValue(settings.lan_port)
        self.remote_enabled.setChecked(settings.remote_enabled)
        self.remote_port.setValue(settings.remote_port)
        self.remote_public_url.setText(settings.remote_public_url)
        self.remote_pairing_enabled.setChecked(settings.remote_pairing_enabled)
        self.zrok_enabled.setChecked(settings.zrok_enabled)
        self.zrok_port.setValue(settings.zrok_port)
        self.zrok_executable.setText(settings.zrok_executable)
        self.zrok_share_name.setText(settings.zrok_share_name)
        self.config_path.setText(config_path)
        if self.log_path is not None:
            self.log_path.setText(str(Path(config_path).parent / "audit.jsonl"))
        if self.server_state_label is not None:
            self.server_state_label.setText(
                "Настройка завершена" if settings.setup_complete else "Ожидает настройки"
            )
        self.mode.blockSignals(True)
        self.mode.setCurrentIndex(1 if settings.advanced_mode else 0)
        self.mode.blockSignals(False)
        self._rebuild_sections()
        self._loading_settings = False
        self._saved_settings_payload = self._settings_payload()
        self.save_button.setVisible(False)

    def _mode_changed(self) -> None:
        self._rebuild_sections()
        self.advanced_mode_changed.emit(bool(self.mode.currentData()))

    def _rebuild_sections(self) -> None:
        previous = (
            self.section_list.currentItem().text() if self.section_list.currentItem() else "Сервер"
        )
        advanced = bool(self.mode.currentData())
        self.section_list.blockSignals(True)
        self.section_list.clear()
        while self.stack.count():
            widget = self.stack.widget(0)
            self.stack.removeWidget(widget)
        selected_row = 0
        for name, advanced_only in self._SECTIONS:
            if advanced_only and not advanced:
                continue
            self.section_list.addItem(QListWidgetItem(name))
            self.stack.addWidget(self._pages_by_name[name])
            if name == previous:
                selected_row = self.section_list.count() - 1
        self.section_list.blockSignals(False)
        self.section_list.setCurrentRow(selected_row)
        self.stack.setCurrentIndex(selected_row)

    def _make_section(self, name: str) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 4, 4)
        title = QLabel(name)
        title.setObjectName("PageTitle")
        layout.addWidget(title)
        if name in {"Система", "Docker", "SSH"}:
            routes = {"Система": "system", "Docker": "docker", "SSH": "ssh"}
            descriptions = {
                "Система": (
                    "Профили сервера, питание, сеть, отчёты и автоматизации собраны в системном "
                    "центре. Его вкладки остаются доступными даже при выключенном Core."
                ),
                "Docker": (
                    "Автонастройка Docker Desktop включает проверку платформы, загрузку с "
                    "официального источника и фоновую установку для контейнеров скриптов и ботов."
                ),
                "SSH": (
                    "Автонастройка SSH устанавливает OpenSSH Server, оставляет вход только по "
                    "публичному ключу и открывает выбранный порт лишь для частной сети Windows."
                ),
            }
            description = QLabel(descriptions[name])
            description.setWordWrap(True)
            description.setProperty("emptyState", True)
            layout.addWidget(description)
            open_button = QPushButton(f"Открыть вкладку «{name}»")
            open_button.setProperty("primary", True)
            open_button.clicked.connect(
                lambda _checked=False, route=routes[name]: self.system_section_requested.emit(route)
            )
            layout.addWidget(open_button)
        elif name == "Сервер":
            form = QFormLayout()
            form.setVerticalSpacing(14)
            form.addRow("Название сервера", self.server_name)
            self.server_state_label = QLabel(
                "Настройка завершена"
                if self._current_settings.setup_complete
                else "Ожидает настройки"
            )
            form.addRow("Состояние", self.server_state_label)
            self.core_status_label = QLabel("Выключено")
            form.addRow("Серверное ядро", self.core_status_label)
            layout.addLayout(form)
            self.core_details_label = QLabel(
                "Ядро запускается отдельным процессом и продолжает работу после закрытия менеджера."
            )
            self.core_details_label.setProperty("muted", True)
            self.core_details_label.setWordWrap(True)
            layout.addWidget(self.core_details_label)
            controls = QHBoxLayout()
            self.core_start_button = QPushButton("Запустить ядро")
            self.core_start_button.setProperty("primary", True)
            self.core_start_button.clicked.connect(self.core_start_requested)
            self.core_stop_button = QPushButton("Остановить ядро")
            self.core_stop_button.setEnabled(False)
            self.core_stop_button.clicked.connect(self.core_stop_requested)
            refresh_core = QPushButton("Обновить состояние")
            refresh_core.clicked.connect(self.core_refresh_requested)
            controls.addWidget(self.core_start_button)
            controls.addWidget(self.core_stop_button)
            controls.addWidget(refresh_core)
            controls.addStretch()
            layout.addLayout(controls)
            mode_card = QFrame()
            mode_card.setProperty("card", True)
            mode_box = QVBoxLayout(mode_card)
            self.server_mode_label = QLabel("Обычный режим")
            self.server_mode_label.setStyleSheet("color: #43c778; font-weight: 700;")
            self.server_mode_detail = QLabel(
                "Загрузка, удаление, подключение устройств и фоновые задания разрешены."
            )
            self.server_mode_detail.setWordWrap(True)
            self.server_mode_detail.setProperty("muted", True)
            mode_controls = QHBoxLayout()
            self.read_only_button = QPushButton("Включить аварийный режим")
            self.read_only_button.clicked.connect(self.server_read_only_requested)
            self.normal_mode_button = QPushButton("Вернуть обычный режим")
            self.normal_mode_button.setProperty("primary", True)
            self.normal_mode_button.setEnabled(False)
            self.normal_mode_button.clicked.connect(self.server_normal_requested)
            mode_controls.addWidget(self.read_only_button)
            mode_controls.addWidget(self.normal_mode_button)
            mode_controls.addStretch()
            mode_box.addWidget(self.server_mode_label)
            mode_box.addWidget(self.server_mode_detail)
            mode_box.addLayout(mode_controls)
            layout.addWidget(mode_card)
        elif name == "Подключение":
            self.connection_panel = ConnectionCodePanel()
            self.connection_panel.generation_requested.connect(
                self.connection_generation_requested
            )
            self.connection_panel.create_user_requested.connect(self.add_user_requested)
            self.connection_panel.refresh_button.clicked.connect(self.core_refresh_requested)
            layout.addWidget(self.connection_panel)
        elif name == "Пользователи":
            top = QHBoxLayout()
            description = QLabel(
                "Личные пространства, постоянные логины и пароли, квоты и резервные одноразовые приглашения."
            )
            description.setProperty("muted", True)
            top.addWidget(description, 1)
            add = QPushButton("Добавить пользователя")
            add.setProperty("primary", True)
            add.clicked.connect(self.add_user_requested)
            top.addWidget(add)
            layout.addLayout(top)
            self.users_rows = QVBoxLayout()
            layout.addLayout(self.users_rows)
        elif name == "Доверенные устройства":
            description = QLabel(
                "Каждое устройство имеет отдельный отзывный токен. Отзыв действует сразу."
            )
            description.setProperty("muted", True)
            description.setWordWrap(True)
            layout.addWidget(description)
            self.trusted_device_rows = QVBoxLayout()
            layout.addLayout(self.trusted_device_rows)
        elif name == "Подключение устройств":
            description = QLabel(
                "Новые компьютеры и телефоны после входа по логину или коду остаются заблокированными до подтверждения."
            )
            description.setProperty("muted", True)
            description.setWordWrap(True)
            layout.addWidget(description)
            self.pending_device_rows = QVBoxLayout()
            layout.addLayout(self.pending_device_rows)
        elif name == "Уведомления":
            layout.addWidget(self.notifications)
            description = QLabel(
                "Core хранит важные сообщения в постоянном центре уведомлений и доставляет их "
                "через безопасные встроенные провайдеры. Они не используют интернет и не загружают "
                "сторонний код."
            )
            description.setWordWrap(True)
            description.setProperty("muted", True)
            layout.addWidget(description)
            provider_title = QLabel("Встроенные провайдеры")
            provider_title.setStyleSheet("font-weight: 700; margin-top: 8px;")
            layout.addWidget(provider_title)
            self.integration_rows = QVBoxLayout()
            layout.addLayout(self.integration_rows)
            notification_title = QLabel("Новые уведомления")
            notification_title.setStyleSheet("font-weight: 700; margin-top: 8px;")
            layout.addWidget(notification_title)
            self.notification_rows = QVBoxLayout()
            layout.addLayout(self.notification_rows)
        elif name == "Интерфейс":
            form = QFormLayout()
            form.addRow("Обновлять данные каждые", self.refresh_interval)
            layout.addLayout(form)
            note = QLabel(
                "Этот интервал относится к состоянию дисков и Core. Выбранный простой или "
                "расширенный режим сохраняется сразу и не сбрасывается после обновления данных."
            )
            note.setWordWrap(True)
            note.setProperty("emptyState", True)
            layout.addWidget(note)
        elif name == "Сеть":
            self.lan_status_label = QLabel("Выключен")
            self.lan_details_label = QLabel(
                "После включения Core создаст постоянный сертификат и отдельный HTTPS-вход для клиентов."
            )
            self.lan_details_label.setProperty("muted", True)
            self.lan_details_label.setWordWrap(True)
            form = QFormLayout()
            form.setVerticalSpacing(14)
            form.addRow("Состояние", self.lan_status_label)
            form.addRow("HTTPS-порт", self.lan_port)
            layout.addWidget(self.lan_enabled)
            layout.addLayout(form)
            layout.addWidget(self.lan_details_label)
            boundary = QFrame()
            boundary.setProperty("accent", "blue")
            boundary_layout = QVBoxLayout(boundary)
            boundary_title = QLabel("Административный API остаётся локальным")
            boundary_title.setStyleSheet("font-weight: 700;")
            boundary_text = QLabel(
                "Через сетевой HTTPS-порт доступны только подключение устройства и файловые функции. "
                "Настройки сервера, пользователи и команды остановки с него скрыты. В Windows при первом "
                "запуске разрешите доступ только для частных сетей."
            )
            boundary_text.setProperty("muted", True)
            boundary_text.setWordWrap(True)
            boundary_layout.addWidget(boundary_title)
            boundary_layout.addWidget(boundary_text)
            layout.addWidget(boundary)
        elif name == "Удалённый доступ":
            warning = QFrame()
            warning.setProperty("accent", "orange")
            warning_layout = QVBoxLayout(warning)
            warning_title = QLabel("Внешний доступ включается только вручную")
            warning_title.setStyleSheet("font-weight: 700; font-size: 16px;")
            warning_text = QLabel(
                "Cloud Storage не открывает порты роутера и не меняет firewall автоматически. "
                "Самый безопасный вариант — VPN. При прямом подключении используйте только HTTPS, "
                "сверяйте отпечаток сертификата и открывайте ровно один клиентский порт."
            )
            warning_text.setWordWrap(True)
            warning_text.setProperty("muted", True)
            warning_layout.addWidget(warning_title)
            warning_layout.addWidget(warning_text)
            layout.addWidget(warning)
            self.remote_status_label = QLabel("Выключен")
            self.remote_details_label = QLabel()
            self.remote_details_label.setWordWrap(True)
            self.remote_details_label.setProperty("muted", True)
            form = QFormLayout()
            form.setVerticalSpacing(14)
            form.addRow("Состояние", self.remote_status_label)
            form.addRow("Внутренний HTTPS-порт", self.remote_port)
            form.addRow("Публичный HTTPS-адрес", self.remote_public_url)
            layout.addWidget(self.remote_enabled)
            layout.addLayout(form)
            layout.addWidget(self.remote_pairing_enabled)
            layout.addWidget(self.remote_details_label)
            pairing_note = QLabel(
                "Подключение по коду выключено отдельно. Включайте его только на время выдачи приглашения; "
                "уже подтверждённые устройства продолжат работать после выключения."
            )
            pairing_note.setWordWrap(True)
            pairing_note.setProperty("muted", True)
            layout.addWidget(pairing_note)
            audit_title = QLabel("Последние внешние обращения")
            audit_title.setStyleSheet("font-weight: 700; margin-top: 8px;")
            layout.addWidget(audit_title)
            self.remote_audit_rows = QVBoxLayout()
            layout.addLayout(self.remote_audit_rows)
            zrok_title = QLabel("zrok · доступ без настройки роутера")
            zrok_title.setStyleSheet("font-weight: 700; font-size: 16px; margin-top: 12px;")
            layout.addWidget(zrok_title)
            zrok_text = QLabel(
                "Core запускает внешний zrok-клиент и публикует только отдельный loopback-шлюз. "
                "Перед включением установите zrok и один раз выполните его штатную команду enable. "
                "Пароль Cloud Storage не передаётся zrok и никогда не попадает в командную строку."
            )
            zrok_text.setWordWrap(True)
            zrok_text.setProperty("muted", True)
            layout.addWidget(zrok_text)
            self.zrok_status_label = QLabel("Выключен")
            self.zrok_details_label = QLabel()
            self.zrok_details_label.setWordWrap(True)
            self.zrok_details_label.setProperty("muted", True)
            zrok_form = QFormLayout()
            zrok_form.setVerticalSpacing(12)
            zrok_form.addRow("Состояние", self.zrok_status_label)
            zrok_form.addRow("Локальный порт шлюза", self.zrok_port)
            zrok_form.addRow("Программа zrok", self.zrok_executable)
            zrok_form.addRow("Reserved share", self.zrok_share_name)
            layout.addWidget(self.zrok_enabled)
            layout.addLayout(zrok_form)
            layout.addWidget(self.zrok_details_label)
            self.zrok_restart_button = QPushButton("Перезапустить zrok без перезапуска Core")
            self.zrok_restart_button.clicked.connect(
                lambda: self.tunnel_restart_requested.emit("zrok")
            )
            layout.addWidget(self.zrok_restart_button)
        elif name == "Диагностика":
            description = QLabel(
                "Core автоматически проверяет SQLite, доступность дисков, безопасный запас места "
                "и ошибки постоянных заданий. Полная проверка дополнительно читает каждый "
                "управляемый объект и пересчитывает SHA-256."
            )
            description.setWordWrap(True)
            description.setProperty("muted", True)
            layout.addWidget(description)
            status_card = QFrame()
            status_card.setProperty("accent", "blue")
            status_box = QVBoxLayout(status_card)
            self.diagnostics_status_label = QLabel("Ожидает проверки")
            self.diagnostics_status_label.setStyleSheet("font-weight: 700; font-size: 17px;")
            self.diagnostics_detail_label = QLabel(
                "Автоматический мониторинг работает внутри Server Core."
            )
            self.diagnostics_detail_label.setWordWrap(True)
            self.diagnostics_detail_label.setProperty("muted", True)
            status_box.addWidget(self.diagnostics_status_label)
            status_box.addWidget(self.diagnostics_detail_label)
            layout.addWidget(status_card)
            controls = QHBoxLayout()
            self.diagnostics_quick_button = QPushButton("Быстрая проверка")
            self.diagnostics_quick_button.setProperty("primary", True)
            self.diagnostics_quick_button.clicked.connect(self.diagnostics_quick_requested)
            self.diagnostics_full_button = QPushButton("Полная проверка SHA-256")
            self.diagnostics_full_button.clicked.connect(self.diagnostics_full_requested)
            refresh = QPushButton("Обновить")
            refresh.clicked.connect(self.core_refresh_requested)
            controls.addWidget(self.diagnostics_quick_button)
            controls.addWidget(self.diagnostics_full_button)
            controls.addWidget(refresh)
            controls.addStretch()
            layout.addLayout(controls)
            support_note = QLabel(
                "Пакет поддержки содержит только обезличенные показатели. База, журналы, "
                "пользовательские файлы, имена, адреса, пути, пароли и токены в него не входят."
            )
            support_note.setWordWrap(True)
            support_note.setProperty("muted", True)
            layout.addWidget(support_note)
            self.support_bundle_button = QPushButton("Сохранить пакет поддержки")
            self.support_bundle_button.clicked.connect(self.support_bundle_requested)
            layout.addWidget(self.support_bundle_button)
            incidents_title = QLabel("Активные инциденты")
            incidents_title.setStyleSheet("font-weight: 700; margin-top: 8px;")
            layout.addWidget(incidents_title)
            self.diagnostics_rows = QVBoxLayout()
            layout.addLayout(self.diagnostics_rows)
            info = QLabel("Файл настроек Manager")
            info.setProperty("muted", True)
            layout.addWidget(info)
            layout.addWidget(self.config_path)
        elif name == "Безопасность":
            self.security_overall_label = QLabel("Проверка ещё не запускалась")
            self.security_overall_label.setWordWrap(True)
            self.security_overall_label.setStyleSheet("font-size: 17px; font-weight: 700;")
            layout.addWidget(self.security_overall_label)
            tabs = QTabWidget()
            for title, attribute in (
                ("Безопасность в сети", "security_network_label"),
                ("Безопасность файлов", "security_files_label"),
                ("Безопасность подключений", "security_connection_label"),
            ):
                tab = QWidget()
                tab_layout = QVBoxLayout(tab)
                status_label = QLabel("Ядро не опрошено")
                status_label.setWordWrap(True)
                status_label.setTextInteractionFlags(
                    Qt.TextInteractionFlag.TextSelectableByMouse
                )
                setattr(self, attribute, status_label)
                tab_layout.addWidget(status_label)
                if attribute == "security_network_label":
                    configure = QPushButton("Открыть сетевые настройки")
                    configure.clicked.connect(lambda: self.show_section("Сеть"))
                    tab_layout.addWidget(configure)
                elif attribute == "security_files_label":
                    controls = QHBoxLayout()
                    read_only_button = QPushButton("Аварийный режим «только чтение»")
                    read_only_button.clicked.connect(self.server_read_only_requested)
                    normal_button = QPushButton("Вернуть обычный режим")
                    normal_button.clicked.connect(self.server_normal_requested)
                    disk_security_button = QPushButton("Шифрование и режимы дисков")
                    disk_security_button.clicked.connect(self.disks_requested)
                    controls.addWidget(read_only_button)
                    controls.addWidget(normal_button)
                    controls.addWidget(disk_security_button)
                    tab_layout.addLayout(controls)
                else:
                    devices_button = QPushButton("Открыть доверенные устройства")
                    devices_button.clicked.connect(
                        lambda: self.show_section("Доверенные устройства")
                    )
                    tab_layout.addWidget(devices_button)
                    invitation_button = QPushButton("Создать безопасное подключение")
                    invitation_button.clicked.connect(lambda: self.show_section("Подключение"))
                    tab_layout.addWidget(invitation_button)
                tab_layout.addStretch()
                tabs.addTab(tab, title)
            layout.addWidget(tabs)
            scan_controls = QHBoxLayout()
            quick = QPushButton("Быстрая проверка")
            quick.clicked.connect(self.diagnostics_quick_requested)
            full = QPushButton("Полная проверка защиты")
            full.setProperty("primary", True)
            full.clicked.connect(self.diagnostics_full_requested)
            scan_controls.addWidget(quick)
            scan_controls.addWidget(full)
            scan_controls.addStretch()
            layout.addLayout(scan_controls)
        elif name == "Хранилище":
            self.storage_status_label = QLabel("Ядро ещё не опрошено")
            self.storage_status_label.setWordWrap(True)
            self.storage_status_label.setProperty("emptyState", True)
            layout.addWidget(self.storage_status_label)
            body = QLabel(
                "Порог заполнения, минимальный резерв и приоритет записи задаются отдельно "
                "для каждого диска на его подробной странице."
            )
            body.setWordWrap(True)
            layout.addWidget(body)
            guide = QLabel(
                "Как это работает:\n"
                "1. Основные диски принимают новые файлы по приоритету и свободному месту.\n"
                "2. Зеркала получают проверенную копию каждой версии файла.\n"
                "3. Диски резервных копий хранят независимые снимки и не участвуют в обычной записи."
            )
            guide.setWordWrap(True)
            guide.setProperty("emptyState", True)
            layout.addWidget(guide)
            storage_controls = QHBoxLayout()
            open_disks = QPushButton("Настроить диски и роли")
            open_disks.setProperty("primary", True)
            open_disks.clicked.connect(self.disks_requested)
            open_backups = QPushButton("Открыть резервные копии")
            open_backups.clicked.connect(lambda: self.show_section("Резервные копии"))
            storage_controls.addWidget(open_disks)
            storage_controls.addWidget(open_backups)
            storage_controls.addStretch()
            layout.addLayout(storage_controls)
        elif name == "Диски":
            self.disk_settings_status_label = QLabel("Данные дисков ещё не загружены")
            self.disk_settings_status_label.setWordWrap(True)
            self.disk_settings_status_label.setProperty("emptyState", True)
            layout.addWidget(self.disk_settings_status_label)
            body = QLabel(
                "Менеджер читает реальные сведения Windows и не форматирует накопители. "
                "Назначение, порог заполнения и приоритет задаются во вкладке «Диски»: "
                "нажмите карточку нужного накопителя, чтобы открыть подробные настройки."
            )
            body.setWordWrap(True)
            layout.addWidget(body)
            note = QLabel(
                "Если диск не отображается: убедитесь, что Windows назначила ему букву, затем "
                "нажмите «Обновить» во вкладке «Диски». Недоступный диск не удаляется из конфигурации."
            )
            note.setWordWrap(True)
            note.setProperty("emptyState", True)
            layout.addWidget(note)
            disk_controls = QHBoxLayout()
            open_disks = QPushButton("Открыть диски")
            open_disks.setProperty("primary", True)
            open_disks.clicked.connect(self.disks_requested)
            refresh_disks = QPushButton("Повторить обнаружение")
            refresh_disks.clicked.connect(self.refresh_requested)
            disk_controls.addWidget(open_disks)
            disk_controls.addWidget(refresh_disks)
            disk_controls.addStretch()
            layout.addLayout(disk_controls)
        elif name == "Права доступа":
            self.access_status_label = QLabel("Ядро ещё не опрошено")
            self.access_status_label.setWordWrap(True)
            self.access_status_label.setProperty("emptyState", True)
            layout.addWidget(self.access_status_label)
            description = QLabel(
                "Права применяются серверным ядром, а не интерфейсом клиента. Пользователь не "
                "может увидеть чужое личное пространство или административные функции."
            )
            description.setWordWrap(True)
            layout.addWidget(description)
            matrix = QLabel(
                "Обычный пользователь\n"
                "• читает, загружает и удаляет только собственные файлы;\n"
                "• подключает только подтверждённые устройства;\n"
                "• не видит диски, журналы, других пользователей и резервные копии.\n\n"
                "Администратор\n"
                "• управляет пользователями, квотами и устройствами через локальный Manager;\n"
                "• запускает диагностику, резервирование и обслуживание;\n"
                "• не получает пароль пользователя — пароли хранятся как стойкие хэши."
            )
            matrix.setWordWrap(True)
            matrix.setProperty("emptyState", True)
            layout.addWidget(matrix)
            access_controls = QHBoxLayout()
            open_users = QPushButton("Управлять пользователями")
            open_users.setProperty("primary", True)
            open_users.clicked.connect(lambda: self.show_section("Пользователи"))
            open_devices = QPushButton("Управлять устройствами")
            open_devices.clicked.connect(lambda: self.show_section("Доверенные устройства"))
            access_controls.addWidget(open_users)
            access_controls.addWidget(open_devices)
            access_controls.addStretch()
            layout.addLayout(access_controls)
        elif name == "Журналы":
            description = QLabel(
                "Здесь показываются последние административные и защитные события Core. "
                "Содержимое пользовательских файлов, пароли и токены в журнал не записываются."
            )
            description.setWordWrap(True)
            layout.addWidget(description)
            self.log_path = QLabel("—")
            self.log_path.setWordWrap(True)
            self.log_path.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            path_form = QFormLayout()
            path_form.addRow("Локальный журнал Manager", self.log_path)
            layout.addLayout(path_form)
            self.log_rows = QVBoxLayout()
            layout.addLayout(self.log_rows)
        elif name == "Обновления":
            description = QLabel(
                "Центр обновлений позволяет выбрать ручной или автоматический режим, стабильный "
                "или тестовый канал и конкретную версию для скачивания."
            )
            description.setWordWrap(True)
            layout.addWidget(description)
            safety = QLabel(
                "Перед установкой проверяются подпись Ed25519 каталога, размер и SHA-256 файла. "
                "Настройки и пользовательские данные установщик не удаляет."
            )
            safety.setWordWrap(True)
            safety.setProperty("emptyState", True)
            layout.addWidget(safety)
            open_updates = QPushButton("Открыть центр обновлений")
            open_updates.setProperty("primary", True)
            open_updates.clicked.connect(self.open_updates_requested)
            layout.addWidget(open_updates)
        elif name == "Автоматизация":
            description = QLabel(
                "Правила работают внутри Core и переживают закрытие Manager и перезапуск Windows. "
                "Доступны события диагностики, дисков, копий, устройств и расписание. Действия "
                "ограничены безопасным встроенным списком: произвольные команды и скрипты запрещены."
            )
            description.setWordWrap(True)
            description.setProperty("muted", True)
            layout.addWidget(description)
            scheduler = QFrame()
            scheduler.setProperty("card", True)
            scheduler_box = QVBoxLayout(scheduler)
            scheduler_title = QLabel("Движок автоматизации")
            scheduler_title.setObjectName("SectionTitle")
            self.automation_scheduler_enabled = QCheckBox(
                "Автоматически проверять и выполнять включённые правила"
            )
            scheduler_form = QFormLayout()
            self.automation_scheduler_interval = QSpinBox()
            self.automation_scheduler_interval.setRange(10, 3600)
            self.automation_scheduler_interval.setValue(60)
            self.automation_scheduler_interval.setSuffix(" сек")
            scheduler_form.addRow("Проверять каждые", self.automation_scheduler_interval)
            self.automation_scheduler_status = QLabel("Состояние Core пока неизвестно")
            self.automation_scheduler_status.setWordWrap(True)
            self.automation_scheduler_status.setProperty("muted", True)
            self.automation_settings_button = QPushButton("Сохранить режим выполнения")
            self.automation_settings_button.clicked.connect(
                self._emit_automation_settings
            )
            scheduler_box.addWidget(scheduler_title)
            scheduler_box.addWidget(self.automation_scheduler_enabled)
            scheduler_box.addLayout(scheduler_form)
            scheduler_box.addWidget(self.automation_scheduler_status)
            scheduler_box.addWidget(self.automation_settings_button)
            layout.addWidget(scheduler)
            rule_form = QFormLayout()
            self.automation_rule_name = QLineEdit()
            self.automation_rule_name.setMaxLength(120)
            self.automation_rule_name.setPlaceholderText("Например: защита при критической ошибке")
            rule_form.addRow("Название правила", self.automation_rule_name)
            self.automation_trigger = QComboBox()
            for label, value in (
                ("Предупреждение диагностики", "diagnostic_warning"),
                ("Критический инцидент", "diagnostic_critical"),
                ("Нехватка места", "storage_low"),
                ("Ошибка резервной копии", "backup_failed"),
                ("Ошибка восстановления", "restore_failed"),
                ("Зеркало требует восстановления", "mirror_degraded"),
                ("Ошибка обслуживания диска", "maintenance_failed"),
                ("Новое устройство ожидает подтверждения", "pending_device"),
                ("Сбой интернет-шлюза", "tunnel_offline"),
                ("По расписанию", "scheduled"),
            ):
                self.automation_trigger.addItem(label, value)
            self.automation_trigger.currentIndexChanged.connect(
                self._automation_trigger_changed
            )
            rule_form.addRow("Если", self.automation_trigger)
            self.automation_action = QComboBox()
            rule_form.addRow("То", self.automation_action)
            self.automation_cooldown = QSpinBox()
            self.automation_cooldown.setRange(1, 10080)
            self.automation_cooldown.setValue(60)
            self.automation_cooldown.setSuffix(" мин")
            rule_form.addRow("Не чаще", self.automation_cooldown)
            layout.addLayout(rule_form)
            self.automation_rule_hint = QLabel()
            self.automation_rule_hint.setWordWrap(True)
            self.automation_rule_hint.setProperty("emptyState", True)
            layout.addWidget(self.automation_rule_hint)
            self._automation_trigger_changed()
            rule_controls = QHBoxLayout()
            self.automation_create_button = QPushButton("Добавить постоянное правило")
            self.automation_create_button.setProperty("primary", True)
            self.automation_create_button.clicked.connect(self._emit_automation_rule)
            self.automation_preview_button = QPushButton("Проверить без запуска")
            self.automation_preview_button.clicked.connect(
                self.automation_preview_requested
            )
            self.automation_evaluate_button = QPushButton("Выполнить совпавшие сейчас")
            self.automation_evaluate_button.clicked.connect(
                self.automation_evaluate_requested
            )
            rule_controls.addWidget(self.automation_create_button)
            rule_controls.addWidget(self.automation_preview_button)
            rule_controls.addWidget(self.automation_evaluate_button)
            rule_controls.addStretch()
            layout.addLayout(rule_controls)
            rule_title = QLabel("Активные правила")
            rule_title.setStyleSheet("font-weight: 700; margin-top: 8px;")
            layout.addWidget(rule_title)
            self.automation_rule_rows = QVBoxLayout()
            layout.addLayout(self.automation_rule_rows)
            run_title = QLabel("Последние срабатывания")
            run_title.setStyleSheet("font-weight: 700; margin-top: 8px;")
            layout.addWidget(run_title)
            self.automation_run_rows = QVBoxLayout()
            layout.addLayout(self.automation_run_rows)
            mirror_title = QLabel("Зеркалирование")
            mirror_title.setStyleSheet("font-weight: 700; font-size: 16px; margin-top: 12px;")
            layout.addWidget(mirror_title)
            mirror_description = QLabel(
                "Каждая новая версия файла автоматически копируется на активные диски с ролью "
                "«Зеркало». Ошибка зеркала не блокирует загрузку: Core отмечает деградацию, а "
                "проверка ниже сверяет SHA-256 и восстанавливает отсутствующие или повреждённые реплики."
            )
            mirror_description.setWordWrap(True)
            mirror_description.setProperty("muted", True)
            layout.addWidget(mirror_description)
            form = QFormLayout()
            self.mirror_target = QComboBox()
            form.addRow("Зеркальный диск", self.mirror_target)
            layout.addLayout(form)
            controls = QHBoxLayout()
            self.mirror_start_button = QPushButton("Проверить и восстановить зеркало")
            self.mirror_start_button.setProperty("primary", True)
            self.mirror_start_button.setEnabled(False)
            self.mirror_start_button.clicked.connect(self._emit_mirror_reconcile)
            refresh_mirrors = QPushButton("Обновить")
            refresh_mirrors.clicked.connect(self.maintenance_refresh_requested)
            controls.addWidget(self.mirror_start_button)
            controls.addWidget(refresh_mirrors)
            controls.addStretch()
            layout.addLayout(controls)
            self.mirror_rows = QVBoxLayout()
            layout.addLayout(self.mirror_rows)
            jobs_title = QLabel("Последние проверки")
            jobs_title.setStyleSheet("font-weight: 700; margin-top: 8px;")
            layout.addWidget(jobs_title)
            self.mirror_job_rows = QVBoxLayout()
            layout.addLayout(self.mirror_job_rows)
        elif name == "Резервные копии":
            description = QLabel(
                "Снимок содержит согласованную SQLite-копию метаданных, manifest.json и все "
                "текущие объекты вместе с историей версий. Готовым он считается только после "
                "проверки размера и SHA-256 каждого файла."
            )
            description.setWordWrap(True)
            description.setProperty("muted", True)
            layout.addWidget(description)
            form = QFormLayout()
            self.backup_target = QComboBox()
            self.backup_target.currentIndexChanged.connect(
                self._load_selected_backup_policy
            )
            form.addRow("Диск с ролью «Резервные копии»", self.backup_target)
            self.restore_target = QComboBox()
            form.addRow("Основной диск для восстановления", self.restore_target)
            self.backup_policy_enabled = QCheckBox(
                "Автоматически создавать и проверять снимки"
            )
            form.addRow("Расписание", self.backup_policy_enabled)
            self.backup_policy_interval = QSpinBox()
            self.backup_policy_interval.setRange(1, 8760)
            self.backup_policy_interval.setValue(24)
            self.backup_policy_interval.setSuffix(" ч")
            form.addRow("Интервал", self.backup_policy_interval)
            self.backup_policy_keep = QSpinBox()
            self.backup_policy_keep.setRange(1, 365)
            self.backup_policy_keep.setValue(7)
            self.backup_policy_keep.setSuffix(" снимков")
            form.addRow("Хранить последние", self.backup_policy_keep)
            layout.addLayout(form)
            controls = QHBoxLayout()
            self.backup_start_button = QPushButton("Создать проверенный снимок")
            self.backup_start_button.setProperty("primary", True)
            self.backup_start_button.setEnabled(False)
            self.backup_start_button.clicked.connect(self._emit_backup)
            refresh = QPushButton("Обновить")
            refresh.clicked.connect(self.maintenance_refresh_requested)
            controls.addWidget(self.backup_start_button)
            controls.addWidget(refresh)
            controls.addStretch()
            layout.addLayout(controls)
            policy_controls = QHBoxLayout()
            self.backup_policy_save_button = QPushButton("Сохранить автоматизацию")
            self.backup_policy_save_button.clicked.connect(self._emit_backup_policy)
            self.backup_policy_run_button = QPushButton("Запустить цикл сейчас")
            self.backup_policy_run_button.clicked.connect(self._emit_backup_policy_run)
            policy_controls.addWidget(self.backup_policy_save_button)
            policy_controls.addWidget(self.backup_policy_run_button)
            policy_controls.addStretch()
            layout.addLayout(policy_controls)
            self.backup_rows = QVBoxLayout()
            layout.addLayout(self.backup_rows)
            verification_title = QLabel("Проверки восстановления")
            verification_title.setStyleSheet("font-weight: 700; margin-top: 8px;")
            layout.addWidget(verification_title)
            self.backup_verification_rows = QVBoxLayout()
            layout.addLayout(self.backup_verification_rows)
            restore_title = QLabel("Последние восстановления")
            restore_title.setStyleSheet("font-weight: 700; margin-top: 8px;")
            layout.addWidget(restore_title)
            self.restore_rows = QVBoxLayout()
            layout.addLayout(self.restore_rows)
        elif name == "Обслуживание":
            description = QLabel(
                "Безопасный перенос доступен только с диска, где новые записи приостановлены, "
                "на активный диск. Каждый объект проверяется по размеру и SHA-256; исходная копия "
                "остаётся на месте как страховочная."
            )
            description.setWordWrap(True)
            description.setProperty("muted", True)
            layout.addWidget(description)
            form = QFormLayout()
            self.migration_source = QComboBox()
            self.migration_target = QComboBox()
            form.addRow("Источник (запись на паузе)", self.migration_source)
            form.addRow("Целевой активный диск", self.migration_target)
            layout.addLayout(form)
            controls = QHBoxLayout()
            self.migration_start_button = QPushButton("Начать проверяемый перенос")
            self.migration_start_button.setProperty("primary", True)
            self.migration_start_button.setEnabled(False)
            self.migration_start_button.clicked.connect(self._emit_migration)
            refresh_jobs = QPushButton("Обновить задания")
            refresh_jobs.clicked.connect(self.maintenance_refresh_requested)
            controls.addWidget(self.migration_start_button)
            controls.addWidget(refresh_jobs)
            controls.addStretch()
            layout.addLayout(controls)
            self.maintenance_rows = QVBoxLayout()
            layout.addLayout(self.maintenance_rows)
        layout.addStretch()
        return _scroll_page(page)

    def _emit_migration(self) -> None:
        if self.migration_source is None or self.migration_target is None:
            return
        source = self.migration_source.currentData()
        target = self.migration_target.currentData()
        if source and target:
            self.migration_requested.emit(str(source), str(target))

    def _emit_backup(self) -> None:
        if self.backup_target is not None and self.backup_target.currentData():
            self.backup_requested.emit(str(self.backup_target.currentData()))

    def _emit_restore(self, backup_job_id: str) -> None:
        if self.restore_target is not None and self.restore_target.currentData():
            self.restore_requested.emit(
                backup_job_id, str(self.restore_target.currentData())
            )

    def _emit_backup_verification(self, backup_job_id: str) -> None:
        if self.restore_target is not None and self.restore_target.currentData():
            self.backup_verify_requested.emit(
                backup_job_id, str(self.restore_target.currentData())
            )

    def _emit_backup_policy(self) -> None:
        if (
            self.backup_target is None
            or not self.backup_target.currentData()
            or self.backup_policy_enabled is None
            or self.backup_policy_interval is None
            or self.backup_policy_keep is None
        ):
            return
        verification_root_id = (
            str(self.restore_target.currentData())
            if self.restore_target is not None and self.restore_target.currentData()
            else None
        )
        self.backup_policy_requested.emit(
            {
                "target_root_id": str(self.backup_target.currentData()),
                "enabled": self.backup_policy_enabled.isChecked(),
                "interval_hours": self.backup_policy_interval.value(),
                "keep_last": self.backup_policy_keep.value(),
                "verification_root_id": verification_root_id,
            }
        )

    def _emit_backup_policy_run(self) -> None:
        if self.backup_target is not None and self.backup_target.currentData():
            self.backup_policy_run_requested.emit(
                str(self.backup_target.currentData())
            )

    def _load_selected_backup_policy(self) -> None:
        target_root_id = (
            str(self.backup_target.currentData())
            if self.backup_target is not None and self.backup_target.currentData()
            else ""
        )
        policy = next(
            (
                item
                for item in self._backup_policies
                if item.get("target_root_id") == target_root_id
            ),
            None,
        )
        if self.backup_policy_enabled is not None:
            self.backup_policy_enabled.setChecked(bool(policy and policy.get("enabled")))
        if self.backup_policy_interval is not None:
            self.backup_policy_interval.setValue(
                int(policy.get("interval_hours", 24)) if policy else 24
            )
        if self.backup_policy_keep is not None:
            self.backup_policy_keep.setValue(
                int(policy.get("keep_last", 7)) if policy else 7
            )
        if policy and self.restore_target is not None:
            verification_index = self.restore_target.findData(
                policy.get("verification_root_id")
            )
            if verification_index >= 0:
                self.restore_target.setCurrentIndex(verification_index)
        if self.backup_policy_run_button is not None:
            self.backup_policy_run_button.setEnabled(
                self._backup_online and policy is not None
            )

    def _emit_mirror_reconcile(self) -> None:
        if self.mirror_target is not None and self.mirror_target.currentData():
            self.mirror_reconcile_requested.emit(str(self.mirror_target.currentData()))

    def _emit_automation_rule(self) -> None:
        if (
            self.automation_rule_name is None
            or self.automation_trigger is None
            or self.automation_action is None
            or self.automation_cooldown is None
        ):
            return
        self.automation_rule_create_requested.emit(
            {
                "name": self.automation_rule_name.text().strip(),
                "enabled": True,
                "trigger_type": str(self.automation_trigger.currentData()),
                "action_type": str(self.automation_action.currentData()),
                "cooldown_minutes": self.automation_cooldown.value(),
            }
        )

    def _automation_trigger_changed(self) -> None:
        if self.automation_trigger is None or self.automation_action is None:
            return
        trigger = str(self.automation_trigger.currentData())
        previous = self.automation_action.currentData()
        self.automation_action.blockSignals(True)
        self.automation_action.clear()
        for action in AUTOMATION_ACTIONS_BY_TRIGGER.get(trigger, ("notify",)):
            self.automation_action.addItem(AUTOMATION_ACTION_LABELS[action].capitalize(), action)
        previous_index = self.automation_action.findData(previous)
        self.automation_action.setCurrentIndex(max(0, previous_index))
        self.automation_action.blockSignals(False)
        if self.automation_rule_hint is not None:
            if trigger == "scheduled":
                if self.automation_cooldown is not None and self.automation_cooldown.value() == 60:
                    self.automation_cooldown.setValue(1440)
                text = (
                    "Для расписания поле «Не чаще» является интервалом: 1440 минут — ежедневно, "
                    "10080 минут — еженедельно. Первый запуск произойдёт при ближайшей проверке Core."
                )
            elif trigger == "pending_device":
                text = (
                    "Для ожидающего устройства разрешено только уведомление: подтверждение всегда "
                    "выполняет администратор вручную."
                )
            else:
                text = (
                    "Список действий уже отфильтрован по безопасности. Cooldown не позволит "
                    "повторять одно действие чаще указанного интервала."
                )
            self.automation_rule_hint.setText(text)

    def _emit_automation_settings(self) -> None:
        if (
            self.automation_scheduler_enabled is None
            or self.automation_scheduler_interval is None
        ):
            return
        self.automation_settings_requested.emit(
            {
                "enabled": self.automation_scheduler_enabled.isChecked(),
                "interval_seconds": self.automation_scheduler_interval.value(),
            }
        )

    def show_automation_preview(self, result: dict, *, rule_name: str = "") -> None:
        if self.automation_scheduler_status is None:
            return
        rules = list(result.get("rules") or [])
        if rule_name and rules:
            item = rules[0]
            if item.get("matched"):
                cooldown = int(item.get("cooldown_remaining_seconds", 0))
                suffix = (
                    f"; cooldown ещё {cooldown} сек"
                    if cooldown
                    else "; действие готово к запуску"
                )
                text = f"Условие правила «{rule_name}» совпало{suffix}. Действие не запускалось."
            else:
                text = f"Условие правила «{rule_name}» сейчас не совпадает. Действие не запускалось."
        else:
            text = (
                f"Проверено правил: {len(rules)} · условия совпали: "
                f"{int(result.get('matched_rules', 0))} · готовы к запуску: "
                f"{int(result.get('ready_rules', 0))}. Действия не запускались."
            )
        self.automation_scheduler_status.setText(text)

    @staticmethod
    def _add_muted(layout: QVBoxLayout, text: str) -> None:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setProperty("emptyState", True)
        layout.addWidget(label)
        label.show()

    def _add_device_card(self, layout: QVBoxLayout, device: dict, pending: bool) -> None:
        card = QFrame()
        card.setProperty("card", True)
        row = QHBoxLayout(card)
        text = QVBoxLayout()
        title = QLabel(device["name"])
        title.setStyleSheet("font-weight: 700; font-size: 16px;")
        detail = QLabel(f"{device['user_display_name']} · {device['platform']}")
        detail.setProperty("muted", True)
        text.addWidget(title)
        text.addWidget(detail)
        row.addLayout(text, 1)
        if pending:
            reject = QPushButton("Отклонить")
            reject.clicked.connect(
                lambda checked=False, device_id=device["id"]: self.revoke_device_requested.emit(
                    device_id
                )
            )
            approve = QPushButton("Подтвердить")
            approve.setProperty("primary", True)
            approve.clicked.connect(
                lambda checked=False, device_id=device["id"]: self.approve_device_requested.emit(
                    device_id
                )
            )
            row.addWidget(reject)
            row.addWidget(approve)
        else:
            revoke = QPushButton("Отключить")
            revoke.clicked.connect(
                lambda checked=False, device_id=device["id"]: self.revoke_device_requested.emit(
                    device_id
                )
            )
            row.addWidget(revoke)
        layout.addWidget(card)
        card.show()

    def _show_section(self, row: int) -> None:
        if row >= 0:
            self.stack.setCurrentIndex(row)

    def show_section(self, name: str) -> None:
        for row in range(self.section_list.count()):
            if self.section_list.item(row).text() == name:
                self.section_list.setCurrentRow(row)
                return

    def _settings_payload(self) -> dict:
        return {
            "server_name": self.server_name.text().strip() or "Домашнее облако",
            "notifications_enabled": self.notifications.isChecked(),
            "refresh_interval_seconds": self.refresh_interval.value(),
            "advanced_mode": bool(self.mode.currentData()),
            "lan_enabled": self.lan_enabled.isChecked(),
            "lan_port": self.lan_port.value(),
            "remote_enabled": self.remote_enabled.isChecked(),
            "remote_port": self.remote_port.value(),
            "remote_public_url": self.remote_public_url.text().strip().rstrip("/"),
            "remote_pairing_enabled": self.remote_pairing_enabled.isChecked(),
            "zrok_enabled": self.zrok_enabled.isChecked(),
            "zrok_port": self.zrok_port.value(),
            "zrok_executable": self.zrok_executable.text().strip() or "zrok",
            "zrok_share_name": self.zrok_share_name.text().strip(),
        }

    def _emit_save(self) -> None:
        self.save_requested.emit(self._settings_payload())

    def mark_settings_saved(self) -> None:
        self._saved_settings_payload = self._settings_payload()
        self.save_button.setVisible(False)

    def _connect_settings_tracking(self) -> None:
        for checkbox in (
            self.notifications,
            self.lan_enabled,
            self.remote_enabled,
            self.remote_pairing_enabled,
            self.zrok_enabled,
        ):
            checkbox.toggled.connect(self._settings_changed)
        for spinbox in (
            self.refresh_interval,
            self.lan_port,
            self.remote_port,
            self.zrok_port,
        ):
            spinbox.valueChanged.connect(self._settings_changed)
        for line_edit in (
            self.server_name,
            self.remote_public_url,
            self.zrok_executable,
            self.zrok_share_name,
        ):
            line_edit.textChanged.connect(self._settings_changed)

    def _settings_changed(self, *_args: object) -> None:
        if self._loading_settings:
            return
        self.save_button.setVisible(self._settings_payload() != self._saved_settings_payload)
