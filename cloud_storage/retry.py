"""Bounded, jittered retry delays; Retry-After is a floor, not a sleep loop."""

from __future__ import annotations

import math
import random
import time
from email.utils import parsedate_to_datetime


def retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = (
            float(value)
            if value.strip().isdigit()
            else parsedate_to_datetime(value).timestamp() - time.time()
        )
        return max(0.0, min(3600.0, seconds)) if math.isfinite(seconds) else None
    except (ValueError, TypeError, OverflowError):
        return None


def retry_delay(attempt: int, retry_after: float | None = None) -> float:
    base = min(60.0, 2.0 ** min(max(1, attempt), 6))
    hint = (
        retry_after if isinstance(retry_after, (int, float)) and math.isfinite(retry_after) else 0
    )
    return max(base + random.uniform(0, base * 0.2), min(3600, max(0, hint)))
