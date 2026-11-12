"""Stand-ins for the services outside this process: file storage and the processing queue."""

import math
import re
import uuid
import zlib
from typing import BinaryIO

from app.models.chunk import EMBEDDING_DIMENSIONS
from app.services.embeddings import EmbeddingError
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


def fake_vector(text: str) -> list[float]:
    """A deterministic 1536-dimension stand-in for a real embedding.

    Each word adds weight to one dimension, so texts that share words point in similar
    directions and texts that share none are orthogonal. That is enough for pgvector's
    real cosine search to rank related passages first, without calling OpenAI.
    """
    vector = [0.0] * EMBEDDING_DIMENSIONS
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        vector[zlib.crc32(word.encode()) % EMBEDDING_DIMENSIONS] += 1.0
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        vector[0] = 1.0
        return vector
    return [value / norm for value in vector]


class FakeEmbedder:
    """Records what it was asked to embed, and can be told to fail."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.fail: EmbeddingError | None = None
        # Fail only once this many calls have succeeded (None: fail from the first call).
        self.fail_after: int | None = None

    @property
    def texts(self) -> list[str]:
        return [text for call in self.calls for text in call]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if self.fail is not None and (
            self.fail_after is None or len(self.calls) >= self.fail_after
        ):
            raise self.fail
        self.calls.append(list(texts))
        return [fake_vector(text) for text in texts]
