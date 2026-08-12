from __future__ import annotations

import hashlib
import json
import platform
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from cloud_storage import __version__
from cloud_storage.client.api_client import (
    CertificateMismatch,
    ClientApi,
    ClientApiError,
    ClientConnectionError,
    TransferInterrupted,
)
from cloud_storage.client.discovery import DiscoveredServer, discover_servers
from cloud_storage.client.drive import DriveManager, wait_for_drive
from cloud_storage.client.integration import (
    autostart_enabled,
    integrate_file_manager,
    set_autostart,
)
from cloud_storage.client.offline import OfflineRecord, OfflineStore
from cloud_storage.client.settings import (
    ClientProfile,
    ClientSettingsStore,
    DeviceTokenVault,
    RemoteSessionVault,
    validate_server_url,
)
from cloud_storage.client.transfers import TransferRecord, TransferStore
from cloud_storage.help.knowledge import KnowledgeBase
from cloud_storage.help.page import HelpPage
from cloud_storage.pairing import parse_pairing_uri
from cloud_storage.ui.theme import create_app_icon
from cloud_storage.ui.update_page import UpdatePage
from cloud_storage.ui.widgets import clear_layout, format_bytes, make_header


class WorkerSignals(QObject):
    success = Signal(object)
    failure = Signal(str)
    progress = Signal(int, int)
    finished = Signal()


class BackgroundTask(QRunnable):
    def __init__(
        self,
        function: Callable[..., Any],
        *arguments: Any,
        with_progress: bool = False,
    ) -> None:
        super().__init__()
        self.function = function
        self.arguments = arguments
        self.with_progress = with_progress
        self.signals = WorkerSignals()

    def run(self) -> None:
        try:
            if self.with_progress:
                result = self.function(*self.arguments, progress=self.signals.progress.emit)
            else:
                result = self.function(*self.arguments)
            self.signals.success.emit(result)
        except (ClientApiError, ClientConnectionError, OSError, ValueError) as exc:
            self.signals.failure.emit(str(exc))
        except Exception as exc:  # UI worker boundary
            self.signals.failure.emit(f"Непредвиденная ошибка: {exc}")
        finally:
            self.signals.finished.emit()


class ClientWindow(QMainWindow):
    def __init__(self, store: ClientSettingsStore | None = None) -> None:
        super().__init__()
        self.store = store or ClientSettingsStore()
        self.profile: ClientProfile = self.store.load()
        self.vault = DeviceTokenVault(self.store.data_directory, self.profile.profile_id)
        self.session_vault = RemoteSessionVault(self.store.data_directory, self.profile.profile_id)
        self.knowledge = KnowledgeBase(self.store.data_directory / "knowledge.db")
        self.transfer_store = TransferStore(self.store.data_directory / "transfers.db")
        self.offline_store = OfflineStore(self.store.data_directory / "offline.db")
        self.drive_manager = DriveManager(self.store.data_directory)
        self.transfer_store.recover_interrupted()
        self.token = self.vault.load()
        self.remote_session = self.session_vault.load()
        self.api: ClientApi | None = None
        self.spaces: list[dict[str, Any]] = []
        self.entries: list[dict[str, Any]] = []
        self.current_directory = ""
        self._workers: set[BackgroundTask] = set()
        self._active_transfer_ids: set[str] = set()
        self._transfer_widgets: dict[str, dict[str, QWidget]] = {}
        self._transfer_retry_after: dict[str, float] = {}
        self._open_after_transfer_ids: set[str] = set()
        self._connection_check_running = False
        self._profile_generation = 0
        self._offline_scan_running = False
        self._quit_requested = False
        self._shutdown_prepared = False
        self._tray_notice_shown = False
        self.tray_icon: QSystemTrayIcon | None = None

        self.setWindowTitle(f"Cloud Storage Client · {__version__}")
        self.setMinimumSize(1040, 700)
        self.resize(1320, 820)
        self._build_ui()
        self._setup_tray()
        self._load_profile()
        self._refresh_transfer_cards()
        self._render_offline_records(self.offline_store.list())

        self.reconnect_timer = QTimer(self)
        self.reconnect_timer.setInterval(10_000)
        self.reconnect_timer.timeout.connect(self.refresh_connection)
        self.reconnect_timer.start()
        self.transfer_timer = QTimer(self)
        self.transfer_timer.setInterval(500)
        self.transfer_timer.timeout.connect(self._transfer_tick)
        self.transfer_timer.start()
        self.drive_timer = QTimer(self)
        self.drive_timer.setInterval(5_000)
        self.drive_timer.timeout.connect(self._reconcile_drives)
        self.drive_timer.start()
        QTimer.singleShot(100, self.refresh_connection)
        QTimer.singleShot(250, self._reconcile_drives)

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("AppRoot")
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(244)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(18, 22, 18, 20)
        mark_row = QHBoxLayout()
        mark = QLabel("CS")
        mark.setObjectName("BrandMark")
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand_box = QVBoxLayout()
        brand = QLabel("CLOUD STORAGE")
        brand.setObjectName("Brand")
        edition = QLabel(f"DESKTOP CLIENT · {__version__}")
        edition.setProperty("muted", True)
        edition.setStyleSheet("font-size: 10px;")
        brand_box.addWidget(brand)
        brand_box.addWidget(edition)
        mark_row.addWidget(mark)
        mark_row.addLayout(brand_box, 1)
        side.addLayout(mark_row)
        side.addSpacing(26)

        self.stack = QStackedWidget()
        self.connection_page = self._make_connection_page()
        self.files_page = self._make_files_page()
        self.transfers_page = self._make_transfers_page()
        self.offline_page = self._make_offline_page()
        self.update_page = UpdatePage("client", self.store.data_directory)
        self.help_page = HelpPage(self.knowledge, "client")
        self.nav_buttons: list[QPushButton] = []
        for label, page in (
            ("⌁   Подключиться", self.connection_page),
            ("▣   Мои файлы", self.files_page),
            ("⇅   Передачи", self.transfers_page),
            ("◫   Офлайн", self.offline_page),
            ("↻   Обновления", self.update_page),
            ("?   Помощь", self.help_page),
        ):
            button = QPushButton(label)
            button.setProperty("nav", True)
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, target=page: self._show_page(target))
            side.addWidget(button)
            self.nav_buttons.append(button)
            self.stack.addWidget(page)
        self.nav_buttons[0].setChecked(True)
        side.addStretch()

        state_card = QFrame()
        state_card.setProperty("card", True)
        state_layout = QVBoxLayout(state_card)
        self.sidebar_state = QLabel("НЕ ПОДКЛЮЧЕНО")
        self.sidebar_state.setStyleSheet("font-weight: 700; font-size: 11px; color: #949ca8;")
        self.sidebar_server = QLabel("Укажите сервер и войдите")
        self.sidebar_server.setWordWrap(True)
        self.sidebar_server.setProperty("muted", True)
        state_layout.addWidget(self.sidebar_state)
        state_layout.addWidget(self.sidebar_server)
        side.addWidget(state_card)
        outer.addWidget(sidebar)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(22, 20, 16, 14)
        content_layout.addWidget(self.stack)
        outer.addWidget(content, 1)

    def _make_connection_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 16, 24)
        layout.setSpacing(18)
        layout.addWidget(
            make_header(
                "Подключиться",
                "Вставьте единый код из Server Manager — адрес и защита сервера настроятся автоматически.",
            )
        )

        state = QFrame()
        state.setProperty("accent", "blue")
        state_layout = QVBoxLayout(state)
        self.connection_title = QLabel("Клиент не подключён")
        self.connection_title.setStyleSheet("font-weight: 700; font-size: 18px;")
        self.connection_detail = QLabel(
            "Получите код вида CS1.… у администратора, вставьте его ниже, задайте пароль "
            "и название этого компьютера. Ручной адрес нужен только для старых серверов."
        )
        self.connection_detail.setWordWrap(True)
        self.connection_detail.setProperty("muted", True)
        state_layout.addWidget(self.connection_title)
        state_layout.addWidget(self.connection_detail)
        layout.addWidget(state)

        form_card = QFrame()
        form_card.setProperty("card", True)
        card_layout = QVBoxLayout(form_card)
        card_layout.setContentsMargins(18, 18, 18, 18)
        form = QFormLayout()
        form.setVerticalSpacing(12)
        self.server_selector = QComboBox()
        self.server_selector.setMinimumWidth(280)
        self.server_selector.currentIndexChanged.connect(self.switch_server)
        profile_row = QHBoxLayout()
        profile_row.addWidget(self.server_selector, 1)
        self.add_server_button = QPushButton("Добавить сервер")
        self.add_server_button.clicked.connect(self.add_server)
        self.remove_server_button = QPushButton("Удалить сервер")
        self.remove_server_button.clicked.connect(self.remove_server)
        profile_row.addWidget(self.add_server_button)
        profile_row.addWidget(self.remove_server_button)
        self.server_url = QLineEdit()
        self.server_url.setPlaceholderText("http://127.0.0.1:8765")
        address_row = QHBoxLayout()
        address_row.addWidget(self.server_url, 1)
        self.discover_button = QPushButton("Найти в сети")
        self.discover_button.clicked.connect(self.discover_lan_servers)
        address_row.addWidget(self.discover_button)
        self.fingerprint = QLineEdit()
        self.fingerprint.setPlaceholderText("SHA-256 сертификата — для HTTPS")
        self.pairing_code = QLineEdit()
        self.pairing_code.setPlaceholderText("Вставьте код CS1.… из Server Manager")
        self.pairing_code.setMaxLength(4096)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("Минимум 10 символов")
        self.device_name = QLineEdit(platform.node() or "Мой компьютер")
        self.username = QLineEdit()
        self.username.setPlaceholderText("Логин, созданный администратором")
        form.addRow("Код подключения", self.pairing_code)
        form.addRow("Пароль", self.password)
        form.addRow("Название устройства", self.device_name)
        form.addRow("Логин", self.username)
        form.addRow("Сохранённые серверы", profile_row)
        form.addRow("Ручной адрес (для старых версий)", address_row)
        form.addRow("Ручной отпечаток TLS", self.fingerprint)
        card_layout.addLayout(form)
        enrollment_controls = QHBoxLayout()
        self.account_login_button = QPushButton("Войти по логину")
        self.account_login_button.clicked.connect(self.login_new_device)
        self.connect_button = QPushButton("Подключиться по коду")
        self.connect_button.setProperty("primary", True)
        self.connect_button.clicked.connect(self.connect_device)
        self.import_access_button = QPushButton("Импортировать файл входа")
        self.import_access_button.clicked.connect(self.import_access_file)
        enrollment_controls.addWidget(self.account_login_button)
        enrollment_controls.addWidget(self.connect_button)
        enrollment_controls.addWidget(self.import_access_button)
        enrollment_controls.addStretch()
        card_layout.addLayout(enrollment_controls)
        controls = QHBoxLayout()
        self.status_button = QPushButton("Проверить подтверждение")
        self.status_button.clicked.connect(self.refresh_connection)
        self.remote_login_button = QPushButton("Войти через интернет")
        self.remote_login_button.clicked.connect(self.login_remote)
        self.forget_button = QPushButton("Забыть подключение")
        self.forget_button.clicked.connect(self.forget_connection)
        controls.addWidget(self.status_button)
        controls.addWidget(self.remote_login_button)
        controls.addWidget(self.forget_button)
        controls.addStretch()
        card_layout.addLayout(controls)
        layout.addWidget(form_card)
        layout.addStretch()
        return page

    def _make_files_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 16, 24)
        layout.setSpacing(14)
        layout.addWidget(
            make_header("Мои файлы", "Логические пространства сервера без физических дисков.")
        )
        toolbar = QHBoxLayout()
        self.space_selector = QComboBox()
        self.space_selector.setMinimumWidth(220)
        self.space_selector.currentIndexChanged.connect(self._space_changed)
        self.up_button = QPushButton("← Выше")
        self.up_button.clicked.connect(self.go_up)
        self.path_label = QLabel("/")
        self.path_label.setProperty("muted", True)
        refresh = QPushButton("Обновить")
        refresh.clicked.connect(self.refresh_entries)
        upload = QPushButton("Загрузить файл")
        upload.setProperty("primary", True)
        upload.clicked.connect(self.choose_upload)
        toolbar.addWidget(self.space_selector)
        toolbar.addWidget(self.up_button)
        toolbar.addWidget(self.path_label, 1)
        toolbar.addWidget(refresh)
        toolbar.addWidget(upload)
        layout.addLayout(toolbar)

        self.files_table = QTableWidget(0, 4)
        self.files_table.setHorizontalHeaderLabels(["Имя", "Тип", "Размер", "Изменён"])
        self.files_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.files_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.files_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.files_table.verticalHeader().setVisible(False)
        self.files_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.files_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.files_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        self.files_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )
        self.files_table.doubleClicked.connect(self.open_selected)
        self.files_empty = QLabel(
            "Файлы появятся после подключения и выбора личного пространства. "
            "Если папка пуста, загрузите первый файл кнопкой выше."
        )
        self.files_empty.setWordWrap(True)
        self.files_empty.setProperty("emptyState", True)
        layout.addWidget(self.files_empty)
        layout.addWidget(self.files_table, 1)
        actions = QHBoxLayout()
        open_button = QPushButton("Открыть")
        open_button.clicked.connect(self.open_selected)
        cache_button = QPushButton("Скачать для офлайн-доступа")
        cache_button.clicked.connect(self.download_selected)
        delete_button = QPushButton("Удалить")
        delete_button.clicked.connect(self.delete_selected)
        actions.addWidget(open_button)
        actions.addWidget(cache_button)
        actions.addWidget(delete_button)
        actions.addStretch()
        layout.addLayout(actions)
        return page

    def _make_transfers_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 16, 24)
        layout.addWidget(
            make_header(
                "Передачи",
                "Очередь сохраняется и продолжает работу после обрыва или перезапуска.",
            )
        )
        toolbar = QHBoxLayout()
        self.transfer_summary = QLabel("Очередь пуста")
        self.transfer_summary.setProperty("muted", True)
        clear_finished = QPushButton("Очистить завершённые")
        clear_finished.clicked.connect(self._clear_finished_transfers)
        toolbar.addWidget(self.transfer_summary, 1)
        toolbar.addWidget(clear_finished)
        layout.addLayout(toolbar)
        self.transfer_rows = QVBoxLayout()
        layout.addLayout(self.transfer_rows)
        transfer_help = QLabel(
            "Здесь появятся загрузки и скачивания, включая прогресс, паузу и повтор после ошибки. "
            "Начните передачу во вкладке «Мои файлы» — очередь сохранится после перезапуска."
        )
        transfer_help.setWordWrap(True)
        transfer_help.setProperty("emptyState", True)
        layout.addWidget(transfer_help)
        layout.addStretch()
        return page

    def _make_offline_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 16, 24)
        layout.setSpacing(16)
        layout.addWidget(
            make_header(
                "Офлайн-доступ",
                "Локальные копии доступны без соединения; изменения сверяются по SHA-256.",
            )
        )
        card = QFrame()
        card.setProperty("card", True)
        card_layout = QVBoxLayout(card)
        title = QLabel("Каталог локального кэша")
        title.setStyleSheet("font-weight: 700; font-size: 17px;")
        self.cache_path = QLabel("—")
        self.cache_path.setProperty("muted", True)
        self.cache_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        buttons = QHBoxLayout()
        choose = QPushButton("Изменить каталог")
        choose.clicked.connect(self.choose_cache_directory)
        open_cache = QPushButton("Открыть каталог")
        open_cache.clicked.connect(self.open_cache_directory)
        self.offline_refresh_button = QPushButton("Проверить изменения")
        self.offline_refresh_button.clicked.connect(self.refresh_offline_index)
        self.file_manager_button = QPushButton("Добавить в Проводник / файлы")
        self.file_manager_button.clicked.connect(self.integrate_offline_directory)
        buttons.addWidget(choose)
        buttons.addWidget(open_cache)
        buttons.addWidget(self.offline_refresh_button)
        buttons.addWidget(self.file_manager_button)
        buttons.addStretch()
        card_layout.addWidget(title)
        card_layout.addWidget(self.cache_path)
        card_layout.addLayout(buttons)
        system_options = QHBoxLayout()
        self.autostart_checkbox = QCheckBox("Запускать вместе с системой")
        self.autostart_checkbox.toggled.connect(self._autostart_toggled)
        self.close_to_tray_checkbox = QCheckBox("Сворачивать в трей при закрытии")
        self.close_to_tray_checkbox.toggled.connect(self._close_to_tray_toggled)
        system_options.addWidget(self.autostart_checkbox)
        system_options.addWidget(self.close_to_tray_checkbox)
        system_options.addStretch()
        card_layout.addLayout(system_options)
        self.integration_status = QLabel(
            "Каталог можно закрепить в Проводнике Windows или файловом менеджере Linux."
        )
        self.integration_status.setProperty("muted", True)
        self.integration_status.setWordWrap(True)
        card_layout.addWidget(self.integration_status)
        layout.addWidget(card)

        drive_card = QFrame()
        drive_card.setProperty("card", True)
        drive_layout = QVBoxLayout(drive_card)
        drive_title = QLabel("Диск в Проводнике Windows")
        drive_title.setStyleSheet("font-weight: 700; font-size: 17px;")
        drive_description = QLabel(
            "Файлы видны как обычный диск, скачиваются при открытии и отправляются на сервер "
            "после сохранения. Для каждого сервера используется отдельная буква."
        )
        drive_description.setProperty("muted", True)
        drive_description.setWordWrap(True)
        drive_controls = QHBoxLayout()
        self.drive_enabled_checkbox = QCheckBox("Подключать автоматически")
        self.drive_enabled_checkbox.toggled.connect(self._drive_settings_changed)
        self.drive_letter_selector = QComboBox()
        for letter in "STUVWXYZRQPONMLKJIHGFED":
            self.drive_letter_selector.addItem(f"Диск {letter}:", letter)
        self.drive_letter_selector.currentIndexChanged.connect(self._drive_settings_changed)
        self.drive_open_button = QPushButton("Открыть диск")
        self.drive_open_button.clicked.connect(self.open_windows_drive)
        drive_controls.addWidget(self.drive_enabled_checkbox)
        drive_controls.addWidget(self.drive_letter_selector)
        drive_controls.addWidget(self.drive_open_button)
        drive_controls.addStretch()
        self.drive_status = QLabel("Диск ещё не подключён")
        self.drive_status.setProperty("muted", True)
        self.drive_status.setWordWrap(True)
        drive_layout.addWidget(drive_title)
        drive_layout.addWidget(drive_description)
        drive_layout.addLayout(drive_controls)
        drive_layout.addWidget(self.drive_status)
        layout.addWidget(drive_card)
        self.offline_summary = QLabel("Индекс офлайн-файлов пуст")
        self.offline_summary.setProperty("muted", True)
        layout.addWidget(self.offline_summary)
        self.offline_table = QTableWidget(0, 4)
        self.offline_table.setHorizontalHeaderLabels(["Файл", "Состояние", "Размер", "Сервер"])
        self.offline_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.offline_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.offline_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.offline_table.verticalHeader().setVisible(False)
        self.offline_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.offline_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.offline_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        self.offline_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )
        self.offline_table.itemSelectionChanged.connect(self._update_offline_actions)
        self.offline_table.doubleClicked.connect(self.open_offline_selected)
        layout.addWidget(self.offline_table, 1)
        actions = QHBoxLayout()
        self.offline_open_button = QPushButton("Открыть")
        self.offline_open_button.clicked.connect(self.open_offline_selected)
        self.offline_upload_button = QPushButton("Отправить локальную версию")
        self.offline_upload_button.clicked.connect(self.upload_offline_selected)
        self.offline_download_button = QPushButton("Скачать серверную версию")
        self.offline_download_button.clicked.connect(self.download_offline_selected)
        self.offline_remove_button = QPushButton("Убрать из индекса")
        self.offline_remove_button.clicked.connect(self.remove_offline_selected)
        actions.addWidget(self.offline_open_button)
        actions.addWidget(self.offline_upload_button)
        actions.addWidget(self.offline_download_button)
        actions.addWidget(self.offline_remove_button)
        actions.addStretch()
        layout.addLayout(actions)
        self._update_offline_actions()
        return page

    def import_access_file(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Импортировать файл доступа Cloud Storage",
            "",
            "Cloud Storage access (*.cloud-access.json *.json)",
        )
        if not selected:
            return
        path = Path(selected)
        try:
            if path.stat().st_size > 64 * 1024:
                raise ValueError("файл доступа слишком большой")
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("format") != "cloud-storage-access-v1":
                raise ValueError("неподдерживаемый формат файла доступа")
            addresses = value.get("addresses")
            if not isinstance(addresses, list) or not addresses:
                raise ValueError("в файле нет адреса сервера")
            address = str(addresses[0]).strip()
            code = str(value.get("one_time_code", "")).strip()
            username = str(value.get("username", "")).strip()
            fingerprint = str(value.get("certificate_fingerprint", "")).strip()
            if not address or not code or not username:
                raise ValueError("в файле не хватает данных входа")
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            QMessageBox.warning(self, "Файл не импортирован", str(exc))
            return
        self.server_url.setText(address)
        self.pairing_code.setText(code)
        self.username.setText(username)
        self.fingerprint.setText(fingerprint)
        QMessageBox.information(
            self,
            "Данные входа загружены",
            "Адрес, логин и одноразовый код заполнены. Введите новый пароль для "
            "этого устройства и нажмите «Подключиться по коду».",
        )

    def _load_profile(self) -> None:
        self._refresh_server_selector()
        self.server_url.setText(self.profile.server_url)
        self.fingerprint.setText(self.profile.certificate_fingerprint)
        self.device_name.setText(self.profile.device_name or platform.node() or "Мой компьютер")
        self.username.setText(self.profile.username)
        self.cache_path.setText(self.profile.download_directory)
        self.drive_enabled_checkbox.blockSignals(True)
        self.drive_enabled_checkbox.setChecked(self.profile.drive_enabled)
        self.drive_enabled_checkbox.blockSignals(False)
        self.drive_letter_selector.blockSignals(True)
        index = self.drive_letter_selector.findData(self.profile.drive_letter)
        self.drive_letter_selector.setCurrentIndex(max(0, index))
        self.drive_letter_selector.blockSignals(False)
        supported = platform.system() == "Windows"
        self.drive_enabled_checkbox.setEnabled(supported)
        self.drive_letter_selector.setEnabled(supported and self.profile.drive_enabled)
        self.drive_open_button.setEnabled(False)
        self.close_to_tray_checkbox.blockSignals(True)
        self.close_to_tray_checkbox.setChecked(self.profile.close_to_tray)
        self.close_to_tray_checkbox.blockSignals(False)
        self.autostart_checkbox.blockSignals(True)
        try:
            self.autostart_checkbox.setChecked(autostart_enabled())
        except OSError:
            self.autostart_checkbox.setChecked(False)
        self.autostart_checkbox.blockSignals(False)
        self._set_connection_state(self.profile.device_status)
        self.status_button.setEnabled(bool(self.token))
        self.forget_button.setEnabled(bool(self.token))
        self.remote_login_button.setEnabled(bool(self.token))

    def _refresh_server_selector(self) -> None:
        profiles = self.store.list_profiles()
        self.server_selector.blockSignals(True)
        self.server_selector.clear()
        selected = 0
        for index, profile in enumerate(profiles):
            name = profile.server_name.strip() or "Сервер"
            self.server_selector.addItem(f"{name} — {profile.server_url}", profile.profile_id)
            if profile.profile_id == self.profile.profile_id:
                selected = index
        self.server_selector.setCurrentIndex(selected)
        self.server_selector.blockSignals(False)

    def _activate_profile(self, profile: ClientProfile) -> None:
        self._profile_generation += 1
        self._connection_check_running = False
        self.profile = profile
        self.vault = DeviceTokenVault(self.store.data_directory, profile.profile_id)
        self.session_vault = RemoteSessionVault(self.store.data_directory, profile.profile_id)
        self.token = self.vault.load()
        self.remote_session = self.session_vault.load()
        self.api = None
        self.current_directory = ""
        self.password.clear()
        self.pairing_code.clear()
        self.connect_button.setEnabled(True)
        self.account_login_button.setEnabled(True)
        self._set_spaces([])
        self._load_profile()
        if self.token:
            QTimer.singleShot(0, self.refresh_connection)

    def switch_server(self, index: int) -> None:
        if index < 0:
            return
        profile_id = str(self.server_selector.itemData(index) or "")
        if not profile_id or profile_id == self.profile.profile_id:
            return
        try:
            profile = self.store.set_active(profile_id)
        except KeyError:
            self._refresh_server_selector()
            return
        self._activate_profile(profile)

    def add_server(self) -> None:
        profile = self.store.add_profile()
        self._activate_profile(profile)
        self.server_url.setFocus()

    def remove_server(self) -> None:
        response = QMessageBox.question(
            self,
            "Удалить сохранённый сервер?",
            "Будет удалён только локальный профиль и его токен. Файлы на сервере и локальные "
            "офлайн-копии останутся на месте.",
        )
        if response != QMessageBox.StandardButton.Yes:
            return
        removed_url = self.profile.server_url.rstrip("/")
        for transfer in self.transfer_store.list():
            if transfer.server_url.rstrip("/") == removed_url and transfer.status in {
                "queued",
                "running",
            }:
                self.transfer_store.set_status(
                    transfer.id,
                    "paused",
                    "Профиль сервера удалён — передача приостановлена",
                )
        removed_profile_id = self.profile.profile_id
        self.drive_manager.stop(removed_profile_id)
        self.vault.clear()
        self.session_vault.clear()
        profile = self.store.remove_profile(self.profile.profile_id)
        self._activate_profile(profile)
        self._refresh_transfer_cards()

    def connect_device(self) -> None:
        raw_code = self.pairing_code.text().strip()
        try:
            invitation = parse_pairing_uri(raw_code)
        except ValueError as exc:
            QMessageBox.warning(self, "Неверное приглашение", str(exc))
            return
        if invitation is not None:
            if invitation.server_url:
                self.server_url.setText(invitation.server_url)
            if invitation.certificate_fingerprint:
                self.fingerprint.setText(invitation.certificate_fingerprint)
            if invitation.username:
                self.username.setText(invitation.username)
            raw_code = invitation.code
        try:
            server_url = validate_server_url(self.server_url.text())
            api = ClientApi(server_url, certificate_fingerprint=self.fingerprint.text())
        except ValueError as exc:
            QMessageBox.warning(self, "Неверные параметры подключения", str(exc))
            return
        code = raw_code
        password = self.password.text()
        device_name = self.device_name.text().strip()
        if len(code.replace("-", "")) != 8 or len(password) < 10 or not device_name:
            QMessageBox.warning(
                self,
                "Проверьте данные",
                "Нужны восьмизначный код, пароль минимум из 10 символов и название устройства.",
            )
            return
        self.connect_button.setEnabled(False)
        self.connection_detail.setText("Проверяем сервер и отправляем запрос на подключение…")
        profile_id = self.profile.profile_id

        def pair() -> dict[str, Any]:
            health = api.health()
            result = api.redeem_invitation(code, password, device_name, platform.system())
            return {"health": health, "pairing": result, "profile_id": profile_id}

        self._start_task(
            pair,
            self._pairing_complete,
            lambda message: self._connection_failed_for(profile_id, message),
        )

    def login_new_device(self) -> None:
        username = self.username.text().strip().casefold()
        password = self.password.text()
        device_name = self.device_name.text().strip()
        if len(username) < 3 or len(password) < 10 or not device_name:
            QMessageBox.warning(
                self,
                "Проверьте данные",
                "Нужны логин, пароль минимум из 10 символов и название устройства.",
            )
            return
        try:
            server_url = validate_server_url(self.server_url.text())
            api = ClientApi(server_url, certificate_fingerprint=self.fingerprint.text())
        except ValueError as exc:
            QMessageBox.warning(self, "Неверные параметры подключения", str(exc))
            return
        self.account_login_button.setEnabled(False)
        self.connection_detail.setText(
            "Проверяем логин и отправляем запрос на подтверждение устройства…"
        )
        profile_id = self.profile.profile_id

        def login() -> dict[str, Any]:
            health = api.health()
            result = api.login_new_device(username, password, device_name, platform.system())
            return {"health": health, "pairing": result, "profile_id": profile_id}

        self._start_task(
            login,
            self._pairing_complete,
            lambda message: self._connection_failed_for(profile_id, message),
        )

    def _pairing_complete(self, result: object) -> None:
        payload = result if isinstance(result, dict) else {}
        if payload.get("profile_id") != self.profile.profile_id:
            return
        pairing = payload.get("pairing", {})
        device = pairing.get("device", {})
        token = pairing.get("device_token", "")
        try:
            self.vault.store(token)
        except (OSError, ValueError) as exc:
            self.connect_button.setEnabled(True)
            self.account_login_button.setEnabled(True)
            QMessageBox.critical(self, "Токен не сохранён", str(exc))
            return
        self.token = token
        self.profile.server_url = self.server_url.text().strip().rstrip("/")
        self.profile.certificate_fingerprint = self.fingerprint.text().strip()
        health = payload.get("health", {})
        self.profile.server_name = str(health.get("server_name") or "Домашнее облако")
        self.profile.device_id = str(device.get("id", ""))
        self.profile.device_name = self.device_name.text().strip()
        self.profile.device_status = str(device.get("status", "pending"))
        self.profile.username = str(device.get("username", ""))
        self.store.save(self.profile)
        self._refresh_server_selector()
        self.password.clear()
        self.pairing_code.clear()
        self.connect_button.setEnabled(True)
        self.account_login_button.setEnabled(True)
        self.status_button.setEnabled(True)
        self.forget_button.setEnabled(True)
        self.remote_login_button.setEnabled(True)
        self._set_connection_state("pending")
        QMessageBox.information(
            self,
            "Запрос отправлен",
            "Теперь администратор должен подтвердить устройство в Server Manager.",
        )

    def discover_lan_servers(self) -> None:
        self.discover_button.setEnabled(False)
        self.connection_detail.setText("Ищем Cloud Storage Server в локальной сети…")
        self._start_task(
            discover_servers,
            self._discovery_complete,
            self._discovery_failed,
        )

    def _discovery_complete(self, result: object) -> None:
        self.discover_button.setEnabled(True)
        servers = result if isinstance(result, list) else []
        servers = [item for item in servers if isinstance(item, DiscoveredServer)]
        if not servers:
            self.connection_detail.setText(
                "Сервер не найден. Проверьте, что LAN-доступ включён и разрешён в частной сети Windows."
            )
            return
        selected = servers[0]
        if len(servers) > 1:
            labels = [f"{item.server_name} — {item.url}" for item in servers]
            label, accepted = QInputDialog.getItem(
                self,
                "Найденные серверы",
                "Выберите сервер:",
                labels,
                0,
                False,
            )
            if not accepted:
                return
            selected = servers[labels.index(label)]
        self.server_url.setText(selected.url)
        self.fingerprint.setText(selected.fingerprint)
        self.profile.server_name = selected.server_name
        self.connection_title.setText(f"Найден сервер «{selected.server_name}»")
        short = selected.display_fingerprint[:23]
        self.connection_detail.setText(
            f"Адрес и TLS-отпечаток заполнены. Перед вводом пароля сверьте начало отпечатка "
            f"с Server Manager: {short}…"
        )

    def _discovery_failed(self, message: str) -> None:
        self.discover_button.setEnabled(True)
        self.connection_detail.setText(f"Поиск сервера не выполнен: {message}")

    def refresh_connection(self) -> None:
        if self._connection_check_running or not self.token:
            return
        self._connection_check_running = True
        profile_id = self.profile.profile_id
        try:
            api = ClientApi(
                self.profile.server_url,
                token=self.token,
                certificate_fingerprint=self.profile.certificate_fingerprint,
                remote_session=self.remote_session,
            )
            self.api = api
        except ValueError:
            self._connection_check_running = False
            return

        def check() -> dict[str, Any]:
            api.health()
            status = api.pairing_status()
            spaces = api.list_spaces() if status.get("status") == "trusted" else []
            return {"status": status, "spaces": spaces, "profile_id": profile_id}

        self._start_task(
            check,
            self._connection_refreshed,
            lambda message: self._connection_refresh_failed_for(profile_id, message),
        )

    def login_remote(self) -> None:
        if not self.token:
            QMessageBox.warning(
                self,
                "Сначала подключите устройство",
                "Сначала войдите по логину или одноразовому коду и дождитесь подтверждения администратора.",
            )
            return
        username = self.username.text().strip().casefold()
        password = self.password.text()
        if len(username) < 3 or len(password) < 10:
            QMessageBox.warning(
                self,
                "Проверьте данные",
                "Введите логин из Server Manager и пароль минимум из 10 символов.",
            )
            return
        try:
            api = ClientApi(
                validate_server_url(self.server_url.text()),
                token=self.token,
                certificate_fingerprint=self.fingerprint.text(),
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Неверные параметры подключения", str(exc))
            return
        self.remote_login_button.setEnabled(False)
        profile_id = self.profile.profile_id

        def login() -> dict[str, Any]:
            result = api.create_remote_session(username, password)
            return {"session": result, "profile_id": profile_id}

        self._start_task(
            login,
            self._remote_login_complete,
            lambda message: self._remote_login_failed(profile_id, message),
        )

    def _remote_login_complete(self, result: object) -> None:
        payload = result if isinstance(result, dict) else {}
        if payload.get("profile_id") != self.profile.profile_id:
            return
        session = payload.get("session", {})
        token = str(session.get("session_token", ""))
        try:
            self.session_vault.store(token)
        except (OSError, ValueError) as exc:
            self.remote_login_button.setEnabled(True)
            QMessageBox.critical(self, "Сессия не сохранена", str(exc))
            return
        self.remote_session = token
        self.profile.username = str(session.get("username") or self.username.text()).casefold()
        self.profile.server_url = self.server_url.text().strip().rstrip("/")
        self.profile.certificate_fingerprint = self.fingerprint.text().strip()
        self.store.save(self.profile)
        self.password.clear()
        self.remote_login_button.setEnabled(True)
        self.refresh_connection()

    def _remote_login_failed(self, profile_id: str, message: str) -> None:
        if profile_id != self.profile.profile_id:
            return
        self.remote_login_button.setEnabled(True)
        QMessageBox.warning(self, "Интернет-вход не выполнен", message)

    def _connection_refreshed(self, result: object) -> None:
        payload = result if isinstance(result, dict) else {}
        if payload.get("profile_id") != self.profile.profile_id:
            return
        self._connection_check_running = False
        status = str(payload.get("status", {}).get("status", "disconnected"))
        self.profile.device_status = status
        self.store.save(self.profile)
        self._set_connection_state(status)
        if status == "trusted":
            self._set_spaces(payload.get("spaces", []))
            self._start_queued_transfers()
        self._reconcile_drives()

    def _connection_refresh_failed(self, message: str) -> None:
        self._connection_check_running = False
        self._set_connection_state("offline", message)

    def _connection_refresh_failed_for(self, profile_id: str, message: str) -> None:
        if profile_id == self.profile.profile_id:
            self._connection_refresh_failed(message)

    def _connection_failed(self, message: str) -> None:
        self.connect_button.setEnabled(True)
        self.account_login_button.setEnabled(True)
        self._set_connection_state("disconnected", message)
        QMessageBox.warning(self, "Подключение не выполнено", message)

    def _connection_failed_for(self, profile_id: str, message: str) -> None:
        if profile_id == self.profile.profile_id:
            self._connection_failed(message)

    def _set_connection_state(self, state: str, detail: str = "") -> None:
        if state == "trusted":
            self.connection_title.setText("Устройство подтверждено")
            self.connection_detail.setText(
                "Личное пространство доступно. Соединение восстановится автоматически."
            )
            self.sidebar_state.setText("ПОДКЛЮЧЕНО")
            self.sidebar_state.setStyleSheet("font-weight: 700; font-size: 11px; color: #43c778;")
            self.sidebar_server.setText(self.profile.server_name)
            self.nav_buttons[1].setEnabled(True)
        elif state == "pending":
            self.connection_title.setText("Ожидается подтверждение администратора")
            self.connection_detail.setText(
                "Устройство зарегистрировано, но файлы останутся заблокированы до подтверждения."
            )
            self.sidebar_state.setText("ОЖИДАЕТ ПОДТВЕРЖДЕНИЯ")
            self.sidebar_state.setStyleSheet("font-weight: 700; font-size: 11px; color: #f5bd4f;")
            self.sidebar_server.setText(self.profile.server_url)
            self.nav_buttons[1].setEnabled(False)
        elif state == "offline":
            self.connection_title.setText("Сервер временно недоступен")
            self.connection_detail.setText(detail or "Клиент повторит подключение автоматически.")
            self.sidebar_state.setText("ОФЛАЙН")
            self.sidebar_state.setStyleSheet("font-weight: 700; font-size: 11px; color: #f5bd4f;")
            self.sidebar_server.setText(self.profile.server_url)
            self.nav_buttons[1].setEnabled(False)
        else:
            self.connection_title.setText("Клиент не подключён")
            if detail:
                self.connection_detail.setText(detail)
            self.sidebar_state.setText("НЕ ПОДКЛЮЧЕНО")
            self.sidebar_state.setStyleSheet("font-weight: 700; font-size: 11px; color: #949ca8;")
            self.sidebar_server.setText("Укажите сервер и войдите")
            self.nav_buttons[1].setEnabled(False)

    def forget_connection(self) -> None:
        response = QMessageBox.question(
            self,
            "Забыть подключение?",
            "Локальный токен будет удалён. Администратору всё равно следует отозвать устройство на сервере.",
        )
        if response != QMessageBox.StandardButton.Yes:
            return
        current_server = self.profile.server_url.rstrip("/")
        for transfer in self.transfer_store.list():
            if transfer.server_url.rstrip("/") == current_server and transfer.status in {
                "queued",
                "running",
            }:
                self.transfer_store.set_status(
                    transfer.id,
                    "paused",
                    "Подключение удалено — продолжение приостановлено",
                )
        self.vault.clear()
        self.drive_manager.stop(self.profile.profile_id)
        self.session_vault.clear()
        self.token = None
        self.remote_session = None
        self.api = None
        self.profile.device_id = ""
        self.profile.device_status = "disconnected"
        self.profile.last_space_id = ""
        self.store.save(self.profile)
        self._set_spaces([])
        self._set_connection_state("disconnected")
        self.status_button.setEnabled(False)
        self.forget_button.setEnabled(False)

    def _set_spaces(self, spaces: list[dict[str, Any]]) -> None:
        self.spaces = spaces
        previous = self.profile.last_space_id
        self.space_selector.blockSignals(True)
        self.space_selector.clear()
        selected = 0
        for index, space in enumerate(spaces):
            self.space_selector.addItem(space.get("name", "Пространство"), space.get("id"))
            if space.get("id") == previous:
                selected = index
        self.space_selector.setCurrentIndex(selected if spaces else -1)
        self.space_selector.blockSignals(False)
        if spaces:
            selected_space = str(self.space_selector.itemData(selected) or "")
            if selected_space and self.profile.last_space_id != selected_space:
                self.profile.last_space_id = selected_space
                self.store.save(self.profile)
            self.current_directory = ""
            self.refresh_entries()
        else:
            self._render_entries([])

    def _space_changed(self) -> None:
        space_id = self.space_selector.currentData()
        if not space_id:
            return
        self.profile.last_space_id = str(space_id)
        self.store.save(self.profile)
        self.current_directory = ""
        self.refresh_entries()
        self._reconcile_drives()

    def refresh_entries(self) -> None:
        if not self.api or not self.space_selector.currentData():
            return
        space_id = str(self.space_selector.currentData())
        directory = self.current_directory
        self.path_label.setText("/" + directory)
        self.up_button.setEnabled(bool(directory))
        self._start_task(
            self.api.list_entries,
            self._render_entries,
            lambda message: self._files_error("Не удалось обновить файлы", message),
            space_id,
            directory,
        )

    def _render_entries(self, result: object) -> None:
        self.entries = result if isinstance(result, list) else []
        self.files_empty.setVisible(not self.entries)
        self.files_table.setRowCount(len(self.entries))
        for row, entry in enumerate(self.entries):
            name = QTableWidgetItem(str(entry.get("name", "")))
            name.setData(Qt.ItemDataRole.UserRole, entry)
            kind = "Папка" if entry.get("type") == "directory" else "Файл"
            size = "—" if kind == "Папка" else format_bytes(int(entry.get("size_bytes", 0)))
            modified = str(entry.get("modified_at", "—"))
            self.files_table.setItem(row, 0, name)
            self.files_table.setItem(row, 1, QTableWidgetItem(kind))
            self.files_table.setItem(row, 2, QTableWidgetItem(size))
            self.files_table.setItem(row, 3, QTableWidgetItem(modified))

    def selected_entry(self) -> dict[str, Any] | None:
        row = self.files_table.currentRow()
        if row < 0:
            return None
        item = self.files_table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def open_selected(self) -> None:
        entry = self.selected_entry()
        if not entry:
            return
        if entry.get("type") == "directory":
            self.current_directory = self._join_logical(self.current_directory, entry["name"])
            self.refresh_entries()
        else:
            self.download_selected(open_after=True)

    def go_up(self) -> None:
        if not self.current_directory:
            return
        parent = PurePosixPath(self.current_directory).parent
        self.current_directory = "" if str(parent) == "." else parent.as_posix()
        self.refresh_entries()

    def choose_upload(self) -> None:
        if not self.api or not self.space_selector.currentData():
            QMessageBox.information(self, "Нет подключения", "Сначала подключитесь к серверу.")
            return
        selected, _ = QFileDialog.getOpenFileName(self, "Выберите файл")
        if not selected:
            return
        source = Path(selected)
        logical = self._join_logical(self.current_directory, source.name)
        self.transfer_store.queue_upload(
            self.profile.server_url,
            str(self.space_selector.currentData()),
            logical,
            source,
        )
        self._refresh_transfer_cards()
        self._start_queued_transfers()
        self._show_page(self.transfers_page)

    def download_selected(self, open_after: bool = False) -> None:
        entry = self.selected_entry()
        if not entry or entry.get("type") != "file" or not self.api:
            return
        logical = self._join_logical(self.current_directory, entry["name"])
        root = Path(self.profile.download_directory)
        destination = root.joinpath(*PurePosixPath(logical).parts)
        transfer = self.transfer_store.queue_download(
            self.profile.server_url,
            str(self.space_selector.currentData()),
            logical,
            destination,
            total_bytes=int(entry.get("size_bytes", 0)),
            expected_sha256=str(entry.get("sha256", "")),
        )
        if open_after:
            self._open_after_transfer_ids.add(transfer.id)
        self._refresh_transfer_cards()
        self._start_queued_transfers()
        self._show_page(self.transfers_page)

    def delete_selected(self) -> None:
        entry = self.selected_entry()
        if not entry or entry.get("type") != "file" or not self.api:
            return
        logical = self._join_logical(self.current_directory, entry["name"])
        response = QMessageBox.question(
            self,
            "Переместить файл в корзину?",
            f"{logical}\n\nФайл останется в серверной корзине.",
        )
        if response != QMessageBox.StandardButton.Yes:
            return
        self._start_task(
            self.api.delete_file,
            lambda result: self.refresh_entries(),
            lambda message: self._files_error("Файл не удалён", message),
            str(self.space_selector.currentData()),
            logical,
        )

    def _transfer_tick(self) -> None:
        self._refresh_transfer_cards()
        self._start_queued_transfers()

    def _start_queued_transfers(self) -> None:
        if not self.token or self.profile.device_status != "trusted":
            return
        available_slots = max(0, 2 - len(self._active_transfer_ids))
        if not available_slots:
            return
        current_server = self.profile.server_url.rstrip("/")
        now = time.monotonic()
        for transfer in self.transfer_store.queued(limit=20):
            if available_slots <= 0:
                break
            if transfer.id in self._active_transfer_ids:
                continue
            if self._transfer_retry_after.get(transfer.id, 0) > now:
                continue
            if transfer.server_url.rstrip("/") != current_server:
                self.transfer_store.set_status(
                    transfer.id,
                    "paused",
                    "Передача относится к другому серверу",
                )
                continue
            self.transfer_store.set_status(transfer.id, "running")
            self._active_transfer_ids.add(transfer.id)
            available_slots -= 1
            self._start_task(
                self._run_transfer,
                self._transfer_finished,
                lambda message, transfer_id=transfer.id: self._unexpected_transfer_failure(
                    transfer_id, message
                ),
                transfer.id,
            )

    def _run_transfer(self, transfer_id: str) -> dict[str, object]:
        transfer = self.transfer_store.get(transfer_id)
        token = self.token
        if not token:
            self.transfer_store.set_status(transfer_id, "paused", "Нет подключения")
            return {"id": transfer_id, "network_error": False}
        api = ClientApi(
            transfer.server_url,
            token=token,
            certificate_fingerprint=self.profile.certificate_fingerprint,
            remote_session=self.remote_session,
        )
        try:
            if transfer.kind == "upload":
                self._run_upload_transfer(api, transfer)
            else:
                self._run_download_transfer(api, transfer)
            return {"id": transfer_id, "network_error": False}
        except TransferInterrupted:
            current = self.transfer_store.get(transfer_id)
            if current.status == "running":
                self.transfer_store.set_status(transfer_id, "paused", "Приостановлено")
            elif current.status == "cancelled":
                self._cleanup_cancelled_transfer(api, current)
            return {"id": transfer_id, "network_error": False}
        except CertificateMismatch as exc:
            self.transfer_store.set_status(
                transfer_id,
                "failed",
                f"Сертификат сервера не совпадает: {exc}",
            )
            return {"id": transfer_id, "network_error": False}
        except ClientConnectionError as exc:
            self.transfer_store.set_status(
                transfer_id,
                "queued",
                f"Ожидание сети: {exc}",
            )
            return {"id": transfer_id, "network_error": True}
        except (ClientApiError, OSError, ValueError, KeyError) as exc:
            self.transfer_store.set_status(transfer_id, "failed", str(exc))
            return {"id": transfer_id, "network_error": False}

    def _run_upload_transfer(self, api: ClientApi, transfer: TransferRecord) -> None:
        source = Path(transfer.local_path)
        stat = source.stat()
        if stat.st_size != transfer.total_bytes or stat.st_mtime_ns != transfer.source_mtime_ns:
            raise ValueError("Исходный файл изменён после добавления в очередь")

        upload_id = transfer.remote_session_id
        status: dict[str, Any] | None = None
        if upload_id:
            try:
                status = api.resumable_upload_status(upload_id)
            except ClientApiError as exc:
                if exc.status_code != 404:
                    raise
                upload_id = ""

        if not upload_id:
            expected_sha256 = self._sha256_file(source, transfer.id)
            self.transfer_store.set_expected_sha256(transfer.id, expected_sha256)
            status = api.create_resumable_upload(
                transfer.space_id,
                transfer.logical_path,
                transfer.total_bytes,
                sha256=expected_sha256,
            )
            upload_id = str(status["id"])
            self.transfer_store.update_progress(
                transfer.id,
                int(status.get("received_bytes", 0)),
                remote_session_id=upload_id,
            )

        assert status is not None
        if status.get("status") == "completed":
            self.transfer_store.update_progress(transfer.id, transfer.total_bytes)
            self.transfer_store.set_status(transfer.id, "completed")
            return

        offset = int(status.get("received_bytes", 0))
        if offset > transfer.total_bytes:
            raise ValueError("Сервер сообщил неверное смещение загрузки")
        self.transfer_store.update_progress(transfer.id, offset, remote_session_id=upload_id)
        with source.open("rb") as handle:
            handle.seek(offset)
            while offset < transfer.total_bytes:
                if not self._transfer_should_continue(transfer.id):
                    raise TransferInterrupted("transfer paused")
                chunk = handle.read(min(4 * 1024 * 1024, transfer.total_bytes - offset))
                if not chunk:
                    raise OSError("Не удалось прочитать исходный файл")
                status = api.append_resumable_upload(upload_id, offset, chunk)
                offset = int(status.get("received_bytes", offset + len(chunk)))
                self.transfer_store.update_progress(transfer.id, offset)
        api.complete_resumable_upload(upload_id)
        self.transfer_store.update_progress(transfer.id, transfer.total_bytes)
        self.transfer_store.set_status(transfer.id, "completed")

    def _run_download_transfer(self, api: ClientApi, transfer: TransferRecord) -> None:
        destination = api.download_file(
            transfer.space_id,
            transfer.logical_path,
            Path(transfer.local_path),
            expected_sha256=transfer.expected_sha256,
            should_continue=lambda: self._transfer_should_continue(transfer.id),
            progress=lambda done, total: self.transfer_store.update_progress(
                transfer.id,
                done,
                total_bytes=total,
            ),
        )
        completed_size = destination.stat().st_size
        self.transfer_store.update_progress(
            transfer.id,
            completed_size,
            total_bytes=completed_size,
        )
        self.transfer_store.set_status(transfer.id, "completed")

    def _transfer_should_continue(self, transfer_id: str) -> bool:
        try:
            return self.transfer_store.get(transfer_id).status == "running"
        except KeyError:
            return False

    def _sha256_file(self, source: Path, transfer_id: str) -> str:
        digest = hashlib.sha256()
        with source.open("rb") as handle:
            while chunk := handle.read(4 * 1024 * 1024):
                if not self._transfer_should_continue(transfer_id):
                    raise TransferInterrupted("transfer paused")
                digest.update(chunk)
        return digest.hexdigest()

    def _cleanup_cancelled_transfer(self, api: ClientApi, transfer: TransferRecord) -> None:
        if transfer.kind == "upload" and transfer.remote_session_id:
            try:
                api.cancel_resumable_upload(transfer.remote_session_id)
            except (ClientApiError, ClientConnectionError):
                pass
        elif transfer.kind == "download":
            destination = Path(transfer.local_path)
            destination.with_name(destination.name + ".part").unlink(missing_ok=True)

    def _transfer_finished(self, result: object) -> None:
        payload = result if isinstance(result, dict) else {}
        transfer_id = str(payload.get("id", ""))
        self._active_transfer_ids.discard(transfer_id)
        if payload.get("network_error"):
            self._transfer_retry_after[transfer_id] = time.monotonic() + 10
        else:
            self._transfer_retry_after.pop(transfer_id, None)
        try:
            transfer = self.transfer_store.get(transfer_id)
        except KeyError:
            transfer = None
        if transfer and transfer.status == "completed":
            if transfer.kind == "upload":
                self.refresh_entries()
                indexed = self.offline_store.find(
                    transfer.server_url,
                    transfer.space_id,
                    transfer.logical_path,
                )
                if indexed and Path(transfer.local_path).is_file():
                    self.offline_store.mark_synced(
                        transfer.server_url,
                        transfer.space_id,
                        transfer.logical_path,
                        Path(transfer.local_path),
                        transfer.expected_sha256,
                        transfer.total_bytes,
                    )
            elif Path(transfer.local_path).is_file():
                self.offline_store.mark_synced(
                    transfer.server_url,
                    transfer.space_id,
                    transfer.logical_path,
                    Path(transfer.local_path),
                    transfer.expected_sha256,
                    Path(transfer.local_path).stat().st_size,
                )
            self._render_offline_records(self.offline_store.list())
            if transfer_id in self._open_after_transfer_ids:
                self._open_after_transfer_ids.discard(transfer_id)
                QDesktopServices.openUrl(QUrl.fromLocalFile(transfer.local_path))
        self._refresh_transfer_cards()
        self._start_queued_transfers()

    def _unexpected_transfer_failure(self, transfer_id: str, message: str) -> None:
        self._active_transfer_ids.discard(transfer_id)
        try:
            self.transfer_store.set_status(transfer_id, "failed", message)
        except KeyError:
            pass
        self._refresh_transfer_cards()
        self._start_queued_transfers()

    def _pause_transfer(self, transfer_id: str) -> None:
        transfer = self.transfer_store.get(transfer_id)
        if transfer.status in {"queued", "running"}:
            self.transfer_store.set_status(transfer_id, "paused", "Приостановлено")
            self._refresh_transfer_cards()

    def _resume_transfer(self, transfer_id: str) -> None:
        transfer = self.transfer_store.get(transfer_id)
        if transfer.status in {"paused", "failed"}:
            self.transfer_store.set_status(transfer_id, "queued")
            self._transfer_retry_after.pop(transfer_id, None)
            self._refresh_transfer_cards()
            self._start_queued_transfers()

    def _cancel_transfer(self, transfer_id: str) -> None:
        transfer = self.transfer_store.get(transfer_id)
        if transfer.status not in {"completed", "cancelled"}:
            self.transfer_store.set_status(transfer_id, "cancelled", "Отменено")
            profile = next(
                (
                    item
                    for item in self.store.list_profiles()
                    if item.server_url.rstrip("/") == transfer.server_url.rstrip("/")
                ),
                None,
            )
            token = (
                DeviceTokenVault(self.store.data_directory, profile.profile_id).load()
                if profile is not None
                else None
            )
            if transfer_id not in self._active_transfer_ids and token and profile is not None:
                api = ClientApi(
                    transfer.server_url,
                    token=token,
                    certificate_fingerprint=profile.certificate_fingerprint,
                    remote_session=RemoteSessionVault(
                        self.store.data_directory, profile.profile_id
                    ).load(),
                )
                self._start_task(
                    self._cleanup_cancelled_transfer,
                    lambda _result: self._refresh_transfer_cards(),
                    lambda _message: self._refresh_transfer_cards(),
                    api,
                    self.transfer_store.get(transfer_id),
                )
            self._refresh_transfer_cards()

    def _open_transfer(self, transfer_id: str) -> None:
        transfer = self.transfer_store.get(transfer_id)
        path = Path(transfer.local_path)
        if path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _clear_finished_transfers(self) -> None:
        self.transfer_store.clear_finished()
        self._refresh_transfer_cards()

    def _refresh_transfer_cards(self) -> None:
        transfers = self.transfer_store.list()
        identifiers = [transfer.id for transfer in transfers]
        if identifiers != list(self._transfer_widgets):
            clear_layout(self.transfer_rows)
            self._transfer_widgets.clear()
            if not transfers:
                empty = QLabel("Здесь появятся загрузки на сервер и скачивания на компьютер.")
                empty.setProperty("muted", True)
                self.transfer_rows.addWidget(empty)
            for transfer in transfers:
                self._add_transfer_card(transfer)
        for transfer in transfers:
            self._update_transfer_card(transfer)

        active = sum(transfer.status == "running" for transfer in transfers)
        waiting = sum(transfer.status in {"queued", "paused", "failed"} for transfer in transfers)
        completed = sum(transfer.status == "completed" for transfer in transfers)
        if transfers:
            self.transfer_summary.setText(
                f"Активно: {active}  ·  ожидают: {waiting}  ·  завершено: {completed}"
            )
        else:
            self.transfer_summary.setText("Очередь пуста")

    def _add_transfer_card(self, transfer: TransferRecord) -> None:
        card = QFrame()
        card.setProperty("card", True)
        layout = QVBoxLayout(card)
        direction = "На сервер" if transfer.kind == "upload" else "На компьютер"
        title = QLabel(f"{direction}: {PurePosixPath(transfer.logical_path).name}")
        title.setStyleSheet("font-weight: 700;")
        detail = QLabel(transfer.logical_path)
        detail.setProperty("muted", True)
        state = QLabel()
        progress = QProgressBar()
        progress.setRange(0, 100)
        controls = QHBoxLayout()
        pause = QPushButton("Пауза")
        resume = QPushButton("Продолжить")
        cancel = QPushButton("Отменить")
        open_button = QPushButton("Открыть")
        pause.clicked.connect(
            lambda _checked=False, transfer_id=transfer.id: self._pause_transfer(transfer_id)
        )
        resume.clicked.connect(
            lambda _checked=False, transfer_id=transfer.id: self._resume_transfer(transfer_id)
        )
        cancel.clicked.connect(
            lambda _checked=False, transfer_id=transfer.id: self._cancel_transfer(transfer_id)
        )
        open_button.clicked.connect(
            lambda _checked=False, transfer_id=transfer.id: self._open_transfer(transfer_id)
        )
        controls.addWidget(pause)
        controls.addWidget(resume)
        controls.addWidget(cancel)
        controls.addWidget(open_button)
        controls.addStretch()
        layout.addWidget(title)
        layout.addWidget(detail)
        layout.addWidget(state)
        layout.addWidget(progress)
        layout.addLayout(controls)
        self.transfer_rows.addWidget(card)
        self._transfer_widgets[transfer.id] = {
            "state": state,
            "progress": progress,
            "pause": pause,
            "resume": resume,
            "cancel": cancel,
            "open": open_button,
        }

    def _update_transfer_card(self, transfer: TransferRecord) -> None:
        widgets = self._transfer_widgets.get(transfer.id)
        if not widgets:
            return
        state = widgets["state"]
        progress = widgets["progress"]
        labels = {
            "queued": "В очереди",
            "running": "Передача",
            "paused": "Приостановлено",
            "completed": "Завершено",
            "failed": "Ошибка",
            "cancelled": "Отменено",
        }
        detail = (
            f"{format_bytes(transfer.transferred_bytes)} из {format_bytes(transfer.total_bytes)}"
        )
        if transfer.error:
            detail += f" · {transfer.error}"
        state.setText(f"{labels[transfer.status]} · {detail}")
        percent = (
            round(transfer.transferred_bytes / transfer.total_bytes * 100)
            if transfer.total_bytes
            else (100 if transfer.status == "completed" else 0)
        )
        progress.setValue(max(0, min(100, percent)))
        widgets["pause"].setVisible(transfer.status == "running")
        widgets["resume"].setVisible(transfer.status in {"paused", "failed"})
        widgets["cancel"].setVisible(transfer.status in {"queued", "running", "paused", "failed"})
        widgets["open"].setVisible(
            transfer.kind == "download"
            and transfer.status == "completed"
            and Path(transfer.local_path).exists()
        )

    def choose_cache_directory(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "Каталог офлайн-файлов",
            self.profile.download_directory,
        )
        if not selected:
            return
        self.profile.download_directory = str(Path(selected).resolve())
        self.cache_path.setText(self.profile.download_directory)
        self.store.save(self.profile)

    def open_cache_directory(self) -> None:
        directory = Path(self.profile.download_directory)
        directory.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))

    def integrate_offline_directory(self) -> None:
        try:
            result = integrate_file_manager(Path(self.profile.download_directory))
        except OSError as exc:
            self.integration_status.setText(f"Интеграция не выполнена: {exc}")
            QMessageBox.warning(self, "Интеграция не выполнена", str(exc))
            return
        self.integration_status.setText(result.detail)

    def _autostart_toggled(self, enabled: bool) -> None:
        try:
            set_autostart(enabled)
        except OSError as exc:
            self.autostart_checkbox.blockSignals(True)
            self.autostart_checkbox.setChecked(not enabled)
            self.autostart_checkbox.blockSignals(False)
            QMessageBox.warning(self, "Автозапуск не изменён", str(exc))

    def _close_to_tray_toggled(self, enabled: bool) -> None:
        self.profile.close_to_tray = enabled
        self.store.save(self.profile)

    def _drive_settings_changed(self) -> None:
        self.profile.drive_enabled = self.drive_enabled_checkbox.isChecked()
        selected = str(self.drive_letter_selector.currentData() or "S")
        self.profile.drive_letter = selected
        self.drive_letter_selector.setEnabled(
            platform.system() == "Windows" and self.profile.drive_enabled
        )
        try:
            self.store.save(self.profile)
        except ValueError as exc:
            QMessageBox.warning(self, "Диск не настроен", str(exc))
            return
        self._reconcile_drives()

    def _reconcile_drives(self) -> None:
        if platform.system() != "Windows":
            self.drive_status.setText("Диски с буквами поддерживаются только в Windows.")
            self.drive_open_button.setEnabled(False)
            return
        # One active Client and one active virtual-drive helper are enough. Other
        # profiles stay configured and are mounted when the user switches to them.
        statuses = self.drive_manager.reconcile([self.profile])
        status = statuses.get(self.profile.profile_id)
        if status is None:
            self.drive_status.setText("Диск отключён в настройках.")
            self.drive_open_button.setEnabled(False)
            return
        labels = {
            "ready": "Подключён",
            "starting": "Запускается",
            "offline": "Сервер недоступен",
            "error": "Ошибка",
            "stopped": "Отключён",
        }
        label = labels.get(status.state, status.state)
        detail = f" · {status.detail}" if status.detail else ""
        self.drive_status.setText(f"{status.drive_letter}: · {label}{detail}")
        self.drive_open_button.setEnabled(status.state == "ready")

    def open_windows_drive(self) -> None:
        letter = self.profile.drive_letter.upper().rstrip(":")
        if not wait_for_drive(letter, timeout_seconds=1):
            QMessageBox.information(
                self,
                "Диск ещё не готов",
                "Подключение ещё запускается или сервер недоступен.",
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(f"{letter}:\\"))

    def _setup_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.close_to_tray_checkbox.setEnabled(False)
            return
        app = QApplication.instance()
        if app is not None:
            app.setQuitOnLastWindowClosed(False)
        tray = QSystemTrayIcon(create_app_icon(), self)
        tray.setToolTip("Cloud Storage Client")
        menu = QMenu(self)
        show_action = QAction("Открыть Cloud Storage", self)
        show_action.triggered.connect(self._show_from_tray)
        pause_action = QAction("Приостановить передачи", self)
        pause_action.triggered.connect(self._pause_all_transfers)
        resume_action = QAction("Продолжить передачи", self)
        resume_action.triggered.connect(self._resume_all_transfers)
        open_cache_action = QAction("Открыть офлайн-каталог", self)
        open_cache_action.triggered.connect(self.open_cache_directory)
        quit_action = QAction("Выйти", self)
        quit_action.triggered.connect(self._quit_from_tray)
        menu.addAction(show_action)
        menu.addSeparator()
        menu.addAction(pause_action)
        menu.addAction(resume_action)
        menu.addAction(open_cache_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        tray.setContextMenu(menu)
        tray.activated.connect(self._tray_activated)
        tray.show()
        self.tray_menu = menu
        self.tray_icon = tray

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in {
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        }:
            self._show_from_tray()

    def _show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _pause_all_transfers(self) -> None:
        for transfer in self.transfer_store.list():
            if transfer.status in {"queued", "running"}:
                self.transfer_store.set_status(transfer.id, "paused", "Приостановлено")
        self._refresh_transfer_cards()

    def _resume_all_transfers(self) -> None:
        for transfer in self.transfer_store.list():
            if transfer.status == "paused":
                self.transfer_store.set_status(transfer.id, "queued")
        self._refresh_transfer_cards()
        self._start_queued_transfers()

    def _quit_from_tray(self) -> None:
        self._quit_requested = True
        self._prepare_shutdown()
        if self.tray_icon is not None:
            self.tray_icon.hide()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def refresh_offline_index(self) -> None:
        if self._offline_scan_running:
            return
        self._offline_scan_running = True
        self.offline_refresh_button.setEnabled(False)
        self.offline_summary.setText("Проверяем локальные и серверные версии…")
        self._start_task(
            self._scan_offline_index,
            self._offline_scan_finished,
            self._offline_scan_failed,
        )

    def _scan_offline_index(self) -> list[OfflineRecord]:
        records = self.offline_store.list()
        for record in records:
            self.offline_store.scan_local(record.id)
        if not self.token or self.profile.device_status != "trusted":
            return self.offline_store.list()

        current_server = self.profile.server_url.rstrip("/")
        relevant = [record for record in records if record.server_url == current_server]
        if not relevant:
            return self.offline_store.list()
        api = ClientApi(
            current_server,
            token=self.token,
            certificate_fingerprint=self.profile.certificate_fingerprint,
            remote_session=self.remote_session,
        )
        groups: dict[tuple[str, str], list[OfflineRecord]] = {}
        for record in relevant:
            logical = PurePosixPath(record.logical_path)
            parent = "" if str(logical.parent) == "." else logical.parent.as_posix()
            groups.setdefault((record.space_id, parent), []).append(record)
        for (space_id, parent), grouped_records in groups.items():
            entries = {
                str(entry.get("name", "")): entry
                for entry in api.list_entries(space_id, parent)
                if entry.get("type") == "file"
            }
            for record in grouped_records:
                entry = entries.get(PurePosixPath(record.logical_path).name)
                self.offline_store.apply_remote_state(
                    record.id,
                    exists=entry is not None,
                    sha256=str(entry.get("sha256", "")) if entry else "",
                    size_bytes=int(entry.get("size_bytes", 0)) if entry else 0,
                )
        return self.offline_store.list()

    def _offline_scan_finished(self, result: object) -> None:
        self._offline_scan_running = False
        self.offline_refresh_button.setEnabled(True)
        records = result if isinstance(result, list) else self.offline_store.list()
        self._render_offline_records(records)

    def _offline_scan_failed(self, message: str) -> None:
        self._offline_scan_running = False
        self.offline_refresh_button.setEnabled(True)
        self._render_offline_records(self.offline_store.list())
        self.offline_summary.setText(
            f"Локальные изменения проверены · сервер недоступен: {message}"
        )

    def _render_offline_records(self, records: list[OfflineRecord]) -> None:
        selected_id = self._selected_offline_id()
        labels = {
            "current": "Актуален",
            "local_changed": "Изменён на компьютере",
            "remote_changed": "Изменён на сервере",
            "conflict": "Конфликт версий",
            "local_missing": "Нет локальной копии",
            "remote_missing": "Удалён на сервере",
            "missing_both": "Файл отсутствует",
            "error": "Ошибка проверки",
        }
        self.offline_table.setRowCount(len(records))
        selected_row = -1
        for row, record in enumerate(records):
            name = QTableWidgetItem(record.logical_path)
            name.setData(Qt.ItemDataRole.UserRole, record.id)
            self.offline_table.setItem(row, 0, name)
            self.offline_table.setItem(
                row, 1, QTableWidgetItem(labels.get(record.status, record.status))
            )
            self.offline_table.setItem(row, 2, QTableWidgetItem(format_bytes(record.size_bytes)))
            self.offline_table.setItem(row, 3, QTableWidgetItem(record.server_url))
            if record.id == selected_id:
                selected_row = row
        if selected_row >= 0:
            self.offline_table.selectRow(selected_row)
        counts = {
            status: sum(record.status == status for record in records)
            for status in {"local_changed", "remote_changed", "conflict"}
        }
        if records:
            self.offline_summary.setText(
                f"Файлов: {len(records)}  ·  локальных изменений: {counts['local_changed']}  ·  "
                f"серверных: {counts['remote_changed']}  ·  конфликтов: {counts['conflict']}"
            )
        else:
            self.offline_summary.setText("Индекс офлайн-файлов пуст")
        self._update_offline_actions()

    def _selected_offline_id(self) -> str:
        row = self.offline_table.currentRow()
        item = self.offline_table.item(row, 0) if row >= 0 else None
        return str(item.data(Qt.ItemDataRole.UserRole)) if item else ""

    def _selected_offline(self) -> OfflineRecord | None:
        record_id = self._selected_offline_id()
        try:
            return self.offline_store.get(record_id) if record_id else None
        except KeyError:
            return None

    def _update_offline_actions(self) -> None:
        record = self._selected_offline()
        self.offline_open_button.setEnabled(bool(record and Path(record.local_path).is_file()))
        self.offline_upload_button.setEnabled(
            bool(record and record.status in {"local_changed", "conflict", "remote_missing"})
        )
        self.offline_download_button.setEnabled(
            bool(record and record.status in {"remote_changed", "conflict", "local_missing"})
        )
        self.offline_remove_button.setEnabled(record is not None)

    def open_offline_selected(self) -> None:
        record = self._selected_offline()
        if record and Path(record.local_path).is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(record.local_path))

    def upload_offline_selected(self) -> None:
        record = self._selected_offline()
        if not record:
            return
        if (
            not self.token
            or self.profile.device_status != "trusted"
            or record.server_url != self.profile.server_url.rstrip("/")
        ):
            QMessageBox.information(
                self,
                "Нет нужного подключения",
                "Подключитесь к серверу, которому принадлежит этот файл.",
            )
            return
        source = Path(record.local_path)
        if not source.is_file():
            return
        if record.status == "conflict":
            response = QMessageBox.question(
                self,
                "Заменить серверную версию?",
                "На сервере и компьютере есть разные изменения. Локальная версия будет "
                "загружена как новая версия файла.",
            )
            if response != QMessageBox.StandardButton.Yes:
                return
        self.transfer_store.queue_upload(
            record.server_url,
            record.space_id,
            record.logical_path,
            source,
        )
        self._refresh_transfer_cards()
        self._start_queued_transfers()
        self._show_page(self.transfers_page)

    def download_offline_selected(self) -> None:
        record = self._selected_offline()
        if not record:
            return
        if (
            not self.token
            or self.profile.device_status != "trusted"
            or record.server_url != self.profile.server_url.rstrip("/")
        ):
            QMessageBox.information(
                self,
                "Нет нужного подключения",
                "Подключитесь к серверу, которому принадлежит этот файл.",
            )
            return
        destination = Path(record.local_path)
        if record.status == "conflict" and destination.is_file():
            response = QMessageBox.question(
                self,
                "Сохранить локальную версию отдельно?",
                "Локальная версия будет переименована с пометкой conflict, затем клиент "
                "скачает серверную. Ни одна версия не потеряется.",
            )
            if response != QMessageBox.StandardButton.Yes:
                return
            backup = destination.with_name(
                f"{destination.stem}.local-conflict-{time.strftime('%Y%m%d-%H%M%S')}"
                f"{destination.suffix}"
            )
            destination.replace(backup)
        self.transfer_store.queue_download(
            record.server_url,
            record.space_id,
            record.logical_path,
            destination,
            total_bytes=record.size_bytes,
            expected_sha256=record.remote_sha256,
        )
        self._refresh_transfer_cards()
        self._start_queued_transfers()
        self._show_page(self.transfers_page)

    def remove_offline_selected(self) -> None:
        record = self._selected_offline()
        if not record:
            return
        response = QMessageBox.question(
            self,
            "Убрать из индекса?",
            "Локальный файл останется на диске, клиент лишь перестанет следить за его версиями.",
        )
        if response == QMessageBox.StandardButton.Yes:
            self.offline_store.remove(record.id)
            self._render_offline_records(self.offline_store.list())

    def _show_page(self, page: QWidget) -> None:
        self.stack.setCurrentWidget(page)
        for index, button in enumerate(self.nav_buttons):
            button.setChecked(self.stack.widget(index) is page)
        if page is self.offline_page:
            self.refresh_offline_index()

    def _start_task(
        self,
        function: Callable[..., Any],
        success: Callable[[object], None],
        failure: Callable[[str], None],
        *arguments: Any,
        with_progress: bool = False,
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        worker = BackgroundTask(function, *arguments, with_progress=with_progress)
        self._workers.add(worker)
        worker.signals.success.connect(success)
        worker.signals.failure.connect(failure)
        if progress:
            worker.signals.progress.connect(progress)
        worker.signals.finished.connect(lambda task=worker: self._workers.discard(task))
        QThreadPool.globalInstance().start(worker)

    @staticmethod
    def _join_logical(directory: str, name: str) -> str:
        return f"{directory}/{name}" if directory else name

    def _files_error(self, title: str, message: str) -> None:
        QMessageBox.warning(self, title, message)

    def closeEvent(self, event: QCloseEvent) -> None:
        if (
            not self._quit_requested
            and self.profile.close_to_tray
            and self.tray_icon is not None
            and self.tray_icon.isVisible()
        ):
            event.ignore()
            self.hide()
            if not self._tray_notice_shown:
                self.tray_icon.showMessage(
                    "Cloud Storage продолжает работу",
                    "Передачи доступны через значок в системном трее.",
                    QSystemTrayIcon.MessageIcon.Information,
                    3500,
                )
                self._tray_notice_shown = True
            return
        self._prepare_shutdown()
        event.accept()

    def _prepare_shutdown(self) -> None:
        if self._shutdown_prepared:
            return
        self._shutdown_prepared = True
        self.reconnect_timer.stop()
        self.transfer_timer.stop()
        self.drive_timer.stop()
        self.drive_manager.stop_all()
        for transfer_id in tuple(self._active_transfer_ids):
            try:
                self.transfer_store.set_status(
                    transfer_id,
                    "queued",
                    "Продолжение после перезапуска",
                )
            except KeyError:
                pass
        self.store.save(self.profile)
