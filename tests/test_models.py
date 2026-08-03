from cloud_storage.models import AppSettings, DiskConfiguration, DiskMode, DiskRole


def test_settings_round_trip_preserves_disk_policy() -> None:
    settings = AppSettings(server_name="NAS", setup_complete=True, advanced_mode=True)
    settings.disk_configurations["disk-1"] = DiskConfiguration(
        display_name="Архив",
        role=DiskRole.ARCHIVE,
        mode=DiskMode.WRITES_PAUSED,
        max_fill_percent=85,
        allowed_users=["admin"],
    )

    restored = AppSettings.from_dict(settings.to_dict())

    assert restored.server_name == "NAS"
    assert restored.advanced_mode is True
    assert restored.disk_configurations["disk-1"].role == DiskRole.ARCHIVE
    assert restored.disk_configurations["disk-1"].mode == DiskMode.WRITES_PAUSED
    assert restored.disk_configurations["disk-1"].allowed_users == ["admin"]


def test_invalid_enum_values_fall_back_safely() -> None:
    config = DiskConfiguration.from_dict({"role": "destroy-everything", "mode": "unknown"})

    assert config.role == DiskRole.UNCONFIGURED
    assert config.mode == DiskMode.ACTIVE
