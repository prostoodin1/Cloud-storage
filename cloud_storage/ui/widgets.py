from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractSpinBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from cloud_storage.models import (
    ROLE_LABELS,
    STATUS_LABELS,
    DiskConfiguration,
    DiskSnapshot,
    DiskStatus,
)
from cloud_storage.services.disk_service import evaluate_status
from cloud_storage.ui.theme import COLORS


def format_bytes(value: int | None) -> str:
    if value is None:
        return "Недоступно"
    size = float(max(0, value))
    units = ("Б", "КБ", "МБ", "ГБ", "ТБ", "ПБ")
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.0f} {unit}" if unit in {"Б", "КБ"} else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ПБ"


def clear_layout(layout: QVBoxLayout | QHBoxLayout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        child = item.layout()
        if widget is not None:
            widget.deleteLater()
        elif child is not None:
            clear_layout(child)  # type: ignore[arg-type]


def apply_context_tooltips(root: QWidget) -> None:
    """Give every interactive configuration control a short plain-language hint."""
    for widget in root.findChildren(QWidget):
        if widget.toolTip().strip():
            continue
        if isinstance(widget, QAbstractButton):
            text = widget.text().replace("&", "").strip()
            if text:
                widget.setToolTip(f"Действие: {text}.")
        elif isinstance(widget, QLineEdit):
            hint = widget.placeholderText().strip()
            widget.setToolTip(hint or "Введите значение этой настройки.")
        elif isinstance(widget, QComboBox):
            widget.setToolTip("Выберите подходящий вариант из списка.")
        elif isinstance(widget, QAbstractSpinBox):
            widget.setToolTip("Измените числовое значение этой настройки.")


class StatusBadge(QLabel):
    _COLORS = {
        DiskStatus.HEALTHY: COLORS["green"],
        DiskStatus.ATTENTION: COLORS["yellow"],
        DiskStatus.CHECK_RECOMMENDED: COLORS["yellow"],
        DiskStatus.DATA_LOSS_RISK: COLORS["red"],
        DiskStatus.ALMOST_FULL: COLORS["yellow"],
        DiskStatus.WRITES_PAUSED: COLORS["yellow"],
        DiskStatus.MAINTENANCE: COLORS["blue"],
        DiskStatus.DISCONNECTED: COLORS["gray"],
        DiskStatus.UNAVAILABLE: COLORS["gray"],
        DiskStatus.UNCONFIGURED: COLORS["gray"],
    }

    def __init__(self, status: DiskStatus, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.set_status(status)

    def set_status(self, status: DiskStatus) -> None:
        color = self._COLORS[status]
        self.setText(f"●  {STATUS_LABELS[status]}")
        self.setStyleSheet(
            f"color: {color}; background: #18{color[1:]}; border: 1px solid #55{color[1:]}; "
            "border-radius: 10px; padding: 3px 9px; font-size: 12px; font-weight: 600;"
        )
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)


class StatCard(QFrame):
    def __init__(self, label: str, value: str = "—", detail: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setProperty("card", True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(5)
        self.label = QLabel(label.upper())
        self.label.setObjectName("MetricLabel")
        self.value = QLabel(value)
        self.value.setObjectName("MetricValue")
        self.detail = QLabel(detail)
        self.detail.setProperty("muted", True)
        self.detail.setWordWrap(True)
        layout.addWidget(self.label)
        layout.addWidget(self.value)
        layout.addWidget(self.detail)

    def set_value(self, value: str, detail: str = "") -> None:
        self.value.setText(value)
        self.detail.setText(detail)


class DiskCard(QFrame):
    selected = Signal(str)
    action_requested = Signal(str, str)

    def __init__(
        self,
        disk: DiskSnapshot,
        configuration: DiskConfiguration | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.disk_id = disk.id
        self.setProperty("card", True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        top = QHBoxLayout()
        title = QLabel(
            (configuration.display_name if configuration else "") or disk.label or disk.mountpoint
        )
        title.setObjectName("SectionTitle")
        activity = QLabel("●")
        busy = (
            (disk.utilization_percent or 0) >= 5
            or (disk.read_speed_mbps or 0) >= 0.1
            or (disk.write_speed_mbps or 0) >= 0.1
        )
        configured_active = configuration is not None and configuration.mode.value == "active"
        if not disk.available or (disk.health_detail and "error" in disk.health_detail.casefold()):
            activity_color, activity_text = COLORS["red"], "Ошибка"
        elif busy:
            activity_color, activity_text = COLORS["blue"], "I/O: диск занят"
        elif configured_active:
            activity_color, activity_text = COLORS["green"], "Активен"
        else:
            activity_color, activity_text = COLORS["gray"], "Простаивает"
        activity.setStyleSheet(f"color: {activity_color}; font-size: 18px;")
        activity.setToolTip(activity_text)
        mount = QLabel(disk.mountpoint)
        mount.setProperty("muted", True)
        mount.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        top.addWidget(activity)
        top.addWidget(title, 1)
        top.addWidget(mount)
        actions_button = QToolButton()
        actions_button.setText("Действия ▾")
        actions_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        actions_menu = QMenu(actions_button)
        for label, action_name in (
            ("Открыть в Проводнике", "open"),
            ("Найти ошибки", "check"),
            ("Оптимизировать", "optimize"),
            ("Форматировать…", "format"),
            ("Удалить из Manager", "remove"),
        ):
            action = actions_menu.addAction(label)
            if disk.is_system and action_name in {"optimize", "format", "remove"}:
                action.setEnabled(False)
            action.triggered.connect(
                lambda _checked=False, name=action_name: self.action_requested.emit(
                    self.disk_id, name
                )
            )
        actions_button.setMenu(actions_menu)
        top.addWidget(actions_button)
        layout.addLayout(top)

        status = evaluate_status(disk, configuration)
        row = QHBoxLayout()
        row.addWidget(StatusBadge(status))
        role = (
            ROLE_LABELS[configuration.role]
            if configuration
            else ROLE_LABELS[next(iter(ROLE_LABELS))]
        )
        role_label = QLabel(
            f"Системный раздел · {role}" if disk.is_system else role
        )
        role_label.setProperty("muted", True)
        row.addWidget(role_label)
        row.addStretch()
        layout.addLayout(row)

        progress = QProgressBar()
        progress.setRange(0, 100)
        progress.setValue(round(disk.used_percent))
        progress.setTextVisible(False)
        if status == DiskStatus.ALMOST_FULL:
            progress.setStyleSheet(f"QProgressBar::chunk {{ background: {COLORS['yellow']}; }}")
        layout.addWidget(progress)

        capacity = QLabel(
            f"{format_bytes(disk.used_bytes)} занято  ·  {format_bytes(disk.free_bytes)} свободно"
            if disk.available
            else "Накопитель сейчас недоступен"
        )
        capacity.setProperty("muted", True)
        layout.addWidget(capacity)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.selected.emit(self.disk_id)
        super().mousePressEvent(event)


def make_header(title: str, subtitle: str, action: tuple[str, Callable[[], None]] | None = None):
    widget = QWidget()
    layout = QHBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    text = QVBoxLayout()
    heading = QLabel(title)
    heading.setObjectName("PageTitle")
    description = QLabel(subtitle)
    description.setProperty("muted", True)
    description.setWordWrap(True)
    text.addWidget(heading)
    text.addWidget(description)
    layout.addLayout(text, 1)
    if action:
        button = QPushButton(action[0])
        button.clicked.connect(action[1])
        layout.addWidget(button)
    return widget
