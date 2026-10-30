"""Bounded, deduplicated finalization that survives a disconnected HTTP poll."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from cloud_storage.core.storage import FileRecord, ResumableUploadRecord, StorageService
from cloud_storage.core.transfer_io import TransferAdmission, transfer_io


@dataclass
class CompletionJob:
    upload: ResumableUploadRecord
    task: asyncio.Task[FileRecord]


class UploadCompletion:
    """Owned by one application event loop; durable state stays in storage.

    A restart can repeat verification of an active session. No background
    worker is started until session ownership is checked.
    Old clients keep their synchronous response contract.
    """

    def __init__(self, storage: StorageService, admission: TransferAdmission) -> None:
        self.storage = storage
        self.admission = admission
        self.jobs: dict[tuple[str, str], CompletionJob] = {}

    def pending(self, upload_id: str, user_id: str) -> ResumableUploadRecord | None:
        job = self.jobs.get((upload_id, user_id))
        return job.upload if job is not None and not job.task.done() else None

    async def complete(
        self, upload_id: str, user_id: str, *, respond_async: bool,
    ) -> FileRecord | None:
        key = (upload_id, user_id)
        job = self.jobs.get(key)
        if job is None:
            upload = await transfer_io(self.storage.get_resumable_upload, upload_id, user_id)
            # The metadata read yielded: another request may have started the
            # same job. Recheck before scheduling expensive work.
            job = self.jobs.get(key)
            if job is None:
                # Reserve before creating the task: no unbounded wait queue.
                reservation = self.admission.slot()
                reservation.__enter__()

                async def finalize() -> FileRecord:
                    try:
                        return await transfer_io(
                            self.storage.complete_resumable_upload, upload_id, user_id,
                        )
                    finally:
                        reservation.__exit__(None, None, None)

                try:
                    task = asyncio.create_task(finalize())
                except BaseException:
                    reservation.__exit__(None, None, None)
                    raise
                # Consume exceptions even if the initiating client disappears;
                # polling the task still receives its original exception.
                task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
                if len(self.jobs) >= 64:
                    for old_key, old_job in list(self.jobs.items()):
                        if old_job.task.done():
                            del self.jobs[old_key]
                job = self.jobs[key] = CompletionJob(upload, task)
        if respond_async:
            await asyncio.wait({job.task}, timeout=0.1)
            if not job.task.done():
                return None
        try:
            result = await asyncio.shield(job.task)
            self.jobs.pop(key, None)
            return result
        except Exception:
            # Allow a later request to retry a transient DB/storage error.
            self.jobs.pop(key, None)
            raise

    async def close(self) -> None:
        if self.jobs:
            await asyncio.gather(*(job.task for job in self.jobs.values()), return_exceptions=True)
        self.jobs.clear()
