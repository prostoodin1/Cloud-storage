from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication, QMessageBox

from cloud_storage import __version__
from cloud_storage.ui.main_window import MainWindow
from cloud_storage.ui.update_page import UpdatePage, _Task
from cloud_storage.updates import UpdateError, UpdateInfo, UpdatePackage, UpdatePreferences


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def update_info(version="99.0.0"):
    return UpdateInfo(
        "client", version, "stable", "", "Test",
        UpdatePackage("https://example.com/setup.exe", "a" * 64, 3, "setup.exe"),
    )


def test_startup_defaults_and_explicit_manual(app, tmp_path, monkeypatch):
    monkeypatch.setattr("cloud_storage.ui.update_page.is_frozen_windows", lambda: True)
    page = UpdatePage("client", tmp_path)
    assert page.startup_timer.isActive()
    page.startup_timer.stop()
    page.service.save_preferences(UpdatePreferences(policy="manual", channel="stable"))
    manual = UpdatePage("client", tmp_path)
    assert not manual.startup_timer.isActive()
    page.deleteLater()
    manual.deleteLater()


def test_auto_pipeline_waits_for_transfers_then_installs_and_quits(app, tmp_path, monkeypatch):
    monkeypatch.setattr("cloud_storage.ui.update_page.is_frozen_windows", lambda: True)
    busy = [True]
    page = UpdatePage("client", tmp_path, can_auto_install=lambda: not busy[0])
    page.startup_timer.stop()
    events = []
    info = update_info()
    package = tmp_path / "setup.exe"
    monkeypatch.setattr(page, "_run", lambda function, success: success(function()))
    monkeypatch.setattr(page.service, "list_versions", lambda channel: [info])
    monkeypatch.setattr(page.service, "download", lambda selected: events.append("download") or package)
    monkeypatch.setattr(page.service, "launch_installer", lambda *args: events.append("install"))
    monkeypatch.setattr(app, "quit", lambda: events.append("quit"))
    page._auto_check()
    assert events == ["download"]
    assert page.install_retry_timer.isActive()
    busy[0] = False
    page._try_auto_install()
    assert events == ["download", "install", "quit"]
    assert not page.install_retry_timer.isActive()
    page.deleteLater()


def test_cancelled_uac_does_not_quit_or_repeat_prompt(app, tmp_path, monkeypatch):
    page = UpdatePage("client", tmp_path)
    page._automatic = True
    page.info = update_info()
    page.installer = Path(tmp_path / "setup.exe")
    events = []

    def reject(*args):
        raise UpdateError("UAC cancelled")

    monkeypatch.setattr(page.service, "launch_installer", reject)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: events.append("warning"))
    monkeypatch.setattr(app, "quit", lambda: events.append("quit"))
    page._try_auto_install()
    assert events == ["warning"]
    assert not page.install_retry_timer.isActive()
    page.deleteLater()


def test_switching_to_manual_cancels_pending_auto_install(app, tmp_path):
    page = UpdatePage("client", tmp_path, can_auto_install=lambda: False)
    page._automatic = True
    page.info = update_info()
    page.installer = tmp_path / "setup.exe"
    page._try_auto_install()
    assert page.install_retry_timer.isActive()
    page.policy_combo.setCurrentIndex(page.policy_combo.findData("manual"))
    assert not page.install_retry_timer.isActive()
    assert page.info is None
    page.deleteLater()


@pytest.mark.parametrize("version", ["0.1.0", __version__])
def test_automatic_install_never_downgrades_or_reinstalls(app, tmp_path, version):
    page = UpdatePage("client", tmp_path)
    page._automatic = True
    page.info = update_info(version)
    page.installer = tmp_path / "setup.exe"
    page._try_auto_install()
    assert not page.install_retry_timer.isActive()
    page.deleteLater()


def test_network_failure_keeps_application_available(app, tmp_path, monkeypatch):
    page = UpdatePage("client", tmp_path)
    monkeypatch.setattr(app, "quit", lambda: pytest.fail("must remain running offline"))
    page._busy("Checking")
    assert not page.channel_combo.isEnabled()
    page._failed("offline")
    assert page.check_button.isEnabled()
    assert page.channel_combo.isEnabled()
    assert "offline" in page.status_label.text()
    page.deleteLater()


def test_unexpected_worker_failure_is_reported(app):
    def broken():
        raise RuntimeError("unexpected response")

    task = _Task(broken)
    spy = QSignalSpy(task.signals.failure)
    task.run()
    assert spy.count() == 1
    assert spy.at(0) == ["unexpected response"]


def test_server_auto_update_guard_fails_closed_without_stopping_core():
    client = SimpleNamespace(transfers=lambda **kwargs: {"counts": {"inbound_active": 1}})
    window = SimpleNamespace(core_client=client)
    assert not MainWindow._can_auto_update_server(window)
    client.transfers = lambda **kwargs: {"counts": {"inbound_active": 0, "outbound_active": 0}}
    assert MainWindow._can_auto_update_server(window)

    def unavailable(**kwargs):
        raise OSError("offline")

    client.transfers = unavailable
    assert not MainWindow._can_auto_update_server(window)
    client.transfers = lambda **kwargs: {"counts": {}}
    assert not MainWindow._can_auto_update_server(window)
