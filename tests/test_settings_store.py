import json

from cloud_storage.models import AppSettings
from cloud_storage.services.settings_store import SettingsStore


def test_atomic_settings_save_and_load(tmp_path) -> None:
    store = SettingsStore(tmp_path)
    settings = AppSettings(
        server_name="Мой сервер",
        refresh_interval_seconds=44,
        lan_enabled=True,
        lan_port=9876,
        remote_enabled=True,
        remote_port=9877,
        remote_public_url="https://cloud.example.net:9877",
        remote_pairing_enabled=True,
    )

    store.save(settings)
    restored = store.load()

    assert restored.server_name == "Мой сервер"
    assert restored.refresh_interval_seconds == 44
    assert restored.lan_enabled is True
    assert restored.lan_port == 9876
    assert restored.remote_enabled is True
    assert restored.remote_port == 9877
    assert restored.remote_public_url == "https://cloud.example.net:9877"
    assert restored.remote_pairing_enabled is True
    assert json.loads(store.path.read_text(encoding="utf-8"))["schema_version"] == 3
    assert not list(tmp_path.glob("*.tmp"))


def test_corrupt_settings_are_quarantined(tmp_path) -> None:
    store = SettingsStore(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{broken", encoding="utf-8")

    restored = store.load()

    assert restored.server_name == "Домашнее облако"
    assert not store.path.exists()
    assert len(list(tmp_path.glob("settings.corrupt-*.json"))) == 1
