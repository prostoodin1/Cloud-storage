import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from cloud_storage.models import DiskSnapshot
from cloud_storage.services.settings_store import SettingsStore
from cloud_storage.ui.main_window import MainWindow


class FakeDiskService:
    def discover(self):
        return [
            DiskSnapshot(
                id="disk-test",
                mountpoint="X:\\",
                device="fake",
                label="Test disk",
                filesystem="NTFS",
                total_bytes=100 * 1024**3,
                used_bytes=25 * 1024**3,
                free_bytes=75 * 1024**3,
            )
        ]


def test_main_window_smoke(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(store=SettingsStore(tmp_path), disk_service=FakeDiskService())
    window.refresh_disks()
    app.processEvents()

    assert window.stack.count() == 4
    assert window.help_page.article_list.count() > 0
    assert window.settings_page.lan_port.value() == 8766
    assert window.settings_page.remote_port.value() == 8767
    assert window.settings_page.remote_enabled.isChecked() is False
    assert window.settings_page.remote_status_label is not None
    assert window.settings_page.remote_audit_rows is not None
    assert len(window.disks) == 1
    assert window.disks[0].label == "Test disk"

    window.close()


def test_advanced_mode_is_saved_immediately_and_survives_refresh(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    store = SettingsStore(tmp_path)
    window = MainWindow(store=store, disk_service=FakeDiskService())

    window.settings_page.mode.setCurrentIndex(1)
    app.processEvents()

    assert window.settings.advanced_mode is True
    assert store.load().advanced_mode is True

    window.refresh_disks()
    app.processEvents()

    assert window.settings_page.mode.currentData() is True
    window.close()
