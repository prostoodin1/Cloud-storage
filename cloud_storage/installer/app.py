from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QLabel,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from cloud_storage.ui.theme import apply_theme, create_app_icon
from cloud_storage.updates import UpdateError, UpdateInfo, UpdateService


class _Signals(QObject):
    success = Signal(object)
    failure = Signal(str)


class _Task(QRunnable):
    def __init__(self, function: Callable[[], object]) -> None:
        super().__init__()
        self.function = function
        self.signals = _Signals()

    def run(self) -> None:
        try:
            self.signals.success.emit(self.function())
        except (OSError, UpdateError, ValueError) as exc:
            self.signals.failure.emit(str(exc))


def installer_data_directory(product: str) -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return root / "CloudStorageInstaller" / product


class InstallerWindow(QWidget):
    def __init__(self, product: str) -> None:
        super().__init__()
        if product not in {"client", "server"}:
            raise ValueError("unknown installer product")
        self.product = product
        self.product_name = "Cloud Storage Client" if product == "client" else "Cloud Storage Server"
        self.service = UpdateService(product, installer_data_directory(product))
        self.versions: list[UpdateInfo] = []
        self.tasks: set[_Task] = set()
        self.pool = QThreadPool.globalInstance()
        self.setWindowTitle(f"Установщик {self.product_name}")
        self.setWindowIcon(create_app_icon())
        self.setMinimumSize(620, 470)
        self._build_ui()
        self.refresh_versions()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 28, 28, 28)
        layout.setSpacing(16)
        title = QLabel(f"Установить {self.product_name}")
        title.setObjectName("PageTitle")
        subtitle = QLabel(
            "Этот установщик постоянный: сохраните его один раз. Он проверяет подписанный "
            "каталог, позволяет выбрать версию и перед запуском сверяет размер и SHA-256."
        )
        subtitle.setWordWrap(True)
        subtitle.setProperty("muted", True)
        layout.addWidget(title)
        layout.addWidget(subtitle)

        card = QFrame()
        card.setProperty("card", True)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(20, 20, 20, 20)
        card_layout.setSpacing(12)
        self.mode = QComboBox()
        self.mode.addItem("Автоматически установить последнюю стабильную", "latest")
        self.mode.addItem("Выбрать конкретную версию", "specific")
        self.mode.currentIndexChanged.connect(self._update_mode)
        self.version = QComboBox()
        self.version.currentIndexChanged.connect(self._show_selected)
        self.notes = QTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setMinimumHeight(150)
        self.status = QLabel("Получаем доступные версии…")
        self.status.setWordWrap(True)
        self.status.setProperty("muted", True)
        self.refresh = QPushButton("Обновить список версий")
        self.refresh.clicked.connect(self.refresh_versions)
        self.install = QPushButton("Скачать и установить")
        self.install.setProperty("primary", True)
        self.install.setEnabled(False)
        self.install.clicked.connect(self.install_selected)
        card_layout.addWidget(QLabel("Режим установки"))
        card_layout.addWidget(self.mode)
        card_layout.addWidget(QLabel("Доступная версия"))
        card_layout.addWidget(self.version)
        card_layout.addWidget(self.notes)
        card_layout.addWidget(self.status)
        card_layout.addWidget(self.refresh)
        card_layout.addWidget(self.install)
        layout.addWidget(card)
        self._update_mode()

    def _run(
        self,
        function: Callable[[], object],
        success: Callable[[object], None],
    ) -> None:
        task = _Task(function)
        self.tasks.add(task)
        task.signals.success.connect(success)
        task.signals.failure.connect(self._failed)
        task.signals.success.connect(lambda _value, item=task: self.tasks.discard(item))
        task.signals.failure.connect(lambda _message, item=task: self.tasks.discard(item))
        self.pool.start(task)

    def refresh_versions(self) -> None:
        self.refresh.setEnabled(False)
        self.install.setEnabled(False)
        self.status.setText("Проверяем подписанный каталог версий…")
        self._run(lambda: self.service.list_versions("stable"), self._versions_loaded)

    def _versions_loaded(self, value: object) -> None:
        self.refresh.setEnabled(True)
        self.versions = list(value) if isinstance(value, list) else []
        self.version.clear()
        for info in self.versions:
            self.version.addItem(f"Версия {info.version} · {info.published_at}", info.version)
        self.status.setText(
            f"Найдено версий: {len(self.versions)}. Пакеты проверяются цифровой подписью."
        )
        self.install.setEnabled(bool(self.versions))
        self._update_mode()
        self._show_selected()

    def _selected(self) -> UpdateInfo | None:
        if not self.versions:
            return None
        if self.mode.currentData() == "latest":
            return self.versions[0]
        version = str(self.version.currentData() or "")
        return next((item for item in self.versions if item.version == version), None)

    def _update_mode(self) -> None:
        self.version.setEnabled(self.mode.currentData() == "specific")
        self._show_selected()

    def _show_selected(self) -> None:
        info = self._selected()
        self.notes.setPlainText(info.release_notes if info else "Версии пока не загружены.")
        if info:
            self.install.setText(f"Скачать и установить {info.version}")

    def install_selected(self) -> None:
        info = self._selected()
        if info is None:
            return
        self.install.setEnabled(False)
        self.refresh.setEnabled(False)
        self.status.setText(
            f"Скачиваем {info.version}; затем проверим подпись каталога, размер и SHA-256…"
        )
        self._run(lambda: (info, self.service.download(info)), self._downloaded)

    def _downloaded(self, value: object) -> None:
        info, path = value  # type: ignore[misc]
        self.status.setText("Пакет проверен. Запускаем установку с правами администратора…")
        try:
            self.service.launch_installer(path, info)
        except (OSError, UpdateError) as exc:
            self._failed(str(exc))
            return
        QApplication.quit()

    def _failed(self, message: str) -> None:
        self.refresh.setEnabled(True)
        self.install.setEnabled(bool(self.versions))
        self.status.setText(message)
        QMessageBox.warning(self, "Установка не выполнена", message)


def run_installer(product: str) -> int:
    app = QApplication(sys.argv)
    apply_theme(app)
    window = InstallerWindow(product)
    window.show()
    return app.exec()
