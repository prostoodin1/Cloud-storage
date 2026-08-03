from __future__ import annotations

import json
import threading
from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AuditEvent:
    timestamp: str
    action: str
    detail: str
    severity: str = "info"


class AuditLog:
    def __init__(self, data_directory: Path) -> None:
        self.path = data_directory / "audit.jsonl"
        self._lock = threading.RLock()

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
