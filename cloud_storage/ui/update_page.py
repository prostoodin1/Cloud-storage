from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from cloud_storage import __version__
from cloud_storage.ui.widgets import make_header
from cloud_storage.updates import (
    UpdateError,
    UpdateInfo,
    UpdatePreferences,
    UpdateService,
    is_frozen_windows,
    version_key,
)


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
    """Signed update center shared by Server Manager and Desktop Client."""

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
        self.preferences = self.service.load_preferences()
        self.versions: list[UpdateInfo] = []
        self.info: UpdateInfo | None = None
        self.installer: Path | None = None
        self._automatic = False
        self._tasks: set[_Task] = set()
        self._build_ui()
        if is_frozen_windows() and self.preferences.policy != "manual":
            QTimer.singleShot(8000, self._auto_check)

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(4, 4, 18, 24)
        layout.setSpacing(16)
        layout.addWidget(
            make_header(
                "Обновления",
                "Выберите режим, канал и конкретную версию. Каждый каталог подписан Ed25519, "
                "а установщик повторно проверяется по SHA-256 перед запуском.",
            )
        )

        settings_card = QFrame()
        settings_card.setProperty("card", True)
        settings_layout = QVBoxLayout(settings_card)
        settings_layout.setContentsMargins(18, 16, 18, 16)
        settings_title = QLabel("Как устанавливать обновления")
        settings_title.setObjectName("SectionTitle")
        settings_layout.addWidget(settings_title)
        form = QFormLayout()
        form.setVerticalSpacing(12)
        self.policy_combo = QComboBox()
        self.policy_combo.addItem("Вручную — только по кнопке", "manual")
        self.policy_combo.addItem("Автоматически скачивать", "download")
        self.policy_combo.addItem("Автоматически устанавливать при запуске", "install")
        self.policy_combo.setCurrentIndex(
            max(0, self.policy_combo.findData(self.preferences.policy))
        )
        self.channel_combo = QComboBox()
        self.channel_combo.addItem("Стабильные версии", "stable")
        self.channel_combo.addItem("Стабильные и тестовые", "beta")
        self.channel_combo.setCurrentIndex(
            max(0, self.channel_combo.findData(self.preferences.channel))
        )
        form.addRow("Режим", self.policy_combo)
        form.addRow("Канал", self.channel_combo)
        settings_layout.addLayout(form)
        self.policy_help = QLabel()
        self.policy_help.setWordWrap(True)
        self.policy_help.setProperty("muted", True)
        settings_layout.addWidget(self.policy_help)
        self.policy_combo.currentIndexChanged.connect(self._preferences_changed)
        self.channel_combo.currentIndexChanged.connect(self._preferences_changed)
        self._update_policy_help()
        layout.addWidget(settings_card)

        card = QFrame()
        card.setProperty("card", True)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(18, 16, 18, 16)
        self.version_label = QLabel(f"Установлена версия {__version__}")
        self.version_label.setStyleSheet("font-size: 18px; font-weight: 700;")
        self.status_label = QLabel(
            "Нажмите «Проверить версии». Автоматическая проверка выполняется только "
            "при выбранном автоматическом режиме."
        )
        self.status_label.setProperty("muted", True)
        self.status_label.setWordWrap(True)
        version_form = QFormLayout()
        self.version_combo = QComboBox()
        self.version_combo.addItem("Сначала проверьте доступные версии", None)
        self.version_combo.setEnabled(False)
        self.version_combo.currentIndexChanged.connect(self._version_selected)
        version_form.addRow("Версия для скачивания", self.version_combo)
        buttons = QHBoxLayout()
        self.check_button = QPushButton("Проверить версии")
        self.check_button.setProperty("primary", True)
        self.check_button.clicked.connect(self.check)
        self.download_button = QPushButton("Скачать выбранную")
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
        card_layout.addLayout(version_form)
        card_layout.addLayout(buttons)
        layout.addWidget(card)

        notes_title = QLabel("Что изменилось в выбранной версии")
        notes_title.setObjectName("SectionTitle")
        layout.addWidget(notes_title)
        self.notes = QTextBrowser()
        self.notes.setMinimumHeight(220)
        self.notes.setPlaceholderText("После проверки здесь появится описание версии.")
        layout.addWidget(self.notes)
        safety = QLabel(
            "Откат на старую версию разрешён только вручную. Настройки и пользовательские данные "
            "установщик не удаляет, однако перед откатом рекомендуется создать резервную копию."
        )
        safety.setWordWrap(True)
        safety.setProperty("emptyState", True)
        layout.addWidget(safety)
        layout.addStretch()
        scroll.setWidget(content)
        outer.addWidget(scroll)

    def _preferences_changed(self) -> None:
        self.preferences = UpdatePreferences(
            policy=str(self.policy_combo.currentData()),
            channel=str(self.channel_combo.currentData()),
        )
        self.service.save_preferences(self.preferences)
        self._update_policy_help()
        self.versions.clear()
        self.info = None
        self.installer = None
        self.version_combo.clear()
        self.version_combo.addItem("Проверьте версии для выбранного канала", None)
        self.version_combo.setEnabled(False)
        self.download_button.setEnabled(False)
        self.install_button.setEnabled(False)
        self.notes.clear()
        self.status_label.setText("Настройка сохранена. Нажмите «Проверить версии».")

    def _update_policy_help(self) -> None:
        descriptions = {
            "manual": "Приложение не обращается к серверу обновлений без нажатия кнопки.",
            "download": (
                "При запуске приложение проверит каталог и скачает новую версию. "
                "Установку вы запускаете отдельной кнопкой."
            ),
            "install": (
                "При запуске новая версия будет проверена, скачана и установлена автоматически. "
                "Server Manager перед установкой корректно остановит Core."
            ),
        }
        self.policy_help.setText(descriptions[str(self.policy_combo.currentData())])

    def check(self) -> None:
        self._automatic = False
        self._start_check()

    def _auto_check(self) -> None:
        if self.preferences.policy == "manual" or self._tasks:
            return
        self._automatic = True
        self._start_check()

    def _start_check(self) -> None:
        channel = str(self.channel_combo.currentData())
        self._busy("Получаем и проверяем подписанный каталог версий…")
        self._run(lambda: self.service.list_versions(channel), self._checked)

    def _checked(self, result: object) -> None:
        self._idle()
        if not isinstance(result, list) or not all(
            isinstance(item, UpdateInfo) for item in result
        ):
            self.status_label.setText("Сервер обновлений вернул неизвестный ответ.")
            return
        self.versions = result
        self.version_combo.blockSignals(True)
        self.version_combo.clear()
        for info in self.versions:
            marker = (
                "новая"
                if info.newer_than_current
                else "установлена"
                if version_key(info.version) == version_key(__version__)
                else "старая"
            )
            channel = "стабильная" if info.channel == "stable" else "тестовая"
            self.version_combo.addItem(
                f"{info.version} · {channel} · {marker}", info.version
            )
        self.version_combo.blockSignals(False)
        self.version_combo.setEnabled(bool(self.versions))
        newest_index = next(
            (index for index, info in enumerate(self.versions) if info.newer_than_current),
            0,
        )
        self.version_combo.setCurrentIndex(newest_index)
        self._version_selected(newest_index)
        newer = [item for item in self.versions if item.newer_than_current]
        if self._automatic and newer and self.preferences.policy in {"download", "install"}:
            self.info = newer[0]
            self.version_combo.setCurrentIndex(self.versions.index(self.info))
            self.download(automatic=True)

    def _version_selected(self, index: int) -> None:
        if not 0 <= index < len(self.versions):
            return
        self.info = self.versions[index]
        self.installer = None
        self.install_button.setEnabled(False)
        self.download_button.setEnabled(True)
        self.notes.setPlainText(self.info.release_notes or "Описание версии не добавлено.")
        if self.info.newer_than_current:
            message = f"Версия {self.info.version} новее установленной. Подпись каталога проверена."
        elif version_key(self.info.version) == version_key(__version__):
            message = f"Выбрана текущая версия {self.info.version}; её можно скачать повторно."
        else:
            message = (
                f"Выбрана старая версия {self.info.version}. Это откат; установка потребует "
                "отдельного подтверждения."
            )
        self.status_label.setText(message)

    def download(self, _checked: bool = False, *, automatic: bool = False) -> None:
        if self.info is None:
            return
        self._automatic = automatic
        self._busy(f"Скачиваем версию {self.info.version} и проверяем SHA-256…")
        self._run(lambda: self.service.download(self.info), self._downloaded)

    def _downloaded(self, result: object) -> None:
        self._idle()
        if not isinstance(result, Path):
            self.status_label.setText("Не удалось определить скачанный установщик.")
            return
        self.installer = result
        self.status_label.setText(
            f"Версия {self.info.version if self.info else '—'} скачана. "
            "Подпись каталога и SHA-256 установщика проверены."
        )
        self.download_button.setEnabled(self.info is not None)
        self.install_button.setEnabled(is_frozen_windows())
        if not is_frozen_windows():
            self.status_label.setText(
                self.status_label.text() + " Установка доступна в собранном Windows-приложении."
            )
        elif self._automatic and self.preferences.policy == "install":
            self._launch_install(require_confirmation=False)

    def install(self) -> None:
        self._launch_install(require_confirmation=True)

    def _launch_install(self, *, require_confirmation: bool) -> None:
        if self.installer is None or self.info is None:
            return
        relation = version_key(self.info.version) < version_key(__version__)
        action = "откатит" if relation else "обновит"
        if require_confirmation and QMessageBox.question(
            self,
            "Установить выбранную версию",
            f"Приложение закроется, а установщик {action} его до версии {self.info.version}. "
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
        self.version_combo.setEnabled(False)
        self.download_button.setEnabled(False)
        self.install_button.setEnabled(False)

    def _idle(self) -> None:
        self.check_button.setEnabled(True)
        self.version_combo.setEnabled(bool(self.versions))

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
