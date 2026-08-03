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


def test_verified_restore_repairs_damage_without_overwriting_newer_file(tmp_path) -> None:
    app, client, manager_headers, device_headers, _, space, _ = provision_trusted_device(
        tmp_path
    )
    primary = tmp_path / "restore-primary" / "CloudStorageData"
    backup = tmp_path / "restore-backup" / "CloudStorageData"
    primary.parent.mkdir()
    backup.parent.mkdir()
    roots = client.put(
        "/v1/admin/storage-roots",
        headers=manager_headers,
        json={
            "roots": [
                {
                    "disk_id": "restore-primary",
                    "path": str(primary),
                    "priority": 100,
                    "max_fill_percent": 99,
                    "min_free_gib": 0,
                    "purpose": "primary",
                },
                {
                    "disk_id": "restore-backup",
                    "path": str(backup),
                    "priority": 100,
                    "max_fill_percent": 99,
                    "min_free_gib": 0,
                    "purpose": "backup",
                },
            ]
        },
    )
    assert roots.status_code == 200
    primary_root_id = next(
        item["id"] for item in roots.json() if item["purpose"] == "primary"
    )
    backup_root_id = next(
        item["id"] for item in roots.json() if item["purpose"] == "backup"
    )
    route = f"/v1/spaces/{space['id']}/files/recovery.bin"
    corrupt_route = f"/v1/spaces/{space['id']}/files/corrupt.bin"
    original = b"verified recovery payload"
    corrupt_original = b"another verified payload"
    assert client.put(route, headers=device_headers, content=original).status_code == 201
    assert (
        client.put(corrupt_route, headers=device_headers, content=corrupt_original).status_code
        == 201
    )
    backup_job = client.post(
        "/v1/admin/backups",
        headers=manager_headers,
        json={"target_root_id": backup_root_id},
    ).json()
    assert backup_job["status"] == "queued"
    completed_backup = client.get(
        f"/v1/admin/backups/{backup_job['id']}", headers=manager_headers
    ).json()
    assert completed_backup["status"] == "completed"

    damaged = app.state.runtime.storage.find_file(space["id"], "recovery.bin")
    corrupt = app.state.runtime.storage.find_file(space["id"], "corrupt.bin")
    assert damaged is not None
    assert corrupt is not None
    damaged_path = primary / damaged.object_path
    corrupt_path = primary / corrupt.object_path
    os.chmod(damaged_path, stat.S_IREAD | stat.S_IWRITE)
    damaged_path.unlink()
    os.chmod(corrupt_path, stat.S_IREAD | stat.S_IWRITE)
    corrupt_path.write_bytes(b"damaged")
    restore = client.post(
        "/v1/admin/restores",
        headers=manager_headers,
        json={
            "backup_job_id": backup_job["id"],
            "target_root_id": primary_root_id,
        },
    )
    assert restore.status_code == 202
    restored = client.get(
        f"/v1/admin/restores/{restore.json()['id']}", headers=manager_headers
    ).json()
    assert restored["status"] == "completed"
    assert restored["restored_objects"] == 2
    assert restored["failed_objects"] == 0
    assert client.get(route, headers=device_headers).content == original
    assert client.get(corrupt_route, headers=device_headers).content == corrupt_original

    newer = b"a newer healthy version"
    assert client.put(route, headers=device_headers, content=newer).status_code == 201
    second = client.post(
        "/v1/admin/restores",
        headers=manager_headers,
        json={
            "backup_job_id": backup_job["id"],
            "target_root_id": primary_root_id,
        },
    )
    assert second.status_code == 202
    second_job = client.get(
        f"/v1/admin/restores/{second.json()['id']}", headers=manager_headers
    ).json()
    assert second_job["status"] == "completed"
    assert second_job["restored_objects"] == 0
    assert second_job["skipped_objects"] == 2
    assert client.get(route, headers=device_headers).content == newer


def test_emergency_read_only_mode_blocks_mutations_but_keeps_reads_and_diagnostics(
    tmp_path,
) -> None:
    _, client, manager_headers, device_headers, user, space, _ = provision_trusted_device(
        tmp_path
    )
    route = f"/v1/spaces/{space['id']}/files/available.txt"
    assert client.put(route, headers=device_headers, content=b"available").status_code == 201

    unconfirmed = client.put(
        "/v1/admin/server-mode",
        headers=manager_headers,
        json={"mode": "read_only", "reason": "disk incident"},
    )
    assert unconfirmed.status_code == 400
    activated = client.put(
        "/v1/admin/server-mode",
        headers=manager_headers,
        json={"mode": "read_only", "reason": "disk incident", "confirmed": True},
    )
    assert activated.status_code == 200
    assert activated.json()["mode"] == "read_only"
    assert client.get("/v1/health").json()["server_mode"] == "read_only"
    assert client.get(route, headers=device_headers).content == b"available"
    assert client.put(route, headers=device_headers, content=b"blocked").status_code == 503
    assert client.delete(route, headers=device_headers).status_code == 503
    invitation = client.post(
        "/v1/admin/invitations",
        headers=manager_headers,
        json={"user_id": user["id"]},
    )
    assert invitation.status_code == 503
    diagnostic = client.post(
        "/v1/admin/diagnostics/scans",
        headers=manager_headers,
        json={"kind": "quick"},
    )
    assert diagnostic.status_code == 202
    assert diagnostic.json()["status"] == "queued"

    normal = client.put(
        "/v1/admin/server-mode",
        headers=manager_headers,
        json={"mode": "normal", "reason": "incident resolved"},
    )
    assert normal.status_code == 200
    assert normal.json()["mode"] == "normal"
    assert client.put(route, headers=device_headers, content=b"working").status_code == 201
    assert client.get(route, headers=device_headers).content == b"working"


def test_backup_policy_runs_verification_and_safely_rotates_verified_snapshots(
    tmp_path,
) -> None:
    app, client, manager_headers, device_headers, _, space, _ = provision_trusted_device(
        tmp_path
    )
    primary = tmp_path / "policy-primary" / "CloudStorageData"
    backup = tmp_path / "policy-backup" / "CloudStorageData"
    primary.parent.mkdir()
    backup.parent.mkdir()
    roots = client.put(
        "/v1/admin/storage-roots",
        headers=manager_headers,
        json={
            "roots": [
                {
                    "disk_id": "policy-primary",
                    "path": str(primary),
                    "priority": 100,
                    "max_fill_percent": 99,
                    "min_free_gib": 0,
                    "purpose": "primary",
                },
                {
                    "disk_id": "policy-backup",
                    "path": str(backup),
                    "priority": 100,
                    "max_fill_percent": 99,
                    "min_free_gib": 0,
                    "purpose": "backup",
                },
            ]
        },
    ).json()
    primary_id = next(item["id"] for item in roots if item["purpose"] == "primary")
    backup_id = next(item["id"] for item in roots if item["purpose"] == "backup")
    route = f"/v1/spaces/{space['id']}/files/policy.txt"
    assert client.put(route, headers=device_headers, content=b"first").status_code == 201

    policy = client.put(
        f"/v1/admin/backup-policies/{backup_id}",
        headers=manager_headers,
        json={
            "enabled": True,
            "interval_hours": 12,
            "keep_last": 1,
            "verification_root_id": primary_id,
        },
    )
    assert policy.status_code == 200
    assert policy.json()["enabled"] is True
    assert policy.json()["next_run_at"] is not None

    first = client.post(
        f"/v1/admin/backup-policies/{backup_id}/run", headers=manager_headers
    )
    assert first.status_code == 202
    first_job = client.get(
        f"/v1/admin/backups/{first.json()['id']}", headers=manager_headers
    ).json()
    assert first_job["status"] == "completed"
    first_snapshot = backup / first_job["snapshot_path"]
    assert first_snapshot.is_dir()
    state = client.get("/v1/admin/backup-automation", headers=manager_headers).json()
    assert state["verifications"][0]["status"] == "completed"
    assert state["verifications"][0]["checked_objects"] == 1
    assert not any((primary / ".staging").glob("verify-*"))

    assert client.put(route, headers=device_headers, content=b"second").status_code == 201
    second = client.post(
        f"/v1/admin/backup-policies/{backup_id}/run", headers=manager_headers
    )
    assert second.status_code == 202
    jobs = client.get("/v1/admin/backups", headers=manager_headers).json()
    current = next(item for item in jobs if item["id"] == second.json()["id"])
    retired = next(item for item in jobs if item["id"] == first.json()["id"])
    assert current["status"] == "completed"
    assert current["pruned_at"] is None
    assert retired["pruned_at"] is not None
    assert retired["snapshot_path"] == ""
    assert not first_snapshot.exists()
    assert client.get(route, headers=device_headers).content == b"second"
    state = client.get("/v1/admin/backup-automation", headers=manager_headers).json()
    assert state["policies"][0]["last_job_id"] == second.json()["id"]
    assert len(state["verifications"]) == 2


def test_due_backup_policy_waits_in_read_only_mode_and_survives_restart(tmp_path) -> None:
    app, client, manager_headers = build_client(tmp_path / "core")
    primary = tmp_path / "due-primary" / "CloudStorageData"
    backup = tmp_path / "due-backup" / "CloudStorageData"
    primary.parent.mkdir()
    backup.parent.mkdir()
    roots = client.put(
        "/v1/admin/storage-roots",
        headers=manager_headers,
        json={
            "roots": [
                {
                    "disk_id": "due-primary",
                    "path": str(primary),
                    "priority": 100,
                    "max_fill_percent": 99,
                    "min_free_gib": 0,
                    "purpose": "primary",
                },
                {
                    "disk_id": "due-backup",
                    "path": str(backup),
                    "priority": 100,
                    "max_fill_percent": 99,
                    "min_free_gib": 0,
                    "purpose": "backup",
                },
            ]
        },
    ).json()
    primary_id = next(item["id"] for item in roots if item["purpose"] == "primary")
    backup_id = next(item["id"] for item in roots if item["purpose"] == "backup")
    assert (
        client.put(
            f"/v1/admin/backup-policies/{backup_id}",
            headers=manager_headers,
            json={
                "enabled": True,
                "interval_hours": 24,
                "keep_last": 3,
                "verification_root_id": primary_id,
            },
        ).status_code
        == 200
    )
    with app.state.runtime.database.transaction() as connection:
        connection.execute(
            "UPDATE backup_policies SET next_run_at = '2020-01-01T00:00:00+00:00'"
        )
    assert (
        client.put(
            "/v1/admin/server-mode",
            headers=manager_headers,
            json={"mode": "read_only", "reason": "maintenance", "confirmed": True},
        ).status_code
        == 200
    )
    assert app.state.runtime.backup_automation.run_due_policies() == []
    assert client.get("/v1/admin/backups", headers=manager_headers).json() == []
    assert (
        client.put(
            "/v1/admin/server-mode",
            headers=manager_headers,
            json={"mode": "normal", "reason": "ready"},
        ).status_code
        == 200
    )
    due_jobs = app.state.runtime.backup_automation.run_due_policies()
    assert len(due_jobs) == 1
    assert app.state.runtime.storage.get_backup_job(due_jobs[0]).status == "completed"

    restarted = create_app(CoreConfig(data_directory=tmp_path / "core"))
    restored_policy = restarted.state.runtime.backup_automation.get_policy(backup_id)
    assert restored_policy.enabled is True
    assert restored_policy.keep_last == 3
    assert restored_policy.last_job_id == due_jobs[0]


def test_trial_restore_rejects_corrupted_snapshot_without_touching_live_file(tmp_path) -> None:
    app, client, manager_headers, device_headers, _, space, _ = provision_trusted_device(
        tmp_path
    )
    primary = tmp_path / "verify-primary" / "CloudStorageData"
    backup = tmp_path / "verify-backup" / "CloudStorageData"
    primary.parent.mkdir()
    backup.parent.mkdir()
    roots = client.put(
        "/v1/admin/storage-roots",
        headers=manager_headers,
        json={
            "roots": [
                {
                    "disk_id": "verify-primary",
                    "path": str(primary),
                    "priority": 100,
                    "max_fill_percent": 99,
                    "min_free_gib": 0,
                    "purpose": "primary",
                },
                {
                    "disk_id": "verify-backup",
                    "path": str(backup),
                    "priority": 100,
                    "max_fill_percent": 99,
                    "min_free_gib": 0,
                    "purpose": "backup",
                },
            ]
        },
    ).json()
    primary_id = next(item["id"] for item in roots if item["purpose"] == "primary")
    backup_id = next(item["id"] for item in roots if item["purpose"] == "backup")
    route = f"/v1/spaces/{space['id']}/files/live.bin"
    live = b"live data remains safe"
    assert client.put(route, headers=device_headers, content=live).status_code == 201
    backup_job = client.post(
        "/v1/admin/backups",
        headers=manager_headers,
        json={"target_root_id": backup_id},
    ).json()
    completed = app.state.runtime.storage.get_backup_job(backup_job["id"])
    snapshot = backup / completed.snapshot_path
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    damaged = snapshot / manifest["objects"][0]["backup_object_path"]
    damaged.write_bytes(b"corrupted backup")

    started = client.post(
        f"/v1/admin/backups/{backup_job['id']}/verify",
        headers=manager_headers,
        json={"target_root_id": primary_id},
    )
    assert started.status_code == 202
    result = client.get(
        f"/v1/admin/backup-verifications/{started.json()['id']}",
        headers=manager_headers,
    ).json()
    assert result["status"] == "failed"
    assert "SHA-256" in result["error"] or "size" in result["error"]
    assert client.get(route, headers=device_headers).content == live
    assert not any((primary / ".staging").glob("verify-*"))


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


def test_full_diagnostics_detects_deduplicates_and_resolves_missing_object(tmp_path) -> None:
    app, client, manager_headers, device_headers, _, space, _ = provision_trusted_device(tmp_path)
    payload = b"diagnostic integrity payload"
    route = f"/v1/spaces/{space['id']}/files/diagnostics.bin"
    assert client.put(route, headers=device_headers, content=payload).status_code == 201
    record = app.state.runtime.storage.find_file(space["id"], "diagnostics.bin")
    assert record is not None
    object_path = app.state.runtime.config.default_storage_root / record.object_path
    os.chmod(object_path, stat.S_IREAD | stat.S_IWRITE)
    object_path.unlink()

    first = client.post(
        "/v1/admin/diagnostics/scans",
        headers=manager_headers,
        json={"kind": "full"},
    )
    assert first.status_code == 202
    first_result = client.get(
        f"/v1/admin/diagnostics/scans/{first.json()['id']}", headers=manager_headers
    ).json()
    assert first_result["status"] == "completed"
    assert first_result["critical_count"] == 1
    overview = client.get("/v1/admin/diagnostics", headers=manager_headers).json()
    assert overview["status"] == "critical"
    assert overview["incidents"][0]["check_key"] == "object.available"

    second = client.post(
        "/v1/admin/diagnostics/scans",
        headers=manager_headers,
        json={"kind": "full"},
    )
    assert second.status_code == 202
    repeated = client.get("/v1/admin/diagnostics", headers=manager_headers).json()
    assert len(repeated["incidents"]) == 1
    assert repeated["incidents"][0]["occurrences"] == 2

    object_path.write_bytes(payload)
    resolved = client.post(
        "/v1/admin/diagnostics/scans",
        headers=manager_headers,
        json={"kind": "full"},
    )
    assert resolved.status_code == 202
    healthy = client.get("/v1/admin/diagnostics", headers=manager_headers).json()
    assert healthy["status"] == "healthy"
    assert healthy["incidents"] == []
    history = client.get(
        "/v1/admin/diagnostics/incidents",
        headers=manager_headers,
        params={"include_resolved": True},
    ).json()
    assert history[0]["status"] == "resolved"
    assert history[0]["resolved_at"] is not None


def test_diagnostics_recover_scan_interrupted_by_restart(tmp_path) -> None:
    app, _, _ = build_client(tmp_path)
    now = "2026-08-03T10:00:00.000Z"
    with app.state.runtime.database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO diagnostic_scans(
                id, kind, source, status, checks, checked_objects, checked_bytes,
                warning_count, critical_count, error, created_at, completed_at, updated_at
            ) VALUES('interrupted-scan', 'full', 'manager', 'running', 0, 0, 0, 0, 0,
                     '', ?, NULL, ?)
            """,
            (now, now),
        )

    restarted = create_app(CoreConfig(data_directory=tmp_path))
    scan = restarted.state.runtime.diagnostics.get_scan("interrupted-scan")
    assert scan.status == "failed"
    assert "перезапуском Core" in scan.error
    assert scan.completed_at is not None

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
