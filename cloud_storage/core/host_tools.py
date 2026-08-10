from __future__ import annotations

import base64
import binascii
import os
import platform
import re
import shutil
import socket
import subprocess
import threading
import urllib.request
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DOCKER_DESKTOP_URL = "https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe"
SSH_CAPABILITY = "OpenSSH.Server~~~~0.0.1.0"
SSH_FIREWALL_RULE = "CloudStorage-OpenSSH-Private"
_SSH_KEY_TYPES = {
    "ssh-ed25519",
    "ssh-rsa",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com",
}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(slots=True)
class HostToolsService:
    data_directory: Path
    platform_name: str = field(default_factory=platform.system)
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
    _jobs: dict[str, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def overview(self) -> dict[str, Any]:
        return {
            "platform": self.platform_name,
            "docker": self.docker_status(),
            "ssh": self.ssh_status(),
        }

    def docker_status(self) -> dict[str, Any]:
        executable = self._docker_executable()
        version = ""
        engine_ready = False
        if executable:
            version_result = self._run([executable, "--version"], timeout=2)
            if version_result.returncode == 0:
                version = (
                    version_result.stdout.strip().removeprefix("Docker version ").split(",")[0]
                )
            info_result = self._run(
                [executable, "info", "--format", "{{.ServerVersion}}"], timeout=2
            )
            engine_ready = info_result.returncode == 0
        machine = platform.machine().lower()
        return {
            "supported": self.platform_name == "Windows" and machine in {"amd64", "x86_64"},
            "installed": executable is not None,
            "engine_ready": engine_ready,
            "executable": executable or "",
            "version": version,
            "job": self._job("docker_install"),
            "official_download": DOCKER_DESKTOP_URL,
        }

    def install_docker(self, *, confirmed: bool) -> dict[str, Any]:
        if not confirmed:
            raise ValueError("Docker Desktop installation requires explicit confirmation")
        if self.platform_name != "Windows":
            raise ValueError("automatic Docker Desktop installation is available only on Windows")
        if platform.machine().lower() not in {"amd64", "x86_64"}:
            raise ValueError("automatic Docker Desktop installation currently requires Windows x64")
        if self._docker_executable():
            return {"status": "already_installed", "docker": self.docker_status()}
        return self._start_job("docker_install", self._install_docker_worker)

    def ssh_status(self) -> dict[str, Any]:
        if self.platform_name != "Windows":
            return {
                "supported": False,
                "installed": shutil.which("sshd") is not None,
                "running": False,
                "startup": "unknown",
                "port": 22,
                "key_only": False,
                "username": "",
                "public_key_configured": False,
                "addresses": [],
                "job": self._job("ssh_configure"),
            }
        sshd = self._sshd_executable()
        service = self._run(["sc.exe", "query", "sshd"], timeout=5)
        service_config = self._run(["sc.exe", "qc", "sshd"], timeout=5)
        config_path = self._ssh_config_path()
        config = (
            config_path.read_text(encoding="utf-8", errors="replace")
            if config_path.is_file()
            else ""
        )
        managed = self._managed_ssh_values(config)
        key_path = self._administrators_key_path()
        return {
            "supported": True,
            "installed": sshd is not None,
            "running": service.returncode == 0 and "RUNNING" in service.stdout.upper(),
            "startup": (
                "automatic"
                if service_config.returncode == 0 and "AUTO_START" in service_config.stdout.upper()
                else "manual_or_disabled"
            ),
            "port": int(managed.get("port", 22)),
            "key_only": managed.get("passwordauthentication", "").lower() == "no",
            "username": managed.get("allowusers", ""),
            "public_key_configured": key_path.is_file() and key_path.stat().st_size > 0,
            "addresses": self._ssh_addresses(int(managed.get("port", 22))),
            "job": self._job("ssh_configure"),
        }

    def enable_ssh(
        self,
        *,
        username: str,
        public_key: str,
        port: int = 22,
        confirmed: bool,
    ) -> dict[str, Any]:
        if not confirmed:
            raise ValueError("SSH activation requires explicit confirmation")
        if self.platform_name != "Windows":
            raise ValueError("automatic OpenSSH Server setup is available only on Windows")
        username = username.strip().lower()
        if not re.fullmatch(r"[a-z0-9_.-]{1,64}", username):
            raise ValueError("use a local Windows administrator account name")
        if not 1 <= port <= 65535:
            raise ValueError("SSH port must be between 1 and 65535")
        public_key = self._validate_public_key(public_key)
        return self._start_job(
            "ssh_configure",
            lambda: self._enable_ssh_worker(username, public_key, port),
        )

    def disable_ssh(self, *, confirmed: bool) -> dict[str, Any]:
        if not confirmed:
            raise ValueError("SSH deactivation requires explicit confirmation")
        if self.platform_name != "Windows":
            raise ValueError("automatic OpenSSH Server setup is available only on Windows")
        return self._start_job("ssh_configure", self._disable_ssh_worker)

    def _start_job(self, job_id: str, worker: Callable[[], None]) -> dict[str, Any]:
        with self._lock:
            current = self._jobs.get(job_id, {})
            if current.get("status") in {"queued", "running"}:
                raise ValueError("this operation is already running")
            self._jobs[job_id] = {
                "status": "queued",
                "message": "Операция поставлена в очередь",
                "started_at": _now(),
                "finished_at": "",
            }

        def run() -> None:
            self._set_job(job_id, status="running", message="Операция выполняется")
            try:
                worker()
            except Exception as exc:
                self._set_job(
                    job_id,
                    status="failed",
                    message=str(exc)[:1000],
                    finished_at=_now(),
                )
            else:
                self._set_job(
                    job_id,
                    status="completed",
                    message="Операция завершена",
                    finished_at=_now(),
                )

        threading.Thread(target=run, name=f"cloud-storage-{job_id}", daemon=True).start()
        return {"status": "queued", "job": self._job(job_id)}

    def _set_job(self, job_id: str, **values: Any) -> None:
        with self._lock:
            self._jobs.setdefault(job_id, {}).update(values)

    def _job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            return deepcopy(
                self._jobs.get(
                    job_id,
                    {"status": "idle", "message": "", "started_at": "", "finished_at": ""},
                )
            )

    def _install_docker_worker(self) -> None:
        downloads = self.data_directory / "downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        installer = downloads / "DockerDesktopInstaller.exe"
        partial = installer.with_suffix(".download")
        request = urllib.request.Request(
            DOCKER_DESKTOP_URL,
            headers={"User-Agent": "CloudStorage-Server-Manager/0.9.6"},
        )
        try:
            with (
                urllib.request.urlopen(request, timeout=90) as response,
                partial.open("wb") as target,
            ):
                length = int(response.headers.get("Content-Length", "0") or 0)
                if length > 1_500_000_000:
                    raise RuntimeError("Docker installer is unexpectedly large")
                copied = 0
                while chunk := response.read(1024 * 1024):
                    copied += len(chunk)
                    if copied > 1_500_000_000:
                        raise RuntimeError("Docker installer exceeded the safe download limit")
                    target.write(chunk)
            os.replace(partial, installer)
            signature = self._run(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "$s=Get-AuthenticodeSignature -LiteralPath $args[0]; "
                    "if ($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notmatch "
                    "'Docker Inc') { Write-Error ('Invalid signer: '+$s.Status+' '+"
                    "$s.SignerCertificate.Subject); exit 9 }",
                    str(installer),
                ],
                timeout=120,
            )
            if signature.returncode != 0:
                raise RuntimeError(
                    self._command_error("Docker installer signature check failed", signature)
                )
            completed = self._run(
                [
                    str(installer),
                    "install",
                    "--quiet",
                    "--accept-license",
                    "--always-run-service",
                    "--no-windows-containers",
                ],
                timeout=3600,
            )
            if completed.returncode != 0:
                detail = (completed.stdout + completed.stderr).strip()[-1000:]
                raise RuntimeError(
                    f"Docker Desktop installer returned {completed.returncode}: {detail}"
                )
        finally:
            partial.unlink(missing_ok=True)
            installer.unlink(missing_ok=True)

    def _enable_ssh_worker(self, username: str, public_key: str, port: int) -> None:
        account = self._run(["net.exe", "user", username], timeout=10)
        if account.returncode != 0:
            raise RuntimeError("the selected local Windows account does not exist")
        admin_check = self._powershell(
            "$u='" + username + "'; "
            "$m=Get-LocalGroupMember -SID 'S-1-5-32-544' -ErrorAction Stop | "
            "Where-Object { $_.Name.ToLower().EndsWith('\\'+$u) }; "
            "if (-not $m) { exit 7 }"
        )
        if admin_check.returncode != 0:
            raise RuntimeError("the SSH account must be a local Windows administrator")
        install = self._powershell(
            f"$c=Get-WindowsCapability -Online -Name '{SSH_CAPABILITY}'; "
            f"if ($c.State -ne 'Installed') {{ Add-WindowsCapability -Online -Name '{SSH_CAPABILITY}' -ErrorAction Stop | Out-Null }}; "
            "Set-Service -Name sshd -StartupType Automatic; Start-Service sshd",
            timeout=1200,
        )
        if install.returncode != 0:
            raise RuntimeError(self._command_error("OpenSSH Server installation failed", install))

        config_path = self._ssh_config_path()
        if not config_path.is_file():
            raise RuntimeError("OpenSSH did not create sshd_config")
        original = config_path.read_text(encoding="utf-8", errors="replace")
        backup = config_path.with_name("sshd_config.cloud-storage-backup")
        if not backup.exists():
            shutil.copy2(config_path, backup)
        configured = self._configure_sshd(original, username=username, port=port)
        config_path.write_text(configured, encoding="utf-8", newline="\n")

        key_path = self._administrators_key_path()
        key_path.parent.mkdir(parents=True, exist_ok=True)
        existing = (
            key_path.read_text(encoding="utf-8", errors="replace") if key_path.exists() else ""
        )
        existing_keys = {line.strip() for line in existing.splitlines() if line.strip()}
        if public_key not in existing_keys:
            with key_path.open("a", encoding="utf-8", newline="\n") as target:
                if existing and not existing.endswith(("\n", "\r")):
                    target.write("\n")
                target.write(public_key + "\n")
        acl = self._run(
            [
                "icacls.exe",
                str(key_path),
                "/inheritance:r",
                "/grant",
                "*S-1-5-32-544:F",
                "/grant",
                "*S-1-5-18:F",
            ],
            timeout=30,
        )
        if acl.returncode != 0:
            config_path.write_text(original, encoding="utf-8", newline="\n")
            raise RuntimeError(self._command_error("SSH key permission setup failed", acl))

        sshd = self._sshd_executable()
        validate = self._run([sshd or "sshd.exe", "-t", "-f", str(config_path)], timeout=30)
        if validate.returncode != 0:
            config_path.write_text(original, encoding="utf-8", newline="\n")
            raise RuntimeError(self._command_error("sshd_config validation failed", validate))
        firewall = self._powershell(
            f"Remove-NetFirewallRule -Name '{SSH_FIREWALL_RULE}' -ErrorAction SilentlyContinue; "
            f"New-NetFirewallRule -Name '{SSH_FIREWALL_RULE}' "
            "-DisplayName 'Cloud Storage OpenSSH (Private)' -Enabled True -Direction Inbound "
            f"-Protocol TCP -Action Allow -LocalPort {port} -Profile Private | Out-Null; "
            "Restart-Service sshd"
        )
        if firewall.returncode != 0:
            config_path.write_text(original, encoding="utf-8", newline="\n")
            self._powershell(
                f"Remove-NetFirewallRule -Name '{SSH_FIREWALL_RULE}' -ErrorAction SilentlyContinue; "
                "Restart-Service sshd -ErrorAction SilentlyContinue"
            )
            raise RuntimeError(self._command_error("SSH firewall/service setup failed", firewall))

    def _disable_ssh_worker(self) -> None:
        result = self._powershell(
            "Stop-Service sshd -Force -ErrorAction SilentlyContinue; "
            "Set-Service sshd -StartupType Disabled -ErrorAction SilentlyContinue; "
            f"Remove-NetFirewallRule -Name '{SSH_FIREWALL_RULE}' -ErrorAction SilentlyContinue"
        )
        if result.returncode != 0:
            raise RuntimeError(self._command_error("SSH deactivation failed", result))

    def _docker_executable(self) -> str | None:
        candidates = [
            shutil.which("docker"),
            str(
                Path(os.environ.get("ProgramFiles", "C:/Program Files"))
                / "Docker/Docker/resources/bin/docker.exe"
            ),
            str(
                Path(os.environ.get("LOCALAPPDATA", ""))
                / "Programs/DockerDesktop/resources/bin/docker.exe"
            ),
        ]
        return next((item for item in candidates if item and Path(item).is_file()), None)

    @staticmethod
    def _sshd_executable() -> str | None:
        candidates = [
            shutil.which("sshd"),
            str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/OpenSSH/sshd.exe"),
        ]
        return next((item for item in candidates if item and Path(item).is_file()), None)

    @staticmethod
    def _ssh_root() -> Path:
        return Path(os.environ.get("ProgramData", "C:/ProgramData")) / "ssh"

    def _ssh_config_path(self) -> Path:
        return self._ssh_root() / "sshd_config"

    def _administrators_key_path(self) -> Path:
        return self._ssh_root() / "administrators_authorized_keys"

    @staticmethod
    def _configure_sshd(text: str, *, username: str, port: int) -> str:
        text = re.sub(
            r"(?ims)^# BEGIN CLOUD STORAGE MANAGED SSH\s*$.*?^# END CLOUD STORAGE MANAGED SSH\s*$\r?\n?",
            "",
            text,
        )
        lines = text.splitlines()
        match_index = next(
            (index for index, line in enumerate(lines) if re.match(r"^\s*Match\s+", line, re.I)),
            len(lines),
        )
        directives = {"port", "pubkeyauthentication", "passwordauthentication", "allowusers"}
        for index in range(match_index):
            match = re.match(r"^(\s*)([A-Za-z]+)\s+", lines[index])
            if match and match.group(2).lower() in directives:
                lines[index] = f"{match.group(1)}# Cloud Storage replaced: {lines[index].strip()}"
        block = [
            "# BEGIN CLOUD STORAGE MANAGED SSH",
            f"Port {port}",
            "PubkeyAuthentication yes",
            "PasswordAuthentication no",
            f"AllowUsers {username}",
            "# END CLOUD STORAGE MANAGED SSH",
            "",
        ]
        lines[match_index:match_index] = block
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _managed_ssh_values(text: str) -> dict[str, str]:
        match = re.search(
            r"(?ims)^# BEGIN CLOUD STORAGE MANAGED SSH\s*$\n(.*?)^# END CLOUD STORAGE MANAGED SSH\s*$",
            text,
        )
        values: dict[str, str] = {}
        if not match:
            return values
        for line in match.group(1).splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2:
                values[parts[0].lower()] = parts[1].strip()
        return values

    @staticmethod
    def _validate_public_key(value: str) -> str:
        line = " ".join(value.strip().split())
        parts = line.split(" ", 2)
        if len(parts) < 2 or parts[0] not in _SSH_KEY_TYPES:
            raise ValueError("paste a supported OpenSSH public key")
        try:
            decoded = base64.b64decode(parts[1], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("the SSH public key is not valid base64") from exc
        if len(decoded) < 32 or len(decoded) > 16_384:
            raise ValueError("the SSH public key has an invalid size")
        name_size = int.from_bytes(decoded[:4], "big")
        embedded_type = decoded[4 : 4 + name_size].decode("ascii", errors="ignore")
        if name_size > 128 or embedded_type != parts[0]:
            raise ValueError("the SSH public key type does not match its payload")
        return line

    @staticmethod
    def _ssh_addresses(port: int) -> list[str]:
        try:
            addresses = socket.gethostbyname_ex(socket.gethostname())[2]
        except OSError:
            addresses = []
        return [
            f"{address}:{port}"
            for address in dict.fromkeys(addresses)
            if address and not address.startswith("127.")
        ]

    def _powershell(self, script: str, *, timeout: int = 300) -> subprocess.CompletedProcess[str]:
        return self._run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            timeout=timeout,
        )

    def _run(self, command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        try:
            return self.runner(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return subprocess.CompletedProcess(command, 1, "", str(exc))

    @staticmethod
    def _command_error(prefix: str, result: subprocess.CompletedProcess[str]) -> str:
        detail = (result.stdout + result.stderr).strip()[-1000:]
        return f"{prefix} ({result.returncode}): {detail}"
