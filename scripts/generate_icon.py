import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter


def main() -> int:
    app = QGuiApplication.instance() or QGuiApplication([])
    output = Path(__file__).resolve().parents[1] / "assets" / "cloud-storage.ico"
    output.parent.mkdir(parents=True, exist_ok=True)
    image = QImage(256, 256, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#e2383f"))
    painter.drawRoundedRect(16, 16, 224, 224, 48, 48)
    painter.setPen(QColor("#ffffff"))
    painter.setFont(QFont("Segoe UI", 72, QFont.Weight.Bold))
    painter.drawText(image.rect(), Qt.AlignmentFlag.AlignCenter, "CS")
    painter.end()
    saved = image.save(str(output), "ICO")

    drive_output = output.with_name("cloud-storage-drive.ico")
    drive = QImage(256, 256, QImage.Format.Format_ARGB32)
    drive.fill(Qt.GlobalColor.transparent)
    painter = QPainter(drive)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#2477e8"))
    painter.drawRoundedRect(16, 32, 224, 176, 34, 34)
    painter.setBrush(QColor("#f5f8ff"))
    painter.drawRoundedRect(34, 48, 188, 112, 22, 22)
    painter.setBrush(QColor("#16324f"))
    painter.drawRoundedRect(52, 68, 152, 70, 14, 14)
    painter.setBrush(QColor("#43c778"))
    painter.drawEllipse(172, 166, 52, 52)
    painter.setPen(QColor("#ffffff"))
    painter.setFont(QFont("Segoe UI", 31, QFont.Weight.Bold))
    painter.drawText(172, 164, 52, 52, Qt.AlignmentFlag.AlignCenter, "✓")
    painter.end()
    saved = drive.save(str(drive_output), "ICO") and saved
    app.quit()
    return 0 if saved else 1


if __name__ == "__main__":
    raise SystemExit(main())
