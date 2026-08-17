from __future__ import annotations

import secrets
import shutil
import string
from datetime import UTC, datetime
from pathlib import Path

import qrcode
from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from cloud_storage.models import (
    ROLE_LABELS,
    AppSettings,
    DiskConfiguration,
    DiskMode,
    DiskRole,
    DiskSnapshot,
)
from cloud_storage.pairing import build_pairing_uri
from cloud_storage.ui.widgets import format_bytes


def _value(value: object | None, suffix: str = "") -> str:
    return f"{value}{suffix}" if value is not None and value != "" else "Недоступно"


class DiskDetailDialog(QDialog):
    configuration_saved = Signal(str, object)
    permissions_saved = Signal(str, object)
    refresh_requested = Signal()
    cleanup_requested = Signal(str)
    check_requested = Signal(str)
    optimize_requested = Signal(str)
    format_requested = Signal(str)
    remove_requested = Signal(str)

    def __init__(
        self,
        disk: DiskSnapshot,
        configuration: DiskConfiguration,
        users: list[dict] | None = None,
        spaces: list[dict] | None = None,
        storage_roots: list[dict] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.disk = disk
        self.core_users = list(users or [])
        self.core_spaces = list(spaces or [])
        self.storage_roots = list(storage_roots or [])
        self.permission_rows: dict[str, dict[str, QCheckBox]] = {}
        root_ids = {
            str(item.get("id") or "")
            for item in self.storage_roots
            if str(item.get("disk_id") or "") == disk.id
        }
        self.disk_spaces = [
            item
            for item in self.core_spaces
            if item.get("kind") == "shared"
            and (
                str(item.get("primary_storage_root_id") or "") in root_ids
                or str(item.get("fallback_storage_root_id") or "") in root_ids
            )
        ]
        self.original_role = configuration.role
        self.setWindowTitle(f"Диск · {configuration.display_name or disk.label or disk.mountpoint}")
        self.setMinimumSize(760, 650)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)

        header = QHBoxLayout()
        names = QVBoxLayout()
        title = QLabel(configuration.display_name or disk.label or disk.mountpoint)
        title.setObjectName("PageTitle")
        subtitle = QLabel(f"{disk.mountpoint}  ·  {disk.model or 'Модель недоступна'}")
        subtitle.setProperty("muted", True)
        names.addWidget(title)
        names.addWidget(subtitle)
        header.addLayout(names, 1)
        refresh = QPushButton("Обновить данные")
        refresh.clicked.connect(self.refresh_requested)
        header.addWidget(refresh)
        if disk.available:
            explorer = QPushButton("Открыть в Проводнике")
            explorer.clicked.connect(
                lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(disk.mountpoint))
            )
            header.addWidget(explorer)
        root.addLayout(header)

        tabs = QTabWidget()
        self.tabs = tabs
        tabs.addTab(self._overview_tab(), "Обзор")
        tabs.addTab(self._configuration_tab(configuration), "Настройка")
        tabs.addTab(self._permissions_tab(), "Доступ")
        tabs.addTab(self._operations_tab(configuration), "Состояние")
        root.addWidget(tabs, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Save).setProperty("primary", True)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _overview_tab(self) -> QWidget:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(8, 18, 8, 8)
        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(12)
        rows = [
            ("ID Cloud Storage", self.disk.id),
            ("Номер физического диска", _value(self.disk.disk_number)),
            ("Точка подключения", self.disk.mountpoint),
            ("Устройство", self.disk.device),
            ("Метка", self.disk.label or "Без метки"),
            ("Модель", _value(self.disk.model)),
            ("Серийный номер", _value(self.disk.serial)),
            ("Интерфейс", _value(self.disk.interface)),
            ("Файловая система", _value(self.disk.filesystem)),
            ("Общий объём", format_bytes(self.disk.total_bytes)),
            ("Занято", format_bytes(self.disk.used_bytes)),
            ("Свободно", format_bytes(self.disk.free_bytes)),
            ("Температура", _value(self.disk.temperature_c, " °C")),
            ("Скорость чтения", _value(self.disk.read_speed_mbps, " МБ/с")),
            ("Скорость записи", _value(self.disk.write_speed_mbps, " МБ/с")),
            ("Текущая нагрузка", _value(self.disk.utilization_percent, "%")),
            ("Время работы", _value(self.disk.power_on_hours, " ч")),
            ("Системная диагностика", self.disk.health_detail or "Ошибок чтения не обнаружено"),
        ]
        for row, (name, value) in enumerate(rows):
            key = QLabel(name)
            key.setProperty("muted", True)
            val = QLabel(str(value))
            val.setWordWrap(True)
            grid.addWidget(key, row, 0)
            grid.addWidget(val, row, 1)
        layout.addLayout(grid)

        note = QFrame()
        note.setProperty("accent", "blue")
        note_layout = QVBoxLayout(note)
        note_title = QLabel("Только подтверждённые показатели")
        note_title.setStyleSheet("font-weight: 700;")
        note_body = QLabel(
            "Если ОС не отдаёт SMART, температуру или скорость, менеджер показывает «Недоступно», "
            "а не подставляет примерные значения."
        )
        note_body.setProperty("muted", True)
        note_body.setWordWrap(True)
        note_layout.addWidget(note_title)
        note_layout.addWidget(note_body)
        layout.addWidget(note)
        layout.addStretch()
        return content

    def _configuration_tab(self, configuration: DiskConfiguration) -> QWidget:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(8, 18, 8, 8)
        form = QFormLayout()
        form.setVerticalSpacing(12)
        self.display_name = QLineEdit(configuration.display_name)
        self.display_name.setPlaceholderText(self.disk.label or self.disk.mountpoint)
        self.role = QComboBox()
        roles = ROLE_LABELS.items()
        if self.disk.is_system:
            roles = [
                (role, label)
                for role, label in roles
                if role in {DiskRole.UNCONFIGURED, DiskRole.UNUSED}
            ]
        for role, label in roles:
            self.role.addItem(label, role.value)
        self.role.setCurrentIndex(max(0, self.role.findData(configuration.role.value)))
        self.priority = QSpinBox()
        self.priority.setRange(0, 100)
        self.priority.setValue(configuration.write_priority)
        self.priority.setSuffix(" / 100")
        self.max_fill = QSpinBox()
        self.max_fill.setRange(50, 99)
        self.max_fill.setValue(configuration.max_fill_percent)
        self.max_fill.setSuffix(" %")
        self.min_free = QSpinBox()
        self.min_free.setRange(1, 10000)
        self.min_free.setValue(configuration.min_free_gib)
        self.min_free.setSuffix(" ГБ")
        self.users = QLineEdit(", ".join(configuration.allowed_users))
        self.users.setPlaceholderText("Например: admin, family")
        self.folders = QLineEdit(", ".join(configuration.allowed_folders))
        self.folders.setPlaceholderText("Например: Фото, Общие файлы")
        self.auto_move = QCheckBox("Разрешить автоматический перенос при обслуживании")
        self.auto_move.setChecked(configuration.auto_move_allowed)
        self.read_only = QCheckBox("Только чтение")
        self.read_only.setChecked(configuration.read_only)
        self.encryption = QCheckBox("Запросить шифрование после появления серверного ядра")
        self.encryption.setChecked(configuration.encryption_requested)
        form.addRow("Название", self.display_name)
        form.addRow("Назначение", self.role)
        form.addRow("Приоритет записи", self.priority)
        form.addRow("Максимальное заполнение", self.max_fill)
        form.addRow("Минимальный запас", self.min_free)
        form.addRow("Пользователи", self.users)
        form.addRow("Логические папки", self.folders)
        form.addRow("", self.auto_move)
        form.addRow("", self.read_only)
        form.addRow("", self.encryption)
        layout.addLayout(form)

        if self.disk.is_system:
            system_warning = QLabel(
                "Это системный диск. Он показывается для диагностики, но не может быть "
                "назначен хранилищем, кэшем, резервом или зеркалом."
            )
            system_warning.setWordWrap(True)
            system_warning.setProperty("danger", True)
            layout.addWidget(system_warning)

        warning = QLabel(
            "Изменение назначения применяет политику новых записей. Перенос существующих "
            "управляемых объектов запускается отдельно в разделе «Обслуживание» и требует "
            "приостановить запись на исходный диск."
        )
        warning.setWordWrap(True)
        warning.setProperty("muted", True)
        layout.addWidget(warning)
        layout.addStretch()
        return content

    def _permissions_tab(self) -> QWidget:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(8, 18, 8, 8)
        title = QLabel("Права на общие пространства этого диска")
        title.setObjectName("SectionTitle")
        names = ", ".join(str(item.get("name") or "Пространство") for item in self.disk_spaces)
        description_text = (
            f"Изменения применяются сервером ко всем общим пространствам на диске: {names}."
            if names
            else "На этом диске пока нет общих логических пространств. "
            "Создайте пространство и назначьте ему этот диск."
        )
        description = QLabel(description_text)
        description.setWordWrap(True)
        description.setProperty("muted", True)
        layout.addWidget(title)
        layout.addWidget(description)

        mass = QHBoxLayout()
        default_access = QPushButton("Всем: просмотр + загрузка")
        default_access.clicked.connect(lambda: self._set_all_permissions({"read", "upload"}))
        full_access = QPushButton("Всем: полный доступ")
        full_access.clicked.connect(
            lambda: self._set_all_permissions({"read", "upload", "modify", "delete", "share"})
        )
        revoke = QPushButton("Снять доступ у всех")
        revoke.clicked.connect(lambda: self._set_all_permissions(set()))
        for button in (default_access, full_access, revoke):
            button.setEnabled(bool(self.disk_spaces and self.core_users))
            mass.addWidget(button)
        mass.addStretch()
        layout.addLayout(mass)

        grid = QGridLayout()
        headers = ("Пользователь", "Просмотр", "Загрузка", "Изменение", "Удаление", "Ссылки")
        for column, label in enumerate(headers):
            heading = QLabel(label)
            heading.setStyleSheet("font-weight: 700;")
            grid.addWidget(heading, 0, column)
        capabilities = ("read", "upload", "modify", "delete", "share")
        for row_index, user in enumerate(self.core_users, start=1):
            user_id = str(user.get("id") or "")
            if not user_id:
                continue
            name = str(user.get("display_name") or user.get("username") or "Пользователь")
            grid.addWidget(QLabel(name), row_index, 0)
            grants = {
                str(item.get("space_id") or ""): item.get("capabilities") or {}
                for item in user.get("space_grants") or []
            }
            checks: dict[str, QCheckBox] = {}
            for column, capability in enumerate(capabilities, start=1):
                check = QCheckBox()
                check.setEnabled(bool(self.disk_spaces))
                values = [
                    bool((grants.get(str(space.get("id") or "")) or {}).get(capability))
                    for space in self.disk_spaces
                ]
                if values and any(values) and not all(values):
                    check.setTristate(True)
                    check.setCheckState(Qt.CheckState.PartiallyChecked)
                    check.setToolTip("Права различаются между пространствами; состояние сохранится")
                else:
                    check.setChecked(bool(values) and all(values))
                checks[capability] = check
                grid.addWidget(check, row_index, column, Qt.AlignmentFlag.AlignCenter)
            self.permission_rows[user_id] = checks
        layout.addLayout(grid)
        layout.addStretch()
        return content

    def _set_all_permissions(self, enabled: set[str]) -> None:
        for checks in self.permission_rows.values():
            for capability, check in checks.items():
                check.setChecked(capability in enabled)

    def _operations_tab(self, configuration: DiskConfiguration) -> QWidget:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(8, 18, 8, 8)
        heading = QLabel("Режим работы")
        heading.setObjectName("SectionTitle")
        description = QLabel(
            "Эти состояния управляют политикой новых записей. Пауза и обслуживание сохраняют "
            "доступ к чтению; менеджер не размонтирует диск и не изменяет системные разделы."
        )
        description.setProperty("muted", True)
        description.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(description)
        self.mode = QComboBox()
        self.mode.addItem("Активен", DiskMode.ACTIVE.value)
        self.mode.addItem("Новые записи приостановлены", DiskMode.WRITES_PAUSED.value)
        self.mode.addItem("Обслуживание", DiskMode.MAINTENANCE.value)
        self.mode.addItem("Отключён в менеджере", DiskMode.DISCONNECTED.value)
        self.mode.setCurrentIndex(max(0, self.mode.findData(configuration.mode.value)))
        layout.addWidget(self.mode)

        actions = QHBoxLayout()
        stop = QPushButton("Остановить запись")
        stop.clicked.connect(
            lambda: self.mode.setCurrentIndex(self.mode.findData(DiskMode.WRITES_PAUSED.value))
        )
        maintenance = QPushButton("Обслуживание")
        maintenance.clicked.connect(
            lambda: self.mode.setCurrentIndex(self.mode.findData(DiskMode.MAINTENANCE.value))
        )
        ignore = QPushButton("Игнорировать")
        ignore.clicked.connect(
            lambda: self.role.setCurrentIndex(self.role.findData(DiskRole.UNUSED.value))
        )
        check = QPushButton("Найти ошибки")
        check.clicked.connect(lambda: self.check_requested.emit(self.disk.id))
        clean = QPushButton("Очистить временное")
        clean.clicked.connect(lambda: self.cleanup_requested.emit(self.disk.id))
        optimize = QPushButton("Оптимизировать")
        optimize.setEnabled(self.disk.available and not self.disk.is_system)
        optimize.clicked.connect(lambda: self.optimize_requested.emit(self.disk.id))
        reassign = QPushButton("Переназначить")
        reassign.clicked.connect(lambda: self.tabs.setCurrentIndex(1))
        for button in (stop, maintenance, ignore, check, clean, optimize, reassign):
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)

        safety = QFrame()
        safety.setProperty("accent", "red")
        safety_layout = QVBoxLayout(safety)
        safety_title = QLabel("Системные операции")
        safety_title.setStyleSheet("font-weight: 700;")
        safety_body = QLabel(
            "Форматирование доступно только для несистемного тома и требует повторно ввести "
            "букву диска. Удаление из Manager не стирает файлы и не удаляет раздел."
        )
        safety_body.setWordWrap(True)
        safety_body.setProperty("muted", True)
        safety_layout.addWidget(safety_title)
        safety_layout.addWidget(safety_body)
        layout.addWidget(safety)

        destructive = QHBoxLayout()
        format_button = QPushButton("Форматировать…")
        format_button.setEnabled(self.disk.available and not self.disk.is_system)
        format_button.clicked.connect(lambda: self.format_requested.emit(self.disk.id))
        remove_button = QPushButton("Удалить диск из Manager")
        remove_button.setEnabled(not self.disk.is_system)
        remove_button.clicked.connect(lambda: self.remove_requested.emit(self.disk.id))
        destructive.addWidget(format_button)
        destructive.addWidget(remove_button)
        destructive.addStretch()
        layout.addLayout(destructive)

        self.last_check = QLabel(
            f"Последнее обновление: {configuration.last_check_at or 'ещё не выполнялось'}"
        )
        self.last_check.setProperty("muted", True)
        layout.addWidget(self.last_check)
        layout.addStretch()
        return content

    @staticmethod
    def _csv(value: str) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))

    def _save(self) -> None:
        role = DiskRole(self.role.currentData())
        if self.disk.is_system and role not in {DiskRole.UNCONFIGURED, DiskRole.UNUSED}:
            QMessageBox.warning(
                self,
                "Системный диск защищён",
                "Системный диск нельзя использовать для данных Cloud Storage.",
            )
            return
        if self.original_role not in {DiskRole.UNCONFIGURED, role}:
            response = QMessageBox.question(
                self,
                "Изменить назначение?",
                "Назначение диска изменится в настройках и политике ядра. Beta 0.2 не переносит "
                "существующие данные. Продолжить?",
            )
            if response != QMessageBox.StandardButton.Yes:
                return
        configuration = DiskConfiguration(
            display_name=self.display_name.text().strip(),
            role=role,
            mode=DiskMode(self.mode.currentData()),
            write_priority=self.priority.value(),
            max_fill_percent=self.max_fill.value(),
            min_free_gib=self.min_free.value(),
            allowed_users=self._csv(self.users.text()),
            allowed_folders=self._csv(self.folders.text()),
            auto_move_allowed=self.auto_move.isChecked(),
            read_only=self.read_only.isChecked(),
            encryption_requested=self.encryption.isChecked(),
            last_check_at=datetime.now(UTC).isoformat(timespec="seconds"),
            identity_mountpoint=self.disk.mountpoint,
            identity_device=self.disk.device,
            identity_serial=self.disk.serial or "",
        )
        self.configuration_saved.emit(self.disk.id, configuration)
        if self.disk_spaces:
            permissions = {
                user_id: {
                    capability: (
                        None
                        if check.checkState() == Qt.CheckState.PartiallyChecked
                        else check.isChecked()
                    )
                    for capability, check in checks.items()
                }
                for user_id, checks in self.permission_rows.items()
            }
            self.permissions_saved.emit(self.disk.id, permissions)
        self.accept()


class SetupDialog(QDialog):
    def __init__(
        self,
        disks: list[DiskSnapshot],
        settings: AppSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.disks = disks
        self.rows: dict[str, tuple[QCheckBox, QComboBox]] = {}
        self.setWindowTitle("Первоначальная настройка")
        self.setMinimumSize(680, 500)
        root = QVBoxLayout(self)
        title = QLabel("Подготовим Server Manager")
        title.setObjectName("PageTitle")
        subtitle = QLabel(
            "Выберите только те накопители, которыми хотите управлять. Никакие данные не меняются."
        )
        subtitle.setProperty("muted", True)
        subtitle.setWordWrap(True)
        root.addWidget(title)
        root.addWidget(subtitle)

        form = QFormLayout()
        self.server_name = QLineEdit(settings.server_name)
        self.server_name.setMaxLength(80)
        form.addRow("Название сервера", self.server_name)
        root.addLayout(form)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        disk_content = QWidget()
        disk_layout = QVBoxLayout(disk_content)
        for disk in disks:
            config = settings.configuration_for(disk.id)
            card = QFrame()
            card.setProperty("card", True)
            card_layout = QGridLayout(card)
            check = QCheckBox(config.display_name or disk.label or disk.mountpoint)
            check.setChecked(config.role not in {DiskRole.UNCONFIGURED, DiskRole.UNUSED})
            details = QLabel(f"{disk.mountpoint} · {format_bytes(disk.total_bytes)}")
            details.setProperty("muted", True)
            role = QComboBox()
            for item, label in ROLE_LABELS.items():
                if item not in {DiskRole.UNCONFIGURED, DiskRole.UNUSED}:
                    role.addItem(label, item.value)
            current = role.findData(config.role.value)
            role.setCurrentIndex(max(0, current))
            role.setEnabled(check.isChecked())
            check.toggled.connect(role.setEnabled)
            card_layout.addWidget(check, 0, 0)
            card_layout.addWidget(details, 1, 0)
            card_layout.addWidget(role, 0, 1, 2, 1)
            disk_layout.addWidget(card)
            self.rows[disk.id] = (check, role)
        disk_layout.addStretch()
        scroll.setWidget(disk_content)
        root.addWidget(scroll, 1)

        safety = QLabel(
            "Безопасность: мастер не форматирует диски, не меняет разделы и не переносит файлы."
        )
        safety.setWordWrap(True)
        safety.setProperty("muted", True)
        root.addWidget(safety)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Завершить настройку")
        buttons.button(QDialogButtonBox.StandardButton.Save).setProperty("primary", True)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Настроить позже")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def apply_to(self, settings: AppSettings) -> bool:
        settings.server_name = self.server_name.text().strip() or "Домашнее облако"
        selected = False
        for disk_id, (check, role) in self.rows.items():
            config = settings.configuration_for(disk_id)
            if check.isChecked():
                config.role = DiskRole(role.currentData())
                selected = True
            elif config.role != DiskRole.UNUSED:
                config.role = DiskRole.UNCONFIGURED
        settings.setup_complete = selected
        settings.setup_reminded_later = not selected
        return selected


class SpaceDialog(QDialog):
    def __init__(
        self,
        storage_roots: list[dict],
        space: dict | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Логическое пространство")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        title = QLabel("Настроить общее пространство")
        title.setObjectName("PageTitle")
        description = QLabel(
            "Пользователи увидят отдельный логический диск. Физические пути и структура "
            "серверного диска останутся скрытыми."
        )
        description.setWordWrap(True)
        description.setProperty("muted", True)
        layout.addWidget(title)
        layout.addWidget(description)
        form = QFormLayout()
        self.name = QLineEdit(str((space or {}).get("name") or ""))
        self.name.setPlaceholderText("Например: Семья или Фото")
        self.quota = QSpinBox()
        self.quota.setRange(1, 1_000_000)
        self.quota.setSuffix(" ГБ")
        self.quota.setValue(max(1, int((space or {}).get("quota_bytes", 100 * 1024**3)) // 1024**3))
        self.primary = QComboBox()
        self.fallback = QComboBox()
        self.primary.addItem("Автоматический выбор", None)
        self.fallback.addItem("Без резервного диска", None)
        for root in storage_roots:
            if root.get("purpose") != "primary":
                continue
            label = str(root.get("disk_id") or root.get("path") or root.get("id"))
            self.primary.addItem(label, root.get("id"))
            self.fallback.addItem(label, root.get("id"))
        self.primary.setCurrentIndex(
            max(0, self.primary.findData((space or {}).get("primary_storage_root_id")))
        )
        self.fallback.setCurrentIndex(
            max(0, self.fallback.findData((space or {}).get("fallback_storage_root_id")))
        )
        self.enabled = QCheckBox("Пространство включено")
        self.enabled.setChecked(bool((space or {}).get("enabled", True)))
        form.addRow("Название", self.name)
        form.addRow("Квота", self.quota)
        form.addRow("Основной диск", self.primary)
        form.addRow("Резервный диск", self.fallback)
        form.addRow("", self.enabled)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Save).setProperty("primary", True)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._validate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _validate(self) -> None:
        if not self.name.text().strip():
            QMessageBox.warning(self, "Нет названия", "Введите название пространства.")
            return
        if self.primary.currentData() and self.primary.currentData() == self.fallback.currentData():
            QMessageBox.warning(
                self,
                "Выберите другой резервный диск",
                "Основной и резервный диски не могут совпадать.",
            )
            return
        self.accept()

    def values(self) -> dict[str, object]:
        return {
            "name": self.name.text().strip(),
            "quota_gib": self.quota.value(),
            "primary_storage_root_id": self.primary.currentData(),
            "fallback_storage_root_id": self.fallback.currentData(),
            "enabled": self.enabled.isChecked(),
        }


class CreateUserDialog(QDialog):
    def __init__(
        self,
        spaces: list[dict] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Новый пользователь")
        self.setMinimumSize(620, 650)
        layout = QVBoxLayout(self)
        title = QLabel("Создать личное пространство")
        title.setObjectName("PageTitle")
        description = QLabel(
            "Задайте постоянный логин и пароль. На новых компьютерах пользователь сможет "
            "войти с ними, но каждое новое устройство всё равно потребует подтверждения."
        )
        description.setProperty("muted", True)
        description.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(description)
        form = QFormLayout()
        self.username = QLineEdit()
        self.username.setPlaceholderText("ivan")
        self.display_name = QLineEdit()
        self.display_name.setPlaceholderText("Иван")
        self.email = QLineEdit()
        self.email.setPlaceholderText("user@example.com (необязательно)")
        self.password = QLineEdit()
        self.password.setPlaceholderText("Минимум 10 символов")
        password_row = QHBoxLayout()
        password_row.addWidget(self.password, 1)
        generate_password = QPushButton("Создать надёжный")
        generate_password.clicked.connect(self.generate_password)
        password_row.addWidget(generate_password)
        copy_password = QPushButton("Копировать")
        copy_password.clicked.connect(
            lambda: QApplication.clipboard().setText(self.password.text())
        )
        password_row.addWidget(copy_password)
        self.password_confirmation = QLineEdit()
        self.password_confirmation.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_confirmation.setPlaceholderText("Повторите пароль")
        self.quota = QSpinBox()
        self.quota.setRange(1, 1_000_000)
        self.quota.setValue(100)
        self.quota.setSuffix(" ГБ")
        self.personal_space = QCheckBox("Создать личное пространство «Мои файлы»")
        self.personal_space.setChecked(True)
        self.personal_space.toggled.connect(self.quota.setEnabled)
        self.admin = QCheckBox("Администратор сервера")
        form.addRow("Логин", self.username)
        form.addRow("Имя", self.display_name)
        form.addRow("Пароль", password_row)
        form.addRow("Повтор пароля", self.password_confirmation)
        form.addRow("Личное хранилище", self.personal_space)
        form.addRow("Квота личного", self.quota)
        form.addRow("Email для файла входа", self.email)
        form.addRow("", self.admin)
        layout.addLayout(form)
        self.space_permissions: dict[str, dict[str, QCheckBox]] = {}
        shared_spaces = [item for item in (spaces or []) if item.get("kind") == "shared"]
        if shared_spaces:
            spaces_title = QLabel("Доступ к общим пространствам")
            spaces_title.setStyleSheet("font-weight: 700; font-size: 16px;")
            layout.addWidget(spaces_title)
            spaces_scroll = QScrollArea()
            spaces_scroll.setWidgetResizable(True)
            spaces_content = QWidget()
            spaces_layout = QVBoxLayout(spaces_content)
            for space in shared_spaces:
                card = QFrame()
                card.setProperty("card", True)
                card_layout = QVBoxLayout(card)
                enabled = QCheckBox(str(space.get("name") or "Общее пространство"))
                card_layout.addWidget(enabled)
                rights = QHBoxLayout()
                checks: dict[str, QCheckBox] = {"enabled": enabled}
                for key, label, default in (
                    ("read", "Просмотр", True),
                    ("upload", "Загрузка", True),
                    ("modify", "Изменение", False),
                    ("delete", "Удаление", False),
                    ("share", "Ссылки", False),
                ):
                    check = QCheckBox(label)
                    check.setChecked(default)
                    check.setEnabled(False)
                    enabled.toggled.connect(check.setEnabled)
                    rights.addWidget(check)
                    checks[key] = check
                rights.addStretch()
                card_layout.addLayout(rights)
                spaces_layout.addWidget(card)
                self.space_permissions[str(space["id"])] = checks
            spaces_layout.addStretch()
            spaces_scroll.setWidget(spaces_content)
            spaces_scroll.setMinimumHeight(180)
            layout.addWidget(spaces_scroll)
        safety = QLabel(
            "Пользователь не увидит физические диски — только логическое пространство «Мои файлы»."
        )
        safety.setProperty("muted", True)
        safety.setWordWrap(True)
        layout.addWidget(safety)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Создать")
        buttons.button(QDialogButtonBox.StandardButton.Save).setProperty("primary", True)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._validate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.generate_password()

    def generate_password(self) -> None:
        alphabet = string.ascii_letters + string.digits + "-_!@"
        value = "Cs!" + "".join(secrets.choice(alphabet) for _ in range(17))
        self.password.setText(value)
        self.password_confirmation.setText(value)

    def _validate(self) -> None:
        username = self.username.text().strip()
        display_name = self.display_name.text().strip()
        if len(username) < 3 or not display_name:
            QMessageBox.warning(
                self,
                "Проверьте данные",
                "Введите логин длиной минимум 3 символа и отображаемое имя.",
            )
            return
        if len(self.password.text()) < 10:
            QMessageBox.warning(
                self,
                "Слишком короткий пароль",
                "Пароль должен содержать минимум 10 символов.",
            )
            return
        if self.password.text() != self.password_confirmation.text():
            QMessageBox.warning(
                self,
                "Пароли не совпадают",
                "Повторно введите одинаковый пароль в оба поля.",
            )
            return
        if not self.personal_space.isChecked() and not any(
            checks["enabled"].isChecked() for checks in self.space_permissions.values()
        ):
            QMessageBox.warning(
                self,
                "Не выбран доступ",
                "Создайте личное пространство или выберите хотя бы одно общее.",
            )
            return
        self.accept()

    def values(self) -> dict[str, object]:
        return {
            "username": self.username.text().strip(),
            "display_name": self.display_name.text().strip(),
            "password": self.password.text(),
            "quota_gib": self.quota.value(),
            "role": "admin" if self.admin.isChecked() else "member",
            "email": self.email.text().strip(),
            "create_personal_space": self.personal_space.isChecked(),
            "space_grants": [
                {
                    "space_id": space_id,
                    "capabilities": {
                        key: checks[key].isChecked()
                        for key in ("read", "upload", "modify", "delete", "share")
                    },
                }
                for space_id, checks in self.space_permissions.items()
                if checks["enabled"].isChecked()
            ],
            "prepare_access": True,
        }


class UserEditDialog(QDialog):
    def __init__(
        self,
        user: dict,
        spaces: list[dict],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Пользователь")
        self.setMinimumSize(620, 580)
        layout = QVBoxLayout(self)
        title = QLabel(f"Изменить пользователя · @{user.get('username', '')}")
        title.setObjectName("PageTitle")
        layout.addWidget(title)
        form = QFormLayout()
        self.display_name = QLineEdit(str(user.get("display_name") or ""))
        self.email = QLineEdit(str(user.get("email") or ""))
        self.quota = QSpinBox()
        self.quota.setRange(1, 1_000_000)
        self.quota.setSuffix(" ГБ")
        self.quota.setValue(max(1, int(user.get("quota_bytes", 100 * 1024**3)) // 1024**3))
        self.admin = QCheckBox("Администратор сервера")
        self.admin.setChecked(user.get("role") == "admin")
        self.enabled = QCheckBox("Учётная запись включена")
        self.enabled.setChecked(bool(user.get("enabled", True)))
        form.addRow("Имя", self.display_name)
        form.addRow("Email", self.email)
        form.addRow("Квота", self.quota)
        form.addRow("", self.admin)
        form.addRow("", self.enabled)
        layout.addLayout(form)
        grants = {
            str(item.get("space_id")): item.get("capabilities") or {}
            for item in user.get("space_grants") or []
        }
        self.space_permissions: dict[str, dict[str, QCheckBox]] = {}
        spaces_scroll = QScrollArea()
        spaces_scroll.setWidgetResizable(True)
        spaces_content = QWidget()
        spaces_layout = QVBoxLayout(spaces_content)
        for space in spaces:
            if space.get("kind") != "shared":
                continue
            space_id = str(space.get("id"))
            current = grants.get(space_id, {})
            card = QFrame()
            card.setProperty("card", True)
            card_layout = QVBoxLayout(card)
            access = QCheckBox(str(space.get("name") or "Общее пространство"))
            access.setChecked(bool(current.get("read", False)))
            card_layout.addWidget(access)
            rights = QHBoxLayout()
            checks: dict[str, QCheckBox] = {"enabled": access}
            for key, label, default in (
                ("read", "Просмотр", True),
                ("upload", "Загрузка", True),
                ("modify", "Изменение", False),
                ("delete", "Удаление", False),
                ("share", "Ссылки", False),
            ):
                check = QCheckBox(label)
                check.setChecked(bool(current.get(key, default)))
                check.setEnabled(access.isChecked())
                access.toggled.connect(check.setEnabled)
                rights.addWidget(check)
                checks[key] = check
            rights.addStretch()
            card_layout.addLayout(rights)
            spaces_layout.addWidget(card)
            self.space_permissions[space_id] = checks
        spaces_layout.addStretch()
        spaces_scroll.setWidget(spaces_content)
        layout.addWidget(spaces_scroll)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Save).setProperty("primary", True)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> tuple[dict[str, object], dict[str, dict[str, bool]]]:
        user_values: dict[str, object] = {
            "display_name": self.display_name.text().strip(),
            "email": self.email.text().strip(),
            "quota_gib": self.quota.value(),
            "role": "admin" if self.admin.isChecked() else "member",
            "enabled": self.enabled.isChecked(),
        }
        grants = {
            space_id: {
                key: checks[key].isChecked() if checks["enabled"].isChecked() else False
                for key in ("read", "upload", "modify", "delete", "share")
            }
            for space_id, checks in self.space_permissions.items()
        }
        return user_values, grants


class PasswordDialog(QDialog):
    def __init__(self, display_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Новый пароль")
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        title = QLabel(f"Изменить пароль · {display_name}")
        title.setObjectName("PageTitle")
        description = QLabel(
            "Новый пароль сразу начнёт действовать для входа на других устройствах. "
            "Текущие интернет-сессии будут отозваны, а уже подтверждённые устройства останутся подключёнными."
        )
        description.setProperty("muted", True)
        description.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(description)
        form = QFormLayout()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("Минимум 10 символов")
        self.confirmation = QLineEdit()
        self.confirmation.setEchoMode(QLineEdit.EchoMode.Password)
        self.confirmation.setPlaceholderText("Повторите пароль")
        form.addRow("Новый пароль", self.password)
        form.addRow("Повтор пароля", self.confirmation)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить пароль")
        buttons.button(QDialogButtonBox.StandardButton.Save).setProperty("primary", True)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._validate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _validate(self) -> None:
        if len(self.password.text()) < 10:
            QMessageBox.warning(
                self,
                "Слишком короткий пароль",
                "Пароль должен содержать минимум 10 символов.",
            )
            return
        if self.password.text() != self.confirmation.text():
            QMessageBox.warning(
                self,
                "Пароли не совпадают",
                "Повторно введите одинаковый пароль в оба поля.",
            )
            return
        self.accept()

    def value(self) -> str:
        return self.password.text()


class InvitationDialog(QDialog):
    cancel_requested = Signal(str)
    email_requested = Signal(str, str)

    def __init__(
        self,
        invitation_id: str,
        display_name: str,
        code: str,
        expires_at: str,
        server_url: str = "",
        certificate_fingerprint: str = "",
        username: str = "",
        password: str = "",
        access_path: str = "",
        email: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.invitation_id = invitation_id
        self.code = code
        self.server_url = server_url
        self.certificate_fingerprint = certificate_fingerprint
        self.username = username
        self.password = password
        self.access_path = access_path
        self.email = email
        self.pairing_uri = build_pairing_uri(
            code, server_url, certificate_fingerprint, username
        )
        self.setWindowTitle("Код подключения")
        self.setMinimumWidth(470)
        layout = QVBoxLayout(self)
        title = QLabel(f"Приглашение для {display_name}")
        title.setObjectName("PageTitle")
        description = QLabel(
            "Отправьте человеку этот одноразовый код или QR. После ввода устройство всё равно "
            "потребует вашего подтверждения."
        )
        description.setProperty("muted", True)
        description.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(description)

        if username:
            credentials = QFrame()
            credentials.setProperty("accent", "blue")
            credentials_layout = QFormLayout(credentials)
            username_value = QLineEdit(username)
            username_value.setReadOnly(True)
            password_value = QLineEdit(password or "пароль уже задан пользователю")
            password_value.setReadOnly(True)
            credentials_layout.addRow("Логин", username_value)
            credentials_layout.addRow("Пароль", password_value)
            copy_credentials = QPushButton("Скопировать логин и пароль")
            copy_credentials.clicked.connect(self._copy_credentials)
            credentials_layout.addRow("", copy_credentials)
            layout.addWidget(credentials)

        row = QHBoxLayout()
        qr_label = QLabel()
        qr_label.setPixmap(self._qr_pixmap(self.pairing_uri))
        qr_label.setStyleSheet("background: white; padding: 10px; border-radius: 10px;")
        row.addWidget(qr_label)
        code_box = QVBoxLayout()
        code_label = QLabel(code)
        code_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        code_label.setStyleSheet(
            "font-family: Consolas; font-size: 30px; font-weight: 700; letter-spacing: 3px;"
        )
        expires = QLabel(f"Действует до: {self._format_expiry(expires_at)}")
        expires.setProperty("muted", True)
        expires.setAlignment(Qt.AlignmentFlag.AlignCenter)
        copy = QPushButton("Копировать код")
        copy.clicked.connect(self._copy)
        code_box.addStretch()
        code_box.addWidget(code_label)
        code_box.addWidget(expires)
        code_box.addWidget(copy)
        if self.server_url:
            copy_invitation = QPushButton("Копировать полное приглашение")
            copy_invitation.setProperty("primary", True)
            copy_invitation.clicked.connect(self._copy_invitation)
            code_box.addWidget(copy_invitation)
        code_box.addStretch()
        row.addLayout(code_box, 1)
        layout.addLayout(row)

        if self.server_url:
            connection = QLabel(
                f"Адрес: {self.server_url}\nTLS SHA-256: {self._short_fingerprint()}"
            )
            connection.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            connection.setWordWrap(True)
            connection.setProperty("muted", True)
            layout.addWidget(connection)

        warning = QLabel(
            "Код одноразовый. Он перестанет работать после первого использования или истечения времени."
        )
        warning.setWordWrap(True)
        warning.setProperty("muted", True)
        layout.addWidget(warning)
        controls = QHBoxLayout()
        if self.access_path:
            save_access = QPushButton("Сохранить файл входа")
            save_access.clicked.connect(self._save_access_file)
            controls.addWidget(save_access)
        if self.email:
            send_email = QPushButton("Отправить файл на почту")
            send_email.clicked.connect(
                lambda: self.email_requested.emit(self.invitation_id, self.email)
            )
            controls.addWidget(send_email)
        cancel = QPushButton("Отменить приглашение")
        cancel.clicked.connect(self._cancel_invitation)
        close = QPushButton("Готово")
        close.setProperty("primary", True)
        close.clicked.connect(self.accept)
        controls.addWidget(cancel)
        controls.addStretch()
        controls.addWidget(close)
        layout.addLayout(controls)

    def _copy(self) -> None:
        QApplication.clipboard().setText(self.code)

    def _copy_invitation(self) -> None:
        QApplication.clipboard().setText(self.pairing_uri)

    def _copy_credentials(self) -> None:
        password = self.password or "(пароль, который вы задали ранее)"
        QApplication.clipboard().setText(
            f"Логин: {self.username}\nПароль: {password}"
        )

    def _save_access_file(self) -> None:
        source = Path(self.access_path)
        if not source.is_file():
            QMessageBox.warning(self, "Файл не найден", "Создайте новый файл доступа.")
            return
        selected, _ = QFileDialog.getSaveFileName(
            self,
            "Сохранить файл входа",
            f"cloud-storage-{self.username or 'access'}.cloud-access.json",
            "Cloud Storage access (*.cloud-access.json)",
        )
        if not selected:
            return
        try:
            shutil.copy2(source, selected)
        except OSError as exc:
            QMessageBox.warning(self, "Файл не сохранён", str(exc))
            return
        QMessageBox.information(self, "Файл сохранён", selected)

    def _short_fingerprint(self) -> str:
        normalized = self.certificate_fingerprint.replace(":", "").upper()
        display = ":".join(normalized[index : index + 2] for index in range(0, len(normalized), 2))
        return display

    def _cancel_invitation(self) -> None:
        response = QMessageBox.question(
            self,
            "Отменить приглашение?",
            "Этот код сразу перестанет работать.",
        )
        if response == QMessageBox.StandardButton.Yes:
            self.cancel_requested.emit(self.invitation_id)
            self.accept()

    @staticmethod
    def _format_expiry(value: str) -> str:
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if moment.tzinfo is not None:
                moment = moment.astimezone()
            return moment.strftime("%d.%m.%Y в %H:%M")
        except ValueError:
            return value

    @staticmethod
    def _qr_pixmap(payload: str) -> QPixmap:
        qr = qrcode.QRCode(version=None, box_size=6, border=2)
        qr.add_data(payload)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        size = len(matrix)
        image = QImage(size, size, QImage.Format.Format_RGB32)
        white = QColor("white").rgb()
        black = QColor("black").rgb()
        for y, row in enumerate(matrix):
            for x, enabled in enumerate(row):
                image.setPixel(x, y, black if enabled else white)
        return QPixmap.fromImage(image).scaled(
            180,
            180,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
