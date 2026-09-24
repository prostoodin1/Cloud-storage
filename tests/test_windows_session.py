import sys

from cloud_storage.client import windows_session


def test_frozen_relaunch_command_preserves_arguments_once(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Program Files\Cloud Storage\Client.exe")
    monkeypatch.setattr(
        sys,
        "argv",
        ["Client.exe", "--desktop-user-relaunch", "--minimized"],
    )
    assert windows_session._command_for_relaunch() == [
        r"C:\Program Files\Cloud Storage\Client.exe",
        "--desktop-user-relaunch",
        "--minimized",
    ]


def test_source_relaunch_command_keeps_script(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Python\python.exe")
    monkeypatch.setattr(sys, "argv", ["client_main.py", "--minimized"])
    assert windows_session._command_for_relaunch() == [
        r"C:\Python\python.exe",
        "client_main.py",
        "--desktop-user-relaunch",
        "--minimized",
    ]
