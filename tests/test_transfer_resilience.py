from __future__ import annotations

import asyncio
import hashlib
import http.client
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from cloud_storage.client.api_client import ClientApi, ClientConnectionError
from cloud_storage.client.transfers import TransferStore
from cloud_storage.client.window import ClientWindow
from cloud_storage.core.api import create_app
from cloud_storage.core.config import CoreConfig
from cloud_storage.core.storage import UploadSession
from cloud_storage.core.transfer_io import (
    MAX_ACTIVE_UPLOADS,
    MAX_UPLOAD_CHUNK_BYTES,
    TransferAdmission,
    TransferProgress,
    transfer_io,
)


@pytest.fixture
def server(tmp_path):
    app = create_app(CoreConfig(data_directory=tmp_path / "core", max_upload_bytes=128*1024**2, lan_enabled=True))
    client = TestClient(app)
    manager = {"Authorization": "Bearer " + app.state.runtime.secrets.manager_token}
    code = client.get("/v1/admin/dynamic-pairing-code", headers=manager).json()["code"]
    paired = client.post("/v1/pairing/redeem", json={
        "code": code, "device_name": "Resilience QA", "platform": "Windows",
    }).json()
    headers = {"Authorization": "Bearer " + paired["device_token"]}
    space = client.get("/v1/spaces", headers=headers).json()[0]["id"]
    return app, client, headers, space


def test_progress_coalesces_telemetry_but_keeps_time_updates(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("cloud_storage.core.transfer_io.time.monotonic", lambda: clock[0])
    calls = []
    progress = TransferProgress(calls.append)
    for count in range(1, 65):
        progress.update(count * 256*1024)
    assert calls == [16*1024**2]
    clock[0] = 0.51
    progress.update(16*1024**2 + 1)
    assert calls[-1] == 16*1024**2 + 1


def test_cancel_waits_for_worker_before_closing_its_file():
    entered, release = threading.Event(), threading.Event()
    writes = []

    def write():
        entered.set()
        assert release.wait(5)
        writes.append("write finished")

    async def scenario():
        task = asyncio.create_task(transfer_io(write))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()  # Repeated cancellation must not orphan the writer.
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        writes.append("file closed")

    asyncio.run(scenario())
    assert writes == ["write finished", "file closed"]


def test_admission_releases_slot_on_exception():
    admission = TransferAdmission(1)
    with pytest.raises(OSError), admission.slot():
        with pytest.raises(HTTPException) as error, admission.slot():
            pass
        assert error.value.status_code == 503
        assert error.value.headers["Retry-After"] == "2"
        raise OSError("disk failure")
    with admission.slot():
        pass


def test_busy_uploads_leave_health_responsive_and_reject_excess(server, monkeypatch):
    app, _, headers, space = server
    release = threading.Event()
    count = [0]
    lock = threading.Lock()
    original = UploadSession.write

    def slow_write(self, chunk):
        with lock:
            count[0] += 1
        assert release.wait(8)
        return original(self, chunk)

    monkeypatch.setattr(UploadSession, "write", slow_write)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://testserver"
        ) as client:
            jobs = [asyncio.create_task(client.put(
                f"/v1/spaces/{space}/files/{n}.bin", headers=headers, content=b"data"
            )) for n in range(MAX_ACTIVE_UPLOADS)]
            try:
                for _ in range(300):
                    if count[0] == MAX_ACTIVE_UPLOADS:
                        break
                    await asyncio.sleep(0.01)
                assert count[0] == MAX_ACTIVE_UPLOADS
                health = await asyncio.wait_for(client.get("/v1/health"), timeout=1)
                assert health.status_code == 200
                rejected = await client.put(
                    f"/v1/spaces/{space}/files/excess.bin", headers=headers, content=b"no"
                )
                assert rejected.status_code == 503
                assert rejected.headers["Retry-After"] == "2"
            finally:
                release.set()
                responses = await asyncio.gather(*jobs)
            assert all(r.status_code == 201 for r in responses)

    asyncio.run(scenario())


def test_partial_write_failure_does_not_publish_file_or_kill_server(server, monkeypatch):
    _, client, headers, space = server
    original = UploadSession.write

    def fail(self, chunk):
        original(self, chunk[:2])
        raise OSError("simulated disk write failure")

    monkeypatch.setattr(UploadSession, "write", fail)
    with pytest.raises(OSError, match="simulated disk write failure"):
        client.put(f"/v1/spaces/{space}/files/broken.bin", headers=headers, content=b"abcdef")
    assert client.get(f"/v1/spaces/{space}/files/broken.bin", headers=headers).status_code == 404
    assert client.get("/v1/health").status_code == 200
    monkeypatch.setattr(UploadSession, "write", original)
    assert client.put(f"/v1/spaces/{space}/files/next.bin", headers=headers, content=b"ok").status_code == 201


def test_new_chunk_limit_is_advertised_and_old_chunk_sizes_still_work(server):
    _, client, headers, space = server
    data = b"x" * (9*1024**2)
    upload = client.post(f"/v1/spaces/{space}/uploads", headers=headers, json={
        "logical_path": "larger-chunk.bin", "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }).json()
    assert upload["max_chunk_bytes"] == MAX_UPLOAD_CHUNK_BYTES
    chunk_headers = {**headers, "Upload-Offset": "0", "Content-Type": "application/offset+octet-stream"}
    assert client.patch(f"/v1/uploads/{upload['id']}", headers=chunk_headers, content=data).status_code == 200
    assert client.post(f"/v1/uploads/{upload['id']}/complete", headers=headers).status_code == 200
    assert client.get(f"/v1/spaces/{space}/files/larger-chunk.bin", headers=headers).content == data


def test_declared_chunk_size_cannot_hide_extra_data(server):
    _, client, headers, space = server
    upload = client.post(f"/v1/spaces/{space}/uploads", headers=headers, json={
        "logical_path": "mismatch.bin", "size_bytes": 100,
    }).json()
    result = client.patch(f"/v1/uploads/{upload['id']}", headers={
        **headers, "Upload-Offset": "0", "Content-Length": "2",
    }, content=b"too long")
    assert result.status_code == 400
    assert client.get(f"/v1/uploads/{upload['id']}", headers=headers).json()["received_bytes"] == 0


def test_stalled_sender_times_out_and_releases_capacity(server, monkeypatch):
    app, _, headers, space = server
    monkeypatch.setattr("cloud_storage.core.transfer_io.UPLOAD_IDLE_SECONDS", 0.05)

    async def stalled():
        yield b"partial"
        await asyncio.sleep(5)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://testserver"
        ) as client:
            result = await client.put(
                f"/v1/spaces/{space}/files/stalled.bin", headers=headers, content=stalled()
            )
            assert result.status_code == 408
            assert (await client.get(f"/v1/spaces/{space}/files/stalled.bin", headers=headers)).status_code == 404
            result = await client.put(
                f"/v1/spaces/{space}/files/healthy.bin", headers=headers, content=b"healthy"
            )
            assert result.status_code == 201

    asyncio.run(scenario())


class Response:
    status = 200
    will_close = False

    def __init__(self, payload=b"{}", headers=None):
        self.payload = payload
        self.headers = headers or {}

    def read(self, size=None):
        data, self.payload = self.payload, b""
        return data

    def getheader(self, name, default=None):
        return self.headers.get(name, default)


class Connection:
    sock = None

    def __init__(self, response=None):
        self.response = response or Response()
        self.fail = False
        self.requests = 0

    def request(self, *args, **kwargs):
        self.requests += 1

    def getresponse(self):
        if self.fail:
            raise http.client.RemoteDisconnected("peer closed")
        return self.response

    def close(self):
        pass


def test_json_keepalive_recovers_reads_but_never_replays_mutations(monkeypatch):
    api = ClientApi("http://127.0.0.1:8765")
    created = []

    def connect(**kwargs):
        connection = Connection()
        created.append(connection)
        return connection

    monkeypatch.setattr(api, "_connection", connect)
    api.health()
    created[0].fail = True
    api.health()
    assert len(created) == 2
    created[1].fail = True
    with pytest.raises(ClientConnectionError):
        api._json_request("/mutation", method="POST", payload={})
    assert len(created) == 2
    assert created[1].requests == 2  # One GET and exactly one POST.


def test_reusable_connections_are_not_shared_between_worker_threads(monkeypatch):
    api = ClientApi("http://127.0.0.1:8765")
    barrier = threading.Barrier(2)
    created = []

    def connect(**kwargs):
        result = Connection()
        created.append(result)
        return result

    monkeypatch.setattr(api, "_connection", connect)

    def worker(_):
        api.health()
        barrier.wait(timeout=3)
        api.health()
        api.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(worker, range(2)))
    assert len(created) == 2
    assert all(c.requests == 2 for c in created)


def test_stale_idle_connection_is_discarded_before_mutation(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("cloud_storage.client.api_client.time.monotonic", lambda: clock[0])
    api = ClientApi("http://127.0.0.1:8765")
    connections = []

    def connect(**kwargs):
        connection = Connection()
        connections.append(connection)
        return connection

    monkeypatch.setattr(api, "_connection", connect)
    api.health()
    connections[0].fail = True
    clock[0] = 3.0
    api._json_request("/mutation", method="POST", payload={})
    assert len(connections) == 2
    assert connections[0].requests == 1
    assert connections[1].requests == 1


@pytest.mark.parametrize("advertised,expected_chunks", [(None, 3), (32*1024**2, 1), (0, 3)])
def test_desktop_queue_negotiates_blocks_and_checks_acknowledgements(tmp_path, advertised, expected_chunks):
    source = tmp_path / "source.bin"
    source.write_bytes(b"x" * (9*1024**2))
    store = TransferStore(tmp_path / "queue.db")
    transfer = store.queue_upload("https://cloud.example", "space", "file.bin", source)
    chunks = []

    class Api:
        def create_resumable_upload(self, *args, **kwargs):
            result = {"id": "session", "received_bytes": 0}
            if advertised is not None:
                result["max_chunk_bytes"] = advertised
            return result

        def append_resumable_upload(self, upload_id, offset, chunk):
            chunks.append(len(chunk))
            return {"received_bytes": offset + len(chunk)}

        def complete_resumable_upload(self, upload_id):
            return {}

    window = SimpleNamespace(transfer_store=store, _transfer_should_continue=lambda _: True,
                             _sha256_file=lambda *_: hashlib.sha256(source.read_bytes()).hexdigest())
    ClientWindow._run_upload_transfer(window, Api(), transfer)
    assert len(chunks) == expected_chunks
    assert sum(chunks) == transfer.total_bytes
    assert store.get(transfer.id).status == "completed"


@pytest.mark.parametrize("bad_range", [None, "bytes 0-2/9"])
def test_incomplete_or_wrong_range_download_never_replaces_existing_file(tmp_path, monkeypatch, bad_range):
    target = tmp_path / "important.bin"
    target.write_bytes(b"original")
    api = ClientApi("http://127.0.0.1:8765")
    response = Response(b"bad", {"Content-Length": "9"})
    if bad_range:
        target.with_name(target.name + ".part").write_bytes(b"prefix")
        response.status = 206
        response.headers["Content-Range"] = bad_range
    monkeypatch.setattr(api, "_connection", lambda **kwargs: Connection(response))
    with pytest.raises(ClientConnectionError):
        api.download_file("space", "important.bin", target)
    assert target.read_bytes() == b"original"
