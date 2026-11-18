import io

import pytest

from app.core.errors import AppError
from app.services import file_validation
from app.services.file_validation import (
    FileKind,
    sanitize_filename,
    storage_filename,
    validate_upload,
)
from tests.files import DOCX_MIME, EXE_BYTES, PDF_MIME, TXT_MIME, make_docx, make_pdf, make_zip

PDF = make_pdf(["Hello"])
DOCX = make_docx()
TXT = b"Plain text.\n"


def validate(name: str, content: bytes, declared: str | None = None):  # noqa: ANN201
    return validate_upload(name, declared, io.BytesIO(content))


def rejection(name: str, content: bytes, declared: str | None = None) -> AppError:
    with pytest.raises(AppError) as caught:
        validate(name, content, declared)
    return caught.value


class TestAcceptedFiles:
    @pytest.mark.parametrize(
        ("name", "content", "kind", "mime"),
        [
            ("report.pdf", PDF, FileKind.PDF, PDF_MIME),
            ("notes.docx", DOCX, FileKind.DOCX, DOCX_MIME),
            ("readme.txt", TXT, FileKind.TXT, TXT_MIME),
        ],
    )
    def test_each_supported_format(
        self, name: str, content: bytes, kind: FileKind, mime: str
    ) -> None:
        result = validate(name, content, mime)

        assert (result.kind, result.mime_type, result.size_bytes) == (kind, mime, len(content))
        assert len(result.sha256) == 64

    def test_extension_is_case_insensitive(self) -> None:
        assert validate("REPORT.PDF", PDF).kind is FileKind.PDF

    def test_mime_type_comes_from_the_content_not_the_request(self) -> None:
        # A browser that does not know the type sends a generic one; it is not copied.
        assert validate("report.pdf", PDF, "application/octet-stream").mime_type == PDF_MIME
        assert validate("report.pdf", PDF, None).mime_type == PDF_MIME

    def test_text_with_a_charset_parameter_and_a_bom(self) -> None:
        content = "\ufeffcafé\n".encode()
        assert validate("a.txt", content, "text/plain; charset=utf-8").kind is FileKind.TXT

    def test_file_is_left_at_its_start_for_storing(self) -> None:
        file = io.BytesIO(PDF)
        validate_upload("a.pdf", None, file)
        assert file.tell() == 0

    def test_same_content_has_the_same_hash_whatever_it_is_called(self) -> None:
        assert validate("a.pdf", PDF).sha256 == validate("b.pdf", PDF).sha256


class TestRejectedExtensions:
    @pytest.mark.parametrize(
        "name",
        [
            "malware.exe",
            "script.sh",
            "page.html",
            "macro.docm",
            "old.doc",
            "image.png",
            "archive.zip",
            "report.pdf.exe",
            "noextension",
            "trailingdot.",
        ],
    )
    def test_unsupported_or_malicious_extension(self, name: str) -> None:
        error = rejection(name, PDF, PDF_MIME)

        assert (error.status_code, error.code) == (415, "unsupported_file_type")


class TestSpoofing:
    def test_executable_renamed_to_pdf(self) -> None:
        error = rejection("invoice.pdf", EXE_BYTES, PDF_MIME)
        assert (error.status_code, error.code) == (415, "content_mismatch")

    def test_executable_renamed_to_docx(self) -> None:
        assert rejection("invoice.docx", EXE_BYTES, DOCX_MIME).code == "content_mismatch"

    def test_executable_renamed_to_txt(self) -> None:
        assert rejection("notes.txt", EXE_BYTES, TXT_MIME).code == "content_mismatch"

    def test_pdf_renamed_to_docx(self) -> None:
        assert rejection("report.docx", PDF, DOCX_MIME).code == "content_mismatch"

    def test_plain_zip_renamed_to_docx(self) -> None:
        archive = make_zip({"payload.bin": b"x" * 100})
        assert rejection("report.docx", archive, DOCX_MIME).code == "content_mismatch"

    def test_truncated_zip_renamed_to_docx(self) -> None:
        assert rejection("report.docx", DOCX[:200], DOCX_MIME).code == "content_mismatch"

    def test_html_renamed_to_pdf(self) -> None:
        html = b"<html><script>alert(1)</script></html>"
        assert rejection("page.pdf", html, PDF_MIME).code == "content_mismatch"

    @pytest.mark.parametrize(
        ("name", "content", "declared"),
        [
            ("report.pdf", PDF, "text/html"),
            ("report.pdf", PDF, "application/x-msdownload"),
            ("report.pdf", PDF, DOCX_MIME),
            ("notes.txt", TXT, PDF_MIME),
            ("notes.docx", DOCX, "application/zip"),
        ],
    )
    def test_content_type_that_contradicts_the_file(
        self, name: str, content: bytes, declared: str
    ) -> None:
        error = rejection(name, content, declared)
        assert (error.status_code, error.code) == (415, "mime_mismatch")

    def test_right_content_type_does_not_rescue_a_wrong_extension(self) -> None:
        assert rejection("malware.exe", PDF, PDF_MIME).code == "unsupported_file_type"

    def test_text_that_is_not_utf8(self) -> None:
        error = rejection("legacy.txt", "café".encode("latin-1"), TXT_MIME)
        assert error.code == "invalid_text_encoding"

    def test_text_cut_in_the_middle_of_a_character(self) -> None:
        assert rejection("cut.txt", "café".encode()[:-1]).code == "invalid_text_encoding"


class TestSizeLimits:
    def test_empty_file(self) -> None:
        error = rejection("empty.pdf", b"", PDF_MIME)
        assert (error.status_code, error.code) == (400, "empty_file")

    def test_file_of_exactly_the_limit_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(file_validation, "MAX_DOCUMENT_BYTES", len(TXT))
        assert validate("a.txt", TXT).size_bytes == len(TXT)

    def test_file_one_byte_over_the_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(file_validation, "MAX_DOCUMENT_BYTES", len(TXT) - 1)
        error = rejection("a.txt", TXT)
        assert (error.status_code, error.code) == (413, "file_too_large")

    def test_real_file_over_25_mb(self) -> None:
        oversized = b"a" * (25 * 1024 * 1024 + 1)
        assert rejection("big.txt", oversized).code == "file_too_large"

    def test_docx_that_unpacks_to_far_more_than_it_claims(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bomb = make_zip(
            {
                "[Content_Types].xml": b"<Types/>",
                "word/document.xml": b"0" * 5_000_000,  # compresses to a few kilobytes
            }
        )
        assert len(bomb) < 50_000
        monkeypatch.setattr(file_validation, "MAX_DOCX_UNCOMPRESSED_BYTES", 1_000_000)

        assert rejection("bomb.docx", bomb, DOCX_MIME).code == "file_too_complex"


class TestFilenameSanitization:
    @pytest.mark.parametrize(
        ("raw", "clean"),
        [
            ("report.pdf", "report.pdf"),
            ("../../etc/passwd.txt", "passwd.txt"),
            ("..\\..\\windows\\system32\\evil.pdf", "evil.pdf"),
            ("/absolute/path/file.docx", "file.docx"),
            ("C:\\Users\\ada\\Desktop\\notes.txt", "notes.txt"),
            ("  spaced   out  .pdf", "spaced out .pdf"),
            ("null\x00byte.pdf", "nullbyte.pdf"),
            ("line\r\nbreak.pdf", "linebreak.pdf"),
            ("right\u202eto-left.pdf", "rightto-left.pdf"),
            ('quo"te<tag>.pdf', "quo_te_tag_.pdf"),
            ("semi;colon&amp.pdf", "semi_colon_amp.pdf"),
            ("résumé.pdf", "résumé.pdf"),
            ("報告書.pdf", "報告書.pdf"),
            ("...hidden.pdf", "hidden.pdf"),
        ],
    )
    def test_cleans_the_name(self, raw: str, clean: str) -> None:
        assert sanitize_filename(raw) == clean

    @pytest.mark.parametrize("raw", ["", "   ", ".", "..", "../", "\\", "\x00\x01", None])
    def test_rejects_a_name_with_nothing_left(self, raw: str | None) -> None:
        with pytest.raises(AppError) as caught:
            sanitize_filename(raw)
        assert caught.value.code == "invalid_filename"

    def test_long_names_are_shortened_but_keep_their_extension(self) -> None:
        name = sanitize_filename("a" * 300 + ".pdf")

        assert len(name) == 120
        assert name.endswith(".pdf")

    def test_sanitised_name_never_contains_a_path_separator(self) -> None:
        for raw in ["a/b.pdf", "a\\b.pdf", "a/../b.pdf", "..//x.pdf", "a\u2215b.pdf"]:
            clean = sanitize_filename(raw)
            assert "/" not in clean and "\\" not in clean and ".." not in clean

    @pytest.mark.parametrize(
        ("filename", "kind", "expected"),
        [
            ("Quarterly Report (final).pdf", FileKind.PDF, "Quarterly-Report-final.pdf"),
            ("résumé.docx", FileKind.DOCX, "resume.docx"),
            ("報告書.pdf", FileKind.PDF, "document.pdf"),
            ("REPORT.PDF", FileKind.PDF, "REPORT.pdf"),
            ("a" * 200 + ".txt", FileKind.TXT, "a" * 80 + ".txt"),
        ],
    )
    def test_storage_name_is_ascii_and_ends_in_the_verified_extension(
        self, filename: str, kind: FileKind, expected: str
    ) -> None:
        name = storage_filename(filename, kind)

        assert name == expected
        assert name.isascii()
