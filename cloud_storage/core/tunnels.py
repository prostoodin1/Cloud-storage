from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cloud_storage.core.config import CoreConfig

_PUBLIC_URL = re.compile(r"https?://[a-zA-Z0-9.-]+(?::\d+)?")


@dataclass(slots=True)
class ZrokTunnelService:
    config: CoreConfig
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _stop_event: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _process: subprocess.Popen[str] | None = field(default=None, init=False, repr=False)
    _state: str = field(default="disabled", init=False)
    _public_url: str = field(default="", init=False)
    _last_error: str = field(default="", init=False)
    _last_output: str = field(default="", init=False)
    _restart_count: int = field(default=0, init=False)
    _resolved_executable: str = field(default="", init=False)

    def start(self) -> None:
        if not self.config.zrok_enabled or self._thread is not None:
            return
        self._stop_event.clear()
        self._set_state("starting")
        self._thread = threading.Thread(
            target=self._monitor,
            name="cloud-storage-zrok-tunnel",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            process = self._process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=7)
        with self._lock:
            self._thread = None
            self._process = None
            if self.config.zrok_enabled:
                self._state = "stopped"

    def status(self) -> dict[str, Any]:
        executable = self._resolve_executable()
        with self._lock:
            running = self._process is not None and self._process.poll() is None
            return {
                "enabled": self.config.zrok_enabled,
                "provider": "zrok",
                "state": self._state if self.config.zrok_enabled else "disabled",
                "installed": executable is not None,
                "executable": self._resolved_executable or self.config.zrok_executable,
                "process_running": running,
                "listener": f"http://{self.config.zrok_host}:{self.config.zrok_port}",
                "public_url": self._public_url,
                "share_type": "reserved" if self.config.zrok_share_name else "ephemeral",
                "share_name": self.config.zrok_share_name,
                "login_required": True,
                "manager_api_exposed": False,
                "automatic_router_changes": False,
                "restart_count": self._restart_count,
                "last_error": self._last_error,
                "last_output": self._last_output,
            }

    def _monitor(self) -> None:
        executable = self._resolve_executable()
        if executable is None:
            self._set_state(
                "not_installed",
                "zrok executable was not found; install zrok and enable its environment",
            )
            return
        delay = 1.0
        while not self._stop_event.is_set():
            command = [executable, "share"]
            if self.config.zrok_share_name:
                command.extend(["reserved", "--headless", self.config.zrok_share_name])
            else:
                command.extend(
                    [
                        "public",
                        "--headless",
                        f"{self.config.zrok_host}:{self.config.zrok_port}",
                    ]
                )
            arguments: dict[str, Any] = {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "bufsize": 1,
                "close_fds": True,
                "env": self._subprocess_environment(),
            }
            if os.name == "nt":
                arguments["creationflags"] = subprocess.CREATE_NO_WINDOW
            try:
                process = subprocess.Popen(command, **arguments)
            except OSError as exc:
                self._set_state("error", f"could not start zrok: {exc}")
                return
            with self._lock:
                self._process = process
                self._state = "starting"
                self._last_error = ""
            assert process.stdout is not None
            for raw_line in process.stdout:
                line = raw_line.strip()
                if line:
                    self._handle_output(line)
                if self._stop_event.is_set():
                    break
            return_code = process.wait()
            with self._lock:
                self._process = None
            if self._stop_event.is_set():
                return
            self._restart_count += 1
            self._set_state("error", f"zrok exited with code {return_code}; retrying")
            if self._stop_event.wait(delay):
                return
            delay = min(delay * 2, 30.0)

    def _handle_output(self, line: str) -> None:
        match = _PUBLIC_URL.search(line)
        with self._lock:
            self._last_output = line[-500:]
            if match:
                public_url = match.group(0).rstrip("/")
                if public_url.startswith("http://") and public_url.endswith(
                    ".share.zrok.io"
                ):
                    public_url = "https://" + public_url.removeprefix("http://")
                if public_url.startswith("https://"):
                    self._public_url = public_url
                    self._state = "online"
                else:
                    self._state = "error"
                    self._last_error = "zrok frontend did not provide an HTTPS address"
            elif self._state == "starting" and "sharing" in line.casefold():
                self._state = "online"

    def _resolve_executable(self) -> str | None:
        configured = self.config.zrok_executable.strip()
        path = Path(configured).expanduser()
        resolved: str | None
        if path.is_absolute() or path.parent != Path("."):
            resolved = str(path.resolve()) if path.is_file() else None
        else:
            resolved = shutil.which(configured)
        with self._lock:
            self._resolved_executable = resolved or ""
        return resolved

    @staticmethod
    def _subprocess_environment() -> dict[str, str]:
        allowed = {
            "appdata",
            "home",
            "homedrive",
            "homepath",
            "https_proxy",
            "http_proxy",
            "localappdata",
            "no_proxy",
            "path",
            "programdata",
            "ssl_cert_dir",
            "ssl_cert_file",
            "systemroot",
            "temp",
            "tmp",
            "userprofile",
        }
        return {
            key: value
            for key, value in os.environ.items()
            if key.casefold() in allowed
            or key.casefold().startswith(("zrok_", "pfxlog_"))
        }

    def _set_state(self, state: str, error: str = "") -> None:
        with self._lock:
            self._state = state
            self._last_error = error
