from __future__ import annotations

import gc
import hashlib
import io
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import uvicorn
from test_server_transfers import provision
from test_updates import signed_manifest

from cloud_storage.client.api_client import ClientApi, ClientApiError, ClientConnectionError
from cloud_storage.file_lock import file_lock
from cloud_storage.retry import retry_after_seconds, retry_delay
from cloud_storage.updates import UpdateError, UpdateService, parse_signed_manifest


@pytest.fixture
def files(tmp_path):
    app, client, manager, headers, space = provision(tmp_path)
    path = f"/v1/spaces/{space['id']}/files/sample.bin"
    assert client.put(path, headers=headers, content=b"0123456789").status_code == 201
    return app, client, manager, headers, space, path


@pytest.mark.parametrize(
    "requested,status,expected",
    [
        ("bytes=3-5", 206, b"345"),
        ("bytes=7-", 206, b"789"),
        ("bytes=-4", 206, b"6789"),
        ("bytes=0-99", 206, b"0123456789"),
        ("bytes=10-", 416, b""),
        ("bytes=4-2", 416, b""),
        ("bytes=-0", 416, b""),
        ("bytes=-", 416, b""),
        ("bytes=0-1,3-4", 200, b"0123456789"),
    ],
)
def test_real_ranges_and_accounting(files, requested, status, expected):
    app, client, _, headers, _, path = files
    response = client.get(path, headers={**headers, "Range": requested})
    assert response.status_code == status
    assert response.content == expected
    assert response.headers["Accept-Ranges"] == "bytes"
    if status == 416:
        assert response.headers["Content-Range"] == "bytes */10"
    else:
        transfer = app.state.runtime.storage.get_transfer(response.headers["X-Transfer-ID"])
        assert transfer.total_bytes == len(expected)
        assert transfer.network_bytes == len(expected)
        assert transfer.status == "completed"


def test_if_range_and_public_link_share_the_same_stream(files):
    _, client, _, headers, space, path = files
    etag = client.get(path, headers=headers).headers["ETag"]
    assert (
        client.get(path, headers={**headers, "Range": "bytes=3-", "If-Range": etag}).content
        == b"3456789"
    )
    changed = client.get(path, headers={**headers, "Range": "bytes=3-", "If-Range": '"old"'})
    assert changed.status_code == 200 and changed.content == b"0123456789"
    share = client.post(
        "/v1/shares",
        headers=headers,
        json={"space_id": space["id"], "logical_path": "sample.bin", "kind": "file"},
    ).json()
    partial = client.get(share["url_path"] + "/download", headers={"Range": "bytes=1-2"})
    assert partial.status_code == 206 and partial.content == b"12"
    assert client.get(path, headers={"Range": "bytes=1-2"}).status_code == 401


def test_empty_file_ranges(files):
    _, client, _, headers, space, _ = files
    path = f"/v1/spaces/{space['id']}/files/empty.bin"
    client.put(path, headers=headers, content=b"")
    assert client.get(path, headers=headers).content == b""
    assert client.get(path, headers={**headers, "Range": "bytes=0-"}).status_code == 416


def test_unused_locks_are_collected_without_replacing_live_waiters(files):
    app, *_ = files
    storage = app.state.runtime.storage
    first = storage._resumable_lock("one")
    identity = id(first)
    entered = threading.Event()

    def waiter():
        lock = storage._resumable_lock("one")
        entered.set()
        with lock:
            assert id(lock) == identity

    with first, ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(waiter)
        assert entered.wait(2)
        # Let the waiter acquire before the pool is joined.
        first.release()
        try:
            future.result(timeout=2)
        finally:
            first.acquire()
    del first
    for n in range(1000):
        with storage._transfer_lock(str(n)):
            pass
    gc.collect()
    assert len(storage._transfer_locks) == 0
    assert len(storage._resumable_locks) == 0


def test_retry_after_is_bounded_and_delays_are_jittered():
    assert retry_after_seconds("120") == 120
    assert retry_after_seconds("99999999999999") == 3600
    assert retry_after_seconds("bad") is None
    assert retry_delay(1, 120) >= 120
    assert 60 <= retry_delay(10000) <= 72


class DownloadResponse(io.BytesIO):
    def __init__(self, data, status=200, range_header=None, break_after_first=False):
        super().__init__(data)
        self.status = status
        self.headers = {"Content-Range": range_header} if range_header else {}
        self.break_after_first = break_after_first
        self.reads = 0

    def geturl(self):
        return "https://updates.example/payload.exe"

    def read(self, n=-1):
        self.reads += 1
        if self.break_after_first and self.reads > 1:
            raise OSError("network interrupted")
        return super().read(n)


@pytest.mark.parametrize("supports_range", [True, False])
def test_update_download_resumes_after_interruption(tmp_path, monkeypatch, supports_range):
    data = b"signed update payload"
    manifest, key = signed_manifest(data)
    info = parse_signed_manifest(manifest, expected_product="client", public_key=key)
    service = UpdateService("client", tmp_path, public_key=key)
    responses = [
        DownloadResponse(data[:5], break_after_first=True),
        DownloadResponse(data[5:], 206, f"bytes 5-{len(data) - 1}/{len(data)}")
        if supports_range
        else DownloadResponse(data),
    ]
    requests = []

    def open_response(request, **kwargs):
        requests.append(request)
        return responses.pop(0)

    monkeypatch.setattr(service, "_open_verified", open_response)
    with pytest.raises(UpdateError):
        service.download(info)
    assert (service.download_directory / f".{info.package.sha256}.download").read_bytes() == data[
        :5
    ]
    assert service.download(info).read_bytes() == data
    assert requests[1].get_header("Range") == "bytes=5-"


def test_corrupt_update_is_discarded_and_not_installed(tmp_path, monkeypatch):
    manifest, key = signed_manifest(b"correct")
    info = parse_signed_manifest(manifest, expected_product="client", public_key=key)
    service = UpdateService("client", tmp_path, public_key=key)
    monkeypatch.setattr(service, "_open_verified", lambda *a, **k: DownloadResponse(b"corrupt"))
    with pytest.raises(UpdateError, match="SHA-256"):
        service.download(info)
    assert not (service.download_directory / info.package.filename).exists()
    assert not list(service.download_directory.glob("*.download"))


def test_download_lock_excludes_a_second_handle(tmp_path):
    path = tmp_path / "download.lock"
    with file_lock(path), pytest.raises(BlockingIOError), file_lock(path):
        pass
    with file_lock(path):
        pass


def test_desktop_resume_over_real_http_and_changed_file_protection(files, tmp_path):
    app, client, _, headers, _, path = files
    observed = []

    async def traced(scope, receive, send):
        if scope["type"] == "http":
            observed.append(dict(scope["headers"]))
        await app(scope, receive, send)

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            traced,
            host="127.0.0.1",
            port=port,
            log_config=None,
            access_log=False,
            lifespan="off",
            timeout_graceful_shutdown=3,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    api = ClientApi(
        f"http://127.0.0.1:{port}", token=headers["Authorization"].removeprefix("Bearer ")
    )
    data = b"0123456789"
    digest = hashlib.sha256(data).hexdigest()
    destination = tmp_path / "copy.bin"
    partial = tmp_path / "copy.bin.part"
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.02)
        assert server.started
        partial.write_bytes(data[:4])
        api.download_file(path.split("/")[3], "sample.bin", destination, expected_sha256=digest)
        assert destination.read_bytes() == data
        assert observed[-1][b"range"] == b"bytes=4-"
        assert observed[-1][b"if-range"] == f'"sha256:{digest}"'.encode()
        partial.write_bytes(data)
        api.download_file(path.split("/")[3], "sample.bin", destination, expected_sha256=digest)
        assert destination.read_bytes() == data
        partial.write_bytes(b"corruption")
        with pytest.raises(ClientConnectionError):
            api.download_file(path.split("/")[3], "sample.bin", destination, expected_sha256=digest)
        assert not partial.exists()
        api.download_file(path.split("/")[3], "sample.bin", destination, expected_sha256=digest)
        assert destination.read_bytes() == data
        partial.write_bytes(data[:4])
        destination.write_bytes(b"keep local copy")
        client.put(path, headers=headers, content=b"abcdefghij").raise_for_status()
        with pytest.raises(ClientApiError) as error:
            api.download_file(path.split("/")[3], "sample.bin", destination, expected_sha256=digest)
        assert error.value.status_code == 409
        assert destination.read_bytes() == b"keep local copy"
    finally:
        api.close()
        server.should_exit = True
        thread.join(5)
    assert not thread.is_alive()
