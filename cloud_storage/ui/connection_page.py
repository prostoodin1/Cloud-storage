from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from cloud_storage.ui.widgets import make_header


class ConnectionCodePanel(QFrame):
    generation_requested = Signal(str, str, str)

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
        self.generate_button = QPushButton("Создать код на 15 минут")
        self.generate_button.setProperty("primary", True)
        self.generate_button.clicked.connect(self._request_generation)
        self.refresh_button = QPushButton("Обновить состояние")
        controls.addWidget(self.generate_button)
        controls.addWidget(self.refresh_button)
        controls.addStretch()
        layout.addLayout(controls)

        self.code = QLineEdit()
        self.code.setReadOnly(True)
        self.code.setPlaceholderText("Здесь появится код вида CS1.…")
        self.code.setMinimumHeight(42)
        code_row = QHBoxLayout()
        code_row.addWidget(self.code, 1)
        self.copy_button = QPushButton("Копировать")
        self.copy_button.setEnabled(False)
        self.copy_button.clicked.connect(self.copy_code)
        code_row.addWidget(self.copy_button)
        layout.addLayout(code_row)

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
        self.result_detail.setText(detail)

    def show_error(self, message: str) -> None:
        self.result_detail.setText(message)

    def copy_code(self) -> None:
        if not self.code.text():
            return
        QGuiApplication.clipboard().setText(self.code.text())
        self.copy_button.setText("Скопировано")

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
