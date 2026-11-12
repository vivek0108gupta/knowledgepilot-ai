"""Turns retrieved passages into the bounded block of evidence the model is shown.

Pure: no database, no network. Each passage keeps its full text and its source details, and
gets a short id (S1, S2...) that the model cites and that is checked afterwards.

Passages are wrapped in tags with a random suffix chosen per request. A document cannot know
that suffix, so text inside it cannot close the tags early or imitate a new passage.
"""

import html
import secrets
from dataclasses import dataclass

from app.services.chunking import Tokenizer, get_tokenizer
from app.services.search import Match


@dataclass(frozen=True, slots=True)
class Source:
    id: str
    match: Match
    location: str | None


@dataclass(frozen=True, slots=True)
class Context:
    sources: list[Source]
    text: str
    boundary: str
    tokens: int

    def by_id(self) -> dict[str, Source]:
        return {source.id: source for source in self.sources}


def location_label(match: Match) -> str | None:
    """Where a passage sits in its document, from the references extraction recorded.

    Only what the format provides: a Word document has no page numbers, so none is given.
    """
    if match.page_number is not None:
        return f"Page {match.page_number}"
    if match.paragraph_index is not None:
        return f"Paragraph {match.paragraph_index + 1}"
    if match.line_start is not None:
        if match.line_end is not None and match.line_end != match.line_start:
            return f"Lines {match.line_start}-{match.line_end}"
        return f"Line {match.line_start}"
    return None


def render_source(source_id: str, match: Match, location: str | None, boundary: str) -> str:
    attributes = f'id="{source_id}" document="{html.escape(match.document_name, quote=True)}"'
    if location:
        attributes += f' location="{location}"'
    # The text is kept exactly as stored; only the (astronomically unlikely) appearance of
    # this request's own boundary is removed, so the tags stay unambiguous.
    content = match.content.replace(boundary, "")
    return f"<source-{boundary} {attributes}>\n{content}\n</source-{boundary}>"


def build_context(
    matches: list[Match],
    *,
    token_budget: int,
    max_per_document: int,
    tokenizer: Tokenizer | None = None,
    boundary: str | None = None,
) -> Context:
    """Choose passages, most similar first, until the token budget is used.

    No single document may take more than `max_per_document` places while passages from other
    documents are waiting; if budget is left after that, the held-back passages are used too,
    so a workspace with one document still gets all of its relevant passages.
    A passage that does not fit is skipped whole, never cut.
    """
    if token_budget < 1 or max_per_document < 1:
        raise ValueError("token budget and per-document limit must be positive")
    tokenizer = tokenizer or get_tokenizer()
    boundary = boundary or secrets.token_hex(8)

    ordered = sorted(matches, key=lambda match: match.similarity, reverse=True)
    costs = {
        match.chunk_id: len(
            tokenizer.encode(render_source("S00", match, location_label(match), boundary))
        )
        for match in ordered
    }

    chosen: list[Match] = []
    held_back: list[Match] = []
    used = 0
    per_document: dict[object, int] = {}

    for match in ordered:
        if costs[match.chunk_id] > token_budget - used:
            continue
        if per_document.get(match.document_id, 0) >= max_per_document:
            held_back.append(match)
            continue
        chosen.append(match)
        used += costs[match.chunk_id]
        per_document[match.document_id] = per_document.get(match.document_id, 0) + 1

    for match in held_back:
        if costs[match.chunk_id] <= token_budget - used:
            chosen.append(match)
            used += costs[match.chunk_id]

    chosen.sort(key=lambda match: match.similarity, reverse=True)
    sources = [
        Source(id=f"S{number}", match=match, location=location_label(match))
        for number, match in enumerate(chosen, start=1)
    ]
    blocks = [render_source(s.id, s.match, s.location, boundary) for s in sources]
    text = f"<sources-{boundary}>\n" + "\n\n".join(blocks) + f"\n</sources-{boundary}>"
    return Context(sources=sources, text=text, boundary=boundary, tokens=used)
