import json

import pytest

from cloud_storage.core.api import create_app
from cloud_storage.core.config import CoreConfig
from cloud_storage.core.storage import (
    InvalidLogicalPath,
    InvalidStorageRoot,
    ManagedRootRequest,
    StorageService,
    normalize_logical_path,
)


@pytest.mark.parametrize(
    "value",
    ["../secret", "/absolute", "folder\\file", "a//b", "folder/../secret", "\x00bad"],
)
def test_logical_path_rejects_traversal_and_ambiguous_names(value: str) -> None:
    with pytest.raises(InvalidLogicalPath):
        normalize_logical_path(value)


def test_logical_path_keeps_unicode_and_nested_folders() -> None:
    assert normalize_logical_path("Фото/Отпуск/море.jpg") == "Фото/Отпуск/море.jpg"


def test_known_managed_root_updates_changed_disk_identity(tmp_path) -> None:
    root = tmp_path / "CloudStorageData"
    root.mkdir()
    marker = root / ".cloud-storage-root.json"
    marker.write_text(
        json.dumps({"schema_version": 1, "disk_id": "old", "created_at": "now"}),
        encoding="utf-8",
    )

    StorageService._initialize_managed_root(root, "new", previous_disk_id="old")

    assert json.loads(marker.read_text(encoding="utf-8"))["disk_id"] == "new"


def test_unknown_managed_root_still_rejects_different_disk(tmp_path) -> None:
    root = tmp_path / "CloudStorageData"
    root.mkdir()
    (root / ".cloud-storage-root.json").write_text(
        json.dumps({"schema_version": 1, "disk_id": "foreign"}), encoding="utf-8"
    )

    with pytest.raises(InvalidStorageRoot, match="different physical disk"):
        StorageService._initialize_managed_root(root, "new")


def test_sync_keeps_root_and_references_when_disk_identity_changes(tmp_path) -> None:
    app = create_app(CoreConfig(data_directory=tmp_path / "core"))
    root = tmp_path / "disk" / "CloudStorageData"
    root.parent.mkdir()
    def request(disk_id: str) -> ManagedRootRequest:
        return ManagedRootRequest(
            disk_id=disk_id,
            path=root,
            priority=50,
            max_fill_percent=90,
            min_free_bytes=0,
        )

    first = app.state.runtime.storage.sync_managed_roots([request("old-id")])[0]
    second = app.state.runtime.storage.sync_managed_roots([request("new-id")])[0]

    assert second.id == first.id
    assert second.disk_id == "new-id"
    assert json.loads((root / ".cloud-storage-root.json").read_text(encoding="utf-8"))[
        "disk_id"
    ] == "new-id"
