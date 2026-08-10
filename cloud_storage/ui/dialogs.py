from __future__ import annotations

from datetime import UTC, datetime

import qrcode
from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
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
    refresh_requested = Signal()
    cleanup_requested = Signal(str)

    def __init__(
        self,
        disk: DiskSnapshot,
        configuration: DiskConfiguration,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.disk = disk
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
            explorer = QPushButton("Открыть папку")
            explorer.clicked.connect(
                lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(disk.mountpoint))
            )
            header.addWidget(explorer)
        root.addLayout(header)

        tabs = QTabWidget()
        self.tabs = tabs
        tabs.addTab(self._overview_tab(), "Обзор")
        tabs.addTab(self._configuration_tab(configuration), "Настройка")
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
        for role, label in ROLE_LABELS.items():
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
        check = QPushButton("Проверить")
        check.clicked.connect(self.refresh_requested)
        clean = QPushButton("Очистить временное")
        clean.clicked.connect(lambda: self.cleanup_requested.emit(self.disk.id))
        reassign = QPushButton("Переназначить")
        reassign.clicked.connect(lambda: self.tabs.setCurrentIndex(1))
        for button in (stop, maintenance, ignore, check, clean, reassign):
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)

        safety = QFrame()
        safety.setProperty("accent", "red")
        safety_layout = QVBoxLayout(safety)
        safety_title = QLabel("Опасные операции заблокированы")
        safety_title.setStyleSheet("font-weight: 700;")
        safety_body = QLabel(
            "Форматирование, удаление разделов и физическое отключение не вызываются. "
            "Перенос работает только внутри управляемых каталогов, после явного подтверждения "
            "и с проверкой SHA-256."
        )
        safety_body.setWordWrap(True)
        safety_body.setProperty("muted", True)
        safety_layout.addWidget(safety_title)
        safety_layout.addWidget(safety_body)
        layout.addWidget(safety)

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
        )
        self.configuration_saved.emit(self.disk.id, configuration)
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


class CreateUserDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Новый пользователь")
        self.setMinimumWidth(480)
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
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("Минимум 10 символов")
        self.password_confirmation = QLineEdit()
        self.password_confirmation.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_confirmation.setPlaceholderText("Повторите пароль")
        self.quota = QSpinBox()
        self.quota.setRange(1, 1_000_000)
        self.quota.setValue(100)
        self.quota.setSuffix(" ГБ")
        self.admin = QCheckBox("Администратор сервера")
        form.addRow("Логин", self.username)
        form.addRow("Имя", self.display_name)
        form.addRow("Пароль", self.password)
        form.addRow("Повтор пароля", self.password_confirmation)
        form.addRow("Личное хранилище", self.quota)
        form.addRow("Email для файла входа", self.email)
        form.addRow("", self.admin)
        layout.addLayout(form)
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
        self.accept()

    def values(self) -> dict[str, object]:
        return {
            "username": self.username.text().strip(),
            "display_name": self.display_name.text().strip(),
            "password": self.password.text(),
            "quota_gib": self.quota.value(),
            "role": "admin" if self.admin.isChecked() else "member",
            "email": self.email.text().strip(),
            "prepare_access": True,
        }


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

    def __init__(
        self,
        invitation_id: str,
        display_name: str,
        code: str,
        expires_at: str,
        server_url: str = "",
        certificate_fingerprint: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.invitation_id = invitation_id
        self.code = code
        self.server_url = server_url
        self.certificate_fingerprint = certificate_fingerprint
        self.pairing_uri = build_pairing_uri(code, server_url, certificate_fingerprint)
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
