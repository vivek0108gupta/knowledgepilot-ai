from pathlib import Path

import pytest

from app.models.document import SegmentKind
from app.services.extraction import ExtractionError, extract
from app.services.file_validation import FileKind
from tests.files import make_docx, make_encrypted_pdf, make_pdf


def write(tmp_path: Path, name: str, content: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(content)
    return path


class TestPdf:
    def test_extracts_each_page_with_its_number(self, tmp_path: Path) -> None:
        path = write(tmp_path, "a.pdf", make_pdf(["Alpha page", "Beta page", "Gamma page"]))

        result = extract(FileKind.PDF, path)

        assert result.page_count == 3
        assert [(s.kind, s.page_number, s.content) for s in result.segments] == [
            (SegmentKind.PAGE, 1, "Alpha page"),
            (SegmentKind.PAGE, 2, "Beta page"),
            (SegmentKind.PAGE, 3, "Gamma page"),
        ]
        assert result.char_count == len("Alpha page") + len("Beta page") + len("Gamma page")

    def test_a_blank_page_is_skipped_without_shifting_later_page_numbers(
        self, tmp_path: Path
    ) -> None:
        path = write(tmp_path, "a.pdf", make_pdf(["First", "", "Third"]))

        result = extract(FileKind.PDF, path)

        assert result.page_count == 3
        assert [(s.page_number, s.content) for s in result.segments] == [(1, "First"), (3, "Third")]

    def test_a_pdf_with_no_text_is_reported_as_a_scan_needing_ocr(self, tmp_path: Path) -> None:
        path = write(tmp_path, "scan.pdf", make_pdf(["", ""]))

        with pytest.raises(ExtractionError, match=r"scan.*OCR.*not supported"):
            extract(FileKind.PDF, path)

    def test_a_password_protected_pdf_is_explained(self, tmp_path: Path) -> None:
        path = write(tmp_path, "locked.pdf", make_encrypted_pdf())

        with pytest.raises(ExtractionError, match="password-protected"):
            extract(FileKind.PDF, path)

    @pytest.mark.parametrize(
        "content",
        [b"%PDF-1.4\nthis is not really a pdf", make_pdf(["Hello"])[:120], b"%PDF-1.4\n%%EOF"],
        ids=["garbage after header", "truncated", "header only"],
    )
    def test_a_damaged_pdf_fails_with_a_clear_message(self, tmp_path: Path, content: bytes) -> None:
        path = write(tmp_path, "broken.pdf", content)

        with pytest.raises(ExtractionError, match=r"damaged|No text"):
            extract(FileKind.PDF, path)


class TestDocx:
    def test_extracts_paragraphs_in_order_with_their_index(self, tmp_path: Path) -> None:
        path = write(tmp_path, "a.docx", make_docx())

        result = extract(FileKind.DOCX, path)

        assert result.page_count is None
        assert [(s.kind, s.paragraph_index, s.content) for s in result.segments] == [
            (SegmentKind.PARAGRAPH, 0, "Introduction"),
            (SegmentKind.PARAGRAPH, 1, "First paragraph."),
            (SegmentKind.PARAGRAPH, 2, "Second paragraph."),
        ]

    def test_each_paragraph_knows_the_heading_it_sits_under(self, tmp_path: Path) -> None:
        def build(document) -> None:  # noqa: ANN001
            document.add_paragraph("Before any heading.")
            document.add_heading("Scope", level=1)
            document.add_paragraph("What is covered.")
            document.add_heading("Method", level=2)
            document.add_paragraph("How it was done.")

        result = extract(FileKind.DOCX, write(tmp_path, "a.docx", make_docx(build)))

        assert [(s.content, s.section) for s in result.segments] == [
            ("Before any heading.", None),
            ("Scope", "Scope"),
            ("What is covered.", "Scope"),
            ("Method", "Method"),
            ("How it was done.", "Method"),
        ]

    def test_empty_paragraphs_are_skipped_and_indices_stay_consecutive(
        self, tmp_path: Path
    ) -> None:
        def build(document) -> None:  # noqa: ANN001
            document.add_paragraph("One")
            document.add_paragraph("")
            document.add_paragraph("   ")
            document.add_paragraph("Two")

        result = extract(FileKind.DOCX, write(tmp_path, "a.docx", make_docx(build)))

        assert [(s.paragraph_index, s.content) for s in result.segments] == [(0, "One"), (1, "Two")]

    def test_table_text_is_kept_in_reading_order(self, tmp_path: Path) -> None:
        def build(document) -> None:  # noqa: ANN001
            document.add_paragraph("Before the table.")
            table = document.add_table(rows=2, cols=2)
            table.cell(0, 0).text, table.cell(0, 1).text = "Name", "Role"
            table.cell(1, 0).text, table.cell(1, 1).text = "Ada", "Owner"
            document.add_paragraph("After the table.")

        result = extract(FileKind.DOCX, write(tmp_path, "a.docx", make_docx(build)))

        assert [s.content for s in result.segments] == [
            "Before the table.",
            "Name | Role",
            "Ada | Owner",
            "After the table.",
        ]

    def test_a_document_with_no_text(self, tmp_path: Path) -> None:
        path = write(tmp_path, "empty.docx", make_docx(lambda document: None))

        with pytest.raises(ExtractionError, match="No text"):
            extract(FileKind.DOCX, path)

    def test_a_damaged_document(self, tmp_path: Path) -> None:
        path = write(tmp_path, "broken.docx", make_docx()[:300])

        with pytest.raises(ExtractionError, match="damaged"):
            extract(FileKind.DOCX, path)


class TestTxt:
    def test_blocks_are_split_at_blank_lines_and_keep_their_line_numbers(
        self, tmp_path: Path
    ) -> None:
        text = "Title\n\nFirst line\nSecond line\n\n\nLast block\n"

        result = extract(FileKind.TXT, write(tmp_path, "a.txt", text.encode()))

        assert result.page_count is None
        assert [(s.kind, s.line_start, s.line_end, s.content) for s in result.segments] == [
            (SegmentKind.LINES, 1, 1, "Title"),
            (SegmentKind.LINES, 3, 4, "First line\nSecond line"),
            (SegmentKind.LINES, 7, 7, "Last block"),
        ]

    def test_a_long_block_is_split_so_no_segment_is_unbounded(self, tmp_path: Path) -> None:
        text = "\n".join(f"line {number}" for number in range(1, 201))

        result = extract(FileKind.TXT, write(tmp_path, "a.txt", text.encode()))

        assert [(s.line_start, s.line_end) for s in result.segments] == [
            (1, 80),
            (81, 160),
            (161, 200),
        ]
        assert result.segments[1].content.startswith("line 81\n")

    def test_windows_line_endings_and_a_bom(self, tmp_path: Path) -> None:
        content = "﻿first\r\nsecond\r\n\r\nthird\r\n".encode()

        result = extract(FileKind.TXT, write(tmp_path, "a.txt", content))

        assert [(s.line_start, s.line_end, s.content) for s in result.segments] == [
            (1, 2, "first\nsecond"),
            (4, 4, "third"),
        ]

    def test_non_ascii_text_survives(self, tmp_path: Path) -> None:
        result = extract(FileKind.TXT, write(tmp_path, "a.txt", "naïve café 報告\n".encode()))

        assert result.segments[0].content == "naïve café 報告"

    def test_text_that_is_not_utf8(self, tmp_path: Path) -> None:
        path = write(tmp_path, "a.txt", "café".encode("latin-1"))

        with pytest.raises(ExtractionError, match="not valid UTF-8"):
            extract(FileKind.TXT, path)

    @pytest.mark.parametrize("content", [b"", b"\n\n\n", b"   \n\t\n"])
    def test_a_file_with_only_whitespace(self, tmp_path: Path, content: bytes) -> None:
        with pytest.raises(ExtractionError, match="no readable text"):
            extract(FileKind.TXT, write(tmp_path, "a.txt", content))
