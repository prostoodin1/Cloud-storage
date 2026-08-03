from __future__ import annotations

import hashlib
from pathlib import Path

from cloud_storage.client.offline import OfflineStore


def test_offline_index_detects_local_remote_and_true_conflicts(tmp_path: Path) -> None:
    path = tmp_path / "offline" / "notes.txt"
    path.parent.mkdir()
    original = b"original"
    original_sha = hashlib.sha256(original).hexdigest()
    path.write_bytes(original)
    store = OfflineStore(tmp_path / "offline.db")
    record = store.mark_synced(
        "https://cloud.home:8766",
        "space-1",
        "notes.txt",
        path,
        original_sha,
        len(original),
    )
    assert record.status == "current"

    path.write_bytes(b"local edit")
    assert store.scan_local(record.id).status == "local_changed"
    assert (
        store.apply_remote_state(record.id, exists=True, sha256=original_sha).status
        == "local_changed"
    )
    remote_sha = hashlib.sha256(b"remote edit").hexdigest()
    assert (
        store.apply_remote_state(record.id, exists=True, sha256=remote_sha).status
        == "conflict"
    )


def test_offline_index_tracks_missing_sides_and_persists(tmp_path: Path) -> None:
    path = tmp_path / "cached.bin"
    path.write_bytes(b"cached")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    database = tmp_path / "offline.db"
    store = OfflineStore(database)
    record = store.mark_synced("http://127.0.0.1:8765", "space", "cached.bin", path, digest, 6)

    assert store.apply_remote_state(record.id, exists=False).status == "remote_missing"
    path.unlink()
    assert store.scan_local(record.id).status == "missing_both"
    assert OfflineStore(database).get(record.id).status == "missing_both"
