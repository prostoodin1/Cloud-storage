import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication, QLabel, QScrollArea

from cloud_storage.container_manager.window import ContainerManagerWindow
from cloud_storage.models import AppSettings, DiskConfiguration, DiskRole, DiskSnapshot
from cloud_storage.pairing import build_connection_code
from cloud_storage.services.settings_store import SettingsStore
from cloud_storage.ui.connection_page import ConnectionCodePanel
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


class FakeContainerClient:
    def control_center(self):
        return {
            "sandbox": {
                "available": True,
                "runtime": "docker",
                "automatic_max": {"cpu": 4, "memory_mib": 4096},
            },
            "cells": [
                {
                    "id": "cell-1",
                    "name": "Bot box",
                    "image": "python:3.12-alpine",
                    "command": ["python", "-c", "print('ok')"],
                    "cpu_limit": 1.0,
                    "memory_mib": 256,
                    "storage_mib": 512,
                    "timeout_seconds": 300,
                    "network_enabled": False,
                    "status": "ready",
                    "last_result": "",
                }
            ],
        }


def test_main_window_smoke(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(store=SettingsStore(tmp_path), disk_service=FakeDiskService())
    window.refresh_disks()
    app.processEvents()

    assert window.stack.count() == 9
    assert window.connection_page.panel is not None
    assert window.settings_page.connection_panel is not None
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
    assert window.settings_page.zrok_executable.text() == "zrok2"
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


def test_connection_panel_generates_without_manual_ip() -> None:
    app = QApplication.instance() or QApplication([])
    panel = ConnectionCodePanel()
    panel.set_data(
        {
            "lan": {
                "enabled": True,
                "endpoints": ["https://192.168.1.20:8766"],
                "fingerprint": "ab" * 32,
            }
        },
        [{"id": "user-1", "username": "alex", "display_name": "Alex"}],
        {},
    )
    panel.scope.setCurrentIndex(panel.scope.findData("lan"))
    app.processEvents()
    spy = QSignalSpy(panel.generation_requested)

    panel.generate_button.click()

    assert panel.endpoint.text() == "https://192.168.1.20:8766"
    assert spy.count() == 1
    assert list(spy.at(0)) == ["user-1", "alex", "lan"]

    panel.show_code(
        build_connection_code(
            "ABCD-2345",
            "https://192.168.1.20:8766",
            "ab" * 32,
            "alex",
        ),
        "Готово",
    )
    assert panel.generated_username.text() == "alex"
    assert "username=alex" in panel.invitation_link.text()
    assert panel.qr_label.pixmap() is not None


def test_disk_role_survives_a_changed_windows_disk_id(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    store = SettingsStore(tmp_path)
    settings = AppSettings()
    settings.disk_configurations["old-transient-id"] = DiskConfiguration(
        role=DiskRole.CACHE,
        identity_mountpoint="X:\\",
        identity_device="fake",
    )
    settings.known_disk_ids.append("old-transient-id")
    store.save(settings)

    window = MainWindow(store=store, disk_service=FakeDiskService())
    app.processEvents()

    restored = store.load()
    assert "old-transient-id" not in restored.disk_configurations
    assert restored.disk_configurations["disk-test"].role == DiskRole.CACHE
    assert restored.disk_configurations["disk-test"].identity_mountpoint == "X:\\"
    window.close()


def test_separate_container_manager_renders_resource_boxes() -> None:
    app = QApplication.instance() or QApplication([])
    window = ContainerManagerWindow(client=FakeContainerClient())
    QThreadPool.globalInstance().waitForDone(1000)
    app.processEvents()

    assert window.cell_list.count() == 1
    assert "Bot box" in window.cell_list.item(0).text()
    assert "docker" in window.runtime_status.text()
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


def test_system_tabs_work_offline_and_settings_links_open_requested_tab(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(store=SettingsStore(tmp_path), disk_service=FakeDiskService())
    page = window.control_page

    page.update_data({}, online=False)
    page.open_tab("docker")
    app.processEvents()

    assert page.isEnabled() is True
    assert page.tabs.isEnabled() is True
    assert page.tabs.tabText(page.tabs.currentIndex()) == "Docker"

    window.settings_page.system_section_requested.emit("ssh")
    app.processEvents()
    assert window.stack.currentWidget() is page
    assert page.tabs.tabText(page.tabs.currentIndex()) == "SSH"
    window.close()


def test_save_button_only_appears_after_real_settings_change(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(store=SettingsStore(tmp_path), disk_service=FakeDiskService())
    page = window.settings_page
    app.processEvents()

    assert page.save_button.isHidden()
    page.server_name.setText("Изменённое имя")
    app.processEvents()
    assert not page.save_button.isHidden()

    page.load_settings(window.settings, str(window.store.path))
    app.processEvents()
    assert page.save_button.isHidden()
    window.close()
