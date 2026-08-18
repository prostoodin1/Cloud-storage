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


def test_legacy_default_zrok_executable_migrates_to_zrok2() -> None:
    assert AppSettings.from_dict({"zrok_executable": "zrok"}).zrok_executable == "zrok2"
    assert AppSettings.from_dict({"zrok_executable": "zrok.exe"}).zrok_executable == "zrok2.exe"
    assert (
        AppSettings.from_dict({"zrok_executable": "C:/tools/zrok.exe"}).zrok_executable
        == "C:/tools/zrok.exe"
    )


def test_new_disks_inherit_global_defaults_without_becoming_active() -> None:
    settings = AppSettings(
        disk_defaults=DiskConfiguration(
            role=DiskRole.SHARED,
            write_priority=73,
            max_fill_percent=82,
            min_free_gib=25,
            auto_move_allowed=True,
        )
    )

    created = settings.configuration_for("new-disk")

    assert created.role == DiskRole.UNCONFIGURED
    assert created.write_priority == 73
    assert created.max_fill_percent == 82
    assert created.min_free_gib == 25
    assert created.auto_move_allowed is True
