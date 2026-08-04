from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtWidgets import QApplication

from cloud_storage.client.settings import ClientSettingsStore
from cloud_storage.client.window import ClientWindow
from cloud_storage.models import DiskSnapshot
from cloud_storage.services.settings_store import SettingsStore
from cloud_storage.ui.main_window import MainWindow
from cloud_storage.ui.theme import apply_theme


class AuditDiskService:
    def discover(self) -> list[DiskSnapshot]:
        return [
            DiskSnapshot(
                id="ui-audit-disk",
                mountpoint="X:\\",
                device="audit",
                label="Диск для проверки интерфейса",
                filesystem="NTFS",
                total_bytes=2 * 1024**4,
                used_bytes=650 * 1024**3,
                free_bytes=1398 * 1024**3,
            )
        ]


def save_contact_sheet(
    captures: list[tuple[str, QPixmap]], output: Path, columns: int
) -> None:
    thumb_width, thumb_height, heading_height = 650, 405, 34
    rows = (len(captures) + columns - 1) // columns
    sheet = QPixmap(columns * thumb_width, rows * (thumb_height + heading_height))
    sheet.fill(QColor("#0d0f12"))
    painter = QPainter(sheet)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.setPen(QColor("#f4f5f7"))
    painter.setFont(QFont("Segoe UI", 12, QFont.Weight.DemiBold))
    for index, (title, pixmap) in enumerate(captures):
        column = index % columns
        row = index // columns
        x = column * thumb_width
        y = row * (thumb_height + heading_height)
        painter.drawText(
            x + 12,
            y,
            thumb_width - 24,
            heading_height,
            Qt.AlignmentFlag.AlignVCenter,
            title,
        )
        scaled = pixmap.scaled(
            thumb_width,
            thumb_height,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        painter.drawPixmap(x, y + heading_height, scaled)
    painter.end()
    output.parent.mkdir(parents=True, exist_ok=True)
    if not sheet.save(str(output)):
        raise OSError(f"cannot save {output}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Render every Server Manager tab for UI QA")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    app = QApplication.instance() or QApplication([])
    apply_theme(app)
    with tempfile.TemporaryDirectory(prefix="cloud-storage-ui-audit-") as temp:
        window = MainWindow(
            store=SettingsStore(Path(temp)), disk_service=AuditDiskService()
        )
        window.resize(1380, 860)
        window.show()
        app.processEvents()

        main_names = [
            "Основная",
            "Диски",
            "Приём",
            "Отправка",
            "Настройки",
            "Обновления",
            "Помощь",
        ]
        main_captures: list[tuple[str, QPixmap]] = []
        for index, title in enumerate(main_names):
            window.stack.setCurrentIndex(index)
            app.processEvents()
            main_captures.append((title, window.grab()))

        settings = window.settings_page
        settings.mode.setCurrentIndex(1)
        app.processEvents()
        settings_captures: list[tuple[str, QPixmap]] = []
        for row in range(settings.section_list.count()):
            settings.section_list.setCurrentRow(row)
            app.processEvents()
            settings_captures.append(
                (settings.section_list.currentItem().text(), window.grab())
            )

        save_contact_sheet(main_captures, args.output / "main-tabs.png", columns=2)
        save_contact_sheet(
            settings_captures, args.output / "settings-tabs.png", columns=3
        )
        window.close()

        client = ClientWindow(store=ClientSettingsStore(Path(temp) / "client"))
        client.resize(1320, 820)
        client.show()
        app.processEvents()
        client_names = [
            "Подключение",
            "Мои файлы",
            "Передачи",
            "Офлайн",
            "Обновления",
            "Помощь",
        ]
        client_captures: list[tuple[str, QPixmap]] = []
        for index, title in enumerate(client_names):
            client.stack.setCurrentIndex(index)
            app.processEvents()
            client_captures.append((title, client.grab()))
        save_contact_sheet(client_captures, args.output / "client-tabs.png", columns=2)
        client.close()
    print(
        f"Rendered {len(main_captures)} Manager tabs, "
        f"{len(settings_captures)} settings tabs and {len(client_captures)} Client tabs"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
