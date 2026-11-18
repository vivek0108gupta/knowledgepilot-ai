"""Checks the model's answer against the evidence it was given, and builds the citations.

The model cites sources by the short ids it was shown (S1, S2...). Nothing it returns is
trusted: every id is looked up in that request's own context, and every citation is built
from the stored chunk, never from what the model wrote. An id that was not supplied is
dropped. An answer left with no valid citation is treated as unsupported and replaced.

Every citation shown has a marker in the answer text, and every marker has a citation.
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
    # Quotes located from a long run of their words rather than in full.
    approximate_quotes: int = 0
    # Valid sources the model listed but never marked in a text that marks others.
    unmarked_citations: int = 0
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
SOFT_HYPHEN = "\u00ad"
HYPHEN_BREAK = re.compile(r"[ \t]*\r?\n\s*(?=\w)")
# A quote that is not in the passage word for word is still located from its longest run of
# consecutive words that is, if the run is at least this long.
MIN_RUN_WORDS = 6


def comparable(text: str) -> tuple[str, list[int]]:
    """Text reduced to lower-case letters and digits separated by single spaces, so quotes
    match despite differences in case, punctuation, dashes, quote marks or ligatures.
    Also returns, for each kept character, its position in the original text."""
    out: list[str] = []
    positions: list[int] = []
    gap = True
    skip_to = 0
    for position, character in enumerate(text):
        if position < skip_to or character == SOFT_HYPHEN:
            continue
        if character in "-‐" and out and not gap:
            # "environ-\nment", as PDF text often has it, compares as "environment".
            rest = HYPHEN_BREAK.match(text, position + 1)
            if rest is not None:
                skip_to = rest.end()
                continue
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


def longest_run(quote: str, passage: str) -> tuple[int, int] | None:
    """The span of the longest run of the quote's consecutive words found, in the same order,
    in the passage: for quotes the model shortened, merged or slightly reworded."""
    quote_words = comparable(quote)[0].split()
    haystack, positions = comparable(passage)
    words = haystack.split(" ") if haystack else []
    # Character offset of each passage word in `haystack`.
    offsets: list[int] = []
    cursor = 0
    for word in words:
        offsets.append(cursor)
        cursor += len(word) + 1
    best_length = best_end = 0
    previous = [0] * (len(words) + 1)
    for quote_word in quote_words:
        current = [0] * (len(words) + 1)
        for index, word in enumerate(words, start=1):
            if word == quote_word:
                current[index] = previous[index - 1] + 1
                if current[index] > best_length:
                    best_length, best_end = current[index], index
        previous = current
    if best_length < MIN_RUN_WORDS:
        return None
    first_word, last_word = best_end - best_length, best_end - 1
    start = offsets[first_word]
    end = offsets[last_word] + len(words[last_word]) - 1
    return positions[start], positions[end] + 1


def excerpt_for(source: Source, quotes: list[str]) -> tuple[str, str]:
    """A short excerpt of the source, and how it was found: "exact" (the quote is in the
    passage, allowing for case, punctuation and layout), "approximate" (a long run of the
    quote's words is), "unverified" (the quote could not be found) or "none" (no quote).

    The model's quote only says where to look: the excerpt is always the document's own text
    at that place. A quote that cannot be found is ignored, and the start of the passage is
    shown instead.
    """
    passage = source.match.content
    found: tuple[tuple[int, int], str] | None = None
    for quote in quotes:
        span = locate(quote, passage)
        if span is not None:
            found = (span, "exact")
            break
    if found is None:
        for quote in quotes:
            span = longest_run(quote, passage)
            if span is not None:
                found = (span, "approximate")
                break
    if found is None:
        return fallback_excerpt(passage), "unverified" if quotes else "none"
    (start, end), how = found
    text = " ".join(passage[start:end].split())
    if len(text) > MAX_EXCERPT_LENGTH:
        text = text[: MAX_EXCERPT_LENGTH - 1].rstrip() + "…"
    return text, how


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
    marked = bool(numbers)

    quotes: dict[str, list[str]] = {}
    unmarked: set[str] = set()
    for cited in answer.citations:
        source_id = cited.source_id.strip()
        if source_id not in sources:
            dropped.add(source_id)
        elif source_id in numbers or not marked:
            # When the text marks no source at all, the listed sources are marked at the end
            # of the answer instead, so that every citation still has a marker.
            number_for(source_id)
            quotes.setdefault(source_id, []).append(cited.quote)
        else:
            # Listed but not tied to anything the answer says: not shown.
            unmarked.add(source_id)

    if dropped:
        logger.warning("Dropped %d citation(s) to sources that were not supplied", len(dropped))
    if not numbers:
        return not_found("no_valid_citations", dropped=len(dropped))
    if not marked:
        content = f"{content} [{', '.join(str(number) for number in sorted(numbers.values()))}]"

    built: list[dict[str, Any]] = []
    unverified = approximate = 0
    for source_id, number in sorted(numbers.items(), key=lambda item: item[1]):
        excerpt, how = excerpt_for(sources[source_id], quotes.get(source_id, []))
        unverified += how == "unverified"
        approximate += how == "approximate"
        built.append(citation(number, sources[source_id], excerpt))

    return FinalAnswer(
        content=content,
        citations=built,
        grounded=True,
        dropped_citations=len(dropped),
        unverified_quotes=unverified,
        approximate_quotes=approximate,
        unmarked_citations=len(unmarked),
    )
