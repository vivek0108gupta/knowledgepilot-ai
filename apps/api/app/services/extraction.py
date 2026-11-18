"""Turns a stored file into text segments that remember where they came from.

Pure and blocking: no database, no storage, no network. The processing pipeline runs these
functions in a worker thread and saves what they return.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

from docx import Document as open_docx
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from app.models.document import SegmentKind
from app.services.file_validation import FileKind

logger = logging.getLogger(__name__)

MAX_PDF_PAGES = 2_000
MAX_SEGMENTS = 50_000
# A text file is split into blocks at blank lines; a block longer than this is split again
# so no single segment grows without bound.
MAX_LINES_PER_SEGMENT = 80
MAX_SECTION_LENGTH = 255


class ExtractionError(Exception):
    """Extraction failed for a reason worth telling the uploader. The message is shown to them."""


@dataclass(frozen=True, slots=True)
class Segment:
    kind: SegmentKind
    content: str
    page_number: int | None = None
    paragraph_index: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    section: str | None = None


@dataclass(frozen=True, slots=True)
class Extraction:
    segments: list[Segment] = field(default_factory=list)
    # Pages for a PDF; null for formats that have no fixed pages.
    page_count: int | None = None

    @property
    def char_count(self) -> int:
        return sum(len(segment.content) for segment in self.segments)


def clean(text: str) -> str:
    # PostgreSQL text columns cannot hold NUL, and some PDFs emit it.
    return text.replace("\x00", "").strip()


def extract_pdf(path: Path) -> Extraction:
    try:
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise ExtractionError(
                "This PDF is password-protected. Remove the password and upload it again."
            )
        page_count = len(reader.pages)
        if page_count > MAX_PDF_PAGES:
            raise ExtractionError(f"This PDF has more than {MAX_PDF_PAGES:,} pages.")

        segments: list[Segment] = []
        for number, page in enumerate(reader.pages, start=1):
            text = clean(page.extract_text() or "")
            # Pages with no text (blank, or an image) are skipped but still counted, so the
            # page numbers of the pages that follow stay correct.
            if text:
                segments.append(Segment(SegmentKind.PAGE, text, page_number=number))
    except ExtractionError:
        raise
    except (PyPdfError, ValueError, KeyError, OSError, RecursionError) as error:
        logger.info("PDF could not be read: %s", type(error).__name__)
        raise ExtractionError("This PDF is damaged or not a valid PDF file.") from error

    if not segments:
        raise ExtractionError(
            "No text could be found in this PDF. It looks like a scan or contains only "
            "images, and text recognition (OCR) is not supported."
        )
    return Extraction(segments, page_count=page_count)


def extract_docx(path: Path) -> Extraction:
    try:
        document = open_docx(str(path))
        segments: list[Segment] = []
        section: str | None = None
        index = 0

        def add(text: str) -> None:
            nonlocal index
            segments.append(
                Segment(SegmentKind.PARAGRAPH, text, paragraph_index=index, section=section)
            )
            index += 1

        # Walk the body in reading order, so text inside tables is not lost or reordered.
        for block in document.iter_inner_content():
            if len(segments) >= MAX_SEGMENTS:
                break
            if isinstance(block, Paragraph):
                text = clean(block.text)
                if not text:
                    continue
                style = (block.style.name or "") if block.style is not None else ""
                if style.startswith(("Heading", "Title")):
                    section = text[:MAX_SECTION_LENGTH]
                add(text)
            elif isinstance(block, Table):
                for row in block.rows:
                    cells = [clean(cell.text) for cell in row.cells]
                    # Merged cells repeat; keep each distinct value once, in order.
                    text = " | ".join(dict.fromkeys(cell for cell in cells if cell))
                    if text:
                        add(text)
    except ExtractionError:
        raise
    except Exception as error:  # python-docx raises many unrelated types for bad files
        logger.info("DOCX could not be read: %s", type(error).__name__)
        raise ExtractionError("This Word document is damaged or could not be read.") from error

    if not segments:
        raise ExtractionError("No text could be found in this Word document.")
    return Extraction(segments)


def extract_txt(path: Path) -> Extraction:
    try:
        # utf-8-sig drops a leading byte-order mark if there is one.
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as error:
        raise ExtractionError("This text file is not valid UTF-8.") from error

    segments: list[Segment] = []
    block: list[str] = []
    block_start = 0

    def flush(end_line: int) -> None:
        content = clean("\n".join(block))
        if content:
            segments.append(
                Segment(SegmentKind.LINES, content, line_start=block_start, line_end=end_line)
            )
        block.clear()

    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            if block:
                flush(number - 1)
            continue
        if not block:
            block_start = number
        block.append(line.replace("\x00", ""))
        if len(block) >= MAX_LINES_PER_SEGMENT:
            flush(number)
    if block:
        flush(block_start + len(block) - 1)

    if not segments:
        raise ExtractionError("This text file has no readable text.")
    if len(segments) > MAX_SEGMENTS:
        raise ExtractionError("This text file is too long to process.")
    return Extraction(segments)


EXTRACTORS = {FileKind.PDF: extract_pdf, FileKind.DOCX: extract_docx, FileKind.TXT: extract_txt}


def extract(kind: FileKind, path: Path) -> Extraction:
    """Extract text from the file at `path`. Blocking: call it from a worker thread."""
    return EXTRACTORS[kind](path)
