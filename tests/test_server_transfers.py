from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from cloud_storage.core.api import create_app
from cloud_storage.core.config import CoreConfig


def provision(tmp_path):
    config = CoreConfig(
        data_directory=tmp_path / "core",
        max_upload_bytes=1024**2,
        pairing_ttl_seconds=900,
    )
    app = create_app(config)
    with app.state.runtime.database.transaction() as connection:
        connection.execute(
            "UPDATE storage_roots SET min_free_bytes = 0, max_fill_percent = 99 "
            "WHERE id = 'default'"
        )
    client = TestClient(app)
    manager_headers = {
        "Authorization": f"Bearer {app.state.runtime.secrets.manager_token}"
    }
    created = client.post(
        "/v1/admin/users",
        headers=manager_headers,
        json={
            "username": "transfer-user",
            "display_name": "Transfer User",
            "quota_gib": 1,
            "role": "member",
        },
    ).json()
    invitation = client.post(
        "/v1/admin/invitations",
        headers=manager_headers,
        json={"user_id": created["user"]["id"]},
    ).json()
    paired = client.post(
        "/v1/pairing/redeem",
        json={
            "code": invitation["code"],
            "password": "transfer-password-2026",
            "device_name": "Transfer Test",
            "platform": "Windows",
        },
    ).json()
    client.post(
        f"/v1/admin/devices/{paired['device']['id']}/approve",
        headers=manager_headers,
    )
    device_headers = {"Authorization": f"Bearer {paired['device_token']}"}
    return app, client, manager_headers, device_headers, created["personal_space"]


def configure_ssd_staging(client, manager_headers, tmp_path):
    ssd_parent = tmp_path / "ssd"
    ssd_parent.mkdir()
    staging = ssd_parent / "CloudStorageCache"
    response = client.put(
        "/v1/admin/transfers/settings",
        headers=manager_headers,
        json={"staging_enabled": True, "staging_path": str(staging)},
    )
    assert response.status_code == 200
    assert response.json()["available"] is True
    assert (staging / ".cloud-storage-cache.json").is_file()
    assert (staging / "incoming").is_dir()
    return staging


def test_inbound_uses_ssd_then_moves_to_storage_and_outbound_is_tracked(tmp_path) -> None:
    app, client, manager_headers, device_headers, space = provision(tmp_path)
    staging = configure_ssd_staging(client, manager_headers, tmp_path)
    payload = (b"ssd-stage-to-hdd|" * 10_000) + b"done"
    digest = hashlib.sha256(payload).hexdigest()

    created = client.post(
        f"/v1/spaces/{space['id']}/uploads",
        headers=device_headers,
        json={
            "logical_path": "SSD/large.bin",
            "size_bytes": len(payload),
            "content_type": "application/octet-stream",
            "sha256": digest,
        },
    )
    assert created.status_code == 201
    transfer_id = created.json()["id"]
    staged_file = staging / "incoming" / f"{transfer_id}.resume"
    assert staged_file.is_file()

    uploaded = client.patch(
        f"/v1/uploads/{transfer_id}",
        headers={
            **device_headers,
            "Upload-Offset": "0",
            "Content-Type": "application/offset+octet-stream",
        },
        content=payload,
    )
    assert uploaded.status_code == 200
    completed = client.post(
        f"/v1/uploads/{transfer_id}/complete", headers=device_headers
    )
    assert completed.status_code == 200
    assert completed.json()["sha256"] == digest
    assert not staged_file.exists()

    overview = client.get("/v1/admin/transfers", headers=manager_headers).json()
    inbound = next(item for item in overview["inbound"] if item["id"] == transfer_id)
    assert inbound["status"] == "completed"
    assert inbound["network_bytes"] == len(payload)
    assert inbound["storage_bytes"] == len(payload)
    assert inbound["staging_path"] == str(staging)

    downloaded = client.get(
        f"/v1/spaces/{space['id']}/files/SSD/large.bin",
        headers=device_headers,
    )
    assert downloaded.status_code == 200
    assert downloaded.content == payload
    assert downloaded.headers["X-Transfer-ID"]
    overview = client.get("/v1/admin/transfers", headers=manager_headers).json()
    outbound = next(
        item
        for item in overview["outbound"]
        if item["id"] == downloaded.headers["X-Transfer-ID"]
    )
    assert outbound["status"] == "completed"
    assert outbound["network_bytes"] == len(payload)

    stored = app.state.runtime.storage.find_file(space["id"], "SSD/large.bin")
    assert stored is not None
    storage_root = app.state.runtime.storage._root_by_id(stored.storage_root_id)
    assert (storage_root.path / stored.object_path).is_file()


def test_failed_ssd_to_storage_move_keeps_staged_file_and_can_retry(
    tmp_path, monkeypatch
) -> None:
    app, client, manager_headers, device_headers, space = provision(tmp_path)
    staging = configure_ssd_staging(client, manager_headers, tmp_path)
    payload = b"retry-staged-transfer"
    digest = hashlib.sha256(payload).hexdigest()
    created = client.post(
        f"/v1/spaces/{space['id']}/uploads",
        headers=device_headers,
        json={
            "logical_path": "retry.bin",
            "size_bytes": len(payload),
            "sha256": digest,
        },
    ).json()
    transfer_id = created["id"]
    client.patch(
        f"/v1/uploads/{transfer_id}",
        headers={
            **device_headers,
            "Upload-Offset": "0",
            "Content-Type": "application/offset+octet-stream",
        },
        content=payload,
    )

    storage = app.state.runtime.storage
    original_commit = storage._commit_upload

    def fail_once(**_values):
        raise RuntimeError("simulated HDD failure")

    monkeypatch.setattr(storage, "_commit_upload", fail_once)
    with pytest.raises(RuntimeError, match="simulated HDD failure"):
        client.post(f"/v1/uploads/{transfer_id}/complete", headers=device_headers)

    staged_file = staging / "incoming" / f"{transfer_id}.resume"
    assert staged_file.is_file()
    failed = storage.get_transfer(transfer_id)
    assert failed.status == "failed"
    assert storage.transfer_to_dict(failed)["retryable"] is True

    monkeypatch.setattr(storage, "_commit_upload", original_commit)
    queued = client.post(
        f"/v1/admin/transfers/{transfer_id}/retry",
        headers=manager_headers,
    )
    assert queued.status_code == 200
    assert queued.json()["status"] == "moving"
    assert storage.get_transfer(transfer_id).status == "completed"
    assert not staged_file.exists()
    downloaded = client.get(
        f"/v1/spaces/{space['id']}/files/retry.bin",
        headers=device_headers,
    )
    assert downloaded.content == payload


def test_staging_settings_reject_unsafe_or_nonempty_paths(tmp_path) -> None:
    _, client, manager_headers, _, _ = provision(tmp_path)
    unsafe = client.put(
        "/v1/admin/transfers/settings",
        headers=manager_headers,
        json={"staging_enabled": True, "staging_path": str(tmp_path / "arbitrary")},
    )
    assert unsafe.status_code == 422

    parent = tmp_path / "occupied"
    parent.mkdir()
    staging = parent / "CloudStorageCache"
    staging.mkdir()
    (staging / "user-file.txt").write_text("keep me", encoding="utf-8")
    occupied = client.put(
        "/v1/admin/transfers/settings",
        headers=manager_headers,
        json={"staging_enabled": True, "staging_path": str(staging)},
    )
    assert occupied.status_code == 422
    assert (staging / "user-file.txt").read_text(encoding="utf-8") == "keep me"
