from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from cloud_storage.ui.widgets import (
    StatCard,
    clear_layout,
    format_bytes,
    make_header,
)

STATUS_LABELS = {
    "receiving": "Приём на SSD",
    "moving": "Перенос SSD → HDD",
    "sending": "Отправка клиенту",
    "completed": "Завершено",
    "failed": "Ошибка",
    "cancelled": "Отменено",
}


class TransfersPage(QWidget):
    refresh_requested = Signal()
    retry_requested = Signal(str)
    configure_cache_requested = Signal()

    def __init__(self, direction: str) -> None:
        super().__init__()
        if direction not in {"inbound", "outbound"}:
            raise ValueError("unknown transfer direction")
        self.direction = direction

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 16, 24)
        root.setSpacing(16)
        title = "Приём" if direction == "inbound" else "Отправка"
        subtitle = (
            "Файл полностью принимается на SSD, проверяется и затем переносится на основной HDD."
            if direction == "inbound"
            else "Файлы, которые Core сейчас передаёт клиентам, и история завершённых скачиваний."
        )
        root.addWidget(make_header(title, subtitle, ("Обновить", self.refresh_requested.emit)))

        stats = QHBoxLayout()
        self.active_card = StatCard("Сейчас", "0", "Активных операций")
        self.completed_card = StatCard("Завершено", "0", "В последних 100 операциях")
        self.failed_card = StatCard("Ошибки", "0", "Можно проверить подробности ниже")
        stats.addWidget(self.active_card)
        stats.addWidget(self.completed_card)
        stats.addWidget(self.failed_card)
        root.addLayout(stats)

        self.staging_card: QFrame | None = None
        self.staging_status: QLabel | None = None
        self.staging_detail: QLabel | None = None
        if direction == "inbound":
            self.staging_card = QFrame()
            self.staging_card.setProperty("card", True)
            staging_layout = QVBoxLayout(self.staging_card)
            staging_layout.setContentsMargins(18, 16, 18, 16)
            top = QHBoxLayout()
            heading = QLabel("SSD-приёмник")
            heading.setObjectName("SectionTitle")
            configure = QPushButton("Выбрать SSD во вкладке «Диски»")
            configure.clicked.connect(self.configure_cache_requested.emit)
            top.addWidget(heading, 1)
            top.addWidget(configure)
            self.staging_status = QLabel("Core выключен")
            self.staging_status.setStyleSheet("font-weight: 650;")
            self.staging_detail = QLabel(
                "Назначьте быстрому диску роль «Кэш». Если SSD недоступен или заполнен, "
                "Core безопасно использует временную папку целевого HDD."
            )
            self.staging_detail.setWordWrap(True)
            self.staging_detail.setProperty("muted", True)
            staging_layout.addLayout(top)
            staging_layout.addWidget(self.staging_status)
            staging_layout.addWidget(self.staging_detail)
            root.addWidget(self.staging_card)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_body = QWidget()
        self.rows = QVBoxLayout(scroll_body)
        self.rows.setContentsMargins(0, 0, 6, 0)
        self.rows.setSpacing(10)
        scroll.setWidget(scroll_body)
        root.addWidget(scroll, 1)
        self.update_data({}, online=False)

    def update_data(self, overview: dict, *, online: bool) -> None:
        jobs = list(overview.get(self.direction, [])) if online else []
        active_statuses = {"receiving", "moving", "sending"}
        active = sum(str(item.get("status")) in active_statuses for item in jobs)
        completed = sum(str(item.get("status")) == "completed" for item in jobs)
        failed = sum(str(item.get("status")) == "failed" for item in jobs)
        self.active_card.set_value(str(active), "Активных операций" if online else "Core выключен")
        self.completed_card.set_value(str(completed), "В последних 100 операциях")
        self.failed_card.set_value(str(failed), "Требуют внимания" if failed else "Ошибок нет")

        if self.direction == "inbound" and self.staging_status and self.staging_detail:
            settings = overview.get("settings", {}) if online else {}
            enabled = bool(settings.get("staging_enabled"))
            available = bool(settings.get("available"))
            path = str(settings.get("staging_path", ""))
            if not online:
                self.staging_status.setText("Core выключен — очередь сохранена")
            elif enabled and available:
                self.staging_status.setText("SSD staging включён")
                self.staging_detail.setText(
                    f"{path}\nСвободно {format_bytes(int(settings.get('free_bytes', 0)))}. "
                    "После полного приёма Core проверит SHA-256 и перенесёт файл на основной диск."
                )
            elif enabled:
                self.staging_status.setText("SSD временно недоступен")
                self.staging_detail.setText(
                    f"Настроенный путь: {path or 'не указан'}. Новые файлы временно принимаются "
                    "на целевой HDD без потери данных."
                )
            else:
                self.staging_status.setText("SSD staging не настроен")
                self.staging_detail.setText(
                    "Назначьте быстрому диску роль «Кэш». Пока этого нет, приём работает через "
                    "защищённую временную папку основного диска."
                )

        clear_layout(self.rows)
        if not online:
            empty = QLabel("Запустите серверное ядро, чтобы увидеть очередь передач.")
            empty.setProperty("emptyState", True)
            self.rows.addWidget(empty)
        elif not jobs:
            empty = QLabel(
                "Входящих файлов пока нет."
                if self.direction == "inbound"
                else "Исходящих скачиваний пока нет."
            )
            empty.setProperty("emptyState", True)
            self.rows.addWidget(empty)
        else:
            for transfer in jobs:
                self.rows.addWidget(self._make_transfer_card(transfer))
        self.rows.addStretch()

    def _make_transfer_card(self, transfer: dict) -> QWidget:
        card = QFrame()
        card.setProperty("card", True)
        if transfer.get("status") == "failed":
            card.setProperty("accent", "orange")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)

        top = QHBoxLayout()
        path = QLabel(str(transfer.get("logical_path", "Файл")))
        path.setStyleSheet("font-weight: 650;")
        path.setWordWrap(True)
        status = str(transfer.get("status", ""))
        status_label = QLabel(STATUS_LABELS.get(status, status or "Неизвестно"))
        status_label.setProperty("muted", True)
        top.addWidget(path, 1)
        top.addWidget(status_label)
        layout.addLayout(top)

        total = max(0, int(transfer.get("total_bytes", 0)))
        progress_bytes = max(0, int(transfer.get("progress_bytes", 0)))
        progress = QProgressBar()
        progress.setRange(0, 1000)
        progress.setValue(round(min(100.0, float(transfer.get("progress_percent", 0))) * 10))
        progress.setTextVisible(False)
        layout.addWidget(progress)

        if status == "moving":
            detail_text = (
                f"Перенос на HDD: {format_bytes(progress_bytes)} из {format_bytes(total)}"
            )
        elif self.direction == "inbound":
            detail_text = (
                f"Принято: {format_bytes(int(transfer.get('network_bytes', 0)))} "
                f"из {format_bytes(total)}"
            )
        else:
            detail_text = f"Отправлено: {format_bytes(progress_bytes)} из {format_bytes(total)}"
        timestamps = str(transfer.get("updated_at", ""))
        detail = QLabel(f"{detail_text}  ·  {timestamps}" if timestamps else detail_text)
        detail.setProperty("muted", True)
        detail.setWordWrap(True)
        layout.addWidget(detail)

        error = str(transfer.get("error", "")).strip()
        if error:
            error_label = QLabel(error)
            error_label.setWordWrap(True)
            error_label.setStyleSheet("color: #f5bd4f;")
            layout.addWidget(error_label)

        if bool(transfer.get("retryable")):
            controls = QHBoxLayout()
            retry = QPushButton("Повторить перенос на HDD")
            retry.setProperty("primary", True)
            transfer_id = str(transfer.get("id", ""))
            retry.clicked.connect(
                lambda _checked=False, current_id=transfer_id: self.retry_requested.emit(
                    current_id
                )
            )
            controls.addWidget(retry)
            controls.addStretch()
            layout.addLayout(controls)
        return card
