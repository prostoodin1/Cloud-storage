from __future__ import annotations

import json
import threading
from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AuditEvent:
    timestamp: str
    action: str
    detail: str
    severity: str = "info"


class AuditLog:
    def __init__(self, data_directory: Path, *, retention_days: int = 30) -> None:
        self.path = data_directory / "audit.jsonl"
        self._lock = threading.RLock()
        self.retention_days = max(1, retention_days)
        self.purge()

    def record(self, action: str, detail: str, severity: str = "info") -> AuditEvent:
        event = AuditEvent(
            timestamp=datetime.now(UTC).isoformat(timespec="seconds"),
            action=action,
            detail=detail,
            severity=severity,
        )
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(asdict(event), ensure_ascii=False) + "\n")
        return event

    def purge(self, *, now: datetime | None = None) -> int:
        """Remove complete, valid events older than the retention window."""
        if not self.path.exists():
            return 0
        threshold = (now or datetime.now(UTC)) - timedelta(days=self.retention_days)
        with self._lock:
            try:
                lines = self.path.read_text(encoding="utf-8").splitlines()
            except OSError:
                return 0
            kept: list[str] = []
            removed = 0
            for line in lines:
                try:
                    value = json.loads(line)
                    timestamp = datetime.fromisoformat(str(value["timestamp"]))
                    if timestamp.tzinfo is None:
                        timestamp = timestamp.replace(tzinfo=UTC)
                    if timestamp < threshold:
                        removed += 1
                        continue
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    # Preserve malformed lines for diagnostics instead of silently losing evidence.
                    pass
                kept.append(line)
            if removed:
                self.path.write_text(
                    "".join(f"{line}\n" for line in kept), encoding="utf-8"
                )
            return removed

    def recent(self, limit: int = 20) -> list[AuditEvent]:
        if not self.path.exists():
            return []
        with self._lock:
            try:
                with self.path.open("r", encoding="utf-8") as handle:
                    lines = deque(handle, maxlen=max(1, limit))
            except OSError:
                return []
        result: list[AuditEvent] = []
        for line in lines:
            try:
                value = json.loads(line)
                result.append(AuditEvent(**value))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return list(reversed(result))
