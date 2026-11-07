"""Non-interactive client for shells and AI tools. No Qt imports or secret arguments."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import tempfile
import time
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from cloud_storage import __version__
from cloud_storage.client.api_client import (
    CertificateMismatch,
    ClientApi,
    ClientApiError,
    ClientConnectionError,
)
from cloud_storage.file_lock import file_lock

COMMANDS = ["capabilities", "health", "doctor", "spaces", "ls", "upload", "upload-status", "download"]


class UsageError(ValueError):
    pass


class DeadlineExceeded(RuntimeError):
    pass


class Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def error(self, message):
        # argparse messages may echo a mistakenly supplied credential argument.
        raise UsageError("invalid CLI arguments; run cloud-storage --help")


def parser() -> Parser:
    p = Parser(prog="cloud-storage", description=__doc__)
    p.add_argument("--server", default=os.environ.get("CLOUD_STORAGE_SERVER_URL", "http://127.0.0.1:8765"))
    p.add_argument("--token-env", default="CLOUD_STORAGE_DEVICE_TOKEN")
    p.add_argument("--fingerprint", default="")
    p.add_argument("--json", action="store_true", help="JSON is also the default output")
    p.add_argument("--retries", type=int, default=3)
    p.add_argument("--transfer-timeout", type=int, default=3600, help="Transfer deadline in seconds; checkpoint survives timeout")
    commands = p.add_subparsers(dest="command", required=True, parser_class=Parser)
    for name in ("capabilities", "health", "spaces"):
        commands.add_parser(name)
    doctor = commands.add_parser("doctor", help="Read-only server-side storage readiness; manager token required")
    doctor.add_argument("--local", action="store_true", help="Read existing local Core credentials without creating them")
    doctor.add_argument("--core-data-dir", type=Path)
    ls = commands.add_parser("ls")
    ls.add_argument("space")
    ls.add_argument("directory", nargs="?", default="")
    upload = commands.add_parser("upload")
    upload.add_argument("space")
    upload.add_argument("source", type=Path)
    upload.add_argument("destination", help="Path inside the remote space")
    upload.add_argument("--state", type=Path, help="Durable checkpoint; repeat this command to resume")
    upload.add_argument("--restart", action="store_true", help="Explicitly begin a new upload instead of reusing the checkpoint")
    upload.add_argument("--chunk-mib", type=int, default=8)
    status = commands.add_parser("upload-status", help="Read server-authoritative progress from a checkpoint")
    status.add_argument("state", type=Path)
    download = commands.add_parser("download")
    download.add_argument("space")
    download.add_argument("source", help="Remote logical path")
    download.add_argument("destination", type=Path)
    download.add_argument("--overwrite", action="store_true")
    return p


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, CertificateMismatch):
        return False
    return (isinstance(exc, (ClientConnectionError, DeadlineExceeded))
            or isinstance(exc, ClientApiError) and exc.status_code in {408, 429, 500, 502, 503, 504})


def _retry(function, retries: int):
    for attempt in range(retries + 1):
        try:
            return function()
        except (ClientConnectionError, ClientApiError) as exc:
            if attempt == retries or not _retryable(exc):
                raise
            delay = getattr(exc, "retry_after", None)
            time.sleep(min(10.0, delay if delay is not None else 0.5 * 2**attempt + random.random() * 0.2))


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".checkpoint-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _upload(api: ClientApi, args) -> dict:
    if not 1 <= args.chunk_mib <= 64:
        raise UsageError("--chunk-mib must be between 1 and 64")
    source = args.source.resolve(strict=True)
    before = source.stat()
    with source.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise UsageError("source changed while hashing; retry after it stops changing")
    identity = {"server": args.server.rstrip("/"), "space": args.space,
                "destination": args.destination, "source": str(source),
                "size": before.st_size, "sha256": digest}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    cache = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".cache")) / "CloudStorageCLI" / "uploads"
    state_path = args.state or cache / (key + ".json")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(state_path.with_suffix(state_path.suffix + ".lock")):
        if state_path.exists() and not args.restart:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if state.get("identity") != identity:
                raise UsageError("checkpoint belongs to a different file or destination; use --restart explicitly")
        else:
            # Creation is intentionally not retried blindly: a lost reply can
            # leave a server session, but must not duplicate/overwrite a file.
            created = api.create_resumable_upload(args.space, args.destination, before.st_size, sha256=digest)
            state = {"schema_version": 1, "identity": identity, "upload_id": created["id"]}
            _atomic_json(state_path, state)

        deadline = time.monotonic() + args.transfer_timeout

        def within_deadline():
            if time.monotonic() >= deadline:
                raise DeadlineExceeded("transfer deadline exceeded")
            return True

        def send_remaining():
            within_deadline()
            status = api.resumable_upload_status(state["upload_id"])
            if (status.get("expected_size") != before.st_size
                    or status.get("expected_sha256") != digest):
                raise UsageError("server upload identity differs from checkpoint")
            offset = int(status["received_bytes"])
            if not 0 <= offset <= before.st_size:
                raise UsageError("server returned invalid upload offset")
            if status["status"] == "completed":
                return api.complete_resumable_upload(state["upload_id"], should_continue=within_deadline)
            chunk_size = min(args.chunk_mib * 1024**2, int(status["max_chunk_bytes"]))
            if chunk_size < 1:
                raise UsageError("server returned invalid chunk limit")
            with source.open("rb") as stream:
                stream.seek(offset)
                while offset < before.st_size:
                    within_deadline()
                    current = source.stat()
                    if (current.st_size, current.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
                        raise UsageError("source changed during upload; checkpoint retained")
                    chunk = stream.read(min(chunk_size, before.st_size - offset))
                    if not chunk:
                        raise UsageError("source ended before declared size")
                    # On an ambiguous failure the outer retry re-reads the
                    # server offset instead of resending at an assumed offset.
                    updated = api.append_resumable_upload(state["upload_id"], offset, chunk)
                    confirmed = int(updated["received_bytes"])
                    if confirmed != offset + len(chunk):
                        raise UsageError("server returned unexpected upload offset")
                    offset = confirmed
            return api.complete_resumable_upload(state["upload_id"], should_continue=within_deadline)

        result = _retry(send_remaining, args.retries)
        if result.get("sha256") != digest or result.get("size_bytes") != before.st_size:
            raise UsageError("completed file differs from source; checkpoint retained")
        return {"file": result, "checkpoint": str(state_path), "sha256_verified": True}


def _download(api: ClientApi, args) -> dict:
    path = PurePosixPath(args.source)
    entries = _retry(lambda: api.list_entries(args.space, str(path.parent) if str(path.parent) != "." else ""), args.retries)
    entry = next((item for item in entries if item.get("name") == path.name and item.get("type") == "file"), None)
    if not entry or not entry.get("sha256"):
        raise UsageError("file with a verifiable SHA-256 was not found")
    destination = args.destination.resolve()
    if destination.exists() and not args.overwrite:
        raise UsageError("destination exists; use --overwrite explicitly")
    deadline = time.monotonic() + args.transfer_timeout

    def within_deadline():
        if time.monotonic() >= deadline:
            raise DeadlineExceeded("transfer deadline exceeded")
        return True

    _retry(lambda: api.download_file(args.space, args.source, destination,
                                   expected_sha256=entry["sha256"],
                                   should_continue=within_deadline), args.retries)
    return {"destination": str(destination), "sha256": entry["sha256"], "sha256_verified": True}


def _execute(args) -> tuple[dict | list, int]:
    if not 0 <= args.retries <= 8:
        raise UsageError("--retries must be between 0 and 8")
    if not 1 <= args.transfer_timeout <= 604800:
        raise UsageError("--transfer-timeout must be between 1 and 604800 seconds")
    if args.command == "capabilities":
        return {"version": __version__, "commands": COMMANDS, "interactive": False,
                "output_schema": 1, "transfer_protocol": "cloud-storage-v1",
                "tus_supported": False, "secret_input": "environment only"}, 0
    token = os.environ.get(args.token_env, "")
    if args.command == "doctor" and args.local:
        from cloud_storage.core.config import default_core_data_directory
        directory = args.core_data_dir or default_core_data_directory()
        config = json.loads((directory / "core-config.json").read_text(encoding="utf-8-sig"))
        secrets = json.loads((directory / "core-secrets.json").read_text(encoding="utf-8-sig"))
        args.server = f"http://127.0.0.1:{int(config.get('port', 8765))}"
        token = secrets["manager_token"]
    elif args.command == "doctor" and args.core_data_dir is not None:
        raise UsageError("--core-data-dir requires --local")
    parsed = urlsplit(args.server)
    if (parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.scheme not in {"https", "http"} or not parsed.hostname):
        raise UsageError("server must be an HTTP(S) URL without credentials, query or fragment")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise UsageError("remote connections require HTTPS")
    if args.command != "health" and not token:
        raise PermissionError("authentication token environment variable is not set")
    api = ClientApi(args.server, token=token or None, certificate_fingerprint=args.fingerprint)
    try:
        if args.command == "health":
            return _retry(api.health, args.retries), 0
        if args.command == "doctor":
            result = _retry(lambda: api._json_request("/v1/admin/storage-readiness"), args.retries)
            return result, 0 if result.get("ready") is True else 5
        if args.command == "spaces":
            return _retry(api.list_spaces, args.retries), 0
        if args.command == "ls":
            return _retry(lambda: api.list_entries(args.space, args.directory), args.retries), 0
        if args.command == "upload":
            return _upload(api, args), 0
        if args.command == "upload-status":
            state = json.loads(args.state.read_text(encoding="utf-8"))
            if state["identity"]["server"] != args.server.rstrip("/"):
                raise UsageError("checkpoint belongs to a different server")
            return _retry(lambda: api.resumable_upload_status(state["upload_id"]), args.retries), 0
        return _download(api, args), 0
    finally:
        api.close()


def main(argv: list[str] | None = None) -> int:
    args = None
    try:
        args = parser().parse_args(argv)
        data, exit_code = _execute(args)
        output = {"schema_version": 1, "ok": exit_code == 0, "data": data}
    except KeyboardInterrupt:
        exit_code = 130
        output = {"schema_version": 1, "ok": False,
                  "error": {"code": "interrupted", "message": "operation interrupted; repeat upload with the same checkpoint to resume", "retryable": True}}
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        if isinstance(exc, DeadlineExceeded):
            code, exit_code = "deadline_exceeded", 4
        elif isinstance(exc, (PermissionError, CertificateMismatch)) or status in {401, 403}:
            code, exit_code = "access_denied", 3
        elif status == 507:
            code, exit_code = "storage_capacity", 5
        elif isinstance(exc, BlockingIOError) or status == 409:
            code, exit_code = "conflict", 6
        elif isinstance(exc, ClientConnectionError) or status in {408, 429, 500, 502, 503, 504}:
            code, exit_code = "server_unavailable", 4
        elif isinstance(exc, (UsageError, ValueError)):
            code, exit_code = "invalid_request", 2
        elif isinstance(exc, OSError):
            code, exit_code = "local_io", 7
        elif status is not None:
            code, exit_code = "api_error", 4
        else:
            code, exit_code = "internal_error", 1
        # Do not echo arbitrary exception text/tracebacks, which can contain a
        # token, URL or response body. Codes provide the stable AI contract.
        messages = {"access_denied": "credentials, permissions or certificate verification failed",
                    "deadline_exceeded": "transfer deadline exceeded; checkpoint retained",
                    "storage_capacity": "server quota or safe storage capacity is exhausted",
                    "server_unavailable": "server or storage is unavailable; checkpoint retained",
                    "conflict": "operation conflicts with current state; checkpoint retained",
                    "local_io": "local file or checkpoint is unavailable",
                    "api_error": "server rejected the operation",
                    "internal_error": "unexpected client error"}
        message = str(exc) if isinstance(exc, UsageError) else messages.get(code, "invalid request")
        if args is not None:
            secret = os.environ.get(args.token_env, "")
            if secret:
                message = message.replace(secret, "[REDACTED_SECRET]")
        output = {"schema_version": 1, "ok": False,
                  "error": {"code": code, "message": message,
                            "http_status": status, "retryable": _retryable(exc)}}
    # ASCII JSON is valid UTF-8 on every console/pipe code page, including
    # Windows agents launched with an ASCII/legacy stdout encoding.
    print(json.dumps(output, ensure_ascii=True, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
