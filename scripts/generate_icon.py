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
    app.quit()
    return 0 if saved else 1


if __name__ == "__main__":
    raise SystemExit(main())
