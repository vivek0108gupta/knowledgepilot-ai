"""Checks the model's answer against the evidence it was given, and builds the citations.

The model cites sources by the short ids it was shown (S1, S2...). Nothing it returns is
trusted: every id is looked up in that request's own context, and every citation is built
from the stored chunk, never from what the model wrote. An id that was not supplied is
dropped. An answer left with no valid citation is treated as unsupported and replaced.
"""

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.services.context_builder import Context, Source
from app.services.llm import Generation
from app.services.prompts import NO_ANSWER, leaks_instructions

logger = logging.getLogger(__name__)

MAX_ANSWER_LENGTH = 8000
MAX_EXCERPT_LENGTH = 300
FALLBACK_EXCERPT_LENGTH = 240

# [S1], [S1, S3], [S2; S4]. A single preceding space is captured so that a marker removed
# for being invalid does not leave a stray space before the punctuation that follows.
MARKER = re.compile(r"(\s?)\[\s*(S\d+(?:\s*[,;]\s*S\d+)*)\s*\]")
SOURCE_ID = re.compile(r"S\d+")


class InvalidAnswer(Exception):
    """The model's output was not a usable answer. The message is for logs, not users."""


class CitedQuote(BaseModel):
    # Strict: "yes" is not a boolean and 1 is not a string, whatever the model sends.
    model_config = ConfigDict(extra="forbid", strict=True)

    source_id: str
    quote: str


class ModelAnswer(BaseModel):
    # Strict: "yes" is not a boolean and 1 is not a string, whatever the model sends.
    model_config = ConfigDict(extra="forbid", strict=True)

    found_answer: bool
    answer: str = Field(max_length=MAX_ANSWER_LENGTH)
    citations: list[CitedQuote]


@dataclass(frozen=True, slots=True)
class FinalAnswer:
    content: str
    citations: list[dict[str, Any]]
    grounded: bool
    # Why an answer was replaced by the standard "not found" reply, if it was.
    reason: str | None = None
    dropped_citations: int = 0
    unverified_quotes: int = 0
    notes: dict[str, Any] = field(default_factory=dict)


def not_found(reason: str, dropped: int = 0) -> FinalAnswer:
    return FinalAnswer(NO_ANSWER, [], grounded=False, reason=reason, dropped_citations=dropped)


def normalise(text: str) -> str:
    return " ".join(text.split()).casefold()


def fallback_excerpt(content: str) -> str:
    text = " ".join(content.split())
    if len(text) <= FALLBACK_EXCERPT_LENGTH:
        return text
    cut = text[:FALLBACK_EXCERPT_LENGTH]
    if " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,;:.-") + "…"


# A quote shortened with an ellipsis is matched piece by piece, in order.
ELLIPSIS = re.compile(r"\s*(?:\.\.\.|…)\s*")
MIN_QUOTE_CHARACTERS = 12


def comparable(text: str) -> tuple[str, list[int]]:
    """Text reduced to lower-case letters and digits separated by single spaces, so quotes
    match despite differences in case, punctuation, dashes, quote marks or ligatures.
    Also returns, for each kept character, its position in the original text."""
    out: list[str] = []
    positions: list[int] = []
    gap = True
    for position, character in enumerate(text):
        for folded in unicodedata.normalize("NFKC", character).casefold():
            if folded.isalnum():
                out.append(folded)
                positions.append(position)
                gap = False
            elif not gap:
                out.append(" ")
                positions.append(position)
                gap = True
    while out and out[-1] == " ":
        out.pop()
        positions.pop()
    return "".join(out), positions


def locate(quote: str, passage: str) -> tuple[int, int] | None:
    """Where `quote` appears in `passage`, as a span of the original text, or None."""
    pieces = [comparable(piece)[0] for piece in ELLIPSIS.split(quote)]
    pieces = [piece for piece in pieces if piece]
    if not pieces or sum(len(piece) for piece in pieces) < MIN_QUOTE_CHARACTERS:
        return None
    haystack, positions = comparable(passage)
    cursor = 0
    first = last = -1
    for piece in pieces:
        found = haystack.find(piece, cursor)
        if found < 0:
            return None
        if first < 0:
            first = found
        last = found + len(piece) - 1
        cursor = found + len(piece)
    return positions[first], positions[last] + 1


def excerpt_for(source: Source, quotes: list[str]) -> tuple[str, bool]:
    """A short excerpt of the source, and whether it was located from the model's quote.

    The model's quote only says where to look: the excerpt is always the document's own text
    at that place. A quote that cannot be found in the passage is ignored, and the start of
    the passage is shown instead.
    """
    passage = source.match.content
    for quote in quotes:
        span = locate(quote, passage)
        if span is not None:
            text = " ".join(passage[span[0] : span[1]].split())
            if len(text) > MAX_EXCERPT_LENGTH:
                text = text[: MAX_EXCERPT_LENGTH - 1].rstrip() + "…"
            return text, True
    return fallback_excerpt(passage), not quotes


def citation(number: int, source: Source, excerpt: str) -> dict[str, Any]:
    match = source.match
    return {
        "number": number,
        "document_id": str(match.document_id),
        "document_name": match.document_name,
        "chunk_id": str(match.chunk_id),
        "page_number": match.page_number,
        "paragraph_index": match.paragraph_index,
        "line_start": match.line_start,
        "line_end": match.line_end,
        "excerpt": excerpt,
    }


def parse(generation: Generation) -> ModelAnswer:
    try:
        return ModelAnswer.model_validate(json.loads(generation.text))
    except (json.JSONDecodeError, ValidationError, TypeError) as error:
        raise InvalidAnswer(
            f"output did not match the answer schema: {type(error).__name__}"
        ) from error


def finalize(generation: Generation, context: Context) -> FinalAnswer:
    """Validate the model's output against `context` and produce what will be stored."""
    if generation.refused:
        return not_found("refused")

    answer = parse(generation)
    if not answer.found_answer:
        return not_found("not_in_documents")

    text = answer.answer.strip()
    if not text:
        raise InvalidAnswer("found_answer was true but the answer was empty")
    if leaks_instructions(text, context.boundary):
        logger.warning("An answer repeated the instructions; it was replaced")
        return not_found("instruction_leak")

    sources = context.by_id()
    numbers: dict[str, int] = {}
    dropped: set[str] = set()

    def number_for(source_id: str) -> int | None:
        if source_id not in sources:
            dropped.add(source_id)
            return None
        if source_id not in numbers:
            numbers[source_id] = len(numbers) + 1
        return numbers[source_id]

    def rewrite(marker: re.Match[str]) -> str:
        found = [number_for(source_id) for source_id in SOURCE_ID.findall(marker.group(2))]
        valid = sorted({number for number in found if number is not None})
        if not valid:
            return ""  # also drops the space before it
        return f"{marker.group(1)}[{', '.join(str(number) for number in valid)}]"

    content = MARKER.sub(rewrite, text).strip()

    # Sources cited with a quote but never marked in the text still count, after the others.
    quotes: dict[str, list[str]] = {}
    for cited in answer.citations:
        source_id = cited.source_id.strip()
        if number_for(source_id) is not None:
            quotes.setdefault(source_id, []).append(cited.quote)

    if dropped:
        logger.warning("Dropped %d citation(s) to sources that were not supplied", len(dropped))
    if not numbers:
        return not_found("no_valid_citations", dropped=len(dropped))

    built: list[dict[str, Any]] = []
    unverified = 0
    for source_id, number in sorted(numbers.items(), key=lambda item: item[1]):
        excerpt, verified = excerpt_for(sources[source_id], quotes.get(source_id, []))
        unverified += not verified
        built.append(citation(number, sources[source_id], excerpt))

    return FinalAnswer(
        content=content,
        citations=built,
        grounded=True,
        dropped_citations=len(dropped),
        unverified_quotes=unverified,
    )
