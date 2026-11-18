"""Splits extracted text into token-sized chunks that remember where they came from.

Pure and blocking, like extraction: no database, no network, and no knowledge of embeddings.
It takes the segments that `extraction.py` produced and yields chunks lazily, so a large
document is never held in memory as one list of chunks.

Sizes are measured in tokens of the embedding model, not characters, and chunks are built
from whole sentences (whole lines for text files) so a sentence is only ever cut when it
alone is larger than a chunk.
"""

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Protocol

import tiktoken

from app.models.document import SegmentKind
from app.services.extraction import ExtractionError, Segment

DEFAULT_CHUNK_TOKENS = 800
DEFAULT_OVERLAP_TOKENS = 100
# A document that would produce more chunks than this is refused rather than half-indexed.
MAX_CHUNKS = 20_000

# The encoding used by text-embedding-3-small and -large.
EMBEDDING_ENCODING = "cl100k_base"

# End of a sentence (Latin or CJK punctuation) followed by whitespace, or a line break.
SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?。！？])\s+|\n+")


class ChunkingError(ExtractionError):
    """The document cannot be chunked. The message is shown to its uploader."""


class Tokenizer(Protocol):
    def encode(self, text: str) -> list[int]: ...

    def decode(self, tokens: list[int]) -> str: ...


class TiktokenTokenizer:
    def __init__(self, encoding_name: str = EMBEDDING_ENCODING) -> None:
        self._encoding = tiktoken.get_encoding(encoding_name)

    def encode(self, text: str) -> list[int]:
        # `encode_ordinary` treats text such as "<|endoftext|>" as plain characters.
        # Document text is untrusted and must never be read as control tokens.
        return self._encoding.encode_ordinary(text)

    def decode(self, tokens: list[int]) -> str:
        return self._encoding.decode(tokens)


@lru_cache
def get_tokenizer() -> Tokenizer:
    return TiktokenTokenizer()


@dataclass(frozen=True, slots=True)
class Chunk:
    index: int
    content: str
    token_count: int
    # Where the chunk's own (non-overlapping) content starts. Only what the format has.
    page_number: int | None = None
    paragraph_index: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Unit:
    """A sentence or line: the smallest piece chunks are built from."""

    text: str
    tokens: int
    segment: Segment
    segment_position: int
    line: int | None = None


def validate_sizes(chunk_tokens: int, overlap_tokens: int) -> None:
    if chunk_tokens < 20:
        raise ValueError("chunk size must be at least 20 tokens")
    if not 0 <= overlap_tokens <= chunk_tokens // 2:
        raise ValueError("overlap must be between 0 and half the chunk size")


def split_oversized(text: str, limit: int, tokenizer: Tokenizer) -> Iterator[tuple[str, int]]:
    """Break one over-long sentence or line into pieces of at most `limit` tokens.

    Splits between words where it can. Only a single "word" longer than a whole chunk
    (a long URL, base64, text with no spaces) is cut inside.
    """
    words: list[str] = []
    count = 0
    for word in text.split(" "):
        if not word:
            continue
        tokens = tokenizer.encode(word)
        if len(tokens) > limit:
            if words:
                yield " ".join(words), count
                words, count = [], 0
            for start in range(0, len(tokens), limit):
                piece = tokens[start : start + limit]
                yield tokenizer.decode(piece), len(piece)
            continue
        cost = len(tokens) + 1
        if words and count + cost > limit:
            yield " ".join(words), count
            words, count = [], 0
        words.append(word)
        count += cost
    if words:
        yield " ".join(words), count


def units_of(segments: Iterable[Segment], limit: int, tokenizer: Tokenizer) -> Iterator[Unit]:
    for position, segment in enumerate(segments):
        if segment.kind is SegmentKind.LINES:
            # A text segment is a run of consecutive non-blank lines starting at line_start,
            # so each line's own number is known exactly.
            first_line = segment.line_start or 1
            pieces = [
                (line, first_line + offset)
                for offset, line in enumerate(segment.content.split("\n"))
            ]
        else:
            pieces = [(sentence, None) for sentence in SENTENCE_BOUNDARY.split(segment.content)]

        for text, line in pieces:
            text = text.strip()
            if not text:
                continue
            tokens = len(tokenizer.encode(text))
            if tokens <= limit:
                yield Unit(text, tokens, segment, position, line)
            else:
                for part, part_tokens in split_oversized(text, limit, tokenizer):
                    yield Unit(part, part_tokens, segment, position, line)


def separator(previous: Unit, unit: Unit) -> str:
    if previous.segment_position == unit.segment_position:
        return "\n" if unit.segment.kind is SegmentKind.LINES else " "
    # A new page or a new block of lines is a stronger break than a new paragraph.
    return "\n" if unit.segment.kind is SegmentKind.PARAGRAPH else "\n\n"


def overlap_tail(units: list[Unit], budget: int, tokenizer: Tokenizer) -> list[Unit]:
    """The end of a chunk to repeat at the start of the next: whole units when they fit."""
    if budget <= 0:
        return []
    tail: list[Unit] = []
    used = 0
    for unit in reversed(units):
        cost = unit.tokens + 1
        if used + cost > budget:
            break
        tail.insert(0, unit)
        used += cost
    if tail:
        return tail

    # The last sentence alone is longer than the overlap: repeat its final words instead.
    last = units[-1]
    words: list[str] = []
    used = 0
    for word in reversed(last.text.split(" ")):
        cost = len(tokenizer.encode(word)) + 1
        if used + cost > budget:
            break
        words.insert(0, word)
        used += cost
    if not words:
        return []
    return [Unit(" ".join(words), used, last.segment, last.segment_position, last.line)]


def build_chunk(index: int, units: list[Unit], carried: int, tokenizer: Tokenizer) -> Chunk:
    parts = [units[0].text]
    for previous, unit in zip(units, units[1:], strict=False):
        parts.append(separator(previous, unit))
        parts.append(unit.text)
    content = "".join(parts)

    # References point at the chunk's own content, not the overlap repeated from the
    # previous chunk, so a citation leads to where this passage really begins.
    own = units[carried:] or units
    first, last = own[0].segment, units[-1].segment
    kind = first.kind
    metadata: dict[str, Any] = {"kind": kind.value}
    page_number = paragraph_index = line_start = line_end = None

    if kind is SegmentKind.PAGE:
        page_number = first.page_number
        metadata["page_end"] = last.page_number
    elif kind is SegmentKind.PARAGRAPH:
        # No page number: a Word file does not record one, and none is invented.
        paragraph_index = first.paragraph_index
        metadata["paragraph_end"] = last.paragraph_index
        if first.section:
            metadata["section"] = first.section
    else:
        lines = [unit.line for unit in units if unit.line is not None]
        line_start, line_end = min(lines), max(lines)

    overlap_tokens = sum(unit.tokens for unit in units[:carried])
    if overlap_tokens:
        metadata["overlap_tokens"] = overlap_tokens

    return Chunk(
        index=index,
        content=content,
        token_count=len(tokenizer.encode(content)),
        page_number=page_number,
        paragraph_index=paragraph_index,
        line_start=line_start,
        line_end=line_end,
        metadata=metadata,
    )


def chunk_segments(
    segments: Iterable[Segment],
    *,
    chunk_tokens: int = DEFAULT_CHUNK_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
    tokenizer: Tokenizer | None = None,
    max_chunks: int = MAX_CHUNKS,
) -> Iterator[Chunk]:
    """Yield chunks of at most `chunk_tokens` tokens, in reading order.

    Each chunk after the first begins with up to `overlap_tokens` tokens repeated from the
    end of the one before, so a passage that straddles a boundary is still found whole.
    An input with no text yields nothing.
    """
    validate_sizes(chunk_tokens, overlap_tokens)
    tokenizer = tokenizer or get_tokenizer()
    # Each unit is budgeted one extra token for the separator that joins it to the next.
    unit_limit = chunk_tokens - 1

    current: list[Unit] = []
    current_tokens = 0
    carried = 0  # how many leading units of `current` are overlap from the previous chunk
    index = 0

    for unit in units_of(segments, unit_limit, tokenizer):
        cost = unit.tokens + 1
        if current_tokens + cost > chunk_tokens:
            if len(current) > carried:
                if index >= max_chunks:
                    raise ChunkingError("This document is too long to index.")
                yield build_chunk(index, current, carried, tokenizer)
                index += 1
                current = overlap_tail(current, overlap_tokens, tokenizer)
                carried = len(current)
                current_tokens = sum(tail.tokens + 1 for tail in current)
            if current_tokens + cost > chunk_tokens:
                # The overlap and this unit do not fit together; the unit matters more.
                current, carried, current_tokens = [], 0, 0
        current.append(unit)
        current_tokens += cost

    if len(current) > carried:
        if index >= max_chunks:
            raise ChunkingError("This document is too long to index.")
        yield build_chunk(index, current, carried, tokenizer)
