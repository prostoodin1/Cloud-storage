"""Bounded transfer I/O shared by old and new v1 clients."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from typing import TypeVar

from fastapi import HTTPException, Request

UPLOAD_BUFFER_BYTES = 4 * 1024**2
MAX_UPLOAD_CHUNK_BYTES = 32 * 1024**2
MAX_ACTIVE_UPLOADS = 4
UPLOAD_IDLE_SECONDS = 30.0
T = TypeVar("T")


async def transfer_body(request: Request) -> AsyncIterator[bytes]:
    """A stalled sender must not reserve admission capacity indefinitely."""
    iterator = request.stream().__aiter__()
    while True:
        try:
            async with asyncio.timeout(UPLOAD_IDLE_SECONDS):
                chunk = await anext(iterator)
        except StopAsyncIteration:
            return
        except TimeoutError as exc:
            raise HTTPException(status_code=408, detail="upload sender timed out; retry later") from exc
        yield chunk


class TransferAdmission:
    """Reject excess work without accumulating waiting tasks or request bodies."""

    def __init__(self, limit: int = MAX_ACTIVE_UPLOADS) -> None:
        self._slots = threading.BoundedSemaphore(limit)

    @contextmanager
    def slot(self) -> Iterator[None]:
        if not self._slots.acquire(blocking=False):
            raise HTTPException(
                status_code=503,
                detail="transfer capacity is busy; retry later",
                headers={"Retry-After": "2"},
            )
        try:
            yield
        finally:
            self._slots.release()


async def transfer_io(function: Callable[..., T], *args) -> T:
    """Finish a disk operation before propagating cancellation to its owner.

    Cancelling to_thread does not stop its worker. The caller must not close or
    delete the file while a write/commit still owns it, including on shutdown.
    """
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
        except BaseException:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
    if cancelled:
        raise asyncio.CancelledError
    return result


class TransferProgress:
    """Progress is telemetry; final durable metadata is committed separately."""

    def __init__(self, update: Callable[[int], None]) -> None:
        self._update = update
        self._bytes = 0
        self._time = time.monotonic()

    def update(self, transferred: int) -> None:
        now = time.monotonic()
        if transferred - self._bytes >= 16 * 1024**2 or now - self._time >= 0.5:
            self._update(transferred)
            self._bytes = transferred
            self._time = now
