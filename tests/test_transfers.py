from __future__ import annotations

from pathlib import Path

from cloud_storage.client.transfers import TransferStore


def test_transfer_queue_persists_progress_and_recovers_after_restart(tmp_path: Path) -> None:
    source = tmp_path / "large.bin"
    source.write_bytes(b"0123456789")
    database = tmp_path / "client" / "transfers.db"

    first = TransferStore(database)
    upload = first.queue_upload(
        "https://cloud.home:8766",
        "space-1",
        "Документы/large.bin",
        source,
    )
    first.set_status(upload.id, "running")
    first.update_progress(upload.id, 4, remote_session_id="upload-session-1")

    restarted = TransferStore(database)
    assert restarted.recover_interrupted() == 1
    recovered = restarted.get(upload.id)
    assert recovered.status == "queued"
    assert recovered.transferred_bytes == 4
    assert recovered.remote_session_id == "upload-session-1"
    assert recovered.server_url == "https://cloud.home:8766"


def test_transfer_queue_tracks_download_and_clears_finished(tmp_path: Path) -> None:
    store = TransferStore(tmp_path / "transfers.db")
    download = store.queue_download(
        "http://127.0.0.1:8765",
        "space-2",
        "Фото/image.jpg",
        tmp_path / "offline" / "Фото" / "image.jpg",
        total_bytes=120,
        expected_sha256="a" * 64,
    )
    store.update_progress(download.id, 120)
    store.set_status(download.id, "completed")

    completed = store.get(download.id)
    assert completed.expected_sha256 == "a" * 64
    assert completed.transferred_bytes == 120
    assert store.clear_finished() == 1
    assert store.list() == []
