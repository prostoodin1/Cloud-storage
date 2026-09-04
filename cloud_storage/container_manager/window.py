from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from cloud_storage import __version__
from cloud_storage.services.core_client import CoreApiError, CoreClient, CoreUnavailable
from cloud_storage.ui.widgets import make_header


class _TaskSignals(QObject):
    success = Signal(object)
    failure = Signal(str)
    finished = Signal()


class _Task(QRunnable):
    def __init__(self, operation: Callable[[], object]) -> None:
        super().__init__()
        self.operation = operation
        self.signals = _TaskSignals()

    def run(self) -> None:
        try:
            self.signals.success.emit(self.operation())
        except (CoreApiError, CoreUnavailable, OSError, ValueError) as exc:
            self.signals.failure.emit(str(exc))
        finally:
            self.signals.finished.emit()


class ContainerManagerWindow(QMainWindow):
    def __init__(
        self,
        client: CoreClient | None = None,
        *,
        smoke_test: bool = False,
    ) -> None:
        super().__init__()
        self.client = client or CoreClient.from_environment()
        self._cells: dict[str, dict[str, Any]] = {}
        self._busy = False
        self.setWindowTitle(f"Cloud Storage · Контейнеры · {__version__}")
        self.setMinimumSize(1040, 700)
        self.resize(1280, 800)
        self._build_ui()
        if not smoke_test:
            self.refresh()

    def activate(self) -> None:
        self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(22, 18, 22, 20)
        outer.setSpacing(14)
        outer.addWidget(
            make_header(
                "Контейнеры и пространства",
                "Отдельные изолированные боксы для скриптов, ботов и обработчиков.",
            )
        )
        top = QHBoxLayout()
        self.runtime_status = QLabel("Проверяем Docker…")
        self.runtime_status.setProperty("muted", True)
        top.addWidget(self.runtime_status, 1)
        refresh = QPushButton("Обновить")
        refresh.clicked.connect(self.refresh)
        top.addWidget(refresh)
        outer.addLayout(top)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        left = QFrame()
        left.setProperty("card", True)
        left_layout = QVBoxLayout(left)
        left_title = QLabel("БОКСЫ")
        left_title.setStyleSheet("font-weight: 700;")
        self.cell_list = QListWidget()
        self.cell_list.currentItemChanged.connect(self._select_cell)
        new_button = QPushButton("+ Новое пространство")
        new_button.setProperty("primary", True)
        new_button.clicked.connect(self.new_cell)
        left_layout.addWidget(left_title)
        left_layout.addWidget(self.cell_list, 1)
        left_layout.addWidget(new_button)
        splitter.addWidget(left)

        editor = QFrame()
        editor.setProperty("card", True)
        edit_layout = QVBoxLayout(editor)
        edit_layout.setContentsMargins(20, 18, 20, 18)
        self.editor_title = QLabel("Новое пространство")
        self.editor_title.setStyleSheet("font-size: 18px; font-weight: 700;")
        edit_layout.addWidget(self.editor_title)
        form = QFormLayout()
        form.setVerticalSpacing(11)
        self.name = QLineEdit()
        self.name.setPlaceholderText("Telegram-бот")
        self.image = QLineEdit("python:3.12-alpine")
        self.command = QPlainTextEdit("python\n-c\nprint('Cloud Storage container OK')")
        self.command.setMaximumHeight(110)
        self.command.setPlaceholderText("Один аргумент команды на строку")
        self.cpu = QDoubleSpinBox()
        self.cpu.setRange(0.25, 256.0)
        self.cpu.setSingleStep(0.25)
        self.cpu.setValue(0.5)
        self.cpu.setSuffix(" ядра")
        self.memory = QSpinBox()
        self.memory.setRange(64, 1_048_576)
        self.memory.setValue(256)
        self.memory.setSuffix(" MiB")
        self.storage = QSpinBox()
        self.storage.setRange(64, 10_240)
        self.storage.setValue(512)
        self.storage.setSuffix(" MiB")
        self.timeout = QSpinBox()
        self.timeout.setRange(1, 86_400)
        self.timeout.setValue(300)
        self.timeout.setSuffix(" сек")
        self.network = QCheckBox("Разрешить выход в интернет")
        self.network.setToolTip("Включайте только для ботов и интеграций, которым нужна сеть.")
        form.addRow("Название бокса", self.name)
        form.addRow("Образ Docker / Podman", self.image)
        form.addRow("Команда (аргумент на строку)", self.command)
        form.addRow("Процессор", self.cpu)
        form.addRow("Оперативная память", self.memory)
        form.addRow("Временное пространство", self.storage)
        form.addRow("Максимальное время", self.timeout)
        form.addRow("Сеть", self.network)
        edit_layout.addLayout(form)
        controls = QHBoxLayout()
        self.save_button = QPushButton("Создать")
        self.save_button.setProperty("primary", True)
        self.save_button.clicked.connect(self.save_cell)
        self.run_button = QPushButton("Запустить")
        self.run_button.clicked.connect(self.run_cell)
        self.delete_button = QPushButton("Удалить")
        self.delete_button.clicked.connect(self.delete_cell)
        controls.addWidget(self.save_button)
        controls.addWidget(self.run_button)
        controls.addWidget(self.delete_button)
        controls.addStretch()
        edit_layout.addLayout(controls)
        result_label = QLabel("ПОСЛЕДНИЙ РЕЗУЛЬТАТ")
        result_label.setStyleSheet("font-weight: 700;")
        self.result = QPlainTextEdit()
        self.result.setReadOnly(True)
        self.result.setPlaceholderText("Вывод контейнера появится здесь.")
        edit_layout.addWidget(result_label)
        edit_layout.addWidget(self.result, 1)
        splitter.addWidget(editor)
        splitter.setSizes([360, 760])
        outer.addWidget(splitter, 1)
        self.new_cell()

    def refresh(self) -> None:
        self._start_task(self.client.control_center, self._loaded)

    def _loaded(self, result: object) -> None:
        data = result if isinstance(result, dict) else {}
        sandbox = data.get("sandbox") or {}
        maximum = sandbox.get("automatic_max") or {}
        if sandbox.get("available"):
            self.runtime_status.setText(
                f"Готово · {sandbox.get('runtime')} · максимум {maximum.get('cpu', '—')} CPU, "
                f"{maximum.get('memory_mib', '—')} MiB RAM"
            )
        else:
            self.runtime_status.setText(
                "Docker/Podman не найден. Боксы можно настроить, запуск станет доступен после установки Docker."
            )
        self.cpu.setMaximum(max(0.25, float(maximum.get("cpu", 256))))
        self.memory.setMaximum(max(64, int(maximum.get("memory_mib", 1_048_576))))
        selected_id = self._selected_id()
        self._cells = {str(cell["id"]): cell for cell in data.get("cells", [])}
        self.cell_list.clear()
        selected_row = -1
        for row, cell in enumerate(self._cells.values()):
            item = QListWidgetItem(
                f"{cell['name']}\n{cell['cpu_limit']:g} CPU · {cell['memory_mib']} MiB · {cell['status']}"
            )
            item.setData(Qt.ItemDataRole.UserRole, cell["id"])
            self.cell_list.addItem(item)
            if cell["id"] == selected_id:
                selected_row = row
        if selected_row >= 0:
            self.cell_list.setCurrentRow(selected_row)

    def new_cell(self) -> None:
        self.cell_list.clearSelection()
        self.editor_title.setText("Новое пространство")
        self.name.clear()
        self.image.setText("python:3.12-alpine")
        self.command.setPlainText("python\n-c\nprint('Cloud Storage container OK')")
        self.cpu.setValue(min(0.5, self.cpu.maximum()))
        self.memory.setValue(min(256, self.memory.maximum()))
        self.storage.setValue(512)
        self.timeout.setValue(300)
        self.network.setChecked(False)
        self.result.clear()
        self.save_button.setText("Создать")
        self.run_button.setEnabled(False)
        self.delete_button.setEnabled(False)

    def _select_cell(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        if current is None:
            return
        cell = self._cells.get(str(current.data(Qt.ItemDataRole.UserRole)))
        if not cell:
            return
        self.editor_title.setText(str(cell["name"]))
        self.name.setText(str(cell["name"]))
        self.image.setText(str(cell["image"]))
        self.command.setPlainText("\n".join(str(arg) for arg in cell["command"]))
        self.cpu.setValue(float(cell["cpu_limit"]))
        self.memory.setValue(int(cell["memory_mib"]))
        self.storage.setValue(int(cell["storage_mib"]))
        self.timeout.setValue(int(cell["timeout_seconds"]))
        self.network.setChecked(bool(cell["network_enabled"]))
        self.result.setPlainText(str(cell.get("last_result") or ""))
        self.save_button.setText("Сохранить")
        self.run_button.setEnabled(cell.get("status") != "running")
        self.delete_button.setEnabled(cell.get("status") != "running")

    def save_cell(self) -> None:
        values = self._values()
        if not values["name"] or not values["command"]:
            QMessageBox.warning(self, "Проверьте настройки", "Нужны название и команда.")
            return
        cell_id = self._selected_id()
        operation = (
            (lambda: self.client.update_sandbox_cell(cell_id, values))
            if cell_id
            else (lambda: self.client.create_sandbox_cell(values))
        )
        self._start_task(operation, self._schedule_refresh)

    def run_cell(self) -> None:
        cell_id = self._selected_id()
        if not cell_id:
            return
        self.result.setPlainText("Контейнер запускается…")
        self._start_task(self._run_and_return(cell_id), self._run_complete)

    def _run_and_return(self, cell_id: str) -> Callable[[], object]:
        return lambda: self.client.run_sandbox_cell(cell_id)

    def _run_complete(self, result: object) -> None:
        cell = result if isinstance(result, dict) else {}
        self.result.setPlainText(str(cell.get("last_result") or "Запуск завершён без вывода."))
        self._schedule_refresh(cell)

    def delete_cell(self) -> None:
        cell_id = self._selected_id()
        if not cell_id:
            return
        answer = QMessageBox.question(
            self,
            "Удалить пространство?",
            "Настройки этого бокса будут удалены. Образ Docker и данные сервера не затрагиваются.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._start_task(lambda: self.client.delete_sandbox_cell(cell_id), self._deleted)

    def _deleted(self, _result: object) -> None:
        self.new_cell()
        self._schedule_refresh(_result)

    def _schedule_refresh(self, _result: object) -> None:
        QTimer.singleShot(50, self.refresh)

    def _selected_id(self) -> str:
        current = self.cell_list.currentItem()
        return str(current.data(Qt.ItemDataRole.UserRole) or "") if current else ""

    def _values(self) -> dict[str, Any]:
        return {
            "name": self.name.text().strip(),
            "image": self.image.text().strip(),
            "command": [line for line in self.command.toPlainText().splitlines() if line],
            "cpu_limit": self.cpu.value(),
            "memory_mib": self.memory.value(),
            "storage_mib": self.storage.value(),
            "timeout_seconds": self.timeout.value(),
            "network_enabled": self.network.isChecked(),
        }

    def _start_task(
        self,
        operation: Callable[[], object],
        success: Callable[[object], None],
    ) -> None:
        if self._busy:
            return
        self._busy = True
        self._set_controls_enabled(False)
        task = _Task(operation)
        task.signals.success.connect(success)
        task.signals.failure.connect(self._task_failed)
        task.signals.finished.connect(self._task_finished)
        QThreadPool.globalInstance().start(task)

    def _task_failed(self, message: str) -> None:
        QMessageBox.warning(self, "Операция не выполнена", message)

    def _task_finished(self) -> None:
        self._busy = False
        self._set_controls_enabled(True)

    def _set_controls_enabled(self, enabled: bool) -> None:
        self.save_button.setEnabled(enabled)
        self.run_button.setEnabled(enabled and bool(self._selected_id()))
        self.delete_button.setEnabled(enabled and bool(self._selected_id()))
