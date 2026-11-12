"""Stand-ins for the services outside this process: file storage and the processing queue."""

import asyncio
import json
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
        # Whether `embed_with_usage` reports token counts, as the real provider does.
        self.report_usage = True

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

    async def embed_with_usage(self, texts: list[str]):  # noqa: ANN201
        """Like the real embedder: vectors plus a provider-style token count (one token per
        word here), or None when `report_usage` is off."""
        from app.services.embeddings import Embedded

        vectors = await self.embed(texts)
        tokens = sum(len(text.split()) for text in texts) if self.report_usage else None
        return Embedded(vectors, tokens)


SOURCE_BLOCK = re.compile(
    r'<source-(?P<boundary>[0-9a-f]+) id="(?P<id>S\d+)"[^>]*>\n'
    r"(?P<content>.*?)\n</source-(?P=boundary)>",
    re.S,
)


def sources_in(turns: list) -> list[tuple[str, str]]:  # noqa: ANN001
    """The (id, text) of every passage in the last turn, as the model would see them."""
    return [(m.group("id"), m.group("content")) for m in SOURCE_BLOCK.finditer(turns[-1].content)]


def grounded_reply(call: dict) -> str:
    """Answers from the first passage, citing it with a quote taken from it."""
    sources = sources_in(call["turns"])
    source_id, content = sources[0]
    quote = " ".join(content.split()[:8])
    return json.dumps(
        {
            "found_answer": True,
            "answer": f"According to the documents, {quote} [{source_id}].",
            "citations": [{"source_id": source_id, "quote": quote}],
        }
    )


class FakeChatModel:
    """A stand-in for the language model: deterministic, recording every request."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail: Exception | None = None
        self.refuse = False
        self.delay = 0.0
        # Builds the raw output from the request. Defaults to citing the first passage.
        self.reply = grounded_reply
        # Streaming: output is sent in pieces of this many characters.
        self.chunk_size = 7
        # Raise `fail` after this many pieces have been sent (None: before any).
        self.fail_after: int | None = None
        # Seconds to wait before each piece.
        self.chunk_delay = 0.0
        # Set when a stream was closed, however it ended.
        self.streams_closed = 0

    async def generate(self, *, instructions, turns, schema, schema_name):  # noqa: ANN001, ANN201
        from app.services.llm import Generation

        call = {"instructions": instructions, "turns": list(turns), "schema": schema}
        self.calls.append(call)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        text = "" if self.refuse else self.reply(call)
        return Generation(
            text=text, refused=self.refuse, model="fake-model-1", input_tokens=120, output_tokens=30
        )

    async def stream(self, *, instructions, turns, schema, schema_name):  # noqa: ANN001, ANN201
        from app.services.llm import Generation, TextDelta

        call = {"instructions": instructions, "turns": list(turns), "schema": schema}
        self.calls.append(call)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.fail is not None and self.fail_after is None:
                raise self.fail
            text = "" if self.refuse else self.reply(call)
            for index in range(0, len(text), self.chunk_size):
                if self.fail is not None and index // self.chunk_size == self.fail_after:
                    raise self.fail
                if self.chunk_delay:
                    await asyncio.sleep(self.chunk_delay)
                yield TextDelta(text[index : index + self.chunk_size])
            yield Generation(
                text=text,
                refused=self.refuse,
                model="fake-model-1",
                input_tokens=120,
                output_tokens=30,
            )
        finally:
            self.streams_closed += 1
