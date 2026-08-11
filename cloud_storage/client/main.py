from __future__ import annotations

import os
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

from cloud_storage.client.window import ClientWindow
from cloud_storage.single_instance import SingleInstance
from cloud_storage.ui.theme import apply_theme


def _activate(window: ClientWindow) -> None:
    window.showNormal()
    window.show()
    window.raise_()
    window.activateWindow()


def main() -> int:
    QApplication.setApplicationName("Cloud Storage Desktop Client")
    QApplication.setOrganizationName("Cloud Storage")
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    instance_name = "cloud-storage-desktop-client-v1"
    if "--smoke-test" in sys.argv:
        instance_name += f"-smoke-{os.getpid()}"
    instance = SingleInstance(instance_name)
    if not instance.acquire():
        return 0
    apply_theme(app)
    window = ClientWindow()
    instance.set_activation_handler(lambda: _activate(window))
    app.aboutToQuit.connect(instance.close)
    if "--smoke-test" in sys.argv:
        window.reconnect_timer.stop()
        QTimer.singleShot(250, app.quit)
        return app.exec()
    if "--minimized" not in sys.argv or window.tray_icon is None:
        window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
