from __future__ import annotations

import os
from pathlib import Path

import pytest

from cloud_storage.core import main as core_main


class _FakeProcess:
    def __init__(self, name: str, executable: str, command: list[str]) -> None:
        self._name = name
        self._executable = executable
        self._command = command

    def name(self) -> str:
        return self._name

    def exe(self) -> str:
        return self._executable

    def cmdline(self) -> list[str]:
        return self._command


def test_stale_pid_reused_by_unrelated_process_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "core.pid"
    path.write_text("4084", encoding="ascii")
    monkeypatch.setattr(
        core_main.psutil,
        "Process",
        lambda _pid: _FakeProcess("svchost.exe", r"C:\Windows\System32\svchost.exe", []),
    )

    guard = core_main.PidGuard(path)
    guard.acquire()
    try:
        assert path.read_text(encoding="ascii") == str(os.getpid())
    finally:
        guard.release()


def test_live_cloud_storage_core_pid_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "core.pid"
    path.write_text("1234", encoding="ascii")
    monkeypatch.setattr(
        core_main.psutil,
        "Process",
        lambda _pid: _FakeProcess(
            "CloudStorageLegacyCore.exe", r"C:\CloudStorageLegacyCore.exe", []
        ),
    )

    with pytest.raises(core_main.AlreadyRunningError):
        core_main.PidGuard(path).acquire()
