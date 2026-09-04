from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Protocol
from urllib.parse import urlsplit

from cloud_storage.client.api_client import ClientApi, ClientApiError, ClientConnectionError
from cloud_storage.core.config import CoreConfig
from cloud_storage.core.zrok_installer import ZROK2_VERSION, download_zrok2, managed_zrok2_path

_PUBLIC_URL = re.compile(r"https?://[^\s\"'<>]+")
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ENDPOINT_MARKER = "access your zrok share at the following endpoints:"
_BARE_FRONTEND = re.compile(r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}(?::\d+)?")


class TunnelProvider(Protocol):
    provider_id: str
    display_name: str

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def restart(self) -> dict[str, Any]: ...

    def status(self) -> dict[str, Any]: ...

    def manifest(self) -> dict[str, Any]: ...


@dataclass(slots=True)
class ZrokTunnelService:
    provider_id: ClassVar[str] = "zrok"
    display_name: ClassVar[str] = "zrok"
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
    _installing: bool = field(default=False, init=False)
    _enabling: bool = field(default=False, init=False)
    _candidate_url: str = field(default="", init=False)
    _expect_endpoint: bool = field(default=False, init=False)
    health_probe_id: str = field(default_factory=lambda: secrets.token_hex(32), init=False)

    def start(self) -> None:
        with self._lock:
            if not self.config.zrok_enabled or (self._thread and self._thread.is_alive()):
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
            self._public_url = self._candidate_url = ""
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
            thread.join(timeout=12)
        with self._lock:
            if thread is not None and thread.is_alive():
                raise RuntimeError("zrok2 ещё останавливается; повторите через несколько секунд")
            self._thread = self._process = None
            if self.config.zrok_enabled:
                self._state = "stopped"

    def restart(self) -> dict[str, Any]:
        if not self.config.zrok_enabled:
            raise RuntimeError("zrok provider is disabled in server settings")
        self.stop()
        self.start()
        return self.status()

    def manifest(self) -> dict[str, Any]:
        return {
            "id": self.provider_id,
            "name": self.display_name,
            "kind": "tunnel",
            "built_in": True,
            "loads_python_code": False,
            "capabilities": [
                "status",
                "restart",
                "public_https",
                "named_share",
            ],
        }

    def status(self) -> dict[str, Any]:
        executable = self._resolve_executable()
        cli_version = self._cli_version(executable or self.config.zrok_executable)
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
                "share_type": "named" if self.config.zrok_share_name else "ephemeral",
                "share_name": self.config.zrok_share_name,
                "share_namespace": "public" if self.config.zrok_share_name else "",
                "cli_version": cli_version,
                "login_required": True,
                "manager_api_exposed": False,
                "automatic_router_changes": False,
                "restart_count": self._restart_count,
                "last_error": self._last_error,
                "last_output": self._last_output,
                "installing": self._installing,
                "enabling": self._enabling,
                "verified": bool(self._public_url) and self._state == "online" and running,
                "managed_version": ZROK2_VERSION,
            }

    def install(self) -> dict[str, Any]:
        with self._lock:
            if self._enabling:
                raise RuntimeError("Дождитесь завершения подключения аккаунта zrok2")
            if self._installing:
                return self.status()
            self._installing = True
            self._state = "installing"
            self._last_error = ""
        thread = threading.Thread(target=self._install_worker, name="zrok2-installer", daemon=True)
        thread.start()
        return self.status()

    def _install_worker(self) -> None:
        try:
            # Windows cannot replace a running executable.
            self.stop()
            path = download_zrok2(self.config.data_directory)
            with self._lock:
                self._resolved_executable = str(path)
                self._state = "installed"
            if self.config.zrok_enabled:
                self.start()
        except (OSError, RuntimeError, tarfile.TarError) as exc:
            self._set_state("install_error", str(exc))
        finally:
            with self._lock:
                self._installing = False

    def enable(self, token: str) -> dict[str, Any]:
        executable = self._resolve_executable()
        if executable is None:
            raise RuntimeError("install zrok2 before connecting the account")
        if not token.strip() or len(token) > 4096:
            raise ValueError("invalid zrok2 enable token")
        with self._lock:
            if self._enabling or self._installing:
                raise RuntimeError("Дождитесь завершения текущей операции zrok2")
            self._enabling = True
        arguments: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": 45,
        }
        if os.name == "nt":
            arguments["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            arguments["env"] = self._runtime_environment(force_managed=True)
            result = subprocess.run(
                [executable, "enable", "--headless", token.strip()], **arguments
            )
            if result.returncode:
                detail = _ANSI_ESCAPE.sub("", result.stdout or "zrok2 enable failed")
                raise RuntimeError(detail.replace(token.strip(), "[скрыто]")[-500:])
            if self.config.zrok_enabled:
                self.restart()
        except subprocess.TimeoutExpired:
            # TimeoutExpired includes the complete command (and the enable token).
            raise RuntimeError("zrok2 не ответил за 45 секунд. Проверьте доступ к сервису zrok2") from None
        except OSError:
            raise RuntimeError("Не удалось запустить zrok2 для подключения аккаунта") from None
        finally:
            with self._lock:
                self._enabling = False
        return self.status()

    def _monitor(self) -> None:
        try:
            self._run_monitor()
        finally:
            process = self._process
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            with self._lock:
                self._process = None
                self._public_url = self._candidate_url = ""
                if self._thread is threading.current_thread():
                    self._thread = None

    def _run_monitor(self) -> None:
        executable = self._resolve_executable()
        if executable is None:
            self._set_state(
                "not_installed",
                "Нажмите «Установить zrok2 автоматически», затем «Подключить аккаунт zrok2»",
            )
            return
        delay = 1.0
        while not self._stop_event.is_set():
            command = self._share_command(executable)
            arguments: dict[str, Any] = {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "bufsize": 1,
                "close_fds": True,
                "env": self._runtime_environment(),
            }
            if os.name == "nt":
                arguments["creationflags"] = subprocess.CREATE_NO_WINDOW
            try:
                with self._lock:
                    if self._stop_event.is_set():
                        return
                    process = subprocess.Popen(command, **arguments)
                    self._process = process
                    self._candidate_url = self._public_url = ""
                    self._expect_endpoint = False
                    self._state = "starting"
                    self._last_error = self._last_output = ""
            except OSError as exc:
                self._set_state("error", f"could not start zrok: {exc}")
                return
            reader = threading.Thread(
                target=self._read_output, args=(process,), name="zrok2-output", daemon=True
            )
            reader.start()
            next_probe = 0.0
            failures = 0
            while process.poll() is None and not self._stop_event.wait(0.2):
                if self._candidate_url and time.monotonic() >= next_probe:
                    if self._check_public_endpoint():
                        failures = 0
                        delay = 1.0
                        next_probe = time.monotonic() + 30
                    else:
                        failures += 1
                        next_probe = time.monotonic() + 5
                        if failures >= 3:
                            break
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            return_code = process.wait()
            reader.join(timeout=2)
            with self._lock:
                self._process = None
                self._candidate_url = self._public_url = ""
            if self._state == "account_required":
                return
            if self._stop_event.is_set():
                return
            self._restart_count += 1
            self._set_state(
                "error", self._last_error or f"zrok2 завершился (код {return_code}); повтор запуска"
            )
            if self._stop_event.wait(delay):
                return
            delay = min(delay * 2, 30.0)

    def _read_output(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        try:
            for raw_line in process.stdout:
                if raw_line.strip():
                    self._handle_output(raw_line.strip())
        finally:
            process.stdout.close()

    def _handle_output(self, line: str) -> None:
        line = _ANSI_ESCAPE.sub("", line)
        try:
            record = json.loads(line)
            message = str(record.get("msg", record.get("message", ""))) if isinstance(record, dict) else line
        except ValueError:
            message = line
        with self._lock:
            self._last_output = line[-500:]
            if "unable to load environment" in message or "not enabled" in message:
                self._set_state("account_required", "Подключите аккаунт кнопкой «Подключить аккаунт zrok2»")
                return
            if _ENDPOINT_MARKER in message:
                self._expect_endpoint = True
                message = message.split(_ENDPOINT_MARKER, 1)[1].strip()
                if not message:
                    return
            elif not self._expect_endpoint:
                return
            self._expect_endpoint = False
            endpoints = [match.group(0) for match in _PUBLIC_URL.finditer(message)]
            # Hosted zrok2 returns bare frontend hostnames (e.g. *.shares.zrok.io),
            # not always URLs. Accept these only in the explicit endpoints message,
            # force HTTPS, and still require the public Core probe before advertising.
            endpoints.extend(
                "https://" + item.strip()
                for item in message.splitlines()
                if _BARE_FRONTEND.fullmatch(item.strip())
            )
            for endpoint in endpoints:
                public_url = endpoint.rstrip("/.,;)")
                try:
                    parsed = urlsplit(public_url)
                    _ = parsed.port  # Validate malformed or out-of-range ports.
                except ValueError:
                    continue
                if parsed.scheme == "http" and (parsed.hostname or "").endswith((".share.zrok.io", ".shares.zrok.io")):
                    public_url = "https://" + public_url.removeprefix("http://")
                    parsed = urlsplit(public_url)
                if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                    continue
                if parsed.path or parsed.query or parsed.fragment:
                    continue
                self._candidate_url = public_url
                self._set_state("checking")
                return
            self._set_state("error", "zrok2 не предоставил публичный HTTPS-адрес")

    def _check_public_endpoint(self) -> bool:
        with self._lock:
            candidate = self._candidate_url
        if not candidate:
            return False
        try:
            health = ClientApi(candidate).health(timeout=5.0)
            zrok = health.get("zrok") if isinstance(health, dict) else None
            if not isinstance(zrok, dict) or zrok.get("probe_id") != self.health_probe_id:
                raise ValueError("по публичному адресу ответил не этот Core")
        except (ClientConnectionError, ClientApiError, ValueError) as exc:
            with self._lock:
                if self._candidate_url == candidate and not self._stop_event.is_set():
                    self._set_state("unreachable", f"Проверка интернет-входа не пройдена: {exc}")
            return False
        with self._lock:
            if self._candidate_url != candidate or self._stop_event.is_set():
                return False
            self._public_url = candidate
            self._state = "online"
            self._last_error = ""
        return True

    def _resolve_executable(self) -> str | None:
        configured = self.config.zrok_executable.strip()
        path = Path(configured).expanduser()
        resolved: str | None
        if path.is_absolute() or path.parent != Path("."):
            resolved = str(path.resolve()) if path.is_file() else None
        else:
            candidates = [
                managed_zrok2_path(self.config.data_directory),
                Path(sys.executable).resolve().parent / ("zrok2.exe" if os.name == "nt" else "zrok2"),
            ]
            if os.name == "nt":
                for variable, suffix in (
                    ("LOCALAPPDATA", "Microsoft/WinGet/Links/zrok2.exe"),
                    ("USERPROFILE", "scoop/shims/zrok2.exe"),
                    ("ChocolateyInstall", "bin/zrok2.exe"),
                ):
                    root = os.environ.get(variable)
                    if root:
                        candidates.append(Path(root) / suffix)
            match = next((item.resolve() for item in candidates if item.is_file()), None)
            resolved = str(match) if match else shutil.which(configured)
        with self._lock:
            self._resolved_executable = resolved or ""
        return resolved

    def _share_command(self, executable: str) -> list[str]:
        host = f"[{self.config.zrok_host}]" if ":" in self.config.zrok_host else self.config.zrok_host
        backend = f"{host}:{self.config.zrok_port}"
        if self._cli_version(executable) >= 2:
            command = [executable, "share", "public", "--headless", "--force-local", backend]
            if self.config.zrok_share_name:
                command.extend(["-n", f"public:{self.config.zrok_share_name}"])
            return command
        if self.config.zrok_share_name:
            return [
                executable,
                "share",
                "reserved",
                "--headless",
                self.config.zrok_share_name,
            ]
        return [executable, "share", "public", "--headless", backend]

    @staticmethod
    def _cli_version(executable: str) -> int:
        name = re.split(r"[\\/]", executable.strip())[-1].casefold()
        return 2 if name in {"zrok2", "zrok2.exe"} else 1

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
            or key.casefold().startswith(("zrok2_", "zrok_", "pfxlog_"))
        }

    def _runtime_environment(self, *, force_managed: bool = False) -> dict[str, str]:
        environment = self._subprocess_environment()
        environment["DL_USE_JSON"] = "true"
        environment["DL_USE_COLOR"] = "false"
        profile = self.config.data_directory / "zrok2-profile"
        profile_name = ".zrok2" if self._cli_version(
            self._resolved_executable or self.config.zrok_executable
        ) >= 2 else ".zrok"
        managed_identity = profile / profile_name / "environment.json"
        using_managed_binary = self._resolved_executable == str(
            managed_zrok2_path(self.config.data_directory)
        )
        if force_managed or using_managed_binary or managed_identity.is_file():
            profile.mkdir(parents=True, exist_ok=True)
            environment["HOME"] = str(profile)
            if os.name == "nt":
                environment["USERPROFILE"] = str(profile)
        return environment

    def _set_state(self, state: str, error: str = "") -> None:
        with self._lock:
            self._state = state
            self._last_error = error
            if state != "online":
                self._public_url = ""


@dataclass(slots=True)
class TunnelProviderRegistry:
    providers: dict[str, TunnelProvider]

    @classmethod
    def built_in(cls, config: CoreConfig) -> TunnelProviderRegistry:
        zrok = ZrokTunnelService(config)
        return cls(providers={zrok.provider_id: zrok})

    def start_all(self) -> None:
        for provider in self.providers.values():
            provider.start()

    def stop_all(self) -> None:
        for provider in self.providers.values():
            provider.stop()

    def restart(self, provider_id: str) -> dict[str, Any]:
        provider = self.providers.get(provider_id)
        if provider is None:
            raise KeyError(provider_id)
        return provider.restart()

    def install(self, provider_id: str) -> dict[str, Any]:
        provider = self.providers.get(provider_id)
        if provider is None:
            raise KeyError(provider_id)
        installer = getattr(provider, "install", None)
        if installer is None:
            raise RuntimeError("provider does not support automatic installation")
        return installer()

    def enable(self, provider_id: str, token: str) -> dict[str, Any]:
        provider = self.providers.get(provider_id)
        if provider is None:
            raise KeyError(provider_id)
        enabler = getattr(provider, "enable", None)
        if enabler is None:
            raise RuntimeError("provider does not support account connection")
        return enabler(token)

    def status(self, provider_id: str) -> dict[str, Any]:
        provider = self.providers.get(provider_id)
        if provider is None:
            raise KeyError(provider_id)
        return provider.status()

    def overview(self) -> dict[str, Any]:
        statuses = {
            provider_id: provider.status()
            for provider_id, provider in self.providers.items()
        }
        return {
            "plugins": [provider.manifest() for provider in self.providers.values()],
            **statuses,
        }

    def support_snapshot(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for provider_id, provider in self.providers.items():
            state = provider.status()
            result[provider_id] = {
                "enabled": bool(state.get("enabled")),
                "state": str(state.get("state", "unknown")),
                "installed": bool(state.get("installed")),
                "process_running": bool(state.get("process_running")),
                "share_type": str(state.get("share_type", "unknown")),
                "login_required": bool(state.get("login_required")),
                "manager_api_exposed": bool(state.get("manager_api_exposed")),
                "restart_count": int(state.get("restart_count", 0)),
            }
        return result
