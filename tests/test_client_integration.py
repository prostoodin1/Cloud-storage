from __future__ import annotations

from pathlib import Path

from cloud_storage.client.integration import autostart_enabled, set_autostart


def test_linux_autostart_entry_can_be_enabled_and_disabled(tmp_path: Path) -> None:
    command = ["/opt/cloud storage/client", "--minimized"]
    assert autostart_enabled(system="Linux", config_home=tmp_path) is False

    set_autostart(True, command=command, system="Linux", config_home=tmp_path)
    entry = tmp_path / "autostart" / "cloud-storage-client.desktop"
    content = entry.read_text(encoding="utf-8")
    assert "Cloud Storage Client" in content
    assert "'/opt/cloud storage/client' --minimized" in content
    assert autostart_enabled(system="Linux", config_home=tmp_path) is True

    set_autostart(False, command=command, system="Linux", config_home=tmp_path)
    assert entry.exists() is False
