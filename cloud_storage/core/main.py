from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path

import psutil
import uvicorn

from cloud_storage.core.api import create_app
from cloud_storage.core.config import CoreConfig
from cloud_storage.core.discovery import DiscoveryResponder


class AlreadyRunningError(RuntimeError):
    pass


class PidGuard:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.acquired = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                existing_pid = int(self.path.read_text(encoding="ascii").strip())
            except (OSError, ValueError):
                existing_pid = 0
            if existing_pid and psutil.pid_exists(existing_pid):
                raise AlreadyRunningError(f"server core is already running with PID {existing_pid}")
            self.path.unlink(missing_ok=True)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise AlreadyRunningError("server core is already starting") from exc
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(str(os.getpid()))
            handle.flush()
            os.fsync(handle.fileno())
        self.acquired = True

    def release(self) -> None:
        if self.acquired:
            try:
                current = int(self.path.read_text(encoding="ascii").strip())
                if current == os.getpid():
                    self.path.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
            self.acquired = False

    def __enter__(self) -> PidGuard:
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


class CoreServerGroup:
    def __init__(self, config: CoreConfig) -> None:
        self.config = config
        self.application = create_app(config)
        self.local_server = uvicorn.Server(self._uvicorn_config(config.host, config.port))
        self.lan_server: uvicorn.Server | None = None
        self.discovery: DiscoveryResponder | None = None
        self._lan_thread: threading.Thread | None = None
        self._lan_error: BaseException | None = None
        identity = self.application.state.runtime.tls_identity
        if config.lan_enabled:
            if identity is None:
                raise RuntimeError("LAN mode requires a TLS identity")
            self.lan_server = uvicorn.Server(
                self._uvicorn_config(
                    config.lan_host,
                    config.lan_port,
                    certificate=identity.certificate_path,
                    private_key=identity.private_key_path,
                )
            )
            self.discovery = DiscoveryResponder(config, identity)
        self.application.state.shutdown_callback = self.request_shutdown

    def _uvicorn_config(
        self,
        host: str,
        port: int,
        *,
        certificate: str | None = None,
        private_key: str | None = None,
    ) -> uvicorn.Config:
        return uvicorn.Config(
            self.application,
            host=host,
            port=port,
            log_level="info",
            access_log=False,
            server_header=False,
            date_header=False,
            ssl_certfile=certificate,
            ssl_keyfile=private_key,
        )

    def run(self) -> None:
        if self.lan_server is not None:
            self._lan_thread = threading.Thread(
                target=self._run_lan,
                name="cloud-storage-lan-api",
                daemon=True,
            )
            self._lan_thread.start()
            deadline = time.monotonic() + 5
            while not self.lan_server.started and self._lan_thread.is_alive():
                if time.monotonic() >= deadline:
                    self.request_shutdown()
                    raise RuntimeError("LAN HTTPS listener did not become ready")
                time.sleep(0.02)
            if self._lan_error is not None or not self.lan_server.started:
                self.request_shutdown()
                raise RuntimeError("LAN HTTPS listener could not start") from self._lan_error
            if self.discovery is not None:
                self.discovery.start()
        try:
            self.local_server.run()
        finally:
            self.request_shutdown(delay=False)
            if self.discovery is not None:
                self.discovery.stop()
            if self._lan_thread is not None:
                self._lan_thread.join(timeout=5)

    def request_shutdown(self, *, delay: bool = True) -> None:
        if delay:
            time.sleep(0.1)
        self.local_server.should_exit = True
        if self.lan_server is not None:
            self.lan_server.should_exit = True

    def _run_lan(self) -> None:
        assert self.lan_server is not None
        try:
            self.lan_server.run()
        except BaseException as exc:  # server thread boundary
            self._lan_error = exc


def run_server(config: CoreConfig) -> int:
    servers = CoreServerGroup(config)
    with PidGuard(config.pid_path):
        servers.run()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Cloud Storage Server Core")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--lan", action="store_true")
    parser.add_argument("--lan-port", type=int, default=None)
    parser.add_argument("--initialize-only", action="store_true")
    parser.add_argument("--smoke-test", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        base = CoreConfig.from_environment()
        config = CoreConfig(
            data_directory=base.data_directory,
            host=args.host or base.host,
            port=args.port or base.port,
            lan_enabled=args.lan or base.lan_enabled,
            lan_host=base.lan_host,
            lan_port=args.lan_port or base.lan_port,
            discovery_port=base.discovery_port,
            server_name=base.server_name,
            max_upload_bytes=base.max_upload_bytes,
            pairing_ttl_seconds=base.pairing_ttl_seconds,
        )
        if config.host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("the administrative API accepts loopback binding only")
        if args.initialize_only or args.smoke_test:
            application = create_app(config)
            runtime = application.state.runtime
            with runtime.database.connection() as connection:
                connection.execute("SELECT 1").fetchone()
            return 0
        return run_server(config)
    except (AlreadyRunningError, RuntimeError, ValueError) as exc:
        print(f"Cloud Storage Core: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
