from __future__ import annotations

import time
from datetime import datetime

import qrcode
from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication, QImage, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from cloud_storage.pairing import build_pairing_uri, parse_connection_code, parse_server_code
from cloud_storage.ui.widgets import clear_layout, make_header


class ConnectionCodePanel(QFrame):
    generation_requested = Signal(str, str, str)
    create_user_requested = Signal()
    edit_user_requested = Signal(str)
    delete_user_requested = Signal(str)
    reset_password_requested = Signal(str, str)
    email_access_requested = Signal(str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("card", True)
        self._health: dict | None = None
        self._tunnels: dict = {}
        self._users_count = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        title = QLabel("Код подключения клиента")
        title.setStyleSheet("font-size: 18px; font-weight: 700;")
        description = QLabel(
            "Сервер автоматически меняет защищённый код каждые 5 минут. Введите его "
            "в Desktop Client один раз — дальнейшие подключения выполняются автоматически."
        )
        description.setWordWrap(True)
        description.setProperty("muted", True)
        layout.addWidget(title)
        layout.addWidget(description)

        form = QFormLayout()
        form.setVerticalSpacing(12)
        self.user = QComboBox()
        self.user.setMinimumWidth(280)
        self.scope = QComboBox()
        self.scope.addItem("Из любой сети (интернет)", "internet")
        self.scope.addItem("Только в локальной сети", "lan")
        self.endpoint = QLabel("Сервер не запущен")
        self.endpoint.setWordWrap(True)
        self.endpoint.setProperty("muted", True)
        form.addRow("Пользователь", self.user)
        form.addRow("Доступ", self.scope)
        form.addRow("Адрес внутри кода", self.endpoint)
        form.setRowVisible(self.user, False)
        form.setRowVisible(self.scope, False)
        form.setRowVisible(self.endpoint, False)
        layout.addLayout(form)

        controls = QHBoxLayout()
        self.create_user_button = QPushButton("+ Новый пользователь")
        self.create_user_button.clicked.connect(self.create_user_requested)
        self.generate_button = QPushButton("Создать код на 15 минут")
        self.generate_button.setProperty("primary", True)
        self.generate_button.clicked.connect(self._request_generation)
        self.refresh_button = QPushButton("Обновить состояние")
        controls.addWidget(self.create_user_button)
        controls.addWidget(self.generate_button)
        controls.addWidget(self.refresh_button)
        controls.addStretch()
        layout.addLayout(controls)
        self.create_user_button.setVisible(False)
        self.generate_button.setVisible(False)

        self.users_toggle = QPushButton("Пользователи (0)  ▾")
        self.users_toggle.setCheckable(True)
        self.users_toggle.setChecked(True)
        self.users_toggle.setStyleSheet("font-size: 16px; font-weight: 700; text-align: left;")
        layout.addWidget(self.users_toggle)
        self.users_container = QWidget()
        self.users_rows = QVBoxLayout(self.users_container)
        self.users_rows.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.users_container)
        self.users_toggle.toggled.connect(self._toggle_users)

        self.access_tabs = QTabWidget()
        credentials_tab = QWidget()
        credentials_layout = QFormLayout(credentials_tab)
        self.generated_username = QLineEdit()
        self.generated_username.setReadOnly(True)
        self.generated_username.setPlaceholderText("Логин пользователя")
        self.generated_password = QLineEdit()
        self.generated_password.setReadOnly(True)
        self.generated_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.generated_password.setPlaceholderText("Создайте или сбросьте пароль")
        self.code = QLineEdit()
        self.code.setReadOnly(True)
        self.code.setPlaceholderText("Здесь появится код сервера вида CS2.…")
        self.code.setMinimumHeight(42)
        self.copy_button = QPushButton("Скопировать логин и пароль")
        self.copy_button.setEnabled(False)
        self.copy_button.clicked.connect(self.copy_code)
        credentials_layout.addRow("Логин", self.generated_username)
        credentials_layout.addRow("Пароль", self.generated_password)
        credentials_layout.addRow("", self.copy_button)
        self.access_tabs.addTab(credentials_tab, "1. Логин и пароль")

        link_tab = QWidget()
        link_layout = QVBoxLayout(link_tab)
        link_help = QLabel(
            "Вставьте эту ссылку в отдельное поле «Ссылка» в Client. Адрес, TLS, логин и одноразовый код заполнятся сами."
        )
        link_help.setWordWrap(True)
        link_help.setProperty("muted", True)
        self.invitation_link = QLineEdit()
        self.invitation_link.setReadOnly(True)
        self.invitation_link.setPlaceholderText("cloudstorage://pair?…")
        self.copy_link_button = QPushButton("Скопировать ссылку")
        self.copy_link_button.setEnabled(False)
        self.copy_link_button.clicked.connect(self.copy_link)
        link_layout.addWidget(link_help)
        link_layout.addWidget(self.invitation_link)
        link_layout.addWidget(self.copy_link_button)
        link_layout.addStretch()
        self.access_tabs.addTab(link_tab, "2. Ссылка")

        qr_tab = QWidget()
        qr_layout = QHBoxLayout(qr_tab)
        self.qr_label = QLabel("Сначала создайте доступ")
        self.qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr_label.setMinimumSize(220, 220)
        self.qr_label.setProperty("muted", True)
        qr_help = QLabel(
            "Отсканируйте QR мобильным Client. QR содержит ту же рабочую ссылку и не требует ручного ввода IP."
        )
        qr_help.setWordWrap(True)
        qr_help.setProperty("muted", True)
        qr_layout.addWidget(self.qr_label)
        qr_layout.addWidget(qr_help, 1)
        self.access_tabs.addTab(qr_tab, "3. QR-code")
        layout.addWidget(self.access_tabs)
        self.access_tabs.setVisible(False)

        dynamic_card = QFrame()
        self.dynamic_card = dynamic_card
        dynamic_card.setProperty("card", True)
        dynamic_layout = QVBoxLayout(dynamic_card)
        dynamic_title = QLabel("Динамический код подключения")
        dynamic_title.setStyleSheet("font-size: 16px; font-weight: 700;")
        self.dynamic_code = QLineEdit()
        self.dynamic_code.setReadOnly(True)
        self.dynamic_code.setPlaceholderText("Запустите Core и включите локальный HTTPS")
        self.dynamic_code.setMinimumHeight(46)
        self.dynamic_code.setMaxLength(4096)
        dynamic_actions = QHBoxLayout()
        self.dynamic_countdown = QLabel("Код ещё не получен")
        self.dynamic_countdown.setProperty("muted", True)
        self.copy_dynamic_button = QPushButton("Копировать код")
        self.copy_dynamic_button.setProperty("primary", True)
        self.copy_dynamic_button.clicked.connect(
            lambda: QGuiApplication.clipboard().setText(self.dynamic_code.text())
        )
        dynamic_actions.addWidget(self.dynamic_countdown, 1)
        dynamic_actions.addWidget(self.copy_dynamic_button)
        dynamic_layout.addWidget(dynamic_title)
        dynamic_layout.addWidget(self.dynamic_code)
        dynamic_layout.addLayout(dynamic_actions)
        self.web_address = QLineEdit()
        self.web_address.setReadOnly(True)
        self.web_address.setPlaceholderText("Адрес веб-клиента появится после запуска сети")
        self.web_open = QPushButton("Открыть файлы в браузере")
        self.web_copy = QPushButton("Копировать адрес сайта")
        self.web_open.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(self.web_address.text())))
        self.web_copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.web_address.text()))
        web_actions = QHBoxLayout()
        web_actions.addWidget(self.web_open)
        web_actions.addWidget(self.web_copy)
        dynamic_layout.addWidget(self.web_address)
        dynamic_layout.addLayout(web_actions)
        web_hint = QLabel("В браузере введите этот же динамический код. Для доступа из другой сети нужен запущенный zrok2 или публичный HTTPS. Локальный адрес работает только в вашей сети.")
        web_hint.setWordWrap(True)
        web_hint.setProperty("muted", True)
        dynamic_layout.addWidget(web_hint)
        layout.addWidget(dynamic_card)
        # The one-time code is the primary action and must stay above the user list.
        layout.insertWidget(2, dynamic_card)
        self._dynamic_expires_at = 0
        self._dynamic_refresh_requested = False
        self._dynamic_timer = QTimer(self)
        self._dynamic_timer.setInterval(1000)
        self._dynamic_timer.timeout.connect(self._update_dynamic_countdown)
        self._dynamic_timer.start()

        self.result_detail = QLabel(
            "Интернет-код станет доступен после запуска zrok или настройки публичного HTTPS-входа."
        )
        self.result_detail.setWordWrap(True)
        self.result_detail.setProperty("muted", True)
        layout.addWidget(self.result_detail)
        self.scope.currentIndexChanged.connect(self._render_endpoint)
        self.set_data(None, [], {})

    def set_data(
        self,
        health: dict | None,
        users: list[dict],
        tunnels: dict | None,
    ) -> None:
        selected = self.user.currentData() or {}
        selected_id = selected.get("id")
        self._health = health
        self._tunnels = tunnels or {}
        zrok = (health or {}).get("zrok") or {}
        remote = (health or {}).get("remote") or {}
        lan = (health or {}).get("lan") or {}
        web_url = str(zrok.get("public_url") or "") if zrok.get("state") == "online" else ""
        if not web_url and remote.get("enabled"):
            web_url = str(remote.get("public_url") or "")
        if not web_url:
            web_url = next(iter(lan.get("endpoints") or []), "")
        if not isinstance(web_url, str) or not web_url.startswith("https://"):
            web_url = ""
        self.web_address.setText(web_url)
        self.web_open.setEnabled(bool(web_url))
        self.web_copy.setEnabled(bool(web_url))
        self.user.blockSignals(True)
        self.user.clear()
        for user in users:
            if not user.get("enabled", True):
                continue
            label = str(user.get("display_name") or user.get("username") or "Пользователь")
            username = str(user.get("username") or "")
            self.user.addItem(f"{label} ({username})" if username else label, user)
        if selected_id:
            for index in range(self.user.count()):
                item = self.user.itemData(index) or {}
                if item.get("id") == selected_id:
                    self.user.setCurrentIndex(index)
                    break
        self.user.blockSignals(False)
        clear_layout(self.users_rows)
        self._users_count = len(users)
        self.users_toggle.setText(
            f"Пользователи ({len(users)})  {'▾' if self.users_toggle.isChecked() else '▸'}"
        )
        if not users:
            empty = QLabel("Пользователей пока нет.")
            empty.setProperty("muted", True)
            self.users_rows.addWidget(empty)
        for item in users:
            card = QFrame()
            card.setProperty("card", True)
            row = QHBoxLayout(card)
            state = str(item.get("presence_state") or "offline")
            state_colors = {
                "offline": "#8b949e",
                "online": "#f59e0b",
                "connected": "#22c55e",
                "transferring": "#3b82f6",
                "deleting": "#ef4444",
            }
            status_dot = QLabel("●")
            status_dot.setToolTip(str(item.get("presence_detail") or "Не в сети"))
            status_dot.setStyleSheet(
                f"font-size: 22px; color: {state_colors.get(state, state_colors['offline'])};"
            )
            row.addWidget(status_dot, 0, Qt.AlignmentFlag.AlignTop)
            text = QVBoxLayout()
            name = str(item.get("display_name") or item.get("username") or "Пользователь")
            title = QLabel(name)
            title.setStyleSheet("font-weight: 700;")
            grants = item.get("space_grants") or []
            detail = QLabel(
                f"{'Администратор' if item.get('role') == 'admin' else 'Обычный пользователь'} · "
                f"пространств: {len(grants)} · "
                f"{'включён' if item.get('enabled', True) else 'отключён'}"
            )
            detail.setProperty("muted", True)
            dates = QLabel(
                f"Создан: {self._display_time(item.get('created_at'))} · "
                f"Последний вход: {self._display_time(item.get('last_login_at'))} · "
                f"{item.get('presence_detail') or 'Не в сети'}"
            )
            dates.setProperty("muted", True)
            dates.setWordWrap(True)
            text.addWidget(title)
            text.addWidget(detail)
            text.addWidget(dates)
            activity = str(item.get("activity_detail") or "").strip()
            if activity:
                activity_label = QLabel(activity)
                activity_label.setWordWrap(True)
                activity_label.setStyleSheet(
                    f"color: {state_colors.get(state, state_colors['offline'])};"
                )
                text.addWidget(activity_label)
            row.addLayout(text, 1)
            user_id = str(item.get("id") or "")
            username = str(item.get("username") or "")
            email = str(item.get("email") or "")
            copy_login = QPushButton("Копировать логин")
            copy_login.clicked.connect(
                lambda _checked=False, value=username: (
                    QGuiApplication.clipboard().setText(value)
                )
            )
            edit = QPushButton("Изменить")
            edit.clicked.connect(
                lambda _checked=False, value=user_id: self.edit_user_requested.emit(value)
            )
            delete_user = QPushButton("Удалить")
            delete_user.setEnabled(bool(item.get("enabled", True)))
            delete_user.setToolTip(
                "Безопасно отключает вход пользователя; его файлы и журнал сохраняются."
            )
            delete_user.clicked.connect(
                lambda _checked=False, value=user_id: self.delete_user_requested.emit(value)
            )
            password = QPushButton("Новый пароль")
            password.clicked.connect(
                lambda _checked=False, value=user_id, label=name: (
                    self.reset_password_requested.emit(value, label)
                )
            )
            email_access = QPushButton("Файл на email")
            email_access.setEnabled(bool(email))
            email_access.clicked.connect(
                lambda _checked=False, value=user_id, recipient=email: (
                    self.email_access_requested.emit(value, recipient)
                )
            )
            row.addWidget(copy_login)
            managed_password = str(item.get("managed_password") or "")
            password_value = QLineEdit(managed_password or "Пароль не сохранён в Manager")
            password_value.setReadOnly(True)
            password_value.setEchoMode(QLineEdit.EchoMode.Password)
            password_value.setMinimumWidth(190)
            show_password = QPushButton("Показать")
            show_password.setEnabled(bool(managed_password))
            show_password.clicked.connect(
                lambda _checked=False, field=password_value, button=show_password: (
                    field.setEchoMode(
                        QLineEdit.EchoMode.Normal
                        if field.echoMode() == QLineEdit.EchoMode.Password
                        else QLineEdit.EchoMode.Password
                    ),
                    button.setText(
                        "Скрыть" if field.echoMode() == QLineEdit.EchoMode.Normal else "Показать"
                    ),
                )
            )
            copy_password = QPushButton("Копировать пароль")
            copy_password.setEnabled(bool(managed_password))
            copy_password.clicked.connect(
                lambda _checked=False, value=managed_password: (
                    QGuiApplication.clipboard().setText(value)
                )
            )
            row.addWidget(password_value)
            row.addWidget(show_password)
            row.addWidget(copy_password)
            row.addWidget(delete_user)
            row.addWidget(edit)
            row.addWidget(password)
            row.addWidget(email_access)
            copy_login.setVisible(False)
            password_value.setVisible(False)
            show_password.setVisible(False)
            copy_password.setVisible(False)
            password.setVisible(False)
            email_access.setVisible(False)
            self.users_rows.addWidget(card)
        self._render_endpoint()

    def _toggle_users(self, expanded: bool) -> None:
        self.users_container.setVisible(expanded)
        self.users_toggle.setText(
            f"Пользователи ({self._users_count})  {'▾' if expanded else '▸'}"
        )

    @staticmethod
    def _display_time(value: object) -> str:
        if not value:
            return "никогда"
        try:
            return datetime.fromisoformat(str(value)).astimezone().strftime("%d.%m.%Y %H:%M")
        except ValueError:
            return str(value)

    def selected_endpoint(self, mode: str | None = None) -> tuple[str, str]:
        if not self._health:
            return "", ""
        selected_mode = mode or str(self.scope.currentData())
        if selected_mode == "internet":
            zrok = {
                **(self._health.get("zrok") or {}),
                **(self._tunnels.get("zrok") or {}),
            }
            public_url = str(zrok.get("public_url") or "").rstrip("/")
            if (
                zrok.get("state") == "online"
                and zrok.get("pairing_enabled")
                and public_url.startswith("https://")
            ):
                return public_url, ""
            remote = self._health.get("remote") or {}
            remote_url = str(remote.get("public_url") or "").rstrip("/")
            if remote.get("enabled") and remote.get("pairing_enabled") and remote_url:
                return remote_url, str(remote.get("fingerprint") or "")
            return "", ""
        lan = self._health.get("lan") or {}
        endpoints = lan.get("endpoints") or []
        if lan.get("enabled") and endpoints:
            return str(endpoints[0]).rstrip("/"), str(lan.get("fingerprint") or "")
        return "", ""

    def show_code(self, code: str, detail: str, pairing_link: str = "") -> None:
        self.code.setText(code)
        try:
            locator = parse_server_code(code)
            if locator is None:
                legacy = parse_connection_code(code)
                if legacy is None:
                    raise ValueError("invalid server code")
                username = legacy.username
                link = pairing_link or build_pairing_uri(
                    legacy.code,
                    legacy.server_url,
                    legacy.certificate_fingerprint,
                    legacy.username,
                )
            else:
                username = locator.username
                link = pairing_link
            self.generated_username.setText(username)
            self.copy_button.setEnabled(
                bool(username and self.generated_password.text())
            )
            self.invitation_link.setText(link)
            self.copy_link_button.setEnabled(bool(link))
            self.qr_label.setText("")
            if link:
                self.qr_label.setPixmap(self._qr_pixmap(link))
            else:
                self.qr_label.setText("Одноразовая ссылка не создана")
        except ValueError as exc:
            self.invitation_link.clear()
            self.copy_link_button.setEnabled(False)
            self.qr_label.setPixmap(QPixmap())
            self.qr_label.setText(str(exc))
        self.result_detail.setText(detail)

    def show_error(self, message: str) -> None:
        self.result_detail.setText(message)

    def set_dynamic_code(self, value: dict | None) -> None:
        value = value or {}
        self.dynamic_code.setText(str(value.get("code") or ""))
        self._dynamic_expires_at = int(value.get("expires_at") or 0)
        self._dynamic_refresh_requested = False
        self.copy_dynamic_button.setEnabled(bool(self.dynamic_code.text()))
        self._update_dynamic_countdown()

    def _update_dynamic_countdown(self) -> None:
        remaining = max(0, self._dynamic_expires_at - int(time.time()))
        if not self.dynamic_code.text():
            self.dynamic_countdown.setText("Код недоступен")
            return
        minutes, seconds = divmod(remaining, 60)
        self.dynamic_countdown.setText(f"Сменится через {minutes:02d}:{seconds:02d}")
        if remaining == 0 and not self._dynamic_refresh_requested:
            self._dynamic_refresh_requested = True
            self.refresh_button.click()

    def copy_code(self) -> None:
        if not self.generated_username.text() or not self.generated_password.text():
            return
        payload = (
            f"Логин: {self.generated_username.text()}\n"
            f"Пароль: {self.generated_password.text()}"
        )
        QGuiApplication.clipboard().setText(payload)
        self.copy_button.setText("Скопировано")

    def copy_link(self) -> None:
        if self.invitation_link.text():
            QGuiApplication.clipboard().setText(self.invitation_link.text())
            self.copy_link_button.setText("Скопировано")

    @staticmethod
    def _qr_pixmap(payload: str) -> QPixmap:
        qr = qrcode.QRCode(version=None, box_size=7, border=2)
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
            220,
            220,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )

    def _request_generation(self) -> None:
        user = self.user.currentData() or {}
        if not user:
            self.show_error("Сначала создайте пользователя в настройках сервера.")
            return
        mode = str(self.scope.currentData())
        self.generated_password.setText(str(user.get("managed_password") or ""))
        endpoint, _fingerprint = self.selected_endpoint(mode)
        if not endpoint:
            if mode == "internet":
                self.show_error(
                    "Интернет-вход пока не работает. Включите zrok или публичный HTTPS-вход "
                    "в «Настройки → Удалённый доступ», затем обновите состояние."
                )
            else:
                self.show_error(
                    "Локальный HTTPS-вход выключен. Включите его в «Настройки → Сеть»."
                )
            return
        self.generation_requested.emit(
            str(user.get("id") or ""),
            str(user.get("username") or ""),
            mode,
        )

    def _render_endpoint(self) -> None:
        endpoint, _fingerprint = self.selected_endpoint()
        if endpoint:
            self.endpoint.setText(endpoint)
            self.generate_button.setEnabled(self.user.count() > 0)
        else:
            self.endpoint.setText(
                "Интернет-вход не настроен"
                if self.scope.currentData() == "internet"
                else "Локальный HTTPS-вход не настроен"
            )
            self.generate_button.setEnabled(False)


class ConnectionPage(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 16, 24)
        layout.setSpacing(18)
        layout.addWidget(
            make_header(
                "Подключение",
                "Создание безопасного кода для клиента без ручного ввода IP-адреса.",
            )
        )
        self.panel = ConnectionCodePanel()
        layout.addWidget(self.panel)
        layout.addStretch()
