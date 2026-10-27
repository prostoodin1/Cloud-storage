"""Single byte ranges; multipart ranges are deliberately ignored (RFC 9110)."""

from __future__ import annotations

import re


class UnsatisfiableRange(ValueError):
    pass


def byte_range(
    value: str | None, if_range: str | None, etag: str, size: int
) -> tuple[int, int] | None:
    if not value or (if_range is not None and if_range != etag):
        return None
    if not value.startswith("bytes=") or "," in value:
        return None
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value)
    if not match or len(value) > 100 or size == 0 or not any(match.groups()):
        raise UnsatisfiableRange
    first, last = match.groups()
    if not first:
        suffix = int(last)
        if suffix == 0:
            raise UnsatisfiableRange
        return max(0, size - suffix), size - 1
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start >= size or end < start:
        raise UnsatisfiableRange
    return start, end
