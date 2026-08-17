import os

import pytest

from cloud_storage.models import DiskConfiguration, DiskMode, DiskRole, DiskSnapshot, DiskStatus
from cloud_storage.services.disk_service import DiskService, evaluate_status


def disk(*, used: int = 50, free: int = 50, available: bool = True) -> DiskSnapshot:
    return DiskSnapshot(
        id="disk-test",
        mountpoint="X:\\",
        device="test",
        total_bytes=used + free,
        used_bytes=used,
        free_bytes=free,
        available=available,
    )


def test_unconfigured_disk_has_explicit_status() -> None:
    assert evaluate_status(disk(), DiskConfiguration()) == DiskStatus.UNCONFIGURED


def test_policy_modes_override_capacity() -> None:
    config = DiskConfiguration(role=DiskRole.SHARED, mode=DiskMode.MAINTENANCE)
    assert evaluate_status(disk(used=99, free=1), config) == DiskStatus.MAINTENANCE


def test_low_space_uses_percentage_or_absolute_reserve() -> None:
    gib = 1024**3
    config = DiskConfiguration(role=DiskRole.SHARED, max_fill_percent=90, min_free_gib=10)
    assert evaluate_status(disk(used=91 * gib, free=9 * gib), config) == DiskStatus.ALMOST_FULL
    assert evaluate_status(disk(used=80 * gib, free=20 * gib), config) == DiskStatus.HEALTHY


def test_unavailable_disk_never_claims_health() -> None:
    config = DiskConfiguration(role=DiskRole.SHARED)
    assert evaluate_status(disk(available=False), config) == DiskStatus.UNAVAILABLE


@pytest.mark.skipif(os.name != "nt", reason="Windows volume protection")
def test_system_volume_is_protected_from_destructive_operations() -> None:
    snapshot = disk()
    snapshot.mountpoint = "C:\\"
    snapshot.is_system = True

    with pytest.raises(PermissionError, match="Системный диск"):
        DiskService().start_format(snapshot)
