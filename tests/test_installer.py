"""Exercise the permanent installer UI without installing into the host OS."""
import os
from pathlib import Path
from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QApplication

from cloud_storage.installer.app import InstallerWindow
from cloud_storage.updates import UpdateInfo, UpdatePackage

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.mark.parametrize("product", ["client", "server"])
def test_installer_version_choice_reaches_interactive_setup(product, monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr("cloud_storage.installer.app.installer_data_directory", lambda _: tmp_path)
    quit_app = Mock()
    monkeypatch.setattr(QApplication, "quit", quit_app)
    window = InstallerWindow(product, smoke_test=True)
    launch = Mock()
    window.service.launch_installer = launch
    package = UpdatePackage("https://example.com/setup.exe", "a" * 64, 10, "setup.exe")
    latest = UpdateInfo(product, "0.10.2", "stable", "2026-09-23", "Latest", package)
    previous = UpdateInfo(product, "0.9.23", "stable", "2026-09-11", "Previous", package)
    try:
        window._versions_loaded([latest, previous])
        assert window._selected() == latest
        window.mode.setCurrentIndex(window.mode.findData("specific"))
        window.version.setCurrentIndex(window.version.findData("0.9.23"))
        assert window._selected() == previous
        assert "0.9.23" in window.install.text()
        path = Path(tmp_path / "setup.exe")
        window._downloaded((previous, path))
        launch.assert_called_once_with(path, previous, interactive=True)
        quit_app.assert_called_once()
    finally:
        window.close()
        app.processEvents()
