from __future__ import annotations

import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

from cloud_storage.client.window import ClientWindow
from cloud_storage.ui.theme import apply_theme


def main() -> int:
    QApplication.setApplicationName("Cloud Storage Desktop Client")
    QApplication.setOrganizationName("Cloud Storage")
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    apply_theme(app)
    window = ClientWindow()
    if "--smoke-test" in sys.argv:
        window.reconnect_timer.stop()
        QTimer.singleShot(250, app.quit)
        return app.exec()
    if "--minimized" not in sys.argv or window.tray_icon is None:
        window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
