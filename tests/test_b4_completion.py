from __future__ import annotations

import asyncio
import hashlib
import socket
import sqlite3
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from cloud_storage.client.api_client import ClientApi, TransferInterrupted
from cloud_storage.core.api import create_app
from cloud_storage.core.config import CoreConfig
from cloud_storage.core.repository import PermissionDeniedError
from cloud_storage.core.transfer_io import MAX_ACTIVE_UPLOADS


@pytest.fixture
def server(tmp_path):
    app = create_app(CoreConfig(data_directory=tmp_path / "core", lan_enabled=True))
    runtime = app.state.runtime
    with runtime.database.transaction() as db:
        db.execute("UPDATE storage_roots SET min_free_bytes=0, max_fill_percent=99")
    client = TestClient(app)
    manager = {"Authorization": "Bearer " + runtime.secrets.manager_token}
    code = client.get("/v1/admin/dynamic-pairing-code", headers=manager).json()["code"]
    paired = client.post("/v1/pairing/redeem", json={
        "code": code, "device_name": "B4 QA", "platform": "Windows",
    }).json()
    headers = {"Authorization": "Bearer " + paired["device_token"]}
    space = client.get("/v1/spaces", headers=headers).json()[0]["id"]
    return app, headers, space, paired


async def prepare(client, headers, space, name="sample.bin", digest=None):
    data = b"verified content"
    created = await client.post(f"/v1/spaces/{space}/uploads", headers=headers, json={
        "logical_path": name, "size_bytes": len(data),
        "sha256": digest or hashlib.sha256(data).hexdigest(),
    })
    assert created.status_code == 201
    upload = created.json()["id"]
    written = await client.patch(f"/v1/uploads/{upload}", headers={
        **headers, "Upload-Offset": "0", "Content-Type": "application/offset+octet-stream",
    }, content=data)
    assert written.status_code == 200
    return upload, data


def test_async_hash_is_deduplicated_and_status_remains_available(server, monkeypatch):
    app, headers, space, _ = server
    storage = app.state.runtime.storage
    original = storage._file_sha256
    entered, release = threading.Event(), threading.Event()
    calls = []

    def hash_file(path):
        calls.append(path)
        entered.set()
        assert release.wait(10)
        return original(path)

    monkeypatch.setattr(storage, "_file_sha256", hash_file)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://testserver") as client:
            upload, data = await prepare(client, headers, space)
            route = f"/v1/uploads/{upload}/complete"
            try:
                replies = await asyncio.gather(*(
                    client.post(route, headers={**headers, "Prefer": "respond-async"}) for _ in range(8)
                ))
                assert entered.is_set()
                assert all(r.status_code == 202 and r.json()["completion_pending"] for r in replies)
                assert all(r.headers["Retry-After"] == "2" for r in replies)
                status = await client.get(f"/v1/uploads/{upload}", headers=headers)
                assert status.json()["status"] == "verifying"
                assert (await client.get("/v1/health")).status_code == 200
                assert (await client.get(f"/v1/spaces/{space}/files/sample.bin", headers=headers)).status_code == 404
                # Disconnection of a synchronous waiter cannot cancel the hash.
                waiter = asyncio.create_task(client.post(route, headers=headers))
                await asyncio.sleep(0.05)
                waiter.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiter
            finally:
                release.set()
            completed = await client.post(route, headers=headers)
            assert completed.status_code == 200
            assert completed.json()["sha256"] == hashlib.sha256(data).hexdigest()
            assert len(calls) == 1
            assert (await client.post(route, headers=headers)).json()["id"] == completed.json()["id"]
            assert len(calls) == 1

    asyncio.run(scenario())


def test_finalizers_share_bounded_upload_capacity(server, monkeypatch):
    app, headers, space, _ = server
    original = app.state.runtime.storage._file_sha256
    release = threading.Event()

    def blocked(path):
        assert release.wait(10)
        return original(path)

    monkeypatch.setattr(app.state.runtime.storage, "_file_sha256", blocked)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://testserver") as client:
            uploads = [await prepare(client, headers, space, f"file-{n}") for n in range(MAX_ACTIVE_UPLOADS + 1)]
            async_headers = {**headers, "Prefer": "respond-async"}
            try:
                for upload, _ in uploads[:-1]:
                    assert (await client.post(f"/v1/uploads/{upload}/complete", headers=async_headers)).status_code == 202
                rejected = await client.post(f"/v1/uploads/{uploads[-1][0]}/complete", headers=async_headers)
                assert rejected.status_code == 503
                assert rejected.headers["Retry-After"] == "2"
            finally:
                release.set()
            for upload, _ in uploads:
                assert (await client.post(f"/v1/uploads/{upload}/complete", headers=headers)).status_code == 200

    asyncio.run(scenario())


def test_async_completion_never_publishes_bad_hash(server):
    app, headers, space, _ = server

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://testserver") as client:
            upload, _ = await prepare(client, headers, space, digest="0" * 64)
            for _ in range(100):
                reply = await client.post(f"/v1/uploads/{upload}/complete", headers={**headers, "Prefer": "respond-async"})
                if reply.status_code != 202:
                    break
                await asyncio.sleep(0.01)
            assert reply.status_code == 409
            assert (await client.get(f"/v1/spaces/{space}/files/sample.bin", headers=headers)).status_code == 404

    asyncio.run(scenario())


def test_authentication_is_read_only_under_writer_lock_and_still_checks_revocation(server):
    app, _, _, paired = server
    repository = app.state.runtime.repository
    database = app.state.runtime.database
    token = paired["device_token"]
    device_id = paired["device"]["id"]
    with database.transaction() as db:
        db.execute("UPDATE devices SET last_seen_at=NULL WHERE id=?", (device_id,))
    with database.transaction() as db:
        db.execute("UPDATE devices SET name=name WHERE id=?", (device_id,))
        started = time.monotonic()
        for _ in range(20):
            assert repository.authenticate_device(token).id == device_id
        assert time.monotonic() - started < 2
    repository.authenticate_device(token)
    assert repository.get_device(device_id).last_seen_at is not None
    with database.connection() as db:
        last_seen = repository.get_device(device_id).last_seen_at
        db.execute("UPDATE devices SET last_seen_at=? WHERE id=?", (last_seen, device_id))
    repository.set_device_status(device_id, "revoked")
    with pytest.raises(PermissionDeniedError):
        repository.authenticate_device(token)


def test_busy_database_gets_retryable_response_without_exposing_details(server, monkeypatch):
    app, headers, _, _ = server

    def busy(*args, **kwargs):
        error = sqlite3.OperationalError("database is locked; sensitive path")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        raise error

    monkeypatch.setattr(app.state.runtime.repository, "authenticate_device", busy)
    response = TestClient(app).get("/v1/spaces", headers=headers)
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "2"
    assert response.json()["code"] == "database_busy"
    assert "sensitive" not in response.text


def test_desktop_completion_polls_and_pause_does_not_report_success(monkeypatch):
    api = ClientApi("http://127.0.0.1:8765")
    responses = iter([{"completion_pending": True}, {"id": "file", "sha256": "verified"}])
    calls = []

    def request(route, **kwargs):
        calls.append((route, kwargs))
        return next(responses)

    monkeypatch.setattr(api, "_json_request", request)
    monkeypatch.setattr("cloud_storage.client.api_client.time.sleep", lambda _: None)
    assert api.complete_resumable_upload("upload")["id"] == "file"
    assert len(calls) == 2
    assert all(kwargs["headers"]["Prefer"] == "respond-async" for _, kwargs in calls)
    monkeypatch.setattr(api, "_json_request", lambda *args, **kwargs: {"completion_pending": True})
    active = iter([True, False])
    with pytest.raises(TransferInterrupted):
        api.complete_resumable_upload("upload", should_continue=lambda: next(active))


def test_real_http_completion_exceeds_old_30_second_limit(server, monkeypatch):
    app, _, space, paired = server
    original = app.state.runtime.storage._file_sha256
    calls = []

    def slow_hash(path):
        calls.append(path)
        time.sleep(31.1)
        return original(path)

    monkeypatch.setattr(app.state.runtime.storage, "_file_sha256", slow_hash)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    http_server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, lifespan="off", log_level="critical", access_log=False,
    ))
    thread = threading.Thread(target=http_server.run, daemon=True)
    thread.start()
    api = ClientApi(f"http://127.0.0.1:{port}", token=paired["device_token"])
    try:
        deadline = time.monotonic() + 5
        while not http_server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert http_server.started
        data = b"slow verification is not a lost connection"
        digest = hashlib.sha256(data).hexdigest()
        upload = api.create_resumable_upload(space, "slow.bin", len(data), sha256=digest)
        api.append_resumable_upload(upload["id"], 0, data)
        started = time.monotonic()
        result = api.complete_resumable_upload(upload["id"])
        assert time.monotonic() - started >= 31
        assert result["sha256"] == digest
        assert len(calls) == 1
        assert api.resumable_upload_status(upload["id"])["status"] == "completed"
    finally:
        api.close()
        http_server.should_exit = True
        thread.join(5)
        assert not thread.is_alive()


def test_busy_progress_is_skipped_but_other_database_errors_propagate(server, monkeypatch):
    app, _, _, _ = server
    storage = app.state.runtime.storage
    database = app.state.runtime.database
    with database.transaction() as db:
        db.execute("UPDATE devices SET name=name")
        started = time.monotonic()
        storage._update_transfer_network("nonexistent", 1024)
        assert time.monotonic() - started < 2
    original = database.connect

    def broken(**kwargs):
        error = sqlite3.OperationalError("disk I/O error")
        error.sqlite_errorcode = sqlite3.SQLITE_IOERR
        raise error

    monkeypatch.setattr(database, "connect", broken)
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        storage._update_transfer_network("nonexistent", 1024)
    monkeypatch.setattr(database, "connect", original)
