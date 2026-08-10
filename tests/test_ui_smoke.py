import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QScrollArea

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

    assert window.stack.count() == 8
    assert window.receive_page.direction == "inbound"
    assert window.send_page.direction == "outbound"
    assert window.help_page.article_list.count() > 0
    assert window.settings_page.lan_port.value() == 8766
    assert window.settings_page.remote_port.value() == 8767
    assert window.settings_page.remote_enabled.isChecked() is False
    assert window.settings_page.remote_status_label is not None
    assert window.settings_page.remote_audit_rows is not None
    assert window.settings_page.zrok_port.value() == 8768
    assert window.settings_page.zrok_enabled.isChecked() is False
    assert window.settings_page.zrok_status_label is not None
    assert window.settings_page.zrok_restart_button is not None
    assert window.settings_page.support_bundle_button is not None
    assert window.settings_page.automation_create_button is not None
    assert window.settings_page.automation_rule_rows is not None
    assert window.settings_page.automation_scheduler_enabled is not None
    assert window.settings_page.automation_scheduler_interval is not None
    assert window.settings_page.automation_preview_button is not None
    scheduled = window.settings_page.automation_trigger.findData("scheduled")
    window.settings_page.automation_trigger.setCurrentIndex(scheduled)
    available_actions = {
        window.settings_page.automation_action.itemData(index)
        for index in range(window.settings_page.automation_action.count())
    }
    assert {"run_backup", "reconcile_mirrors", "full_scan"} <= available_actions
    assert window.settings_page.notification_rows is not None
    assert window.settings_page.integration_rows is not None
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


def test_every_settings_section_is_scrollable_and_has_explanatory_text(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(store=SettingsStore(tmp_path), disk_service=FakeDiskService())
    page = window.settings_page
    page.mode.setCurrentIndex(1)
    window.resize(1040, 700)
    window.show()
    app.processEvents()

    assert page.section_list.count() == len(page._SECTIONS)
    for row in range(page.section_list.count()):
        page.section_list.setCurrentRow(row)
        app.processEvents()
        section = page.stack.currentWidget()
        assert isinstance(section, QScrollArea)
        assert section.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        text = " ".join(
            label.text().strip() for label in section.findChildren(QLabel) if label.text().strip()
        )
        assert len(text) > 40, page.section_list.currentItem().text()
        assert "станет активным" not in text

    window.close()
