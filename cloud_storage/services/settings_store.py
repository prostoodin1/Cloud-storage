from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path

from cloud_storage.models import AppSettings


def default_data_directory() -> Path:
    override = os.environ.get("CLOUD_STORAGE_CONFIG_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return root / "CloudStorage"
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "cloud-storage"


class SettingsStore:
    """Small atomic JSON store. A partial write never replaces the last valid config."""

    def __init__(self, data_directory: Path | None = None) -> None:
        self.data_directory = data_directory or default_data_directory()
        self.path = self.data_directory / "settings.json"
        self._lock = threading.RLock()

    def load(self) -> AppSettings:
        with self._lock:
            if not self.path.exists():
                return AppSettings()
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("settings root must be an object")
                return AppSettings.from_dict(raw)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
                quarantine = self.path.with_name(f"settings.corrupt-{stamp}.json")
                try:
                    self.path.replace(quarantine)
                except OSError:
                    pass
                return AppSettings()

    def save(self, settings: AppSettings) -> None:
        payload = json.dumps(settings.to_dict(), ensure_ascii=False, indent=2)
        with self._lock:
            self.data_directory.mkdir(parents=True, exist_ok=True)
            fd, temporary_name = tempfile.mkstemp(
                prefix="settings-", suffix=".tmp", dir=self.data_directory
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                if os.name != "nt":
                    temporary.chmod(0o600)
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
