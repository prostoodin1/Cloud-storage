from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from cloud_storage import __version__
from cloud_storage.ui.widgets import make_header
from cloud_storage.updates import UpdateError, UpdateInfo, UpdateService, is_frozen_windows


class _Signals(QObject):
    success = Signal(object)
    failure = Signal(str)


class _Task(QRunnable):
    def __init__(self, function: Callable[[], Any]) -> None:
        super().__init__()
        self.function = function
        self.signals = _Signals()

    def run(self) -> None:
        try:
            self.signals.success.emit(self.function())
        except (OSError, UpdateError, ValueError) as exc:
            self.signals.failure.emit(str(exc))


class UpdatePage(QWidget):
    def __init__(
        self,
        product: str,
        data_directory: Path,
        *,
        before_install: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self.service = UpdateService(product, data_directory)
        self.product = product
        self.before_install = before_install
        self.info: UpdateInfo | None = None
        self.installer: Path | None = None
        self._tasks: set[_Task] = set()
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 16, 24)
        layout.setSpacing(16)
        layout.addWidget(
            make_header(
                "Обновления",
                "Подписанные пакеты проверяются перед установкой и не затрагивают пользовательские данные.",
            )
        )
        card = QFrame()
        card.setProperty("card", True)
        card_layout = QVBoxLayout(card)
        self.version_label = QLabel(f"Установлена версия {__version__}")
        self.version_label.setStyleSheet("font-size: 18px; font-weight: 700;")
        self.status_label = QLabel("Проверка выполняется только по вашей команде.")
        self.status_label.setProperty("muted", True)
        self.status_label.setWordWrap(True)
        buttons = QHBoxLayout()
        self.check_button = QPushButton("Проверить обновления")
        self.check_button.setProperty("primary", True)
        self.check_button.clicked.connect(self.check)
        self.download_button = QPushButton("Скачать")
        self.download_button.clicked.connect(self.download)
        self.download_button.setEnabled(False)
        self.install_button = QPushButton("Установить и перезапустить")
        self.install_button.clicked.connect(self.install)
        self.install_button.setEnabled(False)
        buttons.addWidget(self.check_button)
        buttons.addWidget(self.download_button)
        buttons.addWidget(self.install_button)
        buttons.addStretch()
        card_layout.addWidget(self.version_label)
        card_layout.addWidget(self.status_label)
        card_layout.addLayout(buttons)
        layout.addWidget(card)
        self.notes = QTextBrowser()
        self.notes.setPlaceholderText("Здесь появится описание новой версии.")
        layout.addWidget(self.notes, 1)

    def check(self) -> None:
        self._busy("Проверяем подписанный манифест…")
        self._run(self.service.check, self._checked)

    def _checked(self, result: object) -> None:
        self._idle()
        if not isinstance(result, UpdateInfo):
            self.status_label.setText("Сервер обновлений вернул неизвестный ответ.")
            return
        self.info = result
        self.notes.setPlainText(result.release_notes or "Описание версии не добавлено.")
        if result.newer_than_current:
            self.status_label.setText(
                f"Доступна версия {result.version} · канал {result.channel}. Подпись проверена."
            )
            self.download_button.setEnabled(True)
        else:
            self.status_label.setText("Установлена актуальная версия.")

    def download(self) -> None:
        if self.info is None:
            return
        self._busy(f"Скачиваем версию {self.info.version} и проверяем SHA-256…")
        self._run(lambda: self.service.download(self.info), self._downloaded)

    def _downloaded(self, result: object) -> None:
        self._idle()
        if not isinstance(result, Path):
            self.status_label.setText("Не удалось определить скачанный установщик.")
            return
        self.installer = result
        self.status_label.setText("Установщик скачан, подпись манифеста и SHA-256 проверены.")
        self.install_button.setEnabled(is_frozen_windows())
        if not is_frozen_windows():
            self.status_label.setText(
                self.status_label.text() + " Установка доступна в собранном Windows-приложении."
            )

    def install(self) -> None:
        if self.installer is None or self.info is None:
            return
        if QMessageBox.question(
            self,
            "Установить обновление",
            "Приложение закроется, установщик заменит программные файлы и запустит новую версию. "
            "Настройки и данные сохранятся. Продолжить?",
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            if self.before_install is not None:
                self.before_install()
            self.service.launch_installer(self.installer, self.info)
        except (OSError, UpdateError) as exc:
            QMessageBox.warning(self, "Обновление не запущено", str(exc))
            return
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _busy(self, text: str) -> None:
        self.status_label.setText(text)
        self.check_button.setEnabled(False)
        self.download_button.setEnabled(False)
        self.install_button.setEnabled(False)

    def _idle(self) -> None:
        self.check_button.setEnabled(True)

    def _run(self, function: Callable[[], Any], success: Callable[[object], None]) -> None:
        task = _Task(function)
        self._tasks.add(task)
        task.signals.success.connect(success)
        task.signals.failure.connect(self._failed)
        task.signals.success.connect(lambda _result, item=task: self._tasks.discard(item))
        task.signals.failure.connect(lambda _message, item=task: self._tasks.discard(item))
        QThreadPool.globalInstance().start(task)

    def _failed(self, message: str) -> None:
        self._idle()
        self.status_label.setText(f"Проверка не выполнена: {message}")
