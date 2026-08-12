from __future__ import annotations

import qrcode
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPixmap
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

from cloud_storage.pairing import build_pairing_uri, parse_connection_code
from cloud_storage.ui.widgets import make_header


class ConnectionCodePanel(QFrame):
    generation_requested = Signal(str, str, str)
    create_user_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("card", True)
        self._health: dict | None = None
        self._tunnels: dict = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        title = QLabel("Код подключения клиента")
        title.setStyleSheet("font-size: 18px; font-weight: 700;")
        description = QLabel(
            "Выберите пользователя и создайте один код. Человек вставит его во вкладке "
            "«Подключиться» — адрес сервера и защита TLS подставятся автоматически."
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

        self.access_tabs = QTabWidget()
        credentials_tab = QWidget()
        credentials_layout = QFormLayout(credentials_tab)
        self.generated_username = QLineEdit()
        self.generated_username.setReadOnly(True)
        self.generated_username.setPlaceholderText("Логин пользователя")
        self.code = QLineEdit()
        self.code.setReadOnly(True)
        self.code.setPlaceholderText("Здесь появится код вида CS1.…")
        self.code.setMinimumHeight(42)
        self.copy_button = QPushButton("Скопировать логин и код")
        self.copy_button.setEnabled(False)
        self.copy_button.clicked.connect(self.copy_code)
        credentials_layout.addRow("Логин", self.generated_username)
        credentials_layout.addRow("Код", self.code)
        credentials_layout.addRow("", self.copy_button)
        self.access_tabs.addTab(credentials_tab, "1. Логин и код")

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
        self._render_endpoint()

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

    def show_code(self, code: str, detail: str) -> None:
        self.code.setText(code)
        self.copy_button.setEnabled(bool(code))
        try:
            invitation = parse_connection_code(code)
            if invitation is None:
                raise ValueError("invalid connection code")
            link = build_pairing_uri(
                invitation.code,
                invitation.server_url,
                invitation.certificate_fingerprint,
                invitation.username,
            )
            self.generated_username.setText(invitation.username)
            self.invitation_link.setText(link)
            self.copy_link_button.setEnabled(True)
            self.qr_label.setText("")
            self.qr_label.setPixmap(self._qr_pixmap(link))
        except ValueError as exc:
            self.invitation_link.clear()
            self.copy_link_button.setEnabled(False)
            self.qr_label.setPixmap(QPixmap())
            self.qr_label.setText(str(exc))
        self.result_detail.setText(detail)

    def show_error(self, message: str) -> None:
        self.result_detail.setText(message)

    def copy_code(self) -> None:
        if not self.code.text():
            return
        payload = f"Логин: {self.generated_username.text()}\nКод: {self.code.text()}"
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
