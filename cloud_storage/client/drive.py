from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from cloud_storage.client.settings import ClientProfile, DeviceTokenVault


@dataclass(frozen=True, slots=True)
class DriveStatus:
    profile_id: str
    drive_letter: str
    state: str
    detail: str
    pid: int = 0


class DriveManager:
    def __init__(self, data_directory: Path) -> None:
        self.data_directory = data_directory
        self._processes: dict[
            str, tuple[tuple[str, str, str, str], subprocess.Popen[bytes]]
        ] = {}

    def reconcile(self, profiles: list[ClientProfile]) -> dict[str, DriveStatus]:
        wanted: dict[str, ClientProfile] = {}
        for profile in profiles:
            token = DeviceTokenVault(self.data_directory, profile.profile_id).load()
            if profile.drive_enabled and token and profile.device_status in {"trusted", "offline"}:
                wanted[profile.profile_id] = profile

        for profile_id in set(self._processes) - set(wanted):
            self.stop(profile_id)
        for profile_id, profile in wanted.items():
            running = self._processes.get(profile_id)
            desired_letter = profile.drive_letter.upper().rstrip(":")
            signature = (
                desired_letter,
                profile.server_url.rstrip("/"),
                profile.certificate_fingerprint.casefold(),
                profile.last_space_id,
            )
            if running and running[0] == signature and running[1].poll() is None:
                continue
            if running:
                self.stop(profile_id)
            self._start(profile, desired_letter, signature)
        return {profile.profile_id: self.status(profile) for profile in profiles}

    def status(self, profile: ClientProfile) -> DriveStatus:
        path = self._status_path(profile.profile_id)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError
            return DriveStatus(
                profile_id=profile.profile_id,
                drive_letter=str(value.get("drive_letter", profile.drive_letter)),
                state=str(value.get("state", "unknown")),
                detail=str(value.get("detail", ""))[:500],
                pid=int(value.get("pid", 0)),
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            process = self._processes.get(profile.profile_id)
            if process and process[1].poll() is None:
                return DriveStatus(
                    profile.profile_id,
                    profile.drive_letter,
                    "starting",
                    "Диск запускается…",
                    process[1].pid,
                )
            detail = (
                "Компонент диска не найден в исходной среде"
                if self._helper_path() is None
                else "Диск отключён"
            )
            return DriveStatus(profile.profile_id, profile.drive_letter, "stopped", detail)

    def stop(self, profile_id: str) -> None:
        running = self._processes.pop(profile_id, None)
        if running is not None:
            process = running[1]
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
        self._status_path(profile_id).unlink(missing_ok=True)

    def stop_all(self) -> None:
        for profile_id in list(self._processes):
            self.stop(profile_id)

    def _start(
        self,
        profile: ClientProfile,
        letter: str,
        signature: tuple[str, str, str, str],
    ) -> None:
        helper = self._helper_path()
        if helper is None:
            return
        self.data_directory.mkdir(parents=True, exist_ok=True)
        command = [
            str(helper),
            "--profile-id",
            profile.profile_id,
            "--mount",
            f"{letter}:",
            "--data-dir",
            str(self.data_directory),
            "--parent-pid",
            str(os.getpid()),
        ]
        arguments: dict[str, object] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
        }
        if os.name == "nt":
            arguments["creationflags"] = subprocess.CREATE_NO_WINDOW
        process = subprocess.Popen(command, **arguments)  # type: ignore[arg-type]
        self._processes[profile.profile_id] = (signature, process)

    def _status_path(self, profile_id: str) -> Path:
        return self.data_directory / "drives" / f"{profile_id}.json"

    @staticmethod
    def _helper_path() -> Path | None:
        name = "CloudStorageDrive.exe" if os.name == "nt" else "CloudStorageDrive"
        if getattr(sys, "frozen", False):
            executable = Path(sys.executable).resolve()
            candidates = [executable.parent / name, executable.parent.parent / name]
        else:
            candidates = [Path(__file__).resolve().parents[2] / "dist" / name]
        return next((path for path in candidates if path.is_file()), None)


def wait_for_drive(letter: str, timeout_seconds: float = 5.0) -> bool:
    root = Path(f"{letter.upper().rstrip(':')}:\\")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if root.exists():
            return True
        time.sleep(0.1)
    return False
