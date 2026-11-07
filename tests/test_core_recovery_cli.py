from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest
import uvicorn
from fastapi.testclient import TestClient

from cloud_storage import cli
from cloud_storage.client.api_client import ClientApi, ClientConnectionError
from cloud_storage.core.api import create_app
from cloud_storage.core.config import CoreConfig
from cloud_storage.core.repository import ConflictError
from cloud_storage.core.storage import StorageService


@pytest.fixture
def sandbox(tmp_path):
    config = CoreConfig(data_directory=tmp_path / "core", lan_enabled=True)
    app = create_app(config)
    runtime = app.state.runtime
    with runtime.database.transaction() as db:
        db.execute("UPDATE storage_roots SET min_free_bytes=0, max_fill_percent=99")
    client = TestClient(app)
    manager = {"Authorization": "Bearer " + runtime.secrets.manager_token}
    code = client.get("/v1/admin/dynamic-pairing-code", headers=manager).json()["code"]
    paired = client.post("/v1/pairing/redeem", json={
        "code": code, "device_name": "Core CLI QA", "platform": "CLI",
    }).json()
    headers = {"Authorization": "Bearer " + paired["device_token"]}
    space = client.get("/v1/spaces", headers=headers).json()[0]["id"]
    return SimpleNamespace(app=app, runtime=runtime, storage=runtime.storage, client=client,
                           manager=manager, headers=headers, space=space,
                           user=paired["device"]["user_id"], token=paired["device_token"])


def prepare(s, data=b"durable file", name="test.bin"):
    record = s.storage.create_resumable_upload(space_id=s.space, user_id=s.user,
        logical_path=name, expected_size=len(data),
        content_type="application/octet-stream", expected_sha256=hashlib.sha256(data).hexdigest())
    if data:
        s.storage.append_resumable_upload(record.id, s.user, 0, data)
    return record.id


def test_fault_after_publication_leaves_session_completed_without_duplicate(sandbox, monkeypatch):
    s = sandbox
    upload = prepare(s)
    original = s.storage._finalize_inbound_transfer

    def interrupted(upload_id):
        original(upload_id)
        raise RuntimeError("interrupted after durable publication")

    monkeypatch.setattr(s.storage, "_finalize_inbound_transfer", interrupted)
    with pytest.raises(RuntimeError):
        s.storage.complete_resumable_upload(upload, s.user)
    monkeypatch.setattr(s.storage, "_finalize_inbound_transfer", original)
    assert s.storage.get_resumable_upload(upload, s.user).status == "completed"
    result = s.storage.complete_resumable_upload(upload, s.user)
    assert result.version == 1
    assert result.sha256 == hashlib.sha256(b"durable file").hexdigest()


def test_fault_after_metadata_commit_never_rolls_back_published_bytes(sandbox, monkeypatch):
    s = sandbox
    upload = prepare(s)
    original = s.storage._commit_upload

    def interrupted(**kwargs):
        original(**kwargs)
        raise OSError("mirror/response failed after commit")

    monkeypatch.setattr(s.storage, "_commit_upload", interrupted)
    result = s.storage.complete_resumable_upload(upload, s.user)
    transfer = s.storage.get_transfer(upload)
    assert s.storage._inbound_destination(transfer).read_bytes() == b"durable file"
    assert result.version == 1
    assert s.storage.get_resumable_upload(upload, s.user).status == "completed"


@pytest.mark.parametrize("corrupt", [False, True])
def test_restart_after_rename_recovers_only_verified_destination(sandbox, monkeypatch, corrupt):
    s = sandbox
    upload = prepare(s)

    class SimulatedProcessDeath(BaseException):
        pass

    def die(**kwargs):
        raise SimulatedProcessDeath()

    monkeypatch.setattr(s.storage, "_commit_upload", die)
    with pytest.raises(SimulatedProcessDeath):
        s.storage.complete_resumable_upload(upload, s.user)
    final = s.storage._inbound_destination(s.storage.get_transfer(upload))
    if corrupt:
        s.storage._make_staging_file_writable(final)
        final.write_bytes(b"wrong data")
    fresh = StorageService(s.runtime.config, s.runtime.database, s.runtime.repository)
    assert fresh.get_resumable_upload(upload, s.user).received_bytes == len(b"durable file")
    if corrupt:
        with pytest.raises(ConflictError, match="SHA-256"):
            fresh.complete_resumable_upload(upload, s.user)
        assert fresh.find_file(s.space, "test.bin") is None
        assert final.exists()
    else:
        result = fresh.complete_resumable_upload(upload, s.user)
        assert result.version == 1
        assert fresh.complete_resumable_upload(upload, s.user).id == result.id


def test_legacy_split_state_is_repaired_and_old_session_cannot_ack_new_version(sandbox):
    s = sandbox
    first = prepare(s)
    one = s.storage.complete_resumable_upload(first, s.user)
    with s.runtime.database.transaction() as db:
        db.execute("UPDATE upload_sessions SET status='active',result_file_id=NULL WHERE id=?", (first,))
        db.execute("UPDATE transfer_jobs SET status='failed' WHERE id=?", (first,))
    assert s.storage.get_resumable_upload(first, s.user).status == "completed"
    second = prepare(s, b"next version")
    two = s.storage.complete_resumable_upload(second, s.user)
    assert one.id == two.id and two.version == 2
    assert s.storage.complete_resumable_upload(second, s.user).version == 2
    with pytest.raises(ConflictError):
        s.storage.complete_resumable_upload(first, s.user)


def test_real_process_exit_after_rename_then_fresh_core_boot_recovers(sandbox):
    s = sandbox
    upload = prepare(s)
    worker = """
import os,sys
from pathlib import Path
from cloud_storage.core.api import create_app
from cloud_storage.core.config import CoreConfig
app=create_app(CoreConfig(data_directory=Path(sys.argv[1]),lan_enabled=True))
app.state.runtime.storage._commit_upload=lambda **kwargs: os._exit(91)
app.state.runtime.storage.complete_resumable_upload(sys.argv[2],sys.argv[3])
"""
    result = subprocess.run([sys.executable, "-c", worker, str(s.runtime.config.data_directory),
                             upload, s.user], capture_output=True, timeout=20)
    assert result.returncode == 91
    fresh_app = create_app(s.runtime.config)
    fresh = fresh_app.state.runtime.storage
    assert TestClient(fresh_app).get("/v1/health").status_code == 200
    record = fresh.complete_resumable_upload(upload, s.user)
    assert record.sha256 == hashlib.sha256(b"durable file").hexdigest()
    assert record.version == 1
    assert fresh.get_resumable_upload(upload, s.user).status == "completed"


def test_failed_completion_transaction_restores_staging_for_retry(sandbox, monkeypatch):
    s = sandbox
    upload = prepare(s)
    original = s.storage._finish_upload_state

    def failure(*args, **kwargs):
        raise OSError("metadata transaction interrupted")

    monkeypatch.setattr(s.storage, "_finish_upload_state", failure)
    with pytest.raises(OSError):
        s.storage.complete_resumable_upload(upload, s.user)
    assert s.storage.find_file(s.space, "test.bin") is None
    record = s.storage.get_resumable_upload(upload, s.user)
    assert s.storage._resumable_path(record).read_bytes() == b"durable file"
    monkeypatch.setattr(s.storage, "_finish_upload_state", original)
    assert s.storage.complete_resumable_upload(upload, s.user).version == 1


def test_late_mirror_error_after_concurrent_overwrite_preserves_archived_bytes(sandbox, monkeypatch):
    s = sandbox
    first = prepare(s)
    second = prepare(s, b"newest bytes")
    entered = []

    def concurrent_mirror(record):
        if not entered:
            entered.append(True)
            s.storage.complete_resumable_upload(second, s.user)
            raise OSError("mirror failed after newer version was published")

    monkeypatch.setattr(s.storage, "sync_file_to_mirrors", concurrent_mirror)
    with pytest.raises(ConflictError):
        s.storage.complete_resumable_upload(first, s.user)
    first_path = s.storage._inbound_destination(s.storage.get_transfer(first))
    assert first_path.read_bytes() == b"durable file"
    current = s.storage.find_file(s.space, "test.bin")
    assert current.sha256 == hashlib.sha256(b"newest bytes").hexdigest()
    assert current.version == 2
    assert s.storage.complete_resumable_upload(second, s.user).version == 2


def test_startup_cleanup_skips_unavailable_disk_and_retains_session(sandbox, monkeypatch):
    s = sandbox
    upload = prepare(s)
    with s.runtime.database.transaction() as db:
        db.execute("UPDATE upload_sessions SET expires_at='2000-01-01' WHERE id=?", (upload,))

    def unavailable(*args):
        raise OSError("device unavailable")

    monkeypatch.setattr(s.storage, "cancel_resumable_upload", unavailable)
    assert s.storage.cleanup_expired_uploads() == 0
    with s.runtime.database.connection() as db:
        assert db.execute("SELECT count(*) FROM upload_sessions WHERE id=?", (upload,)).fetchone()[0] == 1


def test_readiness_reports_full_and_disappeared_disk_without_killing_api(sandbox, monkeypatch):
    s = sandbox
    assert s.client.get("/v1/admin/storage-readiness", headers=s.headers).status_code == 403
    path = "/v1/admin/storage-readiness"
    assert s.client.get(path, headers=s.manager).json()["ready"]
    with s.runtime.database.transaction() as db:
        db.execute("UPDATE storage_roots SET min_free_bytes=?", (10**18,))
    response = s.client.get(path, headers=s.manager)
    assert not response.json()["ready"]
    assert response.json()["roots"][0]["reason"] == "storage_capacity"
    with s.runtime.database.transaction() as db:
        db.execute("UPDATE storage_roots SET path=?", (str(s.runtime.config.data_directory / "missing"),))
    assert s.client.get(path, headers=s.manager).json()["roots"][0]["reason"] == "storage_unavailable"
    assert s.client.get("/v1/health").status_code == 200


@pytest.mark.parametrize("error_code,status", [(sqlite3.SQLITE_IOERR, 503),
                                              (sqlite3.SQLITE_FULL, 507),
                                              (sqlite3.SQLITE_READONLY, 503)])
def test_metadata_io_failure_is_structured_and_health_stays_available(sandbox, monkeypatch, error_code, status):
    s = sandbox

    def failed():
        error = sqlite3.OperationalError("synthetic metadata I/O error")
        error.sqlite_errorcode = error_code
        raise error

    monkeypatch.setattr(s.storage, "storage_readiness", failed)
    response = s.client.get("/v1/admin/storage-readiness", headers=s.manager)
    assert response.status_code == status
    assert response.json()["code"] in {"metadata_full", "metadata_io_unavailable"}
    assert s.client.get("/v1/health").status_code == 200


@pytest.fixture
def network(sandbox):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(sandbox.app, log_level="critical", access_log=False,
                                           log_config=None, lifespan="off"))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    yield sandbox, f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(10)
    sock.close()
    assert not thread.is_alive()


def test_cli_real_network_lost_chunk_reply_checkpoint_and_verified_download(network, tmp_path, monkeypatch, capsys):
    s, url = network
    monkeypatch.setenv("QA_DEVICE", s.token)
    source = tmp_path / "source.bin"
    source.write_bytes(b"CLI roundtrip" * 200000)
    state = tmp_path / "checkpoint.json"
    prefix = ["--server", url, "--token-env", "QA_DEVICE", "--retries", "0"]
    command = prefix + ["upload", s.space, str(source), "CLI/test.bin", "--state", str(state)]
    original = ClientApi.append_resumable_upload
    calls = []

    def lost_reply(self, *args):
        calls.append(args[1])
        original(self, *args)
        raise ClientConnectionError("lost response " + s.token)

    monkeypatch.setattr(ClientApi, "append_resumable_upload", lost_reply)
    assert cli.main(command) == 4
    first_output = capsys.readouterr().out
    assert s.token not in first_output
    assert json.loads(first_output)["error"]["retryable"]
    assert state.exists() and s.token not in state.read_text()
    assert cli.main(prefix + ["upload-status", str(state)]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["received_bytes"] == source.stat().st_size
    monkeypatch.setattr(ClientApi, "append_resumable_upload", original)
    assert cli.main(command) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["data"]["sha256_verified"]
    assert result["data"]["file"]["version"] == 1
    assert cli.main(command) == 0
    assert json.loads(capsys.readouterr().out)["data"]["file"]["version"] == 1
    target = tmp_path / "download.bin"
    # Exercise a pre-existing partial download through the real HTTP endpoint.
    target.with_name(target.name + ".part").write_bytes(source.read_bytes()[:500])
    assert cli.main(prefix + ["download", s.space, "CLI/test.bin", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"]
    assert target.read_bytes() == source.read_bytes()
    assert cli.main(prefix + ["download", s.space, "CLI/test.bin", str(target)]) == 2
    capsys.readouterr()
    monkeypatch.setenv("QA_MANAGER", s.runtime.secrets.manager_token)
    assert cli.main(["--server", url, "--token-env", "QA_MANAGER", "doctor"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["ready"]


def test_cli_usage_json_never_echoes_mistaken_secret_argument(capsys):
    assert cli.main(["--token", "pretend-private-token", "spaces"]) == 2
    text = capsys.readouterr().out
    assert "pretend-private-token" not in text
    assert json.loads(text)["error"]["code"] == "invalid_request"


def test_cli_missing_credentials_and_remote_http_fail_closed(monkeypatch, capsys):
    monkeypatch.delenv("CLOUD_STORAGE_DEVICE_TOKEN", raising=False)
    assert cli.main(["spaces"]) == 3
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "access_denied"
    assert cli.main(["--server", "http://example.com", "health"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_request"


def test_cli_capabilities_without_graphics(capsys):
    # CLI dependency graph does not need to import QApplication or client.window.
    assert cli.main(["capabilities"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["data"]["interactive"] is False
    assert "upload" in result["data"]["commands"]
    assert "cloud_storage.client.window" not in cli.__dict__


def test_cli_unicode_json_works_in_ascii_encoded_agent_pipe():
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "ascii"
    code = ("from cloud_storage import cli; "
            "cli._execute=lambda args: ({'name':'\\u0414\\u043e\\u043a\\u0443\\u043c\\u0435\\u043d\\u0442'},0); "
            "raise SystemExit(cli.main(['capabilities']))")
    process = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, timeout=10)
    assert process.returncode == 0
    assert json.loads(process.stdout.decode("ascii"))["data"]["name"] == "Документ"
