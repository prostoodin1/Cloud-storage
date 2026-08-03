from __future__ import annotations

import hashlib
import json
import os
import stat

from fastapi.testclient import TestClient

from cloud_storage.core.api import create_app
from cloud_storage.core.config import CoreConfig


def build_client(tmp_path, max_upload_bytes: int = 1024**2):
    config = CoreConfig(
        data_directory=tmp_path,
        max_upload_bytes=max_upload_bytes,
        pairing_ttl_seconds=900,
    )
    app = create_app(config)
    with app.state.runtime.database.transaction() as connection:
        connection.execute(
            "UPDATE storage_roots SET min_free_bytes = 0, max_fill_percent = 99 "
            "WHERE id = 'default'"
        )
    client = TestClient(app)
    manager_headers = {"Authorization": f"Bearer {app.state.runtime.secrets.manager_token}"}
    return app, client, manager_headers


def provision_trusted_device(tmp_path):
    app, client, manager_headers = build_client(tmp_path)
    created = client.post(
        "/v1/admin/users",
        headers=manager_headers,
        json={
            "username": "ivan",
            "display_name": "Иван",
            "quota_gib": 1,
            "role": "member",
        },
    )
    assert created.status_code == 201
    user = created.json()["user"]
    space = created.json()["personal_space"]
    invitation = client.post(
        "/v1/admin/invitations",
        headers=manager_headers,
        json={"user_id": user["id"], "ttl_seconds": 900},
    )
    assert invitation.status_code == 201
    paired = client.post(
        "/v1/pairing/redeem",
        json={
            "code": invitation.json()["code"],
            "password": "correct horse battery staple",
            "device_name": "Ivan PC",
            "platform": "Windows",
        },
    )
    assert paired.status_code == 201
    device = paired.json()["device"]
    token = paired.json()["device_token"]
    device_headers = {"Authorization": f"Bearer {token}"}
    approved = client.post(f"/v1/admin/devices/{device['id']}/approve", headers=manager_headers)
    assert approved.status_code == 200
    return app, client, manager_headers, device_headers, user, space, invitation


def test_health_and_manager_authorization(tmp_path) -> None:
    _, client, manager_headers = build_client(tmp_path)

    health = client.get("/v1/health")
    denied = client.get("/v1/admin/summary")
    allowed = client.get("/v1/admin/summary", headers=manager_headers)

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert denied.status_code == 401
    assert allowed.status_code == 200
    assert allowed.json()["users"] == 0


def test_one_time_pairing_requires_admin_approval(tmp_path) -> None:
    app, client, manager_headers = build_client(tmp_path)
    created = client.post(
        "/v1/admin/users",
        headers=manager_headers,
        json={"username": "anna", "display_name": "Анна", "quota_gib": 10},
    ).json()
    invitation = client.post(
        "/v1/admin/invitations",
        headers=manager_headers,
        json={"user_id": created["user"]["id"]},
    ).json()
    body = {
        "code": invitation["code"],
        "password": "long unique password 2026",
        "device_name": "Anna Laptop",
        "platform": "Linux",
    }

    paired = client.post("/v1/pairing/redeem", json=body)
    duplicate = client.post("/v1/pairing/redeem", json=body)
    token = paired.json()["device_token"]
    device_headers = {"Authorization": f"Bearer {token}"}

    assert paired.status_code == 201
    assert paired.json()["device"]["status"] == "pending"
    assert duplicate.status_code == 422
    assert client.get("/v1/spaces", headers=device_headers).status_code == 403
    assert client.get("/v1/pairing/status", headers=device_headers).json()["status"] == "pending"

    device_id = paired.json()["device"]["id"]
    approved = client.post(f"/v1/admin/devices/{device_id}/approve", headers=manager_headers)

    assert approved.status_code == 200
    spaces = client.get("/v1/spaces", headers=device_headers)
    assert spaces.status_code == 200
    assert spaces.json()[0]["name"] == "Мои файлы"
    assert spaces.json()[0]["permission"] == "owner"
    assert app.state.runtime.repository.summary()["trusted_devices"] == 1

    second_invitation = client.post(
        "/v1/admin/invitations",
        headers=manager_headers,
        json={"user_id": created["user"]["id"]},
    ).json()
    cancelled = client.delete(
        f"/v1/admin/invitations/{second_invitation['id']}", headers=manager_headers
    )
    rejected = client.post("/v1/pairing/redeem", json={**body, "code": second_invitation["code"]})
    assert cancelled.json()["cancelled"] is True
    assert rejected.status_code == 422


def test_resumable_upload_survives_core_restart_and_rejects_wrong_offset(tmp_path) -> None:
    _, client, _, device_headers, _, space, _ = provision_trusted_device(tmp_path)
    payload = (b"resumable-cloud-storage-" * 20_000) + b"done"
    digest = hashlib.sha256(payload).hexdigest()
    created = client.post(
        f"/v1/spaces/{space['id']}/uploads",
        headers=device_headers,
        json={
            "logical_path": "Большой файл.bin",
            "size_bytes": len(payload),
            "content_type": "application/octet-stream",
            "sha256": digest,
        },
    )
    assert created.status_code == 201
    upload_id = created.json()["id"]
    split = len(payload) // 2
    first = client.patch(
        f"/v1/uploads/{upload_id}",
        headers={
            **device_headers,
            "Upload-Offset": "0",
            "Content-Type": "application/offset+octet-stream",
        },
        content=payload[:split],
    )
    assert first.status_code == 200
    assert first.json()["received_bytes"] == split

    restarted = TestClient(create_app(CoreConfig(data_directory=tmp_path)))
    status = restarted.get(f"/v1/uploads/{upload_id}", headers=device_headers)
    assert status.status_code == 200
    assert status.headers["Upload-Offset"] == str(split)
    wrong = restarted.patch(
        f"/v1/uploads/{upload_id}",
        headers={
            **device_headers,
            "Upload-Offset": "0",
            "Content-Type": "application/offset+octet-stream",
        },
        content=payload[split:],
    )
    assert wrong.status_code == 409

    second = restarted.patch(
        f"/v1/uploads/{upload_id}",
        headers={
            **device_headers,
            "Upload-Offset": str(split),
            "Content-Type": "application/offset+octet-stream",
        },
        content=payload[split:],
    )
    assert second.status_code == 200
    completed = restarted.post(f"/v1/uploads/{upload_id}/complete", headers=device_headers)
    assert completed.status_code == 200
    assert completed.json()["sha256"] == digest
    repeated = restarted.post(f"/v1/uploads/{upload_id}/complete", headers=device_headers)
    assert repeated.status_code == 200
    downloaded = restarted.get(
        f"/v1/spaces/{space['id']}/files/%D0%91%D0%BE%D0%BB%D1%8C%D1%88%D0%BE%D0%B9%20%D1%84%D0%B0%D0%B9%D0%BB.bin",
        headers=device_headers,
    )
    assert downloaded.content == payload


def test_manager_can_activate_a_safe_managed_disk_root(tmp_path) -> None:
    app, client, manager_headers = build_client(tmp_path / "core")
    mount = tmp_path / "mounted-disk"
    mount.mkdir()
    managed = mount / "CloudStorageData"

    response = client.put(
        "/v1/admin/storage-roots",
        headers=manager_headers,
        json={
            "roots": [
                {
                    "disk_id": "disk-safe-test",
                    "path": str(managed),
                    "priority": 75,
                    "max_fill_percent": 88,
                    "min_free_gib": 0,
                }
            ]
        },
    )

    assert response.status_code == 200
    assert response.json()[0]["disk_id"] == "disk-safe-test"
    assert (managed / ".cloud-storage-root.json").is_file()
    assert (managed / ".staging").is_dir()
    with app.state.runtime.database.connection() as connection:
        default_enabled = connection.execute(
            "SELECT enabled FROM storage_roots WHERE id = 'default'"
        ).fetchone()[0]
    assert default_enabled == 0


def test_manager_cannot_adopt_nonempty_unmarked_directory(tmp_path) -> None:
    _, client, manager_headers = build_client(tmp_path / "core")
    managed = tmp_path / "mounted-disk" / "CloudStorageData"
    managed.mkdir(parents=True)
    (managed / "user-file.txt").write_text("do not touch", encoding="utf-8")

    response = client.put(
        "/v1/admin/storage-roots",
        headers=manager_headers,
        json={
            "roots": [
                {
                    "disk_id": "disk-safe-test",
                    "path": str(managed),
                    "min_free_gib": 0,
                }
            ]
        },
    )

    assert response.status_code == 422
    assert (managed / "user-file.txt").read_text(encoding="utf-8") == "do not touch"
    assert not (managed / ".cloud-storage-root.json").exists()


def test_paused_root_stays_readable_and_verified_migration_keeps_source_copy(tmp_path) -> None:
    app, client, manager_headers, device_headers, _, space, _ = provision_trusted_device(tmp_path)
    source = tmp_path / "source-disk" / "CloudStorageData"
    target = tmp_path / "target-disk" / "CloudStorageData"
    source.parent.mkdir()
    target.parent.mkdir()
    roots = [
        {
            "disk_id": "disk-source-safe",
            "path": str(source),
            "priority": 100,
            "max_fill_percent": 99,
            "min_free_gib": 0,
            "write_enabled": True,
        },
        {
            "disk_id": "disk-target-safe",
            "path": str(target),
            "priority": 10,
            "max_fill_percent": 99,
            "min_free_gib": 0,
            "write_enabled": True,
        },
    ]
    assert client.put("/v1/admin/storage-roots", headers=manager_headers, json={"roots": roots}).status_code == 200

    payload = b"verified migration payload"
    route = f"/v1/spaces/{space['id']}/files/archive.bin"
    uploaded = client.put(route, headers=device_headers, content=payload)
    assert uploaded.status_code == 201
    record = app.state.runtime.storage.find_file(space["id"], "archive.bin")
    assert record is not None
    source_root_id = record.storage_root_id
    original_path = source / record.object_path
    assert original_path.read_bytes() == payload

    roots[0]["write_enabled"] = False
    paused = client.put(
        "/v1/admin/storage-roots", headers=manager_headers, json={"roots": roots}
    )
    assert paused.status_code == 200
    assert client.get(route, headers=device_headers).content == payload

    target_root_id = next(
        item["id"] for item in paused.json() if item["disk_id"] == "disk-target-safe"
    )
    migration = client.post(
        "/v1/admin/maintenance/migrations",
        headers=manager_headers,
        json={"source_root_id": source_root_id, "target_root_id": target_root_id},
    )
    assert migration.status_code == 202
    job = client.get(
        f"/v1/admin/maintenance/jobs/{migration.json()['id']}", headers=manager_headers
    ).json()
    assert job["status"] == "completed"
    assert job["processed_files"] == 1
    assert job["retained_sources"] == 1

    migrated = app.state.runtime.storage.find_file(space["id"], "archive.bin")
    assert migrated is not None
    assert migrated.storage_root_id == target_root_id
    assert (target / migrated.object_path).read_bytes() == payload
    assert original_path.read_bytes() == payload
    assert client.get(route, headers=device_headers).content == payload


def test_verified_backup_contains_database_manifest_current_file_and_version(tmp_path) -> None:
    app, client, manager_headers, device_headers, _, space, _ = provision_trusted_device(tmp_path)
    primary = tmp_path / "primary-disk" / "CloudStorageData"
    backup = tmp_path / "backup-disk" / "CloudStorageData"
    primary.parent.mkdir()
    backup.parent.mkdir()
    roots_response = client.put(
        "/v1/admin/storage-roots",
        headers=manager_headers,
        json={
            "roots": [
                {
                    "disk_id": "disk-primary-backup-test",
                    "path": str(primary),
                    "priority": 100,
                    "max_fill_percent": 99,
                    "min_free_gib": 0,
                    "purpose": "primary",
                },
                {
                    "disk_id": "disk-backup-target-test",
                    "path": str(backup),
                    "priority": 100,
                    "max_fill_percent": 99,
                    "min_free_gib": 0,
                    "purpose": "backup",
                },
            ]
        },
    )
    assert roots_response.status_code == 200
    backup_root_id = next(
        item["id"] for item in roots_response.json() if item["purpose"] == "backup"
    )
    route = f"/v1/spaces/{space['id']}/files/backup.txt"
    assert client.put(route, headers=device_headers, content=b"version one").status_code == 201
    assert client.put(route, headers=device_headers, content=b"version two").status_code == 201
    current = app.state.runtime.storage.find_file(space["id"], "backup.txt")
    assert current is not None
    assert current.storage_root_id != backup_root_id

    started = client.post(
        "/v1/admin/backups",
        headers=manager_headers,
        json={"target_root_id": backup_root_id},
    )
    assert started.status_code == 202
    job = client.get(
        f"/v1/admin/backups/{started.json()['id']}", headers=manager_headers
    ).json()
    assert job["status"] == "completed"
    assert job["processed_files"] == 2
    snapshot = backup / job["snapshot_path"]
    assert (snapshot / "metadata.sqlite3").is_file()
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["objects"]) == 2
    for item in manifest["objects"]:
        copied = snapshot / item["backup_object_path"]
        assert copied.is_file()
        assert hashlib.sha256(copied.read_bytes()).hexdigest() == item["sha256"]
    assert client.get(route, headers=device_headers).content == b"version two"


def test_mirror_replication_repair_and_download_failover(tmp_path) -> None:
    app, client, manager_headers, device_headers, _, space, _ = provision_trusted_device(tmp_path)
    primary = tmp_path / "primary-mirror-test" / "CloudStorageData"
    mirror = tmp_path / "mirror-target-test" / "CloudStorageData"
    primary.parent.mkdir()
    mirror.parent.mkdir()
    roots_response = client.put(
        "/v1/admin/storage-roots",
        headers=manager_headers,
        json={
            "roots": [
                {
                    "disk_id": "disk-primary-mirror-test",
                    "path": str(primary),
                    "priority": 100,
                    "max_fill_percent": 99,
                    "min_free_gib": 0,
                    "purpose": "primary",
                },
                {
                    "disk_id": "disk-mirror-target-test",
                    "path": str(mirror),
                    "priority": 100,
                    "max_fill_percent": 99,
                    "min_free_gib": 0,
                    "purpose": "mirror",
                },
            ]
        },
    )
    assert roots_response.status_code == 200
    mirror_root_id = next(
        item["id"] for item in roots_response.json() if item["purpose"] == "mirror"
    )
    route = f"/v1/spaces/{space['id']}/files/mirrored.bin"
    assert client.put(route, headers=device_headers, content=b"first").status_code == 201
    assert client.put(route, headers=device_headers, content=b"second version").status_code == 201

    current = app.state.runtime.storage.find_file(space["id"], "mirrored.bin")
    assert current is not None
    with app.state.runtime.database.connection() as connection:
        replica = connection.execute(
            "SELECT * FROM mirror_replicas WHERE file_id = ? AND storage_root_id = ?",
            (current.id, mirror_root_id),
        ).fetchone()
    assert replica is not None
    assert replica["status"] == "current"
    assert replica["source_version"] == 2
    replica_path = mirror / replica["object_path"]
    assert replica_path.read_bytes() == b"second version"

    os.chmod(replica_path, stat.S_IREAD | stat.S_IWRITE)
    replica_path.write_bytes(b"corrupt")
    started = client.post(
        f"/v1/admin/mirrors/{mirror_root_id}/reconcile",
        headers=manager_headers,
    )
    assert started.status_code == 202
    job = client.get(
        f"/v1/admin/mirrors/jobs/{started.json()['id']}", headers=manager_headers
    ).json()
    assert job["status"] == "completed"
    assert job["repaired_files"] == 1
    status_response = client.get("/v1/admin/mirrors", headers=manager_headers).json()
    assert status_response["roots"][0]["degraded_files"] == 0

    primary.rename(primary.with_name("CloudStorageData-offline"))
    downloaded = client.get(route, headers=device_headers)
    assert downloaded.status_code == 200
    assert downloaded.content == b"second version"


def test_mirror_failure_does_not_reject_primary_upload(tmp_path) -> None:
    app, client, manager_headers, device_headers, _, space, _ = provision_trusted_device(tmp_path)
    primary = tmp_path / "primary-degraded-test" / "CloudStorageData"
    mirror = tmp_path / "mirror-degraded-test" / "CloudStorageData"
    primary.parent.mkdir()
    mirror.parent.mkdir()
    response = client.put(
        "/v1/admin/storage-roots",
        headers=manager_headers,
        json={
            "roots": [
                {
                    "disk_id": "disk-primary-degraded-test",
                    "path": str(primary),
                    "priority": 100,
                    "max_fill_percent": 99,
                    "min_free_gib": 0,
                    "purpose": "primary",
                },
                {
                    "disk_id": "disk-mirror-degraded-test",
                    "path": str(mirror),
                    "priority": 100,
                    "max_fill_percent": 99,
                    "min_free_gib": 1_000_000,
                    "purpose": "mirror",
                },
            ]
        },
    )
    assert response.status_code == 200
    route = f"/v1/spaces/{space['id']}/files/still-accepted.bin"
    uploaded = client.put(route, headers=device_headers, content=b"primary survives")
    assert uploaded.status_code == 201
    mirror_state = client.get("/v1/admin/mirrors", headers=manager_headers).json()
    assert mirror_state["roots"][0]["degraded_files"] == 1
    with app.state.runtime.database.connection() as connection:
        replica = connection.execute("SELECT status, error FROM mirror_replicas").fetchone()
    assert replica["status"] == "error"
    assert "free space" in replica["error"]

def test_personal_file_upload_update_download_and_soft_delete(tmp_path) -> None:
    app, client, _, device_headers, _, space, _ = provision_trusted_device(tmp_path)
    url = f"/v1/spaces/{space['id']}/files/Документы/hello.txt"

    uploaded = client.put(
        url,
        headers={**device_headers, "Content-Type": "text/plain"},
        content="первая версия".encode(),
    )
    updated = client.put(
        url,
        headers={**device_headers, "Content-Type": "text/plain"},
        content="вторая версия".encode(),
    )
    listing = client.get(
        f"/v1/spaces/{space['id']}/entries",
        headers=device_headers,
        params={"directory": "Документы"},
    )
    downloaded = client.get(url, headers=device_headers)

    assert uploaded.status_code == 201
    assert uploaded.json()["version"] == 1
    assert updated.status_code == 201
    assert updated.json()["version"] == 2
    assert listing.status_code == 200
    assert listing.json()[0]["name"] == "hello.txt"
    assert downloaded.status_code == 200
    assert downloaded.content == "вторая версия".encode()
    assert downloaded.headers["x-content-type-options"] == "nosniff"
    assert "attachment" in downloaded.headers["content-disposition"]

    current = app.state.runtime.storage.find_file(space["id"], "Документы/hello.txt")
    assert current is not None
    original_object = app.state.runtime.config.default_storage_root / current.object_path
    assert original_object.is_file()
    with app.state.runtime.database.connect() as connection:
        versions = connection.execute("SELECT count(*) FROM file_versions").fetchone()[0]
    assert versions == 1

    deleted = client.delete(url, headers=device_headers)
    assert deleted.status_code == 200
    deleted_record = app.state.runtime.storage.find_file(
        space["id"], "Документы/hello.txt", include_deleted=True
    )
    assert deleted_record is not None
    assert deleted_record.object_path.startswith(".trash/")
    assert not original_object.exists()
    assert (app.state.runtime.config.default_storage_root / deleted_record.object_path).is_file()
    assert client.get(url, headers=device_headers).status_code == 404
    assert client.get(f"/v1/spaces/{space['id']}/entries", headers=device_headers).json() == []


def test_upload_limit_is_enforced_without_partial_file(tmp_path) -> None:
    app, client, _, device_headers, _, space, _ = provision_trusted_device(tmp_path)
    response = client.put(
        f"/v1/spaces/{space['id']}/files/too-large.bin",
        headers=device_headers,
        content=b"x" * (1024**2 + 1),
    )

    assert response.status_code == 507
    staging = app.state.runtime.config.default_storage_root / ".staging"
    assert list(staging.iterdir()) == []
    assert app.state.runtime.storage.space_usage(space["id"]) == 0


def test_revoked_device_immediately_loses_access(tmp_path) -> None:
    _, client, manager_headers, device_headers, _, _, _ = provision_trusted_device(tmp_path)
    device_id = client.get("/v1/admin/devices", headers=manager_headers).json()[0]["id"]

    revoked = client.post(f"/v1/admin/devices/{device_id}/revoke", headers=manager_headers)

    assert revoked.status_code == 200
    assert client.get("/v1/spaces", headers=device_headers).status_code == 403
