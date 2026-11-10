"""Stand-ins for the services outside this process: file storage and the processing queue."""

import uuid
from typing import BinaryIO

from app.services.storage import StorageError


class FakeStorage:
    """An in-memory private bucket that records what was asked of it and can be told to fail."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        self.signed: list[tuple[str, int, str]] = []
        self.downloads: list[str] = []
        self.deleted: list[str] = []
        self.fail_upload = False
        self.fail_download = False
        self.fail_delete = False
        self.fail_sign = False

    async def upload(self, path: str, file: BinaryIO, content_type: str, size: int) -> None:
        if self.fail_upload:
            raise StorageError()
        data = file.read()
        assert len(data) == size, "the declared size must match the bytes sent"
        assert path not in self.objects, "uploads must never overwrite an existing object"
        self.objects[path] = data
        self.content_types[path] = content_type

    async def download_to(self, path: str, destination: BinaryIO) -> None:
        self.downloads.append(path)
        if self.fail_download or path not in self.objects:
            raise StorageError()
        destination.write(self.objects[path])

    async def delete(self, paths: list[str]) -> None:
        if self.fail_delete:
            raise StorageError()
        for path in paths:
            self.deleted.append(path)
            self.objects.pop(path, None)

    async def create_signed_url(self, path: str, expires_in: int, download_name: str) -> str:
        if self.fail_sign:
            raise StorageError()
        self.signed.append((path, expires_in, download_name))
        return f"https://storage.test/object/sign/bucket/{path}?token=signed-for-{expires_in}s"


class RecordingQueue:
    """Collects what was queued instead of running it, so tests decide when processing happens."""

    def __init__(self) -> None:
        self.enqueued: list[uuid.UUID] = []

    def enqueue(self, document_id: uuid.UUID) -> None:
        self.enqueued.append(document_id)
