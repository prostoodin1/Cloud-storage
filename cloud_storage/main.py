from __future__ import annotations

import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

from cloud_storage.ui.main_window import MainWindow
from cloud_storage.ui.theme import apply_theme


def main() -> int:
    QApplication.setApplicationName("Cloud Storage Server Manager")
    QApplication.setOrganizationName("Cloud Storage")
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    apply_theme(app)
    window = MainWindow()
    if "--smoke-test" in sys.argv:
        window.refresh_disks()
        QTimer.singleShot(250, app.quit)
        return app.exec()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
