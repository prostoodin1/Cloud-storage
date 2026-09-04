from __future__ import annotations

import os
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

from cloud_storage.container_manager.window import ContainerManagerWindow
from cloud_storage.single_instance import SingleInstance
from cloud_storage.ui.theme import apply_theme


def main() -> int:
    QApplication.setApplicationName("Cloud Storage Container Manager")
    QApplication.setOrganizationName("Cloud Storage")
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    instance_name = "cloud-storage-container-manager-v1"
    if "--smoke-test" in sys.argv:
        instance_name += f"-smoke-{os.getpid()}"
    instance = SingleInstance(instance_name)
    if not instance.acquire():
        return 0
    apply_theme(app)
    smoke_test = "--smoke-test" in sys.argv
    window = ContainerManagerWindow(smoke_test=smoke_test)
    instance.set_activation_handler(window.activate)
    app.aboutToQuit.connect(instance.close)
    window.show()
    if smoke_test:
        QTimer.singleShot(300, app.quit)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
